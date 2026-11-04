#Requires -RunAsAdministrator
<#
    Installs the "WifiTunnelMonitor" Windows service.
    Must be run from an elevated (Administrator) PowerShell prompt.
#>

$ErrorActionPreference = 'Stop'

# Everything is relative to the folder this script lives in.
$Dir         = $PSScriptRoot
$Exe         = Join-Path $Dir 'WifiTunnelMonitor.exe'
$Name        = 'WifiTunnelMonitor'
$DisplayName = 'WiFi + Cloudflare Tunnel Monitor'
$Description = 'Keeps the WiFi adapter on the target SSID (HomeWiFi_5G) and restarts the cloudflared service when the tunnel to cam.example.com is down (Cloudflare 530/1033).'

if (-not (Test-Path $Exe)) {
    throw "Executable not found: $Exe"
}

Write-Host "Installing service '$Name'..." -ForegroundColor Cyan

# Remove an existing instance, if any.
$existing = Get-Service -Name $Name -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Existing service found - removing..." -ForegroundColor Yellow
    if ($existing.Status -ne 'Stopped') {
        Stop-Service -Name $Name -Force -ErrorAction SilentlyContinue
        $existing.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
    }
    sc.exe delete $Name | Out-Null
    Start-Sleep -Seconds 2
}

# Create the service.
New-Service -Name $Name `
            -BinaryPathName "`"$Exe`"" `
            -DisplayName $DisplayName `
            -Description $Description `
            -StartupType Automatic | Out-Null

# Recovery: restart the process on crash (reset counter daily).
sc.exe failure $Name reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Null

# Start it.
Start-Service -Name $Name

$svc = Get-Service -Name $Name
Write-Host ""
Write-Host "Service installed and started." -ForegroundColor Green
$svc | Format-List Name, DisplayName, Status, StartType
Write-Host "Log file : $Dir\monitor.log"
Write-Host ""
Write-Host "Next monitoring cycle output appears in the log within ~30s."
