# SkillSynapse design docs

Distilling reusable knowledge nightly from a multi-machine corpus of Claude Code sessions.
This directory is a layered set of design documents; **this page is the only entrance** —
big picture, glossary, reading order and delivery status all live here, and each sub-document
covers exactly one thing.

## Big picture: one corpus, several readers

The foundation is a single **aggregated corpus of CC session JSONL**. Several independent
"readers" sit on top of it, each reading a different signal.

```
              ┌──────────────── data plane (foundation) ────────────────┐
   per-machine CC JSONL ──[02 transport/Syncthing/Tailscale]──▶ hub landing zone
                                                          │
                                          [01 archive: append-only]
                                                          ▼
                                                    cc-archive/ (bedrock)
                                                          │
                          ┌──────────── episode slicing + extraction pass ────────────┐
              ┌───────────┴───────────┐          ┌───────┴───────┐    ┌────────┴────────┐
   [05 mark]──▶  04 three loops        │          │ 06 WorkLog    │    │ 06 Notes         │
   stamped live  inductive/directed/   │          │ episodic mem  │    │ semantic mem     │
   (best signal) toil → skill/command  │          │ "what I did"  │    │ "what I learned" │
              └───────────────────────┘          └───────────────┘    └─────────────────┘
                      shared: 03 intensity / git publishing / cross-session aggregation
```

In one line: **05 feeds signal → 04 grows skills; 06 distills episodic/semantic memory from
the same corpus; 01/02/03 are the foundation all three share.**
**07 is the gate hanging across all of their exits**: score and rank candidates, and only a
human `accept` puts one on the live path (missing today — everything ships with equal weight).
**08 picks up where 07 lets go**: once a skill is live, what may it actually do to the local
machine — least privilege at skill granularity, enforced at OS level.

## Document map (dependencies top-down, no cycles)

| # | Document | Covers | Depended on by |
|---|---|---|---|
| — | this page | big picture / glossary / reading order / status | — |
| 01 | [corpus-and-archive](01-corpus-and-archive.md) | what the corpus is, the scanner→episode→extractor base, JSONL promoted to an archived asset | 04 / 05 / 06 |
| 02 | [transport-and-security](02-transport-and-security.md) | Tailscale/Syncthing aggregation, **the network isolation red line**, **the sync channel matrix**, the hub's role, deployment | all |
| 03 | [shared-primitives](03-shared-primitives.md) | **intensity levels 0–4**, git publishing / staging model, **cross-session aggregation base** | 04 / 05 / 06 |
| 04 | [skillsynapse-loops](04-skillsynapse-loops.md) | the three loops: inductive / directed / toil | 05 / 06 |
| 05 | [marking-signal](05-marking-signal.md) | the fourth signal, stamped live, feeding the three loops | — |
| 06 | [worklog-and-notes](06-worklog-and-notes.md) | episodic (WorkLog) / semantic (Notes) memory, peers of SkillSynapse | — |
| 07 | [triage-and-ranking](07-triage-and-ranking.md) | **the priority dimension**: candidate scoring + the `skill review` human gate + a library size cap | — |
| 08 | [capability-and-permission](08-capability-and-permission.md) | **least privilege per skill**: manifest × tier, srt enforcement, the audit projection report (agent runtime safety; orthogonal to 02) | 04 (its output runs under these constraints) |

**How to read**: this page → the foundation 01/02/03 → 04/05/06 as they interest you → 07,
which is about the gate on their exits → 08 for agent runtime safety. Each sub-document opens with
only what it **adds**; shared concepts always point back to 03 rather than restating them.

## Glossary (shared across documents; defined only here)

| Term | Meaning | See |
|---|---|---|
| **corpus** | `~/.claude/projects/**/*.jsonl` on each machine — the plaintext session event stream | 01 |
| **episode** | one stretch of coherent work, cut out of a session by `episode_detector` | 01 |
| **extraction pass** | one LLM call per episode, producing skill candidates / work events / notes at once (three exits) | 06 §2 |
| **hub** | the central machine that aggregates, archives, distills and distributes (before the permanent hub is in place = the interim hub) | 02 |
| **mesh** | the Tailscale WireGuard private network; the security boundary *is* the mesh boundary | 02 |
| **intensity** | 0 off / 1 observe / 2 suggest / 3 draft / 4 live — discrete steps for how far a loop acts on its own | 03 |
| **staging `_pending/`** | where intensity-3 drafts land; nothing takes effect until a human promotes it | 03 |
| **mark** | a ground-truth label stamped on a stretch of a session while it happens (learn/pitfall/toil) | 05 |
| **provenance** | where a mark came from: human (weight ≈ 1) / agent (weight < 1) | 05 |
| **aggregator** | the shared base for cross-session pattern clustering → ranking → human confirmation → verification loop | 03 |
| **priority_score** | a candidate's importance, `repeat × cost × novelty × mark × recency`; ranks only, never auto-promotes | 07 |
| **triage** | `skill review`: candidates ordered by priority for a human to accept/reject/defer; only accept reaches the live path | 07 |
| **manifest** | the capability needs a skill declares in SKILL.md (paths / tools / mesh machines / secrets / actions needing approval); versioned with the skill, carried through the DAG | 08 |
| **tier** | the runtime trust level the system grants a skill by reputation (quarantine → privileged); the grant is `manifest ∩ tier` | 08 |
| **srt** | the OS-level sandbox runtime that pins down the `manifest ∩ tier` boundary (deny by default, allowlist only) | 08 |
| **workstream / ledger** | one incrementally-maintained "line of work" spanning many sessions, machines and weeks | 06 §2.2 |

## Security red line (one line; details in 02)

**Raw JSONL is confidential and must never leave the Tailscale mesh.** The only outbound path
is a session brief or skill file that has passed `sanitizer.scrub()`. The full list is in
[02 §security](02-transport-and-security.md#security-isolation) and the repo-root `CLAUDE.md`.
Topology (machine name ↔ tailnet IP) lives only in the gitignored `LOCAL-TOPOLOGY.md`; public
files use placeholders.

> Security has a second, orthogonal axis: **what an agent may do to the local system**, i.e. least
> privilege at skill granularity — see [08](08-capability-and-permission.md). 02 governs the
> outbound data boundary; 08 governs the inbound constraint on agent actions.

## Delivery status (high level; details in each document)

| Component | Status |
|---|---|
| Transport, Syncthing ↔ Tailscale (source machine → interim hub) | ✅ verified in practice (2026-07-03, ~4.9 GB synced) |
| scanner multi-root (`aggregation_root`) | ✅ implemented |
| extractor scrubbing via `scrub()` | ✅ implemented (both session brief and skill written to disk) |
| headless history isolation (`CLAUDE_CONFIG_DIR`) | ✅ implemented |
| Three loops: inductive | ✅ v0.1; directed / toil loops 📐 in design (04) |
| Marking signal | 📐 in design (05) |
| WorkLog / Notes | 📐 in design (06); archive bug outstanding (01 §archive) |
| Ranking / human triage / prune | ❌ not implemented (07): candidates ship with equal weight, the library only grows |
| Least privilege per skill (manifest / tier / srt) | 📐 in design (08) |
| Testing and verifiability | ⚠️ only 1 of 16 modules has tests — see below |

### Testing and verifiability (measured during the 2026-08-06 refactor round)

**Only 1 of 16 modules (`sanitizer.py`) can be verified by a single command.** The rest have no
verification floor — "just run the existing tests" is not a valid premise when changing them,
so the fallback is a sandbox and a by-hand diff of the before/after output.

There used to be a second, subtler layer on top of that: `tests/test_sanitizer.py` was blocked
by `.gitignore` (now fixed). Beyond the obvious "clone it elsewhere and there are no tests",
the real trap was that **any static analysis tool that only looks at `git ls-files` saw a repo
with zero tests** — so `sanitizer.scrub` came up one caller short, and "zero callers" is exactly
what an automated refactor uses to justify deletion. **What gitignore hid was not just a file,
it was evidence.**

**A probe that exercises the pipeline must be fully sandboxed.** `main.run_pipeline(dry_run=True)`
is **not** read-only: Step 0's `bootstrap.discover_manual_skills` and Step 2's `collect_metrics`
both write to the DB *before* the dry_run check. To collect runtime evidence with reasonable
coverage, use `load_config(<temp yaml>)` to point `skills_root` / `data_dir` / `projects_root`
at temporary directories, and feed it **hand-built synthetic JSONL** (security red line: real
session logs never enter a test path).

> These are working drafts, not final specs. Each document keeps its original "decided this
> round" calls and delivery order.
