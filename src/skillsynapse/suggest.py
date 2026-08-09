"""`skillsuggest` — recommend what is worth turning into a skill, without spending a token.

[docs/07](../../docs/07-triage-and-ranking.md) puts ranking on the *output* side:
extract everything, then rank the drafts, on the stated assumption that "quota is
plentiful, so don't gate upstream to save LLM calls". This module inverts that,
because the constraint here is the opposite one — tokens are the scarce thing, and
*which skills should exist* is a human judgement rather than a threshold.

So: cluster and score first, a human picks, and only the picked candidates are worth
an extraction run. The five priority factors in docs/07 §2 make this possible — the
doc itself notes they are "all computable automatically from JSONL and existing
tables". Nothing here calls a model.

**What this deliberately cannot see.** Clustering without reading content means
clustering on cheap signals: which project, which files, which tools. That finds
"recalibrating recoil in pubg_derecoil, five times this week". It cannot find a
cross-project abstraction like "I always probe before I act" — that only exists once
something has read the prose. Those are missed, and the trade is worth it: an
abstraction that no file-overlap can detect is usually too diffuse to be a good skill
anyway, and the alternative is extracting all ~2900 sessions to find out.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from .config import Config, load_config, resolve_paths
from .episode_detector import Episode, detect_episodes
from .metrics import _CORRECTION_RE
from .models import SessionMeta
from .scanner import Event, find_sessions, parse_jsonl, scan_roots, yesterday_cutoff

log = logging.getLogger(__name__)


# Tool-input keys that hold a path outright. Bash is handled separately: its
# `command` is prose-with-paths, and the paths in it are what say which subsystem
# a run touched — dropping Bash would blind the clustering on exactly the sessions
# that do infrastructure work.
_PATH_KEYS = ("file_path", "path", "notebook_path", "filePath")
_PATH_IN_CMD_RE = re.compile(r"(?:^|[\s'\"=])((?:~|\.{0,2}/|[A-Za-z]:[\\/])[\w./\\-]{3,})")

# Frontmatter of an existing skill, for the novelty factor.
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FILENAME_RE = re.compile(r"\b([\w-]+\.(?:py|md|sh|ps1|json|jsonl|toml|yaml|yml|c|h|ts|tsx|vbs))\b")

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# Claude Code injects a lot of machinery into the *user* role: tool notifications,
# IDE events, reminders, compaction preambles. They are the longest "user" messages
# in most transcripts, so a label picked by length lands on them every time \u2014 the
# first run of this module produced eight candidates labelled `<task-notification>`
# and `[Image: original 3440x1440\u2026]`. Strip the wrappers, then reject what is left
# if it is still machinery.
_TAG_BLOCK_RE = re.compile(
    r"<(system-reminder|task-notification|ide_opened_file|ide_selection|command-name"
    r"|command-message|command-args|local-command-stdout|user-memory-input)\b.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
_MACHINE_PREFIX_RE = re.compile(
    r"^\s*(?:<(?:task-notification|ide_opened_file|ide_selection|system-reminder|command-"
    r"|local-command-stdout|user-memory-input|function_results|antml)"
    r"|\[Image:|\[Request interrupted|\[SYSTEM NOTIFICATION"
    r"|This session is being continued|Caveat: The messages below"
    r"|Please continue the conversation from where)",
    re.IGNORECASE,
)

# Claude Code encodes a project's path into its directory name (`d--10-projects-pubg-
# derecoil`). Everything up to and including the workspace root is noise shared by
# every project on that machine.
_PATH_NOISE = {"", "home", "users", "user", "shuai", "shuaiyan", "10187", "c", "d",
               "code", "projects", "10-projects", "github-repos", "mnt", "tmp"}


@dataclass
class EpisodeFeature:
    """Everything one episode contributes, extracted once."""
    episode_id: str
    session_id: str
    project: str
    host: str
    files: set[str]
    tools: set[str]
    tool_count: int
    corrections: int
    end_time: Optional[datetime]
    openers: list[str]               # genuine human turns, for labelling


@dataclass
class Candidate:
    cid: int
    label: str
    project: str
    hosts: list[str]
    episodes: int
    avg_tools: float
    corrections: int
    last_seen: Optional[datetime]
    top_files: list[str]
    top_tools: list[str]
    nearest_skill: Optional[str]
    nearest_sim: float
    score: float
    breakdown: dict[str, float]
    episode_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.cid,
            "score": round(self.score, 2),
            "label": self.label,
            "project": self.project,
            "hosts": self.hosts,
            "episodes": self.episodes,
            "avg_tools": round(self.avg_tools, 1),
            "corrections": self.corrections,
            "last_seen": self.last_seen.isoformat(timespec="seconds") if self.last_seen else None,
            "top_files": self.top_files,
            "top_tools": self.top_tools,
            "nearest_skill": self.nearest_skill,
            "nearest_sim": round(self.nearest_sim, 2),
            "breakdown": {k: round(v, 2) for k, v in self.breakdown.items()},
            "episode_ids": self.episode_ids,
        }


# ── feature extraction ───────────────────────────────────────────────

def _paths_in_episode(ep: Episode) -> set[str]:
    """Basenames of files this episode touched.

    Basenames rather than full paths on purpose: the same work done on two
    machines has different absolute prefixes (`C:\\Users\\10187\\...` vs
    `/home/shuai/...`), and clustering on full paths would split every
    cross-machine repetition into two candidates — losing precisely the `repeat`
    signal that makes something worth extracting.
    """
    out: set[str] = set()
    for ev in ep.events:
        if ev.type != "tool_use" or not ev.tool_input:
            continue
        for key in _PATH_KEYS:
            v = ev.tool_input.get(key)
            if isinstance(v, str) and v:
                out.add(Path(v.replace("\\", "/")).name)
        cmd = ev.tool_input.get("command")
        if isinstance(cmd, str):
            for m in _PATH_IN_CMD_RE.finditer(cmd):
                name = Path(m.group(1).replace("\\", "/")).name
                if name and not name.startswith("-"):
                    out.add(name)
    return {f for f in out if len(f) > 2}


def _corrections(ep: Episode) -> int:
    return sum(1 for ev in ep.events
               if ev.type == "user" and ev.text and _CORRECTION_RE.search(ev.text))


def _human_text(raw: str) -> str:
    """What the person actually typed, or "" if this turn was machinery."""
    t = _TAG_BLOCK_RE.sub(" ", raw)
    t = " ".join(t.split())
    if not t or _MACHINE_PREFIX_RE.match(t):
        return ""
    return t


def _openers(ep: Episode) -> list[str]:
    """Every genuine human turn in the episode, in order.

    All of them, not just the first: the opening turn is often "继续" or a pasted
    error, while the sentence that names the task can come three turns in.
    """
    out: list[str] = []
    for ev in ep.events:
        if ev.type != "user" or not ev.text:
            continue
        t = _human_text(ev.text)
        if len(t) >= 6:
            out.append(t[:200])
    return out


def _pretty_project(project: str) -> str:
    """`d--10-projects-pubg-derecoil` -> `pubg-derecoil`."""
    parts = [p for p in project.split("-") if p.lower() not in _PATH_NOISE]
    return "-".join(parts) if parts else project


def features_for_session(meta: SessionMeta, cfg: Config) -> list[EpisodeFeature]:
    try:
        events = parse_jsonl(Path(meta.path))
    except OSError as e:
        log.warning("unreadable session %s: %s", meta.path, e)
        return []
    ed = cfg.raw.get("episode_detection", {}) or {}
    episodes = detect_episodes(
        meta.id, events,
        gap_minutes=int(ed.get("gap_minutes", 30)),
        min_tool_overlap=int(ed.get("min_tool_overlap", 0)),
    )
    out: list[EpisodeFeature] = []
    for ep in episodes:
        if ep.tool_call_count < 3:
            # Too small to be a procedure. Not a threshold on quality — a 2-call
            # episode has no shape to recognise, so it only adds noise to clustering.
            continue
        out.append(EpisodeFeature(
            episode_id=ep.episode_id,
            session_id=meta.id,
            project=meta.project,
            host=meta.hostname or "local",
            files=_paths_in_episode(ep),
            tools=set(ep.tool_names),
            tool_count=ep.tool_call_count,
            corrections=_corrections(ep),
            end_time=ep.end_time,
            openers=_openers(ep),
        ))
    return out


# ── clustering ───────────────────────────────────────────────────────

def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalise to tz-aware UTC.

    Session timestamps arrive both ways: Claude Code writes offset-aware ISO-8601,
    but not every transcript in a three-machine corpus does, and arithmetic between
    the two kinds raises rather than returning a wrong number. Treat naive stamps as
    UTC — an hours-off recency factor is a rounding error next to a crash.
    """
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cluster(feats: list[EpisodeFeature], threshold: float = 0.25) -> list[list[EpisodeFeature]]:
    """Single-link clustering by file overlap, within a project.

    Project first because work is project-shaped: two episodes in different repos
    are different work even when they touch same-named files (`main.py` is
    everywhere). Then file overlap, because "the same procedure" shows up as "the
    same files, again".
    """
    by_project: dict[str, list[EpisodeFeature]] = {}
    for f in feats:
        by_project.setdefault(f.project, []).append(f)

    clusters: list[list[EpisodeFeature]] = []
    for _, group in sorted(by_project.items()):
        pool = list(group)
        while pool:
            seed = pool.pop()
            members = [seed]
            merged = set(seed.files)
            changed = True
            while changed:                       # single-link: keep absorbing
                changed = False
                for cand in list(pool):
                    if _jaccard(merged, cand.files) >= threshold:
                        members.append(cand)
                        merged |= cand.files
                        pool.remove(cand)
                        changed = True
            clusters.append(members)
    return clusters


# ── novelty against the existing library ─────────────────────────────

def _tokens(text: str) -> set[str]:
    """Words for English, character bigrams for CJK.

    Whitespace tokenisation returns nothing useful for Chinese, and most of this
    library's skill descriptions are Chinese — a novelty score computed on English
    words alone would rate every Chinese skill as maximally novel.
    """
    t = text.lower()
    out = set(_WORD_RE.findall(t))
    cjk = _CJK_RE.findall(t)
    out |= {"".join(pair) for pair in zip(cjk, cjk[1:])}
    return out


def load_skill_profiles(skills_root: Path) -> dict[str, tuple[set[str], set[str]]]:
    """name -> (description tokens, filenames named anywhere in the body).

    The filenames are what make this work at all. Descriptions in this library are
    English (the repo was translated), while the sessions they cover are Chinese —
    so text similarity between a candidate's label and a skill's description is
    near zero *even when the skill covers it exactly*, and every candidate comes
    back "no existing coverage". Filenames are language-neutral: a skill that walks
    you through `harvest.py` and `fit_curve.py` is demonstrably about the sessions
    that ran them.
    """
    out: dict[str, tuple[set[str], set[str]]] = {}
    for md in sorted(skills_root.glob("*/SKILL.md")):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = _FRONTMATTER_RE.match(text)
        if not m:
            continue
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            continue
        name = str(fm.get("name") or md.parent.name)
        files = {f.lower() for f in _FILENAME_RE.findall(text)}
        out[name] = (_tokens(f"{name} {fm.get('description', '')}"), files)
    return out


def nearest_skill(
    tokens: set[str], files: set[str], lib: dict[str, tuple[set[str], set[str]]]
) -> tuple[Optional[str], float]:
    """Best coverage by either signal — text OR files, whichever is stronger.

    max() rather than a blend: the two signals fail in opposite conditions (text
    dies across languages, filenames die for skills that are pure prose), and a
    blend would let the dead one drag down the live one.
    """
    lower = {f.lower() for f in files}
    best, best_sim = None, 0.0
    for name, (toks, skill_files) in lib.items():
        sim = max(_jaccard(tokens, toks), _jaccard(lower, skill_files))
        if sim > best_sim:
            best, best_sim = name, sim
    return best, best_sim


# ── scoring ──────────────────────────────────────────────────────────

def merge_by_label(clusters: list[list[EpisodeFeature]], threshold: float = 0.55) -> list[list[EpisodeFeature]]:
    """Fold together clusters that describe the same work.

    File overlap alone splits one procedure into several: the same task run twice
    touches overlapping but not identical files (different weapon, different
    script), and single-link never bridges the gap. The first run produced #3 and
    #7 with *character-for-character identical* labels — one piece of work, counted
    twice, its `repeat` factor halved in both halves. What files miss, the sentence
    the person typed catches.
    """
    labelled = [(_label_for(c, Counter(f for m in c for f in m.files)), c) for c in clusters]
    out: list[list[EpisodeFeature]] = []
    used = [False] * len(labelled)
    for i, (label_i, ci) in enumerate(labelled):
        if used[i]:
            continue
        merged = list(ci)
        toks_i = _tokens(label_i)
        for j in range(i + 1, len(labelled)):
            if used[j]:
                continue
            label_j, cj = labelled[j]
            if ci[0].project != cj[0].project:
                continue
            if _jaccard(toks_i, _tokens(label_j)) >= threshold:
                merged.extend(cj)
                used[j] = True
        used[i] = True
        out.append(merged)
    return out


def score_cluster(
    members: list[EpisodeFeature],
    lib: dict[str, tuple[set[str], set[str]]],
    now: datetime,
) -> Candidate:
    n = len(members)
    total_tools = sum(m.tool_count for m in members)
    corrections = sum(m.corrections for m in members)
    last_seen = max((t for m in members if (t := _as_utc(m.end_time))), default=None)

    file_counts = Counter(f for m in members for f in m.files)
    tool_counts = Counter(t for m in members for t in m.tools)
    label = _label_for(members, file_counts)

    tokens = _tokens(label)
    near, sim = nearest_skill(tokens, set(file_counts), lib)

    # repeat: how many times you've done this — the strongest proxy for importance.
    repeat = 1 + math.log2(n) if n > 1 else 1.0
    # cost: how expensive a run was. Corrections count: a run you had to steer is a
    # run whose procedure was not obvious, which is exactly what a skill fixes.
    avg_tools = total_tools / n
    cost = max(0.5, min(2.0, avg_tools / 20.0)) * (1.0 + 0.15 * (corrections / n))
    # novelty: down-weight what the library already covers.
    novelty = max(0.2, min(1.5, 1.5 - sim * 2.0))
    # recency: 30-day half-life. Something done once, months ago, sinks.
    if last_seen is None:
        recency = 0.5
    else:
        days = max(0.0, (now - last_seen).total_seconds() / 86400.0)
        recency = max(0.3, min(1.0, 0.5 ** (days / 30.0)))

    return Candidate(
        cid=0,
        label=label,
        project=_pretty_project(members[0].project),
        hosts=sorted({m.host for m in members}),
        episodes=n,
        avg_tools=avg_tools,
        corrections=corrections,
        last_seen=last_seen,
        top_files=[f for f, _ in file_counts.most_common(6)],
        top_tools=[t for t, _ in tool_counts.most_common(5)],
        nearest_skill=near,
        nearest_sim=sim,
        score=repeat * cost * novelty * recency,
        breakdown={"repeat": repeat, "cost": cost, "novelty": novelty, "recency": recency},
        episode_ids=[m.episode_id for m in members],
    )


def _label_for(members: list[EpisodeFeature], file_counts: Counter) -> str:
    """A human-readable handle for the cluster.

    Bounded length, not maximum length. A turn under ~15 chars ("继续", "跑一下")
    names nothing; one over ~120 is nearly always a pasted traceback or log dump.
    The sentence that states a task lives between those, so search there first and
    only fall back outside the band when the band is empty.
    """
    pool = [t for m in members for t in m.openers]
    banded = [t for t in pool if 15 <= len(t) <= 120]
    best = max(banded, key=len) if banded else (max(pool, key=len) if pool else "")
    best = best[:80].strip()
    if not best:
        top = ", ".join(f for f, _ in file_counts.most_common(3))
        proj = _pretty_project(members[0].project)
        return f"({proj}) {top}" if top else proj
    return best


# ── driver ───────────────────────────────────────────────────────────

def suggest(
    *, cfg: Optional[Config] = None, hours_back: int, top: int, min_episodes: int = 1
) -> list[Candidate]:
    """Rank candidates. Loads the user config itself when not given one.

    Defaulting here rather than trusting callers is deliberate: `Config(load_defaults())`
    silently reverts `aggregation_root` to null, so the hub scans only its own ~30
    sessions instead of all three machines' ~2900. Nothing errors — the answer just
    comes back smaller, which is indistinguishable from "there was less to find".
    """
    cfg = cfg or load_config()
    paths = resolve_paths(cfg)
    cutoff = yesterday_cutoff(hours_back)
    roots = scan_roots(paths.projects_root, paths.aggregation_root)
    sessions = find_sessions(
        roots, modified_after=cutoff,
        exclude_subagents=bool(cfg.raw.get("extraction", {}).get("exclude_subagents", True)),
    )
    log.info("scanning %d session(s) across %d root(s) [%s]",
             len(sessions), len(roots),
             ",".join(sorted({h for h, _ in roots if h})) or "local")

    feats: list[EpisodeFeature] = []
    for meta in sessions:
        feats.extend(features_for_session(meta, cfg))
    log.info("%d episode(s) with enough tool calls to have a shape", len(feats))
    if not feats:
        return []

    lib = load_skill_profiles(paths.skills_root)
    log.info("comparing against %d existing skill(s)", len(lib))

    groups = merge_by_label(cluster(feats))
    log.info("%d candidate group(s) after merging", len(groups))

    now = datetime.now(timezone.utc)
    cands = [score_cluster(c, lib, now) for c in groups if len(c) >= min_episodes]
    cands.sort(key=lambda c: c.score, reverse=True)
    for i, c in enumerate(cands[:top], start=1):
        c.cid = i
    return cands[:top]


def render_table(cands: list[Candidate]) -> str:
    if not cands:
        return "  (no candidates in this window)"
    lines = ["  #  score  reps  cost  novl  project              candidate",
             "  " + "-" * 96]
    for c in cands:
        b = c.breakdown
        lines.append(
            "  %-2d %5.1f  %3dx  %4.1f  %4.1f  %-20s %s"
            % (c.cid, c.score, c.episodes, b["cost"], b["novelty"],
               c.project[:20], c.label[:44])
        )
        cover = (f"covered by {c.nearest_skill} ({c.nearest_sim:.0%})"
                 if c.nearest_skill and c.nearest_sim > 0.15 else "no existing coverage")
        lines.append("        %s · avg %.0f tools · %d corrections · %s"
                     % (",".join(c.hosts), c.avg_tools, c.corrections, cover))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="skillsuggest",
        description="Recommend what is worth extracting into a skill. Calls no model.",
    )
    ap.add_argument("--hours-back", type=int, default=168, help="window (default 168 = 1 week)")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--min-episodes", type=int, default=1,
                    help="drop clusters seen fewer than N times (default 1 = keep all)")
    ap.add_argument("--json", metavar="PATH",
                    help="also write the full candidate list here (what `grow` reads)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
    )

    cands = suggest(hours_back=args.hours_back, top=args.top,
                    min_episodes=args.min_episodes)
    print(render_table(cands))

    if args.json:
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "window_hours": args.hours_back,
            "candidates": [c.to_dict() for c in cands],
        }
        Path(args.json).expanduser().write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
