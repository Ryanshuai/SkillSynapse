# CC-log aggregation — Syncthing over Tailscale

Intranet-only Syncthing to aggregate every machine's `~/.claude/projects/` onto
one hub, feeding the nightly knowledge report + SkillSynapse. Design:
[`../../docs/02-transport-and-security.md`](../../docs/02-transport-and-security.md)
(overview at [`../../docs/README.md`](../../docs/README.md)).

**Security model = internet isolation, not content scrubbing.** Inside the
Tailscale mesh raw logs sync freely; the hard rule is data never leaves the
mesh. Every node is hardened so Syncthing has no public code path: global
discovery / relay / local broadcast / UPnP / auto-upgrade / telemetry all off,
listener bound to the tailnet IP, GUI on localhost only. See design §2 (security)
/ §2.1 (isolation config).

Real hostnames and tailnet IPs live in `LOCAL-TOPOLOGY.md` at the repo root —
that file is gitignored and never enters the repo; docs below use placeholders.

## Flow

```
# 1. On the hub (the always-on box):
./setup-hub.sh <HUB_TAILNET_IP>
#    -> prints HUB_ID

# 2. On each source machine:
./onboard-source.sh <HUB_ID> <HUB_TAILNET_IP> <THIS_MACHINE_TAILNET_IP>
#    -> prints SELF_ID and the add-source line

# 3. Back on the hub, register that source:
./add-source.sh <SELF_ID> <hostname> <source_tailnet_ip>
```

Each source lands at `~/cc-logs/<hostname>/` on the hub. Scripts are idempotent.

## Platform matrix

| Node type | Install |
|---|---|
| Linux x86_64 | scripts handle it (rootless binary) |
| Linux arm64 (Jetson-class boards) | scripts handle it (arch auto-detected) |
| Windows | install Syncthing manually, then apply the same isolation settings via GUI (Advanced): globalAnnounce/relays/localAnnounce/NAT off, listen `tcp://<tailnet-ip>:22000`, share `%USERPROFILE%\.claude\projects` sendonly to the hub |

## Persistence

Validation runs fine detached (`nohup`), but to survive reboot the scripts
install a `systemd --user` unit (`syncthing-cc.service`). A **headless always-on
hub** additionally needs, once, the only privileged step in the whole setup:

```
# Linux hub:
sudo loginctl enable-linger $USER

# macOS hub: a LaunchAgent only runs while someone is logged in at the console,
# and an always-on mini usually has nobody logged in. Install a LaunchDaemon
# instead (system domain, no session needed, still runs as the owning user):
sudo ./macos-launchdaemon.sh $USER
```

## Reducing volume (optional)

`~/.claude/projects` includes verbose `subagents/workflows/*.jsonl`. SkillSynapse
already ignores subagent files downstream, so you may add a `.stignore` in the
shared folder to skip them at transfer time:

```
**/subagents/**
```
