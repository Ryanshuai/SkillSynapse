# 04 · SkillSynapse's three evolution loops: inductive × directed × toil

> Reader document. The three paths by which reusable skills grow out of the session corpus.
> The corpus foundation is in [01](01-corpus-and-archive.md); **intensity levels, git
> publishing and the aggregator base** are in [03](03-shared-primitives.md). This page covers
> only each loop's own logic and where it sits.
>
> **Decided this round**: the directed loop **biases only, it never synthesizes**; goals are
> **mostly manual, plus suggestions from recurring gaps**; the toil loop **writes drafts to
> staging for approval** and never changes the environment on its own.

## 1. Today: only the inductive loop exists

The current pipeline (`run_pipeline` in `main.py`) is purely bottom-up and reactive:

```
real session → episode slicing → extractor (NEW/UPDATE/PITFALL/SKIP) → write skill
→ metrics scoring → evolution gate
```

All four of `extractor.py`'s actions are **reactions** to sessions that already happened;
capabilities you have never exercised can never grow. This line is good at "generalizing what
you already did well into something reusable", but it is the only line, and it has two blind
spots: ① capabilities you **want to develop but have not systematically done yet**; ② the
tedious, slow grind you **keep repeating** that should have been automated away. Two proactive
lines fill those in.

## 2. The three loops at a glance {#three-loops}

| Loop | Signal | Processing unit | Output | Score | Status |
|---|---|---|---|---|---|
| **inductive** | "this session was a good reusable procedure" | one episode | SKILL.md | `completion_rate` | exists (v0.1) |
| **directed** | "a capability you declared you want to develop" | a goal | extraction bias + directed gaps + a progress board | `coverage × health` | §3 |
| **toil** | "one mechanical action recurring across days" | **cross-session patterns** | slash-command / script / hook | `toil_saved` | §4 |

In one line: **inductive generalizes what you did well; directed chases what you declared;
toil kills the grind you keep repeating.**

**They converge** on the same `skills` table and the same probation/metrics gate
(`metrics.py`). The directed and toil loops both enter that existing verification pipeline
"from the top" — anything proactively produced still cannot escape "nobody used it in reality,
so it gets pruned", and the pool stays clean.

**Where they sit** (intensity defined in [03 §1](03-shared-primitives.md#intensity)):
inductive = 4, live; toil = 3, draft; directed = 2, suggest. Toil can be raised to 4 once it
runs stably; an unfamiliar direction can be pinned at 1 to watch without acting.

**Output history**: file-shaped output that reaches level 4 carries git history; layout,
rollback and the blast-radius cap are in [03 §2](03-shared-primitives.md#git-publishing).

## 3. The directed loop {#directed-loop}

### 3.1 A new first-class entity: the capability goal

A new table, `capability_goals`:

```
id, name, description         -- "one-command DFSfM deploy to any bare machine" / "master multi-machine log aggregation"
rubric: list[sub-capability]  -- the LLM expands description into a checklist of sub-capabilities
linked_skill_ids              -- which existing skills serve this goal
priority, status, created_at
strength: 1..3                -- this goal's discrete intensity (same scheme as [03 §1], applied to one direction)
coverage_score, health_score  -- progress (§3.3)
```

**Each goal carries its own intensity**: "I want to develop X" can mean light attention or a
main push. Low intensity = only surface gaps in this direction; high intensity = aggressively
lower `min_tool_calls` and mine even failed/exploratory sessions (the strength of hook 1 in
§3.2 is controlled by this).

### 3.2 Four hooks (all reusing existing mechanisms; **no proactive synthesis**) {#four-hooks}

Skills still come 100% from real sessions; the directed loop only "looks more closely toward the
goal + points out gaps + shows progress + suggests goals".

1. **Extraction bias** — active goals are injected into `build_extraction_prompt`. For sessions
   that hit a goal's domain: lower `min_tool_calls`, lean toward NEW/UPDATE, and **mine failed
   or exploratory sessions too** (today those get SKIPped outright by `AD_HOC_DEBUG` /
   `SESSION_FAILED` — but in **your declared direction**, a failure is signal, not noise).
   Behaviour on non-goal sessions is completely unchanged.
2. **Directed gaps** — add a `goal_id` column to `coverage_gaps`. For each goal, compute
   coverage: which sub-capabilities in the rubric have **no healthy skill covering them**, and
   emit those as directed gaps attached to the goal — a TODO list for advancing that capability.
3. **Capability scoreboard** — add a per-goal progress view to `indexer.py`: `coverage%` (share
   of the rubric covered) + `health%` (weighted `completion_rate` of those skills) + "the next
   gap". This is the evolution dashboard you can **watch it grow** on.
4. **Where goals come from** — mostly manual, plus automatic suggestions: `skill goal add "…"`
   declares one by hand (the authoritative source); a nightly job clusters **recurring
   coverage_gaps** ([aggregator](03-shared-primitives.md#aggregator)) and produces "you seem to
   keep getting stuck on X — want to make it a goal?" — **it only becomes a goal once you
   confirm**.

### 3.3 Why no proactive synthesis (decided this round)

The aggressive version would let the LLM "invent seeded skills out of thin air" from rubric gaps.
Rejected: imagined skills pollute the pool, and when you have not actually done the thing, the
synthesized steps are most likely wrong. The conservative version holds the line that **skills
only grow out of reality**, and the directed loop is just "a more focused magnifying glass". The
cost: when a goal direction has no existing skill, you have to go do the work yourself — which
is exactly what "evolution is driven by reality" was supposed to mean.

## 4. The toil loop — killing repeated grind

### 4.1 The essence: the signal is "repetition across days", not "quality of one run"

The inductive loop looks at whether one session was good; the toil loop looks at whether **the
same mechanical action happens day after day**. So it must **aggregate across sessions** and
cannot judge per session. A new module, `toil_miner.py`, runs cross-session (reusing the
[aggregator](03-shared-primitives.md#aggregator)). The criteria: time-consuming, annoying, slow
and error-prone for a human, but **easy and reliable for an AI**.

### 4.2 Toil scoring {#toil-score}

```
toil_score = frequency × human_cost × ai_fitness
```

| Factor | Meaning | Data source (all read from JSONL, no instrumentation) |
|---|---|---|
| `frequency` | "repetition" | cross-session counts of canonicalized Bash commands / tool-sequence n-grams; similar first-user-messages clustered by the LLM |
| `human_cost` | "slow, annoying, error-prone" | tool-step count spent on the same intent, `session_error_rate`, hits of the existing `_CORRECTION_RE` in `metrics.py` ("wrong / redo / that's not it") |
| `ai_fitness` | "easy and reliable for an AI" | the LLM judges "determinate input → determinate output, no human call needed". **An action requiring human judgement is not toil** (that is judgement, and it should not be automated) |

### 4.3 Output: executable automation, not a SKILL.md for humans to read

High-`toil_score` candidates should produce something directly runnable, in three tiers by how
much the AI touches:

- **slash-command** (the main one): `/xxx` wraps the whole grind. It plugs straight into the
  existing feedback loop — metrics already tracks slash-command usage, so you can measure "after
  automating it, are you still doing this by hand?"
- **script / Makefile / pixi task**: anything purely deterministic gets wrapped into a script,
  and the skill just says "run `pixi run xxx`".
- **hook**: genuinely mindless repetition (lint before every commit) goes into settings.json.
  ⚠️ Hooks have the largest blast radius, so they are **capped at level 3, drafts only** (see
  [03 §2.4](03-shared-primitives.md#git-publishing)).

### 4.4 Delivery: **generate a draft, wait for approval** (decided this round, level 3)

At night, write slash-command/script **drafts** into staging (`_pending/<candidate-name>/`: the
draft body plus a `RATIONALE.md` recording how many times it recurred, the `toil_score`
breakdown, and the suggested packaging form). Generated but never auto-enabled — review in the
morning, promote what you like, delete what you don't. Red line: **never auto-modify
settings.json, and never write a real executable onto the live path.**

### 4.5 The verification loop: "still grinding after automating" is the strongest iterate-again signal

Once shipped and enabled, if that grind is **still being done by hand**, the automation failed
(unusable, or undiscoverable): `toil_score` rises rather than falls, and the system pushes it back
to the front of the queue. Conversely, manual recurrence dropping to zero means that piece of toil
was successfully killed, and the candidate is archived.

## 5. Integration surface (touching existing code as little as possible)

| Location | Change |
|---|---|
| new `goals.py` + `capability_goals` table | directed loop |
| new `toil_miner.py` + `automation_candidates` table (or a `kind` column on skills) | toil loop |
| new `aggregator.py` (see [03 §3](03-shared-primitives.md#aggregator)) | shared by both loops |
| `goal_id` column on `coverage_gaps` | directed gaps |
| a goals section in `build_extraction_prompt` | extraction bias |
| per-goal progress view in `indexer.py` | scoreboard |
| `skill goal add/list`, `skill toil list` in `commands.py` | CLI |
| new steps in `main.py`: directed-suggestion clustering, toil mining (both produce staging, never auto-live) | orchestration |
| **probation / metrics / gates** | **completely unchanged** — all three loops share one set |

`SkillRecord.origin`: the directed loop adds nothing (it only biases); toil output, if merged into
the skills table, gets `origin="toil"` + `artifact_kind ∈ {command, script, hook}`.

## 6. Delivery order

1. **Toil loop first** — the most direct payoff and the most independent (it does not depend on
   the goal concept); `toil_miner` + staging alone already produces a "things to automate" list.
2. **The `aggregator` base** — extract cross-session aggregation out of toil_miner (see
   [03 §3](03-shared-primitives.md#aggregator)).
3. **The directed loop** — `capability_goals` + extraction bias + gap-clustering suggestions
   (reusing aggregator) + the scoreboard.

Each of the three can ship and be accepted independently; there is no need to wait for all of them.
