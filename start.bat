@echo off
title FREEAI Gateway
cd /d "%~dp0"
setlocal

echo ============================================
echo    FREEAI Gateway 一键启动
echo ============================================

REM ---- 1. 检查 Python ----
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 python，请先安装 Python 3.11+
    pause
    exit /b 1
)

REM ---- 2. 检查/创建虚拟环境 ----
if not exist ".venv\Scripts\python.exe" (
    echo [1/3] 首次运行：创建虚拟环境并安装依赖...
    python -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败
        pause
        exit /b 1
    )
    .venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败
        pause
        exit /b 1
    )
) else (
    echo [1/3] 虚拟环境已存在
)

REM ---- 3. 检查配置 ----
if not exist ".env" (
    echo [2/3] 未找到 .env，已从模板生成，请按需修改 API_MASTER_KEY / PORT
    copy .env.example .env >nul
) else (
    echo [2/3] .env 已存在
)

REM ---- 4. 读取端口 ----
set PORT=8110
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
        if "%%a"=="PORT" set PORT=%%b
    )
)

REM ---- 5. 端口占用检查（两段过滤避免误报）----
netstat -ano | findstr /c:":%PORT% " | findstr /c:"LISTENING" >nul 2>nul
if not errorlevel 1 (
    echo [提示] 端口 %PORT% 已被占用，可能服务已在运行；如确认未运行请检查占用进程
)

REM ---- 自检模式：验证 bat 解析与配置读取（不启动服务）----
if defined AIFREE_TEST (
    echo.
    echo [自检通过] 解析与配置读取正常，PORT=%PORT%
    exit /b 0
)

echo.
echo [3/3] 启动网关: http://127.0.0.1:%PORT%
echo.
echo ---------------------------------------------
echo  接入信息（启动后使用）
echo ---------------------------------------------
echo  OpenAI    :  base_url = http://127.0.0.1:%PORT%/v1
echo  Anthropic :  ANTHROPIC_BASE_URL=http://127.0.0.1:%PORT%
echo  健康检查   :  http://127.0.0.1:%PORT%/health
echo  模型列表   :  http://127.0.0.1:%PORT%/v1/models
echo  交互文档   :  http://127.0.0.1:%PORT%/docs
echo.
echo  * 浏览器窗口默认最小化到任务栏，不抢焦点
echo  * 多数情况下无需任何操作，直接即可调用接口
echo  * 需要人机验证或采集 token 时窗口会自动呼出；也可点任务栏打开
echo  * 用 /health 查看状态: session_ready=true 即已可用
echo.
echo  停止:  按 Ctrl+C
echo ---------------------------------------------
echo.

".venv\Scripts\python.exe" -m uvicorn main:app --host 0.0.0.0 --port %PORT%

echo.
echo 服务已停止。
pause
