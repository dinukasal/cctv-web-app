#Requires -RunAsAdministrator
<#
    Registers the "WifiTunnelWatchdog" scheduled task.

    It runs WifiTunnelMonitor.exe "once" every 5 minutes as SYSTEM
    (highest privileges, no UAC prompts) so the WiFi + Cloudflare Tunnel
    checks and the Cloudflared auto-restart happen automatically.

    Must be run once from an elevated (Administrator) PowerShell.
#>

$ErrorActionPreference = 'Stop'

$TaskName = 'WifiTunnelWatchdog'
$Dir      = $PSScriptRoot
$Exe      = Join-Path $Dir 'WifiTunnelMonitor.exe'

if (-not (Test-Path $Exe)) { throw "Executable not found: $Exe" }

Write-Host "Registering scheduled task '$TaskName'..." -ForegroundColor Cyan

$action = New-ScheduledTaskAction -Execute $Exe -Argument 'once' -WorkingDirectory $Dir

# Run at boot, then every 5 minutes forever.
$trigStartup = New-ScheduledTaskTrigger -AtStartup
$trigRepeat  = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
                    -RepetitionInterval (New-TimeSpan -Minutes 5) `
                    -RepetitionDuration (New-TimeSpan -Days 3650)

# SYSTEM = always elevated, runs whether or not anyone is logged on.
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigStartup, $trigRepeat `
    -Principal $principal -Settings $settings `
    -Description 'Every 5 min: keep WiFi on HomeWiFi_5G and restart the Cloudflared tunnel if cam.example.com is down.' `
    -Force | Out-Null

Write-Host "Task registered. Running it once now to clear the current outage..." -ForegroundColor Cyan
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 8

Write-Host ""
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State | Format-List
Get-ScheduledTaskInfo -TaskName $TaskName | Select-Object LastRunTime, LastTaskResult, NextRunTime | Format-List

Write-Host "Log: $Dir\monitor.log" -ForegroundColor Green
