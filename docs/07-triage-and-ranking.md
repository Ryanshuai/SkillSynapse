# 07 · Triage and ranking: order the candidates, and a human does only the important ones

> A cross-cutting document, acting on the **exits** of [04, the three loops](04-skillsynapse-loops.md)
> and [05, marking](05-marking-signal.md). Intensity / staging / aggregator are in
> [03](03-shared-primitives.md); this page covers only the new dimension: **priority**.
>
> **Decided this round**: ① ranking sits on the **output side** (rank after extraction), not the
> candidate side — quota is plentiful, so don't gate upstream to save LLM calls, and a human judges
> a complete draft far better than raw episode metadata; ② the inductive loop drops from level 4 to
> level 3, and only `skill review accept` makes something live; ③ **scores are computed entirely
> automatically; the human only adjudicates** (accept/reject/defer) and is never asked to score;
> ④ ranking is not filtering — low-scoring candidates stay in the queue, and "do only the important
> ones" means only **promoting** the important ones, not only **extracting** them; ⑤ no score is ever
> high enough to skip the human.

## 1. Today: only "produce or not", never "which one first"

Every existing decision is **binary**, and not one of them is "a human picking priorities up front":

| Existing mechanism | Where | Is it ranking? | Is it a human gate? |
|---|---|---|---|
| extractor's `NEW / SKIP` | `extractor.py` | ❌ binary, per episode, no cross-session comparison | ❌ fully automatic |
| `probation` + `metrics` | the probation flip at the end of `collect_metrics()` in `metrics.py` | ❌ looks at usage after the fact, not ranking before it | ❌ fully automatic |
| the `pruning:` config block | `pruning:` in `config_default.yaml` | — | **config exists, code does not** (deferred in v0.1) |
| the `pending_changes` table | `CREATE TABLE pending_changes` in `store.py` | ❌ | **an empty shell**: 0 rows, no writer; only `count_pending_reviews()` is read, by `/skill health` |
| consolidator `plan → apply` | `plan_consolidation()` / `apply_plan()` in `consolidator.py` | ❌ handles dedup/merge | ✅ **the only human-confirmation boundary that actually exists**, but it has nothing to do with importance |
| indexer rendering | the module docstring in `indexer.py` | ❌ the comment says outright "no runtime ranking, no top-k truncation" | ❌ |
| aggregator "cluster → rank → human confirms" | [03 §3](03-shared-primitives.md#aggregator) | ✅ in the design, **no code** | ✅ in the design, **no code** |

The consequence is **grows only, never shrinks, everything equally weighted**: `realize_candidate`
writes straight to level 4, prune was never implemented, so every qualifying candidate is written
indiscriminately into `~/.claude/skills/` — **and every active skill costs context budget in every
CC session**. That is a real cost, not an aesthetic complaint. This document adds the missing
dimension: **priority plus human triage**.

## 2. priority_score: multiplicative, isomorphic to toil_score

Following the multiplicative style of [04 §4.2](04-skillsynapse-loops.md#toil-score), five factors,
**all computable automatically from JSONL and existing tables**:

```
priority = repeat × cost × novelty × mark_boost × recency
```

| Factor | Meaning ("why it matters") | Data source | Rough range |
|---|---|---|---|
| `repeat` | **how many times you've done this** — the strongest proxy for importance | the [aggregator](03-shared-primitives.md#aggregator); until it lands, use the `session_index` FTS to count near neighbours of the episode summary | `1 + log₂(n)` |
| `cost` | **how expensive this run was** — the more expensive, the more worth distilling | the episode's tool-call count / time span (`Episode.start_time/end_time`); hits of the existing `_CORRECTION_RE` in `metrics.py` | 0.5 – 2.0 |
| `novelty` | **whether the pool already covers it** — down-weight what existing skills cover | description similarity against active skills (reusing the consolidator's clustering criteria); a bonus for hitting a `coverage_gaps` entry | 0.2 – 1.5 |
| `mark_boost` | **a human/agent stamped it live** ([05](05-marking-signal.md)) | the provenance weight of `SessionMeta.marks` (human 1.0 / agent 0.4) | 1.0 / 1.5 / 3.0 |
| `recency` | **whether you're still doing it** — something done once three months ago sinks | episode time vs now, 30-day half-life | 0.3 – 1.0 |

- **Where it's stored**: add two columns to `pending_changes` — `priority_score REAL` and
  `score_breakdown TEXT` (JSON, one entry per factor). Reuse the existing empty shell; no new table.
- **Auditable**: every scoring run writes the breakdown to `decisions.jsonl`, so "why was this
  first?" is answerable afterwards.
- **Red line**: a score **only ranks, it never promotes**. Isomorphic to `probation_floor_uses` in
  [05 §4](05-marking-signal.md) — ranking buys "which one to look at first", not exemption from review.

## 3. `skill review`: the human triage desk

```
$ skill review
 #  SCORE  REPEAT COST NOV  MARK    NAME                          WHY
 1   8.4   ×4     ×1.8 1.2  ⚑human  deploy-syncthing-over-tailnet 4 cross-machine deploys, avg 37 steps + 2 redos
 2   3.1   ×2     ×1.1 0.9  -       sfm-scale-from-rig-baseline   2 times, 21 steps each
 3   1.2   ×1     ×0.8 0.4  -       fix-one-off-yaml-typo         once; the pool already has a near neighbour
                                              (17 more below threshold — `skill review --all`)

$ skill review accept 1 2        # level 3 → 4: promote to active + write SKILL.md + symlink
$ skill review reject 3 --note "one-off"
$ skill review defer 4           # stays in the queue, keeps participating in ranking
```

- `accept` is the promote action from [03 §2.3](03-shared-primitives.md#git-publishing) and the
  **only** door onto the live path. It decides *whether a skill goes live at all*; what it may then
  do to the local machine is a separate question, answered by the tier in
  [08](08-capability-and-permission.md#three-axes) — an accepted skill still **starts at the
  quarantine tier**. Priority is not trust.
- `reject` is recorded in `decisions.jsonl` and **kept as a negative sample**: rejected features are
  recorded only, with no online learning (so early noise can't feed back into the scoring).
- **A non-interactive CLI first, no TUI**: scriptable, able to emit a "N candidates awaiting triage
  tonight" report from cron, and easier to test.

## 4. Caps: ranking controls order, budget controls total

Ranking only answers "which one first", not "how many in total". Two hard gates:

```yaml
# new in config_default.yaml
review:
  top_n: 10                  # only the top N enter the triage queue each night; the rest sink (still in the library, visible with --all)
library:
  max_active_skills: 40      # soft cap: over it, review also suggests what to prune
```

Over the cap, `skill review` shows **pruning suggestions** on the same screen (ascending
`effective_rate` + long-standing probation entries with zero selections), filling in the prune that
v0.1 deferred with a **human gate** — steadier than writing another automatic prune gate, and usable
immediately.

## 5. Triaging the existing library (step 0, depends on none of the above)

`~/.claude/skills/` already holds 23 skills (14 captured, all still in probation, never pruned), and
they are **loaded indiscriminately into every session's context**. Start with one offline scoring
pass: score existing active skills with the same formula from §2 (using `source_sessions` count as a
proxy for `repeat`, and `cost` = 1 where there is no episode history), then let a human go through
them once with `skill review --existing` and cut what should not be resident.

## 6. Delivery order

1. **`priority.py` scoring + two columns on `pending_changes`** — pure computation, changes no
   existing behaviour, unit-testable.
2. **`skill review` list/accept/reject/defer** — the queue runs empty at first, but a human can
   already use it.
3. **Drop the inductive loop to level 3** (`loops.inductive.intensity: 3`): `realize_candidate`
   writes to `_pending/` instead, and new candidates enter the queue by default.
4. **Triage the existing library** (§5).
5. **Wire `repeat` to the real aggregator** ([03 §3](03-shared-primitives.md#aggregator)) — using the
   FTS proxy until then.

Steps 1 and 2 change no existing behaviour; step 3 is where the switch flips — so it can be rolled
out gradually and rolled back.
