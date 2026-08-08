#!/usr/bin/env bash
# The fleet's start page — one instance, every machine looks at it. Run with sudo:
#     sudo ./macos-homepage.sh [USER]
#
#     browser ──https──▶ tailscale serve ──┬─ /      ─▶ :8080  code-server
#                                          ├─ /home  ─▶ :3000  homepage  ← this
#                                          ├─ /st    ─▶ :8384  syncthing GUI
#                                          └─ /term  ─▶ :7681  ttyd
#
# Why a self-hosted page instead of a browser extension: an extension stores its data
# per-browser and then needs syncing, which hands the "is this side current?" question
# back to a human. One instance behind one URL has nowhere for that question to live.
set -euo pipefail

[ "$(uname -s)" = Darwin ] || { echo "ERROR: macOS only" >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo "ERROR: run with sudo" >&2; exit 1; }

USER_NAME="${1:-${SUDO_USER:-}}"
USER_HOME="$(dscl . -read "/Users/$USER_NAME" NFSHomeDirectory | awk '{print $2}')"

LABEL="net.skillsynapse.homepage"
PLIST="/Library/LaunchDaemons/${LABEL}.plist"
PORT=3000
APP="$USER_HOME/code/homepage"
WRAPPER="$USER_HOME/.local/bin/homepage-shell"
LOGDIR="$USER_HOME/.claude/skillsynapse/logs"
SECRETS="$USER_HOME/.config/haclaw/secrets.env"
HOSTNAME_TS="mac-mini.tail1a4a56.ts.net"

[ -d "$APP/.next" ] || { echo "ERROR: $APP not built — run pnpm build there first" >&2; exit 1; }

# Homepage builds with `output: standalone`, and next tells you outright that
# `next start` does not work with it. Standalone emits a self-contained server.js but
# deliberately does NOT copy the static assets next to it — that copy is the
# deployer's job, and skipping it yields a page that loads and renders unstyled,
# which reads like a CSS bug rather than a missing deploy step.
SA="$APP/.next/standalone"
[ -f "$SA/server.js" ] || { echo "ERROR: $SA/server.js missing — rebuild" >&2; exit 1; }
install -d -o "$USER_NAME" -g staff "$SA/.next"
rm -rf "$SA/.next/static" "$SA/public"
cp -R "$APP/.next/static" "$SA/.next/static"
[ -d "$APP/public" ] && cp -R "$APP/public" "$SA/public"
chown -R "$USER_NAME":staff "$SA"
echo "staged standalone assets"

# The syncthing widget needs an API key. It goes into secrets.env (600) and reaches
# the config as {{HOMEPAGE_VAR_SYNCTHING_KEY}} — the YAML itself is meant to live in
# claude-config, whose rule is that skill/config text carries placeholders only.
if ! grep -q '^HOMEPAGE_VAR_SYNCTHING_KEY=' "$SECRETS" 2>/dev/null; then
    KEY="$(sed -n 's|.*<apikey>\([^<]*\)</apikey>.*|\1|p' \
           "$USER_HOME/.local/share/syncthing/config.xml" | head -1)"
    if [ -n "$KEY" ]; then
        printf '\n# syncthing API key，供 homepage 的 widget 读取(2026-08-08)\nHOMEPAGE_VAR_SYNCTHING_KEY=%s\n' "$KEY" >> "$SECRETS"
        chown "$USER_NAME":staff "$SECRETS"; chmod 600 "$SECRETS"
        echo "added HOMEPAGE_VAR_SYNCTHING_KEY to secrets.env"
    else
        echo "WARN: could not read syncthing apikey — the widget will show an error"
    fi
else
    echo "HOMEPAGE_VAR_SYNCTHING_KEY already in secrets.env"
fi

install -d -o "$USER_NAME" -g staff "$USER_HOME/.local/bin" "$LOGDIR"

# HOMEPAGE_ALLOWED_HOSTS is not optional: middleware.js rejects any request whose Host
# it does not recognise, and behind `tailscale serve` the Host is the tailnet name, not
# localhost. Same failure shape as syncthing's 403 — the app is fine, the proxy is fine,
# and the page is blank.
cat > "$WRAPPER" <<EOF
#!/bin/zsh
set -a
[ -r "\$HOME/.config/haclaw/secrets.env" ] && . "\$HOME/.config/haclaw/secrets.env"
set +a
export PATH="/opt/homebrew/bin:\$HOME/.local/bin:\$PATH"
export PORT=${PORT}
export HOSTNAME=127.0.0.1
export HOMEPAGE_ALLOWED_HOSTS="${HOSTNAME_TS},100.111.54.114,127.0.0.1:${PORT},localhost:${PORT}"
# server.js runs with cwd = .next/standalone, so the config dir has to be named
# absolutely — otherwise it looks for ./config beside the bundle, finds nothing, and
# silently renders the built-in defaults instead of this fleet's services.
export HOMEPAGE_CONFIG_DIR="${APP}/config"
cd "${APP}/.next/standalone"
exec /opt/homebrew/bin/node server.js
EOF
chown "$USER_NAME":staff "$WRAPPER"; chmod 700 "$WRAPPER"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>${LABEL}</string>
  <key>UserName</key><string>${USER_NAME}</string>
  <key>GroupName</key><string>staff</string>
  <key>ProgramArguments</key><array><string>${WRAPPER}</string></array>
  <key>WorkingDirectory</key><string>${APP}</string>
  <key>EnvironmentVariables</key><dict>
    <key>HOME</key><string>${USER_HOME}</string>
    <key>PATH</key><string>/opt/homebrew/bin:${USER_HOME}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>NODE_ENV</key><string>production</string>
    <key>LANG</key><string>en_US.UTF-8</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>${LOGDIR}/homepage.log</string>
  <key>StandardErrorPath</key><string>${LOGDIR}/homepage.err</string>
</dict></plist>
EOF
chown root:wheel "$PLIST"; chmod 644 "$PLIST"

# bootout is asynchronous, and a node process holding :3000 does not release it the
# instant launchd is told to stop. Bootstrapping into that window makes launchd spawn
# something that cannot bind, then unwind the whole service and report
# `5: Input/output error` — a message that names neither the port nor the conflict.
# The same bug bit the syncthing daemon script earlier tonight; wait for the port to
# actually go quiet rather than assuming.
launchctl bootout "system/${LABEL}" 2>/dev/null || true
for _ in $(seq 1 30); do
    lsof -nP -iTCP:${PORT} -sTCP:LISTEN >/dev/null 2>&1 || break
    sleep 1
done
launchctl enable "system/${LABEL}"
if ! launchctl bootstrap system "$PLIST"; then
    echo "ERROR: bootstrap failed — who holds :${PORT}?" >&2
    lsof -nP -iTCP:${PORT} -sTCP:LISTEN >&2 2>/dev/null || echo "  (nobody)" >&2
    exit 1
fi
sleep 6
echo "homepage:"
launchctl print "system/${LABEL}" 2>/dev/null | grep -E '^\s+(state|pid) ' || echo "  (not loaded)"

TS=/opt/homebrew/bin/tailscale
$TS serve --bg --set-path=/home "http://127.0.0.1:${PORT}" >/dev/null 2>&1 || true
echo
$TS serve status 2>&1 | head -10
