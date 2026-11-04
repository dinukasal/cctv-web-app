#!/usr/bin/env bash
# Install the go2rtc remote person detector on gpu-host. Run as root.
#
# Expects detect_server.py, yolov8n.pt and yolo-detect.service in the same
# directory as this script (copy them to /tmp first, see the repo README).
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/home/gpu-host/yolo-detect
VENV=/home/gpu-host/plate-blur-venv
PORT="${DETECT_PORT:-8882}"

echo ">> installing to $DEST"
install -d -o gpu-host -g gpu-host "$DEST"
install -o gpu-host -g gpu-host -m 0644 "$SRC/detect_server.py" "$DEST/detect_server.py"
install -o gpu-host -g gpu-host -m 0644 "$SRC/yolov8n.pt"       "$DEST/yolov8n.pt"
install -m 0644 "$SRC/yolo-detect.service" /etc/systemd/system/yolo-detect.service

echo ">> checking the venv has what the server needs"
"$VENV/bin/python" - <<'PY'
import torch, ultralytics, cv2, numpy
print("torch", torch.__version__, "cuda", torch.cuda.is_available())
print("ultralytics", ultralytics.__version__, "cv2", cv2.__version__)
PY

echo ">> opening the firewall for $PORT (LAN + tailnet only)"
ufw allow from 192.168.1.0/24 to any port "$PORT" proto tcp >/dev/null || true
ufw allow from 100.64.0.0/10  to any port "$PORT" proto tcp >/dev/null || true

echo ">> enabling and (re)starting the service"
systemctl daemon-reload
systemctl enable yolo-detect.service >/dev/null
systemctl restart yolo-detect.service
sleep 4
systemctl --no-pager --full status yolo-detect.service | head -12 || true
echo ">> health:"
curl -s "http://127.0.0.1:$PORT/health" || true
echo
