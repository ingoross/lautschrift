@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Bitte zuerst install-windows.ps1 ausfuehren.
    pause
    exit /b 1
)
start "Lautschrift" ".venv\Scripts\pythonw.exe" "lautschrift_windows.py"
