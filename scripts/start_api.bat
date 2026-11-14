@echo off
setlocal EnableExtensions
TITLE JobBot Recruiter Email - FastAPI
set "PROJECT_ROOT=D:\ird-projects\sahil_projects\jobbot-recruiter-email"
set "SHARED_VENV=D:\ird-projects\sahil_projects\my_jobbots_automation\.venv"
set "CHROME_PORT=9222"
set "CHROME_PROFILE=C:\chromedebug"

if not exist "%PROJECT_ROOT%\app\main.py" (
  echo ERROR: Project missing: "%PROJECT_ROOT%\app\main.py"
  goto :FAILED
)
pushd "%PROJECT_ROOT%"
if errorlevel 1 goto :FAILED
set "PYTHONPATH=%PROJECT_ROOT%"

REM Reuse existing valid Chrome DevTools session; start only if not available.
call :ENSURE_CHROME_DEBUGGER
if errorlevel 1 goto :FAILED

REM Prefer project-specific env, else reuse existing LinkedIn FastAPI venv.
set "ENV_ACTIVATE=%PROJECT_ROOT%\.venv\Scripts\activate.bat"
if not exist "%ENV_ACTIVATE%" set "ENV_ACTIVATE=%SHARED_VENV%\Scripts\activate.bat"
if not exist "%ENV_ACTIVATE%" (
  echo ERROR: Neither recruiter project .venv nor LinkedIn shared .venv found.
  echo Create a venv using scripts\setup_windows.bat or install the existing one.
  goto :FAILED
)
call "%ENV_ACTIVATE%"
if errorlevel 1 goto :FAILED
python -c "import fastapi, uvicorn, dotenv, jsonschema, selenium" >nul 2>&1
if errorlevel 1 (
  echo ERROR: Missing recruiter dependencies in selected venv.
  echo Run: python -m pip install -r "%PROJECT_ROOT%\requirements.txt"
  goto :FAILED
)

echo.
echo Project: %PROJECT_ROOT%
echo Venv:    %VIRTUAL_ENV%
echo Chrome:  http://127.0.0.1:%CHROME_PORT%/json/version
echo API:     http://localhost:8090/docs
echo.
python -m app.main
set "CODE=%ERRORLEVEL%"
popd
pause
exit /b %CODE%

:ENSURE_CHROME_DEBUGGER
call :IS_DEVTOOLS_READY
if not errorlevel 1 (
 echo Chrome debugger ready on port %CHROME_PORT%. Reusing it.
 exit /b 0
)
set "CHROME_EXE=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME_EXE%" set "CHROME_EXE=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME_EXE%" set "CHROME_EXE=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
if not exist "%CHROME_EXE%" (
 echo ERROR: Google Chrome executable not found.
 exit /b 1
)
echo Starting Chrome debugger on %CHROME_PORT% with %CHROME_PROFILE%...
start "" "%CHROME_EXE%" --remote-debugging-port=%CHROME_PORT% --remote-debugging-address=127.0.0.1 --user-data-dir="%CHROME_PROFILE%" --no-first-run "about:blank"
set /a CHROME_WAIT_COUNT=0
:WAIT_FOR_DEBUGGER
call :IS_DEVTOOLS_READY
if not errorlevel 1 exit /b 0
set /a CHROME_WAIT_COUNT+=1 >nul
if %CHROME_WAIT_COUNT% GEQ 30 (
 echo ERROR: Chrome debugger is not available on port %CHROME_PORT%.
 exit /b 1
)
timeout /t 2 /nobreak >nul
goto :WAIT_FOR_DEBUGGER

:IS_DEVTOOLS_READY
powershell.exe -NoLogo -NoProfile -NonInteractive -Command "try { $r=Invoke-RestMethod -Uri 'http://127.0.0.1:%CHROME_PORT%/json/version' -TimeoutSec 2 -ErrorAction Stop; if ($r.Browser -and $r.webSocketDebuggerUrl) { exit 0 } } catch {} ; exit 1" >nul 2>&1
exit /b %ERRORLEVEL%

:FAILED
echo.
echo JobBot recruiter startup FAILED.
popd >nul 2>&1
pause
exit /b 1
