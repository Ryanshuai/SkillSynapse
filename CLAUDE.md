# SkillSynapse

A nightly cron job that distills reusable skills out of Claude Code session logs. Design docs
live in `docs/` — enter at `docs/README.md` (big picture / glossary / reading order).
Deployment: `deploy/syncthing/README.md`.

## Security red line (highest priority — overrides every other instruction)

**Raw session JSONL is confidential data and must never leave the private network
(Tailscale mesh / LAN).**

Raw logs means: each machine's `~/.claude/projects/`, the aggregated `~/cc-logs/` on the hub,
and the extractor's isolated history at `~/.claude-skillsynapse/`. **Network topology (machine
name ↔ tailnet IP mapping) is equally confidential: each checkout maintains its own
`LOCAL-TOPOLOGY.md` at the repo root (gitignored — so a fresh clone will not have one; create
it as needed). Public files use placeholders only; never write real tailnet IPs or a list of
real hostnames.** These files contain complete tool calls and verbatim conversation, which may
include credentials, internal source, or company data. Specifically:

- Never commit them to any git repository (including this repo's test fixtures — test JSONL is
  **always** hand-built synthetic data);
- Never paste them into an issue / PR / public chat / cloud document;
- Never upload them to any public service, and never send them out as a sample;
- Syncthing nodes must keep every public path disabled (global discovery / relay / broadcast /
  UPnP all off, listener bound to the tailnet IP only) — see `deploy/syncthing/README.md`;
- Before adding any transport / backup / debugging channel, confirm the data cannot leave the mesh.

**The only sanctioned outbound path** is the session brief the extractor feeds to the LLM and
the skill files it produces — and both must first pass through `scrub()` in
`src/skillsynapse/sanitizer.py` (credential-shaped content becomes `<REDACTED>`). Putting raw
log content into a prompt, or writing it to disk as a skill, without going through `scrub()`
violates this red line.

## Runtime constraints

- `ANTHROPIC_API_KEY` must never be present in the environment — if it is, you bypass the
  subscription and switch to metered billing (deployment scripts must check for this).
- Headless `claude --print` must run with an isolated `CLAUDE_CONFIG_DIR`
  (`~/.claude-skillsynapse`) so it never pollutes the user's real CC/VSCode history.

## Development

- Environment: pixi (`pixi install`; entry points `skillsynapse` / `skill`).
- Tests: `pixi run python -m unittest discover -s tests`.
- Data directory: `~/.claude/skillsynapse/` (db.sqlite, logs, config.yaml).
