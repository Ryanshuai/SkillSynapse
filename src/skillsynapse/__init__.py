"""SkillSynapse — nightly cron that grows Claude Code skills from session logs.

See `SkillSynapse-Design-v3.5-final.md` for the full design.
This package implements the v0.1 minimum loop (§12):
    scan sessions → record metrics → extract candidates → write SKILL.md
    → render _index.md. No evolution / prune / Trigger 2 / review in v0.1.
"""

__version__ = "0.1.0"
