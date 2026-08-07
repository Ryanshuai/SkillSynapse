#!/usr/bin/env bash
# Persistence for a HEADLESS macOS hub. Run once, with sudo:
#     sudo ./macos-launchdaemon.sh [USER]
#
# Why this exists at all: install_user_service() installs a LaunchAgent, and a
# LaunchAgent lives in the gui/<uid> domain — it only runs while that user is
# logged in at the console. A Mac mini acting as an always-on hub typically has
# nobody logged in (`uptime` reports 0 users), so `launchctl bootstrap gui/<uid>`
# fails and the hub silently has no daemon: syncthing looks "configured" from
# every script's output while nothing is actually running.
#
# The alternative is enabling automatic login, which FileVault blocks. A
# LaunchDaemon lives in the system domain, needs no session, and still runs
# syncthing as the owning user (UserName) rather than root — the config, keys and
# cc-logs/ all stay owned by that user.
set -euo pipefail

LABEL="net.syncthing.cc"
PLIST="/Library/LaunchDaemons/${LABEL}.plist"

[ "$(uname -s)" = Darwin ] || { echo "ERROR: macOS only" >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo "ERROR: run with sudo" >&2; exit 1; }

USER_NAME="${1:-${SUDO_USER:-}}"
[ -n "$USER_NAME" ] || { echo "ERROR: no user — pass one: sudo $0 <user>" >&2; exit 1; }
USER_HOME="$(dscl . -read "/Users/$USER_NAME" NFSHomeDirectory | awk '{print $2}')"
[ -d "$USER_HOME" ] || { echo "ERROR: no home for $USER_NAME" >&2; exit 1; }

BIN="$USER_HOME/.local/bin/syncthing"
STH_HOME="$USER_HOME/.local/share/syncthing"
[ -x "$BIN" ] || { echo "ERROR: $BIN not installed — run setup-hub.sh first" >&2; exit 1; }

# A LaunchAgent for the same label would fight this daemon for port 22000 the
# moment anyone logs in. One owner only.
rm -f "$USER_HOME/Library/LaunchAgents/${LABEL}.plist"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>${LABEL}</string>
  <key>UserName</key><string>${USER_NAME}</string>
  <key>GroupName</key><string>staff</string>
  <key>ProgramArguments</key><array>
    <string>${BIN}</string>
    <string>serve</string>
    <string>--home</string><string>${STH_HOME}</string>
    <string>--no-browser</string>
  </array>
  <key>WorkingDirectory</key><string>${USER_HOME}</string>
  <key>EnvironmentVariables</key><dict>
    <key>HOME</key><string>${USER_HOME}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${STH_HOME}/syncthing.log</string>
  <key>StandardErrorPath</key><string>${STH_HOME}/syncthing.err</string>
</dict></plist>
EOF
chown root:wheel "$PLIST"; chmod 644 "$PLIST"

# Any instance started by hand (nohup during validation) still owns the port.
pkill -f "syncthing serve" 2>/dev/null || true
sleep 2

launchctl bootout "system/${LABEL}" 2>/dev/null || true
launchctl bootstrap system "$PLIST"
launchctl enable "system/${LABEL}"

echo "installed $PLIST (runs as $USER_NAME, no login session needed)"
launchctl print "system/${LABEL}" | grep -E '^\s+(state|pid) ' || true
