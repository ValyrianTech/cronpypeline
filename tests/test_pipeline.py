"""Tests for cronpypeline.pipeline — Pipeline class, tick() orchestration."""

import json
import os
import time

import pytest

from cronpypeline.config import (
    ActionSpec,
    ActionType,
    MarkerSpec,
    PipelineConfig,
    Stage,
    TriggerCondition,
    TriggerType,
)
from cronpypeline.markers import MarkerType
from cronpypeline.pipeline import Pipeline, TickResult, TickResultStatus


def make_simple_config(workspace_dir, stages=None, targets=None):
    """Helper to build a minimal PipelineConfig dict."""
    config = {
        "name": "test-pipeline",
        "workspace_dir": str(workspace_dir),
        "stages": stages or [],
    }
    if targets:
        config["targets"] = targets
    return PipelineConfig.from_dict(config)


def make_command_stage(stage_id, name, marker_name, command="echo done"):
    """Helper to build a stage with a file_missing trigger and command action."""
    return Stage.from_dict({
        "id": stage_id,
        "name": name,
        "trigger": {"type": "file_missing", "path": marker_name},
        "action": {"type": "command", "params": {"command": command}},
        "markers": {"completion": {"type": "file", "name": marker_name}},
        "chain": False,
        "timeout_minutes": 30,
        "max_retries": 3,
    })


class TestPipelineCreation:
    """Tests for Pipeline construction."""

    def test_from_config(self, tmp_path):
        config = make_simple_config(tmp_path)
        pipeline = Pipeline(config)
        assert pipeline.config.name == "test-pipeline"

    def test_from_config_file(self, tmp_path):
        config_data = {
            "name": "file-pipeline",
            "workspace_dir": str(tmp_path),
            "stages": [],
        }
        config_file = tmp_path / "pipeline.json"
        config_file.write_text(json.dumps(config_data))
        pipeline = Pipeline.from_config(config_file)
        assert pipeline.config.name == "file-pipeline"


class TestTickBasic:
    """Tests for basic tick execution."""

    def test_tick_executes_first_actionable_stage(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        stages = [
            make_command_stage("A0", "Step 1", "a.md", "echo hello"),
        ]
        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [s.__dict__ for s in stages] if False else [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hello"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert result.stage_id == "A0"
        assert (target_dir / "a.md").exists()

    def test_tick_no_work_when_all_complete(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()
        (target_dir / "a.md").touch()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hello"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.NO_WORK

    def test_tick_skips_disabled_stages(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Disabled Step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo a"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                    "enabled": False,
                },
                {
                    "id": "A1",
                    "name": "Active Step",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "echo b"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert result.stage_id == "A1"
        assert (target_dir / "b.md").exists()
        assert not (target_dir / "a.md").exists()

    def test_tick_dry_run_does_not_execute(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hello"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo", dry_run=True)
        assert result.status == TickResultStatus.DRY_RUN
        assert not (target_dir / "a.md").exists()

    def test_tick_creates_completion_marker_on_success(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hello"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        pipeline.tick(target="my-repo")
        assert (target_dir / "a.md").exists()

    def test_tick_creates_processing_marker_for_queue_agent(self, tmp_path):
        """When action type is queue_agent, a processing marker should be created."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Agent Step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "TestAgent", "prompt": "Do stuff"}},
                    "markers": {
                        "completion": {"type": "file", "name": "a.md"},
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                    },
                },
            ],
        })
        pipeline = Pipeline(config)
        # Register a mock action handler for queue_agent
        from cronpypeline.actions import ActionHandler, ActionResult, register_handler
        from cronpypeline.config import ActionType

        class MockQueueHandler(ActionHandler):
            def execute(self, action, context):
                return ActionResult(success=True, stdout="queued")
            def check_complete(self, action, context):
                return False

        register_handler(ActionType.QUEUE_AGENT, MockQueueHandler())

        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert (target_dir / ".processing").exists()
        assert not (target_dir / "a.md").exists()  # completion not yet


class TestTickLocking:
    """Tests for lock integration in tick()."""

    def test_tick_acquires_lock(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "lock_file": str(tmp_path / "pipeline.lock"),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        pipeline.tick(target="my-repo")
        assert (tmp_path / "pipeline.lock").exists()

    def test_tick_dry_run_skips_lock(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "lock_file": str(tmp_path / "pipeline.lock"),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        pipeline.tick(target="my-repo", dry_run=True)
        assert not (tmp_path / "pipeline.lock").exists()


class TestTickChaining:
    """Tests for same-tick chaining of mechanical stages."""

    def test_chain_continues_to_next_stage(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo a"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                    "chain": True,
                },
                {
                    "id": "A1",
                    "name": "Step 2",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "echo b"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                    "chain": False,
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        # Both stages should have completed in one tick due to chaining
        assert (target_dir / "a.md").exists()
        assert (target_dir / "b.md").exists()
        # The last executed stage should be A1
        assert result.stage_id == "A1"

    def test_chain_stops_at_non_chain_stage(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo a"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                    "chain": False,  # No chaining
                },
                {
                    "id": "A1",
                    "name": "Step 2",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "echo b"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert (target_dir / "a.md").exists()
        assert not (target_dir / "b.md").exists()
        assert result.stage_id == "A0"

    def test_chain_stops_on_failure(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Failing Step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "false"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                    "chain": True,
                },
                {
                    "id": "A1",
                    "name": "Step 2",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "echo b"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_FAILED
        assert not (target_dir / "a.md").exists()
        assert not (target_dir / "b.md").exists()


class TestTickMultiTarget:
    """Tests for multi-target tick execution."""

    def test_tick_all_processes_multiple_targets(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()
        (workspace / "repo2").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "targets": {"type": "static", "items": ["repo1", "repo2"]},
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo a"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        results = pipeline.tick_all(dry_run=False, verbose=False)
        assert len(results) == 2
        assert all(r.status == TickResultStatus.ACTION_EXECUTED for r in results)
        assert (workspace / "repo1" / "a.md").exists()
        assert (workspace / "repo2" / "a.md").exists()

    def test_tick_without_target_uses_first_with_work(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()
        (workspace / "repo2").mkdir()
        # repo1 is already complete
        (workspace / "repo1" / "a.md").touch()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "targets": {"type": "static", "items": ["repo1", "repo2"]},
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo a"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick()
        assert result.target == "repo2"
        assert result.status == TickResultStatus.ACTION_EXECUTED


class TestTickStaleHandling:
    """Tests for stale task detection and cleanup."""

    def test_stale_processing_marker_triggers_retry(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        # Create a stale processing marker
        processing_data = {"retry_count": 1, "timestamp": time.time() - 3600}
        (target_dir / ".processing").write_text(json.dumps(processing_data))
        old_time = time.time() - 3600
        os.utime(target_dir / ".processing", (old_time, old_time))

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Agent Step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "TestAgent"}},
                    "markers": {
                        "completion": {"type": "file", "name": "a.md"},
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                    },
                    "timeout_minutes": 30,
                    "max_retries": 3,
                },
            ],
        })
        pipeline = Pipeline(config)
        # Register mock handler
        from cronpypeline.actions import ActionHandler, ActionResult, register_handler
        from cronpypeline.config import ActionType

        class MockQueueHandler(ActionHandler):
            def execute(self, action, context):
                return ActionResult(success=True, stdout="queued")
            def check_complete(self, action, context):
                return False

        register_handler(ActionType.QUEUE_AGENT, MockQueueHandler())

        result = pipeline.tick(target="my-repo")
        # Should clean up stale marker and re-queue
        assert result.status == TickResultStatus.ACTION_EXECUTED
        # New processing marker should have retry_count = 2
        new_data = json.loads((target_dir / ".processing").read_text())
        assert new_data["retry_count"] == 2

    def test_stale_marker_gives_up_after_max_retries(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        # Create a stale processing marker at max retries
        processing_data = {"retry_count": 3, "timestamp": time.time() - 3600}
        (target_dir / ".processing").write_text(json.dumps(processing_data))
        old_time = time.time() - 3600
        os.utime(target_dir / ".processing", (old_time, old_time))

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Agent Step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "TestAgent"}},
                    "markers": {
                        "completion": {"type": "file", "name": "a.md"},
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                        "give_up": {"type": "file", "name": ".gave_up"},
                    },
                    "timeout_minutes": 30,
                    "max_retries": 3,
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.GAVE_UP
        assert (target_dir / ".gave_up").exists()
        assert not (target_dir / ".processing").exists()


class TestActionHandlerWiring:
    """Tests for wiring action handlers from PipelineConfig.action_handler."""

    def test_conversation_queue_handler_wired_from_config(self, tmp_path):
        """Pipeline.__init__ should instantiate and register ConversationQueueHandler
        when action_handler config is present."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()
        queue_dir = tmp_path / "queue"

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "action_handler": {
                "type": "conversation_queue",
                "params": {"queue_dir": str(queue_dir)},
            },
            "stages": [
                {
                    "id": "A0",
                    "name": "Agent Step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "TestAgent", "prompt": "Do stuff"}},
                    "markers": {
                        "completion": {"type": "file", "name": "a.md"},
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                    },
                },
            ],
        })
        pipeline = Pipeline(config)

        # The handler should be registered for QUEUE_AGENT
        from cronpypeline.actions import _HANDLERS
        from cronpypeline.plugins.conversation_queue import ConversationQueueHandler
        handler = _HANDLERS.get(ActionType.QUEUE_AGENT)
        assert isinstance(handler, ConversationQueueHandler)
        assert handler.queue_dir == queue_dir

        # Tick should write to the queue dir
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert queue_dir.exists()
        files = list(queue_dir.glob("*.json"))
        assert len(files) == 1
        entry = json.loads(files[0].read_text())
        assert entry["agent"] == "TestAgent"

    def test_conversation_queue_handler_with_agent_settings_dir(self, tmp_path):
        """Pipeline should pass agent_settings_dir to ConversationQueueHandler."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        queue_dir = tmp_path / "queue"
        agent_settings_dir = tmp_path / "agents"

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "action_handler": {
                "type": "conversation_queue",
                "params": {
                    "queue_dir": str(queue_dir),
                    "agent_settings_dir": str(agent_settings_dir),
                },
            },
            "stages": [],
        })
        Pipeline(config)

        from cronpypeline.actions import _HANDLERS
        from cronpypeline.plugins.conversation_queue import ConversationQueueHandler
        handler = _HANDLERS.get(ActionType.QUEUE_AGENT)
        assert isinstance(handler, ConversationQueueHandler)
        assert handler.agent_settings_dir == agent_settings_dir

    def test_pipeline_without_action_handler_does_not_override(self, tmp_path):
        """Pipeline without action_handler config should not touch _HANDLERS."""
        from cronpypeline.actions import _HANDLERS, ActionHandler, ActionResult
        from cronpypeline.config import ActionType

        # Register a mock handler so we can verify it's not replaced
        class MockHandler(ActionHandler):
            def execute(self, action, context):
                return ActionResult(success=True)
            def check_complete(self, action, context):
                return True

        original = MockHandler()
        _HANDLERS[ActionType.QUEUE_AGENT] = original

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(tmp_path),
            "stages": [],
        })
        Pipeline(config)
        assert _HANDLERS[ActionType.QUEUE_AGENT] is original

    def test_unknown_action_handler_type_raises(self, tmp_path):
        """Pipeline with unknown action_handler type should raise ValueError."""
        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(tmp_path),
            "action_handler": {
                "type": "nonexistent_handler",
                "params": {},
            },
            "stages": [],
        })
        with pytest.raises(ValueError, match="Unknown action handler type"):
            Pipeline(config)


class TestRejectionCounter:
    """Tests for separate rejection counter."""

    def test_rejection_count_read_from_marker(self, tmp_path):
        """StageState should read rejection_count from a rejection marker."""
        from cronpypeline.config import Stage
        from cronpypeline.state import StageState

        workspace = tmp_path / "ws"
        target_dir = workspace / "repo1"
        target_dir.mkdir(parents=True)

        # Create a rejection marker with count
        import json as _json
        (target_dir / ".rejection").write_text(_json.dumps({"rejection_count": 3}))

        stage = Stage(
            id="A0",
            name="Review",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="done.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo hi"}),
            markers={
                "completion": MarkerSpec(type=MarkerType.FILE, name="done.md"),
                "rejection": MarkerSpec(type=MarkerType.JSON, name=".rejection", content={}),
            },
            max_rejections=5,
        )
        ss = StageState(stage=stage)
        ss.derive(target_dir)
        assert ss.rejection_count == 3
        assert ss.is_rejected

    def test_rejection_marker_makes_stage_not_actionable(self, tmp_path):
        """If a rejection marker exists, stage should not be actionable (it needs re-processing)."""
        from cronpypeline.config import Stage
        from cronpypeline.state import StageState

        workspace = tmp_path / "ws"
        target_dir = workspace / "repo1"
        target_dir.mkdir(parents=True)

        import json as _json
        (target_dir / ".rejection").write_text(_json.dumps({"rejection_count": 1}))

        stage = Stage(
            id="A0",
            name="Review",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="done.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo hi"}),
            markers={
                "completion": MarkerSpec(type=MarkerType.FILE, name="done.md"),
                "rejection": MarkerSpec(type=MarkerType.JSON, name=".rejection", content={}),
            },
            max_rejections=5,
        )
        ss = StageState(stage=stage)
        ss.derive(target_dir)
        assert ss.is_rejected
        assert not ss.is_actionable

    def test_rejection_give_up_after_max(self, tmp_path):
        """Stage should give up when rejection_count >= max_rejections."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        import json as _json
        # Create rejection marker with count at max
        (workspace / "my-repo" / ".rejection").write_text(_json.dumps({"rejection_count": 3}))

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Review",
                    "trigger": {"type": "file_missing", "path": "done.md"},
                    "action": {"type": "command", "params": {"command": "echo review"}},
                    "markers": {
                        "completion": {"type": "file", "name": "done.md"},
                        "rejection": {"type": "json", "name": ".rejection", "content": {}},
                        "give_up": {"type": "file", "name": ".gave_up"},
                    },
                    "max_rejections": 3,
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.GAVE_UP
        assert "rejection" in result.message.lower()
        assert (workspace / "my-repo" / ".gave_up").exists()

    def test_rejection_below_max_allows_reprocessing(self, tmp_path):
        """Rejection below max should allow the stage to be re-processed."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        import json as _json
        (workspace / "my-repo" / ".rejection").write_text(_json.dumps({"rejection_count": 1}))

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Review",
                    "trigger": {"type": "file_missing", "path": "done.md"},
                    "action": {"type": "command", "params": {"command": "echo review"}},
                    "markers": {
                        "completion": {"type": "file", "name": "done.md"},
                        "rejection": {"type": "json", "name": ".rejection", "content": {}},
                    },
                    "max_rejections": 5,
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        # Rejection exists but below max — stage should be actionable (not blocked by rejection)
        # The trigger is file_missing done.md, which is missing, so it should execute
        assert result.status == TickResultStatus.ACTION_EXECUTED

    def test_no_rejection_marker_normal_behavior(self, tmp_path):
        """Without a rejection marker, stage should behave normally."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step",
                    "trigger": {"type": "file_missing", "path": "done.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {
                        "completion": {"type": "file", "name": "done.md"},
                        "rejection": {"type": "json", "name": ".rejection", "content": {}},
                    },
                    "max_rejections": 3,
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED


class TestRetryPromptSupport:
    """Tests for retry/reminder prompt support on stale re-queue."""

    def test_retry_uses_reminder_prompt(self, tmp_path):
        """When re-queuing a stale stage, reminder_prompt should be used instead of prompt."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        queue_dir = tmp_path / "queue"

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "action_handler": {
                "type": "conversation_queue",
                "params": {"queue_dir": str(queue_dir)},
            },
            "stages": [
                {
                    "id": "A0",
                    "name": "Async step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {
                        "type": "queue_agent",
                        "params": {
                            "agent": "test",
                            "prompt": "Original prompt for {target}",
                            "reminder_prompt": "Reminder: please finish {target}",
                        },
                    },
                    "markers": {
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                        "completion": {"type": "file", "name": "a.md"},
                    },
                    "timeout_minutes": 30,
                },
            ],
        })
        pipeline = Pipeline(config)

        # First tick: creates processing marker with original prompt
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED

        import json as _json
        queue_files = list(queue_dir.glob("*.json"))
        assert len(queue_files) == 1
        entry1 = _json.loads(queue_files[0].read_text())
        assert entry1["prompt"] == "Original prompt for my-repo"

        # Simulate stale: remove queue file (agent finished without producing completion)
        queue_files[0].unlink()

        # Second tick: should detect stale and re-queue with reminder_prompt
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED

        queue_files = list(queue_dir.glob("*.json"))
        assert len(queue_files) == 1
        entry2 = _json.loads(queue_files[0].read_text())
        assert entry2["prompt"] == "Reminder: please finish my-repo"

    def test_retry_without_reminder_prompt_uses_original(self, tmp_path):
        """Without reminder_prompt, stale re-queue should use the original prompt."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        queue_dir = tmp_path / "queue"

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "action_handler": {
                "type": "conversation_queue",
                "params": {"queue_dir": str(queue_dir)},
            },
            "stages": [
                {
                    "id": "A0",
                    "name": "Async step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {
                        "type": "queue_agent",
                        "params": {
                            "agent": "test",
                            "prompt": "Original prompt for {target}",
                        },
                    },
                    "markers": {
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                        "completion": {"type": "file", "name": "a.md"},
                    },
                    "timeout_minutes": 30,
                },
            ],
        })
        pipeline = Pipeline(config)

        # First tick
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED

        import json as _json
        queue_files = list(queue_dir.glob("*.json"))
        assert len(queue_files) == 1
        entry1 = _json.loads(queue_files[0].read_text())
        assert entry1["prompt"] == "Original prompt for my-repo"

        # Simulate stale
        queue_files[0].unlink()

        # Second tick: should re-queue with original prompt (no reminder_prompt configured)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED

        queue_files = list(queue_dir.glob("*.json"))
        assert len(queue_files) == 1
        entry2 = _json.loads(queue_files[0].read_text())
        assert entry2["prompt"] == "Original prompt for my-repo"

    def test_retry_increments_retry_count(self, tmp_path):
        """Re-queuing a stale stage should increment retry_count in processing marker."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        queue_dir = tmp_path / "queue"

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "action_handler": {
                "type": "conversation_queue",
                "params": {"queue_dir": str(queue_dir)},
            },
            "stages": [
                {
                    "id": "A0",
                    "name": "Async step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {
                        "type": "queue_agent",
                        "params": {"agent": "test", "prompt": "Do {target}"},
                    },
                    "markers": {
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                        "completion": {"type": "file", "name": "a.md"},
                    },
                    "timeout_minutes": 30,
                },
            ],
        })
        pipeline = Pipeline(config)

        # First tick
        pipeline.tick(target="my-repo")

        import json as _json
        proc_data = _json.loads((workspace / "my-repo" / ".processing").read_text())
        assert proc_data["retry_count"] == 0

        # Simulate stale
        queue_files = list(queue_dir.glob("*.json"))
        queue_files[0].unlink()

        # Second tick: re-queue
        pipeline.tick(target="my-repo")

        proc_data = _json.loads((workspace / "my-repo" / ".processing").read_text())
        assert proc_data["retry_count"] == 1


class TestQueueFileStaleDetection:
    """Tests for queue-file-based stale detection."""

    def test_processing_stale_when_queue_file_gone(self, tmp_path):
        """Processing marker is stale if queue file referenced in it no longer exists."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        # Queue file already gone (agent finished but didn't produce completion)

        # Create a processing marker referencing a queue file that doesn't exist
        import json as _json
        processing_data = {
            "retry_count": 0,
            "queue_file": str(queue_dir / "abc123.json"),
        }
        (workspace / "my-repo" / ".processing").write_text(_json.dumps(processing_data))

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Async step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "test", "prompt": "do"}},
                    "markers": {
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                        "completion": {"type": "file", "name": "a.md"},
                    },
                    "timeout_minutes": 30,
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        # Should detect stale and re-queue (action_executed with retry message)
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert "retry" in result.message.lower() or "stale" in result.message.lower()

    def test_processing_not_stale_when_queue_file_exists(self, tmp_path):
        """Processing marker is NOT stale if queue file still exists (agent still working)."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        # Queue file still exists — agent is still working
        queue_file = queue_dir / "abc123.json"
        queue_file.write_text('{"agent": "test"}')

        import json as _json
        processing_data = {
            "retry_count": 0,
            "queue_file": str(queue_file),
        }
        (workspace / "my-repo" / ".processing").write_text(_json.dumps(processing_data))

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Async step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "test", "prompt": "do"}},
                    "markers": {
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                        "completion": {"type": "file", "name": "a.md"},
                    },
                    "timeout_minutes": 30,
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        # Queue file exists, so not stale — should be NO_WORK (still processing)
        assert result.status == TickResultStatus.NO_WORK

    def test_processing_stale_without_queue_file_field(self, tmp_path):
        """Processing marker without queue_file field falls back to time-based staleness."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        import json as _json
        import os as _os
        import time as _time
        processing_data = {
            "retry_count": 0,
        }
        proc_path = workspace / "my-repo" / ".processing"
        proc_path.write_text(_json.dumps(processing_data))
        # Set mtime to 2 hours ago so time-based staleness triggers (timeout is 30min)
        old_time = _time.time() - 7200
        _os.utime(proc_path, (old_time, old_time))

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Async step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "test", "prompt": "do"}},
                    "markers": {
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                        "completion": {"type": "file", "name": "a.md"},
                    },
                    "timeout_minutes": 30,
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        # No queue_file field, but timestamp is 0 → time-based stale
        assert result.status == TickResultStatus.ACTION_EXECUTED

    def test_processing_writes_queue_file_to_marker(self, tmp_path):
        """When a queue_agent action creates a processing marker, it should include queue_file path."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        queue_dir = tmp_path / "queue"

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "action_handler": {
                "type": "conversation_queue",
                "params": {"queue_dir": str(queue_dir)},
            },
            "stages": [
                {
                    "id": "A0",
                    "name": "Async step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "test", "prompt": "do"}},
                    "markers": {
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                        "completion": {"type": "file", "name": "a.md"},
                    },
                    "timeout_minutes": 30,
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED

        # Check that processing marker contains queue_file
        import json as _json
        from pathlib import Path as _P
        proc_data = _json.loads((workspace / "my-repo" / ".processing").read_text())
        assert "queue_file" in proc_data
        assert _P(proc_data["queue_file"]).exists()

    def test_processing_marker_stores_entry_id(self, tmp_path):
        """Processing marker should store entry_id from result.data."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        queue_dir = tmp_path / "queue"

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "action_handler": {
                "type": "conversation_queue",
                "params": {"queue_dir": str(queue_dir)},
            },
            "stages": [
                {
                    "id": "A0",
                    "name": "Async step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "test", "prompt": "do"}},
                    "markers": {
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                        "completion": {"type": "file", "name": "a.md"},
                    },
                    "timeout_minutes": 30,
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED

        import json as _json
        proc_data = _json.loads((workspace / "my-repo" / ".processing").read_text())
        assert "entry_id" in proc_data
        assert proc_data["entry_id"]  # non-empty UUID


class TestCrossStageTargetLock:
    """Tests for cross-stage target lock — processing on one stage blocks others."""

    def test_processing_on_stage_a_blocks_stage_b(self, tmp_path):
        """If stage A has a processing marker, stage B should not be actionable."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        # Manually create a processing marker for stage A0
        (workspace / "my-repo" / ".processing_a").write_text('{"retry_count": 0}')

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "target_lock": True,
            "stages": [
                {
                    "id": "A0",
                    "name": "Async step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "test", "prompt": "do"}},
                    "markers": {
                        "processing": {"type": "json", "name": ".processing_a", "content": {}},
                        "completion": {"type": "file", "name": "a.md"},
                    },
                    "timeout_minutes": 30,
                },
                {
                    "id": "B0",
                    "name": "Sync step",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "echo b"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        # A0 is processing, B0 should be blocked by target lock
        assert result.status == TickResultStatus.NO_WORK
        assert not (workspace / "my-repo" / "b.md").exists()

    def test_no_target_lock_allows_concurrent_stages(self, tmp_path):
        """Without target_lock, stage B can proceed even if A is processing."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        # Manually create a processing marker for stage A0
        (workspace / "my-repo" / ".processing_a").write_text('{"retry_count": 0}')

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Async step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "test", "prompt": "do"}},
                    "markers": {
                        "processing": {"type": "json", "name": ".processing_a", "content": {}},
                        "completion": {"type": "file", "name": "a.md"},
                    },
                    "timeout_minutes": 30,
                },
                {
                    "id": "B0",
                    "name": "Sync step",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "echo b"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        # Without target_lock, B0 should proceed
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert (workspace / "my-repo" / "b.md").exists()

    def test_target_lock_with_completion_allows_next_stage(self, tmp_path):
        """After stage A completes (no processing marker), stage B should proceed."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        # A0 is complete (a.md exists), no processing marker
        (workspace / "my-repo" / "a.md").touch()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "target_lock": True,
            "stages": [
                {
                    "id": "A0",
                    "name": "Async step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "test", "prompt": "do"}},
                    "markers": {
                        "processing": {"type": "json", "name": ".processing_a", "content": {}},
                        "completion": {"type": "file", "name": "a.md"},
                    },
                    "timeout_minutes": 30,
                },
                {
                    "id": "B0",
                    "name": "Sync step",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "echo b"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        # A0 is complete, B0 should proceed
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert (workspace / "my-repo" / "b.md").exists()


class TestModeFile:
    """Tests for pipeline-wide mode switching via mode_file."""

    def test_stage_active_in_matching_mode(self, tmp_path):
        """Stage with modes matching current mode should be active."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        mode_file = tmp_path / "mode.json"
        mode_file.write_text(json.dumps({"mode": "production"}))

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "mode_file": str(mode_file),
            "stages": [
                {
                    "id": "A0",
                    "name": "Prod step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo prod"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                    "modes": ["production"],
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert (workspace / "my-repo" / "a.md").exists()

    def test_stage_skipped_in_non_matching_mode(self, tmp_path):
        """Stage with modes not matching current mode should be skipped."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        mode_file = tmp_path / "mode.json"
        mode_file.write_text(json.dumps({"mode": "staging"}))

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "mode_file": str(mode_file),
            "stages": [
                {
                    "id": "A0",
                    "name": "Prod step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo prod"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                    "modes": ["production"],
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        # Stage A0 is skipped because mode is staging, not production
        assert result.status == TickResultStatus.NO_WORK
        assert not (workspace / "my-repo" / "a.md").exists()

    def test_stage_without_modes_always_active(self, tmp_path):
        """Stage without modes field should be active in any mode."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        mode_file = tmp_path / "mode.json"
        mode_file.write_text(json.dumps({"mode": "staging"}))

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "mode_file": str(mode_file),
            "stages": [
                {
                    "id": "A0",
                    "name": "Always step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo always"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED

    def test_no_mode_file_all_stages_active(self, tmp_path):
        """Without mode_file configured, all stages should be active."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                    "modes": ["production"],
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED

    def test_mode_file_missing_treats_as_no_mode(self, tmp_path):
        """Missing mode_file should be treated as no mode restriction."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "mode_file": str(tmp_path / "nonexistent_mode.json"),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                    "modes": ["production"],
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED

    def test_multiple_modes_match(self, tmp_path):
        """Stage active in multiple modes should match one of them."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        mode_file = tmp_path / "mode.json"
        mode_file.write_text(json.dumps({"mode": "staging"}))

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "mode_file": str(mode_file),
            "stages": [
                {
                    "id": "A0",
                    "name": "Multi-mode step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                    "modes": ["production", "staging"],
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED


class TestTickHooks:
    """Tests for pre-tick and post-tick hooks."""

    def test_pre_tick_hook_called(self, tmp_path):
        """pre_tick hook should be called before tick execution."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        hook_mod = tmp_path / "hook_mod.py"
        hook_mod.write_text("""
calls = []

def pre_tick(context):
    calls.append(("pre", context.get("target"), context.get("stage_id")))
    return True  # True = proceed, False = skip

def post_tick(context, result):
    calls.append(("post", context.get("target"), result.status.value))
""")
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            config = PipelineConfig.from_dict({
                "name": "test",
                "workspace_dir": str(workspace),
                "pre_tick": {"callable": "hook_mod.pre_tick"},
                "post_tick": {"callable": "hook_mod.post_tick"},
                "stages": [
                    {
                        "id": "A0",
                        "name": "Step 1",
                        "trigger": {"type": "file_missing", "path": "a.md"},
                        "action": {"type": "command", "params": {"command": "echo hi"}},
                        "markers": {"completion": {"type": "file", "name": "a.md"}},
                    },
                ],
            })
            pipeline = Pipeline(config)
            result = pipeline.tick(target="my-repo")
            assert result.status == TickResultStatus.ACTION_EXECUTED

            import hook_mod
            assert len(hook_mod.calls) == 2
            assert hook_mod.calls[0][0] == "pre"
            assert hook_mod.calls[0][1] == "my-repo"
            assert hook_mod.calls[1][0] == "post"
            assert hook_mod.calls[1][2] == "action_executed"
        finally:
            sys.path.remove(str(tmp_path))
            if "hook_mod" in sys.modules:
                del sys.modules["hook_mod"]

    def test_pre_tick_returns_false_skips_tick(self, tmp_path):
        """pre_tick hook returning False should skip the tick."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        hook_mod = tmp_path / "skip_hook.py"
        hook_mod.write_text("""
def pre_tick(context):
    return False  # Skip this tick

def post_tick(context, result):
    raise RuntimeError("post_tick should not be called when pre_tick returns False")
""")
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            config = PipelineConfig.from_dict({
                "name": "test",
                "workspace_dir": str(workspace),
                "pre_tick": {"callable": "skip_hook.pre_tick"},
                "post_tick": {"callable": "skip_hook.post_tick"},
                "stages": [
                    {
                        "id": "A0",
                        "name": "Step 1",
                        "trigger": {"type": "file_missing", "path": "a.md"},
                        "action": {"type": "command", "params": {"command": "echo hi"}},
                        "markers": {"completion": {"type": "file", "name": "a.md"}},
                    },
                ],
            })
            pipeline = Pipeline(config)
            result = pipeline.tick(target="my-repo")
            assert result.status == TickResultStatus.NO_WORK
            assert "skipped" in result.message.lower()
            # a.md should not have been created
            assert not (workspace / "my-repo" / "a.md").exists()
        finally:
            sys.path.remove(str(tmp_path))
            if "skip_hook" in sys.modules:
                del sys.modules["skip_hook"]

    def test_post_tick_called_on_no_work(self, tmp_path):
        """post_tick hook should be called even when there's no work."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        hook_mod = tmp_path / "nowork_hook.py"
        hook_mod.write_text("""
calls = []

def post_tick(context, result):
    calls.append(result.status.value)
""")
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            config = PipelineConfig.from_dict({
                "name": "test",
                "workspace_dir": str(workspace),
                "post_tick": {"callable": "nowork_hook.post_tick"},
                "stages": [
                    {
                        "id": "A0",
                        "name": "Step 1",
                        "trigger": {"type": "file_exists", "path": "a.md"},
                        "action": {"type": "command", "params": {"command": "echo hi"}},
                        "markers": {"completion": {"type": "file", "name": "a.md"}},
                    },
                ],
            })
            pipeline = Pipeline(config)
            result = pipeline.tick(target="my-repo")
            assert result.status == TickResultStatus.NO_WORK

            import nowork_hook
            assert len(nowork_hook.calls) == 1
            assert nowork_hook.calls[0] == "no_work"
        finally:
            sys.path.remove(str(tmp_path))
            if "nowork_hook" in sys.modules:
                del sys.modules["nowork_hook"]

    def test_no_hooks_configured(self, tmp_path):
        """Pipeline without hooks should work normally."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED


class TestCrossStageInvalidation:
    """Tests for cross-stage marker invalidation via invalidates field."""

    def test_invalidates_deletes_completion_marker(self, tmp_path):
        """When a stage completes, it should delete markers listed in invalidates."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo step1"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
                {
                    "id": "B0",
                    "name": "Step 2",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "echo step2"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                    "invalidates": [
                        {"type": "file", "name": "a.md"},
                    ],
                },
            ],
        })
        pipeline = Pipeline(config)

        # Complete stage A0
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert (workspace / "my-repo" / "a.md").exists()

        # Complete stage B0 — should invalidate a.md
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert (workspace / "my-repo" / "b.md").exists()
        assert not (workspace / "my-repo" / "a.md").exists()

    def test_invalidates_with_dynamic_name(self, tmp_path):
        """Invalidates should support dynamic marker names."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "repo1_a.md"},
                    "action": {"type": "command", "params": {"command": "echo step1"}},
                    "markers": {"completion": {"type": "file", "name": "{target}_a.md"}},
                },
                {
                    "id": "B0",
                    "name": "Step 2",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "echo step2"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                    "invalidates": [
                        {"type": "file", "name": "{target}_a.md"},
                    ],
                },
            ],
        })
        pipeline = Pipeline(config)

        # Complete stage A0
        result = pipeline.tick(target="repo1")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert (workspace / "repo1" / "repo1_a.md").exists()

        # Complete stage B0 — should invalidate repo1_a.md
        result = pipeline.tick(target="repo1")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert not (workspace / "repo1" / "repo1_a.md").exists()

    def test_invalidates_json_marker(self, tmp_path):
        """Invalidates should delete JSON markers too."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo step1"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
                {
                    "id": "B0",
                    "name": "Step 2",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "echo step2"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                    "invalidates": [
                        {"type": "json", "name": ".processing"},
                    ],
                },
            ],
        })
        pipeline = Pipeline(config)

        # Create a .processing marker manually (simulating leftover from another stage)
        (workspace / "my-repo" / ".processing").write_text('{"retry_count": 0}')

        # Complete stage A0 (trigger is file_missing a.md, which is missing)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert (workspace / "my-repo" / "a.md").exists()

        # Complete stage B0 — should invalidate .processing
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert not (workspace / "my-repo" / ".processing").exists()

    def test_no_invalidates_field(self, tmp_path):
        """Stage without invalidates should not delete any markers."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo step1"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
                {
                    "id": "B0",
                    "name": "Step 2",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "echo step2"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)

        # Complete stage A0
        pipeline.tick(target="my-repo")
        assert (workspace / "my-repo" / "a.md").exists()

        # Complete stage B0 — a.md should still exist
        pipeline.tick(target="my-repo")
        assert (workspace / "my-repo" / "a.md").exists()
        assert (workspace / "my-repo" / "b.md").exists()


class TestEnrichedContext:
    """Tests for per-target config and enriched context flowing to triggers."""

    def test_target_config_passed_to_custom_trigger(self, tmp_path):
        """Custom trigger should receive target_config from registry."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()

        registry = {"repos": [
            {"name": "repo1", "enabled": True, "test_cmd": "pytest -q"},
        ]}
        registry_file = tmp_path / "repos.json"
        registry_file.write_text(json.dumps(registry))

        trigger_mod = tmp_path / "ctx_trigger.py"
        trigger_mod.write_text("""
captured = {}

def my_trigger(context):
    captured.update(context)
    return True
""")
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            config = PipelineConfig.from_dict({
                "name": "test",
                "workspace_dir": str(workspace),
                "targets": {
                    "type": "registry",
                    "file": str(registry_file),
                    "key": "repos",
                    "filter": {"enabled": True},
                },
                "stages": [
                    {
                        "id": "A0",
                        "name": "Step 1",
                        "trigger": {
                            "type": "custom",
                            "callable": "ctx_trigger.my_trigger",
                        },
                        "action": {"type": "command", "params": {"command": "echo hi"}},
                        "markers": {"completion": {"type": "file", "name": "a.md"}},
                    },
                ],
            })
            pipeline = Pipeline(config)
            result = pipeline.tick(target="repo1")
            assert result.status == TickResultStatus.ACTION_EXECUTED

            import ctx_trigger
            assert ctx_trigger.captured.get("target") == "repo1"
            assert ctx_trigger.captured.get("target_config", {}).get("test_cmd") == "pytest -q"
            assert "target_dir" in ctx_trigger.captured
            assert "workspace_dir" in ctx_trigger.captured
        finally:
            sys.path.remove(str(tmp_path))
            if "ctx_trigger" in sys.modules:
                del sys.modules["ctx_trigger"]

    def test_target_config_passed_to_action_handler(self, tmp_path):
        """Action handler should receive target_config via TickContext."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()

        registry = {"repos": [
            {"name": "repo1", "enabled": True, "test_cmd": "pytest -q"},
        ]}
        registry_file = tmp_path / "repos.json"
        registry_file.write_text(json.dumps(registry))

        action_mod = tmp_path / "ctx_action.py"
        action_mod.write_text("""
captured = {}

def my_action(action, context):
    captured.update({
        "target": context.target,
        "target_config": context.target_config,
    })
    return True, "ok"
""")
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            config = PipelineConfig.from_dict({
                "name": "test",
                "workspace_dir": str(workspace),
                "targets": {
                    "type": "registry",
                    "file": str(registry_file),
                    "key": "repos",
                    "filter": {"enabled": True},
                },
                "stages": [
                    {
                        "id": "A0",
                        "name": "Step 1",
                        "trigger": {"type": "file_missing", "path": "a.md"},
                        "action": {
                            "type": "custom",
                            "params": {"callable": "ctx_action.my_action"},
                        },
                        "markers": {"completion": {"type": "file", "name": "a.md"}},
                    },
                ],
            })
            pipeline = Pipeline(config)
            result = pipeline.tick(target="repo1")
            assert result.status == TickResultStatus.ACTION_EXECUTED

            import ctx_action
            assert ctx_action.captured.get("target") == "repo1"
            assert ctx_action.captured.get("target_config", {}).get("test_cmd") == "pytest -q"
        finally:
            sys.path.remove(str(tmp_path))
            if "ctx_action" in sys.modules:
                del sys.modules["ctx_action"]


class TestTickConfigFile:
    """Tests for config_file enabled/disabled check."""

    def test_disabled_config_file_returns_disabled(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        config_toggle = tmp_path / "toggle.json"
        config_toggle.write_text(json.dumps({"enabled": False}))

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "config_file": str(config_toggle),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.DISABLED
        assert not (workspace / "my-repo" / "a.md").exists()

    def test_enabled_config_file_proceeds_normally(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        config_toggle = tmp_path / "toggle.json"
        config_toggle.write_text(json.dumps({"enabled": True}))

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "config_file": str(config_toggle),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert (workspace / "my-repo" / "a.md").exists()

    def test_no_config_file_proceeds_normally(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED

    def test_missing_config_file_treats_as_enabled(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "config_file": str(tmp_path / "nonexistent_toggle.json"),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED

    def test_config_file_without_enabled_key_proceeds(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        config_toggle = tmp_path / "toggle.json"
        config_toggle.write_text(json.dumps({"other_setting": "value"}))

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "config_file": str(config_toggle),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED


class TestTickResult:
    """Tests for TickResult dataclass."""

    def test_action_executed_result(self):
        r = TickResult(
            target="repo",
            stage_id="A0",
            status=TickResultStatus.ACTION_EXECUTED,
            message="Command succeeded",
        )
        assert r.status == TickResultStatus.ACTION_EXECUTED
        assert r.target == "repo"

    def test_no_work_result(self):
        r = TickResult(
            target="repo",
            stage_id=None,
            status=TickResultStatus.NO_WORK,
            message="Nothing to do",
        )
        assert r.status == TickResultStatus.NO_WORK

    def test_dry_run_result(self):
        r = TickResult(
            target="repo",
            stage_id="A0",
            status=TickResultStatus.DRY_RUN,
            message="Would execute A0",
        )
        assert r.status == TickResultStatus.DRY_RUN

    def test_result_str_representation(self):
        r = TickResult(
            target="repo",
            stage_id="A0",
            status=TickResultStatus.ACTION_EXECUTED,
            message="Done",
        )
        s = str(r)
        assert "repo" in s
        assert "A0" in s
