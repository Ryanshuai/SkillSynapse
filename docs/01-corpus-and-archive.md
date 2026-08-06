# 01 · Corpus foundation and archiving

> Foundation document. Defines what all readers (04/05/06) share: **what the corpus is**,
> **how it gets cut into processable units**, and **why raw JSONL must be archived as an
> unreproducible asset**. Transport and security are in [02](02-transport-and-security.md);
> the shared intensity/aggregation primitives are in [03](03-shared-primitives.md).

## 1. Corpus: plaintext JSONL, read straight off disk

Claude Code writes session records to disk in plaintext — no API needed:

```
~/.claude/projects/<project-path-encoded>/<session-id>.jsonl
```

One JSONL per session, one event per line (user / assistant / tool_use / tool_result /
system), each timestamped. This is the ecosystem's **only first-hand experience stream** —
all three kinds of memory (procedural / episodic / semantic, see
[06 §1](06-worklog-and-notes.md)) are distilled from it.

## 2. Processing base: scanner → episode → extractor {#processing-base}

The existing v0.1 pipeline, shared by every reader as its front end:

```
scanner.py walks ~/.claude/projects/**/*.jsonl
   → episode_detector slices it (one stretch of coherent work = one episode)
   → extractor (the LLM extraction pass)
   → store (SQLite) / indexer rendering
```

- **The episode is the shared processing unit**: the three loops, WorkLog and Notes all read
  the corpus at episode granularity. The extraction pass will eventually converge into "one
  LLM call, several exits" (see [06 §2.1](06-worklog-and-notes.md)); early on each reader can
  run independently without touching the existing pipeline.
- **Multi-machine scanning (implemented 2026-07-03)**: the config key `paths.aggregation_root`
  (set to `~/cc-logs` on the hub) makes `scanner.scan_roots()` enumerate every subdirectory
  underneath it as one machine's root (hostname = directory name), so **onboarding a new
  machine needs no config change**. `SessionMeta.hostname` is injected by the root a session
  belongs to, never inferred back out of the path. Single-machine mode
  (`aggregation_root: null`) behaves exactly as before. The change touches
  `config_default.yaml` / `config.py` / `models.py` / `scanner.py` / `main.py`, and was
  verified against real directories for multi-machine scanning, hostname attribution, and
  subagent exclusion.

> ⚠️ **Do not** point the multi-machine root at the single aggregation root `~/cc-logs/`:
> `scanner._derive_project()` would take `rel.parts[0]` as the hostname instead of the
> project. It does not raise — every session's project silently degrades into a machine name
> and all downstream grouping is wrong. Each root must be `.../<hostname>/` (which is what the
> current design does).

## 3. Preprocessing: compress before feeding the LLM

Raw JSONL carries every tool call's full output. It is extremely verbose, and feeding it
straight to Claude burns hundreds of thousands of tokens for nothing. Anything that goes
through an LLM (the knowledge digest, the extraction pass) is preceded by purely local
preprocessing: find JSONL modified in the recent window, pull out user messages plus key
assistant replies, drop verbose `tool_result` bodies, and group by hostname/project.
**Note the distinction from SkillSynapse extraction**: the extraction pass still consumes a
relatively complete episode (see [06 §2](06-worklog-and-notes.md)); only roll-ups such as the
daily digest consume a heavily compressed brief.

Both the brief fed to the LLM and the skill produced must first pass `sanitizer.scrub()`
(credential-shaped content → `<REDACTED>`). This is the sole outbound constraint of the
[security red line](02-transport-and-security.md#security-isolation).

## 4. Archiving: JSONL promoted to the only unreproducible asset {#archive}

Every intermediate layer (the work-event store, skills, Notes, ledgers) can be rebuilt from
raw material — get the schema wrong and you simply rebuild everything; they are materialized
views. **Once the raw material is gone, it is gone.** So raw JSONL is promoted from
"incidental temporary logs" to "the bedrock of the knowledge base", and that requires archiving
discipline.

### 4.1 A real bug in the current deployment: the evidence chain doesn't survive a month {#archive-bug}

Claude Code's `cleanupPeriodDays` (default 30) purges old sessions from `~/.claude/projects`.
A Syncthing send-only→receive-only topology blocks hub-side changes from flowing back, **but
it does not block deletions propagating from the source** — when CC on a source machine
purges old JSONL, the hub loses it too. Look back over a quarter and the
[evidence pointers from 06](06-worklog-and-notes.md#drill-down) 404 from day 31 onward.
`deploy/syncthing/README.md` does not handle this today. **Do both layers of the fix:**

- Raise `cleanupPeriodDays` in each source machine's settings (treats the symptom; it shrinks
  the "deleted before it synced" window);
- On the hub, treat the Syncthing directory as a **landing zone**, and have a nightly archive
  step move new/changed JSONL into an append-only `cc-archive/`, with **every pointer aimed at
  the archive**. This is cleaner than Syncthing's `ignoreDelete` (which upstream discourages,
  as it causes permanent out-of-sync state).

### 4.2 Archive discipline

- **Append-only**; included in backups (it is the one unreproducible asset).
- Pointer address format is fixed:
  `<hostname>/<project-dir>/<session-id>.jsonl#L120-L340` — the drill-down in
  [06](06-worklog-and-notes.md) depends on it.
- Volume is not a concern: JSONL compresses well; a few GB per year after zstd. Cold segments
  are stored compressed and transparently decompressed when a pointer is dereferenced.

> Archiving is only one of the hub's four responsibilities (archive/distill/index/distribute);
> the hub's full role and its resident loops are in
> [02 §hub](02-transport-and-security.md#hub-role).
> The archive, the distillation runtime and pointer dereferencing all live **on the same
> machine** (the interim hub until the permanent hub is in place), so reaching the original
> text is free. The evidence chain is private by construction and never leaves the tailnet by
> construction, which fits the [security red line](02-transport-and-security.md#security-isolation)
> exactly — no new mechanism needed.
