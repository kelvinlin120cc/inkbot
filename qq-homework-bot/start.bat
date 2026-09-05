@echo off
setlocal
cd /d "%~dp0"

set "PY=C:\Users\Kelvinlin\.workbuddy\binaries\python\envs\qqbot\Scripts\python.exe"
set "VENV=C:\Users\Kelvinlin\.workbuddy\binaries\python\envs\qqbot"

if not exist "%PY%" goto :no_py
if not exist "config.json" goto :no_cfg

echo.
echo ============================================================
echo   QQ Homework Bot
echo   Starting... logs go to homework_bot.log
echo   Close this window to stop the bot.
echo ============================================================
echo.

"%PY%" homework_bot.py
set "RC=%ERRORLEVEL%"
echo.
echo [INFO] Bot stopped. exit code = %RC%
echo [INFO] See homework_bot.log for details.
echo.
goto :end

:no_py
echo.
echo [ERROR] Python venv not found at:
echo     %PY%
echo.
echo Create it first:
echo     "C:\Users\Kelvinlin\.workbuddy\binaries\python\versions\3.13.12\python.exe" -m venv "%VENV%"
echo     "%PY%" -m pip install -r requirements.txt
echo.
goto :end

:no_cfg
echo.
echo [ERROR] config.json not found.
echo.
echo Do this:
echo     1. copy config.example.json config.json
echo     2. fill in appid and secret from https://q.qq.com
echo.
goto :end

:end
pause
