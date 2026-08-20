"""SWE pipeline custom triggers and actions plugin.

Provides custom trigger callables and action handlers specific to the SWE pipeline,
referenced from pipeline JSON configs via "callable": "cronpypeline.plugins.swe_plugin.xxx".
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Any


def detect_open_issue(context: dict) -> bool:
    """Trigger: detect if there's an open issue to work on.

    Checks for issues with status "open" in the target's issue tracker file.
    """
    target_dir = Path(context.get("target_dir", "."))
    issues_file = target_dir / ".SWE" / "issues.json"

    if not issues_file.exists():
        return False

    try:
        issues = json.loads(issues_file.read_text())
    except (json.JSONDecodeError, OSError):
        return False

    for issue in issues:
        if issue.get("status") == "open":
            return True
    return False


def detect_agent_forgot_marker(context: dict) -> bool:
    """Trigger: detect if agent forgot to write completion marker.

    Fires when: queue is empty + git commits exist on branch but no completion marker.
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
        )
        if result.returncode != 0:
            return False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False

    # Check if queue is empty
    queue_dir = context.get("queue_dir")
    if queue_dir:
        queue_path = Path(queue_dir)
        if queue_path.exists() and any(queue_path.iterfile()):
            return False

    return True


def cleanup_git_branch(action, context):
    """Action: clean up git branch after failure.

    Runs 'git checkout integration && git branch -D {task_branch}'.
    """
    target_dir = context.target_dir
    task_branch = action.params.get("task_branch", "task-branch")

    commands = [
        ["git", "checkout", "integration"],
        ["git", "branch", "-D", task_branch],
    ]

    for cmd in commands:
        try:
            subprocess.run(cmd, cwd=str(target_dir), capture_output=True, timeout=30)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    return True, f"Cleaned up branch {task_branch}"


def reset_issue_status(action, context):
    """Action: reset issue status to 'open' after failure.

    Updates the issue's status in issues.json back to 'open'.
    """
    target_dir = context.target_dir
    issues_file = target_dir / ".SWE" / "issues.json"

    if not issues_file.exists():
        return False, "No issues file found"

    try:
        issues = json.loads(issues_file.read_text())
    except (json.JSONDecodeError, OSError):
        return False, "Could not parse issues file"

    issue_id = action.params.get("issue_id")
    updated = False
    for issue in issues:
        if issue.get("id") == issue_id or issue.get("number") == issue_id:
            issue["status"] = "open"
            updated = True
            break

    if updated:
        issues_file.write_text(json.dumps(issues, indent=2))
        return True, f"Reset issue {issue_id} to open"
    return False, f"Issue {issue_id} not found"
