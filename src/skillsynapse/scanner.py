"""Session scanner.

Scans `~/.claude/projects/**/*.jsonl`, filters by modified time, excludes
subagent session files (design §5 Step 1 + fix A3). Also exposes the low-level
`parse_jsonl` and event-walk helpers used by metrics.py / extractor.py, so we
only parse each .jsonl once per pipeline run.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional

from .models import SessionMeta


@dataclass
class Event:
    """Normalized view of one .jsonl line.

    The raw Claude Code / Cowork transcript shape varies between runs; this
    dataclass pulls out the fields the pipeline actually uses and keeps the
    rest on `raw` for anything bespoke.
    """
    idx: int
    type: str                      # "user" | "assistant" | "tool_use" | "tool_result" | "system" | ...
    timestamp: Optional[str]
    raw: dict
    # For tool_use:
    tool_name: Optional[str] = None
    tool_id: Optional[str] = None
    tool_input: dict = field(default_factory=dict)
    # For tool_result:
    tool_result_for: Optional[str] = None
    is_error: bool = False
    # For user / assistant:
    text: str = ""


def _iter_text_parts(content: Any) -> Iterator[str]:
    if isinstance(content, str):
        yield content
        return
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                txt = c.get("text")
                if isinstance(txt, str):
                    yield txt


def _extract_text(content: Any) -> str:
    return "\n".join(_iter_text_parts(content))


def parse_jsonl(path: Path) -> list[Event]:
    """Parse a Claude Code .jsonl transcript into a flat Event list.

    Every tool_use / tool_result inside an assistant or user message gets its
    own Event so metrics.py can scan linearly and reference indices.
    """
    events: list[Event] = []
    idx = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = raw.get("timestamp") or raw.get("ts")
            etype = raw.get("type", "")
            msg = raw.get("message")

            if etype in ("user", "assistant") and isinstance(msg, dict):
                content = msg.get("content")
                # Emit one parent event for the message itself (with text)
                events.append(
                    Event(
                        idx=idx, type=etype, timestamp=ts, raw=raw,
                        text=_extract_text(content),
                    )
                )
                idx += 1
                # Emit child events for each tool_use / tool_result block
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "tool_use":
                            events.append(
                                Event(
                                    idx=idx, type="tool_use", timestamp=ts, raw=block,
                                    tool_name=block.get("name", ""),
                                    tool_id=block.get("id", ""),
                                    tool_input=block.get("input", {}) or {},
                                )
                            )
                            idx += 1
                        elif btype == "tool_result":
                            result_content = block.get("content")
                            events.append(
                                Event(
                                    idx=idx, type="tool_result", timestamp=ts, raw=block,
                                    tool_result_for=block.get("tool_use_id", ""),
                                    is_error=bool(block.get("is_error", False)),
                                    text=_extract_text(result_content),
                                )
                            )
                            idx += 1
            else:
                events.append(Event(idx=idx, type=etype, timestamp=ts, raw=raw))
                idx += 1
    return events


def find_tool_result(events: list[Event], tool_use_id: str) -> Optional[Event]:
    for ev in events:
        if ev.type == "tool_result" and ev.tool_result_for == tool_use_id:
            return ev
    return None


def tool_call_stats(events: list[Event]) -> tuple[int, int]:
    """Return (total tool_use count, distinct tool-name count)."""
    names: set[str] = set()
    n = 0
    for ev in events:
        if ev.type == "tool_use" and ev.tool_name:
            n += 1
            names.add(ev.tool_name)
    return n, len(names)


def session_error_rate(events: list[Event]) -> float:
    total = 0
    errs = 0
    for ev in events:
        if ev.type == "tool_result":
            total += 1
            if ev.is_error:
                errs += 1
    return errs / total if total else 0.0


def first_user_text(events: list[Event]) -> str:
    for ev in events:
        if ev.type == "user" and ev.text:
            return ev.text
    return ""


def last_assistant_text(events: list[Event]) -> str:
    for ev in reversed(events):
        if ev.type == "assistant" and ev.text:
            return ev.text
    return ""


# ── Scanning ─────────────────────────────────────────────────────

def _derive_project(path: Path, projects_root: Path) -> str:
    try:
        rel = path.relative_to(projects_root)
        return rel.parts[0] if rel.parts else path.parent.name
    except ValueError:
        return path.parent.name


def find_sessions(
    projects_root: Path,
    *,
    modified_after: Optional[datetime] = None,
    exclude_subagents: bool = True,
) -> list[SessionMeta]:
    """Return session handles (no event parsing yet) matching the filters."""
    if not projects_root.exists():
        return []

    sessions: list[SessionMeta] = []
    for jsonl in projects_root.rglob("*.jsonl"):
        if exclude_subagents and "subagents" in jsonl.parts:
            continue
        try:
            st = jsonl.stat()
        except OSError:
            continue
        if modified_after is not None:
            mtime = datetime.fromtimestamp(st.st_mtime)
            if mtime < modified_after:
                continue
        sessions.append(
            SessionMeta(
                id=jsonl.stem,
                path=str(jsonl),
                project=_derive_project(jsonl, projects_root),
                first_event_time=None,
                num_events=0,
            )
        )
    return sessions


def yesterday_cutoff(hours_back: int = 24) -> datetime:
    return datetime.now() - timedelta(hours=hours_back)
