"""Tests for the VNN prompt builders and queue actions."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cronpypeline.actions import TickContext
from cronpypeline.config import ActionSpec, ActionType, PipelineConfig
from cronpypeline.plugins import vnn_prompts
from cronpypeline.plugins.vnn_prompts import (
    build_compile_prompt,
    build_publishing_prompt,
    build_ranking_prompt,
    build_research_prompt,
    build_revision_prompt,
    build_writing_prompt,
    queue_compile_agent,
    queue_publishing_agent,
    queue_ranking_agent,
    queue_research_agent,
    queue_revision_agent,
    queue_writing_agent,
)
from cronpypeline.plugins.vnn_ranking import write_active_story, write_ranking

COUNTRY = "belgium"
DATE = "20261021"


def _write_compiled(target_dir: Path, story_id: str, **fields) -> None:
    """Write a compiled story JSON file for the country."""
    directory = target_dir / "compiled_stories"
    directory.mkdir(parents=True, exist_ok=True)
    fields.setdefault("compilation_timestamp", datetime.now(timezone.utc).isoformat())
    (directory / f"{story_id}.json").write_text(
        json.dumps({"story_id": story_id, **fields}), encoding="utf-8"
    )


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


def _pipeline(tmp_path: Path, queue_dir: Path) -> object:
    """Build a minimal pipeline whose action handler points at ``queue_dir``."""
    return type("P", (), {
        "config": PipelineConfig.from_dict({
            "name": "vnn-test",
            "workspace_dir": str(tmp_path),
            "stages": [],
            "action_handler": {
                "type": "conversation_queue",
                "params": {
                    "queue_dir": str(queue_dir),
                    "prompt_field": "content",
                    "default_fields": {"sender": "VNN_PIPELINE", "conversation_id": ""},
                    "flatten_agent_settings": True,
                },
            },
        }),
    })()


def _tick_context(tmp_path: Path, queue_dir: Path, dry_run: bool = False) -> TickContext:
    """Build a TickContext wired to a fake pipeline with a queue dir."""
    return TickContext(
        target=COUNTRY,
        workspace_dir=tmp_path,
        dry_run=dry_run,
        pipeline=_pipeline(tmp_path, queue_dir),
    )


def _action(**params) -> ActionSpec:
    """Build a custom action spec with the given params."""
    return ActionSpec(type=ActionType.CUSTOM, params=params)


def _queued_entry(queue_dir: Path) -> dict:
    """Read the single queue entry written to ``queue_dir``."""
    files = sorted(queue_dir.glob("*.json"))
    assert len(files) == 1, f"expected one queue entry, found {files}"
    return json.loads(files[0].read_text())


# ─── Prompt builders ────────────────────────────────────────────────────────


class TestBuildRankingPrompt:
    def test_lists_stories_and_save_script(self):
        prompt = build_ranking_prompt(
            COUNTRY, DATE,
            [{"story_id": "story_1", "title_en": "One", "category": "politics",
              "article_count": 3, "summary_en": "Summary", "file_path": "/x/story_1.json"}],
        )
        assert "belgium" in prompt
        assert DATE in prompt
        assert "story_1" in prompt
        assert "One" in prompt
        assert "save_ranking.py" in prompt
        assert f'--country "{COUNTRY}"' in prompt

    def test_does_not_ask_agent_to_write_ranking_json(self):
        """The agent must go through save_ranking.py, not hand-write the file."""
        prompt = build_ranking_prompt(
            COUNTRY, DATE, [{"story_id": "story_1", "title_en": "One"}]
        )
        assert '"state": "pending"' not in prompt
        assert "as JSON with exactly this shape" not in prompt
        assert f"Do not write `{COUNTRY}/ranking.json` yourself" in prompt

    def test_includes_already_ranked_stories(self):
        prompt = build_ranking_prompt(
            COUNTRY, DATE,
            [{"story_id": "story_2"}],
            previous={"stories": [{"rank": 1, "story_id": "story_1", "title_en": "Old"}]},
        )
        assert "Already ranked" in prompt
        assert "story_1" in prompt

    def test_requires_full_order_when_ranking_exists(self):
        """The agent must submit every id; save_ranking.py rebuilds from scratch.

        Telling the agent to omit already-ranked ids made it submit a partial
        order, which truncated the ranking and left the ranking trigger firing
        forever.
        """
        prompt = build_ranking_prompt(
            COUNTRY, DATE,
            [{"story_id": "story_2"}],
            previous={"stories": [{"rank": 1, "story_id": "story_1", "title_en": "Old"}]},
        )
        assert "Do not include ids from **Already ranked**" not in prompt
        assert "you MUST include them in the order you submit" in prompt
        assert "Use every story id from **Stories to rank** exactly once." in prompt

    def test_no_partial_order_rule_without_previous_ranking(self):
        """With no prior ranking the agent ranks everything it was given."""
        prompt = build_ranking_prompt(COUNTRY, DATE, [{"story_id": "story_1"}])
        assert "Already ranked" not in prompt
        assert "Use every story id from **Stories to rank** exactly once." in prompt

    def test_earlier_day_stories_are_excluded_from_the_order(self):
        """Stories ranked on an earlier day must not be resubmitted.

        save_ranking.py only accepts stories compiled on the day being ranked,
        so it rejects an earlier-day id as unknown. Telling the agent to include
        them made the ranking impossible to save: the agent submitted all six
        ids, the script rejected four, and the retry then dropped them.
        """
        prompt = build_ranking_prompt(
            COUNTRY, DATE,
            [{"story_id": "story_new"}],
            previous={"stories": []},
            earlier_ranked=[{"rank": 1, "story_id": "story_old", "title_en": "Old"}],
        )
        assert "Ranked on an earlier day" in prompt
        assert "story_old" in prompt
        assert "Do NOT include their ids in the order you submit" in prompt
        assert "you MUST include them in the order you submit" not in prompt

    def test_earlier_day_note_absent_when_none(self):
        prompt = build_ranking_prompt(COUNTRY, DATE, [{"story_id": "story_1"}])
        assert "Ranked on an earlier day" not in prompt

    def test_same_day_and_earlier_day_are_listed_separately(self):
        prompt = build_ranking_prompt(
            COUNTRY, DATE,
            [{"story_id": "story_new"}],
            previous={"stories": [{"rank": 1, "story_id": "story_today", "title_en": "Today"}]},
            earlier_ranked=[{"rank": 2, "story_id": "story_old", "title_en": "Old"}],
        )
        already = prompt.split("## Already ranked")[1].split("## Ranked on an earlier day")[0]
        earlier = prompt.split("## Ranked on an earlier day")[1]
        assert "story_today" in already and "story_old" not in already
        assert "story_old" in earlier and "story_today" not in earlier

    def test_empty_story_list(self):
        prompt = build_ranking_prompt(COUNTRY, DATE, [])
        assert "No compiled stories found" in prompt

    def test_extra_instructions(self):
        prompt = build_ranking_prompt(COUNTRY, DATE, [], extra_instructions="Be brief")
        assert "Be brief" in prompt


class TestBuildCompilePrompt:
    def test_mentions_country_date_and_links(self):
        prompt = build_compile_prompt(COUNTRY, DATE, 12)
        assert COUNTRY in prompt
        assert DATE in prompt
        assert "12 archived links" in prompt
        assert "compilation_metadata.json" in prompt

    def test_uses_current_load_articles_cli(self):
        """The prompt must not reference the removed --date/--exclude-compiled flags."""
        prompt = build_compile_prompt(COUNTRY, DATE, 12)
        assert f'load_articles.py --country "{COUNTRY}"' in prompt
        assert "--exclude-compiled" not in prompt
        assert "load_articles.py --date" not in prompt

    def test_extra_instructions(self):
        assert "Extra" in build_compile_prompt(COUNTRY, DATE, 1, extra_instructions="Extra")


class TestBuildStoryPrompts:
    @pytest.mark.parametrize("builder,expected", [
        (build_research_prompt, "research.md"),
        (build_writing_prompt, "article.md"),
        (build_publishing_prompt, "published.json"),
        (build_revision_prompt, "rejected-article.md"),
    ])
    def test_prompts_reference_story_and_artefacts(self, tmp_path, builder, expected):
        story = _make_story(tmp_path, "story_1", "research.md")
        prompt = builder(COUNTRY, "story_1", story)
        assert "story_1" in prompt
        assert str(story) in prompt
        assert expected in prompt

    def test_rejection_count_is_reported(self, tmp_path):
        story = _make_story(tmp_path, "story_1", "rejected-article.md")
        (story / "rejection_log.json").write_text(json.dumps([{"a": 1}]), encoding="utf-8")
        assert "Rejections so far**: 1" in build_revision_prompt(COUNTRY, "story_1", story)

    def test_extra_instructions(self, tmp_path):
        story = _make_story(tmp_path, "story_1")
        assert "Extra" in build_research_prompt(COUNTRY, "story_1", story, extra_instructions="Extra")


# ─── Queue actions ──────────────────────────────────────────────────────────


class TestQueueRankingAgent:
    def test_queues_ranking_prompt(self, tmp_path):
        queue_dir = tmp_path / "queue"
        _write_compiled(tmp_path / COUNTRY, "story_1", title_en="One")
        _write_compiled(tmp_path / COUNTRY, "story_2", title_en="Two")

        result = queue_ranking_agent(_action(agent="RankingAgent"), _tick_context(tmp_path, queue_dir))

        assert result.success is True
        assert result.data["async"] is True
        entry = _queued_entry(queue_dir)
        assert entry["agent"] == "RankingAgent"
        assert "story_1" in entry["content"]
        assert entry["sender"] == "VNN_PIPELINE"

    def test_no_unranked_stories_is_a_noop(self, tmp_path):
        """The agent may rank between the trigger and the action — not a failure."""
        queue_dir = tmp_path / "queue"
        result = queue_ranking_agent(_action(agent="RankingAgent"), _tick_context(tmp_path, queue_dir))
        assert result.success is True
        assert result.data["skipped"] is True
        assert "No unranked" in result.stdout
        assert not queue_dir.exists()

    def test_dry_run_writes_nothing(self, tmp_path):
        queue_dir = tmp_path / "queue"
        result = queue_ranking_agent(
            _action(agent="RankingAgent"), _tick_context(tmp_path, queue_dir, dry_run=True)
        )
        assert result.dry_run is True
        assert not queue_dir.exists()


class TestQueueCompileAgent:
    def test_queues_compile_prompt(self, tmp_path):
        queue_dir = tmp_path / "queue"
        result = queue_compile_agent(
            _action(agent="NewsCompilerAgent", link_count=11), _tick_context(tmp_path, queue_dir)
        )
        assert result.success is True
        entry = _queued_entry(queue_dir)
        assert entry["agent"] == "NewsCompilerAgent"
        assert "11 archived links" in entry["content"]


class TestQueueStoryAgents:
    @pytest.mark.parametrize("queue_fn,agent,artefact", [
        (queue_research_agent, "NewsResearchAgent", None),
        (queue_writing_agent, "ArticleWriterAgent", "research.md"),
        (queue_publishing_agent, "ArticlePublisherAgent", "article.md"),
        (queue_revision_agent, "ArticleRevisionAgent", "rejected-article.md"),
    ])
    def test_queues_story_prompt(self, tmp_path, queue_fn, agent, artefact):
        queue_dir = tmp_path / "queue"
        country_dir = tmp_path / COUNTRY
        _write_ranking(country_dir, [{"rank": 1, "story_id": "story_1", "state": "pending"}])
        _make_story(country_dir, "story_1", *([artefact] if artefact else []))
        write_active_story(country_dir, "story_1", "test")

        result = queue_fn(_action(agent=agent), _tick_context(tmp_path, queue_dir))

        assert result.success is True
        entry = _queued_entry(queue_dir)
        assert entry["agent"] == agent
        assert "story_1" in entry["content"]

    def test_no_story_is_a_noop(self, tmp_path):
        """The story may finish between the trigger and the action — not a failure."""
        queue_dir = tmp_path / "queue"
        result = queue_research_agent(_action(agent="NewsResearchAgent"), _tick_context(tmp_path, queue_dir))
        assert result.success is True
        assert result.data["skipped"] is True
        assert "No story" in result.stdout
        assert not queue_dir.exists()

    def test_dry_run_writes_nothing(self, tmp_path):
        queue_dir = tmp_path / "queue"
        result = queue_research_agent(
            _action(agent="NewsResearchAgent"), _tick_context(tmp_path, queue_dir, dry_run=True)
        )
        assert result.dry_run is True
        assert not queue_dir.exists()


class TestStorySetup:
    """The research stage must materialise the story before queueing an agent."""

    def test_creates_story_dir_and_copies_story_json(self, tmp_path):
        queue_dir = tmp_path / "queue"
        country_dir = tmp_path / COUNTRY
        _write_ranking(country_dir, [{"rank": 1, "story_id": "story_1", "state": "pending"}])
        _write_compiled(country_dir, "story_1", title_en="One")

        result = queue_research_agent(_action(agent="NewsResearchAgent"), _tick_context(tmp_path, queue_dir))

        assert result.success is True
        story_path = country_dir / "stories" / "story_1"
        assert story_path.is_dir()
        copied = json.loads((story_path / "story.json").read_text(encoding="utf-8"))
        assert copied["title_en"] == "One"

    def test_claims_active_story_lock(self, tmp_path):
        queue_dir = tmp_path / "queue"
        country_dir = tmp_path / COUNTRY
        _write_ranking(country_dir, [{"rank": 1, "story_id": "story_1", "state": "pending"}])
        _write_compiled(country_dir, "story_1", title_en="One")

        queue_research_agent(_action(agent="NewsResearchAgent"), _tick_context(tmp_path, queue_dir))

        lock = json.loads((country_dir / ".active_story").read_text(encoding="utf-8"))
        assert lock["story_id"] == "story_1"
        assert lock["stage"] == "research"

    def test_lock_is_reused_on_later_ticks(self, tmp_path):
        queue_dir = tmp_path / "queue"
        country_dir = tmp_path / COUNTRY
        _write_ranking(country_dir, [
            {"rank": 1, "story_id": "story_1", "state": "pending"},
            {"rank": 2, "story_id": "story_2", "state": "pending"},
        ])
        _write_compiled(country_dir, "story_1", title_en="One")
        _write_compiled(country_dir, "story_2", title_en="Two")

        ctx = _tick_context(tmp_path, queue_dir)
        queue_research_agent(_action(agent="NewsResearchAgent"), ctx)
        queue_research_agent(_action(agent="NewsResearchAgent"), ctx)

        lock = json.loads((country_dir / ".active_story").read_text(encoding="utf-8"))
        assert lock["story_id"] == "story_1"
        assert not (country_dir / "stories" / "story_2").exists()


class TestActionsAgreeWithTriggers:
    """The queue actions must resolve the same story the triggers selected.

    Resolving independently let the action claim a story the trigger had
    already rejected, e.g. one whose ``ranking.json`` state lagged behind the
    filesystem and was in fact already published.
    """

    def test_skips_published_story_whose_state_lags(self, tmp_path):
        queue_dir = tmp_path / "queue"
        country_dir = tmp_path / COUNTRY
        _make_story(country_dir, "story_1", "story.json", "research.md", "article.md", "published.json")
        _write_ranking(country_dir, [
            {"rank": 1, "story_id": "story_1", "state": "pending"},
            {"rank": 2, "story_id": "story_2", "state": "pending"},
        ])
        _write_compiled(country_dir, "story_2", title_en="Two")

        result = queue_research_agent(_action(agent="NewsResearchAgent"), _tick_context(tmp_path, queue_dir))

        assert result.success is True
        lock = json.loads((country_dir / ".active_story").read_text(encoding="utf-8"))
        assert lock["story_id"] == "story_2"
        assert "story_2" in _queued_entry(queue_dir)["content"]

    def test_does_not_reuse_lock_on_published_story(self, tmp_path):
        queue_dir = tmp_path / "queue"
        country_dir = tmp_path / COUNTRY
        _make_story(country_dir, "story_1", "story.json", "research.md", "article.md", "published.json")
        write_active_story(country_dir, "story_1", "research")
        _write_ranking(country_dir, [
            {"rank": 1, "story_id": "story_1", "state": "publishing"},
            {"rank": 2, "story_id": "story_2", "state": "pending"},
        ])
        _write_compiled(country_dir, "story_2", title_en="Two")

        result = queue_research_agent(_action(agent="NewsResearchAgent"), _tick_context(tmp_path, queue_dir))

        assert result.success is True
        lock = json.loads((country_dir / ".active_story").read_text(encoding="utf-8"))
        assert lock["story_id"] == "story_2"

    def test_no_story_available_when_all_are_published(self, tmp_path):
        queue_dir = tmp_path / "queue"
        country_dir = tmp_path / COUNTRY
        _make_story(country_dir, "story_1", "story.json", "research.md", "article.md", "published.json")
        _write_ranking(country_dir, [{"rank": 1, "story_id": "story_1", "state": "pending"}])

        result = queue_research_agent(_action(agent="NewsResearchAgent"), _tick_context(tmp_path, queue_dir))

        assert result.success is True
        assert result.data == {"skipped": True}
        assert not list(queue_dir.glob("*.json"))


class TestStageWorkGuard:
    """A stage must not queue its agent for a story that needs other work.

    The stale-stage re-queue path re-runs an action without re-evaluating its
    trigger, and the active story is re-resolved on every call. Once the
    previous story finished, the publisher was re-queued against the next
    ranked story, which had no article at all — the agent could only report
    that it had nothing to load.
    """

    def test_publisher_not_queued_for_story_without_article(self, tmp_path):
        queue_dir = tmp_path / "queue"
        country_dir = tmp_path / COUNTRY
        _make_story(country_dir, "story_1", "story.json")
        _write_ranking(country_dir, [{"rank": 1, "story_id": "story_1", "state": "pending"}])

        result = queue_publishing_agent(
            _action(agent="ArticlePublisherAgent"), _tick_context(tmp_path, queue_dir)
        )

        assert result.success is True
        assert result.data == {"skipped": True}
        assert not list(queue_dir.glob("*.json"))

    def test_publisher_queued_for_story_with_article(self, tmp_path):
        queue_dir = tmp_path / "queue"
        country_dir = tmp_path / COUNTRY
        _make_story(country_dir, "story_1", "story.json", "research.md", "article.md")
        _write_ranking(country_dir, [{"rank": 1, "story_id": "story_1", "state": "pending"}])

        result = queue_publishing_agent(
            _action(agent="ArticlePublisherAgent"), _tick_context(tmp_path, queue_dir)
        )

        assert result.success is True
        assert "story_1" in _queued_entry(queue_dir)["content"]

    def test_writer_not_queued_for_story_without_research(self, tmp_path):
        queue_dir = tmp_path / "queue"
        country_dir = tmp_path / COUNTRY
        _make_story(country_dir, "story_1", "story.json")
        _write_ranking(country_dir, [{"rank": 1, "story_id": "story_1", "state": "pending"}])

        result = queue_writing_agent(
            _action(agent="ArticleWriterAgent"), _tick_context(tmp_path, queue_dir)
        )

        assert result.data == {"skipped": True}
        assert not list(queue_dir.glob("*.json"))

    def test_writer_queued_for_story_with_research(self, tmp_path):
        queue_dir = tmp_path / "queue"
        country_dir = tmp_path / COUNTRY
        _make_story(country_dir, "story_1", "story.json", "research.md")
        _write_ranking(country_dir, [{"rank": 1, "story_id": "story_1", "state": "pending"}])

        result = queue_writing_agent(
            _action(agent="ArticleWriterAgent"), _tick_context(tmp_path, queue_dir)
        )

        assert result.success is True
        assert "story_1" in _queued_entry(queue_dir)["content"]

    def test_researcher_not_queued_for_story_with_research(self, tmp_path):
        queue_dir = tmp_path / "queue"
        country_dir = tmp_path / COUNTRY
        _make_story(country_dir, "story_1", "story.json", "research.md")
        _write_ranking(country_dir, [{"rank": 1, "story_id": "story_1", "state": "pending"}])

        result = queue_research_agent(
            _action(agent="NewsResearchAgent"), _tick_context(tmp_path, queue_dir)
        )

        assert result.data == {"skipped": True}
        assert not list(queue_dir.glob("*.json"))

    def test_reviser_not_queued_for_unrejected_story(self, tmp_path):
        queue_dir = tmp_path / "queue"
        country_dir = tmp_path / COUNTRY
        _make_story(country_dir, "story_1", "story.json", "research.md", "article.md")
        _write_ranking(country_dir, [{"rank": 1, "story_id": "story_1", "state": "pending"}])

        result = queue_revision_agent(
            _action(agent="ArticleRevisionAgent"), _tick_context(tmp_path, queue_dir)
        )

        assert result.data == {"skipped": True}
        assert not list(queue_dir.glob("*.json"))

    def test_reviser_queued_for_rejected_story(self, tmp_path):
        queue_dir = tmp_path / "queue"
        country_dir = tmp_path / COUNTRY
        _make_story(country_dir, "story_1", "story.json", "research.md", "rejected-article.md")
        _write_ranking(country_dir, [{"rank": 1, "story_id": "story_1", "state": "pending"}])

        result = queue_revision_agent(
            _action(agent="ArticleRevisionAgent"), _tick_context(tmp_path, queue_dir)
        )

        assert result.success is True
        assert "story_1" in _queued_entry(queue_dir)["content"]


class TestQueueHandlerConfig:
    def test_missing_queue_dir_raises(self, tmp_path):
        ctx = TickContext(target=COUNTRY, workspace_dir=tmp_path)
        with pytest.raises(ValueError, match="queue_dir is required"):
            queue_compile_agent(_action(agent="A"), ctx)

    def test_traversal_queue_dir_raises(self, tmp_path):
        ctx = TickContext(
            target=COUNTRY,
            workspace_dir=tmp_path,
            pipeline=type("P", (), {"config": PipelineConfig.from_dict({
                "name": "x", "workspace_dir": str(tmp_path), "stages": [],
                "action_handler": {"type": "conversation_queue",
                                   "params": {"queue_dir": "../escape"}},
            })})(),
        )
        with pytest.raises(ValueError, match="path traversal"):
            queue_compile_agent(_action(agent="A"), ctx)

    def test_relative_agent_settings_dir_is_resolved(self, tmp_path):
        queue_dir = tmp_path / "queue"
        ctx = TickContext(
            target=COUNTRY,
            workspace_dir=tmp_path,
            pipeline=type("P", (), {"config": PipelineConfig.from_dict({
                "name": "x", "workspace_dir": str(tmp_path), "stages": [],
                "action_handler": {"type": "conversation_queue",
                                   "params": {"queue_dir": str(queue_dir),
                                              "agent_settings_dir": "agents"}},
            })})(),
        )
        result = queue_compile_agent(_action(agent="A"), ctx)
        assert result.success is True


class TestModuleExports:
    @pytest.mark.parametrize("name", [
        "build_ranking_prompt",
        "build_compile_prompt",
        "build_research_prompt",
        "build_writing_prompt",
        "build_publishing_prompt",
        "build_revision_prompt",
        "queue_ranking_agent",
        "queue_compile_agent",
        "queue_research_agent",
        "queue_writing_agent",
        "queue_publishing_agent",
        "queue_revision_agent",
    ])
    def test_public_callables_exist(self, name):
        assert callable(getattr(vnn_prompts, name))
