@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo  Dongleland Mod Installer v2 - Build Script
echo ============================================
echo.

REM Find a working Python command: prefer "py" (Python Launcher), fall back to "python"
set "PYCMD="
py --version >nul 2>nul && set "PYCMD=py"
if not defined PYCMD (
    python --version >nul 2>nul && set "PYCMD=python"
)
if not defined PYCMD (
    echo [ERROR] Python not found.
    echo  Install Python 3.10+ from https://www.python.org/downloads/
    echo  Make sure to check "Add python.exe to PATH" during install.
    pause & exit /b 1
)

echo [INFO] Using Python command: %PYCMD%
%PYCMD% --version
echo.

echo [1/3] Installing pip + PyInstaller + certifi ...
%PYCMD% -m pip install --upgrade pip pyinstaller certifi
if %errorlevel% neq 0 ( echo [ERROR] Dependency install failed & pause & exit /b 1 )
echo.

echo [2/3] Building exe ... (this may take a while)
%PYCMD% -m PyInstaller --noconfirm --onefile --windowed --name "DonglelandInstaller" --icon "assets/app_icon.ico" --add-data "assets;assets" --collect-data certifi mod_installer.py
if %errorlevel% neq 0 ( echo [ERROR] Build failed. Check the log above. & pause & exit /b 1 )
echo.

echo [3/3] Checking result ...
if exist "dist\DonglelandInstaller.exe" (
    echo.
    echo [SUCCESS] dist\DonglelandInstaller.exe created!
    echo  Location: %cd%\dist\DonglelandInstaller.exe
    echo.
    echo  Distribution: just share this single exe file.
    echo  ^(the assets folder is bundled inside the exe^)
    echo.
    echo  Note: antivirus may falsely flag the exe.
    echo  If the file disappears from dist, check the AV quarantine.
) else (
    echo [ERROR] exe was not created.
    echo  Check the log above or the AV quarantine.
)
echo.
pause
