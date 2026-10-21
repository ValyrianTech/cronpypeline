"""VNN pipeline custom triggers, actions and hooks.

The VNN pipeline is country-scoped: every target is a country, and all of that
country's state lives inside its own target directory::

    <country>/ranking.json              # ranked compiled stories + their state
    <country>/compiled_stories/         # story_<id>.json produced by the compiler
    <country>/stories/<story_id>/       # research.md, article.md, published.json, ...
    <country>/.active_story             # story currently being worked on
    <country>/compilation_metadata.json # written when a compilation finishes

Provides:
- Triggers: `detect_ranking_needed`, `detect_compilation_needed`,
  `detect_research_needed`, `detect_writing_needed`, `detect_publishing_needed`,
  `detect_revision_needed`.
- Actions: `claim_story` (writes `.active_story`), `release_story` (clears it),
  `validate_ranking_action` (checks the ranking the agent wrote),
  `sync_story_states` (reconciles the ranking with the story directories).
- Hooks: `vnn_pre_tick` (global queue gate + inconsistent-state cleanup) and
  `vnn_post_tick` (rejection audit trail).
"""

import json
import time
from pathlib import Path
from typing import Any

from cronpypeline.actions import ActionResult, TickContext
from cronpypeline.config import ActionSpec
from cronpypeline.io_utils import write_json_atomic
from cronpypeline.plugins.vnn_ranking import (
    ACTIVE_STORY_FILENAME,
    COMPILED_SUBDIR,
    RANKING_FILENAME,
    REJECTION_LOG_FILENAME,
    STATE_PENDING,
    clear_active_story,
    derive_story_state,
    load_active_story,
    load_ranking,
    ranking_stories,
    rejection_count,
    set_story_state,
    state_priority,
    story_artifacts,
    story_dir,
    today_utc,
    unranked_stories,
    utc_now_iso,
    validate_ranking,
    write_active_story,
)

VNN_SUBDIR = ".VNN"
COMPILATION_MARKER = "compilation_metadata.json"
NEWS_LINKS_SUBDIR = "news_links"
STORIES_SUBDIR = "stories"
PROCESSING_MARKER = ".processing"
RESEARCH_MARKER = "research.md"
ARTICLE_MARKER = "article.md"
REJECTED_MARKER = "rejected-article.md"
PUBLISHED_MARKER = "published.json"
GAVE_UP_MARKER = ".gave_up"
REVISION_NOTES_MARKER = "revision-notes.md"

DEFAULT_MIN_LINKS = 10
DEFAULT_MIN_NEW_LINKS = 5
DEFAULT_MIN_NEW_STORIES = 1
DEFAULT_MAX_REJECTIONS = 5

#: Story states that still need article work.
_ACTIVE_STATES = ("pending", "researching", "writing", "publishing")


# ─── Config helpers ─────────────────────────────────────────────────────────


def _target_dir(context: dict[str, Any] | TickContext) -> Path:
    """Resolve the target directory from a trigger or tick context.

    :param context: Trigger context dict or :class:`TickContext`.
    :returns: Path to the country target directory.
    """
    if isinstance(context, TickContext):
        return context.target_dir
    raw = context.get("target_dir")
    if raw:
        return Path(raw)
    workspace = Path(context.get("workspace_dir", "."))
    return workspace / str(context.get("target", "."))


def _config(context: dict[str, Any] | TickContext) -> dict[str, Any]:
    """Return the per-country configuration dict.

    :param context: Trigger context dict or :class:`TickContext`.
    :returns: Per-target configuration (empty dict when unset).
    """
    if isinstance(context, TickContext):
        return context.target_config or {}
    return context.get("target_config") or {}


def _int_option(config: dict[str, Any], key: str, default: int) -> int:
    """Read an integer option from the per-country config.

    :param config: Per-country configuration dict.
    :param key: Option name.
    :param default: Value used when the option is missing or not an integer.
    :returns: The resolved integer value.
    """
    value = config.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _date(context: dict[str, Any] | TickContext) -> str:
    """Return the pipeline date (today, UTC, unless the country pins one).

    :param context: Trigger context dict or :class:`TickContext`.
    :returns: Date in ``YYYYMMDD`` form.
    """
    configured = _config(context).get("date")
    if isinstance(configured, str) and configured:
        return configured
    return today_utc()


def _country(context: dict[str, Any] | TickContext) -> str:
    """Return the country name for this context.

    :param context: Trigger context dict or :class:`TickContext`.
    :returns: Country name (the target name).
    """
    if isinstance(context, TickContext):
        return context.target
    return str(context.get("target", ""))


def _is_processing(target_dir: Path) -> bool:
    """Return True when a stage is currently running for this country.

    :param target_dir: Country target directory.
    :returns: True if the country's processing marker exists.
    """
    return (target_dir / PROCESSING_MARKER).exists()


def _current_link_ids(target_dir: Path) -> set[str]:
    """Return the ids of the archived news links for this country.

    :param target_dir: Country target directory.
    :returns: Set of link ids (file stems).
    """
    links_dir = target_dir / NEWS_LINKS_SUBDIR
    if not links_dir.is_dir():
        return set()
    return {p.stem for p in links_dir.glob("*.json") if not p.name.startswith(".")}


def _compiled_link_ids(target_dir: Path) -> set[str]:
    """Return the link ids already folded into compiled stories.

    :param target_dir: Country target directory.
    :returns: Set of link ids recorded in ``compilation_metadata.json``.
    """
    marker = target_dir / COMPILATION_MARKER
    if not marker.exists():
        return set()
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return set()
    if not isinstance(data, dict):
        return set()
    ids = data.get("link_ids")
    if isinstance(ids, list):
        return {str(i) for i in ids}
    return set()


def _active_story(target_dir: Path) -> tuple[str, Path] | None:
    """Resolve the story the pipeline should work on.

    Prefers the ``.active_story`` lock; otherwise the first ranked story that
    still needs article work.

    :param target_dir: Country target directory.
    :returns: ``(story_id, story_path)`` or None when there is nothing to do.
    """
    active = load_active_story(target_dir)
    if active is not None:
        story_id = str(active["story_id"])
        return story_id, story_dir(target_dir, story_id)

    for story in ranking_stories(load_ranking(target_dir)):
        if story.get("state") in _ACTIVE_STATES:
            story_id = str(story["story_id"])
            return story_id, story_dir(target_dir, story_id)
    return None


def _story_needs(story_path: Path) -> dict[str, bool]:
    """Classify what work a story still needs.

    :param story_path: Story directory.
    :returns: Mapping with ``research``, ``writing``, ``publishing`` and
        ``revision`` flags.
    """
    found = story_artifacts(story_path)
    if found["gave_up"] or found["published"]:
        return {"research": False, "writing": False, "publishing": False, "revision": False}
    if found["rejected"] and not found["article"]:
        return {"research": False, "writing": False, "publishing": False, "revision": True}
    if found["article"]:
        return {"research": False, "writing": False, "publishing": True, "revision": False}
    if found["research"]:
        return {"research": False, "writing": True, "publishing": False, "revision": False}
    return {"research": True, "writing": False, "publishing": False, "revision": False}


# ─── Triggers ───────────────────────────────────────────────────────────────


def detect_ranking_needed(context: dict[str, Any]) -> bool:
    """Trigger: fire when enough new compiled stories are waiting to be ranked.

    :param context: Trigger context dict with ``target_dir`` and ``target_config``.
    :returns: True when the number of unranked compiled stories reaches the
        country's ``min_new_stories`` threshold.
    """
    target_dir = _target_dir(context)
    threshold = _int_option(_config(context), "min_new_stories", DEFAULT_MIN_NEW_STORIES)
    return len(unranked_stories(target_dir)) >= threshold


def detect_compilation_needed(context: dict[str, Any]) -> bool:
    """Trigger: fire when the country has enough archived links to compile.

    A first compilation needs ``min_links`` links; a recompilation needs
    ``min_new_links`` links that were not part of the previous compilation.

    :param context: Trigger context dict with ``target_dir`` and ``target_config``.
    :returns: True when a compilation should be queued.
    """
    target_dir = _target_dir(context)
    config = _config(context)
    current = _current_link_ids(target_dir)
    if not current:
        return False
    compiled = _compiled_link_ids(target_dir)
    if not compiled:
        return len(current) >= _int_option(config, "min_links", DEFAULT_MIN_LINKS)
    return len(current - compiled) >= _int_option(config, "min_new_links", DEFAULT_MIN_NEW_LINKS)


def detect_research_needed(context: dict[str, Any]) -> bool:
    """Trigger: fire when the active story still needs research.

    :param context: Trigger context dict with ``target_dir``.
    :returns: True when the active story has no ``research.md`` yet.
    """
    target_dir = _target_dir(context)
    if _is_processing(target_dir):
        return False
    resolved = _active_story(target_dir)
    if resolved is None:
        return False
    return _story_needs(resolved[1])["research"]


def detect_writing_needed(context: dict[str, Any]) -> bool:
    """Trigger: fire when the active story has research but no article.

    :param context: Trigger context dict with ``target_dir``.
    :returns: True when the active story needs an article written.
    """
    target_dir = _target_dir(context)
    if _is_processing(target_dir):
        return False
    resolved = _active_story(target_dir)
    if resolved is None:
        return False
    return _story_needs(resolved[1])["writing"]


def detect_publishing_needed(context: dict[str, Any]) -> bool:
    """Trigger: fire when the active story has an article that is not published.

    :param context: Trigger context dict with ``target_dir``.
    :returns: True when the active story needs publishing.
    """
    target_dir = _target_dir(context)
    if _is_processing(target_dir):
        return False
    resolved = _active_story(target_dir)
    if resolved is None:
        return False
    return _story_needs(resolved[1])["publishing"]


def detect_revision_needed(context: dict[str, Any]) -> bool:
    """Trigger: fire when the active story was rejected and can still be revised.

    :param context: Trigger context dict with ``target_dir`` and ``target_config``.
    :returns: True when the active story needs a revision and its rejection
        count is below the country's ``max_rejections`` threshold.
    """
    target_dir = _target_dir(context)
    if _is_processing(target_dir):
        return False
    resolved = _active_story(target_dir)
    if resolved is None:
        return False
    story_path = resolved[1]
    if not _story_needs(story_path)["revision"]:
        return False
    max_rejections = _int_option(_config(context), "max_rejections", DEFAULT_MAX_REJECTIONS)
    return rejection_count(story_path) < max_rejections


# ─── Actions ────────────────────────────────────────────────────────────────


def claim_story(action: ActionSpec, context: TickContext) -> ActionResult:
    """Custom action: claim the active story for a stage.

    Writes ``.active_story`` (so the story skills know which story to load) and
    records the story's new state in ``ranking.json``.

    Expected action.params:
        - stage: Stage name recorded in the lock (defaults to ``"pipeline"``)
        - state: Story state to record in ``ranking.json``

    :param action: Action spec with optional ``stage``/``state`` params.
    :param context: Tick context with target and directories.
    :returns: Result carrying the claimed ``story_id``.
    """
    if context.dry_run:
        return ActionResult(success=True, dry_run=True)

    resolved = _active_story(context.target_dir)
    if resolved is None:
        return ActionResult(success=False, stderr="No story available to claim")
    story_id = resolved[0]

    stage = str(action.params.get("stage") or "pipeline")
    write_active_story(context.target_dir, story_id, stage)

    state = action.params.get("state")
    if isinstance(state, str) and state:
        set_story_state(context.target_dir, story_id, state)

    return ActionResult(success=True, data={"story_id": story_id, "stage": stage})


def release_story(action: ActionSpec, context: TickContext) -> ActionResult:
    """Custom action: release the active story lock.

    :param action: Action spec (unused).
    :param context: Tick context with target and directories.
    :returns: Result describing whether a lock was released.
    """
    if context.dry_run:
        return ActionResult(success=True, dry_run=True)

    released = clear_active_story(context.target_dir)
    return ActionResult(success=True, data={"released": released})


def validate_ranking_action(action: ActionSpec, context: TickContext) -> ActionResult:
    """Custom action: validate the ``ranking.json`` the ranking agent wrote.

    :param action: Action spec (unused).
    :param context: Tick context with target and directories.
    :returns: Result that fails when the ranking is missing or malformed.
    """
    if context.dry_run:
        return ActionResult(success=True, dry_run=True)

    ok, reason = validate_ranking(context.target_dir, _country(context), _date(context))
    if not ok:
        return ActionResult(success=False, stderr=reason)
    return ActionResult(success=True, stdout=f"{RANKING_FILENAME} is valid")


def sync_story_states(action: ActionSpec, context: TickContext) -> ActionResult:
    """Custom action: reconcile ``ranking.json`` with the story directories.

    Each story's state is derived from the artefacts in its directory; the most
    advanced state wins so progress is never rolled back.

    :param action: Action spec (unused).
    :param context: Tick context with target and directories.
    :returns: Result listing the stories whose state changed.
    """
    if context.dry_run:
        return ActionResult(success=True, dry_run=True)

    target_dir = context.target_dir
    ranking = load_ranking(target_dir)
    if ranking is None:
        return ActionResult(success=True, stdout=f"No {RANKING_FILENAME} to sync")

    updated: list[str] = []
    for story in ranking_stories(ranking):
        story_id = str(story["story_id"])
        path = story_dir(target_dir, story_id)
        if not path.is_dir():
            continue
        derived = derive_story_state(path)
        current = str(story.get("state", STATE_PENDING))
        if state_priority(derived) > state_priority(current):
            story["state"] = derived
            story["state_updated_at"] = utc_now_iso()
            updated.append(story_id)

    if updated:
        ranking["last_updated"] = utc_now_iso()
        write_json_atomic(target_dir / RANKING_FILENAME, ranking)
    return ActionResult(success=True, data={"updated": updated})


# ─── Hooks ──────────────────────────────────────────────────────────────────


def queue_empty_global(context: dict[str, Any]) -> bool:
    """Pre-tick hook: skip the tick while the conversation queue has entries.

    The queue directory is read from the pipeline's ``action_handler`` config so
    the gate always matches the queue the stages actually write to.

    :param context: Hook context dict.
    :returns: True (proceed) when the queue is empty, False otherwise.
    """
    queue_dir = context.get("queue_dir")
    if not queue_dir:
        return True
    queue_path = Path(queue_dir)
    if not queue_path.exists():
        return True
    return not any(queue_path.glob("*.json"))


def cleanup_inconsistent_state(context: dict[str, Any]) -> bool:
    """Pre-tick hook: resolve stories that have both an article and a rejection.

    When ``article.md`` is newer than ``rejected-article.md`` the revision
    succeeded, so the rejection files are removed. Otherwise the article was
    rejected, so the stale ``article.md`` is removed and the revision stage can
    run.

    :param context: Hook context dict with ``target_dir``.
    :returns: True to allow the tick to proceed.
    """
    stories_root = _target_dir(context) / STORIES_SUBDIR
    if not stories_root.is_dir():
        return True

    for story_path in sorted(stories_root.iterdir()):
        if not story_path.is_dir():
            continue
        article = story_path / ARTICLE_MARKER
        rejected = story_path / REJECTED_MARKER
        if not (article.exists() and rejected.exists()):
            continue
        try:
            article_newer = article.stat().st_mtime > rejected.stat().st_mtime
        except OSError:
            continue
        try:
            if article_newer:
                rejected.unlink()
                notes = story_path / REVISION_NOTES_MARKER
                if notes.exists():
                    notes.unlink()
            else:
                article.unlink()
        except OSError:
            continue
    return True


def log_rejection(context: dict[str, Any], result: ActionResult | None) -> None:
    """Post-tick hook: append to the active story's ``rejection_log.json``.

    The rejection log is the source of truth for the rejection count, so it is
    written whenever the story has a ``rejected-article.md``.

    :param context: Hook context dict with ``target_dir``.
    :param result: Tick result from the completed tick, or None.
    """
    resolved = _active_story(_target_dir(context))
    if resolved is None:
        return
    story_path = resolved[1]
    if not (story_path / REJECTED_MARKER).exists():
        return

    log_file = story_path / REJECTION_LOG_FILENAME
    existing: list[Any] = []
    if log_file.exists():
        try:
            data = json.loads(log_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                existing = data
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            existing = []

    reason = ""
    notes = story_path / REVISION_NOTES_MARKER
    if notes.exists():
        try:
            reason = notes.read_text(encoding="utf-8").strip()
        except OSError:
            reason = ""
    if not reason and result is not None:
        reason = result.stderr or result.stdout

    existing.append({
        "timestamp": time.time(),
        "stage_id": context.get("stage_id") or "",
        "story_id": story_path.name,
        "reason": reason,
    })
    write_json_atomic(log_file, existing)


def vnn_pre_tick(context: dict[str, Any]) -> bool:
    """Composite pre_tick hook.

    Runs the global queue gate first (cheapest, and it short-circuits the whole
    tick), then resolves inconsistent story state.

    :param context: Hook context dict.
    :returns: True if all hooks pass, False if any hook returns False.
    """
    if queue_empty_global(context) is False:
        return False
    return cleanup_inconsistent_state(context)


def vnn_post_tick(context: dict[str, Any], result: ActionResult | None) -> None:
    """Composite post_tick hook: record rejections in the audit trail.

    :param context: Hook context dict.
    :param result: Tick result from the completed tick.
    """
    log_rejection(context, result)


__all__ = [
    "ACTIVE_STORY_FILENAME",
    "COMPILATION_MARKER",
    "COMPILED_SUBDIR",
    "NEWS_LINKS_SUBDIR",
    "claim_story",
    "cleanup_inconsistent_state",
    "detect_compilation_needed",
    "detect_publishing_needed",
    "detect_ranking_needed",
    "detect_research_needed",
    "detect_revision_needed",
    "detect_writing_needed",
    "log_rejection",
    "queue_empty_global",
    "release_story",
    "sync_story_states",
    "validate_ranking_action",
    "vnn_post_tick",
    "vnn_pre_tick",
]
