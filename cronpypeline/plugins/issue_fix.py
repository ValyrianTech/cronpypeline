"""SWE issue-fix SELECT/GATE state machine — native cronpypeline port.

Replaces the external ``run_issue_fix.py`` script from the spellbook.
Drives a single issue through a fix loop:

  SELECT  (no active task)  — pick an open issue, wrap a task.json,
                              create the task branch, queue CoderAgent.
  GATE    (agent wrote coding_complete.marker)
                              re-run verification tools, capture the
                              diff, finalize the issue status.
"""

import json
import os
import re
import shutil
import subprocess  # nosec B404 - subprocess used for git and verification commands
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cronpypeline.actions import TickContext
from cronpypeline.config import ActionSpec, ActionType
from cronpypeline.plugins.issue_store import Issue, load_issues, set_issue_status
from cronpypeline.plugins.swe_plugin import (
    COVERAGE_TARGET,
    INTEGRATION_BRANCH,
    PHASE_A_BRANCH,
    PHASE_A_GIT_AUTHOR_EMAIL,
    PHASE_A_GIT_AUTHOR_NAME,
    SWE_SUBDIR,
    TASKS_DIR,
    _find_active_task,
    _git,
    _read_github_session,
)

# ─── Constants ───────────────────────────────────────────────────────────────

CODING_COMPLETE_MARKER = "coding_complete.marker"
GATE_RESULT_FILE = "gate.json"
TASK_FILE = "task.json"
DIFF_FILE = "diff.patch"
FILES_CHANGED_FILE = "files_changed.json"

TASK_BRANCH_PREFIX = "swe-pipeline/task_"
GIT_AUTHOR_NAME = PHASE_A_GIT_AUTHOR_NAME
GIT_AUTHOR_EMAIL = PHASE_A_GIT_AUTHOR_EMAIL

MAX_ATTEMPTS = 3
TASK_TIMEOUT_MINUTES = 30

REVIEW_TYPE = "review"

PIPELINE_EXCLUDES = (
    f"/{SWE_SUBDIR}/",
    ".coverage",
    "htmlcov/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
)

_TOOLING_ARTIFACT_PATTERNS = (".coverage",)


# ─── Git helpers ─────────────────────────────────────────────────────────────


def _ensure_pipeline_excludes(repo_dir: Path, verbose: bool = False) -> None:
    """Ensure pipeline + tooling artifacts are git-excluded via .git/info/exclude.

    :param repo_dir: Target repo directory.
    :param verbose: If True, print progress.
    """
    exclude_file = repo_dir / ".git" / "info" / "exclude"
    try:
        existing = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
        present = set(existing.split())
        missing = [e for e in PIPELINE_EXCLUDES if e not in present]
        if not missing:
            return
        exclude_file.parent.mkdir(parents=True, exist_ok=True)
        with open(exclude_file, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("# Added by SWE pipeline: never commit pipeline/tooling artifacts\n")
            f.writelines(f"{e}\n" for e in missing)
        if verbose:
            print(f"  [git] excluded {missing} via .git/info/exclude")
    except OSError as exc:
        if verbose:
            print(f"  [git] WARNING: could not update .git/info/exclude: {exc}", file=sys.stderr)


def _ensure_tooling_artifacts_untracked(repo_dir: Path, verbose: bool = False) -> None:
    """Untrack tooling artifacts that are accidentally tracked in the repo.

    :param repo_dir: Target repo directory.
    :param verbose: If True, print progress.
    """
    try:
        tracked = _git(repo_dir, "ls-files", "--", *_TOOLING_ARTIFACT_PATTERNS, check=False).stdout.strip()
    except subprocess.CalledProcessError:
        return
    if not tracked:
        return

    tracked_files = tracked.splitlines()
    gitignore = repo_dir / ".gitignore"
    gitignore_lines = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []

    new_ignores = [f for f in tracked_files if f not in gitignore_lines]
    if new_ignores:
        with open(gitignore, "a", encoding="utf-8") as f:
            if gitignore_lines and not gitignore_lines[-1].endswith("\n"):
                f.write("\n")
            f.writelines(f"{pattern}\n" for pattern in new_ignores)

    try:
        _git(repo_dir, "rm", "--cached", "--", *tracked_files)
        if new_ignores:
            _git(repo_dir, "add", ".gitignore")
        _git(repo_dir, "commit", "-m", "chore(swe): untrack pipeline tooling artifacts", check=False)
        if verbose:
            print(f"  [git] untracked tooling artifacts: {', '.join(tracked_files)}")
    except subprocess.CalledProcessError as exc:
        if verbose:
            print(f"  [git] WARNING: could not untrack tooling artifacts: {exc.stderr or exc.stdout}")


def ensure_integration_branch(repo_dir: Path, default_branch: str,
                               verbose: bool = False) -> bool:
    """Create and/or check out the integration branch.

    On first creation the branch is based off PHASE_A_BRANCH when it exists,
    otherwise off default_branch. An existing integration branch is just
    checked out, never rebased.

    :param repo_dir: Target repo directory.
    :param default_branch: Default branch name (e.g. 'main').
    :param verbose: If True, print progress.
    :returns: True on success, False if not a git repo or working tree is dirty.
    """
    try:
        _git(repo_dir, "rev-parse", "--git-dir")
    except subprocess.CalledProcessError:
        if verbose:
            print(f"  [git] {repo_dir} is not a git repo")
        return False

    _ensure_pipeline_excludes(repo_dir, verbose=verbose)
    _ensure_tooling_artifacts_untracked(repo_dir, verbose=verbose)

    dirty = _git(repo_dir, "status", "--porcelain").stdout.strip()
    if dirty:
        if verbose:
            print(f"  [git] working tree at {repo_dir} is dirty; refusing to set up "
                  f"the integration branch:\n{dirty}")
        return False

    existing = _git(repo_dir, "branch", "--list", INTEGRATION_BRANCH, check=False).stdout.strip()
    phase_a_exists = bool(_git(repo_dir, "branch", "--list", PHASE_A_BRANCH, check=False).stdout.strip())
    base_branch = PHASE_A_BRANCH if phase_a_exists else default_branch
    try:
        if existing:
            _git(repo_dir, "checkout", INTEGRATION_BRANCH)
        else:
            _git(repo_dir, "checkout", base_branch)
            _git(repo_dir, "checkout", "-b", INTEGRATION_BRANCH)
        if verbose:
            print(f"  [git] on {INTEGRATION_BRANCH} (base {base_branch})")
        return True
    except subprocess.CalledProcessError as exc:
        if verbose:
            print(f"  [git] failed to set up {INTEGRATION_BRANCH}: {exc.stderr or exc.stdout}")
        return False


def merge_into_integration(repo_dir: Path, task_branch: str,
                           verbose: bool = False) -> bool:
    """Merge a passed task branch into the integration branch (no fast-forward).

    :param repo_dir: Target repo directory.
    :param task_branch: Task branch to merge.
    :param verbose: If True, print progress.
    :returns: True on success, False on failure.
    """
    try:
        _git(repo_dir, "checkout", INTEGRATION_BRANCH)
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = GIT_AUTHOR_NAME
        env["GIT_AUTHOR_EMAIL"] = GIT_AUTHOR_EMAIL
        env["GIT_COMMITTER_NAME"] = GIT_AUTHOR_NAME
        env["GIT_COMMITTER_EMAIL"] = GIT_AUTHOR_EMAIL
        subprocess.run(
            ["git", "-C", str(repo_dir), "merge", "--no-ff", task_branch,
             "-m", f"merge {task_branch} into {INTEGRATION_BRANCH}"],
            check=True, env=env, capture_output=True, text=True,
        )  # nosec B603 - static args
        if verbose:
            print(f"  [git] merged {task_branch} -> {INTEGRATION_BRANCH}")
        return True
    except subprocess.CalledProcessError as exc:
        if verbose:
            print(f"  [git] merge of {task_branch} FAILED: {exc.stderr or exc.stdout}")
        return False


# ─── Coverage parsing ────────────────────────────────────────────────────────


def _parse_coverage_output(output: str) -> dict[str, Any]:
    """Parse pytest-cov term-missing output.

    :param output: Raw stdout from coverage tool.
    :returns: Dict with total_stmts, total_miss, coverage_pct, files, tests_passed, tests_failed.
    """
    summary: dict[str, Any] = {
        "total_stmts": 0, "total_miss": 0, "coverage_pct": 0.0,
        "files": [], "tests_passed": 0, "tests_failed": 0,
    }

    m = re.search(r"^TOTAL\s+(\d+)\s+(\d+)\s+(\d+)%", output, re.MULTILINE)
    if m:
        summary["total_stmts"] = int(m.group(1))
        summary["total_miss"] = int(m.group(2))
        summary["coverage_pct"] = float(m.group(3))

    for fm in re.finditer(
        r"^(\S+\.py)\s+(\d+)\s+(\d+)\s+(\d+)%(?:\s+(.+))?$",
        output, re.MULTILINE,
    ):
        if fm.group(1) == "TOTAL":
            continue
        summary["files"].append({
            "file": fm.group(1),
            "stmts": int(fm.group(2)),
            "miss": int(fm.group(3)),
            "cover": int(fm.group(4)),
            "missing": fm.group(5).strip() if fm.group(5) else "",
        })

    m_pass = re.search(r"(\d+)\s+passed", output)
    if m_pass:
        summary["tests_passed"] = int(m_pass.group(1))
    m_fail = re.search(r"(\d+)\s+failed", output)
    if m_fail:
        summary["tests_failed"] = int(m_fail.group(1))

    return summary


# ─── Issue helpers ───────────────────────────────────────────────────────────


def _safe_slug(value: str) -> str:
    """Make a string safe for use as a directory/file name component.

    :param value: String to slugify.
    :returns: Safe slug string.
    """
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_") or "unknown"


def select_open_issue(repo_dir: Path, issue_id: str | None = None,
                      verbose: bool = False) -> Issue | None:
    """Pick the issue to work on: explicit id, else first 'open' one.

    :param repo_dir: Target repo directory.
    :param issue_id: Optional specific issue id to select.
    :param verbose: If True, print progress.
    :returns: Selected Issue, or None if no open issue.
    """
    issues = load_issues(repo_dir)
    if not issues:
        if verbose:
            print(f"  [select] no issues under {repo_dir / SWE_SUBDIR / 'issues'}")
        return None

    if issue_id:
        for issue in issues:
            if str(issue.id) == issue_id:
                return issue
        if verbose:
            print(f"  ERROR: issue '{issue_id}' not found")
        return None

    open_issues = [i for i in issues if i.status == "open" and i.id is not None]
    if not open_issues:
        if verbose:
            print("  [select] no issues with status 'open'")
        return None

    session = _read_github_session(repo_dir)
    if session is not None and session.get("active"):
        session_start = session.get("started_at", "")
        session_issues = [
            i for i in open_issues
            if i.source == "github" or (i.created_at or "") >= session_start
        ]
        if session_issues:
            return session_issues[0]
        return None

    open_issues.sort(key=lambda i: (
        0 if i.hivemind_score else 1,
        int(i.rank) if i.hivemind_score and i.rank else 0,
        i.id or "",
    ))
    return open_issues[0]


def _finalize_issue_outcome(issue: Issue, repo_dir: Path, passed: bool,
                             verbose: bool = False) -> tuple[str, int]:
    """Record a gate outcome on the issue: set status + bump attempt count.

    :param issue: Issue to finalize.
    :param repo_dir: Target repo directory (for set_issue_status).
    :param passed: Whether the gate passed.
    :param verbose: If True, print progress.
    :returns: Tuple of (new_status, attempts).
    """
    attempts = issue.attempts or 0

    if passed:
        status = "done"
    else:
        attempts += 1
        status = "discarded" if attempts >= MAX_ATTEMPTS else "open"

    set_issue_status(repo_dir, issue.id, status)
    issue.attempts = attempts
    issue.status = status
    issues_dir = repo_dir / SWE_SUBDIR / "issues"
    issue_path = issues_dir / f"{issue.id}.md"
    if issue_path.exists():
        from cronpypeline.plugins.issue_store import _write_issue_file
        _write_issue_file(issue_path, issue)

    if verbose and not passed:
        print(f"  [issue] {issue.id}: attempt {attempts}/{MAX_ATTEMPTS}")
    return (status, attempts)


# ─── Task dir + state detection ──────────────────────────────────────────────


def _read_task(task_dir: Path) -> dict[str, Any]:
    """Read task.json from a task directory.

    :param task_dir: Task directory path.
    :returns: Task dict.
    """
    return json.loads((task_dir / TASK_FILE).read_text(encoding="utf-8"))


def _is_task_stale(task_dir: Path) -> bool:
    """Return True if the task is older than TASK_TIMEOUT_MINUTES.

    :param task_dir: Task directory path.
    :returns: True if stale.
    """
    task_file = task_dir / TASK_FILE
    if not task_file.exists():
        return True
    try:
        task = json.loads(task_file.read_text(encoding="utf-8"))
        created_str = task.get("created_at", "")
        if not created_str:
            return True
        created = datetime.fromisoformat(created_str)
    except (OSError, json.JSONDecodeError, ValueError):
        return True
    age = (datetime.now(timezone.utc) - created).total_seconds() / 60
    return age > TASK_TIMEOUT_MINUTES


def _task_branch_name(task_id: str) -> str:
    """Return the git branch name for a task.

    :param task_id: Task identifier.
    :returns: Branch name string.
    """
    return f"{TASK_BRANCH_PREFIX}{task_id}"


def _iter_task_dirs() -> list[Path]:
    """Return every task directory under TASKS_DIR (all date buckets).

    :returns: List of task directory paths.
    """
    if not TASKS_DIR.is_dir():
        return []
    result: list[Path] = []
    for date_dir in TASKS_DIR.iterdir():
        if not date_dir.is_dir():
            continue
        result.extend(d for d in date_dir.iterdir() if d.is_dir())
    return result


def _cleanup_stale_task(repo_dir: Path, task_dir: Path,
                        verbose: bool = False) -> bool:
    """Clean up a stale task: reset git state, reset issue, delete task dir.

    :param repo_dir: Target repo directory.
    :param task_dir: Stale task directory.
    :param verbose: If True, print progress.
    :returns: True if cleanup succeeded.
    """
    task = _read_task(task_dir)
    task_id = task.get("task_id", task_dir.name)
    branch = task.get("branch", _task_branch_name(task_id))
    default_branch = task.get("default_branch", "main")
    source_issue_id = task.get("source_issue_id", "")

    if verbose:
        age_mins = (datetime.now(timezone.utc) -
                    datetime.fromisoformat(task.get("created_at", ""))).total_seconds() / 60
        print(f"  stale task '{task_id}' ({age_mins:.0f} min old, "
              f"limit {TASK_TIMEOUT_MINUTES} min) — cleaning up")

    try:
        _git(repo_dir, "rev-parse", "--git-dir")
        intg_exists = _git(repo_dir, "branch", "--list", INTEGRATION_BRANCH, check=False).stdout.strip()
        if not intg_exists:
            _git(repo_dir, "checkout", default_branch, check=False)
            _git(repo_dir, "checkout", "-b", INTEGRATION_BRANCH, check=False)

        _git(repo_dir, "checkout", "--force", INTEGRATION_BRANCH, check=False)
        _git(repo_dir, "clean", "-fd", check=False)
        if verbose:
            print(f"  [git] force-checked out {INTEGRATION_BRANCH}, discarded dirty tree")

        branch_list = _git(repo_dir, "branch", "--list", branch, check=False).stdout.strip()
        if branch_list:
            _git(repo_dir, "branch", "-D", branch, check=False)
            if verbose:
                print(f"  [git] deleted stale task branch {branch}")
    except subprocess.CalledProcessError as exc:
        if verbose:
            print(f"  [git] WARNING during stale-task git cleanup: {exc}")

    if source_issue_id:
        for i in load_issues(repo_dir):
            if str(i.id) == source_issue_id:
                _finalize_issue_outcome(i, repo_dir, passed=False, verbose=verbose)
                break

    try:
        shutil.rmtree(task_dir)
        if verbose:
            print(f"  [task] removed stale task dir {task_dir.name}")
    except OSError as exc:
        print(f"  [task] WARNING: could not remove stale task dir {task_dir}: {exc}")
        return False

    return True


def _cleanup_orphaned_task_dirs(repo_name: str, verbose: bool = False) -> None:
    """Remove task dirs for *repo_name* that have no task.json.

    :param repo_name: Repo name to match.
    :param verbose: If True, print progress.
    """
    safe_repo = _safe_slug(repo_name)
    for task_dir in _iter_task_dirs():
        if (task_dir / TASK_FILE).exists():
            continue
        parts = task_dir.name.split("_", 1)
        if len(parts) < 2 or not parts[1].startswith(f"{safe_repo}_"):
            continue
        if verbose:
            print(f"  {repo_name}: removing orphaned task dir {task_dir.name} (no {TASK_FILE})")
        shutil.rmtree(task_dir, ignore_errors=True)


def _recover_orphaned_triaged(repo_dir: Path, repo_name: str,
                               verbose: bool = False) -> None:
    """Reset stale 'triaged' issues that have no matching active task to 'open'.

    :param repo_dir: Target repo directory.
    :param repo_name: Repo name.
    :param verbose: If True, print progress.
    """
    issues = load_issues(repo_dir)
    for issue in sorted(issues, key=lambda i: i.created_at or ""):
        if issue.status != "triaged":
            continue
        issue_id = str(issue.id or "")
        has_task = False
        for task_dir in _iter_task_dirs():
            try:
                task = json.loads((task_dir / TASK_FILE).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if task.get("source_issue_id") == issue_id and task.get("repo_name") == repo_name:
                has_task = True
                break
        if has_task:
            continue
        created_str = issue.created_at or ""
        if not created_str:
            continue
        try:
            created = datetime.fromisoformat(created_str)
        except ValueError:
            continue
        age = (datetime.now(timezone.utc) - created).total_seconds() / 60
        if age <= TASK_TIMEOUT_MINUTES:
            continue
        _finalize_issue_outcome(issue, repo_dir, passed=False, verbose=verbose)


def _ensure_task_branch(repo_dir: Path, task_id: str, default_branch: str,
                        verbose: bool = False) -> bool:
    """Check out the integration branch, then create/switch to the task branch.

    :param repo_dir: Target repo directory.
    :param task_id: Task identifier for branch naming.
    :param default_branch: Default branch name.
    :param verbose: If True, print progress.
    :returns: True on success, False if dirty tree or not a git repo.
    """
    if not ensure_integration_branch(repo_dir, default_branch, verbose=verbose):
        return False

    branch = _task_branch_name(task_id)
    try:
        existing = _git(repo_dir, "branch", "--list", branch, check=False).stdout.strip()
        if existing:
            _git(repo_dir, "checkout", branch)
        else:
            _git(repo_dir, "checkout", "-b", branch)
        if verbose:
            print(f"  [git] on {branch} (from {INTEGRATION_BRANCH})")
        return True
    except subprocess.CalledProcessError as exc:
        print(f"  [git] failed to create task branch {branch}: {exc.stderr or exc.stdout}")
        return False


def _capture_diff(repo_dir: Path, default_branch: str) -> tuple[str, list[str]]:
    """Return (unified diff vs default_branch, list of changed file paths).

    :param repo_dir: Target repo directory.
    :param default_branch: Default branch name for diff base.
    :returns: Tuple of (diff_text, file_paths).
    """
    diff = _git(repo_dir, "diff", f"{default_branch}...HEAD").stdout
    names = _git(repo_dir, "diff", "--name-only", f"{default_branch}...HEAD").stdout.strip()
    files = [f for f in names.splitlines() if f.strip()]
    return diff, files


def _invalidate_reports(repo_dir: Path,
                        subdirs: tuple[str, ...] = ("test-infra", "coverage")) -> None:
    """Delete the given stages' latest.md so the pipeline re-measures them.

    :param repo_dir: Target repo directory.
    :param subdirs: Report subdirectories to invalidate.
    """
    for sub in subdirs:
        latest = repo_dir / SWE_SUBDIR / "reports" / sub / "latest.md"
        try:
            if latest.exists() or latest.is_symlink():
                latest.unlink()
        except OSError:
            pass


# ─── Prompt builders ─────────────────────────────────────────────────────────


def _closing_loop_instructions(repo_dir: Path, task_dir: Path,
                               commit_subject: str) -> str:
    """Shared 'commit + write completion marker' instructions for code fixes.

    :param repo_dir: Target repo directory.
    :param task_dir: Task directory path.
    :param commit_subject: Git commit subject.
    :returns: Instruction text string.
    """
    marker_path = task_dir / CODING_COMPLETE_MARKER
    return (
        f"## Closing the loop (do this LAST, after verification passes)\n\n"
        f"1. Commit your work on the task branch. Run exactly:\n"
        f"     cd {repo_dir} && git add -A && "
        f"git -c user.name='{GIT_AUTHOR_NAME}' "
        f"-c user.email='{GIT_AUTHOR_EMAIL}' "
        f"commit -m \"{commit_subject}\"\n"
        f"2. Write a completion marker so the SWE pipeline runs its verification "
        f"gate on the next tick. Write a short summary of what you changed and "
        f"whether verification passed into this file:\n"
        f"     {marker_path}\n"
        f"   If you concluded the issue is NOT fixable in this repo, still write "
        f"the marker but begin it with the line 'UNFIXABLE:' followed by your "
        f"reasoning, and do NOT commit.\n"
    )


def _build_coder_prompt(repo_dir: Path, repo_name: str, task_dir: Path,
                        branch: str, issue: Issue, test_cmd: str,
                        dep_audit_cmd: str, coverage_cmd: str = "") -> str:
    """Prompt for a dependency-vulnerability (security) fix.

    :param repo_dir: Target repo directory.
    :param repo_name: Repo name.
    :param task_dir: Task directory path.
    :param branch: Task branch name.
    :param issue: Issue to fix.
    :param test_cmd: Test command string.
    :param dep_audit_cmd: Dependency audit command string.
    :param coverage_cmd: Coverage command string.
    :returns: Prompt text string.
    """
    subject = f"fix: {issue.id}"
    coverage_block = ""
    if coverage_cmd:
        coverage_block = (
            f"## Before you start: record baseline coverage\n\n"
            f"Run the coverage command to know the starting point:\n\n"
            f"     cd {repo_dir} && {coverage_cmd}\n\n"
            f"Your fix MUST NOT reduce coverage. If your changes introduce new "
            f"uncovered lines, you MUST write tests to cover them. The final "
            f"coverage must be at least {COVERAGE_TARGET:.0f}%.\n\n"
        )

    verif_lines: list[str] = []
    verif_lines.append(
        f"1. The test suite MUST stay green:\n"
        f"     cd {repo_dir} && {test_cmd}"
    )
    if coverage_cmd:
        verif_lines.append(
            f"{len(verif_lines) + 1}. Coverage MUST stay at or above "
            f"{COVERAGE_TARGET:.0f}% (write tests for any new code you add):\n"
            f"     cd {repo_dir} && {coverage_cmd}"
        )

    return (
        f"You are fixing a single tracked issue in the locally cloned repo at:\n"
        f"  {repo_dir}\n\n"
        f"The repo has already been checked out on a dedicated task branch "
        f"'{branch}' (branched off the integration branch "
        f"'{INTEGRATION_BRANCH}'). Make ALL your changes on this branch. Do NOT "
        f"switch branches.\n\n"
        f"{coverage_block}"
        f"## First: plan with the Progress tool\n\n"
        f"Before you start making changes, use the **Progress** tool to record "
        f"your task list. Keep it updated as you go.\n\n"
        f"Use the OpenCode tool to do the actual editing. The repo_name to pass "
        f"to OpenCode is '{repo_name}'. Decide the best fix approach yourself — "
        f"the only hard requirement is that the verification below passes.\n\n"
        f"## The issue\n\n"
        f"{issue.body.strip()}\n\n"
        f"## Verification (you MUST run these and they MUST pass)\n\n"
        f"IMPORTANT: `cd` into the repo first for every command.\n"
        + "\n".join(verif_lines) + "\n\n"
        + _closing_loop_instructions(repo_dir, task_dir, subject)
    )


def _build_coverage_prompt(repo_dir: Path, repo_name: str, task_dir: Path,
                           branch: str, issue: Issue, test_cmd: str,
                           coverage_cmd: str) -> str:
    """Prompt for a test-coverage fix (write tests to reach the target).

    :param repo_dir: Target repo directory.
    :param repo_name: Repo name.
    :param task_dir: Task directory path.
    :param branch: Task branch name.
    :param issue: Coverage issue to fix.
    :param test_cmd: Test command string.
    :param coverage_cmd: Coverage command string.
    :returns: Prompt text string.
    """
    subject = f"test: {issue.id}"
    return (
        f"You are increasing test coverage for the locally cloned repo at:\n"
        f"  {repo_dir}\n\n"
        f"The repo has already been checked out on a dedicated task branch "
        f"'{branch}' (branched off the integration branch "
        f"'{INTEGRATION_BRANCH}'). Make ALL your changes on this branch. Do NOT "
        f"switch branches.\n\n"
        f"## Step 1: measure current coverage FIRST\n\n"
        f"The coverage gaps listed in the issue below may be stale. Before you "
        f"write any tests, run the coverage command to see the REAL gaps:\n\n"
        f"     cd {repo_dir} && {coverage_cmd}\n\n"
        f"Use the output from THAT run as your target.\n\n"
        f"## First: plan with the Progress tool\n\n"
        f"After measuring current coverage, use the **Progress** tool to record "
        f"your task list. You may need to loop: write tests, re-measure, repeat.\n\n"
        f"Use the OpenCode tool to do the actual editing (repo_name="
        f"'{repo_name}'). Add tests for the uncovered code described below, "
        f"following the existing test style/conventions. Do NOT change "
        f"production code except where strictly necessary for testability.\n\n"
        f"## The issue\n\n"
        f"{issue.body.strip()}\n\n"
        f"## Verification (you MUST run these and they MUST pass)\n\n"
        f"IMPORTANT: `cd` into the repo first for every command.\n"
        f"1. Coverage MUST reach {COVERAGE_TARGET:.0f}%:\n"
        f"     cd {repo_dir} && {coverage_cmd}\n"
        f"2. The test suite MUST stay green:\n"
        f"     cd {repo_dir} && {test_cmd}\n\n"
        + _closing_loop_instructions(repo_dir, task_dir, subject)
    )


def _build_review_prompt(repo_dir: Path, repo_name: str, task_dir: Path,
                         issue: Issue) -> str:
    """Prompt for a full-codebase review that FILES new issues (no code change).

    :param repo_dir: Target repo directory.
    :param repo_name: Repo name.
    :param task_dir: Task directory path.
    :param issue: Review issue.
    :returns: Prompt text string.
    """
    marker_path = task_dir / CODING_COMPLETE_MARKER
    return (
        f"You are performing a CODE REVIEW of the locally cloned repo at:\n"
        f"  {repo_dir}\n\n"
        f"The repo is checked out on the integration branch "
        f"'{INTEGRATION_BRANCH}'. Do NOT modify any source code and do NOT "
        f"commit anything. Your job is to review the codebase and record "
        f"concrete, actionable improvements as NEW issues.\n\n"
        f"## The task\n\n"
        f"{issue.body.strip()}\n\n"
        f"## How to file issues\n\n"
        f"For every finding, use the SWE issue CLI:\n"
        f"  cd {repo_dir} && python3 -m cronpypeline.plugins.issue_store file {repo_name} "
        f"--type <bug|enhancement|refactor> --title \"<title>\" --body-file /tmp/swe_finding.md\n\n"
        f"## Closing the loop (do this LAST)\n\n"
        f"Write a completion marker summarizing your review into:\n"
        f"     {marker_path}\n"
        f"Do NOT commit and do NOT modify source files.\n"
    )


# ─── Subprocess + queue helpers ──────────────────────────────────────────────


def _run(cmd: str, cwd: Path, timeout: int) -> tuple[int, str, str]:
    """Run a shell command and return (exit_code, stdout, stderr).

    :param cmd: Shell command string.
    :param cwd: Working directory.
    :param timeout: Timeout in seconds.
    :returns: Tuple of (exit_code, stdout, stderr).
    """
    try:
        proc = subprocess.run(  # nosec B602 - commands from trusted pipeline config
            cmd, shell=True, cwd=str(cwd),
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        err = (exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")) \
            + f"\n[TIMEOUT after {timeout}s]"
        return 124, out, err


def _queue_agent(agent_name: str, prompt: str, repo_name: str,
                 repo_dir: Path, task_dir: Path, task_id: str,
                 source_issue_id: str, stage: str,
                 context: TickContext) -> bool:
    """Queue an agent via ConversationQueueHandler.

    :param agent_name: Agent name (e.g. 'CoderAgent').
    :param prompt: Prompt text.
    :param repo_name: Repo name.
    :param repo_dir: Repo directory path.
    :param task_dir: Task directory path.
    :param task_id: Task identifier.
    :param source_issue_id: Source issue id.
    :param stage: Pipeline stage label.
    :param context: Tick context for queue handler.
    :returns: True if queued successfully.
    """
    from cronpypeline.plugins.swe_prompts import _build_queue_handler

    handler = _build_queue_handler({}, context)

    queue_action = ActionSpec(
        type=ActionType.CUSTOM,
        params={
            "agent": agent_name,
            "prompt": prompt,
            "repo_name": repo_name,
            "repo_dir": str(repo_dir),
            "stage": stage,
            "task_id": task_id,
            "task_dir": str(task_dir),
            "source_issue_id": source_issue_id,
        },
    )
    result = handler.execute(queue_action, context)
    return result.success


# ─── SELECT stage ────────────────────────────────────────────────────────────


def run_select(repo_dir: Path, repo_name: str, target_config: dict[str, Any],
               context: TickContext, issue_id: str | None = None,
               dry_run: bool = False, verbose: bool = False) -> bool:
    """SELECT stage: pick an open issue, create task dir, branch, and queue agent.

    :param repo_dir: Target repo directory.
    :param repo_name: Repo name.
    :param target_config: Per-target config dict from repos.json.
    :param context: Tick context for queue handler.
    :param issue_id: Optional specific issue id to select.
    :param dry_run: If True, don't mutate anything.
    :param verbose: If True, print progress.
    :returns: True on success.
    """
    briefing = repo_dir / SWE_SUBDIR / "repo_briefing.md"
    if not briefing.exists():
        print(f"  no repo briefing ({briefing}); run Phase A0 first")
        return False

    issue = select_open_issue(repo_dir, issue_id, verbose=verbose)
    if issue is None:
        print(f"  {repo_name}: no open issue to fix.")
        return False

    issue_type = (issue.type or "bug").strip().lower()
    is_review = issue_type == REVIEW_TYPE
    task_id = _safe_slug(str(issue.id))
    default_branch = target_config.get("default_branch", "main")
    test_cmd = (target_config.get("test_cmd") or "").strip() or ".venv/bin/pytest -q"
    dep_audit_cmd = (target_config.get("dep_audit_cmd") or "").strip() or ".venv/bin/pip-audit"
    coverage_cmd = (target_config.get("coverage_cmd") or "").strip() or (
        ".venv/bin/pytest --cov=. --cov-report=term-missing -q"
    )

    date_bucket = datetime.now(timezone.utc).strftime("%Y%m%d")
    task_dir = (TASKS_DIR / date_bucket / f"{date_bucket}_{_safe_slug(repo_name)}_{task_id}")
    branch = _task_branch_name(task_id)
    agent_name = "CodeReviewAgent" if is_review else "CoderAgent"

    print(f"  {repo_name}: SELECT {issue_type} issue '{task_id}'")
    if dry_run:
        print(f"  [DRY-RUN] would create task dir {task_dir}")
        print(f"  [DRY-RUN] would queue {agent_name} + set issue -> triaged")
        return True

    task_dir.mkdir(parents=True, exist_ok=True)

    for stale_artifact in (GATE_RESULT_FILE, CODING_COMPLETE_MARKER, DIFF_FILE, FILES_CHANGED_FILE):
        try:
            (task_dir / stale_artifact).unlink(missing_ok=True)
        except OSError:
            pass

    if is_review:
        if not ensure_integration_branch(repo_dir, default_branch, verbose=verbose):
            return False
        branch = INTEGRATION_BRANCH
        prompt = _build_review_prompt(repo_dir, repo_name, task_dir, issue)
        stage = "C3"
    else:
        if not _ensure_task_branch(repo_dir, task_id, default_branch, verbose=verbose):
            return False
        if issue_type == "coverage":
            prompt = _build_coverage_prompt(
                repo_dir, repo_name, task_dir, branch, issue, test_cmd, coverage_cmd)
        else:
            prompt = _build_coder_prompt(
                repo_dir, repo_name, task_dir, branch, issue,
                test_cmd, dep_audit_cmd, coverage_cmd)
        stage = "C2"

    task = {
        "task_id": task_id,
        "repo": target_config.get("slug", repo_name),
        "repo_name": repo_name,
        "default_branch": default_branch,
        "branch": branch,
        "issue_type": issue_type,
        "kind": issue.type or "bug",
        "title": str(issue.id),
        "source_issue": str(repo_dir / SWE_SUBDIR / "issues" / f"{issue.id}.md"),
        "source_issue_id": str(issue.id),
        "test_cmd": test_cmd,
        "dep_audit_cmd": dep_audit_cmd,
        "coverage_cmd": coverage_cmd,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (task_dir / TASK_FILE).write_text(json.dumps(task, indent=2), encoding="utf-8")

    queued = _queue_agent(
        agent_name, prompt, repo_name, repo_dir, task_dir, task_id,
        str(issue.id), stage, context,
    )
    if not queued:
        return False

    set_issue_status(repo_dir, issue.id, "triaged")
    print(f"  {repo_name}: queued {agent_name} for task '{task_id}'.")
    return True


# ─── GATE stage ──────────────────────────────────────────────────────────────


def _gate_review(repo_dir: Path, task_dir: Path, task: dict[str, Any],
                 verbose: bool = False) -> bool:
    """Finalize a review task: mark the review issue done.

    :param repo_dir: Target repo directory.
    :param task_dir: Task directory path.
    :param task: Task dict.
    :param verbose: If True, print progress.
    :returns: True on success.
    """
    source_issue_id = task.get("source_issue_id", "")
    new_open = sum(
        1 for i in load_issues(repo_dir)
        if i.status == "open" and i.source == "review"
    )
    gate = {
        "task_id": task["task_id"],
        "gated_at": datetime.now(timezone.utc).isoformat(),
        "issue_type": "review",
        "new_issues_open": new_open,
        "passed": True,
    }
    (task_dir / GATE_RESULT_FILE).write_text(json.dumps(gate, indent=2), encoding="utf-8")
    if source_issue_id:
        set_issue_status(repo_dir, source_issue_id, "done")
    print(f"  review complete ({new_open} new issue(s) filed).")
    return True


def run_gate(repo_dir: Path, task_dir: Path,
             dry_run: bool = False, verbose: bool = False) -> bool:
    """GATE stage: re-run verification tools, capture diff, finalize issue.

    :param repo_dir: Target repo directory.
    :param task_dir: Task directory path.
    :param dry_run: If True, don't mutate anything.
    :param verbose: If True, print progress.
    :returns: True on success (passed or resolved out-of-tree).
    """
    task = _read_task(task_dir)
    issue_type = (task.get("issue_type") or "security").lower()
    source_issue_id = task.get("source_issue_id", "")
    marker_path = task_dir / CODING_COMPLETE_MARKER
    marker_text = marker_path.read_text(encoding="utf-8") if marker_path.exists() else ""

    if marker_text.lstrip().upper().startswith("UNFIXABLE"):
        print(f"  agent reported task '{task['task_id']}' UNFIXABLE.")
        if not dry_run:
            (task_dir / GATE_RESULT_FILE).write_text(
                json.dumps({"task_id": task["task_id"], "passed": False,
                            "unfixable": True}, indent=2), encoding="utf-8")
            if source_issue_id:
                set_issue_status(repo_dir, source_issue_id, "discarded")
        return True

    if issue_type == REVIEW_TYPE:
        print(f"  GATE review task '{task['task_id']}'")
        if dry_run:
            return True
        return _gate_review(repo_dir, task_dir, task, verbose)

    test_cmd = task.get("test_cmd", ".venv/bin/pytest -q")
    coverage_cmd = task.get("coverage_cmd", "")
    branch = task.get("branch", _task_branch_name(task["task_id"]))

    print(f"  GATE {issue_type} task '{task['task_id']}'")
    if dry_run:
        return True

    # Measure baseline coverage on integration branch for non-coverage issues
    baseline_pct: float | None = None
    if issue_type != "coverage" and coverage_cmd:
        _git(repo_dir, "checkout", INTEGRATION_BRANCH, check=False)
        _, base_out, base_err = _run(coverage_cmd, repo_dir, timeout=900)
        base_counts = _parse_coverage_output(base_out + "\n" + base_err)
        baseline_pct = base_counts.get("coverage_pct", 0.0)

    # Verify on the task branch
    _git(repo_dir, "checkout", branch, check=False)

    type_detail: dict[str, Any] = {}
    if issue_type == "coverage":
        cov_code, cov_out, cov_err = _run(coverage_cmd, repo_dir, timeout=900)
        counts = _parse_coverage_output(cov_out + "\n" + cov_err)
        cov_pct = counts.get("coverage_pct", 0.0)
        type_ok = cov_pct >= COVERAGE_TARGET
        tests_green = cov_code == 0
        type_detail = {"coverage_pct": cov_pct, "coverage_target": COVERAGE_TARGET}
    else:  # generic bug/enhancement
        if coverage_cmd:
            cov_code, cov_out, cov_err = _run(coverage_cmd, repo_dir, timeout=900)
            counts = _parse_coverage_output(cov_out + "\n" + cov_err)
            cov_pct = counts.get("coverage_pct", 0.0)
            type_ok = (cov_pct >= baseline_pct
                       if baseline_pct is not None
                       else cov_pct >= COVERAGE_TARGET)
            tests_green = cov_code == 0
            type_detail = {"coverage_pct": cov_pct, "coverage_target": COVERAGE_TARGET}
            if baseline_pct is not None:
                type_detail["baseline_pct"] = baseline_pct
        else:
            test_code, _test_out, _test_err = _run(test_cmd, repo_dir, timeout=900)
            tests_green = test_code == 0
            type_ok = True

    diff, files = _capture_diff(repo_dir, INTEGRATION_BRANCH)
    (task_dir / DIFF_FILE).write_text(diff, encoding="utf-8")
    (task_dir / FILES_CHANGED_FILE).write_text(json.dumps(files, indent=2), encoding="utf-8")

    has_diff = bool(diff.strip())
    resolved_out_of_tree = type_ok and tests_green and not has_diff
    passed = type_ok and tests_green and has_diff

    merged = False
    if passed:
        merged = merge_into_integration(repo_dir, branch, verbose=verbose)
        passed = merged
        if merged:
            _invalidate_reports(repo_dir)

    gate = {
        "task_id": task["task_id"],
        "gated_at": datetime.now(timezone.utc).isoformat(),
        "issue_type": issue_type,
        "tests_green": tests_green,
        "type_ok": type_ok,
        "merged": merged,
        "files_changed": files,
        "diff_lines": len(diff.splitlines()),
        "passed": passed,
        "resolved_out_of_tree": resolved_out_of_tree,
        **type_detail,
    }
    (task_dir / GATE_RESULT_FILE).write_text(json.dumps(gate, indent=2), encoding="utf-8")

    print(f"    type_ok={type_ok}  tests_green={tests_green}  merged={merged}  "
          f"files_changed={len(files)}")

    if source_issue_id:
        for i in load_issues(repo_dir):
            if str(i.id) == source_issue_id:
                if resolved_out_of_tree:
                    set_issue_status(repo_dir, source_issue_id, "discarded")
                else:
                    _finalize_issue_outcome(i, repo_dir, passed, verbose=verbose)
                break

    return passed or resolved_out_of_tree


# ─── Main entry point (called by run_c_issue_fix in swe_plugin.py) ───────────


def run_issue_fix_state_machine(repo_dir: Path, repo_name: str,
                                 target_config: dict[str, Any],
                                 context: TickContext,
                                 dry_run: bool = False,
                                 verbose: bool = False) -> bool:
    """Auto-detect SELECT vs GATE from on-disk state and execute.

    :param repo_dir: Target repo directory.
    :param repo_name: Repo name.
    :param target_config: Per-target config dict.
    :param context: Tick context for queue handler.
    :param dry_run: If True, don't mutate anything.
    :param verbose: If True, print progress.
    :returns: True on success.
    """
    active = _find_active_task(repo_name)

    if active is not None:
        marker_ready = (active / CODING_COMPLETE_MARKER).exists()

        if marker_ready:
            return run_gate(repo_dir, active, dry_run=dry_run, verbose=verbose)

        if _is_task_stale(active):
            if not dry_run:
                _cleanup_stale_task(repo_dir, active, verbose=verbose)
                return run_select(repo_dir, repo_name, target_config, context,
                                  dry_run=dry_run, verbose=verbose)
            return True

        # Check if agent finished but forgot marker (queue empty + has commits)
        task = _read_task(active)
        task_age = (datetime.now(timezone.utc) -
                    datetime.fromisoformat(task.get("created_at", ""))).total_seconds() / 60
        if task_age >= 2 and task.get("issue_type") != "review":
            branch = task.get("branch", "")
            has_commits = False
            if branch:
                try:
                    result = _git(repo_dir, "log", "--oneline",
                                  f"{INTEGRATION_BRANCH}..{branch}", check=False)
                    has_commits = bool(result.stdout.strip())
                except OSError:
                    pass
            if has_commits:
                if not dry_run:
                    if verbose:
                        print(f"  {repo_name}: agent queue empty + commits on {branch} — gating")
                    return run_gate(repo_dir, active, dry_run=False, verbose=verbose)
                return True

        if not dry_run:
            print(f"  {repo_name}: task waiting on agent (no {CODING_COMPLETE_MARKER} yet).")
        return True

    # No active task — recover orphaned triaged issues, then select
    _recover_orphaned_triaged(repo_dir, repo_name, verbose=verbose)
    _cleanup_orphaned_task_dirs(repo_name, verbose=verbose)

    return run_select(repo_dir, repo_name, target_config, context,
                      dry_run=dry_run, verbose=verbose)
