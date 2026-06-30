# -*- coding: utf-8 -*-
"""
Modrinth API 연동 모듈 (v2)
https://docs.modrinth.com/api/

변경 이력
---------
v2  get_project_info, install_mod_by_slug 추가
    ModRegistry 클래스 추가
"""

import os
import json
import hashlib
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime

API_BASE = "https://api.modrinth.com/v2"
USER_AGENT = "dongleland-mod-installer/2.0 (contact: garamisme)"

DEFAULT_GAME_VERSION = "26.1.2"
DEFAULT_LOADER = "fabric"
# None 이면 release→beta→alpha 우선순위 폴백 사용 (특정 채널 강제 시 튜플 지정)
DEFAULT_VERSION_TYPES = None

ALLOWED_DOWNLOAD_HOSTS = ("cdn.modrinth.com",)


# ── 예외 ────────────────────────────────────────────────────────────────────

class DownloadSecurityError(Exception):
    """URL / 파일명 / 무결성 검증 실패"""


# ── 보안 헬퍼 ───────────────────────────────────────────────────────────────

def _validate_download_url(url: str):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise DownloadSecurityError(f"안전하지 않은 URL 스킴: {parsed.scheme!r}")
    if parsed.hostname not in ALLOWED_DOWNLOAD_HOSTS:
        raise DownloadSecurityError(f"허용되지 않은 다운로드 호스트: {parsed.hostname!r}")


def _sanitize_filename(filename: str) -> str:
    if not filename:
        raise DownloadSecurityError("파일명이 비어 있습니다")
    base = os.path.basename(filename)
    if base != filename or base in ("", ".", ".."):
        raise DownloadSecurityError(f"안전하지 않은 파일명: {filename!r}")
    if not base.lower().endswith(".jar"):
        raise DownloadSecurityError(f".jar 파일이 아닙니다: {filename!r}")
    return base


# ── 저수준 HTTP ──────────────────────────────────────────────────────────────

def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def sha1_of_file(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def download_file(url: str, dest_path: str, progress_cb=None, expected_sha1: str | None = None):
    """파일 다운로드 + 무결성 검증.

    - URL 호스트 검증 (cdn.modrinth.com 만 허용)
    - 다운로드 중 sha1 계산 → expected_sha1 과 불일치 시 임시파일 삭제 후 예외
    - .part 임시 파일 사용 → os.replace() 원자적 교체
    """
    _validate_download_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp_path = dest_path + ".part"
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            hasher = hashlib.sha1()
            with open(tmp_path, "wb") as out:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if progress_cb and total:
                        progress_cb(min(100, int(downloaded * 100 / total)))

        if expected_sha1 and hasher.hexdigest() != expected_sha1:
            raise DownloadSecurityError(
                f"해시 불일치 — 기대: {expected_sha1[:12]}... 실제: {hasher.hexdigest()[:12]}..."
            )
    except Exception:
        if os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise

    os.replace(tmp_path, dest_path)


# ── Modrinth API 호출 ────────────────────────────────────────────────────────

def get_version_from_hash(file_hash: str, algorithm: str = "sha1"):
    """파일 해시로 Modrinth 버전 정보 조회. 미등록 파일이면 None 반환."""
    url = f"{API_BASE}/version_file/{file_hash}?algorithm={algorithm}"
    try:
        return _get_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def get_project_versions(project_id: str, game_version: str, loader: str) -> list:
    """게임버전 / 로더 조건에 맞는 버전 목록 반환."""
    params = {
        "loaders": json.dumps([loader]),
        "game_versions": json.dumps([game_version]),
    }
    url = f"{API_BASE}/project/{project_id}/version?{urllib.parse.urlencode(params)}"
    try:
        return _get_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise


def _parse_date(version: dict) -> datetime:
    raw = version.get("date_published", "")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return datetime.min


def get_latest_compatible_version(
    project_id: str,
    game_version: str = DEFAULT_GAME_VERSION,
    loader: str = DEFAULT_LOADER,
    version_types: tuple = DEFAULT_VERSION_TYPES,
) -> dict | None:
    """game_version / loader 조건에 맞는 가장 최신 버전 반환.

    버전 채널 우선순위 (channel fallback):
      1) release 가 있으면 release 중 최신
      2) release 없으면 beta 중 최신
      3) beta 도 없으면 alpha 중 최신

    version_types 인자로 명시적 채널 제한도 가능하지만,
    기본 동작은 위 우선순위 폴백이다.
    """
    versions = get_project_versions(project_id, game_version, loader)
    if not versions:
        return None

    # version_types 가 명시적으로 지정되면 해당 채널만 사용 (특정 채널 강제)
    if version_types:
        filtered = [v for v in versions if v.get("version_type") in version_types]
        if not filtered:
            return None
        filtered.sort(key=_parse_date, reverse=True)
        return filtered[0]

    # 기본: release → beta → alpha 우선순위 폴백
    for channel in ("release", "beta", "alpha"):
        channel_versions = [v for v in versions if v.get("version_type") == channel]
        if channel_versions:
            channel_versions.sort(key=_parse_date, reverse=True)
            return channel_versions[0]

    # version_type 정보가 없는 경우 전체 중 최신
    versions.sort(key=_parse_date, reverse=True)
    return versions[0]


def pick_primary_file(version: dict) -> dict | None:
    files = version.get("files", [])
    for f in files:
        if f.get("primary"):
            return f
    return files[0] if files else None


def get_project_info(slug_or_id: str) -> dict | None:
    """프로젝트 기본 정보 조회 (title, description, icon_url 등).
    실패 시 None 반환."""
    url = f"{API_BASE}/project/{slug_or_id}"
    try:
        return _get_json(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def get_projects_batch(slugs: list) -> dict:
    """slug 목록을 단 1번의 API 호출로 조회 → {slug: project_id} 맵 반환.

    Modrinth GET /v2/projects?ids=["slug1","slug2",...] 사용.
    실패한 slug 는 결과에서 제외된다.
    """
    if not slugs:
        return {}
    ids_param = urllib.parse.quote(json.dumps(slugs))
    url = f"{API_BASE}/projects?ids={ids_param}"
    try:
        data = _get_json(url)
        return {item["slug"]: item["id"] for item in data if "slug" in item and "id" in item}
    except Exception:
        return {}


def get_project_members(slug_or_id: str) -> list:
    """프로젝트 팀 멤버(제작자) 목록 조회. 실패 시 빈 리스트 반환."""
    url = f"{API_BASE}/project/{slug_or_id}/members"
    try:
        return _get_json(url)
    except urllib.error.HTTPError:
        return []


def get_project_author(slug_or_id: str) -> str:
    """제작자 이름을 문자열로 반환. 대표 멤버(Owner) 우선, 없으면 첫 번째 멤버."""
    members = get_project_members(slug_or_id)
    if not members:
        return "알 수 없음"
    owners = [m for m in members if m.get("role", "").lower() == "owner"]
    target = owners[0] if owners else members[0]
    return target.get("user", {}).get("username", "알 수 없음")


def install_mod_by_slug(
    slug: str,
    mods_dir: str,
    game_version: str = DEFAULT_GAME_VERSION,
    loader: str = DEFAULT_LOADER,
    version_types: tuple = DEFAULT_VERSION_TYPES,
    progress_cb=None,
) -> dict:
    """Modrinth에서 최신 호환 버전을 mods_dir 에 다운로드.

    반환값:
        status  "installed" | "up_to_date" | "no_version" | "error"
        filename, version, project_id  (status 가 installed / up_to_date 일 때)
        message  (status 가 error / no_version 일 때)
    """
    latest = get_latest_compatible_version(slug, game_version, loader, version_types)
    if not latest:
        return {"status": "no_version", "message": f"{slug}: {game_version}/{loader} 호환 버전 없음"}

    primary = pick_primary_file(latest)
    if not primary:
        return {"status": "error", "message": f"{slug}: 다운로드 파일 없음"}

    try:
        filename = _sanitize_filename(primary["filename"])
    except DownloadSecurityError as e:
        return {"status": "error", "message": f"보안 검증 실패: {e}"}

    dest_path = os.path.join(mods_dir, filename)

    # 경로 이중 검증
    if not os.path.abspath(dest_path).startswith(os.path.abspath(mods_dir) + os.sep):
        return {"status": "error", "message": "비정상적인 대상 경로"}

    expected_sha1 = primary.get("hashes", {}).get("sha1")

    # 이미 동일 파일이 있으면 스킵
    if os.path.isfile(dest_path) and expected_sha1:
        if sha1_of_file(dest_path) == expected_sha1:
            return {
                "status": "up_to_date",
                "filename": filename,
                "version": latest.get("version_number", "?"),
                "project_id": latest.get("project_id"),
            }

    os.makedirs(mods_dir, exist_ok=True)

    try:
        download_file(primary["url"], dest_path, progress_cb=progress_cb, expected_sha1=expected_sha1)
    except DownloadSecurityError as e:
        return {"status": "error", "message": f"보안 검증 실패: {e}"}
    except Exception as e:
        return {"status": "error", "message": f"다운로드 실패: {e}"}

    return {
        "status": "installed",
        "filename": filename,
        "version": latest.get("version_number", "?"),
        "project_id": latest.get("project_id"),
    }


def sync_mod_file(
    path: str,
    game_version: str = DEFAULT_GAME_VERSION,
    loader: str = DEFAULT_LOADER,
    version_types: tuple = DEFAULT_VERSION_TYPES,
) -> dict:
    """로컬 jar 파일을 검사해 최신 버전이 있으면 교체.

    status: up_to_date | updated | unknown | no_compatible_version |
            no_matching_release | error
    """
    filename = os.path.basename(path)

    try:
        local_hash = sha1_of_file(path)
    except Exception as e:
        return {"file": filename, "status": "error", "message": f"파일 읽기 실패: {e}"}

    try:
        version_info = get_version_from_hash(local_hash)
    except Exception as e:
        return {"file": filename, "status": "error", "message": f"Modrinth 조회 실패: {e}"}

    if version_info is None:
        return {"file": filename, "status": "unknown", "message": "Modrinth 미등록 파일"}

    project_id = version_info["project_id"]
    current_version = version_info.get("version_number", "?")

    try:
        latest = get_latest_compatible_version(project_id, game_version, loader, version_types)
    except Exception as e:
        return {"file": filename, "status": "error", "message": f"최신 버전 조회 실패: {e}"}

    if latest is None:
        try:
            any_ver = get_latest_compatible_version(project_id, game_version, loader, None)
        except Exception:
            any_ver = None
        if version_types and any_ver:
            return {
                "file": filename,
                "status": "no_matching_release",
                "message": f"호환 버전 있지만 release 채널 아님 (베타/알파만 존재)",
            }
        return {"file": filename, "status": "no_compatible_version",
                "message": f"{game_version}/{loader} 호환 버전 없음"}

    primary = pick_primary_file(latest)
    if not primary:
        return {"file": filename, "status": "error", "message": "최신 버전에 파일 없음"}

    latest_hash = primary.get("hashes", {}).get("sha1")
    latest_version = latest.get("version_number", "?")

    if latest_hash == local_hash:
        return {"file": filename, "status": "up_to_date",
                "message": f"최신 버전 ({current_version})"}

    # 교체
    try:
        new_filename = _sanitize_filename(primary["filename"])
    except DownloadSecurityError as e:
        return {"file": filename, "status": "error", "message": f"보안 검증 실패: {e}"}

    directory = os.path.dirname(path)
    new_path = os.path.join(directory, new_filename)

    if os.path.commonpath([os.path.abspath(directory), os.path.abspath(new_path)]) \
            != os.path.abspath(directory):
        return {"file": filename, "status": "error", "message": "대상 경로가 mods 폴더를 벗어남"}

    try:
        download_file(primary["url"], new_path, expected_sha1=latest_hash)
    except DownloadSecurityError as e:
        return {"file": filename, "status": "error", "message": f"보안 검증 실패: {e}"}
    except Exception as e:
        return {"file": filename, "status": "error", "message": f"다운로드 실패: {e}"}

    if os.path.abspath(new_path) != os.path.abspath(path) and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            pass

    return {"file": filename, "status": "updated",
            "message": f"{current_version} → {latest_version} 업데이트 완료",
            "new_file": new_filename}


def sync_directory(
    directory: str,
    game_version: str = DEFAULT_GAME_VERSION,
    loader: str = DEFAULT_LOADER,
    version_types: tuple = DEFAULT_VERSION_TYPES,
    progress_cb=None,
) -> list:
    if not os.path.isdir(directory):
        return []
    jars = sorted(f for f in os.listdir(directory) if f.lower().endswith(".jar"))
    results = []
    for i, fname in enumerate(jars, 1):
        r = sync_mod_file(os.path.join(directory, fname), game_version, loader, version_types)
        results.append(r)
        if progress_cb:
            progress_cb(i, len(jars), r)
    return results


# ── 레지스트리 ────────────────────────────────────────────────────────────────

class ModRegistry:
    """설치된 모드 추적 파일 (.dongleland_registry.json).

    구조:
    {
      "version": 1,
      "mods": {
        "<catalog_id>": {
          "filename": "sodium-fabric-0.8.12.jar",
          "version":  "0.8.12",
          "project_id": "AANobbMI"   # None 이면 번들 모드
        }
      }
    }
    """

    REGISTRY_FILENAME = ".dongleland_registry.json"

    def __init__(self, mods_dir: str):
        self._mods_dir = mods_dir
        self._path = os.path.join(mods_dir, self.REGISTRY_FILENAME)
        self._data = self._load()

    # ── 내부 ──────────────────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "mods" not in data:
                raise ValueError
            return data
        except Exception:
            return {"version": 1, "mods": {}}

    def _save(self):
        try:
            os.makedirs(self._mods_dir, exist_ok=True)
            tmp = self._path + ".part"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            pass

    # ── 조회 ──────────────────────────────────────────────────────────────

    def get(self, mod_id: str) -> dict | None:
        """레지스트리 항목 반환. 없으면 None."""
        return self._data["mods"].get(mod_id)

    def is_installed(self, mod_id: str) -> bool:
        """레지스트리에 있고 실제 파일도 존재하면 True."""
        info = self.get(mod_id)
        if not info:
            return False
        return os.path.isfile(os.path.join(self._mods_dir, info["filename"]))

    def installed_ids(self) -> list[str]:
        """실제 파일이 존재하는 모드 id 목록."""
        result = []
        for mid, info in list(self._data["mods"].items()):
            if os.path.isfile(os.path.join(self._mods_dir, info["filename"])):
                result.append(mid)
            else:
                # 파일 없으면 레지스트리에서도 정리
                del self._data["mods"][mid]
        self._save()
        return result

    # ── 쓰기 ──────────────────────────────────────────────────────────────

    def record_install(
        self,
        mod_id: str,
        filename: str,
        version: str,
        project_id: str | None = None,
    ):
        self._data["mods"][mod_id] = {
            "filename": filename,
            "version": version,
            "project_id": project_id,
        }
        self._save()

    def record_remove(self, mod_id: str):
        self._data["mods"].pop(mod_id, None)
        self._save()

    def get_installed_version(self, mod_id: str) -> str | None:
        info = self.get(mod_id)
        return info["version"] if info else None

    def get_installed_filename(self, mod_id: str) -> str | None:
        info = self.get(mod_id)
        return info["filename"] if info else None
