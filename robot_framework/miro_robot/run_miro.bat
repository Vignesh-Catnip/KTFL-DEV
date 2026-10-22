@echo off
REM KTFL MIRO robot - double-click or schedule in Windows Task Scheduler
REM ("Run only when user is logged on": SAP GUI needs a desktop session).
cd /d "%~dp0"
for /f %%i in ('python -c "import datetime;print(datetime.datetime.now().strftime('%%Y%%m%%d_%%H%%M%%S'))"') do set TS=%%i
if "%TS%"=="" set TS=latest
python -m robot --outputdir "output\run_%TS%" --loglevel DEBUG:INFO --consolewidth 120 tasks\miro_posting.robot
echo.
echo Log: %~dp0output\run_%TS%\log.html
exit /b %ERRORLEVEL%
