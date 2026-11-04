# ============================================================================
#  Continuous camera recorder for the cameras VMS fails to record.
#
#  Pulls the HIGH QUALITY (main) streams from go2rtc on localhost, because
#  go2rtc reconnects aggressively where VMS's vendor session silently dies.
#  Writes 30-minute fragmented-MP4 segments, restarts any ffmpeg that dies,
#  and prunes old files so the disk cannot fill.
#
#  Runs as the NSSM service "go2rtc-recorder" (Automatic, restart on failure).
#  Log: <recordings root>\recorder.log
#
#  To add or remove a camera, edit $cameras below.
# ============================================================================

$ErrorActionPreference = 'Continue'

$ff              = 'C:\go2rtc\ffmpeg.exe'
$root            = 'E:\Records\go2rtc'
$segmentMinutes  = 10          # minutes per file
$keepDays        = 20          # delete segments older than this
$pruneEveryMin   = 10
$restartDelaySec = 10

# Folder/clip names (the "_hd" main stream). The recordings browser groups by
# camera IP, so these stay the same even though the H.265 cameras are now
# recorded from their raw HEVC stream - see $recordStream below.
$cameras = @(
  'cam101_hd', # 192.168.1.101  garden front   (H.264 5MP)
  'cam103_hd', # 192.168.1.103  garage/entry   (H.264 1080p)
  'cam104_hd', # 192.168.1.104  driveway       (H.264 1080p)
  'cam105_hd', # 192.168.1.105  hall           (H.265, recorded raw)
  'cam106_hd', # 192.168.1.106  front gate     (H.265, recorded raw)
  'cam108_hd', # 192.168.1.108  side           (H.264 1080p)
  'cam234_hd'  # 192.168.1.234  carport        (H.265, recorded raw)
)

# Which go2rtc stream feeds each recorder. The H.265 cameras are recorded from
# the camera's own stream (camNNN_hd_raw), so go2rtc never runs a transcoder
# around the clock for them: the footage is stored as raw H.265 and the GPU
# stays free. proxy.py converts an H.265 clip to H.264 only when it is played.
# Everything else records itself.
$recordStream = @{
  'cam105_hd' = 'cam105_hd_raw'
  'cam106_hd' = 'cam106_hd_raw'
  'cam234_hd' = 'cam234_hd_raw'
}
function Get-RecordStream($name) {
  if ($recordStream.ContainsKey($name)) { return $recordStream[$name] }
  return $name
}

New-Item -ItemType Directory -Force -Path $root | Out-Null
$logFile = Join-Path $root 'recorder.log'

function Log($msg) {
  $line = "{0}  {1}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $msg
  Write-Host $line
  try { Add-Content -Path $logFile -Value $line -Encoding utf8 } catch {}
}

$procs = @{}
$outFile = @{}
$lastSize = @{}
$lastChange = @{}
$stallMinutes = 3      # restart a recorder whose file hasn't grown in this long
$restarts = @{}        # per camera, so the dashboard can show flapping
foreach ($c in $cameras) { $restarts[$c] = 0 }

# The dashboard (served by proxy.py from C:\go2rtc) reads this to show recording
# state. It lives at a FIXED path on purpose: the recordings themselves can be
# moved to another drive, and the reader should not have to know where.
$statusFile = 'C:\go2rtc\recorder-status.json'
$agg = @{}             # per camera: @{files, bytes, newest, oldest} - refreshed slowly
$lastAgg = [datetime]::MinValue

function Get-DriveOf($path) {
  # 'D:\x\y' -> 'D'; '\\server\share\x' -> '' (UNC, no drive letter)
  $m = [regex]::Match($path, '^([A-Za-z]):')
  if ($m.Success) { return $m.Groups[1].Value.ToUpper() }
  return ''
}

function Write-Status {
  $drive = Get-DriveOf $root
  $freeGB = $null; $usedGB = $null; $totalGB = $null
  if ($drive) {
    try {
      $d = Get-PSDrive $drive -ErrorAction Stop
      $freeGB = [math]::Round($d.Free / 1GB, 2)
      $totalGB = [math]::Round(($d.Used + $d.Free) / 1GB, 2)
      $usedGB = [math]::Round($d.Used / 1GB, 2)
    } catch {}
  }

  $now = Get-Date
  $camList = @()
  foreach ($c in $cameras) {
    $f = $outFile[$c]
    $sz = 0; $wrote = $null
    if ($f -and (Test-Path $f)) {
      $item = Get-Item $f -ErrorAction SilentlyContinue
      if ($item) { $sz = $item.Length; $wrote = $item.LastWriteTime }
    }
    $p = $procs[$c]
    $alive = $false
    try { if ($p) { $p.Refresh(); $alive = -not $p.HasExited } } catch {}
    $silent = if ($lastChange[$c]) { [math]::Round(($now - $lastChange[$c]).TotalSeconds) } else { $null }
    $state = 'down'
    if ($alive) { $state = if ($silent -ne $null -and $silent -gt ($stallMinutes * 60)) { 'stalled' } else { 'recording' } }
    $a = $agg[$c]
    $camList += [ordered]@{
      camera    = $c
      state     = $state
      pid       = if ($alive) { $p.Id } else { $null }
      currentMB = [math]::Round($sz / 1MB, 2)
      written   = if ($wrote) { $wrote.ToString('HH:mm:ss') } else { $null }
      silentSec = $silent
      restarts  = $restarts[$c]
      files     = if ($a) { $a.files } else { $null }
      totalGB   = if ($a) { [math]::Round($a.bytes / 1GB, 3) } else { $null }
      newest    = if ($a) { $a.newest } else { $null }
      oldest    = if ($a) { $a.oldest } else { $null }
    }
  }

  $totFiles = 0; $totBytes = 0
  foreach ($c in $agg.Keys) { $totFiles += $agg[$c].files; $totBytes += $agg[$c].bytes }

  $obj = [ordered]@{
    generated       = $now.ToString('o')
    root            = $root
    drive           = $drive
    segmentMinutes  = $segmentMinutes
    keepDays        = $keepDays
    minFreeGB       = $minFreeGB
    stallMinutes    = $stallMinutes
    disk            = [ordered]@{ freeGB = $freeGB; usedGB = $usedGB; totalGB = $totalGB }
    totals          = [ordered]@{ files = $totFiles; gb = [math]::Round($totBytes / 1GB, 3) }
    cameras         = $camList
  }
  try {
    $obj | ConvertTo-Json -Depth 5 | Set-Content -Path $statusFile -Encoding utf8
  } catch {
    Log ("WARNING   could not write status file: {0}" -f $_.Exception.Message)
  }
}

function Refresh-Aggregates {
  foreach ($c in $cameras) {
    $d = Join-Path $root $c
    $f = @(Get-ChildItem $d -File -Filter *.mp4 -ErrorAction SilentlyContinue)
    $bytes = 0
    foreach ($x in $f) { $bytes += $x.Length }
    $sorted = $f | Sort-Object LastWriteTime
    $agg[$c] = @{
      files  = $f.Count
      bytes  = $bytes
      newest = if ($sorted.Count) { $sorted[-1].LastWriteTime.ToString('yyyy-MM-dd HH:mm') } else { $null }
      oldest = if ($sorted.Count) { $sorted[0].LastWriteTime.ToString('yyyy-MM-dd HH:mm') } else { $null }
    }
  }
}

# The cameras carry PCMA (pcm_alaw) audio, which MP4 cannot store - copying it
# straight through makes ffmpeg refuse to write the file at all
# ("Could not find tag for codec pcm_alaw in stream"). So the video is copied
# untouched and only the tiny 8 kHz audio track is converted to AAC.
# The recordings live on a USB drive, which Windows may not have mounted yet when
# the service starts at boot. Waiting for it is the difference between recording and
# every camera failing silently on its first write.
function Ensure-Target {
  $drive = Get-DriveOf $root
  if (-not $drive) { return (Test-Path $root) }
  if (Test-Path ($drive + ':\')) { return $true }
  Log ("waiting    for drive {0}: (the recordings drive) to appear" -f $drive)
  $waited = 0
  while ($waited -lt 300 -and -not (Test-Path ($drive + ':\'))) { Start-Sleep -Seconds 5; $waited += 5 }
  if (Test-Path ($drive + ':\')) { Log ("ready      drive {0}: appeared after {1}s" -f $drive, $waited); return $true }
  Log ("WARNING    drive {0}: still missing after {1}s" -f $drive, $waited)
  return $false
}
$videoArgs = @('-map','0:v:0','-map','0:a:0?','-c:v','copy','-c:a','aac','-b:a','32k','-ar','8000',
               '-movflags','frag_keyframe+empty_moov+default_base_moof')

function Start-Recorder($name) {
  $dir = Join-Path $root $name
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  $stamp = (Get-Date).ToString('yyyyMMdd_HHmmss')
  $src = Get-RecordStream $name
  # H.265 clips carry a marker so playback knows to convert them for a browser
  # without probing every file (proxy.py _is_hevc / _direct_playable). The
  # folder name is unchanged, so the recordings list and pruning keep working.
  $tag = if ($src -ne $name) { '_h265' } else { '' }
  $out = Join-Path $dir ("{0}{1}_{2}.mp4" -f $name, $tag, $stamp)
  $err = Join-Path $root ($name + '.err.log')

  # One file per run, closed cleanly by ffmpeg's own -t limit, then the
  # supervisor starts the next one. The segment muxer was tried first and it
  # silently wrote nothing here (these streams carry no packet timestamps, which
  # the segment muxer needs to rotate); a plain single-file write is reliable.
  $a = @(
    '-hide_banner','-loglevel','warning',
    '-rtsp_transport','tcp',
    '-i', "rtsp://127.0.0.1:8554/$src"
  ) + $videoArgs + @(
    '-t', ($segmentMinutes * 60),
    '-y', $out
  )
  $p = Start-Process -FilePath $ff -ArgumentList $a -PassThru -NoNewWindow -RedirectStandardError $err
  $procs[$name] = $p
  $script:outFile[$name] = $out
  $script:lastSize[$name] = 0
  $script:lastChange[$name] = Get-Date
  Log ("started   {0}  (pid {1}) from {2} -> {3}" -f $name, $p.Id, $src, (Split-Path $out -Leaf))
}

function Clean-Empty {
  # failed attempts leave 0-byte segments behind; clear them on startup
  $n = 0
  foreach ($cam in $cameras) {
    Get-ChildItem (Join-Path $root $cam) -File -Filter *.mp4 -ErrorAction SilentlyContinue |
      Where-Object { $_.Length -eq 0 } | ForEach-Object { Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue; $n++ }
  }
  if ($n -gt 0) { Log ("cleanup   removed {0} empty segment file(s)" -f $n) }
}

$minFreeGB = 30        # if the recordings drive drops below this, delete oldest until it recovers

function Prune-Old {
  # 1) age-based sweep. Guard the retention value first: with $keepDays missing or
  #    zero, $cut becomes "now" and the sweep below would delete EVERY recording.
  if (-not $keepDays -or [int]$keepDays -lt 1) {
    Log ("WARNING   retention is not set (keepDays='{0}') - SKIPPING the age sweep" -f $keepDays)
  } else {
    $cut = (Get-Date).AddDays(-[int]$keepDays)
    $freed = 0; $n = 0
    foreach ($cam in $cameras) {
      Get-ChildItem (Join-Path $root $cam) -File -Filter *.mp4 -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cut } | ForEach-Object {
          $freed += $_.Length; $n++
          Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
        }
    }
    if ($n -gt 0) { Log ("pruned    age sweep: {0} file(s), {1:N0} MB" -f $n, ($freed/1MB)) }
  }

  # 2) free-space floor. Delete oldest-first until the volume holding the
  #    recordings is back above the floor.
  #
  #    This measured D: no matter where $root pointed. Once the recordings moved
  #    to E: and D: was left failing, the reading came back empty, and in
  #    PowerShell $null -lt 30 is TRUE - so the guard believed the disk was full
  #    and deleted 500 files per sweep, burning its entire cap every time because
  #    re-reading D: never improved.
  #
  #    Two rules now: measure the volume the recordings are really on, and never
  #    treat an unreadable volume as a full one.
  $floorDrive = Get-DriveOf $root
  $freeGB = $null
  if ($floorDrive) {
    try { $freeGB = [math]::Round((Get-PSDrive $floorDrive -ErrorAction Stop).Free / 1GB, 1) }
    catch { $freeGB = $null }
  }
  if ($null -eq $freeGB) {
    Log ("WARNING   cannot read free space on '{0}' - floor check skipped, nothing deleted" -f $floorDrive)
  } elseif ($freeGB -lt $minFreeGB) {
    Log ("WARNING   {0}: free {1} GB is below the {2} GB floor - deleting oldest recordings" -f $floorDrive, $freeGB, $minFreeGB)
    $tries = 0
    while ($tries -lt 500) {
      $freeNow = $null
      try { $freeNow = (Get-PSDrive $floorDrive -ErrorAction Stop).Free / 1GB }
      catch { $freeNow = $null }
      if ($null -eq $freeNow) {
        Log ("          {0}: became unreadable - stopping before deleting more" -f $floorDrive)
        break
      }
      if ($freeNow -ge $minFreeGB) { break }
      $oldest = $null
      foreach ($cam in $cameras) {
        $c = Get-ChildItem (Join-Path $root $cam) -File -Filter *.mp4 -ErrorAction SilentlyContinue |
             Sort-Object LastWriteTime | Select-Object -First 1
        if ($c -and (-not $oldest -or $c.LastWriteTime -lt $oldest.LastWriteTime)) { $oldest = $c }
      }
      if (-not $oldest) { Log "          no recordings left to delete"; break }
      Remove-Item $oldest.FullName -Force -ErrorAction SilentlyContinue
      $tries++
      if ($tries % 25 -eq 0) {
        Log ("          ...deleted {0} files, {1}: free {2} GB" -f $tries, $floorDrive, [math]::Round($freeNow, 1))
      }
    }
    $finalFree = 'n/a'
    try { $finalFree = [math]::Round((Get-PSDrive $floorDrive -ErrorAction Stop).Free / 1GB, 1) } catch {}
    Log ("          deleted {0} oldest file(s); {1}: free now {2} GB" -f $tries, $floorDrive, $finalFree)
  } else {
    Log ("status    {0}: free {1} GB (floor {2} GB), retention {3} days" -f $floorDrive, $freeGB, $minFreeGB, $keepDays)
  }
}

Log "=========== recorder starting ==========="
Log ("cameras: {0}" -f ($cameras -join ', '))
Log ("segment: {0} min   retention: {1} days" -f $segmentMinutes, $keepDays)

# is go2rtc up before we start hammering it?
try {
  $null = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:1984/api/streams' -TimeoutSec 10
  Log "go2rtc API reachable"
} catch { Log "WARNING go2rtc API not reachable yet: $_" }

foreach ($c in $cameras) { New-Item -ItemType Directory -Force -Path (Join-Path $root $c) | Out-Null }

# a restarted service must not leave the previous run's ffmpeg still recording
function Kill-Strays {
  $n = 0
  Get-CimInstance Win32_Process -Filter "Name = 'ffmpeg.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*$root*" } | ForEach-Object {
      try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $n++ } catch {}
    }
  if ($n -gt 0) { Log ("cleanup   stopped {0} orphaned recorder process(es)" -f $n) }
}
Kill-Strays
Start-Sleep -Seconds 2
Clean-Empty

if (Ensure-Target) {
  Clean-Empty
  foreach ($c in $cameras) { Start-Recorder $c }
} else {
  Log "WARNING    starting without the recordings drive - the main loop will keep retrying"
}

$lastPrune = Get-Date
while ($true) {
  Start-Sleep -Seconds 15
  if (-not (Test-Path $root)) {
    if (-not (Ensure-Target)) { Write-Status; continue }
  }
  foreach ($c in $cameras) {
    $p = $procs[$c]
    if ($null -eq $p) { Start-Recorder $c; continue }
    $p.Refresh()
    if ($p.HasExited) {
      $code = try { $p.ExitCode } catch { '?' }
      Log ("EXITED    {0} (code {1}) - starting the next file" -f $c, $code)
      $restarts[$c]++
      Start-Recorder $c
      continue
    }

    # stall watchdog: a recorder whose output stops growing is wedged (the
    # camera dropped the session, go2rtc reconnected, ffmpeg kept waiting).
    # Kill it so the supervisor starts a fresh connection.
    $f = $outFile[$c]
    $sz = 0
    if ($f -and (Test-Path $f)) { $sz = (Get-Item $f).Length }
    if ($sz -ne $lastSize[$c]) {
      $lastSize[$c] = $sz
      $lastChange[$c] = Get-Date
    } elseif (((Get-Date) - $lastChange[$c]).TotalMinutes -gt $stallMinutes) {
      Log ("STALLED   {0} no output for {1} min at {2:N2} MB - restarting" -f $c, $stallMinutes, ($sz/1MB))
      try { $p.Kill() } catch {}
      $restarts[$c]++
      Start-Sleep -Seconds 2
      Start-Recorder $c
    }
  }

  # aggregates are the expensive part (a stat per segment), so refresh them slowly
  if (((Get-Date) - $lastAgg).TotalSeconds -ge 60) { Refresh-Aggregates; $lastAgg = Get-Date }
  Write-Status

  if (((Get-Date) - $lastPrune).TotalMinutes -ge $pruneEveryMin) {
    Prune-Old
    $lastPrune = Get-Date
  }
}
