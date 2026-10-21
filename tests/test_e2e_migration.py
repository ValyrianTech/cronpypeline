"""End-to-end migration validation tests (Phase 5.3).

Tests that simulate multi-tick execution of both the VNN and SWE pipelines,
verifying that state flows correctly through all stages and that queue entries
are in the expected Serendipity-compatible format.
"""

import json
import time
from pathlib import Path

from cronpypeline.actions import (
    ActionHandler,
    ActionResult,
    TickContext,
)
from cronpypeline.config import ActionSpec, ActionType, PipelineConfig
from cronpypeline.pipeline import Pipeline, TickResultStatus
from cronpypeline.plugins.vnn_ranking import write_ranking

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockAgentHandler(ActionHandler):
    """Mock handler that simulates async agent dispatch.

    On execute: writes a queue file (simulating ConversationQueueHandler)
    and returns queue_file/entry_id in data.
    On check_complete: always returns False (agent is still "running").
    """

    def __init__(self, queue_dir: Path):
        self.queue_dir = queue_dir
        self.entries: list[tuple[str, Path]] = []

    def execute(self, action, context):
        if context.dry_run:
            return ActionResult(success=True, dry_run=True)

        entry_id = f"mock-{len(self.entries)}"
        entry = {
            "id": entry_id,
            "agent": action.params.get("agent", "default"),
            "content": action.params.get("prompt", action.params.get("prompt_template", "")),
            "sender": "TEST_PIPELINE",
            "conversation_id": "",
            "folder_name": "TEST",
            "model_name": "default_model",
            "runs_left": 3,
            "target": context.target,
            "timestamp": time.time(),
        }
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        queue_file = self.queue_dir / f"{entry_id}.json"
        queue_file.write_text(json.dumps(entry, indent=2))
        self.entries.append((entry_id, queue_file))

        return ActionResult(
            success=True,
            stdout=f"Queued agent {entry['agent']} for {context.target}",
            data={"queue_file": str(queue_file), "entry_id": entry_id},
        )

    def check_complete(self, action, context):
        return False


def _make_mock_handler(pipeline: Pipeline, queue_dir: Path):
    """Install a MockAgentHandler for QUEUE_AGENT on the pipeline instance.

    Registers on the per-pipeline registry (not the module-global one) so the
    mock dispatches agent work for this pipeline only.
    """
    handler = MockAgentHandler(queue_dir)
    pipeline._handlers[ActionType.QUEUE_AGENT] = handler
    return handler


class TestMockAgentHandlerUnit:
    """Directly exercise MockAgentHandler.execute/check_complete branches."""

    def test_execute_dry_run_and_check_complete(self, tmp_path):
        handler = MockAgentHandler(tmp_path / "queue")
        action = ActionSpec(type=ActionType.QUEUE_AGENT, params={"agent": "TestAgent"})
        context = TickContext(target="story-1", workspace_dir=tmp_path, dry_run=True)

        result = handler.execute(action, context)
        assert result.success is True
        assert result.dry_run is True

        assert handler.check_complete(action, context) is False


# ---------------------------------------------------------------------------
# VNN multi-tick simulation
# ---------------------------------------------------------------------------

COUNTRY = "belgium"
VNN_DATE = "20261021"


def _vnn_stage(stage_id: str, agent: str, completion: str, timeout: int = 30, queue_callable: str | None = None) -> dict:
    """Build a VNN stage config dict for the given stage."""
    return {
        "id": stage_id,
        "name": stage_id.title(),
        "trigger": {
            "type": "custom",
            "callable": f"cronpypeline.plugins.vnn_plugin.detect_{stage_id}_needed",
        },
        "action": {
            "type": "custom",
            "params": {
                "callable": queue_callable or f"cronpypeline.plugins.vnn_prompts.queue_{stage_id}_agent",
                "agent": agent,
            },
        },
        "markers": {
            "completion": {"type": "file", "name": completion},
            "processing": {"type": "json", "name": ".processing", "content": {}},
        },
        "timeout_minutes": timeout,
        "max_retries": 3,
    }


def _make_vnn_config(workspace: Path, queue_dir: Path) -> PipelineConfig:
    """Build a country-scoped VNN pipeline config for testing."""
    return PipelineConfig.from_dict({
        "name": "vnn-e2e-test",
        "workspace_dir": str(workspace),
        "lock_file": str(workspace / ".VNN" / "pipeline.lock"),
        "target_lock": True,
        "post_tick": {"callable": "cronpypeline.plugins.vnn_plugin.vnn_post_tick"},
        "targets": {"type": "static", "items": [COUNTRY]},
        "action_handler": {
            "type": "conversation_queue",
            "params": {
                "queue_dir": str(queue_dir),
                "prompt_field": "content",
                "default_fields": {
                    "sender": "VNN_PIPELINE",
                    "conversation_id": "",
                    "folder_name": "VNN",
                    "model_name": "default_model",
                    "runs_left": 3,
                },
                "flatten_agent_settings": True,
            },
        },
        "stages": [
            _vnn_stage("ranking", "RankingAgent", "ranking.json"),
            _vnn_stage("compilation", "NewsCompilerAgent", "compilation_metadata.json", timeout=60,
                       queue_callable="cronpypeline.plugins.vnn_prompts.queue_compile_agent"),
            _vnn_stage("research", "NewsResearchAgent", "research.md", timeout=60),
            _vnn_stage("writing", "ArticleWriterAgent", "article.md"),
            _vnn_stage("publishing", "ArticlePublisherAgent", "published.json"),
            _vnn_stage("revision", "ArticleRevisionAgent", "article.md"),
        ],
    })


def _write_ranking(country_dir: Path, stories: list[dict]) -> None:
    """Write a country-local ranking.json."""
    write_ranking(country_dir, {
        "date": VNN_DATE,
        "country": COUNTRY,
        "version": 1,
        "stories": stories,
    })


def _make_story(country_dir: Path, story_id: str, *artefacts: str) -> Path:
    """Create a story directory with the given artefacts."""
    path = country_dir / "stories" / story_id
    path.mkdir(parents=True, exist_ok=True)
    for name in artefacts:
        (path / name).write_text("{}", encoding="utf-8")
    return path


def _write_compiled(country_dir: Path, story_id: str) -> None:
    """Write a compiled story JSON file."""
    directory = country_dir / "compiled_stories"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{story_id}.json").write_text(
        json.dumps({"story_id": story_id, "title_en": story_id}), encoding="utf-8"
    )


class TestVNNMultiTickSimulation:
    """Simulate multi-tick execution of the country-scoped VNN pipeline.

    Flow: ranking → compilation → research → writing → publishing → revision.
    """

    def _setup(self, tmp_path: Path) -> tuple[Pipeline, Path, Path]:
        """Create a workspace with one country target and a mock handler."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        country_dir = workspace / COUNTRY
        country_dir.mkdir()
        queue_dir = workspace / "queue"

        pipeline = Pipeline(_make_vnn_config(workspace, queue_dir))
        _make_mock_handler(pipeline, queue_dir)
        return pipeline, country_dir, queue_dir

    def test_ranking_fires_before_article_work(self, tmp_path):
        """Two unranked compiled stories trigger the ranking stage first."""
        pipeline, country_dir, _ = self._setup(tmp_path)
        _write_compiled(country_dir, "story_1")
        _write_compiled(country_dir, "story_2")

        result = pipeline.tick(target=COUNTRY)

        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert result.stage_id == "ranking"
        assert (country_dir / ".processing").exists()

    def test_compilation_fires_when_links_accumulate(self, tmp_path):
        """Enough archived links trigger the compilation stage."""
        pipeline, country_dir, _ = self._setup(tmp_path)
        links = country_dir / "news_links"
        links.mkdir()
        for i in range(10):
            (links / f"link{i}.json").write_text("{}", encoding="utf-8")

        result = pipeline.tick(target=COUNTRY)

        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert result.stage_id == "compilation"

    def test_full_research_to_publish_flow(self, tmp_path):
        """research → writing → publishing → published, one stage per tick."""
        pipeline, country_dir, _ = self._setup(tmp_path)
        _write_ranking(country_dir, [{"rank": 1, "story_id": "story_1", "state": "pending"}])
        story_dir = _make_story(country_dir, "story_1")

        r1 = pipeline.tick(target=COUNTRY)
        assert r1.status == TickResultStatus.ACTION_EXECUTED
        assert r1.stage_id == "research"
        assert (country_dir / ".processing").exists()

        (story_dir / "research.md").write_text("# Research")
        (country_dir / ".processing").unlink(missing_ok=True)

        r2 = pipeline.tick(target=COUNTRY)
        assert r2.status == TickResultStatus.ACTION_EXECUTED
        assert r2.stage_id == "writing"

        (story_dir / "article.md").write_text("# Article")
        (country_dir / ".processing").unlink(missing_ok=True)

        r3 = pipeline.tick(target=COUNTRY)
        assert r3.status == TickResultStatus.ACTION_EXECUTED
        assert r3.stage_id == "publishing"

        (story_dir / "published.json").write_text(json.dumps({"url": "https://example.com"}))
        (country_dir / ".processing").unlink(missing_ok=True)

        r4 = pipeline.tick(target=COUNTRY)
        assert r4.status == TickResultStatus.NO_WORK

    def test_rejection_revision_loop(self, tmp_path):
        """publishing → rejected → revision → publishing → published."""
        pipeline, country_dir, _ = self._setup(tmp_path)
        _write_ranking(country_dir, [{"rank": 1, "story_id": "story_1", "state": "writing"}])
        story_dir = _make_story(country_dir, "story_1", "research.md")

        r1 = pipeline.tick(target=COUNTRY)
        assert r1.stage_id == "writing"

        (story_dir / "article.md").write_text("# Article v1")
        (country_dir / ".processing").unlink(missing_ok=True)

        r2 = pipeline.tick(target=COUNTRY)
        assert r2.stage_id == "publishing"

        (story_dir / "rejected-article.md").write_text("Rejected: needs improvement")
        (story_dir / "article.md").unlink()
        (story_dir / "revision-notes.md").write_text("Add more sources", encoding="utf-8")
        (country_dir / ".processing").unlink(missing_ok=True)

        r3 = pipeline.tick(target=COUNTRY)
        assert r3.stage_id == "revision"
        # The post_tick hook records the rejection in the audit trail.
        log = json.loads((story_dir / "rejection_log.json").read_text())
        assert len(log) == 1
        assert log[0]["reason"] == "Add more sources"

        (story_dir / "article.md").write_text("# Article v2")
        (story_dir / "rejected-article.md").unlink()
        (country_dir / ".processing").unlink(missing_ok=True)

        r4 = pipeline.tick(target=COUNTRY)
        assert r4.stage_id == "publishing"

        (story_dir / "published.json").write_text(json.dumps({"url": "https://example.com"}))
        (country_dir / ".processing").unlink(missing_ok=True)

        r5 = pipeline.tick(target=COUNTRY)
        assert r5.status == TickResultStatus.NO_WORK

    def test_revision_gives_up_at_max_rejections(self, tmp_path):
        """A story rejected max_rejections times is no longer revised."""
        pipeline, country_dir, _ = self._setup(tmp_path)
        _write_ranking(country_dir, [{"rank": 1, "story_id": "story_1", "state": "writing"}])
        story_dir = _make_story(country_dir, "story_1", "research.md", "rejected-article.md")
        (story_dir / "rejection_log.json").write_text(
            json.dumps([{"reason": str(i)} for i in range(5)]), encoding="utf-8"
        )

        result = pipeline.tick(target=COUNTRY)

        assert result.status == TickResultStatus.NO_WORK

    def test_queue_entry_format_is_serendipity_compatible(self, tmp_path):
        """Queue entries keep the Serendipity-compatible field set."""
        pipeline, country_dir, queue_dir = self._setup(tmp_path)
        _write_ranking(country_dir, [{"rank": 1, "story_id": "story_1", "state": "pending"}])
        _make_story(country_dir, "story_1")

        result = pipeline.tick(target=COUNTRY)
        assert result.status == TickResultStatus.ACTION_EXECUTED

        queue_files = list(queue_dir.glob("*.json"))
        assert len(queue_files) == 1
        entry = json.loads(queue_files[0].read_text())

        assert "content" in entry
        assert "sender" in entry
        assert "conversation_id" in entry
        assert "folder_name" in entry
        assert "model_name" in entry
        assert "runs_left" in entry
        assert "prompt" not in entry
        assert entry["agent"] == "NewsResearchAgent"
        assert "story_1" in entry["content"]

    def test_pre_tick_gate_skips_when_queue_busy(self, tmp_path):
        """The global queue gate blocks new work while the queue has entries."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        country_dir = workspace / COUNTRY
        country_dir.mkdir()
        queue_dir = workspace / "queue"
        queue_dir.mkdir()
        (queue_dir / "pending.json").write_text("{}", encoding="utf-8")

        config = PipelineConfig.from_dict({
            "name": "vnn-pre-tick-test",
            "workspace_dir": str(workspace),
            "lock_file": str(workspace / ".VNN" / "pipeline.lock"),
            "target_lock": True,
            "targets": {"type": "static", "items": [COUNTRY]},
            "action_handler": {
                "type": "conversation_queue",
                "params": {
                    "queue_dir": str(queue_dir),
                    "prompt_field": "content",
                    "default_fields": {"sender": "VNN_PIPELINE", "conversation_id": ""},
                    "flatten_agent_settings": True,
                },
            },
            "pre_tick": {"callable": "cronpypeline.plugins.vnn_plugin.vnn_pre_tick"},
            "stages": [_vnn_stage("research", "NewsResearchAgent", "research.md")],
        })
        pipeline = Pipeline(config)
        _make_mock_handler(pipeline, queue_dir)
        _write_ranking(country_dir, [{"rank": 1, "story_id": "story_1", "state": "pending"}])
        _make_story(country_dir, "story_1")

        result = pipeline.tick(target=COUNTRY)

        assert result.status == TickResultStatus.NO_WORK
        assert "pre_tick" in result.message
        assert len(list(queue_dir.glob("*.json"))) == 1


# ---------------------------------------------------------------------------
# SWE dry-run validation
# ---------------------------------------------------------------------------

class TestSWEDryRunValidation:
    """Validate the SWE pipeline config loads and dry-runs correctly."""

    CONFIG_PATH = Path(__file__).parent.parent / "configs" / "swe_pipeline.json"

    def test_swe_config_dry_run_does_not_modify_state(self, tmp_path):
        """Load the SWE config and run a dry-run tick — should not create any files."""
        config = PipelineConfig.from_file(self.CONFIG_PATH)

        # Override workspace and lock to temp paths
        config = PipelineConfig.from_dict({
            **json.loads(self.CONFIG_PATH.read_text()),
            "workspace_dir": str(tmp_path),
            "lock_file": str(tmp_path / ".SWE" / "pipeline.lock"),
            "mode_file": str(tmp_path / ".SWE" / "mode.json"),
            "targets": {"type": "static", "items": ["test-repo"]},
            "action_handler": {
                "type": "conversation_queue",
                "params": {
                    "queue_dir": str(tmp_path / "queue"),
                    "prompt_field": "content",
                    "default_fields": {
                        "sender": "SWE_PIPELINE",
                        "conversation_id": "",
                        "folder_name": "SWE",
                        "model_name": "default_model",
                        "runs_left": 3,
                    },
                    "flatten_agent_settings": True,
                },
            },
        })

        (tmp_path / "test-repo").mkdir()
        (tmp_path / "test-repo" / ".SWE").mkdir()
        (tmp_path / "test-repo" / ".SWE" / "init_complete.marker").touch()
        pipeline = Pipeline(config)

        # Dry run should not create any files
        r = pipeline.tick(target="test-repo", dry_run=True)
        assert r.status == TickResultStatus.DRY_RUN
        # No processing markers should exist
        assert not (tmp_path / "test-repo" / ".processing").exists()

    def test_swe_config_all_stages_have_valid_triggers(self):
        """Verify all SWE stages have well-formed trigger configs."""
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        for stage in config.stages:
            assert stage.trigger is not None, f"Stage {stage.id} has no trigger"
            assert stage.trigger.type is not None, f"Stage {stage.id} trigger has no type"

    def test_swe_config_diagnostic_stages_chain(self):
        """Verify that diagnostic stages (A2-A8) have chain=true for same-tick chaining."""
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        chain_stages = [s for s in config.stages if s.chain]
        # At least A2 through A8 should have chain=True
        assert len(chain_stages) >= 6, \
            f"Expected at least 6 chain stages, got {len(chain_stages)}"


# ---------------------------------------------------------------------------
# VNN dry-run validation
# ---------------------------------------------------------------------------

class TestVNNDryRunValidation:
    """Validate the VNN pipeline config loads and dry-runs correctly."""

    CONFIG_PATH = Path(__file__).parent.parent / "configs" / "vnn_pipeline.json"

    def test_vnn_config_dry_run_does_not_modify_state(self, tmp_path):
        """Load the VNN config and run a dry-run tick — should not create any files."""
        raw = json.loads(self.CONFIG_PATH.read_text())
        config = PipelineConfig.from_dict({
            **raw,
            "workspace_dir": str(tmp_path),
            "lock_file": str(tmp_path / ".VNN" / "pipeline.lock"),
            "targets": {"type": "static", "items": [COUNTRY]},
            "action_handler": {
                "type": "conversation_queue",
                "params": {
                    "queue_dir": str(tmp_path / "queue"),
                    "prompt_field": "content",
                    "default_fields": {
                        "sender": "VNN_PIPELINE",
                        "conversation_id": "",
                        "folder_name": "VNN",
                        "model_name": "default_model",
                        "runs_left": 3,
                    },
                    "flatten_agent_settings": True,
                },
            },
        })

        country_dir = tmp_path / COUNTRY
        country_dir.mkdir()
        _write_ranking(country_dir, [{"rank": 1, "story_id": "story_1", "state": "pending"}])
        _make_story(country_dir, "story_1")

        pipeline = Pipeline(config)
        _make_mock_handler(pipeline, tmp_path / "queue")

        # Dry run should not create any files
        r = pipeline.tick(target=COUNTRY, dry_run=True)
        assert r.status == TickResultStatus.DRY_RUN
        assert not (country_dir / ".processing").exists()
        assert not (tmp_path / "queue").exists()

    def test_vnn_config_all_stages_have_valid_triggers(self):
        """Verify all VNN stages have well-formed trigger configs."""
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        for stage in config.stages:
            assert stage.trigger is not None, f"Stage {stage.id} has no trigger"
            assert stage.trigger.type is not None, f"Stage {stage.id} trigger has no type"

    def test_vnn_config_detector_chain_order(self):
        """Verify the detector chain order.

        Ranking and compilation run first (they produce the stories the article
        stages consume), then research → writing → publishing → revision.
        """
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        ids = [s.id for s in config.stages]
        assert ids == ["ranking", "compilation", "research", "writing", "publishing", "revision"], \
            f"Unexpected stage order: {ids}"
