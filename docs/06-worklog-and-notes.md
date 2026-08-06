# 06 · WorkLog × Notes: distilling episodic and semantic memory from the corpus

> Reader document, a **peer** of [04 SkillSynapse](04-skillsynapse-loops.md) (it is not one of the
> three loops). Defines two new corpus "readers" — **WorkLog** (episodic memory, "what happened")
> and **Notes** (semantic memory, "what I looked up, what I learned"). The corpus foundation and
> archiving are in [01](01-corpus-and-archive.md); the hub runtime and its resident loops are in
> [02 §4](02-transport-and-security.md#hub-role); intensity and the aggregator are in
> [03](03-shared-primitives.md).
>
> **Decided this round**: ① WorkLog/Notes and SkillSynapse are **peer readers** sharing the corpus
> foundation and the extraction pass, not part of the three loops; ② stepwise pointer drill-down is
> the only read path, and a claim with a broken pointer must not be written.

## 1. Position: one corpus, three kinds of memory

Borrowing cognitive science's memory taxonomy, the whole ecosystem distills three kinds of memory
from the same experience stream:

| Memory type | Question it answers | System | Output | Shareable? |
|---|---|---|---|---|
| **episodic** | "what happened" | **WorkLog** (this document) | work events, workstream ledgers, daily/weekly/quarterly narrative | private by nature (the evidence chain roots into raw conversation) |
| **procedural** | "how to do this kind of thing" | [SkillSynapse](04-skillsynapse-loops.md) | SKILL.md / slash-command | publishable (a distillate, detached from the raw material) |
| **semantic** | "what is true / what I looked up" | **Notes** (this document) | one-line lessons, question→source mappings | conclusions shareable, evidence private |

The key asymmetry: **a skill is a distillate, detached from its raw material the moment it ships;
WorkLog/Notes are indexes, forever pinning their raw material.** A SKILL.md still works after the
source session is deleted, which is why it can be published over git; every WorkLog claim lives on
its evidence pointer, and a broken pointer degrades it into unverifiable prose. That dictates
completely different storage/publishing/archiving discipline on the two sides
([01 §4 archiving](01-corpus-and-archive.md#archive)).

They are **two readers at the same layer** as SkillSynapse, feeding each other: a self-assessment of
"what skills do I want to develop" becomes a directed-loop goal directly; the skills SkillSynapse
distilled and the toil it killed this quarter are in turn material for "achievements". **The
quarterly self-assessment is just the first query, not the point** — the same ledger layer also
serves "how did I solve X last time" (the highest-frequency daily use), weekly/monthly reports (the
same roll-up over a different window), and **feeding context to future Claude sessions** (checking
"have I done something like this / have I hit this trap" before starting — the most valuable of the
three consumers). **The layer is the product; queries are cheap views.**

## 2. Writing: layered map-reduce, extraction through a router

A quarter's corpus far exceeds any single call's context, so "summarize" has to be a layered,
incremental map-reduce that leaves durable intermediate artifacts. Digest continuously at night,
and the end of the quarter is just one query over those intermediates.

### 2.1 Layer one: episode → router extraction pass (map)

Each [episode](01-corpus-and-archive.md#processing-base) goes through the LLM once and **produces
three streams at the same time** — the corpus is read once, the LLM runs once:

```
episode ──→ extraction pass ──┬─→ work event   (episodic) → WorkLog
                              ├─→ skill candidate (procedural) → SkillSynapse's three loops
                              └─→ knowledge note (semantic) → Notes library
```

The existing NEW/UPDATE/PITFALL/SKIP decision in `extractor.py` is already part of this pass and
will converge into it; early on, WorkLog extraction can run independently to avoid disturbing the
existing pipeline.

**Work event schema** (every field carries an evidence pointer — the only reliable defence against
hallucination):

```yaml
date: 2026-05-14
machine: <hostname>
project: dfsfm                    # the projects/<path> directory name already has it
intent: fix matcher drift in low-texture scenes
outcome: shipped | partial | failed | abandoned
deliverables: [commit a3f21c, PR #47]     # scraped from git commands in Bash calls
challenges: [3h chasing a CUDA OOM; turned out to be a batch-dimension bug]
duration_est: 4h
evidence: [<hostname>/dfsfm/session-uuid.jsonl#L120-L340]
```

`outcome` keeps failed/abandoned — git only records what successfully landed, while "challenges"
hide precisely in the experimental line you ran for three days and then abandoned. Only the session
record has those.

### 2.1.1 Router taxonomy: hard criteria for the three exits

> A router most easily slides back into "classify by topic" (→ and then it fills up with proper
> nouns again). The criteria must bite down on **the essential properties of the memory types** —
> the three kinds of memory are three orthogonal axes, not three mutually exclusive buckets.
> **Each exit passes its own gate independently**; one episode may produce a skill *and* a note
> *and* an event. Modelling this as "pick one of three" is a guaranteed mistake.

**Three hard gates (each yes/no; all must pass for that exit to fire):**

| Exit | Gate (all must pass) | One-line test |
|---|---|---|
| **Skill** (procedural) | **S1 reusable**: holds for the future and for different inputs (not just this one file/scene/camera) · **S2 imperative**: can be written as "do X / run Y" steps · **S3 non-trivial**: contains a decision or ordering that a smart operator would still get wrong | next time, with a different input, would I re-run this procedure? |
| **Note** (semantic) | **N1 propositional**: an assertion of fact or constraint, not an instruction · **N2 durable**: still true outside this run, and I'd want to know it before starting · **N3 earned**: hit at least twice, or discovering it cost something real | is this a fact I want to **know** before starting related work? |
| **WorkEvent** (episodic) | **W1**: something happened that is worth a ledger line — it has an outcome, a deliverable, or a multi-hour challenge | is this a record of "I did X, and on some day it shipped/failed"? |

Notes have two subtypes: **learned facts** (lessons/gotchas/constraints) and **lookup trails**
(question→source mappings). **Note that an SOP is not a Note** — an SOP is procedural memory, shaped
exactly like a SKILL.md, and belongs to [SkillSynapse's inductive loop](04-skillsynapse-loops.md).
N3's repetition gate is isomorphic to the
[aggregator's cross-session pattern detection](03-shared-primitives.md#aggregator) (the toil loop
mines repeated mechanical actions, Notes mines repeated lookups).

**"Neither" has to split into two**: it happened but produced no distillate → **WorkEvent** (this is
what catches failed/abandoned lines, which git does not have); genuinely nothing at all → **SKIP**.

**Two disambiguation rules (specifically for the misclassifications in the skill library):**

- **D1 · method vs conclusion**: split an episode's takeaway into "how I figured it out (method)"
  and "what I concluded (conclusion)". A method that generalizes → Skill; a conclusion about **this
  project / this batch of data** → Note / WorkEvent. **When the method is trivial, only the
  conclusion survives, and it goes to Note.** This cures fake skills of the
  `explain-* / assess-* / decide-whether-*` shape.
- **D2 · instruction vs statement**: imperative ("always lock factory intrinsics before BA") →
  Skill/SOP; declarative ("factory intrinsics are already accurate to 0.2px") → Note. The same
  discovery often produces both: the observation → Note, and the rule it hardens into → SOP/skill.

**Worked examples (against real entries):**

| Candidate | Verdict | Basis |
|---|---|---|
| `calibrate-mono-intrinsics-from-board-captures` | **Skill** | S1-3 all pass; runs on any board type or camera |
| `explain-fiducial-pose-error-vs-distance` | **Note** (fact) | propositional; the plotting method is trivial (D1) |
| `label-rig-intrinsics-provenance-factory-vs-refined` | **Note + WorkEvent** | a finding about this batch of files, with no reusable procedure |
| `set-rig-ba-refine-policy-for-factory-intrinsics` | **Note** (constraint) | the textbook D1/D2 double split |
| "faster-whisper emits 'thanks for watching' on silence" | **Note** | a gotcha; N1-3 all pass |
| "split keyboard = two keyboard devices = Chinese input stalls" | **Note** | the skill exit correctly SKIPped; the gotcha is recovered by Note |
| the keyd config that merges a split keyboard | **Skill** | a reusable procedure |

> **Counter-anchor**: the last two rows come from the same session — the skill exit **correctly
> returned SKIP** (multi-topic-drift), but the real lesson ("a split keyboard makes Chinese input
> stall") would have been thrown away outright without a Note exit. For the same SKIP, a three-way
> router recovers vastly more information. This is the strongest argument for "converge the
> extraction now, don't wait for v0.2".

**Engineering implication**: the router is an upgrade of `extractor.py`. NEW/UPDATE/PITFALL/SKIP
degrade into **the skill exit's verdict**, plus two more exits, `notes[]` and `events[]`, all
produced by **the same LLM call**. The router prompt is organized as "each exit passes its own
gate", never as a three-way choice.

> **Open (cost-sensitive)**: whether a WorkEvent really fires for "nearly every substantive
> episode". If so, the ledger roll-up's attribution workload must be designed for that magnitude —
> it is the upper bound on how often §2.2's "the only step requiring intelligence" is called.

### 2.2 Layer two: two orthogonal roll-up axes

**Time axis (daily digest)**: aggregate per day. Useful operationally, of limited use for quarterly
self-assessment.

**Workstream axis (the ledger) — what the self-assessment actually reads.** The unit of a quarterly
narrative is not a day, it is a *thing*: one workstream may span six weeks, twenty sessions, three
machines. Maintain a ledger incrementally, by theme:

```
workstreams/dfsfm-low-texture.md
  status: shipped (2026-06-10)
  timeline: 04-28 started → 05-14 approach A failed → 05-20 switched to B → 06-10 merged
  output: PR #47, #52; benchmark +12%
  challenges: the three-week detour on approach A (evidence: ...)
```

Nightly job: new work events → decide which existing workstream they belong to (or open a new one) →
update the ledger incrementally. **This is the only step in the whole system that requires
intelligence**; everything else is mechanical aggregation.

### 2.3 Layer three: synthesize at query time

The four self-assessment questions are just different query views over the ledger:

| Question | Query |
|---|---|
| major achievements | workstreams with `outcome=shipped`, ranked by impact of output |
| challenges | each stream's `challenges` field + the `abandoned` streams |
| examples of values | pick concrete events from the workstreams that fit the narrative (with evidence, so the examples are real) |
| skills I want to develop | cluster coverage_gaps → feeds the [directed loop](04-skillsynapse-loops.md#directed-loop) directly |

Input = a few dozen ledger entries + git log, all fitting in one context. **git log is the skeleton,
sessions are the flesh**: first `git log --author --since` across every repo for "output that
definitely happened", then use work events to explain the process behind each one. **Own the blind
spot**: only work visible in a terminal is visible here — meetings, code review, mentoring and
cross-team coordination are not. The draft explicitly states "the following covers coding work
only"; the rest comes from a calendar/Jira (to be wired up later) or is filled in by hand.

## 3. Reading: stepwise pointer drill-down {#drill-down}

Writing is bottom-up; reading is top-down. An evidence pointer is not merely a citation against
hallucination — **it is the index structure itself**. Three rules:

1. **Always enter at the coarsest layer, and stop as soon as you can.** The ledger index (one line
   per stream, a few dozen lines) goes into context in full — that layer is the table of contents,
   read it directly. "What did I do this quarter" is answered by the ledger body, with no drill-down.
2. **Drill down only where detail or verification is needed, following pointers, never full-text
   search.** One ledger stream → the few work events it cites → if necessary, follow `evidence` and
   open that stretch of the raw JSONL. Fan-out is bounded at each hop, so cost is controlled and the
   path is reproducible.
3. **The raw layer is only for verifying and quoting, never for "finding".** If answering a question
   requires grepping the raw JSONL, the intermediate layer's extraction missed something — treat it
   as a bug in the layer-one schema (add a field) rather than as a reason to strengthen search at the
   bottom. This rule forces the intermediate layer to stay good enough to answer questions.

Corollary: **no vector store or RAG is needed.** The top layer is small enough to load entirely, and
every step downward is a deterministic pointer hop with Claude as the navigator. Isomorphic
precedents: the memory system (MEMORY.md index → memory files) and skills (a description line →
the full SKILL.md). **Engineering discipline: every claim at every layer must carry a pointer to the
next layer down, and a claim with a broken pointer is better left unwritten.** Capacity budget:
ledger index ≤ a few K tokens (so it always fits), one stream's body ≤ a few hundred tokens, work
events unbounded (only fetched by pointer).

> The archive, the distillation runtime and pointer dereferencing live on the same machine (see
> [01 §4](01-corpus-and-archive.md#archive) / [02 §4](02-transport-and-security.md#hub-role)); the
> index layer is already local on each machine when work starts (synced downward), so coarse
> questions are answered locally and only evidence requires going back to the hub to dereference
> (degrading to "conclusion without evidence" when offline).

## 4. Intensity and security

- **Intensity** (defined in [03 §1](03-shared-primitives.md#intensity)): WorkLog/Notes output is an
  **incremental private record that changes no live environment**, so its risk surface is far smaller
  than the three loops'. Work events and ledgers go straight to **level 4** (written = in effect);
  Notes start at **level 3** (a staging area a human skims before it enters the index) and move to 4
  once stable.
- **Security**: the evidence chain is private by construction and never leaves the tailnet by
  construction, fitting the
  [network isolation red line](02-transport-and-security.md#security-isolation) exactly — no new
  mechanism needed.

## 5. Delivery order

1. **Fix the archiving bug** ([01 §4.1](01-corpus-and-archive.md#archive-bug)): raise
   `cleanupPeriodDays` on the source machines + add the nightly landing-zone→archive move on the hub.
   Do this first — evidence is being lost every day.
2. **The layer-one map script**: episode → work-event JSONL (extract the episodic stream only at
   first, leaving the extractor alone). Once written it can backfill this quarter's existing corpus
   in one pass and answer this self-assessment directly — no need to wait for the whole system.
3. **Workstream attribution + ledger roll-up**: incremental, nightly.
4. **Query views**: quarterly self-assessment / weekly report / a "have I done something like this"
   slash-command.
5. **The Notes stream + repetition detection**: ride along with the
   [aggregator's cross-session pattern detection](03-shared-primitives.md#aggregator); build them
   together.
6. **Index-layer downward sync + per-machine startup queries**: the most valuable consumer, last
   because it depends on everything above.

Open items: the exact prompt and dedup strategy for workstream attribution; which code Notes
repetition detection shares with the toil loop; whether the hub's dereferencing service is SSH or
MCP; wiring up non-CC signal sources such as a calendar or Jira (to cover the blind spot).
