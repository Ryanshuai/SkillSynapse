"""Manual skill discovery (§5 Step 0).

Runs every night. Scans `~/.claude/skills/**/SKILL.md`, imports anything not
yet in the DB as origin=manual + manual_protected=True, and refreshes the DB
copy when a user edits a manual SKILL.md outside SkillSynapse.

Post-review fixes applied:
  - #A: flat layout — category comes from frontmatter only, NOT from the
    directory name. Missing category is stored as NULL → uncategorized.
  - #B: tool_dependencies falls back to `allowed-tools` (Hermes / Claude Code
    native field name).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import yaml

from .models import SkillRecord, normalize_ts
from .store import Store


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


@dataclass
class SkillMd:
    frontmatter: dict[str, Any]
    body: str


def parse_skill_md(path: Path) -> SkillMd:
    """Parse a SKILL.md file. Raises ValueError on missing frontmatter."""
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError(f"{path}: no YAML frontmatter found")
    fm_text, body = m.group(1), m.group(2)
    fm = yaml.safe_load(fm_text) or {}
    if not isinstance(fm, dict):
        raise ValueError(f"{path}: frontmatter is not a mapping")
    return SkillMd(frontmatter=fm, body=body)


def _resolve_tool_deps(frontmatter: dict) -> list[str]:
    """Post-review #B: accept both field names."""
    raw = (
        frontmatter.get("tool_dependencies")
        or frontmatter.get("allowed-tools")
        or []
    )
    if isinstance(raw, str):
        # "Read, Write, Bash" → list
        return [t.strip() for t in raw.split(",") if t.strip()]
    if isinstance(raw, list):
        return [str(t) for t in raw]
    return []


def _resolve_trigger(frontmatter: dict) -> Optional[list[str]]:
    raw = frontmatter.get("trigger")
    if raw is None:
        return None
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(t) for t in raw]
    return None


def _resolve_tags(frontmatter: dict) -> list[str]:
    raw = frontmatter.get("tags") or []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(t) for t in raw]
    return []


def _description_of(frontmatter: dict) -> str:
    desc = frontmatter.get("description", "")
    if isinstance(desc, str):
        return desc.strip()
    return str(desc)


GENERATED_BY_MARKER = "skillsynapse"


def _is_skillsynapse_generated(frontmatter: dict) -> bool:
    """True when frontmatter carries `generated_by: skillsynapse` — the marker
    indexer.write_skill_md stamps on every captured SKILL.md. Guarantees we
    never re-import our own output as a user-authored manual skill if the DB
    row is later pruned or the DB is rebuilt from scratch."""
    return frontmatter.get("generated_by") == GENERATED_BY_MARKER


def discover_manual_skills(store: Store, skills_root: Path) -> dict[str, int]:
    """Scan skills_root for SKILL.md files. Return counts.

    Flat layout — `SKILL.md` lives at `skills_root/<name>/SKILL.md`. We still
    `rglob` so deeper layouts (hub-style subfolders) keep working, but we never
    infer category from any parent directory name.

    Every manual SKILL.md present on disk is also recorded so we can
    deactivate DB rows whose file has been deleted (Step 0 incremental delete).
    """
    counts = {
        "imported": 0,
        "refreshed": 0,
        "skipped": 0,
        "failed": 0,
        "skipped_generated": 0,
        "deactivated": 0,
    }
    if not skills_root.exists():
        # Still reconcile DB → disk: if the root disappeared entirely, nothing
        # to deactivate either (user may be on a fresh machine). Return early.
        return counts

    seen_manual_names: set[str] = set()

    for skill_md in skills_root.rglob("SKILL.md"):
        try:
            parsed = parse_skill_md(skill_md)
        except Exception as e:
            store.log_decision("import_failed", details={"path": str(skill_md), "error": str(e)})
            counts["failed"] += 1
            continue

        # Skip SkillSynapse's own output — it's mirrored from the DB, not an
        # independent source of truth.
        if _is_skillsynapse_generated(parsed.frontmatter):
            counts["skipped_generated"] += 1
            continue

        name = parsed.frontmatter.get("name")
        if not name or not isinstance(name, str):
            store.log_decision("import_skipped_no_name", details={"path": str(skill_md)})
            counts["skipped"] += 1
            continue

        seen_manual_names.add(name)

        existing = store.get_skill_by_name(name)
        if existing is None:
            _import_as_manual(store, skill_md, parsed, name)
            counts["imported"] += 1
        elif existing.origin == "manual":
            if _file_newer_than_db(skill_md, existing):
                _refresh_manual_from_disk(store, existing, parsed, skill_md)
                counts["refreshed"] += 1
        # Non-manual with the same name: ignored by bootstrap. Collision is
        # handled at NEW time in the extractor (new_rejected_manual_collision).

    counts["deactivated"] = _deactivate_missing_manuals(store, seen_manual_names)
    return counts


def _file_newer_than_db(skill_md: Path, existing: SkillRecord) -> bool:
    """Compare file mtime against the DB's updated_at *as datetime objects*.

    Pure string comparison works only when every timestamp was produced on the
    same machine in the same tz. Cross-machine DB backups or DST transitions
    can flip ordinal string order even though the real mtimes are correctly
    ordered. Normalize both sides through `normalize_ts` (DB value) /
    `datetime.fromtimestamp` (file mtime) and compare as datetimes. If the DB
    value can't be parsed, refresh conservatively — user-edited files should
    win over an unparseable DB stamp."""
    try:
        file_dt = datetime.fromtimestamp(skill_md.stat().st_mtime).replace(microsecond=0)
    except OSError:
        return False
    db_normalized = normalize_ts(existing.updated_at)
    if not db_normalized:
        return True
    try:
        db_dt = datetime.fromisoformat(db_normalized)
    except ValueError:
        return True
    return file_dt > db_dt


def _deactivate_missing_manuals(store: Store, seen_names: set[str]) -> int:
    """Deactivate active `origin=manual` skills whose SKILL.md is no longer on
    disk. Explicit file removal is the user's retire signal for a manual skill."""
    deactivated = 0
    for skill in store.list_active_skills():
        if skill.origin != "manual":
            continue
        if skill.name in seen_names:
            continue
        store.deactivate_skill(
            skill.skill_id,
            action="manual_skill_deactivated_file_missing",
            details={"skill_id": skill.skill_id, "reason": "SKILL.md removed from disk"},
        )
        deactivated += 1
    return deactivated


def _import_as_manual(store: Store, skill_md: Path, parsed: SkillMd, name: str) -> None:
    category = parsed.frontmatter.get("category")
    if isinstance(category, str):
        category = category.strip() or None
    else:
        category = None

    if category and not store.has_category(category):
        store.insert_category(
            slug=category,
            description=f"[user-defined] {category}",
            manual_only=True,
        )

    stat = skill_md.stat()
    created_at = datetime.fromtimestamp(stat.st_ctime).isoformat(timespec="seconds")
    updated_at = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")

    tool_deps = _resolve_tool_deps(parsed.frontmatter)
    # Design §5 Step 0: manual skills ship with `critical_tools=[]`. Hermes /
    # Claude Code frontmatter has no such field, and "没它 skill 就废" can't be
    # inferred from the body. §5 Step 4.5's aggregate_tool_health() applies the
    # documented fallback `skill.critical_tools or skill.tool_dependencies`, so
    # an empty list still lets manual skills participate in Trigger 2 in v0.2.
    critical_tools: list[str] = []

    record = SkillRecord(
        skill_id=str(uuid4()),
        name=name,
        description=_description_of(parsed.frontmatter),
        category=category,
        content=parsed.body,
        origin="manual",
        manual_protected=True,
        version=1,
        probation=False,  # Manual skills ship production-ready.
        parent_skill_ids=[],
        content_snapshot={},
        change_summary="Discovered from existing SKILL.md",
        created_at=created_at,
        updated_at=updated_at,
        tool_dependencies=tool_deps,
        critical_tools=critical_tools,
        tags=_resolve_tags(parsed.frontmatter),
        trigger=_resolve_trigger(parsed.frontmatter),
        source_sessions=[],
        pitfalls=[],
        recent_analyses=[],
    )
    store.upsert_skill(record)
    store.log_decision(
        "manual_skill_discovered", skill=record, details={"path": str(skill_md)}
    )


def _refresh_manual_from_disk(
    store: Store, existing: SkillRecord, parsed: SkillMd, skill_md: Path
) -> None:
    updated_at = datetime.fromtimestamp(
        skill_md.stat().st_mtime
    ).isoformat(timespec="seconds")
    store.update_skill_content(
        existing.skill_id,
        content=parsed.body,
        description=_description_of(parsed.frontmatter) or existing.description,
        updated_at=updated_at,
    )
    store.log_decision(
        "manual_skill_refreshed_from_disk",
        skill=existing,
        details={"path": str(skill_md)},
    )
