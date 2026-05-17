@echo off
REM Start the Academic Library web console.
REM Edit PORT below or `library_web_port` in mcp_runtime_config.json.

setlocal
cd /d "%~dp0"

set HOST=127.0.0.1
set PORT=5577
set PYEXE=.\.venv\Scripts\python.exe

if not exist "%PYEXE%" (
  echo [ERROR] Python venv not found at %PYEXE%
  echo Run: python -m venv .venv ^&^& .\.venv\Scripts\pip install -r requirements.txt
  exit /b 1
)

REM Refuse to start if the port is already in use.
netstat -ano | findstr "%HOST%:%PORT%" | findstr "LISTENING" >nul
if not errorlevel 1 (
  echo [WARN] Port %PORT% is already in use. Library web console may already be running.
  echo        Visit http://%HOST%:%PORT%/  or run library_web_stop.bat to terminate.
  start "" "http://%HOST%:%PORT%/"
  exit /b 0
)

echo Starting Academic Library web console at http://%HOST%:%PORT%/ ...
start "Academic Library Web" "%PYEXE%" -m uvicorn library_web.app:app --host %HOST% --port %PORT% --log-level warning

REM Give uvicorn a moment to bind the port before launching the browser.
powershell -NoProfile -Command "Start-Sleep -Milliseconds 1500"
start "" "http://%HOST%:%PORT%/"
endlocal
