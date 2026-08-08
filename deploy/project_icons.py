"""给每个项目配一张一眼能认出来的图。

一列纯字母缩写的方块认起来很慢:你其实是靠"那个蓝绿色的""那个有截图的"在扫这一页,
而不是靠读两个大写字母。`SS` / `SM` / `HC` 三个方块并排,读完才能区分,那就已经慢了。

三级来源,越靠前信息量越大:

  1. **库里的图** —— 项目自己画过/截过的东西,没有比这更代表它的了。
     不只看 README 的第一张:很多库的图在 docs/ 或 assets/ 底下,README 里根本没引。
  2. **agent 看一眼这个库,给一个 emoji + 色相** —— 🎯 比 `PD` 快得多,因为它不需要
     先回忆"PD 是哪个项目"。
  3. **名字哈希出来的渐变 + 缩写** —— agent 不可用时的兜底。关键是**确定性**:
     同一个名字永远同一张图,否则每次刷新颜色都在变,视觉记忆就白建立了。

## 为什么可以调 agent,以及它凭什么不会烧钱

这个模块之前的注释里写着"刻意不调模型生成图片:那要为每个新目录付一次生成成本"。
那句话的前提是**每次刷新都要付** —— 而 bookmarks 是 5 分钟跑一次的,那确实不能忍。

真正的成本单位不是"每次刷新",是"每个新项目一次"。加一层缓存,这个前提就没了:
建一个新目录,agent 看它一次,之后永远读缓存。一年新建二十个项目,就是二十次。

所以贵的从来不是 agent,是**没有缓存的** agent。

## 缓存失效:成功的条目永不重算

`kind` 是 `image` 或 `agent` 的条目**永久有效**,不会因为 README 改了就重挑 —— 图标的
价值在于稳定,今天是🎯明天是🔫,视觉记忆一样白建立。

只有 `fallback`(agent 那次没成)会重试,最多 3 次,和 merge 那条回路的重试上限一致。
想强制重挑,`--rescan` 或者删掉 icons.json 里那一行。
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import struct
import subprocess
from pathlib import Path
from typing import Optional

CACHE_PATH = Path.home() / ".claude/skillsynapse/icons.json"
SECRETS = Path.home() / ".config/haclaw/secrets.env"

# 每次刷新最多为几个新项目调 agent。一次 `git clone` 十个库不应该在同一分钟里
# 扇出十个请求 —— 剩下的下一轮(5 分钟后)接着补,反正只补一次。
MAX_NEW_PER_RUN = 4
MAX_ATTEMPTS = 3

_IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
_README_NAMES = ("README.md", "readme.md", "README.MD", "Readme.md", "README.rst")

# ![alt](path)  以及  <img src="path">
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)")
_HTML_IMG_RE = re.compile(r"""<img[^>]+src\s*=\s*["']([^"']+)""", re.IGNORECASE)

# 徽章不是项目的脸。一排 build badge 是 README 里最常见的第一张图,也是最没用的一张:
# 每个项目的都长一样。
_BADGE_HINTS = ("shields.io", "badge", "travis-ci", "codecov", "img.shields",
                "circleci", "appveyor", "coveralls")

_SKIP_DIRS = {"node_modules", ".git", ".next", ".pixi", ".venv", "venv", "__pycache__",
              "dist", "build", "target", "site-packages", ".github", "vendor"}

# 文件名里的线索。分数只用来排序,不做阈值 —— 有图总比没图强,哪怕它叫 fig3.png。
_NAME_SCORES = (
    (("logo",), 10), (("banner", "hero", "cover", "teaser"), 9), (("icon",), 8),
    (("screenshot", "screen", "demo", "preview"), 7),
    (("overview", "arch", "architecture", "pipeline", "diagram"), 6),
    (("result", "example", "sample", "output"), 4),
)
_DIR_BONUS = {"docs": 3, "doc": 3, "assets": 3, "images": 3, "img": 3,
              "media": 3, "static": 2, "figures": 2, "fig": 2}

# 200KB 上限:超过这个尺寸的图内联进 YAML 会让每次页面加载都背上它,
# 而它换来的只是一个 128px 的方块。
_MAX_BYTES = 200_000

# 图标格子是**方的**。一张 2048×512 的横幅塞进去只剩一条缝,而那条缝里的字比
# 缩写还难认 —— 它作为 README 顶部的门面是最好的一张,作为图标是最差的一张。
# 这两件事没有关系,所以形状要单独判一次,不能只看"有没有图"。
_RATIO_MAX = 2.2
_RATIO_MIN = 1 / _RATIO_MAX


# ── 一级:库里的图 ────────────────────────────────────────────────────


def _readme_refs(project: Path) -> tuple[list[str], str]:
    """README 里引用的图片路径(按出现顺序),以及 README 全文。"""
    for name in _README_NAMES:
        readme = project / name
        if not readme.is_file():
            continue
        try:
            text = readme.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        refs = [m.group(1).strip() for m in _MD_IMG_RE.finditer(text)]
        refs += [m.group(1).strip() for m in _HTML_IMG_RE.finditer(text)]
        return [r for r in refs if r and not _is_badge(r)], text
    return [], ""


def _is_badge(ref: str) -> bool:
    low = ref.lower()
    return any(b in low for b in _BADGE_HINTS)


def _dims(raw: bytes, suffix: str) -> Optional[tuple[int, int]]:
    """(宽, 高),读不出来返回 None。手拆文件头而不是拉 Pillow —— 这个 daemon 跑在
    系统 python3 上,为了两个整数引入一个原生依赖不划算。
    """
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", raw[16:24])
        if raw[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", raw[6:10])
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            if raw[12:16] == b"VP8X":
                w = int.from_bytes(raw[24:27], "little") + 1
                h = int.from_bytes(raw[27:30], "little") + 1
                return w, h
            if raw[12:16] == b"VP8 ":
                return (struct.unpack("<H", raw[26:28])[0] & 0x3FFF,
                        struct.unpack("<H", raw[28:30])[0] & 0x3FFF)
            return None
        if raw[:2] == b"\xff\xd8":                      # JPEG:扫到 SOF 段
            i = 2
            while i + 9 < len(raw):
                if raw[i] != 0xFF:
                    i += 1
                    continue
                marker = raw[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg = struct.unpack(">H", raw[i + 2:i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                    return w, h
                i += 2 + seg
    except (struct.error, IndexError, ValueError):
        pass
    return None


def _shape_ok(raw: bytes, path: Path) -> bool:
    if path.suffix.lower() == ".svg":
        return True                                     # 矢量,怎么缩都不糊
    d = _dims(raw, path.suffix.lower())
    if d is None or d[1] == 0:
        return True                                     # 读不出来就不拦
    return _RATIO_MIN <= d[0] / d[1] <= _RATIO_MAX


def _score(path: Path, project: Path, readme_refs: set[str]) -> tuple[int, int]:
    """返回 (理由, 总分)。

    **两个数是两回事,不能合成一个。** 总分决定"哪张最好",理由决定"到底该不该用图"。

    一个只有总分的排名永远排得出第一名 —— 哪怕候选全是 `docs/` 底下十几张随机截图,
    分数一模一样,第一名由 `os.walk` 的顺序决定。那张图缩到 48px 是一团糊,而它占掉了
    本该属于 emoji 的位置。**"有图"不是用图的理由,"这张图是门面"才是。**
    """
    rel = path.relative_to(project).as_posix().lower()
    reason = 0
    for keys, pts in _NAME_SCORES:
        if any(k in path.stem.lower() for k in keys):
            reason = max(reason, pts)
    # README 引过的图,是项目自己挑过的封面 —— 压过任何文件名启发式。
    if rel in readme_refs or path.name.lower() in readme_refs:
        reason += 12

    total = reason
    for part in path.relative_to(project).parts[:-1]:
        total += _DIR_BONUS.get(part.lower(), 0)
    # 越浅越可能是门面图,越深越可能是某个子模块的插图。
    total -= len(path.relative_to(project).parts) - 1
    return reason, total


def _walk_images(project: Path, limit: int = 400) -> list[Path]:
    out: list[Path] = []
    for root, dirs, files in os.walk(project):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if Path(f).suffix.lower() in _IMG_EXT:
                out.append(Path(root) / f)
                if len(out) >= limit:
                    return out
    return out


def repo_image(project: Path) -> Optional[tuple[str, str]]:
    """全库找一张最能代表这个项目的图。返回 (data_uri, 说明) 或 None。"""
    refs, _ = _readme_refs(project)
    # 远程图直接交给浏览器 —— 它是 README 的第一张,项目自己选的。
    for r in refs:
        if r.startswith(("http://", "https://")):
            return r, f"README 外链 {r[:48]}"
    ref_set = {r.lstrip("./").lower() for r in refs}
    ref_set |= {Path(r).name.lower() for r in refs}

    ranked: list[tuple[int, Path]] = []
    for p in _walk_images(project):
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if not 0 < size <= _MAX_BYTES:
            continue
        reason, total = _score(p, project, ref_set)
        if reason <= 0:                # 说不出为什么是它,就不是它
            continue
        ranked.append((total, p))
    ranked.sort(key=lambda t: -t[0])

    # 排第一的不合格就往下走,而不是直接放弃 —— 最典型的情况正是"最高分的是横幅"。
    for _, path in ranked:
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if not _shape_ok(raw, path):
            continue
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        return _data_uri(mime, raw), f"库内图片 {path.relative_to(project).as_posix()}"
    return None


# ── 二级:agent 看一眼 ────────────────────────────────────────────────


def _oauth_token() -> Optional[str]:
    """只取这一个键,不 source 整个 secrets.env —— 那里面还有邮箱密码和 bot token。"""
    try:
        for line in SECRETS.read_text(encoding="utf-8").splitlines():
            if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _project_brief(project: Path, budget: int = 1800) -> str:
    """给 agent 的上下文。**刻意不给它工具** —— 一个 daemon 里跑的、带文件系统权限的
    agent,要么需要交互式授权(它没有 tty),要么就得开 --dangerously-skip-permissions。
    把料在这里备好,它只需要读一段文本,成本和失败模式都是确定的。
    """
    parts = [f"项目名: {project.name}"]
    try:
        top = sorted(p.name + ("/" if p.is_dir() else "")
                     for p in project.iterdir()
                     if not p.name.startswith(".") and p.name not in _SKIP_DIRS)
        parts.append("顶层内容: " + ", ".join(top[:40]))
    except OSError:
        pass
    _, readme = _readme_refs(project)
    if readme:
        body = re.sub(r"^\s*[-*+]\s*!\[.*$", "", readme, flags=re.M)  # 徽章行
        parts.append("README 开头:\n" + body[:budget])
    else:
        # 没有 README 的项目,名字和目录树就是全部线索了。
        try:
            names = [p.name for p in project.rglob("*")
                     if p.is_file() and not any(s in p.parts for s in _SKIP_DIRS)][:60]
            parts.append("部分文件: " + ", ".join(names))
        except OSError:
            pass
    return "\n".join(parts)


_PROMPT = """下面是一个代码项目的概况。给它挑一个图标。

只输出一行 JSON,不要解释、不要代码块围栏:
{{"emoji":"<恰好一个 emoji>","hue":<0-359 的整数>,"why":"<不超过 12 个字>"}}

emoji 要让人一眼想起这个项目在**做什么**。避开 📁📦🔧⚙️🚀 这类对任何项目都成立的
通用图标 —— 那等于没挑。hue 是 HSL 色相,用来和别的项目区分开。

{brief}
"""

_JSON_RE = re.compile(r"\{.*?\}", re.S)


def agent_pick(project: Path, timeout: int = 90) -> Optional[dict]:
    """问 agent 要一个 emoji + 色相。失败返回 None,由调用方降级。"""
    return _ask(_project_brief(project), fallback_name=project.name, timeout=timeout)


def agent_pick_label(name: str, description: str, timeout: int = 90) -> Optional[dict]:
    """给不在本机上的项目挑图标 —— 只有名字和一句话,没有代码可读。

    信息比扫库少得多,但**图标不需要理解一个项目,只需要认出它**,而名字加一句话
    通常足够挑出一个不会认错的 emoji。
    """
    brief = f"项目名: {name}\n一句话说明: {description}\n(这个项目不在本机,没有代码可读)"
    return _ask(brief, fallback_name=name, timeout=timeout)


def _ask(brief: str, fallback_name: str, timeout: int) -> Optional[dict]:
    env = dict(os.environ)
    if "CLAUDE_CODE_OAUTH_TOKEN" not in env:
        tok = _oauth_token()
        if tok:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    prompt = _PROMPT.format(brief=brief)
    try:
        r = subprocess.run(
            ["claude", "--print", "--model", "claude-haiku-4-5-20251001", prompt],
            capture_output=True, text=True, encoding="utf-8",
            timeout=timeout, env=env, cwd=str(Path.home()))
    except (OSError, subprocess.SubprocessError) as e:
        print(f"    agent 调用失败: {e}")
        return None
    if r.returncode != 0:
        # "Not logged in" 走的是 stdout,不是 stderr —— 只读 stderr 会看到一片空白,
        # 然后以为是别的问题。
        tail = ((r.stderr or "")[-300:] or (r.stdout or "")[-300:]).strip()
        print(f"    agent exit {r.returncode}: {tail}")
        return None
    m = _JSON_RE.search(r.stdout or "")
    if not m:
        print(f"    agent 没给出 JSON: {(r.stdout or '')[:120]!r}")
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    emoji = str(d.get("emoji", "")).strip()
    if not emoji:
        return None
    hue = d.get("hue")
    hue = int(hue) % 360 if isinstance(hue, (int, float)) else _hue_pair(fallback_name)[0]
    return {"emoji": emoji, "hue": hue, "why": str(d.get("why", ""))[:24]}


# ── 三级:名字哈希 ────────────────────────────────────────────────────


def _hue_pair(name: str) -> tuple[int, int]:
    """Two hues from the name's digest. Deterministic — same name, same colours."""
    h = hashlib.sha256(name.encode("utf-8")).digest()
    base = h[0] * 360 // 256
    # +50° keeps the pair related rather than clashing, and never lands on grey.
    return base, (base + 50) % 360


def _data_uri(mime: str, raw: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def tile(name: str, glyph: Optional[str] = None, hue: Optional[int] = None) -> str:
    """一块渐变方砖,中间放 glyph(emoji)或名字缩写。内联成 data URI。

    内联而不是写到某个被服务的路径下,是故意的。`config/icons/` 是 Docker 镜像的约定,
    源码部署下 404;standalone 的 `public/icons/` 同样 404。与其继续猜这个 build 到底
    服务哪个目录,不如把字节放进 YAML —— **一个不需要路由的图标,不会被路由弄坏。**
    每张几百字节。
    """
    if hue is None:
        h1, h2 = _hue_pair(name)
    else:
        h1, h2 = hue % 360, (hue + 50) % 360
    if glyph:
        # emoji 用系统彩色字体渲染。字号比缩写小一点:emoji 的字面框比字母满,
        # 同样字号会顶到圆角上。
        inner = (f'<text x="64" y="68" font-size="64" text-anchor="middle" '
                 f'dominant-baseline="central" font-family="Apple Color Emoji,'
                 f'Segoe UI Emoji,Noto Color Emoji,sans-serif">{_xml_escape(glyph)}</text>')
    else:
        initials = "".join(p[:1] for p in re.split(r"[-_. ]+", name) if p)[:2].upper() \
            or name[:2].upper()
        inner = (f'<text x="64" y="64" fill="#fff" fill-opacity="0.92" font-size="52" '
                 f'font-family="ui-monospace,SFMono-Regular,Menlo,monospace" '
                 f'font-weight="600" text-anchor="middle" '
                 f'dominant-baseline="central">{_xml_escape(initials)}</text>')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">'
           f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
           f'<stop offset="0%" stop-color="hsl({h1},68%,52%)"/>'
           f'<stop offset="100%" stop-color="hsl({h2},62%,34%)"/>'
           f'</linearGradient></defs>'
           f'<rect width="128" height="128" rx="26" fill="url(#g)"/>{inner}</svg>')
    return _data_uri("image/svg+xml", svg.encode("utf-8"))


# ── 缓存 ─────────────────────────────────────────────────────────────


class IconCache:
    def __init__(self, path: Path = CACHE_PATH):
        self.path = path
        self.data: dict = {}
        # 删除是一个**意图**,不是"我的 dict 里没有它"。合并回盘上内容时,前者能表达
        # "这一条请去掉",后者会被盘上那份原样带回来 —— forget() 就白做了。
        self.dropped: set[str] = set()
        self.dirty = False
        self.spent = 0
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        """落盘,并把盘上已有的条目并回来。

        并回来是因为**这个文件记的是已经花掉的钱**。整个 dict 覆盖式写出去,等于宣称
        "我这一轮看到的就是全部" —— 而另一个进程刚为一个我没扫到的项目付过一次
        agent 调用,它的记录会被我抹掉,下一轮再付一次。
        """
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            on_disk = {}
        for k in self.dropped:
            on_disk.pop(k, None)
        on_disk.update(self.data)      # 本轮算出来的更新,盘上多出来的保留
        self.data = on_disk
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.path)
        self.dirty = False

    def forget(self, key: Optional[str] = None) -> None:
        if key is None:
            self.dropped |= set(self.data)
            try:                       # 盘上可能还有本进程没加载过的条目
                self.dropped |= set(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
            self.data = {}
        else:
            self.dropped.add(key)
            self.data.pop(key, None)
        self.dirty = True

    def icon_for_label(self, name: str, description: str) -> Optional[str]:
        """给手写条目挑图标。键用 `manual:<名字>` —— 它没有本机路径可当键。

        额度用尽时返回 None 而不是缩写砖:手写条目本来就带 `abbr`,Homepage 会自己显示
        它。塞一块砖进去只是把"还没挑"伪装成"挑好了",下一轮就不会再来补。
        """
        key = f"manual:{name}"
        hit = self.data.get(key)
        if hit and (hit.get("kind") == "agent"
                    or hit.get("attempts", 0) >= MAX_ATTEMPTS):
            return hit.get("uri")
        if self.spent >= MAX_NEW_PER_RUN:
            return None
        self.spent += 1
        pick = agent_pick_label(name, description)
        if pick:
            rec = {"uri": tile(name, pick["emoji"], pick["hue"]), "kind": "agent",
                   "source": f'{pick["emoji"]} {pick["why"]}'.strip()}
            print(f'  {name}(手写): {pick["emoji"]}  {pick["why"]}')
        else:
            rec = {"uri": tile(name), "kind": "fallback", "source": "缩写渐变",
                   "attempts": (hit or {}).get("attempts", 0) + 1}
        self.data[key] = rec
        self.dropped.discard(key)
        self.dirty = True
        self.save()
        return rec["uri"]

    def icon_for(self, project: Path) -> str:
        key = str(project.resolve())
        hit = self.data.get(key)
        # 成功的条目永不重算 —— 图标的价值在稳定,不在最新。
        if hit and (hit.get("kind") in ("image", "agent")
                    or hit.get("attempts", 0) >= MAX_ATTEMPTS):
            return hit["uri"]

        attempts = (hit or {}).get("attempts", 0)

        found = repo_image(project)
        if found:
            uri, src = found
            rec = {"uri": uri, "kind": "image", "source": src}
            print(f"  {project.name}: {src}")
        elif self.spent < MAX_NEW_PER_RUN:
            self.spent += 1
            pick = agent_pick(project)
            if pick:
                rec = {"uri": tile(project.name, pick["emoji"], pick["hue"]),
                       "kind": "agent",
                       "source": f'{pick["emoji"]} {pick["why"]}'.strip()}
                print(f'  {project.name}: {pick["emoji"]}  {pick["why"]}')
            else:
                rec = {"uri": tile(project.name), "kind": "fallback",
                       "source": "缩写渐变", "attempts": attempts + 1}
                print(f"  {project.name}: 降级为缩写(第 {attempts + 1} 次)")
        else:
            # 这一轮的额度用完了。写成 fallback 而不是不写:页面立刻有图可用,
            # 下一轮(5 分钟后)再来补。attempts 不加 —— 没试过,不算失败。
            rec = {"uri": tile(project.name), "kind": "fallback",
                   "source": "本轮额度用尽", "attempts": attempts}

        self.data[key] = rec
        self.dropped.discard(key)
        self.dirty = True
        # 每算出一条就落一条,不攒到最后。一轮要跑一分多钟,中间挂掉、被 launchd 杀掉、
        # 或者下一个项目把 agent 调爆,都不该让**已经付过的**那几次白付。
        self.save()
        return rec["uri"]


# 兼容旧签名:refresh_bookmarks 之前按 icon_for(path, icons_dir) 调。
def icon_for(project: Path, icons_dir: Optional[Path] = None,
             cache: Optional[IconCache] = None) -> str:
    return (cache or IconCache()).icon_for(project)
