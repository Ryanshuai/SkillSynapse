"""SQLite + SKILL.md file management.

Implements all 8 tables from §10 so v0.2 won't need migrations. v0.1 only reads
/writes `skills`, `categories`, and `decisions`; the other 5 tables are created
empty and exercised by later versions.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from .models import (
    ExecutionAnalysis,
    Pitfall,
    SkillRecord,
    iso_now,
)


_logger = logging.getLogger(__name__)


# 15 preset categories. Matches §10 of the design doc and the extractor prompt.
PRESET_CATEGORIES: list[tuple[str, str]] = [
    ("devops",             "容器构建、部署、灰度发布"),
    ("ml-experiments",     "模型训练/推理/评估生命周期"),
    ("ml-data-prep",       "数据集处理、标注、背景图生成"),
    ("codebase-analysis",  "理解一个 repo，或规划 refactor"),
    ("refactoring",        "具体的代码改造执行"),
    ("data-exploration",   "从 S3/DB/API 定位和下载数据"),
    ("python-testing",     "pytest / pdb / debugging"),
    ("skill-authoring",    "写 skill 本身相关的元操作"),
    ("embedded",           "MCU / 固件 / 硬件相关"),
    ("smart-home",         "Home Assistant / Zigbee / Z2M"),
    ("research-reading",   "论文 / arxiv / PDF 提取"),
    ("remote-ops",         "SSH / VPN / tmux / 远程训练监控"),
    ("git-ops",            "复杂 git workflow (rebase/worktree/submodule)"),
    ("ide-integration",    "编辑器配置、快捷键、扩展"),
    ("prompt-engineering", "LLM prompt 工程"),
]


SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    skill_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    category TEXT,
    content TEXT,
    compressed_content TEXT,
    trigger TEXT,
    tags TEXT,
    version INTEGER,
    origin TEXT,
    manual_protected INTEGER DEFAULT 0,
    parent_skill_ids TEXT,
    content_snapshot TEXT,
    content_diff TEXT,
    change_summary TEXT,
    is_active INTEGER DEFAULT 1,
    probation INTEGER DEFAULT 1,
    selections_since_version INTEGER DEFAULT 0,
    applied_since_version INTEGER DEFAULT 0,
    completions_since_version INTEGER DEFAULT 0,
    fallbacks_since_version INTEGER DEFAULT 0,
    total_selections INTEGER DEFAULT 0,
    total_applied INTEGER DEFAULT 0,
    total_completions INTEGER DEFAULT 0,
    total_fallbacks INTEGER DEFAULT 0,
    last_used_at TEXT,
    pitfalls TEXT,
    recent_analyses TEXT,
    created_at TEXT,
    updated_at TEXT,
    tool_dependencies TEXT,
    critical_tools TEXT,
    source_sessions TEXT
);
CREATE INDEX IF NOT EXISTS idx_skills_name ON skills(name);
CREATE INDEX IF NOT EXISTS idx_skills_category ON skills(category);
CREATE INDEX IF NOT EXISTS idx_skills_active ON skills(is_active);

CREATE TABLE IF NOT EXISTS categories (
    slug TEXT PRIMARY KEY,
    description TEXT,
    manual_only INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT,
    action TEXT,
    skill_id TEXT,
    skill_name TEXT,
    details TEXT,
    source_session TEXT
);

CREATE TABLE IF NOT EXISTS coverage_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT,
    session_summary TEXT,
    project_path TEXT,
    resolved INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS addressed_degradations (
    tool_name TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    addressed_at TEXT NOT NULL,
    reason TEXT,
    PRIMARY KEY (tool_name, skill_id)
);
CREATE INDEX IF NOT EXISTS idx_addressed_tool ON addressed_degradations(tool_name);
CREATE INDEX IF NOT EXISTS idx_addressed_at ON addressed_degradations(addressed_at);

CREATE TABLE IF NOT EXISTS orphan_pitfalls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    source_session TEXT NOT NULL,
    intended_parent_hint TEXT NOT NULL,
    context_text TEXT,
    created_at TEXT NOT NULL,
    status TEXT DEFAULT 'floating',
    pending_review_change_id INTEGER,
    attached_to_skill_id TEXT,
    attached_at TEXT,
    mirrored_to_gap_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_orphan_hint ON orphan_pitfalls(intended_parent_hint);

CREATE TABLE IF NOT EXISTS pending_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    proposed_content TEXT NOT NULL,
    proposed_diff TEXT NOT NULL,
    reason TEXT NOT NULL,
    source_session TEXT,
    proposed_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    reviewed_at TEXT,
    review_note TEXT,
    cleanup_anti_loop_tool TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_changes(status);
CREATE INDEX IF NOT EXISTS idx_pending_skill ON pending_changes(skill_id);
"""

# FTS5 is optional — some Python/sqlite builds ship without it. v0.1 doesn't
# read/write this table; v0.2's /skill recall does. Create it opportunistically
# and log + continue if the module is missing.
_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS session_index USING fts5(
    session_id UNINDEXED,
    project,
    timestamp UNINDEXED,
    first_user,
    last_assistant,
    tool_use_summary,
    topics,
    outcome UNINDEXED,
    tokenize = 'unicode61'
);
"""


# ── Serialization helpers ───────────────────────────────────────

def _to_json_list(value: Any) -> str:
    return json.dumps(value or [], ensure_ascii=False)


def _from_json_list(text: Optional[str]) -> list:
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # Corrupted row masking as "empty" hides data issues during audit.
        _logger.warning("corrupted json list in DB row (%s); treating as empty: %.120s", e, text)
        return []


def _row_to_skill(row: sqlite3.Row) -> SkillRecord:
    return SkillRecord(
        skill_id=row["skill_id"],
        name=row["name"],
        description=row["description"] or "",
        category=row["category"],
        content=row["content"] or "",
        compressed_content=row["compressed_content"],
        trigger=_from_json_list(row["trigger"]) or None,
        tags=_from_json_list(row["tags"]),
        version=row["version"] or 1,
        origin=row["origin"] or "captured",
        manual_protected=bool(row["manual_protected"]),
        parent_skill_ids=_from_json_list(row["parent_skill_ids"]),
        content_snapshot=json.loads(row["content_snapshot"] or "{}"),
        content_diff=row["content_diff"],
        change_summary=row["change_summary"] or "",
        is_active=bool(row["is_active"]),
        probation=bool(row["probation"]),
        selections_since_version=row["selections_since_version"] or 0,
        applied_since_version=row["applied_since_version"] or 0,
        completions_since_version=row["completions_since_version"] or 0,
        fallbacks_since_version=row["fallbacks_since_version"] or 0,
        total_selections=row["total_selections"] or 0,
        total_applied=row["total_applied"] or 0,
        total_completions=row["total_completions"] or 0,
        total_fallbacks=row["total_fallbacks"] or 0,
        last_used_at=row["last_used_at"],
        pitfalls=[Pitfall(**p) for p in _from_json_list(row["pitfalls"])],
        recent_analyses=[
            ExecutionAnalysis(**a) for a in _from_json_list(row["recent_analyses"])
        ],
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
        tool_dependencies=_from_json_list(row["tool_dependencies"]),
        critical_tools=_from_json_list(row["critical_tools"]),
        source_sessions=_from_json_list(row["source_sessions"]),
    )


# ── Store ───────────────────────────────────────────────────────

class Store:
    def __init__(self, db_path: Path, decisions_log: Path):
        self.db_path = db_path
        self.decisions_log = decisions_log
        db_path.parent.mkdir(parents=True, exist_ok=True)
        decisions_log.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        self._seed_categories()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc):
        self.close()

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        # FTS5 is optional; swallow "no such module: fts5" so v0.1 still runs.
        try:
            self.conn.executescript(_FTS_SCHEMA)
            self.has_fts5 = True
        except sqlite3.OperationalError as e:
            if "fts5" in str(e).lower():
                self.has_fts5 = False
            else:
                raise
        self.conn.commit()

    def _seed_categories(self) -> None:
        cur = self.conn.execute("SELECT COUNT(*) FROM categories WHERE manual_only=0")
        if cur.fetchone()[0] > 0:
            return
        now = iso_now()
        self.conn.executemany(
            "INSERT OR IGNORE INTO categories (slug, description, manual_only, "
            "created_at, updated_at) VALUES (?, ?, 0, ?, ?)",
            [(slug, desc, now, now) for slug, desc in PRESET_CATEGORIES],
        )
        self.conn.commit()

    # ── categories ────────────────────────────

    def has_category(self, slug: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM categories WHERE slug=?", (slug,))
        return cur.fetchone() is not None

    def insert_category(self, slug: str, description: str, manual_only: bool = False) -> None:
        now = iso_now()
        self.conn.execute(
            "INSERT OR IGNORE INTO categories (slug, description, manual_only, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (slug, description, int(manual_only), now, now),
        )
        self.conn.commit()

    def list_categories(self) -> list[dict]:
        cur = self.conn.execute(
            "SELECT slug, description, manual_only FROM categories ORDER BY manual_only, slug"
        )
        return [dict(r) for r in cur.fetchall()]

    def preset_category_slugs(self) -> set[str]:
        cur = self.conn.execute("SELECT slug FROM categories WHERE manual_only=0")
        return {r["slug"] for r in cur.fetchall()}

    # ── skills ────────────────────────────────

    def get_skill(self, skill_id: str) -> Optional[SkillRecord]:
        cur = self.conn.execute("SELECT * FROM skills WHERE skill_id=?", (skill_id,))
        row = cur.fetchone()
        return _row_to_skill(row) if row else None

    def get_skill_by_name(self, name: str, active_only: bool = True) -> Optional[SkillRecord]:
        sql = "SELECT * FROM skills WHERE name=?"
        if active_only:
            sql += " AND is_active=1"
        sql += " ORDER BY version DESC LIMIT 1"
        cur = self.conn.execute(sql, (name,))
        row = cur.fetchone()
        return _row_to_skill(row) if row else None

    def list_active_skills(self) -> list[SkillRecord]:
        cur = self.conn.execute("SELECT * FROM skills WHERE is_active=1 ORDER BY name")
        return [_row_to_skill(r) for r in cur.fetchall()]

    # Counters for the /skill health view. v0.2 will add more read paths here
    # so commands.py never has to poke conn directly.

    def count_pending_reviews(self) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM pending_changes WHERE status='pending'"
        )
        return int(cur.fetchone()[0])

    def count_floating_orphans(self) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM orphan_pitfalls WHERE status='floating'"
        )
        return int(cur.fetchone()[0])

    def count_open_coverage_gaps(self) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM coverage_gaps WHERE resolved=0"
        )
        return int(cur.fetchone()[0])

    def upsert_skill(self, s: SkillRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO skills (
                skill_id, name, description, category, content, compressed_content,
                trigger, tags, version, origin, manual_protected, parent_skill_ids,
                content_snapshot, content_diff, change_summary, is_active, probation,
                selections_since_version, applied_since_version,
                completions_since_version, fallbacks_since_version,
                total_selections, total_applied, total_completions, total_fallbacks,
                last_used_at, pitfalls, recent_analyses, created_at, updated_at,
                tool_dependencies, critical_tools, source_sessions
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(skill_id) DO UPDATE SET
                name=excluded.name,
                description=excluded.description,
                category=excluded.category,
                content=excluded.content,
                compressed_content=excluded.compressed_content,
                trigger=excluded.trigger,
                tags=excluded.tags,
                version=excluded.version,
                origin=excluded.origin,
                manual_protected=excluded.manual_protected,
                parent_skill_ids=excluded.parent_skill_ids,
                content_snapshot=excluded.content_snapshot,
                content_diff=excluded.content_diff,
                change_summary=excluded.change_summary,
                is_active=excluded.is_active,
                probation=excluded.probation,
                selections_since_version=excluded.selections_since_version,
                applied_since_version=excluded.applied_since_version,
                completions_since_version=excluded.completions_since_version,
                fallbacks_since_version=excluded.fallbacks_since_version,
                total_selections=excluded.total_selections,
                total_applied=excluded.total_applied,
                total_completions=excluded.total_completions,
                total_fallbacks=excluded.total_fallbacks,
                last_used_at=excluded.last_used_at,
                pitfalls=excluded.pitfalls,
                recent_analyses=excluded.recent_analyses,
                updated_at=excluded.updated_at,
                tool_dependencies=excluded.tool_dependencies,
                critical_tools=excluded.critical_tools,
                source_sessions=excluded.source_sessions
            """,
            (
                s.skill_id, s.name, s.description, s.category, s.content,
                s.compressed_content,
                _to_json_list(s.trigger), _to_json_list(s.tags),
                s.version, s.origin, int(s.manual_protected),
                _to_json_list(s.parent_skill_ids),
                json.dumps(s.content_snapshot, ensure_ascii=False),
                s.content_diff, s.change_summary,
                int(s.is_active), int(s.probation),
                s.selections_since_version, s.applied_since_version,
                s.completions_since_version, s.fallbacks_since_version,
                s.total_selections, s.total_applied,
                s.total_completions, s.total_fallbacks,
                s.last_used_at,
                _to_json_list([asdict(p) for p in s.pitfalls]),
                _to_json_list([asdict(a) for a in s.recent_analyses]),
                s.created_at or iso_now(),
                s.updated_at or iso_now(),
                _to_json_list(s.tool_dependencies),
                _to_json_list(s.critical_tools),
                _to_json_list(s.source_sessions),
            ),
        )
        self.conn.commit()

    def update_skill_content(self, skill_id: str, *, content: str,
                             description: Optional[str] = None,
                             updated_at: Optional[str] = None) -> None:
        fields = ["content=?", "updated_at=?"]
        args: list[Any] = [content, updated_at or iso_now()]
        if description is not None:
            fields.append("description=?")
            args.append(description)
        args.append(skill_id)
        self.conn.execute(
            f"UPDATE skills SET {', '.join(fields)} WHERE skill_id=?", args
        )
        self.conn.commit()

    def deactivate_skill(self, skill_id: str, *, action: str,
                         details: Optional[dict] = None) -> None:
        """Flip is_active=0 and emit a decision. v0.1 uses this only for the
        Step 0 "manual SKILL.md was removed by the user" path, which is a
        user-authored signal to retire the skill. The design's stricter
        `deactivate_skill()` gate (§4.3, manual_protected + no successor →
        ManualProtected) lands with prune/split/merge in v0.2.

        `action` must be one of the decisions.jsonl action enums (§10) — the
        caller owns the audit vocabulary, not this helper."""
        skill = self.get_skill(skill_id)
        if skill is None:
            # No matching row: there is nothing to deactivate. Do NOT emit a
            # phantom UPDATE + decision — that would pollute the audit trail
            # with rows that claim to retire a skill_id that never existed.
            _logger.warning(
                "deactivate_skill: skill_id=%s not found; no-op", skill_id
            )
            return

        ts = iso_now()
        payload = json.dumps(details or {}, ensure_ascii=False)
        # Atomic: UPDATE + decisions insert land together or not at all.
        # Without this wrapper a crash between the two statements would leave
        # `is_active=0` in skills with no matching audit entry.
        with self.conn:
            self.conn.execute(
                "UPDATE skills SET is_active=0, updated_at=? WHERE skill_id=?",
                (ts, skill_id),
            )
            self.conn.execute(
                "INSERT INTO decisions (timestamp, action, skill_id, skill_name, "
                "details, source_session) VALUES (?, ?, ?, ?, ?, ?)",
                (ts, action, skill.skill_id, skill.name, payload, None),
            )

        # jsonl mirror lives outside the DB transaction (§10 audit note: the
        # DB row is the source of truth; jsonl is a convenience for grep).
        line = {
            "timestamp": ts,
            "action": action,
            "skill_id": skill.skill_id,
            "skill_name": skill.name,
            "details": details or {},
            "source_session": None,
        }
        try:
            with self.decisions_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError as e:
            _logger.error(
                "failed to append to decisions.jsonl (%s): %s", self.decisions_log, e
            )

    # ── decisions log ─────────────────────────

    def log_decision(self, action: str, *, skill: Optional[SkillRecord] = None,
                     details: Optional[dict] = None,
                     source_session: Optional[str] = None,
                     skill_id: Optional[str] = None,
                     skill_name: Optional[str] = None) -> None:
        ts = iso_now()
        sid = skill_id if skill_id is not None else (skill.skill_id if skill else None)
        sname = skill_name if skill_name is not None else (skill.name if skill else None)
        payload = json.dumps(details or {}, ensure_ascii=False)

        self.conn.execute(
            "INSERT INTO decisions (timestamp, action, skill_id, skill_name, details, "
            "source_session) VALUES (?, ?, ?, ?, ?, ?)",
            (ts, action, sid, sname, payload, source_session),
        )
        self.conn.commit()

        # Also write to jsonl for easy grep. The DB row is the source of truth
        # — if this file-side append fails (disk full, permission flip), log
        # and continue so a single log-sink hiccup can't kill the whole run.
        # The two sinks can diverge by one line; acceptable v0.1 trade-off.
        line = {
            "timestamp": ts,
            "action": action,
            "skill_id": sid,
            "skill_name": sname,
            "details": details or {},
            "source_session": source_session,
        }
        try:
            with self.decisions_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError as e:
            _logger.error(
                "failed to append to decisions.jsonl (%s): %s", self.decisions_log, e
            )


# ── Atomic SKILL.md write (§11.3) ───────────────────────────────

def atomic_write_text(target: Path, text: str) -> None:
    """Write `text` to `target` atomically via tempfile + os.replace."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
