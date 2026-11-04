@echo off
REM ---------------------------------------------------------------------------
REM  Fix the Cloudflare Tunnel + verify.  Double-click this file, then click
REM  "Yes" on the UAC prompt.
REM ---------------------------------------------------------------------------
setlocal
net session >nul 2>&1
if %errorlevel%==0 (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0fix-tunnel.ps1"
) else (
    echo Requesting Administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
)
endlocal
