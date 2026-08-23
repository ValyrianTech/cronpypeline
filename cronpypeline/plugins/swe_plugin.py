"""SWE pipeline custom triggers and actions plugin.

Provides custom trigger callables and action handlers specific to the SWE pipeline,
referenced from pipeline JSON configs via "callable": "cronpypeline.plugins.swe_plugin.xxx".
"""

import json
import os
import re
import shutil
import subprocess  # nosec B404 - subprocess is used by design to run git commands for pipeline state detection
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from cronpypeline.actions import ActionResult, ActionSpec, TickContext
from cronpypeline.plugins.issue_store import (
    create_issue,
    load_issues,
    parse_frontmatter,
    set_issue_status,
)
from cronpypeline.reporting import update_latest_symlink, write_report

PHASE_A_BRANCH = "swe-pipeline/phase-a-hygiene"
PHASE_A_GIT_AUTHOR_NAME = "Valyrian SWE Pipeline"
PHASE_A_GIT_AUTHOR_EMAIL = "swe-pipeline@valyrian.tech"

GITHUB_RECHECK_SECONDS = 10 * 60
SWE_SUBDIR = ".SWE"
GITHUB_SESSION_FILE = f"{SWE_SUBDIR}/github_session.json"

SWE_WORKSPACE_DIR = Path("/spellbook_data/Serendipity/swe/workspace")
TASKS_DIR = SWE_WORKSPACE_DIR / "tasks"
RANKING_SCRIPT = "/home/wouter/Repos/spellbook/apps/Serendipity/SWE/scripts/run_swe_issue_ranking.py"
ISSUE_FIX_SCRIPT = "/home/wouter/Repos/spellbook/apps/Serendipity/SWE/scripts/run_issue_fix.py"


def detect_open_issue(context: dict[str, Any]) -> bool:
    """Trigger: detect if there's an open issue to work on.

    Scans ``.SWE/issues/*.md`` files with YAML frontmatter for ``status: open``.

    :param context: Trigger context dict with ``target_dir``.
    :returns: True if at least one open issue exists.
    """
    target_dir = Path(context.get("target_dir", "."))
    issues = load_issues(target_dir)
    return any(issue.status == "open" for issue in issues)


def detect_deadcode_trigger(context: dict[str, Any]) -> bool:
    """Trigger: fire if deadcode report is missing AND skip_deadcode is not set.

    :param context: Trigger context dict with ``target_dir`` and ``target_config``.
    :returns: True if the deadcode report is missing and skip_deadcode is not true.
    """
    target_config = context.get("target_config", {})
    if target_config.get("skip_deadcode"):
        return False
    target_dir = Path(context.get("target_dir", "."))
    return not (target_dir / ".SWE" / "reports" / "deadcode" / "latest.md").exists()


def _resolve_latest_report(target_dir: Path, subdir: str) -> Path | None:
    """Resolve the latest.md symlink in a report subdir to the actual report file.

    :param target_dir: Target repo directory.
    :param subdir: Report subdirectory name under ``.SWE/reports/``.
    :returns: Resolved report Path, or None if not found.
    """
    latest = target_dir / ".SWE" / "reports" / subdir / "latest.md"
    if not (latest.exists() or latest.is_symlink()):
        return None
    report_path = latest.resolve() if latest.is_symlink() else latest
    if not report_path.exists():
        return None
    return report_path


def detect_lint_fail(context: dict[str, Any]) -> bool:
    """Trigger: fire when lint report shows errors with no auto-fixable remaining.

    Mirrors the old pipeline's ``detect_a2_fix_agent`` logic:
    - Lint report must exist (latest.md in .SWE/reports/lint/)
    - Error count > 0
    - Auto-fixable count == 0 (let autofix run first)
    - No existing ``queued_for_{stem}.marker`` in .SWE/markers/

    :param context: Trigger context dict with ``target_dir``.
    :returns: True if a lint fix agent should be queued.
    """
    target_dir = Path(context.get("target_dir", "."))
    report_path = _resolve_latest_report(target_dir, "lint")
    if report_path is None:
        return False
    try:
        text = report_path.read_text(encoding="utf-8")
    except OSError:
        return False
    m_errors = re.search(r'\*\*errors\*\*:\s*(\d+)', text)
    if not m_errors:
        m_errors = re.search(r'(\d+)\s+error\(s\)', text)
    errors = int(m_errors.group(1)) if m_errors else 0
    m_fixable = re.search(r'\*\*fixable\*\*:\s*(\d+)', text)
    if not m_fixable:
        m_fixable = re.search(r'(\d+)\s+auto-fixable', text)
    fixable = int(m_fixable.group(1)) if m_fixable else 0
    if errors <= 0 or fixable > 0:
        return False
    marker = target_dir / ".SWE" / "markers" / f"queued_for_{report_path.stem}.marker"
    return not marker.exists()


def _detect_report_fail(target_dir: Path, report_subdir: str) -> bool:
    """Check if a report shows FAIL and no dedup marker exists.

    :param target_dir: Target repo directory.
    :param report_subdir: Report subdirectory name under .SWE/reports/.
    :returns: True if report first line contains '— FAIL' and no dedup marker.
    """
    report_path = _resolve_latest_report(target_dir, report_subdir)
    if report_path is None:
        return False
    try:
        first_line = report_path.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return False
    if "— FAIL" not in first_line:
        return False
    marker = target_dir / ".SWE" / "markers" / f"queued_for_{report_path.stem}.marker"
    return not marker.exists()


def detect_docstring_fail(context: dict[str, Any]) -> bool:
    """Trigger: fire when docstring report shows FAIL.

    :param context: Trigger context dict with ``target_dir``.
    :returns: True if a docstring fix agent should be queued.
    """
    target_dir = Path(context.get("target_dir", "."))
    return _detect_report_fail(target_dir, "docstrings")


def detect_typecheck_fail(context: dict[str, Any]) -> bool:
    """Trigger: fire when typecheck report shows FAIL.

    :param context: Trigger context dict with ``target_dir``.
    :returns: True if a typecheck fix agent should be queued.
    """
    target_dir = Path(context.get("target_dir", "."))
    return _detect_report_fail(target_dir, "typecheck")


def detect_security_fail(context: dict[str, Any]) -> bool:
    """Trigger: fire when security report shows FAIL.

    :param context: Trigger context dict with ``target_dir``.
    :returns: True if a security fix agent should be queued.
    """
    target_dir = Path(context.get("target_dir", "."))
    return _detect_report_fail(target_dir, "security")


def detect_coverage_fail(context: dict[str, Any]) -> bool:
    """Trigger: fire when coverage report shows FAIL (below threshold).

    :param context: Trigger context dict with ``target_dir``.
    :returns: True if a coverage fix agent should be queued.
    """
    target_dir = Path(context.get("target_dir", "."))
    return _detect_report_fail(target_dir, "coverage")


def detect_vulture_fail(context: dict[str, Any]) -> bool:
    """Trigger: fire when deadcode report shows FAIL.

    Mirrors the old pipeline's ``detect_a6_fix_agent`` logic:
    - Deadcode report must exist (latest.md in .SWE/reports/deadcode/)
    - First line contains "— FAIL"
    - No existing ``queued_for_{stem}.marker`` in .SWE/markers/

    :param context: Trigger context dict with ``target_dir``.
    :returns: True if a deadcode fix agent should be queued.
    """
    target_dir = Path(context.get("target_dir", "."))
    report_path = _resolve_latest_report(target_dir, "deadcode")
    if report_path is None:
        return False
    try:
        first_line = report_path.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return False
    if "— FAIL" not in first_line:
        return False
    marker = target_dir / ".SWE" / "markers" / f"queued_for_{report_path.stem}.marker"
    return not marker.exists()


def detect_session_complete(context: dict[str, Any]) -> bool:
    """Trigger: fire when a GitHub session's issue is discarded without a PR.

    Mirrors the old pipeline's ``detect_c_github_session_terminal`` logic:
    - GitHub session is active
    - No PR was ever published (pr_published.json doesn't exist)
    - The session's issue has status 'discarded'

    :param context: Trigger context dict with ``target_dir``.
    :returns: True if the session should be finalized.
    """
    target_dir = Path(context.get("target_dir", "."))
    session_file = target_dir / ".SWE" / "github_session.json"
    if not session_file.exists():
        return False
    try:
        session = json.loads(session_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not session.get("active"):
        return False
    pr_marker = target_dir / ".SWE" / "pr_published.json"
    if pr_marker.exists():
        return False
    issue_id = session.get("issue_id", "")
    if not issue_id:
        return False
    issue_path = target_dir / ".SWE" / "issues" / f"{issue_id}.md"
    if not issue_path.exists():
        return False
    try:
        fm, _ = parse_frontmatter(issue_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return fm.get("status") == "discarded"


def select_issue(action: ActionSpec, context: TickContext) -> tuple[bool, str]:
    """Action: select the first open issue and mark it as triaged.

    Mirrors the old pipeline's ``select_open_issue`` + ``run_select`` logic:
    - Load all issues from .SWE/issues/
    - Filter for status='open'
    - Sort by hivemind_score (ranked first), then rank, then filename
    - Set selected issue status to 'triaged'

    :param action: Action spec (no special params needed).
    :param context: Tick context with target directory.
    :returns: Tuple of (success, message).
    """
    target_dir = context.target_dir
    issues = load_issues(target_dir)
    open_issues = [i for i in issues if i.status == "open" and i.id is not None]
    if not open_issues:
        return False, "No open issues to select"
    open_issues.sort(key=lambda i: (
        0 if i.hivemind_score is not None else 1,
        i.rank or 0 if i.hivemind_score is not None else 0,
        str(i.id),
    ))
    selected = open_issues[0]
    set_issue_status(target_dir, selected.id, "triaged")
    return True, f"Selected issue {selected.id}"


def finalize_session(action: ActionSpec, context: TickContext) -> ActionResult:
    """Action: finalize a completed GitHub session.

    Mirrors the original ``detect_c_github_session_terminal`` execute logic:
    - Post a comment on the GitHub issue explaining the discard
    - Close the GitHub issue via API
    - Mark the session as inactive and completed

    :param action: Action spec (no special params needed).
    :param context: Tick context with target directory and config.
    :returns: ActionResult indicating success.
    """
    if context.dry_run:
        return ActionResult(success=True, dry_run=True)

    target_dir = context.target_dir
    target_config = context.target_config
    session_file = target_dir / ".SWE" / "github_session.json"
    if not session_file.exists():
        return ActionResult(success=False, stderr="No session file found")
    try:
        session = json.loads(session_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ActionResult(success=False, stderr="Failed to read session file")

    issue_id = session.get("issue_id", "")
    issue_path = target_dir / ".SWE" / "issues" / f"{issue_id}.md"
    gh_number = 0
    if issue_path.exists():
        try:
            fm, _ = parse_frontmatter(issue_path.read_text(encoding="utf-8"))
            gh_number = int(fm.get("github_number", 0))
        except (OSError, ValueError):
            pass

    token = _load_github_token(target_config)
    slug = (target_config.get("slug") or "").strip()

    if gh_number and token and "/" in slug:
        owner, gh_repo_name = slug.split("/", 1)
        msg = (
            "The SWE pipeline reviewed this issue and determined no code "
            "changes are needed — the issue appears to already be addressed "
            "or is not actionable. Closing."
        )
        _gh_api_post(
            owner, gh_repo_name, f"issues/{gh_number}/comments",
            {"body": msg}, token, expected_statuses=(200, 201),
        )
        _gh_api_patch(
            owner, gh_repo_name, f"issues/{gh_number}",
            {"state": "closed"}, token,
        )

    session["active"] = False
    session["completed"] = True
    session["completed_at"] = datetime.now(timezone.utc).isoformat()
    session_file.write_text(json.dumps(session, indent=2), encoding="utf-8")
    return ActionResult(success=True, data={"gh_number": gh_number})


def detect_agent_forgot_marker(context: dict[str, Any]) -> bool:
    """Trigger: detect if agent forgot to write completion marker.

    Fires when: queue is empty + git commits exist on branch but no completion marker.

    :param context: Trigger context dict with ``target_dir`` and optional ``queue_dir``.
    :returns: True if the agent likely forgot to write the completion marker.
    """
    target_dir = Path(context.get("target_dir", "."))

    # Check if coding_complete.marker is missing
    if (target_dir / "coding_complete.marker").exists():
        return False

    # Check if task.json exists (active task)
    task_file = target_dir / "task.json"
    if not task_file.exists():
        return False

    # Check if there are git commits on the current branch
    git_bin = shutil.which("git")
    if git_bin is None:
        return False
    try:
        result = subprocess.run(  # nosec B603 - git_bin is resolved to an absolute path via shutil.which; args are a static list
            [git_bin, "log", "--oneline", "-1"],
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False

    # Check if queue is empty
    queue_dir = context.get("queue_dir")
    if queue_dir:
        queue_path = Path(queue_dir)
        if queue_path.exists() and any(queue_path.iterdir()):
            return False

    return True


def cleanup_git_branch(action: ActionSpec, context: TickContext) -> tuple[bool, str]:
    """Action: clean up git branch after failure.

    Runs ``git checkout integration && git branch -D {task_branch}``.

    :param action: Action spec with optional ``task_branch`` param.
    :param context: Tick context with target directory.
    :returns: Tuple of (success, message).
    """
    target_dir = context.target_dir
    task_branch = action.params.get("task_branch", "task-branch")

    commands = [
        ["git", "checkout", "integration"],
        ["git", "branch", "-D", task_branch],
    ]

    for cmd in commands:
        try:
            subprocess.run(cmd, cwd=str(target_dir), capture_output=True, timeout=30, check=False)  # nosec B603 - commands are static lists passed without a shell
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    return True, f"Cleaned up branch {task_branch}"


def reset_issue_status(action: ActionSpec, context: TickContext) -> tuple[bool, str]:
    """Action: reset issue status to 'open' after failure.

    Updates the issue's frontmatter status field back to 'open'.

    :param action: Action spec with ``issue_id`` param.
    :param context: Tick context with target directory.
    :returns: Tuple of (success, message).
    """
    target_dir = context.target_dir
    issue_id = action.params.get("issue_id")

    result = set_issue_status(target_dir, issue_id, "open")
    if result:
        return True, f"Reset issue {issue_id} to open"
    return False, f"Issue {issue_id} not found"


def _git(repo_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a git command in the given repo directory.

    :param repo_dir: Target repo directory.
    :param args: Git command arguments.
    :param check: If True, raise on non-zero exit.
    :returns: CompletedProcess result.
    """
    return subprocess.run(  # pragma: no cover - coverage.py artifact with multi-line return
        ["git", "-C", str(repo_dir), *args],
        capture_output=True, text=True, check=check,
    )  # nosec B603 - args are controlled by the plugin


def ensure_phase_a_branch(repo_dir: Path, verbose: bool = False) -> bool:
    """Ensure repo is on PHASE_A_BRANCH and .SWE/ is gitignored.

    Creates the branch from current HEAD if missing. Idempotent.

    :param repo_dir: Target repo directory.
    :param verbose: If True, print progress.
    :returns: True on success, False if not a git repo.
    """
    try:
        _git(repo_dir, "rev-parse", "--git-dir")
    except subprocess.CalledProcessError:
        return False

    try:
        cur = _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if cur != PHASE_A_BRANCH:
            existing = _git(
                repo_dir, "branch", "--list", PHASE_A_BRANCH, check=False,
            ).stdout.strip()
            if existing:
                _git(repo_dir, "checkout", PHASE_A_BRANCH)
            else:
                _git(repo_dir, "checkout", "-b", PHASE_A_BRANCH)
    except subprocess.CalledProcessError:
        return False

    gitignore = repo_dir / ".gitignore"
    needs_add = True
    existing_content = ""
    if gitignore.exists():
        existing_content = gitignore.read_text(encoding="utf-8")
        for line in existing_content.splitlines():
            if line.strip() in (".SWE/", ".SWE"):
                needs_add = False
                break

    if needs_add:
        sep = "" if (not existing_content or existing_content.endswith("\n")) else "\n"
        gitignore.write_text(existing_content + sep + ".SWE/\n", encoding="utf-8")
        commit_phase_a_change(repo_dir, "chore(swe): gitignore SWE pipeline artifacts", paths=[".gitignore"])

    return True


def commit_phase_a_change(
    repo_dir: Path, message: str, paths: list[str] | None = None,
) -> str | None:
    """Stage and commit changes on PHASE_A_BRANCH. No-op if tree is clean.

    :param repo_dir: Target repo directory.
    :param message: Commit message.
    :param paths: Optional list of paths to stage (default: git add -A).
    :returns: Commit SHA, or None on no-op/failure.
    """
    try:
        if paths:
            _git(repo_dir, "add", "--", *paths)
        else:
            _git(repo_dir, "add", "-A")

        staged = _git(repo_dir, "diff", "--cached", "--name-only").stdout.strip()
        if not staged:
            return None

        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = PHASE_A_GIT_AUTHOR_NAME
        env["GIT_AUTHOR_EMAIL"] = PHASE_A_GIT_AUTHOR_EMAIL
        env["GIT_COMMITTER_NAME"] = PHASE_A_GIT_AUTHOR_NAME
        env["GIT_COMMITTER_EMAIL"] = PHASE_A_GIT_AUTHOR_EMAIL
        subprocess.run(
            ["git", "-C", str(repo_dir), "commit", "-m", message],
            check=True, env=env, capture_output=True, text=True,
        )  # nosec B603 - static args
        return _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    except subprocess.CalledProcessError:
        return None


def detect_lint_autofix(context: dict[str, Any]) -> bool:
    """Trigger: fire when lint report has auto-fixable errors and no prior autofix marker.

    Mirrors the original ``detect_a2_autofix`` logic:
    - Lint report must exist (latest.md in .SWE/reports/lint/)
    - Auto-fixable count > 0
    - No existing ``applied_for_{stem}.marker`` in .SWE/reports/lint-autofix/

    :param context: Trigger context dict with ``target_dir``.
    :returns: True if autofix should run.
    """
    target_dir = Path(context.get("target_dir", "."))
    report_path = _resolve_latest_report(target_dir, "lint")
    if report_path is None:
        return False
    try:
        text = report_path.read_text(encoding="utf-8")
    except OSError:
        return False
    m_fixable = re.search(r"\*\*fixable\*\*:\s*(\d+)", text)
    if not m_fixable:
        m_fixable = re.search(r"(\d+)\s+auto-fixable", text)
    fixable = int(m_fixable.group(1)) if m_fixable else 0
    if fixable <= 0:
        return False
    autofix_dir = target_dir / ".SWE" / "reports" / "lint-autofix"
    marker = autofix_dir / f"applied_for_{report_path.stem}.marker"
    return not marker.exists()


def run_lint_autofix(action: ActionSpec, context: TickContext) -> ActionResult:
    """Action: run ruff --fix, write report, commit, and invalidate A2 latest.md.

    Mirrors the original ``detect_a2_autofix`` execute logic:
    - Switches to PHASE_A_BRANCH
    - Runs ``ruff check --fix .``
    - Writes timestamped report to .SWE/reports/lint-autofix/
    - Creates applied_for_{stem}.marker
    - Commits changes if any fixes were applied
    - Deletes A2 latest.md so it regenerates on next tick

    :param action: Action spec with optional ``command`` param (default: ruff check --fix .).
    :param context: Tick context with target directory.
    :returns: ActionResult indicating success/failure.
    """
    if context.dry_run:
        return ActionResult(success=True, dry_run=True)

    target_dir = context.target_dir
    report_path = _resolve_latest_report(target_dir, "lint")
    if report_path is None:
        return ActionResult(success=False, stderr="No lint report found")

    ensure_phase_a_branch(target_dir)

    command = action.params.get("command", "ruff check --fix .")
    started_at = datetime.now(timezone.utc).isoformat()
    t0 = datetime.now(timezone.utc)
    try:
        proc = subprocess.run(
            command, shell=True, cwd=str(target_dir),
            capture_output=True, text=True, timeout=600, check=False,
        )  # nosec B603 - command from config, intentional shell for CLI flexibility
        stdout, stderr, exit_code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired:
        return ActionResult(success=False, stderr="ruff --fix timed out (600s)")
    duration_s = (datetime.now(timezone.utc) - t0).total_seconds()

    combined = stdout + "\n" + stderr
    m = re.search(r"\((\d+)\s+fixed", combined) or re.search(r"Fixed\s+(\d+)\s+errors?", combined)
    fixed_count = int(m.group(1)) if m else 0

    autofix_dir = target_dir / ".SWE" / "reports" / "lint-autofix"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_name = f"{ts}.md"
    content = "\n".join([
        f"# Lint Autofix — applied {fixed_count} fix(es)",
        "",
        f"- **Repo:** `{target_dir.name}`",
        f"- **Started at:** {started_at}",
        f"- **Duration:** {duration_s:.2f}s",
        f"- **Command:** `{command}`",
        f"- **Exit code:** {exit_code}",
        f"- **Source A2 report:** `{report_path.name}`",
        f"- **Fixes applied (parsed):** {fixed_count}",
        "",
        "## Stdout",
        "",
        "```",
        stdout.strip() or "(empty)",
        "```",
        "",
        "## Stderr",
        "",
        "```",
        stderr.strip() or "(empty)",
        "```",
        "",
    ])
    write_report(autofix_dir, report_name, content)
    update_latest_symlink(autofix_dir, "latest.md", report_name)

    if fixed_count > 0:
        commit_phase_a_change(target_dir, f"style: apply ruff safe auto-fixes ({fixed_count})")

    marker = autofix_dir / f"applied_for_{report_path.stem}.marker"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"autofix applied at {started_at} -> {report_name}\n", encoding="utf-8")

    a2_latest = target_dir / ".SWE" / "reports" / "lint" / "latest.md"
    try:
        if a2_latest.exists() or a2_latest.is_symlink():
            a2_latest.unlink()
    except OSError:
        pass

    return ActionResult(success=True, data={"fixed_count": fixed_count})


# ─── GitHub API helpers ─────────────────────────────────────────────────────


def _load_github_token(target_config: dict[str, Any]) -> str | None:
    """Load a GitHub token from target_config or environment.

    Resolution order: per-repo ``github_token`` → ``SWE_GITHUB_TOKEN`` → ``GITHUB_TOKEN``.

    :param target_config: Per-target config dict.
    :returns: Token string, or None.
    """
    token = (target_config.get("github_token") or "").strip()
    if token:
        return token
    for key in ("SWE_GITHUB_TOKEN", "GITHUB_TOKEN"):
        val = os.environ.get(key, "")
        if val:
            return val
    return None


def _gh_api_get_list(
    owner: str, gh_repo: str, endpoint: str, token: str,
    params: dict[str, str] | None = None,
) -> list[dict[str, Any]] | None:
    """GET from the GitHub REST API, expecting a JSON list.

    :param owner: Repo owner.
    :param gh_repo: Repo name.
    :param endpoint: API endpoint (e.g. "issues").
    :param token: GitHub auth token.
    :param params: Optional query parameters.
    :returns: List of dicts, or None on error.
    """
    url = f"https://api.github.com/repos/{owner}/{gh_repo}/{endpoint}"
    if params:
        url += "?" + urlencode(params)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        req = Request(url, headers=headers, method="GET")
        with urlopen(req, timeout=30) as resp:  # nosec B310 - HTTPS URL to GitHub API
            data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, list):
                return data
            return None
    except (HTTPError, URLError, OSError):
        return None


def _gh_api_post(
    owner: str, gh_repo: str, endpoint: str, payload: dict, token: str,
    expected_statuses: tuple[int, ...] = (201,),
) -> dict[str, Any] | None:
    """POST to the GitHub REST API.

    :param owner: Repo owner.
    :param gh_repo: Repo name.
    :param endpoint: API endpoint.
    :param payload: JSON body dict.
    :param token: GitHub auth token.
    :param expected_statuses: HTTP status codes considered success.
    :returns: Response JSON dict, or None on error.
    """
    url = f"https://api.github.com/repos/{owner}/{gh_repo}/{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    try:
        body = json.dumps(payload).encode("utf-8")
        req = Request(url, data=body, headers=headers, method="POST")
        with urlopen(req, timeout=30) as resp:  # nosec B310 - HTTPS URL to GitHub API
            if resp.status in expected_statuses:
                return json.loads(resp.read().decode("utf-8"))
            return None
    except (HTTPError, URLError, OSError):
        return None


def _gh_api_patch(
    owner: str, gh_repo: str, endpoint: str, payload: dict, token: str,
) -> dict[str, Any] | None:
    """PATCH to the GitHub REST API.

    :param owner: Repo owner.
    :param gh_repo: Repo name.
    :param endpoint: API endpoint.
    :param payload: JSON body dict.
    :param token: GitHub auth token.
    :returns: Response JSON dict, or None on error.
    """
    url = f"https://api.github.com/repos/{owner}/{gh_repo}/{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    try:
        body = json.dumps(payload).encode("utf-8")
        req = Request(url, data=body, headers=headers, method="PATCH")
        with urlopen(req, timeout=30) as resp:  # nosec B310 - HTTPS URL to GitHub API
            return json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, OSError):
        return None


def _read_github_session(target_dir: Path) -> dict[str, Any] | None:
    """Read the GitHub session marker.

    :param target_dir: Target repo directory.
    :returns: Session dict, or None if absent/unparseable.
    """
    path = target_dir / GITHUB_SESSION_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _git_issue_type_from_labels(labels: list[dict[str, Any]]) -> str:
    """Map GitHub labels to issue type values.

    :param labels: List of label dicts from GitHub API.
    :returns: Issue type string.
    """
    for label in labels:
        name = (label.get("name") or "").lower()
        if name == "bug":
            return "bug"
        if name == "enhancement":
            return "enhancement"
        if name == "refactor":
            return "refactor"
    return "enhancement"


def _git_issue_already_ingested(target_dir: Path, gh_number: int) -> bool:
    """Check if a GitHub issue with this number already exists in .SWE/issues/.

    :param target_dir: Target repo directory.
    :param gh_number: GitHub issue number.
    :returns: True if already ingested.
    """
    issues_dir = target_dir / SWE_SUBDIR / "issues"
    if not issues_dir.is_dir():
        return False
    for path in sorted(issues_dir.glob("*.md")):
        try:
            head = path.read_text(encoding="utf-8")[:800]
        except OSError:
            continue
        if not head.startswith("---"):
            continue
        sm = re.search(r"(?m)^source:\s*(\S+)", head)
        if not sm or sm.group(1) != "github":
            continue
        nm = re.search(r"(?m)^github_number:\s*(\d+)", head)
        if nm and int(nm.group(1)) == gh_number:
            return True
    return False


# ─── B1: GitHub issue intake ────────────────────────────────────────────────


def detect_b1_issue_gathering(context: dict[str, Any]) -> bool:
    """Trigger: fire when GitHub issue intake should run.

    Mirrors the original ``detect_b1_issue_gathering`` logic:
    - No active GitHub session
    - No completed session (or recheck interval elapsed)
    - Token available
    - Slug configured (contains '/')

    :param context: Trigger context dict with ``target_dir`` and ``target_config``.
    :returns: True if B1 should run.
    """
    target_dir = Path(context.get("target_dir", "."))
    target_config = context.get("target_config", {})

    session = _read_github_session(target_dir)
    if session is not None:
        if session.get("active"):
            return False
        if session.get("completed"):
            return False
        # Idle check state — re-check only after interval
        checked_at = session.get("checked_at", "")
        if checked_at:
            try:
                last = datetime.fromisoformat(checked_at)
                ago = (datetime.now(timezone.utc) - last).total_seconds()
                if ago < GITHUB_RECHECK_SECONDS:
                    return False
            except ValueError:
                pass

    token = _load_github_token(target_config)
    if not token:
        return False

    slug = (target_config.get("slug") or "").strip()
    return "/" in slug


def run_b1_issue_gathering(action: ActionSpec, context: TickContext) -> ActionResult:
    """Action: fetch oldest open GitHub issue with configured label and ingest it.

    Mirrors the original ``detect_b1_issue_gathering`` execute logic:
    - Calls GitHub API for open issues with the configured label
    - If no issues: writes idle session marker with checked_at timestamp
    - If issues found: picks oldest, writes issue file, creates active session
    - If issue already ingested: creates session pointing to existing issue

    :param action: Action spec (no special params needed).
    :param context: Tick context with target directory and config.
    :returns: ActionResult indicating success/failure.
    """
    if context.dry_run:
        return ActionResult(success=True, dry_run=True)

    target_dir = context.target_dir
    target_config = context.target_config
    token = _load_github_token(target_config)
    if not token:
        return ActionResult(success=False, stderr="No GitHub token available")

    slug = (target_config.get("slug") or "").strip()
    if "/" not in slug:
        return ActionResult(success=False, stderr=f"Invalid slug: {slug}")
    owner, gh_repo_name = slug.split("/", 1)

    issue_label = (target_config.get("issue_label") or "swe-pipeline").strip()
    repo_name = context.target

    issues = _gh_api_get_list(
        owner, gh_repo_name, "issues", token,
        params={"state": "open", "labels": issue_label},
    )
    if issues is None:
        return ActionResult(success=False, stderr="GitHub API request failed")

    session_path = target_dir / GITHUB_SESSION_FILE
    session_path.parent.mkdir(parents=True, exist_ok=True)

    if not issues:
        session_path.write_text(json.dumps({
            "active": False,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2), encoding="utf-8")
        return ActionResult(success=True, data={"issues_found": 0})

    oldest = min(issues, key=lambda i: i.get("created_at", ""))
    gh_number = oldest.get("number")
    if not gh_number:
        return ActionResult(success=False, stderr="No issue number in GitHub response")

    if _git_issue_already_ingested(target_dir, gh_number):
        session_path.write_text(json.dumps({
            "active": True,
            "github_number": gh_number,
            "issue_id": f"github-{gh_number}",
            "started_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2), encoding="utf-8")
        return ActionResult(success=True, data={"issue_id": f"github-{gh_number}", "already_ingested": True})

    issue_id = f"github-{gh_number}"
    title = oldest.get("title", f"GitHub Issue #{gh_number}")
    body = oldest.get("body") or ""
    gh_url = oldest.get("html_url", "")
    gh_labels = [l.get("name", "") for l in oldest.get("labels", [])]
    issue_type = _git_issue_type_from_labels(oldest.get("labels", []))

    create_issue(
        target_dir,
        issue_data={
            "id": issue_id,
            "status": "open",
            "source": "github",
            "type": issue_type,
            "repo": repo_name,
            "labels": gh_labels,
            "github_number": gh_number,
            "github_url": gh_url,
        },
        body=f"# {title}\n\n{body}",
    )

    session_path.write_text(json.dumps({
        "active": True,
        "github_number": gh_number,
        "issue_id": issue_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")

    return ActionResult(success=True, data={"issue_id": issue_id, "gh_number": gh_number})


# ─── Task detection helper ──────────────────────────────────────────────────


def _find_active_task(repo_name: str) -> Path | None:
    """Return the most recent unfinished task dir for this repo, if any.

    A task is "unfinished" when its task.json exists but no gate.json has been
    written yet. Scans all date buckets in TASKS_DIR.

    :param repo_name: Repo name to match in task.json.
    :returns: Path to the active task dir, or None.
    """
    if not TASKS_DIR.is_dir():
        return None
    candidates: list[Path] = []
    for date_dir in sorted(TASKS_DIR.iterdir()):
        if not date_dir.is_dir():
            continue
        for task_dir in date_dir.iterdir():
            if not task_dir.is_dir():
                continue
            if not (task_dir / "task.json").exists():
                continue
            if (task_dir / "gate.json").exists():
                continue
            try:
                task = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if task.get("repo_name") == repo_name:
                candidates.append(task_dir)
    if not candidates:
        return None
    return max(candidates)


def _count_unranked_review_issues(target_dir: Path) -> int:
    """Count open review-sourced issues that have no hivemind_score yet.

    :param target_dir: Target repo directory.
    :returns: Count of unranked review issues.
    """
    count = 0
    issues_dir = target_dir / SWE_SUBDIR / "issues"
    if not issues_dir.is_dir():
        return 0
    for path in sorted(issues_dir.glob("*.md")):
        try:
            head = path.read_text(encoding="utf-8")[:800]
        except OSError:
            continue
        if not head.startswith("---"):
            continue
        sm = re.search(r"(?m)^status:\s*(\S+)", head)
        if not sm or sm.group(1) != "open":
            continue
        sm = re.search(r"(?m)^source:\s*(\S+)", head)
        if not sm or sm.group(1) != "review":
            continue
        if not re.search(r"(?m)^hivemind_score:", head):
            count += 1
    return count


# ─── C-review-ranking: Rank unranked review issues ──────────────────────────


def detect_c_review_ranking(context: dict[str, Any]) -> bool:
    """Trigger: fire when there are >=2 unranked review issues to rank.

    Mirrors the original ``detect_c_review_ranking`` logic:
    - No active GitHub session
    - No active task
    - >= 2 unranked review issues
    - No ranked_{count}.marker

    :param context: Trigger context dict with ``target_dir`` and ``target``.
    :returns: True if ranking should run.
    """
    target_dir = Path(context.get("target_dir", "."))
    repo_name = context.get("target", "")

    session = _read_github_session(target_dir)
    if session is not None and session.get("active"):
        return False

    if _find_active_task(repo_name) is not None:
        return False

    unranked = _count_unranked_review_issues(target_dir)
    if unranked < 2:
        return False

    markers_dir = target_dir / SWE_SUBDIR / "reports" / "review-ranking"
    marker_file = markers_dir / f"ranked_{unranked}.marker"
    return not marker_file.exists()


def run_c_review_ranking(action: ActionSpec, context: TickContext) -> ActionResult:
    """Action: run the Targaryen Council Hivemind ranking script.

    :param action: Action spec with optional ``max_issues`` param.
    :param context: Tick context with target directory and name.
    :returns: ActionResult indicating success/failure.
    """
    if context.dry_run:
        return ActionResult(success=True, dry_run=True)

    target_dir = context.target_dir
    repo_name = context.target
    target_config = context.target_config

    unranked = _count_unranked_review_issues(target_dir)
    max_issues = target_config.get("max_review_issues_per_generation", 3)

    cmd = [sys.executable, RANKING_SCRIPT, "--repo", repo_name, "--max-issues", str(max_issues)]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600, check=False,
        )  # nosec B603 - calling known script
    except subprocess.TimeoutExpired:
        markers_dir = target_dir / SWE_SUBDIR / "reports" / "review-ranking"
        markers_dir.mkdir(parents=True, exist_ok=True)
        (markers_dir / f"ranked_{unranked}.marker").write_text("timeout", encoding="utf-8")
        return ActionResult(success=False, stderr="Review ranking timed out (600s)")

    markers_dir = target_dir / SWE_SUBDIR / "reports" / "review-ranking"
    markers_dir.mkdir(parents=True, exist_ok=True)

    if result.returncode != 0:
        (markers_dir / f"ranked_{unranked}.marker").write_text("failed", encoding="utf-8")
        return ActionResult(
            success=False,
            stderr=f"Ranking failed: {result.stderr[:500]}",
        )

    (markers_dir / f"ranked_{unranked}.marker").write_text("ok", encoding="utf-8")
    return ActionResult(success=True, data={"unranked_count": unranked})


# ─── C-issue-fix: SELECT/GATE state machine ─────────────────────────────────


def detect_c_issue_fix(context: dict[str, Any]) -> bool:
    """Trigger: fire when there's an active task or an open issue to work on.

    Mirrors the original ``detect_c_issue_fix`` logic:
    - Active task exists (C-gate/C-stale/C-wait), OR
    - Open issue exists (C-select)
    - No active GitHub session (Phase C runs in default mode)

    :param context: Trigger context dict with ``target_dir`` and ``target``.
    :returns: True if the issue-fix state machine should run.
    """
    target_dir = Path(context.get("target_dir", "."))
    repo_name = context.get("target", "")

    session = _read_github_session(target_dir)
    if session is not None and session.get("active"):
        return False

    if _find_active_task(repo_name) is not None:
        return True

    issues = load_issues(target_dir)
    return any(i.status == "open" for i in issues)


def run_c_issue_fix(action: ActionSpec, context: TickContext) -> ActionResult:
    """Action: run run_issue_fix.py to drive the SELECT/GATE state machine.

    The script auto-detects whether to gate an active task or select a new issue.

    :param action: Action spec (no special params needed).
    :param context: Tick context with target directory and name.
    :returns: ActionResult indicating success/failure.
    """
    if context.dry_run:
        return ActionResult(success=True, dry_run=True)

    repo_name = context.target
    cmd = [sys.executable, ISSUE_FIX_SCRIPT, repo_name]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600, check=False,
        )  # nosec B603 - calling known script
    except subprocess.TimeoutExpired:
        return ActionResult(success=False, stderr="Issue fix timed out (600s)")

    if result.returncode != 0:
        return ActionResult(
            success=False,
            stderr=f"Issue fix failed: {result.stderr[:500]}",
            stdout=result.stdout,
        )

    return ActionResult(success=True, stdout=result.stdout, data={"stage": "c-issue-fix"})


# ─── C-phase shared helpers ─────────────────────────────────────────────────

INTEGRATION_BRANCH = "swe-pipeline/integration"
COVERAGE_TARGET = 100.0
MAX_REVIEW_GENERATIONS = 3
MAX_REVIEW_ISSUES_PER_GENERATION = 3
MAX_PR_REVIEW_CYCLES = 3
DOC_SYNC_MARKER = "doc_sync.json"
DOC_SYNC_QUEUED_MARKER = "doc_sync_queued.json"


def integration_head_sha(target_dir: Path, default_branch: str) -> str | None:
    """Return the integration branch HEAD sha, or default branch tip.

    :param target_dir: Target repo directory.
    :param default_branch: Default branch name (e.g. 'main').
    :returns: SHA string, or None if neither branch resolves.
    """
    for ref in (INTEGRATION_BRANCH, default_branch):
        res = _git(target_dir, "rev-parse", "--verify", ref, check=False)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    return None


def _open_issue_count(target_dir: Path) -> int:
    """Count open issues under .SWE/issues/.

    When a GitHub session is active, only github-sourced issues are counted.

    :param target_dir: Target repo directory.
    :returns: Number of open issues.
    """
    session = _read_github_session(target_dir)
    github_only = session is not None and session.get("active", False)
    count = 0
    issues_dir = target_dir / SWE_SUBDIR / "issues"
    if not issues_dir.is_dir():
        return 0
    for path in sorted(issues_dir.glob("*.md")):
        try:
            head = path.read_text(encoding="utf-8")[:800]
        except OSError:
            continue
        if not head.startswith("---"):
            continue
        sm = re.search(r"(?m)^status:\s*(\S+)", head)
        if not sm or sm.group(1) != "open":
            continue
        if github_only:
            sm2 = re.search(r"(?m)^source:\s*(\S+)", head)
            if not sm2 or sm2.group(1) != "github":
                continue
        count += 1
    return count


def _a1_is_pass(target_dir: Path) -> bool:
    """Check if the A1 test-infra report shows PASS.

    :param target_dir: Target repo directory.
    :returns: True if A1 report first line contains '— PASS'.
    """
    latest = target_dir / SWE_SUBDIR / "reports" / "test-infra" / "latest.md"
    target = latest.resolve() if latest.is_symlink() else latest
    if not target.exists():
        return False
    try:
        return "— PASS" in target.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return False


def _a7_coverage_pct(target_dir: Path) -> float | None:
    """Read the total coverage % from the latest A7 report.

    :param target_dir: Target repo directory.
    :returns: Coverage percentage, or None.
    """
    report = _resolve_latest_report(target_dir, "coverage")
    if report is None:
        return None
    try:
        text = report.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"\*\*Coverage:\*\*\s*([\d.]+)%", text)
    return float(m.group(1)) if m else None


def _find_issue_by_id(target_dir: Path, issue_id: str) -> Path | None:
    """Return the issue file path if it exists.

    :param target_dir: Target repo directory.
    :param issue_id: Issue ID to find.
    :returns: Path to the issue file, or None.
    """
    path = target_dir / SWE_SUBDIR / "issues" / f"{issue_id}.md"
    return path if path.exists() else None


def _write_pipeline_issue(
    target_dir: Path, repo_name: str, issue_id: str,
    issue_type: str, title: str, body: str,
    labels: list[str] | None = None,
    extra: list[tuple[str, Any]] | None = None,
) -> Path:
    """Write a pipeline-generated issue via the issue store.

    :param target_dir: Target repo directory.
    :param repo_name: Repo name.
    :param issue_id: Issue ID.
    :param issue_type: Issue type (coverage, review, revision, etc.).
    :param title: Issue title.
    :param body: Issue markdown body.
    :param labels: Optional list of labels.
    :param extra: Optional extra frontmatter fields.
    :returns: Path to the written issue file.
    """
    issue_data: dict[str, Any] = {
        "id": issue_id,
        "status": "open",
        "source": "pipeline",
        "type": issue_type,
        "repo": repo_name,
        "labels": labels or ["pipeline"],
    }
    if extra:
        for key, value in extra:
            issue_data[key] = value
    create_issue(target_dir, issue_data=issue_data, body=f"# {title}\n\n{body}")
    return target_dir / SWE_SUBDIR / "issues" / f"{issue_id}.md"


def _close_and_comment_github_issue(
    owner: str, gh_repo: str, gh_issue_number: int,
    pr_number: int, pr_url: str, token: str,
    merged: bool,
) -> None:
    """Post a comment on the GitHub issue and close it (if merged).

    :param owner: Repo owner.
    :param gh_repo: Repo name.
    :param gh_issue_number: GitHub issue number.
    :param pr_number: PR number.
    :param pr_url: PR URL.
    :param token: GitHub auth token.
    :param merged: Whether the PR was merged.
    """
    if merged:
        comment = (
            f"Fixed by PR [#{pr_number}]({pr_url}). "
            f"The SWE pipeline has addressed this issue and the fix has been "
            f"merged into the default branch."
        )
    else:
        comment = (
            f"The PR [#{pr_number}]({pr_url}) was closed without merging. "
            f"This issue remains open."
        )
    _gh_api_post(
        owner, gh_repo, f"issues/{gh_issue_number}/comments",
        {"body": comment}, token, expected_statuses=(200, 201),
    )
    if merged:
        _gh_api_patch(
            owner, gh_repo, f"issues/{gh_issue_number}",
            {"state": "closed"}, token,
        )


def _build_pr_body(target_dir: Path, repo_name: str, default_branch: str) -> str:
    """Build the PR description summarising the pipeline's work.

    :param target_dir: Target repo directory.
    :param repo_name: Repo name.
    :param default_branch: Default branch name.
    :returns: PR body markdown string.
    """
    bug_count = refactor_count = enhance_count = 0
    issues_dir = target_dir / SWE_SUBDIR / "issues"
    if issues_dir.is_dir():
        for path in sorted(issues_dir.glob("*.md")):
            try:
                head = path.read_text(encoding="utf-8")[:800]
            except OSError:
                continue
            if not head.startswith("---"):
                continue
            sm = re.search(r"(?m)^status:\s*(\S+)", head)
            if not sm or sm.group(1) != "done":
                continue
            tm = re.search(r"(?m)^type:\s*(\S+)", head)
            itype = tm.group(1) if tm else ""
            if itype == "bug":
                bug_count += 1
            elif itype == "refactor":
                refactor_count += 1
            elif itype == "enhancement":
                enhance_count += 1

    done_count = bug_count + refactor_count + enhance_count
    pct = _a7_coverage_pct(target_dir) or 100.0
    sha = integration_head_sha(target_dir, default_branch) or "unknown"

    return "\n".join([
        "## SWE Pipeline — Automated Code Improvements",
        "",
        ("This pull request was automatically generated by the "
         "[SWE Pipeline](https://github.com/ValyrianTech/spellbook)."),
        "",
        "### Summary",
        "",
        (f"- **{done_count} issues fixed**: {bug_count} bugs, {refactor_count} "
         f"refactors, {enhance_count} enhancements"),
        f"- **Coverage**: {pct:.0f}%",
        "- **Tests**: all passing",
        "",
        "### What changed",
        "",
        (f"See the commit history on the `{INTEGRATION_BRANCH}` branch for "
         "the full list of changes."),
        "",
        "### Review",
        "",
        ("The pipeline performed multiple code reviews and all issues found "
         f"were addressed. The codebase is at {pct:.0f}% test coverage with "
         "a green test suite."),
        "---",
        f"*Automatically generated at commit `{sha[:8]}`.*",
    ])


def _build_doc_sync_prompt(
    target_dir: Path, repo_name: str, default_branch: str,
    sha: str, pr_exists: bool,
) -> str:
    """Build the prompt for the DocumentationSyncAgent.

    :param target_dir: Target repo directory.
    :param repo_name: Repo name.
    :param default_branch: Default branch name.
    :param sha: Integration HEAD SHA.
    :param pr_exists: Whether a PR already exists.
    :returns: Prompt string.
    """
    marker_path = target_dir / SWE_SUBDIR / DOC_SYNC_MARKER

    push_instruction = ""
    if pr_exists:
        push_instruction = (
            f"\n\nAfter committing, push the integration branch to update "
            f"the existing PR:\n"
            f"  RunCommand: cd {target_dir} && git push origin {INTEGRATION_BRANCH}\n"
        )

    return (
        f"Sync documentation for the locally cloned repo at:\n"
        f"  {target_dir}\n\n"
        f"## Your task\n\n"
        f"You are a Documentation Synchronization AI. Your job is to update "
        f"standalone documentation files (README, CHANGELOG, files under "
        f"`docs/`) to reflect recent code changes. Do NOT edit source-code "
        f"docstrings — those are handled separately by the pipeline.\n\n"
        f"## Step 1 — understand the changes\n\n"
        f"Use the Progress tool to plan your steps. Then read the repo "
        f"briefing and examine recent changes:\n"
        f"  read {target_dir}/.SWE/repo_briefing.md\n"
        f"  cd {target_dir} && git log {default_branch}..{INTEGRATION_BRANCH} --oneline\n"
        f"  cd {target_dir} && git diff --stat {default_branch}...{INTEGRATION_BRANCH}\n\n"
        f"Use ReadFile to examine existing documentation files in the repo.\n\n"
        f"## Step 2 — update documentation\n\n"
        f"Use OpenCode to update documentation files. Pass repo_name="
        f"'{repo_name}'. Focus on:\n"
        f"- **README.md**: update feature descriptions, usage examples, "
        f"or setup instructions if the changes affect them\n"
        f"- **CHANGELOG.md**: add entries for notable changes (bug fixes, "
        f"new features, breaking changes)\n"
        f"- **docs/** files: update API docs, architecture docs, or any "
        f"other documentation that should reflect the latest code\n\n"
        f"Be concise and accurate. Only document changes that are actually "
        f"in the commit history. Do NOT invent or assume features that "
        f"are not in the diff.\n\n"
        f"## Step 3 — verify and commit\n\n"
        f"Run the test suite to make sure your doc-only changes "
        f"don't break anything:\n"
        f"  cd {target_dir} && .venv/bin/pytest -q\n\n"
        f"Commit your changes on the `{INTEGRATION_BRANCH}` branch:\n"
        f"  cd {target_dir} && git add -A && "
        f"git -c user.name='{PHASE_A_GIT_AUTHOR_NAME}' "
        f"-c user.email='{PHASE_A_GIT_AUTHOR_EMAIL}' "
        f"commit -m \"docs: sync documentation with latest changes\"\n\n"
        f"If `cd {target_dir} && git status --porcelain` shows no changes, "
        f"skip the commit (nothing to update is fine) and proceed."
        f"{push_instruction}\n\n"
        f"Write the completion marker as your LAST step using the WriteFile "
        f"tool. Write this exact JSON content to:\n"
        f"  {marker_path}\n"
        f"{{\n"
        f'  "sha": "{sha}",\n'
        f'  "completed_at": "<current ISO timestamp>",\n'
        f'  "changes_made": true\n'
        f"}}\n"
        f"Replace `<current ISO timestamp>` with the actual current time "
        f"in ISO format (e.g. 2026-06-23T12:00:00Z). If you made no "
        f"documentation changes, set `\"changes_made\": false`.\n\n"
        f"## Important notes\n\n"
        f"- Always use the Progress tool to track your steps.\n"
        f"- Do NOT modify source code — only documentation files.\n"
        f"- Do NOT change the branch — stay on `{INTEGRATION_BRANCH}`.\n"
        f"- Write the completion marker EVEN IF no changes were needed — "
        f"the pipeline needs it to proceed.\n"
    )


def _build_pr_review_prompt(
    target_dir: Path, repo_name: str, default_branch: str, sha: str,
    pr_number: int, owner: str, gh_repo_name: str,
    review_cycle: int = 0, max_cycles: int = 0,
) -> str:
    """Build the prompt for the PRReviewAgent.

    :param target_dir: Target repo directory.
    :param repo_name: Repo name.
    :param default_branch: Default branch name.
    :param sha: Integration HEAD sha.
    :param pr_number: PR number.
    :param owner: Repo owner.
    :param gh_repo_name: Repo name on GitHub.
    :param review_cycle: Current review cycle number.
    :param max_cycles: Maximum review cycles.
    :returns: Prompt string.
    """
    marker_path = target_dir / SWE_SUBDIR / "pr_reviewed.json"
    repo_root = "/home/wouter/Repos/spellbook"
    reviewer_cmd_base = (
        f"cd {repo_root} && {sys.executable} "
        f"-m apps.Serendipity.SWE.scripts.post_pr_review {repo_name} "
        f"--pr-number {pr_number} --body-file /tmp/pr_review.md"
    )

    cycle_guidance = ""
    if max_cycles > 0:
        remaining = max_cycles - review_cycle
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(review_cycle % 10, "th")
        if 11 <= review_cycle % 100 <= 13:
            suffix = "th"
        cycle_guidance += (
            f"\n\n## Review Cycle Context\n\n"
            f"This is the **{review_cycle}{suffix}** "
        )
        if remaining <= 0:
            cycle_guidance += "and **final** "
        cycle_guidance += (
            f"round of automated review for this PR (max {max_cycles} cycles). "
            f"The pipeline will stop fixing issues after this round and a "
            f"human will take over.\n\n"
        )
        if review_cycle >= 2:
            cycle_guidance += (
                "This code has already been reviewed and fixed multiple times. "
                "File only **critical issues** that affect correctness, "
                "security, or reliability — NOT style nits, refactors, or "
                "minor improvements. If the PR is acceptable, prefer COMMENT "
                "over REQUEST_CHANGES.\n\n"
            )
        if review_cycle >= 3:
            cycle_guidance += (
                "This is a late-stage review. Request changes ONLY for "
                "**showstopper bugs or regressions** that would break the "
                "application. If you are unsure, lean toward COMMENT.\n"
            )

    return (
        f"You are reviewing GitHub PR #{pr_number} for the repo at:\n"
        f"  {target_dir}\n\n"
        f"PR URL:    https://github.com/{owner}/{gh_repo_name}/pull/{pr_number}\n"
        f"Branch:    {INTEGRATION_BRANCH}\n"
        f"Target:    {default_branch}\n"
        f"Commit:    {sha[:12]}\n\n"
        f"## Your task\n\n"
        f"Review this PR and post a review. Use **COMMENT** if the changes "
        f"are good or only have minor observations. Use **REQUEST_CHANGES** "
        f"if you find concrete problems that should block merging — real bugs, "
        f"regressions, missing error handling, or security concerns. Be "
        f"thorough but fair. Do NOT use APPROVE (that is for humans)."
        f"{cycle_guidance}\n"
        f"## Step 1 — gather context\n\n"
        f"Use RunCommand to understand the changes. Start small:\n"
        f"  cd {target_dir} && git log {default_branch}..{INTEGRATION_BRANCH} --oneline\n"
        f"  cd {target_dir} && git diff --stat {default_branch}...{INTEGRATION_BRANCH}\n\n"
        f"## Step 2 — review the changes\n\n"
        f"Use the Progress tool to plan your review steps. For each major "
        f"changed file, read the file from the repo directory and assess:\n\n"
        f"- **Correctness**: Do the changes fix real bugs without introducing new ones?\n"
        f"- **Cohesion**: Do the changes make sense together as a unit?\n"
        f"- **Regressions**: Could any existing behaviour be broken?\n"
        f"- **Test coverage**: Are the right things tested? Any gaps?\n"
        f"- **Code quality**: Clear names, no dead code, consistent style?\n"
        f"- **PR hygiene**: Are commit messages clear? Does the diff match "
        f"the description?\n\n"
        f"## Step 3 — post the review\n\n"
        f"Write your review as markdown to `/tmp/pr_review.md`, using this "
        f"structure:\n\n"
        f"### Summary\n"
        f"1-2 paragraph high-level assessment of the overall change.\n\n"
        f"### Changes Overview\n"
        f"What was changed, grouped by area (e.g. module names, test files)."
        f"\n\n"
        f"### Issues & Concerns\n"
        f"Specific problems found. If you found issues that should block "
        f"merging, list each one clearly so the pipeline can fix them "
        f"individually. Start each item with a plain numbered line (1. / 2. "
        f"/ 3.) — do NOT wrap it in bold (**) or markdown headings (###). "
        f"Leave a blank line after the title, then write a detailed "
        f"description paragraph.\n\n"
        f"If the changes look clean, write \"No issues identified.\"\n\n"
        f"### Recommendation\n"
        f"Clear recommendation: either \"Ready to merge\" (post as COMMENT) "
        f"or \"Changes needed before merging\" (post as REQUEST_CHANGES, "
        f"with the specific problems listed above).\n\n"
        f"Then post it with RunCommand:\n\n"
        f"  If the PR is ready to merge (COMMENT):\n"
        f"    {reviewer_cmd_base} --event COMMENT\n\n"
        f"  If changes are needed before merging (REQUEST_CHANGES):\n"
        f"    {reviewer_cmd_base} --event REQUEST_CHANGES\n\n"
        f"CRITICAL: The RunCommand output MUST show \"Posted ... review\" before "
        f"you proceed. If the post fails, read the error and fix the problem. "
        f"Do NOT write the completion marker until the post succeeds.\n\n"
        f"## Step 4 — close the loop (do this LAST)\n\n"
        f"After a SUCCESSFUL post (verified by RunCommand output), write a "
        f"completion marker with WriteFile to:\n"
        f"  {marker_path}\n"
        f'Content: {{"pr_number": {pr_number}, "reviewed_at": "<ISO timestamp>"}}\n\n'
        f"Do NOT modify any source code and do NOT commit anything.\n"
    )


# ─── C-pr-status: Poll GitHub PR ────────────────────────────────────────────


def detect_c_pr_status(context: dict[str, Any]) -> bool:
    """Trigger: fire when a PR has been published and needs status polling.

    :param context: Trigger context dict.
    :returns: True if PR status should be polled.
    """
    target_dir = Path(context.get("target_dir", "."))
    target_config = context.get("target_config", {})

    pr_marker = target_dir / SWE_SUBDIR / "pr_published.json"
    if not pr_marker.exists():
        return False
    try:
        pr_data = json.loads(pr_marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not pr_data.get("pr_number"):
        return False
    pr_state = pr_data.get("pr_state", "open")
    if pr_state in ("merged", "rejected"):
        return False

    token = _load_github_token(target_config)
    if not token:
        return False

    slug = (target_config.get("slug") or "").strip()
    return "/" in slug


def run_c_pr_status(action: ActionSpec, context: TickContext) -> ActionResult:
    """Action: poll GitHub PR for merge/reject/changes-requested.

    :param action: Action spec.
    :param context: Tick context.
    :returns: ActionResult indicating success.
    """
    if context.dry_run:
        return ActionResult(success=True, dry_run=True)

    target_dir = context.target_dir
    target_config = context.target_config
    pr_marker = target_dir / SWE_SUBDIR / "pr_published.json"
    pr_data = json.loads(pr_marker.read_text(encoding="utf-8"))
    pr_number = pr_data["pr_number"]

    token = _load_github_token(target_config)
    if token is None:
        return ActionResult(success=False, stderr="No GitHub token configured")
    slug = (target_config.get("slug") or "").strip()
    owner, gh_repo_name = slug.split("/", 1)

    # Fetch PR info
    url = f"https://api.github.com/repos/{owner}/{gh_repo_name}/pulls/{pr_number}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        req = Request(url, headers=headers, method="GET")
        with urlopen(req, timeout=30) as resp:  # nosec B310 - HTTPS GitHub API
            pr_info = json.loads(resp.read().decode("utf-8"))
    except (HTTPError, URLError, OSError):
        return ActionResult(success=False, stderr="Failed to fetch PR info")

    gh_state = pr_info.get("state", "open")
    merged = pr_info.get("merged", False)

    def _update_marker(new_state: str, **kwargs: Any) -> None:
        pr_data["pr_state"] = new_state
        pr_data.update(kwargs)
        pr_marker.write_text(json.dumps(pr_data, indent=2), encoding="utf-8")

    # Terminal: merged
    if gh_state == "closed" and merged:
        _update_marker("merged", merged_at=pr_info.get("merged_at", ""))
        session = _read_github_session(target_dir)
        if session is not None and session.get("active"):
            gh_issue_number = session.get("github_number")
            if gh_issue_number:
                _close_and_comment_github_issue(
                    owner, gh_repo_name, gh_issue_number, pr_number,
                    pr_info.get("html_url", ""), token, merged=True,
                )
            session["active"] = False
            session["completed"] = True
            session["completed_at"] = datetime.now(timezone.utc).isoformat()
            (target_dir / GITHUB_SESSION_FILE).write_text(
                json.dumps(session, indent=2), encoding="utf-8")
        return ActionResult(success=True, data={"pr_state": "merged"})

    # Terminal: rejected
    if gh_state == "closed" and not merged:
        _update_marker("rejected", closed_at=pr_info.get("closed_at", ""))
        session = _read_github_session(target_dir)
        if session is not None and session.get("active"):
            gh_issue_number = session.get("github_number")
            if gh_issue_number:
                _close_and_comment_github_issue(
                    owner, gh_repo_name, gh_issue_number, pr_number,
                    pr_info.get("html_url", ""), token, merged=False,
                )
            session["active"] = False
            session["completed"] = True
            session["completed_at"] = datetime.now(timezone.utc).isoformat()
            (target_dir / GITHUB_SESSION_FILE).write_text(
                json.dumps(session, indent=2), encoding="utf-8")
        return ActionResult(success=True, data={"pr_state": "rejected"})

    # Still open — check for changes-requested reviews
    reviews_url = f"https://api.github.com/repos/{owner}/{gh_repo_name}/pulls/{pr_number}/reviews"
    try:
        req2 = Request(reviews_url, headers=headers, method="GET")
        with urlopen(req2, timeout=30) as resp2:  # nosec B310 - HTTPS GitHub API
            reviews = json.loads(resp2.read().decode("utf-8"))
    except (HTTPError, URLError, OSError):
        reviews = []

    # Find the most recent CHANGES_REQUESTED review
    changes_requested = [
        r for r in reviews
        if r.get("state") == "CHANGES_REQUESTED"
    ]

    if changes_requested:
        latest_cr = max(changes_requested, key=lambda r: r.get("submitted_at", ""))
        # File revision issues for each change request
        from cronpypeline.plugins.swe_prompts import _parse_change_requests
        requests_list = _parse_change_requests(latest_cr.get("body", ""))
        if requests_list:
            prefix = f"pr-revision-{pr_number}-"
            issues_dir = target_dir / SWE_SUBDIR / "issues"
            existing_max = 0
            if issues_dir.is_dir():
                for path in issues_dir.glob(f"{prefix}*.md"):
                    try:
                        num = int(path.stem[len(prefix):])
                        existing_max = max(existing_max, num)
                    except ValueError:
                        pass
            for i, req_text in enumerate(requests_list, start=existing_max + 1):
                issue_id = f"{prefix}{i}"
                if not _find_issue_by_id(target_dir, issue_id):
                    title = req_text if len(req_text) <= 120 else req_text[:117] + "..."
                    body = (
                        f"# PR #{pr_number} — Change Request {i}\n\n"
                        f"A reviewer requested changes on PR #{pr_number}. "
                        f"Address this request and push an update.\n\n"
                        f"## Request\n\n{req_text}\n"
                    )
                    _write_pipeline_issue(
                        target_dir, context.target, issue_id, "revision",
                        title, body, ["revision", "pr-review"],
                    )

        # Update cycle count
        pr_data["pr_review_cycles"] = pr_data.get("pr_review_cycles", 0) + 1
        pr_marker.write_text(json.dumps(pr_data, indent=2), encoding="utf-8")

    return ActionResult(success=True, data={"pr_state": "open"})


# ─── C-coverage: Create coverage issue ──────────────────────────────────────


def detect_c_coverage_issue(context: dict[str, Any]) -> bool:
    """Trigger: fire when coverage < target and queue is empty.

    :param context: Trigger context dict.
    :returns: True if a coverage issue should be created.
    """
    target_dir = Path(context.get("target_dir", "."))
    target_config = context.get("target_config", {})

    if _open_issue_count(target_dir) > 0:
        return False
    if not _a1_is_pass(target_dir):
        return False
    pct = _a7_coverage_pct(target_dir)
    if pct is None or pct >= COVERAGE_TARGET:
        return False

    # Defer if PR published but not reviewed
    pr_marker = target_dir / SWE_SUBDIR / "pr_published.json"
    if pr_marker.exists():
        reviewed_marker = target_dir / SWE_SUBDIR / "pr_reviewed.json"
        if not reviewed_marker.exists():
            return False

    default_branch = target_config.get("default_branch", "main")
    sha = integration_head_sha(target_dir, default_branch)
    if not sha:
        return False

    issue_id = f"coverage-{sha[:8]}"
    existing = _find_issue_by_id(target_dir, issue_id)
    if existing is not None:
        try:
            head = existing.read_text(encoding="utf-8")[:800]
            sm = re.search(r"(?m)^status:\s*(\S+)", head)
            if sm and sm.group(1) != "discarded":
                return False
        except OSError:
            pass

    return True


def run_c_coverage_issue(action: ActionSpec, context: TickContext) -> ActionResult:
    """Action: create a coverage issue file.

    :param action: Action spec.
    :param context: Tick context.
    :returns: ActionResult.
    """
    if context.dry_run:
        return ActionResult(success=True, dry_run=True)

    target_dir = context.target_dir
    target_config = context.target_config
    repo_name = context.target
    default_branch = target_config.get("default_branch", "main")
    sha = integration_head_sha(target_dir, default_branch)
    if sha is None:
        return ActionResult(success=False, stderr="Failed to determine integration head SHA")
    issue_id = f"coverage-{sha[:8]}"
    pct = _a7_coverage_pct(target_dir) or 0.0

    title = f"Increase test coverage to {COVERAGE_TARGET:.0f}% (currently {pct:.0f}%)"
    body = (
        f"Overall coverage for `{repo_name}` is **{pct:.0f}%**, below the "
        f"pipeline target of {COVERAGE_TARGET:.0f}%. Add tests so every "
        f"reachable line/branch is covered. Genuinely unreachable lines may "
        f"be marked with `# pragma: no cover` with a short justification.\n\n"
        f"## Source\n\n"
        f"Generated by SWE pipeline (Phase C coverage check). "
        f"See `.SWE/reports/coverage/latest.md`.\n"
    )
    _write_pipeline_issue(
        target_dir, repo_name, issue_id, "coverage", title, body,
        ["coverage", "tests"],
    )
    return ActionResult(success=True, data={"issue_id": issue_id})


# ─── C-review: Create review issue ──────────────────────────────────────────


def _count_done_review_issues(target_dir: Path) -> int:
    """Count done review meta-issues.

    :param target_dir: Target repo directory.
    :returns: Count of done review issues.
    """
    count = 0
    issues_dir = target_dir / SWE_SUBDIR / "issues"
    if not issues_dir.is_dir():
        return 0
    for path in sorted(issues_dir.glob("*.md")):
        try:
            head = path.read_text(encoding="utf-8")[:800]
        except OSError:
            continue
        if not head.startswith("---"):
            continue
        sm = re.search(r"(?m)^status:\s*(\S+)", head)
        if not sm or sm.group(1) != "done":
            continue
        tm = re.search(r"(?m)^type:\s*(\S+)", head)
        if not tm or tm.group(1) != "review":
            continue
        count += 1
    return count


def _find_previous_review_sha(target_dir: Path) -> str | None:
    """Extract the SHA from the most recent done review issue.

    :param target_dir: Target repo directory.
    :returns: 8-char SHA prefix, or None.
    """
    best_path: Path | None = None
    issues_dir = target_dir / SWE_SUBDIR / "issues"
    if not issues_dir.is_dir():
        return None
    for path in sorted(issues_dir.glob("*.md")):
        try:
            head = path.read_text(encoding="utf-8")[:800]
        except OSError:
            continue
        if not head.startswith("---"):
            continue
        sm = re.search(r"(?m)^status:\s*(\S+)", head)
        if not sm or sm.group(1) != "done":
            continue
        tm = re.search(r"(?m)^type:\s*(\S+)", head)
        if not tm or tm.group(1) != "review":
            continue
        best_path = path
    if best_path:
        m = re.search(r"review-([0-9a-f]{8})$", best_path.stem)
        if m:
            return m.group(1)
    return None


def detect_c_review_issue(context: dict[str, Any]) -> bool:
    """Trigger: fire when coverage >= target and queue is empty.

    :param context: Trigger context dict.
    :returns: True if a review issue should be created.
    """
    target_dir = Path(context.get("target_dir", "."))
    target_config = context.get("target_config", {})

    if _open_issue_count(target_dir) > 0:
        return False
    if not _a1_is_pass(target_dir):
        return False
    pct = _a7_coverage_pct(target_dir)
    if pct is None or pct < COVERAGE_TARGET:
        return False

    default_branch = target_config.get("default_branch", "main")
    sha = integration_head_sha(target_dir, default_branch)
    if not sha:
        return False

    issue_id = f"review-{sha[:8]}"
    existing = _find_issue_by_id(target_dir, issue_id)
    if existing is not None:
        try:
            head = existing.read_text(encoding="utf-8")[:800]
            sm = re.search(r"(?m)^status:\s*(\S+)", head)
            if sm and sm.group(1) != "discarded":
                return False
        except OSError:
            pass

    # Generation cap
    review_gen = _count_done_review_issues(target_dir) + 1
    max_gens = target_config.get("max_review_generations", MAX_REVIEW_GENERATIONS)
    return not (max_gens > 0 and review_gen > max_gens)


def run_c_review_issue(action: ActionSpec, context: TickContext) -> ActionResult:
    """Action: create a review issue file.

    :param action: Action spec.
    :param context: Tick context.
    :returns: ActionResult.
    """
    if context.dry_run:
        return ActionResult(success=True, dry_run=True)

    target_dir = context.target_dir
    target_config = context.target_config
    repo_name = context.target
    default_branch = target_config.get("default_branch", "main")
    sha = integration_head_sha(target_dir, default_branch)
    if sha is None:
        return ActionResult(success=False, stderr="Failed to determine integration head SHA")
    issue_id = f"review-{sha[:8]}"

    review_gen = _count_done_review_issues(target_dir) + 1
    prev_sha = _find_previous_review_sha(target_dir)

    extra: list[tuple[str, Any]] = [("review_generation", review_gen)]
    if prev_sha:
        extra.append(("previous_review_sha", prev_sha))

    if review_gen == 1 or not prev_sha:
        scope = "- Review the full tree, not just recent changes."
    else:
        scope = (
            f"- Review the **changes** since the previous review at "
            f"`{prev_sha}`. Use `git diff {prev_sha}..{sha}` to see what "
            f"has changed."
        )

    title = f"Code review of {repo_name} @ {sha[:8]}"
    body = (
        f"## Review Scope\n\n{scope}\n\n"
        f"## Instructions\n\n"
        f"Review the codebase and file issues for any problems found. "
        f"Use the CodeReviewAgent to perform a thorough review.\n\n"
        f"## Source\n\n"
        f"Generated by SWE pipeline (Phase C review check).\n"
    )
    _write_pipeline_issue(
        target_dir, repo_name, issue_id, "review", title, body,
        ["review", "pipeline"], extra=extra,
    )
    return ActionResult(success=True, data={"issue_id": issue_id, "review_generation": review_gen})


# ─── C-doc-sync: Queue DocumentationSyncAgent ───────────────────────────────


def detect_c_doc_sync(context: dict[str, Any]) -> bool:
    """Trigger: fire when documentation needs syncing before PR.

    :param context: Trigger context dict.
    :returns: True if doc sync should be queued.
    """
    target_dir = Path(context.get("target_dir", "."))
    target_config = context.get("target_config", {})

    if _open_issue_count(target_dir) > 0:
        return False
    if not _a1_is_pass(target_dir):
        return False
    pct = _a7_coverage_pct(target_dir)
    if pct is None or pct < COVERAGE_TARGET:
        return False

    default_branch = target_config.get("default_branch", "main")
    sha = integration_head_sha(target_dir, default_branch)
    if not sha:
        return False

    # Check integration is ahead of default
    behind = subprocess.run(
        ["git", "-C", str(target_dir), "rev-list", "--count",
         f"{default_branch}..{INTEGRATION_BRANCH}"],
        capture_output=True, text=True, check=False,
    )  # nosec B603 - git with fixed args
    try:
        ahead_by = int((behind.stdout or "").strip() or "0")
    except ValueError:
        ahead_by = 0
    if ahead_by == 0:
        return False

    if (target_config.get("delivery") or "").strip() != "open_pr":
        return False

    token = _load_github_token(target_config)
    if not token:
        return False

    slug = (target_config.get("slug") or "").strip()
    if "/" not in slug:
        return False

    # Idempotency: already synced for this SHA?
    done_marker = target_dir / SWE_SUBDIR / DOC_SYNC_MARKER
    if done_marker.exists():
        try:
            data = json.loads(done_marker.read_text(encoding="utf-8"))
            if data.get("sha") == sha:
                return False
        except (OSError, json.JSONDecodeError):
            pass

    # Re-queue guard
    queued_marker = target_dir / SWE_SUBDIR / DOC_SYNC_QUEUED_MARKER
    if queued_marker.exists():
        try:
            data = json.loads(queued_marker.read_text(encoding="utf-8"))
            queued_at = datetime.fromisoformat(data.get("queued_at", ""))
            age_mins = (datetime.now(timezone.utc) - queued_at).total_seconds() / 60
            if age_mins > 30:
                queued_marker.unlink(missing_ok=True)
            else:
                return False
        except (OSError, json.JSONDecodeError, ValueError):
            queued_marker.unlink(missing_ok=True)

    return True


def run_c_doc_sync(action: ActionSpec, context: TickContext) -> ActionResult:
    """Action: queue DocumentationSyncAgent.

    :param action: Action spec with queue params.
    :param context: Tick context.
    :returns: ActionResult.
    """
    if context.dry_run:
        return ActionResult(success=True, dry_run=True)

    target_dir = context.target_dir
    target_config = context.target_config
    repo_name = context.target
    default_branch = target_config.get("default_branch", "main")
    sha = integration_head_sha(target_dir, default_branch)
    if sha is None:
        return ActionResult(success=False, stderr="Failed to determine integration head SHA")
    pr_exists = (target_dir / SWE_SUBDIR / "pr_published.json").exists()

    # Checkout integration branch
    try:
        _git(target_dir, "checkout", INTEGRATION_BRANCH)
    except subprocess.CalledProcessError:
        return ActionResult(success=False, stderr=f"Failed to checkout {INTEGRATION_BRANCH}")

    prompt = _build_doc_sync_prompt(target_dir, repo_name, default_branch, sha, pr_exists)

    # Queue via conversation queue handler
    from cronpypeline.plugins.swe_prompts import _build_queue_handler
    params = action.params
    handler = _build_queue_handler(params)
    queue_action = ActionSpec(
        type=action.type,
        params={
            "agent": "DocumentationSyncAgent",
            "prompt": prompt,
        },
    )
    result = handler.execute(queue_action, context)
    if not result.success:
        return result

    # Write queued marker
    queued_marker = target_dir / SWE_SUBDIR / DOC_SYNC_QUEUED_MARKER
    queued_marker.parent.mkdir(parents=True, exist_ok=True)
    queued_marker.write_text(json.dumps({
        "sha": sha,
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")

    result.data = {**result.data, "async": True}
    return result


# ─── C-publish: Push integration and open PR ────────────────────────────────


def detect_c_pr_publish(context: dict[str, Any]) -> bool:
    """Trigger: fire when pipeline is complete and PR should be published.

    :param context: Trigger context dict.
    :returns: True if PR should be published.
    """
    target_dir = Path(context.get("target_dir", "."))
    target_config = context.get("target_config", {})

    if _open_issue_count(target_dir) > 0:
        return False
    if not _a1_is_pass(target_dir):
        return False
    pct = _a7_coverage_pct(target_dir)
    if pct is None or pct < COVERAGE_TARGET:
        return False

    default_branch = target_config.get("default_branch", "main")
    sha = integration_head_sha(target_dir, default_branch)
    if not sha:
        return False

    # Integration must be ahead of default
    behind = subprocess.run(
        ["git", "-C", str(target_dir), "rev-list", "--count",
         f"{default_branch}..{INTEGRATION_BRANCH}"],
        capture_output=True, text=True, check=False,
    )  # nosec B603 - git with fixed args
    try:
        ahead_by = int((behind.stdout or "").strip() or "0")
    except ValueError:
        ahead_by = 0
    if ahead_by == 0:
        return False

    # No existing PR
    pr_marker = target_dir / SWE_SUBDIR / "pr_published.json"
    if pr_marker.exists():
        try:
            pr_data = json.loads(pr_marker.read_text(encoding="utf-8"))
            if pr_data.get("pr_number"):
                return False
        except (OSError, json.JSONDecodeError):
            pass

    if (target_config.get("delivery") or "").strip() != "open_pr":
        return False

    token = _load_github_token(target_config)
    if not token:
        return False

    slug = (target_config.get("slug") or "").strip()
    if "/" not in slug:
        return False

    # Doc sync must be done for this SHA
    doc_sync_marker = target_dir / SWE_SUBDIR / DOC_SYNC_MARKER
    if not doc_sync_marker.exists():
        return False
    try:
        ds_data = json.loads(doc_sync_marker.read_text(encoding="utf-8"))
        if ds_data.get("sha") != sha:
            return False
    except (OSError, json.JSONDecodeError):
        return False

    return True


def run_c_pr_publish(action: ActionSpec, context: TickContext) -> ActionResult:
    """Action: push integration branch and create PR via GitHub API.

    :param action: Action spec.
    :param context: Tick context.
    :returns: ActionResult.
    """
    if context.dry_run:
        return ActionResult(success=True, dry_run=True)

    target_dir = context.target_dir
    target_config = context.target_config
    repo_name = context.target
    default_branch = target_config.get("default_branch", "main")
    sha = integration_head_sha(target_dir, default_branch)

    token = _load_github_token(target_config)
    if token is None:
        return ActionResult(success=False, stderr="No GitHub token configured")
    slug = (target_config.get("slug") or "").strip()
    owner, gh_repo_name = slug.split("/", 1)

    # Push integration branch
    try:
        push_result = subprocess.run(
            ["git", "-C", str(target_dir), "push", "--set-upstream", "origin", INTEGRATION_BRANCH],
            capture_output=True, text=True, timeout=120, check=False,
        )  # nosec B603 - git push with fixed args
    except subprocess.TimeoutExpired:
        return ActionResult(success=False, stderr="Git push timed out")

    if push_result.returncode != 0:
        return ActionResult(success=False, stderr=f"Git push failed: {push_result.stderr}")

    # Create PR
    title = f"SWE Pipeline: Automated code improvements for {repo_name}"
    body = _build_pr_body(target_dir, repo_name, default_branch)
    pr_data = _gh_api_post(
        owner, gh_repo_name, "pulls",
        {"title": title, "head": INTEGRATION_BRANCH, "base": default_branch, "body": body},
        token,
    )
    if pr_data is None:
        return ActionResult(success=False, stderr="Failed to create PR")

    pr_number = pr_data.get("number")
    pr_url = pr_data.get("html_url", "")
    pr_marker = target_dir / SWE_SUBDIR / "pr_published.json"
    pr_marker.parent.mkdir(parents=True, exist_ok=True)
    pr_marker.write_text(json.dumps({
        "sha": sha,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "published_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")

    return ActionResult(success=True, data={"pr_number": pr_number, "pr_url": pr_url})


# ─── C-pr-review: Queue PRReviewAgent ───────────────────────────────────────


def detect_c_pr_review(context: dict[str, Any]) -> bool:
    """Trigger: fire when a PR is published but not yet reviewed.

    :param context: Trigger context dict.
    :returns: True if PR review agent should be queued.
    """
    target_dir = Path(context.get("target_dir", "."))
    target_config = context.get("target_config", {})

    pr_marker = target_dir / SWE_SUBDIR / "pr_published.json"
    if not pr_marker.exists():
        return False
    try:
        pr_data = json.loads(pr_marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    pr_number = pr_data.get("pr_number")
    if not pr_number:
        return False

    # Already reviewed for this PR?
    reviewed_marker = target_dir / SWE_SUBDIR / "pr_reviewed.json"
    if reviewed_marker.exists():
        try:
            reviewed_data = json.loads(reviewed_marker.read_text(encoding="utf-8"))
            if reviewed_data.get("pr_number") == pr_number:
                return False
        except (OSError, json.JSONDecodeError):
            pass

    # Re-queue guard
    queued_marker = target_dir / SWE_SUBDIR / "pr_review_queued.json"
    if queued_marker.exists():
        try:
            data = json.loads(queued_marker.read_text(encoding="utf-8"))
            queued_at = datetime.fromisoformat(data.get("queued_at", ""))
            age_mins = (datetime.now(timezone.utc) - queued_at).total_seconds() / 60
            if age_mins > 30:
                queued_marker.unlink(missing_ok=True)
            else:
                return False
        except (OSError, json.JSONDecodeError, ValueError):
            queued_marker.unlink(missing_ok=True)

    # Cycle limit
    pr_cycles = pr_data.get("pr_review_cycles", 0)
    max_cycles = target_config.get("max_pr_review_cycles", MAX_PR_REVIEW_CYCLES)
    if max_cycles > 0 and pr_cycles >= max_cycles:
        return False

    if (target_config.get("delivery") or "").strip() != "open_pr":
        return False

    token = _load_github_token(target_config)
    return bool(token)


def run_c_pr_review(action: ActionSpec, context: TickContext) -> ActionResult:
    """Action: queue PRReviewAgent for the published PR.

    :param action: Action spec with queue params.
    :param context: Tick context.
    :returns: ActionResult.
    """
    if context.dry_run:
        return ActionResult(success=True, dry_run=True)

    target_dir = context.target_dir
    target_config = context.target_config
    repo_name = context.target
    default_branch = target_config.get("default_branch", "main")
    sha = integration_head_sha(target_dir, default_branch)

    pr_marker = target_dir / SWE_SUBDIR / "pr_published.json"
    pr_data = json.loads(pr_marker.read_text(encoding="utf-8"))
    pr_number = pr_data["pr_number"]
    pr_cycles = pr_data.get("pr_review_cycles", 0)
    max_cycles = target_config.get("max_pr_review_cycles", MAX_PR_REVIEW_CYCLES)

    if sha is None:
        return ActionResult(success=False, stderr="Failed to determine integration head SHA")

    slug = (target_config.get("slug") or "").strip()
    owner, gh_repo_name = slug.split("/", 1)

    prompt = _build_pr_review_prompt(
        target_dir, repo_name, default_branch, sha,
        pr_number, owner, gh_repo_name,
        review_cycle=pr_cycles + 1, max_cycles=max_cycles,
    )

    # Queue via conversation queue handler
    from cronpypeline.plugins.swe_prompts import _build_queue_handler
    params = action.params
    handler = _build_queue_handler(params)
    queue_action = ActionSpec(
        type=action.type,
        params={
            "agent": "PRReviewAgent",
            "prompt": prompt,
        },
    )
    result = handler.execute(queue_action, context)
    if not result.success:
        return result

    # Write queued marker
    queued_marker = target_dir / SWE_SUBDIR / "pr_review_queued.json"
    queued_marker.parent.mkdir(parents=True, exist_ok=True)
    queued_marker.write_text(json.dumps({
        "pr_number": pr_number,
        "sha": sha,
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")

    result.data = {**result.data, "async": True}
    return result


def sync_session_mode(context: dict[str, Any], mode_file: str | None = None) -> bool:
    """Pre-tick hook: sync .SWE/github_session.json to the pipeline mode_file.

    Reads the GitHub session file from the target's ``.SWE`` directory. If the session
    is active, writes ``{"mode": "github"}`` to the mode_file. Otherwise writes
    ``{"mode": "default"}``.

    The mode_file path can be passed explicitly or resolved from target_config.

    :param context: Hook context dict with ``target_dir`` and ``target_config``.
    :param mode_file: Optional explicit path to the mode file.
    :returns: True to allow the tick to proceed.
    """
    target_dir = Path(context.get("target_dir", "."))
    session_file = target_dir / ".SWE" / "github_session.json"

    # Resolve mode_file path
    if mode_file is None:
        target_config = context.get("target_config", {})
        mode_file = target_config.get("mode_file")

    if mode_file is None:
        return True  # No mode_file configured, nothing to sync

    mode_path = Path(mode_file)

    # Determine mode from session file
    mode = "default"
    if session_file.exists():
        try:
            session_data = json.loads(session_file.read_text())
            if session_data.get("active") is True:
                mode = "github"
        except (json.JSONDecodeError, OSError):
            pass

    # Write mode file
    mode_path.parent.mkdir(parents=True, exist_ok=True)
    mode_path.write_text(json.dumps({"mode": mode}))

    return True
