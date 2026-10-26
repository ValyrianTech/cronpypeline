"""VNN pipeline custom triggers, actions and hooks.

The VNN pipeline is country-scoped: every target is a country, and all of that
country's state lives inside its own target directory. Everything that grows
without bound is partitioned by day::

    <country>/rankings/<YYYY>/<MM>/<DD>/ranking.json  # ranked stories + state
    <country>/compiled_stories/<YYYY>/<MM>/<DD>/      # story_<id>.json from the compiler
    <country>/stories/<YYYY>/<MM>/<DD>/<story_id>/    # research.md, article.md, published.json, ...
    <country>/news_links/<YYYY>/<MM>/<DD>/            # archived links awaiting compilation
    <country>/.active_story                           # story currently being worked on
    <country>/compilation_metadata.json               # written when a compilation finishes

Provides:
- Triggers: `detect_ranking_needed`, `detect_compilation_needed`,
  `detect_research_needed`, `detect_writing_needed`, `detect_publishing_needed`,
  `detect_revision_needed`.
- Actions: `claim_story` (writes `.active_story`), `release_story` (clears it),
  `validate_ranking_action` (checks the ranking the agent wrote),
  `sync_story_states` (reconciles the ranking with the story directories).
- Hooks: `vnn_pre_tick` (global queue gate, ranking sync, finished-story lock
  release, inconsistent-state cleanup) and `vnn_post_tick` (rejection audit
  trail).
"""

import json
import re
import time
from pathlib import Path
from typing import Any

from cronpypeline.actions import ActionResult, TickContext
from cronpypeline.config import ActionSpec, ActionType
from cronpypeline.io_utils import write_json_atomic
from cronpypeline.plugins.vnn_ranking import (
    ACTIVE_STORY_FILENAME,
    COMPILED_SUBDIR,
    NEWS_LINKS_SUBDIR,
    RANKING_FILENAME,
    REJECTION_LOG_FILENAME,
    STATE_PENDING,
    STORIES_SUBDIR,
    clear_active_story,
    derive_story_state,
    find_story_dir,
    load_active_story,
    load_ranking,
    news_links_root,
    ranking_path,
    ranking_stories,
    rejection_count,
    set_story_state,
    state_priority,
    story_artifacts,
    story_day,
    story_dir,
    stories_root,
    today_utc,
    unranked_stories,
    utc_now_iso,
    validate_ranking,
    write_active_story,
    write_ranking,
)

VNN_SUBDIR = ".VNN"
COMPILATION_MARKER = "compilation_metadata.json"
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
DEFAULT_PROCESSING_TIMEOUT_MINUTES = 20

#: Where the publisher skill copies articles before committing them.
DEFAULT_POSTS_DIR = Path("/home/wouter/Repos/ValyrianNewsNetwork/src/content/posts")

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


def _stage(context: dict[str, Any] | TickContext) -> Any:
    """Return the stage being evaluated, when the context carries one.

    :param context: Trigger context dict or :class:`TickContext`.
    :returns: The :class:`Stage` object, or None.
    """
    return context.get("stage") if isinstance(context, dict) else None


def _stage_timeout(context: dict[str, Any] | TickContext) -> int:
    """Return the stale timeout (in minutes) for the stage being evaluated.

    The timeout is read from the stage definition when the context carries it
    and otherwise falls back to ``DEFAULT_PROCESSING_TIMEOUT_MINUTES``.

    :param context: Trigger context dict or :class:`TickContext`.
    :returns: Timeout in minutes.
    """
    timeout = getattr(_stage(context), "timeout_minutes", None)
    if isinstance(timeout, int) and timeout > 0:
        return timeout
    return DEFAULT_PROCESSING_TIMEOUT_MINUTES


def _processing_marker_name(stage: Any) -> str:
    """Return the processing marker filename for a stage.

    Every VNN stage declares its own processing marker so that one stage's
    marker cannot reset another's. The name is read from the stage definition,
    falling back to the legacy shared ``PROCESSING_MARKER`` when the stage is
    unavailable (e.g. a trigger evaluated without a stage in its context).

    :param stage: Stage object, or None.
    :returns: Processing marker filename.
    """
    markers = getattr(stage, "markers", None)
    if isinstance(markers, dict):
        spec = markers.get("processing")
        name = getattr(spec, "name", None)
        if isinstance(name, str) and name:
            return name
    return PROCESSING_MARKER


def _is_processing(
    target_dir: Path,
    timeout_minutes: int | None = None,
    stage: Any = None,
) -> bool:
    """Return True when a stage is currently running for this country.

    Each stage has its own processing marker, so a marker left behind by an
    agent that died without clearing it only gates that stage's triggers.
    Mirroring the core pipeline's staleness rules, the marker is treated as
    stale (not processing) when it is older than ``timeout_minutes`` or when
    its ``queue_file`` is gone and no reminder file is waiting in the queue
    directory.

    :param target_dir: Country target directory.
    :param timeout_minutes: Age in minutes after which the marker is stale.
        When None, only the queue-file rule applies.
    :param stage: Stage whose processing marker to inspect. When None, the
        legacy shared marker name is used.
    :returns: True if the stage's processing marker exists and is not stale.
    """
    marker = target_dir / _processing_marker_name(stage)
    if not marker.exists():
        return False
    try:
        age_seconds = time.time() - marker.stat().st_mtime
    except OSError:
        return False
    if timeout_minutes is not None and age_seconds >= timeout_minutes * 60:
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return True
    if not isinstance(data, dict):
        return True
    queue_file = data.get("queue_file")
    if not isinstance(queue_file, str) or not queue_file:
        return True
    queue_path = Path(queue_file)
    if queue_path.exists():
        return True
    queue_dir = queue_path.parent
    try:
        has_reminder = queue_dir.is_dir() and any(
            "_reminder_" in entry.name for entry in queue_dir.iterdir()
        )
    except OSError:
        has_reminder = False
    return has_reminder


def _current_link_ids(target_dir: Path) -> set[str]:
    """Return the ids of the archived news links for this country.

    Links are partitioned by day, so the whole tree is walked.

    :param target_dir: Country target directory.
    :returns: Set of link ids (file stems).
    """
    root = news_links_root(target_dir)
    if not root.is_dir():
        return set()
    return {p.stem for p in root.rglob("*.json") if not p.name.startswith(".")}


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


def _published_post_exists(story_path: Path, posts_dir: Path) -> bool:
    """Return True when the story's article is already in the VNN content repo.

    ``publish_article.py`` copies the article into the content repository and
    only then commits it. When the commit fails (e.g. a DNS failure resolving
    github.com) the copy survives but ``published.json`` is never written, so
    every retry fails with a duplicate-slug error and the story is wedged
    forever. The post file carries the ``story_id`` in its front matter, which
    is what identifies the story's own article rather than a slug collision.

    :param story_path: Story directory.
    :param posts_dir: VNN content repository posts directory.
    :returns: True when a post file for this story exists.
    """
    if not posts_dir.is_dir():
        return False
    story_id = story_path.name
    for post in posts_dir.rglob("*.md"):
        try:
            text = post.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        match = re.search(r"^story_id:\s*[\"']?([^\"'\n]+)", text, re.MULTILINE)
        if match and match.group(1).strip() == story_id:
            return True
    return False


def _posts_dir(context: dict[str, Any] | TickContext) -> Path | None:
    """Return the VNN content repository posts directory, when configured.

    The publisher skill copies articles into the content repository before
    committing them, so the posts directory is what reveals a publish whose
    commit failed. It is read from the country's ``posts_dir`` option and
    falls back to ``DEFAULT_POSTS_DIR``; the reconciliation is skipped when the
    directory does not exist.

    :param context: Trigger context dict or :class:`TickContext`.
    :returns: Path to the posts directory, or None when it does not exist.
    """
    configured = _config(context).get("posts_dir")
    path = Path(str(configured)) if configured else DEFAULT_POSTS_DIR
    return path if path.is_dir() else None


def _max_rejections(context: dict[str, Any] | TickContext) -> int:
    """Return the country's ``max_rejections`` option.

    :param context: Trigger context dict or :class:`TickContext`.
    :returns: Configured value, or ``DEFAULT_MAX_REJECTIONS``.
    """
    return _int_option(_config(context), "max_rejections", DEFAULT_MAX_REJECTIONS)


def _rejection_exhausted(story_path: Path, max_rejections: int) -> bool:
    """Return True when a story needs revision beyond the rejection budget.

    :param story_path: Story directory.
    :param max_rejections: Rejection cap.
    :returns: True when the story needs revision and the cap is reached.
    """
    return (
        _story_needs(story_path)["revision"]
        and rejection_count(story_path) >= max_rejections
    )


def _is_finished(
    story_path: Path,
    posts_dir: Path | None = None,
    max_rejections: int | None = None,
) -> bool:
    """Return True when a story needs no further article work.

    A story is finished once it is published or has given up. The check reads
    the story directory rather than ``ranking.json`` because the ranking can
    lag behind the filesystem (an agent publishes without updating the state).

    When ``posts_dir`` is given, a story whose article already exists in the
    content repository also counts as finished, and the missing
    ``published.json`` marker is written so the pipeline stops re-queueing the
    publisher.

    When ``max_rejections`` is given, a story that needs revision but has
    reached the cap also counts as finished — ``detect_revision_needed`` can
    never fire for it again — and ``.gave_up`` is written so the ranking sync
    derives ``discarded``. This is what unblocks target selection: a wedged
    story suppresses every trigger, so the transition has to happen inside
    trigger evaluation rather than in a post-selection hook.

    :param story_path: Story directory.
    :param posts_dir: Optional VNN content repository posts directory.
    :param max_rejections: Optional rejection cap.
    :returns: True when the story is published or gave up.
    """
    if not story_path.is_dir():
        # A story that has not been started cannot be finished. Bailing out
        # here also avoids scanning the whole content repository for a post
        # that cannot exist yet.
        return False
    found = story_artifacts(story_path)
    if found["published"] or found["gave_up"]:
        return True
    if posts_dir is not None and _published_post_exists(story_path, posts_dir):
        _write_published_marker(story_path, posts_dir)
        return True
    if max_rejections is not None and _rejection_exhausted(story_path, max_rejections):
        _write_gave_up_marker(story_path, max_rejections)
        return True
    return False


def _write_gave_up_marker(story_path: Path, max_rejections: int) -> None:
    """Write the ``.gave_up`` marker for a story out of rejections.

    :param story_path: Story directory.
    :param max_rejections: Rejection cap that was reached.
    """
    count = rejection_count(story_path)
    write_json_atomic(story_path / GAVE_UP_MARKER, {
        "story_id": story_path.name,
        "reason": f"rejection budget exhausted ({count} >= {max_rejections})",
        "rejection_count": count,
        "gave_up_at": utc_now_iso(),
    })


def _write_published_marker(story_path: Path, posts_dir: Path) -> None:
    """Write the ``published.json`` marker for an already-published story.

    :param story_path: Story directory.
    :param posts_dir: VNN content repository posts directory.
    """
    write_json_atomic(story_path / PUBLISHED_MARKER, {
        "story_id": story_path.name,
        "reconciled": True,
        "posts_dir": str(posts_dir),
        "published_at": utc_now_iso(),
    })


def _locked_story_path(target_dir: Path, active: dict[str, Any]) -> Path:
    """Return the story directory recorded in the ``.active_story`` lock.

    The lock stores the absolute path it claimed, which survives the story
    directory moving into a different day partition. When the recorded path is
    gone the directory is looked up by id, falling back to the day recorded in
    the lock and finally to today.

    :param target_dir: Country target directory.
    :param active: Parsed lock dict.
    :returns: Path to the story directory.
    """
    recorded = active.get("story_dir")
    if isinstance(recorded, str) and recorded:
        path = Path(recorded)
        if path.is_dir():
            return path
    story_id = str(active["story_id"])
    found = find_story_dir(target_dir, story_id)
    if found is not None:
        return found
    return story_dir(target_dir, story_id, active.get("story_date"))


def _entry_story_path(target_dir: Path, story: dict[str, Any]) -> Path:
    """Return the story directory for a ranking entry.

    Ranking entries carry the day their story was compiled, so the directory is
    resolved without scanning the tree. A story that has not been started yet
    resolves to the path it will be created at.

    :param target_dir: Country target directory.
    :param story: Story entry from a ranking document.
    :returns: Path to the story directory.
    """
    story_id = str(story["story_id"])
    day = story.get("date")
    found = find_story_dir(target_dir, story_id, day) if day else find_story_dir(target_dir, story_id)
    return found if found is not None else story_dir(target_dir, story_id, day)


def _story_dirs(target_dir: Path) -> list[Path]:
    """Return every story directory for a country, across all days.

    :param target_dir: Country target directory.
    :returns: Story directories sorted by path.
    """
    root = stories_root(target_dir)
    if not root.is_dir():
        return []
    dated = [path for path in root.glob("*/*/*/*") if path.is_dir()]
    legacy = [
        path for path in root.iterdir()
        if path.is_dir() and not path.name.isdigit()
    ]
    return sorted(dated + legacy)


def _active_story(
    target_dir: Path,
    posts_dir: Path | None = None,
    max_rejections: int | None = None,
) -> tuple[str, Path] | None:
    """Resolve the story the pipeline should work on.

    Prefers the ``.active_story`` lock; otherwise the first ranked story that
    still needs article work. Stories that are already finished are skipped in
    both branches: a lock left behind by a completed story must not pin the
    pipeline forever, and a ranking whose ``state`` lags behind the filesystem
    must not select an already-published story.

    :param target_dir: Country target directory.
    :param posts_dir: Optional VNN content repository posts directory, used to
        recognise a story whose article was copied but never committed.
    :param max_rejections: Optional rejection cap, used to recognise a story
        whose revision budget is exhausted.
    :returns: ``(story_id, story_path)`` or None when there is nothing to do.
    """
    active = load_active_story(target_dir)
    if active is not None:
        story_id = str(active["story_id"])
        path = _locked_story_path(target_dir, active)
        if not _is_finished(path, posts_dir, max_rejections):
            return story_id, path

    for story in ranking_stories(load_ranking(target_dir)):
        if story.get("state") in _ACTIVE_STATES:
            story_id = str(story["story_id"])
            path = _entry_story_path(target_dir, story)
            if not _is_finished(path, posts_dir, max_rejections):
                return story_id, path
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
    if _is_processing(target_dir, _stage_timeout(context), _stage(context)):
        return False
    threshold = _int_option(_config(context), "min_new_stories", DEFAULT_MIN_NEW_STORIES)
    return len(unranked_stories(target_dir)) >= threshold


def detect_compilation_needed(context: dict[str, Any]) -> bool:
    """Trigger: fire when the country has enough archived links to compile.

    A first compilation needs ``min_links`` links; a recompilation needs
    ``min_new_links`` links that were not part of the previous compilation.

    The stage is skipped while its own agent is still running. Without this
    gate the trigger kept firing on every tick during a compilation: the
    pending-link count only drops once the agent writes
    ``compilation_metadata.json``, so the retry budget was exhausted and the
    stage gave up before the agent could finish.

    :param context: Trigger context dict with ``target_dir`` and ``target_config``.
    :returns: True when a compilation should be queued.
    """
    target_dir = _target_dir(context)
    if _is_processing(target_dir, _stage_timeout(context), _stage(context)):
        return False
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
    if _is_processing(target_dir, _stage_timeout(context), _stage(context)):
        return False
    resolved = _active_story(target_dir, _posts_dir(context), _max_rejections(context))
    if resolved is None:
        return False
    return _story_needs(resolved[1])["research"]


def detect_writing_needed(context: dict[str, Any]) -> bool:
    """Trigger: fire when the active story has research but no article.

    :param context: Trigger context dict with ``target_dir``.
    :returns: True when the active story needs an article written.
    """
    target_dir = _target_dir(context)
    if _is_processing(target_dir, _stage_timeout(context), _stage(context)):
        return False
    resolved = _active_story(target_dir, _posts_dir(context), _max_rejections(context))
    if resolved is None:
        return False
    return _story_needs(resolved[1])["writing"]


def detect_publishing_needed(context: dict[str, Any]) -> bool:
    """Trigger: fire when the active story has an article that is not published.

    :param context: Trigger context dict with ``target_dir``.
    :returns: True when the active story needs publishing.
    """
    target_dir = _target_dir(context)
    if _is_processing(target_dir, _stage_timeout(context), _stage(context)):
        return False
    resolved = _active_story(target_dir, _posts_dir(context), _max_rejections(context))
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
    if _is_processing(target_dir, _stage_timeout(context), _stage(context)):
        return False
    resolved = _active_story(target_dir, _posts_dir(context), _max_rejections(context))
    if resolved is None:
        return False
    story_path = resolved[1]
    if not _story_needs(story_path)["revision"]:
        return False
    return rejection_count(story_path) < _max_rejections(context)


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

    resolved = _active_story(
        context.target_dir, _posts_dir(context), _max_rejections(context)
    )
    if resolved is None:
        return ActionResult(success=False, stderr="No story available to claim")
    story_id, story_path = resolved

    stage = str(action.params.get("stage") or "pipeline")
    write_active_story(
        context.target_dir,
        story_id,
        stage,
        story_day(context.target_dir, story_path) or None,
    )

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
        path = _entry_story_path(target_dir, story)
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
        write_ranking(target_dir, ranking)
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


def _relative_to_target(target_dir: Path, path: Path) -> str:
    """Return ``path`` relative to ``target_dir``, or a non-existent sentinel.

    Marker templates must stay inside the target directory, so a path that
    cannot be expressed relatively is replaced by a sentinel that can never
    exist. That keeps a marker from resolving to a country-level file and being
    mistaken for story completion.

    :param target_dir: Country target directory.
    :param path: Path to express relatively.
    :returns: Relative POSIX path, or ``"stories/.none"``.
    """
    try:
        return path.relative_to(target_dir).as_posix()
    except ValueError:
        return "stories/.none"


def vnn_marker_context(context: dict[str, Any]) -> dict[str, Any]:
    """Marker-context hook: expose the active story and ranking directories.

    Story-scoped stages keep their artifacts inside
    ``<country>/stories/<YYYY>/<MM>/<DD>/<story_id>/`` and the ranking stage
    writes ``<country>/rankings/<YYYY>/<MM>/<DD>/ranking.json``, so their
    markers need the date partitions to resolve the right file. This hook
    supplies them as relative paths, letting the config declare e.g.
    ``"directory": "{active_story_dir}"`` and
    ``"directory": "{ranking_dir}"``.

    The story comes from :func:`_active_story` so the markers agree with the
    triggers and the actions. Reading the ``.active_story`` lock directly let a
    stale lock on a completed story resolve the completion markers into that
    story's directory, so the stages derived as complete and target selection
    skipped the country even though its triggers fired.

    When no story needs work the directory is a sentinel that cannot exist, so
    a stale country-level file is never mistaken for story completion.

    :param context: Base marker context dict with ``target_dir``.
    :returns: Dict with the ``active_story``, ``active_story_dir`` and
        ``ranking_dir`` template variables.
    """
    target_dir = Path(str(context.get("target_dir", "")))
    day = str(context.get("date") or today_utc())
    if not target_dir:
        return {
            "active_story": "",
            "active_story_dir": "stories/.none",
            "ranking_dir": "rankings/.none",
        }

    resolved = _active_story(target_dir, _posts_dir(context), _max_rejections(context))
    if resolved is None:
        story_id = ""
        story_dir_rel = "stories/.none"
    else:
        story_id, story_path = resolved
        story_dir_rel = _relative_to_target(target_dir, story_path)

    return {
        "active_story": story_id,
        "active_story_dir": story_dir_rel,
        "ranking_dir": _relative_to_target(target_dir, ranking_path(target_dir, day).parent),
    }


def cleanup_inconsistent_state(context: dict[str, Any]) -> bool:
    """Pre-tick hook: resolve stories that have both an article and a rejection.

    When ``article.md`` is newer than ``rejected-article.md`` the revision
    succeeded, so the rejection files are removed. Otherwise the article was
    rejected, so the stale ``article.md`` is removed and the revision stage can
    run.

    :param context: Hook context dict with ``target_dir``.
    :returns: True to allow the tick to proceed.
    """
    for story_path in _story_dirs(_target_dir(context)):
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
    resolved = _active_story(
        _target_dir(context), _posts_dir(context), _max_rejections(context)
    )
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


def _hook_tick_context(context: dict[str, Any]) -> TickContext:
    """Adapt a pre/post-tick hook context dict into a :class:`TickContext`.

    Hooks receive a plain dict, but the action callables expect a TickContext.
    ``target_dir`` is derived from ``workspace_dir``/``target``, so both are
    taken from the hook context.

    :param context: Hook context dict.
    :returns: Equivalent TickContext.
    """
    return TickContext(
        target=str(context.get("target", "")),
        workspace_dir=Path(str(context.get("workspace_dir", ""))),
        target_config=dict(context.get("target_config") or {}),
    )


def sync_story_states_hook(context: dict[str, Any]) -> bool:
    """Pre-tick hook: reconcile ``ranking.json`` with the story directories.

    Runs before state is derived so a story whose ``state`` lags behind the
    filesystem (an agent published without updating the ranking) is corrected
    before triggers and target selection read it. Never blocks the tick.

    :param context: Hook context dict with ``target_dir``.
    :returns: Always True.
    """
    target_dir = Path(str(context.get("target_dir", "")))
    if not target_dir:
        return True
    sync_story_states(ActionSpec(type=ActionType.CUSTOM, params={}), _hook_tick_context(context))
    return True


def release_finished_story_hook(context: dict[str, Any]) -> bool:
    """Pre-tick hook: release the active story lock once its story is finished.

    A lock left behind by a completed story is already ignored by
    :func:`_active_story`, but clearing it keeps ``.active_story`` an accurate
    record of what the pipeline is working on. Never blocks the tick.

    :param context: Hook context dict with ``target_dir``.
    :returns: Always True.
    """
    target_dir = Path(str(context.get("target_dir", "")))
    if not target_dir:
        return True
    active = load_active_story(target_dir)
    if active is None:
        return True
    if _is_finished(
        _locked_story_path(target_dir, active), _posts_dir(context), _max_rejections(context)
    ):
        release_story(ActionSpec(type=ActionType.CUSTOM, params={}), _hook_tick_context(context))
    return True


def discard_exhausted_stories_hook(context: dict[str, Any]) -> bool:
    """Pre-tick hook: discard stories whose rejection budget is exhausted.

    A story that needs revision can only proceed through
    :func:`detect_revision_needed`, which refuses to fire once
    ``rejection_count`` reaches ``max_rejections``. Without a terminal
    transition such a story keeps the ``.active_story`` lock forever and pins
    every story ranked behind it. Writing ``.gave_up`` makes the story
    finished: the ranking sync then derives ``discarded`` and the lock is
    released, all within the same tick. Never blocks the tick.

    :param context: Hook context dict with ``target_dir``.
    :returns: Always True.
    """
    target_dir = Path(str(context.get("target_dir", "")))
    if not target_dir:
        return True
    max_rejections = _max_rejections(context)

    for story_path in _story_dirs(target_dir):
        if not _rejection_exhausted(story_path, max_rejections):
            continue
        try:
            _write_gave_up_marker(story_path, max_rejections)
        except OSError:
            continue
    return True


def vnn_pre_tick(context: dict[str, Any]) -> bool:
    """Composite pre_tick hook.

    Runs the global queue gate first (cheapest, and it short-circuits the whole
    tick), then discards stories whose rejection budget is exhausted, then
    reconciles the ranking with the filesystem, then releases a lock left
    behind by a finished story, then resolves inconsistent story state.

    :param context: Hook context dict.
    :returns: True if all hooks pass, False if any hook returns False.
    """
    if queue_empty_global(context) is False:
        return False
    discard_exhausted_stories_hook(context)
    sync_story_states_hook(context)
    release_finished_story_hook(context)
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
    "release_finished_story_hook",
    "release_story",
    "sync_story_states",
    "sync_story_states_hook",
    "validate_ranking_action",
    "vnn_post_tick",
    "vnn_pre_tick",
]
