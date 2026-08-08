#!/usr/bin/env python3
"""Regenerate homepage's bookmarks from what is actually on disk.

Addressing a project by URL (`/?folder=<path>`) is what removes "which project am I
in" from global mutable state — you point at one instead of hoping the server
remembered. But it moves a cost: every new project needs a link, and hand-maintaining
that list just relocates the chore. So the list is derived, never written by hand.

Make a directory, and it is on the start page by the next tick. Delete one, and it is
gone. Nothing to keep in sync because there is nothing authored to fall out of sync.

Hand-written entries (things not on this machine — projects pinned to another box,
external links) live in `bookmarks.manual.yaml` and are merged in verbatim.
"""
from __future__ import annotations

import fcntl
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from project_icons import IconCache, kick_homepage  # noqa: E402
from skillsynapse_status import write_status  # noqa: E402

HOST = "https://mac-mini.tail1a4a56.ts.net"
CONFIG = Path.home() / "code/homepage/config"
OUT = CONFIG / "bookmarks.yaml"
MANUAL = CONFIG / "bookmarks.manual.yaml"

# Where projects live. Order matters: it becomes the group order on the page.
ROOTS = [
    ("项目", Path.home() / "code"),
    ("agent 工作区", Path.home() / "agent_space"),
]

# Directories that are never a project you would open as a workspace.
SKIP = {"node_modules", "__pycache__", ".git", ".pixi", ".venv", "venv",
        "dist", "build", ".next", "target", "Library"}


def abbr_for(name: str) -> str:
    """Two characters, derived from the name's own word boundaries.

    Falls back to the first two letters rather than inventing anything — an
    abbreviation you cannot trace back to the folder is worse than a dull one.
    """
    parts = [p for p in re.split(r"[-_. ]+", name) if p]
    if len(parts) >= 2:
        return (parts[0][:1] + parts[1][:1]).upper()
    caps = re.findall(r"[A-Z]", name)
    if len(caps) >= 2:
        return (caps[0] + caps[1]).upper()
    return name[:2].upper()


def describe(path: Path) -> str:
    """One line about the project, from git if it has any, else from the tree."""
    git = path / ".git"
    if git.exists():
        try:
            out = subprocess.run(
                ["git", "-C", str(path), "log", "-1", "--pretty=%s"],
                capture_output=True, text=True, encoding="utf-8", timeout=6)
            subject = (out.stdout or "").strip()
            if subject:
                return subject[:70]
        except (OSError, subprocess.SubprocessError):
            pass
        return "git 仓库"
    try:
        n = sum(1 for _ in path.iterdir())
        return f"{n} 项，未纳入 git"
    except OSError:
        return ""


def scan(root: Path) -> list[tuple[str, Path]]:
    if not root.is_dir():
        return []
    out = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith(".") or child.name in SKIP:
            continue
        out.append((child.name, child))
    return out


def yaml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


_ENTRY_RE = re.compile(r"^(\s+)- (?!abbr:|href:|icon:|description:)(\S.*?):\s*$")
_ABBR_RE = re.compile(r"^(\s+)- abbr:\s*(.*)$")
_DESC_RE = re.compile(r"^\s+description:\s*\"?(.*?)\"?\s*$")


def fill_manual_icons(manual: str, cache) -> str:
    """给 bookmarks.manual.yaml 里没写 icon 的条目补一个。

    手写文件本身不动 —— 它是**你**写的,补出来的图标是派生物,派生物不该回写进源头。
    补在合并出去的那一份里,源文件永远只有你打的那几行。
    """
    src = manual.splitlines()
    out: list[str] = []
    i = 0
    while i < len(src):
        line = src[i]
        out.append(line)
        m = _ENTRY_RE.match(line)
        if not m:
            i += 1
            continue
        # 一个条目从这里到下一个同级(或更浅)的 `- 名字:` 为止
        j = i + 1
        while j < len(src) and not (_ENTRY_RE.match(src[j])
                                    and len(_ENTRY_RE.match(src[j]).group(1))
                                    <= len(m.group(1))):
            j += 1
        block = src[i + 1:j]
        if any("icon:" in b for b in block):
            out += block
            i = j
            continue
        desc = next((_DESC_RE.match(b).group(1) for b in block if _DESC_RE.match(b)), "")
        uri = cache.icon_for_label(m.group(2).strip(), desc)
        for b in block:
            out.append(b)
            am = _ABBR_RE.match(b)
            if am and uri:
                # 和 abbr 同一个映射块,缩进对齐到 `abbr` 这个键本身(短横之后)
                out.append(" " * (len(am.group(1)) + 2) + f"icon: {uri}")
        i = j
    return "\n".join(out)


LOCK = Path.home() / ".claude/skillsynapse/bookmarks.lock"


def single_instance():
    """拿不到锁就退出,不排队。

    这个脚本每 5 分钟跑一次,而给一个新项目挑图标要花掉一次 agent 调用和一分多钟。
    没有锁的时候,手动跑一次正好压在定时那一次上,两边**各自看到空缓存、各自付一遍钱**,
    最后写文件的那个赢 —— 图标还会因此换一套。这不是假想,2026-08-08 就这么撞了一次。

    排队是错的:后面那次要做的事,前面那次已经做完了。
    """
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh                          # 进程退出时自动释放


def main() -> int:
    held = single_instance()
    if held is None:
        print("另一轮正在跑,跳过")
        return 0

    cache = IconCache()
    if "--rescan" in sys.argv:
        # 强制重挑所有图标。日常不需要 —— 图标的价值在于稳定,不在于最新。
        cache.forget()
        print("图标缓存已清空,本轮起重挑")

    lines = [
        "---",
        "# 自动生成 —— 不要手改，改动会在下一次刷新时消失。",
        "# 生成者: skillsynapse/deploy/refresh_bookmarks.py（每 5 分钟一次）",
        "# 手写条目放 bookmarks.manual.yaml，会被原样并进来。",
        "",
    ]
    total = 0
    for group, root in ROOTS:
        entries = scan(root)
        if not entries:
            continue
        lines.append(f"- {group}:")
        for name, path in entries:
            total += 1
            lines += [
                f"    - {yaml_escape(name)}:",
                f'        - abbr: {abbr_for(name)}',
                f'          icon: {cache.icon_for(path)}',
                f'          href: {HOST}/?folder={path}',
                f'          description: "{yaml_escape(describe(path))}"',
            ]
        lines.append("")

    if MANUAL.exists():
        manual = MANUAL.read_text(encoding="utf-8")
        # Strip a leading document marker so the merge stays one document.
        manual = re.sub(r"\A---\s*\n", "", manual)
        manual = fill_manual_icons(manual, cache)
        lines += ["# ── 以下来自 bookmarks.manual.yaml ──", manual.rstrip(), ""]

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT} — {total} project(s)")

    # SkillSynapse 没有 Web UI,状态只能由这一侧主动摆出来给首页那张卡片读。
    # 搭在这个 5 分钟的心跳上,而不是再起一个 daemon —— 同样的节奏,同一把锁。
    write_status()

    # YAML 先落地再踢服务:重启后它读到的就该是这一份,而不是上一轮的。
    cache.finish(also=["/skillsynapse.json"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
