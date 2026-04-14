@echo off
setlocal enabledelayedexpansion
title FTC Whisper — Installer
cd /d "%~dp0"

echo.
echo  ==============================================
echo   FTC Whisper  ^|  Installer
echo  ==============================================
echo.

:: ── 1. Check Python ──────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python was not found on your PATH.
    echo.
    echo  Please install Python 3.10 or newer:
    echo    https://www.python.org/downloads/
    echo.
    echo  Make sure to tick "Add Python to PATH" during install.
    pause
    exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
for /f "tokens=1,2 delims=." %%a in ("!PYVER!") do (
    set PYMAJ=%%a
    set PYMIN=%%b
)
if !PYMAJ! LSS 3 goto :badpython
if !PYMAJ! EQU 3 if !PYMIN! LSS 10 goto :badpython
echo  [OK] Python !PYVER!
goto :pythonok

:badpython
echo  [ERROR] Python 3.10 or newer is required. Found !PYVER!.
pause & exit /b 1

:pythonok

:: ── 2. Create virtual environment ────────────────────────────────────────
if not exist "venv\Scripts\python.exe" (
    echo  Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo  [ERROR] Could not create virtual environment.
        pause & exit /b 1
    )
    echo  [OK] Virtual environment created.
) else (
    echo  [OK] Virtual environment already exists.
)

:: ── 3. Upgrade pip silently ───────────────────────────────────────────────
echo  Upgrading pip...
venv\Scripts\python.exe -m pip install --upgrade pip --quiet

:: ── 4. Install dependencies ───────────────────────────────────────────────
echo  Installing dependencies (this may take a few minutes on first run)...
venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo  [ERROR] Dependency installation failed. Check your internet connection.
    pause & exit /b 1
)
echo  [OK] Dependencies installed.

:: ── 5. Run Python post-install (config, icon, shortcut) ──────────────────
echo.
venv\Scripts\python.exe installer.py
if errorlevel 1 (
    echo  [WARN] Post-install step reported an error — see above.
)

echo.
pause
