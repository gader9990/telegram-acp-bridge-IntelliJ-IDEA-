@echo off
cd /d "%~dp0"
python standalone.py --config "%~dp0config.local.json"
if errorlevel 1 pause
