"""Tests for cronpypeline.state — PipelineState, marker resolution."""

import json
import os
import time

from cronpypeline.config import (
    ActionSpec,
    ActionType,
    MarkerSpec,
    Stage,
    TriggerCondition,
    TriggerType,
)
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


class TestStageStateRejectionCount:
    """Tests for rejection_count derivation from rejection marker."""

    def test_rejection_count_read_from_marker(self, tmp_path):
        """rejection_count should be read from rejection marker JSON data."""
        stage = Stage(
            id="A0",
            name="Review",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="done.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo hi"}),
            markers={
                "completion": MarkerSpec(name="done.md", type=MarkerType.FILE),
                "rejection": MarkerSpec(name=".rejection", type=MarkerType.JSON, content={}),
            },
        )
        # Create rejection marker with rejection_count
        create_marker(stage.markers["rejection"], tmp_path)
        import json
        rej_path = tmp_path / ".rejection"
        rej_path.write_text(json.dumps({"rejection_count": 3, "reason": "bad"}))

        state = StageState(stage=stage)
        state.derive(tmp_path)
        assert state.is_rejected is True
        assert state.rejection_count == 3

    def test_rejection_marker_blocks_actionable_when_tracking_enabled(self, tmp_path):
        """A rejection marker should make the stage non-actionable when max_rejections > 0."""
        stage = Stage(
            id="A0",
            name="Review",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="done.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo hi"}),
            markers={
                "completion": MarkerSpec(name="done.md", type=MarkerType.FILE),
                "rejection": MarkerSpec(name=".rejection", type=MarkerType.JSON, content={}),
            },
            max_rejections=5,
        )
        create_marker(stage.markers["rejection"], tmp_path)
        (tmp_path / ".rejection").write_text(json.dumps({"rejection_count": 1}))

        state = StageState(stage=stage)
        state.derive(tmp_path)
        assert state.is_rejected is True
        assert state.is_actionable is False

    def test_rejection_marker_does_not_block_when_tracking_disabled(self, tmp_path):
        """A rejection marker should NOT block the stage when max_rejections == 0 (disabled)."""
        stage = Stage(
            id="A0",
            name="Review",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="done.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo hi"}),
            markers={
                "completion": MarkerSpec(name="done.md", type=MarkerType.FILE),
                "rejection": MarkerSpec(name=".rejection", type=MarkerType.JSON, content={}),
            },
            max_rejections=0,
        )
        create_marker(stage.markers["rejection"], tmp_path)
        (tmp_path / ".rejection").write_text(json.dumps({"rejection_count": 1}))

        state = StageState(stage=stage)
        state.derive(tmp_path)
        assert state.is_rejected is True
        assert state.is_actionable is True


class TestStageStateProcessingQueueFile:
    """Tests for processing staleness with queue_file."""

    def test_processing_with_nonexistent_queue_file_is_stale(self, tmp_path):
        """Processing marker with queue_file that doesn't exist should be stale."""
        stage = Stage(
            id="A0",
            name="Agent",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="done.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo hi"}),
            markers={
                "completion": MarkerSpec(name="done.md", type=MarkerType.FILE),
                "processing": MarkerSpec(name=".processing", type=MarkerType.JSON, content={}),
            },
        )
        import json
        proc_path = tmp_path / ".processing"
        proc_path.write_text(json.dumps({"queue_file": "/nonexistent/queue/entry.json"}))

        state = StageState(stage=stage)
        state.derive(tmp_path)
        assert state.is_processing is True
        assert state.is_stale is True

    def test_processing_with_existing_queue_file_not_stale(self, tmp_path):
        """Processing marker with queue_file that exists should not be stale."""
        stage = Stage(
            id="A0",
            name="Agent",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="done.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo hi"}),
            markers={
                "completion": MarkerSpec(name="done.md", type=MarkerType.FILE),
                "processing": MarkerSpec(name=".processing", type=MarkerType.JSON, content={}),
            },
        )
        queue_file = tmp_path / "queue" / "entry.json"
        queue_file.parent.mkdir()
        queue_file.touch()

        import json
        proc_path = tmp_path / ".processing"
        proc_path.write_text(json.dumps({"queue_file": str(queue_file)}))

        state = StageState(stage=stage)
        state.derive(tmp_path)
        assert state.is_processing is True
        assert state.is_stale is False

    def test_processing_with_gone_queue_file_and_reminder_not_stale(self, tmp_path):
        """Queue file gone but reminder file exists → agent restarted, not stale."""
        stage = Stage(
            id="A0",
            name="Agent",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="done.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo hi"}),
            markers={
                "completion": MarkerSpec(name="done.md", type=MarkerType.FILE),
                "processing": MarkerSpec(name=".processing", type=MarkerType.JSON, content={}),
            },
        )
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        # Queue file is gone, but a reminder file exists
        (queue_dir / "20260903104701585888_reminder_1.json").touch()

        import json
        proc_path = tmp_path / ".processing"
        proc_path.write_text(json.dumps({"queue_file": str(queue_dir / "entry.json")}))

        state = StageState(stage=stage)
        state.derive(tmp_path)
        assert state.is_processing is True
        assert state.is_stale is False


class TestTargetStateDisabledStage:
    """Tests for disabled stages in TargetState."""

    def test_disabled_stage_skipped_in_derive(self, tmp_path):
        """Disabled stages should not appear in stage_states."""
        stage1 = Stage(
            id="A0",
            name="Disabled",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="a.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo a"}),
            markers={"completion": MarkerSpec(name="a.md", type=MarkerType.FILE)},
            enabled=False,
        )
        stage2 = Stage(
            id="A1",
            name="Active",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="b.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo b"}),
            markers={"completion": MarkerSpec(name="b.md", type=MarkerType.FILE)},
        )
        ts = TargetState(target="repo", stages=[stage1, stage2])
        ts.derive(tmp_path)
        assert "A0" not in ts.stage_states
        assert "A1" in ts.stage_states

    def test_disabled_stage_skipped_in_first_actionable(self, tmp_path):
        """Disabled stages should be skipped in first_actionable_stage."""
        stage1 = Stage(
            id="A0",
            name="Disabled",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="a.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo a"}),
            markers={"completion": MarkerSpec(name="a.md", type=MarkerType.FILE)},
            enabled=False,
        )
        stage2 = Stage(
            id="A1",
            name="Active",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="b.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo b"}),
            markers={"completion": MarkerSpec(name="b.md", type=MarkerType.FILE)},
        )
        ts = TargetState(target="repo", stages=[stage1, stage2])
        ts.derive(tmp_path)
        first = ts.first_actionable_stage
        assert first is not None
        assert first.stage.id == "A1"


class TestTargetStateTargetLock:
    """Tests for target_lock behavior."""

    def test_target_lock_blocks_actionable_when_processing(self, tmp_path):
        """With target_lock, no stage should be actionable while any stage is processing."""
        stage1 = Stage(
            id="A0",
            name="Agent",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="a.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo a"}),
            markers={
                "completion": MarkerSpec(name="a.md", type=MarkerType.FILE),
                "processing": MarkerSpec(name=".processing", type=MarkerType.JSON, content={}),
            },
        )
        stage2 = Stage(
            id="A1",
            name="Next",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="b.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo b"}),
            markers={"completion": MarkerSpec(name="b.md", type=MarkerType.FILE)},
        )
        # Create processing marker
        create_marker(stage1.markers["processing"], tmp_path)

        ts = TargetState(target="repo", stages=[stage1, stage2], target_lock=True)
        ts.derive(tmp_path)
        assert ts.has_processing is True
        assert ts.first_actionable_stage is None


class TestTargetStateOrphanedProcessingCleanup:
    """Tests for orphaned processing marker cleanup during derivation."""

    def test_orphaned_processing_marker_cleaned_up(self, tmp_path):
        """When a stage is complete but its processing marker is still on disk,
        derive() should delete it and clear is_processing so target_lock
        doesn't block downstream stages."""
        stage1 = Stage(
            id="A0",
            name="Async step",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="a.md"),
            action=ActionSpec(type=ActionType.QUEUE_AGENT, params={"agent": "test", "prompt": "do"}),
            markers={
                "completion": MarkerSpec(name="a.md", type=MarkerType.FILE),
                "processing": MarkerSpec(name=".processing_a", type=MarkerType.JSON, content={}),
            },
            timeout_minutes=30,
        )
        stage2 = Stage(
            id="B0",
            name="Next step",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="b.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo b"}),
            markers={"completion": MarkerSpec(name="b.md", type=MarkerType.FILE)},
        )
        # Simulate: agent completed (a.md exists) but processing marker left behind
        (tmp_path / "a.md").touch()
        create_marker(stage1.markers["processing"], tmp_path)

        ts = TargetState(target="repo", stages=[stage1, stage2], target_lock=True)
        ts.derive(tmp_path)
        # Processing marker should be cleaned up
        assert ts.stage_states["A0"].is_complete is True
        assert ts.stage_states["A0"].is_processing is False
        assert not (tmp_path / ".processing_a").exists()
        # target_lock should not block downstream stages
        assert ts.has_processing is False
        assert ts.first_actionable_stage is not None
        assert ts.first_actionable_stage.stage.id == "B0"

    def test_orphaned_processing_cleanup_in_multi_target_selection(self, tmp_path):
        """When target_lock is enabled and a target has an orphaned processing
        marker, get_target_with_work should still find it (the marker is
        cleaned up during derivation, before selection)."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        for t in ["repo1", "repo2"]:
            (workspace / t).mkdir()

        stage1 = Stage(
            id="A0",
            name="Async step",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="a.md"),
            action=ActionSpec(type=ActionType.QUEUE_AGENT, params={"agent": "test", "prompt": "do"}),
            markers={
                "completion": MarkerSpec(name="a.md", type=MarkerType.FILE),
                "processing": MarkerSpec(name=".processing_a", type=MarkerType.JSON, content={}),
            },
            timeout_minutes=30,
        )
        stage2 = Stage(
            id="B0",
            name="Next step",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="b.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo b"}),
            markers={"completion": MarkerSpec(name="b.md", type=MarkerType.FILE)},
        )
        # repo1: A0 complete, orphaned processing marker
        (workspace / "repo1" / "a.md").touch()
        create_marker(stage1.markers["processing"], workspace / "repo1")
        # repo2: A0 not complete (no markers at all)
        state = PipelineState(
            workspace_dir=workspace, stages=[stage1, stage2], target_lock=True
        )
        state.derive(["repo1", "repo2"])
        # repo1 should have work (B0 actionable after orphan cleanup)
        target = state.get_target_with_work(["repo1", "repo2"])
        assert target == "repo1"


class TestPipelineStateFlattenConfig:
    """Tests for PipelineState flattening target_config into context."""

    def test_stale_processing_does_not_block_target_selection(self, tmp_path):
        """When target_lock is enabled and the only processing stage is stale,
        first_stage_with_work should still find work — stale markers indicate
        the agent is gone and should not block target selection."""
        stage1 = Stage(
            id="A0",
            name="Async step",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="a.md"),
            action=ActionSpec(type=ActionType.QUEUE_AGENT, params={"agent": "test", "prompt": "do"}),
            markers={
                "completion": MarkerSpec(name="a.md", type=MarkerType.FILE),
                "processing": MarkerSpec(name=".processing_a", type=MarkerType.JSON, content={}),
            },
            timeout_minutes=30,
        )
        stage2 = Stage(
            id="B0",
            name="Next step",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="b.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo b"}),
            markers={"completion": MarkerSpec(name="b.md", type=MarkerType.FILE)},
        )
        # A0 is not complete, processing marker exists but is old → stale
        create_marker(stage1.markers["processing"], tmp_path)
        # Make it stale by setting the file mtime in the past
        import os as _os
        import time as _time
        old_ts = _time.time() - 3600  # 1 hour ago, well past timeout
        _os.utime(tmp_path / ".processing_a", (old_ts, old_ts))

        ts = TargetState(target="repo", stages=[stage1, stage2], target_lock=True)
        ts.derive(tmp_path)
        # A0 should be stale
        assert ts.stage_states["A0"].is_stale is True
        assert ts.stage_states["A0"].is_processing is True
        # has_active_processing should be False (only stale processing)
        assert ts.has_active_processing is False
        # first_stage_with_work should find B0 (stale doesn't block)
        fsw = ts.first_stage_with_work
        assert fsw is not None
        assert fsw.stage.id == "B0"

    def test_flatten_target_config_keys_into_context(self, tmp_path):
        """Target config keys should be flattened into the derivation context."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()

        stage = Stage(
            id="A0",
            name="Step 1",
            trigger=TriggerCondition(type=TriggerType.FILE_MISSING, path="a.md"),
            action=ActionSpec(type=ActionType.COMMAND, params={"command": "echo a"}),
            markers={"completion": MarkerSpec(name="a.md", type=MarkerType.FILE)},
        )
        state = PipelineState(workspace_dir=workspace, stages=[stage])
        state.derive(["repo1"], target_configs={"repo1": {"test_cmd": "pytest", "threshold": 90}})
        # The target state should exist
        assert "repo1" in state.target_states
