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
from dataclasses import dataclass
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


@dataclass
class LLMCallResult:
    output: str
    elapsed_seconds: float


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
        # can run as a nightly cron triggered from within a Claude Code session.
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)

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
            tail = stderr[-500:]
            raise LLMError(f"claude --print exit {proc.returncode}: {tail}")

        out = stdout.strip()
        if not out:
            raise LLMError("claude --print produced empty stdout")

        return out

    def try_call(self, prompt: str, **kwargs) -> Optional[str]:
        """Same as `call` but returns None on any non-budget failure."""
        try:
            return self.call(prompt, **kwargs)
        except RateLimitDeferred:
            raise
        except LLMError:
            return None
