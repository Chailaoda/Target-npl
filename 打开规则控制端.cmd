@echo off
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -X utf8 scripts\start_control.py --open
if errorlevel 1 pause
