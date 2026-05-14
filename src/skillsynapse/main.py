"""Nightly cron entry. Chains the v0.1 pipeline:

  Step 0    discover manual skills              (bootstrap.py)
  Step 1    scan recent sessions                (scanner.py)
  Step 1.5  session_index                       — DEFERRED to v0.2
  Step 2/2.5 collect metrics                    (metrics.py)
  Step 3    extract candidates (LLM)            (extractor.py)
  Step 4    realize NEW → store + disk          (extractor.py + indexer.py)
  Step 4.5  Trigger 2 tool degradation          — DEFERRED to v0.2
  Step 5-8  split/merge/prune/compress          — DEFERRED to v0.2/v0.3
  Step 10   render hierarchy index              (indexer.py)

Usage:
  python -m skillsynapse.main                       # cron: yesterday → now
  python -m skillsynapse.main --hours-back 48       # wider window
  python -m skillsynapse.main --dry-run             # no LLM, no disk writes,
                                                      still prints what would happen
  python -m skillsynapse.main --skip-extract        # metrics + index only
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from . import bootstrap
from .config import Config, load_config, resolve_paths
from .extractor import extract_from_session, log_non_new_action, realize_candidate
from .indexer import render_categories_md, render_index_md, sync_captured_skill_files
from .llm_provider import LLMProvider, RateLimitDeferred
from .metrics import collect_metrics
from .scanner import find_sessions, yesterday_cutoff
from .store import Store


logger = logging.getLogger("skillsynapse")


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def run_pipeline(
    *,
    hours_back: int = 24,
    dry_run: bool = False,
    skip_extract: bool = False,
    cfg: Config | None = None,
) -> int:
    cfg = cfg or load_config()
    paths = resolve_paths(cfg)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.skills_root.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    logger.info("SkillSynapse run @ %s (hours_back=%d dry_run=%s)",
                datetime.now().isoformat(timespec="seconds"), hours_back, dry_run)

    with Store(paths.db_path, paths.decisions_log) as store:
        # ── Step 0: manual skill discovery ─────────────────
        discover_counts = bootstrap.discover_manual_skills(store, paths.skills_root)
        logger.info("Step 0  manual-skill discovery: %s", discover_counts)

        # ── Step 1: scan sessions ──────────────────────────
        cutoff = yesterday_cutoff(hours_back)
        sessions = find_sessions(
            paths.projects_root,
            modified_after=cutoff,
            exclude_subagents=cfg.extraction.exclude_subagents,
        )
        logger.info("Step 1  found %d sessions modified after %s",
                    len(sessions), cutoff.isoformat(timespec="seconds"))

        session_paths = [Path(s.path) for s in sessions]

        # ── Step 2/2.5: metrics ────────────────────────────
        metric_stats = collect_metrics(store, session_paths, cfg)
        logger.info("Step 2  metrics: %s", metric_stats)

        # ── Step 3/4: extract candidates ───────────────────
        # Buckets are disjoint and name the cause precisely:
        #   new_created         action=NEW, realize_candidate succeeded
        #   new_rejected        action=NEW, realize_candidate returned None
        #                       (bad name / bad category / manual collision /
        #                        name-exists / incomplete payload)
        #   extractor_skipped   action=SKIP (LLM's verdict — nothing reusable)
        #   update_deferred     action=UPDATE, v0.2 evolver will handle
        #   pitfall_deferred    action=PITFALL, v0.2 evolver will handle
        #   llm_error           LLM subprocess failed OR LLM output was
        #                       unparseable JSON — see decisions.jsonl for the
        #                       specific action (extractor_llm_error /
        #                       extractor_parse_error)
        #   no_candidate        below complexity threshold (silent filter miss)
        extract_stats = {
            "new_created": 0,
            "new_rejected": 0,
            "extractor_skipped": 0,
            "update_deferred": 0,
            "pitfall_deferred": 0,
            "llm_error": 0,
            "no_candidate": 0,
        }
        if skip_extract:
            logger.info("Step 3  extractor SKIPPED by flag")
        else:
            llm = LLMProvider(cfg)
            for path in session_paths:
                if dry_run:
                    logger.debug("  [dry-run] would extract from %s", path.name)
                    continue
                try:
                    candidates, sess_stats = extract_from_session(
                        path, store, llm, cfg
                    )
                except RateLimitDeferred as e:
                    store.log_decision(
                        "deferred_rate_limit",
                        source_session=path.stem,
                        details={"reason": str(e)},
                    )
                    logger.warning("rate limit deferred: %s", e)
                    break

                # Per-session stats (accumulated at episode level inside
                # extract_from_session). llm_error + no_candidate are
                # already disjoint from the NEW/SKIP/UPDATE/PITFALL buckets
                # below — each episode contributes to exactly one bucket.
                for key, value in sess_stats.items():
                    extract_stats[key] += value

                for cand in candidates:
                    if cand.action == "NEW":
                        skill = realize_candidate(cand, store, cfg)
                        if skill is not None:
                            extract_stats["new_created"] += 1
                        else:
                            extract_stats["new_rejected"] += 1
                    else:
                        log_non_new_action(cand, store)
                        if cand.action == "UPDATE":
                            extract_stats["update_deferred"] += 1
                        elif cand.action == "PITFALL":
                            extract_stats["pitfall_deferred"] += 1
                        else:
                            extract_stats["extractor_skipped"] += 1
            logger.info("Step 3/4  extractor: %s (llm_calls=%d)",
                        extract_stats, llm.calls_this_run)

        # ── Step 10: render hierarchy + sync files ─────────
        if not dry_run:
            files_written = sync_captured_skill_files(store, paths.skills_root)
            render_index_md(store, paths.index_md)
            render_categories_md(store, paths.categories_md)
            logger.info("Step 10  wrote %d SKILL.md, rendered _index.md + _categories.md",
                        files_written)
        else:
            logger.info("Step 10  SKIPPED (dry-run)")

        elapsed = time.time() - t0
        logger.info("run complete in %.1fs", elapsed)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="skillsynapse", description="SkillSynapse nightly pipeline (v0.1).")
    p.add_argument("--hours-back", type=int, default=24,
                   help="Only consider sessions modified in the last N hours (default 24).")
    p.add_argument("--dry-run", action="store_true",
                   help="Skip LLM calls and disk writes; still walk metrics.")
    p.add_argument("--skip-extract", action="store_true",
                   help="Run bootstrap + metrics + index, skip the LLM extractor.")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return run_pipeline(
            hours_back=args.hours_back,
            dry_run=args.dry_run,
            skip_extract=args.skip_extract,
        )
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
