@echo off
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

rem 端口与服务端保持同一事实来源:server.mjs 读 YIYE_PORT,未设置时默认 4173
if not defined YIYE_PORT set YIYE_PORT=4173
rem 监督模式标记:服务端自动重载时以 75 退出交由本脚本重启;
rem 未设此标记的直接运行(如 node server.mjs)会自行拉起替换进程,不依赖外部监督
set YIYE_SUPERVISED=1

echo 正在启动译页（前台模式，本窗口需保持打开；按 Ctrl+C 停止服务）
echo 若希望关闭窗口后服务继续在后台运行，请改用：后台启动.bat
rem 只有当前后端代码已真正启动时才打开页面，避免新版前端误连未重启的旧服务。
start "" cmd /c "timeout /t 2 /nobreak >nul & curl.exe -fsS http://127.0.0.1:%YIYE_PORT%/api/runtime-ready >nul && start "" http://127.0.0.1:%YIYE_PORT%"

rem 监督循环:服务端检测到代码更新后以退出码 75 优雅退出,这里自动重启,无需手动重开
:run
node server.mjs
if %errorlevel% equ 75 (
  echo.
  echo [自动重载] 检测到服务端代码更新，正在重启服务…
  timeout /t 2 /nobreak >nul
  goto run
)
echo.
echo 服务已停止。
pause
