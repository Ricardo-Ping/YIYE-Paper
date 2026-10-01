@echo off
rem 停止后台运行的译页服务(按 4173 端口监听进程整树终止,含引擎子进程)
set FOUND=
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":4173" ^| findstr "LISTENING"') do (
  set FOUND=%%p
  taskkill /PID %%p /T /F >nul 2>&1
)
if defined FOUND (
  echo 已停止译页服务（PID %FOUND%）。
) else (
  echo 译页服务未在运行。
)
timeout /t 2 >nul
