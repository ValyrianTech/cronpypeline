"""CLI entry point for cronpypeline.

Usage: python -m cronpypeline [OPTIONS]
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from cronpypeline.pipeline import Pipeline, TickResultStatus


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    :returns: A configured :class:`argparse.ArgumentParser` instance.
    """
    parser = argparse.ArgumentParser(
        prog="cronpypeline",
        description="Cron-friendly, stateful, multi-stage agentic pipelines.",
    )
    parser.add_argument(
        "--config",
        type=str,
        required=True,
        help="Path to pipeline JSON config",
    )
    parser.add_argument(
        "--target",
        type=str,
        default=None,
        help="Limit to a single target (repo, country, etc.)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        default=False,
        help="Process one action per target (default: first target with work)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show planned action without executing",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=False,
        help="Verbose output",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        default=False,
        help="Print pipeline state and exit (no actions)",
    )
    parser.add_argument(
        "--reset-stage",
        type=str,
        default=None,
        help="Delete a stage's completion marker to force re-run",
    )
    parser.add_argument(
        "--reset-target",
        type=str,
        default=None,
        help="Clear all markers for a target (nuclear reset)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point.

    :param argv: Command-line arguments. If None, uses ``sys.argv``.
    :returns: Exit code (0 on success, non-zero on error).
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # Load config
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}", file=sys.stderr)
        return 1

    try:
        pipeline = Pipeline.from_config(config_path)
    except Exception as e:  # noqa: BLE001
        print(f"Error loading config: {e}", file=sys.stderr)
        return 1

    # Handle --status
    if args.status:
        targets = None
        if args.target:
            targets = [args.target]
        status = pipeline.status(targets)
        print(json.dumps(status, indent=2))
        return 0

    # Handle --reset-stage
    if args.reset_stage:
        target = args.target or "."
        target_dir = pipeline.workspace_dir / target
        for stage in pipeline.config.stages:
            if stage.id == args.reset_stage:
                if "completion" in stage.markers:
                    from cronpypeline.markers import delete_marker
                    delete_marker(stage.markers["completion"], target_dir)
                    print(f"Reset stage {stage.id} for target {target}")
                break
        return 0

    # Handle --reset-target
    if args.reset_target:
        target_dir = pipeline.workspace_dir / args.reset_target
        for stage in pipeline.config.stages:
            from cronpypeline.markers import delete_marker
            for marker in stage.markers.values():
                delete_marker(marker, target_dir)
        print(f"Reset all markers for target {args.reset_target}")
        return 0

    # Normal tick execution
    if args.all:
        results = pipeline.tick_all(dry_run=args.dry_run, verbose=args.verbose)
        for result in results:
            print(result)
        # Return non-zero if any action failed
        if any(r.status == TickResultStatus.ACTION_FAILED for r in results):
            return 1
        return 0
    else:
        result = pipeline.tick(
            target=args.target,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
        print(result)
        if result.status == TickResultStatus.ACTION_FAILED:
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
