@echo off
REM ---------------------------------------------------------------------------
REM  Switch the Cloudflare tunnel to HTTP/2 (TCP) so it stops flapping on the
REM  lossy Starlink WiFi link.  Double-click this file, then click "Yes" on the
REM  UAC prompt.
REM ---------------------------------------------------------------------------
setlocal
net session >nul 2>&1
if %errorlevel%==0 (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0apply-tunnel-fix.ps1"
) else (
    echo Requesting Administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
)
endlocal
