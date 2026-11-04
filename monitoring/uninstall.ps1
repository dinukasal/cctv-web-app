#Requires -RunAsAdministrator
<#
    Stops and removes the "WifiTunnelMonitor" Windows service.
    Pass -RemoveFiles to also delete C:\ProgramData\WifiTunnelMonitor.
#>
param(
    [switch]$RemoveFiles
)

$ErrorActionPreference = 'Stop'
$Dir  = $PSScriptRoot
$Name = 'WifiTunnelMonitor'

$svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
if ($svc) {
    Write-Host "Stopping service '$Name'..." -ForegroundColor Cyan
    if ($svc.Status -ne 'Stopped') {
        Stop-Service -Name $Name -Force -ErrorAction SilentlyContinue
        try { $svc.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30)) } catch {}
    }
    sc.exe delete $Name | Out-Null
    Write-Host "Service removed." -ForegroundColor Green
} else {
    Write-Host "Service '$Name' is not installed." -ForegroundColor Yellow
}

if ($RemoveFiles) {
    Write-Host "Deleting $Dir ..." -ForegroundColor Cyan
    Remove-Item -Path $Dir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Files removed." -ForegroundColor Green
}
