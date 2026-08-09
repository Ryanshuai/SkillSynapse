# SkillSynapse

Distills reusable skills out of Claude Code session logs. **You run it**; there is
no scheduler, on purpose.

Part of what you do with Claude Code every day is a **reusable procedure** — but it only
exists as a chat transcript sitting in `~/.claude/projects/**/*.jsonl`, so next time you
explain it all over again. SkillSynapse scans those logs when you ask it to and turns the parts
worth keeping into `~/.claude/skills/<name>/SKILL.md`, so Claude Code already knows them
next time.

---

## ⚠️ Read this first: raw session logs are confidential

**This tool reads your complete conversation transcripts and tool-call records** — which
may contain credentials, internal source, or customer data. The repository's first rule is:

> **Raw session JSONL must never leave the private network** (Tailscale mesh / LAN).
> The only sanctioned outbound path is the session brief and skill files *after* they pass
> through `scrub()` in `src/skillsynapse/sanitizer.py`.

The full prohibition list (never commit, never paste into an issue, never upload to a public
service, Syncthing must have every public code path disabled) lives in [CLAUDE.md](CLAUDE.md).
Read it end to end before touching anything. Test JSONL is **always hand-built synthetic data**.

> **One question is still open for multi-person use.** Everyone's session logs belong to
> them. The transport design in this repo ([docs/02](docs/02-transport-and-security.md)) is a
> **single-owner, many-machines** model, not a **many-people** one. Before running this across
> a team, answer: whose logs aggregate where, and who may read them. This repo does not
> currently have an answer.

---

## What actually works today

| | Status |
|---|---|
| **Inductive loop** (session → episode slicing → LLM extraction → SKILL.md → rendered index) | ✅ working, v0.1 |
| Scrubbing via `scrub()` (both the session brief and the skill written to disk) | ✅ wired in |
| Headless history isolation (its own `CLAUDE_CONFIG_DIR`, never pollutes your real CC history) | ✅ wired in |
| Multi-machine aggregation of **session logs** (Syncthing over Tailscale, send-only → hub) | ✅ verified in practice |
| Multi-machine sync of **skills** (`skills-canon`, bidirectional, 3 machines) | ✅ live 2026-08-08 |
| Agent merge of divergent skills (`merge_conflicts.py`, 5-min scan + hourly arbitration) | ✅ live 2026-08-08 |
| Skill dedup / merge (`consolidator.py` — clusters *differently-named* near-duplicates; not the same problem as `merge_conflicts.py`, which merges *one* skill's two versions) | ⚠️ code exists, **has no entry point**, has never run |
| Directed loop / toil loop / marking signal / triage & ranking / prune | 📐 design docs only, no implementation |

**Biggest gap right now:** every skill produced goes live with equal weight, and the library
only grows — there is no priority ordering and no human gate. Design in
[docs/07](docs/07-triage-and-ranking.md). That gap now has teeth it did not have in v0.1: `merge_conflicts.py` rewrites skills unattended and syncthing puts the result on all three
machines within seconds. `merge_gate()` is the only thing standing between a bad merge and
every machine, and it checks shape (size, frontmatter, skill name), not correctness.

## Running it

```bash
pixi install
pixi run python -m unittest discover -s tests     # 31 sanitizer tests

skill list          # existing skills
skill show <name>   # one skill's body + metrics
skill health        # library-wide health

skillsynapse --dry-run        # no LLM calls, no SKILL.md written
skillsynapse --hours-back 48  # a real run
```

⚠️ **`ANTHROPIC_API_KEY` must not be present in the environment** — if it is, you bypass
your subscription and switch to metered API billing.

⚠️ **`--dry-run` is not read-only.** Step 0's manual-skill discovery and Step 2's metrics
collection both write to `~/.claude/skillsynapse/db.sqlite` *before* the dry_run check. For a
genuinely side-effect-free trial, point `paths.*` at a temporary directory — see "Testing and
verifiability" in [docs/README.md](docs/README.md).

Data directory: `~/.claude/skillsynapse/` (`db.sqlite` / `logs/` / `config.yaml`).

## Code map

```
src/skillsynapse/
  main.py              nightly pipeline orchestration (the cron entry point)
  scanner.py           walks .jsonl, parses it into an event stream
  episode_detector.py  splits one session into stretches of coherent work
  extractor.py         one LLM pass per episode: NEW / UPDATE / PITFALL / SKIP
  indexer.py           writes SKILL.md, renders _index.md / _categories.md
  metrics.py           re-reads sessions to score how often a skill is picked and completed
  store.py             SQLite + a decisions.jsonl audit trail
  sanitizer.py         scrub() — the single outbound sanitization gate
  consolidator.py      dedup / merge (not wired to any entry point)
deploy/syncthing/      multi-machine aggregation (run once per machine, idempotent)
docs/                  design docs; start at docs/README.md
```

## Known traps

- **Only 1 of 16 modules has tests** (just `sanitizer.py`). Changing anything else has no
  verification floor — add tests first, or sandbox it in a temp directory and diff the
  before/after output by hand.
- `consolidator.py` is 448 lines of complete implementation with no CLI entry point, and
  `decisions.jsonl` has no record of it ever running. Don't extend it before deciding whether
  it should exist at all.

## Design docs

Enter through [docs/README.md](docs/README.md) — it has the big picture, the glossary, and
the reading order.
