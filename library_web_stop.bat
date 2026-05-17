@echo off
REM Stop the Academic Library web console by killing whatever holds the port.

setlocal
set HOST=127.0.0.1
set PORT=5577

set FOUND=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr "%HOST%:%PORT%" ^| findstr "LISTENING"') do (
  echo Killing PID %%a ...
  taskkill /F /PID %%a >nul 2>&1
  set FOUND=1
)

if "%FOUND%"=="0" (
  echo No listener found on %HOST%:%PORT%.
) else (
  echo Library web console stopped.
)
endlocal
