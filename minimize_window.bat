@echo off
REM 最小化 Edge 窗口（会话保持后台，不要关闭窗口！）
powershell -ExecutionPolicy Bypass -File "%~dp0tools\minimize_window.ps1"
pause