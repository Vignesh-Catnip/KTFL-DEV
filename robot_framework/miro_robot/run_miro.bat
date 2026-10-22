@echo off
REM KTFL MIRO robot - schedule this in Windows Task Scheduler (run only when user is logged on:
REM SAP GUI needs an interactive desktop session).
cd /d "%~dp0"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set TS=%%i
python -m robot --outputdir "output\run_%TS%" --loglevel DEBUG:INFO --consolewidth 120 tasks\miro_posting.robot
exit /b %ERRORLEVEL%
