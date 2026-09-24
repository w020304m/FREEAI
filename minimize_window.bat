@echo off
REM 最小化浏览器 CDP 窗口（会话保持后台运行，不要关闭窗口！）
powershell -ExecutionPolicy Bypass -File "%~dp0minimize_window.ps1"
pause
