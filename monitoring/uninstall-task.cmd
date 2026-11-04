@echo off
setlocal
net session >nul 2>&1
if %errorlevel%==0 (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall-task.ps1"
) else (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
)
endlocal
