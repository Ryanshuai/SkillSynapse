# 08 · Capability and permission: least privilege at skill granularity

> A foundation document. The single source of truth for **agent runtime safety**: what a skill is
> allowed to do to the local system while it runs, how that is enforced, and how it loosens as the
> skill earns trust. It and [02](02-transport-and-security.md) are the **two orthogonal axes** of
> security — 02 governs the **outbound data boundary** (raw JSONL never leaves the mesh); this page
> governs the **inbound constraint on agent actions** (what it may do to your local machine, files
> and tools). Reputation signals (probation / metrics / intensity / blast radius / staging) are
> reused from [03](03-shared-primitives.md) and [04](04-skillsynapse-loops.md) and are not restated.
>
> **Decided this round**: ① the unit of permission is the **skill, not the agent** — the manifest is
> versioned with the skill and travels through the DAG; ② scope is **inward-facing and agent-facing
> only** — the public internet belongs to the 02 red line, a human is only a kill switch, and there
> is no RBAC; ③ **no credential proxy this round** — secrets are injected at launch, with the proxy
> left as a deferred upgrade slot; ④ **four layers**, of which the fourth is a **projection report
> over the audit log**, not a system of its own; ⑤ human attention decays to ≈0, so **safety must
> never depend on somebody reading**.
>
> All 📐 in design; a working draft, not a final spec.

## 1. Core principle and threat model {#principle}

**Don't build a central table of "what the agent may do" — build a per-skill declaration of what
*that skill* may do.** The unit of permission is the skill: each one declares what it needs (which
paths, which local tools, which machines on the mesh, which secrets, which actions require human
approval), and the system grants exactly that and denies the rest. That declaration (the manifest)
is a block inside SKILL.md, so it is **versioned with the skill, travels the evolution DAG with it,
and rolls back with it** — which is precisely where this meshes with the rest of the architecture.

**The threat model, pinned down first** — otherwise every trade-off below drifts. The adversary is
**not** a malicious peer machine, **not** an insider, **not** an external attacker. It is **a skill
your own pipeline generated or mutated overnight out of session logs**, which may have been led
astray by hallucination or by a poisoned session log, and which then reaches for things it should
not touch on your local system or on other machines. Two narrowings follow:

| Narrowing | What it means |
|---|---|
| **Nothing to do with the public internet** | Egress and exfiltration are the job of the "data never leaves the mesh" red line in [02 §security](02-transport-and-security.md#security-isolation). This system is orthogonal to it: no overlap, and no second egress policy to maintain. |
| **Nothing to do with people** | No multi-user RBAC, no insider model. In this system a human has exactly **one role: the break-glass approver** — not an authorized subject, but the switch that decides and backstops. |

There is one subject: the skill the agent is running. There is one class of object: local and mesh
resources.

## 2. The four layers {#four-layers}

Coarse to fine. Each layer stands on its own and each maps onto infrastructure that already exists:

| # | Layer | Granularity | What it does | Where it lands |
|---|---|---|---|---|
| 1 | **Mesh segmentation** | node | the agent node can only reach the machines a task needs, never your day-to-day machines | Tailscale ACL (§3) |
| 2 | **Capability declaration** | action | the skill's manifest declares what it needs; the tier grants a trust ceiling by reputation; the grant is `manifest ∩ tier` | SKILL.md manifest + orchestrator (§4) |
| 3 | **Enforcement** | OS | pin down the boundary compiled in §4 at OS level: deny writes / network / secret reads by default, allowlist only | srt (§5) |
| 4 | **Audit + approval** | — | everything appends to an **off-machine** log; the very few high-risk actions ping a human; day to day it degrades into a projection report | log + Telegram + digest (§6) |

**One run, end to end**: a task arrives → routed to a skill → its manifest is read → the
orchestrator compiles `manifest ∩ tier` into an srt configuration and injects the declared
secrets → the skill runs inside the srt cage → hitting a `requires_approval` action pings you over
Telegram → everything is written to the off-machine log → overnight it is projected into a report.

## 3. Layer 1 · Mesh segmentation (Tailscale ACL) {#segmentation}

The coarsest cut: which machine may reach which. The agent node (the Mac mini, today) is reachable
in the Tailscale ACL **only to the machines a task actually needs**, and cannot touch your daily
driver or interactive machines. The justification changes from "restrict access to external APIs"
to **network segmentation**: even if a bad skill wants to move laterally, the network layer stops
it first.

Tailscale is WireGuard, so node-to-node traffic is already encrypted and identity-authenticated —
**agent-to-agent mTLS comes free**, and there is no reason to stack a second certificate system on
top. ACL configuration and the port matrix follow [02 §2](02-transport-and-security.md#security-isolation).

## 4. Layer 2 · Capability declaration: manifest × tier {#capability}

Two dimensions, and **not one collapsed `permission_level` field** — collapsing them loses the
ability to say "this skill wants X but has not yet earned it".

### 4.1 manifest: what the skill asks for (fine-grained, moves with the version)

A block in SKILL.md front-matter, versioned with the skill and carried through the DAG:

```
capability:
  fs:
    read:  [ ... ]          # paths it may read; default = only the skill's own working directory
    write: [ ... ]          # paths it may write; default = none
  tools:   [ ... ]          # local MCP servers / tools it may call
  reach:   [ ... ]          # mesh machines it may touch (maps onto a subset of the layer-1 ACL)
  secrets:                  # local secrets it needs, plus a delivery mode (§5.2)
    - { name: ..., mode: ambient | brokered-token | brokered-static }
  approve: [ ... ]          # actions requiring a human; should be as close to empty as possible (§6.2)
```

**There is no tier field in the manifest** — a skill cannot issue itself trust. An empty default
means: read the skill's own working directory, and nothing else — no tools, no reach, no secrets.

### 4.2 tier: how much trust the system grants it (coarse, moves with reputation)

The tier is derived by the orchestrator from **the probation / metrics state that already exists**;
a skill never writes it. The effective grant is `manifest ∩ the tier ceiling`. It binds directly
onto machinery already in the codebase:

| tier | Trigger (existing signals) | Ceiling |
|---|---|---|
| **quarantine** | newly created or just mutated (`probation=True` with a small `selections_since_version`) | read its own working directory only; no tools, reach or secrets |
| **probation** | `probation=True`, real usage accumulating (`< min_selections=5`) | manifest ∩ read-only; writes and secrets still held down |
| **trusted** | probation cleared (≥5 real selections, no degradation) | the full manifest, except `approve` entries |
| **privileged** | long-standing trusted plus an explicit human bump | the full manifest, plus some `approve` entries waived (remembered TOFU) |

Demotion is symmetric: `auto_rollback` firing (`degradation_threshold=0.5`), or over-reach climbing
again (§5.3), knocks it back to probation or quarantine. **New code is inherently more suspect** —
this extends default-deny from the spatial dimension (scope) into the temporal one (trust grows
over time), and it reuses the probation gate that already exists rather than building new machinery.

### 4.3 Two hard rules {#hard-rules}

1. **Default to the tightest tier**, no matter how much the manifest asks for. A skill that has
   mutated to a new version returns to quarantine automatically because `selections_since_version`
   resets — that is existing behaviour in `metrics.py`, free of charge.
2. **A tier promotion may only automatically loosen things already declared; widening the manifest
   (a new tool, a new reach, a new secret) always goes through a human.** The manifest was written
   by the LLM itself: raising the tier says "I trust this stable code more", widening the manifest
   says "this code wants to touch something new", and the second must never be granted by
   reputation alone.

### 4.4 Three axes that are easy to confuse {#three-axes}

The repo now has three graded quantities. They are **orthogonal** and must not be collapsed:

| | Axis | Governs | When it acts |
|---|---|---|---|
| **intensity** ([03](03-shared-primitives.md#intensity)) | output autonomy | how far a loop goes on its own (0 off … 4 live) | when a skill is **generated / promoted** |
| **priority_score** ([07](07-triage-and-ranking.md)) | importance | which candidate a human looks at first | when a skill is **triaged** |
| **tier** (this page) | runtime trust | how much local resource a running skill may touch | when a skill is **executed** |

A draft that the automation loop dropped into staging at intensity 3, ranked first by
`priority_score`, and then promoted to intensity 4 "live", still **starts at the quarantine tier at
runtime**. High generation autonomy ≠ high importance ≠ high runtime trust. The three chain rather
than substitute: 07 decides *whether it goes live at all*, this page decides *what it may do once
it does*.

## 5. Layer 3 · srt enforcement and secrets {#srt}

A declaration nobody enforces is waste paper. **srt**, the OS-level sandbox runtime, pins the
boundary compiled from `manifest ∩ tier`: **deny all writes, network and secret reads by default;
allowlist only**. With the outbound axis delegated to 02, what srt guards is the local filesystem,
exec, processes and mesh reachability — the only place the boundary can actually be held, which
makes **srt the primary boundary of this system**. The corollary: srt being bypassed means losing
everything, so its only backstop is the **off-machine** audit and kill switch of layer 4.

### 5.1 Secrets: injected at launch, no proxy (this round) {#secrets}

No proxy this round. Secret handling collapses into the srt + manifest layer instead of being a
component of its own: when the orchestrator launches a skill it **injects only the secrets that
skill's manifest declared** into that skill's sandbox (env or tmpfs), and srt denies reads of every
other secret file. This keeps what matters most — **per-skill least privilege, determinism
and auditability**. What it gives up is short TTLs, runtime revocation and sub-scoping, which is
consistent with a threat model that is not afraid of the box being popped.

### 5.2 Delivery modes (per secret, not a global switch) {#delivery-modes}

Each secret in the manifest carries a `mode`; pick the cheapest one that works:

| mode | Mechanism | Friction |
|---|---|---|
| **ambient** | identity rather than a key (Tailscale SSH, where node identity *is* the credential) | none. **Prefer this** |
| **brokered-token** | the backend signs a short-lived scoped token; the skill caches it for the session | near none (in the launch-injection version: injected directly, no TTL) |
| **brokered-static** | old things that only accept a static key; handed in JIT, held only in memory | real friction, and the smallest payoff |

> **Deferred: the credential proxy.** Once a secret appears that is high-value, accepts only a
> static key, and must be used repeatedly by an agent, swap the `brokered-*` backend from "inject at
> launch" to "a deterministic proxy signing short-TTL tokens". **The manifest contract does not
> change and skills are not modified** — it is a drop-in upgrade. The proxy **must be dead code, not
> an agent**: it is the crown jewel, and making it an LLM would move hallucination and prompt
> injection to the vault door while destroying the "deterministic code on an isolated machine >
> an LLM on an exposed one" trust hierarchy. It does one boolean lookup (`is this request ⊆
> manifest ∩ tier?`), escalates to a human when unsure, and never improvises.

### 5.3 srt needs statistics: is the cage worth anything? {#statistics}

Every srt enforcement decision emits a structured event (this is also the content of layer 4a's audit):
`{skill, version, tier, action, resource, decision, deny_class}`. The JSONL schema from
`instrument-logs` / `analyze-logs` can be reused to reconstruct the "deny → what happened next →
success or failure" chain. Aggregated per skill × version, it yields three quantities:

| Quantity | Meaning | Use |
|---|---|---|
| `deny_rate` | does the cage ever bite | never biting = the allowlist is too loose and enforcement is doing no security work; biting a lot = check whether it is friendly fire |
| `over_reach` | attempts to touch things outside the manifest | a safety signal; feeds tier demotion (§4.2) |
| `outcome_delta` | success-rate difference between enforcing and permissive | **the cost of the cage**: ≈0 = free containment; >0 = it is blocking something the task genuinely needs, and the denials that surfaced point at exactly which lines the manifest is missing |

**Two permissive modes, not to be confused:**

- **Record intent** (safe everywhere): still block, but record "it wanted to, and I stopped it".
  Feeds over-reach and reverse-calibrates the manifest, but **measures no outcome**.
- **Shadow allow** (measures outcome, dangerous): what should be blocked is allowed and recorded,
  so the action **really executes** — **only ever against a throwaway sandbox target**, never
  against a real environment with an untrusted skill.

**The probation window is a natural shadow-run window, which kills two birds**: a new skill spends
its first few nights running shadow-allow in a sandbox, which (a) infers the manifest from what it
*actually* touched, rather than trusting the LLM's self-report, and (b) measures whether enforcement
would cost success rate. Once that settles, switch to enforcing against the real environment.
`outcome_delta` can join the existing metrics gate as a fifth rate. ⚠️ LLM skills are
non-deterministic, so **n=1 proves nothing** — use small paired replay samples (the A/B plus
checkpoint machinery in `my-claude-coding-agent` will drive this).

### 5.4 The FP/FN view {#fp-fn}

Take positive = "should be blocked". **srt is zero-error with respect to the manifest** (it executes
the manifest exactly); FP and FN are not srt's fault, they are **the manifest's error relative to
real intent and real safety**:

| | What it is | Root cause | Fix | Observable? |
|---|---|---|---|---|
| **FP**, false block | blocked something legitimate the skill genuinely needed | manifest too tight | shadow-run and infer the loosening | ✅ measured directly (`outcome_delta`) |
| **FN**, false allow | allowed something harmful | manifest too loose / over-declared | tighten, demote the tier | ❌ **dark**; only stumbled upon in later audit |

Three conclusions: ① **tier and manifest tightness are the operating point on an ROC curve**;
loosening by reputation is sliding the threshold as evidence arrives, and the tier *is* the
threshold. ② **The losses are severely asymmetric**: an FP is cheap and reversible (rerun, loosen,
answer one prompt), an FN may be irreversible (the data is already out) — the formal justification
for default-deny is **asymmetric loss**, not fastidiousness. ③ **Observability is asymmetric too**:
FPs are visible, FNs sit in the dark, so **FNs cannot be closed by a measure-then-optimize loop**
(you have no labels for them). They can only be handled by staying conservative, estimating via
shadow runs, and watching "over-reach blocked" as a leading indicator. **Never read "the report is
all green" as "there are no FNs".**

## 6. Layer 4 · Audit + approval = a projection report {#report}

The fourth layer is not a new system, it is a **read model**: a projection over the 4a off-machine
log, the three enforcement quantities, and tier/DAG state. It **reuses the existing digest**
([02 §4.1](02-transport-and-security.md#hub-role)) — one more section in the nightly digest, not a
second pipeline. It splits in two, and **only approval should be low-frequency; audit is the exact
opposite.**

### 6.1 4a audit: high-frequency, off-machine, exhaustive

Every tool call, secret use, srt decision and egress is written to an append-only log in real time.
The critical part: **the log must land off-machine (the hub), and the agent machine may only append,
never edit or delete** — srt is the primary boundary *on that machine*, so once that machine is
breached the off-machine log is the only trustworthy evidence left. It is also what
makes blast radius computable after an incident (the capping policy is in
[03 §2](03-shared-primitives.md#git-publishing)).

### 6.2 4b approval: frequency ≈ 0 {#approval}

Every ping spends your attention, and enough pings turn into eyes-closed confirmation, at which
point the control is worthless. Make it naturally rare by having layers 1–3 do the work, not by
wishing:

1. **The vast majority of risk should be denied outright, not escalated.** Human approval is
   reserved for the small set that is permitted in principle but irreversible or large-blast-radius.
2. **TOFU**: the same skill doing the same class of action asks once; once approved it is remembered
   scoped, so frequency decays as the skill matures.
3. **The tier covers both ends**: an untrusted skill wanting something spicy is denied without
   asking; a trusted one has already been remembered or asks once. Pings come only from the narrow
   band in the middle.
4. **"Worth knowing but not worth blocking" goes into the digest**, not a live ping. Only "I cannot
   continue until you decide" is allowed to interrupt.
5. Each ping shows the exact resolved action (judgeable in two seconds), is bound to an action hash
   (against TOCTOU), and **defaults to deny on timeout** (silence falls on the safe side).

**Treat the frequency as a metric, not a knob**: a stable night should trend toward zero approvals.
A skill that keeps hitting the approval wall is signalling that its manifest over-declares `approve`
or that its design is wrong — **go fix it upstream in the DAG / reputation loop, don't tolerate it**.
Approval frequency is itself a health and safety signal, and wires back into tier changes in §4.2.

### 6.3 An inverted pyramid for the report (or the report becomes the attention cost)

A green night stops at the top line (last night: X skills, HITL pings = N against a target of 0,
anomalies = M). Itemize only: **denials that were followed by failure** (the manifest may need
loosening), the **over-reach list** (the leading indicator for FNs), tier changes, and new TOFU
approvals (so they can be objected to retroactively). Denials that succeeded anyway get a count only.

### 6.4 Attention decay: safety must not depend on a reader {#decay}

Expect human attention to go from daily at first, to weekly, to essentially never. That is the
target state — trust growing means attention *should* fall to zero — but it imposes hard constraints
on the system:

- **Pull may decay; push must hold.** The digest is pull (you may ignore it); escalations and
  interrupts are push (they reach you regardless of your rhythm). Whatever you withdraw from pull,
  safety has to make up on push. The push threshold may **rise as the system proves itself, but
  never to zero**.
- **Decay must be evidence-driven, not calendar-driven**: it is *earned* by FP/FN records going
  green. The system says "stable for N weeks, you can drop to weekly", and pulls you back when
  something happens (a new batch of skills, an FP spike, over-reach climbing).
- **If you stop reading, you have cut the human review path that catches FNs** (§5.4: FNs are only
  found in later audit). So "eventually not reading" **is only safe once FN detection is automated
  and pushed**: periodic permissive shadow re-audits, log anomaly and metric drift detection, run
  automatically, pushing the moment a leading indicator moves.
- **A liveness heartbeat**: once you stop reading, "the report is all green" and "the pipeline died"
  look identical. That needs an external watchdog — if the report stops arriving, it pokes you.
  Otherwise "not reading" plus "silent failure" means you are blind and don't know it.

**The invariant that must survive attention reaching zero**: **silence means safe, and if something
really goes wrong, something actively comes and grabs you.** Layers 1–3 are controls that hold
without a reader; layer 4 degrades into "silent normally, push on anomaly, digest available when
you want it".

## 7. Delivery order {#delivery}

Ordered by blast-radius reduction per unit of work. **Do not start with the manifest compiler** —
it is the largest engineering effort and its marginal safety gain is smaller than the first few
steps:

1. **Mesh segmentation (Tailscale ACL) + 4a off-machine audit** — the most independent, the most
   direct cut to blast radius, and it does not depend on the hub, so it can fold into the 30-day
   commitment in [02 §7](02-transport-and-security.md).
2. **The manifest × tier policy** — the tier hangs directly off the existing `probation` /
   `selections_since_version` / `auto_rollback`, and `approve` rides the Telegram bot already
   committed to in 02 §7; the orchestrator compiles `manifest ∩ tier`.
3. **srt enforcement + launch-injected secrets + the three statistics** — the probation window
   doubles as manifest calibration via shadow runs.
4. **The push channel + liveness heartbeat** — the precondition for attention decay.
5. **The credential proxy** — deferred; trigger conditions in §5.2.

**How it wires into existing code**: probation / metrics / auto_rollback / staging are **untouched**
— the tier is a runtime read of them. The manifest is one new block in SKILL.md, carried through the
existing DAG and the git publishing / rollback model ([03 §2](03-shared-primitives.md#git-publishing)).
srt logs reuse the `instrument-logs` schema. **The whole system permits exactly one
non-deterministic component: the worker skill locked inside the srt cage.**
