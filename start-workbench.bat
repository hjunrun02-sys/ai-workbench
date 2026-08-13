@echo off
REM AI 工作台 Windows 启动脚本
REM 用法：双击本文件，或命令行执行 start-workbench.bat

cd /d "%~dp0"

REM 检测 python
where python >nul 2>nul
if %errorlevel%==0 (
    set PY=python
) else (
    where py >nul 2>nul
    if %errorlevel%==0 (
        set PY=py
    ) else (
        echo 未找到 Python。请先安装 Python 3.8+ 并勾选 "Add to PATH"。
        pause
        exit /b 1
    )
)

echo 正在启动 AI 工作台...
%PY% server.py
pause
