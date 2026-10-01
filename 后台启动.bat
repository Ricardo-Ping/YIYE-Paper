@echo off
cd /d "%~dp0"
rem ── 后台启动:关闭/隐藏窗口后服务继续运行 ──
rem 以 hidden-bg 参数重启自身进入隐藏运行段(无窗口),日志写入 logs\后台服务.log
if "%~1"=="hidden-bg" goto hidden-run

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

rem 服务已在运行(端口有监听)则不再重复启动,直接打开页面
netstat -ano | findstr ":4173" | findstr "LISTENING" >nul
if not errorlevel 1 (
  echo 译页服务已在后台运行，将直接打开页面。
  start "" http://127.0.0.1:4173
  timeout /t 2 >nul
  exit /b 0
)

if not defined YIYE_PORT set YIYE_PORT=4173
set YIYE_SUPERVISED=1
if not exist logs mkdir logs

echo 正在后台启动译页服务...
rem 以隐藏窗口重启自身进入运行段(Start-Process 走 Unicode,路径含中文也安全)
powershell -NoProfile -Command "Start-Process -FilePath 'cmd.exe' -ArgumentList '/c','"%~f0" hidden-bg' -WindowStyle Hidden"
echo 服务已在后台运行（日志：logs\后台服务.log）。
echo 停止服务：双击项目根目录的 停止服务.bat
start "" cmd /c "timeout /t 3 /nobreak >nul & start "" http://127.0.0.1:4173"
timeout /t 2 >nul
exit /b 0

:hidden-run
cd /d "%~dp0"
if not defined YIYE_PORT set YIYE_PORT=4173
set YIYE_SUPERVISED=1
:runloop
node server.mjs >> "logs\后台服务.log" 2>&1
rem 退出码 75 = 检测到代码更新,监督循环自动重启;其他退出码记录后结束
if %errorlevel% equ 75 (
  timeout /t 2 /nobreak >nul
  goto runloop
)
echo [%date% %time%] 服务退出，退出码 %errorlevel% >> "logs\后台服务.log"
