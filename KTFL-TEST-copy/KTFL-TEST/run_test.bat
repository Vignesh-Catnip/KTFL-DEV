@echo off
REM ===== TEST COPY on http://localhost:7100 - the live app keeps its own port, folders and database =====
REM Use the python environment of the LIVE app (all packages are already installed there). Check this path:
set LIVE_VENV=C:\Users\ckts00126\Desktop\KTFL-EXTRACTION\final_app\backend\venv

cd /d %~dp0backend
call "%LIVE_VENV%\Scripts\activate"
uvicorn app.main:app --host 127.0.0.1 --port 7100
