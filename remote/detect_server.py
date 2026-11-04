#!/usr/bin/env python3
"""Remote person detector for the go2rtc camera stack - runs on gpu-host.

The laptop's events.py asks this service first. Only when it is unreachable,
erroring or *busy* does events.py fall back to its own GTX 1050.

This box's RTX 2060 is shared with an LLM (`strata`), so the service deliberately
refuses work (HTTP 503) instead of queueing when free VRAM is low or the single
inference slot is taken. That keeps the laptop's scans snappy and never makes the
detector fight the LLM for memory.

    POST /detect   body = JPEG bytes  -> {"people": n, "confidence": c, "ms": t}
    GET  /health                      -> {"ok": ..., "busy": ..., ...}

Start it with the yolo-detect systemd unit (see yolo-detect.service).
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Bind / model config, overridable by environment.
HOST = os.environ.get("DETECT_HOST", "0.0.0.0")
PORT = int(os.environ.get("DETECT_PORT", "8882"))
MODEL_PATH = os.environ.get("DETECT_MODEL", "/home/gpu-host/yolo-detect/yolov8n.pt")
IMGSZ = int(os.environ.get("DETECT_IMGSZ", "640"))
# Keep this much VRAM in reserve for the LLM and anything else on the card.
# YOLOv8n itself needs only tens of MiB once the CUDA context exists, but the
# context allocation is the risky part, so require headroom before accepting work.
MIN_FREE_MB = int(os.environ.get("DETECT_MIN_FREE_MB", "160"))
MAX_BODY = 12 * 1024 * 1024
PERSON_CLASS = "person"

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import numpy as np  # noqa: E402
import cv2  # noqa: E402
import torch  # noqa: E402
from ultralytics import YOLO  # noqa: E402

MODEL = None
DEVICE = 0 if torch.cuda.is_available() else "cpu"
GPU_NAME = torch.cuda.get_device_name(0) if DEVICE == 0 else "cpu"
SLOT = threading.Lock()            # one inference at a time
STATE_LOCK = threading.Lock()
COOLDOWN_UNTIL = 0.0
INFLIGHT = 0
STATS = {"served": 0, "busy": 0, "errors": 0}


def log(msg):
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def free_mb():
    if DEVICE != 0:
        return None
    free, _total = torch.cuda.mem_get_info()
    return int(free / 1024 / 1024)


def load_model():
    global MODEL
    log(f"loading {MODEL_PATH} on {GPU_NAME} (free {free_mb()} MiB)")
    MODEL = YOLO(MODEL_PATH)
    # Warm up so the first real request does not pay the CUDA-context/cuDNN cost.
    dummy = np.zeros((IMGSZ, IMGSZ, 3), dtype="uint8")
    started = time.time()
    MODEL.predict(dummy, imgsz=IMGSZ, device=DEVICE, verbose=False)
    log(f"warm-up done in {(time.time() - started) * 1000:.0f} ms "
        f"(free {free_mb()} MiB)")


def busy_reason():
    """Why the remote should not take a frame right now, or None if it can."""
    with STATE_LOCK:
        until = COOLDOWN_UNTIL
    if time.monotonic() < until:
        return f"cooling down {until - time.monotonic():.0f}s"
    free = free_mb()
    if free is not None and free < MIN_FREE_MB:
        return f"only {free} MiB free"
    return None


def detect(jpeg):
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("not a decodable image")
    res = MODEL.predict(img, imgsz=IMGSZ, device=DEVICE, verbose=False)[0]
    hits = [float(c) for c, cls in
            zip(res.boxes.conf.tolist(), res.boxes.cls.tolist())
            if res.names[int(cls)] == PERSON_CLASS]
    return len(hits), (round(max(hits), 3) if hits else None)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "yolo-detect"

    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] not in ("/health", "/"):
            self._json(404, {"error": "not found"})
            return
        with STATE_LOCK:
            inflight, stats = INFLIGHT, dict(STATS)
        reason = busy_reason()
        self._json(200, {
            "ok": True, "device": "cuda" if DEVICE == 0 else "cpu",
            "gpu": GPU_NAME, "free_mb": free_mb(), "min_free_mb": MIN_FREE_MB,
            "model_ready": MODEL is not None, "busy": reason is not None,
            "busy_reason": reason, "inflight": inflight, "stats": stats,
        })

    def do_POST(self):
        global INFLIGHT, COOLDOWN_UNTIL
        if self.path.split("?")[0] != "/detect":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            self._json(400, {"error": "bad length"})
            return
        jpeg = self.rfile.read(length)
        if MODEL is None:
            self._json(503, {"busy": True, "reason": "model not ready"})
            return
        reason = busy_reason()
        if reason:
            with STATE_LOCK:
                STATS["busy"] += 1
            self._json(503, {"busy": True, "reason": reason})
            return
        # Never queue behind another frame: answer "busy" so the caller can use
        # its own GPU instead of waiting for this one.
        if not SLOT.acquire(timeout=0.5):
            with STATE_LOCK:
                STATS["busy"] += 1
            self._json(503, {"busy": True, "reason": "inference slot busy"})
            return
        with STATE_LOCK:
            INFLIGHT += 1
        try:
            started = time.time()
            people, conf = detect(jpeg)
            ms = round((time.time() - started) * 1000)
            with STATE_LOCK:
                STATS["served"] += 1
            self._json(200, {"people": people, "confidence": conf, "ms": ms,
                             "device": "cuda" if DEVICE == 0 else "cpu"})
        except torch.cuda.OutOfMemoryError as exc:             # noqa: BLE001
            with STATE_LOCK:
                COOLDOWN_UNTIL = time.monotonic() + 60
                STATS["errors"] += 1
            torch.cuda.empty_cache()
            log(f"CUDA OOM ({exc}) - cooling down 60s")
            self._json(503, {"busy": True, "reason": "cuda oom"})
        except Exception as exc:                               # noqa: BLE001
            with STATE_LOCK:
                STATS["errors"] += 1
            log(f"detect failed: {type(exc).__name__}: {exc}")
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            with STATE_LOCK:
                INFLIGHT -= 1
            SLOT.release()


if __name__ == "__main__":
    load_model()
    log(f"listening on {HOST}:{PORT}")
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    srv.serve_forever()
