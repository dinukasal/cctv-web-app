"""
One-origin bridge + recordings browser for the CCTV setup.

Listens on :8880 and:

  * serves the dashboard page and any other file under C:\\go2rtc\\www
  * proxies everything else (stream.html, /api/*, video-*.js, MJPEG/WS streams)
    to go2rtc on 127.0.0.1:1984, including WebSocket upgrades - so a single
    public hostname serves both the page and the live video.
  * lists and plays recorded clips:
      GET /api/recordings         JSON index of every clip
      GET /rec/stream?f=<id>      play a clip (HTTP Range, so it can be seeked)
      GET /api/recstatus?f=<id>   conversion progress
      GET /recordings             the recordings page

Playback strategy: a clip that is already browser-friendly (our recorder's MP4)
is served straight from disk with Range support - instant and seekable. Anything
else (VMS .avi, raw HEVC .h265) is converted once into a cache file, stream-copied
when the video is already H.264 and transcoded on the GPU otherwise - then served
with Range. First play costs a few seconds, every play after that is instant.

Run:  python proxy.py [port]
"""
import base64
import datetime
import hashlib
import json
import os
import re
import select
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

try:
    import cctvconfig                      # save/restore the whole camera config
except ImportError:                        # the bridge still runs without it
    cctvconfig = None

ROOT = os.path.dirname(os.path.abspath(__file__))
WWW = os.path.join(ROOT, "www")
UP_HOST, UP_PORT = "127.0.0.1", 1984
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8880

FFMPEG = r"C:\go2rtc\ffmpeg.exe"
CACHE = r"C:\Records\.webplay"
# Converted copies used to expire after 24 hours, so a clip opened today had to be
# converted all over again tomorrow - which made old recordings slow every single
# time rather than only the first time. The age is now a generous backstop and the
# size cap does the real work.
CACHE_MAX_AGE_HOURS = 720          # a month
CACHE_MAX_GB = 12

# Recorder integration. The recorder script publishes its own state as JSON, at a
# path fixed next to this script so the reader does not need to know where the
# recordings currently live (that location is editable, see /api/recorder/path).
STATUS_FILE = os.path.join(ROOT, "recorder-status.json")
RECORDER_SCRIPT = os.path.join(ROOT, "record-cameras.ps1")
RECORDER_SERVICE = "go2rtc-recorder"

# Camera events, written by the collector (events.py) and read here for the UI.
EVENTS_FILE = os.path.join(ROOT, "events.jsonl")
THUMBS_DIR = os.path.join(ROOT, "event-thumbs")

# Folders holding clips. The recorder's own target is read live from its script and
# status file (it can be moved from the dashboard), and anywhere it has written
# before stays listed so older clips remain playable after a move.
VMS_SOURCES = [
    ("VMS C:", r"C:\Records\Local Records"),
    ("VMS D:", r"D:\Records\Local Records"),
    ("VMS E:", r"E:\Records\Local Records"),
]
LEGACY_RECORDER_ROOTS = [
    r"D:\Records\go2rtc",
    r"C:\Records\go2rtc",
    r"E:\Records\go2rtc",
]
_src_cache = {"at": 0.0, "value": None}


_thumb_cache = {"at": 0.0, "value": None}


def _thumb_folders():
    """Every folder an event snapshot may live in.

    The collector writes them beside the recordings, so they follow the recorder
    when its location changes, and falls back to the local folder when that drive
    is not mounted - so both places have to be searched.
    """
    now = time.time()
    if _thumb_cache["value"] is not None and now - _thumb_cache["at"] < 10:
        return _thumb_cache["value"]
    folders = [THUMBS_DIR]
    root = _script_settings().get("root") or _read_status().get("root") or ""
    if root:
        folders.insert(0, os.path.join(root, "event-thumbs"))
    _thumb_cache.update(at=now, value=folders)
    return folders


def _recorder_roots():
    """Every folder the recorder writes - or has written - MP4s to.

    The script is the authority on where it is *configured* to write; the status
    file only records where the running instance was pointed when it started, so
    it goes second (a target changed since the last restart would otherwise be
    mislabelled as a former location).
    """
    roots = []
    for candidate in ([_script_settings().get("root"), _read_status().get("root")]
                      + LEGACY_RECORDER_ROOTS):
        if candidate and candidate not in roots:
            roots.append(candidate)
    return roots


def _record_sources(force=False):
    """[(label, folder)] for the recordings list.

    Current target first, then old locations, then the vendor archives. Sources
    whose folder is not present are dropped, so moving the recorder does not leave
    broken entries behind.
    """
    now = time.time()
    if not force and _src_cache["value"] is not None and now - _src_cache["at"] < 5:
        return _src_cache["value"]
    roots = _recorder_roots()
    current = roots[0] if roots else ""
    out = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        drive = os.path.splitdrive(root)[0].rstrip(":\\") or "?"
        out.append((f"recorder {drive}:" if root == current else f"recorder (old {drive}:)", root))
    for label, folder in VMS_SOURCES:
        if os.path.isdir(folder):
            out.append((label, folder))
    _src_cache.update(at=now, value=out)
    return out
CLIP_EXTS = (".mp4", ".avi", ".h265", ".ts")

# Dashboard settings - camera names and tile order - editable from the page and
# shared by every device that opens it. Keyed by camera IP address.
NAMES_FILE = os.path.join(ROOT, "cameras.json")


def _load_config():
    """{'names': {ip: name}, 'order': [ip, ...], 'events': {ip: url}}.

    Tolerates the older flat {ip: name} shape, which is what existing installs
    have on disk, so nothing already named gets lost. Any other keys are carried
    through untouched: 'events' holds the ONVIF event endpoints the collector
    subscribes to, and a rename here must not quietly delete them.
    """
    try:
        with open(NAMES_FILE, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {"names": {}, "order": []}
    if not isinstance(raw, dict):
        return {"names": {}, "order": []}
    if "names" not in raw and "order" not in raw:
        return {"names": raw, "order": []}          # legacy flat map
    cfg = dict(raw)                                 # keep unknown keys
    cfg["names"] = raw.get("names") if isinstance(raw.get("names"), dict) else {}
    cfg["order"] = [str(x) for x in (raw.get("order") or [])
                    if isinstance(raw.get("order"), list)] or []
    return cfg


def _save_config(cfg):
    """Write names/order while preserving anything else already in the file."""
    merged = _load_config()
    merged.update({k: v for k, v in cfg.items() if k in ("names", "order")})
    tmp = NAMES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=2, sort_keys=True, ensure_ascii=False)
    os.replace(tmp, NAMES_FILE)


def _load_names():
    return _load_config()["names"]


def _ip_of(camera, name):
    """Camera IP, from a VMS folder ('Ch1_192.168.1.104') or a go2rtc stream
    name ('cam104_hd_...' - the number is the last octet: cam104 -> .104)."""
    m = re.search(r"\d{1,3}(?:\.\d{1,3}){3}", camera or "")
    if m:
        return m.group(0)
    m = re.match(r"cam(\d+)$", (camera or "").split("_")[0])
    if m:
        return "192.168.1." + m.group(1)
    return ""

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".mp4": "video/mp4",
}
HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade",
              "proxy-authenticate", "proxy-authorization", "te", "trailer"}

_conv_state = {}          # cache path -> {"state","bytes","error"}
_conv_lock = threading.Lock()


# ---------------------------------------------------------------- recordings

def _clip_id(path):
    return base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii").rstrip("=")


def _clip_path(cid):
    return base64.urlsafe_b64decode(cid + "=" * (-len(cid) % 4)).decode("utf-8")


def _allowed(path):
    real = os.path.realpath(path)
    for _label, folder in _record_sources():
        base = os.path.realpath(folder)
        try:
            if os.path.commonpath([real, base]) == base:
                return True
        except ValueError:
            continue
    return False


def _start_time(name):
    """Clip start time parsed from the filename.

    our recorder : cam104_hd_20261102_083503.mp4
    VMS          : 20261102084919327.avi
    """
    m = re.search(r"(\d{8})_(\d{6})", name)
    if m:
        d, t = m.group(1), m.group(2)
        return f"{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:4]}:{t[4:]}"
    m = re.match(r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})", name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}:{m.group(6)}"
    return ""


def _direct_playable(path):
    """Our recorder's own files: MP4/H.264/AAC - browsers play them as they are.

    An MP4 that came from an H.265 camera is deliberately *not* direct: the
    browser has to be handed a converted H.264 copy (marked with `_h265` in the
    name). Matched against the recorder's live list of roots, so clips stay
    playable wherever the recorder has been pointed.
    """
    if os.path.splitext(path)[1].lower() != ".mp4":
        return False
    if "_h265" in os.path.basename(path).lower():
        return False
    real = os.path.realpath(path)
    return any(real.startswith(os.path.realpath(r)) for r in _recorder_roots())


def _is_open_by_other(path):
    """Is another process holding this file open for writing?

    Uses the Win32 share mode directly: requesting the file with dwShareMode=0
    fails with ERROR_SHARING_VIOLATION if anybody else has a handle on it. That
    reliably identifies VMS's current recording segment and our own in-progress
    MP4, neither of which can be played or converted yet.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        GENERIC_READ, OPEN_EXISTING = 0x80000000, 3
        invalid = ctypes.c_void_p(-1).value
        create = ctypes.windll.kernel32.CreateFileW
        create.restype = ctypes.c_void_p
        handle = create(str(path), GENERIC_READ, 0, None, OPEN_EXISTING, 0, None)
        if handle in (invalid, None):
            return True
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    except Exception:                                          # noqa: BLE001
        return False


def _conv_known(dst):
    """True when we already have a conversion for this clip - running or done.

    Needed because a conversion reads its source, so the source looks 'held open'
    to _is_open_by_other for as long as ffmpeg is working on it. Without this,
    our own transcode would masquerade as the recorder still writing the file.
    """
    with _conv_lock:
        state = (_conv_state.get(dst) or {}).get("state")
    return state in ("converting", "ready") or _cache_ready(dst)


def list_clips():
    clips = []
    names = _load_names()
    for label, folder in _record_sources():
        if not os.path.isdir(folder):
            continue
        for dirpath, _dirs, filenames in os.walk(folder):
            if os.path.basename(dirpath).startswith("."):
                continue
            for name in filenames:
                if not name.lower().endswith(CLIP_EXTS):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if st.st_size == 0:            # still open, or a failed clip
                    continue
                cam_dir = os.path.basename(dirpath)
                ip = _ip_of(cam_dir, name)
                # Only a recent file can be mid-write, and the open-handle test is a
                # real syscall per file - with 10-minute segments that is ~1000 files
                # a day, so skip the test for anything older than half an hour.
                live = False
                if st.st_mtime > time.time() - 1800:
                    live = _is_open_by_other(full) and not _conv_known(_cache_path(full))
                clips.append({
                    "id": _clip_id(full),
                    "source": label,
                    "camera": cam_dir,
                    "ip": ip,
                    "label": names.get(ip) or ip or cam_dir.replace("Ch1_", ""),
                    "name": name,
                    "start": _start_time(name),
                    "mb": round(st.st_size / 1048576.0, 2),
                    "mtime": int(st.st_mtime),
                    "direct": _direct_playable(full),
                    "live": live,
                })
    clips.sort(key=lambda c: c["mtime"], reverse=True)
    return clips


def _cache_path(src):
    return os.path.join(CACHE, hashlib.sha1(src.encode("utf-8")).hexdigest() + ".mp4")


def _is_hevc(src):
    """True when the video in `src` is HEVC.

    Two cases: a raw .h265/.hevc file, or an MP4 our recorder wrote from an
    H.265 camera. Those carry a `_h265` marker in the name (see
    record-cameras.ps1) so playback knows to convert without probing every
    clip - which a recordings listing cannot afford.
    """
    name = os.path.basename(src).lower()
    if "_h265" in name:
        return True
    return os.path.splitext(name)[1] in (".h265", ".hevc")


def _convert(src, dst):
    """Convert src into the cache at dst.

    Writes to dst + ".part" and renames it into place only when ffmpeg has
    finished successfully. A plain partial write would otherwise look like a
    finished clip the instant ffmpeg created it (size > 0), and the browser would
    be handed a truncated MP4 with no index - which never reaches readyState 1.
    """
    try:
        os.makedirs(CACHE, exist_ok=True)
    except OSError:
        pass
    tmp = dst + ".part"
    with _conv_lock:
        _conv_state[dst] = {"state": "converting", "bytes": 0, "error": "",
                            "started": time.time(), "grew": time.time(),
                            "last_size": 0, "proc": None}
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except OSError:
        pass
    proc = None
    try:
        # "-f mp4" matters: the output is written to a ".part" name so the cache
        # only ever contains whole files, and ffmpeg cannot infer the muxer from
        # that extension ("Unable to choose an output format").
        if _is_hevc(src):
            # HEVC, which browsers do not reliably play - transcode on the GPU,
            # keeping the camera's audio as AAC (optional: raw .h265 has none).
            args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-fflags", "+genpts", "-i", src,
                    "-map", "0:v:0", "-map", "0:a:0?",
                    "-c:v", "h264_nvenc", "-preset", "p4",
                    "-b:v", "4M", "-c:a", "aac", "-b:a", "64k",
                    "-f", "mp4", "-movflags", "+faststart", tmp]
        else:
            # AVI carrying H.264: copy the video, convert the PCMA audio to AAC
            args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                    "-i", src, "-map", "0:v:0", "-map", "0:a:0?",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "64k",
                    "-f", "mp4", "-movflags", "+faststart", tmp]
        print(f"convert start: {src}", flush=True)
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        with _conv_lock:
            _conv_state[dst]["proc"] = proc
        while proc.poll() is None:
            time.sleep(1)
            try:
                size = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            except OSError:
                size = 0
            with _conv_lock:
                st = _conv_state[dst]
                st["bytes"] = size
                if size != st.get("last_size"):
                    st["last_size"] = size
                    st["grew"] = time.time()
        err = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
        ok = proc.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0
        if ok:
            os.replace(tmp, dst)          # atomic: the clip appears only when whole
            size = os.path.getsize(dst)
        else:
            size = 0
            try:
                os.remove(tmp)
            except OSError:
                pass
        with _conv_lock:
            _conv_state[dst] = {
                "state": "ready" if ok else "error",
                "bytes": size,
                "error": "" if ok else (err[:300] or f"ffmpeg exit {proc.returncode}"),
                "started": time.time(), "grew": time.time(), "last_size": 0, "proc": None,
            }
        if ok:
            print(f"convert done:  {os.path.basename(dst)} ({size/1048576.0:.1f} MB)", flush=True)
        else:
            print(f"convert FAILED: {src} :: {err[:200]}", flush=True)
    except Exception as exc:                                   # noqa: BLE001
        try:
            os.remove(tmp)
        except OSError:
            pass
        with _conv_lock:
            _conv_state[dst] = {"state": "error", "bytes": 0, "error": str(exc)[:300],
                                "started": time.time(), "grew": time.time(),
                                "last_size": 0, "proc": None}
        print(f"convert EXCEPTION: {src} :: {exc}", flush=True)


STALL_SECONDS = 90          # no output growth for this long = wedged
CONVERT_CAP_SECONDS = 1800  # absolute ceiling for one conversion
CACHE_GRACE_SECONDS = 10    # a cache file this old with no record is taken as whole


def _cache_ready(dst):
    """True only when dst is a finished, non-empty cache file."""
    try:
        size = os.path.getsize(dst)
    except OSError:
        return False
    if size <= 0:
        return False
    with _conv_lock:
        state = (_conv_state.get(dst) or {}).get("state")
    if state in ("converting", "error"):
        return False
    return True


def _public_state(st):
    """Strip the internal bookkeeping (process handle, timers) before sending."""
    st = dict(st)
    st.pop("proc", None)
    st.pop("last_size", None)
    st.pop("started", None)
    if st.get("state") == "converting":
        grew = st.pop("grew", time.time())
        st["stalled_for"] = int(time.time() - grew)
    else:
        st.pop("grew", None)
    return st


def _conv_status(src, dst):
    """Status of a cached conversion, or of the source when it needs no cache."""
    if _cache_ready(dst):
        return {"state": "ready", "direct": False}
    with _conv_lock:
        st = dict(_conv_state.get(dst, {"state": "new", "bytes": 0, "error": "",
                                        "grew": time.time(), "started": time.time()}))
    if st.get("state") == "converting":
        now = time.time()
        if now - st.get("grew", now) > STALL_SECONDS:
            proc = st.get("proc")
            if proc is not None:
                try:
                    proc.kill()               # stop the wedged ffmpeg
                except Exception:             # noqa: BLE001
                    pass
            with _conv_lock:
                _conv_state[dst] = {"state": "error", "bytes": st.get("bytes", 0),
                                    "error": f"conversion stalled (no output for {STALL_SECONDS}s)",
                                    "started": now, "grew": now, "last_size": 0, "proc": None}
            print(f"convert stalled: {src}", flush=True)
            return {"state": "error", "bytes": 0, "error": f"conversion stalled after {STALL_SECONDS}s of no output"}
        if now - st.get("started", now) > CONVERT_CAP_SECONDS:
            with _conv_lock:
                _conv_state[dst] = {"state": "error", "bytes": st.get("bytes", 0),
                                    "error": "conversion exceeded the time limit",
                                    "started": now, "grew": now, "last_size": 0, "proc": None}
            return {"state": "error", "bytes": 0, "error": "conversion exceeded the time limit"}
    return _public_state(st)


def _start_conversion(src):
    """Kick off a conversion for src unless one is already running or cached."""
    dst = _cache_path(src)
    if _cache_ready(dst):
        return "ready"
    with _conv_lock:
        st = _conv_state.get(dst, {})
        running = (st.get("state") == "converting"
                   and time.time() - st.get("grew", 0) < STALL_SECONDS)
    if running:
        return "converting"
    threading.Thread(target=_convert, args=(src, dst), daemon=True).start()
    return "converting"


def _prune_cache():
    """Drop interrupted conversions, then the least recently used copies.

    Age alone is not enough and size alone is not enough: a month-old clip someone
    watches daily is worth keeping, while a shelf of untouched gigabyte conversions
    is not. So the age limit is generous and the size cap does the work.
    """
    cutoff = time.time() - CACHE_MAX_AGE_HOURS * 3600
    entries = []
    try:
        for name in os.listdir(CACHE):
            path = os.path.join(CACHE, name)
            if not os.path.isfile(path):
                continue
            if name.endswith(".part"):
                try:
                    os.remove(path)            # interrupted mid-conversion
                except OSError:
                    pass
                continue
            try:
                entries.append((os.path.getmtime(path), os.path.getsize(path), path))
            except OSError:
                pass
    except OSError:
        return
    total = sum(size for _m, size, _p in entries)
    cap = CACHE_MAX_GB * 1024 ** 3
    removed = 0
    for mtime, size, path in sorted(entries):          # oldest first
        if total <= cap and mtime >= cutoff:
            continue
        try:
            os.remove(path)
            total -= size
            removed += 1
        except OSError:
            pass
    if removed:
        print(f"conversion cache: dropped {removed} file(s), {total/1e9:.2f} GB kept "
              f"of a {CACHE_MAX_GB} GB budget", flush=True)


def _prune_loop():
    """Prune periodically, not only at startup - a service that runs for weeks
    would otherwise never re-check its budget."""
    while True:
        time.sleep(1800)
        _prune_cache()


_svc_cache = {"at": 0.0, "value": "?"}


def _service_state(force=False):
    """go2rtc-recorder's state, via sc.exe (no PowerShell spawn) cached briefly."""
    now = time.time()
    if not force and now - _svc_cache["at"] < 30:
        return _svc_cache["value"]
    value = "Unknown"
    try:
        out = subprocess.run(["sc", "query", RECORDER_SERVICE],
                             capture_output=True, text=True, timeout=15).stdout
        m = re.search(r"STATE\s*:\s*\d+\s+(\w+)", out)
        value = m.group(1) if m else "NotFound"
    except Exception:                                          # noqa: BLE001
        pass
    _svc_cache.update(at=now, value=value)
    return value


def _service_restart():
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"Restart-Service {RECORDER_SERVICE} -Force"],
                             capture_output=True, text=True, timeout=120)
        return out.returncode == 0
    except Exception:                                          # noqa: BLE001
        return False


def _service_set(action):
    """Start or stop the recorder service. Returns (ok, error_text)."""
    cmd = f"Start-Service {RECORDER_SERVICE}" if action == "start" \
        else f"Stop-Service {RECORDER_SERVICE} -Force"
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                             capture_output=True, text=True, timeout=180)
        msg = " ".join((out.stderr or "").split())[:400]
        return out.returncode == 0, msg
    except Exception as exc:                                   # noqa: BLE001
        return False, str(exc)[:400]


def _service_start_type():
    """AUTOMATIC / MANUAL / DISABLED. A DISABLED service cannot be started at all."""
    try:
        out = subprocess.run(["sc", "qc", RECORDER_SERVICE],
                             capture_output=True, text=True, timeout=15).stdout
        m = re.search(r"START_TYPE\s*:\s*\d+\s+(\w+)", out)
        return m.group(1) if m else "?"
    except Exception:                                          # noqa: BLE001
        return "?"


def _service_enable():
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"Set-Service -Name {RECORDER_SERVICE} -StartupType Automatic"],
                             capture_output=True, text=True, timeout=60)
        return out.returncode == 0, " ".join((out.stderr or "").split())[:300]
    except Exception as exc:                                   # noqa: BLE001
        return False, str(exc)[:300]


def _kill_recorder_ffmpeg(root):
    """Stop the recorder's ffmpeg children.

    Stopping the service does not kill them on Windows, and a recorder left
    running would keep writing half-finished segments.
    """
    if os.name != "nt" or not root:
        return 0
    try:
        esc = root.replace("'", "''")
        script = ("Get-CimInstance Win32_Process -Filter \"Name='ffmpeg.exe'\" | "
                  f"Where-Object {{ $_.CommandLine -like '*{esc}*' }} | "
                  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                  "-ErrorAction SilentlyContinue; $_.ProcessId }")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, timeout=90).stdout
        return len([p for p in out.split() if p.isdigit()])
    except Exception:                                          # noqa: BLE001
        return 0


def _volume_info(path):
    """Free/total GB of the volume that holds path."""
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if not drive:
        return {}
    try:
        import ctypes
        free, total = ctypes.c_ulonglong(0), ctypes.c_ulonglong(0)
        k32 = ctypes.windll.kernel32
        if not k32.GetDiskFreeSpaceExW(ctypes.c_wchar_p(drive + "\\"), None,
                                       ctypes.byref(total), ctypes.byref(free)):
            return {}
        return {"drive": drive.rstrip("\\"),
                "freeGB": round(free.value / 1073741824, 2),
                "totalGB": round(total.value / 1073741824, 2)}
    except Exception:                                          # noqa: BLE001
        return {}


def _volumes():
    """Every fixed local volume, for the storage readout."""
    vols = []
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        mask = k32.GetLogicalDrives()
        for i, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
            if not (mask >> i) & 1:
                continue
            root = letter + ":\\"
            if k32.GetDriveTypeW(ctypes.c_wchar_p(root)) != 3:   # DRIVE_FIXED
                continue
            free, total = ctypes.c_ulonglong(0), ctypes.c_ulonglong(0)
            if k32.GetDiskFreeSpaceExW(ctypes.c_wchar_p(root), None,
                                       ctypes.byref(total), ctypes.byref(free)):
                vols.append({"drive": letter,
                             "freeGB": round(free.value / 1073741824, 2),
                             "totalGB": round(total.value / 1073741824, 2)})
    except Exception:                                          # noqa: BLE001
        pass
    return vols


def _read_status():
    """The recorder's self-reported state (rewritten every ~15s by the service)."""
    try:
        with open(STATUS_FILE, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_events(limit=120):
    """Newest-first camera events, tailed from the collector's log."""
    try:
        with open(EVENTS_FILE, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _status_age(status):
    gen = status.get("generated")
    if not gen:
        return None
    try:
        dt = datetime.datetime.fromisoformat(gen)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(time.time() - dt.timestamp())
    except (ValueError, TypeError):
        return None


def _script_settings():
    """$root / $segmentMinutes / $keepDays / $minFreeGB out of the recorder script."""
    out = {}
    try:
        with open(RECORDER_SCRIPT, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return out
    for key, pat in (("root", r"^\$root\s*=\s*'([^']+)'"),
                     ("segmentMinutes", r"^\$segmentMinutes\s*=\s*(\d+)"),
                     ("keepDays", r"^\$keepDays\s*=\s*(\d+)"),
                     ("minFreeGB", r"^\$minFreeGB\s*=\s*(\d+)")):
        m = re.search(pat, text, re.M)
        if m:
            out[key] = int(m.group(1)) if m.group(1).isdigit() else m.group(1)
    return out


def _patch_script(new_root=None, keep_days=None):
    """Rewrite the recorder's settings in place, atomically."""
    with open(RECORDER_SCRIPT, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    changed = {}
    if new_root:
        text2, n = re.subn(r"^(\$root\s*=\s*)'[^']*'",
                           lambda m: m.group(1) + "'" + new_root + "'", text, count=1, flags=re.M)
        if not n:
            raise ValueError("could not find $root in the recorder script")
        text = text2
        changed["root"] = new_root
    if keep_days:
        text2, n = re.subn(r"^(\$keepDays\s*=\s*)\d+",
                           lambda m: m.group(1) + str(int(keep_days)), text, count=1, flags=re.M)
        if not n:
            raise ValueError("could not find $keepDays in the recorder script")
        text = text2
        changed["keepDays"] = int(keep_days)
    tmp = RECORDER_SCRIPT + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, RECORDER_SCRIPT)
    return changed


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "cctv-bridge"
    # Unbuffered request stream: with the default buffered rfile, bytes the client
    # sends immediately after a WebSocket handshake stay trapped in the buffer, the
    # raw relay never sees them, and every player sits at readyState 0 even though
    # the handshake correctly returns 101.
    rbufsize = 0

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)

    # ---------------- helpers ----------------
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_range(self, path, content_type):
        """Serve a file with HTTP Range support so the video can be seeked."""
        try:
            size = os.path.getsize(path)
            fh = open(path, "rb")
        except OSError as exc:
            self._json({"error": str(exc)}, 404)
            return
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
                if start > end:
                    start = end
        length = max(0, end - start + 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(262144, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            fh.close()

    # ---------------- static ----------------
    def _local_path(self):
        path = urlsplit(self.path).path
        if path in ("", "/"):
            return os.path.join(WWW, "index.html")
        if path in ("/recordings", "/recordings/"):
            return os.path.join(WWW, "recordings.html")
        candidate = os.path.normpath(os.path.join(WWW, path.lstrip("/")))
        if candidate.startswith(WWW) and os.path.isfile(candidate):
            return candidate
        return None

    def _serve_file(self, path):
        with open(path, "rb") as fh:
            body = fh.read()
        ext = os.path.splitext(path)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ---------------- whole-configuration save / load ----------------
    def _api_config_get(self):
        """Download everything needed to rebuild this setup, as one file."""
        if cctvconfig is None:
            return self._json({"error": "cctvconfig.py is missing next to proxy.py"}, 500)
        try:
            cfg = cctvconfig.build()
        except Exception as exc:                               # noqa: BLE001
            return self._json({"error": f"could not read the configuration: {exc}"}, 500)
        body = json.dumps(cfg, indent=2, ensure_ascii=False).encode("utf-8")
        name = f"cctv-config-{datetime.datetime.now():%Y%m%d-%H%M}.json"
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _api_config_put(self):
        """Apply an uploaded configuration over the live one."""
        if cctvconfig is None:
            return self._json({"error": "cctvconfig.py is missing next to proxy.py"}, 500)
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return self._json({"error": "no file was uploaded"}, 400)
        if length > 4 * 1024 * 1024:
            return self._json({"error": "that file is far too large to be a camera config"}, 413)
        raw = self.rfile.read(length)
        if raw[:3] == b"\xef\xbb\xbf":
            raw = raw[3:]                       # the BOM Windows Notepad adds
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            return self._json({"error": f"that is not valid JSON: {exc}"}, 400)
        try:
            report = cctvconfig.apply(data)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:                               # noqa: BLE001
            return self._json({"error": f"could not apply the configuration: {exc}"}, 500)
        self._json({"ok": True, **report})

    # ---------------- recordings api ----------------
    def _api_recordings(self):
        clips = list_clips()
        cams = sorted({(c["source"], c["camera"]) for c in clips})
        self._json({"clips": clips,
                    "cameras": [{"source": s, "camera": c} for s, c in cams]})

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _api_names(self):
        cfg = _load_config()
        self._json({"names": cfg["names"], "order": cfg["order"]})

    def _api_set_name(self):
        body = self._read_json()
        key = (body.get("key") or "").strip()
        name = (body.get("name") or "").strip()
        if not key:
            self._json({"error": "key required"}, 400)
            return
        cfg = _load_config()
        names = cfg["names"]
        if name:
            names[key] = name[:40]
        else:
            names.pop(key, None)          # an empty name restores the default
        try:
            _save_config(cfg)
        except OSError as exc:
            self._json({"error": f"could not save: {exc}"}, 500)
            return
        self._json({"names": names, "order": cfg["order"]})

    def _api_set_order(self):
        body = self._read_json()
        order = body.get("order")
        if not isinstance(order, list):
            self._json({"error": "order must be a list of camera IPs"}, 400)
            return
        cfg = _load_config()
        cfg["order"] = [str(x) for x in order if str(x).strip()][:64]
        try:
            _save_config(cfg)
        except OSError as exc:
            self._json({"error": f"could not save: {exc}"}, 500)
            return
        self._json({"names": cfg["names"], "order": cfg["order"]})

    def _resolve_clip(self):
        """Shared id -> source path lookup for the clip endpoints."""
        qs = parse_qs(urlsplit(self.path).query)
        cid = (qs.get("f") or [""])[0]
        try:
            src = _clip_path(cid)
        except Exception:                                      # noqa: BLE001
            return None, {"error": "bad id"}, 400
        if not _allowed(src) or not os.path.isfile(src):
            return None, {"state": "missing"}, 404
        return src, None, None

    def _api_recstart(self):
        """Start (or join) the conversion for a clip. Idempotent.

        The page needs this: polling the status never starts any work, so without
        an explicit kick the UI sits on "converting" forever.
        """
        src, err, code = self._resolve_clip()
        if src is None:
            self._json(err, code)
            return
        if _direct_playable(src):
            self._json({"state": "ready", "direct": True})
            return
        dst = _cache_path(src)
        if _cache_ready(dst):
            self._json({"state": "ready", "direct": False})
            return
        with _conv_lock:
            inflight = (_conv_state.get(dst) or {}).get("state") == "converting"
        # A half-written file cannot be remuxed (and an incomplete HEVC stream has
        # no usable parameter sets at all). Say so plainly instead of surfacing a
        # codec error the user cannot act on. Skipped when we are the one reading
        # the file.
        if not inflight and _is_open_by_other(src):
            self._json({"state": "error",
                        "error": "this clip is still being recorded - it becomes "
                                 "playable once the recorder finishes and closes the file"})
            return
        state = _start_conversion(src)
        self._json({"state": state,
                    "src_mb": round(os.path.getsize(src) / 1048576.0, 2)})

    def _api_events(self):
        events = read_events(150)
        self._json({"events": events, "count": len(events)})

    def _event_thumb(self):
        """Serve an event snapshot. Name is whitelisted to keep paths confined.

        Snapshots live beside the recordings, but the collector falls back to the
        local folder when that drive is away, so both are searched.
        """
        name = os.path.basename(urlsplit(self.path).path)
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+\.jpg", name):
            self.send_error(400, "bad name")
            return
        for folder in _thumb_folders():
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                self._serve_file(path)
                return
        self.send_error(404, "no such snapshot")

    def _api_recorder(self):
        """Recorder state for the dashboard: what it is doing and where it writes."""
        status = _read_status()
        settings = _script_settings()
        age = _status_age(status)
        root = status.get("root") or settings.get("root") or ""
        target = _volume_info(root) if root else {}
        self._json({
            "service": _service_state(),
            "start_type": _service_start_type(),
            "reporting": age is not None and age < 90,
            "status_age_sec": age,
            "status_present": bool(status),
            "status_file": STATUS_FILE,
            "script": settings,
            "status": status,
            "target": target,
            "volumes": _volumes(),
        })

    def _api_set_recorder(self):
        """Point the recorder at a different folder (and/or change retention).

        Deliberately does NOT start a stopped recorder: it only takes effect the
        next time the service runs, so a stopped recorder stays stopped.
        """
        body = self._read_json()
        new_root = (body.get("path") or "").strip().rstrip("\\/")
        keep_days = body.get("keepDays")
        if not new_root:
            self._json({"error": "give a folder, for example E:\\cctv"}, 400)
            return
        if not (re.match(r"^[A-Za-z]:[\\/]", new_root) or new_root.startswith("\\\\")):
            self._json({"error": "use an absolute path, e.g. E:\\cctv or "
                                 "\\\\server\\share\\cctv"}, 400)
            return
        vol = _volume_info(new_root)
        if not vol:
            self._json({"error": f"{new_root} is not on a volume this machine can read"}, 400)
            return
        settings = _script_settings()
        minfree = int(settings.get("minFreeGB") or 30)
        if vol["freeGB"] < minfree + 5:
            self._json({"error": f"only {vol['freeGB']} GB free on {vol['drive']}: the recorder "
                                 f"keeps a {minfree} GB floor, so it would stop recording almost "
                                 f"immediately. Free up space or lower the floor first."}, 400)
            return
        try:
            os.makedirs(new_root, exist_ok=True)
            probe = os.path.join(new_root, ".write-test")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(probe)
        except OSError as exc:
            self._json({"error": f"cannot write to {new_root}: {exc}"}, 400)
            return
        try:
            changed = _patch_script(new_root=new_root, keep_days=keep_days)
        except (OSError, ValueError) as exc:
            self._json({"error": f"could not update the recorder settings: {exc}"}, 500)
            return
        before = _service_state(force=True)
        restarted = False
        if before.lower().startswith("run"):
            restarted = _service_restart()
        note = (f"recorder restarted - new recordings go to {new_root}" if restarted
                else f"saved. The recorder is not running right now, so this takes effect "
                     f"the next time it starts.")
        self._json({"ok": True, "changed": changed, "service_before": before,
                    "restarted": restarted, "note": note,
                    "freeGB": vol["freeGB"]})

    def _api_recorder_run(self):
        """Start or stop the recorder from the dashboard.

        Starting is refused unless the target volume is readable and has room -
        the point is to fail loudly here rather than start a recorder that
        silently dies on its first write (an offline drive, a full disk, or a
        volume busy with a disk check).
        """
        body = self._read_json()
        action = (body.get("action") or "").strip().lower()
        if action not in ("start", "stop"):
            self._json({"error": "action must be 'start' or 'stop'"}, 400)
            return
        settings = _script_settings()
        root = settings.get("root") or ""

        if action == "stop":
            ok, err = _service_set("stop")
            killed = _kill_recorder_ffmpeg(root)
            _svc_cache.update(at=0.0, value="?")
            self._json({"ok": ok, "action": "stop", "service": _service_state(force=True),
                        "killed_writers": killed, "error": err,
                        "note": f"recording stopped ({killed} writer(s) ended)"})
            return

        if not root:
            self._json({"error": "the recorder script has no $root target set"}, 400)
            return
        vol = _volume_info(root)
        if not vol:
            self._json({"error": f"cannot read the volume for {root}. It may be offline, "
                                 f"removed, or busy with a disk check - fix that first, "
                                 f"or point the recorder somewhere else."}, 400)
            return
        floor = int(settings.get("minFreeGB") or 30)
        if vol["freeGB"] < floor + 2:
            self._json({"error": f"{vol['drive']}: has {vol['freeGB']} GB free, below the "
                                 f"{floor} GB the recorder keeps in reserve, so it would stop "
                                 f"almost immediately."}, 400)
            return
        try:
            os.makedirs(root, exist_ok=True)
            probe = os.path.join(root, ".write-test")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.remove(probe)
        except OSError as exc:
            self._json({"error": f"cannot write to {root}: {exc}"}, 400)
            return

        # A DISABLED service cannot be started by anything, and Start-Service's
        # error does not say why - so check the configured start type directly
        # rather than trying to read the reason out of the failure message.
        start_type = _service_start_type()
        reenabled = False
        if start_type.upper().startswith("DISABLED"):
            enabled, eerr = _service_enable()
            if not enabled:
                self._json({"error": f"the recorder service is disabled and could not be "
                                     f"re-enabled: {eerr}"}, 400)
                return
            reenabled = True

        ok, err = _service_set("start")
        _svc_cache.update(at=0.0, value="?")
        if ok:
            time.sleep(8)                     # let it write its first status file
        state = _service_state(force=True)
        note = f"recording to {root}" if ok else "could not start the service"
        if ok and reenabled:
            note += " (the service had been disabled - it is set back to Automatic)"
        self._json({"ok": ok, "action": "start", "service": state, "target": root,
                    "freeGB": vol["freeGB"], "reenabled": reenabled,
                    "start_type": _service_start_type(),
                    "reporting": (_status_age(_read_status()) or 999) < 90,
                    "error": err, "note": note})

    def _api_recstatus(self):
        src, err, code = self._resolve_clip()
        if src is None:
            self._json(err, code)
            return
        if _direct_playable(src):
            self._json({"state": "ready", "direct": True})
            return
        st = _conv_status(src, _cache_path(src))
        st["src_mb"] = round(os.path.getsize(src) / 1048576.0, 2)
        self._json(st)

    def _rec_stream(self):
        qs = parse_qs(urlsplit(self.path).query)
        cid = (qs.get("f") or [""])[0]
        try:
            src = _clip_path(cid)
        except Exception:                                      # noqa: BLE001
            self.send_error(400, "bad id")
            return
        if not _allowed(src) or not os.path.isfile(src):
            self.send_error(404, "not found")
            return
        if _direct_playable(src):
            self._serve_range(src, "video/mp4")
            return
        dst = _cache_path(src)
        if _cache_ready(dst):
            self._serve_range(dst, "video/mp4")
            return
        _start_conversion(src)
        for _ in range(60):                 # let short clips start almost at once
            time.sleep(0.5)
            if os.path.exists(dst) and os.path.getsize(dst) > 8192:
                break
            with _conv_lock:
                if _conv_state.get(dst, {}).get("state") == "error":
                    break
        if _cache_ready(dst):
            self._serve_range(dst, "video/mp4")
        else:
            with _conv_lock:
                st = _public_state(_conv_state.get(dst, {}))
            self._json({"state": st.get("state", "converting"),
                        "error": st.get("error", "")}, 202)

    # ---------------- go2rtc proxy ----------------
    def _proxy(self):
        try:
            up = socket.create_connection((UP_HOST, UP_PORT), timeout=15)
        except OSError as exc:
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"go2rtc unreachable: {exc}\n".encode())
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            head = [f"{self.command} {self.path} HTTP/1.0", f"Host: {UP_HOST}:{UP_PORT}"]
            for key, value in self.headers.items():
                if key.lower() in HOP_BY_HOP or key.lower() == "host":
                    continue
                head.append(f"{key}: {value}")
            up.sendall(("\r\n".join(head) + "\r\n\r\n").encode() + body)

            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = up.recv(4096)
                if not chunk:
                    break
                buf += chunk
            head_raw, _, rest = buf.partition(b"\r\n\r\n")
            lines = head_raw.decode("iso-8859-1").split("\r\n")
            try:
                code = int(lines[0].split()[1])
            except (IndexError, ValueError):
                code = 502
            self.send_response(code)
            for line in lines[1:]:
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                if key.strip().lower() in HOP_BY_HOP:
                    continue
                self.send_header(key.strip(), value.strip())
            self.end_headers()
            if rest:
                self.wfile.write(rest)
            up.settimeout(30)
            while True:
                try:
                    chunk = up.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
        finally:
            up.close()

    # ---------------- websocket ----------------
    def _ws_proxy(self):
        try:
            upstream = socket.create_connection((UP_HOST, UP_PORT), timeout=15)
        except OSError:
            self.send_response(502)
            self.end_headers()
            return
        lines = [f"{self.command} {self.path} HTTP/1.1"]
        for key, value in self.headers.items():
            lines.append(f"{key}: {value}")
        upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        client = self.connection
        self.close_connection = True
        try:
            while True:
                ready, _, _ = select.select([client, upstream], [], [], 30)
                if client in ready:
                    data = client.recv(65536)
                    if not data:
                        break
                    upstream.sendall(data)
                if upstream in ready:
                    data = upstream.recv(65536)
                    if not data:
                        break
                    client.sendall(data)
        except OSError:
            pass
        finally:
            upstream.close()

    # ---------------- dispatch ----------------
    def handle_request(self):
        path = urlsplit(self.path).path
        if self.headers.get("Upgrade", "").lower() == "websocket":
            return self._ws_proxy()
        if path == "/api/recordings":
            return self._api_recordings()
        if path == "/api/names":
            return self._api_names() if self.command == "GET" else self._api_set_name()
        if path == "/api/order":
            return self._api_set_order()
        if path == "/api/recstatus":
            return self._api_recstatus()
        if path == "/api/recstart":
            return self._api_recstart()
        if path == "/api/recorder":
            return self._api_recorder() if self.command == "GET" else self._api_set_recorder()
        if path == "/api/events":
            return self._api_events()
        if path == "/api/config":
            return self._api_config_get() if self.command == "GET" else self._api_config_put()
        if path.startswith("/eventthumb/"):
            return self._event_thumb()
        if path == "/api/recorder/run":
            return self._api_recorder_run()
        if path == "/rec/stream":
            return self._rec_stream()
        local = self._local_path()
        if local and self.command in ("GET", "HEAD"):
            return self._serve_file(local)
        return self._proxy()

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_OPTIONS = handle_request


def _kill_orphan_conversions():
    """Kill ffmpeg conversions left over from a previous run.

    Restarting the service (or a crash) does not kill child processes on Windows,
    so an interrupted transcode keeps burning CPU and disk space and can hold the
    source file open.
    """
    if os.name != "nt":
        return 0
    try:
        script = ("Get-CimInstance Win32_Process -Filter \"Name='ffmpeg.exe'\" | "
                  "Where-Object { $_.CommandLine -like '*\\.webplay*' } | "
                  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "
                  "$_.ProcessId }")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                             capture_output=True, text=True, timeout=30).stdout
        pids = [p for p in out.split() if p.isdigit()]
        if pids:
            print(f"killed {len(pids)} orphaned conversion(s): {', '.join(pids)}", flush=True)
        return len(pids)
    except Exception:                                          # noqa: BLE001
        return 0


if __name__ == "__main__":
    os.makedirs(WWW, exist_ok=True)
    try:
        os.makedirs(CACHE, exist_ok=True)
    except OSError:
        pass
    _kill_orphan_conversions()
    _prune_cache()
    threading.Thread(target=_prune_loop, daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.daemon_threads = True
    print(f"cctv bridge on :{PORT}  static={WWW}  upstream={UP_HOST}:{UP_PORT}", flush=True)
    print(f"recordings sources: {[lbl for lbl, _ in _record_sources(force=True)]}", flush=True)
    httpd.serve_forever()
