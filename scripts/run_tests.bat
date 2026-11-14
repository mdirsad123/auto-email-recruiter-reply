@echo off
setlocal
cd /d "D:\ird-projects\sahil_projects\jobbot-recruiter-email"
set "ENV_ACTIVATE=%CD%\.venv\Scripts\activate.bat"
if not exist "%ENV_ACTIVATE%" set "ENV_ACTIVATE=D:\ird-projects\sahil_projects\my_jobbots_automation\.venv\Scripts\activate.bat"
if not exist "%ENV_ACTIVATE%" (echo Missing venv & exit /b 1)
call "%ENV_ACTIVATE%"
python -m pytest tests\ai -q
pause
