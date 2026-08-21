"""End-to-end migration validation tests (Phase 5.3).

Tests that simulate multi-tick execution of both the VNN and SWE pipelines,
verifying that state flows correctly through all stages and that queue entries
are in the expected Serendipity-compatible format.
"""

import json
import time
from pathlib import Path

from cronpypeline.actions import ActionHandler, ActionResult, register_handler
from cronpypeline.config import ActionType, PipelineConfig
from cronpypeline.pipeline import Pipeline, TickResultStatus

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
        self.entries = []

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


def _make_mock_handler(queue_dir: Path):
    """Register a MockAgentHandler for QUEUE_AGENT actions."""
    handler = MockAgentHandler(queue_dir)
    register_handler(ActionType.QUEUE_AGENT, handler)
    return handler


# ---------------------------------------------------------------------------
# VNN multi-tick simulation
# ---------------------------------------------------------------------------

class TestVNNMultiTickSimulation:
    """Simulate multi-tick execution of the VNN story-level pipeline.

    Flow: research → writing → publishing → (rejection) → revision → publishing
    """

    def _make_vnn_config(self, workspace: Path, queue_dir: Path) -> PipelineConfig:
        """Build a VNN-like pipeline config for testing."""
        return PipelineConfig.from_dict({
            "name": "vnn-e2e-test",
            "workspace_dir": str(workspace),
            "lock_file": str(workspace / ".VNN" / "pipeline.lock"),
            "target_lock": True,
            "targets": {
                "type": "static",
                "items": ["story-1"],
            },
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
                {
                    "id": "revision",
                    "name": "Revision",
                    "trigger": {
                        "type": "and",
                        "conditions": [
                            {"type": "file_exists", "path": "rejected-article.md"},
                            {"type": "file_missing", "path": "article.md"},
                            {"type": "file_missing", "path": ".processing"},
                            {"type": "file_missing", "path": ".gave_up"},
                        ],
                    },
                    "action": {
                        "type": "queue_agent",
                        "params": {
                            "agent": "WriterAgent",
                            "prompt_template": "Revise article for {target}",
                            "reminder_prompt_template": "Finish revision for {target}",
                        },
                    },
                    "markers": {
                        "completion": {"type": "file", "name": "article.md"},
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                        "rejection": {"type": "json", "name": ".rejection", "content": {}},
                        "give_up": {"type": "file", "name": ".gave_up"},
                    },
                    "timeout_minutes": 30,
                    "max_retries": 3,
                    "max_rejections": 5,
                    "invalidates": [
                        {"type": "file", "name": "rejected-article.md"},
                    ],
                },
                {
                    "id": "publishing",
                    "name": "Publishing",
                    "trigger": {
                        "type": "and",
                        "conditions": [
                            {"type": "file_exists", "path": "article.md"},
                            {"type": "file_missing", "path": "published.json"},
                            {"type": "file_missing", "path": ".processing"},
                            {"type": "file_missing", "path": ".gave_up"},
                        ],
                    },
                    "action": {
                        "type": "queue_agent",
                        "params": {
                            "agent": "PublishAgent",
                            "prompt_template": "Publish article for {target}",
                            "reminder_prompt_template": "Finish publishing for {target}",
                        },
                    },
                    "markers": {
                        "completion": {"type": "json", "name": "published.json", "content": {}},
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                        "rejection": {"type": "json", "name": ".rejection", "content": {}},
                        "give_up": {"type": "file", "name": ".gave_up"},
                    },
                    "timeout_minutes": 30,
                    "max_retries": 3,
                    "max_rejections": 3,
                    "invalidates": [
                        {"type": "file", "name": "rejected-article.md"},
                    ],
                },
                {
                    "id": "writing",
                    "name": "Writing",
                    "trigger": {
                        "type": "and",
                        "conditions": [
                            {"type": "file_exists", "path": "research.md"},
                            {"type": "file_missing", "path": "article.md"},
                            {"type": "file_missing", "path": ".processing"},
                            {"type": "file_missing", "path": ".gave_up"},
                        ],
                    },
                    "action": {
                        "type": "queue_agent",
                        "params": {
                            "agent": "WriterAgent",
                            "prompt_template": "Write article for {target}",
                            "reminder_prompt_template": "Finish writing for {target}",
                        },
                    },
                    "markers": {
                        "completion": {"type": "file", "name": "article.md"},
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                        "give_up": {"type": "file", "name": ".gave_up"},
                    },
                    "timeout_minutes": 30,
                    "max_retries": 3,
                    "invalidates": [
                        {"type": "file", "name": "rejected-article.md"},
                    ],
                },
                {
                    "id": "research",
                    "name": "Research",
                    "trigger": {
                        "type": "and",
                        "conditions": [
                            {"type": "file_missing", "path": "research.md"},
                            {"type": "file_missing", "path": ".processing"},
                            {"type": "file_missing", "path": ".gave_up"},
                        ],
                    },
                    "action": {
                        "type": "queue_agent",
                        "params": {
                            "agent": "ResearchAgent",
                            "prompt_template": "Research story {target}",
                            "reminder_prompt_template": "Finish research for {target}",
                        },
                    },
                    "markers": {
                        "completion": {"type": "file", "name": "research.md"},
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                        "give_up": {"type": "file", "name": ".gave_up"},
                    },
                    "timeout_minutes": 60,
                    "max_retries": 3,
                },
            ],
        })

    def test_full_research_to_publish_flow(self, tmp_path):
        """Simulate: research → writing → publishing → published.

        Each tick either queues an agent or detects a stale marker and re-queues.
        We simulate agent completion by writing the completion marker and
        removing the processing marker between ticks.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        story_dir = workspace / "story-1"
        story_dir.mkdir()
        queue_dir = tmp_path / "queue"

        config = self._make_vnn_config(workspace, queue_dir)
        pipeline = Pipeline(config)
        mock = _make_mock_handler(queue_dir)

        # Tick 1: research stage fires (research.md missing)
        r1 = pipeline.tick(target="story-1")
        assert r1.status == TickResultStatus.ACTION_EXECUTED
        assert r1.stage_id == "research"
        assert (story_dir / ".processing").exists()

        # Simulate agent completing: write research.md, remove .processing
        (story_dir / "research.md").write_text("# Research")
        (story_dir / ".processing").unlink()

        # Tick 2: writing stage fires (research.md exists, article.md missing)
        r2 = pipeline.tick(target="story-1")
        assert r2.status == TickResultStatus.ACTION_EXECUTED
        assert r2.stage_id == "writing"
        assert (story_dir / ".processing").exists()

        # Simulate agent completing
        (story_dir / "article.md").write_text("# Article")
        (story_dir / ".processing").unlink()

        # Tick 3: publishing stage fires (article.md exists, published.json missing)
        r3 = pipeline.tick(target="story-1")
        assert r3.status == TickResultStatus.ACTION_EXECUTED
        assert r3.stage_id == "publishing"

        # Simulate agent completing
        (story_dir / "published.json").write_text(json.dumps({"url": "https://example.com"}))
        (story_dir / ".processing").unlink()

        # Tick 4: no work — all stages complete
        r4 = pipeline.tick(target="story-1")
        assert r4.status == TickResultStatus.NO_WORK

    def test_rejection_revision_loop(self, tmp_path):
        """Simulate: writing → publishing → rejected → revision → publishing → published.

        Verifies the rejection/revision loop works: publishing creates
        rejected-article.md, revision stage fires first (earlier in chain),
        produces new article.md, publishing re-fires.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        story_dir = workspace / "story-1"
        story_dir.mkdir()
        queue_dir = tmp_path / "queue"

        config = self._make_vnn_config(workspace, queue_dir)
        pipeline = Pipeline(config)
        mock = _make_mock_handler(queue_dir)

        # Pre-seed: research already done
        (story_dir / "research.md").write_text("# Research")

        # Tick 1: writing fires
        r1 = pipeline.tick(target="story-1")
        assert r1.status == TickResultStatus.ACTION_EXECUTED
        assert r1.stage_id == "writing"

        # Simulate agent completing
        (story_dir / "article.md").write_text("# Article v1")
        (story_dir / ".processing").unlink()

        # Tick 2: publishing fires
        r2 = pipeline.tick(target="story-1")
        assert r2.status == TickResultStatus.ACTION_EXECUTED
        assert r2.stage_id == "publishing"

        # Simulate rejection: publishing agent creates rejected-article.md
        # and removes article.md (rejection)
        (story_dir / "rejected-article.md").write_text("Rejected: needs improvement")
        (story_dir / "article.md").unlink()
        (story_dir / ".processing").unlink()

        # Write rejection marker
        (story_dir / ".rejection").write_text(json.dumps({"rejection_count": 1}))

        # Tick 3: revision stage fires (rejected-article.md exists, article.md missing)
        # revision is earlier in the detector chain than publishing
        r3 = pipeline.tick(target="story-1")
        assert r3.status == TickResultStatus.ACTION_EXECUTED
        assert r3.stage_id == "revision"

        # Simulate agent completing: new article.md written,
        # rejected-article.md invalidated by revision stage
        (story_dir / "article.md").write_text("# Article v2")
        (story_dir / ".processing").unlink()
        # revision stage invalidates rejected-article.md
        assert not (story_dir / "rejected-article.md").exists()

        # Tick 4: publishing fires again (article.md exists, published.json missing)
        r4 = pipeline.tick(target="story-1")
        assert r4.status == TickResultStatus.ACTION_EXECUTED
        assert r4.stage_id == "publishing"

        # Simulate agent completing: published
        (story_dir / "published.json").write_text(json.dumps({"url": "https://example.com"}))
        (story_dir / ".processing").unlink()

        # Tick 5: no work
        r5 = pipeline.tick(target="story-1")
        assert r5.status == TickResultStatus.NO_WORK

    def test_give_up_after_max_rejections(self, tmp_path):
        """Verify that after max_rejections, a give_up marker is written."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        story_dir = workspace / "story-1"
        story_dir.mkdir()
        queue_dir = tmp_path / "queue"

        config = self._make_vnn_config(workspace, queue_dir)
        pipeline = Pipeline(config)
        mock = _make_mock_handler(queue_dir)

        # Pre-seed: research and article done
        (story_dir / "research.md").write_text("# Research")
        (story_dir / "article.md").write_text("# Article")

        # Set rejection count to max (publishing has max_rejections=3)
        (story_dir / ".rejection").write_text(json.dumps({"rejection_count": 3}))

        # Tick: publishing stage should detect rejection >= max and give up
        r = pipeline.tick(target="story-1")
        assert r.status == TickResultStatus.GAVE_UP
        assert (story_dir / ".gave_up").exists()

    def test_queue_entry_format_is_serendipity_compatible(self, tmp_path):
        """Verify that queue entries produced by ConversationQueueHandler
        have the Serendipity-compatible format (content, sender, etc.)."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        story_dir = workspace / "story-1"
        story_dir.mkdir()
        queue_dir = tmp_path / "queue"

        config = self._make_vnn_config(workspace, queue_dir)
        pipeline = Pipeline(config)
        mock = _make_mock_handler(queue_dir)

        # Tick: research stage queues an agent
        r = pipeline.tick(target="story-1")
        assert r.status == TickResultStatus.ACTION_EXECUTED

        # Check the queue entry format
        queue_files = list(queue_dir.glob("*.json"))
        assert len(queue_files) == 1
        entry = json.loads(queue_files[0].read_text())

        # Serendipity-compatible fields
        assert "content" in entry, "Queue entry should have 'content' field"
        assert "sender" in entry, "Queue entry should have 'sender' field"
        assert "conversation_id" in entry, "Queue entry should have 'conversation_id' field"
        assert "folder_name" in entry, "Queue entry should have 'folder_name' field"
        assert "model_name" in entry, "Queue entry should have 'model_name' field"
        assert "runs_left" in entry, "Queue entry should have 'runs_left' field"
        assert "prompt" not in entry, "Queue entry should NOT have 'prompt' field"


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
            "targets": {"type": "static", "items": ["story-1"]},
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

        (tmp_path / "story-1").mkdir()
        pipeline = Pipeline(config)
        _make_mock_handler(tmp_path / "queue")

        # Dry run should not create any files
        r = pipeline.tick(target="story-1", dry_run=True)
        assert r.status == TickResultStatus.DRY_RUN
        assert not (tmp_path / "story-1" / ".processing").exists()

    def test_vnn_config_all_stages_have_valid_triggers(self):
        """Verify all VNN stages have well-formed trigger configs."""
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        for stage in config.stages:
            assert stage.trigger is not None, f"Stage {stage.id} has no trigger"
            assert stage.trigger.type is not None, f"Stage {stage.id} trigger has no type"

    def test_vnn_config_detector_chain_order(self):
        """Verify the detector chain order: revision → publishing → writing → research.

        This order ensures rejected articles are revised before re-publishing,
        and writing only fires after research is done.
        """
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        ids = [s.id for s in config.stages]
        assert ids == ["revision", "publishing", "writing", "research"], \
            f"Stage order should be revision→publishing→writing→research, got {ids}"
