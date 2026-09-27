@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 译页 Yiye Paper

where uv >nul 2>nul
if errorlevel 1 (
  echo [错误] 未检测到 uv，请先安装：https://docs.astral.sh/uv/getting-started/installation/
  pause
  exit /b 1
)

where node >nul 2>nul
if errorlevel 1 (
  echo [错误] 未检测到 Node.js，请先安装：https://nodejs.org/
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo 首次运行：正在安装翻译引擎，可能需要几分钟...
  uv sync
  if errorlevel 1 (
    echo [错误] 翻译引擎安装失败，请检查网络后重试。
    pause
    exit /b 1
  )
)

echo 正在启动译页，浏览器将自动打开 http://127.0.0.1:4173 （按 Ctrl+C 停止服务）
start "" cmd /c "timeout /t 2 /nobreak >nul & start "" http://127.0.0.1:4173"
npm start
echo.
echo 服务已停止。
pause
