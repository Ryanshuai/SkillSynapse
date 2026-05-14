"""Regex extractor for `<command-name>/xxx</command-name>` markers.

Phase 0 Finding 1: slash commands like `/infer-init` don't go through the
`Skill` tool — they only appear as this marker inside a user message. This
module is the sole owner of that regex so the metrics path and any future
consumers share one source of truth.
"""
from __future__ import annotations

import re

CMD_RE = re.compile(r"<command-name>/([\w-]+)</command-name>")


def find_slash_commands(text: str) -> list[str]:
    """Return the list of command names referenced in `text`, in order."""
    if not text:
        return []
    return CMD_RE.findall(text)
