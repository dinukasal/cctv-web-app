# Adding a camera to this CCTV setup

Working procedure for wiring a new IP camera into the stack in `C:\go2rtc`
(go2rtc relay + `proxy.py` bridge + `record-cameras.ps1` recorder + `events.py`
collector + `www\index.html` dashboard).

Example used throughout: a new camera at **`192.168.1.109`**.

---

## 0. First: find out which transport it speaks — don't guess

The hard-won lesson from `README.md` section 4: these cameras answer *nothing* for
a wrong RTSP path — no 401, no 404, just silence. That is indistinguishable from
"RTSP is not supported", and it produced a wrong conclusion that cost a lot of time.
So probe, then ask the device instead of guessing paths.

Ports to try, in order:

| Port | Meaning |
| --- | --- |
| 554 | plain RTSP (the .101 / .106 / .234 family) |
| 10554 | RTSP with a `/tcp/av0_N` path, password REQUIRED (the .103 / .104 / .108 family) |
| 34567 | XM/Xiongmai DVRIP SDK (what .105 answers when RTSP is switched off) |
| 10080 | ONVIF on the .103/.104/.108 family (port 80 is closed on those) |
| 8899 | ONVIF on the plain-RTSP family |

The authoritative URL comes from the cameras' own ONVIF media service, never from a
guessed path. Sequence: `GetCapabilities` -> media endpoint (`/onvif/media_service`)
-> `GetProfiles` -> **`GetStreamUri`**.

Helpers that already exist in `C:\Users\<you>\winupload\`:

- `probe.ps1` — raw-socket RTSP prober (DESCRIBE / OPTIONS per port, with a timeout),
  the pattern for "does this port answer at all"
- `probe_cam_events.py` — finds each camera's ONVIF *events* service and prints the
  event topics it advertises; it scrapes credentials out of `go2rtc.yaml`
  (`cam_creds()`), so nothing sensitive has to be pasted anywhere

Also useful: the event-endpoint port tells you the family back, in reverse —
see `event_url_for()` in `cctvconfig.py`.

---

## 1. The naming rule (this is the trap)

Everything in the stack derives a camera's **IP from the stream name**, not the
other way round:

- `cctvconfig.py` -> `ip_of_stream()` matches `cam(\d+)` -> `192.168.1.NNN`
- `events.py` -> `camera_table()` builds the stream name as `"cam" + ip.split(".")[-1]`

So the streams **must** be named `cam109` (sub / low-bandwidth) and `cam109_hd`
(main / high quality), where `109` is the last octet of the camera's IP.

Anything else still plays in go2rtc, but the recorder, the tile names and the
event collector will not see it. A camera outside the `192.168.1.x` scheme cannot
be tracked by this tooling at all without changing those two functions.

---

## 2. The four edits

The config is deliberately spread over files with three different owners
(`cctvconfig.py`'s docstring says so). One file per concern:

| File | Owns | Format |
| --- | --- | --- |
| `go2rtc.yaml` | the stream URLs | YAML |
| `cameras.json` | names, tile order, event endpoints | JSON |
| `record-cameras.ps1` | recorder settings + camera list | PowerShell |
| `www\index.html` | the dashboard tiles | JS inside HTML |

### 2.1 `C:\go2rtc\go2rtc.yaml`

Insert after the `cam108_hd` line, under the `streams:` block. Pick the block that
matches the transport found in step 0.

**A) RTSP on 554, password ignored** (the .101 / .106 / .234 family; `stream=1` is
the substream, `stream=0` the main one):

```yaml
  cam109:        "rtsp://admin:@192.168.1.109:554/user=admin&password=&channel=1&stream=1.sdp?real_stream"
  cam109_hd:     "rtsp://admin:@192.168.1.109:554/user=admin&password=&channel=1&stream=0.sdp?real_stream"
```

If the camera sends **H.265**, no browser will play it, so it needs an H.264
version. Give it two extra `_raw` streams carrying the camera's own H.265, then
chain the H.264 versions off them (`#hardware=cuda` = the GTX 1050). The recorder
and the self-scan read the `_raw` streams, so **nothing transcodes around the
clock**; the H.264 versions are produced only while a browser is watching:

```yaml
  cam109_raw:    "rtsp://admin:@192.168.1.109:554/user=admin&password=&channel=1&stream=1.sdp?real_stream"
  cam109_hd_raw: "rtsp://admin:@192.168.1.109:554/user=admin&password=&channel=1&stream=0.sdp?real_stream"
  cam109:        "ffmpeg:cam109_raw#video=h264#hardware=cuda"
  cam109_hd:     "ffmpeg:cam109_hd_raw#video=h264#hardware=cuda"
```

Then add the octet to `H265_OCTETS` in `events.py` and to `$recordStream` in
`record-cameras.ps1` (see 2.3), or the recorder and self-scan will fall back to
reading the transcoded stream and keep the encoder running.

**B) RTSP on 10554 with `/tcp/av0_N`, password required** (the .103 / .104 / .108
family). The password is URL-encoded: `@` is `%40`. `av0_0` is 1080p, `av0_1` is
640x360:

```yaml
  cam109:        "rtsp://admin:YOUR-CAMERA-PASSWORD@192.168.1.109:10554/tcp/av0_1"
  cam109_hd:     "rtsp://admin:YOUR-CAMERA-PASSWORD@192.168.1.109:10554/tcp/av0_0"
```

**C) no RTSP at all — DVRIP only** (the .105 case). ffmpeg cannot speak `dvrip://`,
so the transcode is chained by *stream name* back out of go2rtc itself — the
documented way to chain a transcode:

```yaml
  cam109_raw:    "dvrip://admin:@192.168.1.109:34567?channel=0&subtype=1"
  cam109_hd_raw: "dvrip://admin:@192.168.1.109:34567?channel=0&subtype=0"
  cam109:        "ffmpeg:cam109_raw#video=h264#hardware=cuda"
  cam109_hd:     "ffmpeg:cam109_hd_raw#video=h264#hardware=cuda"
```

### 2.2 `C:\go2rtc\cameras.json`

Adding the camera here is what makes `events.py` pick it up automatically —
`camera_table()` reads `events` for the ONVIF subscriptions, `scan_cameras()`
reads `names` / `order` / `events` for the YOLO self-scan. No change to
`events.py` itself.

The event endpoint can be **inferred** from the RTSP port (`event_url_for()` in
`cctvconfig.py`):

| RTSP port found | event endpoint |
| --- | --- |
| 554 | `http://<ip>:8899/onvif/event_service` |
| 10554 | `http://<ip>:10080/onvif/event_services` |
| DVRIP only (34567) | none — the camera has no ONVIF at all (like .105) |

```json
{
  "events": { "192.168.1.109": "http://192.168.1.109:8899/onvif/event_service" },
  "names":  { "192.168.1.109": "New camera" },
  "order":  [ "192.168.1.109", "192.168.1.108", "...existing..." ]
}
```

Notes:

- `events.py` skips the DVRIP-only camera on purpose; but `scan_cameras()` builds
  its scan list from the *dashboard* cameras, not the event endpoints, so a camera
  with no ONVIF is still scanned for people (the .105 case was the one that yielded
  a person at conf 0.367).
- The tile order can also be set by dragging tiles in the dashboard — it POSTs
  `/api/order` and rewrites this file server-side.

### 2.3 `C:\go2rtc\record-cameras.ps1`

The `$cameras` array starts at line 28. `camNNN_hd` is the high-quality stream —
that is what gets recorded. Keep the comma syntax legal (the last entry has none):

```powershell
  'cam109_hd', # 192.168.1.109  new camera  (H.264 1080p)
```

No re-encoding happens: every recording is a straight stream copy. The H.265
cameras (105/106/234) are recorded from their **`_raw`** stream, so the file is
raw H.265 and go2rtc runs no transcoder for them. Map the folder name to the raw
stream in `$recordStream`, just above the array:

```powershell
$recordStream = @{
  'cam109_hd' = 'cam109_hd_raw'
}
```

Clips written from a raw camera get `_h265` in their filename; `proxy.py` uses
that marker to detect HEVC without probing every file, and converts the clip to
H.264 for the browser only when it is played. Add the octet to `H265_OCTETS` in
`events.py` too, so the self-scan reads the same raw stream.

Other recorder knobs live at the top of the same file: `$root` (currently
`E:\Records\go2rtc`), `$segmentMinutes` = 10, `$keepDays` = 20, `$pruneEveryMin`,
`$restartDelaySec`, `$stallMinutes` = 3. `proxy.py` discovers clips from
`$root` plus `LEGACY_RECORDER_ROOTS` (`C:\`, `D:\`, `E:\Records\go2rtc`), so a new
drive does not have to be configured in code.

### 2.4 `C:\go2rtc\www\index.html` — the step that's easy to forget

**Nothing in the dashboard is discovered.** There is no `/api/streams` reader
anywhere; the tile list is hardcoded in the `const CAMS = [...]` block (around
line 165). A camera added to the other three files simply has no tile:

```js
  { name: "New camera", ip: "192.168.1.109", sub: "cam109", hd: "cam109_hd" },
```

`name` here is only the default — a renamed tile is stored server-side under its
IP in `cameras.json` and wins over this value.

---

## 3. Restart and verify

`cctvconfig.apply()` restarts the three consumer services for you. If you
hand-edited, restart manually:

```powershell
Restart-Service go2rtc -Force
Restart-Service go2rtc-recorder -Force
Restart-Service go2rtc-events -Force
Restart-Service go2rtc-dashboard -Force
```

Then check in this order:

```powershell
# 1. did go2rtc define the streams at all?
curl -s http://127.0.0.1:1984/api/streams | grep -o 'cam109[^,"]*'

# 2. did they connect, or fail? (errors are only visible here)
Get-Content C:\go2rtc\go2rtc.log -Tail 40 | Select-String 'ERR|WRN|timeout|Ret"'

# 3. is it playable?
start http://192.168.1.146:8880/stream.html?src=cam109

# 4. recorder picked it up? (new camera appears once the service restarted)
Get-Content C:\go2rtc\recorder-status.json | Select-String cam109
```

Two verify traps, both documented in `README.md`:

- A DVRIP login failure shows up **only** in `go2rtc.log` — the HTTP API reports an
  empty error string, so an API-only check looks like unexplained silence. `Ret: 205`
  means the user/IP is *locked* on the camera (203 wrong password, 206 blacklisted,
  207 already logged in), with `SessionID: 0x00000000` = no session granted.
- A "silent" RTSP port is not a negative result (step 0).

## 3b. The safer route for several changes at once

`cctvconfig.py` exists precisely because the setup is spread over three files with
three owners. It turns them into one portable JSON document and applies one back.
Nothing is rewritten wholesale: `go2rtc.yaml` keeps its hand-written prologue,
`record-cameras.ps1` is patched by targeted substitution, so settings this module
does not own survive.

```
python cctvconfig.py show              # print the config (passwords masked)
python cctvconfig.py save cctv-config.json
```

Then edit `streams` / `names` / `order` / `events` / `recorder.cameras` in that one
file, and apply it either in Python (`cctvconfig.apply(data)`) or through the
bridge — `PUT /api/config` (`_api_config_put` in `proxy.py`) feeds the upload
straight into `apply()`. What it buys you:

- `validate()` rejects bad stream names / non-absolute recorder roots with a
  human-readable message *before* anything is written
- all three files are copied to `config-backups\<timestamp>\` first, and any
  failure restores them atomically
- `apply()` re-reads the ps1 and confirms the camera list actually took
- the three services are restarted automatically (`go2rtc`, `go2rtc-recorder`,
  `go2rtc-events`)

It still does **not** touch `www\index.html`. That remains a manual edit — or a
worthwhile improvement to make `CAMS` come from `/api/streams`.

## 4. Gotchas specific to *adding* a camera

- **XM cameras allow very few simultaneous streams.** `record-cameras.ps1` holds a
  continuous `_hd` connection for every camera, and the dashboard pulls substreams
  by default and opens HD only on request. An 8th camera makes the
  `read tcp ... i/o timeout` contention worse, not better. Today's
  `recorder-status.json` already shows **32–36 restarts per camera** since 15:35 —
  that flapping is stream contention, and it is worth resolving before adding
  cameras.
- An H.265 camera no longer costs GPU time around the clock: it is recorded and
  self-scanned as raw H.265, and transcoded (NVENC on the GTX 1050) only while a
  browser is actually watching. More H.265 cameras = more CPU decode for the
  self-scan and more on-demand GPU load when viewed.
- `events.py` self-scans every `SCAN_SECONDS = 4` s at ~1% CPU per camera
  (measured on this machine) with `yolov8n.pt`; more cameras = more CPU and more
  YOLO work. `PERSON_CONF = 0.4`, `PRESENCE_GAP = 45`, `MIN_INTERVAL = 60`.
- Disk: the recorder prunes to `$keepDays` and `recorder-status.json` reports
  `minFreeGB` (currently 30) so the drive cannot fill. Budget for the new camera's
  bitrate before adding it.
- Firewall: **nothing to add.** The new camera is a peer on the LAN; only the
  laptop's own ports (1984, 8554, 8880, 8555 TCP+UDP) are opened, and those rules
  carry no remote-address restriction.
- The public tunnel serves **MSE**, not WebRTC (WebRTC cannot cross a Cloudflare
  tunnel), and HLS stalls through the tunnel. The per-tile mode dropdown plus
  automatic fallback covers a browser disagreement without a code change.
- Password in clear text: any camera whose stream URL carries credentials lands in
  `go2rtc.yaml` in plain text — which is why `go2rtc.yaml` is **gitignored** and
  `go2rtc.yaml.example` is the sanitised stand-in. Keep it that way, and remember
  that if a camera's password is changed on the device, those lines must change too.
- Stray leftover: the `ztest:` block at the end of `go2rtc.yaml` (last 3 lines) is a
  probe artifact — it logs into `.101` with a default password and produces log
  noise. Delete it; `ip_of_stream` skips it so it never becomes a camera, but it
  still costs a connection.

## 5. Checklist

- [ ] transport and the authoritative URL obtained from ONVIF `GetStreamUri`, not guessed
- [ ] `camNNN` / `camNNN_hd` naming, `NNN` = last IP octet
- [ ] `go2rtc.yaml`: sub + `_hd` stream (H.265: add `_raw` twins and chain the H.264 versions off them)
- [ ] `events.py`: add the octet to `H265_OCTETS` if the camera is H.265
- [ ] `record-cameras.ps1`: add the `camNNN_hd -> camNNN_hd_raw` mapping to `$recordStream` if H.265
- [ ] `cameras.json`: `names`, `order`, `events` (or omitted for DVRIP-only)
- [ ] `record-cameras.ps1`: `camNNN_hd` in `$cameras`
- [ ] `www\index.html`: entry in `const CAMS` — otherwise there is no tile
- [ ] services restarted (`Restart-Service ... -Force`)
- [ ] `go2rtc.log` tail checked for errors, not just the API
- [ ] recorder state re-read from `recorder-status.json`; remember a newly created
      segment sits at 0 bytes until it flushes, so sample twice before concluding
      anything (VMS also flushes in large bursts)
- [ ] `config-backups\<timestamp>\` looks sane if the `cctvconfig` route was used
