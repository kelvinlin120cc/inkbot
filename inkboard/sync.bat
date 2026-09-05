@echo off
chcp 65001 >nul 2>nul
set "HERE=%~dp0"
echo [%date% %time%] sync.bat invoked >> "%HERE%sync.log"
"C:\Users\Kelvinlin\.workbuddy\binaries\python\versions\3.13.12\python.exe" "%HERE%sync_events.py" --source wecom >> "%HERE%sync.log" 2>&1
echo [%date% %time%] sync.bat exit=%errorlevel% >> "%HERE%sync.log"
