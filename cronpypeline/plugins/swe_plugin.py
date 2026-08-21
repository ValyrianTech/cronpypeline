"""SWE pipeline custom triggers and actions plugin.

Provides custom trigger callables and action handlers specific to the SWE pipeline,
referenced from pipeline JSON configs via "callable": "cronpypeline.plugins.swe_plugin.xxx".
"""

import json
import subprocess
from pathlib import Path
from typing import Any

from cronpypeline.actions import ActionSpec, TickContext
from cronpypeline.plugins.issue_store import load_issues, set_issue_status


def detect_open_issue(context: dict[str, Any]) -> bool:
    """Trigger: detect if there's an open issue to work on.

    Scans ``.SWE/issues/*.md`` files with YAML frontmatter for ``status: open``.

    :param context: Trigger context dict with ``target_dir``.
    :returns: True if at least one open issue exists.
    """
    target_dir = Path(context.get("target_dir", "."))
    issues = load_issues(target_dir)
    return any(issue.status == "open" for issue in issues)


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
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-1"],
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
            subprocess.run(cmd, cwd=str(target_dir), capture_output=True, timeout=30, check=False)
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
