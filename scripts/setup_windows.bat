@echo off
setlocal
cd /d "D:\ird-projects\sahil_projects\jobbot-recruiter-email"
if not exist .venv\Scripts\activate.bat python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
if not exist .env copy .env.example .env
python -m pytest tests\ai -q
pause
