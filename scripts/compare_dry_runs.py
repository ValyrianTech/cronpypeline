#!/usr/bin/env python3
"""Compare dry-run outputs of the old SWE pipeline and the cronpypeline migration.

Runs both pipelines in --dry-run --verbose mode for every enabled repo in the
SWE repo registry, captures their stdout/stderr, and prints a side-by-side
comparison so discrepancies can be spotted quickly.

Usage:
    python3 scripts/compare_dry_runs.py
    python3 scripts/compare_dry_runs.py --repo cronpypeline
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

# ─── Paths ───────────────────────────────────────────────────────────────────

CRONPYPELINE_ROOT = Path(__file__).resolve().parent.parent
SPELLBOOK_ROOT = CRONPYPELINE_ROOT.parent / "spellbook"
SWE_DATA_DIR = Path("/spellbook_data/Serendipity/swe")
REPOS_JSON = SWE_DATA_DIR / "repos.json"
SWE_PIPELINE_CONFIG = CRONPYPELINE_ROOT / "configs" / "swe_pipeline.json"

OLD_PIPELINE_SCRIPT = SPELLBOOK_ROOT / "apps" / "Serendipity" / "SWE" / "scripts" / "run_swe_pipeline.py"
OLD_PIPELINE_VENV = SPELLBOOK_ROOT / ".venv" / "bin" / "python"

CRONPYPELINE_VENV = CRONPYPELINE_ROOT / ".venv" / "bin" / "python"


# ─── Helpers ─────────────────────────────────────────────────────────────────


def load_enabled_repos() -> list[dict]:
    """Load enabled repos from the SWE repo registry."""
    if not REPOS_JSON.exists():
        print(f"ERROR: repo registry not found at {REPOS_JSON}", file=sys.stderr)
        return []
    data = json.loads(REPOS_JSON.read_text())
    return [r for r in data.get("repos", []) if r.get("enabled")]


def run_command(cmd: list[str], cwd: Path, timeout: int = 120) -> tuple[int, str, str]:
    """Run a command and return (exit_code, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"TIMEOUT after {timeout}s"
    except FileNotFoundError as exc:
        return 127, "", str(exc)


def run_old_pipeline(repo_name: str) -> tuple[int, str, str]:
    """Run the old SWE pipeline in dry-run mode for a single repo."""
    cmd = [
        str(OLD_PIPELINE_VENV),
        str(OLD_PIPELINE_SCRIPT),
        "--repo",
        repo_name,
        "--dry-run",
        "--verbose",
    ]
    return run_command(cmd, cwd=SPELLBOOK_ROOT)


def run_cronpypeline(repo_name: str) -> tuple[int, str, str]:
    """Run cronpypeline in dry-run mode for a single repo."""
    cmd = [
        str(CRONPYPELINE_VENV),
        "-m",
        "cronpypeline",
        "--config",
        str(SWE_PIPELINE_CONFIG),
        "--target",
        repo_name,
        "--dry-run",
        "--verbose",
    ]
    return run_command(cmd, cwd=CRONPYPELINE_ROOT)


def normalize_output(text: str) -> str:
    """Normalize output for comparison: strip trailing whitespace per line."""
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines)


# ─── Main ────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare dry-run outputs of old SWE pipeline vs cronpypeline."
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Only compare a specific repo (default: all enabled repos)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Timeout in seconds per command (default: 120)",
    )
    args = parser.parse_args()

    repos = load_enabled_repos()
    if not repos:
        print("No enabled repos found.", file=sys.stderr)
        return 1

    if args.repo:
        repos = [r for r in repos if r["name"] == args.repo]
        if not repos:
            print(f"Repo '{args.repo}' not found or not enabled.", file=sys.stderr)
            return 1

    any_diff = False

    for repo in repos:
        repo_name = repo["name"]
        print(f"\n{'=' * 70}")
        print(f"  REPO: {repo_name}")
        print(f"{'=' * 70}")

        # Run old pipeline
        old_code, old_out, old_err = run_old_pipeline(repo_name)
        old_combined = old_out + ("\n" + old_err if old_err.strip() else "")

        # Run cronpypeline
        cron_code, cron_out, cron_err = run_cronpypeline(repo_name)
        cron_combined = cron_out + ("\n" + cron_err if cron_err.strip() else "")

        # Print side-by-side
        print(f"\n--- OLD PIPELINE (exit={old_code}) ---")
        print(old_combined.strip() or "(no output)")

        print(f"\n--- CRONPYPELINE (exit={cron_code}) ---")
        print(cron_combined.strip() or "(no output)")

        # Quick comparison summary
        old_norm = normalize_output(old_combined)
        cron_norm = normalize_output(cron_combined)
        match = old_norm == cron_norm

        print("\n--- COMPARISON ---")
        if match:
            print("  ✅ Outputs match (after normalization)")
        else:
            any_diff = True
            print("  ❌ Outputs DIFFER")
            # Show a simple diff
            import difflib

            diff = difflib.unified_diff(
                old_norm.splitlines(),
                cron_norm.splitlines(),
                fromfile="old_pipeline",
                tofile="cronpypeline",
                lineterm="",
            )
            for line in diff:
                print(f"  {line}")

    print(f"\n{'=' * 70}")
    if any_diff:
        print("  RESULT: Differences found — review the diffs above.")
        return 1
    else:
        print("  RESULT: All repos match.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
