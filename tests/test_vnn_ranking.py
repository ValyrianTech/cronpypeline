"""Tests for the VNN ranking state helpers."""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

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
    day_from_timestamp,
    derive_story_state,
    find_compiled_story,
    find_story_dir,
    is_valid_story_id,
    load_active_story,
    load_compiled_stories,
    load_ranking,
    normalize_day,
    ranking_days,
    ranking_stories,
    rejection_count,
    set_story_state,
    split_ranked_by_day,
    state_priority,
    story_day,
    story_dir,
    story_state,
    today_utc,
    unranked_stories,
    validate_ranking,
    write_active_story,
    write_ranking,
)


def _write_compiled(target_dir: Path, story_id: str, date: str | None = None, **fields) -> Path:
    """Write a compiled story JSON file into the country's dated compiled dir."""
    fields.setdefault("compilation_timestamp", datetime.now(timezone.utc).isoformat())
    day = date or day_from_timestamp(fields["compilation_timestamp"]) or today_utc()
    directory = vnn_ranking.compiled_dir(target_dir, day)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{story_id}.json"
    path.write_text(json.dumps({"story_id": story_id, **fields}), encoding="utf-8")
    return path


def _ranking_file(target_dir: Path, date: str = "20261021") -> Path:
    """Return a day's ranking file path, creating its directory."""
    path = vnn_ranking.ranking_path(target_dir, date)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_ranking(target_dir: Path, stories, country="belgium", date="20261021") -> None:
    """Write a ranking.json document into the country's dated rankings dir."""
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


class TestDateHelpers:
    def test_normalize_day_accepts_both_forms(self):
        assert normalize_day("2026-10-21") == "20261021"
        assert normalize_day("20261021") == "20261021"

    def test_normalize_day_defaults_to_today(self):
        assert normalize_day() == today_utc()

    def test_normalize_day_rejects_garbage(self):
        with pytest.raises(ValueError):
            normalize_day("not-a-date")

    def test_day_from_timestamp(self):
        assert day_from_timestamp("2026-10-21T09:00:00+00:00") == "20261021"
        assert day_from_timestamp("2026-10-21") == "20261021"
        assert day_from_timestamp("20261021") == "20261021"
        assert day_from_timestamp("") == ""
        assert day_from_timestamp(None) == ""

    def test_date_dir_is_year_month_day(self, tmp_path):
        assert vnn_ranking.date_dir(tmp_path, "20261021") == tmp_path / "2026" / "10" / "21"

    def test_partitioned_paths(self, tmp_path):
        assert vnn_ranking.ranking_path(tmp_path, "20261021") == \
            tmp_path / "rankings" / "2026" / "10" / "21" / "ranking.json"
        assert vnn_ranking.stories_dir(tmp_path, "20261021") == \
            tmp_path / "stories" / "2026" / "10" / "21"
        assert vnn_ranking.compiled_dir(tmp_path, "20261021") == \
            tmp_path / "compiled_stories" / "2026" / "10" / "21"
        assert vnn_ranking.news_links_dir(tmp_path, "20261021") == \
            tmp_path / "news_links" / "2026" / "10" / "21"


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
        assert story_dir(tmp_path, "story_1", "20261021") == \
            tmp_path / "stories" / "2026" / "10" / "21" / "story_1"

    def test_story_dir_defaults_to_today(self, tmp_path):
        day = today_utc()
        assert story_dir(tmp_path, "story_1") == \
            tmp_path / "stories" / day[:4] / day[4:6] / day[6:8] / "story_1"

    def test_story_day_reads_the_partition(self, tmp_path):
        path = tmp_path / "stories" / "2026" / "10" / "21" / "story_1"
        assert story_day(tmp_path, path) == "20261021"

    def test_story_day_of_legacy_dir_is_empty(self, tmp_path):
        assert story_day(tmp_path, tmp_path / "stories" / "story_1") == ""

    def test_story_day_outside_stories_root_is_empty(self, tmp_path):
        assert story_day(tmp_path, tmp_path / "elsewhere" / "story_1") == ""


class TestStoryState:
    def test_unknown_state_falls_back_to_pending(self):
        assert story_state({"state": "bogus"}) == STATE_PENDING

    def test_known_state_is_preserved(self):
        assert story_state({"state": "published"}) == STATE_PUBLISHED


class TestFindStoryDir:
    def test_missing_returns_none(self, tmp_path):
        assert find_story_dir(tmp_path, "story_1") is None

    def test_finds_dated_dir_without_date(self, tmp_path):
        path = tmp_path / "stories" / "2026" / "10" / "21" / "story_1"
        path.mkdir(parents=True)
        assert find_story_dir(tmp_path, "story_1") == path

    def test_finds_dated_dir_with_date(self, tmp_path):
        path = tmp_path / "stories" / "2026" / "10" / "21" / "story_1"
        path.mkdir(parents=True)
        assert find_story_dir(tmp_path, "story_1", "20261021") == path

    def test_wrong_date_returns_none(self, tmp_path):
        (tmp_path / "stories" / "2026" / "10" / "21" / "story_1").mkdir(parents=True)
        assert find_story_dir(tmp_path, "story_1", "20261022") is None

    def test_finds_legacy_flat_dir(self, tmp_path):
        path = tmp_path / "stories" / "story_1"
        path.mkdir(parents=True)
        assert find_story_dir(tmp_path, "story_1") == path

    def test_rejects_traversal(self, tmp_path):
        with pytest.raises(ValueError):
            find_story_dir(tmp_path, "../escape")


class TestFindCompiledStory:
    def test_missing_returns_none(self, tmp_path):
        assert find_compiled_story(tmp_path, "story_1") is None

    def test_finds_dated_file(self, tmp_path):
        path = _write_compiled(tmp_path, "story_1", "20261021")
        assert find_compiled_story(tmp_path, "story_1") == path

    def test_finds_legacy_flat_file(self, tmp_path):
        directory = tmp_path / "compiled_stories"
        directory.mkdir()
        path = directory / "story_1.json"
        path.write_text(json.dumps({"story_id": "story_1"}), encoding="utf-8")
        assert find_compiled_story(tmp_path, "story_1") == path

    def test_rejects_traversal(self, tmp_path):
        with pytest.raises(ValueError):
            find_compiled_story(tmp_path, "../evil")


class TestLoadRanking:
    def test_missing_file_returns_none(self, tmp_path):
        assert load_ranking(tmp_path) is None

    def test_invalid_json_returns_none(self, tmp_path):
        _ranking_file(tmp_path).write_text("{not json", encoding="utf-8")
        assert load_ranking(tmp_path, "20261021") is None

    def test_non_dict_returns_none(self, tmp_path):
        _ranking_file(tmp_path).write_text("[]", encoding="utf-8")
        assert load_ranking(tmp_path, "20261021") is None

    def test_valid_ranking_roundtrip(self, tmp_path):
        _write_ranking(tmp_path, [{"rank": 1, "story_id": "story_1", "state": "pending"}])
        ranking = load_ranking(tmp_path, "20261021")
        assert ranking is not None
        assert ranking["country"] == "belgium"

    def test_falls_back_to_most_recent_earlier_day(self, tmp_path):
        """A story in progress must survive midnight."""
        _write_ranking(tmp_path, [{"rank": 1, "story_id": "story_old"}], date="20261020")
        _write_ranking(tmp_path, [{"rank": 1, "story_id": "story_new"}], date="20261021")
        ranking = load_ranking(tmp_path, "20261022")
        assert ranking is not None
        assert ranking["date"] == "20261021"

    def test_does_not_look_into_the_future(self, tmp_path):
        _write_ranking(tmp_path, [{"rank": 1, "story_id": "story_new"}], date="20261022")
        assert load_ranking(tmp_path, "20261021") is None

    def test_prefers_the_exact_day(self, tmp_path):
        _write_ranking(tmp_path, [{"rank": 1, "story_id": "story_old"}], date="20261020")
        _write_ranking(tmp_path, [{"rank": 1, "story_id": "story_new"}], date="20261021")
        assert load_ranking(tmp_path, "20261020")["date"] == "20261020"


class TestRankingDays:
    def test_empty(self, tmp_path):
        assert ranking_days(tmp_path) == []

    def test_newest_first(self, tmp_path):
        for day in ("20261020", "20261022", "20261021"):
            _write_ranking(tmp_path, [{"rank": 1, "story_id": "story_1"}], date=day)
        assert ranking_days(tmp_path) == ["20261022", "20261021", "20261020"]

    def test_upto_filters(self, tmp_path):
        for day in ("20261020", "20261022"):
            _write_ranking(tmp_path, [{"rank": 1, "story_id": "story_1"}], date=day)
        assert ranking_days(tmp_path, upto="20261021") == ["20261020"]


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
        day = today_utc()
        assert lock["story_dir"].endswith(f"stories/{day[:4]}/{day[4:6]}/{day[6:8]}/story_1")
        assert lock["story_date"] == day

    def test_write_records_explicit_date(self, tmp_path):
        write_active_story(tmp_path, "story_1", "research", "20261021")
        lock = load_active_story(tmp_path)
        assert lock["story_date"] == "20261021"
        assert lock["story_dir"].endswith("stories/2026/10/21/story_1")

    def test_write_rejects_traversal(self, tmp_path):
        with pytest.raises(ValueError):
            write_active_story(tmp_path, "../escape", "research")

    def test_invalid_lock_returns_none(self, tmp_path):
        (tmp_path / ".active_story").write_text(json.dumps({"story_id": "../x"}), encoding="utf-8")
        assert load_active_story(tmp_path) is None

    def test_invalid_json_lock_returns_none(self, tmp_path):
        (tmp_path / ".active_story").write_text("{nope", encoding="utf-8")
        assert load_active_story(tmp_path) is None

    def test_non_dict_lock_returns_none(self, tmp_path):
        (tmp_path / ".active_story").write_text("[]", encoding="utf-8")
        assert load_active_story(tmp_path) is None

    def test_clear_unlink_error_returns_false(self, tmp_path):
        write_active_story(tmp_path, "story_1", "research")
        with patch("pathlib.Path.unlink", side_effect=OSError("boom")):
            assert clear_active_story(tmp_path) is False

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
        assert set_story_state(tmp_path, "story_1", "writing", "20261021") is True
        stories = ranking_stories(load_ranking(tmp_path, "20261021"))
        assert stories[0]["state"] == "writing"
        assert "state_updated_at" in stories[0]

    def test_unknown_story_returns_false(self, tmp_path):
        _write_ranking(tmp_path, [{"rank": 1, "story_id": "story_1", "state": "pending"}])
        assert set_story_state(tmp_path, "story_2", "writing", "20261021") is False

    def test_missing_ranking_returns_false(self, tmp_path):
        assert set_story_state(tmp_path, "story_1", "writing") is False

    def test_corrupt_ranking_day_is_skipped(self, tmp_path):
        _ranking_file(tmp_path, "20261021").write_text("{nope", encoding="utf-8")
        assert set_story_state(tmp_path, "story_1", "writing", "20261021") is False

    def test_updates_the_ranking_that_contains_the_story(self, tmp_path):
        """A story ranked yesterday keeps its state in yesterday's ranking."""
        _write_ranking(tmp_path, [{"rank": 1, "story_id": "story_old", "state": "pending"}], date="20261020")
        _write_ranking(tmp_path, [{"rank": 1, "story_id": "story_new", "state": "pending"}], date="20261021")
        assert set_story_state(tmp_path, "story_old", "writing", "20261021") is True
        assert ranking_stories(load_ranking(tmp_path, "20261020"))[0]["state"] == "writing"
        assert ranking_stories(load_ranking(tmp_path, "20261021"))[0]["state"] == "pending"


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

    def test_skips_hidden_files(self, tmp_path):
        directory = tmp_path / "compiled_stories"
        directory.mkdir()
        (directory / ".hidden.json").write_text(json.dumps({"story_id": "story_1"}), encoding="utf-8")
        assert load_compiled_stories(tmp_path) == []

    def test_skips_non_dict(self, tmp_path):
        directory = tmp_path / "compiled_stories"
        directory.mkdir()
        (directory / "list.json").write_text("[]", encoding="utf-8")
        assert load_compiled_stories(tmp_path) == []

    def test_missing_dir(self, tmp_path):
        assert load_compiled_stories(tmp_path) == []

    def test_date_limits_to_one_day(self, tmp_path):
        _write_compiled(tmp_path, "story_old", "20261020")
        _write_compiled(tmp_path, "story_new", "20261021")
        assert [s["story_id"] for s in load_compiled_stories(tmp_path, "20261021")] == ["story_new"]

    def test_without_date_reads_every_day(self, tmp_path):
        _write_compiled(tmp_path, "story_old", "20261020")
        _write_compiled(tmp_path, "story_new", "20261021")
        assert [s["story_id"] for s in load_compiled_stories(tmp_path)] == ["story_old", "story_new"]

    def test_unranked_excludes_ranked(self, tmp_path):
        _write_compiled(tmp_path, "story_1")
        _write_compiled(tmp_path, "story_2")
        _write_ranking(tmp_path, [{"rank": 1, "story_id": "story_1", "state": "pending"}])
        assert [s["story_id"] for s in unranked_stories(tmp_path)] == ["story_2"]

    def test_unranked_without_ranking(self, tmp_path):
        _write_compiled(tmp_path, "story_1")
        assert [s["story_id"] for s in unranked_stories(tmp_path)] == ["story_1"]

    def test_unranked_ignores_previous_days(self, tmp_path):
        _write_compiled(tmp_path, "story_old", compilation_timestamp="2026-10-22T09:00:00+00:00")
        _write_compiled(tmp_path, "story_new")
        assert [s["story_id"] for s in unranked_stories(tmp_path)] == ["story_new"]

    def test_unranked_ignores_stories_without_timestamp(self, tmp_path):
        _write_compiled(tmp_path, "story_1", compilation_timestamp="")
        assert unranked_stories(tmp_path) == []


class TestSplitRankedByDay:
    def test_splits_same_day_from_earlier(self, tmp_path):
        _write_compiled(tmp_path, "story_today")
        _write_compiled(tmp_path, "story_old", compilation_timestamp="2026-10-22T09:00:00+00:00")
        _write_ranking(tmp_path, [
            {"rank": 1, "story_id": "story_old", "state": "pending"},
            {"rank": 2, "story_id": "story_today", "state": "pending"},
        ])
        same, earlier = split_ranked_by_day(tmp_path)
        assert [s["story_id"] for s in same] == ["story_today"]
        assert [s["story_id"] for s in earlier] == ["story_old"]

    def test_no_ranking_yields_empty_lists(self, tmp_path):
        assert split_ranked_by_day(tmp_path) == ([], [])

    def test_entry_without_compiled_story_is_same_day(self, tmp_path):
        """A missing compiled story must not silently drop the entry."""
        _write_ranking(tmp_path, [{"rank": 1, "story_id": "story_gone", "state": "pending"}])
        same, earlier = split_ranked_by_day(tmp_path)
        assert [s["story_id"] for s in same] == ["story_gone"]
        assert earlier == []

    def test_preserves_rank_order(self, tmp_path):
        _write_compiled(tmp_path, "story_a", compilation_timestamp="2026-10-22T09:00:00+00:00")
        _write_compiled(tmp_path, "story_b", compilation_timestamp="2026-10-23T09:00:00+00:00")
        _write_ranking(tmp_path, [
            {"rank": 1, "story_id": "story_b", "state": "pending"},
            {"rank": 2, "story_id": "story_a", "state": "pending"},
        ])
        _, earlier = split_ranked_by_day(tmp_path)
        assert [s["story_id"] for s in earlier] == ["story_b", "story_a"]


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

    def test_entry_date_defaults_to_the_ranking_day(self):
        ranking = build_ranking("belgium", "20261021", [{"story_id": "story_a"}])
        assert ranking["stories"][0]["date"] == "20261021"

    def test_entry_date_comes_from_the_compiled_story(self):
        ranking = build_ranking("belgium", "20261021", [
            {"story_id": "story_a", "compilation_timestamp": "2026-10-19T09:00:00+00:00"},
        ])
        assert ranking["stories"][0]["date"] == "20261019"

    def test_entry_date_survives_a_rerank(self):
        previous = {"stories": [{"rank": 1, "story_id": "story_a", "date": "20261019"}]}
        ranking = build_ranking(
            "belgium", "20261021", [{"story_id": "story_a"}], previous=previous
        )
        assert ranking["stories"][0]["date"] == "20261019"


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
        _ranking_file(tmp_path).write_text(json.dumps({
            "date": "20260101",
            "country": "belgium",
            "stories": [{"rank": 1, "story_id": "story_1"}],
        }), encoding="utf-8")
        ok, reason = validate_ranking(tmp_path, "belgium", "20261021")
        assert ok is False
        assert "date" in reason

    def test_ignores_other_days(self, tmp_path):
        """A ranking written for another day must not satisfy this day."""
        _write_ranking(tmp_path, [{"rank": 1, "story_id": "story_1"}], date="20261020")
        ok, reason = validate_ranking(tmp_path, "belgium", "20261021")
        assert ok is False
        assert "missing" in reason

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

    def test_invalid_json(self, tmp_path):
        _ranking_file(tmp_path).write_text("{nope", encoding="utf-8")
        ok, reason = validate_ranking(tmp_path, "belgium", "20261021")
        assert ok is False
        assert "missing" in reason

    def test_non_dict(self, tmp_path):
        _ranking_file(tmp_path).write_text("[]", encoding="utf-8")
        ok, reason = validate_ranking(tmp_path, "belgium", "20261021")
        assert ok is False
        assert "missing" in reason
