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

## 缓存里存的是**决定**,不是那张图

`{"kind":"agent","emoji":"🎯","hue":12}` —— 而不是渲染好的 SVG。

花钱买来的是"这个项目该配 🎯"这个判断;把它画成 128×128 的方砖是免费的,而且可以重画
任意多次。两者存在一起时,任何一次渲染方式的改动(尺寸、配色、从 data URI 换成文件)
都会连坐地作废缓存,于是为了改一个像素重新付一遍钱。

**分开存之后,渲染层可以随便推倒重来。** 下面那次 data URI → 静态文件的搬迁,一次
agent 调用都没有重付。

## 图必须落成文件,不能内联 —— 这条是撞出来的

Homepage 的 `ResolvedIcon` 只认三种前缀:`http`、`/`、以及 `mdi-`/`si-`/`sh-`。
`data:` 一种都不是,于是它一路掉到最后一个兜底分支,把整条 658 字符的 data URI
**当成图标名**拼进 jsdelivr 的 URL 去请求:

    https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/data:image/svg+xml;base64,PHN2...png

404,一排破图。189KB 的那张连破图都没有 —— URL 太长,请求根本发不出去。

⚠ 而这个模块之前的注释振振有词地写着"内联成 data URI,一个不需要路由的图标不会被
路由弄坏"。**那句话的前提(有 data: 这条路)从来不成立**,只是当时没人去读那个组件。
它是自洽的、听起来很有道理的、而且瞎的。

## 文件必须在服务启动**之前**就在,否则 404

`next-server` 在启动时把 `public/` 扫成一张表,之后新增的文件不在表里,永远 404 ——
**目录选对了也没用,写晚了就是没有。** 当初"public/icons 也 404"的结论就是这么来的:
路径是对的,时机是错的,而两种失败长得一模一样。

所以新增一个图标文件之后要踢一下服务。

⚠ 而"要不要踢"这个判断,第一版问的是**"这个文件刚才存在吗"** —— 又错了一次,原因和
上面那条一模一样:文件由上一次运行建好之后,对这个进程来说不新,对**服务**来说是新的。
于是文件在正确的位置上,URL 一直 404,而日志里一句异常都没有。

现在直接量那件事本身:`ensure_served()` 把这一轮要引的路径挨个取一遍,取不到才踢。
**判据要能看见它要管的那个维度** —— "文件在不在"和"服务认不认"是两个量。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import subprocess
from pathlib import Path
from typing import Optional

CACHE_PATH = Path.home() / ".claude/skillsynapse/icons.json"
SECRETS = Path.home() / ".config/haclaw/secrets.env"

# 两个目录都要写:
#   standalone/public  —— **正在跑的那个 server 读的就是这里**,不写这里,页面看不见
#   <repo>/public      —— macos-homepage.sh 重新部署时会 `rm -rf` 掉 standalone 那份
#                         再从这里拷回去。不写这里,图标活不过下一次部署。
_HOMEPAGE = Path.home() / "code/homepage"
ICON_DIRS = (_HOMEPAGE / ".next/standalone/public/icons", _HOMEPAGE / "public/icons")
ICON_URL_PREFIX = "/icons"
HOMEPAGE_SERVICE = "system/net.skillsynapse.homepage"

# 每次刷新最多为几个新项目调 agent。一次 `git clone` 十个库不应该在同一分钟里
# 扇出十个请求 —— 剩下的下一轮(5 分钟后)接着补,反正只补一次。
MAX_NEW_PER_RUN = 4
MAX_ATTEMPTS = 3

# 没图可看的时候一个便宜模型就够了(读一段文本挑个 emoji);要**看图**并判断"这一块
# 缩到 64px 还认得出吗"是视觉判断,换强的 —— 每个项目一次,这个钱值得花。
_MODEL_TEXT = "claude-haiku-4-5-20251001"
_MODEL_VISION = "claude-sonnet-5"

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

# 200KB 上限。现在图是独立的 HTTP 资源、浏览器会缓存,不再压在每次页面加载上,
# 但一个 128px 的格子也用不着更大的图。
_MAX_BYTES = 200_000

# 图标格子是**方的**。一张 2048×512 的横幅塞进去只剩一条缝,而那条缝里的字比
# 缩写还难认 —— 它作为 README 顶部的门面是最好的一张,作为图标是最差的一张。
# 这两件事没有关系,所以形状要单独判一次,不能只看"有没有图"。
_RATIO_MAX = 2.2
_RATIO_MIN = 1 / _RATIO_MAX


# ── 落盘 ─────────────────────────────────────────────────────────────


def _slug(name: str, key: str) -> str:
    """文件名。带一段 key 的摘要,因为 `~/code/x` 和 `~/agent_space/x` 同名不同项目。"""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "icon"
    return f"{base[:32]}-{hashlib.sha1(key.encode()).hexdigest()[:6]}"


def write_icon(stem: str, ext: str, data: bytes) -> str:
    """写进所有 ICON_DIRS,返回页面用的路径。

    要不要重启服务不在这里判 —— 见 `ensure_served`:那件事只能靠取一次才知道。
    """
    for d in ICON_DIRS:
        d.mkdir(parents=True, exist_ok=True)
        # 同名不同扩展的旧文件要清掉:从 emoji 换成截图会把 .svg 变成 .png,留下的
        # 那个 .svg 再也不会被引用,但它会一直在目录里,下次翻这个目录的人分不清
        # 哪个是活的。
        for old in d.glob(f"{stem}.*"):
            if old.name != f"{stem}.{ext}":
                old.unlink(missing_ok=True)
        (d / f"{stem}.{ext}").write_bytes(data)
    return f"{ICON_URL_PREFIX}/{stem}.{ext}"


def crop_image(src: Path, box: list, out: Path) -> bool:
    """把 src 的 [x,y,w,h] 区域裁到 out(PNG)。

    ⚠ 用 ffmpeg 而不是系统自带的 `sips`,因为 `sips --cropOffset` 的原点**不是左上角**
    (文档没说是什么,实测把 [168,190,430,430] 裁成了一片空白)。`crop=w:h:x:y` 的
    x/y 就是左上角,没有歧义。

    **这种错不会报错**,它只是给你一块别的区域,而那块区域看上去完全正常。所以裁完
    要真的看一眼,不能只看 returncode。
    """
    dims = _dims(src.read_bytes())
    if not dims:
        return False
    W, H = dims
    x, y, w, h = box
    w, h = max(1, min(w, W)), max(1, min(h, H))
    x, y = max(0, min(x, W - w)), max(0, min(y, H - h))
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(src),
             "-vf", f"crop={w}:{h}:{x}:{y}", str(out)],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"    裁剪失败: {e}")
        return False
    if r.returncode != 0 or not out.exists():
        print(f"    裁剪失败: {(r.stderr or '').strip()[:160]}")
        return False
    return True


_LOCAL = "http://127.0.0.1:3000"


def _served(url_path: str, timeout: float = 5) -> bool:
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(_LOCAL + url_path, timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def ensure_served(paths: list[str]) -> None:
    """确认这些路径真的能取到,取不到就踢服务,踢完再确认一次。

    ⚠ 之前这里问的是"这个文件刚才存在吗",答案为否才踢。那个判据是瞎的:文件由上一次
    运行(或另一个进程)建好之后,对**我**来说不新,但对**服务**来说是新的 —— 服务在它
    出现之前就启动了,public 那张表里没有它。于是文件躺在正确的位置上,而 URL 一直 404。

    所以现在直接量那件事本身:**取一次看能不能取到。** 判据要能看见它要管的那个维度,
    不能拿一个相关但不同的量去替。
    """
    missing = [p for p in paths if not _served(p)]
    if not missing:
        return
    print(f"  {len(missing)} 个路径取不到(如 {missing[0]}),踢服务")
    if not kick_homepage():
        return
    import time
    for _ in range(12):                # 起来大约 2–5 秒,给到 12 秒
        time.sleep(1)
        if _served(missing[0]):
            print("  已生效")
            return
    still = [p for p in missing if not _served(p)]
    if still:
        print(f"  ⚠ 重启后仍取不到: {still[:3]}")


def kick_homepage() -> bool:
    """踢一下 homepage,让它重新扫 public/。只在真的新增了文件时调。"""
    try:
        r = subprocess.run(["sudo", "-n", "launchctl", "kickstart", "-k",
                            HOMEPAGE_SERVICE],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  重启 homepage 失败: {e}")
        return False
    if r.returncode != 0:
        print(f"  重启 homepage 失败(exit {r.returncode}): {(r.stderr or '').strip()[:160]}")
        return False
    print("  homepage 已重启(有新图标文件)")
    return True


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


def _dims(raw: bytes) -> Optional[tuple[int, int]]:
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
                return (int.from_bytes(raw[24:27], "little") + 1,
                        int.from_bytes(raw[27:30], "little") + 1)
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
    d = _dims(raw)
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


def candidates(project: Path, top: int = 24) -> list[dict]:
    """能当图标用的图,按启发式排序后取前若干张,交给 agent 去选。

    **启发式只负责淘汰,不负责挑选。** 尺寸不合适、形状不对、根本不是图 —— 这些规则
    判得了;而"哪张最能代表这个项目",规则判不了:`pubg_derecoil` 的 docs/ 底下十五张
    截图分数完全相同,排第一的由 `os.walk` 的顺序决定。**一个永远排得出第一名、而那个
    第一名毫无含义的排名,比没有排名更糟**,因为它看起来像个答案。

    所以这里只把候选缩到一屏之内(排序仍然用得上:它决定谁进这一屏),剩下的交给
    一个知道"这个项目在做枪械识别"的读者。
    """
    refs, _ = _readme_refs(project)
    ref_set = {r.lstrip("./").lower() for r in refs if not r.startswith("http")}
    ref_set |= {Path(r).name.lower() for r in refs if not r.startswith("http")}

    ranked: list[tuple[int, Path, tuple]] = []
    for p in _walk_images(project):
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if not 0 < size <= _MAX_BYTES:
            continue
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        if not _shape_ok(raw, p):
            continue
        _, total = _score(p, project, ref_set)
        ranked.append((total, p, (_dims(raw) or (0, 0), size)))
    ranked.sort(key=lambda t: -t[0])

    out = []
    for _, p, (wh, size) in ranked[:top]:
        out.append({"path": p.relative_to(project).as_posix(),
                    "w": wh[0], "h": wh[1], "kb": round(size / 1024)})
    return out


def repo_image(project: Path) -> Optional[str]:
    """agent 不可用时的确定性兜底:回到"说得出理由才用图"那条规则。"""
    refs, _ = _readme_refs(project)
    ref_set = {r.lstrip("./").lower() for r in refs if not r.startswith("http")}
    ref_set |= {Path(r).name.lower() for r in refs if not r.startswith("http")}
    best: Optional[tuple[int, Path]] = None
    for c in candidates(project, top=999):
        p = project / c["path"]
        reason, total = _score(p, project, ref_set)
        if reason > 0 and (best is None or total > best[0]):
            best = (total, p)
    return best[1].relative_to(project).as_posix() if best else None


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

**优先从项目自己的图里选一张** —— 项目画过/截过的东西,比任何符号都更能代表它。
只有在没有图、或者没有一张说得上代表这个项目时,才退回 emoji。

只输出一行 JSON,不要解释、不要代码块围栏:
{{"image":"<候选里的路径,或 null>","crop":[x,y,w,h] 或 null,
 "emoji":"<恰好一个 emoji>","hue":<0-359 的整数>,"why":"<不超过 12 个字>"}}

`image` 非空时用图,`emoji`/`hue` 是它取不到时的备胎 —— **两个字段永远都要给**。
emoji 要让人一眼想起这个项目在**做什么**,避开 📁📦🔧⚙️🚀 这类对任何项目都成立的
通用图标。hue 是 HSL 色相,用来和别的项目区分开。

{brief}
"""

_CANDIDATE_HINT = """
可选的图(路径 · 宽×高 · 大小):
{rows}

**用 Read 工具真的打开其中最有希望的几张看看**(最多 3 张,别把列表读完)。光看文件名
选不出来 —— 这正是把这件事交给你而不是交给一条规则的原因。

选完给一个 `crop`:原图里最有辨识度的那一块,`[x, y, w, h]` 像素,原点在左上角。

⚠ **`w` 和 `h` 必须相等或接近(差不超过 15%)**,因为它最终落在一个方格子里;给一个
10:1 的横条,缩进去还是一条缝。裁出来的块会被缩到约 64–128px:**满屏小字缩到那个
尺寸只是一团灰**,要挑有明确形状、大色块、或者一个清晰主体的区域。

整张图本身就合适就给 `"crop": null`。
"""

_JSON_RE = re.compile(r"\{.*?\}", re.S)


def agent_pick(project: Path, timeout: int = 300) -> Optional[dict]:
    """让 agent 打开项目里的图看一眼,选一张并给个裁剪框;选不出来才给 emoji。

    图和 emoji 在**同一次**调用里要 —— 拆成两次的话,"有没有合适的图"这个判断本身
    就要先花一次钱,而那次的答案多半是"有",于是第二次又要问"哪一张"。一次问完,
    出口是哪个由内容决定。
    """
    cands = candidates(project)
    brief = _project_brief(project)
    if cands:
        rows = "\n".join(f'  {c["path"]} · {c["w"]}×{c["h"]} · {c["kb"]}KB'
                         for c in cands)
        brief += "\n" + _CANDIDATE_HINT.format(rows=rows)
    else:
        brief += "\n(这个项目里没有任何可用的图,只能给 emoji)"

    d = _ask(brief, fallback_name=project.name, timeout=timeout,
             look_in=project if cands else None)
    if not d:
        return None
    if d.get("image"):
        ok = {c["path"] for c in cands}
        # agent 可能顺手"修正"路径或者编一个。只认候选表里的那几个。
        if d["image"] not in ok:
            print(f'    路径不在候选里,忽略: {d["image"]!r}')
            d["image"] = d["crop"] = None
    if not d["image"] and not d["emoji"]:
        return None
    return d


def agent_pick_label(name: str, description: str, timeout: int = 90) -> Optional[dict]:
    """给不在本机上的项目挑图标 —— 只有名字和一句话,没有代码可读。

    信息比扫库少得多,但**图标不需要理解一个项目,只需要认出它**,而名字加一句话
    通常足够挑出一个不会认错的 emoji。
    """
    brief = f"项目名: {name}\n一句话说明: {description}\n(这个项目不在本机,没有代码可读)"
    return _ask(brief, fallback_name=name, timeout=timeout)


def _ask(brief: str, fallback_name: str, timeout: int,
         look_in: Optional[Path] = None) -> Optional[dict]:
    env = dict(os.environ)
    if "CLAUDE_CODE_OAUTH_TOKEN" not in env:
        tok = _oauth_token()
        if tok:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    prompt = _PROMPT.format(brief=brief)

    if look_in is None:
        cmd = ["claude", "--print", "--model", _MODEL_TEXT]
    else:
        # 看图这一步换更强的模型:判断"这一块缩到 64px 还认得出吗"是视觉判断,
        # 而它每个项目只做一次。给 Read 是**唯一**让它真的看见图的办法。
        cmd = ["claude", "--print", "--model", _MODEL_VISION,
               "--allowedTools", "Read", "--add-dir", str(look_in)]
    try:
        # prompt 走 stdin,不走 argv:`--add-dir` 是变长参数,跟在它后面的位置参数会被
        # 当成又一个目录吃掉,然后报"Input must be provided"。
        r = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
            timeout=timeout, env=env, cwd=str(look_in or Path.home()))
    except (OSError, subprocess.SubprocessError) as e:
        print(f"    agent 调用失败: {e}")
        return None
    if r.returncode != 0:
        # "Not logged in" 走的是 stdout,不是 stderr —— 只读 stderr 会看到一片空白,
        # 然后以为是别的问题。
        tail = ((r.stderr or "")[-300:] or (r.stdout or "")[-300:]).strip()
        print(f"    agent exit {r.returncode}: {tail}")
        return None
    # 带工具的那次会先吐工具调用再吐结论,所以取**最后**一个 JSON 对象,不是第一个。
    ms = _JSON_RE.findall(r.stdout or "")
    d = None
    for cand in reversed(ms):
        try:
            parsed = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and ("emoji" in parsed or "image" in parsed):
            d = parsed
            break
    if d is None:
        print(f"    agent 没给出 JSON: {(r.stdout or '')[:160]!r}")
        return None
    emoji = str(d.get("emoji") or "").strip()
    hue = d.get("hue")
    hue = int(hue) % 360 if isinstance(hue, (int, float)) else _hue_pair(fallback_name)[0]
    return {"emoji": emoji, "hue": hue, "why": str(d.get("why", ""))[:24],
            "image": d.get("image") or None, "crop": _clean_crop(d.get("crop"))}


def _clean_crop(v) -> Optional[list]:
    if not (isinstance(v, (list, tuple)) and len(v) == 4):
        return None
    try:
        box = [int(x) for x in v]
    except (TypeError, ValueError):
        return None
    return box if box[2] > 0 and box[3] > 0 else None


# ── 三级:名字哈希 ────────────────────────────────────────────────────


def _hue_pair(name: str) -> tuple[int, int]:
    """Two hues from the name's digest. Deterministic — same name, same colours."""
    h = hashlib.sha256(name.encode("utf-8")).digest()
    base = h[0] * 360 // 256
    # +50° keeps the pair related rather than clashing, and never lands on grey.
    return base, (base + 50) % 360


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tile_svg(name: str, glyph: Optional[str] = None, hue: Optional[int] = None) -> bytes:
    """一块渐变方砖,中间放 glyph(emoji)或名字缩写。"""
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
    return svg.encode("utf-8")


# ── 缓存 ─────────────────────────────────────────────────────────────


_LEGACY_HUE_RE = re.compile(r"hsl\((\d+),")


class IconCache:
    def __init__(self, path: Path = CACHE_PATH):
        self.path = path
        self.data: dict = {}
        # 删除是一个**意图**,不是"我的 dict 里没有它"。合并回盘上内容时,前者能表达
        # "这一条请去掉",后者会被盘上那份原样带回来 —— forget() 就白做了。
        self.dropped: set[str] = set()
        self.dirty = False
        self.spent = 0
        self.served: list[str] = []    # 这一轮页面会引到的所有路径
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        self._migrate()

    def _migrate(self) -> None:
        """把上一版存的 data URI 记录翻成"决定"记录。

        目的只有一个:**不要为了换一种传输方式重新付一遍 agent 的钱。** emoji 在
        `source` 的第一个词里,色相在那段 SVG 里,两样都能白捡回来。
        """
        import base64
        for k, v in list(self.data.items()):
            if "uri" not in v:
                continue
            uri = v.pop("uri")
            if v.get("kind") == "agent":
                v["emoji"] = (v.get("source") or " ").split()[0]
                try:
                    svg = base64.b64decode(uri.split(",", 1)[1]).decode("utf-8")
                    m = _LEGACY_HUE_RE.search(svg)
                    v["hue"] = int(m.group(1)) if m else _hue_pair(k)[0]
                except (ValueError, UnicodeDecodeError):
                    v["hue"] = _hue_pair(k)[0]
            elif v.get("kind") == "image":
                # 图那一级本来就不花钱,重扫一次即可 —— 而且旧记录没存源路径。
                v["kind"] = "stale"
            v.pop("file", None)
            self.dirty = True

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

    def finish(self, also: Optional[list[str]] = None) -> None:
        """一轮结束时调。只有真的取不到时才踢服务。"""
        self.save()
        ensure_served(self.served + list(also or []))

    # ── 渲染:把"决定"变成一个页面能引的路径 ──────────────────────

    def _render(self, rec: dict, name: str, key: str) -> str:
        stem = _slug(name, key)
        if rec["kind"] == "image":
            src = Path(rec["project"]) / rec["src"]
            ext = src.suffix.lstrip(".").lower()
            data = None
            if rec.get("crop"):
                tmp = self.path.parent / f"crop-{stem}.png"
                if crop_image(src, rec["crop"], tmp):
                    data, ext = tmp.read_bytes(), "png"   # 裁剪一律出 PNG
                    tmp.unlink(missing_ok=True)
            url = write_icon(stem, ext, data if data else src.read_bytes())
        else:
            url = write_icon(stem, "svg",
                             tile_svg(name, rec.get("emoji"), rec.get("hue")))
        self.served.append(url)
        return url

    def _decide(self, project: Path) -> dict:
        """让 agent 先看。**它是选图的那一个,启发式只在它够不着时兜底。**

        每个项目都会花掉一次调用,包括那些 README 第一张图明摆着就是封面的项目 ——
        因为"明摆着"是我以为的,而规则一次次证明它分不清十五张同分的截图。
        """
        if self.spent < MAX_NEW_PER_RUN:
            self.spent += 1
            pick = agent_pick(project)
            if pick and pick.get("image"):
                print(f'  {project.name}: 图 {pick["image"]}'
                      f'{" 裁 " + str(pick["crop"]) if pick.get("crop") else ""}'
                      f'  {pick["why"]}')
                return {"kind": "image", "src": pick["image"], "crop": pick.get("crop"),
                        "project": str(project), "source": f'图 {pick["image"]}'}
            if pick and pick.get("emoji"):
                print(f'  {project.name}: {pick["emoji"]}  {pick["why"]}')
                return {"kind": "agent", "emoji": pick["emoji"], "hue": pick["hue"],
                        "source": f'{pick["emoji"]} {pick["why"]}'.strip()}
            # agent 够不着了,回到那条说得出理由的规则 —— 它选不出最好的一张,
            # 但"README 自己引的那张"这种情况它是对的。
            rel = repo_image(project)
            if rel:
                print(f"  {project.name}: 规则兜底 {rel}")
                return {"kind": "image", "src": rel, "crop": None,
                        "project": str(project), "source": f"规则兜底 {rel}"}
            return {"kind": "fallback", "source": "缩写渐变", "attempts": 1}
        # 这一轮的额度用完了。给一块缩写砖让页面立刻有图可用,attempts 不加 ——
        # 没试过,不算失败,下一轮(5 分钟后)再来补。
        return {"kind": "fallback", "source": "本轮额度用尽", "attempts": 0}

    def icon_for(self, project: Path) -> str:
        key = str(project.resolve())
        hit = self.data.get(key)
        # 成功的条目永不重挑 —— 图标的价值在稳定,不在最新。但**每轮都重渲染**:
        # 渲染是免费的,而它保证文件确实躺在服务目录里(部署会把那个目录清空)。
        if hit and (hit.get("kind") in ("image", "agent")
                    or hit.get("attempts", 0) >= MAX_ATTEMPTS):
            try:
                return self._render(hit, project.name, key)
            except OSError:
                self.forget(key)       # 源图没了,重挑
                hit = None

        rec = self._decide(project)
        if (hit or {}).get("kind") == "fallback" and rec["kind"] == "fallback":
            rec["attempts"] = hit.get("attempts", 0) + rec.get("attempts", 0)
        self.data[key] = rec
        self.dropped.discard(key)
        self.dirty = True
        # 每算出一条就落一条,不攒到最后。一轮要跑一分多钟,中间挂掉、被 launchd 杀掉、
        # 或者下一个项目把 agent 调爆,都不该让**已经付过的**那几次白付。
        self.save()
        return self._render(rec, project.name, key)

    def icon_for_label(self, name: str, description: str) -> Optional[str]:
        """给手写条目挑图标。键用 `manual:<名字>` —— 它没有本机路径可当键。

        额度用尽时返回 None 而不是缩写砖:手写条目本来就带 `abbr`,Homepage 会自己显示
        它。塞一块砖进去只是把"还没挑"伪装成"挑好了",下一轮就不会再来补。
        """
        key = f"manual:{name}"
        hit = self.data.get(key)
        if hit and (hit.get("kind") == "agent"
                    or hit.get("attempts", 0) >= MAX_ATTEMPTS):
            return self._render(hit, name, key)
        if self.spent >= MAX_NEW_PER_RUN:
            return None
        self.spent += 1
        pick = agent_pick_label(name, description)
        if pick:
            rec = {"kind": "agent", "emoji": pick["emoji"], "hue": pick["hue"],
                   "source": f'{pick["emoji"]} {pick["why"]}'.strip()}
            print(f'  {name}(手写): {pick["emoji"]}  {pick["why"]}')
        else:
            rec = {"kind": "fallback", "source": "缩写渐变",
                   "attempts": (hit or {}).get("attempts", 0) + 1}
        self.data[key] = rec
        self.dropped.discard(key)
        self.dirty = True
        self.save()
        return self._render(rec, name, key)
