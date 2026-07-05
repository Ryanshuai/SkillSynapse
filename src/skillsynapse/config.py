"""Config loader.

First run copies `config_default.yaml` to `~/.claude/skillsynapse/config.yaml`.
Subsequent runs load the user copy and deep-merge it over the defaults so new
keys added in later versions are auto-filled.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


_DEFAULT_YAML = Path(__file__).parent / "config_default.yaml"


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    """Attribute-style access over a nested dict.

    `cfg.extraction.min_tool_calls` is the same as `cfg.raw["extraction"]["min_tool_calls"]`.
    """

    def __init__(self, raw: dict[str, Any]):
        self.raw = raw

    def __getattr__(self, key: str) -> Any:
        if key == "raw":
            raise AttributeError(key)
        if key not in self.raw:
            raise AttributeError(f"Config has no key '{key}'")
        val = self.raw[key]
        if isinstance(val, dict):
            return Config(val)
        return val

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self.raw


@dataclass(frozen=True)
class Paths:
    skills_root: Path
    data_dir: Path
    projects_root: Path
    aggregation_root: Path | None
    db_path: Path
    decisions_log: Path
    index_md: Path
    categories_md: Path
    user_config: Path


def resolve_paths(cfg: Config) -> Paths:
    skills_root = Path(cfg.paths.skills_root).expanduser()
    data_dir = Path(cfg.paths.data_dir).expanduser()
    projects_root = Path(cfg.paths.projects_root).expanduser()
    agg_raw = cfg.paths.get("aggregation_root")
    aggregation_root = Path(agg_raw).expanduser() if agg_raw else None
    return Paths(
        skills_root=skills_root,
        data_dir=data_dir,
        projects_root=projects_root,
        aggregation_root=aggregation_root,
        db_path=data_dir / "db.sqlite",
        decisions_log=data_dir / "logs" / "decisions.jsonl",
        index_md=skills_root / "_index.md",
        categories_md=skills_root / "_categories.md",
        user_config=data_dir / "config.yaml",
    )


def load_defaults() -> dict[str, Any]:
    with _DEFAULT_YAML.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_config(user_config_path: Path | None = None) -> Config:
    """Load config. If user config doesn't exist, copy defaults there first."""
    defaults = load_defaults()

    if user_config_path is None:
        data_dir = Path(defaults["paths"]["data_dir"]).expanduser()
        user_config_path = data_dir / "config.yaml"

    if not user_config_path.exists():
        user_config_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(_DEFAULT_YAML, user_config_path)
        merged = defaults
    else:
        with user_config_path.open("r", encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        merged = _deep_merge(defaults, user)

    return Config(merged)
