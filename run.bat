@echo off
chcp 65001 >nul
title StockSentry 个股哨兵

echo.
echo  ===================================================
echo    StockSentry 个股哨兵 v1.0
echo    A股民营企业价值筛选系统
echo  ===================================================
echo.

:: 检查 .env 文件
if not exist ".env" (
    echo [ERROR] 未找到 .env 配置文件！
    echo.
    echo 请执行以下步骤：
    echo   1. 复制 .env.example 为 .env
    echo   2. 填写 TUSHARE_TOKEN 和 OPENROUTER_API_KEY
    echo.
    pause
    exit /b 1
)

:: 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 未找到 Python！请安装 Python 3.11+
    pause
    exit /b 1
)

:: 检查依赖
python -c "import fastapi" >nul 2>&1
if errorlevel 1 (
    echo [INFO] 首次运行，安装依赖包...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] 依赖安装失败，请检查网络连接
        pause
        exit /b 1
    )
)

:: 初始化数据库（如果不存在）
if not exist "data\stocksentry.db" (
    echo [INFO] 首次运行，初始化数据库...
    python scripts/init_db.py
    if errorlevel 1 (
        echo [ERROR] 数据库初始化失败
        pause
        exit /b 1
    )
)

:: 启动服务
echo [INFO] 启动 StockSentry 服务...
echo [INFO] 访问地址：http://127.0.0.1:8000
echo [INFO] 按 Ctrl+C 停止服务
echo.

python -m app.main

pause
