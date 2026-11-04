@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -X utf8 -m content_screening configure-ark
pause
