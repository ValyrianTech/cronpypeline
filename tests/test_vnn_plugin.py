"""Tests for VNN plugin: conversation ID continuation and rejection audit trail."""

import json
from pathlib import Path

from cronpypeline.actions import ActionResult, TickContext
from cronpypeline.config import ActionSpec, ActionType
from cronpypeline.plugins.conversation_queue import ConversationQueueHandler


class TestConversationIdContinuation:
    """Tests for conversation ID continuation on retry (Phase 3.2)."""

    def test_retry_reuses_entry_id_as_conversation_id(self, tmp_path):
        """On retry, the previous entry_id should be reused as conversation_id."""
        queue_dir = tmp_path / "queue"
        handler = ConversationQueueHandler(
            queue_dir=str(queue_dir),
            prompt_field="content",
            default_fields={
                "sender": "VNN_PIPELINE",
                "conversation_id": "",
                "folder_name": "VNN",
                "model_name": "default_model",
                "runs_left": 3,
            },
        )

        previous_entry_id = "prev-conv-id-123"
        action = ActionSpec(
            type=ActionType.QUEUE_AGENT,
            params={"agent": "WriterAgent", "prompt": "Write article", "reminder_prompt": "Continue writing"},
        )
        ctx = TickContext(
            target="story-1",
            workspace_dir=tmp_path,
            dry_run=False,
            retry_count=1,
            retry_data={"entry_id": previous_entry_id},
        )
        result = handler.execute(action, ctx)

        entry = json.loads(Path(result.data["queue_file"]).read_text())
        assert entry["conversation_id"] == previous_entry_id
        assert entry["id"] == previous_entry_id  # id also reused

    def test_first_attempt_uses_new_uuid(self, tmp_path):
        """On first attempt (no retry), a new UUID should be generated."""
        queue_dir = tmp_path / "queue"
        handler = ConversationQueueHandler(
            queue_dir=str(queue_dir),
            prompt_field="content",
            default_fields={"conversation_id": ""},
        )

        action = ActionSpec(
            type=ActionType.QUEUE_AGENT,
            params={"agent": "WriterAgent", "prompt": "Write article"},
        )
        ctx = TickContext(target="story-1", workspace_dir=tmp_path, dry_run=False, retry_count=0)
        result = handler.execute(action, ctx)

        entry = json.loads(Path(result.data["queue_file"]).read_text())
        assert entry["conversation_id"] == ""
        assert entry["id"] != ""

    def test_retry_without_retry_data_uses_new_uuid(self, tmp_path):
        """On retry but with no retry_data, a new UUID is generated."""
        queue_dir = tmp_path / "queue"
        handler = ConversationQueueHandler(
            queue_dir=str(queue_dir),
            prompt_field="content",
            default_fields={"conversation_id": ""},
        )

        action = ActionSpec(
            type=ActionType.QUEUE_AGENT,
            params={"agent": "WriterAgent", "prompt": "Write", "reminder_prompt": "Continue"},
        )
        ctx = TickContext(
            target="story-1",
            workspace_dir=tmp_path,
            dry_run=False,
            retry_count=1,
            retry_data=None,
        )
        result = handler.execute(action, ctx)

        entry = json.loads(Path(result.data["queue_file"]).read_text())
        # conversation_id should be empty (default), id should be new UUID
        assert entry["conversation_id"] == ""

    def test_retry_decrements_runs_left(self, tmp_path):
        """On retry, runs_left should be decremented based on retry_count."""
        queue_dir = tmp_path / "queue"
        handler = ConversationQueueHandler(
            queue_dir=str(queue_dir),
            prompt_field="content",
            default_fields={
                "conversation_id": "",
                "runs_left": 3,
            },
        )

        action = ActionSpec(
            type=ActionType.QUEUE_AGENT,
            params={"agent": "WriterAgent", "prompt": "Write", "reminder_prompt": "Continue"},
        )
        ctx = TickContext(
            target="story-1",
            workspace_dir=tmp_path,
            dry_run=False,
            retry_count=2,
            retry_data={"entry_id": "prev-id"},
        )
        result = handler.execute(action, ctx)

        entry = json.loads(Path(result.data["queue_file"]).read_text())
        assert entry["runs_left"] == 1  # 3 - 2 = 1


class TestRejectionAuditTrail:
    """Tests for rejection audit trail (Phase 3.3)."""

    def test_log_rejection_appends_to_log(self, tmp_path):
        """log_rejection should append an entry to rejection_log.json."""
        from cronpypeline.plugins.vnn_plugin import log_rejection

        target_dir = tmp_path / "story-1"
        target_dir.mkdir()
        swe_dir = target_dir / ".VNN"
        swe_dir.mkdir()

        # Create a rejection marker
        rej_marker = target_dir / ".rejection"
        rej_marker.write_text(json.dumps({"rejection_count": 1}))

        context = {
            "target": "story-1",
            "target_dir": str(target_dir),
            "workspace_dir": str(tmp_path),
            "target_config": {},
            "stage_id": "publishing",
        }

        result = ActionResult(success=False, stderr="Article rejected: poor quality")
        result.stage_id = "publishing"

        log_rejection(context, result)

        log_file = swe_dir / "rejection_log.json"
        assert log_file.exists()
        log = json.loads(log_file.read_text())
        assert len(log) == 1
        assert log[0]["target"] == "story-1"
        assert log[0]["stage_id"] == "publishing"
        assert "timestamp" in log[0]
        assert "rejection_count" in log[0]

    def test_log_rejection_appends_multiple_entries(self, tmp_path):
        """Multiple rejections should append multiple entries (append-only)."""
        from cronpypeline.plugins.vnn_plugin import log_rejection

        target_dir = tmp_path / "story-1"
        target_dir.mkdir()
        swe_dir = target_dir / ".VNN"
        swe_dir.mkdir()

        rej_marker = target_dir / ".rejection"
        rej_marker.write_text(json.dumps({"rejection_count": 2}))

        context = {
            "target": "story-1",
            "target_dir": str(target_dir),
            "workspace_dir": str(tmp_path),
            "target_config": {},
            "stage_id": "publishing",
        }

        result = ActionResult(success=False, stderr="Rejected again")

        log_rejection(context, result)
        log_rejection(context, result)

        log_file = swe_dir / "rejection_log.json"
        log = json.loads(log_file.read_text())
        assert len(log) == 2

    def test_log_rejection_no_rejection_marker(self, tmp_path):
        """If no rejection marker exists, don't log anything."""
        from cronpypeline.plugins.vnn_plugin import log_rejection

        target_dir = tmp_path / "story-1"
        target_dir.mkdir()

        context = {
            "target": "story-1",
            "target_dir": str(target_dir),
            "workspace_dir": str(tmp_path),
            "target_config": {},
            "stage_id": "publishing",
        }

        result = ActionResult(success=True)

        log_rejection(context, result)

        log_file = target_dir / ".VNN" / "rejection_log.json"
        assert not log_file.exists()

    def test_log_rejection_includes_reason(self, tmp_path):
        """Rejection log entry should include the reason from stderr."""
        from cronpypeline.plugins.vnn_plugin import log_rejection

        target_dir = tmp_path / "story-1"
        target_dir.mkdir()
        swe_dir = target_dir / ".VNN"
        swe_dir.mkdir()

        rej_marker = target_dir / ".rejection"
        rej_marker.write_text(json.dumps({"rejection_count": 1, "reason": "poor quality"}))

        context = {
            "target": "story-1",
            "target_dir": str(target_dir),
            "workspace_dir": str(tmp_path),
            "target_config": {},
            "stage_id": "publishing",
        }

        result = ActionResult(success=False, stderr="Article rejected: poor quality")

        log_rejection(context, result)

        log_file = swe_dir / "rejection_log.json"
        log = json.loads(log_file.read_text())
        assert "poor quality" in log[0].get("reason", "") or "poor quality" in log[0].get("stderr", "")


class TestVnnQueueEmptyGlobal:
    """Tests for queue_empty_global pre_tick hook."""

    def test_returns_false_when_queue_has_files(self, tmp_path):
        """Should return False (skip tick) when queue dir has files."""
        from cronpypeline.plugins.vnn_plugin import queue_empty_global

        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        (queue_dir / "task.json").write_text("{}")

        context = {
            "target": "story-1",
            "target_dir": str(tmp_path / "story-1"),
            "workspace_dir": str(tmp_path),
            "target_config": {"queue_dir": str(queue_dir)},
        }

        result = queue_empty_global(context)
        assert result is False

    def test_returns_true_when_queue_empty(self, tmp_path):
        """Should return True (proceed) when queue dir is empty."""
        from cronpypeline.plugins.vnn_plugin import queue_empty_global

        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()

        context = {
            "target": "story-1",
            "target_dir": str(tmp_path / "story-1"),
            "workspace_dir": str(tmp_path),
            "target_config": {"queue_dir": str(queue_dir)},
        }

        result = queue_empty_global(context)
        assert result is True

    def test_returns_true_when_queue_dir_missing(self, tmp_path):
        """Should return True when queue dir doesn't exist."""
        from cronpypeline.plugins.vnn_plugin import queue_empty_global

        context = {
            "target": "story-1",
            "target_dir": str(tmp_path / "story-1"),
            "workspace_dir": str(tmp_path),
            "target_config": {"queue_dir": str(tmp_path / "nonexistent")},
        }

        result = queue_empty_global(context)
        assert result is True


class TestVnnSyncStoryStates:
    """Tests for sync_story_states pre_tick hook."""

    def test_syncs_ranking_json_with_filesystem(self, tmp_path):
        """sync_story_states should update ranking.json based on story directories."""
        from cronpypeline.plugins.vnn_plugin import sync_story_states

        target_dir = tmp_path / "story-1"
        target_dir.mkdir()
        vnn_dir = target_dir / ".VNN"
        vnn_dir.mkdir()

        # Create some story markers
        (target_dir / "article.md").write_text("# Article")
        (target_dir / "published.json").write_text(json.dumps({"url": "https://example.com"}))

        context = {
            "target": "story-1",
            "target_dir": str(target_dir),
            "workspace_dir": str(tmp_path),
            "target_config": {},
        }

        result = sync_story_states(context)
        assert result is not False

        ranking_file = vnn_dir / "ranking.json"
        assert ranking_file.exists()
        ranking = json.loads(ranking_file.read_text())
        assert "story-1" in ranking or isinstance(ranking, list)


class TestVnnCleanupInconsistentState:
    """Tests for cleanup_inconsistent_state pre_tick hook."""

    def test_removes_conflicting_markers(self, tmp_path):
        """Should remove processing marker if completion marker also exists."""
        from cronpypeline.plugins.vnn_plugin import cleanup_inconsistent_state

        target_dir = tmp_path / "story-1"
        target_dir.mkdir()

        # Both processing and completion markers exist (inconsistent)
        (target_dir / ".processing").write_text(json.dumps({"retry_count": 1}))
        (target_dir / "done.md").write_text("Done")

        context = {
            "target": "story-1",
            "target_dir": str(target_dir),
            "workspace_dir": str(tmp_path),
            "target_config": {},
        }

        result = cleanup_inconsistent_state(context)
        assert result is not False

        # Processing marker should be removed
        assert not (target_dir / ".processing").exists()
        # Completion marker should remain
        assert (target_dir / "done.md").exists()

    def test_no_conflict_does_nothing(self, tmp_path):
        """When no conflict exists, should do nothing and return True."""
        from cronpypeline.plugins.vnn_plugin import cleanup_inconsistent_state

        target_dir = tmp_path / "story-1"
        target_dir.mkdir()
        (target_dir / "done.md").write_text("Done")

        context = {
            "target": "story-1",
            "target_dir": str(target_dir),
            "workspace_dir": str(tmp_path),
            "target_config": {},
        }

        result = cleanup_inconsistent_state(context)
        assert result is not False
        assert (target_dir / "done.md").exists()
