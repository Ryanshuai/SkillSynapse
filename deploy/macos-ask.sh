#!/usr/bin/env bash
# 首页底部那一行问答条的后端。用 sudo 跑:
#     sudo ./macos-ask.sh [USER]
#
#     浏览器 ──https──▶ tailscale serve :8443 ──┬─ /      ─▶ :3000  homepage
#                                               └─ /ask   ─▶ :7788  这个
#
# 挂在同一个端口的另一条路径下,而不是换端口:换端口就跨域,于是要么开 CORS 要么加
# 代理。**同源之后这个问题根本不存在。**
set -euo pipefail

[ "$(uname -s)" = Darwin ] || { echo "ERROR: macOS only" >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo "ERROR: run with sudo" >&2; exit 1; }

USER_NAME="${1:-${SUDO_USER:-}}"
USER_HOME="$(dscl . -read "/Users/$USER_NAME" NFSHomeDirectory | awk '{print $2}')"

LABEL="net.skillsynapse.ask"
PLIST="/Library/LaunchDaemons/${LABEL}.plist"
PORT=7788
SCRIPT="$USER_HOME/code/SkillSynapse/deploy/ask_server.py"
LOGDIR="$USER_HOME/.claude/skillsynapse/logs"

[ -f "$SCRIPT" ] || { echo "ERROR: $SCRIPT 不在" >&2; exit 1; }
install -d -o "$USER_NAME" -g staff "$LOGDIR"

# 凭据不进 plist —— /Library/LaunchDaemons/*.plist 是 644,全机可读。服务自己在运行时
# 从 secrets.env(600)里只取它要的那一个键。
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>${LABEL}</string>
  <key>UserName</key><string>${USER_NAME}</string>
  <key>GroupName</key><string>staff</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string>
    <string>${SCRIPT}</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>HOME</key><string>${USER_HOME}</string>
    <key>PATH</key><string>/opt/homebrew/bin:${USER_HOME}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>LANG</key><string>en_US.UTF-8</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>${LOGDIR}/ask.log</string>
  <key>StandardErrorPath</key><string>${LOGDIR}/ask.err</string>
</dict></plist>
PLIST_EOF
chmod 644 "$PLIST"

launchctl bootout "system/${LABEL}" 2>/dev/null || true

# ⚠ 端口要等它真的放开。上一个进程还占着 :7788 时 bootstrap 会起一个绑不上的进程,
# launchd 判定失败并**整体回滚**,报出来的是 "Bootstrap failed: 5: Input/output error"
# —— 一个和端口毫无关系的错误。这个坑在 syncthing 和 homepage 上各踩过一次。
for _ in $(seq 1 20); do
    lsof -nP -iTCP:${PORT} -sTCP:LISTEN >/dev/null 2>&1 || break
    sleep 0.5
done
pkill -f "ask_server.py" 2>/dev/null || true
sleep 1

# enable 必须在 bootstrap **之前**:set -e 之下,bootstrap 失败会让后面的 enable 永远
# 跑不到,于是服务停在 disabled 上,而错误信息说的是别的事。
launchctl enable "system/${LABEL}"
launchctl bootstrap system "$PLIST"

sleep 2
if curl -sf -o /dev/null "http://127.0.0.1:${PORT}/"; then
    echo "ask 服务已起 (:${PORT})"
else
    echo "WARN: :${PORT} 没应答,看 ${LOGDIR}/ask.err" >&2
fi

# tailscale serve:把 /ask 挂到 :8443,和 homepage 同源。
sudo -u "$USER_NAME" true 2>/dev/null || true
tailscale serve --bg --https 8443 --set-path /ask "http://127.0.0.1:${PORT}" \
    && echo "已挂到 https://mac-mini.tail1a4a56.ts.net:8443/ask"
tailscale serve status
