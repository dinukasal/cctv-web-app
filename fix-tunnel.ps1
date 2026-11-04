# Restart cloudflared and verify the tunnel + dashboard are reachable.
$ErrorActionPreference = 'Continue'

function Probe($host) {
    try {
        $r = Invoke-WebRequest "https://$host/" -UseBasicParsing -TimeoutSec 20 -MaximumRedirection 3
        $title = ''
        if ($r.Content) { $title = [regex]::Match($r.Content, '<title>(.*?)</title>', 'IgnoreCase').Groups[1].Value }
        return "HTTP $($r.StatusCode)  len=$($r.RawContentLength)  title='$title'"
    } catch {
        return "ERR $($_.Exception.Message)"
    }
}

Write-Host "=== BEFORE ===" -ForegroundColor Cyan
Write-Host ("cam.example.com -> " + (Probe 'cam.example.com'))
Write-Host ("cam.example.com  -> " + (Probe 'cam.example.com'))

Write-Host ""
Write-Host "=== Restarting Cloudflared ===" -ForegroundColor Cyan
Restart-Service Cloudflared -Force
Write-Host "Restarted. Waiting for tunnel to establish..."

$ok = $false
for ($i = 1; $i -le 20; $i++) {
    Start-Sleep -Seconds 3
    $p = Probe 'cam.example.com'
    Write-Host ("  t+{0,2}s: {1}" -f ($i * 3), $p)
    if ($p -match '^HTTP (2|3|4)') { $ok = $true; break }
}
Get-Service Cloudflared | Format-List Name, Status

Write-Host ""
Write-Host "=== AFTER ===" -ForegroundColor Cyan
Write-Host ("cam.example.com -> " + (Probe 'cam.example.com'))
Write-Host ("cam.example.com  -> " + (Probe 'cam.example.com'))

Write-Host ""
Write-Host "=== Recent Cloudflared events ===" -ForegroundColor Cyan
Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='Cloudflared'} -MaxEvents 6 -ErrorAction SilentlyContinue |
    Select-Object TimeCreated, Message | Format-Table -AutoSize -Wrap

if ($ok) { Write-Host "RESULT: tunnel reconnected" -ForegroundColor Green }
else     { Write-Host "RESULT: tunnel still down" -ForegroundColor Red }
