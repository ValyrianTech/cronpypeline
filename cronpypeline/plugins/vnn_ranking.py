"""VNN ranking state helpers.

Ranking is country-local and daily: every country target keeps one ranking per
day under ``rankings/<YYYY>/<MM>/<DD>/ranking.json``. The ranking for a day is
the authoritative record of which compiled stories exist for that country on
that day and how far each one has progressed through the article pipeline.
Keeping one file per day preserves history instead of overwriting yesterday's
ranking on every run.

Everything that grows without bound is partitioned by day, so a country
directory stays navigable after a year of operation::

    <country>/rankings/<YYYY>/<MM>/<DD>/ranking.json
    <country>/stories/<YYYY>/<MM>/<DD>/<story_id>/   # created by the research agent
    <country>/compiled_stories/<YYYY>/<MM>/<DD>/<story_id>.json
    <country>/news_links/<YYYY>/<MM>/<DD>/<link_id>.json
    <country>/compilation_metadata.json             # cumulative, bounded
    <country>/.active_story                         # the story currently being worked on

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
          "date": "20261021",
          "title_en": "...",
          "summary_en": "...",
          "category": "...",
          "article_count": 3,
          "file_path": "<country>/compiled_stories/2026/10/21/story_ac03c622.json",
          "state": "pending"
        }
      ]
    }

Each story entry carries its own ``date`` so a story directory can be located
without scanning the tree. ``story_id`` stays date-free: the day lives in the
path, which keeps ids stable across the front matter of published articles.

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
RANKINGS_SUBDIR = "rankings"
STORIES_SUBDIR = "stories"
COMPILED_SUBDIR = "compiled_stories"
NEWS_LINKS_SUBDIR = "news_links"
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
_DAY_RE = re.compile(r"^\d{8}$")


def today_utc() -> str:
    """Return the current UTC date in ``YYYYMMDD`` form.

    :returns: Today's date as an 8-digit string, e.g. ``"20261021"``.
    """
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def normalize_day(date: str | None = None) -> str:
    """Normalise a date to the ``YYYYMMDD`` form used in directory names.

    :param date: Date as ``YYYYMMDD`` or ``YYYY-MM-DD``. Defaults to today (UTC).
    :returns: The date in ``YYYYMMDD`` form.
    :raises ValueError: If the value is not a valid 8-digit day.
    """
    day = today_utc() if not date else str(date).replace("-", "")
    if not _DAY_RE.match(day):
        raise ValueError(f"Invalid date: {date!r} (expected YYYYMMDD or YYYY-MM-DD)")
    return day


def day_from_timestamp(value: Any) -> str:
    """Extract a ``YYYYMMDD`` day from an ISO timestamp or date string.

    :param value: Timestamp such as ``"2026-10-21T09:00:00+00:00"``, a date such
        as ``"2026-10-21"``, or an already-normalised ``"20261021"``.
    :returns: The 8-digit day, or an empty string when it cannot be determined.
    """
    text = str(value or "").replace("-", "")
    return text[:8] if _DAY_RE.match(text[:8]) else ""


def date_dir(root: Path, date: str | None = None) -> Path:
    """Return the ``<root>/<YYYY>/<MM>/<DD>`` directory for a day.

    :param root: Directory holding the date-partitioned tree.
    :param date: Day as ``YYYYMMDD`` or ``YYYY-MM-DD``. Defaults to today (UTC).
    :returns: Path to the day's directory.
    """
    day = normalize_day(date)
    return root / day[:4] / day[4:6] / day[6:8]


def utc_now_iso() -> str:
    """Return the current UTC timestamp as an ISO-8601 string.

    :returns: Timezone-aware ISO timestamp.
    """
    return datetime.now(timezone.utc).isoformat()


def rankings_root(target_dir: Path) -> Path:
    """Return the directory holding the per-day ranking files.

    :param target_dir: Country target directory.
    :returns: Path to the country's rankings directory.
    """
    return target_dir / RANKINGS_SUBDIR


def ranking_path(target_dir: Path, date: str | None = None) -> Path:
    """Return the ``ranking.json`` path for a day.

    :param target_dir: Country target directory.
    :param date: Day as ``YYYYMMDD`` or ``YYYY-MM-DD``. Defaults to today (UTC).
    :returns: Path to ``<country>/rankings/<YYYY>/<MM>/<DD>/ranking.json``.
    """
    return date_dir(rankings_root(target_dir), date) / RANKING_FILENAME


def stories_root(target_dir: Path) -> Path:
    """Return the root of the date-partitioned story directories.

    :param target_dir: Country target directory.
    :returns: Path to the country's stories directory.
    """
    return target_dir / STORIES_SUBDIR


def stories_dir(target_dir: Path, date: str | None = None) -> Path:
    """Return the directory holding a day's story subdirectories.

    :param target_dir: Country target directory.
    :param date: Day as ``YYYYMMDD`` or ``YYYY-MM-DD``. Defaults to today (UTC).
    :returns: Path to ``<country>/stories/<YYYY>/<MM>/<DD>``.
    """
    return date_dir(stories_root(target_dir), date)


def compiled_root(target_dir: Path) -> Path:
    """Return the root of the date-partitioned compiled stories.

    :param target_dir: Country target directory.
    :returns: Path to the country's compiled-stories directory.
    """
    return target_dir / COMPILED_SUBDIR


def compiled_dir(target_dir: Path, date: str | None = None) -> Path:
    """Return the directory holding a day's compiled story JSON files.

    :param target_dir: Country target directory.
    :param date: Day as ``YYYYMMDD`` or ``YYYY-MM-DD``. Defaults to today (UTC).
    :returns: Path to ``<country>/compiled_stories/<YYYY>/<MM>/<DD>``.
    """
    return date_dir(compiled_root(target_dir), date)


def news_links_root(target_dir: Path) -> Path:
    """Return the root of the date-partitioned news links.

    :param target_dir: Country target directory.
    :returns: Path to the country's news-links directory.
    """
    return target_dir / NEWS_LINKS_SUBDIR


def news_links_dir(target_dir: Path, date: str | None = None) -> Path:
    """Return the directory holding a day's archived news links.

    :param target_dir: Country target directory.
    :param date: Day as ``YYYYMMDD`` or ``YYYY-MM-DD``. Defaults to today (UTC).
    :returns: Path to ``<country>/news_links/<YYYY>/<MM>/<DD>``.
    """
    return date_dir(news_links_root(target_dir), date)


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


def ranking_days(target_dir: Path, upto: str | None = None) -> list[str]:
    """List the days that have a ranking file, newest first.

    :param target_dir: Country target directory.
    :param upto: Only include days at or before this one (``YYYYMMDD``).
        Defaults to no upper bound.
    :returns: Day strings in descending order.
    """
    root = rankings_root(target_dir)
    if not root.is_dir():
        return []
    days: list[str] = []
    for path in root.glob("*/*/*/" + RANKING_FILENAME):
        parts = path.parts[-4:-1]
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            days.append("".join(parts))
    if upto:
        days = [day for day in days if day <= upto]
    return sorted(days, reverse=True)


def load_ranking(target_dir: Path, date: str | None = None) -> dict[str, Any] | None:
    """Load the ranking that is current on ``date``.

    The ranking for ``date`` is preferred. When that day has no ranking yet
    (the pipeline is still working through the previous day's stories, or the
    ranking agent has not run), the most recent earlier ranking is returned so
    a story in progress is never lost at midnight.

    :param target_dir: Country target directory.
    :param date: Day as ``YYYYMMDD`` or ``YYYY-MM-DD``. Defaults to today (UTC).
    :returns: The parsed ranking dict, or None when no ranking exists at or
        before ``date``.
    """
    day = normalize_day(date)
    path = ranking_path(target_dir, day)
    if not path.exists():
        earlier = ranking_days(target_dir, upto=day)
        if not earlier:
            return None
        path = ranking_path(target_dir, earlier[0])
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


def story_dir(target_dir: Path, story_id: str, date: str | None = None) -> Path:
    """Return the directory for a single story.

    This is pure path construction: it does not touch the filesystem. Pass the
    story's own ``date`` when it is known (the ranking entry carries it);
    otherwise the path is built for today, which is what a story that has not
    been started yet needs.

    :param target_dir: Country target directory.
    :param story_id: Story identifier from ``ranking.json``.
    :param date: Day the story belongs to. Defaults to today (UTC).
    :returns: Path to ``<country>/stories/<YYYY>/<MM>/<DD>/<story_id>``.
    :raises ValueError: If ``story_id`` is not a safe path component.
    """
    if not is_valid_story_id(story_id):
        raise ValueError(f"Invalid story id: {story_id!r}")
    return stories_dir(target_dir, date) / story_id


def story_day(target_dir: Path, story_path: Path) -> str:
    """Return the ``YYYYMMDD`` day encoded in a story directory path.

    :param target_dir: Country target directory.
    :param story_path: Story directory, dated or legacy-flat.
    :returns: The day, or an empty string for a legacy flat directory.
    """
    try:
        parts = story_path.relative_to(stories_root(target_dir)).parts
    except ValueError:
        return ""
    if len(parts) >= 4 and all(part.isdigit() for part in parts[:3]):
        return "".join(parts[:3])
    return ""


def find_story_dir(
    target_dir: Path,
    story_id: str,
    date: str | None = None,
) -> Path | None:
    """Locate an existing story directory.

    With ``date`` the lookup is a single ``is_dir`` check. Without it the
    date-partitioned tree is scanned, and a legacy flat
    ``stories/<story_id>`` directory is accepted as a fallback.

    :param target_dir: Country target directory.
    :param story_id: Story identifier from ``ranking.json``.
    :param date: Day the story belongs to, when known.
    :returns: The story directory, or None when it does not exist.
    :raises ValueError: If ``story_id`` is not a safe path component.
    """
    if not is_valid_story_id(story_id):
        raise ValueError(f"Invalid story id: {story_id!r}")
    if date:
        candidate = story_dir(target_dir, story_id, date)
        return candidate if candidate.is_dir() else None

    root = stories_root(target_dir)
    if not root.is_dir():
        return None
    for candidate in sorted(root.glob(f"*/*/*/{story_id}")):
        if candidate.is_dir():
            return candidate
    legacy = root / story_id
    return legacy if legacy.is_dir() else None


def find_compiled_story(target_dir: Path, story_id: str) -> Path | None:
    """Locate a compiled story JSON file by id.

    :param target_dir: Country target directory.
    :param story_id: Story identifier.
    :returns: Path to the compiled file, or None when it does not exist.
    :raises ValueError: If ``story_id`` is not a safe path component.
    """
    if not is_valid_story_id(story_id):
        raise ValueError(f"Invalid story id: {story_id!r}")
    root = compiled_root(target_dir)
    if not root.is_dir():
        return None
    for candidate in sorted(root.glob(f"*/*/*/{story_id}.json")):
        if candidate.is_file():
            return candidate
    legacy = root / f"{story_id}.json"
    return legacy if legacy.is_file() else None


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


def write_active_story(
    target_dir: Path,
    story_id: str,
    stage: str,
    date: str | None = None,
) -> None:
    """Create or update the country's ``.active_story`` lock.

    :param target_dir: Country target directory.
    :param story_id: Story the pipeline is now working on.
    :param stage: Pipeline stage that claimed the story.
    :param date: Day the story belongs to. Defaults to today (UTC).
    :raises ValueError: If ``story_id`` is not a safe path component.
    """
    if not is_valid_story_id(story_id):
        raise ValueError(f"Invalid story id: {story_id!r}")
    day = normalize_day(date)
    write_json_atomic(
        active_story_path(target_dir),
        {
            "story_id": story_id,
            "story_dir": str(story_dir(target_dir, story_id, day)),
            "story_date": day,
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


def set_story_state(
    target_dir: Path,
    story_id: str,
    state: str,
    date: str | None = None,
) -> bool:
    """Update a story's state in the ranking that contains it.

    Rankings are per-day, so the story is looked up in the ranking for ``date``
    first and then in earlier rankings. A story that was ranked yesterday and
    is still being written therefore keeps its state in yesterday's ranking
    instead of silently failing to update.

    :param target_dir: Country target directory.
    :param story_id: Story whose state should change.
    :param state: New state value.
    :param date: Day to start looking from. Defaults to today (UTC).
    :returns: True if a ranking file was updated, False when the story is not
        in any ranking.
    """
    day = normalize_day(date)
    for candidate in ranking_days(target_dir, upto=day):
        ranking = load_ranking(target_dir, candidate)
        if ranking is None:
            continue
        changed = False
        for story in ranking_stories(ranking):
            if story.get("story_id") == story_id:
                story["state"] = state
                story["state_updated_at"] = utc_now_iso()
                changed = True
                break
        if not changed:
            continue
        ranking["last_updated"] = utc_now_iso()
        write_ranking(target_dir, ranking)
        return True
    return False


def load_compiled_stories(
    target_dir: Path,
    date: str | None = None,
) -> list[dict[str, Any]]:
    """Load compiled story JSON files for a country.

    Files that cannot be parsed, or that lack a usable ``story_id``, are
    skipped. The returned dicts always carry ``story_id`` and ``file_path``.

    :param target_dir: Country target directory.
    :param date: When given, only that day's directory is read. When omitted,
        every day is read.
    :returns: List of compiled story dicts sorted by ``story_id``.
    """
    root = compiled_root(target_dir)
    if not root.is_dir():
        return []
    if date:
        paths = sorted(compiled_dir(target_dir, date).glob("*.json"))
    else:
        paths = sorted(root.rglob("*.json"))
    stories: list[dict[str, Any]] = []
    for path in paths:
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
    """Return a day's compiled stories that do not appear in the ranking yet.

    The ranking is rebuilt every day from that day's compiled stories, so only
    stories compiled on ``date`` are candidates. Stories from previous days are
    never re-ranked.

    :param target_dir: Country target directory.
    :param date: Day to consider as ``YYYYMMDD``. Defaults to today (UTC).
    :returns: Compiled story dicts missing from the ranking, sorted by id.
    """
    day = normalize_day(date)
    ranked_ids = {
        str(s.get("story_id"))
        for s in ranking_stories(load_ranking(target_dir, day))
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
    day = normalize_day(date)
    compiled = {str(s["story_id"]): s for s in load_compiled_stories(target_dir)}
    same_day: list[dict[str, Any]] = []
    earlier: list[dict[str, Any]] = []
    for story in ranking_stories(load_ranking(target_dir, day)):
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


def _normalise_story(
    entry: dict[str, Any],
    rank: int,
    date: str,
    previous_date: str | None = None,
) -> dict[str, Any]:
    """Normalise a single ranking story entry.

    :param entry: Raw story dict, either from the agent or from a compiled file.
    :param rank: Position the story should occupy.
    :param date: Day of the ranking, used when the entry carries no date.
    :param previous_date: Day recorded for this story by an earlier ranking.
    :returns: Story dict with the canonical field set.
    """
    return {
        "rank": rank,
        "story_id": str(entry.get("story_id", "")),
        "date": (
            day_from_timestamp(entry.get("date"))
            or day_from_timestamp(entry.get("compilation_timestamp"))
            or previous_date
            or date
        ),
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
    re-rank never resets article progress. Each entry records the day its story
    was compiled, so the story directory can be located without a scan.

    :param country: Country name (target name).
    :param date: Date in ``YYYYMMDD`` form.
    :param ordered_stories: Stories in the desired order (best first).
    :param previous: Existing ranking document, if any.
    :returns: Ranking document ready to be written.
    """
    day = normalize_day(date)
    previous_states = {
        str(s.get("story_id")): story_state(s)
        for s in ranking_stories(previous)
    }
    previous_dates = {
        str(s.get("story_id")): day_from_timestamp(s.get("date"))
        for s in ranking_stories(previous)
    }
    stories = []
    for index, entry in enumerate(ordered_stories, start=1):
        story_id = str(entry.get("story_id", ""))
        story = _normalise_story(entry, index, day, previous_dates.get(story_id))
        story["state"] = previous_states.get(story["story_id"], story["state"])
        stories.append(story)
    return {
        "date": day,
        "country": country,
        "version": RANKING_VERSION,
        "last_updated": utc_now_iso(),
        "stories": stories,
    }


def write_ranking(target_dir: Path, ranking: dict[str, Any]) -> None:
    """Atomically write a ranking to its own day's file.

    :param target_dir: Country target directory.
    :param ranking: Ranking document to persist. Its ``date`` selects the file.
    """
    write_json_atomic(ranking_path(target_dir, ranking.get("date")), ranking)


def validate_ranking(target_dir: Path, country: str, date: str) -> tuple[bool, str]:
    """Validate the ranking file an agent produced for a day.

    Checks that the day's file exists, is a dict, targets the expected
    country/date, and that every story id is a safe path component.

    :param target_dir: Country target directory.
    :param country: Expected country name.
    :param date: Expected date in ``YYYYMMDD`` form.
    :returns: ``(True, "")`` when valid, otherwise ``(False, reason)``.
    """
    day = normalize_day(date)
    path = ranking_path(target_dir, day)
    if not path.exists():
        return False, f"{RANKING_FILENAME} is missing or not valid JSON"
    try:
        ranking = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False, f"{RANKING_FILENAME} is missing or not valid JSON"
    if not isinstance(ranking, dict):
        return False, f"{RANKING_FILENAME} is missing or not valid JSON"
    if str(ranking.get("country", "")) != country:
        return False, f"{RANKING_FILENAME} country is {ranking.get('country')!r}, expected {country!r}"
    if str(ranking.get("date", "")) != day:
        return False, f"{RANKING_FILENAME} date is {ranking.get('date')!r}, expected {day!r}"
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
