"""Resolve divergence in the shared skill library by merging both versions with an
LLM instead of picking a winner.

Why this exists: `skills-canon` is a bidirectional syncthing folder across
home-desktop / company-laptop / mac-mini. When the same SKILL.md is edited on two
machines, syncthing does not merge — it keeps one version and drops the other next
to it as `SKILL.sync-conflict-<date>-<time>-<device>.md`. Left alone, whichever
machine happened to write last silently wins and the other machine's work becomes
a file nobody ever opens.

Both versions are real work; the job is to take what each got right and drop what
it got wrong, then write the result back to the canonical name — from where
syncthing distributes it to all three machines on its own. Distribution is not
this module's job, it is its side effect.

**Divergence reaches us two ways, and watching only the first one misses work:**

  1. `<stem>.sync-conflict-<date>-<time>-<device>.<ext>` — two machines edited the
     same file inside one sync window. syncthing refuses to choose and keeps both.
     This is the unambiguous signal, and the only case where both versions are on
     disk at once.

  2. A tracked SKILL.md whose working tree copy *deletes* lines relative to git
     HEAD. Here syncthing already chose — last writer won, whole-file, without
     looking at content — so one machine's edit may have been silently erased.
     There is no conflict file to find; the previous merge commit is the only
     surviving record of what was lost. Net deletion is what that looks like in a
     diff, so that is what we hand to the model. Pure additions need no arbitration.

Ordering note: this runs on the hub, and the hub is the only machine where the
skill tree is also a git working tree. Merging here (rather than on whichever
machine noticed the conflict) is what makes every merge a reviewable commit.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:          # Windows: no flock, and no daemons there either
    fcntl = None             # type: ignore[assignment]

import yaml

from .config import Config, load_defaults, resolve_paths
from .llm_provider import LLMError, LLMProvider, RateLimitDeferred
from .sanitizer import scrub

log = logging.getLogger(__name__)

# syncthing names conflicts `<stem>.sync-conflict-<YYYYMMDD>-<HHMMSS>-<DEVICE>.<ext>`
_CONFLICT_RE = re.compile(
    r"^(?P<stem>.+)\.sync-conflict-"
    r"(?P<date>\d{8})-(?P<time>\d{6})-(?P<device>[A-Z0-9]+)"
    r"(?P<ext>\.[^.]*)?$"
)

# A fenced block the model wrapped the whole file in, which would otherwise be
# written into the skill verbatim.
_FENCE_RE = re.compile(r"\A\s*```[a-zA-Z]*\n(?P<body>.*)\n```\s*\Z", re.DOTALL)

_FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)
_FRONTMATTER_BLOCK_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# syncthing's per-folder metadata. It belongs to this machine's transport layer,
# not to the content, and must never enter the repo.
#
# `**/*.sync-conflict-*` is the one that bites: `git add -A` happily commits conflict
# files, and once tracked, resolving a conflict (which deletes the file) reads back
# as a tracked file losing all its lines — i.e. exactly the signature the silent-
# overwrite detector looks for. It then tries to merge a path that no longer exists.
# A conflict file is transport-layer debris like the rest of this list.
_REPO_IGNORES = (".stfolder/", ".stignore", ".stversions/", "**/*.sync-conflict-*")

CODE_MERGE_SYSTEM = """You merge two divergent versions of the same source file from a \
shared skill library. Two machines edited it independently; both edits are real work.

Return the merged file and NOTHING else — no preamble, no explanation, no code fence. \
The output is written directly to disk and will be executed on three machines.

Rules:
- Keep every substantive change from either side. When both sides touched the same \
function, prefer the version that is more correct or more current; if the changes are \
independent, apply both.
- **Preserve exact indentation and the file's existing style.** In Python a wrong \
indent is a different program, not a formatting nit.
- **Never invent code that is in neither version**, and never leave a placeholder, a \
`...`, or a "rest unchanged" comment. The output replaces the file entirely.
- If the two sides define the same name differently and you cannot tell which is \
current, keep the one that the rest of the file is consistent with.
- Keep imports in sync with what the merged body actually uses.
- Comments explain why the code is the way it is; a comment that no longer matches \
the merged code is worse than no comment. Update it or drop it.
"""

MERGE_SYSTEM = """You merge two divergent versions of the same file from a shared \
skill library. Two machines edited it independently; both edits are real work.

Return the merged file and NOTHING else — no preamble, no explanation, no code \
fence. The output is written directly to disk as the new canonical version.

Rules:
- Keep every substantive improvement from either side. When both sides changed the \
same passage, prefer the one that is more specific, more current, or better \
evidenced; if they are complementary, combine them.
- **One side being much longer does not make it a superset.** Before settling on a \
structure, walk the SHORTER side end to end and account for every passage: either it \
appears in your output, or the longer side already states the same thing elsewhere. \
Returning the longer side unchanged is the specific way this task fails — it looks \
like a clean merge and is indistinguishable from one unless someone diffs the side \
you dropped.
- Drop what is clearly worse: stale instructions, content contradicted by the other \
side, accidental truncation, debugging leftovers.
- Never invent content that is in neither version.
- Preserve the YAML frontmatter. If the two versions disagree there, keep the more \
complete `description` and the union of any list fields.
- The frontmatter `name:` is the skill's identity — reproduce it exactly. Renaming \
it does not edit a skill, it replaces one skill with a different one and deletes \
the original.
- If the two sides genuinely contradict on a fact and neither is newer or better \
evidenced, keep both and mark the disagreement inline for a human to settle. \
Silently picking one is worse than surfacing it.
- Preserve the document's existing structure and language (including Chinese prose)."""


@dataclass(frozen=True)
class Conflict:
    """One conflict file and the canonical file it diverged from."""
    conflict_path: Path
    canonical_path: Path
    device: str
    detected_at: str

    @property
    def rel(self) -> str:
        return self.conflict_path.name


@dataclass(frozen=True)
class Divergence:
    """A tracked file whose working tree copy dropped lines relative to HEAD."""
    path: Path
    added: int
    removed: int

    @property
    def rel(self) -> str:
        return self.path.name


# --------------------------------------------------------------------------
# Discovery — deterministic, no model involved
# --------------------------------------------------------------------------

def find_conflicts(skills_root: Path) -> list[Conflict]:
    out: list[Conflict] = []
    for p in sorted(skills_root.rglob("*.sync-conflict-*")):
        if not p.is_file():
            continue
        m = _CONFLICT_RE.match(p.name)
        if not m:
            log.warning("unparseable conflict name, skipping: %s", p.name)
            continue
        canonical = p.with_name(m.group("stem") + (m.group("ext") or ""))
        out.append(
            Conflict(
                conflict_path=p,
                canonical_path=canonical,
                device=m.group("device"),
                detected_at=f"{m.group('date')}-{m.group('time')}",
            )
        )
    return out


def attempt_counts(log_path: Path, tail: int = 800) -> dict[str, int]:
    """How many times each conflict has already been handed to a model and bounced.

    Without this the hourly beat retries a conflict the model cannot do forever. The
    first real case: a 28KB skill where the shorter side had 3 unique lines — the
    model returned the longer side verbatim, the gate rejected it, and nothing about
    the next attempt would be different. Retrying is only worth it when something has
    changed; when nothing has, it is six minutes of sonnet per hour, indefinitely.
    """
    counts: dict[str, int] = {}
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()[-tail:]
    except OSError:
        return counts
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("action") in ("rejected", "failed_llm"):
            key = rec.get("conflict") or ""
            counts[key] = counts.get(key, 0) + 1
    return counts


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {(proc.stderr or '').strip()[:300]}")
    return proc.stdout


def find_divergences(repo: Path, skills_subdir: str) -> list[Divergence]:
    """Tracked .md files under `skills_subdir` with a net deletion against HEAD.

    Only net deletions. A pure addition means someone wrote something new, which
    needs no arbitration — it just gets committed. What loses information is an
    overwrite, and an overwrite shows up as more lines removed than added.
    """
    out: list[Divergence] = []
    try:
        numstat = _git(repo, "diff", "--numstat", "--", skills_subdir)
    except RuntimeError as e:
        log.warning("cannot diff working tree, skipping divergence scan: %s", e)
        return out
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added_s, removed_s, rel = parts
        if added_s == "-" or removed_s == "-":       # binary
            continue
        if not rel.endswith(".md"):
            continue
        # Conflict files that a previous run committed before they were gitignored.
        # Their "deletion" is a resolution, not a loss.
        if ".sync-conflict-" in rel:
            continue
        added, removed = int(added_s), int(removed_s)
        if removed <= added:
            continue
        # Deleted outright (by the conflict pass earlier in this same beat, or by a
        # machine that removed the skill). There is no working-tree side to merge.
        if not (repo / rel).exists():
            continue
        out.append(Divergence(path=repo / rel, added=added, removed=removed))
    return out


# --------------------------------------------------------------------------
# Merge — the only place a model is used
# --------------------------------------------------------------------------

def _strip_fence(text: str) -> str:
    m = _FENCE_RE.match(text)
    return m.group("body") if m else text


def _frontmatter_name(text: str) -> str | None:
    m = _FRONTMATTER_BLOCK_RE.match(text)
    if not m:
        return None
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None
    name = fm.get("name")
    return str(name) if name is not None else None


# 融合它们产出的是**没发生过的事实**,不是「可能有错的文件」。语法闸救不了这一档:
# 输出完全合法,而且完全是伪造的。
_RECORD_PATTERNS = (
    re.compile(r"(^|/)runs/[^/]+\.json$"),          # 一次运行的记录
    re.compile(r"(^|/)state\.json$"),               # 某台机器此刻的状态
    re.compile(r"(^|/)\.agent-refactor/"),          # 同上,按目录
    re.compile(r"(^|/)[^/]*\.jsonl$"),              # 追加型日志,合并只会乱序
)


def is_record(path: Path) -> bool:
    s = path.as_posix()
    return any(r.search(s) for r in _RECORD_PATTERNS)


def _syntax_ok(path: Path, text: str) -> bool | None:
    """能不能解析。None = 这种类型没有可用的检查,不作判断。"""
    ext = path.suffix.lower()
    try:
        if ext == ".py":
            import ast
            ast.parse(text)
            return True
        if ext == ".json":
            import json as _json
            _json.loads(text)
            return True
        if ext in (".yaml", ".yml"):
            try:
                import yaml
            except ImportError:
                return None
            yaml.safe_load(text)
            return True
        if ext in (".sh", ".bash"):
            import subprocess
            r = subprocess.run(["bash", "-n"], input=text, capture_output=True,
                               text=True, timeout=20)
            return r.returncode == 0
    except SyntaxError:
        return False
    except (ValueError, OSError, subprocess.SubprocessError):
        return False
    except Exception:                                # noqa: BLE001 — 解析器五花八门
        return False
    return None


def regression_gate(path: Path, merged: str, a: str, b: str) -> str | None:
    """只在「两边原本都是好的、融出来的坏了」时打回。

    问「输出完美吗」会把一个本来就语法有问题的文件永远卡死在冲突态,而融合并没有做错
    任何事。要防的是**回归**,不是**不完美**。
    """
    after = _syntax_ok(path, merged)
    if after is not False:
        return None
    if _syntax_ok(path, a) is True and _syntax_ok(path, b) is True:
        return f"{path.suffix or 'this file'} parsed on both sides but not after merging"
    return None


def _substantive_lines(text: str) -> set[str]:
    """Lines long enough to carry a claim.

    The 20-char floor drops headers, bullets, fence markers and blank lines —
    those coincide between any two versions and would drown the signal.
    """
    return {s for line in text.splitlines() if len(s := line.strip()) >= 20}


def merge_gate(merged: str, a: str, b: str) -> str | None:
    """Return a rejection reason, or None if the merge is acceptable.

    These skills are auto-loaded by Claude Code on three machines, so a bad merge
    is not a bad file — it is a bad tool in everyone's hands. Anything that fails
    here leaves the conflict file in place for a human, which is the safe default:
    an unresolved conflict is visible, a silently mangled skill is not.
    """
    if not merged.strip():
        return "empty output"
    # Losing half the content is the failure mode an LLM actually has here
    # (summarising instead of merging), and it is invisible in a diff nobody reads.
    floor = int(min(len(a), len(b)) * 0.5)
    if len(merged) < floor:
        return f"too short: {len(merged)} < {floor} (half the smaller input)"
    had_fm = _FRONTMATTER_RE.match(a) or _FRONTMATTER_RE.match(b)
    if had_fm and not _FRONTMATTER_RE.match(merged):
        return "frontmatter lost"
    # A renamed skill is not a damaged file, it is a different skill: Claude Code
    # keys on `name`, so the original silently stops existing on all three machines
    # while a stranger appears. The size and frontmatter checks both pass for it.
    want = _frontmatter_name(a) or _frontmatter_name(b)
    if want is not None:
        got = _frontmatter_name(merged)
        if got != want:
            return f"skill renamed: {want!r} -> {got!r}"

    # Everything above checks *shape* — size, frontmatter, identity. None of it sees
    # whether any merging happened at all: returning the larger side verbatim passes
    # every one of them.
    #
    # Byte-identical output is worth looking at, but it is NOT by itself wrong, and
    # the first version of this check learned that the expensive way. It rejected a
    # correct merge: one side said "必须出现在报告里…的三样", the other had rewritten
    # the same passage as "四样" with a fourth item added. Exact line matching reports
    # the older phrasing as content unique to the shorter side, when in truth the
    # longer side already contained it and improved on it. **Rewording is what merging
    # looks like**, so a line-level diff cannot distinguish "dropped" from "restated".
    #
    # So: only fire when the unmatched material is too large to be rewording. A
    # handful of lines is normal editing; a large fraction of one side going
    # unaccounted for is not.
    for label, kept, other in (("A", a, b), ("B", b, a)):
        if merged.strip() != kept.strip():
            continue
        other_lines = _substantive_lines(other)
        orphaned = other_lines - _substantive_lines(kept)
        if not orphaned or not other_lines:
            continue
        ratio = len(orphaned) / len(other_lines)
        if len(orphaned) >= 8 and ratio >= 0.15:
            sample = sorted(orphaned)[0][:60]
            return (f"output is byte-identical to version {label}, and {len(orphaned)} "
                    f"of version {'B' if label == 'A' else 'A'}'s {len(other_lines)} "
                    f"substantive lines ({ratio:.0%}) appear nowhere in it "
                    f"(e.g. {sample!r}) — too much to be rewording")
    return None


def _merge_texts(
    llm: LLMProvider, *, label_a: str, text_a: str, label_b: str, text_b: str,
    timeout_seconds: int, is_code: bool = False,
) -> str:
    prompt = (
        f"## Version A — {label_a}\n\n{text_a}\n\n"
        f"## Version B — {label_b}\n\n{text_b}\n\n"
        "## Task\n\nReturn the merged file."
    )
    system = CODE_MERGE_SYSTEM if is_code else MERGE_SYSTEM
    raw = llm.call(prompt, system=system, timeout_seconds=timeout_seconds)
    return (scrub(_strip_fence(raw).strip()) or "") + "\n"


def resolve_one(
    c: Conflict, llm: LLMProvider, *, dry_run: bool, timeout: int, attempts: int = 0,
    max_attempts: int = 3,
) -> dict:
    """Merge a single conflict. Returns a record for the audit log."""
    rec: dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": "conflict",
        "conflict": c.rel,
        "canonical": str(c.canonical_path),
        "from_device": c.device,
    }

    if attempts >= max_attempts:
        # Stop paying for the same failure. The conflict file stays — this is a
        # handoff to a human, not a resolution, and it has to stay visible as one.
        rec |= {"action": "needs_human", "attempts": attempts,
                "reason": f"failed {attempts}x — a model is not going to get this one"}
        return rec

    # Only prose gets merged. "Take what each side got right" presumes the two
    # sides are two drafts of one document — true for a SKILL.md, false for the
    # things that live alongside it:
    #
    #   agent-refactor/runs/<session>.json   two machines each ran the skill once.
    #     These are not two versions of one record, they are two records. Merging
    #     them yields a run that never happened — a fabricated entry in an audit
    #     trail, which is worse than either input.
    #   .agent-refactor/state.json           per-machine state, momentary by design.
    #   account.sh / *.py                    executable. A merge that reads fine and
    #     runs wrong is exactly what no gate here can catch, and it lands on three
    #     machines at once.
    #
    # These stay as conflict files: unresolved is visible, and a human settles it.
    if is_record(c.canonical_path):
        rec |= {"action": "skipped_unmergeable",
                "reason": "a record of what happened — merging two of them "
                          "states a run that never occurred"}
        return rec

    conflict_text = c.conflict_path.read_text(encoding="utf-8", errors="replace")

    # The canonical side can be gone when a machine deleted the file while another
    # edited it. There is nothing to merge then, and the surviving edit is the only
    # copy of that work — promote it rather than discarding it.
    if not c.canonical_path.exists():
        rec |= {"action": "promoted_orphan", "reason": "canonical missing"}
        if not dry_run:
            c.conflict_path.rename(c.canonical_path)
        return rec

    canonical_text = c.canonical_path.read_text(encoding="utf-8", errors="replace")
    rec |= {"len_canonical": len(canonical_text), "len_conflict": len(conflict_text)}

    if canonical_text == conflict_text:
        rec |= {"action": "dropped_identical"}
        if not dry_run:
            c.conflict_path.unlink()
        return rec

    try:
        merged = _merge_texts(
            llm,
            label_a=f"current canonical file ({c.canonical_path.name})",
            text_a=canonical_text,
            label_b=f"conflicting version from device {c.device} ({c.detected_at})",
            text_b=conflict_text,
            timeout_seconds=timeout,
            is_code=c.canonical_path.suffix.lower() != ".md",
        )
    except RateLimitDeferred as e:
        rec |= {"action": "deferred_rate_limit", "error": str(e)}
        return rec
    except LLMError as e:
        rec |= {"action": "failed_llm", "error": str(e)}
        return rec

    rec["len_merged"] = len(merged)
    reason = (merge_gate(merged, canonical_text, conflict_text)
              or regression_gate(c.canonical_path, merged, canonical_text, conflict_text))
    if reason:
        rec |= {"action": "rejected", "reason": reason}
        return rec

    rec |= {"action": "merged"}
    if not dry_run:
        c.canonical_path.write_text(merged, encoding="utf-8")
        c.conflict_path.unlink()
    return rec


def resolve_divergence(
    d: Divergence, repo: Path, llm: LLMProvider, *, dry_run: bool, timeout: int
) -> dict:
    """Reconcile a silent overwrite against the last merged version in HEAD."""
    rel = d.path.relative_to(repo).as_posix()
    rec: dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": "divergence",
        "conflict": rel,
        "canonical": str(d.path),
        "from_device": "unknown (syncthing already overwrote)",
        "lines_added": d.added,
        "lines_removed": d.removed,
    }

    try:
        head_text = _git(repo, "show", f"HEAD:{rel}")
    except RuntimeError as e:
        rec |= {"action": "failed_git", "error": str(e)}
        return rec

    cur_text = d.path.read_text(encoding="utf-8", errors="replace")
    rec |= {"len_canonical": len(head_text), "len_conflict": len(cur_text)}
    if head_text == cur_text:
        rec |= {"action": "dropped_identical"}
        return rec

    try:
        merged = _merge_texts(
            llm,
            label_a="last merged version (git HEAD)", text_a=head_text,
            label_b="incoming version (synced in from another machine)", text_b=cur_text,
            timeout_seconds=timeout,
            is_code=c.canonical_path.suffix.lower() != ".md",
        )
    except RateLimitDeferred as e:
        rec |= {"action": "deferred_rate_limit", "error": str(e)}
        return rec
    except LLMError as e:
        rec |= {"action": "failed_llm", "error": str(e)}
        return rec

    rec["len_merged"] = len(merged)
    reason = merge_gate(merged, head_text, cur_text)
    if reason:
        rec |= {"action": "rejected", "reason": reason}
        return rec

    rec |= {"action": "merged"}
    if not dry_run:
        d.path.write_text(merged, encoding="utf-8")
    return rec


# --------------------------------------------------------------------------
# Landing the result
# --------------------------------------------------------------------------

def ensure_gitignore(repo: Path, skills_subdir: str) -> bool:
    gi = repo / ".gitignore"
    cur = gi.read_text(encoding="utf-8") if gi.exists() else ""
    missing = [p for p in _REPO_IGNORES if f"{skills_subdir}/{p}" not in cur]
    if not missing:
        return False
    block = (
        "\n# syncthing's per-folder metadata — transport layer, not content.\n"
        + "".join(f"{skills_subdir}/{p}\n" for p in missing)
    )
    gi.write_text(cur.rstrip() + "\n" + block, encoding="utf-8")
    return True


def git_commit(repo: Path, records: list[dict], *, push: bool = True) -> str | None:
    """Commit merged skills. The hub's skill tree is a git working tree precisely
    so a bad merge is one `git revert` away — syncthing itself keeps no history
    that survives the next sync."""
    if not _git(repo, "status", "--porcelain").strip():
        return None

    merged = [r for r in records if r.get("action") in ("merged", "promoted_orphan")]
    if merged:
        names = sorted({Path(r["canonical"]).parent.name for r in merged})
        subject = f"merge(skills): {len(merged)} conflict(s) across {', '.join(names[:3])}"
        if len(names) > 3:
            subject += f" +{len(names) - 3}"
        body = "\n".join(
            f"- {r['conflict']} ({r['action']}, from {r['from_device']})" for r in merged
        )
    else:
        # Nothing needed arbitration, but syncthing still brought content in. It has
        # to be committed anyway: HEAD is the baseline every future divergence scan
        # measures against, so leaving it behind makes the next overwrite invisible.
        subject = "chore(skills): take in content synced from other machines"
        body = ""

    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    cmd = ["git", "-C", str(repo), "commit", "-m", subject]
    if body:
        cmd += ["-m", body]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        log.warning("git commit failed: %s", (proc.stderr or "").strip()[:300])
        return None

    rev = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip() or None

    if push:
        try:
            _git(repo, "push", "origin", "HEAD")
        except RuntimeError as e:
            # Not fatal: the merge is on disk and syncthing is already distributing
            # it. The bare repo is a backup, not part of the delivery path.
            log.warning("push failed (distribution unaffected): %s", e)
    return rev


def run(
    *,
    dry_run: bool,
    skills_root: Path,
    log_path: Path,
    push: bool = True,
    scan_only: bool = False,
    merge_timeout: int = 900,
    model: str | None = None,
    max_attempts: int = 3,
) -> int:
    """One beat.

    Two cadences share this function, and the split is deliberate. Detection is
    cheap and wants to be frequent; arbitration is slow and expensive and wants
    to be rare. A 28KB SKILL.md takes minutes to merge — chasing that on a
    five-minute clock means paying for the same unfinished work over and over.
    So the frequent beat runs with `scan_only` and leaves conflicts sitting on
    disk, which costs nothing: an unresolved conflict file is stable, and the
    slow beat picks up whatever has accumulated.
    """
    repo = skills_root.parent
    skills_subdir = skills_root.name
    has_git = (repo / ".git").is_dir()

    conflicts = find_conflicts(skills_root)
    divergences = find_divergences(repo, skills_subdir) if has_git else []

    def land_synced_content() -> None:
        """Commit whatever synced in. Runs on *every* beat, arbitration or not:
        HEAD is the baseline the divergence scan measures against, so a beat that
        skips it makes the next silent overwrite unmeasurable."""
        if not dry_run and has_git:
            rev = git_commit(repo, [], push=push)
            if rev:
                log.info("committed %s", rev)

    if not conflicts and not divergences:
        log.info("nothing to arbitrate in %s", skills_root)
        land_synced_content()
        return 0

    if scan_only:
        for c in conflicts:
            log.info("%-22s %s (from %s)", "pending_conflict", c.rel, c.device)
        for d in divergences:
            log.info("%-22s %s (-%d/+%d)", "pending_overwrite", d.rel, d.removed, d.added)
        log.info("%d conflict(s), %d silent overwrite(s) left for the merge pass",
                 len(conflicts), len(divergences))
        land_synced_content()
        return 0

    log.info("%d conflict(s), %d silent overwrite(s) to resolve",
             len(conflicts), len(divergences))
    cfg = Config(load_defaults())
    llm = LLMProvider(cfg)
    if model:
        # Merging two documents is structural work, not reasoning work: decide
        # what each side has, keep it, drop what the other supersedes. The
        # pipeline's default (opus) spent 25 minutes on a 53KB input without
        # returning — 1.7s of CPU, all of it waiting. A faster model finishes,
        # and finishing is the only property that matters on an hourly beat.
        llm.model = model
    log.info("merging with %s", llm.model)

    records: list[dict] = []

    def record(rec: dict) -> dict:
        """Log and persist one outcome immediately.

        Not batched at the end of the run: a merge costs minutes of model time, and
        a crash after it lands would otherwise erase the only evidence that it ever
        happened. That is not hypothetical — the first successful merge in this
        service's life (claude-account) left no audit trail at all, because the beat
        died two files later and took the whole unwritten batch with it. An audit log
        that only survives the successful runs is missing on exactly the runs worth
        auditing.
        """
        # `reason` explains a rejection, `error` explains a failure — printing only
        # the first makes every LLM/transport failure show up as a bare action name
        # with no cause, which is exactly how the missing `claude` CLI first looked.
        why = rec.get("reason") or rec.get("error") or ""
        log.info("%-22s %s %s", rec["action"], rec["conflict"], why)
        records.append(rec)
        if not dry_run:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    tried = attempt_counts(log_path)
    for c in conflicts:
        record(resolve_one(c, llm, dry_run=dry_run, timeout=merge_timeout,
                           attempts=tried.get(c.rel, 0), max_attempts=max_attempts))

    # Conflicts just rewrote files on disk; recompute so the same file is not
    # merged twice in one pass.
    if conflicts and not dry_run and has_git:
        divergences = find_divergences(repo, skills_subdir)
    for d in divergences:
        record(resolve_divergence(d, repo, llm, dry_run=dry_run, timeout=merge_timeout))

    if not dry_run:
        if has_git:
            ensure_gitignore(repo, skills_subdir)
            rev = git_commit(repo, records, push=push)
            if rev:
                log.info("committed %s", rev)
        else:
            # Not fatal — the merge already landed and syncthing will distribute it.
            # But it means this run has no rollback point, which is worth saying out
            # loud rather than discovering after a bad merge has reached three machines.
            log.warning("no git repo at %s — merged without a rollback point", repo)

    # A rejected or failed merge is the whole reason a human would want to look,
    # so it has to reach the exit code — a service that always exits 0 is a
    # service nobody checks.
    bad = [r for r in records if r["action"] in ("rejected", "failed_llm", "failed_git")]
    return 1 if bad else 0


@contextlib.contextmanager
def single_instance(lock_path: Path):
    """Yield True if this process got the lock, False if another beat holds it.

    The two cadences overlap by construction: the scan beat fires every 5 minutes
    and a merge beat runs for as long as the model takes — well past 5 minutes on a
    28KB skill. Both of them `git add -A` and commit, so without this they can stage
    each other's half-written files, and two merge passes could hand the same
    conflict to two agents and race to write the winner back.

    A skipped beat is free. The scan beat comes back in five minutes and the merge
    beat on the hour, and conflicts sitting on disk do not decay.
    """
    if fcntl is None:
        yield True
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = lock_path.open("w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="skillmerge",
        description="Merge divergent skills in the shared library; syncthing distributes the result.",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be merged; no LLM writes, no git commit")
    p.add_argument("--skills-root", default=None,
                   help="default: paths.skills_root from config")
    p.add_argument("--scan-only", action="store_true",
                   help="detect and report divergence, commit synced content, but never "
                        "call the model — the frequent beat")
    p.add_argument("--merge-timeout", type=int, default=900, metavar="SEC",
                   help="per-merge LLM timeout (default 900; a 28KB SKILL.md does not "
                        "fit in the CLI's usual 300)")
    p.add_argument("--max-attempts", type=int, default=3, metavar="N",
                   help="stop handing a conflict to the model after N failures and mark it "
                        "needs_human (default 3)")
    p.add_argument("--model", default="claude-sonnet-5", metavar="ID",
                   help="model for merging (default claude-sonnet-5). Pass empty to use "
                        "the pipeline default from config.")
    p.add_argument("--interval", type=int, default=0, metavar="SEC",
                   help="stay resident and beat every SEC seconds; default is one pass and exit")
    p.add_argument("--no-push", action="store_true", help="do not push to origin")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    cfg = Config(load_defaults())
    paths = resolve_paths(cfg)
    skills_root = Path(args.skills_root).expanduser() if args.skills_root else paths.skills_root
    if not skills_root.is_dir():
        print(f"ERROR: skills root not found: {skills_root}", file=sys.stderr)
        return 2
    # `skills_root` is `~/.claude/skills`, which on every machine in this fleet is a
    # symlink into the claude-config checkout. Resolving it is what puts `.parent` on
    # the git repo instead of on `~/.claude` — without this, every commit silently
    # finds nothing to stage and each merge ships with no way back.
    skills_root = skills_root.resolve()
    log_path = paths.data_dir / "logs" / "merges.jsonl"

    beat = dict(
        dry_run=args.dry_run, skills_root=skills_root, log_path=log_path,
        push=not args.no_push, scan_only=args.scan_only,
        merge_timeout=args.merge_timeout, model=args.model or None,
        max_attempts=args.max_attempts,
    )

    lock_path = paths.data_dir / "merge.lock"

    def guarded() -> int:
        with single_instance(lock_path) as got:
            if not got:
                log.info("another beat is still running — skipping this one")
                return 0
            return run(**beat)

    if args.interval <= 0:
        return guarded()

    log.info("heartbeat started: every %ds on %s (scan_only=%s)",
             args.interval, skills_root, args.scan_only)
    while True:
        try:
            guarded()
        except Exception:
            # One bad beat must not stop the heartbeat: the next beat gets another
            # chance, a dead process never does.
            log.exception("beat failed, waiting for the next one")
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
