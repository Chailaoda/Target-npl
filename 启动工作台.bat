@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动 Target-NPL 目标筛选工作台...
"%~dp0.venv\Scripts\python.exe" -X utf8 scripts\start_control.py --open
if errorlevel 1 (
    echo.
    echo 启动发生异常，请检查 output\control\server.log
    pause
)
