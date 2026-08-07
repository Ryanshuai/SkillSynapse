#!/usr/bin/env bash
# Run ON THE HUB to register one source machine: adds the source device and a
# receive-only folder cc-<hostname> at $HUB_BASE/<hostname>/.
# Idempotent (stconfig.py skips existing device/folder).
#
# Usage:
#   ./add-source.sh <SOURCE_ID> <SOURCE_HOSTNAME> <SOURCE_TAILNET_IP> [HUB_BASE]
set -euo pipefail
cd "$(dirname "$0")"
source ./lib.sh

SRC_ID="${1:?SOURCE_ID}"; HOST="${2:?SOURCE_HOSTNAME}"; SRC_IP="${3:?SOURCE_TAILNET_IP}"
HUB_BASE="${4:-$HOME/cc-logs}"
FID="cc-${HOST}"
mkdir -p "$HUB_BASE/$HOST"

python3 "$STCONFIG" --config "$STH_HOME/config.xml" \
  --add-device "${SRC_ID}|${HOST}|tcp://${SRC_IP}:22000" \
  --add-folder "${FID}|${HUB_BASE}/${HOST}|receiveonly|$(self_device_id),${SRC_ID}"

# hot-reload if the daemon is running, else it picks it up on next start
# sed, not `grep -oP`: this script runs ON THE HUB, and the hub is macOS —
# BSD grep has no -P. Same reason install_user_service() branches on st_os.
APIKEY="$(sed -n 's/.*<apikey>\([^<]*\)<\/apikey>.*/\1/p' "$STH_HOME/config.xml" | head -1)"
curl -fsS -X POST -H "X-API-Key: $APIKEY" http://127.0.0.1:8384/rest/system/restart >/dev/null 2>&1 \
  && echo "hub reloaded" || echo "start/restart hub daemon to apply"
echo "registered source '$HOST' -> folder $FID at $HUB_BASE/$HOST"
