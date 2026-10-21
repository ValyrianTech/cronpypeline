"""Tests for the VNN ranking state helpers."""

import json
from pathlib import Path

import pytest

from cronpypeline.plugins import vnn_ranking
from cronpypeline.plugins.vnn_ranking import (
    STATE_DISCARDED,
    STATE_PENDING,
    STATE_PUBLISHED,
    STATE_PUBLISHING,
    STATE_WRITING,
    build_ranking,
    clear_active_story,
    derive_story_state,
    is_valid_story_id,
    load_active_story,
    load_compiled_stories,
    load_ranking,
    ranking_stories,
    rejection_count,
    set_story_state,
    state_priority,
    story_dir,
    today_utc,
    unranked_stories,
    validate_ranking,
    write_active_story,
    write_ranking,
)


def _write_compiled(target_dir: Path, story_id: str, **fields) -> Path:
    """Write a compiled story JSON file into the country's compiled dir."""
    directory = target_dir / "compiled_stories"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{story_id}.json"
    path.write_text(json.dumps({"story_id": story_id, **fields}), encoding="utf-8")
    return path


def _write_ranking(target_dir: Path, stories, country="belgium", date="20261021") -> None:
    """Write a ranking.json document into the country target dir."""
    write_ranking(target_dir, {
        "date": date,
        "country": country,
        "version": 1,
        "stories": stories,
    })


class TestTodayUtc:
    def test_returns_eight_digit_date(self):
        value = today_utc()
        assert len(value) == 8
        assert value.isdigit()


class TestStoryIds:
    @pytest.mark.parametrize("story_id", ["story_abc123", "story-1", "a.b", "S1"])
    def test_valid_ids(self, story_id):
        assert is_valid_story_id(story_id) is True

    @pytest.mark.parametrize("story_id", ["", ".", "..", "../etc", "a/b", "a\\b"])
    def test_invalid_ids(self, story_id):
        assert is_valid_story_id(story_id) is False

    def test_story_dir_rejects_traversal(self, tmp_path):
        with pytest.raises(ValueError):
            story_dir(tmp_path, "../escape")

    def test_story_dir_is_country_local(self, tmp_path):
        assert story_dir(tmp_path, "story_1") == tmp_path / "stories" / "story_1"


class TestLoadRanking:
    def test_missing_file_returns_none(self, tmp_path):
        assert load_ranking(tmp_path) is None

    def test_invalid_json_returns_none(self, tmp_path):
        (tmp_path / "ranking.json").write_text("{not json", encoding="utf-8")
        assert load_ranking(tmp_path) is None

    def test_non_dict_returns_none(self, tmp_path):
        (tmp_path / "ranking.json").write_text("[]", encoding="utf-8")
        assert load_ranking(tmp_path) is None

    def test_valid_ranking_roundtrip(self, tmp_path):
        _write_ranking(tmp_path, [{"rank": 1, "story_id": "story_1", "state": "pending"}])
        ranking = load_ranking(tmp_path)
        assert ranking is not None
        assert ranking["country"] == "belgium"


class TestRankingStories:
    def test_sorted_by_rank(self, tmp_path):
        _write_ranking(tmp_path, [
            {"rank": 2, "story_id": "story_b"},
            {"rank": 1, "story_id": "story_a"},
        ])
        stories = ranking_stories(load_ranking(tmp_path))
        assert [s["story_id"] for s in stories] == ["story_a", "story_b"]

    def test_drops_unsafe_ids(self, tmp_path):
        _write_ranking(tmp_path, [
            {"rank": 1, "story_id": "../escape"},
            {"rank": 2, "story_id": "story_ok"},
        ])
        stories = ranking_stories(load_ranking(tmp_path))
        assert [s["story_id"] for s in stories] == ["story_ok"]

    def test_none_ranking(self):
        assert ranking_stories(None) == []

    def test_non_list_stories(self):
        assert ranking_stories({"stories": "nope"}) == []


class TestActiveStory:
    def test_missing_lock(self, tmp_path):
        assert load_active_story(tmp_path) is None

    def test_write_and_load(self, tmp_path):
        write_active_story(tmp_path, "story_1", "research")
        lock = load_active_story(tmp_path)
        assert lock is not None
        assert lock["story_id"] == "story_1"
        assert lock["stage"] == "research"
        assert lock["story_dir"].endswith("stories/story_1")

    def test_write_rejects_traversal(self, tmp_path):
        with pytest.raises(ValueError):
            write_active_story(tmp_path, "../escape", "research")

    def test_invalid_lock_returns_none(self, tmp_path):
        (tmp_path / ".active_story").write_text(json.dumps({"story_id": "../x"}), encoding="utf-8")
        assert load_active_story(tmp_path) is None

    def test_clear_removes_lock(self, tmp_path):
        write_active_story(tmp_path, "story_1", "research")
        assert clear_active_story(tmp_path) is True
        assert load_active_story(tmp_path) is None

    def test_clear_other_story_keeps_lock(self, tmp_path):
        write_active_story(tmp_path, "story_1", "research")
        assert clear_active_story(tmp_path, "story_2") is False
        assert load_active_story(tmp_path) is not None

    def test_clear_missing_lock(self, tmp_path):
        assert clear_active_story(tmp_path) is False


class TestSetStoryState:
    def test_updates_state(self, tmp_path):
        _write_ranking(tmp_path, [{"rank": 1, "story_id": "story_1", "state": "pending"}])
        assert set_story_state(tmp_path, "story_1", "writing") is True
        stories = ranking_stories(load_ranking(tmp_path))
        assert stories[0]["state"] == "writing"
        assert "state_updated_at" in stories[0]

    def test_unknown_story_returns_false(self, tmp_path):
        _write_ranking(tmp_path, [{"rank": 1, "story_id": "story_1", "state": "pending"}])
        assert set_story_state(tmp_path, "story_2", "writing") is False

    def test_missing_ranking_returns_false(self, tmp_path):
        assert set_story_state(tmp_path, "story_1", "writing") is False


class TestCompiledStories:
    def test_loads_valid_files(self, tmp_path):
        _write_compiled(tmp_path, "story_1", title_en="One")
        _write_compiled(tmp_path, "story_2", title_en="Two")
        stories = load_compiled_stories(tmp_path)
        assert [s["story_id"] for s in stories] == ["story_1", "story_2"]
        assert stories[0]["file_path"].endswith("story_1.json")

    def test_skips_invalid_json(self, tmp_path):
        directory = tmp_path / "compiled_stories"
        directory.mkdir()
        (directory / "broken.json").write_text("{nope", encoding="utf-8")
        _write_compiled(tmp_path, "story_1")
        assert [s["story_id"] for s in load_compiled_stories(tmp_path)] == ["story_1"]

    def test_skips_unsafe_ids(self, tmp_path):
        directory = tmp_path / "compiled_stories"
        directory.mkdir()
        (directory / "bad.json").write_text(json.dumps({"story_id": "../x"}), encoding="utf-8")
        assert load_compiled_stories(tmp_path) == []

    def test_missing_dir(self, tmp_path):
        assert load_compiled_stories(tmp_path) == []

    def test_unranked_excludes_ranked(self, tmp_path):
        _write_compiled(tmp_path, "story_1")
        _write_compiled(tmp_path, "story_2")
        _write_ranking(tmp_path, [{"rank": 1, "story_id": "story_1", "state": "pending"}])
        assert [s["story_id"] for s in unranked_stories(tmp_path)] == ["story_2"]

    def test_unranked_without_ranking(self, tmp_path):
        _write_compiled(tmp_path, "story_1")
        assert [s["story_id"] for s in unranked_stories(tmp_path)] == ["story_1"]


class TestDeriveStoryState:
    def test_empty_story_is_pending(self, tmp_path):
        assert derive_story_state(tmp_path) == STATE_PENDING

    def test_research_means_writing(self, tmp_path):
        (tmp_path / "research.md").write_text("r", encoding="utf-8")
        assert derive_story_state(tmp_path) == STATE_WRITING

    def test_article_means_publishing(self, tmp_path):
        (tmp_path / "article.md").write_text("a", encoding="utf-8")
        assert derive_story_state(tmp_path) == STATE_PUBLISHING

    def test_published_wins(self, tmp_path):
        (tmp_path / "article.md").write_text("a", encoding="utf-8")
        (tmp_path / "published.json").write_text("{}", encoding="utf-8")
        assert derive_story_state(tmp_path) == STATE_PUBLISHED

    def test_gave_up_is_discarded(self, tmp_path):
        (tmp_path / "published.json").write_text("{}", encoding="utf-8")
        (tmp_path / ".gave_up").write_text("{}", encoding="utf-8")
        assert derive_story_state(tmp_path) == STATE_DISCARDED

    def test_rejected_without_article_means_writing(self, tmp_path):
        (tmp_path / "rejected-article.md").write_text("r", encoding="utf-8")
        assert derive_story_state(tmp_path) == STATE_WRITING


class TestStatePriority:
    def test_ordering(self):
        assert state_priority(STATE_PENDING) < state_priority(STATE_WRITING)
        assert state_priority(STATE_WRITING) < state_priority(STATE_PUBLISHING)
        assert state_priority(STATE_PUBLISHING) < state_priority(STATE_PUBLISHED)
        assert state_priority(STATE_PUBLISHED) < state_priority(STATE_DISCARDED)

    def test_unknown_state(self):
        assert state_priority("bogus") == 0


class TestRejectionCount:
    def test_missing_log(self, tmp_path):
        assert rejection_count(tmp_path) == 0

    def test_counts_entries(self, tmp_path):
        (tmp_path / "rejection_log.json").write_text(
            json.dumps([{"reason": "a"}, {"reason": "b"}]), encoding="utf-8"
        )
        assert rejection_count(tmp_path) == 2

    def test_non_list_log(self, tmp_path):
        (tmp_path / "rejection_log.json").write_text("{}", encoding="utf-8")
        assert rejection_count(tmp_path) == 0

    def test_invalid_json(self, tmp_path):
        (tmp_path / "rejection_log.json").write_text("{nope", encoding="utf-8")
        assert rejection_count(tmp_path) == 0


class TestBuildRanking:
    def test_assigns_sequential_ranks(self):
        ranking = build_ranking("belgium", "20261021", [
            {"story_id": "story_b", "title_en": "B"},
            {"story_id": "story_a", "title_en": "A"},
        ])
        assert [s["rank"] for s in ranking["stories"]] == [1, 2]
        assert [s["story_id"] for s in ranking["stories"]] == ["story_b", "story_a"]

    def test_preserves_previous_state(self):
        previous = {"stories": [{"rank": 1, "story_id": "story_a", "state": "published"}]}
        ranking = build_ranking(
            "belgium", "20261021",
            [{"story_id": "story_a"}, {"story_id": "story_b"}],
            previous=previous,
        )
        states = {s["story_id"]: s["state"] for s in ranking["stories"]}
        assert states["story_a"] == "published"
        assert states["story_b"] == "pending"

    def test_metadata(self):
        ranking = build_ranking("belgium", "20261021", [{"story_id": "story_a"}])
        assert ranking["country"] == "belgium"
        assert ranking["date"] == "20261021"
        assert ranking["version"] == vnn_ranking.RANKING_VERSION
        assert "last_updated" in ranking

    def test_normalises_fields(self):
        ranking = build_ranking("belgium", "20261021", [{"story_id": "story_a", "article_count": "4"}])
        story = ranking["stories"][0]
        assert story["article_count"] == 4
        assert story["title_en"] == ""
        assert story["file_path"] == ""


class TestValidateRanking:
    def test_missing_file(self, tmp_path):
        ok, reason = validate_ranking(tmp_path, "belgium", "20261021")
        assert ok is False
        assert "missing" in reason

    def test_valid(self, tmp_path):
        _write_ranking(tmp_path, [{"rank": 1, "story_id": "story_1"}])
        assert validate_ranking(tmp_path, "belgium", "20261021") == (True, "")

    def test_wrong_country(self, tmp_path):
        _write_ranking(tmp_path, [{"rank": 1, "story_id": "story_1"}], country="france")
        ok, reason = validate_ranking(tmp_path, "belgium", "20261021")
        assert ok is False
        assert "country" in reason

    def test_wrong_date(self, tmp_path):
        _write_ranking(tmp_path, [{"rank": 1, "story_id": "story_1"}], date="20260101")
        ok, reason = validate_ranking(tmp_path, "belgium", "20261021")
        assert ok is False
        assert "date" in reason

    def test_empty_stories(self, tmp_path):
        _write_ranking(tmp_path, [])
        ok, reason = validate_ranking(tmp_path, "belgium", "20261021")
        assert ok is False
        assert "no stories" in reason

    def test_non_object_story(self, tmp_path):
        _write_ranking(tmp_path, ["nope"])
        ok, reason = validate_ranking(tmp_path, "belgium", "20261021")
        assert ok is False
        assert "non-object" in reason

    def test_invalid_story_id(self, tmp_path):
        _write_ranking(tmp_path, [{"rank": 1, "story_id": "../x"}])
        ok, reason = validate_ranking(tmp_path, "belgium", "20261021")
        assert ok is False
        assert "invalid story id" in reason
