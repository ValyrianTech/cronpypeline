"""Tests for SWE prompt builder custom action callables."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from cronpypeline.actions import TickContext
from cronpypeline.config import ActionSpec, ActionType
from cronpypeline.plugins.issue_store import create_issue, get_issue
from cronpypeline.plugins.swe_prompts import (
    build_coder_prompt,
    build_fix_prompt,
    build_review_prompt,
    queue_coder_agent,
    queue_fix_agent,
    queue_review_agent,
)


class TestBuildFixPrompt:
    """Tests for build_fix_prompt — builds a prompt from a diagnostic report."""

    def test_prompt_contains_report_content(self, tmp_path):
        report_path = tmp_path / "report.md"
        report_path.write_text("# Lint Report\n\n**Status**: FAIL\n\n3 errors found")

        prompt = build_fix_prompt(
            report_content=report_path.read_text(),
            report_name="lint_report",
            target="my-repo",
        )
        assert "3 errors found" in prompt
        assert "lint_report" in prompt
        assert "my-repo" in prompt

    def test_prompt_includes_instructions(self, tmp_path):
        report_content = "# Test Report\n**Status**: FAIL\n5 failed"
        prompt = build_fix_prompt(
            report_content=report_content,
            report_name="test_report",
            target="repo",
        )
        assert "fix" in prompt.lower()
        assert "FAIL" in prompt


class TestBuildCoderPrompt:
    """Tests for build_coder_prompt — builds a prompt from an issue."""

    def test_prompt_contains_issue_body(self, tmp_path):
        create_issue(tmp_path, {
            "id": 42,
            "source": "dep-audit",
            "type": "bug",
            "status": "open",
            "repo": "org/repo",
        }, body="The foo function crashes on empty input")

        issue = get_issue(tmp_path, 42)
        prompt = build_coder_prompt(
            issue=issue,
            target="org/repo",
            integration_sha="abc12345",
        )
        assert "The foo function crashes on empty input" in prompt
        assert "42" in prompt
        assert "abc12345" in prompt

    def test_prompt_includes_issue_metadata(self, tmp_path):
        create_issue(tmp_path, {
            "id": "issue-abc",
            "source": "review",
            "type": "enhancement",
            "status": "open",
            "repo": "org/repo",
            "labels": ["bug", "urgent"],
        }, body="Add new feature")

        issue = get_issue(tmp_path, "issue-abc")
        prompt = build_coder_prompt(
            issue=issue,
            target="org/repo",
            integration_sha="def67890",
        )
        assert "issue-abc" in prompt
        assert "review" in prompt
        assert "bug" in prompt or "urgent" in prompt


class TestBuildReviewPrompt:
    """Tests for build_review_prompt — builds a prompt for review agent."""

    def test_prompt_contains_cycle_and_diff(self):
        prompt = build_review_prompt(
            target="org/repo",
            cycle_number=3,
            diff_stats="5 files changed, 120 insertions(+), 30 deletions(-)",
            integration_sha="abc12345",
        )
        assert "3" in prompt
        assert "120 insertions" in prompt
        assert "abc12345" in prompt

    def test_prompt_includes_pr_state(self):
        prompt = build_review_prompt(
            target="org/repo",
            cycle_number=1,
            diff_stats="2 files changed",
            integration_sha="abc12345",
            pr_number=42,
            pr_url="https://github.com/org/repo/pull/42",
        )
        assert "42" in prompt
        assert "github.com/org/repo/pull/42" in prompt


class TestQueueFixAgent:
    """Tests for queue_fix_agent custom action callable."""

    def test_writes_queue_entry_with_report_content(self, tmp_path):
        # Set up report
        report_dir = tmp_path / ".SWE" / "reports" / "lint"
        report_dir.mkdir(parents=True)
        report_path = report_dir / "lint_report_20240101_120000.md"
        report_path.write_text("# Lint Report\n\n**Status**: FAIL\n\nFound 3 errors")

        # Set up queue
        queue_dir = tmp_path / "queue"
        agent_settings_dir = tmp_path / "agents"
        agent_settings_dir.mkdir()
        (agent_settings_dir / "FixAgent.json").write_text(json.dumps({
            "user_name": "fixer",
            "model_name": "gpt-4",
        }))

        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "callable": "cronpypeline.plugins.swe_prompts.queue_fix_agent",
                "report_path": str(report_path),
                "agent": "FixAgent",
                "queue_dir": str(queue_dir),
                "agent_settings_dir": str(agent_settings_dir),
                "prompt_field": "content",
                "default_fields": {
                    "sender": "SWE_PIPELINE",
                    "conversation_id": "",
                    "folder_name": "SWE",
                    "model_name": "default_model",
                    "runs_left": 3,
                },
                "flatten_agent_settings": True,
            },
        )
        ctx = TickContext(target="my-repo", workspace_dir=tmp_path, dry_run=False)

        result = queue_fix_agent(action, ctx)

        assert result.success is True
        assert "queue_file" in result.data

        entry = json.loads(Path(result.data["queue_file"]).read_text())
        assert entry["agent"] == "FixAgent"
        assert "3 errors" in entry["content"]
        assert entry["sender"] == "SWE_PIPELINE"
        assert entry["model_name"] == "gpt-4"  # from agent settings
        assert entry["user_name"] == "fixer"
        assert "prompt" not in entry

    def test_dry_run_does_not_write(self, tmp_path):
        report_path = tmp_path / "report.md"
        report_path.write_text("FAIL report")

        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "report_path": str(report_path),
                "agent": "FixAgent",
                "queue_dir": str(tmp_path / "queue"),
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=True)

        result = queue_fix_agent(action, ctx)
        assert result.success is True
        assert result.dry_run is True

    def test_missing_report_file_returns_error(self, tmp_path):
        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "report_path": str(tmp_path / "nonexistent.md"),
                "agent": "FixAgent",
                "queue_dir": str(tmp_path / "queue"),
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)

        result = queue_fix_agent(action, ctx)
        assert result.success is False


class TestQueueCoderAgent:
    """Tests for queue_coder_agent custom action callable."""

    def test_writes_queue_entry_with_issue_details(self, tmp_path):
        # Set up issue in target_dir
        target_dir = tmp_path / "repo"
        target_dir.mkdir()
        create_issue(target_dir, {
            "id": 42,
            "source": "dep-audit",
            "type": "bug",
            "status": "open",
            "repo": "org/repo",
        }, body="Fix the crash in foo()")

        # Set up queue
        queue_dir = tmp_path / "queue"

        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "callable": "cronpypeline.plugins.swe_prompts.queue_coder_agent",
                "issue_id": 42,
                "agent": "CoderAgent",
                "queue_dir": str(queue_dir),
                "prompt_field": "content",
                "default_fields": {
                    "sender": "SWE_PIPELINE",
                    "folder_name": "SWE",
                    "runs_left": 3,
                },
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)

        # Mock git to return a sha
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="abc1234567\n")
            result = queue_coder_agent(action, ctx)

        assert result.success is True
        entry = json.loads(Path(result.data["queue_file"]).read_text())
        assert entry["agent"] == "CoderAgent"
        assert "Fix the crash in foo()" in entry["content"]
        assert "42" in entry["content"]
        assert entry["sender"] == "SWE_PIPELINE"

    def test_missing_issue_returns_error(self, tmp_path):
        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "issue_id": 999,
                "agent": "CoderAgent",
                "queue_dir": str(tmp_path / "queue"),
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)

        result = queue_coder_agent(action, ctx)
        assert result.success is False


class TestQueueReviewAgent:
    """Tests for queue_review_agent custom action callable."""

    def test_writes_queue_entry_with_review_prompt(self, tmp_path):
        queue_dir = tmp_path / "queue"

        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "callable": "cronpypeline.plugins.swe_prompts.queue_review_agent",
                "agent": "ReviewAgent",
                "queue_dir": str(queue_dir),
                "prompt_field": "content",
                "default_fields": {
                    "sender": "SWE_PIPELINE",
                    "folder_name": "SWE",
                    "runs_left": 3,
                },
                "cycle_number": 2,
                "pr_number": 42,
                "pr_url": "https://github.com/org/repo/pull/42",
            },
        )
        ctx = TickContext(target="org/repo", workspace_dir=tmp_path, dry_run=False)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="abc1234567\n")
            result = queue_review_agent(action, ctx)

        assert result.success is True
        entry = json.loads(Path(result.data["queue_file"]).read_text())
        assert entry["agent"] == "ReviewAgent"
        assert "2" in entry["content"]  # cycle number
        assert "42" in entry["content"]  # PR number
        assert "github.com/org/repo/pull/42" in entry["content"]

    def test_dry_run_does_not_write(self, tmp_path):
        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "agent": "ReviewAgent",
                "queue_dir": str(tmp_path / "queue"),
                "cycle_number": 1,
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=True)

        result = queue_review_agent(action, ctx)
        assert result.success is True
        assert result.dry_run is True
