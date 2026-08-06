# 05 · The marking signal: a stamp applied live

> Reader document, feeding [04, the three loops](04-skillsynapse-loops.md). All three loops are
> **passive inference** (a nightly extractor reads a compressed brief, a day later, and guesses
> NEW/PITFALL/toil); this page adds a path where **a human or the working agent emits a signal
> live, in the session**: the mark.
>
> **Decided this round**: ① the channel is primarily the transcript (`/skillsynapse mark`, a
> front door that avoids CC's built-in `/skill`), not a local sidecar; ② a mark is both an
> **entry ticket into extraction** and a **fast lane** (with a probation discount); ③ both
> humans and agents can mark, but **weighted by provenance** (human ≈ 1, agent < 1);
> ④ deployment must ship the **discovery entry point** at the same time, or agents on other
> machines will never know this feature exists.

## 1. Position: not a fourth loop — a ground-truth label feeding the three

Every signal the three loops use is **inferred** (an LLM reads a transcript and guesses). A mark
is **the person who was there stamping it live** — no guessing. Its value is not "another
pipeline" but injecting ground-truth labels into the pipeline that already exists.

| Signal | Produced by | When | Context | Quality |
|---|---|---|---|---|
| inductive / directed / toil | the nightly extractor | next day | **a compressed brief** | inference; can miss, can be wrong |
| **mark** | **the human / working agent, in the session** | **live** | **full context** | **ground truth / judged with full information** |

**Why the working agent should have this ability (the core motivation)**: the nightly extractor
reads a summary with `tool_result` stripped, a day later. The agent that was doing the work has
the complete context — it knows best that "this trap is worth recording", "this procedure is
reusable", "that stretch was tedious and mechanical and should be automated". Letting it stamp
live beats another LLM guessing at night by a wide margin.

## 2. Three polarities (mapped onto the three loops' existing exits, no new pipelines)

| Polarity | Feeds | Lands in | Meaning |
|---|---|---|---|
| **`LEARN`** | inductive loop | the extractor's NEW path | "this is a good reusable procedure, extract a skill" |
| **`PITFALL`** | (pitfall) | the `OrphanPitfall` path | "record this trap so nobody hits it again" |
| **`TOIL`** | toil loop | `toil_miner` candidates | "this work is tedious and mechanical, wrap it in a command/script" |

Marking just gives those three existing exits a **high-confidence entrance**; it does not change
the downstream verification loop.

## 3. Capture channel: the transcript, not a local sidecar

Marks are written **into the session transcript** rather than a local DB. Three hard reasons:

1. **Zero cross-machine cost**: marks ride `~/.claude/projects/*.jsonl` to the hub over Syncthing
   for free ([sync matrix](02-transport-and-security.md#sync-matrix)); a local sidecar DB never
   enters the sync chain, so marking into it is wasted.
2. **Episode location**: a mark carries its event index within the transcript, so it inherently
   knows **which stretch** of the session is worth learning from, which locates it precisely for
   `episode_detector`.
3. **One channel for humans and agents**: a human types `/skillsynapse mark`, an agent emits the
   same sentinel, and one scanner regex catches both — they differ only in provenance.

A `skill mark <session-id>` CLI for marking after the fact is a **supplementary channel** (for
when you forgot to mark live), but the transcript is the main one.

### 3.1 Sentinel format: one regex catches both sources

Humans and agents produce the **same token**, so the scanner maintains one regex:

```
⟦synapse:mark kind=learn⟧ the symlink must exist before activate, or CC won't find it
⟦synapse:mark kind=pitfall⟧ MagicDNS is unreliable under a DNS warning; hard-code the tailnet IP
⟦synapse:mark kind=toil⟧ every Lambda launch means hand-copying the IP into ssh config — automate it
```

- **Human**: the `.md` body of `/skillsynapse mark <kind> <note>` (see §6.1) expands into the line
  above (a slash command is fundamentally a prompt template, so the expanded text lands in the
  transcript, along with the `<command-name>` marker that `slash_command_parser` recognizes).
  **Naming**: the front door is `/skillsynapse`, not `/skill` (which collides with CC's built-in
  skill mechanism).
- **Agent**: the working agent emits that line directly in its reply.
- **Provenance is determined automatically**: a sentinel landing in a `user` event = human; in an
  `assistant` event = agent. The scanner already knows `event.type`, so there is no extra
  instrumentation.

## 4. Provenance weighting: human ≈ 1, agent < 1

A mark is a **weighted signal**, and the weight (set by provenance) modulates two things:
**(A) how hard it overrides a PRE-SKIP**, and **(B) the fast lane's probation discount**.

```yaml
# new in config_default.yaml
marking:
  weight_human: 1.0     # a human stamp ≈ ground truth
  weight_agent: 0.4     # a working agent's stamp: a full-context judgement, but still a judgement, not a fact
  override_skip_threshold: 0.8   # weight ≥ this → hard override of PRE-SKIP (only humans qualify)
  probation_floor_uses: 1        # however high the weight, at least 1 real successful use before graduating (invariant)
```

**(A) Overriding PRE-SKIP** — today the extractor force-SKIPs on AD_HOC_DEBUG / SESSION_FAILED /
MULTI_TOPIC_DRIFT / `min_tool_calls`:

- `weight ≥ threshold` (**human**): **hard override** — "the person who was there says learn from
  it, so mine it even if it looks like failure or flailing" (the extreme version of
  [the directed loop's hook 1](04-skillsynapse-loops.md#four-hooks)).
- `weight < threshold` (**agent**): **strong bias, not a hard override** — waive `min_tool_calls`
  and lean strongly toward NEW, but still respect a hard failure like `SESSION_FAILED` (unless the
  polarity is `PITFALL` — failed sessions are exactly where pitfalls are richest).

**(B) The fast lane** — for a skill extracted because of a mark, discount its `probation` by weight:

```
probation_selections_needed = base * (1 - weight * fast_track_factor)
                              # but the result is ≥ probation_floor_uses (a red line, never 0)
```

- A human stamp (1.0): the steepest discount, the fastest graduation — but **still requires ≥ 1
  real successful use** before leaving probation.
- An agent stamp (0.4): a shallow discount, slightly faster.
- **Invariant**: marking buys **faster verification, not exemption from it**.
  `probation_floor_uses ≥ 1` is nailed down — "nobody used it in reality, so it gets pruned"
  ([where the three loops converge](04-skillsynapse-loops.md#three-loops)) applies to marks too.
  A human can make a skill **graduate fast**, but cannot graduate a skill that has **never
  actually been used**.

## 5. Pipeline touch points (landing on existing code wherever possible)

| Touch point | Change |
|---|---|
| `models.py` | new `Mark` dataclass (`kind ∈ {learn,pitfall,toil}` / `note` / `provenance ∈ {human,agent}` / `event_idx` / `timestamp`); `SessionMeta.marks: list[Mark]` |
| `scanner.py` | catch marks with the sentinel regex during `parse_jsonl`, set provenance from the event role, attach to `SessionMeta.marks` |
| `episode_detector.py` | an episode containing a mark is **exempt from `min_tool_calls`**; the mark's `event_idx` maps to the episode it belongs to |
| `extractor.py` | check marks before extracting: apply §4's weighting for override/bias; `build_extraction_prompt` injects `⚑ this session was marked by {human/agent} as {kind}: '{note}'` |
| probation gate | a mark-driven SkillRecord sets `probation_selections_needed` per §4(B); nothing else in metrics changes |
| decisions.jsonl | record `mark_driven_extraction` (provenance / kind / note) for audit |
| `commands.py` | add `skill mark <session-id> --kind learn --note "..."` for marking after the fact |

**`SkillRecord.origin`**: a skill extracted because of a mark gets `origin="marked"` (alongside
captured/manual), so downstream can tell at a glance and can separately measure "the graduation
rate of the marking signal".

## 6. Deployment and discovery: so agents on other machines aren't clueless (the crux) {#deploy-and-discovery}

**Whether this feature actually works at all comes down to whether deployment shipped the
"discovery entry point" to every machine.** A slash command file existing ≠ an agent knowing to
use it; for an agent to mark **proactively**, it must be told in its context. Skip this step and
any freshly started agent simply does not know `/skillsynapse` exists.

Today: `~/.claude/commands/` **does not exist** and the global `~/.claude/CLAUDE.md` is a few
lines — the discovery entry point is **empty**. Three pieces must be shipped to every machine
(an idempotent script, parallel to `deploy/syncthing/`).

> **Why deploy rather than sync**: `~/.claude/commands/` and the global `CLAUDE.md` **do not enter
> Syncthing** (which only syncs `~/.claude/projects/` one-way — see the
> [sync matrix](02-transport-and-security.md#sync-matrix)). So the discovery entry point is shipped
> per machine by a deploy script. Onboarding a new machine = running
> `deploy/skillsynapse-cmd/install.sh` once.

### 6.1 The `/skillsynapse` command family (the front door for humans)

One `/skillsynapse` front door covers every interaction — `mark` is only one of them. **Single
dispatcher pattern**: a single `~/.claude/commands/skillsynapse.md` takes the whole string
(`mark …` / `remember …`) via `$ARGUMENTS`, and the body dispatches on the first word. Benefits:
one command surfaces every subcommand (autocomplete + a menu on empty args) and permissions only
need to allow one command.

| Subcommand | What it does | Lands in | How it differs from mark |
|---|---|---|---|
| `mark <kind> <note>` | stamp this session/stretch (learn/pitfall/toil) for the nightly extractor to mine | the body expands into the §3.1 sentinel, in the transcript | asynchronous — **points the direction, lets the pipeline extract** |
| `new <note>` | **create a skill draft on the spot**: dictate it as one skill, land it in staging | writes a [level-3 draft](03-shared-primitives.md#intensity) (not auto-live) | synchronous — **the human writes the skill out now** |
| `remember <note>` | record a **lightweight memory/fact** (lighter than a skill, not a procedure), as a seed/note for synapse | lands in the notes table / a seed file | records a fact, not a procedure and not a trap |
| `list` · `status` | inspect existing skills / progress on this direction (fed by `indexer`'s scoreboard) | read-only, answered back into the session | a query, produces no signal |
| (no args) | print the menu above | plain text | — |

`mark` / `new` / `remember` all drop their sentinel or draft into the transcript/staging, and the
scanner collects them uniformly at night; provenance determination (§3.1) is identical for all
three. An optional shorthand: also ship `~/.claude/commands/syn.md` as an alias forwarding to the
same body (only the full name is installed by default).

### 6.2 The global CLAUDE.md advertisement (the front door for agents — the key part)

Inject a block of **capability advertisement + trigger conditions** into every machine's
`~/.claude/CLAUDE.md`. This is the actual cure for cluelessness:

```markdown
## SkillSynapse marking: stamp it live when you finish something worth keeping

While working you have the full context, so you know better than the nightly extractor what is
worth learning. In the three situations below, emit one sentinel line in your reply (the nightly
SkillSynapse pipeline will collect it):

- you got a **reusable procedure** working → ⟦synapse:mark kind=learn⟧ <one line: what the procedure is>
- you hit a **trap worth recording** → ⟦synapse:mark kind=pitfall⟧ <one line: what it is, how to avoid it>
- you ground through **tedious mechanical toil that should be automated** → ⟦synapse:mark kind=toil⟧ <one line: what work>

Be sparing: only mark what is genuinely worth reusing across sessions; skip the trivial and the
one-off. The human equivalent is /skillsynapse mark.
```

- This block is the **only trigger** for an agent to mark on its own — it must be in CLAUDE.md to
  be in the agent's context.
- Idempotent: the script wraps it in marker comments (`<!-- synapse:begin -->…<!-- synapse:end -->`),
  so redeploying replaces only what is inside the block, never appending twice and never touching
  the user's other content.

### 6.3 The deploy script

Add `deploy/skillsynapse-cmd/install.sh` (run once per machine): it writes
`~/.claude/commands/skillsynapse.md` (§6.1) and idempotently injects the CLAUDE.md block (§6.2);
`uninstall.sh` removes both symmetrically. Like syncthing, idempotent and re-runnable per machine.

## 7. Delivery order

1. **sentinel + scanner + models** — first get marks out of the transcript and into
   `SessionMeta.marks`, so `skill scan` can print "N marks found this run". Independently verifiable.
2. **extractor consumes marks** — §4's weighted override/bias + prompt injection +
   `origin="marked"`; audit into decisions.jsonl.
3. **The probation fast lane** — §4(B)'s discount + the `probation_floor_uses` red line.
4. **The three deployment pieces** (§6) — **without this step, the first three do not exist as far
   as agents on other machines are concerned.**
5. The after-the-fact marking CLI (§5) — icing.

Steps 1 and 4 are the two endpoints of "can be produced" and "can be discovered"; get those working
first, and fill in 2 and 3 for strength afterwards.
