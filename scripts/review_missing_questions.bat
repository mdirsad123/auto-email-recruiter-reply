@echo off
set "QUESTIONS=%~dp0..\data\recruiter_questions_to_review.json"
if not exist "%QUESTIONS%" (
  echo No missing recruiter questions captured yet.
  echo The file will appear after an email requests a missing or unmapped fact.
  pause
  exit /b 0
)
start "" notepad.exe "%QUESTIONS%"
