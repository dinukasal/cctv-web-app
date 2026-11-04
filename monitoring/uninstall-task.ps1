#Requires -RunAsAdministrator
<#
    Removes the "WifiTunnelWatchdog" scheduled task.
#>

$ErrorActionPreference = 'Continue'
$TaskName = 'WifiTunnelWatchdog'

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask   -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Task '$TaskName' removed." -ForegroundColor Green
} else {
    Write-Host "Task '$TaskName' is not registered." -ForegroundColor Yellow
}
