"""hub 这一侧的图标收件箱:接收别的机器挑好的项目图。

首页上那几个"落在别处的"项目 —— `pubg_derecoil` 在 home-desktop,`MLClaw` 在别处 ——
hub 上没有它们的代码,所以 hub 上的 agent 看不见它们的图,只能给个 emoji。

**能看见那些图的只有持有项目的那台机器。** 所以选图这件事发生在那一头:它扫本地项目、
调 agent 挑一张、把**选中的那一张**(几十 KB)送过来。送的是结果不是素材 —— 一个项目
可能有两万张 PNG(pubg 就是),同步整个目录是另一回事,而且是错的那件事。

## 布局

    ~/cc-icons/<host>/<项目名>.png     图本身(原图,未裁)
    ~/cc-icons/<host>/<项目名>.json    {"src","crop","why","ts"}

裁剪在 **hub 这一头**做,不在发送端:裁剪逻辑只该有一份实现,而且将来某台机器没有
ffmpeg 时,它仍然能把图和裁剪框送过来。发送端只需要 `claude` 和 `scp`。

## 谁决定要哪些名字

`bookmarks.manual.yaml` 里的条目名 —— **不在那张表上的项目,首页上根本没有它**,
为它挑一张图就是白花一次 agent 调用。所以发送端先问 hub 要这张表,再决定扫什么。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Optional

INBOX = Path.home() / "cc-icons"
MANUAL = Path.home() / "code/homepage/config/bookmarks.manual.yaml"

# 条目行:缩进 >= 4 的 `- 名字:`,且不是 abbr/href/icon/description 这些键。
_ENTRY_RE = re.compile(r"^(\s{4,})- (?!abbr:|href:|icon:|description:)(\S.*?):\s*$")


def wanted() -> list[str]:
    """首页上那些"落在别处的"条目名。"""
    if not MANUAL.is_file():
        return []
    out = []
    for line in MANUAL.read_text(encoding="utf-8").splitlines():
        m = _ENTRY_RE.match(line)
        if m:
            out.append(m.group(2).strip())
    return out


def find(name: str) -> Optional[dict]:
    """某个项目名对应的、最新的一份来件。返回 dict 且带 `image` 绝对路径。

    多台机器可能都送过同一个名字(比如一个项目在两台机器上都有 checkout)。取 `ts`
    最新的那一份 —— 不是取第一个找到的,那等于按 `iterdir` 的顺序选,而那个顺序
    没有含义。
    """
    if not INBOX.is_dir():
        return None
    best: Optional[dict] = None
    for host_dir in INBOX.iterdir():
        if not host_dir.is_dir():
            continue
        side = host_dir / f"{name}.json"
        if not side.is_file():
            continue
        try:
            rec = json.loads(side.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        img = host_dir / rec.get("file", "")
        if not img.is_file():
            continue
        rec["image"] = str(img)
        rec["host"] = host_dir.name
        if best is None or rec.get("ts", "") > best.get("ts", ""):
            best = rec
    return best


def main() -> int:
    if "--wanted" in sys.argv:
        print("\n".join(wanted()))
        return 0
    INBOX.mkdir(parents=True, exist_ok=True)
    print(f"收件箱 {INBOX}")
    for name in wanted():
        rec = find(name)
        print(f"  {name:18} " + (f'{rec["host"]} · {rec["src"]} · {rec.get("why","")}'
                                 if rec else "(还没有来件)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
