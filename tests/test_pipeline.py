"""Tests for cronpypeline.pipeline — Pipeline class, tick() orchestration."""

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cronpypeline.config import (
    PipelineConfig, Stage, TriggerCondition, TriggerType,
    ActionSpec, ActionType, MarkerSpec, TargetSpec, TargetType,
)
from cronpypeline.markers import MarkerType, create_marker, marker_exists
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
        from cronpypeline.actions import register_handler, ActionHandler, ActionResult
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
        from cronpypeline.actions import register_handler, ActionHandler, ActionResult
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
