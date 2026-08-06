# 02 · Transport, security, and the hub

> Foundation document. The **single source of truth** for the data plane: how data moves
> between machines, where the security boundary is, **the sync channel matrix** (what travels
> over what), what the hub is, and how to deploy it. Every other document cites this page
> rather than restating it.
>
> **Decided for v0.2**: the security boundary moved from "sanitize the content" to "isolate the
> network" — inside the tailnet, machines sync raw JSONL freely with no sanitization (small
> team, efficiency first). The one red line is that **aggregation traffic never leaves the
> Tailscale mesh**.

## 1. Aggregation: push, not pull

**Choice: one-way Syncthing (send-only → receive-only), aggregating to a hub.**

```
each machine  ~/.claude/projects/   (send-only folder)
                 │
                 ▼  Syncthing over Tailscale
hub           ~/cc-logs/<hostname>/   (receive-only)  →  archived nightly into cc-archive/ (see 01)
```

Why push rather than pull (the hub SSHing out to rsync from each machine at night):

- **Unattended fault tolerance**: a closed laptop lid or a powered-off machine makes a pull come
  up empty. With push, a machine catches up automatically when it comes back online, and the
  nightly job reads "the newest data already cached locally".
- It reuses the existing Tailscale mesh. The alternative, `rsync over Tailscale SSH`, also works
  (about ten lines), but you have to accept "machine offline means today's data is missing".

### 1.1 Transport layer: Tailscale

The mesh is up (verified 2026-07-03). **The concrete node list (machine name ↔ tailnet IP ↔
role) is confidential topology and lives only in a local, gitignored `LOCAL-TOPOLOGY.md`.**
Structurally it is: several source machines + one always-on Linux box acting as the **interim
hub** + one eventual **permanent hub** (not yet in place).

> Until the permanent hub is in place, run the nightly loop on the always-on interim hub; once
> it arrives, move the receive-only directories over.
> Note that `tailscale status` has reported a DNS health warning (configured DNS unreachable),
> which can affect MagicDNS — so Syncthing device addresses always **hard-code the tailnet IP**
> and never rely on MagicDNS.

## 2. Security: network isolation, not content sanitization {#security-isolation}

**Red line: data never leaves the Tailscale mesh.** Inside the mesh is trusted private network
(plaintext travels freely); the mesh boundary is the boundary between inside and outside. This
drops content-level controls such as "sanitize on the company machine, raw JSONL never leaves
that host" in favour of network-layer isolation, which saves writing a sanitizing script on
every machine. Isolation is three independent layers stacked:

1. **Tailscale transport**: all aggregation traffic runs over the encrypted WireGuard mesh and
   is not exposed to the public internet.
2. **Syncthing's public paths disabled** (see §2.1): global discovery / relays / local broadcast
   / UPnP all off, device addresses hard-coded to tailnet IPs. Syncthing therefore has **no code
   path that reaches the public internet at all**.
3. **Tailscale ACL**: the admin panel restricts the Syncthing port (22000) to "source machine ↔
   hub" only; unrelated devices cannot connect.

**Hub-side narrowing**: `~/cc-logs/` is mode 700, readable only by the hub's own user. **The
real inside→outside exit is the hub's outbound steps, not anything inside the mesh**: syncing
the knowledge digest to a cloud notebook, Step C calling a cloud service — those are the points
that need their own gate (is this content allowed out, has it passed `scrub()`). Sync inside the
mesh is unguarded.

> **The complete red line list** is in the repo-root `CLAUDE.md` (highest priority). In short:
> raw logs (each machine's `~/.claude/projects/`, the hub's `~/cc-logs/`, the extractor's
> isolated history `~/.claude-skillsynapse/`) are never committed, never sent out, never
> uploaded to a public service; the only outbound path is a session brief or skill file that has
> passed `sanitizer.scrub()`; the topology list lives only in `LOCAL-TOPOLOGY.md`.

### 2.1 Syncthing isolation config (hard constraint)

Each machine configures `~/.claude/projects/` as a send-only folder; the hub receives read-only
into `~/cc-logs/<hostname>/`. **To guarantee data never leaves the mesh, every public path must
be turned off:**

```
Syncthing config on each machine (config.xml / GUI Advanced):
  · folderType            = sendonly / receiveonly     # sources only push, hub only receives
  · globalAnnounceEnabled = false      # no global discovery, nothing reported to public servers
  · relaysEnabled         = false      # no relaying through Syncthing's public relays
  · localAnnounceEnabled  = false      # no local broadcast (a tailnet is not an L2 domain)
  · natEnabled            = false      # no UPnP/NAT-PMP, no hole punching
  · every peer's device address hard-coded: tcp://100.x.x.x:22000   # tailnet IP only
  · GUI bound to 127.0.0.1:8384 with a strong password   # admin plane not even on the tailnet
```

The data flow is pinned to `100.x.x.x:22000` (tailnet, WireGuard-encrypted). **That is how
network isolation lands at the sync layer.**

> **Verification (2026-07-03)**: source machine (sendonly) ↔ interim hub (receiveonly) confirmed
> working. Logs show `Established secure connection … connection.lan=false connection.crypto=TLS1.3`,
> with the connection established between the two machines' tailnet IPs;
> `~/.claude/projects` (4.9 GB / 4200+ files) synced in full. Installed root-free from the v2.1.1
> static binary into `~/.local/bin`. Configuration scripts are in `deploy/syncthing/`.

## 3. Sync channel matrix (single owner) {#sync-matrix}

Several kinds of data move through this system, and **each takes its own channel — don't mix
them**. Any document that mentions "how X syncs" defers to this table:

| Data | Channel | Direction | History? | Why |
|---|---|---|---|---|
| Raw JSONL corpus | **Syncthing** | source → hub, one-way | no | large (GB), append-only, no merging needed |
| Evolved skills / commands | **git** (push/pull) | bidirectional | yes | small, needs rollback and **cross-machine merges** (several sources edit the same skills; git merging beats one-way overwrite), see [03 §git publishing](03-shared-primitives.md#git-publishing) |
| Ledger / Notes **index layer** (a few K tokens) | a reverse Syncthing folder, or a synapse repo push | hub → each machine | no | small; each machine queries it locally when starting work (see [06 §3](06-worklog-and-notes.md)) |
| `~/.claude/commands/` + global `CLAUDE.md` (the discovery entry point) | **deploy script, per machine** | not synced | — | idempotent install per machine, never enters any sync chain, see [05 §6](05-marking-signal.md#deploy-and-discovery) |

**Corollary**: marks ride along with `~/.claude/projects/*.jsonl` and reach the hub over
Syncthing for free (which is why [05](05-marking-signal.md) goes through the transcript rather
than a local sidecar DB — a sidecar never enters the sync chain, so marking into it is wasted);
whereas the discovery entry point (commands / CLAUDE.md) is **not synced** and must be installed
per machine by a deploy script. The overall direction is **raw material up, distillate down**.

## 4. The hub = knowledge base manager {#hub-role}

The hub's identity is promoted from "a staging area the nightly job reads" to a **knowledge base
runtime**. Four responsibilities:

| Responsibility | What it covers | See |
|---|---|---|
| **Archive** | landing zone → archive moves, append-only, backups, dereferenceable pointers | [01 §4](01-corpus-and-archive.md#archive) |
| **Distill** | run the extraction pass nightly + ledger roll-up + digest (headless `claude -p`, on subscription quota) | [06 §2](06-worklog-and-notes.md) |
| **Index** | maintain the ledger/Notes index; it is the entry point for drill-down and the dereferencing service | [06 §3](06-worklog-and-notes.md) |
| **Distribute** | distillate flows down: skills go through the synapse repo → the `~/.claude` publishing view; the index layer syncs back to each machine | §3 matrix |

### 4.1 Resident loops: the hub is a service, not a single nightly batch

"Continuously pulling and managing" does not mean every step runs at the same rate. Receiving
sync is solved by Syncthing's push (the hub receives passively, nothing to pull); what needs to
be resident is a handful of processing loops, each with its own natural cadence:

| Loop | Frequency | Cost | Notes |
|---|---|---|---|
| Receive sync | real time | zero | sources push, hub receives passively |
| Archive move | hourly | pure file operations | landing zone additions → `cc-archive/`; the more often, the smaller the "deleted before archived" window |
| Extraction pass | rolling, one batch every 2–4h | LLM (subscription) | only handles sessions **quiet for ≥ 30 min** (a live JSONL is still being appended to, so its episodes aren't closed) |
| Ledger roll-up + index rebuild | triggered after extraction | LLM (light) | event-driven, no independent cadence |
| Index layer distribution | after the index changes | zero | reverse Syncthing / repo push |
| Digest synthesis | once nightly | LLM | the only genuinely "nightly" step |
| Backup + health self-check | daily | low | each source's last-seen / backlog / failure count folds into the digest — a broken pipe is visible the next morning |

**Incremental discipline is the safety precondition for running continuously**: every loop is
idempotent and carries a watermark (a state table records the processed line offset per session
file). Re-running has no side effects, and a missed run catches up on its own. A powered-off
machine or a crashed loop only causes delay, never data loss (data safety is the archive layer's
job). Implementing this needs no bespoke daemon: reuse the `systemd --user` pattern already in
`deploy` (same as syncthing-cc.service), with one timer per loop calling an idempotent entry point.

## 5. Quota and billing (subscription plan)

- The CLI logs in over OAuth with a claude.ai account, sharing one subscription quota pool with
  the web and IDE clients — **no extra charge**.
- **Two traps that must be avoided**:
  1. `ANTHROPIC_API_KEY` **must never** be in the cron/systemd environment — if it is, you switch
     to metered API billing and bypass the subscription. Deployment scripts must check for it
     explicitly (red line, see the runtime constraints in `CLAUDE.md`).
  2. On hitting the limit the CLI offers to "continue with API credits"; do not enable
     auto-reload on the Console side. The worst case is then a failed job waiting for the window
     to reset, not a larger bill.
- v0.1's `llm_provider` already has a rate-limit guard built in (defers on hitting the limit).
  Quota is plentiful (Max $200) — run freely, don't add conservative caps; the one red line is
  that the environment has no `ANTHROPIC_API_KEY`.
- Headless `claude --print` must use an isolated `CLAUDE_CONFIG_DIR` (`~/.claude-skillsynapse`)
  so it never pollutes the user's real CC/VSCode history (implemented, see `llm_provider.py`).
- For reference: official Claude Code Routines (cloud-scheduled, 15/day on Max) suit **tasks
  with no local dependency**; anything touching local files, devices, or the mesh belongs to
  local scheduling.

## 6. Deployment

> **Already delivered as scripts**: `deploy/syncthing/` (`setup-hub.sh` / `onboard-source.sh` /
> `add-source.sh` + `stconfig.py`). The source ↔ interim hub link is verified working (§2.1).
> Deployment of the discovery entry point is in [05 §6](05-marking-signal.md#deploy-and-discovery).

**A. Doable now (no dependency on the permanent hub)**

- [x] Stand up the interim hub on an always-on Linux box: install Syncthing, create `~/cc-logs/` mode 700 — done and verified
- [ ] Install Syncthing on each source machine, configure `~/.claude/projects/` send-only → hub receive-only (one subdirectory per `<hostname>`)
- [ ] **Network isolation hard measures** (every machine, §2.1): disable globalAnnounce/relays/localAnnounce/nat; hard-code addresses; bind the GUI to 127.0.0.1
- [ ] Tailscale ACL: port 22000 reachable only between "source machine ↔ hub"
- [x] scanner multi-root rework — implemented (see [01 §2](01-corpus-and-archive.md#processing-base))
- [ ] Preprocessing script (raw JSONL → summary for the digest; the extraction pass still consumes episodes)
- [ ] Fix the archiving bug (see [01 §4.1](01-corpus-and-archive.md#archive-bug)) — evidence is being lost every day; prioritize
- [ ] Pre-deployment check: no `ANTHROPIC_API_KEY` in the environment; Console auto-reload off

**B. Once the permanent hub is in place**

- [ ] Join the permanent hub to the tailnet; `~/cc-logs/` or `/data/cc-logs/` mode 700
- [ ] Repoint each source's send-only peer from the interim hub to the permanent hub (or have both receive during the transition)
- [ ] Timers: launchd on macOS (not cron — sleep and permissions are unreliable there), `systemd --user` timers on Linux
- [ ] Retire the interim hub, or demote it to a warm standby

## 7. Acceptance criteria (a 30-day commitment)

Three things must be working within 30 days of the permanent hub being in place; otherwise the
bottleneck was never the hardware:

1. A resident Telegram bot (the interactive entry point)
2. The nightly multi-machine knowledge summary job in production
3. The SkillSynapse v0.1 nightly loop running end to end
