"""在**持有项目的那台机器上**挑一张代表图,送到 hub。

hub 上的 agent 看不见别的机器上的代码,所以那些项目在首页上只能顶着一个 emoji。
能看见它们的只有本机 —— 于是选图这件事挪到本机来做,送过去的是**结果**:一张图
加一个裁剪框,几十 KB。

    本机: 扫项目 → 问 agent 挑一张 → scp 一张图 + 一份 sidecar
    hub:  按项目名认领 → 裁剪 → 变成首页上的图标

⚠ **送的是选中的那一张,不是那个目录。** `pubg_derecoil` 里有两万一千张 PNG(几乎全是
武器模板),同步整个目录是另外一件事,而且是错的那件事:首页要的是一张 64px 的图。

## 只处理 hub 真的要的名字

先问 hub 要 `bookmarks.manual.yaml` 上的条目名。**不在那张表上的项目,首页上根本没有
它** —— 为它挑图就是白花一次 agent 调用。`--all` 可以绕过这个限制。

## 用法

    python ship_icons.py --roots D:/10_projects D:/github-repos
    python ship_icons.py --roots ~/code --hub mac-mini --rescan

依赖只有 `claude` 和 `scp`(裁剪在 hub 那头做,本机不需要 ffmpeg)。
"""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from project_icons import agent_pick  # noqa: E402

STATE = Path.home() / ".claude/skillsynapse/shipped_icons.json"
HUB_INBOX = "~/cc-icons"
HUB_WANTED_CMD = "python3 ~/code/SkillSynapse/deploy/icon_inbox.py --wanted"

_SKIP = {"node_modules", "__pycache__", ".git", ".pixi", ".venv", "venv",
         "dist", "build", ".next", "target", "Library"}


def host_name(override: Optional[str] = None) -> str:
    """本机在 hub 上叫什么。

    ⚠ **不能直接用 `platform.node()`。** 这台 Windows 的计算机名是 `ShuaiYan`,而它
    的语料上行到的是 `~/cc-logs/home-desktop/` —— 同一台机器在同一个系统里就有了两个
    名字,而两边都"没错"。第一次用 `--host` 说清楚,之后记在状态文件里。
    """
    if override:
        _remember_host(override)
        return override
    saved = load_state().get("_host")
    if saved:
        return saved
    return platform.node().split(".")[0].lower()


def _remember_host(name: str) -> None:
    st = load_state()
    if st.get("_host") != name:
        st["_host"] = name
        save_state(st)


def hub_wanted(hub: str) -> list[str]:
    try:
        r = subprocess.run(["ssh", hub, HUB_WANTED_CMD],
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"问 hub 要清单失败: {e}")
        return []
    if r.returncode != 0:
        print(f"问 hub 要清单失败(exit {r.returncode}): {(r.stderr or '').strip()[:200]}")
        return []
    return [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]


def scan(roots: list[Path]) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for root in roots:
        if not root.is_dir():
            print(f"  跳过不存在的根: {root}")
            continue
        for child in sorted(root.iterdir()):
            if child.is_dir() and not child.name.startswith(".") and child.name not in _SKIP:
                found.setdefault(child.name, child)
    return found


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE)


def ship(hub: str, name: str, project: Path, rec: dict) -> bool:
    """把图和 sidecar 送到 hub。用 scp —— 一次两个小文件,不值得拉 rsync 进来。"""
    src = project / rec["src"]
    if not src.is_file():
        print(f"    源图没了: {src}")
        return False
    dest = f"{HUB_INBOX}/{host_name()}"
    side = {"file": f"{name}{src.suffix.lower()}", "src": rec["src"],
            "crop": rec.get("crop"), "why": rec.get("why", ""),
            "ts": datetime.now(timezone.utc).isoformat()}

    staged = Path(STATE.parent / "outbox")
    staged.mkdir(parents=True, exist_ok=True)
    img = staged / side["file"]
    meta = staged / f"{name}.json"
    shutil.copyfile(src, img)
    meta.write_text(json.dumps(side, ensure_ascii=False), encoding="utf-8")

    try:
        # 先建目录:scp 不会替你 mkdir,而失败信息是 "No such file or directory",
        # 看上去像是源文件不见了。
        subprocess.run(["ssh", hub, f"mkdir -p {dest}"], check=True,
                       capture_output=True, timeout=60)
        r = subprocess.run(["scp", "-q", str(img), str(meta), f"{hub}:{dest}/"],
                           capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"    送出失败: {e}")
        return False
    finally:
        img.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
    if r.returncode != 0:
        print(f"    送出失败: {(r.stderr or '').strip()[:200]}")
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", nargs="+", required=True, help="本机放项目的目录")
    ap.add_argument("--hub", default="mac-mini")
    ap.add_argument("--host", help="本机在 hub 上的名字（与 ~/cc-logs/<host>/ 保持一致，只需给一次）")
    ap.add_argument("--all", action="store_true",
                    help="不问 hub 要清单,本机扫到的都处理")
    ap.add_argument("--rescan", action="store_true", help="忽略本地缓存,全部重挑")
    ap.add_argument("--only", nargs="*", help="只处理这几个名字")
    args = ap.parse_args()

    host_name(args.host)          # 给了就记住,没给就用记住的
    roots = [Path(r).expanduser() for r in args.roots]
    local = scan(roots)
    print(f"本机扫到 {len(local)} 个项目")

    if args.all:
        names = list(local)
    else:
        want = hub_wanted(args.hub)
        if not want:
            print("hub 没给出清单,退出(要绕过用 --all)")
            return 1
        names = [n for n in want if n in local]
        missing = [n for n in want if n not in local]
        print(f"hub 要 {len(want)} 个,本机有 {len(names)} 个"
              + (f";本机没有: {', '.join(missing)}" if missing else ""))
    if args.only:
        names = [n for n in names if n in args.only]

    state = {"_host": load_state().get("_host")} if args.rescan else load_state()
    shipped = 0
    for name in names:
        key = str(local[name].resolve())
        if key in state and not args.rescan:
            print(f"  {name}: 已送过 ({state[key].get('src')})，跳过")
            continue
        pick = agent_pick(local[name])
        if not pick or not pick.get("image"):
            # 没挑出图不算失败:这个项目可能真的没有能当图标的图,hub 那边继续用
            # emoji。记下来免得每轮重问。
            print(f"  {name}: 没有可用的图,留给 hub 的 emoji")
            state[key] = {"src": None, "ts": datetime.now(timezone.utc).isoformat()}
            save_state(state)
            continue
        rec = {"src": pick["image"], "crop": pick.get("crop"), "why": pick.get("why", "")}
        print(f'  {name}: {rec["src"]}'
              + (f' 裁 {rec["crop"]}' if rec["crop"] else "")
              + f'  {rec["why"]}')
        if ship(args.hub, name, local[name], rec):
            rec["ts"] = datetime.now(timezone.utc).isoformat()
            state[key] = rec
            save_state(state)
            shipped += 1

    print(f"送出 {shipped} 张")
    return 0


if __name__ == "__main__":
    sys.exit(main())
