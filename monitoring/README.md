# WiFi + Cloudflare Tunnel Monitor

A dependency-free Windows service that keeps this laptop online:

1. **Wi-Fi** – keeps the adapter on the preferred SSID `HomeWiFi_5G`, and
   **fails over to `HomeWiFi_2.4G`** when the preferred network has no
   internet or the tunnel cannot be reached through it. It reconnects with
   `netsh wlan connect`.
2. **Cloudflare Tunnel** – probes `https://cam.example.com`.
   A Cloudflare **530** / error **1033** response (or no response) means the
   tunnel is down. The `Cloudflared` Windows service is restarted **only after
   several consecutive failures and a cooldown**, so a single transient blip no
   longer causes a self-inflicted tunnel outage.

## Files

| File                    | Purpose                                   |
|-------------------------|-------------------------------------------|
| `WifiTunnelMonitor.exe` | The service executable                    |
| `WifiTunnelMonitor.cs`  | Source code                               |
| `config.ini`            | SSID / URL / service name / interval      |
| `monitor.log`           | Rolling activity log (max 5 MB + `.old`)  |
| `install.ps1`           | Installs + starts the service             |
| `uninstall.ps1`         | Stops + removes the service               |
| `install-task.ps1`      | Registers the scheduled-task option        |
| `uninstall-task.ps1`    | Removes the scheduled task                 |

## Install

Open **PowerShell as Administrator** and run:

```powershell
cd C:\ProgramData\WifiTunnelMonitor
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

## Verify

```powershell
Get-Service WifiTunnelMonitor
Get-Content C:\ProgramData\WifiTunnelMonitor\monitor.log -Tail 40 -Wait
```

One-shot health check (no changes made):

```powershell
C:\ProgramData\WifiTunnelMonitor\WifiTunnelMonitor.exe test
```

## Uninstall

```powershell
cd C:\ProgramData\WifiTunnelMonitor
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
# add -RemoveFiles to also delete the folder
```

## Rebuild after editing the source

```powershell
& "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe" `
  -nologo -target:exe -out:WifiTunnelMonitor.exe `
  -reference:System.dll -reference:System.Core.dll -reference:System.ServiceProcess.dll `
  WifiTunnelMonitor.cs
Stop-Service WifiTunnelMonitor; Start-Service WifiTunnelMonitor
```

## How detection works

* Restarts the tunnel only after **`fail_threshold`** consecutive failed checks
  (default **3**), and at most once every **`restart_cooldown`** seconds
  (default **600**), to avoid churn.
* Before restarting cloudflared it tries the **fallback SSID**
  (`failover_after`, default 2 failures), at most every `failover_cooldown`
  seconds (default 900).
* Never restarts cloudflared when the local origin (`127.0.0.1:8880`) is down
  too – that proves the fault is not the tunnel.
* Skips the tunnel restart while there is **no internet**, so a Starlink outage
  doesn't trigger pointless cloudflared restarts.
* Internet is considered up if **any** of several well-known endpoints is reachable.
* Failure streak, last restart and last failover are persisted to
  `monitor-state.ini`, so the thresholds also apply to the 5-minute one-shot task.

## Tunnel transport

On a lossy link QUIC (the default UDP transport) re-connects constantly, which
shows up as intermittent Cloudflare **530 / 1033** and **502** pages. The tunnel
is therefore run over **HTTP/2 (TCP)** via `apply-tunnel-fix.cmd` in
`C:\go2rtc`, which adds `--protocol http2` to the `Cloudflared` service.

## Run as a scheduled task instead of a service

If you prefer a scheduled job over a Windows service, register
`WifiTunnelWatchdog`. It runs `WifiTunnelMonitor.exe once` as **SYSTEM** with
**highest privileges** (so it never shows a UAC prompt) — at boot and then
every 5 minutes:

```powershell
# from an elevated PowerShell, once:
powershell -ExecutionPolicy Bypass -File C:\ProgramData\WifiTunnelMonitor\install-task.ps1
```

Or simply double-click `install-task.cmd` and click **Yes** on the UAC prompt.

Raw equivalent (elevated `cmd`):

```bat
schtasks /Create /F /TN WifiTunnelWatchdog ^
  /TR "C:\ProgramData\WifiTunnelMonitor\WifiTunnelMonitor.exe once" ^
  /SC MINUTE /MO 5 /RU SYSTEM /RL HIGHEST
```

Check it:

```powershell
Get-ScheduledTask     WifiTunnelWatchdog | Select TaskName,State
Get-ScheduledTaskInfo WifiTunnelWatchdog | Select LastRunTime,LastTaskResult,NextRunTime
Get-Content C:\ProgramData\WifiTunnelMonitor\monitor.log -Tail 20
```

Remove it:

```powershell
powershell -ExecutionPolicy Bypass -File C:\ProgramData\WifiTunnelMonitor\uninstall-task.ps1
```

> **Note:** creating a task that runs as SYSTEM/Highest always requires
elevation **once**. After that it runs automatically forever with no prompts.
