"""Session → skill-candidate extractor (§5 Step 3).

Builds the v3.5 extractor prompt, injects the 15 preset categories and the
current hierarchy, calls the LLM, parses the JSON verdict. v0.1 is wired for
action ∈ {NEW, SKIP} only; UPDATE/PITFALL are accepted by the schema but
handled conservatively (treated as SKIP for now — evolution lands in v0.2).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from uuid import uuid4

from .config import Config
from .episode_detector import Episode, detect_episodes
from .llm_provider import LLMError, LLMProvider, RateLimitDeferred
from .models import SkillRecord, iso_now
from .sanitizer import scrub
from .scanner import (
    Event,
    first_user_text,
    last_assistant_text,
    parse_jsonl,
    session_error_rate,
    tool_call_stats,
)
from .store import Store


SYSTEM_PROMPT = (
    "You are SkillSynapse's extractor. You read a Claude Code session and "
    "decide whether it contains a reusable skill. Return ONLY a single JSON "
    "object. No prose, no markdown fences."
)


@dataclass
class ExtractedCandidate:
    action: str  # NEW | UPDATE | PITFALL | SKIP
    category: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    content: Optional[str] = None
    critical_tools: list[str] = field(default_factory=list)
    # Tools actually used in the session. Independent of critical_tools so
    # Trigger 2 has a non-empty set to read when the LLM returns no criticals.
    observed_tools: list[str] = field(default_factory=list)
    # UPDATE-only:
    target_skill: Optional[str] = None
    change: Optional[str] = None
    # PITFALL-only:
    intended_parent_hint: Optional[str] = None
    pitfall_text: Optional[str] = None
    context: Optional[str] = None
    # Session id so downstream can cite evidence
    session_id: Optional[str] = None
    # SKIP rationale tag
    skip_reason: Optional[str] = None


def _render_hierarchy(store: Store) -> str:
    cats = store.list_categories()
    active = store.list_active_skills()
    by_cat: dict[str, list[SkillRecord]] = {}
    for s in active:
        by_cat.setdefault(s.category or "uncategorized", []).append(s)

    lines: list[str] = []
    for cat in cats:
        slug = cat["slug"]
        skills = by_cat.get(slug, [])
        if not skills:
            continue
        lines.append(f"  {slug}:")
        for s in skills:
            desc = (s.description or "").splitlines()[0][:200]
            lines.append(f"    - {s.name}: {desc}")
    if by_cat.get("uncategorized"):
        lines.append("  uncategorized:")
        for s in by_cat["uncategorized"]:
            desc = (s.description or "").splitlines()[0][:200]
            lines.append(f"    - {s.name}: {desc}")
    if not lines:
        lines.append("  (hierarchy is empty)")
    return "\n".join(lines)


def _render_preset_categories(store: Store) -> str:
    cats = [c for c in store.list_categories() if not c["manual_only"]]
    return "\n".join(f"  {c['slug']:20s} {c['description']}" for c in cats)


def _build_session_brief(events: list[Event], max_calls: int = 60) -> str:
    """Compact one-page digest of the session for the LLM prompt."""
    total, diversity = tool_call_stats(events)
    err_rate = session_error_rate(events)
    fu = first_user_text(events)[:500]
    la = last_assistant_text(events)[:500]

    brief_tools: list[str] = []
    for ev in events:
        if ev.type != "tool_use":
            continue
        n = ev.tool_name or "?"
        inp = ev.tool_input or {}
        if n == "Bash":
            cmd = str(inp.get("command", ""))[:200]
            desc = str(inp.get("description", ""))[:80]
            brief_tools.append(f"Bash[{desc}]: {cmd}")
        elif n in ("Edit", "Write", "Read"):
            fp = str(inp.get("file_path", ""))[-80:]
            brief_tools.append(f"{n}: {fp}")
        elif n == "Grep":
            brief_tools.append(f"Grep: {str(inp.get('pattern', ''))[:80]}")
        elif n == "Glob":
            brief_tools.append(f"Glob: {str(inp.get('pattern', ''))[:80]}")
        elif n == "Skill":
            brief_tools.append(f"Skill[{inp.get('skill', '?')}]")
        elif n == "Agent":
            brief_tools.append(f"Agent[{inp.get('subagent_type', '?')}]: {str(inp.get('description', ''))[:80]}")
        else:
            brief_tools.append(f"{n}: {str(inp)[:120]}")
        if len(brief_tools) >= max_calls:
            break

    # Scrub the assembled brief once: credentials typed in commands or pasted
    # into chat must never reach the LLM prompt.
    return scrub(
        f"SESSION STATS: {total} tool calls, {diversity} distinct tools, "
        f"error_rate={err_rate:.2f}\n\n"
        f"FIRST USER MESSAGE:\n{fu}\n\n"
        f"TOOL CALL SEQUENCE (truncated):\n" + "\n".join(brief_tools) + "\n\n"
        f"LAST ASSISTANT MESSAGE:\n{la}\n"
    )


def build_extraction_prompt(session_brief: str, hierarchy: str,
                            preset_categories: str) -> str:
    return f"""\
PRESET CATEGORIES (the ONLY categories you may use as `category`):

{preset_categories}

EXISTING SKILLS in hierarchy:

{hierarchy}

── SESSION ──

{session_brief}

── TASK ──

Analyze this session and pick ONE:

  NEW      — a reusable workflow not covered by any existing skill
  UPDATE   — an existing skill has a bug, missing step, or a better alternative
  PITFALL  — a failure mode worth attaching to an existing skill
  SKIP     — nothing reusable

DEDUP FIRST: before choosing NEW, scan EXISTING SKILLS above. If your candidate
is the SAME underlying capability as one that already exists — even under a
different name or with different specifics (a different board/camera/dataset) —
do NOT mint a near-duplicate. Choose UPDATE (add the missing step/variant) or
PITFALL, or narrow the NEW to only what is genuinely uncovered.

PRE-SKIP RULES (any match → SKIP, do not force):

  a. MULTI_TOPIC_DRIFT: the session touches >3 unrelated concerns
     (different subsystems, APIs, or root dirs) → SKIP.
  b. AD_HOC_DEBUG: the workflow is exploratory debugging / trial-and-error
     with no clear repeatable step sequence → SKIP.
  c. SESSION_FAILED: >40% error rate or ended blocked/failed → SKIP
     (the pipeline will log this as a coverage gap).

If NEW, you MUST produce:

  category:       MUST be one of the preset slugs above. No new categories.
                  If none fit, pick the closest and mention the mismatch
                  inside `content`.
  name:           slug, ≤64 chars, unique in the hierarchy.
  description:    one line, ≤1024 chars, verb-first. Answers "when would I use
                  this?", not "how does it work?". Describe the GENERAL, reusable
                  capability — NOT the one specific instance you happened to see.
                  HARD RULE — no project-specific proper nouns in `name` or
                  `description`: no source file names (rig.json, gt_pipeline), no
                  dataset/scene IDs (scene_042, eth3d), no hardware model numbers
                  (Gemini 335Le, Jetson Orin). Those go in `content` as examples.
                  Scope facts (language, OS, scale, input/output kind) are good;
                  accidental sources are not.
                    bad : "Refine a box GT pose against its SfM cloud for the
                           Gemini 335Le rig using scene_XXX/rig.json"
                    good: "Refine a fitted 3D cuboid pose against an SfM point
                           cloud when one face is under-constrained"
                  Optional "not ..." clause is fine if prone to keyword mis-match.
  content:        full SKILL.md body (steps, commands, pitfalls).
  critical_tools: 1–3 tools without which the skill is useless. OK empty.

If UPDATE, include `target_skill` and `change` describing what to fix.
If PITFALL, include `intended_parent_hint` (even if that skill doesn't exist
yet), `pitfall_text`, and short `context`.

── OUTPUT FORMAT ──

Return ONE JSON object, no markdown fences, no commentary. Example schema:

{{
  "action": "NEW" | "UPDATE" | "PITFALL" | "SKIP",
  "skip_reason": "MULTI_TOPIC_DRIFT" | "AD_HOC_DEBUG" | "SESSION_FAILED" | null,
  "category": "...",
  "name": "...",
  "description": "...",
  "content": "...",
  "critical_tools": ["..."],
  "target_skill": "...",
  "change": "...",
  "intended_parent_hint": "...",
  "pitfall_text": "...",
  "context": "..."
}}
"""


_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _coerce_candidate(
    raw: dict, session_id: str, observed_tools: list[str]
) -> ExtractedCandidate:
    action = str(raw.get("action", "SKIP")).upper().strip()
    if action not in ("NEW", "UPDATE", "PITFALL", "SKIP"):
        action = "SKIP"
    tools = raw.get("critical_tools") or []
    if isinstance(tools, str):
        tools = [tools]
    return ExtractedCandidate(
        action=action,
        category=(raw.get("category") or None),
        name=(raw.get("name") or None),
        description=(raw.get("description") or None),
        content=(raw.get("content") or None),
        critical_tools=[str(t) for t in tools if t],
        observed_tools=observed_tools,
        target_skill=(raw.get("target_skill") or None),
        change=(raw.get("change") or None),
        intended_parent_hint=(raw.get("intended_parent_hint") or None),
        pitfall_text=(raw.get("pitfall_text") or None),
        context=(raw.get("context") or None),
        session_id=session_id,
        skip_reason=(raw.get("skip_reason") or None),
    )


def _collect_observed_tools(events: list[Event]) -> list[str]:
    """Distinct tool_use names seen in the session, in first-seen order.
    Used as the default tool_dependencies for captured skills — keeps Trigger 2
    useful even when the LLM returns critical_tools=[]."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for ev in events:
        if ev.type == "tool_use" and ev.tool_name and ev.tool_name not in seen_set:
            seen.append(ev.tool_name)
            seen_set.add(ev.tool_name)
    return seen


def _parse_llm_json(text: str) -> Optional[dict]:
    """LLM output sometimes arrives with a stray markdown fence. Be tolerant."""
    text = text.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```\s*$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return obj if isinstance(obj, dict) else None


def extract_from_events(
    events: list, session_id: str,
    store: Store, llm: LLMProvider, cfg: Config,
    *, episode_id: Optional[str] = None,
) -> Optional[ExtractedCandidate]:
    """Run the extractor on a slice of events (a session or a single episode).

    Returns
    -------
    Optional[ExtractedCandidate]
        None if events fall below the complexity threshold (silent filter miss,
        bucketed as `no_candidate` by the caller).

    Raises
    ------
    LLMError
        On LLM subprocess failure OR unparseable LLM output. The precise
        cause is recorded in decisions.jsonl as `extractor_llm_error` or
        `extractor_parse_error`. Caller buckets as `llm_error`.
    RateLimitDeferred
        On upstream throttling. Caller should break the per-run loop.
    """
    total, diversity = tool_call_stats(events)
    if total < cfg.extraction.min_tool_calls:
        return None
    if diversity < cfg.extraction.min_tool_diversity:
        return None

    # decisions.jsonl keeps session-level granularity for backward
    # compatibility; episode_id goes into `details` when present.
    brief = _build_session_brief(events)
    hierarchy = _render_hierarchy(store)
    presets = _render_preset_categories(store)
    prompt = build_extraction_prompt(brief, hierarchy, presets)

    try:
        out = llm.call(prompt, system=SYSTEM_PROMPT)
    except RateLimitDeferred:
        raise
    except LLMError as e:
        details = {"error": str(e)[:300]}
        if episode_id is not None:
            details["episode_id"] = episode_id
        store.log_decision(
            "extractor_llm_error",
            source_session=session_id,
            details=details,
        )
        # Re-raise so main.py can distinguish an LLM failure from a silent
        # complexity-filter miss (both previously returned None and were
        # lumped into `no_candidate`, hiding subprocess/env bugs).
        raise

    raw = _parse_llm_json(out)
    if raw is None:
        details = {"raw": out[:500]}
        if episode_id is not None:
            details["episode_id"] = episode_id
        store.log_decision(
            "extractor_parse_error",
            source_session=session_id,
            details=details,
        )
        # Same reasoning as LLMError — raise a distinct signal so parse
        # failures are counted separately in extract_stats.
        raise LLMError(
            f"parse error for session {session_id}"
            + (f" episode {episode_id}" if episode_id else "")
            + ": LLM returned unparseable output"
        )

    observed = _collect_observed_tools(events)
    return _coerce_candidate(raw, session_id, observed)


def extract_from_session(
    session_path: Path, store: Store, llm: LLMProvider, cfg: Config
) -> tuple[list[ExtractedCandidate], dict[str, int]]:
    """Run the extractor on one session.

    Returns
    -------
    tuple[list[ExtractedCandidate], dict[str, int]]
        (candidates, stats) where stats counts `no_candidate` and
        `llm_error` events at the episode level. `llm_error` is caught
        per-episode so a single failing LLM call doesn't lose the
        other episodes' candidates.

    Raises
    ------
    RateLimitDeferred
        Propagated untouched — caller should break the run loop.
    """
    events = parse_jsonl(session_path)
    session_id = session_path.stem

    # Episode detection: opt-in via config, default on for v0.2.
    # When disabled, the whole session is treated as one episode for
    # backward compatibility with v0.1 behavior.
    ep_cfg = cfg.raw.get("episode_detection", {})
    if ep_cfg.get("enabled", False):
        episodes = detect_episodes(
            session_id,
            events,
            gap_minutes=int(ep_cfg.get("gap_minutes", 30)),
            min_tool_overlap=int(ep_cfg.get("min_tool_overlap", 0)),
            window=int(ep_cfg.get("window", 5)),
        )
    else:
        # v0.1-compatible path: synthesize a single episode wrapping all events.
        episodes = [Episode(
            session_id=session_id, episode_idx=0, events=events,
            start_time=None, end_time=None, boundary_reason="disabled",
        )]

    candidates: list[ExtractedCandidate] = []
    stats = {"no_candidate": 0, "llm_error": 0}

    for ep in episodes:
        try:
            cand = extract_from_events(
                ep.events, session_id, store, llm, cfg,
                episode_id=ep.episode_id,
            )
        except RateLimitDeferred:
            raise
        except LLMError:
            # Already logged to decisions.jsonl by extract_from_events.
            stats["llm_error"] += 1
            continue

        if cand is None:
            stats["no_candidate"] += 1
            continue

        candidates.append(cand)

    return candidates, stats


def log_non_new_action(candidate: ExtractedCandidate, store: Store) -> None:
    """Emit the decisions.jsonl entry for SKIP / UPDATE / PITFALL candidates.

    v0.1 doesn't execute UPDATE/PITFALL (v0.2's evolver will); SKIP is purely
    the LLM telling us there's nothing reusable. All three are audit-only, so
    they live outside `realize_candidate` which is strictly NEW → SkillRecord.
    """
    if candidate.action == "SKIP":
        store.log_decision(
            "extractor_skipped",
            source_session=candidate.session_id,
            details={"reason": candidate.skip_reason or "unspecified"},
        )
    elif candidate.action == "UPDATE":
        store.log_decision(
            "extractor_update_deferred_v02",
            source_session=candidate.session_id,
            details={
                "target_skill": candidate.target_skill,
                "change": scrub((candidate.change or "")[:300]),
            },
        )
    elif candidate.action == "PITFALL":
        store.log_decision(
            "extractor_pitfall_deferred_v02",
            source_session=candidate.session_id,
            details={
                "intended_parent_hint": candidate.intended_parent_hint,
                "pitfall_text": scrub((candidate.pitfall_text or "")[:300]),
            },
        )
    else:
        # Unknown action — shouldn't happen given _coerce_candidate, but log
        # so audits show the anomaly instead of silently swallowing it.
        store.log_decision(
            "extractor_unknown_action",
            source_session=candidate.session_id,
            details={"action": candidate.action},
        )


def realize_candidate(
    candidate: ExtractedCandidate, store: Store, cfg: Config
) -> Optional[SkillRecord]:
    """Turn a NEW candidate into a SkillRecord row. Returns None when rejected
    (bad name, bad category, manual collision, name-exists, incomplete payload).

    Precondition: `candidate.action == "NEW"`. Non-NEW actions are handled by
    `log_non_new_action` — the main loop dispatches, this function only writes.
    """
    if candidate.action != "NEW":
        # Defensive: earlier refactors had realize_candidate handle every
        # action. Keep a guard so a future regression surfaces as an audit
        # entry rather than a silent no-op.
        store.log_decision(
            "extractor_realize_called_non_new",
            source_session=candidate.session_id,
            details={"action": candidate.action},
        )
        return None

    name = (candidate.name or "").strip()
    if not _NAME_RE.match(name):
        store.log_decision(
            "new_rejected_bad_name",
            source_session=candidate.session_id,
            details={"name": name},
        )
        return None

    # v3.5: name collision with a manual_protected skill → SKIP.
    existing = store.get_skill_by_name(name, active_only=False)
    if existing and existing.manual_protected:
        store.log_decision(
            "new_rejected_manual_collision",
            skill=existing,
            source_session=candidate.session_id,
            details={"candidate_name": name},
        )
        return None
    if existing:
        # Collision with a non-manual skill: v0.1 doesn't do UPDATE yet,
        # so we just skip to avoid silently overwriting.
        store.log_decision(
            "new_rejected_name_exists",
            skill=existing,
            source_session=candidate.session_id,
            details={"candidate_name": name},
        )
        return None

    cat = (candidate.category or "").strip()
    preset_slugs = store.preset_category_slugs()
    if cat not in preset_slugs:
        store.log_decision(
            "new_rejected_bad_category",
            source_session=candidate.session_id,
            details={"candidate_name": name, "category": cat},
        )
        return None

    if not candidate.content or not candidate.description:
        store.log_decision(
            "new_rejected_incomplete",
            source_session=candidate.session_id,
            details={"candidate_name": name},
        )
        return None

    now = iso_now()
    # Invariant: critical_tools ⊆ tool_dependencies. The prompt only asks the
    # LLM for critical_tools (keeps output small) — tool_dependencies is the
    # union of LLM criticals and every tool actually invoked in the session.
    # This matters when the LLM returns critical=[]; without observed_tools
    # the skill would ship with an empty tool set and Trigger 2 / Hermes tool
    # gating would pass every caller through unconditionally.
    critical = list(candidate.critical_tools)
    tool_deps_ordered: list[str] = []
    seen_deps: set[str] = set()
    for t in critical + list(candidate.observed_tools):
        if t and t not in seen_deps:
            tool_deps_ordered.append(t)
            seen_deps.add(t)
    # Second scrub before persisting: the brief fed to the LLM is already
    # clean, but the model can still echo credentials it saw elsewhere in
    # context into the skill body — and skill files get published/synced.
    skill = SkillRecord(
        skill_id=str(uuid4()),
        name=name,
        description=scrub((candidate.description or "").strip()[:1024]),
        category=cat,
        content=scrub(candidate.content),
        origin="captured",
        manual_protected=False,
        version=1,
        probation=True,
        parent_skill_ids=[],
        content_snapshot={},
        change_summary=f"Captured from session {candidate.session_id}",
        created_at=now,
        updated_at=now,
        tool_dependencies=tool_deps_ordered,
        critical_tools=critical,
        tags=[],
        trigger=None,
        source_sessions=[candidate.session_id] if candidate.session_id else [],
        pitfalls=[],
        recent_analyses=[],
    )
    store.upsert_skill(skill)
    store.log_decision(
        "created",
        skill=skill,
        source_session=candidate.session_id,
        details={"category": cat},
    )
    return skill
