@echo off
setlocal
cd /d "%~dp0"

rem Prefer the local venv, but fall back to whatever python is on PATH.
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -c "import fastapi, uvicorn" >nul 2>&1
if errorlevel 1 set "PY=python"

echo Relay Lab API: http://127.0.0.1:9011/   (docs: /api/docs)
echo Reports dir: %~dp0data
echo Keep this window open. Press Ctrl+C to stop.
"%PY%" -m relay_lab --port 9011
pause
