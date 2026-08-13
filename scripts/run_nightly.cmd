@echo off
REM AI-Investo nightly job.
REM Registered with Windows Task Scheduler as "AI-Investo Nightly".
REM Runs unattended: appends to reports\nightly.log and never prompts.

setlocal
set "ROOT=%~dp0.."
cd /d "%ROOT%"
set "PYTHONPATH=%ROOT%"
set "PYTHONUTF8=1"

"%ROOT%\.venv\Scripts\python.exe" -m engine.nightly
set "RC=%ERRORLEVEL%"

REM A non-zero code means at least one stage failed; the log names which.
REM The task itself still counts as run, so a bad night does not stop tomorrow.
exit /b %RC%
