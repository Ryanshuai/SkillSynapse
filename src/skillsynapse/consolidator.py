"""Skill consolidation operator — the deferred v0.2 `merge`, brought forward
and made content-driven so it can run on a fresh backfill store.

Per-episode extraction (and especially a full backfill) produces many narrow,
proper-noun-laden skills. Two failure modes show up together:

  * semantic near-duplicates with *different* names (the extractor re-derives a
    capability it already captured, so lexical dedup misses them), and
  * over-fine siblings that are really one parametric capability split across a
    board type / camera model / dataset.

plus over-specific descriptions that copy the evidence's proper nouns.

The design's v0.2 `merge` keys on co-selection telemetry, which freshly-captured
skills don't have yet. This operator works purely on CONTENT via the LLM, split
into small reliable calls (one big "cluster-and-rewrite-everything" call times
out on a 78-skill catalog):

  1. cluster    — ONE light call names only the groups that should MERGE
                  (2+ members). Everything unmentioned stays as-is.
  2. synthesize — per merge cluster, one call unifies the member SKILL.md bodies
                  into a single parametric skill.
  3. generalize — a BATCHED call rewrites the over-specific descriptions of the
                  surviving singletons (proper nouns → general capability).

Merged members are deactivated (is_active=0) with parent lineage on the survivor.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional
from uuid import uuid4

from .config import Config
from .extractor import _parse_llm_json
from .llm_provider import LLMError, LLMProvider, RateLimitDeferred
from .models import SkillRecord, iso_now
from .sanitizer import scrub
from .store import Store


# Descriptions carrying these markers are project/hardware-specific and are
# candidates for the generalize pass. Deliberately broad — a false positive just
# sends a clean description through a no-op rewrite.
_PROPER_NOUN = re.compile(
    r"\b([a-z_]+\.(json|py|png|md|yaml|db)|scene_[a-z0-9]+|rig\.json|gt_pipeline|"
    r"gemini|orbbec|pyorbbecsdk|charuco|aruco|colmap|dfsfm|pycolmap4?|glomap|"
    r"labelme|jetson|335le|orin|seer|s2dnet|eth3d|rerun|build123d|zhwiki)\b",
    re.I,
)


# ── clustering (merge-only) ─────────────────────────────────────

CLUSTER_SYSTEM = (
    "You are SkillSynapse's consolidator. From the skill catalog, identify only "
    "the GROUPS of skills that should be merged into one. Return ONLY a single "
    "JSON object. No prose, no markdown fences."
)


def _skill_catalog(skills: list[SkillRecord]) -> str:
    lines = []
    for i, s in enumerate(skills):
        desc = (s.description or "").replace("\n", " ")[:240]
        lines.append(f"{i+1}. [{s.category}] {s.name} — {desc}")
    return "\n".join(lines)


def build_cluster_prompt(skills: list[SkillRecord], preset_categories: str) -> str:
    return f"""\
PRESET CATEGORIES (canonical_category MUST be one of these slugs):

{preset_categories}

── SKILL CATALOG ({len(skills)} skills) ──

{_skill_catalog(skills)}

── TASK ──

List ONLY the groups of 2+ skills that should be MERGED into one skill. Two
reasons to merge:

  (a) true duplicates — same goal, tool, and output, just re-derived or reworded.
  (b) over-fine siblings — skills that differ ONLY by a parameter (board type,
      camera model, dataset, file layout); they should become ONE parametric
      skill.

Do NOT merge skills with a genuinely different goal, primary tool, or output
artifact. Skills you don't list are kept as-is — you do NOT need to mention them.
Be conservative: only group skills you are confident are the same capability.

For each merge group emit:
  members: exact skill names from the catalog (2 or more).
  canonical_name: slug, lowercase, ≤64 chars, verb-first, no proper nouns.
  canonical_category: one preset slug above.
  rationale: one short sentence.

── OUTPUT FORMAT ──

Return ONE JSON object, no fences (empty list is valid if nothing should merge):

{{"groups": [
  {{"members": ["name-a", "name-b"], "canonical_name": "...",
    "canonical_category": "...", "rationale": "..."}}
]}}
"""


# ── synthesis (per merge group) ─────────────────────────────────

SYNTH_SYSTEM = (
    "You are SkillSynapse's consolidator. You merge several related SKILL.md "
    "documents into ONE general, parametric skill. Return ONLY a single JSON "
    "object. No prose, no markdown fences."
)


def build_synthesis_prompt(canonical_name: str, members: list[SkillRecord]) -> str:
    blocks = []
    for m in members:
        body = (m.content or "").strip()[:3500]
        blocks.append(
            f"### MEMBER: {m.name}\n"
            f"description: {m.description}\n"
            f"category: {m.category}\n"
            f"tools: {', '.join(m.tool_dependencies) or '(none)'}\n\n{body}"
        )
    joined = "\n\n---\n\n".join(blocks)
    return f"""\
Unify the {len(members)} skills below into ONE general skill named "{canonical_name}".

RULES
- ONE parametric SKILL.md covering every member. Where members differ (board
  type, camera model, dataset, file names), express the difference as a PARAMETER
  or an example inside the body — never bake one instance's proper nouns into the
  steps' assumptions.
- Keep the UNION of concrete commands and pitfalls; drop redundancy.
- description: one verb-first line, NO project-specific proper nouns (file names,
  scene IDs, hardware model numbers).
- Keep it a real SKILL.md: short intro, numbered steps, commands, pitfalls.

── MEMBERS ──

{joined}

── OUTPUT ──

Return ONE JSON object, no fences:

{{"name": "...", "description": "...", "category": "<preset-slug>", "content": "<full SKILL.md>"}}
"""


# ── generalize (batched description rewrite) ────────────────────

GENERALIZE_SYSTEM = (
    "You are SkillSynapse's editor. You rewrite over-specific skill descriptions "
    "into general, reusable ones. Return ONLY a single JSON object. No prose, no "
    "markdown fences."
)


def build_generalize_prompt(items: list[SkillRecord]) -> str:
    listing = "\n".join(
        f'{i+1}. name: {s.name}\n   description: {(s.description or "").replace(chr(10)," ")[:400]}'
        for i, s in enumerate(items)
    )
    return f"""\
Rewrite each skill's description to describe the GENERAL reusable capability.

HARD RULE — the rewritten description (and name) must contain NO project-specific
proper nouns: no source file names (rig.json, gt_pipeline), dataset/scene IDs
(scene_042, eth3d), or hardware model numbers (Gemini 335Le, Jetson Orin). Keep
it one verb-first line answering "when would I use this?". Keep genuinely useful
scope facts (language, OS, input/output kind). If a description is already clean,
return it unchanged. Only lightly adjust `name` if it also carries a proper noun.

  bad : "Refine a box GT pose against its SfM cloud for the Gemini 335Le rig
         using scene_XXX/rig.json"
  good: "Refine a fitted 3D cuboid pose against an SfM point cloud when one face
         is under-constrained"

── SKILLS ──

{listing}

── OUTPUT ──

Return ONE JSON object, no fences, one entry per input index:

{{"items": [{{"index": 1, "name": "...", "description": "..."}}]}}
"""


@dataclass
class ConsolGroup:
    members: list[str]
    canonical_name: str
    canonical_category: Optional[str]
    rationale: str = ""


@dataclass
class GenProposal:
    skill: SkillRecord
    new_name: str
    new_description: str


@dataclass
class ConsolStats:
    skills_before: int = 0
    merge_groups: int = 0
    merged_survivors: int = 0
    deactivated: int = 0
    generalized: int = 0
    skills_after: int = 0
    errors: list[str] = field(default_factory=list)


def _preset_categories_block(store: Store) -> str:
    cats = [c for c in store.list_categories() if not c["manual_only"]]
    return "\n".join(f"  {c['slug']:20s} {c['description']}" for c in cats)


def _valid_category(store: Store, cand: Optional[str], fallback: Optional[str]) -> Optional[str]:
    valid = store.preset_category_slugs()
    return cand if cand in valid else fallback


def _union(*lists: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for lst in lists:
        for x in (lst or []):
            if x and x not in seen:
                seen.add(x)
                out.append(x)
    return out


def plan_merges(
    store: Store, llm: LLMProvider, cfg: Config, *, timeout: int = 300
) -> tuple[list[ConsolGroup], dict[str, SkillRecord]]:
    """Clustering call → merge groups. Raises LLMError / RateLimitDeferred."""
    skills = store.list_active_skills()
    by_name = {s.name: s for s in skills}
    out = llm.call(
        build_cluster_prompt(skills, _preset_categories_block(store)),
        system=CLUSTER_SYSTEM, timeout_seconds=timeout,
    )
    raw = _parse_llm_json(out)
    if raw is None or "groups" not in raw:
        raise LLMError("consolidator: cluster call returned no parseable groups")

    groups: list[ConsolGroup] = []
    used: set[str] = set()
    for g in raw.get("groups", []):
        members = [m for m in (g.get("members") or [])
                   if m in by_name and m not in used]
        if len(members) < 2:
            continue
        used.update(members)
        groups.append(ConsolGroup(
            members=members,
            canonical_name=(g.get("canonical_name") or members[0]).strip(),
            canonical_category=(g.get("canonical_category") or by_name[members[0]].category),
            rationale=(g.get("rationale") or "")[:200],
        ))
    return groups, by_name


def plan_generalizations(
    store: Store, llm: LLMProvider, cfg: Config, *,
    exclude: set[str], batch: int = 12, timeout: int = 200,
) -> list[GenProposal]:
    """Batched rewrite of over-specific descriptions among active skills not in
    `exclude` (the to-be-merged members). Best-effort per batch."""
    cand = [s for s in store.list_active_skills()
            if s.name not in exclude and _PROPER_NOUN.search(s.description or "")]
    props: list[GenProposal] = []
    for i in range(0, len(cand), batch):
        chunk = cand[i:i+batch]
        try:
            out = llm.call(build_generalize_prompt(chunk),
                           system=GENERALIZE_SYSTEM, timeout_seconds=timeout)
            raw = _parse_llm_json(out) or {}
        except LLMError:
            continue  # skip this batch; others still land
        for item in raw.get("items", []):
            try:
                idx = int(item.get("index", 0)) - 1
            except (TypeError, ValueError):
                continue
            if not (0 <= idx < len(chunk)):
                continue
            s = chunk[idx]
            new_desc = (item.get("description") or s.description).strip()
            new_name = (item.get("name") or s.name).strip()
            if new_desc != (s.description or "").strip() or new_name != s.name:
                props.append(GenProposal(s, new_name, new_desc[:1024]))
    return props


def _synthesize(group: ConsolGroup, members: list[SkillRecord],
                store: Store, llm: LLMProvider, *, timeout: int = 300) -> Optional[SkillRecord]:
    out = llm.call(build_synthesis_prompt(group.canonical_name, members),
                   system=SYNTH_SYSTEM, timeout_seconds=timeout)
    synth = _parse_llm_json(out) or {}
    content = synth.get("content")
    if not content:
        return None
    name = (synth.get("name") or group.canonical_name).strip()
    existing = store.get_skill_by_name(name, active_only=True)
    member_ids = {m.skill_id for m in members}
    if existing and existing.skill_id not in member_ids:
        name = f"{name}-merged"
    now = iso_now()
    # Member content was scrubbed at ingest, but the LLM can still surface
    # credentials from elsewhere in its context — scrub before persisting.
    return SkillRecord(
        skill_id=str(uuid4()),
        name=name,
        description=scrub((synth.get("description") or "").strip()[:1024]),
        category=_valid_category(store, synth.get("category") or group.canonical_category, members[0].category),
        content=scrub(content),
        origin="derived",
        version=1,
        probation=True,
        parent_skill_ids=[m.skill_id for m in members],
        change_summary=f"Consolidated from {len(members)} skills: {', '.join(m.name for m in members)}",
        created_at=now,
        updated_at=now,
        tool_dependencies=_union(*[m.tool_dependencies for m in members]),
        critical_tools=_union(*[m.critical_tools for m in members]),
        tags=_union(*[m.tags for m in members]),
        source_sessions=_union(*[m.source_sessions for m in members]),
    )


def apply_consolidation(
    groups: list[ConsolGroup], by_name: dict[str, SkillRecord],
    gen_props: list[GenProposal], store: Store, llm: LLMProvider, cfg: Config,
) -> ConsolStats:
    st = ConsolStats(skills_before=len(by_name), merge_groups=len(groups))

    for g in groups:
        members = [by_name[m] for m in g.members if m in by_name]
        if len(members) < 2:
            continue
        try:
            survivor = _synthesize(g, members, store, llm)
        except RateLimitDeferred:
            raise
        except LLMError as e:
            st.errors.append(f"{g.canonical_name}: synthesis failed ({e})")
            continue
        if survivor is None:
            st.errors.append(f"{g.canonical_name}: synthesis returned empty content")
            continue
        store.upsert_skill(survivor)
        store.log_decision("consolidated", skill=survivor,
                           details={"members": [m.name for m in members], "rationale": g.rationale})
        st.merged_survivors += 1
        for m in members:
            store.deactivate_skill(m.skill_id, action="merged",
                                   details={"survivor": survivor.name, "survivor_id": survivor.skill_id})
            st.deactivated += 1

    for p in gen_props:
        rec = store.get_skill_by_name(p.skill.name, active_only=True)
        if rec is None:  # got merged away between plan and apply
            continue
        rec.name = p.new_name or rec.name
        rec.description = (p.new_description or rec.description)[:1024]
        rec.updated_at = iso_now()
        store.upsert_skill(rec)
        store.log_decision("generalized", skill=rec, details={"old_name": p.skill.name})
        st.generalized += 1

    st.skills_after = len(store.list_active_skills())
    return st


# ── deterministic plan / apply (persist the LLM decisions, apply from file) ──
#
# The clustering + generalize calls are non-deterministic: re-running them at
# apply time can pick a different merge set than the one you reviewed. So the
# review→apply boundary must cross a PERSISTED plan, not a second LLM call.
# `plan_consolidation` makes all the decisions once and returns a JSON-able dict;
# `apply_plan` consumes exactly that dict — the only LLM calls it makes are the
# per-group body syntheses, whose membership is already locked by the plan.

PLAN_VERSION = 1


def plan_consolidation(store: Store, llm: LLMProvider, cfg: Config) -> dict:
    """Run every non-deterministic decision (clustering + generalize) once and
    return a serializable plan. Raises LLMError / RateLimitDeferred."""
    groups, by_name = plan_merges(store, llm, cfg)
    merged = {m for g in groups for m in g.members}
    gens = plan_generalizations(store, llm, cfg, exclude=merged)
    return {
        "version": PLAN_VERSION,
        "created_at": iso_now(),
        "skills_before": len(by_name),
        "merge_groups": [
            {"members": g.members, "canonical_name": g.canonical_name,
             "canonical_category": g.canonical_category, "rationale": g.rationale}
            for g in groups
        ],
        "generalizations": [
            {"skill_name": p.skill.name, "new_name": p.new_name,
             "new_description": p.new_description}
            for p in gens
        ],
    }


def apply_plan(plan: dict, store: Store, llm: LLMProvider, cfg: Config) -> ConsolStats:
    """Apply a persisted plan deterministically. Membership + generalization
    wording are fixed by `plan`; only per-group body synthesis calls the LLM.
    Skills that no longer exist (already merged in an earlier apply) are skipped."""
    if plan.get("version") != PLAN_VERSION:
        raise LLMError(f"consolidator: unsupported plan version {plan.get('version')}")
    by_name = {s.name: s for s in store.list_active_skills()}
    groups = [
        ConsolGroup(
            members=[m for m in g["members"] if m in by_name],
            canonical_name=g["canonical_name"],
            canonical_category=g.get("canonical_category"),
            rationale=g.get("rationale", ""),
        )
        for g in plan.get("merge_groups", [])
    ]
    gen_props: list[GenProposal] = []
    for gp in plan.get("generalizations", []):
        s = by_name.get(gp["skill_name"])
        if s is None:
            continue
        gen_props.append(GenProposal(
            s, gp.get("new_name") or s.name, gp.get("new_description") or s.description,
        ))
    return apply_consolidation(groups, by_name, gen_props, store, llm, cfg)
