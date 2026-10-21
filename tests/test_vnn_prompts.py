"""Tests for the VNN prompt builders and queue actions."""

import json
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
    def test_lists_stories_and_schema(self):
        prompt = build_ranking_prompt(
            COUNTRY, DATE,
            [{"story_id": "story_1", "title_en": "One", "category": "politics",
              "article_count": 3, "summary_en": "Summary", "file_path": "/x/story_1.json"}],
        )
        assert "belgium" in prompt
        assert DATE in prompt
        assert "story_1" in prompt
        assert "One" in prompt
        assert "ranking.json" in prompt
        assert '"state": "pending"' in prompt

    def test_includes_already_ranked_stories(self):
        prompt = build_ranking_prompt(
            COUNTRY, DATE,
            [{"story_id": "story_2"}],
            previous={"stories": [{"rank": 1, "story_id": "story_1", "title_en": "Old"}]},
        )
        assert "Already ranked" in prompt
        assert "story_1" in prompt

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

    def test_fails_without_unranked_stories(self, tmp_path):
        queue_dir = tmp_path / "queue"
        result = queue_ranking_agent(_action(agent="RankingAgent"), _tick_context(tmp_path, queue_dir))
        assert result.success is False
        assert "No unranked" in result.stderr

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

    def test_fails_without_story(self, tmp_path):
        queue_dir = tmp_path / "queue"
        result = queue_research_agent(_action(agent="NewsResearchAgent"), _tick_context(tmp_path, queue_dir))
        assert result.success is False
        assert "No story" in result.stderr

    def test_dry_run_writes_nothing(self, tmp_path):
        queue_dir = tmp_path / "queue"
        result = queue_research_agent(
            _action(agent="NewsResearchAgent"), _tick_context(tmp_path, queue_dir, dry_run=True)
        )
        assert result.dry_run is True
        assert not queue_dir.exists()


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
