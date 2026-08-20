"""Tests for cronpypeline.state — PipelineState, marker resolution."""

import json
import os
import time
from pathlib import Path

import pytest

from cronpypeline.config import Stage, TriggerCondition, TriggerType, ActionSpec, ActionType, MarkerSpec
from cronpypeline.markers import MarkerType, create_marker
from cronpypeline.state import PipelineState, StageState, TargetState


class TestStageState:
    """Tests for StageState — per-stage derived state."""

    def test_stage_not_started(self, tmp_path):
        stage = Stage(
            id="A0",
            name="Onboarding",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="briefing.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo hi"}),
            markers={"completion": MarkerSpec(name="briefing.md", type=MarkerType.FILE)},
        )
        state = StageState(stage=stage)
        state.derive(tmp_path)
        assert state.is_complete is False
        assert state.is_processing is False
        assert state.is_given_up is False
        assert state.retry_count == 0

    def test_stage_complete(self, tmp_path):
        stage = Stage(
            id="A0",
            name="Onboarding",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="briefing.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo hi"}),
            markers={"completion": MarkerSpec(name="briefing.md", type=MarkerType.FILE)},
        )
        (tmp_path / "briefing.md").touch()
        state = StageState(stage=stage)
        state.derive(tmp_path)
        assert state.is_complete is True

    def test_stage_processing(self, tmp_path):
        stage = Stage(
            id="A0",
            name="Onboarding",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="briefing.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo hi"}),
            markers={
                "completion": MarkerSpec(name="briefing.md", type=MarkerType.FILE),
                "processing": MarkerSpec(name=".processing", type=MarkerType.JSON, content={}),
            },
        )
        create_marker(stage.markers["processing"], tmp_path)
        state = StageState(stage=stage)
        state.derive(tmp_path)
        assert state.is_processing is True
        assert state.is_complete is False

    def test_stage_given_up(self, tmp_path):
        stage = Stage(
            id="A0",
            name="Onboarding",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="briefing.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo hi"}),
            markers={
                "completion": MarkerSpec(name="briefing.md", type=MarkerType.FILE),
                "give_up": MarkerSpec(name=".gave_up", type=MarkerType.FILE),
            },
        )
        (tmp_path / ".gave_up").touch()
        state = StageState(stage=stage)
        state.derive(tmp_path)
        assert state.is_given_up is True

    def test_stage_retry_count_from_processing_marker(self, tmp_path):
        stage = Stage(
            id="A0",
            name="Onboarding",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="briefing.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo hi"}),
            markers={
                "completion": MarkerSpec(name="briefing.md", type=MarkerType.FILE),
                "processing": MarkerSpec(name=".processing", type=MarkerType.JSON, content={}),
            },
        )
        data = {"retry_count": 2, "timestamp": time.time()}
        (tmp_path / ".processing").write_text(json.dumps(data))
        state = StageState(stage=stage)
        state.derive(tmp_path)
        assert state.retry_count == 2

    def test_stage_retry_count_defaults_to_zero(self, tmp_path):
        stage = Stage(
            id="A0",
            name="Onboarding",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="briefing.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo hi"}),
            markers={
                "completion": MarkerSpec(name="briefing.md", type=MarkerType.FILE),
                "processing": MarkerSpec(name=".processing", type=MarkerType.JSON, content={}),
            },
        )
        create_marker(stage.markers["processing"], tmp_path)
        state = StageState(stage=stage)
        state.derive(tmp_path)
        assert state.retry_count == 0

    def test_stage_is_stale(self, tmp_path):
        stage = Stage(
            id="A0",
            name="Onboarding",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="briefing.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo hi"}),
            timeout_minutes=30,
            markers={
                "completion": MarkerSpec(name="briefing.md", type=MarkerType.FILE),
                "processing": MarkerSpec(name=".processing", type=MarkerType.JSON, content={}),
            },
        )
        create_marker(stage.markers["processing"], tmp_path)
        # Set processing marker mtime to 60 minutes ago
        old_time = time.time() - 3600
        os.utime(tmp_path / ".processing", (old_time, old_time))
        state = StageState(stage=stage)
        state.derive(tmp_path)
        assert state.is_stale is True

    def test_stage_not_stale_when_recent(self, tmp_path):
        stage = Stage(
            id="A0",
            name="Onboarding",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="briefing.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo hi"}),
            timeout_minutes=30,
            markers={
                "completion": MarkerSpec(name="briefing.md", type=MarkerType.FILE),
                "processing": MarkerSpec(name=".processing", type=MarkerType.JSON, content={}),
            },
        )
        create_marker(stage.markers["processing"], tmp_path)
        state = StageState(stage=stage)
        state.derive(tmp_path)
        assert state.is_stale is False

    def test_stage_not_stale_without_processing_marker(self, tmp_path):
        stage = Stage(
            id="A0",
            name="Onboarding",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="briefing.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo hi"}),
            timeout_minutes=30,
            markers={"completion": MarkerSpec(name="briefing.md", type=MarkerType.FILE)},
        )
        state = StageState(stage=stage)
        state.derive(tmp_path)
        assert state.is_stale is False


class TestTargetState:
    """Tests for TargetState — all stages for one target."""

    def test_derive_all_stages(self, tmp_path):
        stages = [
            Stage(
                id="A0",
                name="Step 1",
                trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="a.md"),
                action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo a"}),
                markers={"completion": MarkerSpec(name="a.md", type=MarkerType.FILE)},
            ),
            Stage(
                id="A1",
                name="Step 2",
                trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="b.md"),
                action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo b"}),
                markers={"completion": MarkerSpec(name="b.md", type=MarkerType.FILE)},
            ),
        ]
        # A0 is complete, A1 is not
        (tmp_path / "a.md").touch()
        target_state = TargetState(target="my-repo", stages=stages)
        target_state.derive(tmp_path)
        assert target_state.stage_states["A0"].is_complete is True
        assert target_state.stage_states["A1"].is_complete is False

    def test_first_actionable_stage(self, tmp_path):
        stages = [
            Stage(
                id="A0",
                name="Step 1",
                trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="a.md"),
                action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo a"}),
                markers={"completion": MarkerSpec(name="a.md", type=MarkerType.FILE)},
            ),
            Stage(
                id="A1",
                name="Step 2",
                trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="b.md"),
                action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo b"}),
                markers={"completion": MarkerSpec(name="b.md", type=MarkerType.FILE)},
            ),
        ]
        # A0 is complete, A1 is not → first actionable is A1
        (tmp_path / "a.md").touch()
        target_state = TargetState(target="my-repo", stages=stages)
        target_state.derive(tmp_path)
        first = target_state.first_actionable_stage
        assert first is not None
        assert first.stage.id == "A1"

    def test_no_actionable_stage_when_all_complete(self, tmp_path):
        stages = [
            Stage(
                id="A0",
                name="Step 1",
                trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="a.md"),
                action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo a"}),
                markers={"completion": MarkerSpec(name="a.md", type=MarkerType.FILE)},
            ),
        ]
        (tmp_path / "a.md").touch()
        target_state = TargetState(target="my-repo", stages=stages)
        target_state.derive(tmp_path)
        assert target_state.first_actionable_stage is None

    def test_skips_given_up_stages(self, tmp_path):
        stages = [
            Stage(
                id="A0",
                name="Step 1",
                trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="a.md"),
                action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo a"}),
                markers={
                    "completion": MarkerSpec(name="a.md", type=MarkerType.FILE),
                    "give_up": MarkerSpec(name=".gave_up", type=MarkerType.FILE),
                },
            ),
            Stage(
                id="A1",
                name="Step 2",
                trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="b.md"),
                action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo b"}),
                markers={"completion": MarkerSpec(name="b.md", type=MarkerType.FILE)},
            ),
        ]
        (tmp_path / ".gave_up").touch()
        target_state = TargetState(target="my-repo", stages=stages)
        target_state.derive(tmp_path)
        first = target_state.first_actionable_stage
        assert first is not None
        assert first.stage.id == "A1"

    def test_skips_processing_stages(self, tmp_path):
        stages = [
            Stage(
                id="A0",
                name="Step 1",
                trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="a.md"),
                action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo a"}),
                markers={
                    "completion": MarkerSpec(name="a.md", type=MarkerType.FILE),
                    "processing": MarkerSpec(name=".processing", type=MarkerType.JSON, content={}),
                },
            ),
        ]
        create_marker(stages[0].markers["processing"], tmp_path)
        target_state = TargetState(target="my-repo", stages=stages)
        target_state.derive(tmp_path)
        # Stage is processing, not actionable
        assert target_state.first_actionable_stage is None


class TestPipelineState:
    """Tests for PipelineState — all targets, all stages."""

    def test_derive_single_target(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        target_dir = workspace / "my-repo"
        target_dir.mkdir()

        stages = [
            Stage(
                id="A0",
                name="Step 1",
                trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="a.md"),
                action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo a"}),
                markers={"completion": MarkerSpec(name="a.md", type=MarkerType.FILE)},
            ),
        ]
        state = PipelineState(workspace_dir=workspace, stages=stages)
        state.derive(["my-repo"])
        assert "my-repo" in state.target_states
        assert state.target_states["my-repo"].stage_states["A0"].is_complete is False

    def test_derive_multiple_targets(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        for t in ["repo1", "repo2"]:
            (workspace / t).mkdir()

        stages = [
            Stage(
                id="A0",
                name="Step 1",
                trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="a.md"),
                action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo a"}),
                markers={"completion": MarkerSpec(name="a.md", type=MarkerType.FILE)},
            ),
        ]
        state = PipelineState(workspace_dir=workspace, stages=stages)
        state.derive(["repo1", "repo2"])
        assert "repo1" in state.target_states
        assert "repo2" in state.target_states

    def test_get_target_with_work(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        for t in ["repo1", "repo2"]:
            (workspace / t).mkdir()

        stages = [
            Stage(
                id="A0",
                name="Step 1",
                trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="a.md"),
                action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo a"}),
                markers={"completion": MarkerSpec(name="a.md", type=MarkerType.FILE)},
            ),
        ]
        # repo1 is complete, repo2 is not
        (workspace / "repo1" / "a.md").touch()

        state = PipelineState(workspace_dir=workspace, stages=stages)
        state.derive(["repo1", "repo2"])
        target = state.get_target_with_work(["repo1", "repo2"])
        assert target == "repo2"

    def test_get_target_with_work_returns_none_when_all_done(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()
        (workspace / "repo1" / "a.md").touch()

        stages = [
            Stage(
                id="A0",
                name="Step 1",
                trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="a.md"),
                action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo a"}),
                markers={"completion": MarkerSpec(name="a.md", type=MarkerType.FILE)},
            ),
        ]
        state = PipelineState(workspace_dir=workspace, stages=stages)
        state.derive(["repo1"])
        assert state.get_target_with_work(["repo1"]) is None

    def test_get_all_targets_with_work(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        for t in ["repo1", "repo2", "repo3"]:
            (workspace / t).mkdir()

        stages = [
            Stage(
                id="A0",
                name="Step 1",
                trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="a.md"),
                action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo a"}),
                markers={"completion": MarkerSpec(name="a.md", type=MarkerType.FILE)},
            ),
        ]
        # repo1 is complete
        (workspace / "repo1" / "a.md").touch()

        state = PipelineState(workspace_dir=workspace, stages=stages)
        state.derive(["repo1", "repo2", "repo3"])
        targets = state.get_all_targets_with_work(["repo1", "repo2", "repo3"])
        assert "repo2" in targets
        assert "repo3" in targets
        assert "repo1" not in targets
