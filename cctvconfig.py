"""Save and restore the whole camera configuration as one file.

The setup is spread over three files with three different owners:

  go2rtc.yaml         the stream URLs (three transports), hand-written
  cameras.json        camera names and tile order, written by the dashboard
  record-cameras.ps1  recorder settings and camera list, hand-edited

This module turns those into a single portable document and applies one back.
Nothing is ever rewritten wholesale: go2rtc.yaml keeps its prologue (the
hand-written commentary plus the api/rtsp/webrtc/ffmpeg sections) and
record-cameras.ps1 is edited by targeted substitution, so settings and code this
module does not own survive untouched.

Every apply first copies all three files into config-backups/<timestamp>/; if any
step then fails, the copies are put back.

Run:  python cctvconfig.py show     # print what the config would contain
      python cctvconfig.py save f   # write it to a file
"""
import datetime
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
YAML_FILE = os.path.join(ROOT, "go2rtc.yaml")
NAMES_FILE = os.path.join(ROOT, "cameras.json")
SCRIPT_FILE = os.path.join(ROOT, "record-cameras.ps1")
BACKUP_DIR = os.path.join(ROOT, "config-backups")

CONFIG_FORMAT = "cctv-config"
CONFIG_VERSION = 1
STREAM_SERVICE = "go2rtc"
RECORDER_SERVICE = "go2rtc-recorder"
EVENTS_SERVICE = "go2rtc-events"


# --------------------------------------------------------------------- files
def _read(path):
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _write(path, text):
    """Atomic: a half-written config would break the service that reads it."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _unquote(value):
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


# ---------------------------------------------------------------------- yaml
def parse_yaml(text):
    """-> (prologue, entries, trailing). Entries keep their comments and order.

    Handles both stream shapes go2rtc accepts: 'name: url' and 'name:' followed
    by an indented '- url' list.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^streams:\s*$", line):
            start = i
            break
        if re.match(r"^streams:\s*\S", line):
            raise ValueError("this go2rtc.yaml writes streams on one line; "
                             "that shape cannot be edited safely")
    if start is None:
        raise ValueError("no 'streams:' block found in go2rtc.yaml")

    prologue = lines[:start + 1]
    entries, pending, i = [], [], start + 1
    while i < len(lines):
        line = lines[i]
        if line.strip() == "":
            pending.append("")
            i += 1
            continue
        if not line[:1].isspace():
            break                                   # a top-level key ends the block
        stripped = line.strip()
        if stripped.startswith("#"):
            pending.append(stripped)
            i += 1
            continue
        m = re.match(r"^\s+([\w.\-]+):\s*(.*)$", line)
        if not m:
            pending.append(stripped)                 # unexpected: keep it verbatim
            i += 1
            continue
        name, value = m.group(1), m.group(2).strip()
        comment, pending = pending, []
        if value:
            entries.append({"name": name, "url": _unquote(value), "comment": comment})
            i += 1
        else:
            urls = []
            i += 1
            while i < len(lines) and re.match(r"^\s+-\s*", lines[i]):
                urls.append(_unquote(re.match(r"^\s+-\s*(.*)$", lines[i]).group(1)))
                i += 1
            entries.append({"name": name, "urls": urls, "comment": comment})
    return prologue, entries, lines[i:]


def render_yaml(prologue, entries, trailing):
    out = list(prologue)
    width = max((len(e["name"]) for e in entries), default=0)
    for e in entries:
        for c in e.get("comment") or []:
            out.append(("  " + c) if c else "")
        if "urls" in e:
            out.append(f"  {e['name']}:")
            for u in e["urls"]:
                out.append(f"    - {u}")
        else:
            out.append(f"  {(e['name'] + ':').ljust(width + 1)} {json.dumps(e['url'])}")
    out.extend(trailing)
    return "\n".join(out).rstrip("\n") + "\n"


# ------------------------------------------------------------------ recorder
def parse_ps1(text):
    """Recorder settings, plus the camera list with its trailing comments."""
    out = {}
    for key, pat in (("root", r"^\$root\s*=\s*'([^']*)'"),
                     ("segmentMinutes", r"^\$segmentMinutes\s*=\s*(\d+)"),
                     ("keepDays", r"^\$keepDays\s*=\s*(\d+)"),
                     ("minFreeGB", r"^\$minFreeGB\s*=\s*(\d+)")):
        m = re.search(pat, text, re.M)
        if m:
            out[key] = int(m.group(1)) if m.group(1).isdigit() else m.group(1)
    cameras = []
    m = re.search(r"^\$cameras\s*=\s*@\((.*?)^\)", text, re.M | re.S)
    if m:
        for line in m.group(1).splitlines():
            cm = re.match(r"^\s*'([^']+)'\s*,?\s*(#.*)?$", line)
            if cm:
                cameras.append({"name": cm.group(1),
                                "comment": (cm.group(2) or "").lstrip("# ").strip()})
    out["cameras"] = cameras
    return out


def patch_ps1(text, root=None, segment_minutes=None, keep_days=None, cameras=None):
    """Replace only the values named, leaving every other byte alone."""
    changed = {}

    def sub_once(pattern, replacement):
        nonlocal text
        text, n = re.subn(pattern, replacement, text, count=1, flags=re.M)
        return n

    if root:
        if not sub_once(r"^(\$root\s*=\s*)'[^']*'", lambda m: m.group(1) + f"'{root}'"):
            raise ValueError("could not find $root in the recorder script")
        changed["root"] = root
    if segment_minutes:
        if not sub_once(r"^(\$segmentMinutes\s*=\s*)\d+",
                        lambda m: m.group(1) + str(int(segment_minutes))):
            raise ValueError("could not find $segmentMinutes in the recorder script")
        changed["segmentMinutes"] = int(segment_minutes)
    if keep_days:
        if not sub_once(r"^(\$keepDays\s*=\s*)\d+",
                        lambda m: m.group(1) + str(int(keep_days))):
            raise ValueError("could not find $keepDays in the recorder script")
        changed["keepDays"] = int(keep_days)
    if cameras:
        m = re.search(r"^(\$cameras\s*=\s*@\()(.*?)(^\))", text, re.M | re.S)
        if not m:
            raise ValueError("could not find the $cameras list in the recorder script")
        body = []
        for i, cam in enumerate(cameras):
            comma = "," if i < len(cameras) - 1 else ""
            note = (cam.get("comment") or "").strip()
            width = max(len(c.get("name", "")) for c in cameras)
            entry = f"'{cam['name']}'{comma}".ljust(width + 3)
            body.append("  " + (f"{entry} # {note}" if note else entry.rstrip()).rstrip())
        text = text[:m.start(2)] + "\n" + "\n".join(body) + "\n" + text[m.start(3):]
        changed["cameras"] = [c["name"] for c in cameras]
    return text, changed


# ------------------------------------------------------------------- cameras
def event_url_for(stream_urls):
    """The ONVIF event service, inferred from the transport the camera uses.

    The two families differ: the ones with RTSP on 10554 expose events on 10080,
    the plain-RTSP ones on 8899, and DVRIP-only cameras have no ONVIF at all.
    """
    for url in stream_urls:
        m = re.search(r"rtsp://(?:[^/@]*@)?(\d+\.\d+\.\d+\.\d+):(\d+)", url or "")
        if m:
            ip, port = m.group(1), m.group(2)
            if port == "10554":
                return f"http://{ip}:10080/onvif/event_services"
            if port == "554":
                return f"http://{ip}:8899/onvif/event_service"
    for url in stream_urls:
        m = re.search(r"dvrip://(?:[^@]*@)?(\d+\.\d+\.\d+\.\d+)", url or "")
        if m:
            return None                      # speaks DVRIP only; no event service
    return None


def ip_of_stream(name):
    m = re.search(r"cam(\d+)", name or "")
    return f"192.168.1.{m.group(1)}" if m else None


def cameras_from_streams(entries, names):
    """One record per camera, gathering its sub/main streams under one IP."""
    by_ip = {}
    for e in entries:
        ip = ip_of_stream(e["name"])
        if not ip:
            continue
        rec = by_ip.setdefault(ip, {"ip": ip, "name": names.get(ip) or "", "streams": []})
        rec["streams"].append(e["name"])
    for ip, rec in by_ip.items():
        urls = [e.get("url") or (e.get("urls") or [None])[0]
                for e in entries if ip_of_stream(e["name"]) == ip]
        rec["eventUrl"] = event_url_for(urls)
    return [by_ip[k] for k in sorted(by_ip, key=lambda a: int(a.split(".")[-1]))]


# --------------------------------------------------------------------- build
def build():
    yaml_text = _read(YAML_FILE)
    if yaml_text is None:
        raise FileNotFoundError(f"{YAML_FILE} not found")
    prologue, entries, trailing = parse_yaml(yaml_text)
    names_raw = _read(NAMES_FILE)
    try:
        cfg = json.loads(names_raw) if names_raw else {}
    except ValueError:
        cfg = {}
    names = cfg.get("names") if isinstance(cfg, dict) else {}
    order = cfg.get("order") if isinstance(cfg, dict) else []
    ps1_text = _read(SCRIPT_FILE) or ""
    rec = parse_ps1(ps1_text)
    cams = cameras_from_streams(entries, names or {})
    return {
        "format": CONFIG_FORMAT,
        "version": CONFIG_VERSION,
        "saved": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "host": platform.node(),
        "note": "Camera passwords are included - keep this file private.",
        "cameras": cams,
        "streams": entries,
        "prologue": prologue,
        "trailing": trailing,
        "names": names or {},
        "order": order or [],
        "recorder": {
            "root": rec.get("root"),
            "segmentMinutes": rec.get("segmentMinutes"),
            "keepDays": rec.get("keepDays"),
            "minFreeGB": rec.get("minFreeGB"),
            "cameras": rec.get("cameras") or [],
        },
        "events": {c["ip"]: c["eventUrl"] for c in cams},
    }


# ------------------------------------------------------------------- validate
def validate(data):
    """Raise ValueError with something a human can act on."""
    if not isinstance(data, dict):
        raise ValueError("the file is not a JSON object")
    if data.get("format") != CONFIG_FORMAT:
        raise ValueError(f"not a {CONFIG_FORMAT} file "
                         f"(found format={data.get('format')!r})")
    if not isinstance(data.get("prologue"), list):
        raise ValueError("the file has no go2rtc.yaml prologue")
    streams = data.get("streams")
    if not isinstance(streams, list) or not streams:
        raise ValueError("the file lists no streams")
    for e in streams:
        if not isinstance(e, dict) or not re.fullmatch(r"[\w.\-]+", str(e.get("name") or "")):
            raise ValueError(f"bad stream name: {e!r}")
        urls = [e.get("url")] if e.get("url") else (e.get("urls") or [])
        if not urls or not all(isinstance(u, str) and u.strip() for u in urls):
            raise ValueError(f"stream {e.get('name')} has no URL")
    for key, val in (data.get("names") or {}).items():
        if not isinstance(val, str):
            raise ValueError(f"name for {key} is not text")
    rec = data.get("recorder") or {}
    root = rec.get("root")
    if not root or not (re.match(r"^[A-Za-z]:[\\/]", str(root)) or str(root).startswith("\\\\")):
        raise ValueError("recorder root must be an absolute path")
    for key in ("segmentMinutes", "keepDays"):
        v = rec.get(key)
        if v is not None and (not isinstance(v, int) or v <= 0):
            raise ValueError(f"{key} must be a positive whole number (got {v!r})")
    return True


# -------------------------------------------------------------------- apply
def _service_running(name):
    try:
        out = subprocess.run(["sc", "query", name], capture_output=True, text=True,
                             timeout=15).stdout
        m = re.search(r"STATE\s*:\s*\d+\s+(\w+)", out)
        return bool(m and m.group(1).upper().startswith("RUN"))
    except Exception:                                          # noqa: BLE001
        return False


def _restart_service(name):
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Restart-Service {name} -Force"],
                       capture_output=True, text=True, timeout=120)
        return True, ""
    except Exception as exc:                                   # noqa: BLE001
        return False, str(exc)


def _backup_all():
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = os.path.join(BACKUP_DIR, stamp)
    os.makedirs(folder, exist_ok=True)
    kept = []
    for path in (YAML_FILE, NAMES_FILE, SCRIPT_FILE):
        if os.path.isfile(path):
            shutil.copy2(path, os.path.join(folder, os.path.basename(path)))
            kept.append(os.path.basename(path))
    return folder, kept


def _restore_backup(folder, kept):
    for name in kept:
        src = os.path.join(folder, name)
        dst = os.path.join(ROOT, name)
        if os.path.isfile(src):
            shutil.copy2(src, dst)


def apply(data, restart=True):
    """Write a saved config back. Returns a report; raises ValueError if refused."""
    validate(data)
    report = {"applied": [], "warnings": [], "backups": None, "restarts": []}

    folder, kept = _backup_all()
    report["backups"] = {"folder": folder, "files": kept}
    print(f"backed up {kept} to {folder}", flush=True)
    try:
        # 1. streams -> go2rtc.yaml, prologue and comments preserved
        yaml_text = render_yaml(data["prologue"], data["streams"], data.get("trailing") or [])
        _write(YAML_FILE, yaml_text)
        report["applied"].append(f"go2rtc.yaml: {len(data['streams'])} stream(s)")

        # 2. names + order + event endpoints -> cameras.json
        events_map = {ip: url for ip, url in (data.get("events") or {}).items() if url}
        payload = {"names": data.get("names") or {}, "order": data.get("order") or [],
                   "events": events_map}
        _write(NAMES_FILE, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        report["applied"].append(f"cameras.json: {len(payload['names'])} name(s), "
                                 f"{len(payload['order'])} in order, "
                                 f"{len(events_map)} event endpoint(s)")

        # 3. recorder settings + camera list -> record-cameras.ps1
        rec = data.get("recorder") or {}
        text = _read(SCRIPT_FILE)
        if text is None:
            report["warnings"].append("no recorder script, so its settings were skipped")
        else:
            new_text, changed = patch_ps1(
                text, root=rec.get("root"),
                segment_minutes=rec.get("segmentMinutes"),
                keep_days=rec.get("keepDays"),
                cameras=rec.get("cameras") or None)
            _write(SCRIPT_FILE, new_text)
            # read it back: a substitution that went wrong must not reach the service
            check = parse_ps1(_read(SCRIPT_FILE) or "")
            for key, want in (("root", rec.get("root")),
                              ("segmentMinutes", rec.get("segmentMinutes")),
                              ("keepDays", rec.get("keepDays"))):
                if want is not None and check.get(key) != want:
                    raise ValueError(f"the recorder script did not take {key}="
                                     f"{want!r} (now {check.get(key)!r})")
            if rec.get("cameras"):
                got = [c["name"] for c in check.get("cameras") or []]
                want = [c["name"] for c in rec["cameras"]]
                if got != want:
                    raise ValueError(f"the recorder camera list did not take "
                                     f"(wanted {want}, now {got})")
            report["applied"].append("record-cameras.ps1: " +
                                     ", ".join(f"{k}={v}" for k, v in changed.items()))
    except Exception as exc:                                   # noqa: BLE001
        _restore_backup(folder, kept)
        print(f"apply failed ({exc}); restored from {folder}", flush=True)
        raise ValueError(f"{exc} - every file has been put back from {folder}") from exc

    if restart:
        for name in (STREAM_SERVICE, RECORDER_SERVICE, EVENTS_SERVICE):
            if not _service_running(name):
                continue
            ok, err = _restart_service(name)
            report["restarts"].append({"service": name, "ok": ok, "error": err})
    return report


# ---------------------------------------------------------------------- main
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "show":
        cfg = build()
        printable = dict(cfg)
        printable["streams"] = [
            {**e, **(  {"url": re.sub(r"://([^:@/]+):[^@]*@", r"://\1:***@", e["url"])}
                    if e.get("url") else {"urls": [re.sub(r"://([^:@/]+):[^@]*@", r"://\1:***@", u)
                                                   for u in e["urls"]]})}
            for e in cfg["streams"]]
        print(json.dumps(printable, indent=2, ensure_ascii=False))
    elif cmd == "save":
        target = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "cctv-config.json")
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(build(), fh, indent=2, ensure_ascii=False)
        print(f"wrote {target} ({os.path.getsize(target):,} bytes)")
    else:
        print("usage: cctvconfig.py [show|save <file>]")
        sys.exit(2)
