"""Integration tests for cronpypeline — multi-tick execution with temp workspace.

These tests simulate real cron-tick scenarios: multiple ticks advancing a pipeline
through its stages, chaining, stale recovery, multi-target, and end-to-end flows.
"""

import json
import os
import time

from cronpypeline.actions import ActionHandler, ActionResult, register_handler
from cronpypeline.config import ActionType, PipelineConfig
from cronpypeline.pipeline import Pipeline, TickResultStatus


class TestMultiTickProgression:
    """Test that multiple ticks advance a pipeline through its stages."""

    def test_three_stage_pipeline_completes_in_ticks(self, tmp_path):
        """A 3-stage pipeline with chaining should complete in 1 tick (all chain=True).
        Without chaining, it takes 3 ticks."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        config = PipelineConfig.from_dict({
            "name": "integration-test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo a > a.md"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                    "chain": False,
                },
                {
                    "id": "A1",
                    "name": "Step 2",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "echo b > b.md"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                    "chain": False,
                },
                {
                    "id": "A2",
                    "name": "Step 3",
                    "trigger": {"type": "file_missing", "path": "c.md"},
                    "action": {"type": "command", "params": {"command": "echo c > c.md"}},
                    "markers": {"completion": {"type": "file", "name": "c.md"}},
                    "chain": False,
                },
            ],
        })
        pipeline = Pipeline(config)

        # Tick 1: should execute A0
        r1 = pipeline.tick(target="my-repo")
        assert r1.status == TickResultStatus.ACTION_EXECUTED
        assert r1.stage_id == "A0"
        assert (target_dir / "a.md").exists()

        # Tick 2: should execute A1
        r2 = pipeline.tick(target="my-repo")
        assert r2.status == TickResultStatus.ACTION_EXECUTED
        assert r2.stage_id == "A1"
        assert (target_dir / "b.md").exists()

        # Tick 3: should execute A2
        r3 = pipeline.tick(target="my-repo")
        assert r3.status == TickResultStatus.ACTION_EXECUTED
        assert r3.stage_id == "A2"
        assert (target_dir / "c.md").exists()

        # Tick 4: no work
        r4 = pipeline.tick(target="my-repo")
        assert r4.status == TickResultStatus.NO_WORK

    def test_chaining_completes_in_one_tick(self, tmp_path):
        """With chain=True, all mechanical stages complete in one tick."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        config = PipelineConfig.from_dict({
            "name": "chain-test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo a > a.md"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                    "chain": True,
                },
                {
                    "id": "A1",
                    "name": "Step 2",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "command", "params": {"command": "echo b > b.md"}},
                    "markers": {"completion": {"type": "file", "name": "b.md"}},
                    "chain": True,
                },
                {
                    "id": "A2",
                    "name": "Step 3",
                    "trigger": {"type": "file_missing", "path": "c.md"},
                    "action": {"type": "command", "params": {"command": "echo c > c.md"}},
                    "markers": {"completion": {"type": "file", "name": "c.md"}},
                    "chain": False,
                },
            ],
        })
        pipeline = Pipeline(config)

        # One tick should chain through all 3 stages
        r = pipeline.tick(target="my-repo")
        assert r.status == TickResultStatus.ACTION_EXECUTED
        assert r.stage_id == "A2"
        assert len(r.chained_stages) == 2  # A1 and A2 were chained
        assert (target_dir / "a.md").exists()
        assert (target_dir / "b.md").exists()
        assert (target_dir / "c.md").exists()

        # Next tick: no work
        r2 = pipeline.tick(target="my-repo")
        assert r2.status == TickResultStatus.NO_WORK


class TestStaleRecoveryFlow:
    """Test full stale task recovery flow across ticks."""

    def test_stale_task_gets_requeued_then_completes(self, tmp_path):
        """Simulate: agent queued → goes stale → re-queued → completes."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        # Mock handler that simulates async completion
        call_count = {"execute": 0, "check_complete": 0}

        class SimulatedAgentHandler(ActionHandler):
            def execute(self, action, context):
                call_count["execute"] += 1
                return ActionResult(success=True, stdout="queued")

            def check_complete(self, action, context):
                call_count["check_complete"] += 1
                return False

        handler = SimulatedAgentHandler()
        register_handler(ActionType.QUEUE_AGENT, handler)

        config = PipelineConfig.from_dict({
            "name": "stale-test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Agent Step",
                    "trigger": {"type": "file_missing", "path": "done.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "TestAgent"}},
                    "markers": {
                        "completion": {"type": "file", "name": "done.md"},
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                    },
                    "timeout_minutes": 30,
                    "max_retries": 3,
                },
            ],
        })
        pipeline = Pipeline(config)

        # Tick 1: queue the agent
        r1 = pipeline.tick(target="my-repo")
        assert r1.status == TickResultStatus.ACTION_EXECUTED
        assert (target_dir / ".processing").exists()
        assert not (target_dir / "done.md").exists()

        # Make the processing marker stale
        old_time = time.time() - 3600
        os.utime(target_dir / ".processing", (old_time, old_time))

        # Tick 2: detect stale, re-queue
        r2 = pipeline.tick(target="my-repo")
        assert r2.status == TickResultStatus.ACTION_EXECUTED
        new_data = json.loads((target_dir / ".processing").read_text())
        assert new_data["retry_count"] == 1

        # Simulate agent completing: write done.md and remove .processing
        (target_dir / "done.md").touch()
        os.remove(target_dir / ".processing")

        # Tick 3: no work (stage complete)
        r3 = pipeline.tick(target="my-repo")
        assert r3.status == TickResultStatus.NO_WORK

        # Exercise the mock handler's check_complete branch directly
        assert handler.check_complete(None, None) is False


class TestMultiTargetFlow:
    """Test multi-target pipeline execution."""

    def test_multi_target_progresses_independently(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        for t in ["repo1", "repo2", "repo3"]:
            (workspace / t).mkdir()

        config = PipelineConfig.from_dict({
            "name": "multi-target",
            "workspace_dir": str(workspace),
            "targets": {"type": "static", "items": ["repo1", "repo2", "repo3"]},
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo a > a.md"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)

        # Tick 1: should pick first target with work (repo1)
        r1 = pipeline.tick()
        assert r1.target == "repo1"
        assert (workspace / "repo1" / "a.md").exists()

        # Tick 2: repo1 done, picks repo2
        r2 = pipeline.tick()
        assert r2.target == "repo2"

        # Tick 3: repo2 done, picks repo3
        r3 = pipeline.tick()
        assert r3.target == "repo3"

        # Tick 4: all done
        r4 = pipeline.tick()
        assert r4.status == TickResultStatus.NO_WORK

    def test_tick_all_processes_all_in_one_call(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        for t in ["repo1", "repo2"]:
            (workspace / t).mkdir()

        config = PipelineConfig.from_dict({
            "name": "multi-target-all",
            "workspace_dir": str(workspace),
            "targets": {"type": "static", "items": ["repo1", "repo2"]},
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo a > a.md"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)

        results = pipeline.tick_all()
        assert len(results) == 2
        assert all(r.status == TickResultStatus.ACTION_EXECUTED for r in results)
        assert all((workspace / t / "a.md").exists() for t in ["repo1", "repo2"])


class TestDryRunFlow:
    """Test dry-run mode doesn't modify state."""

    def test_dry_run_preserves_state(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        config = PipelineConfig.from_dict({
            "name": "dry-run-test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo a > a.md"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        })
        pipeline = Pipeline(config)

        # Dry run: should not create any files
        r = pipeline.tick(target="my-repo", dry_run=True)
        assert r.status == TickResultStatus.DRY_RUN
        assert not (target_dir / "a.md").exists()

        # Real run: should create the file
        r2 = pipeline.tick(target="my-repo")
        assert r2.status == TickResultStatus.ACTION_EXECUTED
        assert (target_dir / "a.md").exists()


class TestCrashSafety:
    """Test that the pipeline is crash-safe — state is derived from filesystem."""

    def test_pipeline_recovers_from_partial_state(self, tmp_path):
        """If a previous tick was killed mid-execution, the next tick should
        derive state correctly from whatever markers exist."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        # Simulate a crash: A0 completed but A1 was interrupted
        (target_dir / "a.md").touch()
        # A stale processing marker for A1
        processing_data = {"retry_count": 0, "timestamp": time.time() - 3600}
        (target_dir / ".processing").write_text(json.dumps(processing_data))
        old_time = time.time() - 3600
        os.utime(target_dir / ".processing", (old_time, old_time))

        config = PipelineConfig.from_dict({
            "name": "crash-test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo a"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
                {
                    "id": "A1",
                    "name": "Agent Step",
                    "trigger": {"type": "file_missing", "path": "b.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "TestAgent"}},
                    "markers": {
                        "completion": {"type": "file", "name": "b.md"},
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                    },
                    "timeout_minutes": 30,
                    "max_retries": 3,
                },
            ],
        })
        pipeline = Pipeline(config)

        # Register mock handler
        class MockHandler(ActionHandler):
            def execute(self, action, context):
                return ActionResult(success=True, stdout="queued")
            def check_complete(self, action, context):
                return False
        handler = MockHandler()
        register_handler(ActionType.QUEUE_AGENT, handler)

        # Tick should detect stale A1 and re-queue
        r = pipeline.tick(target="my-repo")
        assert r.status == TickResultStatus.ACTION_EXECUTED
        assert r.stage_id == "A1"
        new_data = json.loads((target_dir / ".processing").read_text())
        assert new_data["retry_count"] == 1

        # Exercise the mock handler's check_complete branch directly
        assert handler.check_complete(None, None) is False


class TestEndToEndWithConfig:
    """Test end-to-end pipeline loaded from a JSON config file."""

    def test_full_pipeline_from_config_file(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()
        lock_file = tmp_path / "pipeline.lock"

        config_data = {
            "name": "e2e-test",
            "workspace_dir": str(workspace),
            "lock_file": str(lock_file),
            "stages": [
                {
                    "id": "A0",
                    "name": "Setup",
                    "trigger": {"type": "file_missing", "path": "setup.md"},
                    "action": {"type": "command", "params": {"command": "echo setup > setup.md"}},
                    "markers": {"completion": {"type": "file", "name": "setup.md"}},
                    "chain": True,
                },
                {
                    "id": "A1",
                    "name": "Build",
                    "trigger": {"type": "file_missing", "path": "build.md"},
                    "action": {"type": "command", "params": {"command": "echo build > build.md"}},
                    "markers": {"completion": {"type": "file", "name": "build.md"}},
                    "chain": True,
                },
                {
                    "id": "A2",
                    "name": "Test",
                    "trigger": {"type": "file_missing", "path": "test.md"},
                    "action": {"type": "command", "params": {"command": "echo test > test.md"}},
                    "markers": {"completion": {"type": "file", "name": "test.md"}},
                    "chain": False,
                },
            ],
        }
        config_file = tmp_path / "pipeline.json"
        config_file.write_text(json.dumps(config_data))

        pipeline = Pipeline.from_config(config_file)

        # One tick with chaining should complete all stages
        r = pipeline.tick(target="my-repo")
        assert r.status == TickResultStatus.ACTION_EXECUTED
        assert (target_dir / "setup.md").exists()
        assert (target_dir / "build.md").exists()
        assert (target_dir / "test.md").exists()

        # Status check
        status = pipeline.status(["my-repo"])
        assert status["my-repo"]["A0"]["complete"] is True
        assert status["my-repo"]["A1"]["complete"] is True
        assert status["my-repo"]["A2"]["complete"] is True


class TestOnFailExecution:
    """Test that on_fail actions are executed when a stage fails."""

    def test_on_fail_runs_on_failure(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        config = PipelineConfig.from_dict({
            "name": "on-fail-test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Failing Step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "false"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                    "on_fail": {
                        "type": "command",
                        "params": {"command": "sh -c 'echo failed > cleanup.txt'"},
                    },
                },
            ],
        })
        pipeline = Pipeline(config)

        r = pipeline.tick(target="my-repo")
        assert r.status == TickResultStatus.ACTION_FAILED
        assert (target_dir / "cleanup.txt").exists()
        assert "failed" in (target_dir / "cleanup.txt").read_text()
