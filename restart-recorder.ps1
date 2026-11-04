# One-shot: restart the recorder so it picks up the staged changes (fragmented MP4
# output and 10-minute segments), then remove the task that launched it.
Start-Sleep -Seconds 5
try {
  Restart-Service go2rtc-recorder -Force
  $msg = "{0}  recorder restarted; fragmented MP4 + 10-minute segments are now live" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
} catch {
  $msg = "{0}  restart FAILED: {1}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $_.Exception.Message
}
Add-Content -Path 'C:\go2rtc\restart-recorder.log' -Value $msg
schtasks /Delete /TN "cctv-apply-recorder-change" /F 2>&1 | Out-Null
