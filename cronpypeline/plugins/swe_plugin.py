"""SWE pipeline custom triggers and actions plugin.

Provides custom trigger callables and action handlers specific to the SWE pipeline,
referenced from pipeline JSON configs via "callable": "cronpypeline.plugins.swe_plugin.xxx".
"""

import json
import re
import shutil
import subprocess  # nosec B404 - subprocess is used by design to run git commands for pipeline state detection
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cronpypeline.actions import ActionSpec, TickContext
from cronpypeline.plugins.issue_store import load_issues, parse_frontmatter, set_issue_status


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
    except Exception:
        return False
    m_errors = re.search(r'(\d+)\s+error\(s\)', text)
    errors = int(m_errors.group(1)) if m_errors else 0
    m_fixable = re.search(r'(\d+)\s+auto-fixable', text)
    fixable = int(m_fixable.group(1)) if m_fixable else 0
    if errors <= 0 or fixable > 0:
        return False
    marker = target_dir / ".SWE" / "markers" / f"queued_for_{report_path.stem}.marker"
    if marker.exists():
        return False
    return True


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
    except Exception:
        return False
    if "— FAIL" not in first_line:
        return False
    marker = target_dir / ".SWE" / "markers" / f"queued_for_{report_path.stem}.marker"
    if marker.exists():
        return False
    return True


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
    except Exception:
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
    except Exception:
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


def finalize_session(action: ActionSpec, context: TickContext) -> tuple[bool, str]:
    """Action: finalize a completed GitHub session.

    Mirrors the old pipeline's ``detect_c_github_session_terminal`` execute logic:
    - Mark the session as inactive and completed
    - Write completed_at timestamp

    In the full implementation this would also close the GitHub issue via API
    and post a comment, but that requires GitHub API access which is handled
    by the http_request action handler in the cronpypeline config.

    :param action: Action spec (no special params needed).
    :param context: Tick context with target directory.
    :returns: Tuple of (success, message).
    """
    target_dir = context.target_dir
    session_file = target_dir / ".SWE" / "github_session.json"
    if not session_file.exists():
        return False, "No session file found"
    try:
        session = json.loads(session_file.read_text(encoding="utf-8"))
    except Exception:
        return False, "Failed to read session file"
    session["active"] = False
    session["completed"] = True
    session["completed_at"] = datetime.now(timezone.utc).isoformat()
    session_file.write_text(json.dumps(session, indent=2), encoding="utf-8")
    return True, "Session finalized"


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
