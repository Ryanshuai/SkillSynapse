#!/usr/bin/env bash
# Run on EACH source machine (any box whose ~/.claude/projects should aggregate).
# Sets up an intranet-only Syncthing that send-only pushes ~/.claude/projects to
# the hub. Sharing only completes once the hub registers this device
# (add-source.sh on the hub) — this script prints the exact command for that.
#
# Usage:
#   ./onboard-source.sh <HUB_ID> <HUB_TAILNET_IP> <SELF_TAILNET_IP>
# Example (source -> hub; real IPs in LOCAL-TOPOLOGY.md, not in repo):
#   ./onboard-source.sh <HUB_DEVICE_ID> 100.x.y.z 100.x.y.w
set -euo pipefail
cd "$(dirname "$0")"
source ./lib.sh

HUB_ID="${1:?HUB_ID}"; HUB_IP="${2:?HUB_TAILNET_IP}"; SELF_IP="${3:?SELF_TAILNET_IP}"
HOST="$(hostname -s)"
SRC_DIR="${CC_PROJECTS_DIR:-$HOME/.claude/projects}"
FID="cc-${HOST}"

[ -d "$SRC_DIR" ] || die "no $SRC_DIR on this machine"

install_syncthing
generate_config
SELF_ID="$(self_device_id)"

python3 "$STCONFIG" --config "$STH_HOME/config.xml" \
  --isolate --listen "tcp://${SELF_IP}:22000" \
  --add-device "${HUB_ID}|hub|tcp://${HUB_IP}:22000" \
  --add-folder "${FID}|${SRC_DIR}|sendonly|${SELF_ID},${HUB_ID}"

install_user_service || true

echo
echo "================ SOURCE READY: $HOST ================"
echo "self device-id: $SELF_ID"
echo "sharing        : $SRC_DIR  (sendonly)  ->  folder $FID"
echo
echo ">>> Register this source ON THE HUB (run there, or via ssh):"
echo "    ./add-source.sh $SELF_ID $HOST $SELF_IP"
