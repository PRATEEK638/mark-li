@echo off
REM ── AURA launcher ──────────────────────────────────────────────────────
REM  Double-click to start. Checks the routes first so a dead key shows up
REM  here rather than as silence after the HUD is already open.

cd /d "%~dp0"
title AURA

if not exist ".venv\Scripts\python.exe" (
    echo [SETUP] No virtual environment found - creating one...
    py -3.11 -m venv .venv || goto :fail
    .venv\Scripts\python.exe -m pip install --upgrade pip
    .venv\Scripts\python.exe -m pip install -r requirements.txt || goto :fail
    .venv\Scripts\python.exe -m playwright install chromium
)

if not exist "config\api_keys.json" (
    echo [ERROR] config\api_keys.json is missing.
    echo         Copy config\api_keys.example.json to config\api_keys.json
    echo         and fill in your keys.
    pause
    exit /b 1
)

echo [CHECK] Verifying model routes...
.venv\Scripts\python.exe -m core.selftest
if errorlevel 1 (
    echo.
    echo [WARN] Self-test reported a problem. Start anyway?
    choice /c YN /m "Continue"
    if errorlevel 2 exit /b 1
)

echo.
echo [START] Launching AURA...
.venv\Scripts\python.exe main.py
goto :eof

:fail
echo [ERROR] Setup failed. See the messages above.
pause
exit /b 1
