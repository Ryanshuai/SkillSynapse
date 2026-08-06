"""Metrics collection — Step 2 (tool_use) + Step 2.5 (slash command).

Walks each session's Event list, attributes every `Skill` tool_use and every
`<command-name>/xxx</command-name>` marker to a tracked skill, and updates the
four counters plus the rolling `recent_analyses` window.

v3.5 post-review #3 + #4: both paths share `_record_skill_activation()` so the
error-fallback branch can't silently drop an analysis entry, and so every
patch to the shared logic flows to both activation sources.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from .config import Config
from .models import ExecutionAnalysis, SkillRecord, iso_now, normalize_ts
from .scanner import Event, find_tool_result, parse_jsonl
from .slash_command_parser import CMD_RE
from .store import Store


logger = logging.getLogger(__name__)


# User-message patterns that look like a correction. §5 Step 2.
_CORRECTION_RE = re.compile(
    r"(不对|重来|错了|不是这样|别这样|stop|undo|revert|rollback|that's wrong|"
    r"please don't|doesn't work|didn't work|no,? that|no,? i meant)",
    re.IGNORECASE,
)


def looks_like_correction(texts: list[str]) -> bool:
    for t in texts:
        if _CORRECTION_RE.search(t):
            return True
    return False


def session_ended_without_error(
    events: list[Event], from_index: int, tail_window: int
) -> bool:
    """Did the session reach a clean end after `from_index`?

    Heuristic: the assistant responded at least once after the anchor AND the
    last `tail_window` events of the post-anchor slice contain no is_error
    tool_result. A mid-session error followed by a successful retry is the
    norm in real sessions and should NOT deflate completion_rate — only errors
    at the very end count.
    """
    after = events[from_index:]
    if not any(ev.type == "assistant" for ev in after):
        return False
    # The tail must live *inside* `after` — otherwise an anchor near the end of
    # a long session would inspect events that happened BEFORE the skill was
    # activated, producing nonsense completion verdicts.
    tail = after[-tail_window:] if tail_window > 0 else after
    return not any(ev.type == "tool_result" and ev.is_error for ev in tail)


def _collect_user_msgs_until_next_skill(
    events: list[Event], *, from_idx: int, limit: int
) -> list[str]:
    """Collect user message texts starting at from_idx, stopping at the next
    Skill activation (tool_use or <command-name>) or after `limit` messages.
    """
    collected: list[str] = []
    for ev in events[from_idx:]:
        if ev.type == "tool_use" and ev.tool_name == "Skill":
            break
        if ev.type == "user":
            if CMD_RE.search(ev.text or ""):
                break
            if ev.text:
                collected.append(ev.text)
                if len(collected) >= limit:
                    break
    return collected


def _summarize_skill_use(skill_name: str, events: list[Event], anchor_idx: int) -> str:
    """One-liner for ExecutionAnalysis.note."""
    anchor = events[anchor_idx]
    window = events[anchor_idx + 1 : anchor_idx + 11]
    tools_after = [e.tool_name for e in window if e.type == "tool_use" and e.tool_name]
    tools_preview = ", ".join(tools_after[:5]) or "<no follow-up tools>"
    anchor_desc = "tool_use" if anchor.type == "tool_use" else "slash_cmd"
    return f"{skill_name} via {anchor_desc}; after: {tools_preview}"


def _extract_tool_issues(
    events: list[Event], anchor_idx: int, *, window: int
) -> list[str]:
    """Return tool names whose result in the window was is_error=True."""
    issues: list[str] = []
    # Map tool_use_id -> tool_name for look-up when we see a failing result.
    id_to_name: dict[str, str] = {}
    for ev in events[anchor_idx : anchor_idx + 1 + window]:
        if ev.type == "tool_use" and ev.tool_id:
            id_to_name[ev.tool_id] = ev.tool_name or ""
        elif ev.type == "tool_result" and ev.is_error:
            name = id_to_name.get(ev.tool_result_for or "", "")
            if name:
                issues.append(name)
    return issues


def _trim_recent_analyses(skill: SkillRecord, window: int) -> None:
    if len(skill.recent_analyses) > window:
        skill.recent_analyses = skill.recent_analyses[-window:]


def _record_skill_activation(
    skill: SkillRecord,
    anchor_idx: int,
    events: list[Event],
    session_id: str,
    cfg: Config,
) -> None:
    """Shared per-activation update. Caller must have already bumped
    total_selections / selections_since_version."""
    anchor_ev = events[anchor_idx]
    window_applied = cfg.metrics.window_applied
    window_correct = cfg.metrics.window_correct
    recent_window = cfg.metrics.recent_analyses_window
    tail_window = cfg.metrics.get("completion_tail_window", 5)

    verdict: Optional[str] = None
    is_error_fallback = False

    # 1. Error fallback (only meaningful when anchor is a tool_use).
    if anchor_ev.type == "tool_use":
        result = find_tool_result(events, anchor_ev.tool_id or "")
        if result and result.is_error:
            skill.total_fallbacks += 1
            skill.fallbacks_since_version += 1
            verdict = "fallback"
            is_error_fallback = True

    # 2. Applied: any tool_use in the window after the anchor, but STOP at the
    # next distinct skill activation so B's tools aren't credited to A.
    # — a Skill tool_use for a different skill name is a new activation
    # — a user message carrying <command-name>/foo</command-name> is also one
    # — a Skill tool_use for the SAME skill is just a reload (doesn't count as
    #   applied, doesn't truncate).
    start = anchor_idx + 1
    end = start + window_applied
    applied = False
    for e in events[start:end]:
        if e.type == "user" and e.text and CMD_RE.search(e.text):
            break
        if e.type == "tool_use" and e.tool_name == "Skill":
            other = str((e.tool_input or {}).get("skill", "")).strip()
            if other and other != skill.name:
                break
            continue
        if e.type == "tool_use":
            applied = True
            break
    if applied:
        skill.total_applied += 1
        skill.applied_since_version += 1

    # 3. Completion / soft-fallback / applied_only.
    if not is_error_fallback:
        user_msgs = _collect_user_msgs_until_next_skill(
            events, from_idx=anchor_idx + 1, limit=window_correct
        )
        if looks_like_correction(user_msgs):
            skill.total_fallbacks += 1
            skill.fallbacks_since_version += 1
            verdict = "fallback"
        elif session_ended_without_error(events, anchor_idx, tail_window):
            skill.total_completions += 1
            skill.completions_since_version += 1
            verdict = "completion"
        else:
            verdict = "applied_only"

    # 4. Always write the ExecutionAnalysis (fix #3).
    ts = normalize_ts(anchor_ev.timestamp) or iso_now()
    analysis = ExecutionAnalysis(
        timestamp=ts,
        session_id=session_id,
        verdict=verdict or "applied_only",
        note=_summarize_skill_use(skill.name, events, anchor_idx),
        tool_issues=_extract_tool_issues(events, anchor_idx, window=window_applied),
    )
    skill.recent_analyses.append(analysis)
    _trim_recent_analyses(skill, recent_window)


def collect_metrics(
    store: Store,
    session_paths: list[Path],
    cfg: Config,
) -> dict[str, int]:
    """Walk every session and update metrics for skills referenced inside.

    Returns a small stats dict used by main.py for the run summary.
    """
    stats = {
        "sessions_parsed": 0,
        "sessions_unreadable": 0,
        "tool_use_hits": 0,
        "slash_command_hits": 0,
        "skill_not_found": 0,
    }

    # Cache active skills by name; reloaded once per run.
    cache: dict[str, SkillRecord] = {
        s.name: s for s in store.list_active_skills()
    }
    touched: dict[str, SkillRecord] = {}

    def _get_active(name: str) -> Optional[SkillRecord]:
        return touched.get(name) or cache.get(name)

    for path in session_paths:
        try:
            events = parse_jsonl(path)
        except Exception as e:
            # A session that never parses contributes no selections, so every
            # skill it would have exercised looks unused — the pruning gates
            # read that as "nobody wants this". Keep the run going, but never
            # silently: `sessions_unreadable` makes the shortfall countable.
            stats["sessions_unreadable"] += 1
            logger.warning("metrics: cannot parse %s (%s) — skills used in this "
                           "session go uncounted", path.name, e)
            continue
        stats["sessions_parsed"] += 1
        session_id = path.stem

        for i, ev in enumerate(events):
            # ── Step 2: Skill tool_use ─────────────────
            if ev.type == "tool_use" and ev.tool_name == "Skill":
                skill_name = str(ev.tool_input.get("skill", "")).strip()
                if not skill_name:
                    continue
                skill = _get_active(skill_name)
                if skill is None:
                    stats["skill_not_found"] += 1
                    continue
                skill.total_selections += 1
                skill.selections_since_version += 1
                skill.last_used_at = normalize_ts(ev.timestamp) or iso_now()
                _record_skill_activation(skill, i, events, session_id, cfg)
                if session_id not in skill.source_sessions:
                    skill.source_sessions.append(session_id)
                touched[skill.name] = skill
                stats["tool_use_hits"] += 1
                continue

            # ── Step 2.5: slash command ────────────────
            if ev.type == "user":
                text = ev.text or ""
                if not text or "<command-name>" not in text:
                    continue
                # Dedupe within a single user message: Claude Code renders the
                # same `<command-name>/foo</command-name>` tag multiple times
                # when the assistant echoes the slash command back, but that's
                # still one activation (§5 Step 2.5).
                seen_in_msg: set[str] = set()
                for m in CMD_RE.finditer(text):
                    name = m.group(1)
                    if name in seen_in_msg:
                        continue
                    seen_in_msg.add(name)
                    skill = _get_active(name)
                    if skill is None:
                        stats["skill_not_found"] += 1
                        continue
                    skill.total_selections += 1
                    skill.selections_since_version += 1
                    skill.last_used_at = normalize_ts(ev.timestamp) or iso_now()
                    _record_skill_activation(skill, i, events, session_id, cfg)
                    if session_id not in skill.source_sessions:
                        skill.source_sessions.append(session_id)
                    touched[skill.name] = skill
                    stats["slash_command_hits"] += 1

    # Persist everything we touched.
    for skill in touched.values():
        # Probation exit (§7): v0.1 only flips the flag; no downstream effect
        # because no evolution runs yet, but v0.2 expects this invariant.
        if skill.probation and skill.selections_since_version >= cfg.probation.min_selections:
            skill.probation = False
        skill.updated_at = iso_now()
        store.upsert_skill(skill)

    return stats
