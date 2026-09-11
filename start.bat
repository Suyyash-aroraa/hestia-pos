@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 exit /b 1
)
if not exist ".venv\.hestia-ready" (
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 exit /b 1
    type nul > ".venv\.hestia-ready"
)
".venv\Scripts\python.exe" start.py
pause
