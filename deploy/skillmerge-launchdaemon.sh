#!/usr/bin/env bash
# Persistence for the skill-merge service on a HEADLESS macOS hub.
#     sudo ./skillmerge-launchdaemon.sh [USER]
#
# Same reasoning as macos-launchdaemon.sh: a LaunchAgent lives in gui/<uid> and
# this mini has nobody logged in (`uptime` reports 0 users), so only a system-domain
# LaunchDaemon actually runs. It still runs as the owning user, because the whole
# job — the claude CLI's auth, the git identity, the skill tree — is that user's.
#
# StartInterval rather than a cron-style calendar: conflicts appear whenever two
# machines happen to edit the same skill, not at 2am. A conflict that sits unmerged
# is a skill where one machine's work is invisible, so the useful cadence is minutes.
set -euo pipefail

LABEL="net.skillsynapse.merge"
PLIST="/Library/LaunchDaemons/${LABEL}.plist"
INTERVAL=300   # seconds

[ "$(uname -s)" = Darwin ] || { echo "ERROR: macOS only" >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo "ERROR: run with sudo" >&2; exit 1; }

USER_NAME="${1:-${SUDO_USER:-}}"
[ -n "$USER_NAME" ] || { echo "ERROR: no user — pass one: sudo $0 <user>" >&2; exit 1; }
USER_HOME="$(dscl . -read "/Users/$USER_NAME" NFSHomeDirectory | awk '{print $2}')"
[ -d "$USER_HOME" ] || { echo "ERROR: no home for $USER_NAME" >&2; exit 1; }

REPO="$USER_HOME/code/SkillSynapse"
PY="$REPO/.pixi/envs/default/bin/python"
LOGDIR="$USER_HOME/.claude/skillsynapse/logs"
[ -x "$PY" ] || { echo "ERROR: $PY missing — run 'pixi install' in $REPO first" >&2; exit 1; }

mkdir -p "$LOGDIR"; chown "$USER_NAME" "$LOGDIR"

# The daemon runs with a near-empty PATH; `claude` lives in /opt/homebrew/bin and
# llm_provider resolves it via shutil.which, so that directory has to be on PATH
# or every merge fails as "LLM subprocess not found".
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>${LABEL}</string>
  <key>UserName</key><string>${USER_NAME}</string>
  <key>GroupName</key><string>staff</string>
  <key>ProgramArguments</key><array>
    <string>${PY}</string>
    <string>-m</string><string>skillsynapse.merge_conflicts</string>
  </array>
  <key>WorkingDirectory</key><string>${REPO}</string>
  <key>EnvironmentVariables</key><dict>
    <key>HOME</key><string>${USER_HOME}</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>${INTERVAL}</integer>
  <key>StandardOutPath</key><string>${LOGDIR}/merge.log</string>
  <key>StandardErrorPath</key><string>${LOGDIR}/merge.err</string>
</dict></plist>
EOF
chown root:wheel "$PLIST"; chmod 644 "$PLIST"

launchctl bootout "system/${LABEL}" 2>/dev/null || true
launchctl enable "system/${LABEL}"
launchctl bootstrap system "$PLIST"

echo "installed $PLIST (every ${INTERVAL}s, as $USER_NAME)"
launchctl print "system/${LABEL}" | grep -E '^\s+(state|runs) ' || true
