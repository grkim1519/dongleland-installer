# -*- coding: utf-8 -*-
"""
preflight.py — 앱 실행 전 선행 조건 확인 및 설치 모듈

담당 역할
---------
1. 설정 파일 (config.json) 읽기/쓰기
2. 마인크래프트 설치 경로 탐색
3. Java 설치 여부 확인 + 미설치 시 Adoptium JRE 21 자동 다운로드/실행
4. Fabric 26.1.2 설치 여부 확인 + 미설치 시 fabric-installer 자동 다운로드/실행

GUI(mod_installer.py)는 이 모듈의 함수를 백그라운드 스레드에서 호출한다.
"""

import os
import sys
import json
import shutil
import hashlib
import tempfile
import subprocess
import urllib.request
import urllib.error
import urllib.parse

# ── 상수 ────────────────────────────────────────────────────────────────────

# Windows에서 콘솔창 깜빡임 방지 (감지/설치 호출에만 사용, 런처 실행엔 미적용)
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

GAME_VERSION = "26.1.2"
APP_NAME     = "dongleland_installer"
USER_AGENT   = "dongleland-mod-installer/2.0 (contact: garamisme)"

FABRIC_INSTALLER_URL  = (
    "https://maven.fabricmc.net/net/fabricmc/fabric-installer/"
    "1.1.1/fabric-installer-1.1.1.exe"
)
FABRIC_INSTALLER_HOST = "maven.fabricmc.net"

ADOPTIUM_API_URL = (
    "https://api.adoptium.net/v3/assets/latest/21/hotspot"
    "?os=windows&architecture=x64&image_type=jre&vendor=eclipse"
)
ADOPTIUM_HOST = "api.adoptium.net"

# Java 설치 여부 확인 시 탐색할 일반 경로
JAVA_COMMON_PATHS = [
    r"C:\Program Files\Java",
    r"C:\Program Files\Eclipse Adoptium",
    r"C:\Program Files\Microsoft",
    r"C:\Program Files\OpenJDK",
    r"C:\Program Files\BellSoft",
]

STARTUP_WARNING = (
    "본 프로그램은 마인크래프트 기본(Mojang) 런처 환경을 기준으로 합니다.\n"
    "mods 폴더가 생성되도록, 반드시 마인크래프트 26.1.2 버전을\n"
    "최소 1회 실행한 후 이 도구를 사용해주세요."
)


# ── 설정 파일 ────────────────────────────────────────────────────────────────

def _config_path() -> str:
    appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
    return os.path.join(appdata, APP_NAME, "config.json")


def _log_path() -> str:
    appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
    return os.path.join(appdata, APP_NAME, "log.txt")


def write_log(message: str):
    """로그 파일에 타임스탬프와 함께 한 줄 기록.

    %APPDATA%/dongleland_installer/log.txt 에 누적.
    실패해도 앱 동작에 영향을 주지 않도록 모든 예외를 무시한다.
    파일이 1MB 를 넘으면 비우고 새로 시작 (무한 증가 방지).
    """
    try:
        import datetime
        path = _log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 크기 제한 (1MB)
        try:
            if os.path.isfile(path) and os.path.getsize(path) > 1_000_000:
                os.remove(path)
        except OSError:
            pass
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:
        pass


def load_config() -> dict:
    """설정 파일 로드. 없거나 손상됐으면 기본값 반환."""
    defaults = {
        "minecraft_dir": "",
        "theme": "system",   # system | dark | light
    }
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        # 알 수 없는 키 무시, 누락된 키는 기본값으로 채움
        for k, v in defaults.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return dict(defaults)


def save_config(config: dict):
    """설정 파일 저장 (원자적)."""
    path = _config_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


# ── 마인크래프트 경로 ────────────────────────────────────────────────────────

def find_minecraft_dir(config: dict | None = None) -> str | None:
    """마인크래프트 설치 디렉터리 탐색.

    우선순위:
    1. config["minecraft_dir"] 에 저장된 경로 (사용자가 직접 지정한 경우)
    2. %APPDATA%\\.minecraft (기본 경로)
    """
    if config:
        saved = config.get("minecraft_dir", "")
        if saved and os.path.isdir(saved):
            return saved

    default = os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")),
        ".minecraft"
    )
    return default if os.path.isdir(default) else None


def get_mods_dir(minecraft_dir: str) -> str:
    return os.path.join(minecraft_dir, "mods")


def get_versions_dir(minecraft_dir: str) -> str:
    return os.path.join(minecraft_dir, "versions")


# ── Java ─────────────────────────────────────────────────────────────────────

def get_java_version_string() -> str | None:
    """java -version 실행 결과에서 버전 문자열 추출. 실패 시 None."""
    try:
        result = subprocess.run(
            ["java", "-version"],
            capture_output=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        # java -version 은 stderr 로 출력
        output = (result.stderr or result.stdout).decode("utf-8", errors="ignore")
        for line in output.splitlines():
            if "version" in line.lower():
                # 예: 'openjdk version "21.0.7" 2025-04-15'
                parts = line.strip().split('"')
                if len(parts) >= 2:
                    return parts[1]
        return output.splitlines()[0] if output else None
    except Exception:
        return None


def _find_java_in_common_paths() -> bool:
    """PATH 외의 일반 설치 폴더에서 java.exe 탐색."""
    for base in JAVA_COMMON_PATHS:
        if not os.path.isdir(base):
            continue
        try:
            for entry in os.listdir(base):
                java_exe = os.path.join(base, entry, "bin", "java.exe")
                if os.path.isfile(java_exe):
                    return True
        except OSError:
            continue
    return False


def is_java_installed() -> bool:
    """Java 설치 여부 확인 (PATH + 일반 경로 탐색)."""
    if get_java_version_string() is not None:
        return True
    return _find_java_in_common_paths()


def _get_java_installer_url() -> str | None:
    """Adoptium API에서 최신 JRE 21 Windows x64 MSI 설치 프로그램 URL 가져오기."""
    req = urllib.request.Request(ADOPTIUM_API_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for asset in data:
            binary = asset.get("binary", {})
            if binary.get("image_type") == "jre":
                installer = binary.get("installer")
                if installer and installer.get("link"):
                    return installer["link"]
    except Exception:
        pass
    return None


def download_java_installer(progress_cb=None) -> str:
    """Adoptium JRE 21 설치 프로그램을 임시 폴더에 다운로드 후 경로 반환.

    보안: HTTPS + 허용 호스트(api.adoptium.net, github.com) 검증
    """
    url = _get_java_installer_url()
    if not url:
        raise RuntimeError("Java 설치 프로그램 URL을 가져올 수 없습니다. 인터넷 연결을 확인해주세요.")

    parsed = urllib.parse.urlsplit(url)
    allowed = ("api.adoptium.net", "github.com", "objects.githubusercontent.com")
    if parsed.scheme != "https" or parsed.hostname not in allowed:
        raise RuntimeError(f"허용되지 않은 Java 다운로드 URL: {url}")

    tmp_dir = tempfile.mkdtemp(prefix="dongleland_java_")
    filename = os.path.basename(parsed.path) or "java-installer.msi"
    dest = os.path.join(tmp_dir, filename)

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp_path = dest + ".part"
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(tmp_path, "wb") as out:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb and total:
                        try:
                            progress_cb(min(100, int(downloaded * 100 / total)))
                        except Exception:
                            pass
        os.replace(tmp_path, dest)
    except Exception:
        if os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise

    return dest


def run_java_installer(installer_path: str):
    """Java 설치 프로그램 실행 후 완료 대기.

    .msi 파일이면 msiexec /i 로 실행, .exe 이면 직접 실행.
    """
    abs_path = os.path.abspath(installer_path)
    if not os.path.isfile(abs_path):
        raise RuntimeError(f"설치 프로그램 파일을 찾을 수 없습니다: {abs_path}")

    ext = abs_path.lower().rsplit(".", 1)[-1]
    if ext not in ("exe", "msi"):
        raise RuntimeError(f"예상치 않은 파일 형식: {abs_path}")

    if ext == "msi":
        proc = subprocess.Popen(["msiexec", "/i", abs_path],
                                creationflags=CREATE_NO_WINDOW)
    else:
        proc = subprocess.Popen([abs_path], creationflags=CREATE_NO_WINDOW)

    proc.wait()


# ── Fabric ───────────────────────────────────────────────────────────────────

def is_fabric_installed(minecraft_dir: str, game_version: str = GAME_VERSION) -> bool:
    """versions 폴더를 검사해 game_version 용 Fabric 로더가 설치되어 있는지 확인."""
    versions_dir = get_versions_dir(minecraft_dir)
    if not os.path.isdir(versions_dir):
        return False

    for name in os.listdir(versions_dir):
        version_path = os.path.join(versions_dir, name)
        if not os.path.isdir(version_path):
            continue

        name_lower = name.lower()
        is_fabric_name = "fabric" in name_lower

        data = None
        json_path = os.path.join(version_path, f"{name}.json")
        if os.path.isfile(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = None

        is_fabric_json = False
        inherits_from  = None
        if data:
            main_class = str(data.get("mainClass", ""))
            if "fabric" in main_class.lower():
                is_fabric_json = True
            for lib in data.get("libraries", []):
                if "fabricmc" in str(lib.get("name", "")).lower():
                    is_fabric_json = True
                    break
            inherits_from = data.get("inheritsFrom")

        if not (is_fabric_name or is_fabric_json):
            continue

        # Fabric 프로필 확인 → 게임 버전도 일치하는지 확인
        if game_version in name:
            return True
        if inherits_from == game_version:
            return True

    return False


def get_fabric_version(minecraft_dir: str, game_version: str = GAME_VERSION) -> str | None:
    """설치된 Fabric 로더 버전 문자열 반환. 미설치 또는 파싱 실패 시 None."""
    versions_dir = get_versions_dir(minecraft_dir)
    if not os.path.isdir(versions_dir):
        return None

    for name in os.listdir(versions_dir):
        if "fabric" not in name.lower():
            continue
        if game_version not in name:
            continue

        json_path = os.path.join(versions_dir, name, f"{name}.json")
        if not os.path.isfile(json_path):
            continue
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 폴더명에서 로더 버전 파싱: fabric-loader-0.16.5-26.1.2
            parts = name.split("-")
            # "fabric-loader-<version>-<mc>" 형식 처리
            if len(parts) >= 4 and parts[0] == "fabric" and parts[1] == "loader":
                return parts[2]
            # 버전 정보가 json 내 id 필드에 있는 경우
            return data.get("id", name)
        except Exception:
            return name

    return None


# ── Fabric 로더 업데이트 감지 (독립 기능 — 현재 앱 흐름과 미연결) ────────────
#
# 아래 두 함수는 Fabric 공식 메타 API(meta.fabricmc.net)를 사용해
# 설치된 로더 버전이 최신인지 확인한다.
# 어디서도 자동 호출하지 않으므로 기존 동작에 영향이 없다.
# 2.1 에서 UI에 연결할 때 import 해서 사용하면 된다.

FABRIC_META_BASE = "https://meta.fabricmc.net"
FABRIC_META_HOST = "meta.fabricmc.net"


def get_latest_fabric_loader(game_version: str = GAME_VERSION,
                             stable_only: bool = True) -> str | None:
    """Fabric 메타 API에서 game_version 호환 최신 로더 버전 문자열을 반환.

    GET /v2/versions/loader/{game_version}
      → [{ "loader": {"version": "0.16.5", "stable": true, ...}, ... }, ...]
        (목록은 최신순 정렬)

    stable_only=True 면 stable=True 인 첫 항목, 없으면 None.
    네트워크/파싱 실패 시 None.
    """
    url = f"{FABRIC_META_BASE}/v2/versions/loader/{urllib.parse.quote(game_version)}"

    # 호스트 검증 (보안 일관성)
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != FABRIC_META_HOST:
        return None

    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    if not isinstance(data, list) or not data:
        return None

    for entry in data:
        loader = entry.get("loader", {})
        ver = loader.get("version")
        if not ver:
            continue
        if stable_only and not loader.get("stable", False):
            continue
        return ver  # 목록이 최신순이므로 첫 매칭이 최신

    # stable 이 하나도 없으면 (드묾) 전체 최신이라도 반환
    if stable_only:
        first = data[0].get("loader", {}).get("version")
        return first
    return None


def check_fabric_loader_update(minecraft_dir: str,
                               game_version: str = GAME_VERSION) -> dict:
    """설치된 Fabric 로더와 최신 로더를 비교.

    반환 dict:
      {
        "installed": "0.16.5" | None,
        "latest":    "0.17.2" | None,
        "update_available": True/False,
        "status": "up_to_date" | "update_available"
                  | "not_installed" | "check_failed"
      }
    """
    installed = get_fabric_version(minecraft_dir, game_version)
    latest    = get_latest_fabric_loader(game_version, stable_only=True)

    if installed is None:
        return {"installed": None, "latest": latest,
                "update_available": False, "status": "not_installed"}
    if latest is None:
        return {"installed": installed, "latest": None,
                "update_available": False, "status": "check_failed"}

    if _version_tuple(installed) < _version_tuple(latest):
        return {"installed": installed, "latest": latest,
                "update_available": True, "status": "update_available"}
    return {"installed": installed, "latest": latest,
            "update_available": False, "status": "up_to_date"}


def _version_tuple(v: str) -> tuple:
    """'0.16.5' → (0,16,5) 형태로 변환해 숫자 비교. 파싱 실패분은 0 처리."""
    nums = []
    for part in str(v).split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    return tuple(nums)


def download_fabric_installer(progress_cb=None) -> str:
    """Fabric 설치 프로그램(.exe)을 임시 폴더에 내려받고 경로를 반환.

    보안: https + maven.fabricmc.net 호스트만 허용.
    """
    parsed = urllib.parse.urlsplit(FABRIC_INSTALLER_URL)
    if parsed.scheme != "https" or parsed.hostname != FABRIC_INSTALLER_HOST:
        raise RuntimeError(f"안전하지 않은 Fabric 다운로드 URL: {FABRIC_INSTALLER_URL}")

    tmp_dir = tempfile.mkdtemp(prefix="dongleland_fabric_")
    dest = os.path.join(tmp_dir, "fabric-installer-1.1.1.exe")

    req = urllib.request.Request(FABRIC_INSTALLER_URL, headers={"User-Agent": USER_AGENT})
    tmp_path = dest + ".part"
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(tmp_path, "wb") as out:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb and total:
                        try:
                            progress_cb(min(100, int(downloaded * 100 / total)))
                        except Exception:
                            pass
        os.replace(tmp_path, dest)
    except Exception:
        if os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise

    return dest


def run_fabric_installer(installer_path: str):
    """Fabric 설치 프로그램 실행 후 완료 대기.

    보안: 파일 존재 + .exe 확장자 검증 후 실행
    """
    abs_path = os.path.abspath(installer_path)
    if not os.path.isfile(abs_path):
        raise RuntimeError(f"설치 프로그램 파일을 찾을 수 없습니다: {abs_path}")
    if not abs_path.lower().endswith(".exe"):
        raise RuntimeError(f"예상치 않은 파일 형식: {abs_path}")
    proc = subprocess.Popen([abs_path], creationflags=CREATE_NO_WINDOW)
    proc.wait()


# ── 런처 실행 ────────────────────────────────────────────────────────────────

def launch_minecraft():
    """마인크래프트 Java Edition 런처 실행.

    우선순위:
    1. Windows 레지스트리 3가지 경로에서 InstallLocation 조회
    2. 알려진 Program Files 경로 탐색 (C·D·E 드라이브)
    3. Windows shell 'start' 명령으로 직접 실행 시도
    Bedrock(WindowsApps/Packages) 경로는 의도적으로 제외.
    """
    import string as _string

    # 1) 레지스트리에서 공식 Mojang Java 런처 경로 조회
    try:
        import winreg
        reg_keys = [
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Mojang\InstalledProducts\Minecraft Launcher"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Mojang\InstalledProducts\Minecraft Launcher"),
            (winreg.HKEY_CURRENT_USER,
             r"SOFTWARE\Mojang\InstalledProducts\Minecraft Launcher"),
        ]
        for hive, subkey in reg_keys:
            try:
                key = winreg.OpenKey(hive, subkey)
                for val_name in ("InstallLocation", "DisplayIcon", "UninstallString"):
                    try:
                        val, _ = winreg.QueryValueEx(key, val_name)
                        # InstallLocation 이면 폴더 + exe, 나머지는 경로에서 폴더 추출
                        if val_name == "InstallLocation":
                            launcher = os.path.join(val.strip('"'), "MinecraftLauncher.exe")
                        else:
                            launcher = os.path.join(
                                os.path.dirname(val.strip('"')), "MinecraftLauncher.exe"
                            )
                        if os.path.isfile(launcher):
                            subprocess.Popen([launcher])
                            return True
                    except OSError:
                        continue
            except OSError:
                continue
    except ImportError:
        pass

    # 2) 알려진 경로 + 모든 드라이브 탐색 (Bedrock/WindowsApps 제외)
    rel_paths = [
        # Xbox / Microsoft Store 설치 (Java Edition 런처)
        os.path.join("XboxGames", "Minecraft Launcher", "Content", "Minecraft.exe"),
        os.path.join("XboxGames", "Minecraft Launcher", "Content", "MinecraftLauncher.exe"),
        # 독립 설치형 런처
        os.path.join("Program Files (x86)", "Minecraft Launcher", "MinecraftLauncher.exe"),
        os.path.join("Program Files", "Minecraft Launcher", "MinecraftLauncher.exe"),
        os.path.join("Program Files (x86)", "Minecraft", "MinecraftLauncher.exe"),
        os.path.join("Program Files", "Minecraft", "MinecraftLauncher.exe"),
    ]
    drives = [f"{d}:\\" for d in _string.ascii_uppercase
              if os.path.exists(f"{d}:\\")]
    for drive in drives:
        for rel in rel_paths:
            path = os.path.join(drive, rel)
            if os.path.isfile(path):
                subprocess.Popen([path])
                return True

    # LOCALAPPDATA 아래 독립 런처
    local = os.environ.get("LOCALAPPDATA", "")
    for sub in [
        os.path.join(local, "Minecraft Launcher", "MinecraftLauncher.exe"),
    ]:
        if os.path.isfile(sub):
            subprocess.Popen([sub])
            return True

    # 3) Windows shell 명령으로 직접 실행 시도
    try:
        subprocess.Popen(
            ["cmd", "/c", "start", "", "MinecraftLauncher.exe"],
            shell=False,
        )
        return True
    except Exception:
        pass

    return False


# ── Preflight 진행 상황 콜백 타입 ────────────────────────────────────────────
#
# GUI 에서 백그라운드 스레드로 run_preflight() 를 호출하면서
# on_status(step: str, message: str) 콜백으로 진행 상황을 받는다.
#
# step 목록
#   "start"          시작
#   "mc_check"       마인크래프트 경로 확인 중
#   "mc_not_found"   마인크래프트 미발견 (→ GUI 는 오류 팝업 후 종료)
#   "java_check"     Java 확인 중
#   "java_download"  Java 다운로드 중 (message: "N%")
#   "java_install"   Java 설치 프로그램 실행 중
#   "java_ok"        Java 확인 완료
#   "fabric_check"   Fabric 확인 중
#   "fabric_download" Fabric 다운로드 중
#   "fabric_install" Fabric 설치 프로그램 실행 중
#   "fabric_ok"      Fabric 확인 완료
#   "done"           모든 선행 조건 충족
#   "error"          오류 발생 (message: 오류 내용)

def run_preflight(on_status, config: dict | None = None) -> dict | None:
    """선행 조건 전체를 순서대로 확인/설치.

    Args:
        on_status: (step, message) → None 콜백
        config: 설정 딕셔너리 (없으면 파일에서 로드)

    Returns:
        성공 시: {"minecraft_dir": str, "mods_dir": str, "java_version": str}
        실패 시: None  (on_status("mc_not_found", ...) 또는 ("error", ...) 호출됨)
    """
    if config is None:
        config = load_config()

    on_status("start", "선행 조건 확인을 시작합니다")

    # 1) 마인크래프트 경로
    on_status("mc_check", "마인크래프트 설치 경로를 확인하는 중...")
    minecraft_dir = find_minecraft_dir(config)
    if not minecraft_dir:
        on_status("mc_not_found", (
            "마인크래프트 설치 경로를 찾을 수 없습니다.\n"
            "마인크래프트를 먼저 설치하고 한 번 실행해주세요."
        ))
        return None

    mods_dir = get_mods_dir(minecraft_dir)

    # config 에 경로 저장
    if config.get("minecraft_dir") != minecraft_dir:
        config["minecraft_dir"] = minecraft_dir
        save_config(config)

    # 2) Java
    on_status("java_check", "Java 설치 여부를 확인하는 중...")
    if not is_java_installed():
        on_status("java_download", "Java(JRE 21)를 다운로드하는 중... 0%")
        try:
            def java_progress(pct):
                on_status("java_download", f"Java(JRE 21)를 다운로드하는 중... {pct}%")

            installer = download_java_installer(progress_cb=java_progress)
            on_status("java_install",
                      "Java 설치 프로그램을 실행합니다.\n설치 창에서 진행을 완료해주세요.")
            run_java_installer(installer)

            if not is_java_installed():
                on_status("error",
                          "Java 설치가 확인되지 않았습니다.\n"
                          "설치 프로그램을 완료했는지 확인 후 앱을 다시 실행해주세요.")
                return None
        except Exception as e:
            on_status("error", f"Java 설치 중 오류가 발생했습니다:\n{e}")
            return None

    java_ver = get_java_version_string() or "감지됨"
    on_status("java_ok", f"Java 확인 완료 ({java_ver})")

    # 3) Fabric
    on_status("fabric_check", f"Fabric {GAME_VERSION} 설치 여부를 확인하는 중...")
    if not is_fabric_installed(minecraft_dir):
        on_status("fabric_download", "Fabric 설치 프로그램을 다운로드하는 중...")
        try:
            def fabric_progress(pct):
                on_status("fabric_download", f"Fabric 설치 프로그램을 다운로드하는 중... {pct}%")

            installer = download_fabric_installer(progress_cb=fabric_progress)
            on_status("fabric_install",
                      f"Fabric 설치 프로그램을 실행합니다.\n"
                      f"설치 창에서 마인크래프트 버전을 '{GAME_VERSION}' 으로 선택 후\n"
                      f"'Install' 버튼을 눌러 설치를 완료해주세요.")
            run_fabric_installer(installer)

            if not is_fabric_installed(minecraft_dir):
                on_status("error",
                          f"Fabric({GAME_VERSION}) 설치가 확인되지 않았습니다.\n"
                          f"설치 창에서 버전을 {GAME_VERSION}으로 선택했는지 확인 후\n"
                          f"앱을 다시 실행해주세요.")
                return None
        except Exception as e:
            on_status("error", f"Fabric 설치 중 오류가 발생했습니다:\n{e}")
            return None

    fabric_ver = get_fabric_version(minecraft_dir) or "감지됨"
    on_status("fabric_ok", f"Fabric 확인 완료 ({fabric_ver})")

    on_status("done", "모든 선행 조건이 충족되었습니다")

    return {
        "minecraft_dir": minecraft_dir,
        "mods_dir": mods_dir,
        "java_version": java_ver,
        "fabric_version": fabric_ver,
    }
