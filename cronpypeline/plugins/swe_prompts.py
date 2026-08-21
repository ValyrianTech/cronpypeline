"""SWE prompt builder custom action callables.

Provides:
- `build_fix_prompt`: Build a prompt from a diagnostic report for fix agents.
- `build_coder_prompt`: Build a prompt from an issue for coder agents.
- `build_review_prompt`: Build a prompt for review agents with PR/diff context.
- `queue_fix_agent`: Custom action that builds a fix prompt and queues it.
- `queue_coder_agent`: Custom action that builds a coder prompt and queues it.
- `queue_review_agent`: Custom action that builds a review prompt and queues it.
"""

import subprocess
from pathlib import Path
from typing import Any, Optional

from cronpypeline.actions import ActionResult, TickContext
from cronpypeline.config import ActionSpec
from cronpypeline.plugins.conversation_queue import ConversationQueueHandler
from cronpypeline.plugins.issue_store import Issue, get_issue


# ─── Prompt builders ────────────────────────────────────────────────────────


def build_fix_prompt(
    report_content: str,
    report_name: str,
    target: str,
    extra_instructions: str = "",
) -> str:
    """Build a prompt for a fix agent from a diagnostic report."""
    prompt = f"""You are working on repository: {target}

A diagnostic report ({report_name}) has identified issues that need fixing.

## Report Contents

{report_content}
"""
    if extra_instructions:
        prompt += f"\n## Additional Instructions\n\n{extra_instructions}\n"
    prompt += """
## Task

Review the report above and fix all identified issues. Make the minimal necessary changes to resolve each problem. After fixing, verify your changes work correctly.
"""
    return prompt


def build_coder_prompt(
    issue: Issue,
    target: str,
    integration_sha: str = "",
    extra_instructions: str = "",
) -> str:
    """Build a prompt for a coder agent from an issue."""
    prompt = f"""You are working on repository: {target}

## Issue Details

- **Issue ID**: {issue.id}
- **Source**: {issue.source or 'unknown'}
- **Type**: {issue.type or 'unknown'}
- **Status**: {issue.status}
- **Attempts**: {issue.attempts}
- **Repo**: {issue.repo or target}
"""
    if issue.labels:
        prompt += f"- **Labels**: {', '.join(issue.labels)}\n"
    if issue.github_url:
        prompt += f"- **GitHub URL**: {issue.github_url}\n"

    prompt += f"""
## Issue Description

{issue.body}
"""
    if integration_sha:
        prompt += f"\n## Integration Branch\n\nCurrent HEAD: `{integration_sha}`\n"

    if extra_instructions:
        prompt += f"\n## Additional Instructions\n\n{extra_instructions}\n"

    prompt += """
## Task

Create a new branch from the integration branch, implement the fix for this issue, and write a completion marker when done. Follow the project's coding standards.
"""
    return prompt


def build_review_prompt(
    target: str,
    cycle_number: int = 0,
    diff_stats: str = "",
    integration_sha: str = "",
    pr_number: Optional[int] = None,
    pr_url: str = "",
    extra_instructions: str = "",
) -> str:
    """Build a prompt for a review agent."""
    prompt = f"""You are reviewing changes in repository: {target}

## Review Context

- **Cycle**: {cycle_number}
"""
    if pr_number is not None:
        prompt += f"- **PR Number**: {pr_number}\n"
    if pr_url:
        prompt += f"- **PR URL**: {pr_url}\n"
    if integration_sha:
        prompt += f"- **Integration SHA**: `{integration_sha}`\n"
    if diff_stats:
        prompt += f"- **Diff Stats**: {diff_stats}\n"

    prompt += """
## Task

Review the changes in this PR. Check for:
1. Code quality and adherence to project standards
2. Correctness of the implementation
3. Test coverage for new code
4. Any potential regressions or side effects

Provide your review as structured feedback. If the changes are acceptable, approve the PR. If not, request changes with specific, actionable feedback.
"""
    if extra_instructions:
        prompt += f"\n## Additional Instructions\n\n{extra_instructions}\n"
    return prompt


# ─── Git helpers ────────────────────────────────────────────────────────────


def _get_integration_sha(target_dir: Path) -> str:
    """Get the current HEAD SHA of the integration branch."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return ""


def _get_diff_stats(target_dir: Path) -> str:
    """Get diff stats for the current branch vs integration."""
    try:
        result = subprocess.run(
            ["git", "diff", "--stat", "integration"],
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return ""


# ─── Queue action callables ─────────────────────────────────────────────────


def _build_queue_handler(params: dict[str, Any]) -> ConversationQueueHandler:
    """Build a ConversationQueueHandler from action params."""
    return ConversationQueueHandler(
        queue_dir=params.get("queue_dir", ""),
        agent_settings_dir=params.get("agent_settings_dir"),
        prompt_field=params.get("prompt_field", "prompt"),
        default_fields=params.get("default_fields"),
        flatten_agent_settings=params.get("flatten_agent_settings", False),
    )


def queue_fix_agent(action: ActionSpec, context: TickContext) -> ActionResult:
    """Custom action: build a fix prompt from a report and queue it.

    Expected action.params:
        - report_path: Path to the diagnostic report file
        - agent: Agent name to queue
        - queue_dir: Queue directory
        - agent_settings_dir: Optional agent settings directory
        - prompt_field: Field name for prompt (default "prompt")
        - default_fields: Static fields for queue entry
        - flatten_agent_settings: Whether to flatten agent settings
        - extra_instructions: Optional extra instructions for the prompt
    """
    if context.dry_run:
        return ActionResult(success=True, dry_run=True)

    params = action.params
    report_path = Path(params.get("report_path", ""))

    if not report_path.exists():
        return ActionResult(
            success=False,
            stderr=f"Report file not found: {report_path}",
        )

    report_content = report_path.read_text()
    report_name = report_path.name

    prompt = build_fix_prompt(
        report_content=report_content,
        report_name=report_name,
        target=context.target,
        extra_instructions=params.get("extra_instructions", ""),
    )

    # Build a queue action spec and dispatch via ConversationQueueHandler
    handler = _build_queue_handler(params)
    queue_action = ActionSpec(
        type=action.type,
        params={
            "agent": params.get("agent", "default"),
            "prompt": prompt,
        },
    )
    return handler.execute(queue_action, context)


def queue_coder_agent(action: ActionSpec, context: TickContext) -> ActionResult:
    """Custom action: build a coder prompt from an issue and queue it.

    Expected action.params:
        - issue_id: ID of the issue to work on
        - agent: Agent name to queue
        - queue_dir: Queue directory
        - agent_settings_dir: Optional agent settings directory
        - prompt_field: Field name for prompt (default "prompt")
        - default_fields: Static fields for queue entry
        - flatten_agent_settings: Whether to flatten agent settings
        - extra_instructions: Optional extra instructions for the prompt
    """
    if context.dry_run:
        return ActionResult(success=True, dry_run=True)

    params = action.params
    issue_id = params.get("issue_id")

    issue = get_issue(context.target_dir, issue_id)
    if issue is None:
        return ActionResult(
            success=False,
            stderr=f"Issue not found: {issue_id}",
        )

    integration_sha = _get_integration_sha(context.target_dir)

    prompt = build_coder_prompt(
        issue=issue,
        target=context.target,
        integration_sha=integration_sha,
        extra_instructions=params.get("extra_instructions", ""),
    )

    handler = _build_queue_handler(params)
    queue_action = ActionSpec(
        type=action.type,
        params={
            "agent": params.get("agent", "default"),
            "prompt": prompt,
        },
    )
    return handler.execute(queue_action, context)


def queue_review_agent(action: ActionSpec, context: TickContext) -> ActionResult:
    """Custom action: build a review prompt and queue it.

    Expected action.params:
        - agent: Agent name to queue
        - queue_dir: Queue directory
        - agent_settings_dir: Optional agent settings directory
        - prompt_field: Field name for prompt (default "prompt")
        - default_fields: Static fields for queue entry
        - flatten_agent_settings: Whether to flatten agent settings
        - cycle_number: Review cycle number
        - pr_number: Optional PR number
        - pr_url: Optional PR URL
        - extra_instructions: Optional extra instructions for the prompt
    """
    if context.dry_run:
        return ActionResult(success=True, dry_run=True)

    params = action.params

    integration_sha = _get_integration_sha(context.target_dir)
    diff_stats = _get_diff_stats(context.target_dir)

    prompt = build_review_prompt(
        target=context.target,
        cycle_number=params.get("cycle_number", 0),
        diff_stats=diff_stats,
        integration_sha=integration_sha,
        pr_number=params.get("pr_number"),
        pr_url=params.get("pr_url", ""),
        extra_instructions=params.get("extra_instructions", ""),
    )

    handler = _build_queue_handler(params)
    queue_action = ActionSpec(
        type=action.type,
        params={
            "agent": params.get("agent", "default"),
            "prompt": prompt,
        },
    )
    return handler.execute(queue_action, context)
