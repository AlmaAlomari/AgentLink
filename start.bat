@echo off
cd /d "%~dp0"
where python >nul 2>nul || (echo Python is not installed. Install it from python.org and tick "Add Python to PATH", then run this file again. & pause & exit /b)
if not exist .venv python -m venv .venv
.venv\Scripts\python -m pip install -q -r requirements.txt
if not exist .env if exist .env.example copy .env.example .env >nul
start "" cmd /c "timeout /t 4 >nul & start http://localhost:8000"
echo AgentLink is running at http://localhost:8000  (close this window to stop)
.venv\Scripts\python -m uvicorn backend.main:app
pause
