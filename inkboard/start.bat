@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PY=C:\Users\Kelvinlin\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"

echo Starting InkBoard ...
"%PY%" server.py --port 8765
pause
