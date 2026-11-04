# Person detection: gpu-host's GPU first, the laptop's GTX 1050 as fallback

`events.py` confirms people with YOLOv8n. That work now runs on the **RTX 2060 in
gpu-host** (`192.168.1.157`) by default. Only when that box is unreachable,
erroring or **busy** does the collector fall back to this laptop's own **GTX
1050**, and it keeps retrying the remote so the big card becomes the primary
again automatically.

    camera frame ──► events.py ──► http://192.168.1.157:8882/detect   (primary)
                          │                    │
                          │                    └─ 503 busy / timeout / refused
                          ▼
                     local YOLOv8n on the GTX 1050                    (fallback)

## Why a service and not just "run YOLO over there"

A GPU is only usable from a process on the machine that owns it. So the remote
runs a tiny HTTP server (`remote/detect_server.py`) that loads YOLOv8n on CUDA
once and answers one JPEG at a time. The laptop sends the frame and gets back
`{"people": n, "confidence": c}`.

## The remote service (gpu-host)

| | |
|---|---|
| Host | `gpu-host` = `192.168.1.157` (also Tailscale `100.94.198.22`) |
| GPU | NVIDIA GeForce RTX 2060 SUPER, 8 GB |
| Python | `/home/gpu-host/plate-blur-venv` (torch 2.14.1+cu130, ultralytics 8.4.184) |
| Code | `/home/gpu-host/yolo-detect/detect_server.py` |
| Model | `/home/gpu-host/yolo-detect/yolov8n.pt` |
| Port | TCP `8882` |
| Service | `yolo-detect.service` (systemd, enabled, `User=gpu-host`) |

Files live in this repo under `remote/`:

    remote/detect_server.py     the HTTP service
    remote/yolo-detect.service  systemd unit
    remote/install-remote.sh    idempotent installer (run as root on gpu-host)

The GPU there is **shared with an LLM** (`strata`, which holds ~7.4 GB). The
detector is deliberately frugal: YOLOv8n adds only ~60 MB. When free VRAM drops
below `DETECT_MIN_FREE_MB` (default 160), or the single inference slot is taken,
or a CUDA OOM happens, the server replies **HTTP 503** immediately instead of
queueing. That is the signal that makes the laptop use its own GPU rather than
waiting behind the LLM.

### Endpoints

    GET  /health   -> {"ok", "device", "gpu", "free_mb", "busy", "stats", ...}
    POST /detect   -> body = JPEG bytes
                      {"people": n, "confidence": c, "ms": t, "device": "cuda"}

### Installing / updating it

    scp remote/detect_server.py remote/yolo-detect.service remote/install-remote.sh \
        yolov8n.pt root@gpu-host:/tmp/yolo-detect-install/
    ssh root@gpu-host \
        "cd /tmp/yolo-detect-install && sed -i 's/\r$//' * && bash install-remote.sh"

`install-remote.sh` copies the files, opens the firewall for `8882` from
`192.168.1.0/24` and `100.64.0.0/10`, then enables and restarts the unit. It is
safe to re-run. Access is root **over Tailscale** (`ssh root@gpu-host`); use your
normal SSH/sudo authentication on that host.

## The client side (`events.py`)

Configuration is at the top of the file:

    REMOTE_DETECT_URLS  default "http://192.168.1.157:8882,http://100.94.198.22:8882"
    REMOTE_TIMEOUT      4.0 s   (a normal answer is ~30 ms)
    REMOTE_RETRY_SEC    30 s    skip the remote this long after a hard failure
    REMOTE_BUSY_SEC     8 s     shorter skip when the remote reports "busy"
    REMOTE_JPEG_QUALITY 85

- URLs are tried in order; the first one that answers wins.
- A `503` means the remote is up but busy, so it is skipped for `REMOTE_BUSY_SEC`.
- A timeout / refused connection / other error skips it for `REMOTE_RETRY_SEC`.
- While the remote is skipped, frames go to the local model, which is loaded
  **lazily** - the GTX 1050 is only touched the first time the remote cannot
  answer, so its VRAM stays free while gpu-host is doing the work.
- Set `REMOTE_DETECT_URLS=""` to force local-only again.

The collector's `/health` reports the routing:

    curl http://127.0.0.1:8881/health   # look at the "detector" block

## Verifying

    # remote is up and not busy
    curl -s http://192.168.1.157:8882/health

    # a real frame through the remote
    curl -s -X POST -H "Content-Type: image/jpeg" \
         --data-binary @E:/Records/go2rtc/event-thumbs/<thumb>.jpg \
         http://192.168.1.157:8882/detect

    # the collector is using it
    curl -s http://127.0.0.1:8881/health   # detector.stats.served climbs, local_loaded=false

    # the laptop's GPU is idle
    nvidia-smi --query-gpu=memory.used --format=csv

Restart the collector after editing `events.py`:

    sudo "C:\go2rtc\nssm.exe" restart go2rtc-events

## Troubleshooting

- **LAN connection to `:8882` hangs (other ports work).** The laptop's IP was
  banned by CrowdSec on gpu-host - usually a false positive from SSH attempts.
  Check and clear it:

      ssh root@gpu-host "cscli decisions list --ip 192.168.1.224; \
          cscli decisions delete --ip 192.168.1.224"

  Docker-published ports (e.g. `:8000`) are not affected because Docker's
  iptables rules bypass ufw/INPUT, which is why they can work while `:8882`
  does not.
- **`busy: true` in `/health`.** Free VRAM is below `DETECT_MIN_FREE_MB` because
  the LLM is using the card. This is expected; the laptop uses its own GPU until
  the card frees up. Lower `DETECT_MIN_FREE_MB` only if you accept the OOM risk.
- **Remote service logs.** `journalctl -u yolo-detect -f` on gpu-host.
- **The laptop never uses the remote.** Check `detector.remote_urls` and
  `detector.remote_down` in `http://127.0.0.1:8881/health`, then test `:8882`
  from the laptop by hand.
