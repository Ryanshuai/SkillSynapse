# 03 · Shared primitives: intensity · publishing · aggregation

> Foundation document. Three mechanisms reused by several readers (04/05/06), defined only
> here. Any document that mentions "intensity", "staging/git rollback" or "cross-session
> aggregation" defers to this page.

## 1. Intensity: discrete steps of direction × strength {#intensity}

A loop is a **direction**; its strength is **how far it acts on its own**. Strength is **not a
linear slider — it is a few discrete steps**, because each step is a **change in kind** (it
changes the *kind* of thing the loop does, not "more of the same"). Think of a camera's
auto / aperture-priority / manual modes, not a brightness bar.

| Level | Name | What this level does | Red line |
|---|---|---|---|
| **0** | off | doesn't run | — |
| **1** | observe | only surfaces the signal on a dashboard ("I see you keep doing X"), **produces nothing** | produces no candidates |
| **2** | suggest | produces concrete candidates (a suggested skill/goal/automation), **writes no real files** | nothing hits disk |
| **3** | draft | output is written to staging `_pending/`, inert; only takes effect when a human promotes it | never on the live path |
| **4** | live | written and enabled directly; a human can veto or roll back **afterwards** | modifies real files |
| ~~5~~ | ~~autonomous~~ | ~~proactively rehearses headless and enables without waiting~~ | reserved, disabled by default |

**Every loop and every goal sets its own level, raised as trust grows.** Intensity is
**runtime configuration, not a code fork** — one codebase branching on `intensity`.
Implementation: each loop carries `intensity: 0..4` in config, and every candidate passes
through one `gate(intensity)` before hitting disk, which decides "record only / emit a
suggestion / write to staging / write to the live path".

**Where each reader sits today** (rationale in their own documents):

| Reader / output | Current level |
|---|---|
| Three loops · inductive | 4, live (`realize_candidate` already writes directly; the most mature) |
| Three loops · toil | 3, draft (generates a draft for approval) |
| Three loops · directed | 2, suggest (biases only, never synthesizes) |
| Marking · `/skillsynapse new` | 3, draft |
| WorkLog / ledger | 4 (incremental private records; changes no live environment, small blast radius) |
| Notes | starts at 3 (a human skims before it enters the index), moves to 4 once stable |

## 2. Publishing model: staging + git + symlink {#git-publishing}

**Decided: skills/commands produced by synapse carry full git history.** The moment any loop
reaches level 4 (auto-live), it must be cleanly rollback-able; for file-shaped output, git is
the cleanest rollback (atomic undo, familiar tooling for diffs, cross-machine merges). Level 3
and below don't require git — deleting a draft is enough — so **git is the entry ticket to
level 4**.

### 2.1 What needs history

| Output | Rollback mechanism |
|---|---|
| Skill content in the DB | `models.py` already reserves a version DAG (`version / parent_skill_ids / content_snapshot / content_diff`) plus decisions.jsonl; app-level rollback suffices |
| **File-shaped output** (SKILL.md / slash-command / scripts) | **git** — the DB is not the source of truth, the file is |

### 2.2 Layout: synapse's own repo is the source, `~/.claude` is just a publishing view

**Do not git the whole of `~/.claude`** (4.9 GB of JSONL, a shared directory, many writers).
Instead, git a **single-writer** repo that belongs to synapse:

```
~/.claude/skills/                 ← manual root (hand-written skills; bootstrap scans here, never git)
    my-hand-skill/SKILL.md
    synapse/  ───── symlink ──────┐  ← publishing view (this link is only created at level 4)
                                  │
~/synapse/skills-repo/  (.git)◄───┘  ← synapse's own root = the source = a git repo (single writer)
    _pending/<cand>/              ← level-3 drafts: committed (kept in history) but not symlinked, so CC can't see them
    active/<skill>/SKILL.md       ← level-4 live: symlinked into ~/.claude/skills/synapse
```

- **The source** is synapse's own repo; **going live** means a symlink hangs `active/` into CC's
  scan path. Rollback is `git revert` inside the repo — what the symlink points at changes
  immediately, with no re-copying.
- One nightly run = one commit; every promote/go-live = one commit with a clear message.

### 2.3 Intensity ↔ git actions

- **Level 3, draft** = write to `_pending/` and commit (drafts belong in history too — that's
  the audit trail of "what was proposed that night, and what you rejected"), **no symlink**.
- **Level 4, live** = move to `active/` and create/update the symlink. **The act of publishing
  is itself level 4.**

### 2.4 Two constraints

1. **Cap each artifact kind's maximum level by blast radius**: `settings.json` hooks are the most
   dangerous (they execute automatically), the file is shared, and it does not git cleanly →
   **capped at level 3 (drafts only)** forever, no matter how much trust accumulates. Skills and
   commands may reach level 4 with git.
2. **Bootstrap must not eat its own output**: `bootstrap.py` currently scans
   `~/.claude/skills/**/SKILL.md` and imports them as manual, so once the symlink is in place it
   would re-import synapse's own skills as manual. Manual discovery must therefore **exclude the
   `synapse/` subtree** — `resolve_paths` splits `skills_root` into `manual_skills_root` (the
   scan-only root) and `synapse_skills_root` (default `~/synapse/skills-repo`, git-managed).

> Consistent with the [sync matrix](02-transport-and-security.md#sync-matrix): skills/commands
> travel over **git** (history + cross-machine merges), raw JSONL travels over Syncthing (large,
> no history wanted). Two kinds of data, two kinds of sync.

## 3. Cross-session aggregation base (aggregator) {#aggregator}

Several readers need to "mine something out of cross-session patterns", and underneath it is the
same mechanism, factored out as a shared `aggregator`:

```
cluster cross-session patterns → rank → human confirms → enters the verification loop
```

Each reader only defines "which pattern to cluster, what to produce"; nobody re-implements
aggregation:

| Reuser | Pattern clustered | Produces |
|---|---|---|
| Three loops · toil ([04 §4](04-skillsynapse-loops.md)) | recurring mechanical actions (command / tool-sequence n-grams) | automation suggestions (command/script/hook drafts) |
| Three loops · directed ([04 §3](04-skillsynapse-loops.md)) | recurring coverage_gaps | capability goal suggestions |
| Notes intake ([06 §2](06-worklog-and-notes.md)) | recurring lookups/facts (the N3 "earned" repetition gate) | Notes entries |

> On delivery order, it is worth letting **the toil loop go first** and extracting `aggregator`
> out of `toil_miner`, then reusing it for the directed loop and Notes.
