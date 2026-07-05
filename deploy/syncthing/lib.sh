# Shared helpers for the CC-log aggregation Syncthing setup.
# Sourced by setup-hub.sh / onboard-source.sh. Not meant to run standalone.
#
# Design principle: intranet-only. Syncthing rides the Tailscale mesh and has
# NO public code path (discovery / relay / NAT / auto-upgrade / telemetry all
# off). See ../../multi-machine-cc-aggregation-design.md §3.2, §7.

SYNCTHING_VER="${SYNCTHING_VER:-v2.1.1}"   # pin the same major across all nodes
STH_HOME="${STH_HOME:-$HOME/.local/share/syncthing}"
STCONFIG="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/stconfig.py"

die() { echo "ERROR: $*" >&2; exit 1; }

st_arch() {
  case "$(uname -m)" in
    x86_64)  echo "amd64" ;;
    aarch64) echo "arm64" ;;   # Jetson-class arm64 boards
    *) die "unsupported arch $(uname -m) — Windows/mac install Syncthing manually" ;;
  esac
}

# Rootless install of the official static binary into ~/.local/bin (no sudo).
install_syncthing() {
  if [ -x "$HOME/.local/bin/syncthing" ] && \
     "$HOME/.local/bin/syncthing" --version 2>/dev/null | grep -q "$SYNCTHING_VER"; then
    echo "syncthing $SYNCTHING_VER already installed"; return
  fi
  local arch tarball url tmp
  arch="$(st_arch)"
  tarball="syncthing-linux-${arch}-${SYNCTHING_VER}.tar.gz"
  url="https://github.com/syncthing/syncthing/releases/download/${SYNCTHING_VER}/${tarball}"
  tmp="$(mktemp -d)"
  echo "downloading $url"
  curl -fsSL -o "$tmp/st.tgz" "$url" || die "download failed"
  tar xzf "$tmp/st.tgz" -C "$tmp"
  mkdir -p "$HOME/.local/bin"
  install -m755 "$tmp/syncthing-linux-${arch}-${SYNCTHING_VER}/syncthing" "$HOME/.local/bin/syncthing"
  rm -rf "$tmp"
  echo "installed: $("$HOME/.local/bin/syncthing" --version | head -1)"
}

# Generate config + device id if absent. GUI bound to localhost with a random
# password saved to $STH_HOME/gui-password.txt.
generate_config() {
  if [ -f "$STH_HOME/config.xml" ]; then
    echo "config exists at $STH_HOME (keeping it)"; return
  fi
  local pw; pw="$(openssl rand -base64 12)"
  "$HOME/.local/bin/syncthing" generate --home "$STH_HOME" \
    --gui-user "${GUI_USER:-$USER}" --gui-password "$pw" >/dev/null 2>&1
  echo "$pw" > "$STH_HOME/gui-password.txt"; chmod 600 "$STH_HOME/gui-password.txt"
  echo "generated config, gui password -> $STH_HOME/gui-password.txt"
}

self_device_id() { "$HOME/.local/bin/syncthing" --home "$STH_HOME" device-id 2>/dev/null; }

# Install a systemd --user unit so syncthing survives logout/reboot.
# NOTE: for a headless always-on hub you also need, ONCE, with sudo:
#     sudo loginctl enable-linger "$USER"
# (that is the only sudo step in the whole setup; validation runs fine without it.)
install_user_service() {
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/syncthing-cc.service" <<EOF
[Unit]
Description=Syncthing (CC-log aggregation, intranet-only)
After=network-online.target

[Service]
ExecStart=$HOME/.local/bin/syncthing serve --home $STH_HOME --no-browser
Restart=on-failure
SuccessExitStatus=3 4

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now syncthing-cc.service 2>/dev/null \
    && echo "systemd --user service enabled" \
    || echo "WARN: could not enable user service (need: sudo loginctl enable-linger $USER)"
}
