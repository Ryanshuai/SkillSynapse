#!/usr/bin/env bash
# Install the two skill-merge daemons on a headless macOS hub. Run once, with sudo:
#     sudo ./macos-merge-daemons.sh [USER]
#
# Two daemons, not one, because detection and arbitration want opposite cadences:
#
#   net.skillsynapse.merge.scan   every 5 min   --scan-only, never calls the model
#   net.skillsynapse.merge        on the hour   merges conflicts with an agent
#
# Merging one 28KB SKILL.md takes minutes (300s was not enough for agent-refactor),
# so chasing conflicts on a five-minute clock means paying repeatedly for work that
# cannot finish in a beat. Leaving a conflict file on disk costs nothing — it is a
# stable state, and the hourly pass takes however many have accumulated.
#
# The scan beat is not idle work: it commits whatever syncthing brought in, and
# HEAD is the baseline the silent-overwrite detector measures against. Skip a beat
# there and the next overwrite becomes unmeasurable.
#
# StartCalendarInterval rather than StartInterval=3600 for the merge pass: the
# latter counts from whenever launchd loaded the job, so it drifts across every
# reboot and stops lining up with anything you can read in a log.
set -euo pipefail

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

# claude lives in one of these depending on how it was installed; the merge pass is
# useless without it on PATH, and llm_provider's shutil.which is what resolves it.
PATH_VAL="$USER_HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Create the whole chain owned by the user, not just the leaf. Running this script
# under sudo makes every mkdir root-owned by default, and the daemon then runs as
# $USER_NAME inside a directory it cannot write to. That failure is lopsided and
# therefore confusing: logs/ happened to get created by the daemon itself (so writing
# logs worked fine), while the lock file one level up could not be created at all.
install -d -o "$USER_NAME" -g staff "$(dirname "$LOGDIR")"
install -d -o "$USER_NAME" -g staff "$LOGDIR"
chown -R "$USER_NAME":staff "$(dirname "$LOGDIR")"

write_plist() {
    local label="$1" schedule="$2" extra_arg="$3" logbase="$4"
    local plist="/Library/LaunchDaemons/${label}.plist"
    local arg_xml=""
    [ -n "$extra_arg" ] && arg_xml="    <string>${extra_arg}</string>"

    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>${label}</string>
  <key>UserName</key><string>${USER_NAME}</string>
  <key>GroupName</key><string>staff</string>
  <key>ProgramArguments</key><array>
    <string>${PY}</string>
    <string>-m</string><string>skillsynapse.merge_conflicts</string>
${arg_xml}
  </array>
  <key>WorkingDirectory</key><string>${REPO}</string>
  <key>EnvironmentVariables</key><dict>
    <key>HOME</key><string>${USER_HOME}</string>
    <key>PATH</key><string>${PATH_VAL}</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>PYTHONIOENCODING</key><string>utf-8</string>
  </dict>
${schedule}
  <key>StandardOutPath</key><string>${LOGDIR}/${logbase}.log</string>
  <key>StandardErrorPath</key><string>${LOGDIR}/${logbase}.err</string>
</dict></plist>
EOF
    chown root:wheel "$plist"; chmod 644 "$plist"

    launchctl bootout "system/${label}" 2>/dev/null || true
    launchctl enable "system/${label}"
    launchctl bootstrap system "$plist"
    echo "installed ${label}"
}

write_plist "net.skillsynapse.merge.scan" \
    "  <key>StartInterval</key><integer>300</integer>" \
    "--scan-only" "scan"

# No RunAtLoad on the merge pass: bootstrapping it should not fire an agent run as
# a side effect of installing the daemon. It waits for the hour like it will every
# other time.
write_plist "net.skillsynapse.merge" \
    "  <key>StartCalendarInterval</key><dict><key>Minute</key><integer>0</integer></dict>" \
    "" "merge"

echo
for l in net.skillsynapse.merge.scan net.skillsynapse.merge; do
    printf '%-32s ' "$l"
    launchctl print "system/$l" 2>/dev/null | grep -E '^\s+state ' | head -1 || echo "(not loaded)"
done
