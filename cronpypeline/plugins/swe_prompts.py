"""SWE prompt builder custom action callables.

Provides:
- `build_fix_prompt`: Build a prompt from a diagnostic report for fix agents.
- `build_coder_prompt`: Build a prompt from an issue for coder agents.
- `build_review_prompt`: Build a prompt for review agents with PR/diff context.
- `queue_fix_agent`: Custom action that builds a fix prompt and queues it.
- `queue_coder_agent`: Custom action that builds a coder prompt and queues it.
- `queue_review_agent`: Custom action that builds a review prompt and queues it.
"""

import shlex
import shutil
import subprocess  # nosec B404 - subprocess is used by design to run git commands for prompt building
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cronpypeline.actions import ActionResult, TickContext
from cronpypeline.config import ActionSpec
from cronpypeline.plugins.conversation_queue import ConversationQueueHandler
from cronpypeline.plugins.issue_store import Issue, get_issue
from cronpypeline.plugins.swe_plugin import (
    PHASE_A_BRANCH,
    PHASE_A_GIT_AUTHOR_EMAIL,
    PHASE_A_GIT_AUTHOR_NAME,
)

# ─── Prompt builders ────────────────────────────────────────────────────────


def build_fix_prompt(
    report_content: str,
    report_name: str,
    target: str,
    extra_instructions: str = "",
) -> str:
    """Build a prompt for a fix agent from a diagnostic report.

    :param report_content: Full text content of the diagnostic report.
    :param report_name: Filename of the report (for reference in the prompt).
    :param target: Target repository name.
    :param extra_instructions: Optional additional instructions to append.
    :returns: Formatted prompt string.
    """
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
    """Build a prompt for a coder agent from an issue.

    :param issue: Issue object with details and description.
    :param target: Target repository name.
    :param integration_sha: Current HEAD SHA of the integration branch.
    :param extra_instructions: Optional additional instructions to append.
    :returns: Formatted prompt string.
    """
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
    pr_number: int | None = None,
    pr_url: str = "",
    extra_instructions: str = "",
) -> str:
    """Build a prompt for a review agent.

    :param target: Target repository name.
    :param cycle_number: Review cycle number.
    :param diff_stats: Git diff stats string.
    :param integration_sha: Current HEAD sha of the integration branch.
    :param pr_number: Optional PR number.
    :param pr_url: Optional PR URL.
    :param extra_instructions: Optional additional instructions to append.
    :returns: Formatted prompt string.
    """
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
    """Get the current HEAD SHA of the integration branch.

    :param target_dir: Target directory to run git in.
    :returns: Short SHA string, or empty string on failure.
    """
    git_bin = shutil.which("git")
    if git_bin is None:
        return ""
    try:
        result = subprocess.run(  # nosec B603 - git_bin is resolved to an absolute path via shutil.which; args are a static list
            [git_bin, "rev-parse", "--short", "HEAD"],
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return ""


def _get_diff_stats(target_dir: Path) -> str:
    """Get diff stats for the current branch vs integration.

    :param target_dir: Target directory to run git in.
    :returns: Diff stats string, or empty string on failure.
    """
    git_bin = shutil.which("git")
    if git_bin is None:
        return ""
    try:
        result = subprocess.run(  # nosec B603 - git_bin is resolved to an absolute path via shutil.which; args are a static list
            [git_bin, "diff", "--stat", "integration"],
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return ""


# ─── Queue action callables ─────────────────────────────────────────────────


def _build_queue_handler(params: dict[str, Any], context: TickContext) -> ConversationQueueHandler:
    """Build a ConversationQueueHandler from action params, falling back to pipeline config.

    Stage action params take precedence; missing keys fall back to the pipeline's
    top-level ``action_handler`` config so that ``queue_dir``, ``prompt_field``,
    ``default_fields``, etc. don't need to be repeated in every stage.

    :param params: Action params dict with optional queue settings.
    :param context: Tick context with pipeline reference for fallback.
    :returns: A :class:`ConversationQueueHandler` instance.
    :raises ValueError: If queue_dir or agent_settings_dir is missing, empty, or
        contains path traversal (``..``) segments.
    """
    fallback: dict[str, Any] = {}
    pipeline = getattr(context, "pipeline", None)
    if pipeline is not None and pipeline.config.action_handler is not None:
        fallback = dict(pipeline.config.action_handler.params)
    merged = {**fallback, **{k: v for k, v in params.items() if v is not None}}
    queue_dir = merged.get("queue_dir", "")
    if not queue_dir:
        raise ValueError("queue_dir is required for ConversationQueueHandler")
    handler = ConversationQueueHandler(
        queue_dir=queue_dir,
        agent_settings_dir=merged.get("agent_settings_dir"),
        prompt_field=merged.get("prompt_field", "prompt"),
        default_fields=merged.get("default_fields"),
        flatten_agent_settings=merged.get("flatten_agent_settings", False),
    )
    qd = Path(handler.queue_dir)
    if not str(qd).strip():
        raise ValueError(f"queue_dir is required: {handler.queue_dir}")
    if ".." in qd.parts:
        raise ValueError(f"queue_dir contains path traversal: {handler.queue_dir}")
    if handler.agent_settings_dir is not None:
        asd = Path(handler.agent_settings_dir)
        if not str(asd).strip():
            raise ValueError(f"agent_settings_dir is required: {handler.agent_settings_dir}")
        if ".." in asd.parts:
            raise ValueError(f"agent_settings_dir contains path traversal: {handler.agent_settings_dir}")
    return handler


def queue_fix_agent(action: ActionSpec, context: TickContext) -> ActionResult:
    """Build a fix prompt from a report and queue it.

    Expected action.params:
        - report_path: Path to the diagnostic report (symlink or direct)
        - agent: Agent name to queue (e.g. "FixLintingAgent")
        - queue_dir: Queue directory
        - agent_settings_dir: Optional agent settings directory
        - prompt_field: Field name for prompt (default "prompt")
        - default_fields: Static fields for queue entry
        - flatten_agent_settings: Whether to flatten agent settings
        - extra_instructions: Optional extra instructions for the prompt
        - invalidate_paths: List of paths the agent should delete after committing
        - completion_marker: Path the agent should write as its LAST step
        - commit_message: Git commit message for the agent to use

    :param action: Action spec with report_path and queue params.
    :param context: Tick context with target and directories.
    :returns: Result from the queue handler.
    """
    if context.dry_run:
        return ActionResult(success=True, dry_run=True)

    params = action.params
    report_path_raw = context.target_dir / params.get("report_path", "")

    # Resolve symlink to get the actual report file
    report_path = report_path_raw
    if report_path_raw.is_symlink():
        try:
            report_path = report_path_raw.resolve()
        except OSError:
            pass
    if not report_path.exists():
        return ActionResult(
            success=False,
            stderr=f"Report file not found: {report_path_raw}",
        )

    report_content = report_path.read_text(encoding="utf-8")
    report_name = report_path.name

    # Build prompt with report content + commit/delete/completion instructions
    repo_name = context.target
    target_dir = context.target_dir
    extra_instructions = params.get("extra_instructions", "")
    verify_commands = params.get("verify_commands", [])
    invalidate_paths = params.get("invalidate_paths", [])
    completion_marker = params.get("completion_marker", "")
    commit_message = params.get("commit_message", "fix: resolve diagnostic issues")

    prompt = f"""You are working on repository: {repo_name}

A diagnostic report ({report_name}) has identified issues that need fixing.

## First: plan with the Progress tool

Before you start making changes, use the **Progress** tool to record your task list — e.g. 'understand the report', 'plan the fixes', 'apply fixes', 'run verification', 'commit changes', 'delete latest.md files'. Keep it updated as you go: mark each task 'in_progress' when you start it and 'completed' when done, so your progress is tracked across turns.

## Report Contents

{report_content}
"""
    if extra_instructions:
        prompt += f"\n## Additional Instructions\n\n{extra_instructions}\n"

    prompt += f"""
## Task

Use the OpenCode tool to delegate the actual editing work. The repo_name to pass to OpenCode is '{repo_name}'. Phrase the OpenCode prompt as a self-contained goal that addresses every issue listed in the report below, preferring minimal diffs and preserving intentional side-effecting calls.

After OpenCode finishes, verify the result with RunCommand. IMPORTANT: you MUST `cd` into the repo first or pytest will collect zero tests.
"""
    if verify_commands:
        prompt += "Run exactly:\n"
        for cmd in verify_commands:
            prompt += f"  cd {shlex.quote(str(target_dir))} && {cmd}\n"
    prompt += "\n"

    prompt += f"""
## Git Workflow

You are on branch `{PHASE_A_BRANCH}`. After making your changes:

1. Commit your changes:
   cd {shlex.quote(str(target_dir))} && git add -A && \\
   git -c user.name='{PHASE_A_GIT_AUTHOR_NAME}' \\
   -c user.email='{PHASE_A_GIT_AUTHOR_EMAIL}' \\
   commit -m "{commit_message}"
"""
    if invalidate_paths:
        prompt += "\n2. After committing, delete these files so the pipeline re-runs the upstream stages:\n"
        for p in invalidate_paths:
            prompt += f"   rm -f {p}\n"

    if completion_marker:
        prompt += f"""
3. As your FINAL step, write the completion marker using WriteFile:
   Path: {completion_marker}
   Content: {{"completed_at": "<current ISO timestamp>"}}
   Replace <current ISO timestamp> with the actual current time in ISO format.
"""

    # Build a queue action spec and dispatch via ConversationQueueHandler
    handler = _build_queue_handler(params, context)
    queue_action = ActionSpec(
        type=action.type,
        params={
            "agent": params.get("agent", "default"),
            "prompt": prompt,
        },
    )
    result = handler.execute(queue_action, context)
    if not result.success:
        return result

    # Write deduplication marker only after successful queue. If the marker
    # write fails (e.g. disk full, permission error), remove the queued entry
    # so the report is not queued without a dedup marker (double-queue risk).
    try:
        markers_dir = context.target_dir / ".SWE" / "markers"
        markers_dir.mkdir(parents=True, exist_ok=True)
        dedup_marker = markers_dir / f"queued_for_{report_path.stem}.marker"
        dedup_marker.write_text(
            f"queued at {datetime.now(timezone.utc).isoformat()} "
            f"against report {report_name}\n",
            encoding="utf-8",
        )
    except OSError as e:
        # Best-effort cleanup: remove the queued entry to avoid a double-queue.
        queue_file = result.data.get("queue_file")
        if queue_file:
            try:
                Path(queue_file).unlink(missing_ok=True)
            except OSError:
                pass
        return ActionResult(
            success=False,
            stderr=f"Failed to write dedup marker after queueing: {e}",
        )

    result.data = {**result.data, "async": True}
    return result


def queue_coder_agent(action: ActionSpec, context: TickContext) -> ActionResult:
    """Build a coder prompt from an issue and queue it.

    Expected action.params:
        - issue_id: ID of the issue to work on
        - agent: Agent name to queue
        - queue_dir: Queue directory
        - agent_settings_dir: Optional agent settings directory
        - prompt_field: Field name for prompt (default "prompt")
        - default_fields: Static fields for queue entry
        - flatten_agent_settings: Whether to flatten agent settings
        - extra_instructions: Optional extra instructions for the prompt

    :param action: Action spec with issue_id and queue params.
    :param context: Tick context with target and directories.
    :returns: Result from the queue handler.
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

    handler = _build_queue_handler(params, context)
    queue_action = ActionSpec(
        type=action.type,
        params={
            "agent": params.get("agent", "default"),
            "prompt": prompt,
        },
    )
    result = handler.execute(queue_action, context)
    if result.success:
        result.data = {**result.data, "async": True}
    return result


def queue_review_agent(action: ActionSpec, context: TickContext) -> ActionResult:
    """Build a review prompt and queue it.

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

    :param action: Action spec with review and queue params.
    :param context: Tick context with target and directories.
    :returns: Result from the queue handler.
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

    handler = _build_queue_handler(params, context)
    queue_action = ActionSpec(
        type=action.type,
        params={
            "agent": params.get("agent", "default"),
            "prompt": prompt,
        },
    )
    result = handler.execute(queue_action, context)
    if result.success:
        result.data = {**result.data, "async": True}
    return result


def _parse_change_requests(body: str) -> list[str]:
    """Extract individual change requests from a PR review body.

    Extracts numbered items from the 'Issues & Concerns' section. Falls back
    to generic numbered/bullet extraction for reviews written outside the pipeline.

    :param body: PR review body text.
    :returns: List of cleaned, non-empty change request strings.
    """
    import re

    if not body:
        return []

    items: list[str] = []

    # 1) Try the PRReviewAgent's defined structure: extract the
    #    "Issues & Concerns" section and split by numbered sub-items.
    m = re.search(
        r"(?:^|\n)#{1,3}\s*Issues?\s*[&]\s*Concerns?\s*\n"
        r"(.*?)(?=\n##+\s+\w|\Z)",
        body, re.DOTALL | re.IGNORECASE,
    )
    issues_section = m.group(1).strip() if m else None

    if issues_section:
        section_body = re.sub(
            r"^#+\s*Issues?\s*[&]\s*Concerns?\s*", "",
            issues_section, flags=re.IGNORECASE,
        ).strip()

        # Format A: bold-numbered
        match = re.findall(
            r"(?:^|\n)\s*\*\*(\d+[.)]\s+[^\n]+)\*\*\s*\n"
            r"(.*?)(?=\n\s*\*\*\d+[.)]|\n\s*No\s+other|\Z)",
            section_body, re.DOTALL,
        )
        if match:
            for title, content in match:
                items.append(title.strip() + "\n" + content.strip())

        # Format B: heading-numbered
        if not items:
            match = re.findall(
                r"(?:^|\n)\s*#+\s*\d+[.)]\s+(.*?)(?=\n\s*#+\s*\d+[.)]|\Z)",
                section_body, re.DOTALL,
            )
            if match:
                items.extend(item.strip() for item in match if item.strip())

        # Format C: plain-numbered
        if not items:
            match = re.findall(
                r"(?:^|\n)\s*(\d+[.)]\s+)(.*?)(?=\n\s*(?:\d+[.)]|No\s+other)|\Z)",
                section_body, re.DOTALL,
            )
            if match:
                items.extend(item.strip() for _, item in match if item.strip())

    # 2) Generic numbered list
    if not items:
        match = re.findall(
            r"(?:^|\n)\s*(\d+[.)]\s+)(.*?)(?=\n\s*(?:\d+[.)])|\Z)",
            body, re.DOTALL,
        )
        if match:
            items.extend(item.strip() for _, item in match if item.strip())

    # 3) Bullet points
    if not items:
        match = re.findall(
            r"(?:^|\n)\s*[-*+]\s+(.*?)(?=\n\s*[-*+]|$)",
            body, re.DOTALL,
        )
        if match:
            items.extend(item.strip() for item in match if item.strip())

    # 4) Last resort: non-heading paragraphs
    if not items:
        for para in body.split("\n\n"):
            para = para.strip()
            if para and not para.startswith("#"):
                items.append(para)

    # Filter out headings, empty items, "No issues found", boilerplate.
    cleaned = []
    for item in items:
        if not item or item.startswith("#"):
            continue
        if re.match(r"no\s+issues?\s+(?:found|identified)", item, re.IGNORECASE):
            continue
        text = item.strip()
        if not text:  # pragma: no cover - defensive: items are pre-stripped
            continue
        if "\n" in text:
            cleaned.append(text)
        elif len(text) > 200:
            cleaned.append(text[:197] + "...")
        else:
            cleaned.append(text)
    return cleaned
