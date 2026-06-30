@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo  동글랜드 모드 설치 도우미 v2 - 빌드 스크립트
echo ============================================
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [오류] Python을 찾을 수 없습니다.
    echo  -^> https://www.python.org/downloads/ 에서 Python 3.10 이상을 설치하세요.
    echo  -^> 설치 시 "Add python.exe to PATH" 를 반드시 체크하세요.
    pause & exit /b 1
)

echo [확인] Python 버전:
python --version
echo.

echo [1/3] pip + PyInstaller 설치 중...
python -m pip install --upgrade pip pyinstaller
if %errorlevel% neq 0 ( echo [오류] PyInstaller 설치 실패 & pause & exit /b 1 )
echo.

echo [2/3] exe 빌드 중... (시간이 걸릴 수 있습니다)
python -m PyInstaller --noconfirm --onefile --windowed ^
  --name "동글랜드_모드_설치_도우미" ^
  --icon "assets/app_icon.ico" ^
  --add-data "assets;assets" ^
  mod_installer.py
if %errorlevel% neq 0 ( echo [오류] 빌드 실패. 위 로그를 확인하세요. & pause & exit /b 1 )
echo.

echo [3/3] 결과 확인...
if exist "dist\동글랜드_모드_설치_도우미.exe" (
    echo.
    echo [성공] dist\동글랜드_모드_설치_도우미.exe 생성 완료!
    echo  위치: %cd%\dist\동글랜드_모드_설치_도우미.exe
    echo.
    echo  배포 방법: 이 exe 파일 하나만 전달하면 됩니다.
    echo  ^(assets 폴더가 exe 내부에 포함되어 있습니다^)
    echo.
    echo  참고: 백신이 exe 를 오탐지해서 삭제하는 경우가 있습니다.
    echo  dist 폴더에서 파일이 사라졌다면 백신 격리함을 확인해주세요.
) else (
    echo [오류] exe 가 생성되지 않았습니다.
    echo  위 로그를 확인하거나 백신 격리함을 확인해주세요.
)
echo.
pause
