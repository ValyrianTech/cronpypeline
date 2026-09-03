"""Tests for the JSONL execution log feature (pipeline.py + actions.py)."""

import json
import os
import time
from pathlib import Path
from unittest import mock

import cronpypeline.pipeline as pipeline_mod
from cronpypeline.actions import TickContext, resolved_command
from cronpypeline.config import (
    ActionSpec,
    ActionType,
    HookConfig,
    PipelineConfig,
    Stage,
    TargetSpec,
)
from cronpypeline.lock import FileLock
from cronpypeline.pipeline import Pipeline, TickResultStatus


def make_config(workspace_dir, stages=None, targets=None, **kwargs):
    """Build a PipelineConfig with Stage objects and optional log_file etc."""
    cfg = PipelineConfig(
        name="test-pipeline",
        workspace_dir=str(workspace_dir),
        stages=list(stages or []),
    )
    if targets:
        cfg.targets = TargetSpec.from_dict(targets)
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return cfg


def make_command_stage(stage_id, name, marker_name, command="echo done", **kwargs):
    """Build a stage with a file_missing trigger and command action."""
    data = {
        "id": stage_id,
        "name": name,
        "trigger": {"type": "file_missing", "path": marker_name},
        "action": {"type": "command", "params": {"command": command}},
        "markers": {"completion": {"type": "file", "name": marker_name}},
        "chain": False,
        "timeout_minutes": 30,
        "max_retries": 3,
    }
    data.update(kwargs)
    return Stage.from_dict(data)


def read_lines(log_path):
    """Parse a JSONL log file into a list of dicts."""
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


class TestLogFileSetup:
    def test_log_file_created_when_ticking(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()
        config = make_config(
            workspace,
            stages=[make_command_stage("A0", "A", "a.md")],
            log_file="execution.log",
        )
        pipeline = Pipeline(config)
        assert pipeline.log_file_path == workspace / "execution.log"
        pipeline.tick(target="repo1")
        assert (workspace / "execution.log").exists()

    def test_no_log_file_when_unset(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()
        config = make_config(workspace, stages=[make_command_stage("A0", "A", "a.md")])
        pipeline = Pipeline(config)
        assert pipeline.log_file_path is None
        pipeline.tick(target="repo1")
        assert list(workspace.glob("*.log")) == []

    def test_log_file_resolved_relative_to_workspace(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = make_config(workspace, log_file="logs/exec.log")
        pipeline = Pipeline(config)
        assert pipeline.log_file_path == workspace / "logs" / "exec.log"
        assert (workspace / "logs").is_dir()

    def test_log_file_absolute_path(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        log_path = tmp_path / "custom" / "exec.log"
        config = make_config(workspace, log_file=str(log_path))
        pipeline = Pipeline(config)
        assert pipeline.log_file_path == log_path
        assert log_path.parent.is_dir()


class TestBasicTickLogging:
    def test_successful_tick_logs_start_stages_end(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()
        config = make_config(
            workspace,
            stages=[make_command_stage("A0", "First", "a.md")],
            log_file="execution.log",
        )
        pipeline = Pipeline(config)
        result = pipeline.tick(target="repo1")
        assert result.status == TickResultStatus.ACTION_EXECUTED

        lines = read_lines(workspace / "execution.log")
        events = [line["event"] for line in lines]
        assert events[0] == "tick_start"
        assert events[-1] == "tick_end"
        assert events.count("stage") >= 2

        tick_start = lines[0]
        assert tick_start["target"] == "repo1"
        assert tick_start["dry_run"] is False
        assert tick_start["tick_id"]
        assert "timestamp" in tick_start

        tick_end = lines[-1]
        assert tick_end["tick_id"] == tick_start["tick_id"]
        assert tick_end["target"] == "repo1"
        assert "total_duration_ms" in tick_end
        assert tick_end["stages_checked"] >= 1
        assert tick_end["actions_executed"] == 1
        assert tick_end["failures"] == 0
        assert tick_end["final_status"] == "action_executed"
        assert tick_end["final_stage_id"] == "A0"

        # The executed stage is logged as trigger_fired then action_executed
        stage_entries = [line for line in lines if line["event"] == "stage"]
        assert stage_entries[0]["result"] == "trigger_fired"
        assert stage_entries[-1]["result"] == "action_executed"
        for entry in stage_entries:
            assert entry["tick_id"] == tick_start["tick_id"]
            assert entry["target"] == "repo1"
            assert entry["stage_id"] == "A0"
            assert entry["stage_name"] == "First"
            assert entry["trigger_type"] == "file_missing"
            assert entry["action_type"] == "command"
            assert entry["dry_run"] is False
            assert "duration_ms" in entry

    def test_stage_entry_results(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        repo = workspace / "repo1"
        repo.mkdir()
        (repo / "done0.md").touch()   # A0 complete
        (repo / "exists1.md").touch()  # A1 trigger won't fire
        config = make_config(
            workspace,
            stages=[
                Stage.from_dict({
                    "id": "A0", "name": "Complete stage",
                    "trigger": {"type": "file_missing", "path": "done0.md"},
                    "action": {"type": "command", "params": {"command": "echo a"}},
                    "markers": {"completion": {"type": "file", "name": "done0.md"}},
                }),
                Stage.from_dict({
                    "id": "A1", "name": "Skipped stage",
                    "trigger": {"type": "file_missing", "path": "exists1.md"},
                    "action": {"type": "command", "params": {"command": "echo b"}},
                }),
                make_command_stage("A2", "Executed stage", "final.md"),
            ],
            log_file="execution.log",
        )
        pipeline = Pipeline(config)
        pipeline.tick(target="repo1")

        lines = read_lines(workspace / "execution.log")
        stage_results = [line["result"] for line in lines if line["event"] == "stage"]
        assert stage_results == ["complete", "skipped", "trigger_fired", "action_executed"]

    def test_all_entries_share_tick_id_and_bracketing(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()
        config = make_config(
            workspace,
            stages=[make_command_stage("A0", "A", "a.md")],
            log_file="execution.log",
        )
        pipeline = Pipeline(config)
        pipeline.tick(target="repo1")

        lines = read_lines(workspace / "execution.log")
        assert lines[0]["event"] == "tick_start"
        assert lines[-1]["event"] == "tick_end"
        tick_id = lines[0]["tick_id"]
        assert tick_id
        for line in lines:
            assert line["tick_id"] == tick_id


class TestDryRunLogging:
    def test_dry_run_tick_logs_dry_run(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()
        config = make_config(
            workspace,
            stages=[make_command_stage("A0", "A", "a.md")],
            log_file="execution.log",
        )
        pipeline = Pipeline(config)
        result = pipeline.tick(target="repo1", dry_run=True)
        assert result.status == TickResultStatus.DRY_RUN

        lines = read_lines(workspace / "execution.log")
        tick_start = lines[0]
        assert tick_start["dry_run"] is True

        stage_entries = [line for line in lines if line["event"] == "stage"]
        assert any(e["result"] == "dry_run" for e in stage_entries)
        dry_run_entry = next(e for e in stage_entries if e["result"] == "dry_run")
        assert dry_run_entry["dry_run"] is True

        tick_end = lines[-1]
        assert tick_end["final_status"] == "dry_run"


class TestActionCapture:
    def test_stdout_stderr_and_command_captured(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()
        command = "sh -c 'echo hello; echo boom >&2'"
        config = make_config(
            workspace,
            stages=[make_command_stage("A0", "A", "a.md", command=command)],
            log_file="execution.log",
        )
        pipeline = Pipeline(config)
        pipeline.tick(target="repo1")

        lines = read_lines(workspace / "execution.log")
        executed = next(
            line for line in lines
            if line["event"] == "stage" and line["result"] == "action_executed"
        )
        assert "hello" in executed["stdout"]
        assert "boom" in executed["stderr"]
        assert executed["action_command"] == command


class TestRotation:
    def _big(self, log_path):
        log_path.write_bytes(b"x" * (10 * 1024 * 1024 + 1))

    def test_no_log_file_rotate_is_noop(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = make_config(workspace)
        pipeline = Pipeline(config)
        pipeline._rotate_log()  # no exception

    def test_under_max_bytes_no_rotation(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = make_config(workspace, log_file="execution.log")
        pipeline = Pipeline(config)
        pipeline.log_file_path.write_text("small")
        pipeline._rotate_log()
        assert pipeline.log_file_path.exists()
        assert not (workspace / "execution.log.1").exists()

    def test_stat_oserror_returns_early(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = make_config(workspace, log_file="execution.log")
        pipeline = Pipeline(config)
        pipeline.log_file_path.write_text("small")
        with mock.patch.object(Path, "stat", side_effect=OSError("boom")):
            pipeline._rotate_log()  # no exception

    def test_rotates_to_dot_one_when_over_max(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = make_config(workspace, log_file="execution.log")
        pipeline = Pipeline(config)
        self._big(pipeline.log_file_path)
        pipeline._rotate_log()
        assert (workspace / "execution.log.1").exists()
        assert not pipeline.log_file_path.exists()

    def test_existing_backups_shift(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = make_config(workspace, log_file="execution.log")
        pipeline = Pipeline(config)
        log = pipeline.log_file_path
        (Path(f"{log}.1")).write_text("one")
        (Path(f"{log}.2")).write_text("two")
        (Path(f"{log}.3")).write_text("three")
        (Path(f"{log}.4")).write_text("four")
        (Path(f"{log}.5")).write_text("five")
        self._big(log)

        pipeline._rotate_log()

        assert (Path(f"{log}.1")).read_text() == "x" * (10 * 1024 * 1024 + 1)
        assert (Path(f"{log}.2")).read_text() == "one"
        assert (Path(f"{log}.3")).read_text() == "two"
        assert (Path(f"{log}.4")).read_text() == "three"
        assert (Path(f"{log}.5")).read_text() == "four"
        assert not (Path(f"{log}.6")).exists()

    def test_oldest_backup_deleted(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = make_config(workspace, log_file="execution.log")
        pipeline = Pipeline(config)
        log = pipeline.log_file_path
        (Path(f"{log}.6")).write_text("six")
        self._big(log)

        pipeline._rotate_log()

        assert not (Path(f"{log}.6")).exists()
        assert (Path(f"{log}.1")).exists()

    def test_shift_rename_oserror_is_swallowed(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = make_config(workspace, log_file="execution.log")
        pipeline = Pipeline(config)
        log = pipeline.log_file_path
        (Path(f"{log}.1")).write_text("one")
        self._big(log)
        with mock.patch.object(Path, "rename", side_effect=OSError("rename fail")):
            pipeline._rotate_log()  # no exception

    def test_oldest_unlink_oserror_is_swallowed(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = make_config(workspace, log_file="execution.log")
        pipeline = Pipeline(config)
        log = pipeline.log_file_path
        (Path(f"{log}.6")).write_text("six")
        self._big(log)
        with mock.patch.object(Path, "unlink", side_effect=OSError("unlink fail")):
            pipeline._rotate_log()  # no exception


class TestLogInternal:
    def test_log_writes_jsonl_with_timestamp(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = make_config(workspace, log_file="execution.log")
        pipeline = Pipeline(config)
        pipeline._log({"event": "custom"})
        lines = read_lines(workspace / "execution.log")
        assert len(lines) == 1
        assert lines[0]["event"] == "custom"
        assert "timestamp" in lines[0]

    def test_log_oserror_is_swallowed(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = make_config(workspace, log_file="execution.log")
        pipeline = Pipeline(config)
        with mock.patch("builtins.open", side_effect=OSError("disk full")):
            pipeline._log({"event": "custom"})  # no exception

    def test_log_tick_end_guard_when_no_current_tick(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = make_config(workspace, log_file="execution.log")
        pipeline = Pipeline(config)
        from cronpypeline.pipeline import TickResult
        result = TickResult(target="x", stage_id=None, status=TickResultStatus.NO_WORK)
        pipeline._log_tick_end(result)  # no exception, returns early
        assert not (workspace / "execution.log").exists()

    def test_log_stage_guard_when_no_current_tick(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = make_config(workspace, log_file="execution.log")
        pipeline = Pipeline(config)
        stage = make_command_stage("A0", "A", "a.md")
        pipeline._log_stage(stage, "complete")  # no exception, returns early
        assert not (workspace / "execution.log").exists()

    def test_log_stage_increments_stages_checked(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = make_config(workspace, log_file="execution.log")
        pipeline = Pipeline(config)
        pipeline._log_tick_start("repo1", False)
        assert pipeline._current_tick_stages_checked == 0
        stage = make_command_stage("A0", "A", "a.md")
        pipeline._log_stage(stage, "skipped")
        pipeline._log_stage(stage, "trigger_fired")
        assert pipeline._current_tick_stages_checked == 1
        stage2 = make_command_stage("A1", "B", "b.md")
        pipeline._log_stage(stage2, "skipped")
        assert pipeline._current_tick_stages_checked == 2


class TestChainedStageLogging:
    def test_chained_stage_logs_chained_true(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()
        config = make_config(
            workspace,
            stages=[
                Stage.from_dict({
                    "id": "A0", "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo a"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                    "chain": True,
                }),
                make_command_stage("A1", "Step 2", "b.md", command="echo b"),
            ],
            log_file="execution.log",
        )
        pipeline = Pipeline(config)
        result = pipeline.tick(target="repo1")
        assert result.stage_id == "A1"

        lines = read_lines(workspace / "execution.log")
        executed = [l for l in lines if l["event"] == "stage" and l["result"] == "action_executed"]
        assert len(executed) == 2
        assert executed[0]["chained"] is False
        assert executed[0]["stage_id"] == "A0"
        assert executed[1]["chained"] is True
        assert executed[1]["stage_id"] == "A1"


class TestStaleLogging:
    def test_stale_requeue_produces_log_entries(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        repo = workspace / "repo1"
        repo.mkdir()

        config = make_config(
            workspace,
            stages=[
                Stage.from_dict({
                    "id": "A0", "name": "Agent Step",
                    "trigger": {"type": "file_missing", "path": "done.md"},
                    "action": {"type": "command", "params": {"command": "echo retry"}},
                    "markers": {
                        "completion": {"type": "file", "name": "done.md"},
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                    },
                    "timeout_minutes": 30,
                    "max_retries": 3,
                }),
            ],
            log_file="execution.log",
        )
        pipeline = Pipeline(config)

        # First tick: execute, creating a processing marker (sync command,
        # so processing is created? No — processing only for queue_agent).
        # Instead simulate an agent-style stale marker manually.
        (repo / ".processing").write_text(json.dumps({"retry_count": 0}))
        old = time.time() - 3600
        os.utime(repo / ".processing", (old, old))

        result = pipeline.tick(target="repo1")
        assert result.status == TickResultStatus.ACTION_EXECUTED

        lines = read_lines(workspace / "execution.log")
        executed = [l for l in lines if l["event"] == "stage" and l["result"] == "action_executed"]
        assert len(executed) == 1
        assert executed[0]["stage_id"] == "A0"

    def test_stale_give_up_produces_log_entries(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        repo = workspace / "repo1"
        repo.mkdir()

        config = make_config(
            workspace,
            stages=[
                Stage.from_dict({
                    "id": "A0", "name": "Agent Step",
                    "trigger": {"type": "file_missing", "path": "done.md"},
                    "action": {"type": "command", "params": {"command": "echo retry"}},
                    "markers": {
                        "completion": {"type": "file", "name": "done.md"},
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                        "give_up": {"type": "file", "name": ".gave_up"},
                    },
                    "timeout_minutes": 30,
                    "max_retries": 0,
                }),
            ],
            log_file="execution.log",
        )
        pipeline = Pipeline(config)

        (repo / ".processing").write_text(json.dumps({"retry_count": 0}))
        old = time.time() - 3600
        os.utime(repo / ".processing", (old, old))

        result = pipeline.tick(target="repo1")
        assert result.status == TickResultStatus.GAVE_UP

        lines = read_lines(workspace / "execution.log")
        stage_results = [l["result"] for l in lines if l["event"] == "stage"]
        assert "gave_up" in stage_results
        tick_end = lines[-1]
        assert tick_end["final_status"] == "gave_up"


class TestStatusTicks:
    def test_lock_failure_logs_lock_failed(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()
        config = make_config(
            workspace,
            stages=[make_command_stage("A0", "A", "a.md")],
            log_file="execution.log",
        )
        pipeline = Pipeline(config)
        other_lock = FileLock(workspace / "pipeline.lock")
        other_lock.acquire()
        try:
            result = pipeline.tick(target="repo1")
        finally:
            other_lock.release()

        assert result.status == TickResultStatus.LOCK_FAILED
        lines = read_lines(workspace / "execution.log")
        assert lines[0]["event"] == "tick_start"
        assert lines[-1]["event"] == "tick_end"
        assert lines[-1]["final_status"] == "lock_failed"

    def test_disabled_tick_logs_disabled(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()
        config_file = tmp_path / "toggle.json"
        config_file.write_text(json.dumps({"enabled": False}))
        config = make_config(
            workspace,
            stages=[make_command_stage("A0", "A", "a.md")],
            config_file=str(config_file),
            targets={"type": "static", "items": ["repo1"]},
            log_file="execution.log",
        )
        pipeline = Pipeline(config)
        result = pipeline.tick()
        assert result.status == TickResultStatus.DISABLED

        lines = read_lines(workspace / "execution.log")
        assert lines[0]["event"] == "tick_start"
        assert lines[-1]["event"] == "tick_end"
        assert lines[-1]["final_status"] == "disabled"

    def test_no_work_tick_logs_no_work(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        repo = workspace / "repo1"
        repo.mkdir()
        (repo / "a.md").touch()
        config = make_config(
            workspace,
            stages=[make_command_stage("A0", "A", "a.md")],
            log_file="execution.log",
        )
        pipeline = Pipeline(config)
        result = pipeline.tick(target="repo1")
        assert result.status == TickResultStatus.NO_WORK

        lines = read_lines(workspace / "execution.log")
        assert lines[0]["event"] == "tick_start"
        assert lines[-1]["event"] == "tick_end"
        assert lines[-1]["final_status"] == "no_work"
        stage_results = [l["result"] for l in lines if l["event"] == "stage"]
        assert stage_results == ["complete"]

    def test_no_targets_tick_logs_no_work(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = make_config(
            workspace,
            stages=[make_command_stage("A0", "A", "a.md")],
            targets={"type": "static", "items": []},
            log_file="execution.log",
        )
        pipeline = Pipeline(config)
        result = pipeline.tick()
        assert result.status == TickResultStatus.NO_WORK

        lines = read_lines(workspace / "execution.log")
        assert lines[0]["target"] == "*"
        assert lines[-1]["final_status"] == "no_work"


class TestNoStateStage:
    def test_no_state_stage_entry(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()
        config = make_config(
            workspace,
            stages=[make_command_stage("A0", "A", "a.md")],
            log_file="execution.log",
        )
        pipeline = Pipeline(config)

        real_state_cls = pipeline_mod.PipelineState

        class _MissingState:
            def __init__(self, workspace_dir, stages, target_lock=False):
                self._real = real_state_cls(
                    workspace_dir=workspace_dir, stages=stages, target_lock=target_lock
                )

            def derive(self, targets, target_configs=None):
                self._real.derive(targets, target_configs)
                for ts in self._real.target_states.values():
                    ts.stage_states = {}

            @property
            def target_states(self):
                return self._real.target_states

        with mock.patch.object(pipeline_mod, "PipelineState", _MissingState):
            result = pipeline.tick(target="repo1")

        assert result.status == TickResultStatus.NO_WORK
        lines = read_lines(workspace / "execution.log")
        stage_results = [l["result"] for l in lines if l["event"] == "stage"]
        assert stage_results == ["no_state"]


class TestResolvedCommand:
    def _ctx(self, tmp_path, target="repo1", target_config=None):
        return TickContext(
            target=target,
            workspace_dir=tmp_path,
            target_config=target_config or {},
        )

    def test_command(self, tmp_path):
        action = ActionSpec(type=ActionType.COMMAND, params={"command": "echo {target}"})
        assert resolved_command(action, self._ctx(tmp_path)) == "echo repo1"

    def test_command_template_failure_returns_raw(self, tmp_path):
        action = ActionSpec(type=ActionType.COMMAND, params={"command": "echo {missing}"})
        assert resolved_command(action, self._ctx(tmp_path)) == "echo {missing}"

    def test_command_uses_target_config(self, tmp_path):
        action = ActionSpec(type=ActionType.COMMAND, params={"command": "echo {slug}"})
        ctx = self._ctx(tmp_path, target_config={"slug": "my-slug"})
        assert resolved_command(action, ctx) == "echo my-slug"

    def test_subprocess(self, tmp_path):
        action = ActionSpec(
            type=ActionType.SUBPROCESS,
            params={"script": "run_{target}.py", "args": ["a", "b"]},
        )
        assert resolved_command(action, self._ctx(tmp_path)) == "run_repo1.py a b"

    def test_http_request(self, tmp_path):
        action = ActionSpec(
            type=ActionType.HTTP_REQUEST,
            params={"url": "https://api.example.com/{target}"},
        )
        assert resolved_command(action, self._ctx(tmp_path)) == "https://api.example.com/repo1"

    def test_custom(self, tmp_path):
        action = ActionSpec(type=ActionType.CUSTOM, params={"callable": "my.module.func"})
        assert resolved_command(action, self._ctx(tmp_path)) == "my.module.func"

    def test_queue_agent_prompt(self, tmp_path):
        action = ActionSpec(
            type=ActionType.QUEUE_AGENT,
            params={"agent": "fixer", "prompt": "fix {target}"},
        )
        assert resolved_command(action, self._ctx(tmp_path)) == "fix repo1"

    def test_queue_agent_prompt_template(self, tmp_path):
        action = ActionSpec(
            type=ActionType.QUEUE_AGENT,
            params={"agent": "fixer", "prompt_template": "fix {target}"},
        )
        assert resolved_command(action, self._ctx(tmp_path)) == "fix repo1"

    def test_queue_agent_falls_back_to_agent(self, tmp_path):
        action = ActionSpec(type=ActionType.QUEUE_AGENT, params={"agent": "fixer"})
        assert resolved_command(action, self._ctx(tmp_path)) == "fixer"

    def test_unknown_type_returns_empty(self, tmp_path):
        action = ActionSpec(type="unknown_type", params={})
        assert resolved_command(action, self._ctx(tmp_path)) == ""


class TestStageBlockedAndGivenUp:
    """Tests for the given_up and blocked branches in stage evaluation."""

    def test_given_up_stage_logs_given_up(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        repo = workspace / "repo1"
        repo.mkdir()
        # Create give_up marker so stage is not actionable
        (repo / ".gave_up").touch()
        config = make_config(
            workspace,
            stages=[
                Stage.from_dict({
                    "id": "A0", "name": "Gave up stage",
                    "trigger": {"type": "file_missing", "path": "done.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {
                        "completion": {"type": "file", "name": "done.md"},
                        "give_up": {"type": "file", "name": ".gave_up"},
                    },
                }),
                make_command_stage("A1", "Next", "final.md"),
            ],
            log_file="execution.log",
        )
        pipeline = Pipeline(config)
        result = pipeline.tick(target="repo1")
        assert result.status == TickResultStatus.ACTION_EXECUTED

        lines = read_lines(workspace / "execution.log")
        stage_results = [l["result"] for l in lines if l["event"] == "stage"]
        assert "given_up" in stage_results
        assert "trigger_fired" in stage_results

    def test_blocked_stage_logs_blocked(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        repo = workspace / "repo1"
        repo.mkdir()
        # Create rejection marker with max_rejections > 0 and a trigger that
        # does NOT fire (file exists), so the stage stays rejected/blocked.
        (repo / ".rejection").write_text(json.dumps({"rejection_count": 1}))
        (repo / "trigger.txt").touch()  # trigger file exists -> trigger won't fire
        config = make_config(
            workspace,
            stages=[
                Stage.from_dict({
                    "id": "A0", "name": "Rejected stage",
                    "trigger": {"type": "file_missing", "path": "trigger.txt"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {
                        "completion": {"type": "file", "name": "done.md"},
                        "rejection": {"type": "json", "name": ".rejection", "content": {}},
                    },
                    "max_rejections": 3,
                }),
                make_command_stage("A1", "Next", "final.md"),
            ],
            log_file="execution.log",
        )
        pipeline = Pipeline(config)
        result = pipeline.tick(target="repo1")
        assert result.status == TickResultStatus.ACTION_EXECUTED

        lines = read_lines(workspace / "execution.log")
        stage_results = [l["result"] for l in lines if l["event"] == "stage"]
        assert "blocked" in stage_results
        assert "trigger_fired" in stage_results


class TestExceptionLogClosure:
    """Verify that exceptions after tick_start always produce a matching tick_end."""

    def test_tick_exception_logs_matching_tick_end(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()
        config = make_config(
            workspace,
            stages=[make_command_stage("A0", "A", "a.md")],
            log_file="execution.log",
        )
        pipeline = Pipeline(config)

        with mock.patch.object(pipeline_mod, "execute_action", side_effect=RuntimeError("boom")):
            result = pipeline.tick(target="repo1")

        assert result.status == TickResultStatus.ACTION_FAILED

        lines = read_lines(workspace / "execution.log")
        events = [line["event"] for line in lines]
        assert events[0] == "tick_start"
        assert events[-1] == "tick_end"

        tick_start = lines[0]
        tick_end = lines[-1]
        assert tick_start["tick_id"]
        assert tick_end["tick_id"] == tick_start["tick_id"]
        assert tick_end["target"] == "repo1"
        assert tick_end["final_status"] == "action_failed"

    def test_tick_all_exception_logs_matching_tick_end_per_target(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()
        (workspace / "repo2").mkdir()
        config = make_config(
            workspace,
            stages=[make_command_stage("A0", "A", "a.md")],
            targets={"type": "static", "items": ["repo1", "repo2"]},
            log_file="execution.log",
        )
        pipeline = Pipeline(config)

        with mock.patch.object(pipeline_mod, "execute_action", side_effect=RuntimeError("boom")):
            results = pipeline.tick_all()

        assert len(results) == 2
        assert all(r.status == TickResultStatus.ACTION_FAILED for r in results)

        lines = read_lines(workspace / "execution.log")
        tick_starts = [line for line in lines if line["event"] == "tick_start"]
        tick_ends = [line for line in lines if line["event"] == "tick_end"]
        assert len(tick_starts) == 2
        assert len(tick_ends) == 2

        start_ids = {line["tick_id"] for line in tick_starts}
        for end in tick_ends:
            assert end["tick_id"] in start_ids
            assert end["final_status"] == "action_failed"

        targets_seen = sorted(line["target"] for line in tick_starts)
        assert targets_seen == ["repo1", "repo2"]


class TestPreTickSkipLogging:
    """Verify log output when a pre_tick hook returns False (no_work skip)."""

    def test_pre_tick_false_logs_no_work(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()

        mod = tmp_path / "pre_tick_skip_mod.py"
        mod.write_text("def pre_tick(context):\n    return False\n")

        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            config = make_config(
                workspace,
                stages=[make_command_stage("A0", "A", "a.md")],
                pre_tick=HookConfig(callable="pre_tick_skip_mod.pre_tick"),
                log_file="execution.log",
            )
            pipeline = Pipeline(config)
            result = pipeline.tick(target="repo1")
        finally:
            sys.path.remove(str(tmp_path))
            if "pre_tick_skip_mod" in sys.modules:
                del sys.modules["pre_tick_skip_mod"]

        assert result.status == TickResultStatus.NO_WORK

        lines = read_lines(workspace / "execution.log")
        events = [line["event"] for line in lines]
        assert events[0] == "tick_start"
        assert events[-1] == "tick_end"
        assert events == ["tick_start", "tick_end"]

        tick_start = lines[0]
        tick_end = lines[-1]
        assert tick_start["target"] == "repo1"
        assert tick_end["target"] == "repo1"
        assert tick_end["final_status"] == "no_work"
        assert tick_end["tick_id"] == tick_start["tick_id"]


class TestTargetLockBlockedLogging:
    """Verify log output when target_lock blocks stages during processing."""

    def test_target_lock_processing_logs_blocked(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        repo = workspace / "repo1"
        repo.mkdir()
        (repo / ".processing").write_text(json.dumps({"retry_count": 0}))

        config = make_config(
            workspace,
            stages=[
                Stage.from_dict({
                    "id": "A0", "name": "Processing stage",
                    "trigger": {"type": "file_missing", "path": "done.md"},
                    "action": {"type": "queue_agent", "params": {"agent": "TestAgent", "prompt": "do"}},
                    "markers": {
                        "completion": {"type": "file", "name": "done.md"},
                        "processing": {"type": "json", "name": ".processing", "content": {}},
                    },
                    "timeout_minutes": 30,
                    "max_retries": 3,
                }),
                make_command_stage("A1", "Blocked stage", "final.md"),
            ],
            target_lock=True,
            log_file="execution.log",
        )
        pipeline = Pipeline(config)
        result = pipeline.tick(target="repo1")
        assert result.status == TickResultStatus.NO_WORK

        lines = read_lines(workspace / "execution.log")
        assert lines[0]["event"] == "tick_start"
        assert lines[-1]["event"] == "tick_end"
        assert lines[-1]["final_status"] == "no_work"

        stage_results = {l["stage_id"]: l["result"] for l in lines if l["event"] == "stage"}
        assert stage_results["A0"] == "processing"
        assert stage_results["A1"] == "blocked"


class TestStaleDryRunLogging:
    """Verify dry-run log output for stale processing markers."""

    def _stale_stage(self):
        return Stage.from_dict({
            "id": "A0", "name": "Agent Step",
            "trigger": {"type": "file_missing", "path": "done.md"},
            "action": {"type": "queue_agent", "params": {"agent": "TestAgent", "prompt": "do"}},
            "markers": {
                "completion": {"type": "file", "name": "done.md"},
                "processing": {"type": "json", "name": ".processing", "content": {}},
            },
            "timeout_minutes": 0,
            "max_retries": 3,
        })

    def _write_stale_processing(self, repo, retry_count):
        queue_file = repo / "nonexistent_queue.json"
        (repo / ".processing").write_text(
            json.dumps({"retry_count": retry_count, "queue_file": str(queue_file)})
        )
        old = time.time() - 3600
        os.utime(repo / ".processing", (old, old))

    def test_stale_dry_run_would_give_up_logged(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        repo = workspace / "repo1"
        repo.mkdir()
        self._write_stale_processing(repo, retry_count=3)

        config = make_config(
            workspace,
            stages=[self._stale_stage()],
            log_file="execution.log",
        )
        pipeline = Pipeline(config)
        result = pipeline.tick(target="repo1", dry_run=True)
        assert result.status == TickResultStatus.DRY_RUN

        lines = read_lines(workspace / "execution.log")
        stage_results = [l["result"] for l in lines if l["event"] == "stage"]
        assert stage_results == ["would_give_up"]
        assert lines[-1]["final_status"] == "dry_run"

    def test_stale_dry_run_would_requeue_logged(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        repo = workspace / "repo1"
        repo.mkdir()
        self._write_stale_processing(repo, retry_count=1)

        config = make_config(
            workspace,
            stages=[self._stale_stage()],
            log_file="execution.log",
        )
        pipeline = Pipeline(config)
        result = pipeline.tick(target="repo1", dry_run=True)
        assert result.status == TickResultStatus.DRY_RUN

        lines = read_lines(workspace / "execution.log")
        stage_results = [l["result"] for l in lines if l["event"] == "stage"]
        assert stage_results == ["would_requeue"]
        assert lines[-1]["final_status"] == "dry_run"
