#!/usr/bin/env bash
# A browser-reachable Claude Code on the hub. Run once, with sudo:
#     sudo ./deploy-ttyd.sh [USER]
#
# Shape:
#     browser ──https──▶ tailscale serve ──▶ 127.0.0.1:7681 (ttyd) ──▶ tmux ──▶ claude
#
# Why ttyd binds loopback and not the tailnet address: ttyd is a shell in a web page.
# Bound to the tailnet it is reachable by every node on it — including `zedbox`, which
# LOCAL-TOPOLOGY records as "joined 2026-07-25, purpose unknown, fleet key does not
# get in". `tailscale serve` puts the tailnet's own identity check in front instead,
# so reaching the page requires being a node *and* passing Tailscale auth, and the
# transport is HTTPS rather than plaintext across the mesh.
#
# Why tmux and not bare claude: closing the tab kills the pty. tmux makes the session
# outlive the browser — reopen and the conversation is still there, which is the whole
# point of running the agent on the always-on box instead of the laptop.
set -euo pipefail

[ "$(uname -s)" = Darwin ] || { echo "ERROR: macOS only" >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo "ERROR: run with sudo" >&2; exit 1; }

USER_NAME="${1:-${SUDO_USER:-}}"
[ -n "$USER_NAME" ] || { echo "ERROR: no user — pass one: sudo $0 <user>" >&2; exit 1; }
USER_HOME="$(dscl . -read "/Users/$USER_NAME" NFSHomeDirectory | awk '{print $2}')"

LABEL="net.skillsynapse.ttyd"
PLIST="/Library/LaunchDaemons/${LABEL}.plist"
PORT=7681
TTYD="/opt/homebrew/bin/ttyd"
WRAPPER="$USER_HOME/.local/bin/claude-shell"
LOGDIR="$USER_HOME/.claude/skillsynapse/logs"

[ -x "$TTYD" ] || { echo "ERROR: $TTYD missing — brew install ttyd" >&2; exit 1; }

install -d -o "$USER_NAME" -g staff "$USER_HOME/.local/bin" "$LOGDIR"

# The wrapper exists so the OAuth token never lands in the plist. This box has never
# run `claude /login` — there is no ~/.claude/.credentials.json — and an interactive
# claude would sit at the login prompt forever. The token it does have lives in
# secrets.env (mode 600); sourcing it here keeps it at 600 instead of copying it into
# a world-readable plist. Same reasoning as com.haclaw.bot, which also reads the file
# rather than carrying the value.
cat > "$WRAPPER" <<'EOF'
#!/bin/zsh
# Interactive Claude Code for the browser terminal. Loads the fleet's subscription
# token, because this machine has no interactive login of its own.
set -a
[ -r "$HOME/.config/haclaw/secrets.env" ] && . "$HOME/.config/haclaw/secrets.env"
set +a
# The bot's sessions live in ~/.claude-haclaw-bot; these are the human's, so they
# belong in the real config dir where `claude --resume` and the history picker find them.
unset CLAUDE_CONFIG_DIR
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"
cd "${1:-$HOME/code}" 2>/dev/null || cd "$HOME"
exec /opt/homebrew/bin/tmux new-session -A -s claude
EOF
chown "$USER_NAME":staff "$WRAPPER"
chmod 700 "$WRAPPER"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>${LABEL}</string>
  <key>UserName</key><string>${USER_NAME}</string>
  <key>GroupName</key><string>staff</string>
  <key>ProgramArguments</key><array>
    <string>${TTYD}</string>
    <string>--port</string><string>${PORT}</string>
    <string>--interface</string><string>127.0.0.1</string>
    <string>--writable</string>
    <string>--client-option</string><string>titleFixed=mac-mini · claude</string>
    <string>--client-option</string><string>fontSize=14</string>
    <string>--client-option</string><string>disableLeaveAlert=true</string>
    <string>${WRAPPER}</string>
  </array>
  <key>WorkingDirectory</key><string>${USER_HOME}</string>
  <key>EnvironmentVariables</key><dict>
    <key>HOME</key><string>${USER_HOME}</string>
    <key>PATH</key><string>/opt/homebrew/bin:${USER_HOME}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>TERM</key><string>xterm-256color</string>
    <key>LANG</key><string>en_US.UTF-8</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>${LOGDIR}/ttyd.log</string>
  <key>StandardErrorPath</key><string>${LOGDIR}/ttyd.err</string>
</dict></plist>
EOF
chown root:wheel "$PLIST"; chmod 644 "$PLIST"

launchctl bootout "system/${LABEL}" 2>/dev/null || true
launchctl enable "system/${LABEL}"
launchctl bootstrap system "$PLIST"
sleep 2
echo "ttyd:"
launchctl print "system/${LABEL}" 2>/dev/null | grep -E '^\s+(state|pid) ' || echo "  (not loaded)"

# tailscale serve config is persistent across reboots on its own, so this is idempotent.
sudo -u "$USER_NAME" /opt/homebrew/bin/tailscale serve --bg "${PORT}" >/dev/null 2>&1 \
  || /opt/homebrew/bin/tailscale serve --bg "${PORT}" >/dev/null 2>&1 || true

echo
/opt/homebrew/bin/tailscale serve status 2>&1 | head -8
