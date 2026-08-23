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


class TestLogRejectionEdgeCases:
    """Tests for log_rejection edge cases."""

    def test_log_rejection_no_marker_is_noop(self, tmp_path):
        """log_rejection should be a no-op when no rejection marker exists."""
        from cronpypeline.plugins.vnn_plugin import log_rejection

        target_dir = tmp_path / "story-1"
        target_dir.mkdir()

        context = {
            "target": "story-1",
            "target_dir": str(target_dir),
            "stage_id": "A0",
        }
        result = ActionResult(success=False, stderr="test error")

        # Should not raise
        log_rejection(context, result)
        assert not (target_dir / ".VNN" / "rejection_log.json").exists()

    def test_log_rejection_invalid_json_falls_back_to_empty(self, tmp_path):
        """log_rejection with invalid JSON in rejection marker should use empty data."""
        from cronpypeline.plugins.vnn_plugin import log_rejection

        target_dir = tmp_path / "story-1"
        target_dir.mkdir()
        (target_dir / ".rejection").write_text("{invalid json}")

        context = {
            "target": "story-1",
            "target_dir": str(target_dir),
            "stage_id": "A0",
        }
        result = ActionResult(success=False, stderr="test error")

        log_rejection(context, result)

        log_file = target_dir / ".VNN" / "rejection_log.json"
        assert log_file.exists()
        log_data = json.loads(log_file.read_text())
        assert log_data[0]["rejection_count"] == 0
        assert log_data[0]["reason"] == "test error"

    def test_log_rejection_existing_log_not_list_reset(self, tmp_path):
        """log_rejection with non-list existing log should reset to list."""
        from cronpypeline.plugins.vnn_plugin import log_rejection

        target_dir = tmp_path / "story-1"
        target_dir.mkdir()
        (target_dir / ".rejection").write_text(json.dumps({"rejection_count": 1, "reason": "bad"}))

        vnn_dir = target_dir / ".VNN"
        vnn_dir.mkdir()
        log_file = vnn_dir / "rejection_log.json"
        log_file.write_text('{"not": "a list"}')

        context = {
            "target": "story-1",
            "target_dir": str(target_dir),
            "stage_id": "A0",
        }
        result = ActionResult(success=False, stderr="err")

        log_rejection(context, result)

        log_data = json.loads(log_file.read_text())
        assert isinstance(log_data, list)
        assert len(log_data) == 1

    def test_log_rejection_with_none_result(self, tmp_path):
        """log_rejection with None result should not crash."""
        from cronpypeline.plugins.vnn_plugin import log_rejection

        target_dir = tmp_path / "story-1"
        target_dir.mkdir()
        (target_dir / ".rejection").write_text(json.dumps({"rejection_count": 2}))

        context = {
            "target": "story-1",
            "target_dir": str(target_dir),
            "stage_id": "A0",
        }

        log_rejection(context, None)

        log_file = target_dir / ".VNN" / "rejection_log.json"
        assert log_file.exists()
        log_data = json.loads(log_file.read_text())
        assert log_data[0]["rejection_count"] == 2
        assert log_data[0]["reason"] == ""

    def test_log_rejection_os_error_on_read_treated_as_empty(self, tmp_path):
        """OSError reading existing log should be treated as empty list."""
        from unittest.mock import patch
        from cronpypeline.plugins.vnn_plugin import log_rejection

        target_dir = tmp_path / "story-1"
        target_dir.mkdir()
        (target_dir / ".rejection").write_text(json.dumps({"rejection_count": 1}))

        # Pre-create the log file so log_file.exists() returns True
        vnn_dir = target_dir / ".VNN"
        vnn_dir.mkdir(parents=True, exist_ok=True)
        log_file = vnn_dir / "rejection_log.json"
        log_file.write_text("corrupt data")

        context = {
            "target": "story-1",
            "target_dir": str(target_dir),
            "stage_id": "A0",
        }
        result = ActionResult(success=False, stderr="err")

        # Mock only json.loads for the log file read to raise OSError
        original_json_loads = json.loads

        def mock_loads(data):
            if isinstance(data, str) and "corrupt" in data:
                raise OSError("io error")
            return original_json_loads(data)

        with patch("json.loads", side_effect=mock_loads):
            log_rejection(context, result)

        # Log should have been written with just the new entry
        log_data = original_json_loads(log_file.read_text())
        assert len(log_data) == 1
        assert log_data[0]["rejection_count"] == 1


class TestQueueEmptyGlobalEdgeCases:
    """Tests for queue_empty_global edge cases."""

    def test_no_queue_dir_returns_true(self):
        """When no queue_dir in config, should return True."""
        from cronpypeline.plugins.vnn_plugin import queue_empty_global

        assert queue_empty_global({"target_config": {}}) is True

    def test_nonexistent_queue_dir_returns_true(self, tmp_path):
        """Non-existent queue dir should return True."""
        from cronpypeline.plugins.vnn_plugin import queue_empty_global

        context = {"target_config": {"queue_dir": str(tmp_path / "nonexistent")}}
        assert queue_empty_global(context) is True

    def test_queue_with_json_files_returns_false(self, tmp_path):
        """Queue dir with .json files should return False."""
        from cronpypeline.plugins.vnn_plugin import queue_empty_global

        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        (queue_dir / "entry1.json").write_text("{}")

        context = {"target_config": {"queue_dir": str(queue_dir)}}
        assert queue_empty_global(context) is False


class TestSyncStoryStatesEdgeCases:
    """Tests for sync_story_states edge cases."""

    def test_existing_ranking_not_dict_reset(self, tmp_path):
        """Non-dict ranking file should be reset to empty dict."""
        from cronpypeline.plugins.vnn_plugin import sync_story_states

        target_dir = tmp_path / "story-1"
        target_dir.mkdir()
        vnn_dir = target_dir / ".VNN"
        vnn_dir.mkdir()
        ranking_file = vnn_dir / "ranking.json"
        ranking_file.write_text('["not", "a", "dict"]')

        context = {
            "target": "story-1",
            "target_dir": str(target_dir),
            "workspace_dir": str(tmp_path),
            "target_config": {},
        }

        result = sync_story_states(context)
        assert result is True
        data = json.loads(ranking_file.read_text())
        assert isinstance(data, dict)
        assert "story-1" in data

    def test_existing_ranking_invalid_json_reset(self, tmp_path):
        """Invalid JSON ranking file should be reset to empty dict."""
        from cronpypeline.plugins.vnn_plugin import sync_story_states

        target_dir = tmp_path / "story-1"
        target_dir.mkdir()
        vnn_dir = target_dir / ".VNN"
        vnn_dir.mkdir()
        ranking_file = vnn_dir / "ranking.json"
        ranking_file.write_text("{invalid json")

        context = {
            "target": "story-1",
            "target_dir": str(target_dir),
            "workspace_dir": str(tmp_path),
            "target_config": {},
        }

        result = sync_story_states(context)
        assert result is True
        data = json.loads(ranking_file.read_text())
        assert isinstance(data, dict)


class TestCheckCompletedCompilations:
    """Tests for check_completed_compilations hook."""

    def test_no_compilation_marker_returns_true(self, tmp_path):
        """No compilation marker should return True immediately."""
        from cronpypeline.plugins.vnn_plugin import check_completed_compilations

        target_dir = tmp_path / "story-1"
        target_dir.mkdir()

        context = {
            "target": "story-1",
            "target_dir": str(target_dir),
        }
        assert check_completed_compilations(context) is True

    def test_compilation_marker_updates_state(self, tmp_path):
        """Compilation marker should update compilation_state.json."""
        from cronpypeline.plugins.vnn_plugin import check_completed_compilations

        target_dir = tmp_path / "story-1"
        target_dir.mkdir()
        (target_dir / ".compilation_complete").write_text(
            json.dumps({"timestamp": 12345, "output": "success"})
        )

        context = {
            "target": "story-1",
            "target_dir": str(target_dir),
        }
        result = check_completed_compilations(context)
        assert result is True

        state_file = target_dir / ".VNN" / "compilation_state.json"
        assert state_file.exists()
        state = json.loads(state_file.read_text())
        assert state["story-1"]["completed"] is True
        assert state["story-1"]["output"] == "success"

    def test_compilation_marker_invalid_json(self, tmp_path):
        """Invalid JSON in compilation marker should use empty data."""
        from cronpypeline.plugins.vnn_plugin import check_completed_compilations

        target_dir = tmp_path / "story-1"
        target_dir.mkdir()
        (target_dir / ".compilation_complete").write_text("{invalid json}")

        context = {
            "target": "story-1",
            "target_dir": str(target_dir),
        }
        result = check_completed_compilations(context)
        assert result is True

        state_file = target_dir / ".VNN" / "compilation_state.json"
        state = json.loads(state_file.read_text())
        assert state["story-1"]["completed"] is True
        assert state["story-1"]["output"] == ""

    def test_compilation_state_invalid_json_reset(self, tmp_path):
        """Invalid JSON in compilation_state.json should be reset."""
        from cronpypeline.plugins.vnn_plugin import check_completed_compilations

        target_dir = tmp_path / "story-1"
        target_dir.mkdir()
        (target_dir / ".compilation_complete").write_text(json.dumps({"output": "ok"}))

        vnn_dir = target_dir / ".VNN"
        vnn_dir.mkdir()
        state_file = vnn_dir / "compilation_state.json"
        state_file.write_text("{invalid json}")

        context = {
            "target": "story-1",
            "target_dir": str(target_dir),
        }
        result = check_completed_compilations(context)
        assert result is True
        state = json.loads(state_file.read_text())
        assert isinstance(state, dict)


class TestCleanupStaleCompilationMarkers:
    """Tests for cleanup_stale_compilation_markers hook."""

    def test_no_marker_returns_true(self, tmp_path):
        """No compilation marker should return True."""
        from cronpypeline.plugins.vnn_plugin import cleanup_stale_compilation_markers

        target_dir = tmp_path / "story-1"
        target_dir.mkdir()

        context = {
            "target_dir": str(target_dir),
            "target_config": {},
        }
        assert cleanup_stale_compilation_markers(context) is True

    def test_stale_marker_removed(self, tmp_path):
        """Stale compilation marker older than timeout should be removed."""
        import os
        import time as _time
        from cronpypeline.plugins.vnn_plugin import cleanup_stale_compilation_markers

        target_dir = tmp_path / "story-1"
        target_dir.mkdir()
        marker = target_dir / ".compilation_complete"
        marker.write_text("{}")

        # Set mtime to 2 hours ago
        old_time = _time.time() - 7200
        os.utime(marker, (old_time, old_time))

        context = {
            "target_dir": str(target_dir),
            "target_config": {"compilation_timeout_minutes": 60},
        }
        result = cleanup_stale_compilation_markers(context)
        assert result is True
        assert not marker.exists()

    def test_fresh_marker_kept(self, tmp_path):
        """Fresh compilation marker should not be removed."""
        from cronpypeline.plugins.vnn_plugin import cleanup_stale_compilation_markers

        target_dir = tmp_path / "story-1"
        target_dir.mkdir()
        marker = target_dir / ".compilation_complete"
        marker.write_text("{}")

        context = {
            "target_dir": str(target_dir),
            "target_config": {"compilation_timeout_minutes": 60},
        }
        result = cleanup_stale_compilation_markers(context)
        assert result is True
        assert marker.exists()

    def test_os_error_on_stat_returns_true(self, tmp_path):
        """OSError on stat should be caught and return True."""
        from unittest.mock import patch
        from cronpypeline.plugins.vnn_plugin import cleanup_stale_compilation_markers

        target_dir = tmp_path / "story-1"
        target_dir.mkdir()
        marker = target_dir / ".compilation_complete"
        marker.write_text("{}")

        context = {
            "target_dir": str(target_dir),
            "target_config": {"compilation_timeout_minutes": 60},
        }

        original_stat = Path.stat

        def stat_side_effect(self, *args, **kwargs):
            if not hasattr(stat_side_effect, "_called"):
                stat_side_effect._called = True
                return original_stat(self, *args, **kwargs)
            raise OSError("permission denied")

        with patch("pathlib.Path.stat", stat_side_effect):
            result = cleanup_stale_compilation_markers(context)
        assert result is True
        assert marker.exists()


class TestDiscoverStories:
    """Tests for discover_stories hook."""

    def test_discovers_story_directories(self, tmp_path):
        """Should find directories with .VNN or article.md."""
        from cronpypeline.plugins.vnn_plugin import discover_stories

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        # Story with .VNN
        (workspace / "story-1").mkdir()
        (workspace / "story-1" / ".VNN").mkdir()

        # Story with article.md
        (workspace / "story-2").mkdir()
        (workspace / "story-2" / "article.md").touch()

        # Not a story
        (workspace / "not-a-story").mkdir()

        context = {
            "workspace_dir": str(workspace),
            "target_config": {},
        }
        result = discover_stories(context)
        assert result is True

        registry = json.loads((workspace / ".VNN" / "stories.json").read_text())
        assert "story-1" in registry["stories"]
        assert "story-2" in registry["stories"]
        assert "not-a-story" not in registry["stories"]

    def test_empty_workspace(self, tmp_path):
        """Empty workspace should produce empty stories list."""
        from cronpypeline.plugins.vnn_plugin import discover_stories

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        context = {
            "workspace_dir": str(workspace),
            "target_config": {},
        }
        result = discover_stories(context)
        assert result is True

        registry = json.loads((workspace / ".VNN" / "stories.json").read_text())
        assert registry["stories"] == []

    def test_non_dir_files_skipped(self, tmp_path):
        """Non-directory files in workspace should be skipped."""
        from cronpypeline.plugins.vnn_plugin import discover_stories

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        # A regular file (not a directory) — should be skipped
        (workspace / "readme.txt").write_text("not a story")

        # A real story directory
        (workspace / "story-1").mkdir()
        (workspace / "story-1" / ".VNN").mkdir()

        context = {
            "workspace_dir": str(workspace),
            "target_config": {},
        }
        result = discover_stories(context)
        assert result is True

        registry = json.loads((workspace / ".VNN" / "stories.json").read_text())
        assert "story-1" in registry["stories"]
        assert "readme.txt" not in registry["stories"]


class TestVnnCompositeHooks:
    """Tests for composite VNN hooks."""

    def test_vnn_pre_tick_all_pass(self, tmp_path):
        """vnn_pre_tick should return True when all hooks pass."""
        from cronpypeline.plugins.vnn_plugin import vnn_pre_tick

        context = {
            "target_dir": str(tmp_path),
            "target_config": {},
            "workspace_dir": str(tmp_path),
            "target": "test",
        }
        assert vnn_pre_tick(context) is True

    def test_vnn_pre_tick_queue_not_empty_returns_false(self, tmp_path):
        """vnn_pre_tick should return False when queue is not empty."""
        from cronpypeline.plugins.vnn_plugin import vnn_pre_tick

        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        (queue_dir / "entry.json").write_text("{}")

        context = {
            "target_dir": str(tmp_path),
            "target_config": {"queue_dir": str(queue_dir)},
            "workspace_dir": str(tmp_path),
            "target": "test",
        }
        assert vnn_pre_tick(context) is False

    def test_vnn_post_tick_runs_all_hooks(self, tmp_path):
        """vnn_post_tick should run all post-tick hooks without error."""
        from cronpypeline.plugins.vnn_plugin import vnn_post_tick

        context = {
            "target_dir": str(tmp_path),
            "target": "test",
            "stage_id": "A0",
        }
        result = ActionResult(success=False, stderr="err")
        vnn_post_tick(context, result)
