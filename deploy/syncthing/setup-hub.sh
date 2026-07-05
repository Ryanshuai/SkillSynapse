#!/usr/bin/env bash
# Run ONCE on the aggregation hub (the always-on box; real host/IP mapping
# lives in LOCAL-TOPOLOGY.md, which never enters the repo).
# Sets up an intranet-only Syncthing that receives CC logs from every source
# machine into $HUB_BASE/<hostname>/.
#
# Usage:
#   ./setup-hub.sh <HUB_TAILNET_IP> [HUB_BASE]
# Example:
#   ./setup-hub.sh 100.x.y.z
#
# After it prints the hub Device ID, onboard each source with onboard-source.sh
# and register it here with add-source.sh.
set -euo pipefail
cd "$(dirname "$0")"
source ./lib.sh

HUB_IP="${1:?usage: setup-hub.sh <HUB_TAILNET_IP> [HUB_BASE]}"
HUB_BASE="${2:-$HOME/cc-logs}"

install_syncthing
generate_config
mkdir -p "$HUB_BASE"

# Harden for intranet-only + bind listener to the tailnet IP.
python3 "$STCONFIG" --config "$STH_HOME/config.xml" \
  --isolate --listen "tcp://${HUB_IP}:22000"

install_user_service || true

echo
echo "================ HUB READY ================"
echo "hub device-id : $(self_device_id)"
echo "hub base dir  : $HUB_BASE   (chmod 700 recommended)"
echo "listen        : tcp://${HUB_IP}:22000  (tailnet only)"
echo "Next: on each source run onboard-source.sh, then register it here:"
echo "  ./add-source.sh <SOURCE_ID> <SOURCE_HOSTNAME> <SOURCE_TAILNET_IP> $HUB_BASE"
chmod 700 "$HUB_BASE" || true
