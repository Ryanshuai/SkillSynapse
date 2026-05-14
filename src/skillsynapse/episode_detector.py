"""Episode detection for session extraction (v0.2).

Splits a parsed session into topically-coherent episodes, so the extractor
can produce multiple focused skill candidates per session instead of a
single diluted one. Inherits from HyperMem (EverMind-AI, ACL 2026) but
swaps the per-message LLM boundary check for a zero-LLM heuristic on
`tool_use` events — Claude Code sessions carry structured events we can
slice cheaply.

Integration contract (see docs/episode_detection_integration.md):

    episodes = detect_episodes(session_id, events)
    for ep in episodes:
        total, diversity = tool_call_stats(ep.events)
        if total < min_tool_calls: continue
        ...

Boundary rules (Layer 1, heuristic):
    (a) time gap between consecutive tool_use events ≥ gap_minutes
    (b) tool-name sets in the adjacent windows disjoint
          (`|prev_window ∩ next_window| < min_tool_overlap`)

Either rule fires → new episode starts at the next tool_use. Events with
no tool_use activity collapse into a single "degenerate" episode that the
caller will typically filter out via the existing complexity gate.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .scanner import Event


logger = logging.getLogger("skillsynapse.episode_detector")


# ── Data ─────────────────────────────────────────────────────────────

@dataclass
class Episode:
    """A topically-coherent slice of a session.

    Episodes are produced by `detect_episodes`; the extractor treats each
    as the unit of skill extraction (not the whole session).
    """
    session_id: str
    episode_idx: int                  # 0-based position within the session
    events: list[Event]
    start_time: Optional[datetime]    # first parseable timestamp in `events`
    end_time: Optional[datetime]      # last parseable timestamp in `events`
    tool_names: set[str] = field(default_factory=set)
    boundary_reason: str = "heuristic"  # "heuristic" | "final" | "llm_merge"

    @property
    def episode_id(self) -> str:
        """Stable identifier: <session_id>:ep<idx>. Stored as `source_episode`
        on SkillRecord once §5.5 of the integration plan lands."""
        return f"{self.session_id}:ep{self.episode_idx}"

    @property
    def tool_call_count(self) -> int:
        return sum(1 for e in self.events if e.type == "tool_use" and e.tool_name)


# ── Internal helpers ─────────────────────────────────────────────────

def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    """Best-effort ISO-8601 → datetime. Returns None on failure."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _time_gap(a: Optional[str], b: Optional[str]) -> Optional[timedelta]:
    """Gap from `a` to `b`. Returns None if either timestamp is unparseable
    (caller treats None as "no gap signal available"). Negative gaps are
    possible if events arrive out of order — caller ignores them."""
    ta, tb = _parse_ts(a), _parse_ts(b)
    if ta is None or tb is None:
        return None
    return tb - ta


def _tool_indices(events: list[Event]) -> list[int]:
    return [i for i, ev in enumerate(events) if ev.type == "tool_use" and ev.tool_name]


def _tool_names_in_range(events: list[Event], idxs: list[int]) -> set[str]:
    """Tool-name set for the tool_use events at the given indices."""
    return {events[i].tool_name for i in idxs if events[i].tool_name}


def _first_ts(events: list[Event]) -> Optional[datetime]:
    for ev in events:
        ts = _parse_ts(ev.timestamp)
        if ts is not None:
            return ts
    return None


def _last_ts(events: list[Event]) -> Optional[datetime]:
    for ev in reversed(events):
        ts = _parse_ts(ev.timestamp)
        if ts is not None:
            return ts
    return None


def _make_episode(
    session_id: str, idx: int, events: list[Event], reason: str
) -> Episode:
    return Episode(
        session_id=session_id,
        episode_idx=idx,
        events=events,
        start_time=_first_ts(events),
        end_time=_last_ts(events),
        tool_names={e.tool_name for e in events
                    if e.type == "tool_use" and e.tool_name},
        boundary_reason=reason,
    )


# ── Public API ───────────────────────────────────────────────────────

def detect_episodes(
    session_id: str,
    events: list[Event],
    *,
    gap_minutes: int = 30,
    min_tool_overlap: int = 0,
    window: int = 5,
) -> list[Episode]:
    """Split a session into episodes using tool_use boundaries.

    Parameters
    ----------
    session_id
        The session's .jsonl stem (used to build each Episode's id).
    events
        Full event list from `scanner.parse_jsonl`.
    gap_minutes
        Two consecutive tool_use events ≥ this many minutes apart start a
        new episode. Defaults to 30 (matches config_default.yaml once the
        key lands).
    min_tool_overlap
        Minimum number of tool names shared between the trailing window
        of the current episode and the leading window of the next
        tool_use candidate. 0 disables the overlap rule. **Default 0**:
        empirically the overlap rule over-splits real Claude Code
        sessions (one debug session split into 26 tiny episodes when
        set to 1), because legitimate tool switches within a single
        task happen frequently. Time-gap alone is a cleaner signal.
        Raise to 1+ only when timestamps are unreliable.
    window
        How many tool_use events to include on each side when computing
        overlap. Default 5 matches HyperMem's 5-exchange episode horizon.

    Returns
    -------
    list[Episode]
        In session order. Never empty if `events` is non-empty.

    Behavior edge cases:
    - 0 events → []
    - 0 or 1 tool_use events → single episode wrapping the whole session
      (not worth splitting)
    - Missing / unparseable timestamps → gap rule falls back to "no
      signal", relying solely on tool-overlap. If both signals are
      absent, episodes stay merged (conservative default).
    """
    if not events:
        return []

    tool_idxs = _tool_indices(events)

    # Fewer than 2 tool_uses → nothing to split on; one degenerate episode.
    # Caller's complexity filter will likely discard it.
    if len(tool_idxs) < 2:
        return [_make_episode(session_id, 0, events, "heuristic")]

    # Walk consecutive tool_use pairs, accumulate split points.
    # A split point is an event index at which a new episode begins.
    split_points: list[int] = [0]

    for k in range(len(tool_idxs) - 1):
        cur_tool_idx = tool_idxs[k]
        nxt_tool_idx = tool_idxs[k + 1]

        cur_ev = events[cur_tool_idx]
        nxt_ev = events[nxt_tool_idx]

        # Time-gap rule
        gap = _time_gap(cur_ev.timestamp, nxt_ev.timestamp)
        time_split = (
            gap is not None
            and gap.total_seconds() >= gap_minutes * 60
        )

        # Tool-overlap rule. Trailing window = last `window` tool_uses up
        # to and including current. Leading window = next `window`
        # tool_uses starting at the candidate next one.
        trail_start = max(0, k - window + 1)
        lead_end = min(len(tool_idxs), k + 1 + window)
        trail_set = _tool_names_in_range(events, tool_idxs[trail_start:k + 1])
        lead_set = _tool_names_in_range(events, tool_idxs[k + 1:lead_end])
        overlap = len(trail_set & lead_set)
        overlap_split = overlap < min_tool_overlap

        if time_split or overlap_split:
            # Begin a new episode at the next tool_use event. Events
            # strictly before `nxt_tool_idx` (including any user/assistant
            # messages that led into this new tool_use) go with the
            # current episode. Precise "split at preceding user message"
            # is deferred — see plan doc §10.
            split_points.append(nxt_tool_idx)
            logger.debug(
                "split session=%s at ev_idx=%d (tool %d→%d): "
                "time_split=%s (gap=%s), overlap_split=%s (%d shared)",
                session_id, nxt_tool_idx, cur_tool_idx, nxt_tool_idx,
                time_split, gap, overlap_split, overlap,
            )

    split_points.append(len(events))

    # Slice events into episodes.
    episodes: list[Episode] = []
    for i in range(len(split_points) - 1):
        start, end = split_points[i], split_points[i + 1]
        slice_evs = events[start:end]
        if not slice_evs:
            continue
        reason = "final" if i == len(split_points) - 2 and len(split_points) == 2 \
            else "heuristic"
        episodes.append(_make_episode(session_id, len(episodes), slice_evs, reason))

    logger.debug(
        "session=%s split into %d episode(s); tool_counts=%s",
        session_id, len(episodes), [ep.tool_call_count for ep in episodes],
    )
    return episodes
