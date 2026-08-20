"""Tests for cronpypeline.plugins — conversation_queue and swe_plugin."""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from cronpypeline.actions import TickContext, ActionResult, register_handler
from cronpypeline.config import ActionSpec, ActionType
from cronpypeline.plugins.conversation_queue import ConversationQueueHandler
from cronpypeline.plugins.swe_plugin import (
    detect_open_issue,
    detect_agent_forgot_marker,
    cleanup_git_branch,
    reset_issue_status,
)


class TestConversationQueueHandler:
    """Tests for the conversation queue action handler."""

    def test_writes_json_to_queue_dir(self, tmp_path):
        queue_dir = tmp_path / "queue"
        handler = ConversationQueueHandler(queue_dir=str(queue_dir))

        action = ActionSpec(
            type=ActionType.QUEUE_AGENT,
            params={"agent": "CoderAgent", "prompt": "Fix issue 42"},
        )
        ctx = TickContext(target="my-repo", workspace_dir=tmp_path, dry_run=False, verbose=False)
        result = handler.execute(action, ctx)

        assert result.success is True
        assert queue_dir.exists()
        files = list(queue_dir.glob("*.json"))
        assert len(files) == 1

        entry = json.loads(files[0].read_text())
        assert entry["agent"] == "CoderAgent"
        assert entry["prompt"] == "Fix issue 42"
        assert entry["target"] == "my-repo"

    def test_dry_run_does_not_write(self, tmp_path):
        queue_dir = tmp_path / "queue"
        handler = ConversationQueueHandler(queue_dir=str(queue_dir))

        action = ActionSpec(
            type=ActionType.QUEUE_AGENT,
            params={"agent": "TestAgent", "prompt": "Test"},
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=True, verbose=False)
        result = handler.execute(action, ctx)

        assert result.success is True
        assert result.dry_run is True
        assert not queue_dir.exists() or not list(queue_dir.glob("*.json"))

    def test_prompt_template_substitution(self, tmp_path):
        queue_dir = tmp_path / "queue"
        handler = ConversationQueueHandler(queue_dir=str(queue_dir))

        action = ActionSpec(
            type=ActionType.QUEUE_AGENT,
            params={
                "agent": "CoderAgent",
                "prompt_template": "Fix issue in {target}",
            },
        )
        ctx = TickContext(target="my-repo", workspace_dir=tmp_path, dry_run=False, verbose=False)
        result = handler.execute(action, ctx)

        files = list(queue_dir.glob("*.json"))
        entry = json.loads(files[0].read_text())
        assert "my-repo" in entry["prompt"]

    def test_prompt_template_with_target_config(self, tmp_path):
        """Prompt templates should support target_config variables."""
        queue_dir = tmp_path / "queue"
        handler = ConversationQueueHandler(queue_dir=str(queue_dir))

        action = ActionSpec(
            type=ActionType.QUEUE_AGENT,
            params={
                "agent": "CoderAgent",
                "prompt_template": "Fix issue {issue_id} in {target} using {test_cmd}",
            },
        )
        ctx = TickContext(
            target="my-repo",
            workspace_dir=tmp_path,
            dry_run=False,
            verbose=False,
            target_config={"issue_id": "42", "test_cmd": "pytest"},
        )
        result = handler.execute(action, ctx)

        files = list(queue_dir.glob("*.json"))
        entry = json.loads(files[0].read_text())
        assert "Fix issue 42 in my-repo using pytest" == entry["prompt"]

    def test_prompt_with_target_config(self, tmp_path):
        """Plain prompt should also support target_config variables."""
        queue_dir = tmp_path / "queue"
        handler = ConversationQueueHandler(queue_dir=str(queue_dir))

        action = ActionSpec(
            type=ActionType.QUEUE_AGENT,
            params={
                "agent": "CoderAgent",
                "prompt": "Run {test_cmd} for {target}",
            },
        )
        ctx = TickContext(
            target="my-repo",
            workspace_dir=tmp_path,
            dry_run=False,
            verbose=False,
            target_config={"test_cmd": "tox"},
        )
        result = handler.execute(action, ctx)

        files = list(queue_dir.glob("*.json"))
        entry = json.loads(files[0].read_text())
        assert "Run tox for my-repo" == entry["prompt"]

    def test_includes_optional_fields(self, tmp_path):
        queue_dir = tmp_path / "queue"
        handler = ConversationQueueHandler(queue_dir=str(queue_dir))

        action = ActionSpec(
            type=ActionType.QUEUE_AGENT,
            params={
                "agent": "TestAgent",
                "prompt": "Test",
                "model": "gpt-4",
                "temperature": 0.7,
                "max_tokens": 4096,
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False, verbose=False)
        result = handler.execute(action, ctx)

        files = list(queue_dir.glob("*.json"))
        entry = json.loads(files[0].read_text())
        assert entry["model"] == "gpt-4"
        assert entry["temperature"] == 0.7
        assert entry["max_tokens"] == 4096

    def test_check_complete_when_queue_empty(self, tmp_path):
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        handler = ConversationQueueHandler(queue_dir=str(queue_dir))

        action = ActionSpec(type=ActionType.QUEUE_AGENT, params={"agent": "Test"})
        ctx = TickContext(target="repo", workspace_dir=tmp_path)
        assert handler.check_complete(action, ctx) is True

    def test_check_complete_when_queue_has_files(self, tmp_path):
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        (queue_dir / "task.json").write_text("{}")
        handler = ConversationQueueHandler(queue_dir=str(queue_dir))

        action = ActionSpec(type=ActionType.QUEUE_AGENT, params={"agent": "Test"})
        ctx = TickContext(target="repo", workspace_dir=tmp_path)
        assert handler.check_complete(action, ctx) is False

    def test_check_complete_when_queue_dir_missing(self, tmp_path):
        handler = ConversationQueueHandler(queue_dir=str(tmp_path / "nonexistent"))
        action = ActionSpec(type=ActionType.QUEUE_AGENT, params={"agent": "Test"})
        ctx = TickContext(target="repo", workspace_dir=tmp_path)
        assert handler.check_complete(action, ctx) is True

    def test_loads_agent_settings(self, tmp_path):
        queue_dir = tmp_path / "queue"
        agent_settings_dir = tmp_path / "agents"
        agent_settings_dir.mkdir()
        (agent_settings_dir / "CoderAgent.json").write_text(json.dumps({
            "system_prompt": "You are a coder",
            "tools": ["git", "pytest"],
        }))

        handler = ConversationQueueHandler(
            queue_dir=str(queue_dir),
            agent_settings_dir=str(agent_settings_dir),
        )

        action = ActionSpec(
            type=ActionType.QUEUE_AGENT,
            params={"agent": "CoderAgent", "prompt": "Fix issue"},
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler.execute(action, ctx)

        files = list(queue_dir.glob("*.json"))
        entry = json.loads(files[0].read_text())
        assert "agent_config" in entry
        assert entry["agent_config"]["system_prompt"] == "You are a coder"


class TestDetectOpenIssue:
    """Tests for the SWE plugin detect_open_issue trigger."""

    def test_fires_when_open_issue_exists(self, tmp_path):
        swe_dir = tmp_path / ".SWE"
        swe_dir.mkdir()
        issues = [{"id": 1, "status": "open"}, {"id": 2, "status": "closed"}]
        (swe_dir / "issues.json").write_text(json.dumps(issues))

        result = detect_open_issue({"target_dir": str(tmp_path)})
        assert result is True

    def test_does_not_fire_when_no_open_issues(self, tmp_path):
        swe_dir = tmp_path / ".SWE"
        swe_dir.mkdir()
        issues = [{"id": 1, "status": "closed"}, {"id": 2, "status": "resolved"}]
        (swe_dir / "issues.json").write_text(json.dumps(issues))

        result = detect_open_issue({"target_dir": str(tmp_path)})
        assert result is False

    def test_does_not_fire_when_no_issues_file(self, tmp_path):
        result = detect_open_issue({"target_dir": str(tmp_path)})
        assert result is False

    def test_handles_corrupt_issues_file(self, tmp_path):
        swe_dir = tmp_path / ".SWE"
        swe_dir.mkdir()
        (swe_dir / "issues.json").write_text("not valid json")

        result = detect_open_issue({"target_dir": str(tmp_path)})
        assert result is False


class TestDetectAgentForgotMarker:
    """Tests for the SWE plugin detect_agent_forgot_marker trigger."""

    def test_fires_when_marker_missing_and_task_exists(self, tmp_path):
        # Create task.json (active task)
        (tmp_path / "task.json").write_text(json.dumps({"issue_id": "42"}))
        # No coding_complete.marker
        # Mock git log to return success
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="abc123 commit msg\n")
            result = detect_agent_forgot_marker({"target_dir": str(tmp_path)})
        assert result is True

    def test_does_not_fire_when_marker_exists(self, tmp_path):
        (tmp_path / "task.json").write_text("{}")
        (tmp_path / "coding_complete.marker").touch()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="abc123 commit\n")
            result = detect_agent_forgot_marker({"target_dir": str(tmp_path)})
        assert result is False

    def test_does_not_fire_when_no_task(self, tmp_path):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="abc123\n")
            result = detect_agent_forgot_marker({"target_dir": str(tmp_path)})
        assert result is False


class TestCleanupGitBranch:
    """Tests for the SWE plugin cleanup_git_branch action."""

    def test_runs_git_commands(self, tmp_path):
        action = ActionSpec(
            type=ActionType.COMMAND,
            params={"task_branch": "task-issue-42"},
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = cleanup_git_branch(action, ctx)

        assert result[0] is True
        assert mock_run.call_count == 2
        # Check the git commands
        calls = mock_run.call_args_list
        assert calls[0].args[0] == ["git", "checkout", "integration"]
        assert calls[1].args[0] == ["git", "branch", "-D", "task-issue-42"]


class TestResetIssueStatus:
    """Tests for the SWE plugin reset_issue_status action."""

    def test_resets_issue_to_open(self, tmp_path):
        ctx = TickContext(target="repo", workspace_dir=tmp_path)
        swe_dir = ctx.target_dir / ".SWE"
        swe_dir.mkdir(parents=True)
        issues = [{"id": 42, "status": "in_progress"}]
        issues_file = swe_dir / "issues.json"
        issues_file.write_text(json.dumps(issues))

        action = ActionSpec(
            type=ActionType.COMMAND,
            params={"issue_id": 42},
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path)

        result = reset_issue_status(action, ctx)
        assert result[0] is True

        updated = json.loads(issues_file.read_text())
        assert updated[0]["status"] == "open"

    def test_returns_false_when_issue_not_found(self, tmp_path):
        ctx = TickContext(target="repo", workspace_dir=tmp_path)
        swe_dir = ctx.target_dir / ".SWE"
        swe_dir.mkdir(parents=True)
        issues = [{"id": 99, "status": "closed"}]
        (swe_dir / "issues.json").write_text(json.dumps(issues))

        action = ActionSpec(
            type=ActionType.COMMAND,
            params={"issue_id": 42},
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path)

        result = reset_issue_status(action, ctx)
        assert result[0] is False

    def test_returns_false_when_no_issues_file(self, tmp_path):
        action = ActionSpec(
            type=ActionType.COMMAND,
            params={"issue_id": 42},
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path)

        result = reset_issue_status(action, ctx)
        assert result[0] is False
