"""首页底部那一行对话框的后端:收一句话,交给 agent,把回答送回去。

## 为什么是一个独立的小服务,而不是塞进 Homepage

Homepage 是个只读的仪表盘,它没有"执行"这个概念,也不该有。加一条能跑 agent 的路径
进去,等于把一个静态页面变成一个执行入口,而它的每一次升级都要重新对付这件事。

## 为什么挂在 :8443 的 `/ask` 下

页面在 `https://mac-mini…:8443/`。后端换个端口就跨域了,于是要么开 CORS、要么加代理 ——
**而挂在同一个端口的另一条路径下,这个问题根本不存在。** `tailscale serve` 按最长前缀
匹配,`/ask` 归这里,其余归 Homepage。

## 权限:给读和 Bash,但只认 POST

这条路径**没有口令**,和同一 tailnet 上那个无密码的 code-server 一样 —— 那个给的是
完整 shell,所以这里给 agent 工具并没有扩大攻击面,它本来就在。

但**只接受 POST**:一个 GET 就能触发的执行入口,会被浏览器预取、被聊天软件的链接
预览、被任何一个爬 tailnet 的东西无意中打中。那不是权限问题,是**误触**问题,而误触
比攻击常见得多。

## 一次只跑一个

agent 一次要跑几十秒。不限并发的话,页面上手快点几下就是几十个 `claude` 进程同时在
这台机器上跑。排队而不是并发 —— 这是个对话框,不是任务队列。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 7788
SECRETS = Path.home() / ".config/haclaw/secrets.env"
MODEL = "claude-sonnet-5"
TOOLS = "Read,Glob,Grep,Bash,WebSearch,WebFetch"
TIMEOUT = 240
MAX_PROMPT = 4000

# 回答落在一条几十像素高的带子里,不是聊天窗。不给这个约束的话,一句"uptime 是多少"
# 会换回三段话外加一个反问 —— 内容没错,但**形状不对**,而形状决定了这个东西好不好用。
_PREAMBLE = (
    "你在回答一个仪表盘底部单行输入框里的问题。答案会显示在一条很窄的带子里。\n"
    "规则:直接给结果,不要铺垫,不要反问,不要罗列可选项。最多三句话。\n"
    "需要查证的就用工具查,你就跑在 mac-mini 这台机器上(不要试图 ssh 到它)。\n"
    "这台机器是什么、跑着什么服务、有哪些坑,见 ~/CLAUDE.md(已自动加载)。\n\n"
)

_one_at_a_time = threading.Semaphore(1)

HOMEPAGE_CONFIG = Path.home() / "code/homepage/config"
_YAML_ENTRY_RE = None                                   # 见 _page_snapshot


def _page_snapshot() -> str:
    """首页上现在有什么 —— 只要名字,每次请求现读。

    **不写死在 CLAUDE.md 里**:书签是每 5 分钟自动生成的,写死的那一份第二天就在撒谎,
    而它撒谎的方式是"看起来很确定"。这里现读,永远是当下的。

    只取名字不取细节:一句"XX 在不在首页上"靠名字就能答,**不用调工具**;真要细节,
    CLAUDE.md 里写了这几个文件在哪,agent 自己去读。广度放上下文里,深度留给工具。
    """
    import re
    entry = re.compile(r"^\s*- ([^:#][^:]*):\s*$")
    out = []
    for fname, label in (("services.yaml", "服务"), ("bookmarks.yaml", "书签")):
        p = HOMEPAGE_CONFIG / fname
        try:
            names = [m.group(1).strip() for m in
                     (entry.match(ln) for ln in p.read_text(encoding="utf-8").splitlines())
                     if m]
        except OSError:
            continue
        if names:
            out.append(f"首页{label}: " + " / ".join(names))
    return "\n".join(out)


def _token() -> str | None:
    """只取这一个键 —— secrets.env 里还有邮箱密码和 bot token。"""
    try:
        for line in SECRETS.read_text(encoding="utf-8").splitlines():
            if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def ask(prompt: str) -> tuple[bool, str]:
    env = dict(os.environ)
    if "CLAUDE_CODE_OAUTH_TOKEN" not in env:
        tok = _token()
        if tok:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    try:
        r = subprocess.run(
            ["claude", "--print", "--model", MODEL, "--allowedTools", TOOLS],
            input=_PREAMBLE + _page_snapshot() + "\n\n问题:" + prompt,
            capture_output=True, text=True, encoding="utf-8",
            timeout=TIMEOUT, env=env, cwd=str(Path.home()))
    except subprocess.TimeoutExpired:
        return False, f"超时({TIMEOUT}s)——这一句可能太重了,换个小一点的问法"
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"起不来: {e}"
    if r.returncode != 0:
        # "Not logged in" 走 stdout 不走 stderr,只读 stderr 会看到一片空白。
        tail = ((r.stderr or "")[-400:] or (r.stdout or "")[-400:]).strip()
        return False, f"exit {r.returncode}: {tail}"
    return True, (r.stdout or "").strip() or "(空回答)"


class Handler(BaseHTTPRequestHandler):
    server_version = "ask/1.0"

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                                   # noqa: N802
        # 健康检查而已。**执行只走 POST** —— 见模块顶部。
        self._json(200, {"ok": True, "hint": "POST {\"prompt\": \"...\"}"})

    def do_POST(self):                                  # noqa: N802
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if not 0 < n <= MAX_PROMPT * 4:
            return self._json(400, {"error": "请求体为空或过大"})
        try:
            payload = json.loads(self.rfile.read(n).decode("utf-8"))
            prompt = str(payload.get("prompt", "")).strip()
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "请求体不是合法 JSON"})
        if not prompt:
            return self._json(400, {"error": "没有内容"})
        if len(prompt) > MAX_PROMPT:
            prompt = prompt[:MAX_PROMPT]

        if not _one_at_a_time.acquire(timeout=1):
            # 排队会让页面看起来卡住,而它其实在等别人。直说。
            return self._json(429, {"error": "上一句还在跑,等它结束再问"})
        try:
            ok, text = ask(prompt)
        finally:
            _one_at_a_time.release()
        return self._json(200 if ok else 500,
                          {"text": text} if ok else {"error": text})

    def log_message(self, fmt, *args):                  # noqa: A003
        print(f"{self.address_string()} {fmt % args}", flush=True)


def main() -> int:
    # 只绑回环:外面进来的一律经过 tailscale serve,那一层已经做了 tailnet 鉴别。
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"ask 服务在 127.0.0.1:{PORT}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
