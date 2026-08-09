"""LLM provider abstraction.

v0.1: `claude --print` subprocess. Simple, zero SDK dependency. Every call
counts against a per-run budget; hitting the budget raises RateLimitDeferred
so the caller can record `deferred_rate_limit` in decisions.jsonl and continue.

v0.2 swaps the subprocess for the Claude Agent SDK; the public API (`call()`)
stays the same so the rest of the pipeline doesn't care.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .config import Config


class RateLimitDeferred(Exception):
    """Raised when we've spent our per-run LLM budget, or when the CLI tells
    us we've been throttled upstream (usage/rate-limit banner)."""


class LLMError(Exception):
    """Generic LLM invocation failure (non-zero exit, empty stdout, etc)."""


# Fragments the `claude --print` CLI emits (to stderr and sometimes stdout)
# when the account has exhausted its subscription / 5-hour / API quota. Matched
# case-insensitively against a decoded string.
#
# Every entry carries an explicit "limit / quota / exhausted" token so the
# model's own output (e.g. "please try again in a moment") won't false-match.
_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate-limit",
    "usage limit",
    "usage-limit",
    "limit reached",
    "limit exceeded",
    "5-hour limit",
    "5 hour limit",
    "quota exceeded",
    "subscription limit",
)


def _looks_like_rate_limit(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(m in t for m in _RATE_LIMIT_MARKERS)


# Where a headless hub keeps its subscription token. `claude --print` accepts
# CLAUDE_CODE_OAUTH_TOKEN in place of an interactive login, which is the only
# thing that works on a box nobody logs into.
_OAUTH_TOKEN_FILES = (
    "~/.config/haclaw/secrets.env",
)


def _oauth_token_from_file() -> Optional[str]:
    """Read CLAUDE_CODE_OAUTH_TOKEN out of a KEY=value env file.

    Deliberately parses out the single key instead of sourcing the file: these
    env files are shared secret stores (Gmail app passwords, Telegram bot
    tokens), and none of the rest has any business being in the environment of
    an LLM subprocess.
    """
    for raw in _OAUTH_TOKEN_FILES:
        path = Path(raw).expanduser()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("CLAUDE_CODE_OAUTH_TOKEN"):
                continue
            _, _, value = line.partition("=")
            value = value.strip().strip("\'\"")
            if value:
                return value
    return None


class LLMProvider:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.calls_this_run = 0
        # subprocess_cmd lives under cfg.llm in config_default.yaml
        llm_cfg = cfg.llm
        cmd = llm_cfg.get("subprocess_cmd", ["claude", "--print"])
        self.cmd: list[str] = list(cmd)
        self.model: str = llm_cfg.get("model", "claude-opus-4-6")
        guard = llm_cfg.get("rate_limit_guard", {}) or {}
        self.max_calls = int(guard.get("max_calls_per_run", 200))
        self.defer_log_action = guard.get("defer_log_action", "deferred_rate_limit")

        # Every `claude --print` subprocess persists a session .jsonl under the
        # config dir's projects/ tree. With the default config dir (~/.claude),
        # those extractor runs pollute the user's real Claude Code history (the
        # VSCode extension / history picker lists them). We redirect the child
        # to an isolated CLAUDE_CONFIG_DIR sidecar — the extension never scans
        # it — while symlinking the auth-bearing files back to the real config
        # dir so subscription/OAuth login still resolves. Set to null/empty to
        # opt out and share the caller's history dir.
        iso = llm_cfg.get("isolated_config_dir", "~/.claude-skillsynapse")
        self.isolated_config_dir: Optional[str] = (
            str(Path(iso).expanduser()) if iso else None
        )
        if self.isolated_config_dir:
            self._ensure_isolated_config_dir()

    def _ensure_isolated_config_dir(self) -> None:
        """Idempotently provision the sidecar CLAUDE_CONFIG_DIR: create it and
        symlink the auth files (`.credentials.json`, `.claude.json`) from the
        real config dir so the headless child authenticates with the same
        subscription account. Best-effort — a symlink failure just means the
        child falls back to its own (empty) auth, surfacing as an LLMError the
        caller already handles, rather than crashing provider construction."""
        try:
            side = Path(self.isolated_config_dir)
            side.mkdir(parents=True, exist_ok=True)
            real = Path(
                os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
            ).expanduser()
            # `.claude.json` lives next to the config dir (in $HOME), not inside it.
            for target, link in (
                (real / ".credentials.json", side / ".credentials.json"),
                (Path.home() / ".claude.json", side / ".claude.json"),
            ):
                if target.exists() and not link.is_symlink():
                    link.symlink_to(target)
        except OSError:
            # Don't let history-isolation setup take down the whole run.
            self.isolated_config_dir = None

    def call(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        timeout_seconds: int = 120,
    ) -> str:
        """Send a prompt and return stdout. Raises RateLimitDeferred when out
        of budget, LLMError on subprocess failures."""
        if self.calls_this_run >= self.max_calls:
            raise RateLimitDeferred(
                f"hit per-run cap ({self.max_calls} calls)"
            )

        full_prompt = prompt if system is None else f"{system}\n\n---\n\n{prompt}"

        cmd = list(self.cmd)
        # Allow overriding the model per call via --model flag.
        if self.model and "--model" not in cmd:
            cmd.extend(["--model", self.model])

        # Windows: subprocess.run(argv=[...]) with shell=False does NOT consult
        # PATHEXT, so a bare "claude" cannot resolve to "claude.cmd". shutil.which
        # honors PATHEXT and returns the absolute path. No-op on Linux/macOS
        # when the command is already an absolute path or resolves directly.
        resolved = shutil.which(cmd[0])
        if resolved is not None:
            cmd = [resolved] + cmd[1:]

        # Claude Code sets CLAUDECODE=1; nested `claude --print` refuses to
        # launch with that flag. Strip it from the child env so SkillSynapse
        # can run when manually triggered from within a Claude Code session.
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)

        # A headless hub has no `.credentials.json` — nobody ever ran /login there.
        # Fall back to the fleet's stored subscription token so a manually
        # triggered run and the merge heartbeat can authenticate without an
        # interactive session. An inherited token always wins: it is what the
        # caller deliberately chose.
        if not env.get("CLAUDE_CODE_OAUTH_TOKEN"):
            tok = _oauth_token_from_file()
            if tok:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = tok

        # Keep the child's session history out of the user's real Claude Code
        # history (see _ensure_isolated_config_dir). When opted out, the child
        # inherits whatever CLAUDE_CONFIG_DIR the parent had (default ~/.claude).
        if self.isolated_config_dir:
            env["CLAUDE_CONFIG_DIR"] = self.isolated_config_dir

        import time
        t0 = time.time()
        try:
            proc = subprocess.run(
                cmd,
                input=full_prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout_seconds,
                env=env,
            )
        except FileNotFoundError as e:
            raise LLMError(f"LLM subprocess not found: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"LLM call timed out after {timeout_seconds}s") from e

        self.calls_this_run += 1

        stderr = proc.stderr or ""
        stdout = proc.stdout or ""

        # Upstream throttling takes priority over every other failure mode.
        # The CLI prints usage-limit banners on STDERR (even when the process
        # exits 0). STDOUT carries the LLM's own output — which may legitimately
        # contain phrases like "rate limit" when the model is describing
        # throttling behavior, so matching stdout against marker strings
        # produced false-positive deferrals. Only inspect stderr here.
        if _looks_like_rate_limit(stderr):
            raise RateLimitDeferred(
                f"CLI reported upstream throttling: {stderr.strip()[:200]}"
            )

        if proc.returncode != 0:
            # The CLI does not consistently put failures on stderr: an
            # unauthenticated run prints "Not logged in \u00b7 Please run /login" to
            # STDOUT and exits 1, leaving stderr empty. Reporting only stderr turns
            # that into `claude --print exit 1: ` — an error with a colon and
            # nothing after it, which is how a missing login on the hub survived
            # two full heartbeats looking like an unexplained subprocess failure.
            tail = (stderr[-500:] or stdout[-500:]).strip() or "(no output on either stream)"
            raise LLMError(f"claude --print exit {proc.returncode}: {tail}")

        out = stdout.strip()
        if not out:
            raise LLMError("claude --print produced empty stdout")

        return out
