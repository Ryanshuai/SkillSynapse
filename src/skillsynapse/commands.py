"""Read-only user commands — v0.1 ships `/skill list`, `/skill show`,
`/skill health` (§9, §12 v0.1).

Invoke via `python -m skillsynapse.commands list|show <name>|health` or the
`skill` entry-point installed by pyproject.toml.
"""
from __future__ import annotations

import argparse
import sys

from .config import Config, load_config, resolve_paths
from .store import Store


def _mk_store(cfg: Config) -> Store:
    paths = resolve_paths(cfg)
    return Store(paths.db_path, paths.decisions_log)


def _truncate(s: str, n: int) -> str:
    if not s:
        return ""
    first = s.splitlines()[0]
    return first[:n] + "…" if len(first) > n else first


def cmd_list(cfg: Config) -> int:
    with _mk_store(cfg) as store:
        skills = store.list_active_skills()
        if not skills:
            print("(no active skills)")
            return 0
        # Both columns are version-scoped so EFF/SEL tell a coherent story. A
        # freshly version-bumped skill shows SEL=0 + EFF=- rather than an old
        # lifetime SEL paired with a zero version-scoped EFF (misleading).
        # Lifetime totals are still available via `/skill show` and /health.
        header = (
            f"{'NAME':<32} {'CAT':<20} {'ORIGIN':<9} {'PROT':<4} "
            f"{'SEL(v)':>6} {'EFF(v)':>6}  DESCRIPTION"
        )
        print(header)
        print("-" * len(header))
        for s in skills:
            cat = s.category or "-"
            prot = "M" if s.manual_protected else " "
            eff = f"{s.effective_rate:.2f}" if s.selections_since_version else "    -"
            print(
                f"{s.name[:32]:<32} {cat[:20]:<20} {s.origin:<9} {prot:<4} "
                f"{s.selections_since_version:>6} {eff:>6}  "
                f"{_truncate(s.description, 80)}"
            )
    return 0


def cmd_show(cfg: Config, name: str) -> int:
    with _mk_store(cfg) as store:
        skill = store.get_skill_by_name(name)
        if skill is None:
            print(f"No active skill named '{name}'.", file=sys.stderr)
            return 1
        print(f"── {skill.name} (v{skill.version}, {skill.origin}"
              f"{', manual-protected' if skill.manual_protected else ''}) ──")
        print(f"category:    {skill.category or '(uncategorized)'}")
        print(f"description: {skill.description}")
        print(f"created_at:  {skill.created_at}")
        print(f"updated_at:  {skill.updated_at}")
        print(f"selections:  total={skill.total_selections}  "
              f"since_version={skill.selections_since_version}")
        print(f"rates (version-scoped): applied={skill.applied_rate:.2f} "
              f"completion={skill.completion_rate:.2f} "
              f"effective={skill.effective_rate:.2f} "
              f"fallback={skill.fallback_rate:.2f}")
        print(f"tool_dependencies: {', '.join(skill.tool_dependencies) or '-'}")
        print(f"critical_tools:    {', '.join(skill.critical_tools) or '-'}")
        if skill.pitfalls:
            print()
            print("pitfalls:")
            for p in skill.pitfalls:
                print(f"  - [{p.hit_count}x] {p.description}")
        if skill.recent_analyses:
            print()
            print(f"recent analyses (last {len(skill.recent_analyses)}):")
            for a in skill.recent_analyses:
                print(f"  [{a.timestamp}] {a.verdict:<12} {a.note}")
        print()
        print("── content ──")
        print(skill.content)
    return 0


def cmd_health(cfg: Config) -> int:
    with _mk_store(cfg) as store:
        skills = store.list_active_skills()
        total = len(skills)
        manual = sum(1 for s in skills if s.manual_protected)
        captured = total - manual
        probation = sum(1 for s in skills if s.probation)
        total_selections = sum(s.total_selections for s in skills)
        cats = store.list_categories()

        pending_count = store.count_pending_reviews()
        orphan_count = store.count_floating_orphans()
        gap_count = store.count_open_coverage_gaps()

        print("── SkillSynapse health ──")
        print(f"active skills:       {total}  (manual={manual}, captured={captured})")
        print(f"probation:           {probation}")
        print(f"categories:          {len(cats)} "
              f"(main={sum(1 for c in cats if not c['manual_only'])}, "
              f"manual_only={sum(1 for c in cats if c['manual_only'])})")
        print(f"lifetime selections: {total_selections}")
        print(f"pending reviews:     {pending_count}")
        print(f"floating orphans:    {orphan_count}")
        print(f"open coverage gaps:  {gap_count}")
        if skills:
            hot = sorted(skills, key=lambda s: s.total_selections, reverse=True)[:5]
            print()
            print("top 5 by lifetime selections:")
            for s in hot:
                print(f"  {s.name:<40} {s.total_selections:>4}  "
                      f"eff={s.effective_rate:.2f}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skill",
        description="SkillSynapse read-only commands (v0.1).",
    )
    sub = parser.add_subparsers(dest="sub", required=True)
    sub.add_parser("list", help="List active skills")
    p_show = sub.add_parser("show", help="Show a skill's full content + metrics")
    p_show.add_argument("name")
    sub.add_parser("health", help="Print library-wide health summary")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config()

    if args.sub == "list":
        return cmd_list(cfg)
    if args.sub == "show":
        return cmd_show(cfg, args.name)
    if args.sub == "health":
        return cmd_health(cfg)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
