"""Credential scrubber — strips passwords/tokens/keys from text before it
reaches an LLM prompt or a persisted skill body.

Design bias: over-redaction is acceptable, leakage is not. Skill content is
supposed to carry placeholders, never live credentials, so a false positive
costs a `<REDACTED>` where a placeholder belonged anyway. Values that already
look like placeholders (`$VAR`, `${VAR}`, `<host>`, `***`) are left intact so
scrubbed skills stay readable.
"""
from __future__ import annotations

import re

REDACTED = "<REDACTED>"

# Placeholder-looking values we deliberately keep: shell/env references,
# angle-bracket templates, masked stars.
_PLACEHOLDER_RE = re.compile(r"^(\$|\{\{|<|\*\*\*)")

# --- Rule 1: private-key PEM blocks (multi-line) -------------------------
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)

# --- Rule 2: well-known token shapes (safe even standalone) ---------------
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:"
    r"sk-ant-[A-Za-z0-9_-]{10,}"          # Anthropic
    r"|sk-[A-Za-z0-9_-]{20,}"             # OpenAI-style
    r"|gh[pousr]_[A-Za-z0-9]{20,}"        # GitHub tokens
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|glpat-[A-Za-z0-9_-]{10,}"          # GitLab
    r"|AKIA[0-9A-Z]{16}"                  # AWS access key id
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"      # Slack
    r"|AIza[0-9A-Za-z_-]{35}"             # Google API key
    r"|ya29\.[0-9A-Za-z_-]{20,}"          # Google OAuth
    r"|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"  # JWT
    r")"
)

# --- Rule 3: Authorization headers ----------------------------------------
_AUTH_HEADER_RE = re.compile(
    r"(?i)(authorization\s*[:=]\s*['\"]?(?:bearer|basic|token)\s+)[^\s'\"]+"
)

# --- Rule 4: credentials embedded in URLs (keep the username) --------------
_URL_CRED_RE = re.compile(
    r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s/:@'\"]+):([^\s@'\"]+)@"
)

# --- Rule 5: KEY=value / key: value assignments ----------------------------
# Matches env vars, shell exports, yaml/json-ish pairs whose key smells like
# a credential. `pwd` and bare `auth` are deliberately absent (PWD is the
# working directory; `auth` matches `author`).
_ASSIGN_RE = re.compile(
    r"(?i)\b((?:[A-Za-z0-9_.-]*[_.-])?"
    r"(?:password|passwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|credentials?)"
    r"(?:[_.-][A-Za-z0-9_.-]*)?)"
    r"(\s*[:=]\s*)"
    r"(\"[^\"]+\"|'[^']+'|[^\s'\";,]+)"
)

# --- Rule 6: CLI credential flags ------------------------------------------
_FLAG_RE = re.compile(
    r"(?i)(--?(?:password|passwd|token|api-?key|secret|access-?key)(?:[= ]|\s+))"
    r"(\"[^\"]+\"|'[^']+'|\S+)"
)

# sshpass -p <pass> (space or attached)
_SSHPASS_RE = re.compile(r"(sshpass\s+-p\s*)(\"[^\"]+\"|'[^']+'|\S+)")

# mysql-family attached -pSecret (only in mysql commands; `ssh -p2222` is a
# port and must survive)
_MYSQL_P_RE = re.compile(
    r"((?:mysql|mysqldump|mysqladmin|mariadb)\b[^\n]*?\s-p)([^\s'\"-]\S*)"
)

# curl/wget/httpie style `-u user:pass` / `--user user:pass` (keep the user)
_USERPASS_RE = re.compile(r"((?:-u|--user)[= ]\s*['\"]?[^\s:'\"]+:)([^\s'\"]+)")

# --- Rule 7: passwords stated in prose --------------------------------------
# "password is hunter2" / "密码是 hunter2" / "密码：hunter2" — user messages
# and assistant replies are prose, not commands, so structural rules miss
# them. Value ends at whitespace or CJK/ASCII punctuation.
_PROSE_PW_RE = re.compile(
    r"(?i)((?:password|passphrase|pass\s?word)\s*(?:is|was|[:=])\s*"
    r"|[密口]\s*[码令][是为]\s*|[密口]\s*[码令]\s*[:：=]\s*)"
    r"([^\s,，。;；、'\"`]+)"
)


def _keep_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER_RE.match(value.strip("'\"")))


def _sub_keep_placeholder(pattern: re.Pattern, text: str, group_prefix: int) -> str:
    """Replace the trailing value group with REDACTED unless it's a placeholder.
    `group_prefix` is the group number holding the part to keep verbatim."""
    def repl(m: re.Match) -> str:
        value = m.group(group_prefix + 1)
        if _keep_placeholder(value):
            return m.group(0)
        return m.group(group_prefix) + REDACTED
    return pattern.sub(repl, text)


def scrub(text: str | None) -> str | None:
    """Return `text` with anything credential-shaped replaced by <REDACTED>.
    None/empty passes through unchanged."""
    if not text:
        return text
    text = _PEM_RE.sub(REDACTED, text)
    text = _KNOWN_TOKEN_RE.sub(REDACTED, text)
    text = _AUTH_HEADER_RE.sub(lambda m: m.group(1) + REDACTED, text)
    text = _URL_CRED_RE.sub(lambda m: f"{m.group(1)}:{REDACTED}@", text)
    # Assignments: keep "KEY=" and the quoting style is dropped — the value
    # becomes the bare REDACTED sentinel.
    def _assign_repl(m: re.Match) -> str:
        if _keep_placeholder(m.group(3)):
            return m.group(0)
        return m.group(1) + m.group(2) + REDACTED
    text = _ASSIGN_RE.sub(_assign_repl, text)
    text = _sub_keep_placeholder(_FLAG_RE, text, 1)
    text = _sub_keep_placeholder(_SSHPASS_RE, text, 1)
    text = _sub_keep_placeholder(_MYSQL_P_RE, text, 1)
    text = _sub_keep_placeholder(_USERPASS_RE, text, 1)
    text = _sub_keep_placeholder(_PROSE_PW_RE, text, 1)
    return text
