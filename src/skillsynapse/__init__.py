"""SkillSynapse — nightly cron that grows Claude Code skills from session logs.

Design docs live in `docs/` — start at `docs/README.md`.
This package implements the v0.1 minimum loop:
    scan sessions → record metrics → extract candidates → write SKILL.md
    → render _index.md. No evolution / prune / Trigger 2 / review in v0.1.
"""

__version__ = "0.1.0"
