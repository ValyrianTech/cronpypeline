"""Tests for cronpypeline.pipeline — Pipeline class, tick() orchestration."""

import json
import os
import sys
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

    def test_make_simple_config_with_targets(self, tmp_path):
        config = make_simple_config(
            tmp_path,
            targets={"type": "static", "items": ["repo1", "repo2"]},
        )
        assert config.targets is not None
        assert config.targets.items == ["repo1", "repo2"]

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


class TestTickResultStr:
    """Tests for TickResult.__str__ formatting."""

    def test_str_omits_stderr_when_empty(self):
        result = TickResult(
            target="repo1",
            stage_id="A0",
            status=TickResultStatus.ACTION_EXECUTED,
            message="done",
        )
        assert str(result) == "repo1 | A0 -> action_executed | done"

    def test_str_uses_dash_when_stage_id_none(self):
        result = TickResult(
            target="repo1",
            stage_id=None,
            status=TickResultStatus.NO_WORK,
            message="no work",
        )
        assert str(result) == "repo1 | - -> no_work | no work"

    def test_str_includes_stderr_when_non_empty(self):
        result = TickResult(
            target="repo1",
            stage_id="A0",
            status=TickResultStatus.ACTION_FAILED,
            message="action failed",
            stderr="Traceback (most recent call last):\n  ...",
        )
        expected = (
            "repo1 | A0 -> action_failed | action failed\n"
            "Traceback (most recent call last):\n  ..."
        )
        assert str(result) == expected


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

        handler = MockQueueHandler()
        register_handler(ActionType.QUEUE_AGENT, handler)

        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert (target_dir / ".processing").exists()
        assert not (target_dir / "a.md").exists()  # completion not yet
        assert handler.check_complete(None, None) is False


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


class TestTickAllExceptionHandling:
    """Tests for tick_all continuing when one target raises an exception."""

    def test_tick_all_continues_when_one_target_raises(self, tmp_path):
        """When one target raises an exception, remaining targets should still be processed."""
        from unittest.mock import patch

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

        original_tick_single = pipeline._tick_single

        def raising_tick_single(target, target_config, dry_run, verbose):
            if target == "repo1":
                raise KeyError("malformed marker")
            return original_tick_single(target, target_config, dry_run, verbose)

        with patch.object(pipeline, "_tick_single", raising_tick_single):
            results = pipeline.tick_all(dry_run=False, verbose=False)

        # Both targets should have results
        assert len(results) == 2
        # repo1 failed with unhandled exception
        repo1_result = next(r for r in results if r.target == "repo1")
        assert repo1_result.status == TickResultStatus.ACTION_FAILED
        assert repo1_result.stage_id is None
        assert repo1_result.message.startswith("Unhandled KeyError:")
        assert "malformed marker" in repo1_result.message
        # repo2 was still processed
        repo2_result = next(r for r in results if r.target == "repo2")
        assert repo2_result.status == TickResultStatus.ACTION_EXECUTED
        assert (workspace / "repo2" / "a.md").exists()

    def test_tick_all_exception_message_captured(self, tmp_path):
        """The exception message should be captured in the TickResult."""
        from unittest.mock import patch

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

        original_tick_single = pipeline._tick_single

        def raising_tick_single(target, target_config, dry_run, verbose):
            if target == "repo1":
                raise ValueError("unknown trigger type")
            return original_tick_single(target, target_config, dry_run, verbose)

        with patch.object(pipeline, "_tick_single", raising_tick_single):
            results = pipeline.tick_all(dry_run=False, verbose=False)

        repo1_result = next(r for r in results if r.target == "repo1")
        assert repo1_result.status == TickResultStatus.ACTION_FAILED
        assert "unknown trigger type" in repo1_result.message
        # repo2 still processed
        repo2_result = next(r for r in results if r.target == "repo2")
        assert repo2_result.status == TickResultStatus.ACTION_EXECUTED

    def test_tick_all_single_target_exception(self, tmp_path):
        """When the only target raises, the exception should be recorded and lock released."""
        from unittest.mock import patch

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "targets": {"type": "static", "items": ["repo1"]},
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

        def raising_tick_single(target, target_config, dry_run, verbose):
            raise OSError("filesystem issue")

        with patch.object(pipeline, "_tick_single", raising_tick_single):
            results = pipeline.tick_all(dry_run=False, verbose=False)

        assert len(results) == 1
        assert results[0].target == "repo1"
        assert results[0].status == TickResultStatus.ACTION_FAILED
        assert "filesystem issue" in results[0].message
        # Lock should be released
        assert not pipeline.lock.is_acquired

    def test_tick_all_exception_includes_type_and_traceback(self, tmp_path):
        """The exception type should be in the message and traceback in stderr."""
        from unittest.mock import patch

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "targets": {"type": "static", "items": ["repo1"]},
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

        def raising_tick_single(target, target_config, dry_run, verbose):
            raise KeyError("malformed marker")

        with patch.object(pipeline, "_tick_single", raising_tick_single):
            results = pipeline.tick_all(dry_run=False, verbose=False)

        assert len(results) == 1
        result = results[0]
        assert result.status == TickResultStatus.ACTION_FAILED
        assert result.message == "Unhandled KeyError: 'malformed marker'"
        assert "KeyError" in result.stderr
        assert "malformed marker" in result.stderr
        assert "raising_tick_single" in result.stderr
        assert "Traceback" in result.stderr

    def test_tick_all_exception_with_empty_str(self, tmp_path):
        """Exceptions with empty str(e) should still produce a useful message."""
        from unittest.mock import patch

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "targets": {"type": "static", "items": ["repo1"]},
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

        def raising_tick_single(target, target_config, dry_run, verbose):
            raise KeyError()  # str(e) is empty

        with patch.object(pipeline, "_tick_single", raising_tick_single):
            results = pipeline.tick_all(dry_run=False, verbose=False)

        assert len(results) == 1
        result = results[0]
        assert result.status == TickResultStatus.ACTION_FAILED
        assert result.message == "Unhandled KeyError: "
        assert "KeyError" in result.stderr


class TestTickExceptionHandling:
    """Tests for tick() converting exceptions to TickResult."""

    def test_tick_catches_exception_and_returns_action_failed(self, tmp_path):
        """When _tick_single raises, tick() should return ACTION_FAILED."""
        from unittest.mock import patch

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "targets": {"type": "static", "items": ["repo1"]},
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

        def raising_tick_single(target, target_config, dry_run, verbose):
            raise KeyError("malformed marker")

        with patch.object(pipeline, "_tick_single", raising_tick_single):
            result = pipeline.tick(target="repo1")

        assert result.target == "repo1"
        assert result.stage_id is None
        assert result.status == TickResultStatus.ACTION_FAILED
        assert result.message.startswith("Unhandled KeyError:")
        assert "malformed marker" in result.message

    def test_tick_exception_includes_type_and_traceback(self, tmp_path):
        """The exception type should be in the message and traceback in stderr."""
        from unittest.mock import patch

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "targets": {"type": "static", "items": ["repo1"]},
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

        def raising_tick_single(target, target_config, dry_run, verbose):
            raise ValueError("unknown trigger type")

        with patch.object(pipeline, "_tick_single", raising_tick_single):
            result = pipeline.tick(target="repo1")

        assert result.status == TickResultStatus.ACTION_FAILED
        assert result.message == "Unhandled ValueError: unknown trigger type"
        assert "ValueError" in result.stderr
        assert "unknown trigger type" in result.stderr
        assert "raising_tick_single" in result.stderr
        assert "Traceback" in result.stderr

    def test_tick_exception_with_empty_str(self, tmp_path):
        """Exceptions with empty str(e) should still produce a useful message."""
        from unittest.mock import patch

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "targets": {"type": "static", "items": ["repo1"]},
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

        def raising_tick_single(target, target_config, dry_run, verbose):
            raise ValueError()  # str(e) is empty

        with patch.object(pipeline, "_tick_single", raising_tick_single):
            result = pipeline.tick(target="repo1")

        assert result.status == TickResultStatus.ACTION_FAILED
        assert result.message == "Unhandled ValueError: "
        assert "ValueError" in result.stderr

    def test_tick_exception_message_captured(self, tmp_path):
        """The exception message should be captured in the TickResult."""
        from unittest.mock import patch

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "targets": {"type": "static", "items": ["repo1"]},
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

        def raising_tick_single(target, target_config, dry_run, verbose):
            raise ValueError("unknown trigger type")

        with patch.object(pipeline, "_tick_single", raising_tick_single):
            result = pipeline.tick(target="repo1")

        assert result.status == TickResultStatus.ACTION_FAILED
        assert "unknown trigger type" in result.message

    def test_tick_lock_released_on_exception(self, tmp_path):
        """The lock should be released even when an exception is caught."""
        from unittest.mock import patch

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "targets": {"type": "static", "items": ["repo1"]},
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

        def raising_tick_single(target, target_config, dry_run, verbose):
            raise OSError("filesystem issue")

        with patch.object(pipeline, "_tick_single", raising_tick_single):
            result = pipeline.tick(target="repo1")

        assert result.status == TickResultStatus.ACTION_FAILED
        assert "filesystem issue" in result.message
        assert not pipeline.lock.is_acquired

    def test_tick_without_target_exception_reports_actual_target(self, tmp_path):
        """When no target is requested, the TickResult target should be the failing target."""
        from unittest.mock import patch

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "targets": {"type": "static", "items": ["repo1"]},
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

        def raising_tick_single(target, target_config, dry_run, verbose):
            raise RuntimeError("boom")

        with patch.object(pipeline, "_tick_single", raising_tick_single):
            result = pipeline.tick()

        assert result.status == TickResultStatus.ACTION_FAILED
        assert result.target == "repo1"

    def test_tick_without_target_multi_reports_actual_target(self, tmp_path):
        """When no target is requested and the first target with work raises,
        the TickResult target should be that specific target (not '*')."""
        from unittest.mock import patch

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

        def raising_tick_single(target, target_config, dry_run, verbose):
            raise RuntimeError("boom")

        with patch.object(pipeline, "_tick_single", raising_tick_single):
            result = pipeline.tick()

        assert result.target == "repo1"
        assert result.status == TickResultStatus.ACTION_FAILED

    def test_tick_generic_exception_handler(self, tmp_path):
        """A non-PipelineTickError exception from _tick_inner should fall through
        to the generic handler and report target or '*'."""
        from unittest.mock import patch

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "targets": {"type": "static", "items": ["repo1"]},
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

        def raising_tick_inner(targets, target_config_map, dry_run, verbose):
            raise RuntimeError("unexpected")

        with patch.object(pipeline, "_tick_inner", raising_tick_inner):
            result = pipeline.tick(target="repo1")

        assert result.target == "repo1"
        assert result.status == TickResultStatus.ACTION_FAILED
        assert result.message == "Unhandled RuntimeError: unexpected"


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

        handler = MockQueueHandler()
        register_handler(ActionType.QUEUE_AGENT, handler)

        result = pipeline.tick(target="my-repo")
        # Should clean up stale marker and re-queue
        assert result.status == TickResultStatus.ACTION_EXECUTED
        # New processing marker should have retry_count = 2
        new_data = json.loads((target_dir / ".processing").read_text())
        assert new_data["retry_count"] == 2
        assert handler.check_complete(None, None) is False

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

    def test_stale_requeue_does_not_mutate_marker_spec_content(self, tmp_path):
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
                        "processing": {"type": "json", "name": ".processing", "content": {"initial": "value"}},
                    },
                    "timeout_minutes": 30,
                    "max_retries": 3,
                },
            ],
        })
        original_spec = config.stages[0].markers["processing"]
        assert original_spec.content == {"initial": "value"}

        pipeline = Pipeline(config)
        # Register mock handler
        from cronpypeline.actions import ActionHandler, ActionResult, register_handler
        from cronpypeline.config import ActionType

        class MockQueueHandler(ActionHandler):
            def execute(self, action, context):
                return ActionResult(
                    success=True,
                    stdout="queued",
                    data={"queue_file": "/tmp/queue/abc123.json", "entry_id": "entry-123"},
                )
            def check_complete(self, action, context):
                return False

        handler = MockQueueHandler()
        register_handler(ActionType.QUEUE_AGENT, handler)

        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        new_data = json.loads((target_dir / ".processing").read_text())
        assert new_data["retry_count"] == 2
        assert handler.check_complete(None, None) is False

        assert config.stages[0].markers["processing"].content == {"initial": "value"}

    def _make_stale_stage(self, workspace, target_name="my-repo", on_fail=None):
        target_dir = workspace / target_name
        target_dir.mkdir()

        # Create a stale processing marker
        processing_data = {"retry_count": 1, "timestamp": time.time() - 3600}
        (target_dir / ".processing").write_text(json.dumps(processing_data))
        old_time = time.time() - 3600
        os.utime(target_dir / ".processing", (old_time, old_time))

        stage = {
            "id": "A0",
            "name": "Failing Step",
            "trigger": {"type": "file_missing", "path": "a.md"},
            "action": {"type": "command", "params": {"command": "sh -c 'echo fail >&2; exit 1'"}},
            "markers": {
                "completion": {"type": "file", "name": "a.md"},
                "processing": {"type": "json", "name": ".processing", "content": {}},
            },
            "timeout_minutes": 30,
            "max_retries": 3,
        }
        if on_fail is not None:
            stage["on_fail"] = on_fail

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [stage],
        })
        return target_dir, Pipeline(config)

    def test_stale_requeue_failing_action_returns_action_failed(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        _target_dir, pipeline = self._make_stale_stage(workspace)

        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_FAILED
        assert result.stderr
        assert "fail" in result.stderr

    def test_stale_requeue_failing_action_runs_on_fail(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        target_dir, pipeline = self._make_stale_stage(
            workspace,
            on_fail={"type": "command", "params": {"command": "touch on_fail_marker.txt"}},
        )

        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_FAILED
        assert (target_dir / "on_fail_marker.txt").exists()

    def test_stale_requeue_failing_action_no_on_fail(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        target_dir, pipeline = self._make_stale_stage(workspace)

        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_FAILED
        assert not (target_dir / "on_fail_marker.txt").exists()

    def _make_stale_sync_stage(self, workspace, stage_overrides=None):
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        # Create a stale processing marker
        processing_data = {"retry_count": 1, "timestamp": time.time() - 3600}
        (target_dir / ".processing").write_text(json.dumps(processing_data))
        old_time = time.time() - 3600
        os.utime(target_dir / ".processing", (old_time, old_time))

        stage = {
            "id": "A0",
            "name": "Sync Step",
            "trigger": {"type": "file_missing", "path": "a.md"},
            "action": {"type": "command", "params": {"command": "echo done"}},
            "markers": {
                "completion": {"type": "file", "name": "a.md"},
                "processing": {"type": "json", "name": ".processing", "content": {}},
            },
            "timeout_minutes": 30,
            "max_retries": 3,
        }
        if stage_overrides:
            stage.update(stage_overrides)

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [stage],
        })
        return target_dir, Pipeline(config)

    def test_stale_sync_action_creates_completion_marker(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        target_dir, pipeline = self._make_stale_sync_stage(workspace)

        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert (target_dir / "a.md").exists()

    def test_stale_sync_action_deletes_processing_marker(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        target_dir, pipeline = self._make_stale_sync_stage(workspace)
        assert (target_dir / ".processing").exists()

        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert (target_dir / "a.md").exists()
        assert not (target_dir / ".processing").exists()

        status = pipeline.status(targets=["my-repo"])
        stage_status = status["my-repo"]["A0"]
        assert stage_status["complete"] is True
        assert stage_status["processing"] is False

    def test_stale_sync_action_creates_produces_markers(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        target_dir, pipeline = self._make_stale_sync_stage(
            workspace,
            stage_overrides={
                "action": {
                    "type": "command",
                    "params": {"command": "echo done"},
                    "produces": [{"type": "file", "name": "produced.txt"}],
                },
            },
        )

        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert (target_dir / "produced.txt").exists()

    def test_stale_sync_action_deletes_invalidates_markers(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        target_dir, pipeline = self._make_stale_sync_stage(workspace)
        (target_dir / "rejected-article.md").write_text("stale")

        pipeline.config.stages[0].invalidates = [
            MarkerSpec.from_dict({"type": "file", "name": "rejected-article.md"}),
        ]

        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert not (target_dir / "rejected-article.md").exists()

    def test_stale_sync_action_clears_rejection_marker(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        target_dir, pipeline = self._make_stale_sync_stage(
            workspace,
            stage_overrides={
                "markers": {
                    "completion": {"type": "file", "name": "a.md"},
                    "processing": {"type": "json", "name": ".processing", "content": {}},
                    "rejection": {"type": "json", "name": ".rejection", "content": {}},
                },
            },
        )
        (target_dir / ".rejection").write_text(json.dumps({"rejection_count": 2}))

        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert not (target_dir / ".rejection").exists()

    def test_stale_queue_agent_action_does_not_create_completion(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

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

        from cronpypeline.actions import ActionHandler, ActionResult, register_handler

        class MockQueueHandler(ActionHandler):
            def execute(self, action, context):
                return ActionResult(success=True, stdout="queued")

        register_handler(ActionType.QUEUE_AGENT, MockQueueHandler())

        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert not (target_dir / "a.md").exists()

    def test_stale_async_custom_action_does_not_create_completion(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        processing_data = {"retry_count": 1, "timestamp": time.time() - 3600}
        (target_dir / ".processing").write_text(json.dumps(processing_data))
        old_time = time.time() - 3600
        os.utime(target_dir / ".processing", (old_time, old_time))

        (tmp_path / "async_mod.py").write_text(
            "def my_action(action, context):\n"
            "    return {\"success\": True, \"async\": True}\n"
        )
        sys.path.insert(0, str(tmp_path))
        try:
            config = PipelineConfig.from_dict({
                "name": "test",
                "workspace_dir": str(workspace),
                "stages": [
                    {
                        "id": "A0",
                        "name": "Async Step",
                        "trigger": {"type": "file_missing", "path": "a.md"},
                        "action": {"type": "custom", "params": {"callable": "async_mod.my_action"}},
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

            result = pipeline.tick(target="my-repo")
            assert result.status == TickResultStatus.ACTION_EXECUTED
            assert not (target_dir / "a.md").exists()

            # Processing marker should have retry_count reset to 0 for async custom actions
            proc_data = json.loads((target_dir / ".processing").read_text())
            assert proc_data["retry_count"] == 0
        finally:
            sys.path.remove(str(tmp_path))
            if "async_mod" in sys.modules:
                del sys.modules["async_mod"]

    def _make_stale_sync_stage_with_chain(self, workspace, second_stage, stage_overrides=None):
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        # Create a stale processing marker
        processing_data = {"retry_count": 1, "timestamp": time.time() - 3600}
        (target_dir / ".processing").write_text(json.dumps(processing_data))
        old_time = time.time() - 3600
        os.utime(target_dir / ".processing", (old_time, old_time))

        stage = {
            "id": "A0",
            "name": "Sync Step",
            "trigger": {"type": "file_missing", "path": "a.md"},
            "action": {"type": "command", "params": {"command": "echo done"}},
            "markers": {
                "completion": {"type": "file", "name": "a.md"},
                "processing": {"type": "json", "name": ".processing", "content": {}},
            },
            "timeout_minutes": 30,
            "max_retries": 3,
            "chain": True,
        }
        if stage_overrides:
            stage.update(stage_overrides)

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [stage, second_stage],
        })
        return target_dir, Pipeline(config)

    def test_stale_sync_chain_continues_to_next_stage(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        second_stage = {
            "id": "A1",
            "name": "Step 2",
            "trigger": {"type": "file_missing", "path": "b.md"},
            "action": {"type": "command", "params": {"command": "echo b"}},
            "markers": {"completion": {"type": "file", "name": "b.md"}},
            "chain": False,
        }

        target_dir, pipeline = self._make_stale_sync_stage_with_chain(workspace, second_stage)

        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert (target_dir / "a.md").exists()
        assert (target_dir / "b.md").exists()
        assert result.stage_id == "A1"
        assert result.chained_stages == ["A1"]

    def test_stale_sync_chain_failure_returns_action_failed(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        second_stage = {
            "id": "A1",
            "name": "Failing Step 2",
            "trigger": {"type": "file_missing", "path": "b.md"},
            "action": {"type": "command", "params": {"command": "false"}},
            "markers": {"completion": {"type": "file", "name": "b.md"}},
            "chain": False,
        }

        target_dir, pipeline = self._make_stale_sync_stage_with_chain(workspace, second_stage)

        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_FAILED
        assert result.stage_id == "A1"
        assert result.failed_chained_stages == ["A1"]
        assert (target_dir / "a.md").exists()
        assert not (target_dir / "b.md").exists()

    def test_stale_sync_no_chain_does_not_chain(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        second_stage = {
            "id": "A1",
            "name": "Step 2",
            "trigger": {"type": "file_missing", "path": "b.md"},
            "action": {"type": "command", "params": {"command": "echo b"}},
            "markers": {"completion": {"type": "file", "name": "b.md"}},
        }

        target_dir, pipeline = self._make_stale_sync_stage_with_chain(
            workspace,
            second_stage,
            stage_overrides={"chain": False},
        )

        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert (target_dir / "a.md").exists()
        assert not (target_dir / "b.md").exists()
        assert result.stage_id == "A0"
        assert result.chained_stages == []

    def test_stale_queue_agent_does_not_chain(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        second_stage = {
            "id": "A1",
            "name": "Step 2",
            "trigger": {"type": "file_missing", "path": "b.md"},
            "action": {"type": "command", "params": {"command": "echo b"}},
            "markers": {"completion": {"type": "file", "name": "b.md"}},
        }

        target_dir, pipeline = self._make_stale_sync_stage_with_chain(
            workspace,
            second_stage,
            stage_overrides={
                "action": {"type": "queue_agent", "params": {"agent": "TestAgent"}},
                "chain": True,
            },
        )

        from cronpypeline.actions import ActionHandler, ActionResult, register_handler

        class MockQueueHandler(ActionHandler):
            def execute(self, action, context):
                return ActionResult(success=True, stdout="queued")

        register_handler(ActionType.QUEUE_AGENT, MockQueueHandler())

        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert not (target_dir / "a.md").exists()
        assert not (target_dir / "b.md").exists()
        assert result.stage_id == "A0"
        assert result.chained_stages == []

    def test_stale_async_custom_action_does_not_chain(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        (tmp_path / "async_mod.py").write_text(
            "def my_action(action, context):\n"
            "    return {\"success\": True, \"async\": True}\n"
        )
        sys.path.insert(0, str(tmp_path))
        try:
            second_stage = {
                "id": "A1",
                "name": "Step 2",
                "trigger": {"type": "file_missing", "path": "b.md"},
                "action": {"type": "command", "params": {"command": "echo b"}},
                "markers": {"completion": {"type": "file", "name": "b.md"}},
            }

            target_dir, pipeline = self._make_stale_sync_stage_with_chain(
                workspace,
                second_stage,
                stage_overrides={
                    "action": {"type": "custom", "params": {"callable": "async_mod.my_action"}},
                    "chain": True,
                },
            )

            result = pipeline.tick(target="my-repo")
            assert result.status == TickResultStatus.ACTION_EXECUTED
            assert not (target_dir / "a.md").exists()
            assert not (target_dir / "b.md").exists()
            assert result.stage_id == "A0"
            assert result.chained_stages == []
        finally:
            sys.path.remove(str(tmp_path))
            if "async_mod" in sys.modules:
                del sys.modules["async_mod"]


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

        # Exercise the mock handler's execute/check_complete branches directly
        assert original.execute(None, None).success is True
        assert original.check_complete(None, None) is True

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

    def test_rejection_cleared_on_success(self, tmp_path):
        """Rejection marker should be cleared when the stage succeeds."""
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
        # Stage should execute (re-processed) and succeed
        assert result.status == TickResultStatus.ACTION_EXECUTED
        # Rejection marker should be cleared on success
        rej_path = workspace / "my-repo" / ".rejection"
        assert not rej_path.exists()
        # Completion marker should exist
        assert (workspace / "my-repo" / "done.md").exists()

    def test_chained_stage_rejection_cleared_on_success(self, tmp_path):
        """A chained stage's rejection marker should be cleared when it succeeds."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        import json as _json
        (target_dir / ".rejection_b").write_text(_json.dumps({"rejection_count": 1}))

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
                    "markers": {
                        "completion": {"type": "file", "name": "b.md"},
                        "rejection": {"type": "json", "name": ".rejection_b", "content": {}},
                    },
                    "max_rejections": 5,
                    "chain": False,
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        # Both stages completed in one tick via chaining
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert (target_dir / "a.md").exists()
        assert (target_dir / "b.md").exists()
        # Chained stage's rejection marker should be cleared on success
        assert not (target_dir / ".rejection_b").exists()

    def test_rejection_count_accumulates_to_give_up(self, tmp_path):
        """Rejection count should accumulate across rejection cycles until the stage gives up."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        import json as _json
        rej_path = workspace / "my-repo" / ".rejection"
        done_path = workspace / "my-repo" / "done.md"
        rej_path.write_text(_json.dumps({"rejection_count": 1}))

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

        # Tick 1: count 1 -> stage re-processed and succeeds, rejection marker cleared
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        # Rejection marker should be cleared on success
        assert not rej_path.exists()

        # Simulate a new rejection written by the agent (clear completion, rewrite count)
        done_path.unlink()
        rej_path.write_text(_json.dumps({"rejection_count": 2}))

        # Tick 2: count 2 -> stage re-processed and succeeds, rejection marker cleared
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert not rej_path.exists()

        # Simulate a new rejection written by the agent (clear completion, rewrite count)
        done_path.unlink()
        rej_path.write_text(_json.dumps({"rejection_count": 3}))

        # Tick 3: count 3 >= max 3 -> give up
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.GAVE_UP
        assert (workspace / "my-repo" / ".gave_up").exists()

    def test_file_rejection_marker_deleted_below_max(self, tmp_path):
        """FILE-type rejection markers can't hold a count; below max they are deleted for re-processing."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        rej_path = workspace / "my-repo" / ".rejection"
        rej_path.touch()

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
                        "rejection": {"type": "file", "name": ".rejection"},
                    },
                    "max_rejections": 3,
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert not rej_path.exists()

    def test_queue_agent_rejection_marker_not_cleared_on_queue(self, tmp_path):
        """A QUEUE_AGENT stage's rejection marker must not be cleared on queue."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        import json as _json
        rej_path = target_dir / ".rejection"
        rej_path.write_text(_json.dumps({"rejection_count": 1}))

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Review",
                    "trigger": {"type": "file_missing", "path": "done.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "ReviewAgent", "prompt": "Review"}},
                    "markers": {
                        "completion": {"type": "file", "name": "done.md"},
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                        "rejection": {"type": "json", "name": ".rejection", "content": {}},
                        "give_up": {"type": "file", "name": ".gave_up"},
                    },
                    "max_rejections": 5,
                },
            ],
        })
        pipeline = Pipeline(config)

        from cronpypeline.actions import ActionHandler, ActionResult, register_handler
        from cronpypeline.config import ActionType

        class MockQueueHandler(ActionHandler):
            def execute(self, action, context):
                return ActionResult(success=True, stdout="queued")
            def check_complete(self, action, context):
                return False

        handler = MockQueueHandler()
        register_handler(ActionType.QUEUE_AGENT, handler)

        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert handler.check_complete(None, None) is False
        assert rej_path.exists()
        assert not (target_dir / "done.md").exists()
        assert not (target_dir / ".gave_up").exists()
        assert (target_dir / ".processing").exists()

    def test_queue_agent_rejection_count_accumulates_to_give_up(self, tmp_path):
        """For a QUEUE_AGENT stage, rejection count should accumulate across
        rejection cycles (marker is never cleared on queue) until give-up."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        import json as _json
        rej_path = target_dir / ".rejection"
        rej_path.write_text(_json.dumps({"rejection_count": 1}))

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Review",
                    "trigger": {"type": "file_missing", "path": "done.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "ReviewAgent", "prompt": "Review"}},
                    "markers": {
                        "completion": {"type": "file", "name": "done.md"},
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                        "rejection": {"type": "json", "name": ".rejection", "content": {}},
                        "give_up": {"type": "file", "name": ".gave_up"},
                    },
                    "max_rejections": 3,
                },
            ],
        })
        pipeline = Pipeline(config)

        from cronpypeline.actions import ActionHandler, ActionResult, register_handler
        from cronpypeline.config import ActionType

        class MockQueueHandler(ActionHandler):
            def execute(self, action, context):
                return ActionResult(success=True, stdout="queued")
            def check_complete(self, action, context):
                return False

        handler = MockQueueHandler()
        register_handler(ActionType.QUEUE_AGENT, handler)

        # Tick 1: count 1 -> below max, incremented to 2, agent re-queued.
        # Rejection marker is NOT cleared.
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert handler.check_complete(None, None) is False
        assert rej_path.exists()
        assert _json.loads(rej_path.read_text())["rejection_count"] == 2

        # Simulate agent finishing its run (and rejecting again).
        (target_dir / ".processing").unlink()

        # Tick 2: count 2 -> below max, incremented to 3, agent re-queued.
        # Rejection marker is still NOT cleared.
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert rej_path.exists()
        assert _json.loads(rej_path.read_text())["rejection_count"] == 3

        # Simulate agent finishing its run (and rejecting again).
        (target_dir / ".processing").unlink()

        # Tick 3: count 3 >= max 3 -> give up.
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.GAVE_UP
        assert (target_dir / ".gave_up").exists()
        assert not rej_path.exists()

    def test_async_custom_action_rejection_marker_not_cleared_on_queue(self, tmp_path):
        """An async custom action's rejection marker must not be cleared on queue."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        import json as _json
        rej_path = target_dir / ".rejection"
        rej_path.write_text(_json.dumps({"rejection_count": 1}))

        (tmp_path / "async_rejection_mod.py").write_text(
            "from cronpypeline.actions import ActionResult\n"
            "def my_action(action, context):\n"
            "    return ActionResult(success=True, data={'async': True})\n"
        )
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            config = PipelineConfig.from_dict({
                "name": "test",
                "workspace_dir": str(workspace),
                "stages": [
                    {
                        "id": "A0",
                        "name": "Async Custom Step",
                        "trigger": {"type": "file_missing", "path": "done.md"},
                        "action": {"type": "custom", "params": {"callable": "async_rejection_mod.my_action"}},
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
            assert result.status == TickResultStatus.ACTION_EXECUTED
            assert rej_path.exists()
            assert not (target_dir / "done.md").exists()
        finally:
            sys.path.remove(str(tmp_path))
            if "async_rejection_mod" in sys.modules:
                del sys.modules["async_rejection_mod"]

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

    def test_rejection_marker_ignored_when_tracking_disabled(self, tmp_path):
        """Stage with max_rejections=0 should still execute even with a rejection marker present."""
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
                    "max_rejections": 0,
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert (workspace / "my-repo" / "done.md").exists()

    def test_rejection_count_not_incremented_when_trigger_does_not_fire(self, tmp_path):
        """Rejection count should NOT increment when the stage's trigger doesn't fire."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        import json as _json
        rej_path = target_dir / ".rejection"
        done_path = target_dir / "done.md"
        # Rejection marker with count 1
        rej_path.write_text(_json.dumps({"rejection_count": 1}))
        # Completion file exists, so trigger `file_missing done.md` does NOT fire
        done_path.touch()

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

        # Run two ticks; both should find no work since the trigger doesn't fire
        result1 = pipeline.tick(target="my-repo")
        result2 = pipeline.tick(target="my-repo")

        assert result1.status == TickResultStatus.NO_WORK
        assert result2.status == TickResultStatus.NO_WORK

        # Rejection count must remain unchanged
        rej_data = _json.loads(rej_path.read_text())
        assert rej_data["rejection_count"] == 1

        # Give-up marker must NOT have been created
        assert not (target_dir / ".gave_up").exists()


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

    def test_target_lock_orphaned_processing_marker_cleaned_up(self, tmp_path):
        """When a queue_agent stage completes externally, the orphaned processing
        marker should be cleaned up so target_lock doesn't block downstream stages."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        # A0 is complete (a.md exists) BUT processing marker is still on disk
        # (agent finished and created completion, pipeline hadn't cleaned up yet)
        (workspace / "my-repo" / "a.md").touch()
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
        # Orphaned processing marker should be cleaned up, B0 should proceed
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert (workspace / "my-repo" / "b.md").exists()
        assert not (workspace / "my-repo" / ".processing_a").exists()


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
        """Without mode_file configured, stages with modes restrictions are skipped."""
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
        assert result.status == TickResultStatus.NO_WORK

    def test_mode_file_missing_treats_as_no_mode(self, tmp_path):
        """Missing mode_file means no mode is set, so modes-restricted stages are skipped."""
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
        assert result.status == TickResultStatus.NO_WORK

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

    def test_chain_failure_result_str(self):
        r = TickResult(
            target="repo",
            stage_id="A1",
            status=TickResultStatus.ACTION_FAILED,
            message="Chained stage A1 failed",
            failed_chained_stages=["A1"],
        )
        s = str(r)
        assert "repo" in s
        assert "A1" in s
        assert "action_failed" in s


class TestTickLockFailures:
    """Tests for lock acquisition failures."""

    def test_tick_lock_failed(self, tmp_path):
        """When lock can't be acquired, tick should return LOCK_FAILED."""
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
                },
            ],
        })
        pipeline = Pipeline(config)
        # Acquire lock with another FileLock to block
        from cronpypeline.lock import FileLock
        other_lock = FileLock(workspace / "pipeline.lock")
        other_lock.acquire()

        try:
            result = pipeline.tick(target="my-repo")
            assert result.status == TickResultStatus.LOCK_FAILED
        finally:
            other_lock.release()

    def test_tick_all_lock_failed(self, tmp_path):
        """When lock can't be acquired, tick_all should return LOCK_FAILED."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "targets": {"type": "static", "items": ["my-repo"]},
            "stages": [
                {
                    "id": "A0",
                    "name": "Step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        from cronpypeline.lock import FileLock
        other_lock = FileLock(workspace / "pipeline.lock")
        other_lock.acquire()

        try:
            results = pipeline.tick_all()
            assert len(results) == 1
            assert results[0].status == TickResultStatus.LOCK_FAILED
        finally:
            other_lock.release()


class TestTickConfigFileDisabled:
    """Tests for config_file enabled toggle."""

    def test_config_file_disables_pipeline(self, tmp_path):
        """When config_file has enabled=False, tick should return DISABLED."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        config_file = tmp_path / "toggle.json"
        config_file.write_text(json.dumps({"enabled": False}))

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "config_file": str(config_file),
            "targets": {"type": "static", "items": ["my-repo"]},
            "stages": [
                {
                    "id": "A0",
                    "name": "Step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick()
        assert result.status == TickResultStatus.DISABLED

    def test_config_file_invalid_json_treated_as_enabled(self, tmp_path):
        """Invalid JSON in config_file should be treated as enabled."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        config_file = tmp_path / "toggle.json"
        config_file.write_text("{invalid json")

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "config_file": str(config_file),
            "targets": {"type": "static", "items": ["my-repo"]},
            "stages": [
                {
                    "id": "A0",
                    "name": "Step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick()
        # Should not be DISABLED — should proceed normally
        assert result.status != TickResultStatus.DISABLED

    def test_config_file_os_error_treated_as_enabled(self, tmp_path):
        """OSError reading config_file should be treated as enabled."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        # Point to a path that will cause OSError (use a directory)
        config_file = tmp_path / "toggle_dir"
        config_file.mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "config_file": str(config_file),
            "targets": {"type": "static", "items": ["my-repo"]},
            "stages": [
                {
                    "id": "A0",
                    "name": "Step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick()
        assert result.status != TickResultStatus.DISABLED


class TestTickNoTargets:
    """Tests for tick with no targets."""

    def test_tick_no_targets_returns_no_work(self, tmp_path):
        """When no targets are configured, tick should return NO_WORK."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "targets": {"type": "static", "items": []},
            "stages": [
                {
                    "id": "A0",
                    "name": "Step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick()
        assert result.status == TickResultStatus.NO_WORK


class TestTickModeFiltering:
    """Tests for mode-based stage filtering in tick."""

    def test_stage_filtered_by_mode(self, tmp_path):
        """Stages not in current mode should be filtered out."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        mode_file = tmp_path / "mode.json"
        mode_file.write_text(json.dumps({"mode": "github"}))

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "mode_file": str(mode_file),
            "targets": {"type": "static", "items": ["my-repo"]},
            "stages": [
                {
                    "id": "A0",
                    "name": "GitHub Step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                    "modes": ["github"],
                },
                {
                    "id": "A1",
                    "name": "Default Step",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                    "modes": ["default"],
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo", dry_run=True)
        # A0 should be actionable (in github mode), A1 should be filtered out
        assert result.status == TickResultStatus.DRY_RUN
        assert result.stage_id == "A0"

    def test_mode_file_invalid_json_returns_none(self, tmp_path):
        """Invalid JSON in mode_file results in None mode, so modes-restricted stages are skipped."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        mode_file = tmp_path / "mode.json"
        mode_file.write_text("{invalid json")

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "mode_file": str(mode_file),
            "targets": {"type": "static", "items": ["my-repo"]},
            "stages": [
                {
                    "id": "A0",
                    "name": "Step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                    "modes": ["github"],
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo", dry_run=True)
        # With None mode, the stage's modes restriction is not met, so A0 is skipped
        assert result.status == TickResultStatus.NO_WORK


class TestTickTargetStateNone:
    """Tests for target_state being None."""

    def test_target_state_none_returns_no_work(self, tmp_path):
        """When target_state is None, should return NO_WORK."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        # Don't create the target directory — it will be created by _tick_single_inner
        # but PipelineState.derive won't have any state for it if all stages are disabled

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "targets": {"type": "static", "items": ["my-repo"]},
            "stages": [
                {
                    "id": "A0",
                    "name": "Disabled",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                    "enabled": False,
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        # With all stages disabled, target_state should be None
        assert result.status == TickResultStatus.NO_WORK


class TestTickProducesMarkers:
    """Tests for action.produces markers."""

    def test_produces_markers_created_on_success(self, tmp_path):
        """Markers in action.produces should be created after successful action."""
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
                    "action": {
                        "type": "command",
                        "params": {"command": "echo hi"},
                        "produces": [
                            {"type": "file", "name": "produced.txt"},
                        ],
                    },
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert (workspace / "my-repo" / "produced.txt").exists()


class TestTickChainEdgeCases:
    """Tests for chaining edge cases."""

    def test_chain_skips_disabled_stage(self, tmp_path):
        """Chaining should skip disabled stages."""
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
                    "name": "Disabled Step",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "echo b"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                    "enabled": False,
                },
                {
                    "id": "A2",
                    "name": "Step 3",
                    "trigger": {"type": "file_missing", "path": "c.md"},
                    "action": {"type": "command", "params": {"command": "echo c"}},
                    "markers": {"completion": {"type": "file", "name": "c.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        # A1 should be skipped, A2 should be chained
        assert "A2" in result.chained_stages

    def test_chain_breaks_on_trigger_failure(self, tmp_path):
        """Chaining should break when next stage trigger doesn't fire."""
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
                    "trigger": {"type": "file_exists", "path": "nonexistent.md"},
                    "action": {"type": "command", "params": {"command": "echo b"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert result.chained_stages == []

    def test_chain_breaks_on_queue_agent(self, tmp_path):
        """Chaining should break when next stage is queue_agent."""
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
                    "name": "Queue Step",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "test"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert result.chained_stages == []

    def test_chain_breaks_on_action_failure(self, tmp_path):
        """Chaining should break when chained action fails."""
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
                    "name": "Failing Step",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "false"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        # A0 succeeds, A1 fails — chain breaks
        assert result.status == TickResultStatus.ACTION_FAILED
        assert result.stage_id == "A1"
        assert "A1" in result.failed_chained_stages

    def test_chain_runs_on_fail_for_failing_chained_stage(self, tmp_path):
        """When a chained stage's action fails, its on_fail action should run."""
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
                    "name": "Failing Step",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "false"}},
                    "on_fail": {"type": "command", "params": {"command": "touch on_fail_marker.txt"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        # A0 succeeds, A1 fails — chain breaks, but on_fail should run
        assert result.status == TickResultStatus.ACTION_FAILED
        assert result.stage_id == "A1"
        assert "A1" in result.failed_chained_stages
        assert (target_dir / "on_fail_marker.txt").exists()

    def test_chain_surfaces_on_fail_failure_for_failing_chained_stage(self, tmp_path):
        """When a chained stage's action fails AND its on_fail action also fails,
        the on_fail failure should be surfaced in the TickResult message."""
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
                    "name": "Failing Step",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "false"}},
                    "on_fail": {"type": "command", "params": {"command": "sh -c 'echo on_fail_failed >&2 && false'"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        # A0 succeeds, A1 fails, and A1's on_fail also fails
        assert result.status == TickResultStatus.ACTION_FAILED
        assert result.stage_id == "A1"
        assert "A1" in result.failed_chained_stages
        # The on_fail failure should be surfaced in the message
        assert "[on_fail]" in result.message
        assert "on_fail_failed" in result.message

    def test_chain_failure_reports_failed_stage(self, tmp_path):
        """When a chained stage fails, the TickResult reports the failing stage."""
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
                    "name": "Failing Step",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "false"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_FAILED
        assert result.stage_id == "A1"
        assert "A1" in result.failed_chained_stages
        assert "A1" in result.message

    def test_chain_failure_with_prior_chained_stages(self, tmp_path):
        """A0 chains into A1 (success), A1 chains into A2 (failure)."""
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
                    "chain": True,
                },
                {
                    "id": "A2",
                    "name": "Failing Step",
                    "trigger": {"type": "file_missing", "path": "c.md"},
                    "action": {"type": "command", "params": {"command": "false"}},
                    "markers": {"completion": {"type": "file", "name": "c.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_FAILED
        assert result.stage_id == "A2"
        assert "A1" in result.chained_stages
        assert "A2" in result.failed_chained_stages

    def test_chain_does_not_run_on_fail_for_successful_chained_stage(self, tmp_path):
        """When a chained stage succeeds, its on_fail action should NOT run."""
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
                    "name": "Success Step",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "echo b"}},
                    "on_fail": {"type": "command", "params": {"command": "touch on_fail_marker.txt"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert "A1" in result.chained_stages
        assert (target_dir / "b.md").exists()
        assert not (target_dir / "on_fail_marker.txt").exists()

    def test_chain_with_produces_and_invalidates(self, tmp_path):
        """Chained stage should create produced markers and invalidate others."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()
        # Create a marker to be invalidated
        (target_dir / "old.md").touch()

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
                    "action": {
                        "type": "command",
                        "params": {"command": "echo b"},
                        "produces": [{"type": "file", "name": "chained_produced.txt"}],
                    },
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                    "invalidates": [{"type": "file", "name": "old.md"}],
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert "A1" in result.chained_stages
        assert (target_dir / "chained_produced.txt").exists()
        assert not (target_dir / "old.md").exists()

    def test_chain_skips_completed_stage_with_custom_trigger(self, tmp_path):
        """Chaining should skip a completed stage even when its custom trigger
        would fire (e.g. C-review-ranking with a custom callable that doesn't
        check its own completion marker)."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        # Pre-create the completion marker for B0 so it's already complete
        (target_dir / "b_done.md").write_text("done", encoding="utf-8")

        import sys
        sys.path.insert(0, str(tmp_path))
        (tmp_path / "chain_skip_completed_mod.py").write_text(
            "call_count = 0\n"
            "from cronpypeline.actions import ActionResult\n"
            "def my_trigger(ctx):\n"
            "    return True\n"
            "def my_action(action, context):\n"
            "    global call_count\n"
            "    call_count += 1\n"
            "    return ActionResult(success=True)\n",
            encoding="utf-8",
        )
        try:
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
                        "id": "B0",
                        "name": "Already Completed Custom Stage",
                        "trigger": {"type": "custom", "callable": "chain_skip_completed_mod.my_trigger"},
                        "action": {"type": "custom", "params": {"callable": "chain_skip_completed_mod.my_action"}},
                        "markers": {"completion": {"type": "file", "name": "b_done.md"}},
                    },
                    {
                        "id": "A1",
                        "name": "Step 3",
                        "trigger": {"type": "file_missing", "path": "c.md"},
                        "action": {"type": "command", "params": {"command": "echo c"}},
                        "markers": {"completion": {"type": "file", "name": "c.md"}},
                        "chain": False,
                    },
                ],
            })
            pipeline = Pipeline(config)
            result = pipeline.tick(target="my-repo")

            assert result.status == TickResultStatus.ACTION_EXECUTED
            assert (target_dir / "a.md").exists()
            assert (target_dir / "c.md").exists()
            # B0 should NOT have been re-executed during chaining
            import chain_skip_completed_mod
            assert chain_skip_completed_mod.call_count == 0
            assert "B0" not in result.chained_stages
        finally:
            sys.path.remove(str(tmp_path))
            if "chain_skip_completed_mod" in sys.modules:
                del sys.modules["chain_skip_completed_mod"]

    def test_chain_no_chained_returns_normal_result(self, tmp_path):
        """When chain is enabled but no stages chain, should return normal result."""
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
                    "trigger": {"type": "file_exists", "path": "nonexistent.md"},
                    "action": {"type": "command", "params": {"command": "echo b"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        # A0 succeeds, A1 trigger doesn't fire, no chaining
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert result.stage_id == "A0"
        assert result.chained_stages == []

    def test_chain_failure_message_no_trailing_colon_when_no_output(self, tmp_path):
        """Chained failure message should have no trailing colon when no output."""
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
                    "name": "Failing Step",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "false"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_FAILED
        assert result.stage_id == "A1"
        assert result.message == "Chained stage A1 failed"

    def test_chain_failure_message_includes_detail_when_output_present(self, tmp_path):
        """Chained failure message should include stderr detail when present."""
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
                    "name": "Failing Step",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "sh -c 'echo error-detail >&2; exit 1'"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_FAILED
        assert result.stage_id == "A1"
        assert result.message.strip() == "Chained stage A1 failed: error-detail"


class TestTickStaleDryRun:
    """Tests for stale processing marker handling in dry run."""

    def test_stale_dry_run_returns_dry_run(self, tmp_path):
        """Stale processing marker in dry run should return DRY_RUN without deleting the marker."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        # Create a stale processing marker
        import json as _json
        proc_path = target_dir / ".processing"
        proc_path.write_text(_json.dumps({"queue_file": "/nonexistent/queue/entry.json"}))

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Agent",
                    "trigger": {"type": "file_missing", "path": "done.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "test"}},
                    "markers": {
                        "completion": {"type": "file", "name": "done.md"},
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                    },
                    "timeout_minutes": 0,
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo", dry_run=True)
        assert result.status == TickResultStatus.DRY_RUN
        assert proc_path.exists(), "Processing marker must not be deleted during dry run"

    def test_stale_dry_run_give_up_no_mutation(self, tmp_path):
        """Dry run with retry_count >= max_retries should return DRY_RUN without mutation."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        # Create a stale processing marker that is already past max_retries
        import json as _json
        proc_path = target_dir / ".processing"
        proc_path.write_text(_json.dumps({
            "queue_file": "/nonexistent/queue/entry.json",
            "retry_count": 3,
        }))
        give_up_path = target_dir / ".give_up"

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Agent",
                    "trigger": {"type": "file_missing", "path": "done.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "test"}},
                    "markers": {
                        "completion": {"type": "file", "name": "done.md"},
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                        "give_up": {"type": "file", "name": ".give_up"},
                    },
                    "timeout_minutes": 0,
                    "max_retries": 3,
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo", dry_run=True)
        assert result.status == TickResultStatus.DRY_RUN
        assert proc_path.exists(), "Processing marker must not be deleted during dry run"
        assert not give_up_path.exists(), "Give up marker must not be created during dry run"

    def test_stale_dry_run_give_up_message(self, tmp_path):
        """Dry run at max_retries should report the give-up message."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        # Create a stale processing marker that is already past max_retries
        import json as _json
        proc_path = target_dir / ".processing"
        proc_path.write_text(_json.dumps({
            "queue_file": "/nonexistent/queue/entry.json",
            "retry_count": 3,
        }))
        give_up_path = target_dir / ".give_up"

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Agent",
                    "trigger": {"type": "file_missing", "path": "done.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "test"}},
                    "markers": {
                        "completion": {"type": "file", "name": "done.md"},
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                        "give_up": {"type": "file", "name": ".give_up"},
                    },
                    "timeout_minutes": 0,
                    "max_retries": 3,
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo", dry_run=True)
        assert result.status == TickResultStatus.DRY_RUN
        assert "Would give up on stale stage" in result.message
        assert "retry 3 >= max 3" in result.message
        assert proc_path.exists(), "Processing marker must not be deleted during dry run"
        assert not give_up_path.exists(), "Give up marker must not be created during dry run"

    def test_stale_dry_run_requeue_message(self, tmp_path):
        """Dry run below max_retries should report the re-queue message."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        # Create a stale processing marker below max_retries
        import json as _json
        proc_path = target_dir / ".processing"
        proc_path.write_text(_json.dumps({
            "queue_file": "/nonexistent/queue/entry.json",
            "retry_count": 1,
        }))
        give_up_path = target_dir / ".give_up"

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Agent",
                    "trigger": {"type": "file_missing", "path": "done.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "test"}},
                    "markers": {
                        "completion": {"type": "file", "name": "done.md"},
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                        "give_up": {"type": "file", "name": ".give_up"},
                    },
                    "timeout_minutes": 0,
                    "max_retries": 3,
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo", dry_run=True)
        assert result.status == TickResultStatus.DRY_RUN
        assert "Would re-queue stale stage" in result.message
        assert "retry 2" in result.message
        assert proc_path.exists(), "Processing marker must not be deleted during dry run"
        assert not give_up_path.exists(), "Give up marker must not be created during dry run"


class TestTickProcessingRetryCount:
    """Tests for processing marker retry_count preservation."""

    def test_processing_marker_preserves_retry_count(self, tmp_path):
        """When re-queueing, retry_count from processing_data should be preserved."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        # Create a stale processing marker with retry_count
        import json as _json
        proc_path = target_dir / ".processing"
        proc_path.write_text(_json.dumps({
            "queue_file": "/nonexistent/queue/entry.json",
            "retry_count": 2,
        }))

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Agent",
                    "trigger": {"type": "file_missing", "path": "done.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "test"}},
                    "markers": {
                        "completion": {"type": "file", "name": "done.md"},
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                    },
                    "timeout_minutes": 0,
                    "max_retries": 5,
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(target="my-repo")
        # Should re-queue with retry_count = 3
        assert result.status == TickResultStatus.ACTION_EXECUTED
        proc_data = _json.loads(proc_path.read_text())
        assert proc_data["retry_count"] == 3


class TestTickInnerMultiTargetModeFiltering:
    """Tests for mode-based stage filtering in _tick_inner with multiple targets."""

    def test_multi_target_disabled_stage_skipped(self, tmp_path):
        """Disabled stages should be skipped in multi-target tick."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo-a").mkdir()
        (workspace / "repo-b").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "targets": {"type": "static", "items": ["repo-a", "repo-b"]},
            "stages": [
                {
                    "id": "A0",
                    "name": "Disabled",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo a"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                    "enabled": False,
                },
                {
                    "id": "A1",
                    "name": "Active",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "echo b"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(dry_run=True)
        # A0 is disabled, A1 should be actionable
        assert result.status == TickResultStatus.DRY_RUN
        assert result.stage_id == "A1"

    def test_multi_target_mode_filtered_stage_skipped(self, tmp_path):
        """Stages not in current mode should be skipped in multi-target tick."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo-a").mkdir()
        (workspace / "repo-b").mkdir()

        mode_file = tmp_path / "mode.json"
        mode_file.write_text(json.dumps({"mode": "github"}))

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "mode_file": str(mode_file),
            "targets": {"type": "static", "items": ["repo-a", "repo-b"]},
            "stages": [
                {
                    "id": "A0",
                    "name": "Default Only",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo a"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                    "modes": ["default"],
                },
                {
                    "id": "A1",
                    "name": "GitHub",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "echo b"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                    "modes": ["github"],
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(dry_run=True)
        # A0 is filtered out (mode=default, current=github), A1 should be actionable
        assert result.status == TickResultStatus.DRY_RUN
        assert result.stage_id == "A1"

    def test_multi_target_no_mode_file_modes_stage_skipped(self, tmp_path):
        """Stages with modes restrictions should be skipped when no mode_file is configured."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo-a").mkdir()
        (workspace / "repo-b").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "targets": {"type": "static", "items": ["repo-a", "repo-b"]},
            "stages": [
                {
                    "id": "A0",
                    "name": "Modes Only",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo a"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                    "modes": ["github"],
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(dry_run=True)
        # A0 has modes restriction but no mode is set, so it should be skipped
        assert result.status == TickResultStatus.NO_WORK

    def test_multi_target_missing_mode_file_modes_stage_skipped(self, tmp_path):
        """Stages with modes restrictions should be skipped when mode_file is missing."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo-a").mkdir()
        (workspace / "repo-b").mkdir()

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "mode_file": str(tmp_path / "nonexistent_mode.json"),
            "targets": {"type": "static", "items": ["repo-a", "repo-b"]},
            "stages": [
                {
                    "id": "A0",
                    "name": "Modes Only",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo a"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                    "modes": ["github"],
                },
            ],
        })
        pipeline = Pipeline(config)
        result = pipeline.tick(dry_run=True)
        # A0 has modes restriction but mode_file is missing (None mode), so it should be skipped
        assert result.status == TickResultStatus.NO_WORK


class TestTickQueueAgentProcessingData:
    """Tests for queue_agent processing_data retry_count preservation."""

    def test_processing_data_retry_count_preserved(self, tmp_path):
        """When stage_state has processing_data with retry_count, it should be preserved."""
        from unittest.mock import patch

        from cronpypeline.actions import ActionHandler, ActionResult, register_handler
        from cronpypeline.state import PipelineState

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
                    "name": "Agent",
                    "trigger": {"type": "file_missing", "path": "done.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "test", "prompt": "do"}},
                    "markers": {
                        "completion": {"type": "file", "name": "done.md"},
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                    },
                },
            ],
        })
        pipeline = Pipeline(config)

        class MockQueueHandler(ActionHandler):
            def execute(self, action, context):
                return ActionResult(success=True, stdout="ok")
            def check_complete(self, action, context):
                return False

        handler = MockQueueHandler()
        register_handler(ActionType.QUEUE_AGENT, handler)

        # Mock PipelineState.derive to inject processing_data while keeping stage actionable
        original_derive = PipelineState.derive

        def mock_derive(self, targets, target_configs=None):
            original_derive(self, targets, target_configs)
            for ts in self.target_states.values():
                for ss in ts.stage_states.values():
                    ss.processing_data = {"retry_count": 3}
                    ss.is_processing = False

        with patch.object(PipelineState, "derive", mock_derive):
            result = pipeline.tick(target="my-repo")

        assert result.status == TickResultStatus.ACTION_EXECUTED
        proc_path = target_dir / ".processing"
        assert proc_path.exists()
        import json as _json
        proc_data = _json.loads(proc_path.read_text())
        assert proc_data["retry_count"] == 3
        assert handler.check_complete(None, None) is False

    def test_processing_data_retry_count_preserved_with_result_data(self, tmp_path):
        """When stage_state has processing_data with retry_count AND result.data is returned,
        retry_count should be preserved (not reset to 0)."""
        from unittest.mock import patch

        from cronpypeline.actions import ActionHandler, ActionResult, register_handler
        from cronpypeline.state import PipelineState

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
                    "name": "Agent",
                    "trigger": {"type": "file_missing", "path": "done.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "test", "prompt": "do"}},
                    "markers": {
                        "completion": {"type": "file", "name": "done.md"},
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                    },
                },
            ],
        })
        pipeline = Pipeline(config)

        class MockQueueHandler(ActionHandler):
            def execute(self, action, context):
                return ActionResult(
                    success=True,
                    stdout="ok",
                    data={"queue_file": "/tmp/queue/abc123.json", "entry_id": "entry-123"},
                )
            def check_complete(self, action, context):
                return False

        handler = MockQueueHandler()
        register_handler(ActionType.QUEUE_AGENT, handler)

        # Mock PipelineState.derive to inject processing_data while keeping stage actionable
        original_derive = PipelineState.derive

        def mock_derive(self, targets, target_configs=None):
            original_derive(self, targets, target_configs)
            for ts in self.target_states.values():
                for ss in ts.stage_states.values():
                    ss.processing_data = {"retry_count": 3}
                    ss.is_processing = False

        with patch.object(PipelineState, "derive", mock_derive):
            result = pipeline.tick(target="my-repo")

        assert result.status == TickResultStatus.ACTION_EXECUTED
        proc_path = target_dir / ".processing"
        assert proc_path.exists()
        import json as _json
        proc_data = _json.loads(proc_path.read_text())
        # retry_count should be preserved from processing_data, not reset to 0
        assert proc_data["retry_count"] == 3
        assert proc_data["queue_file"] == "/tmp/queue/abc123.json"
        assert proc_data["entry_id"] == "entry-123"
        assert handler.check_complete(None, None) is False

    def test_queue_agent_does_not_mutate_marker_spec_content(self, tmp_path):
        """A queue_agent tick must not mutate the config's MarkerSpec.content."""
        from cronpypeline.actions import ActionHandler, ActionResult, register_handler

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
                    "name": "Agent",
                    "trigger": {"type": "file_missing", "path": "done.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "test", "prompt": "do"}},
                    "markers": {
                        "completion": {"type": "file", "name": "done.md"},
                        "processing": {"type": "json", "name": ".processing", "content": {"initial": "value"}},
                    },
                },
            ],
        })
        original_spec = config.stages[0].markers["processing"]
        assert original_spec.content == {"initial": "value"}

        pipeline = Pipeline(config)

        class MockQueueHandler(ActionHandler):
            def execute(self, action, context):
                return ActionResult(
                    success=True,
                    stdout="ok",
                    data={"queue_file": "/tmp/queue/abc123.json", "entry_id": "entry-123"},
                )
            def check_complete(self, action, context):
                return False

        handler = MockQueueHandler()
        register_handler(ActionType.QUEUE_AGENT, handler)

        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        proc_path = target_dir / ".processing"
        assert proc_path.exists()
        proc_data = json.loads(proc_path.read_text())
        assert proc_data["retry_count"] == 0
        assert proc_data["queue_file"] == "/tmp/queue/abc123.json"
        assert proc_data["entry_id"] == "entry-123"
        assert handler.check_complete(None, None) is False

        assert config.stages[0].markers["processing"].content == {"initial": "value"}


class TestModeConfigPathResolution:
    """Tests for mode_file/config_file path resolution relative to workspace_dir."""

    def test_relative_mode_file_resolved_against_workspace_dir(self, tmp_path):
        """A relative mode_file should be resolved relative to workspace_dir."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        (workspace / "mode.json").write_text(json.dumps({"mode": "production"}))

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "mode_file": "mode.json",
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
        assert pipeline.mode_file == workspace / "mode.json"
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert (workspace / "my-repo" / "a.md").exists()

    def test_relative_config_file_resolved_against_workspace_dir(self, tmp_path):
        """A relative config_file should be resolved relative to workspace_dir."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        (workspace / "toggle.json").write_text(json.dumps({"enabled": False}))

        config = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": str(workspace),
            "config_file": "toggle.json",
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
        assert pipeline.config_file == workspace / "toggle.json"
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.DISABLED
        assert not (workspace / "my-repo" / "a.md").exists()

    def test_absolute_mode_file_path_preserved(self, tmp_path):
        """An absolute mode_file should be preserved and still work."""
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
        assert pipeline.mode_file == mode_file
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.ACTION_EXECUTED

    def test_absolute_config_file_path_preserved(self, tmp_path):
        """An absolute config_file should be preserved and still work."""
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
        assert pipeline.config_file == config_toggle
        result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.DISABLED


class TestTickTargetStateNoneDefensive:
    """Test the defensive target_state is None check."""

    def test_target_state_none_returns_no_work(self, tmp_path):
        """When target_state is None (shouldn't normally happen), return NO_WORK."""
        from unittest.mock import patch

        from cronpypeline.state import PipelineState

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
                },
            ],
        })
        pipeline = Pipeline(config)

        # Mock derive to not create target_state for the target
        def mock_derive(self, targets, target_configs=None):
            self.target_states = {}

        with patch.object(PipelineState, "derive", mock_derive):
            result = pipeline.tick(target="my-repo")
        assert result.status == TickResultStatus.NO_WORK
        assert result.message == "No state derived"


class TestCustomAsyncActionCompletionMarker:
    """Completion markers must not be created for async custom actions."""

    def _write_custom_module(self, tmp_path, name, body):
        (tmp_path / f"{name}.py").write_text(body)
        import sys
        sys.path.insert(0, str(tmp_path))
        return sys

    def _cleanup_custom_module(self, sys_mod, tmp_path, name):
        sys_mod.path.remove(str(tmp_path))
        if name in sys_mod.modules:
            del sys_mod.modules[name]

    def test_async_custom_action_does_not_create_completion_marker(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        sys_mod = self._write_custom_module(tmp_path, "async_action_mod", """
from cronpypeline.actions import ActionResult

def my_action(action, context):
    return ActionResult(success=True, data={"async": True, "queue_file": "/tmp/x.json"})
""")
        try:
            config = PipelineConfig.from_dict({
                "name": "test",
                "workspace_dir": str(workspace),
                "stages": [
                    {
                        "id": "A0",
                        "name": "Async Custom Step",
                        "trigger": {"type": "file_missing", "path": "a.md"},
                        "action": {"type": "custom", "params": {"callable": "async_action_mod.my_action"}},
                        "markers": {"completion": {"type": "file", "name": "a.md"}},
                    },
                ],
            })
            pipeline = Pipeline(config)
            result = pipeline.tick(target="my-repo")
            assert result.status == TickResultStatus.ACTION_EXECUTED
            assert not (target_dir / "a.md").exists()
        finally:
            self._cleanup_custom_module(sys_mod, tmp_path, "async_action_mod")

    def test_sync_custom_action_creates_completion_marker(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        sys_mod = self._write_custom_module(tmp_path, "sync_action_mod", """
from cronpypeline.actions import ActionResult

def my_action(action, context):
    return ActionResult(success=True)
""")
        try:
            config = PipelineConfig.from_dict({
                "name": "test",
                "workspace_dir": str(workspace),
                "stages": [
                    {
                        "id": "A0",
                        "name": "Sync Custom Step",
                        "trigger": {"type": "file_missing", "path": "a.md"},
                        "action": {"type": "custom", "params": {"callable": "sync_action_mod.my_action"}},
                        "markers": {"completion": {"type": "file", "name": "a.md"}},
                    },
                ],
            })
            pipeline = Pipeline(config)
            result = pipeline.tick(target="my-repo")
            assert result.status == TickResultStatus.ACTION_EXECUTED
            assert (target_dir / "a.md").exists()
        finally:
            self._cleanup_custom_module(sys_mod, tmp_path, "sync_action_mod")

    def test_chain_async_custom_action_does_not_create_completion_marker(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        sys_mod = self._write_custom_module(tmp_path, "chain_async_mod", """
from cronpypeline.actions import ActionResult

def my_action(action, context):
    return ActionResult(success=True, data={"async": True})
""")
        try:
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
                        "name": "Async Chained Step",
                        "trigger": {"type": "file_missing", "path": "b.md"},
                        "action": {"type": "custom", "params": {"callable": "chain_async_mod.my_action"}},
                        "markers": {"completion": {"type": "file", "name": "b.md"}},
                    },
                ],
            })
            pipeline = Pipeline(config)
            result = pipeline.tick(target="my-repo")
            assert result.status == TickResultStatus.ACTION_EXECUTED
            assert result.stage_id == "A1"
            assert "A1" in result.chained_stages
            assert (target_dir / "a.md").exists()
            assert not (target_dir / "b.md").exists()
        finally:
            self._cleanup_custom_module(sys_mod, tmp_path, "chain_async_mod")

    def test_chain_async_custom_action_creates_processing_marker(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        sys_mod = self._write_custom_module(tmp_path, "chain_async_proc_mod", """
from cronpypeline.actions import ActionResult

def my_action(action, context):
    return ActionResult(success=True, data={"async": True, "queue_file": "/tmp/queue/abc123.json", "entry_id": "entry-123"})
""")
        try:
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
                        "name": "Async Chained Step",
                        "trigger": {"type": "file_missing", "path": "b.md"},
                        "action": {"type": "custom", "params": {"callable": "chain_async_proc_mod.my_action"}},
                        "markers": {
                            "completion": {"type": "file", "name": "b.md"},
                            "processing": {"type": "json", "name": ".processing", "content": {}},
                        },
                    },
                ],
            })
            pipeline = Pipeline(config)
            result = pipeline.tick(target="my-repo")
            assert result.status == TickResultStatus.ACTION_EXECUTED
            assert result.stage_id == "A1"
            assert "A1" in result.chained_stages
            assert not (target_dir / "b.md").exists()
            assert (target_dir / ".processing").exists()
            with open(target_dir / ".processing") as f:
                processing = json.load(f)
            assert processing.get("queue_file") == "/tmp/queue/abc123.json"
            assert processing.get("entry_id") == "entry-123"
            assert processing.get("retry_count") == 0
        finally:
            self._cleanup_custom_module(sys_mod, tmp_path, "chain_async_proc_mod")

    def test_chain_async_custom_action_does_not_mutate_marker_spec_content(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        sys_mod = self._write_custom_module(tmp_path, "chain_async_no_mutate_mod", """
from cronpypeline.actions import ActionResult

def my_action(action, context):
    return ActionResult(success=True, data={"async": True, "queue_file": "/tmp/queue/abc123.json", "entry_id": "entry-123"})
""")
        try:
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
                        "name": "Async Chained Step",
                        "trigger": {"type": "file_missing", "path": "b.md"},
                        "action": {"type": "custom", "params": {"callable": "chain_async_no_mutate_mod.my_action"}},
                        "markers": {
                            "completion": {"type": "file", "name": "b.md"},
                            "processing": {"type": "json", "name": ".processing", "content": {"initial": "value"}},
                        },
                    },
                ],
            })
            original_spec = config.stages[1].markers["processing"]
            assert original_spec.content == {"initial": "value"}

            pipeline = Pipeline(config)
            result = pipeline.tick(target="my-repo")
            assert result.status == TickResultStatus.ACTION_EXECUTED
            assert result.stage_id == "A1"
            assert "A1" in result.chained_stages
            assert not (target_dir / "b.md").exists()
            assert (target_dir / ".processing").exists()
            with open(target_dir / ".processing") as f:
                processing = json.load(f)
            assert processing.get("queue_file") == "/tmp/queue/abc123.json"
            assert processing.get("entry_id") == "entry-123"
            assert processing.get("retry_count") == 0

            assert config.stages[1].markers["processing"].content == {"initial": "value"}
        finally:
            self._cleanup_custom_module(sys_mod, tmp_path, "chain_async_no_mutate_mod")

    def test_chain_async_custom_action_without_processing_marker(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        sys_mod = self._write_custom_module(tmp_path, "chain_async_noproc_mod", """
from cronpypeline.actions import ActionResult

def my_action(action, context):
    return ActionResult(success=True, data={"async": True})
""")
        try:
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
                        "name": "Async Chained Step",
                        "trigger": {"type": "file_missing", "path": "b.md"},
                        "action": {"type": "custom", "params": {"callable": "chain_async_noproc_mod.my_action"}},
                        "markers": {
                            "completion": {"type": "file", "name": "b.md"},
                        },
                    },
                ],
            })
            pipeline = Pipeline(config)
            result = pipeline.tick(target="my-repo")
            assert result.status == TickResultStatus.ACTION_EXECUTED
            assert result.stage_id == "A1"
            assert "A1" in result.chained_stages
            assert not (target_dir / "b.md").exists()
            assert not (target_dir / ".processing").exists()
        finally:
            self._cleanup_custom_module(sys_mod, tmp_path, "chain_async_noproc_mod")

    def test_non_chained_async_custom_action_creates_processing_marker(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        sys_mod = self._write_custom_module(tmp_path, "nonchain_async_proc_mod", """
from cronpypeline.actions import ActionResult

def my_action(action, context):
    return ActionResult(success=True, data={"async": True, "queue_file": "/tmp/queue/abc123.json", "entry_id": "entry-123"})
""")
        try:
            config = PipelineConfig.from_dict({
                "name": "test",
                "workspace_dir": str(workspace),
                "stages": [
                    {
                        "id": "A0",
                        "name": "Async Custom Step",
                        "trigger": {"type": "file_missing", "path": "a.md"},
                        "action": {"type": "custom", "params": {"callable": "nonchain_async_proc_mod.my_action"}},
                        "markers": {
                            "completion": {"type": "file", "name": "a.md"},
                            "processing": {"type": "json", "name": ".processing", "content": {}},
                        },
                    },
                ],
            })
            pipeline = Pipeline(config)
            result = pipeline.tick(target="my-repo")
            assert result.status == TickResultStatus.ACTION_EXECUTED
            assert not (target_dir / "a.md").exists()
            assert (target_dir / ".processing").exists()
            with open(target_dir / ".processing") as f:
                processing = json.load(f)
            assert processing.get("queue_file") == "/tmp/queue/abc123.json"
            assert processing.get("entry_id") == "entry-123"
            assert processing.get("retry_count") == 0
        finally:
            self._cleanup_custom_module(sys_mod, tmp_path, "nonchain_async_proc_mod")

    def test_non_chained_async_custom_action_does_not_mutate_marker_spec_content(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        sys_mod = self._write_custom_module(tmp_path, "nonchain_async_no_mutate_mod", """
from cronpypeline.actions import ActionResult

def my_action(action, context):
    return ActionResult(success=True, data={"async": True, "queue_file": "/tmp/queue/abc123.json", "entry_id": "entry-123"})
""")
        try:
            config = PipelineConfig.from_dict({
                "name": "test",
                "workspace_dir": str(workspace),
                "stages": [
                    {
                        "id": "A0",
                        "name": "Async Custom Step",
                        "trigger": {"type": "file_missing", "path": "a.md"},
                        "action": {"type": "custom", "params": {"callable": "nonchain_async_no_mutate_mod.my_action"}},
                        "markers": {
                            "completion": {"type": "file", "name": "a.md"},
                            "processing": {"type": "json", "name": ".processing", "content": {"initial": "value"}},
                        },
                    },
                ],
            })
            original_spec = config.stages[0].markers["processing"]
            assert original_spec.content == {"initial": "value"}

            pipeline = Pipeline(config)
            result = pipeline.tick(target="my-repo")
            assert result.status == TickResultStatus.ACTION_EXECUTED
            assert not (target_dir / "a.md").exists()
            assert (target_dir / ".processing").exists()
            with open(target_dir / ".processing") as f:
                processing = json.load(f)
            assert processing.get("queue_file") == "/tmp/queue/abc123.json"
            assert processing.get("entry_id") == "entry-123"
            assert processing.get("retry_count") == 0

            assert config.stages[0].markers["processing"].content == {"initial": "value"}
        finally:
            self._cleanup_custom_module(sys_mod, tmp_path, "nonchain_async_no_mutate_mod")

    def test_non_chained_async_custom_action_not_reexecuted(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        sys_mod = self._write_custom_module(tmp_path, "nonchain_async_reexec_mod", """
from cronpypeline.actions import ActionResult

counter = {"runs": 0}

def my_action(action, context):
    counter["runs"] += 1
    return ActionResult(success=True, data={"async": True})
""")
        try:
            config = PipelineConfig.from_dict({
                "name": "test",
                "workspace_dir": str(workspace),
                "stages": [
                    {
                        "id": "A0",
                        "name": "Async Custom Step",
                        "trigger": {"type": "file_missing", "path": "a.md"},
                        "action": {"type": "custom", "params": {"callable": "nonchain_async_reexec_mod.my_action"}},
                        "markers": {
                            "completion": {"type": "file", "name": "a.md"},
                            "processing": {"type": "json", "name": ".processing", "content": {}},
                        },
                    },
                ],
            })
            pipeline = Pipeline(config)
            first = pipeline.tick(target="my-repo")
            assert first.status == TickResultStatus.ACTION_EXECUTED
            second = pipeline.tick(target="my-repo")
            assert second.status == TickResultStatus.NO_WORK
            assert not (target_dir / "a.md").exists()
            assert (target_dir / ".processing").exists()

            import importlib
            mod = importlib.import_module("nonchain_async_reexec_mod")
            assert mod.counter["runs"] == 1
        finally:
            self._cleanup_custom_module(sys_mod, tmp_path, "nonchain_async_reexec_mod")

    def test_chain_async_custom_action_stops_chaining_with_chain_true(self, tmp_path):
        """Async custom action with chain=True stops further chaining."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        sys_mod = self._write_custom_module(tmp_path, "chain_async_stop_mod", """
from cronpypeline.actions import ActionResult

def my_action(action, context):
    return ActionResult(success=True, data={"async": True})
""")
        try:
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
                        "name": "Async Chained Step",
                        "trigger": {"type": "file_missing", "path": "b.md"},
                        "action": {"type": "custom", "params": {"callable": "chain_async_stop_mod.my_action"}},
                        "markers": {"completion": {"type": "file", "name": "b.md"}},
                        "chain": True,
                    },
                    {
                        "id": "A2",
                        "name": "Should Not Run",
                        "trigger": {"type": "file_missing", "path": "c.md"},
                        "action": {"type": "command", "params": {"command": "echo c"}},
                        "markers": {"completion": {"type": "file", "name": "c.md"}},
                    },
                ],
            })
            pipeline = Pipeline(config)
            result = pipeline.tick(target="my-repo")
            assert result.status == TickResultStatus.ACTION_EXECUTED
            assert result.stage_id == "A1"
            assert "A1" in result.chained_stages
            assert "A2" not in result.chained_stages
            assert (target_dir / "a.md").exists()
            assert not (target_dir / "b.md").exists()
            assert not (target_dir / "c.md").exists()
        finally:
            self._cleanup_custom_module(sys_mod, tmp_path, "chain_async_stop_mod")

    def test_initial_async_custom_action_does_not_chain(self, tmp_path):
        """Initial stage returning async=True with chain=True must not chain."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        sys_mod = self._write_custom_module(tmp_path, "initial_async_stop_mod", """
from cronpypeline.actions import ActionResult

def my_action(action, context):
    return ActionResult(success=True, data={"async": True})
""")
        try:
            config = PipelineConfig.from_dict({
                "name": "test",
                "workspace_dir": str(workspace),
                "stages": [
                    {
                        "id": "A0",
                        "name": "Async Initial Step",
                        "trigger": {"type": "file_missing", "path": "a.md"},
                        "action": {"type": "custom", "params": {"callable": "initial_async_stop_mod.my_action"}},
                        "markers": {"completion": {"type": "file", "name": "a.md"}},
                        "chain": True,
                    },
                    {
                        "id": "A1",
                        "name": "Should Not Chain",
                        "trigger": {"type": "file_missing", "path": "b.md"},
                        "action": {"type": "command", "params": {"command": "echo b"}},
                        "markers": {"completion": {"type": "file", "name": "b.md"}},
                    },
                ],
            })
            pipeline = Pipeline(config)
            result = pipeline.tick(target="my-repo")
            assert result.status == TickResultStatus.ACTION_EXECUTED
            assert result.stage_id == "A0"
            assert result.chained_stages == []
            assert not (target_dir / "a.md").exists()
            assert not (target_dir / "b.md").exists()
        finally:
            self._cleanup_custom_module(sys_mod, tmp_path, "initial_async_stop_mod")
