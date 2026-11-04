"""Camera event collector + optional person confirmation (the "hybrid").

The cameras already run their own analytics. Rather than analysing seven streams
continuously, this listens for the cameras' own events - which cost nothing - and
only then looks at a frame, optionally confirming with a detector.

Design notes from probing the actual hardware:

  * Both camera families accept a PUSH subscription (wsnt:Subscribe with a
    ConsumerReference). The .103/.104/.108 family cannot do pull-points at all
    ("Action Not Implemented"), so push is the only mechanism that works on all
    of them.
  * They grant their own termination time (30-90 min), so we re-subscribe well
    before that.
  * The callback address must be this machine's CURRENT LAN address; the laptop
    has moved between .146 and .224, so it is discovered at runtime.

Writes:
  C:\\go2rtc\\events.jsonl        one JSON object per event, newest last
  C:\\go2rtc\\event-thumbs\\*.jpg  the frame captured when the event fired

Run:  python events.py [port]        (default 8881)
"""
import base64
import datetime
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
EVENTS_FILE = os.path.join(ROOT, "events.jsonl")
LOCAL_THUMBS = os.path.join(ROOT, "event-thumbs")
RECORDER_SCRIPT = os.path.join(ROOT, "record-cameras.ps1")
RECORDER_STATUS = os.path.join(ROOT, "recorder-status.json")
THUMBS_SUBDIR = "event-thumbs"
NAMES_FILE = os.path.join(ROOT, "cameras.json")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8881
GO2RTC_FRAME = "http://127.0.0.1:1984/api/frame.jpeg?src={stream}"
RESUBSCRIBE_SEC = 900          # cameras grant 30+ min; refresh well inside that
MAX_LINES = 8000               # trim events.jsonl to this many lines
KEEP_DAYS = 14                 # age limit for events and their snapshots

# Self-scanning: look for people ourselves.
#
# The cameras cannot be relied on for this. All six ONVIF cameras push a rule
# called "TestRule" that only ever reports IsMotion=false - roughly three
# notifications a second, forever, never a transition to true - and their
# analytics service does not answer, so the rule cannot be enabled from here.
# Checking frames ourselves is affordable: measured on this machine, decoding a
# stream and running yolov8n costs about 1% of the CPU per camera.
SCAN_SECONDS = 4               # how often each camera is looked at
PERSON_CONF = 0.4              # confidence needed before calling it a person
PRESENCE_GAP = 45              # no person seen for this long ends a presence
MIN_INTERVAL = 60              # never report the same camera twice inside this

# Person detection runs on gpu-host's RTX 2060 first (the URLs below, tried in
# order). Only when none of them answers - unreachable, erroring or reporting
# itself busy - does the detector fall back to this laptop's own GTX 1050. That
# keeps the laptop's small card free while the big one is available, and keeps
# scanning working when it is not. Set REMOTE_DETECT_URLS="" to always run local.
REMOTE_DETECT_URLS = [u.strip() for u in os.environ.get(
    "REMOTE_DETECT_URLS",
    "http://192.168.1.157:8882,http://100.94.198.22:8882").split(",") if u.strip()]
REMOTE_TIMEOUT = 4.0           # seconds per request; a normal answer is ~30 ms
REMOTE_RETRY_SEC = 30          # skip the remote this long after a hard failure
REMOTE_BUSY_SEC = 8            # shorter skip when the remote says it is busy
REMOTE_JPEG_QUALITY = 85

# camera -> (event service URL, go2rtc stream for snapshots).
# .105 is absent on purpose: it is DVRIP-only and has no ONVIF service at all.
CAMERAS = [
    ("192.168.1.101", "http://192.168.1.101:8899/onvif/event_service",  "cam101"),
    ("192.168.1.103", "http://192.168.1.103:10080/onvif/event_services", "cam103"),
    ("192.168.1.104", "http://192.168.1.104:10080/onvif/event_services", "cam104"),
    ("192.168.1.106", "http://192.168.1.106:8899/onvif/event_service",  "cam106"),
    ("192.168.1.108", "http://192.168.1.108:10080/onvif/event_services", "cam108"),
    ("192.168.1.234", "http://192.168.1.234:8899/onvif/event_service",  "cam234"),
]

# The three cameras whose main stream is H.265. Their `_raw` streams carry the
# camera's own HEVC, so the detector reads those and go2rtc does not have to run
# a transcoder just to feed the self-scan. The browser-only H.264 versions are
# untouched; they come up only while somebody is watching.
H265_OCTETS = {"105", "106", "234"}


def analysis_stream(octet, hd=True):
    """go2rtc stream name to read for a camera's picture.

    `hd` picks the main stream over the sub stream. The H.265 cameras expose a
    `_raw` twin of each, which is what the detector should read.
    """
    name = f"cam{octet}_hd" if hd else f"cam{octet}"
    return name + "_raw" if octet in H265_OCTETS else name


NS = ('xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
      'xmlns:wsa="http://www.w3.org/2005/08/addressing" '
      'xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2" '
      'xmlns:tt="http://www.onvif.org/ver10/schema" '
      'xmlns:tns1="http://www.onvif.org/ver10/topics"')

EVENT_LOCK = threading.Lock()
DEDUP_LOCK = threading.Lock()
LAST_SEEN = {}      # (ip, topic) -> monotonic time, for de-duplication
DETECTOR = None
DETECT_TRIES = 0
REMOTE_DOWN_UNTIL = 0.0        # monotonic; the remote is skipped until this time
REMOTE_STATE = {"served": 0, "busy": 0, "failed": 0, "last": None}
REMOTE_LOCK = threading.Lock()


# ---------------------------------------------------------------- helpers
def log(msg):
    print(f"{datetime.datetime.now():%H:%M:%S}  {msg}", flush=True)


def local_ip():
    """This machine's LAN address, which is what the cameras must call back on."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.1.1", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def names():
    try:
        with open(NAMES_FILE, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        n = data.get("names") if isinstance(data, dict) else None
        return n if isinstance(n, dict) else {}
    except (OSError, ValueError):
        return {}


def camera_table():
    """Which cameras to subscribe to.

    Prefers the event endpoints recorded in cameras.json, so restoring a saved
    configuration reconfigures this collector too, and falls back to the built-in
    list on a machine that has never had one written.
    """
    try:
        with open(NAMES_FILE, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        ev = data.get("events") if isinstance(data, dict) else None
    except (OSError, ValueError):
        ev = None
    if isinstance(ev, dict) and ev:
        out = []
        try:
            for ip, url in sorted(ev.items(), key=lambda kv: int(kv[0].split(".")[-1])):
                if url:
                    out.append((ip, url, "cam" + ip.split(".")[-1]))
        except (ValueError, AttributeError):
            out = []
        if out:
            return out
    return CAMERAS


def scan_cameras():
    """Every camera worth looking at - which is more than we can subscribe to.

    Subscribing needs an ONVIF event endpoint, and .105 has none (it speaks only
    DVRIP), but its picture is perfectly detectable: in testing it was the camera
    that yielded a person at 0.367. So the scan list is built from the cameras the
    dashboard knows about, not from the event endpoints - otherwise the one camera
    with no ONVIF would be the only one never examined.
    """
    ips = []
    try:
        with open(NAMES_FILE, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if isinstance(data, dict):
        for source in (data.get("order"), list(data.get("names") or {}),
                       list(data.get("events") or {})):
            for ip in source or []:
                if (isinstance(ip, str) and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip)
                        and ip not in ips):
                    ips.append(ip)
    if not ips:
        ips = [ip for ip, _u, _s in camera_table()]
    return ips


def label_for(ip, stream):
    n = names()
    if ip in n:
        return n[ip]
    return ip


# ------------------------------------------------------- where snapshots live
#
# Snapshots belong beside the recordings: they follow the recorder when its
# location changes and do not fill the system drive. The recorder script is the
# authority on the configured location, with its status file second - the same
# order the bridge uses, so the two agree on where things are.
_root_cache = {"at": 0.0, "value": None}


def _script_root():
    try:
        with open(RECORDER_SCRIPT, "r", encoding="utf-8", errors="replace") as fh:
            m = re.search(r"^\$root\s*=\s*'([^']*)'", fh.read(), re.M)
        return m.group(1).strip() if m else ""
    except OSError:
        return ""


def _status_root():
    try:
        with open(RECORDER_STATUS, "r", encoding="utf-8-sig") as fh:
            return ((json.load(fh) or {}).get("root") or "").strip()
    except (OSError, ValueError):
        return ""


def recorder_root():
    """Where the recorder is configured to write, cached for a moment."""
    now = time.time()
    if now - _root_cache["at"] < 30:
        return _root_cache["value"]
    root = _script_root() or _status_root()
    _root_cache.update(at=now, value=root)
    return root


def thumbs_dir():
    """The live snapshot folder.

    Recordings live on a USB disk that is not always mounted, so this falls back
    to the local folder rather than losing evidence - the same reasoning as the
    recorder's own wait-for-the-drive guard.
    """
    root = recorder_root()
    if root:
        drive = os.path.splitdrive(root)[0]
        if drive and os.path.isdir(drive + os.sep):
            folder = os.path.join(root, THUMBS_SUBDIR)
            try:
                os.makedirs(folder, exist_ok=True)
                return folder
            except OSError:
                pass
    try:
        os.makedirs(LOCAL_THUMBS, exist_ok=True)
    except OSError:
        pass
    return LOCAL_THUMBS


def thumb_path(name):
    """Full path of a snapshot, wherever it actually is - old ones stay in the
    folder they were written to when the recorder's location changes."""
    for folder in (thumbs_dir(), LOCAL_THUMBS,
                   os.path.join(recorder_root() or ROOT, THUMBS_SUBDIR)):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            return path
    return os.path.join(thumbs_dir(), name)


# ---------------------------------------------------------------- detector
def get_detector():
    """Load the local YOLO model on demand.

    This is the *fallback*: it is only touched when the remote GPU cannot
    answer, so the laptop's card stays idle (and its VRAM free) while gpu-host
    is doing the work. Absence is not an error - events are still recorded, just
    without a person confirmation."""
    global DETECTOR, DETECT_TRIES
    if DETECTOR is not None:
        return DETECTOR
    if DETECT_TRIES > 3:
        return None
    DETECT_TRIES += 1
    try:
        from ultralytics import YOLO
        import torch
        dev = 0 if torch.cuda.is_available() else "cpu"
        # absolute path: a service's working directory is not guaranteed, and a
        # missing model would silently stall the first event on a download
        weights = os.path.join(ROOT, "yolov8n.pt")
        DETECTOR = (YOLO(weights), dev)
        log(f"local detector ready ({weights} on {'cuda' if dev == 0 else 'cpu'})")
    except Exception as exc:                                   # noqa: BLE001
        log(f"local detector unavailable ({type(exc).__name__}) - events will be unconfirmed")
        DETECTOR = False
    return DETECTOR or None


def _jpeg_bytes(source):
    """A path, an OpenCV frame or raw bytes -> JPEG bytes for the remote."""
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    if isinstance(source, str):
        with open(source, "rb") as fh:
            return fh.read()
    import cv2
    ok, buf = cv2.imencode(".jpg", source,
                           [int(cv2.IMWRITE_JPEG_QUALITY), REMOTE_JPEG_QUALITY])
    return buf.tobytes() if ok else None


def _remote_down(seconds, reason, busy):
    """Stop asking the remote for a while after it fails or reports busy."""
    global REMOTE_DOWN_UNTIL
    with REMOTE_LOCK:
        REMOTE_DOWN_UNTIL = time.monotonic() + seconds
        REMOTE_STATE["busy" if busy else "failed"] += 1
        if REMOTE_STATE["last"] != reason:
            log(f"remote detector {'busy' if busy else 'unavailable'} ({reason}) - "
                f"using the local GPU for {seconds:g}s")
            REMOTE_STATE["last"] = reason


def remote_confirm(source):
    """Ask gpu-host's GPU. Returns (people, conf), or (None, None) to fall back."""
    if not REMOTE_DETECT_URLS:
        return None, None
    with REMOTE_LOCK:
        if time.monotonic() < REMOTE_DOWN_UNTIL:
            return None, None
    try:
        payload = _jpeg_bytes(source)
    except Exception as exc:                                   # noqa: BLE001
        log(f"could not encode a frame for the remote detector: {exc}")
        return None, None
    if payload is None:
        return None, None
    error = "no address answered"
    for url in REMOTE_DETECT_URLS:
        req = urllib.request.Request(
            url.rstrip("/") + "/detect", data=payload,
            headers={"Content-Type": "image/jpeg"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=REMOTE_TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            with REMOTE_LOCK:
                REMOTE_STATE["served"] += 1
                REMOTE_STATE["last"] = None
            return data.get("people"), data.get("confidence")
        except urllib.error.HTTPError as exc:
            if exc.code == 503:
                # The box is up but its GPU is busy - another address for the
                # same box will not help, so fall back for a short while.
                _remote_down(REMOTE_BUSY_SEC, "gpu busy", True)
                return None, None
            error = f"http {exc.code}"
        except Exception as exc:                               # noqa: BLE001
            error = type(exc).__name__
    _remote_down(REMOTE_RETRY_SEC, error, False)
    return None, None


def confirm_person(source):
    """Person count + best confidence: remote GPU first, local GPU second.

    `source` may be an image path, a JPEG byte string or an OpenCV frame.
    Returns (count, confidence), or (None, None) when neither detector is up.
    """
    people, conf = remote_confirm(source)
    if people is not None:
        return people, conf
    det = get_detector()
    if not det:
        return None, None
    model, dev = det
    try:
        res = model.predict(source, imgsz=640, device=dev, verbose=False)[0]
        hits = []
        for c, cls in zip(res.boxes.conf.tolist(), res.boxes.cls.tolist()):
            if res.names[int(cls)] == "person":
                hits.append(float(c))
        return len(hits), (round(max(hits), 3) if hits else None)
    except Exception as exc:                                   # noqa: BLE001
        log(f"detection failed: {exc}")
        return None, None


def detector_available():
    """True if a detector can be used at all. The remote is assumed reachable
    until proven otherwise, so scanning can start without loading the local
    model (and its VRAM) at all."""
    if REMOTE_DETECT_URLS:
        return True
    return get_detector() is not None


# ---------------------------------------------------------------- storage
def append_event(record):
    try:
        os.makedirs(ROOT, exist_ok=True)
        with _file_lock:
            with open(EVENTS_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            _trim_if_needed()
    except OSError as exc:
        log(f"could not write the event: {exc}")


_file_lock = threading.Lock()


def _trim_if_needed():
    try:
        with open(EVENTS_FILE, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        if len(lines) > MAX_LINES:
            with open(EVENTS_FILE, "w", encoding="utf-8") as fh:
                fh.writelines(lines[-MAX_LINES:])
            log(f"trimmed the event log to {MAX_LINES} lines")
    except OSError:
        pass


JPEG_MIN = 1024          # anything smaller is an error body, not a picture


def snapshot(stream, stamp, ip, tag=""):
    """Grab the current frame for a stream. Returns the filename, or None.

    The bytes are checked rather than the HTTP status: go2rtc answers with a tiny
    error body (observed: 37 bytes) instead of an error code when a stream has no
    frame ready, and saving that would file a corrupt snapshot as evidence.
    """
    try:
        folder = thumbs_dir()
        name = f"{stamp}{tag}_{ip.replace('.', '-')}.jpg"
        path = os.path.join(folder, name)
        with urllib.request.urlopen(GO2RTC_FRAME.format(stream=stream), timeout=15) as r:
            data = r.read()
        if len(data) < JPEG_MIN or data[:2] != b"\xff\xd8":
            log(f"{stream}: no usable frame ({len(data)} bytes)")
            return None
        with open(path, "wb") as fh:
            fh.write(data)
        return name
    except Exception as exc:                                   # noqa: BLE001
        log(f"snapshot failed for {stream}: {exc}")
        return None


def _better(new, old):
    """Is `new` stronger evidence than `old`?"""
    new_people, old_people = new.get("people"), old.get("people")
    if new_people and not old_people:
        return True
    if old_people and not new_people:
        return False
    if (new_people or 0) != (old_people or 0):
        return (new_people or 0) > (old_people or 0)
    return (new.get("confidence") or 0) > (old.get("confidence") or 0)


def _discard(name):
    try:
        os.remove(thumb_path(name))
    except OSError:
        pass


def event_evidence(ip, stamp):
    """Sample the camera around an event; keep the frame with the best evidence.

    The HD stream is preferred for two measured reasons: it already has a live
    consumer, so it answers in ~0.15s with a current frame where a sub stream with
    no consumer takes 1-2.4s to produce one - and late evidence misses people who
    have already walked on. It is also the stream on which the person was actually
    detected in testing. Two samples are taken because the subject is usually
    still moving when the camera's own event fires; the search stops as soon as
    someone is confirmed.
    """
    octet = ip.split(".")[-1]
    attempts = [(analysis_stream(octet, True), "_a"),
                (analysis_stream(octet, True), "_b"),
                (analysis_stream(octet, False), "_sub")]
    best, tried = None, 0
    for i, (stream, tag) in enumerate(attempts):
        time.sleep(1.5 if i == 0 else 1.2)
        name = snapshot(stream, stamp, ip, tag)
        if not name:
            continue
        tried += 1
        people, conf = confirm_person(thumb_path(name))
        cand = {"thumb": name, "people": people, "confidence": conf, "stream": stream}
        if best is None or _better(cand, best):
            if best and best.get("thumb"):
                _discard(best["thumb"])
            best = cand
        else:
            _discard(name)
        if people:
            break                                   # a confirmed person ends it
    if best is None:
        return {"thumb": None, "people": None, "confidence": None,
                "views": 0, "stream": None}
    best["views"] = tried
    return best


# ---------------------------------------------------------------- receiving
def local(tag):
    return tag.split("}")[-1]


def parse_notification(node, ip):
    """Pull the interesting fields out of one NotificationMessage element.

    Takes an already-parsed ELEMENT, not a string: extracting the inner XML and
    reparsing it fails because the namespace declarations live on the envelope.
    """
    topic = ""
    items = {}
    when = ""
    for el in node.iter():
        tag = local(el.tag)
        if tag == "Topic":
            topic = (el.text or "").strip()
        elif tag == "SimpleItem":
            items[el.get("Name") or "?"] = el.get("Value")
        elif tag == "Message":
            when = (el.text or "").strip()
    kind = "unknown"
    if "CellMotionDetector" in topic or "MotionAlarm" in topic:
        kind = "motion"
    elif "ObjectsInside" in topic:
        kind = "intrusion"
    elif "LineDetector" in topic or "Crossed" in topic:
        kind = "line-crossing"
    elif "Tamper" in topic:
        kind = "tamper"
    elif "SignalLoss" in topic:
        kind = "signal-loss"
    state = None
    for key in ("IsMotion", "State", "IsInside", "IsCrossed", "LogicalState"):
        if key in items:
            state = items[key]
            break
    return {"ip": ip, "topic": topic, "kind": kind, "state": state,
            "items": items, "camera_time": when}


def handle_event(info):
    """A camera event arrived - record it, and confirm it with the detector."""
    ip = info["ip"]
    key = (ip, info["kind"])
    rising = str(info.get("state", "")).lower() in ("true", "1", "on")
    # One camera can emit a burst of notifications for a single movement, and they
    # arrive on separate threads, so the check and the record must be atomic or
    # several of them slip past together.
    with DEDUP_LOCK:
        now = time.monotonic()
        last = LAST_SEEN.get(key, 0)
        # motion keeps firing while it lasts; only record a fresh rise, and treat
        # the pause after a rise as the same movement rather than a new one
        if rising and now - last < 5:
            return
        if not rising and now - last < 30:
            return
        LAST_SEEN[key] = now
    if not rising:
        return

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    stream = next((s for i, _u, s in camera_table() if i == ip), None)
    # sample a couple of frames and let the detector pick the best evidence
    ev = event_evidence(ip, stamp) if stream else {}
    record = {
        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ip": ip,
        "camera": label_for(ip, stream or ""),
        "kind": info["kind"],
        "state": info.get("state"),
        "topic": info["topic"],
        "thumb": ev.get("thumb"),
        "people": ev.get("people"),
        "confidence": ev.get("confidence"),
        "views": ev.get("views"),
        "detectStream": ev.get("stream"),
        "source": "camera",
    }
    append_event(record)
    extra = "" if record["people"] is None else f"  person={record['people']}"
    log(f"EVENT {record['camera']} {record['kind']} state={record['state']} "
        f"thumb={record['thumb']}{extra} (checked {record['views']} frame(s))")


def handle_event_safe(info):
    """Never let one malformed event kill the handler thread silently."""
    try:
        handle_event(info)
    except Exception as exc:                                   # noqa: BLE001
        log(f"event handling failed: {type(exc).__name__}: {exc}")


class Receiver(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "cctv-events"

    def log_message(self, fmt, *args):
        pass                                   # notifications are frequent; stay quiet

    def _send(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/health", "/"):
            # renewal: how long until this camera is re-subscribed. Cameras grant
            # very different lifetimes, so this is scheduled per camera rather
            # than on one global interval.
            now = time.monotonic()
            renewals = {}
            for ip in SUB_STATE:
                if ip == "last_ip":
                    continue
                due = NEXT_SUB.get(ip)
                renewals[ip] = (round(due - now) if due is not None else None)
            self._send(200, json.dumps({
                "ok": True, "ip": local_ip(), "subscriptions": SUB_STATE,
                "renew_in_seconds": renewals,
                "events_file": EVENTS_FILE,
                "snapshots_dir": thumbs_dir(),
                "recorder_root": recorder_root(),
                "subscribed_cameras": [ip for ip, _u, _s in camera_table()],
                "scanned_cameras": scan_cameras(),
                "person_conf": PERSON_CONF,
                "scan_seconds": SCAN_SECONDS,
                "presence_gap": PRESENCE_GAP,
                "detector": {
                    "remote_urls": REMOTE_DETECT_URLS,
                    "remote_down": time.monotonic() < REMOTE_DOWN_UNTIL,
                    "stats": dict(REMOTE_STATE),
                    "local_loaded": isinstance(DETECTOR, tuple),
                },
            }).encode(), "application/json")
            return
        self._send(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        text = raw.decode("utf-8", "replace")
        path = urllib.parse.urlsplit(self.path).path

        if path != "/onvif/notify":
            self._send(404)
            return

        # The camera POSTs from its own address, which identifies it reliably -
        # better than hunting for a Source item inside the payload.
        sender = self.client_address[0]
        known = {ip for ip, _u, _s in CAMERAS}
        ip = sender if sender in known else None

        count = 0
        nodes = []
        try:
            root = ET.fromstring(text)          # namespaces live on the envelope
            nodes = [el for el in root.iter() if local(el.tag) == "NotificationMessage"]
        except ET.ParseError as exc:
            log(f"unparseable notification from {sender}: {exc}")

        if not nodes:
            # some firmware sends a bare message; fall back to a text scan
            flat = re.sub(r"\s+", " ", text)
            info = {"ip": ip, "topic": (re.search(r"Topic[^>]*>([^<]+)<", flat) or [None, ""])[1],
                    "kind": "unknown", "state": None, "items": {}, "camera_time": ""}
            if "Motion" in info["topic"]:
                info["kind"] = "motion"
                info["state"] = "true" if "true" in flat.lower() else "false"
                threading.Thread(target=handle_event_safe, args=(info,), daemon=True).start()
                count = 1
        else:
            for node in nodes:
                if not ip:
                    # last resort: a Source item naming the camera
                    for el in node.iter():
                        if local(el.tag) == "SimpleItem" and (el.get("Name") or "") == "Source":
                            cand = re.search(r"(\d+\.\d+\.\d+\.\d+)", el.get("Value") or "")
                            if cand:
                                ip = cand.group(1)
                if not ip:
                    continue
                info = parse_notification(node, ip)
                count += 1
                # Every camera repeats the same state about three times a second, so
                # logging each one would bury everything else. One line per camera
                # per state per minute is enough to see what they are doing.
                notify_key = (ip, info.get("state"))
                now_mono = time.monotonic()
                if now_mono - NOTIFY_SEEN.get(notify_key, 0) > 60:
                    NOTIFY_SEEN[notify_key] = now_mono
                    log(f"notify {ip}: {info['kind']} state={info.get('state')} "
                        f"items={info.get('items')}")
                threading.Thread(target=handle_event_safe, args=(info,), daemon=True).start()

        self._send(200, b"ok")


SENDER_HINT = {}       # camera ip -> its own address as seen by us (never differs here)
SUB_STATE = {}
NOTIFY_SEEN = {}       # (camera, state) -> when it was last logged


NEXT_SUB = {}          # camera ip -> monotonic time its subscription should be renewed


def granted_seconds(term):
    """How long a camera says the subscription lasts. Their clocks disagree with
    ours (the .101/.106/.234 family returns ~3 min, the .103 family ~60), so this
    trusts the reply and falls back to a short renewal when the answer is unusable."""
    try:
        when = datetime.datetime.strptime(term, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
        delta = (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        if delta <= 0:
            return 120.0
        return min(RESUBSCRIBE_SEC, max(60.0, delta * 0.8))
    except (TypeError, ValueError):
        return 300.0


def subscribe_one(ip, url):
    """(Re)create a push subscription on one camera."""
    me = local_ip()
    callback = f"http://{me}:{PORT}/onvif/notify"
    action = "http://docs.oasis-open.org/wsn/bw-2/NotificationProducer/SubscribeRequest"
    body = (f'<wsnt:Subscribe>'
            f'<wsnt:ConsumerReference><wsa:Address>{callback}</wsa:Address>'
            f'</wsnt:ConsumerReference>'
            f'<wsnt:InitialTerminationTime>PT3600S</wsnt:InitialTerminationTime>'
            f'</wsnt:Subscribe>')
    env = (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope {NS}>'
           f'<s:Header><wsa:Action>{action}</wsa:Action>'
           f'<wsa:MessageID>urn:uuid:{uuid.uuid4()}</wsa:MessageID>'
           f'<wsa:To>{url}</wsa:To></s:Header><s:Body>{body}</s:Body></s:Envelope>')
    req = urllib.request.Request(url, data=env.encode(), headers={
        "Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"',
        "SOAPAction": f'"{action}"'})
    # Renew early rather than exactly on time: a lapsed subscription is a silent
    # gap in the event feed, and the cameras' clocks cannot be trusted to agree
    # with ours, so each one gets its own deadline taken from its own reply.
    seconds = 300.0
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            reply = r.read().decode("utf-8", "replace")
        if "Fault" in reply:
            reason = re.search(r"<[^>]*Text[^>]*>([^<]*)<", reply)
            SUB_STATE[ip] = f"fault: {reason.group(1) if reason else '?'}"
            seconds = 120.0                       # retry sooner than a good grant
        else:
            term = re.search(r"TerminationTime[^>]*>([^<]+)<", reply)
            granted = term.group(1) if term else "?"
            SUB_STATE[ip] = f"subscribed until {granted}"
            SUB_STATE["last_ip"] = ip
            seconds = granted_seconds(granted)
    except Exception as exc:                               # noqa: BLE001
        SUB_STATE[ip] = f"error: {type(exc).__name__}"
        seconds = 120.0
    NEXT_SUB[ip] = time.monotonic() + seconds
    log(f"subscribe {ip}: {SUB_STATE.get(ip)} (renew in {seconds:.0f}s)")
    return seconds


def subscribe_all():
    """Subscribe to every camera, then keep each one renewed on its own schedule."""
    for ip, url, _stream in camera_table():
        subscribe_one(ip, url)


def prune():
    """Honour KEEP_DAYS for snapshots and log lines.

    The line-count cap alone is not enough: snapshots are never overwritten, so
    without an age limit they would accumulate for the life of the machine.
    """
    cutoff = time.time() - KEEP_DAYS * 86400
    removed = dropped = 0
    # Both folders: snapshots written before the recorder's location changed stay
    # where they were, so they have to expire there too.
    for folder in {thumbs_dir(), LOCAL_THUMBS,
                   os.path.join(recorder_root() or ROOT, THUMBS_SUBDIR)}:
        try:
            for name in os.listdir(folder):
                path = os.path.join(folder, name)
                try:
                    if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                        os.remove(path)
                        removed += 1
                except OSError:
                    pass
        except OSError:
            pass
    try:
        with _file_lock:
            with open(EVENTS_FILE, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
            keep = []
            for line in lines:
                try:
                    rec = json.loads(line)
                    when = datetime.datetime.strptime(
                        rec.get("time", ""), "%Y-%m-%d %H:%M:%S").timestamp()
                except (ValueError, AttributeError):
                    keep.append(line)          # unparseable - don't silently drop it
                    continue
                if when >= cutoff:
                    keep.append(line)
                else:
                    dropped += 1
            if dropped:
                with open(EVENTS_FILE, "w", encoding="utf-8") as fh:
                    fh.writelines(keep[-MAX_LINES:])
    except OSError:
        pass
    if removed or dropped:
        log(f"pruned {removed} snapshot(s) and {dropped} event(s) older than {KEEP_DAYS} days")


def record_person(ip, stream, name, people, conf):
    """Write an event we produced ourselves, rather than one a camera sent."""
    record = {
        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ip": ip,
        "camera": label_for(ip, stream),
        "kind": "person",
        "state": "true",
        "topic": "self/scan",
        "thumb": name,
        "people": people,
        "confidence": conf,
        "views": 1,
        "detectStream": stream,
        "source": "detector",
    }
    append_event(record)
    log(f"EVENT {record['camera']} person x{people} conf={conf} (found by us, "
        f"{stream}) -> {name}")


def _open_capture(ip):
    """Open a persistent stream for a camera. Returns (capture, stream name).

    Keeping the connection open is what makes the chosen interval real. The
    one-shot frame endpoint costs 1-3 seconds per grab because it waits for a
    fresh frame, which across seven cameras stretched a "4 second" pass to ~17
    seconds and would have missed anyone crossing the view quickly.
    """
    try:
        import cv2
    except ImportError:
        return None, None
    octet = ip.split(".")[-1]
    names = (analysis_stream(octet, True), analysis_stream(octet, False))
    for name in names:                                    # HD first, sub as fallback
        cap = cv2.VideoCapture(f"rtsp://127.0.0.1:8554/{name}")
        if cap.isOpened():
            return cap, name
        cap.release()
    return None, None


def _latest_frame(cap, flush=3):
    """The newest frame, dropping the ones already sitting in the buffer."""
    for _ in range(flush):
        if not cap.grab():
            return None
    ok, frame = cap.read()
    return frame if ok else None


def _write_evidence(frame, ip, stamp):
    """Save the frame that triggered an event."""
    try:
        import cv2
        os.makedirs(thumbs_dir(), exist_ok=True)
        name = f"{stamp}_scan_{ip.replace('.', '-')}.jpg"
        cv2.imwrite(os.path.join(thumbs_dir(), name), frame,
                    [int(cv2.IMWRITE_JPEG_QUALITY), 88])
        return name
    except Exception as exc:                                   # noqa: BLE001
        log(f"could not save evidence for {ip}: {exc}")
        return None


def scan_loop():
    """Look for people in each camera ourselves.

    The cameras' own rules never report motion, so this is what actually produces
    person events. Streams are held open and sampled every SCAN_SECONDS, and one
    *presence* is reported once: somebody sitting in view for an hour is one event,
    not one a minute, which also keeps the snapshots from filling the disk. A
    presence ends after PRESENCE_GAP with nobody seen, so leaving and returning
    reports again.
    """
    if not detector_available():
        log("no detector available - self-scanning is off")
        return
    log(f"self-scanning every {SCAN_SECONDS}s, person confidence >= {PERSON_CONF}, "
        f"one event per presence (gap {PRESENCE_GAP}s)")
    seen = {}          # camera -> when a person was last seen there
    reported = {}      # camera -> when we last recorded one
    captures = {}      # camera -> (open capture, stream name)
    gained = {}
    cycles = 0
    while True:
        cycles += 1
        started = time.time()
        for ip in scan_cameras():
            entry = captures.get(ip)
            if entry is None:
                cap, stream = _open_capture(ip)
                if cap is None:
                    continue
                captures[ip] = (cap, stream)
                log(f"scan: opened {stream} for {label_for(ip, stream)}")
            cap, stream = captures[ip]
            frame = _latest_frame(cap)
            if frame is None:
                cap.release()
                captures.pop(ip, None)
                log(f"scan: {stream} stopped delivering frames - will reconnect")
                continue
            gained[ip] = gained.get(ip, 0) + 1
            people, conf = confirm_person(frame)
            now = time.monotonic()
            if people and (conf or 0) >= PERSON_CONF:
                was_away = now - seen.get(ip, 0) > PRESENCE_GAP
                seen[ip] = now
                if was_away and now - reported.get(ip, 0) >= MIN_INTERVAL:
                    reported[ip] = now
                    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                    name = _write_evidence(frame, ip, stamp)
                    if name:
                        record_person(ip, stream, name, people, conf)
        # A quiet summary every few minutes: which cameras are really being looked
        # at, and whether any has stopped handing over frames. Without it, a camera
        # that is silently failing looks exactly like one with nothing in view.
        if cycles % 75 == 0:
            cams = scan_cameras()
            ok = [ip for ip in cams if gained.get(ip)]
            bad = [ip for ip in cams if not gained.get(ip)]
            log(f"scan summary after {cycles} cycles ({time.time()-started:.1f}s/pass): "
                f"frames ok from {len(ok)}/{len(cams)}"
                + (f", no frames from {', '.join(bad)}" if bad else "")
                + (f", people in view: {', '.join(label_for(i, '') for i in seen)}"
                   if seen else ""))
        elapsed = time.time() - started
        time.sleep(max(0.5, SCAN_SECONDS - elapsed))


def subscriber_loop():
    """Renew each camera on its own deadline.

    A single global sleep cannot work here: the cameras grant wildly different
    lifetimes (the .101/.106/.234 family answered ~3 min, the .103 family ~60),
    so one interval either lapses the short ones or hammers the long ones.
    """
    subscribe_all()
    prune()
    next_prune = time.monotonic() + 3600
    while True:
        now = time.monotonic()
        for ip, url, _stream in camera_table():
            due = NEXT_SUB.get(ip)
            if due is None or now >= due:
                subscribe_one(ip, url)
        if now >= next_prune:
            prune()
            next_prune = now + 3600
        time.sleep(20)


if __name__ == "__main__":
    import shutil
    os.makedirs(LOCAL_THUMBS, exist_ok=True)
    os.makedirs(thumbs_dir(), exist_ok=True)
    log(f"collector starting - callback will be http://{local_ip()}:{PORT}/onvif/notify")
    _where = thumbs_dir()
    log(f"snapshots go to {_where}"
        + (f" (recorder root {recorder_root()})" if recorder_root()
           else " (no recorder root configured - using the local folder)"))
    table = camera_table()
    log(f"subscribing to {len(table)} camera(s): " + ", ".join(ip for ip, _u, _s in table))
    _scan = scan_cameras()
    log(f"scanning {len(_scan)} camera(s): " + ", ".join(_scan))
    if REMOTE_DETECT_URLS:
        log(f"detector: remote {', '.join(REMOTE_DETECT_URLS)} first, "
            f"local GPU as fallback")
    else:
        log(f"detector: local {'ready' if get_detector() else 'unavailable'}")
    threading.Thread(target=subscriber_loop, daemon=True).start()
    threading.Thread(target=scan_loop, daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Receiver)
    httpd.daemon_threads = True
    log(f"listening on :{PORT}")
    httpd.serve_forever()
