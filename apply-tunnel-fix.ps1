# apply-tunnel-fix.ps1
#
# Makes the Cloudflare tunnel survive this lossy Starlink WiFi link.
#
# The link drops ~10% of packets with 70-260 ms of jitter. cloudflared's default
# QUIC transport (UDP) re-connects constantly on a link like that, which shows up
# as intermittent Cloudflare HTTP 530 / error 1033 and 502 "Bad gateway" pages -
# i.e. the dashboard loads, then the Recorder / Camera-events panels fail with
# "cannot reach the bridge", and camera tiles show Bad gateway.
#
# Switching to HTTP/2 (TCP) tolerates packet loss far better. This script:
#   1. records the current service command line,
#   2. adds "--protocol http2" to the Cloudflared service,
#   3. restarts the service and waits for cloudflared's readiness endpoint to
#      report all 4 edge connections up.
#
# Run it elevated (double-click apply-tunnel-fix.cmd). Revert with:
#    sc.exe config Cloudflared binPath= "<the value printed at the top>"

#Requires -RunAsAdministrator
$ErrorActionPreference = 'Continue'

$Service = 'Cloudflared'
$Exe     = 'C:\Program Files (x86)\cloudflared\cloudflared.exe'
$Token   = 'C:\ProgramData\cloudflared\token'
$NewBin  = '"' + $Exe + '" tunnel run --protocol http2 --token-file ' + $Token
$OldFile = Join-Path $PSScriptRoot 'cloudflared-binpath.old.txt'

function Ready {
    try {
        $r = Invoke-WebRequest 'http://127.0.0.1:20241/ready' -UseBasicParsing -TimeoutSec 4
        return $r.Content
    } catch { return '<unreachable>' }
}

Write-Host "=== BEFORE ===" -ForegroundColor Cyan
sc.exe qc $Service | Select-String -Pattern 'BINARY_PATH_NAME'
Write-Host ("readiness: " + (Ready))

# Save the old command line so it can be restored.
$old = (sc.exe qc $Service | Select-String -Pattern 'BINARY_PATH_NAME') -replace '.*BINARY_PATH_NAME\s*:\s*',''
try { Set-Content -Path $OldFile -Value $old -Encoding UTF8 } catch {}
Write-Host ("Old binPath saved to: " + $OldFile) -ForegroundColor DarkGray

Write-Host ""
Write-Host "=== Applying --protocol http2 ===" -ForegroundColor Cyan
& sc.exe config $Service 'binPath=' $NewBin | Out-Host
sc.exe qc $Service | Select-String -Pattern 'BINARY_PATH_NAME'

Write-Host ""
Write-Host "=== Restarting Cloudflared ===" -ForegroundColor Cyan
Restart-Service $Service -Force
Write-Host "Waiting for the tunnel to register all 4 edge connections..."

$ok = $false
for ($i = 1; $i -le 20; $i++) {
    Start-Sleep -Seconds 3
    $r = Ready
    Write-Host ("  t+{0,2}s: {1}" -f ($i * 3), $r)
    if ($r -match '"readyConnections":4') { $ok = $true; break }
}

Write-Host ""
Write-Host "=== AFTER ===" -ForegroundColor Cyan
sc.exe qc $Service | Select-String -Pattern 'BINARY_PATH_NAME'
Write-Host ("readiness: " + (Ready))

if ($ok) { Write-Host "RESULT: tunnel up on http2 with 4 connections" -ForegroundColor Green }
else     { Write-Host "RESULT: http2 applied, but only some edge connections are up yet (this can take a minute)" -ForegroundColor Yellow }
