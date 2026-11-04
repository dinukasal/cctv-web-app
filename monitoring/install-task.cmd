@echo off
REM ---------------------------------------------------------------------------
REM  Register the WifiTunnelWatchdog scheduled task (runs as SYSTEM).
REM  Double-click this file, then click "Yes" on the UAC prompt.
REM ---------------------------------------------------------------------------
setlocal
net session >nul 2>&1
if %errorlevel%==0 (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-task.ps1"
) else (
    echo Requesting Administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
)
endlocal
