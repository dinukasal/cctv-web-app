# CCTV on the Windows laptop — what was set up and what was learned

Date: 1 November 2026
Machine: Windows 11 Home laptop, hostname `DESKTOP-XXXXXXX`, user `<you>` (admin)
Addresses: 192.168.1.146 and 192.168.1.224 (both on the LAN; SSH works on .146)
Goal: view all the IP cameras in a web browser, serving from this laptop, starting at boot.

---

## 1. Result

All seven cameras stream, on the LAN and over the public tunnel.

| Where | URL | Mode |
| --- | --- | --- |
| Local network | http://192.168.1.146:8880/ | WebRTC (lowest latency) |
| Public | https://cam.example.com/ | MSE (WebRTC cannot cross a Cloudflare tunnel) |
| Any player on the LAN | rtsp://192.168.1.146:8554/cam101 (add `_hd` for full resolution) | plain RTSP |

14 streams are exposed: each camera as a low-bandwidth substream plus an HD one
(`cam101` / `cam101_hd`, and so on).

## 2. What is installed where

Everything lives in `C:\go2rtc`:

    go2rtc.exe       the relay itself (v1.9.15, windows/amd64)
    ffmpeg.exe       transcodes the H.265 cameras; also read from here by go2rtc
    nssm.exe         service wrapper
    go2rtc.yaml      the camera list and ports  <- the file to edit
    proxy.py         one-origin bridge (see section 5)
    events.py        camera event collector + person detection
    remote/          YOLO detection service that runs on the GPU box's RTX 2060
                     (see REMOTE-DETECTOR.md)
    ADD-CAMERAS.md   how to add an 8th camera (see section 2 of that file)
    *.log            runtime logs
    start-*.cmd      leftovers from the first (scheduled-task) install, harmless

Installer scripts and probe helpers are in `C:\Users\<you>\winupload\`.

**Autostart.** Two Windows services, created with NSSM, both Automatic and set to
restart on failure, so they come up at boot with nobody logged in:

    go2rtc             -> go2rtc.exe -config C:\go2rtc\go2rtc.yaml
    go2rtc-dashboard   -> python.exe proxy.py 8880

They appear in services.msc and respond to `net start` / `net stop`. Firewall rules
were added for 1984, 8554, 8880 (TCP) and 8555 (TCP+UDP), with no remote-address
restriction so any LAN client can connect. After editing `go2rtc.yaml`:

    Restart-Service go2rtc -Force

## 3. Getting the cameras to talk

The cameras turned out to use three different transports. This was the hard part;
the cameras were originally dismissed as unreachable, which was wrong twice over.

| Camera | Transport | Codec | Notes |
| --- | --- | --- | --- |
| .101 | RTSP port 554 | H.264 | user `admin`, password ignored |
| .106 | RTSP port 554 | H.265 | transcoded to H.264 |
| .234 | RTSP port 554 | H.265 | transcoded to H.264 |
| .105 | no RTSP at all — XM SDK on 34567 | H.265 | via go2rtc's built-in `dvrip` module |
| .103 | RTSP port 10554, path `/tcp/av0_0` | H.264 | password REQUIRED |
| .104 | RTSP port 10554, path `/tcp/av0_0` | H.264 | password REQUIRED |
| .108 | RTSP port 10554, path `/tcp/av0_0` | H.264 | password REQUIRED |

The URL shape for the first three:

    rtsp://admin:@<ip>:554/user=admin&password=&channel=1&stream=<0|1>.sdp?real_stream

`stream=0` is the main stream, `stream=1` the low-bandwidth substream.

The URLs for .103/.104/.108 are:

    rtsp://admin:<password>@<ip>:10554/tcp/av0_0     (main, 1080p)
    rtsp://admin:<password>@<ip>:10554/tcp/av0_1     (sub, 640x360)

and they came out of the cameras' own ONVIF media service, which listens on
**port 10080** (not 80 — 80 is closed on those three). Sequence: `GetCapabilities`
-> media endpoint `/onvif/media_service` -> `GetProfiles` -> `GetStreamUri`.

.105 has RTSP switched off entirely and only answers the XM DVRIP/Sofia SDK on
34567, which go2rtc speaks natively:

    dvrip://admin:@192.168.1.105:34567?channel=0&subtype=<0|1>

Because that source is H.265 and ffmpeg cannot read `dvrip://`, the transcode is
chained by stream name inside `go2rtc.yaml`:

    cam105_raw:  "dvrip://admin:@192.168.1.105:34567?channel=0&subtype=1"
    cam105:      "ffmpeg:cam105_raw#video=h264#hardware=cuda"

The three H.265 cameras are transcoded to H.264 on the **GTX 1050 via NVENC**
(`#hardware=cuda`), not on the CPU.

## 4. Two hard-won protocol lessons

**A silent hang is not a negative result.** These cameras drop RTSP requests for
unknown paths without answering — no 401, no 404, just nothing. That is
indistinguishable from "RTSP is not supported", and it produced a wrong
conclusion that cost a lot of time. The fix is to stop guessing paths and ask the
device: ONVIF `GetStreamUri` returns the authoritative URL. Equally, a
hand-rolled DVRIP login packet that is slightly malformed fails as a timeout, which
reads as "unsupported device" rather than "my packet is wrong" — check what the
relay already supports before writing protocol code.

**Not everything in a config file is reachable the same way.** The seven cameras
were all listed together in one `DeviceManager` XML, and looked identical, but
three transports were hiding in there.

## 5. Serving the page and the video from one URL

A Cloudflare tunnel points one hostname at one LAN port. Serving the dashboard from
:8880 alone means the page loads and every stream 404s — which is exactly what
happened on the first attempt at the public URL.

`proxy.py` fixes it: it serves `www\index.html` and reverse-proxies everything
else (stream.html, `/api/*`, MJPEG, WebSocket upgrades) to go2rtc on 1984. One
hostname now serves both the page and the video.

Two bugs were found and fixed in that bridge, worth remembering:

1. **`rbufsize = 0` is mandatory.** With http.server's default buffered read
   stream, the bytes a client sends immediately behind a WebSocket handshake
   (go2rtc's init frame) stay trapped in the buffer, the raw relay never sees
   them, and every player sits at `readyState 0`. The WebSocket handshake still
   returns 101, so everything looks healthy while nothing plays.
2. **MSE over the tunnel, not HLS.** HLS stalls through the tunnel; MSE (over
   WebSocket) plays. On the LAN, WebRTC is best.

The dashboard picks its mode per context and each tile has a mode dropdown
(webrtc / mse / hls) plus automatic fallback, so a browser disagreement can be
worked around without editing code.

Also fixed: tiles scrolled off-screen are blanked to save bandwidth, but
`frame.src = 'about:blank'` is a *truthy* string, so a naive `!frame.src` check
never matched and the tile could never come back. Compare against `'about:blank'`
explicitly.

## 6. The VMS app and its recordings

The vendor recorder is `C:\Program Files (x86)\VMS\VMS.exe` and it runs in the
interactive desktop session. Its configuration is SQLite, under
`C:\Program Files (x86)\VMS\data\users\admin\` — `devices.db` (device list with
addresses, ports, users, password hashes, vendor and login type), `storage.db`
(index of recorded clips), `logs.db`, `recordplan.db`.

It records over the SDK (DVRIP on 34567) for its `Vendor=0` entries and something
different on 10080 for `Vendor=1`, which is why a camera can stream fine over RTSP
while VMS still cannot record it.

Recordings land in `D:\Records\Local Records\Ch1_<ip>\` as
`YYYYMMDDHHMMSSmmm.{avi,h265}`: `.avi` is H.264 for .101/.103/.104/.108, `.h265`
is a **raw HEVC elementary stream** for .105/.106/.234. Neither plays in a browser
as-is — AVI is unsupported and raw HEVC needs a container — so browser playback
would need ffmpeg (remux when the video is already H.264, GPU-transcode otherwise)
plus a listing page and Range serving for scrubbing. Not built yet.

**Our own recorder** (`record-cameras.ps1`) stores the H.265 cameras the same way:
it records their `_raw` stream, so the `cam105_hd` / `cam106_hd` / `cam234_hd`
folders now hold raw H.265 clips (named with an `_h265` marker). go2rtc therefore
runs the NVENC transcode only while a browser is watching a stream, and
`proxy.py` converts an H.265 clip to H.264 on first play. The laptop GPU stays
idle unless somebody is actually viewing.

### Reading the recording state correctly

Segments are 30 minutes long. **The newest file in each folder sits at 0 bytes
until it flushes, and that is normal.** VMS creates the next segment the moment
the previous one closes and writes into it progressively. A 90-second sample
showed no change on any camera, which looked like "recording has stopped"; a
30-second sample immediately after showed .103 +24.9 MB, .106 +222.5 MB,
.234 +83.3 MB. VMS flushes in large bursts, so a short snapshot can easily land
between flushes. To check whether recording is live, sample twice and watch the
currently open segment.

Throughput is roughly 680 MB per 30 minutes across six cameras — about 33 GB per
day. D: had 964 GB free, so roughly a month of history.

Gaps are normal when the laptop is asleep: VMS only records while it is awake.
For 24/7 coverage that machine has to stay on.

### A grey tile in VMS is cosmetic

VMS's live-view decoder is a separate pipeline from its recording engine, so a
tile can go grey while recording continues. Verified: the cameras behind grey
tiles were streaming normally, and their files kept growing.

One caveat: XM cameras allow very few simultaneous streams. go2rtc's log shows
`read tcp ... i/o timeout` when a camera's substream and HD stream are pulled at
once while VMS already holds a connection for recording. The dashboard therefore
defaults to substreams and only opens HD on request. If a VMS tile goes grey right
after opening the dashboard, suspect stream contention rather than a fault.

## 7. Outstanding problem: 192.168.1.101 is not being recorded

VMS stopped recording .101 on 29 October. The camera's own SDK reply explains it:

    { "Name" : "", "Ret" : 205, "SessionID" : "0x00000000" }

In the DVRIP/Sofia protocol **205 means the user or IP is locked** (203 is a wrong
password, 206 blacklisted, 207 already logged in). A session ID of 0x00000000
means no session was granted.

Ruled out by test:

- **Not credentials.** VMS stores .101 identically to the working .105 and .234 —
  same user, same password hash, same port, Vendor 0 — and .105 logs in fine.
- **Not a wrong password.** Five passwords (empty plus a few common guesses)
  all returned the same 205. A wrong password returns 203.
- **Not an IP ban.** The Mac at 192.168.1.197 got the same 205, so the lock is on
  the camera itself, not on the laptop's address.
- **Not a broken camera.** RTSP still streams it perfectly.

So the admin account is locked on the camera. Suggested fix order:

1. Power-cycle the camera — XM firmware usually clears the lock on reboot.
2. If it persists, reset the admin password through the XMEye phone app's
   "Forgot password" (QR code / security questions), which resets and unlocks it.
3. Then update the password in VMS's Device Manager, or it will lock itself again.

There is no remote unlock: the DVRIP login is precisely what is blocked.

Note: the `Ret` code only appears in go2rtc's log (`C:\go2rtc\go2rtc.log`); the
HTTP API reports an empty error string, so an API-only check looks like an
unexplained silence.

Also worth knowing: .101's RTSP ignores authentication entirely (any password works
for user `admin`), which is why it displays perfectly in the dashboard while VMS
cannot touch it — and why the camera cannot be used to discover its own real
password.

## 8. Security notes

- `https://cam.example.com/` is public. Anyone with the URL can watch the
  cameras. Cloudflare Access, or go2rtc's `api`/`rtsp` username and password,
  would close that.
- The .103/.104/.108 password is written into `go2rtc.yaml` in clear text. If it
  is changed on the cameras, those three lines must change too.
- Several cameras listen on plain HTTP with weak authentication; .101/.103/.104/
  .108 accept anything or a single shared password over RTSP.

## 9. Still to do

- A recordings browser for `D:\Records\Local Records` (section 6).
- Resolve the .101 lock (section 7).
- Put access control in front of the public URL (section 8).
- Give the other six cameras real names in the dashboard — only .101 is known
  ("Garden Front", from its on-screen display); the rest are placeholders.
