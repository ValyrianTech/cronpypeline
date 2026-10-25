"""VNN ranking state helpers.

Ranking is country-local: every country target owns a single ``ranking.json``
inside its own directory. The file is the authoritative record of which
compiled stories exist for that country and how far each one has progressed
through the article pipeline.

Layout (relative to the country target directory)::

    <country>/ranking.json
    <country>/stories/<story_id>/        # created by the research agent
    <country>/.active_story              # the story currently being worked on

``ranking.json`` schema::

    {
      "date": "20261021",
      "country": "belgium",
      "version": 1,
      "last_updated": "2026-10-21T09:00:00+00:00",
      "stories": [
        {
          "rank": 1,
          "story_id": "story_ac03c622",
          "title_en": "...",
          "summary_en": "...",
          "category": "...",
          "article_count": 3,
          "file_path": "<country>/compiled_stories/story_ac03c622.json",
          "state": "pending"
        }
      ]
    }

Story states, in pipeline order: ``pending`` → ``researching`` → ``writing`` →
``publishing`` → ``published``. ``discarded`` is terminal and never processed.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cronpypeline.io_utils import write_json_atomic

RANKING_FILENAME = "ranking.json"
STORIES_SUBDIR = "stories"
COMPILED_SUBDIR = "compiled_stories"
ACTIVE_STORY_FILENAME = ".active_story"
REJECTION_LOG_FILENAME = "rejection_log.json"

RANKING_VERSION = 1

STATE_PENDING = "pending"
STATE_RESEARCHING = "researching"
STATE_WRITING = "writing"
STATE_PUBLISHING = "publishing"
STATE_PUBLISHED = "published"
STATE_DISCARDED = "discarded"

#: Story states that still need article work, in the order they are handled.
ACTIVE_STATES = (STATE_PENDING, STATE_RESEARCHING, STATE_WRITING, STATE_PUBLISHING)

#: Higher priority = more advanced. Used to pick the best state when the same
#: story is observed through several artefacts (ranking.json vs. filesystem).
_STATE_PRIORITY = {
    STATE_PENDING: 0,
    STATE_RESEARCHING: 1,
    STATE_WRITING: 2,
    STATE_PUBLISHING: 3,
    STATE_PUBLISHED: 4,
    STATE_DISCARDED: 5,
}

_STORY_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def today_utc() -> str:
    """Return the current UTC date in ``YYYYMMDD`` form.

    :returns: Today's date as an 8-digit string, e.g. ``"20261021"``.
    """
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def utc_now_iso() -> str:
    """Return the current UTC timestamp as an ISO-8601 string.

    :returns: Timezone-aware ISO timestamp.
    """
    return datetime.now(timezone.utc).isoformat()


def ranking_path(target_dir: Path) -> Path:
    """Return the country-local ``ranking.json`` path.

    :param target_dir: Country target directory.
    :returns: Path to the country's ranking file.
    """
    return target_dir / RANKING_FILENAME


def stories_dir(target_dir: Path) -> Path:
    """Return the directory holding per-story subdirectories.

    :param target_dir: Country target directory.
    :returns: Path to the country's stories directory.
    """
    return target_dir / STORIES_SUBDIR


def compiled_dir(target_dir: Path) -> Path:
    """Return the directory holding compiled story JSON files.

    :param target_dir: Country target directory.
    :returns: Path to the country's compiled-stories directory.
    """
    return target_dir / COMPILED_SUBDIR


def active_story_path(target_dir: Path) -> Path:
    """Return the path of the active-story lock file.

    :param target_dir: Country target directory.
    :returns: Path to ``.active_story``.
    """
    return target_dir / ACTIVE_STORY_FILENAME


def is_valid_story_id(story_id: str) -> bool:
    """Return True if ``story_id`` is safe to use as a path component.

    :param story_id: Candidate story identifier.
    :returns: True when the id is non-empty, has no separators, and is not a
        traversal segment.
    """
    if not story_id or story_id in (".", ".."):
        return False
    return bool(_STORY_ID_RE.match(story_id))


def load_ranking(target_dir: Path) -> dict[str, Any] | None:
    """Load a country's ``ranking.json``.

    :param target_dir: Country target directory.
    :returns: The parsed ranking dict, or None when the file is missing or
        unreadable/invalid.
    """
    path = ranking_path(target_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def ranking_stories(ranking: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the story entries of a ranking dict, sorted by rank.

    Entries without a usable ``story_id`` are dropped so callers never build
    paths from untrusted values.

    :param ranking: Parsed ranking dict, or None.
    :returns: List of story dicts sorted by ``rank`` then ``story_id``.
    """
    if not ranking:
        return []
    raw = ranking.get("stories")
    if not isinstance(raw, list):
        return []
    stories = [
        s for s in raw
        if isinstance(s, dict) and is_valid_story_id(str(s.get("story_id", "")))
    ]
    return sorted(stories, key=lambda s: (s.get("rank") or 0, str(s.get("story_id", ""))))


def story_state(story: dict[str, Any]) -> str:
    """Return the normalised state of a ranking story entry.

    :param story: Story entry from ``ranking.json``.
    :returns: One of :data:`ACTIVE_STATES`, ``published`` or ``discarded``.
        Unknown values fall back to ``pending``.
    """
    state = str(story.get("state", STATE_PENDING))
    if state in _STATE_PRIORITY:
        return state
    return STATE_PENDING


def story_dir(target_dir: Path, story_id: str) -> Path:
    """Return the directory for a single story.

    :param target_dir: Country target directory.
    :param story_id: Story identifier from ``ranking.json``.
    :returns: Path to ``<country>/stories/<story_id>``.
    :raises ValueError: If ``story_id`` is not a safe path component.
    """
    if not is_valid_story_id(story_id):
        raise ValueError(f"Invalid story id: {story_id!r}")
    return stories_dir(target_dir) / story_id


def load_active_story(target_dir: Path) -> dict[str, Any] | None:
    """Load the country's ``.active_story`` lock.

    :param target_dir: Country target directory.
    :returns: The parsed lock dict, or None when absent or unreadable.
    """
    path = active_story_path(target_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if not is_valid_story_id(str(data.get("story_id", ""))):
        return None
    return data


def write_active_story(target_dir: Path, story_id: str, stage: str) -> None:
    """Create or update the country's ``.active_story`` lock.

    :param target_dir: Country target directory.
    :param story_id: Story the pipeline is now working on.
    :param stage: Pipeline stage that claimed the story.
    :raises ValueError: If ``story_id`` is not a safe path component.
    """
    if not is_valid_story_id(story_id):
        raise ValueError(f"Invalid story id: {story_id!r}")
    write_json_atomic(
        active_story_path(target_dir),
        {
            "story_id": story_id,
            "story_dir": str(story_dir(target_dir, story_id)),
            "stage": stage,
            "locked_at": utc_now_iso(),
        },
    )


def clear_active_story(target_dir: Path, story_id: str | None = None) -> bool:
    """Remove the ``.active_story`` lock.

    :param target_dir: Country target directory.
    :param story_id: When given, only clear the lock if it points at this story.
    :returns: True if a lock file was removed.
    """
    path = active_story_path(target_dir)
    if not path.exists():
        return False
    if story_id is not None:
        current = load_active_story(target_dir)
        if current is not None and current.get("story_id") != story_id:
            return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def set_story_state(target_dir: Path, story_id: str, state: str) -> bool:
    """Update a story's state in ``ranking.json``.

    :param target_dir: Country target directory.
    :param story_id: Story whose state should change.
    :param state: New state value.
    :returns: True if the ranking file was updated, False when the story or
        ranking file is missing.
    """
    ranking = load_ranking(target_dir)
    if ranking is None:
        return False
    changed = False
    for story in ranking_stories(ranking):
        if story.get("story_id") == story_id:
            story["state"] = state
            story["state_updated_at"] = utc_now_iso()
            changed = True
            break
    if not changed:
        return False
    ranking["last_updated"] = utc_now_iso()
    write_json_atomic(ranking_path(target_dir), ranking)
    return True


def load_compiled_stories(target_dir: Path) -> list[dict[str, Any]]:
    """Load every compiled story JSON file for a country.

    Files that cannot be parsed, or that lack a usable ``story_id``, are
    skipped. The returned dicts always carry ``story_id`` and ``file_path``.

    :param target_dir: Country target directory.
    :returns: List of compiled story dicts sorted by ``story_id``.
    """
    directory = compiled_dir(target_dir)
    if not directory.is_dir():
        return []
    stories: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        story_id = str(data.get("story_id", ""))
        if not is_valid_story_id(story_id):
            continue
        stories.append({**data, "story_id": story_id, "file_path": str(path)})
    return stories


def unranked_stories(target_dir: Path, date: str | None = None) -> list[dict[str, Any]]:
    """Return today's compiled stories that do not appear in ``ranking.json`` yet.

    The ranking is rebuilt every day from that day's compiled stories, so only
    stories compiled on ``date`` are candidates. Stories from previous days are
    never re-ranked.

    :param target_dir: Country target directory.
    :param date: Day to consider as ``YYYYMMDD``. Defaults to today (UTC).
    :returns: Compiled story dicts missing from the ranking, sorted by id.
    """
    day = (date or datetime.now(timezone.utc).strftime("%Y%m%d")).replace("-", "")
    ranked_ids = {
        str(s.get("story_id"))
        for s in ranking_stories(load_ranking(target_dir))
    }
    return [
        s for s in load_compiled_stories(target_dir)
        if s["story_id"] not in ranked_ids and _compiled_on(s, day)
    ]


def _compiled_on(story: dict[str, Any], date: str) -> bool:
    """Return True when a compiled story's timestamp falls on ``date``.

    :param story: Compiled story dict.
    :param date: Day in ``YYYYMMDD`` form.
    :returns: True if ``compilation_timestamp`` starts with that day.
    """
    timestamp = str(story.get("compilation_timestamp", ""))
    if not timestamp:
        return False
    return timestamp.replace("-", "")[:8] == date


def split_ranked_by_day(
    target_dir: Path,
    date: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split the existing ranking into same-day and earlier-day stories.

    ``save_ranking.py`` only accepts stories compiled on the day being ranked,
    so a story ranked on an earlier day cannot be resubmitted. Callers need to
    tell the two apart: the same-day ones must be included in the submitted
    order, the earlier-day ones must not.

    A ranking entry carries no compilation timestamp, so each one is matched
    against the compiled stories to find out when it was compiled. An entry
    whose compiled story is missing is treated as same-day, so it is still
    offered for resubmission rather than silently dropped.

    :param target_dir: Country target directory.
    :param date: Day to consider as ``YYYYMMDD``. Defaults to today (UTC).
    :returns: ``(same_day, earlier)`` ranking entries, each in rank order.
    """
    day = (date or datetime.now(timezone.utc).strftime("%Y%m%d")).replace("-", "")
    compiled = {str(s["story_id"]): s for s in load_compiled_stories(target_dir)}
    same_day: list[dict[str, Any]] = []
    earlier: list[dict[str, Any]] = []
    for story in ranking_stories(load_ranking(target_dir)):
        source = compiled.get(str(story["story_id"]))
        if source is not None and not _compiled_on(source, day):
            earlier.append(story)
        else:
            same_day.append(story)
    return same_day, earlier


def story_artifacts(story_path: Path) -> dict[str, bool]:
    """Report which pipeline artefacts exist in a story directory.

    :param story_path: Story directory.
    :returns: Mapping of artefact name to existence flag.
    """
    return {
        "research": (story_path / "research.md").exists(),
        "article": (story_path / "article.md").exists(),
        "rejected": (story_path / "rejected-article.md").exists(),
        "published": (story_path / "published.json").exists(),
        "gave_up": (story_path / ".gave_up").exists(),
    }


def derive_story_state(story_path: Path) -> str:
    """Derive a story's state from the artefacts in its directory.

    Mirrors the legacy ``_determine_state_from_dir`` helper so the ranking can
    be reconciled with the filesystem.

    :param story_path: Story directory.
    :returns: Derived state value.
    """
    found = story_artifacts(story_path)
    if found["gave_up"]:
        return STATE_DISCARDED
    if found["published"]:
        return STATE_PUBLISHED
    if found["rejected"]:
        return STATE_WRITING
    if found["article"]:
        return STATE_PUBLISHING
    if found["research"]:
        return STATE_WRITING
    return STATE_PENDING


def state_priority(state: str) -> int:
    """Return the advancement priority of a story state.

    :param state: Story state value.
    :returns: Higher values mean further along the pipeline.
    """
    return _STATE_PRIORITY.get(state, 0)


def rejection_count(story_path: Path) -> int:
    """Count the rejections recorded for a story.

    :param story_path: Story directory.
    :returns: Number of entries in ``rejection_log.json`` (0 when absent).
    """
    log_path = story_path / REJECTION_LOG_FILENAME
    if not log_path.exists():
        return 0
    try:
        data = json.loads(log_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return 0
    return len(data) if isinstance(data, list) else 0


def _normalise_story(entry: dict[str, Any], rank: int) -> dict[str, Any]:
    """Normalise a single ranking story entry.

    :param entry: Raw story dict, either from the agent or from a compiled file.
    :param rank: Position the story should occupy.
    :returns: Story dict with the canonical field set.
    """
    return {
        "rank": rank,
        "story_id": str(entry.get("story_id", "")),
        "title_en": str(entry.get("title_en", "")),
        "summary_en": str(entry.get("summary_en", "")),
        "category": str(entry.get("category", "")),
        "article_count": int(entry.get("article_count") or 0),
        "file_path": str(entry.get("file_path", "")),
        "state": story_state(entry),
    }


def build_ranking(
    country: str,
    date: str,
    ordered_stories: list[dict[str, Any]],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a complete ``ranking.json`` document.

    Stories already present in ``previous`` keep their recorded state so a
    re-rank never resets article progress.

    :param country: Country name (target name).
    :param date: Date in ``YYYYMMDD`` form.
    :param ordered_stories: Stories in the desired order (best first).
    :param previous: Existing ranking document, if any.
    :returns: Ranking document ready to be written.
    """
    previous_states = {
        str(s.get("story_id")): story_state(s)
        for s in ranking_stories(previous)
    }
    stories = []
    for index, entry in enumerate(ordered_stories, start=1):
        story = _normalise_story(entry, index)
        story["state"] = previous_states.get(story["story_id"], story["state"])
        stories.append(story)
    return {
        "date": date,
        "country": country,
        "version": RANKING_VERSION,
        "last_updated": utc_now_iso(),
        "stories": stories,
    }


def write_ranking(target_dir: Path, ranking: dict[str, Any]) -> None:
    """Atomically write a country's ``ranking.json``.

    :param target_dir: Country target directory.
    :param ranking: Ranking document to persist.
    """
    write_json_atomic(ranking_path(target_dir), ranking)


def validate_ranking(target_dir: Path, country: str, date: str) -> tuple[bool, str]:
    """Validate the ranking file an agent produced.

    Checks that the file exists, is a dict, targets the expected country/date,
    and that every story id is a safe path component.

    :param target_dir: Country target directory.
    :param country: Expected country name.
    :param date: Expected date in ``YYYYMMDD`` form.
    :returns: ``(True, "")`` when valid, otherwise ``(False, reason)``.
    """
    ranking = load_ranking(target_dir)
    if ranking is None:
        return False, f"{RANKING_FILENAME} is missing or not valid JSON"
    if str(ranking.get("country", "")) != country:
        return False, f"{RANKING_FILENAME} country is {ranking.get('country')!r}, expected {country!r}"
    if str(ranking.get("date", "")) != date:
        return False, f"{RANKING_FILENAME} date is {ranking.get('date')!r}, expected {date!r}"
    raw = ranking.get("stories")
    if not isinstance(raw, list) or not raw:
        return False, f"{RANKING_FILENAME} has no stories"
    for entry in raw:
        if not isinstance(entry, dict):
            return False, f"{RANKING_FILENAME} contains a non-object story entry"
        story_id = str(entry.get("story_id", ""))
        if not is_valid_story_id(story_id):
            return False, f"{RANKING_FILENAME} contains an invalid story id: {story_id!r}"
    return True, ""
