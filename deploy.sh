#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
HOST="${SONOS_ART_HOST:-}"
REMOTE_DIR="/tmp/sonos-art-deploy"

if [ -z "$HOST" ]; then
  echo "Set SONOS_ART_HOST, for example: SONOS_ART_HOST=nick@sonos-art.local bash $0" >&2
  exit 2
fi

ssh "$HOST" "mkdir -p '$REMOTE_DIR'"
rsync -av "$SCRIPT_DIR/sonos_art.py" "$SCRIPT_DIR/sonos-art.service" "$HOST:$REMOTE_DIR/"
ssh -tt "$HOST" "sudo /opt/sonos-art/venv/bin/pip install paho-mqtt && sudo cp '$REMOTE_DIR/sonos_art.py' /opt/sonos-art/sonos_art.py && sudo cp '$REMOTE_DIR/sonos-art.service' /etc/systemd/system/sonos-art.service && sudo systemctl daemon-reload && sudo systemctl restart sonos-art.service && systemctl --no-pager --full status sonos-art.service"
