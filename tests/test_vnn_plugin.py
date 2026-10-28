"""Tests for the VNN plugin triggers, actions and hooks."""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cronpypeline.plugins import vnn_plugin
from cronpypeline.plugins.vnn_plugin import (
    DEFAULT_MIN_LINKS,
    DEFAULT_PROCESSING_TIMEOUT_MINUTES,
    detect_compilation_needed,
    detect_publishing_needed,
    detect_ranking_needed,
    detect_research_needed,
    detect_revision_needed,
    detect_writing_needed,
)
from cronpypeline.plugins.vnn_ranking import (
    compiled_dir,
    day_from_timestamp,
    load_active_story,
    load_ranking,
    news_links_dir,
    ranking_path,
    story_dir,
    write_active_story,
    write_ranking,
)

COUNTRY = "china"
DATE = "20261022"
STORY_ID = "story_4dd1c317"


def _write_ranking(target_dir: Path, stories) -> None:
    """Write a ranking.json document for the country."""
    write_ranking(target_dir, {
        "date": DATE,
        "country": COUNTRY,
        "version": 1,
        "stories": [{"date": DATE, **story} for story in stories],
    })


def _make_story(target_dir: Path, story_id: str, *artefacts: str) -> Path:
    """Create a story directory containing the given artefact files."""
    path = story_dir(target_dir, story_id, DATE)
    path.mkdir(parents=True, exist_ok=True)
    for name in artefacts:
        (path / name).write_text("{}", encoding="utf-8")
    return path


def _write_link(target_dir: Path, link_id: str, **fields) -> None:
    """Write an archived news link file for the country."""
    directory = news_links_dir(target_dir, DATE)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{link_id}.json").write_text(
        json.dumps({"id": link_id, **fields}), encoding="utf-8"
    )


def _write_compiled(target_dir: Path, story_id: str, **fields) -> None:
    """Write a compiled story JSON file for the country."""
    fields.setdefault("compilation_timestamp", datetime.now(timezone.utc).isoformat())
    day = day_from_timestamp(fields["compilation_timestamp"]) or DATE
    directory = compiled_dir(target_dir, day)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{story_id}.json").write_text(
        json.dumps({"story_id": story_id, **fields}), encoding="utf-8"
    )


def _write_processing(
    target_dir: Path,
    queue_file: Path | None = None,
    age_seconds: float = 0,
    name: str | None = None,
) -> Path:
    """Write a processing marker, optionally aged and under a custom name."""
    marker = target_dir / (name or vnn_plugin.PROCESSING_MARKER)
    content = {"retry_count": 0}
    if queue_file is not None:
        content["queue_file"] = str(queue_file)
    marker.write_text(json.dumps(content), encoding="utf-8")
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(marker, (old, old))
    return marker


def _context(target_dir: Path) -> dict:
    """Build a trigger context dict for the country.

    ``posts_dir`` points at a path that does not exist so the publish
    reconciliation never reads the live VNN content repository.
    """
    return {
        "target": COUNTRY,
        "target_dir": str(target_dir),
        "workspace_dir": str(target_dir.parent),
        "target_config": {"posts_dir": str(target_dir.parent / "no-such-posts")},
    }


@pytest.fixture
def country(tmp_path: Path) -> Path:
    """A country directory with one claimed story that still needs research."""
    target_dir = tmp_path / COUNTRY
    target_dir.mkdir()
    _write_ranking(target_dir, [{"story_id": STORY_ID, "state": "pending", "rank": 1}])
    _make_story(target_dir, STORY_ID, "story.json")
    write_active_story(target_dir, STORY_ID, "research", DATE)
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


def _stage(stage_id: str, marker: str):
    """Build a minimal stage carrying a processing marker spec."""
    from cronpypeline.config import (
        ActionSpec,
        ActionType,
        MarkerSpec,
        Stage,
        TriggerCondition,
        TriggerType,
    )

    return Stage(
        id=stage_id,
        name=stage_id,
        trigger=TriggerCondition(type=TriggerType.CUSTOM, callable="x"),
        action=ActionSpec(type=ActionType.CUSTOM, params={}),
        markers={"processing": MarkerSpec(name=marker, type="json")},
    )


class TestPerStageProcessingMarkers:
    """Each stage must own its processing marker.

    All stages previously shared a country-level ``.processing``, so one
    stage's marker reset another's: ranking saw research's marker and vice
    versa, and the tick alternated between them forever.
    """

    def test_reads_the_stage_own_marker(self, country: Path, tmp_path: Path):
        queue_file = tmp_path / "queue" / "Agent_1.json"
        queue_file.parent.mkdir()
        queue_file.write_text("{}", encoding="utf-8")
        _write_processing(country, queue_file, name=".processing_research")

        assert vnn_plugin._is_processing(country, 60, _stage("research", ".processing_research")) is True
        assert vnn_plugin._is_processing(country, 60, _stage("ranking", ".processing_ranking")) is False

    def test_one_stage_marker_does_not_gate_another_trigger(self, country: Path, tmp_path: Path):
        queue_file = tmp_path / "queue" / "Agent_1.json"
        queue_file.parent.mkdir()
        queue_file.write_text("{}", encoding="utf-8")
        _write_processing(country, queue_file, name=".processing_research")
        _write_compiled(country, "story_new")

        ranking_ctx = {**_context(country), "stage": _stage("ranking", ".processing_ranking")}
        research_ctx = {**_context(country), "stage": _stage("research", ".processing_research")}

        assert detect_research_needed(research_ctx) is False
        assert detect_ranking_needed(ranking_ctx) is True

    def test_ranking_trigger_blocked_by_its_own_marker(self, country: Path, tmp_path: Path):
        queue_file = tmp_path / "queue" / "RankingAgent_1.json"
        queue_file.parent.mkdir()
        queue_file.write_text("{}", encoding="utf-8")
        _write_processing(country, queue_file, name=".processing_ranking")
        _write_compiled(country, "story_new")

        ctx = {**_context(country), "stage": _stage("ranking", ".processing_ranking")}

        assert detect_ranking_needed(ctx) is False

    def test_compilation_trigger_blocked_by_its_own_marker(self, country: Path, tmp_path: Path):
        """A running compiler must not be re-queued on the next tick.

        The pending-link count only drops once the agent writes
        compilation_metadata.json, so without the gate the trigger fired every
        tick, exhausted the retry budget and gave up mid-compilation.
        """
        queue_file = tmp_path / "queue" / "NewsCompilerAgent_1.json"
        queue_file.parent.mkdir()
        queue_file.write_text("{}", encoding="utf-8")
        _write_processing(country, queue_file, name=".processing_compilation")
        for index in range(DEFAULT_MIN_LINKS):
            _write_link(country, f"link_{index}")

        ctx = {**_context(country), "stage": _stage("compilation", ".processing_compilation")}

        assert detect_compilation_needed(ctx) is False

    def test_compilation_trigger_fires_without_a_marker(self, country: Path):
        for index in range(DEFAULT_MIN_LINKS):
            _write_link(country, f"link_{index}")

        ctx = {**_context(country), "stage": _stage("compilation", ".processing_compilation")}

        assert detect_compilation_needed(ctx) is True

    def test_every_vnn_trigger_honours_the_processing_gate(self):
        """Guard: a trigger that forgets the gate re-queues its own agent."""
        import inspect

        from cronpypeline.plugins import vnn_plugin

        triggers = [
            name
            for name, value in vars(vnn_plugin).items()
            if name.startswith("detect_") and name.endswith("_needed") and callable(value)
        ]

        missing = [
            name for name in triggers if "_is_processing(" not in inspect.getsource(getattr(vnn_plugin, name))
        ]

        assert not missing, f"triggers missing the processing gate: {missing}"

    def test_shipped_config_gives_every_stage_a_distinct_marker(self):
        from cronpypeline.config import PipelineConfig

        config = PipelineConfig.from_file(
            Path(__file__).resolve().parents[1] / "configs" / "vnn_pipeline.json"
        )
        names = [
            stage.markers["processing"].name
            for stage in config.stages
            if "processing" in stage.markers
        ]

        assert len(names) == len(set(names)), f"stages share a processing marker: {names}"


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

        result = vnn_plugin.vnn_marker_context(_hook_context(country))

        assert result["active_story"] == next_id
        assert result["active_story_dir"] == f"stories/2026/10/22/{next_id}"
        assert result["ranking_dir"].startswith("rankings/")

    def test_empty_when_every_story_is_finished(self, country: Path):
        _make_story(country, STORY_ID, "story.json", "research.md", "article.md", "published.json")
        _write_ranking(country, [{"story_id": STORY_ID, "state": "pending", "rank": 1}])

        result = vnn_plugin.vnn_marker_context(_hook_context(country))

        assert result["active_story"] == ""
        assert result["active_story_dir"] == "stories/.none"

    def test_claimed_story_still_reported(self, country: Path):
        result = vnn_plugin.vnn_marker_context(_hook_context(country))

        assert result["active_story"] == STORY_ID
        assert result["active_story_dir"] == f"stories/2026/10/22/{STORY_ID}"


def _hook_context(target_dir: Path) -> dict:
    """Build a pre_tick hook context for a country directory.

    ``posts_dir`` points at a path that does not exist so the publish
    reconciliation never reads the live VNN content repository.
    """
    return {
        "target": target_dir.name,
        "target_dir": str(target_dir),
        "workspace_dir": str(target_dir.parent),
        "target_config": {"posts_dir": str(target_dir.parent / "no-such-posts")},
        "queue_dir": None,
    }


class TestSyncStoryStatesHook:
    """The pre_tick hook reconciles ranking.json with the story directories."""

    def test_published_story_state_is_corrected(self, country: Path):
        _make_story(country, STORY_ID, "story.json", "research.md", "article.md", "published.json")
        _write_ranking(country, [{"story_id": STORY_ID, "state": "pending", "rank": 1}])

        assert vnn_plugin.sync_story_states_hook(_hook_context(country)) is True

        states = {s["story_id"]: s["state"] for s in load_ranking(country)["stories"]}
        assert states[STORY_ID] == "published"

    def test_progress_is_never_rolled_back(self, country: Path):
        _make_story(country, STORY_ID, "story.json")
        _write_ranking(country, [{"story_id": STORY_ID, "state": "publishing", "rank": 1}])

        vnn_plugin.sync_story_states_hook(_hook_context(country))

        states = {s["story_id"]: s["state"] for s in load_ranking(country)["stories"]}
        assert states[STORY_ID] == "publishing"

    def test_missing_ranking_is_a_no_op(self, country: Path):
        ranking_path(country, DATE).unlink()

        assert vnn_plugin.sync_story_states_hook(_hook_context(country)) is True


class TestPublishReconciliation:
    """A publish whose git commit failed must not wedge the story forever.

    ``publish_article.py`` copies the article into the content repository and
    only then commits it. When the commit fails the copy survives but
    ``published.json`` is never written, so every retry fails with a
    duplicate-slug error and the story is re-queued until it gives up.
    """

    def _write_post(self, posts_dir: Path, slug: str, story_id: str) -> Path:
        """Write a content-repository post file carrying the story id."""
        day = posts_dir / "2026" / "10" / "25"
        day.mkdir(parents=True, exist_ok=True)
        post = day / f"{slug}.md"
        post.write_text(
            f'---\nstory_id: {story_id}\ntitle: "T"\nslug: {slug}\n---\n\nBody\n',
            encoding="utf-8",
        )
        return post

    def test_published_post_marks_story_finished(self, country: Path, tmp_path: Path):
        story = _make_story(country, STORY_ID, "story.json", "research.md", "article.md")
        posts_dir = tmp_path / "posts"
        self._write_post(posts_dir, "some-slug", STORY_ID)

        assert vnn_plugin._is_finished(story, posts_dir) is True
        marker = json.loads((story / "published.json").read_text(encoding="utf-8"))
        assert marker["story_id"] == STORY_ID
        assert marker["reconciled"] is True

    def test_post_for_another_story_does_not_finish(self, country: Path, tmp_path: Path):
        story = _make_story(country, STORY_ID, "story.json", "research.md", "article.md")
        posts_dir = tmp_path / "posts"
        self._write_post(posts_dir, "some-slug", "story_other")

        assert vnn_plugin._is_finished(story, posts_dir) is False
        assert not (story / "published.json").exists()

    def test_missing_posts_dir_does_not_finish(self, country: Path, tmp_path: Path):
        story = _make_story(country, STORY_ID, "story.json", "research.md", "article.md")

        assert vnn_plugin._is_finished(story, tmp_path / "no-such-posts") is False
        assert not (story / "published.json").exists()

    def test_release_hook_reconciles_and_releases_lock(self, country: Path, tmp_path: Path):
        _make_story(country, STORY_ID, "story.json", "research.md", "article.md")
        posts_dir = tmp_path / "posts"
        self._write_post(posts_dir, "some-slug", STORY_ID)
        context = {**_hook_context(country), "target_config": {"posts_dir": str(posts_dir)}}

        assert vnn_plugin.release_finished_story_hook(context) is True

        assert load_active_story(country) is None
        assert (story_dir(country, STORY_ID, DATE) / "published.json").exists()

    def test_publishing_trigger_does_not_fire_for_reconciled_story(
        self, country: Path, tmp_path: Path
    ):
        """The wedged story must stop re-queueing the publisher."""
        _make_story(country, STORY_ID, "story.json", "research.md", "article.md")
        posts_dir = tmp_path / "posts"
        self._write_post(posts_dir, "some-slug", STORY_ID)
        context = {**_context(country), "target_config": {"posts_dir": str(posts_dir)}}

        assert detect_publishing_needed(context) is False


class TestDiscardExhaustedStoriesHook:
    """A story that needs revision but hit ``max_rejections`` must be terminal.

    ``detect_revision_needed`` refuses to fire at the cap, so without a
    terminal transition the story keeps the lock forever and pins every story
    ranked behind it. The pre-tick hook writes ``.gave_up`` so the ranking
    sync derives ``discarded`` and the lock is released in the same tick.
    """

    def _write_rejections(self, story: Path, count: int) -> None:
        """Write a rejection_log.json holding ``count`` entries."""
        log = [{"reason": f"rejection {i}"} for i in range(count)]
        (story / "rejection_log.json").write_text(json.dumps(log), encoding="utf-8")

    def test_exhausted_story_is_discarded(self, country: Path):
        story = _make_story(country, STORY_ID, "story.json", "research.md", "rejected-article.md")
        self._write_rejections(story, vnn_plugin.DEFAULT_MAX_REJECTIONS)

        assert vnn_plugin.discard_exhausted_stories_hook(_hook_context(country)) is True

        assert (story / ".gave_up").exists()
        gave_up = json.loads((story / ".gave_up").read_text(encoding="utf-8"))
        assert gave_up["story_id"] == STORY_ID
        assert gave_up["rejection_count"] == vnn_plugin.DEFAULT_MAX_REJECTIONS

    def test_story_below_cap_is_kept(self, country: Path):
        story = _make_story(country, STORY_ID, "story.json", "research.md", "rejected-article.md")
        self._write_rejections(story, vnn_plugin.DEFAULT_MAX_REJECTIONS - 1)

        vnn_plugin.discard_exhausted_stories_hook(_hook_context(country))

        assert not (story / ".gave_up").exists()

    def test_custom_max_rejections_is_respected(self, country: Path):
        story = _make_story(country, STORY_ID, "story.json", "research.md", "rejected-article.md")
        self._write_rejections(story, 3)
        context = {**_hook_context(country), "target_config": {"max_rejections": 3}}

        vnn_plugin.discard_exhausted_stories_hook(context)

        assert (story / ".gave_up").exists()

    def test_story_needing_publishing_is_not_discarded(self, country: Path):
        """A story with an article still in flight is not revision-blocked."""
        story = _make_story(country, STORY_ID, "story.json", "research.md", "article.md")
        self._write_rejections(story, vnn_plugin.DEFAULT_MAX_REJECTIONS + 2)

        vnn_plugin.discard_exhausted_stories_hook(_hook_context(country))

        assert not (story / ".gave_up").exists()

    def test_finished_story_is_untouched(self, country: Path):
        story = _make_story(country, STORY_ID, "story.json", "published.json")
        self._write_rejections(story, vnn_plugin.DEFAULT_MAX_REJECTIONS + 2)

        vnn_plugin.discard_exhausted_stories_hook(_hook_context(country))

        assert not (story / ".gave_up").exists()

    def test_no_stories_dir_is_a_no_op(self, tmp_path: Path):
        target_dir = tmp_path / "nowhere"
        target_dir.mkdir()
        assert vnn_plugin.discard_exhausted_stories_hook(_hook_context(target_dir)) is True

    def test_pre_tick_releases_lock_and_advances(self, country: Path):
        """The whole transition completes in one pre-tick pass."""
        next_id = "story_next1234"
        _write_ranking(country, [
            {"story_id": STORY_ID, "state": "writing", "rank": 1},
            {"story_id": next_id, "state": "pending", "rank": 2},
        ])
        story = _make_story(country, STORY_ID, "story.json", "research.md", "rejected-article.md")
        self._write_rejections(story, vnn_plugin.DEFAULT_MAX_REJECTIONS)

        assert vnn_plugin.vnn_pre_tick(_hook_context(country)) is True

        assert (story / ".gave_up").exists()
        assert load_active_story(country) is None
        states = {s["story_id"]: s["state"] for s in load_ranking(country)["stories"]}
        assert states[STORY_ID] == "discarded"
        resolved = vnn_plugin._active_story(country)
        assert resolved is not None and resolved[0] == next_id

    def test_exhausted_locked_story_is_finished_during_resolution(self, country: Path):
        """``_is_finished`` discards the story inside trigger evaluation.

        Target selection evaluates triggers before ``pre_tick`` runs, so a
        wedged story that suppresses every trigger is never selected at all.
        ``_is_finished`` must therefore perform the transition itself.
        """
        next_id = "story_next1234"
        _write_ranking(country, [
            {"story_id": STORY_ID, "state": "writing", "rank": 1},
            {"story_id": next_id, "state": "pending", "rank": 2},
        ])
        story = _make_story(country, STORY_ID, "story.json", "research.md", "rejected-article.md")
        self._write_rejections(story, vnn_plugin.DEFAULT_MAX_REJECTIONS)
        context = _context(country)

        assert detect_research_needed(context) is True
        assert (story / ".gave_up").exists()
        resolved = vnn_plugin._active_story(country)
        assert resolved is not None and resolved[0] == next_id


class TestReleaseFinishedStoryHook:
    """The pre_tick hook clears a lock left behind by a finished story."""

    def test_releases_lock_on_published_story(self, country: Path):
        _make_story(country, STORY_ID, "story.json", "research.md", "article.md", "published.json")

        assert vnn_plugin.release_finished_story_hook(_hook_context(country)) is True

        assert load_active_story(country) is None

    def test_releases_lock_on_given_up_story(self, country: Path):
        _make_story(country, STORY_ID, "story.json", ".gave_up")

        vnn_plugin.release_finished_story_hook(_hook_context(country))

        assert load_active_story(country) is None

    def test_keeps_lock_on_unfinished_story(self, country: Path):
        vnn_plugin.release_finished_story_hook(_hook_context(country))

        lock = load_active_story(country)
        assert lock is not None
        assert lock["story_id"] == STORY_ID

    def test_no_lock_is_a_no_op(self, country: Path):
        (country / ".active_story").unlink()

        assert vnn_plugin.release_finished_story_hook(_hook_context(country)) is True
