"""Tests for cronpypeline.plugins — conversation_queue and swe_plugin."""

import json
from unittest.mock import MagicMock, patch

from cronpypeline.actions import TickContext
from cronpypeline.config import ActionSpec, ActionType
from cronpypeline.plugins.conversation_queue import ConversationQueueHandler
from cronpypeline.plugins.issue_store import get_issue
from cronpypeline.plugins.swe_plugin import (
    cleanup_git_branch,
    detect_agent_forgot_marker,
    detect_open_issue,
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
        handler.execute(action, ctx)

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
        handler.execute(action, ctx)

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
        handler.execute(action, ctx)

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
        handler.execute(action, ctx)

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


class TestConversationQueueSerendipityFormat:
    """Tests for Serendipity-compatible queue entry format (Phase 1)."""

    def test_prompt_field_content(self, tmp_path):
        """When prompt_field='content', the prompt is stored under 'content' key."""
        queue_dir = tmp_path / "queue"
        handler = ConversationQueueHandler(
            queue_dir=str(queue_dir),
            prompt_field="content",
        )

        action = ActionSpec(
            type=ActionType.QUEUE_AGENT,
            params={"agent": "CoderAgent", "prompt": "Fix issue 42"},
        )
        ctx = TickContext(target="my-repo", workspace_dir=tmp_path, dry_run=False)
        handler.execute(action, ctx)

        entry = json.loads(next(iter(queue_dir.glob("*.json"))).read_text())
        assert "content" in entry
        assert entry["content"] == "Fix issue 42"
        assert "prompt" not in entry

    def test_default_prompt_field_is_prompt(self, tmp_path):
        """Default prompt_field should be 'prompt' (backward compat)."""
        queue_dir = tmp_path / "queue"
        handler = ConversationQueueHandler(queue_dir=str(queue_dir))

        action = ActionSpec(
            type=ActionType.QUEUE_AGENT,
            params={"agent": "CoderAgent", "prompt": "Fix issue"},
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)
        handler.execute(action, ctx)

        entry = json.loads(next(iter(queue_dir.glob("*.json"))).read_text())
        assert "prompt" in entry
        assert "content" not in entry

    def test_default_fields_injected(self, tmp_path):
        """default_fields should be injected into every queue entry."""
        queue_dir = tmp_path / "queue"
        handler = ConversationQueueHandler(
            queue_dir=str(queue_dir),
            prompt_field="content",
            default_fields={
                "sender": "SWE_PIPELINE",
                "conversation_id": "",
                "folder_name": "SWE",
                "model_name": "default_model",
                "runs_left": 3,
            },
        )

        action = ActionSpec(
            type=ActionType.QUEUE_AGENT,
            params={"agent": "CoderAgent", "prompt": "Fix issue"},
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)
        handler.execute(action, ctx)

        entry = json.loads(next(iter(queue_dir.glob("*.json"))).read_text())
        assert entry["sender"] == "SWE_PIPELINE"
        assert entry["conversation_id"] == ""
        assert entry["folder_name"] == "SWE"
        assert entry["model_name"] == "default_model"
        assert entry["runs_left"] == 3

    def test_default_fields_not_overriding_action_params(self, tmp_path):
        """Action params should override default_fields when both specify the same key."""
        queue_dir = tmp_path / "queue"
        handler = ConversationQueueHandler(
            queue_dir=str(queue_dir),
            default_fields={"model_name": "default_model", "runs_left": 3},
        )

        action = ActionSpec(
            type=ActionType.QUEUE_AGENT,
            params={"agent": "Test", "prompt": "Test", "model_name": "gpt-4"},
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)
        handler.execute(action, ctx)

        entry = json.loads(next(iter(queue_dir.glob("*.json"))).read_text())
        assert entry["model_name"] == "gpt-4"

    def test_flatten_agent_settings(self, tmp_path):
        """When flatten_agent_settings=True, agent settings are merged flat."""
        queue_dir = tmp_path / "queue"
        agent_settings_dir = tmp_path / "agents"
        agent_settings_dir.mkdir()
        (agent_settings_dir / "CoderAgent.json").write_text(json.dumps({
            "user_name": "coder",
            "model_name": "gpt-4",
            "temperature": 0.5,
            "system_prompt": "You are a coder",
        }))

        handler = ConversationQueueHandler(
            queue_dir=str(queue_dir),
            agent_settings_dir=str(agent_settings_dir),
            flatten_agent_settings=True,
        )

        action = ActionSpec(
            type=ActionType.QUEUE_AGENT,
            params={"agent": "CoderAgent", "prompt": "Fix issue"},
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)
        handler.execute(action, ctx)

        entry = json.loads(next(iter(queue_dir.glob("*.json"))).read_text())
        assert entry["user_name"] == "coder"
        assert entry["model_name"] == "gpt-4"
        assert entry["temperature"] == 0.5
        assert "agent_config" not in entry

    def test_nested_agent_settings_when_not_flattened(self, tmp_path):
        """When flatten_agent_settings=False (default), agent settings are nested."""
        queue_dir = tmp_path / "queue"
        agent_settings_dir = tmp_path / "agents"
        agent_settings_dir.mkdir()
        (agent_settings_dir / "CoderAgent.json").write_text(json.dumps({
            "system_prompt": "You are a coder",
        }))

        handler = ConversationQueueHandler(
            queue_dir=str(queue_dir),
            agent_settings_dir=str(agent_settings_dir),
        )

        action = ActionSpec(
            type=ActionType.QUEUE_AGENT,
            params={"agent": "CoderAgent", "prompt": "Fix issue"},
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)
        handler.execute(action, ctx)

        entry = json.loads(next(iter(queue_dir.glob("*.json"))).read_text())
        assert "agent_config" in entry
        assert entry["agent_config"]["system_prompt"] == "You are a coder"

    def test_runs_left_decrement_on_retry(self, tmp_path):
        """On retry, runs_left should be decremented from default_fields value."""
        queue_dir = tmp_path / "queue"
        handler = ConversationQueueHandler(
            queue_dir=str(queue_dir),
            prompt_field="content",
            default_fields={"runs_left": 3},
        )

        action = ActionSpec(
            type=ActionType.QUEUE_AGENT,
            params={"agent": "CoderAgent", "prompt": "Fix issue", "reminder_prompt": "Try again"},
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False, retry_count=1)
        handler.execute(action, ctx)

        entry = json.loads(next(iter(queue_dir.glob("*.json"))).read_text())
        assert entry["runs_left"] == 2

    def test_runs_left_does_not_go_below_zero(self, tmp_path):
        """runs_left should not go below 0 on retries."""
        queue_dir = tmp_path / "queue"
        handler = ConversationQueueHandler(
            queue_dir=str(queue_dir),
            default_fields={"runs_left": 1},
        )

        action = ActionSpec(
            type=ActionType.QUEUE_AGENT,
            params={"agent": "CoderAgent", "prompt": "Fix", "reminder_prompt": "Again"},
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False, retry_count=5)
        handler.execute(action, ctx)

        entry = json.loads(next(iter(queue_dir.glob("*.json"))).read_text())
        assert entry["runs_left"] == 0

    def test_serendipity_full_format(self, tmp_path):
        """Full Serendipity-compatible entry with all fields."""
        queue_dir = tmp_path / "queue"
        agent_settings_dir = tmp_path / "agents"
        agent_settings_dir.mkdir()
        (agent_settings_dir / "CoderAgent.json").write_text(json.dumps({
            "user_name": "coder",
            "model_name": "gpt-4",
            "temperature": 0.5,
        }))

        handler = ConversationQueueHandler(
            queue_dir=str(queue_dir),
            agent_settings_dir=str(agent_settings_dir),
            prompt_field="content",
            default_fields={
                "sender": "SWE_PIPELINE",
                "conversation_id": "",
                "folder_name": "SWE",
                "model_name": "default_model",
                "runs_left": 3,
            },
            flatten_agent_settings=True,
        )

        action = ActionSpec(
            type=ActionType.QUEUE_AGENT,
            params={"agent": "CoderAgent", "prompt": "Fix issue 42"},
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)
        result = handler.execute(action, ctx)

        entry = json.loads(next(iter(queue_dir.glob("*.json"))).read_text())
        assert entry["content"] == "Fix issue 42"
        assert entry["agent"] == "CoderAgent"
        assert entry["sender"] == "SWE_PIPELINE"
        assert entry["folder_name"] == "SWE"
        assert entry["model_name"] == "gpt-4"  # from agent settings, overriding default
        assert entry["temperature"] == 0.5  # from agent settings
        assert entry["runs_left"] == 3
        assert entry["user_name"] == "coder"
        assert "agent_config" not in entry
        assert "prompt" not in entry
        assert result.data["queue_file"] is not None
        assert result.data["entry_id"] is not None


class TestDetectOpenIssue:
    """Tests for the SWE plugin detect_open_issue trigger.

    Uses .SWE/issues/*.md files with YAML frontmatter (not issues.json).
    """

    def test_fires_when_open_issue_exists(self, tmp_path):
        issues_dir = tmp_path / ".SWE" / "issues"
        issues_dir.mkdir(parents=True)
        (issues_dir / "issue-1.md").write_text("---\nid: 1\nstatus: open\n---\nBody")
        (issues_dir / "issue-2.md").write_text("---\nid: 2\nstatus: closed\n---\nBody")

        result = detect_open_issue({"target_dir": str(tmp_path)})
        assert result is True

    def test_does_not_fire_when_no_open_issues(self, tmp_path):
        issues_dir = tmp_path / ".SWE" / "issues"
        issues_dir.mkdir(parents=True)
        (issues_dir / "issue-1.md").write_text("---\nid: 1\nstatus: closed\n---\nBody")
        (issues_dir / "issue-2.md").write_text("---\nid: 2\nstatus: resolved\n---\nBody")

        result = detect_open_issue({"target_dir": str(tmp_path)})
        assert result is False

    def test_does_not_fire_when_no_issues_dir(self, tmp_path):
        result = detect_open_issue({"target_dir": str(tmp_path)})
        assert result is False

    def test_does_not_fire_when_issues_dir_empty(self, tmp_path):
        issues_dir = tmp_path / ".SWE" / "issues"
        issues_dir.mkdir(parents=True)

        result = detect_open_issue({"target_dir": str(tmp_path)})
        assert result is False

    def test_handles_corrupt_issue_file(self, tmp_path):
        issues_dir = tmp_path / ".SWE" / "issues"
        issues_dir.mkdir(parents=True)
        (issues_dir / "issue-1.md").write_text("not valid frontmatter at all")

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

    def test_does_not_fire_when_queue_has_files(self, tmp_path):
        """Queue with files should prevent firing (uses iterdir, not iterfile)."""
        (tmp_path / "task.json").write_text(json.dumps({"issue_id": "42"}))
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        (queue_dir / "task.json").write_text("{}")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="abc123 commit\n")
            result = detect_agent_forgot_marker({
                "target_dir": str(tmp_path),
                "queue_dir": str(queue_dir),
            })
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
    """Tests for the SWE plugin reset_issue_status action.

    Uses .SWE/issues/*.md files with YAML frontmatter (not issues.json).
    """

    def test_resets_issue_to_open(self, tmp_path):
        ctx = TickContext(target="repo", workspace_dir=tmp_path)
        issues_dir = ctx.target_dir / ".SWE" / "issues"
        issues_dir.mkdir(parents=True)
        (issues_dir / "42.md").write_text("---\nid: 42\nstatus: in_progress\nattempts: 1\n---\nBody")

        action = ActionSpec(
            type=ActionType.COMMAND,
            params={"issue_id": 42},
        )

        result = reset_issue_status(action, ctx)
        assert result[0] is True

        issue = get_issue(ctx.target_dir, 42)
        assert issue.status == "open"
        assert issue.attempts == 1  # other fields preserved

    def test_returns_false_when_issue_not_found(self, tmp_path):
        ctx = TickContext(target="repo", workspace_dir=tmp_path)
        issues_dir = ctx.target_dir / ".SWE" / "issues"
        issues_dir.mkdir(parents=True)
        (issues_dir / "99.md").write_text("---\nid: 99\nstatus: closed\n---\nBody")

        action = ActionSpec(
            type=ActionType.COMMAND,
            params={"issue_id": 42},
        )

        result = reset_issue_status(action, ctx)
        assert result[0] is False

    def test_returns_false_when_no_issues_dir(self, tmp_path):
        action = ActionSpec(
            type=ActionType.COMMAND,
            params={"issue_id": 42},
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path)

        result = reset_issue_status(action, ctx)
        assert result[0] is False
