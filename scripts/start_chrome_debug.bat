@echo off
setlocal EnableExtensions
set "ROOT=D:\ird-projects\sahil_projects\jobbot-recruiter-email"
set "PORT=9222"
powershell.exe -NoLogo -NoProfile -NonInteractive -Command "try { $r=Invoke-RestMethod -Uri 'http://127.0.0.1:%PORT%/json/version' -TimeoutSec 2; if ($r.Browser -and $r.webSocketDebuggerUrl) { exit 0 } } catch {} ; exit 1" >nul 2>&1
if not errorlevel 1 (
  echo Chrome debugger already active on port %PORT%. Reusing it.
  exit /b 0
)
set "CHROME_EXE=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME_EXE%" set "CHROME_EXE=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME_EXE%" set "CHROME_EXE=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME_EXE%" (
  echo Google Chrome not found.
  exit /b 1
)
start "" "%CHROME_EXE%" --remote-debugging-port=%PORT% --remote-debugging-address=127.0.0.1 --user-data-dir="C:\chromedebug" --no-first-run "about:blank"
echo Chrome launched. Use scripts\start_api.bat to wait for readiness.
exit /b 0
