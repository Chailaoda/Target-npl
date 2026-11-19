@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 按统一任务模板中的数据来源、规则和额度执行。
echo 仅使用项目专用浏览器；如遇登录或验证码，需要本人在该窗口完成。
"%~dp0.venv\Scripts\python.exe" -X utf8 -m content_screening.task_agent run-active --wait
echo.
echo 本轮退出码：%ERRORLEVEL%
pause
