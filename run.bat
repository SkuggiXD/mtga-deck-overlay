@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python is not on PATH. Install Python 3 from python.org and tick "Add Python to PATH".
  pause
  exit /b 1
)
python overlay.py %*
if errorlevel 1 pause
