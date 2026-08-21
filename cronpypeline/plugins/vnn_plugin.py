"""VNN pipeline plugin — custom triggers, actions, and hooks.

Provides:
- `log_rejection`: post_tick hook for append-only rejection audit trail.
- `queue_empty_global`: pre_tick hook for global queue-empty gate.
- `sync_story_states`: pre_tick hook to sync ranking.json with filesystem state.
- `cleanup_inconsistent_state`: pre_tick hook to resolve marker conflicts.
- `check_completed_compilations`: pre_tick hook to check for completed compilations.
- `cleanup_stale_compilation_markers`: pre_tick hook to remove stale compilation markers.
- `discover_stories`: pre_tick hook to scan story directories and update registry.
- `vnn_pre_tick`: composite pre_tick hook that runs all VNN pre_tick hooks in sequence.
- `vnn_post_tick`: composite post_tick hook that runs all VNN post_tick hooks in sequence.
"""

import json
import time
from pathlib import Path
from typing import Any

from cronpypeline.actions import ActionResult


def log_rejection(context: dict[str, Any], result: ActionResult) -> None:
    """Post-tick hook: append to rejection_log.json when a rejection occurs.

    Checks if a rejection marker exists in the target directory. If so,
    appends a detailed entry to ``.VNN/rejection_log.json`` with timestamp,
    target, stage, rejection count, and reason.

    :param context: Hook context dict with ``target_dir`` and ``target``.
    :param result: Tick result from the completed tick.
    """
    target_dir = Path(context.get("target_dir", "."))
    rejection_marker = target_dir / ".rejection"

    if not rejection_marker.exists():
        return

    # Read rejection marker data
    try:
        rej_data = json.loads(rejection_marker.read_text())
    except (json.JSONDecodeError, OSError):
        rej_data = {}

    # Build log entry
    log_entry = {
        "target": context.get("target", ""),
        "stage_id": context.get("stage_id", ""),
        "timestamp": time.time(),
        "rejection_count": rej_data.get("rejection_count", 0),
        "reason": rej_data.get("reason", result.stderr if result else ""),
        "stderr": result.stderr if result else "",
    }

    # Append to rejection log
    vnn_dir = target_dir / ".VNN"
    vnn_dir.mkdir(parents=True, exist_ok=True)
    log_file = vnn_dir / "rejection_log.json"

    existing_log = []
    if log_file.exists():
        try:
            existing_log = json.loads(log_file.read_text())
            if not isinstance(existing_log, list):
                existing_log = []
        except (json.JSONDecodeError, OSError):
            existing_log = []

    existing_log.append(log_entry)
    log_file.write_text(json.dumps(existing_log, indent=2))


def queue_empty_global(context: dict[str, Any]) -> bool:
    """Pre-tick hook: return False if conversation queue is not empty.

    Acts as a global gate — when the queue has pending entries, no new
    work should be queued.

    :param context: Hook context dict with ``target_config``.
    :returns: True (proceed) when queue is empty, False otherwise.
    """
    target_config = context.get("target_config", {})
    queue_dir = target_config.get("queue_dir")

    if not queue_dir:
        return True

    queue_path = Path(queue_dir)
    if not queue_path.exists():
        return True

    # Check for any .json files in the queue
    if any(queue_path.glob("*.json")):
        return False

    return True


def sync_story_states(context: dict[str, Any]) -> bool:
    """Pre-tick hook: sync ranking.json with filesystem state.

    Scans the target directory for story state markers (article.md,
    published.json, rejected-article.md, etc.) and updates ``.VNN/ranking.json``
    to reflect the current state of each story.

    :param context: Hook context dict with ``target_dir`` and ``target``.
    :returns: True to allow the tick to proceed.
    """
    target_dir = Path(context.get("target_dir", "."))
    target = context.get("target", "")

    vnn_dir = target_dir / ".VNN"
    vnn_dir.mkdir(parents=True, exist_ok=True)
    ranking_file = vnn_dir / "ranking.json"

    # Determine story state from filesystem markers
    story_state = {
        "target": target,
        "has_article": (target_dir / "article.md").exists(),
        "is_published": (target_dir / "published.json").exists(),
        "is_rejected": (target_dir / "rejected-article.md").exists(),
        "is_processing": (target_dir / ".processing").exists(),
        "is_done": (target_dir / "done.md").exists(),
        "is_given_up": (target_dir / ".gave_up").exists(),
    }

    # Load existing ranking
    existing = {}
    if ranking_file.exists():
        try:
            existing = json.loads(ranking_file.read_text())
            if not isinstance(existing, dict):
                existing = {}
        except (json.JSONDecodeError, OSError):
            existing = {}

    # Update ranking with current state
    existing[target] = story_state
    ranking_file.write_text(json.dumps(existing, indent=2))

    return True


def cleanup_inconsistent_state(context: dict[str, Any]) -> bool:
    """Pre-tick hook: resolve marker conflicts.

    If both a processing marker and a completion marker exist, the processing
    marker is stale (agent completed but marker wasn't cleaned up). Remove it.

    :param context: Hook context dict with ``target_dir``.
    :returns: True to allow the tick to proceed.
    """
    target_dir = Path(context.get("target_dir", "."))

    processing = target_dir / ".processing"
    completion = target_dir / "done.md"

    if processing.exists() and completion.exists():
        processing.unlink()

    return True


def check_completed_compilations(context: dict[str, Any]) -> bool:
    """Pre-tick hook: check for completed compilation markers.

    Scans for ``.compilation_complete`` markers and updates story state
    accordingly.

    :param context: Hook context dict with ``target_dir`` and ``target``.
    :returns: True to allow the tick to proceed.
    """
    target_dir = Path(context.get("target_dir", "."))
    target = context.get("target", "")

    compilation_marker = target_dir / ".compilation_complete"
    if not compilation_marker.exists():
        return True

    # Read compilation data
    try:
        comp_data = json.loads(compilation_marker.read_text())
    except (json.JSONDecodeError, OSError):
        comp_data = {}

    # Update story state if needed
    vnn_dir = target_dir / ".VNN"
    vnn_dir.mkdir(parents=True, exist_ok=True)
    state_file = vnn_dir / "compilation_state.json"

    state = {}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except (json.JSONDecodeError, OSError):
            state = {}

    state[target] = {
        "completed": True,
        "timestamp": comp_data.get("timestamp", time.time()),
        "output": comp_data.get("output", ""),
    }
    state_file.write_text(json.dumps(state, indent=2))

    return True


def cleanup_stale_compilation_markers(context: dict[str, Any]) -> bool:
    """Pre-tick hook: remove stale compilation markers.

    Removes ``.compilation_complete`` markers that are older than the configured
    timeout (default 60 minutes).

    :param context: Hook context dict with ``target_dir`` and ``target_config``.
    :returns: True to allow the tick to proceed.
    """
    target_dir = Path(context.get("target_dir", "."))
    target_config = context.get("target_config", {})
    timeout_minutes = target_config.get("compilation_timeout_minutes", 60)

    compilation_marker = target_dir / ".compilation_complete"
    if not compilation_marker.exists():
        return True

    # Check marker age
    try:
        mtime = compilation_marker.stat().st_mtime
        age_minutes = (time.time() - mtime) / 60.0
        if age_minutes > timeout_minutes:
            compilation_marker.unlink()
    except OSError:
        pass

    return True


def discover_stories(context: dict[str, Any]) -> bool:
    """Pre-tick hook: scan story directories and update registry file.

    Scans the workspace directory for story subdirectories and writes
    a registry file that cronpypeline can use as its target list.

    :param context: Hook context dict with ``workspace_dir`` and ``target_config``.
    :returns: True to allow the tick to proceed.
    """
    workspace_dir = Path(context.get("workspace_dir", "."))
    target_config = context.get("target_config", {})
    registry_file = target_config.get("registry_file", str(workspace_dir / ".VNN" / "stories.json"))

    # Scan for story directories (directories with a .VNN subdirectory or article.md)
    stories = []
    if workspace_dir.exists():
        for child in sorted(workspace_dir.iterdir()):
            if not child.is_dir():
                continue
            # A story directory has either .VNN/ or article.md
            if (child / ".VNN").exists() or (child / "article.md").exists():
                stories.append(child.name)

    # Write registry
    reg_path = Path(registry_file)
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text(json.dumps({"stories": stories}, indent=2))

    return True


# --- Composite hooks ---

_VNN_PRE_TICK_HOOKS = [
    queue_empty_global,
    discover_stories,
    sync_story_states,
    cleanup_inconsistent_state,
    check_completed_compilations,
    cleanup_stale_compilation_markers,
]

_VNN_POST_TICK_HOOKS = [
    log_rejection,
]


def vnn_pre_tick(context: dict[str, Any]) -> bool:
    """Composite pre_tick hook: run all VNN pre_tick hooks in sequence.

    Returns False (skip tick) if any hook returns False.
    Hooks run in order: queue_empty_global, discover_stories, sync_story_states,
    cleanup_inconsistent_state, check_completed_compilations,
    cleanup_stale_compilation_markers.

    :param context: Hook context dict.
    :returns: True if all hooks pass, False if any hook returns False.
    """
    for hook in _VNN_PRE_TICK_HOOKS:
        result = hook(context)
        if result is False:
            return False
    return True


def vnn_post_tick(context: dict[str, Any], result: ActionResult) -> None:
    """Composite post_tick hook: run all VNN post_tick hooks in sequence.

    :param context: Hook context dict.
    :param result: Tick result from the completed tick.
    """
    for hook in _VNN_POST_TICK_HOOKS:
        hook(context, result)
