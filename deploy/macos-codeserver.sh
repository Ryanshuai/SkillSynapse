#!/usr/bin/env bash
# VS Code + the Claude Code extension, in a browser, on the hub. Run once, with sudo:
#     sudo ./macos-codeserver.sh [USER]
#
#     browser ──https──▶ tailscale serve ──┬─ /      ─▶ 127.0.0.1:8080  code-server
#                                          └─ /term  ─▶ 127.0.0.1:7681  ttyd (bare shell)
#
# Why this rather than the bare terminal: a shell makes you drive the agent through a
# TUI. The extension gives the thing the terminal cannot — message history, tool calls
# rendered as tool calls, diffs as diffs. Same agent, a UI that shows its work.
#
# The extension is `Anthropic.claude-code`, and it is on Open VSX under Anthropic's own
# namespace, so code-server installs it from its default registry — no marketplace
# workaround, no sideloaded vsix.
set -euo pipefail

[ "$(uname -s)" = Darwin ] || { echo "ERROR: macOS only" >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo "ERROR: run with sudo" >&2; exit 1; }

USER_NAME="${1:-${SUDO_USER:-}}"
[ -n "$USER_NAME" ] || { echo "ERROR: no user" >&2; exit 1; }
USER_HOME="$(dscl . -read "/Users/$USER_NAME" NFSHomeDirectory | awk '{print $2}')"

LABEL="net.skillsynapse.codeserver"
PLIST="/Library/LaunchDaemons/${LABEL}.plist"
PORT=8080
CS="/opt/homebrew/bin/code-server"
WRAPPER="$USER_HOME/.local/bin/code-server-shell"
LOGDIR="$USER_HOME/.claude/skillsynapse/logs"
WORKDIR="$USER_HOME/code"

[ -x "$CS" ] || { echo "ERROR: $CS missing — brew install code-server" >&2; exit 1; }

install -d -o "$USER_NAME" -g staff "$USER_HOME/.local/bin" "$LOGDIR" \
        "$USER_HOME/.config/code-server"

# No password. The owner's call: the tailnet is the boundary, and a prompt in front
# of a machine only he reaches is friction without a threat model behind it.
#
# What that leans on: `tailscale serve` scopes to "tailnet only", so this is never
# reachable from the internet — but "tailnet only" does mean *every node on the
# tailnet*. Narrowing it further is a Tailscale ACL grant on this node's 443, not
# something code-server can do.
#
# Written unconditionally rather than only-if-missing: code-server writes its own
# config with a generated password the first time it runs, so "leave it alone if it
# exists" would silently reinstate the password on any fresh box.
CS_CONFIG="$USER_HOME/.config/code-server/config.yaml"
cat > "$CS_CONFIG" <<EOF
bind-addr: 127.0.0.1:${PORT}
auth: none
cert: false
EOF
chown "$USER_NAME":staff "$CS_CONFIG"
chmod 600 "$CS_CONFIG"

# Same reason as the ttyd wrapper: this box has never run `claude /login`, so the
# extension has no credentials of its own. The token lives in secrets.env at 600 and
# is sourced here rather than copied into the world-readable plist.
cat > "$WRAPPER" <<EOF
#!/bin/zsh
set -a
[ -r "\$HOME/.config/haclaw/secrets.env" ] && . "\$HOME/.config/haclaw/secrets.env"
set +a
# The bot's config dir is a separate identity; the human's sessions belong in ~/.claude.
unset CLAUDE_CONFIG_DIR
export PATH="/opt/homebrew/bin:\$HOME/.local/bin:\$PATH"
# No folder argument on purpose. Passing one pins every session to that directory —
# the picker still opens but the URL keeps snapping back, so you cannot actually work
# anywhere else. Without it code-server behaves like local VS Code: it restores the
# last workspace, File → Open Folder browses the whole filesystem, and ?folder=<path>
# opens a specific tree in its own tab.
exec ${CS} --disable-telemetry --disable-update-check
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
  <key>ProgramArguments</key><array><string>${WRAPPER}</string></array>
  <key>WorkingDirectory</key><string>${WORKDIR}</string>
  <key>EnvironmentVariables</key><dict>
    <key>HOME</key><string>${USER_HOME}</string>
    <key>PATH</key><string>/opt/homebrew/bin:${USER_HOME}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>LANG</key><string>en_US.UTF-8</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>${LOGDIR}/codeserver.log</string>
  <key>StandardErrorPath</key><string>${LOGDIR}/codeserver.err</string>
</dict></plist>
EOF
chown root:wheel "$PLIST"; chmod 644 "$PLIST"

echo "installing the Claude Code extension (as $USER_NAME)…"
sudo -u "$USER_NAME" env HOME="$USER_HOME" PATH="/opt/homebrew/bin:$PATH" \
    "$CS" --install-extension Anthropic.claude-code 2>&1 | tail -4 || \
    echo "  WARN: extension install failed — install it from the UI's Extensions pane"

launchctl bootout "system/${LABEL}" 2>/dev/null || true
launchctl enable "system/${LABEL}"
launchctl bootstrap system "$PLIST"
sleep 4
echo "code-server:"
launchctl print "system/${LABEL}" 2>/dev/null | grep -E '^\s+(state|pid) ' || echo "  (not loaded)"

# Re-lay the serve map: the editor takes /, the bare shell keeps /term.
TS=/opt/homebrew/bin/tailscale
$TS serve reset >/dev/null 2>&1 || true
$TS serve --bg --set-path=/ "http://127.0.0.1:${PORT}" >/dev/null 2>&1 || \
    $TS serve --bg "${PORT}" >/dev/null 2>&1 || true
$TS serve --bg --set-path=/term "http://127.0.0.1:7681" >/dev/null 2>&1 || true

echo
$TS serve status 2>&1 | head -10
