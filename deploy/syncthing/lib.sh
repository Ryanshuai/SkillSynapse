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

st_os() {
  case "$(uname -s)" in
    Linux)  echo "linux" ;;
    Darwin) echo "macos" ;;
    *) die "unsupported OS $(uname -s) — Windows installs Syncthing manually (see README)" ;;
  esac
}

st_arch() {
  case "$(uname -m)" in
    x86_64)  echo "amd64" ;;
    aarch64) echo "arm64" ;;   # Jetson-class arm64 boards
    arm64)   echo "arm64" ;;   # Apple Silicon reports arm64, not aarch64
    *) die "unsupported arch $(uname -m)" ;;
  esac
}

# Rootless install of the official static binary into ~/.local/bin (no sudo).
# Linux ships .tar.gz, macOS ships .zip — same layout inside (<name>/syncthing).
install_syncthing() {
  if [ -x "$HOME/.local/bin/syncthing" ] && \
     "$HOME/.local/bin/syncthing" --version 2>/dev/null | grep -q "$SYNCTHING_VER"; then
    echo "syncthing $SYNCTHING_VER already installed"; return
  fi
  local os arch name ext url tmp
  os="$(st_os)"; arch="$(st_arch)"
  name="syncthing-${os}-${arch}-${SYNCTHING_VER}"
  [ "$os" = macos ] && ext="zip" || ext="tar.gz"
  url="https://github.com/syncthing/syncthing/releases/download/${SYNCTHING_VER}/${name}.${ext}"
  tmp="$(mktemp -d)"
  echo "downloading $url"
  curl -fsSL -o "$tmp/st.$ext" "$url" || die "download failed"
  if [ "$ext" = zip ]; then unzip -q "$tmp/st.zip" -d "$tmp"; else tar xzf "$tmp/st.tar.gz" -C "$tmp"; fi
  mkdir -p "$HOME/.local/bin"
  install -m755 "$tmp/${name}/syncthing" "$HOME/.local/bin/syncthing"
  rm -rf "$tmp"
  # Gatekeeper quarantines anything curl'd; without this macOS kills it on exec.
  [ "$os" = macos ] && xattr -d com.apple.quarantine "$HOME/.local/bin/syncthing" 2>/dev/null
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

# Install a per-user service so syncthing survives logout/reboot.
# NOTE: for a headless always-on hub you also need, ONCE, with sudo:
#     Linux: sudo loginctl enable-linger "$USER"
#     macOS: enable automatic login (System Settings > Users & Groups), because a
#            LaunchAgent lives in the gui/<uid> domain and needs a logged-in session.
#            FileVault blocks auto-login; with FileVault on, use a LaunchDaemon instead.
# (that is the only privileged step in the whole setup; validation runs fine without it.)
install_user_service() {
  [ "$(st_os)" = macos ] && { install_launch_agent; return; }
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

# macOS counterpart: a LaunchAgent. Deliberately NOT a plist template file —
# it has to carry absolute paths, and a checked-in template with someone else's
# $HOME baked in is the kind of thing that silently starts nothing.
install_launch_agent() {
  local label="net.syncthing.cc" dir="$HOME/Library/LaunchAgents" plist
  plist="$dir/${label}.plist"
  mkdir -p "$dir"
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>${label}</string>
  <key>ProgramArguments</key><array>
    <string>${HOME}/.local/bin/syncthing</string>
    <string>serve</string>
    <string>--home</string><string>${STH_HOME}</string>
    <string>--no-browser</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${STH_HOME}/syncthing.log</string>
  <key>StandardErrorPath</key><string>${STH_HOME}/syncthing.err</string>
</dict></plist>
EOF
  launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
  if launchctl bootstrap "gui/$(id -u)" "$plist" 2>/dev/null; then
    echo "LaunchAgent loaded: $plist"
  else
    echo "WARN: launchctl bootstrap failed — no GUI session for uid $(id -u)?"
    echo "      A LaunchAgent only runs while someone is logged in. For an"
    echo "      always-on headless hub, enable auto-login or use a LaunchDaemon."
  fi
}
