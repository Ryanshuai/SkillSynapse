"""Data models. Mirrors §3 of the design doc.

v0.1 only exercises `origin in ("captured", "manual")` and keeps version = 1 for
all records. The full version DAG (fix/derive/split/merge) lands in v0.2; the
shape is kept complete so v0.2 can fill it in without a schema migration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional


SkillOrigin = Literal["captured", "derived", "fixed", "manual"]
Verdict = Literal["completion", "applied_only", "fallback"]


@dataclass
class Pitfall:
    description: str
    source_session: str
    hit_count: int = 0


@dataclass
class ExecutionAnalysis:
    """One skill activation's execution result. Feeds `recent_analyses` (§3)."""
    timestamp: str  # ISO8601 string for cheap JSON round-trip
    session_id: str
    verdict: Verdict
    note: str
    tool_issues: list[str] = field(default_factory=list)


@dataclass
class SkillRecord:
    # Identity
    skill_id: str
    name: str
    description: str = ""
    category: Optional[str] = None  # None = uncategorized (v3.5 post-review #A)
    content: str = ""
    compressed_content: Optional[str] = None
    trigger: Optional[list[str]] = None
    tags: list[str] = field(default_factory=list)

    # Dependencies
    tool_dependencies: list[str] = field(default_factory=list)
    critical_tools: list[str] = field(default_factory=list)

    # Version DAG
    version: int = 1
    origin: SkillOrigin = "captured"
    manual_protected: bool = False
    parent_skill_ids: list[str] = field(default_factory=list)
    content_snapshot: dict[str, str] = field(default_factory=dict)
    content_diff: Optional[str] = None
    change_summary: str = ""

    # State
    is_active: bool = True
    probation: bool = True

    # Version-scoped counters (used by all automated evolution gates)
    selections_since_version: int = 0
    applied_since_version: int = 0
    completions_since_version: int = 0
    fallbacks_since_version: int = 0

    # Lifetime counters (shown in /skill health, not used for gates)
    total_selections: int = 0
    total_applied: int = 0
    total_completions: int = 0
    total_fallbacks: int = 0

    last_used_at: Optional[str] = None

    pitfalls: list[Pitfall] = field(default_factory=list)
    recent_analyses: list[ExecutionAnalysis] = field(default_factory=list)

    created_at: str = ""
    updated_at: str = ""
    source_sessions: list[str] = field(default_factory=list)

    # Derived ratios (version-scoped, §3)
    @property
    def applied_rate(self) -> float:
        n = self.selections_since_version
        return self.applied_since_version / n if n else 0.0

    @property
    def completion_rate(self) -> float:
        n = self.applied_since_version
        return self.completions_since_version / n if n else 0.0

    @property
    def effective_rate(self) -> float:
        n = self.selections_since_version
        return self.completions_since_version / n if n else 0.0

    @property
    def fallback_rate(self) -> float:
        n = self.selections_since_version
        return self.fallbacks_since_version / n if n else 0.0


@dataclass
class OrphanPitfall:
    id: int
    description: str
    source_session: str
    intended_parent_hint: str
    context_text: str
    created_at: str
    status: str = "floating"
    pending_review_change_id: Optional[int] = None
    attached_to_skill_id: Optional[str] = None
    attached_at: Optional[str] = None
    mirrored_to_gap_id: Optional[int] = None


@dataclass
class SessionMeta:
    """Lightweight handle to a scanned .jsonl session file."""
    id: str
    path: str
    project: str
    # Source machine name in multi-machine aggregation (subdir of aggregation_root).
    # None in single-machine mode. Carried for audit/grouping; downstream v0.1 does
    # not gate on it.
    hostname: Optional[str] = None
    first_event_time: Optional[str] = None
    num_events: int = 0


def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_ts(raw: Optional[str]) -> Optional[str]:
    """Coerce any ISO-ish timestamp to our canonical format.

    Claude Code writes UTC-Z timestamps with millisecond precision
    (``2026-04-15T10:20:30.123Z``); `iso_now()` writes local-naive seconds
    (``2026-04-15T18:20:30``). Mixing them breaks ordinal string comparison,
    so every timestamp that enters the DB or a SkillRecord flows through here.

    Behavior: parse to datetime (handling the Z suffix and fractional seconds),
    drop the tz, truncate to seconds, re-emit. Unparseable input returns None
    so a downstream comparison fails loudly rather than silently ordering wrong.
    """
    if not raw:
        return None
    s = raw.strip()
    # Python 3.11+ fromisoformat accepts "Z" as +00:00 on 3.11+, but be
    # defensive for older inputs.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        # Strip tz by converting to naive local time. We explicitly lose the
        # tz here: downstream comparisons are all against local-naive values
        # produced by datetime.now().
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.isoformat(timespec="seconds")
