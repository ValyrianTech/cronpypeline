"""Tests for the VNN plugin triggers, actions and hooks."""

import json
import os
import time
from pathlib import Path

import pytest

from cronpypeline.plugins import vnn_plugin
from cronpypeline.plugins.vnn_plugin import (
    DEFAULT_PROCESSING_TIMEOUT_MINUTES,
    detect_publishing_needed,
    detect_research_needed,
    detect_revision_needed,
    detect_writing_needed,
)
from cronpypeline.plugins.vnn_ranking import write_active_story, write_ranking

COUNTRY = "china"
DATE = "20261022"
STORY_ID = "story_4dd1c317"


def _write_ranking(target_dir: Path, stories) -> None:
    """Write a ranking.json document for the country."""
    write_ranking(target_dir, {
        "date": DATE,
        "country": COUNTRY,
        "version": 1,
        "stories": stories,
    })


def _make_story(target_dir: Path, story_id: str, *artefacts: str) -> Path:
    """Create a story directory containing the given artefact files."""
    path = target_dir / "stories" / story_id
    path.mkdir(parents=True, exist_ok=True)
    for name in artefacts:
        (path / name).write_text("{}", encoding="utf-8")
    return path


def _write_processing(target_dir: Path, queue_file: Path | None = None, age_seconds: float = 0) -> Path:
    """Write the country-level processing marker, optionally aged."""
    marker = target_dir / vnn_plugin.PROCESSING_MARKER
    content = {"retry_count": 0}
    if queue_file is not None:
        content["queue_file"] = str(queue_file)
    marker.write_text(json.dumps(content), encoding="utf-8")
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(marker, (old, old))
    return marker


def _context(target_dir: Path) -> dict:
    """Build a trigger context dict for the country."""
    return {
        "target": COUNTRY,
        "target_dir": str(target_dir),
        "workspace_dir": str(target_dir.parent),
        "target_config": {},
    }


@pytest.fixture
def country(tmp_path: Path) -> Path:
    """A country directory with one claimed story that still needs research."""
    target_dir = tmp_path / COUNTRY
    target_dir.mkdir()
    _write_ranking(target_dir, [{"story_id": STORY_ID, "state": "pending", "rank": 1}])
    _make_story(target_dir, STORY_ID, "story.json")
    write_active_story(target_dir, STORY_ID, "research")
    return target_dir


class TestProcessingGate:
    def test_no_marker_is_not_processing(self, country: Path):
        assert vnn_plugin._is_processing(country) is False

    def test_fresh_marker_with_live_queue_file_is_processing(self, country: Path, tmp_path: Path):
        queue_file = tmp_path / "queue" / "NewsResearchAgent_1.json"
        queue_file.parent.mkdir()
        queue_file.write_text("{}", encoding="utf-8")
        _write_processing(country, queue_file)
        assert vnn_plugin._is_processing(country, 60) is True

    def test_marker_older_than_timeout_is_stale(self, country: Path, tmp_path: Path):
        queue_file = tmp_path / "queue" / "NewsResearchAgent_1.json"
        queue_file.parent.mkdir()
        queue_file.write_text("{}", encoding="utf-8")
        _write_processing(country, queue_file, age_seconds=61 * 60)
        assert vnn_plugin._is_processing(country, 60) is False

    def test_missing_queue_file_is_stale(self, country: Path, tmp_path: Path):
        queue_file = tmp_path / "queue" / "NewsResearchAgent_1.json"
        queue_file.parent.mkdir()
        _write_processing(country, queue_file)
        assert vnn_plugin._is_processing(country, 60) is False

    def test_missing_queue_file_with_reminder_is_processing(self, country: Path, tmp_path: Path):
        queue_file = tmp_path / "queue" / "NewsResearchAgent_1.json"
        queue_file.parent.mkdir()
        (queue_file.parent / "NewsResearchAgent_1_reminder_2.json").write_text("{}", encoding="utf-8")
        _write_processing(country, queue_file)
        assert vnn_plugin._is_processing(country, 60) is True

    def test_marker_without_queue_file_is_processing(self, country: Path):
        _write_processing(country)
        assert vnn_plugin._is_processing(country, 60) is True

    def test_corrupt_marker_is_processing(self, country: Path):
        (country / vnn_plugin.PROCESSING_MARKER).write_text("not json", encoding="utf-8")
        assert vnn_plugin._is_processing(country, 60) is True


class TestStageTimeout:
    def test_defaults_when_context_has_no_stage(self, country: Path):
        assert vnn_plugin._stage_timeout(_context(country)) == DEFAULT_PROCESSING_TIMEOUT_MINUTES

    def test_uses_stage_timeout(self, country: Path):
        stage = type("S", (), {"timeout_minutes": 15})()
        context = {**_context(country), "stage": stage}
        assert vnn_plugin._stage_timeout(context) == 15


class TestStaleProcessingDoesNotBlockTriggers:
    def test_research_fires_when_processing_marker_is_stale(self, country: Path, tmp_path: Path):
        queue_file = tmp_path / "queue" / "NewsResearchAgent_1.json"
        queue_file.parent.mkdir()
        _write_processing(country, queue_file)
        assert detect_research_needed(_context(country)) is True

    def test_research_blocked_while_agent_runs(self, country: Path, tmp_path: Path):
        queue_file = tmp_path / "queue" / "NewsResearchAgent_1.json"
        queue_file.parent.mkdir()
        queue_file.write_text("{}", encoding="utf-8")
        _write_processing(country, queue_file)
        assert detect_research_needed(_context(country)) is False

    def test_writing_fires_when_processing_marker_is_stale(self, country: Path):
        _make_story(country, STORY_ID, "story.json", "research.md")
        _write_processing(country, age_seconds=61 * 60)
        assert detect_writing_needed(_context(country)) is True

    def test_publishing_fires_when_processing_marker_is_stale(self, country: Path):
        _make_story(country, STORY_ID, "story.json", "research.md", "article.md")
        _write_processing(country, age_seconds=61 * 60)
        assert detect_publishing_needed(_context(country)) is True

    def test_revision_fires_when_processing_marker_is_stale(self, country: Path):
        _make_story(country, STORY_ID, "story.json", "rejected-article.md")
        _write_processing(country, age_seconds=61 * 60)
        assert detect_revision_needed(_context(country)) is True


class TestActiveStorySkipsFinishedStories:
    """A finished story must never pin the pipeline, however it is selected."""

    def test_stale_lock_on_published_story_falls_through_to_ranking(self, country: Path):
        _make_story(country, STORY_ID, "story.json", "research.md", "article.md", "published.json")
        next_id = "story_9a8b7c6d"
        _write_ranking(country, [
            {"story_id": STORY_ID, "state": "publishing", "rank": 1},
            {"story_id": next_id, "state": "pending", "rank": 2},
        ])

        resolved = vnn_plugin._active_story(country)

        assert resolved is not None
        assert resolved[0] == next_id

    def test_stale_lock_on_given_up_story_falls_through_to_ranking(self, country: Path):
        _make_story(country, STORY_ID, "story.json", ".gave_up")
        next_id = "story_9a8b7c6d"
        _write_ranking(country, [
            {"story_id": STORY_ID, "state": "pending", "rank": 1},
            {"story_id": next_id, "state": "pending", "rank": 2},
        ])

        resolved = vnn_plugin._active_story(country)

        assert resolved is not None
        assert resolved[0] == next_id

    def test_ranking_state_lagging_behind_filesystem_is_skipped(self, country: Path):
        (country / ".active_story").unlink()
        published_id = "story_11111111"
        pending_id = "story_22222222"
        _make_story(country, published_id, "story.json", "research.md", "article.md", "published.json")
        _write_ranking(country, [
            {"story_id": published_id, "state": "pending", "rank": 1},
            {"story_id": pending_id, "state": "pending", "rank": 2},
        ])

        resolved = vnn_plugin._active_story(country)

        assert resolved is not None
        assert resolved[0] == pending_id

    def test_research_fires_for_unstarted_story_behind_published_one(self, country: Path):
        (country / ".active_story").unlink()
        published_id = "story_11111111"
        pending_id = "story_22222222"
        _make_story(country, published_id, "story.json", "research.md", "article.md", "published.json")
        _write_ranking(country, [
            {"story_id": published_id, "state": "pending", "rank": 1},
            {"story_id": pending_id, "state": "pending", "rank": 2},
        ])

        assert detect_research_needed(_context(country)) is True

    def test_returns_none_when_every_story_is_finished(self, country: Path):
        _make_story(country, STORY_ID, "story.json", "research.md", "article.md", "published.json")
        _write_ranking(country, [{"story_id": STORY_ID, "state": "pending", "rank": 1}])

        assert vnn_plugin._active_story(country) is None


class TestMarkerContextSkipsFinishedStories:
    """The marker context must agree with the triggers and the actions.

    Reading the raw ``.active_story`` lock made the story-scoped completion
    markers resolve into a completed story's directory, so the stages derived
    as complete and target selection skipped the country even though its
    triggers fired.
    """

    def test_stale_lock_on_published_story_points_at_next_story(self, country: Path):
        _make_story(country, STORY_ID, "story.json", "research.md", "article.md", "published.json")
        next_id = "story_9a8b7c6d"
        _write_ranking(country, [
            {"story_id": STORY_ID, "state": "publishing", "rank": 1},
            {"story_id": next_id, "state": "pending", "rank": 2},
        ])

        result = vnn_plugin.vnn_marker_context({"target_dir": str(country)})

        assert result == {"active_story": next_id}

    def test_empty_when_every_story_is_finished(self, country: Path):
        _make_story(country, STORY_ID, "story.json", "research.md", "article.md", "published.json")
        _write_ranking(country, [{"story_id": STORY_ID, "state": "pending", "rank": 1}])

        result = vnn_plugin.vnn_marker_context({"target_dir": str(country)})

        assert result == {"active_story": ""}

    def test_claimed_story_still_reported(self, country: Path):
        assert vnn_plugin.vnn_marker_context({"target_dir": str(country)}) == {
            "active_story": STORY_ID
        }
