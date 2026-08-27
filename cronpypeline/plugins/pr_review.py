#!/usr/bin/env python3
"""post_pr_review — CLI to post a review comment on a GitHub PR.

Agent-facing front door for the PRReviewAgent.  Instead of hand-writing
GitHub API calls, the agent writes its review body to a temp file and
invokes this CLI (via RunCommand) to post it.

Usage:
    python3 -m cronpypeline.plugins.pr_review <repo> \\
        --pr-number 2 --event COMMENT --body-file /tmp/pr_review.md

``<repo>`` is the workspace repo name (resolved in the SWE repos registry).
``--event`` must be one of COMMENT, APPROVE, or REQUEST_CHANGES.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from cronpypeline.plugins.swe_plugin import (
    _gh_api_post,
    _load_github_token,
)

VALID_EVENTS = ("COMMENT", "APPROVE", "REQUEST_CHANGES")

# Default repos registry path — can be overridden with --repos-file
DEFAULT_REPOS_FILE = "/spellbook_data/Serendipity/swe/repos.json"


def _load_repo_registry(repos_file: str) -> list[dict[str, Any]]:
    """Load the repo registry JSON file.

    :param repos_file: Path to the repos.json file.
    :returns: List of repo config dicts.
    """
    p = Path(repos_file)
    if not p.exists():
        print(f"ERROR: repos file '{repos_file}' not found.", file=sys.stderr)
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: failed to parse repos file: {exc}", file=sys.stderr)
        return []
    return data.get("repos", [])


def cmd_post(args: argparse.Namespace) -> int:
    """Post a PR review to GitHub.

    :param args: Parsed CLI arguments.
    :returns: Exit code (0 = success, 1 = error).
    """
    repos = _load_repo_registry(args.repos_file)
    repo = next((r for r in repos if r.get("name") == args.repo), None)
    if repo is None:
        print(f"ERROR: repo '{args.repo}' not found in registry "
              f"({args.repos_file}).", file=sys.stderr)
        return 1

    body = ""
    if args.body_file:
        p = Path(args.body_file)
        if not p.exists():
            print(f"ERROR: --body-file '{args.body_file}' not found.",
                  file=sys.stderr)
            return 1
        body = p.read_text(encoding="utf-8").strip()
    elif args.body:
        body = args.body.strip()
    if not body:
        print("ERROR: review body must be non-empty.", file=sys.stderr)
        return 1

    pr_number = args.pr_number
    if not pr_number or pr_number < 1:
        print("ERROR: --pr-number must be a positive integer.", file=sys.stderr)
        return 1

    event = (args.event or "").strip().upper()
    if event not in VALID_EVENTS:
        print(f"ERROR: --event is required and must be one of "
              f"{', '.join(VALID_EVENTS)}.", file=sys.stderr)
        return 1

    token = _load_github_token(repo)
    if not token:
        print("ERROR: no GitHub token found.", file=sys.stderr)
        return 1

    slug = (repo.get("slug") or "").strip()
    if "/" not in slug:
        print(f"ERROR: repo slug '{slug}' invalid.", file=sys.stderr)
        return 1
    owner, gh_repo_name = slug.split("/", 1)

    if args.dry_run:
        print(f"[DRY-RUN] would post {event} review on "
              f"{owner}/{gh_repo_name}#{pr_number}")
        print(f"Body ({len(body)} chars):")
        print(body[:500])
        return 0

    result = _gh_api_post(
        owner, gh_repo_name,
        f"pulls/{pr_number}/reviews",
        {"event": event, "body": body},
        token,
        expected_statuses=(200, 201),
    )
    if result is None:
        print("ERROR: failed to post PR review.", file=sys.stderr)
        return 1

    review_id = result.get("id", "?")
    print(f"Posted {event} review (id={review_id}) on "
          f"{owner}/{gh_repo_name}#{pr_number}")
    if args.json:
        print(json.dumps(result, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    :returns: ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(
        description="Post a review comment on a GitHub pull request.")
    parser.add_argument(
        "repo", help="Workspace repo name (as in repos.json)")
    parser.add_argument(
        "--repos-file", default=DEFAULT_REPOS_FILE,
        help=f"Path to repos.json (default: {DEFAULT_REPOS_FILE})")
    parser.add_argument(
        "--pr-number", type=int, required=True,
        help="Pull request number to review")
    parser.add_argument(
        "--event", required=True, choices=VALID_EVENTS,
        help="Review event type (COMMENT, APPROVE, or REQUEST_CHANGES)")
    parser.add_argument(
        "--body-file", help="Path to a file holding the markdown review body")
    parser.add_argument(
        "--body", help="Inline markdown review body (alternative to --body-file)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be posted without actually posting")
    parser.add_argument(
        "--json", action="store_true",
        help="Emit the full API response as JSON on success")
    return parser


def main() -> int:
    """CLI entry point.

    :returns: Exit code.
    """
    args = build_parser().parse_args()
    return cmd_post(args)


if __name__ == "__main__":
    sys.exit(main())
