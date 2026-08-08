"""把 SkillSynapse 的当前状态写成一份 JSON,给首页那张卡片读。

它没有 Web UI —— 是个 daemon,所以卡片不能像 Syncthing 那样去问服务要状态,只能由
这一侧主动把状态摆出来。

## ⚠ 数的是**此刻的状态**,不是日志行数

`merges.jsonl` 是 append-only 的**尝试**记录:那 5 个非 md 冲突(.py/.json/.sh)每小时
被重新扫到、重新判定一次,于是 `skipped_unmergeable` 在日志里有 70 行。

    70 行  ≠  70 个冲突        70 = 5 个冲突 × 14 次尝试

一张显示 70 的卡片会让人以为出了大事,而实际情况是同样的 5 个文件躺在那儿没动。
**要显示"现在有几个",就得去盘上数几个,不能去日志里数几行。**

同一个形状在这个仓库里出现过不止一次:一个算术正确、自洽、而且答非所问的数字,比
没有数字更危险,因为没人会去质疑它。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

HOMEPAGE = Path.home() / "code/homepage"
# 和图标同一条规矩:standalone 那份是**正在跑的 server 读的**,repo 那份是为了活过
# 下一次部署(部署会 rm -rf 掉 standalone 的 public 再从 repo 拷)。
PUBLIC_DIRS = (HOMEPAGE / ".next/standalone/public", HOMEPAGE / "public")
OUT_NAME = "skillsynapse.json"

SKILLS_ROOTS = (Path.home() / ".claude/skills",
                Path.home() / "code/claude-config/skills")
MERGES = Path.home() / ".claude/skillsynapse/logs/merges.jsonl"
CC_LOGS = Path.home() / "cc-logs"

_CONFLICT_GLOB = "*.sync-conflict-*"


def _skill_count() -> int:
    """技能数取各处的**并集**,不是相加 —— 两个根同步的是同一批东西,加起来是双计。"""
    names: set[str] = set()
    for root in SKILLS_ROOTS:
        if root.is_dir():
            names |= {p.name for p in root.iterdir()
                      if p.is_dir() and not p.name.startswith(".")}
    return len(names)


def _conflicts_now() -> int:
    """按 **resolve 之后的真实路径**去重。

    `~/.claude/skills/<x>` 是软链,指向 `claude-config/skills/<x>` —— 同一个冲突文件
    从两个根都能走到。相加得 10,而盘上只有 5 个。(`find` 不跟软链所以报 0,`rglob`
    跟,两个工具给出两个都不对的答案,而它们都"没算错"。)
    """
    seen: set[Path] = set()
    for root in SKILLS_ROOTS:
        if root.is_dir():
            seen |= {p.resolve() for p in root.rglob(_CONFLICT_GLOB)}
    return len(seen)


def _merge_stats() -> tuple[int, str]:
    """(合并过的不同文件数, 上次跑的时刻)。按 canonical 去重,同一个文件合过三次算一个。"""
    merged: set[str] = set()
    last = ""
    if not MERGES.is_file():
        return 0, ""
    for line in MERGES.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("ts"):
            last = r["ts"]
        if r.get("action") == "merged" and r.get("canonical"):
            merged.add(r["canonical"])
    if last:
        try:
            last = datetime.fromisoformat(last).astimezone().strftime("%m-%d %H:%M")
        except ValueError:
            last = last[:16]
    return len(merged), last


def _hosts() -> int:
    """上行语料的机器数 —— 每台一个子目录。"""
    if not CC_LOGS.is_dir():
        return 0
    return sum(1 for p in CC_LOGS.iterdir()
               if p.is_dir() and not p.name.startswith("."))


def _sessions() -> int:
    """可供生长的原料:三台机器上行的会话数。"""
    if not CC_LOGS.is_dir():
        return 0
    return sum(1 for _ in CC_LOGS.rglob("*.jsonl"))


def _newest_skill() -> str:
    """最近一个技能是什么时候长出来的。

    走 `resolve()` 之后的真实目录 —— `~/.claude/skills` 本身是个软链,对它取 mtime
    得到的是**建软链**那一刻,和技能有没有更新毫无关系。
    """
    newest = 0.0
    for root in SKILLS_ROOTS:
        root = root.resolve()
        if not root.is_dir():
            continue
        for p in root.iterdir():
            if p.is_dir() and not p.name.startswith("."):
                newest = max(newest, p.stat().st_mtime)
    return datetime.fromtimestamp(newest).strftime("%m-%d") if newest else "—"


def build() -> dict:
    """两张卡片共用一份 JSON,各自映射自己那几个字段。

    **生长和合并是两件事**,不是一件事的两个角度:
      · 生长 —— 从对话日志里蒸馏新技能。费 token,所以**没有定时器**,由人决定什么时候跑。
      · 合并 —— 三台机器改同一个技能撞出的冲突。每 5 分钟扫,整点交给 agent 融。
    把它们写在同一张卡上,等于宣称跑其中一个就等于跑了另一个。
    """
    merged, last = _merge_stats()
    return {
        # 生长
        "skills": _skill_count(), "sessions": _sessions(), "hosts": _hosts(),
        "newest": _newest_skill(),
        # 合并
        "conflicts": _conflicts_now(), "merged": merged, "last": last or "—",
    }


def write_status() -> bool:
    """写 JSON,返回"是不是新建了文件"(新建要踢服务,覆盖不用 —— 同图标那条规矩)。"""
    data = json.dumps(build(), ensure_ascii=False)
    fresh = False
    for d in PUBLIC_DIRS:
        d.mkdir(parents=True, exist_ok=True)
        p = d / OUT_NAME
        if not p.exists():
            fresh = True
        p.write_text(data, encoding="utf-8")
    return fresh


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=1))
    print("新建:" if write_status() else "覆盖:", ", ".join(str(d / OUT_NAME) for d in PUBLIC_DIRS))
