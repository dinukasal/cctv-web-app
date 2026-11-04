# YOLO11 vs YOLOv8n for person detection — benchmark notes

**Date:** 2026-11-03
**Question:** can we switch the human detection from `yolov8n` to a YOLO11 model?

**Answer:** yes, it is supported and it runs — but on *this* camera footage the
YOLO11 models detect **fewer** people than the current `yolov8n`, including clear
ones. It is not a drop-in upgrade, so the model was left on `yolov8n` pending a
decision. This file records the evidence.

## Where the model is used today

| role | file | set by |
| --- | --- | --- |
| primary (PC, RTX 2060) | `/home/gpu-host/yolo-detect/yolov8n.pt` | `DETECT_MODEL` env in `yolo-detect.service` |
| fallback (laptop, GTX 1050) | `C:\go2rtc\yolov8n.pt` | `get_detector()` in `events.py` |

Detection runs at `imgsz=640`, person class only, and a person counts at
`PERSON_CONF = 0.4` (the saved evidence image is still full 1920×1080).

## Benchmark — primary GPU (gpu-host RTX 2060 SUPER, ~7.5 GB free)

30 real frames (1920×1080) taken from `events.jsonl` entries where a person had
been detected, `imgsz=640`, person class only. "frames@X" = frames with a person
at that confidence.

| model | ms/frame | people found | frames@0.25 | @0.30 | **@0.40** |
| --- | --- | --- | --- | --- | --- |
| **yolov8n** (current) | 26 | 36 | 30 | 30 | **24** |
| yolo11n | 18 | 32 | 23 | 21 | 19 |
| yolo11s | 19 | 26 | 20 | 20 | 17 |
| yolo11m | 27 | 22 | 20 | 19 | 19 |
| yolo11l | 30 | 23 | 20 | 20 | 20 |

YOLO11 is faster and its confidences are higher on what it *does* detect, but it
finds fewer of the frames.

### Caveat on the sample

The 30 frames were **selected because `yolov8n` detected a person in them**, so
the sample is biased toward `yolov8n`. That alone would not prove much — but the
misses were checked by eye and several are genuine people (below).

## Benchmark — fallback GPU (laptop GTX 1050, 6 frames)

| model | avg ms/frame |
| --- | --- |
| yolov8n | 44 |
| yolo11n | 36 |
| yolo11s | 42 |

## Sanity check on a standard image (`bus.jpg`)

To rule out a setup problem, all five models were run on Ultralytics' `bus.jpg`.
Here YOLO11 is clearly **better** — it even finds the small background person:

| model | detections |
| --- | --- |
| yolov8n | bus 0.87, person 0.87 / 0.85 / 0.83, person 0.26, stop sign 0.26 |
| yolo11n | bus 0.94, person 0.89 / 0.88 / 0.86 / 0.62 |
| yolo11s | bus 0.93, person 0.90 / 0.88 / 0.87 / 0.66 |
| yolo11m | bus 0.93, person 0.92 / 0.91 / 0.89 / 0.79 |
| yolo11l | bus 0.94, person 0.91 / 0.91 / 0.89 / 0.82 |

So the models work correctly; the difference is specific to these security-camera
scenes (wide-angle, burned-in OSD text, occlusion, small/edge subjects).

## Real misses confirmed visually

| frame | scene | yolov8n | yolo11n | yolo11s | yolo11m | yolo11l |
| --- | --- | --- | --- | --- | --- | --- |
| 07 | person on a ladder, hanging laundry | 0.42 | — | — | — | — |
| 08 | woman standing at the door | 0.58 | 0.39 | — | — | 0.52 |
| 20 | person walking up the stairs | 0.47 | — | 0.92 | — | — |

(`—` = no person detection at all.) All three are real people; the ladder and
door cases are exactly the kind of event the system must not miss.

## Options

1. **Keep YOLOv8n** — best recall on this footage (current state).
2. **Switch to YOLO11-l on the PC** — closest YOLO11 (30 ms/frame) and lower
   `PERSON_CONF` from 0.4 to ~0.30. Even then it found 20/30 vs yolov8n's 30/30.
3. **Ensemble** — run both `yolov8n` and `yolo11l` on the PC and count a person
   if **either** sees one. Best recall, ~50 ms/frame on the 2060 (still trivial
   at ~1.75 scans/sec); the laptop fallback stays `yolov8n`.

## How to reproduce

```bash
# stage frames + models on the PC
scp -r <frames> gpu-host@192.168.1.157:/tmp/bench_frames/
# download YOLO11 weights (any size)
curl -sL -O https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11l.pt
# run
~/plate-blur-venv/bin/python - <<'PY'
import glob, time
from ultralytics import YOLO
frames = sorted(glob.glob("/tmp/bench_frames/*.jpg"))
for w in ("yolov8n.pt", "yolo11n.pt", "yolo11s.pt", "yolo11m.pt", "yolo11l.pt"):
    m = YOLO("/tmp/bench_frames/" + w); m.predict(frames[0], imgsz=640, device=0, verbose=False)
    t = time.time(); hits = 0
    for f in frames:
        r = m.predict(f, imgsz=640, device=0, verbose=False)[0]
        cs = [float(c) for c, cl in zip(r.boxes.conf.tolist(), r.boxes.cls.tolist())
              if r.names[int(cl)] == "person"]
        hits += bool(cs and max(cs) >= 0.4)
    print(f"{w:12} {(time.time()-t)/len(frames)*1000:5.0f} ms  frames@0.4={hits}/{len(frames)}")
PY
```

## If we do switch

- PC: put the weights in `/home/gpu-host/yolo-detect/` and set
  `Environment=DETECT_MODEL=/home/gpu-host/yolo-detect/<weights>.pt` in
  `yolo-detect.service`, then `systemctl restart yolo-detect`.
- Laptop fallback: change the `weights` path in `events.py::get_detector()`
  (currently `os.path.join(ROOT, "yolov8n.pt")`) and restart `go2rtc-events`.
- Re-tune `PERSON_CONF` — YOLO11's confidences are calibrated differently, so the
  0.4 threshold (tuned for v8n) is not transferable.
