"""Tests for SWE prompt builder custom action callables."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cronpypeline.actions import ActionResult, TickContext
from cronpypeline.config import ActionSpec, ActionType
from cronpypeline.plugins.issue_store import create_issue, get_issue
from cronpypeline.plugins.swe_prompts import (
    _build_queue_handler,
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
        assert result.data.get("async") is True

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

    def test_extra_instructions_added_to_prompt(self, tmp_path):
        report_path = tmp_path / "report.md"
        report_path.write_text("FAIL report")

        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "report_path": str(report_path),
                "agent": "FixAgent",
                "queue_dir": str(tmp_path / "queue"),
                "extra_instructions": "Do it carefully.",
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)

        result = queue_fix_agent(action, ctx)
        assert result.success is True
        entry = json.loads(Path(result.data["queue_file"]).read_text())
        assert "## Additional Instructions" in entry["prompt"]
        assert "Do it carefully." in entry["prompt"]

    def test_verify_commands_added_to_prompt(self, tmp_path):
        report_path = tmp_path / "report.md"
        report_path.write_text("FAIL report")

        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "report_path": str(report_path),
                "agent": "FixAgent",
                "queue_dir": str(tmp_path / "queue"),
                "verify_commands": ["pytest -q", "ruff check ."],
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)

        result = queue_fix_agent(action, ctx)
        assert result.success is True
        entry = json.loads(Path(result.data["queue_file"]).read_text())
        assert "Run exactly:" in entry["prompt"]
        assert "cd" in entry["prompt"]
        assert "pytest -q" in entry["prompt"]
        assert "ruff check ." in entry["prompt"]

    def test_dedup_marker_written_on_success(self, tmp_path):
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
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)

        result = queue_fix_agent(action, ctx)
        assert result.success is True

        marker = ctx.target_dir / ".SWE" / "markers" / f"queued_for_{report_path.stem}.marker"
        assert marker.exists()
        content = marker.read_text()
        assert "report.md" in content

    def test_dedup_marker_not_written_on_queue_failure(self, tmp_path):
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
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)

        mock_handler = MagicMock()
        mock_handler.execute.return_value = ActionResult(success=False, stderr="queue failed")

        with patch("cronpypeline.plugins.swe_prompts._build_queue_handler", return_value=mock_handler):
            result = queue_fix_agent(action, ctx)

        assert result.success is False
        marker = ctx.target_dir / ".SWE" / "markers" / f"queued_for_{report_path.stem}.marker"
        assert not marker.exists()

    def test_dedup_marker_not_written_when_execute_raises(self, tmp_path):
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
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)

        mock_handler = MagicMock()
        mock_handler.execute.side_effect = ValueError("boom")

        with (
            patch("cronpypeline.plugins.swe_prompts._build_queue_handler", return_value=mock_handler),
            pytest.raises(ValueError, match="boom"),
        ):
            queue_fix_agent(action, ctx)

        marker = ctx.target_dir / ".SWE" / "markers" / f"queued_for_{report_path.stem}.marker"
        assert not marker.exists()

    def test_dedup_marker_write_failure_returns_error_and_removes_queue_entry(self, tmp_path):
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
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)

        queue_file = tmp_path / "queue" / "FixAgent_20240101_120000.json"

        def mock_execute(queue_action, context):
            # Actually write the queue file so the fallback path of the
            # patched write_text is exercised for non-marker writes.
            queue_file.parent.mkdir(parents=True, exist_ok=True)
            queue_file.write_text(json.dumps({"agent": "FixAgent"}))
            return ActionResult(
                success=True,
                data={"queue_file": str(queue_file)},
            )

        mock_handler = MagicMock()
        mock_handler.execute.side_effect = mock_execute

        real_write_text = Path.write_text

        def failing_write_text(self, data, encoding=None):
            if self == ctx.target_dir / ".SWE" / "markers" / f"queued_for_{report_path.stem}.marker":
                raise OSError("disk full")
            return real_write_text(self, data, encoding=encoding)

        with (
            patch("cronpypeline.plugins.swe_prompts._build_queue_handler", return_value=mock_handler),
            patch.object(Path, "write_text", new=failing_write_text),
        ):
            result = queue_fix_agent(action, ctx)

        assert result.success is False
        assert "dedup marker" in result.stderr
        assert "disk full" in result.stderr
        assert not queue_file.exists()

    def test_dedup_marker_write_failure_returns_error_when_cleanup_fails(self, tmp_path):
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
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)

        queue_file = tmp_path / "queue" / "FixAgent_20240101_120000.json"

        def mock_execute(queue_action, context):
            # Actually write the queue file so the fallback path of the
            # patched write_text is exercised for non-marker writes.
            queue_file.parent.mkdir(parents=True, exist_ok=True)
            queue_file.write_text(json.dumps({"agent": "FixAgent"}))
            return ActionResult(
                success=True,
                data={"queue_file": str(queue_file)},
            )

        mock_handler = MagicMock()
        mock_handler.execute.side_effect = mock_execute

        real_write_text = Path.write_text

        def failing_write_text(self, data, encoding=None):
            if self == ctx.target_dir / ".SWE" / "markers" / f"queued_for_{report_path.stem}.marker":
                raise OSError("disk full")
            return real_write_text(self, data, encoding=encoding)

        with (
            patch("cronpypeline.plugins.swe_prompts._build_queue_handler", return_value=mock_handler),
            patch.object(Path, "write_text", new=failing_write_text),
            patch.object(Path, "unlink", side_effect=OSError("unlink failed")),
        ):
            result = queue_fix_agent(action, ctx)

        assert result.success is False
        assert "dedup marker" in result.stderr
        assert "disk full" in result.stderr


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
        assert result.data.get("async") is True
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

    def test_issue_body_with_unescaped_braces(self, tmp_path):
        target_dir = tmp_path / "repo"
        target_dir.mkdir()
        create_issue(target_dir, {
            "id": 42,
            "source": "dep-audit",
            "type": "bug",
            "status": "open",
            "repo": "org/repo",
        }, body="The bug occurs when using {'key': 'value'}")

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

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="abc1234567\n")
            result = queue_coder_agent(action, ctx)

        assert result.success is True
        entry = json.loads(Path(result.data["queue_file"]).read_text())
        assert "The bug occurs when using {'key': 'value'}" in entry["content"]


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
        assert result.data.get("async") is True
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


class TestBuildFixPromptExtraInstructions:
    """Tests for build_fix_prompt with extra_instructions."""

    def test_prompt_with_extra_instructions(self):
        prompt = build_fix_prompt(
            report_content="# Report\nFAIL",
            report_name="lint",
            target="repo",
            extra_instructions="Also fix the tests",
        )
        assert "Also fix the tests" in prompt
        assert "Additional Instructions" in prompt


class TestBuildCoderPromptExtraFields:
    """Tests for build_coder_prompt with extra fields."""

    def test_prompt_with_github_url(self, tmp_path):
        create_issue(tmp_path, {
            "id": 42,
            "source": "review",
            "type": "bug",
            "status": "open",
            "repo": "org/repo",
            "github_url": "https://github.com/org/repo/issues/42",
        }, body="Fix the bug")

        issue = get_issue(tmp_path, 42)
        prompt = build_coder_prompt(
            issue=issue,
            target="org/repo",
            integration_sha="abc123",
            extra_instructions="Use best practices",
        )
        assert "https://github.com/org/repo/issues/42" in prompt
        assert "abc123" in prompt
        assert "Use best practices" in prompt
        assert "Additional Instructions" in prompt


class TestBuildReviewPromptExtraFields:
    """Tests for build_review_prompt with extra fields."""

    def test_prompt_with_integration_sha_and_diff_stats(self):
        prompt = build_review_prompt(
            target="org/repo",
            cycle_number=1,
            diff_stats="5 files changed",
            integration_sha="abc12345",
            extra_instructions="Be thorough",
        )
        assert "abc12345" in prompt
        assert "5 files changed" in prompt
        assert "Be thorough" in prompt
        assert "Additional Instructions" in prompt


class TestGetIntegrationShaFailure:
    """Tests for _get_integration_sha error handling."""

    def test_timeout_returns_empty(self, tmp_path):
        from cronpypeline.plugins.swe_prompts import _get_integration_sha
        with patch("subprocess.run", side_effect=__import__("subprocess").TimeoutExpired(cmd="git", timeout=10)):
            result = _get_integration_sha(tmp_path)
        assert result == ""

    def test_filenotfound_returns_empty(self, tmp_path):
        from cronpypeline.plugins.swe_prompts import _get_integration_sha
        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            result = _get_integration_sha(tmp_path)
        assert result == ""

    def test_git_not_found_returns_empty(self, tmp_path):
        from cronpypeline.plugins.swe_prompts import _get_integration_sha
        with patch("shutil.which", return_value=None):
            result = _get_integration_sha(tmp_path)
        assert result == ""


class TestGetDiffStatsFailure:
    """Tests for _get_diff_stats error handling."""

    def test_timeout_returns_empty(self, tmp_path):
        from cronpypeline.plugins.swe_prompts import _get_diff_stats
        with patch("subprocess.run", side_effect=__import__("subprocess").TimeoutExpired(cmd="git", timeout=10)):
            result = _get_diff_stats(tmp_path)
        assert result == ""

    def test_filenotfound_returns_empty(self, tmp_path):
        from cronpypeline.plugins.swe_prompts import _get_diff_stats
        with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
            result = _get_diff_stats(tmp_path)
        assert result == ""

    def test_git_not_found_returns_empty(self, tmp_path):
        from cronpypeline.plugins.swe_prompts import _get_diff_stats
        with patch("shutil.which", return_value=None):
            result = _get_diff_stats(tmp_path)
        assert result == ""


class TestQueueCoderAgentDryRun:
    """Tests for queue_coder_agent dry run."""

    def test_dry_run_does_not_write(self, tmp_path):
        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "issue_id": 42,
                "agent": "CoderAgent",
                "queue_dir": str(tmp_path / "queue"),
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=True)

        result = queue_coder_agent(action, ctx)
        assert result.success is True
        assert result.dry_run is True


class TestParseChangeRequests:
    """Tests for _parse_change_requests — parses review body for change requests."""

    def test_issues_and_concerns_bold_numbered(self):
        from cronpypeline.plugins.swe_prompts import _parse_change_requests
        body = (
            "## Review\n\n"
            "### Issues & Concerns\n\n"
            "**1. Fix the bug**\nDetails about the bug\n\n"
            "**2. Add tests**\nNeed more test coverage\n\n"
            "No other issues found.\n"
        )
        result = _parse_change_requests(body)
        assert len(result) == 2
        assert "Fix the bug" in result[0]
        assert "Add tests" in result[1]

    def test_issues_and_concerns_heading_numbered(self):
        from cronpypeline.plugins.swe_prompts import _parse_change_requests
        body = (
            "## Review\n\n"
            "### Issues & Concerns\n\n"
            "#### 1. Fix the bug\nDetails about the bug\n\n"
            "#### 2. Add tests\nNeed more test coverage\n"
        )
        result = _parse_change_requests(body)
        assert len(result) >= 1
        assert "Fix the bug" in result[0]

    def test_issues_and_concerns_plain_numbered(self):
        from cronpypeline.plugins.swe_prompts import _parse_change_requests
        body = (
            "## Review\n\n"
            "### Issues & Concerns\n\n"
            "1. Fix the bug\nDetails about the bug\n\n"
            "2. Add tests\nNeed more test coverage\n"
        )
        result = _parse_change_requests(body)
        assert len(result) == 2
        assert "Fix the bug" in result[0]
        assert "Add tests" in result[1]

    def test_generic_numbered_list(self):
        from cronpypeline.plugins.swe_prompts import _parse_change_requests
        body = (
            "Please address the following:\n\n"
            "1. Fix the bug\n"
            "2. Add tests\n"
            "3. Update docs\n"
        )
        result = _parse_change_requests(body)
        assert len(result) == 3
        assert "Fix the bug" in result[0]
        assert "Add tests" in result[1]
        assert "Update docs" in result[2]

    def test_bullet_points(self):
        from cronpypeline.plugins.swe_prompts import _parse_change_requests
        body = (
            "Please address:\n"
            "- Fix the bug\n"
            "- Add tests\n"
            "* Update docs\n"
        )
        result = _parse_change_requests(body)
        assert len(result) == 3
        assert "Fix the bug" in result[0]
        assert "Add tests" in result[1]

    def test_last_resort_paragraphs(self):
        from cronpypeline.plugins.swe_prompts import _parse_change_requests
        body = "Fix the bug in foo.\n\nAdd tests for bar.\n\nUpdate the docs."
        result = _parse_change_requests(body)
        assert len(result) == 3
        assert "Fix the bug" in result[0]
        assert "Add tests" in result[1]

    def test_filters_no_issues_found(self):
        from cronpypeline.plugins.swe_prompts import _parse_change_requests
        body = "No issues found.\n\nNo issues identified."
        result = _parse_change_requests(body)
        assert len(result) == 0

    def test_filters_headings(self):
        from cronpypeline.plugins.swe_prompts import _parse_change_requests
        body = "## Review\n\nSome heading\n\n1. Actual issue\n"
        result = _parse_change_requests(body)
        # The heading "## Review" should be filtered, only "Actual issue" remains
        assert all(not item.startswith("#") for item in result)

    def test_truncates_long_items(self):
        from cronpypeline.plugins.swe_prompts import _parse_change_requests
        long_text = "A" * 300
        body = f"{long_text}\n\nShort item"
        result = _parse_change_requests(body)
        # Long items should be truncated to 200 chars
        for item in result:
            assert len(item) <= 200

    def test_filters_empty_items(self):
        from cronpypeline.plugins.swe_prompts import _parse_change_requests
        body = "1. \n\n2. Real issue\n"
        result = _parse_change_requests(body)
        assert all(item.strip() for item in result)

    def test_empty_body(self):
        from cronpypeline.plugins.swe_prompts import _parse_change_requests
        result = _parse_change_requests("")
        assert result == []

    def test_no_issues_section_no_numbered_list(self):
        from cronpypeline.plugins.swe_prompts import _parse_change_requests
        body = "Just some random text without any structure."
        result = _parse_change_requests(body)
        # Should fall through to last-resort paragraphs
        assert len(result) == 1
        assert "random text" in result[0]


class TestQueueFixAgentSymlinkOSError:
    """Tests for queue_fix_agent with broken symlink."""

    def test_broken_symlink_falls_back_to_raw_path(self, tmp_path):
        report_dir = tmp_path / ".SWE" / "reports" / "lint"
        report_dir.mkdir(parents=True)
        report_path = report_dir / "r.md"
        report_path.write_text("# Lint — FAIL\n\n3 errors")
        latest = report_dir / "latest.md"
        latest.symlink_to("r.md")

        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "report_path": str(latest),
                "agent": "FixAgent",
                "queue_dir": str(tmp_path / "queue"),
                "prompt_field": "content",
                "default_fields": {
                    "sender": "SWE_PIPELINE",
                    "runs_left": 3,
                },
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)

        result = queue_fix_agent(action, ctx)
        assert result.success is True


class TestQueueFixAgentInvalidatePaths:
    """Tests for queue_fix_agent with invalidate_paths and completion_marker."""

    def test_prompt_with_invalidate_paths(self, tmp_path):
        report_dir = tmp_path / ".SWE" / "reports" / "lint"
        report_dir.mkdir(parents=True)
        report_path = report_dir / "r.md"
        report_path.write_text("# Lint — FAIL\n\n3 errors")

        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "report_path": str(report_path),
                "agent": "FixAgent",
                "queue_dir": str(tmp_path / "queue"),
                "prompt_field": "content",
                "default_fields": {
                    "sender": "SWE_PIPELINE",
                    "runs_left": 3,
                },
                "invalidate_paths": [".SWE/reports/lint/latest.md", ".SWE/reports/typecheck/latest.md"],
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)

        result = queue_fix_agent(action, ctx)
        assert result.success is True
        entry = json.loads(Path(result.data["queue_file"]).read_text())
        assert "rm -f" in entry["content"]
        assert ".SWE/reports/lint/latest.md" in entry["content"]

    def test_prompt_with_completion_marker(self, tmp_path):
        report_dir = tmp_path / ".SWE" / "reports" / "lint"
        report_dir.mkdir(parents=True)
        report_path = report_dir / "r.md"
        report_path.write_text("# Lint — FAIL\n\n3 errors")

        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "report_path": str(report_path),
                "agent": "FixAgent",
                "queue_dir": str(tmp_path / "queue"),
                "prompt_field": "content",
                "default_fields": {
                    "sender": "SWE_PIPELINE",
                    "runs_left": 3,
                },
                "completion_marker": ".SWE/coding_complete.marker",
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)

        result = queue_fix_agent(action, ctx)
        assert result.success is True
        entry = json.loads(Path(result.data["queue_file"]).read_text())
        assert "completion marker" in entry["content"].lower()
        assert ".SWE/coding_complete.marker" in entry["content"]

    def test_prompt_with_both_invalidate_and_completion(self, tmp_path):
        report_dir = tmp_path / ".SWE" / "reports" / "lint"
        report_dir.mkdir(parents=True)
        report_path = report_dir / "r.md"
        report_path.write_text("# Lint — FAIL\n\n3 errors")

        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "report_path": str(report_path),
                "agent": "FixAgent",
                "queue_dir": str(tmp_path / "queue"),
                "prompt_field": "content",
                "default_fields": {
                    "sender": "SWE_PIPELINE",
                    "runs_left": 3,
                },
                "invalidate_paths": [".SWE/reports/lint/latest.md"],
                "completion_marker": ".SWE/coding_complete.marker",
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)

        result = queue_fix_agent(action, ctx)
        assert result.success is True
        entry = json.loads(Path(result.data["queue_file"]).read_text())
        assert "rm -f" in entry["content"]
        assert "completion marker" in entry["content"].lower()


class TestParseChangeRequestsEdgeCases:
    """Tests for _parse_change_requests filtering edge cases."""

    def test_filters_heading_items(self):
        from cronpypeline.plugins.swe_prompts import _parse_change_requests
        # Body with a heading that gets captured as an item, then filtered
        body = "## Some Heading\n\n1. Real issue here\n"
        result = _parse_change_requests(body)
        # Headings starting with # should be filtered out
        assert all(not item.startswith("#") for item in result)

    def test_filters_empty_text_after_strip(self):
        from cronpypeline.plugins.swe_prompts import _parse_change_requests
        # Body with numbered items that have only whitespace content
        body = "1.   \n\n2.   \n\n3. Real issue\n"
        result = _parse_change_requests(body)
        # Empty items should be filtered
        assert all(item.strip() for item in result)
        assert len(result) >= 1

    def test_heading_filtered_from_last_resort(self):
        from cronpypeline.plugins.swe_prompts import _parse_change_requests
        # Body with only a heading - falls to last-resort, then filtered by startswith("#")
        body = "## Some Heading\n\nReal issue here"
        result = _parse_change_requests(body)
        # The heading should be filtered out
        assert all(not item.startswith("#") for item in result)
        assert "Real issue here" in result

    def test_whitespace_only_item_filtered(self):
        from cronpypeline.plugins.swe_prompts import _parse_change_requests
        # Body with last-resort paragraphs where one is whitespace-only
        body = "Real issue\n\n   \n\nAnother issue"
        result = _parse_change_requests(body)
        # Whitespace-only items should be filtered
        assert all(item.strip() for item in result)

    def test_heading_in_numbered_item_filtered(self):
        from cronpypeline.plugins.swe_prompts import _parse_change_requests
        # Numbered item whose content starts with # — should be filtered by line 530
        body = "1. ## Sub-heading\n\n2. Real issue\n"
        result = _parse_change_requests(body)
        assert all(not item.startswith("#") for item in result)

    def test_whitespace_only_bullet_filtered(self):
        from cronpypeline.plugins.swe_prompts import _parse_change_requests
        # Bullet with only whitespace content — should be filtered by line 535
        body = "-   \n\n- Real issue\n"
        result = _parse_change_requests(body)
        assert all(item.strip() for item in result)


class TestQueueFixAgentBrokenSymlinkOSError:
    """Test queue_fix_agent with a broken symlink that raises OSError on resolve."""

    def test_broken_symlink_oserror_falls_back(self, tmp_path):
        report_dir = tmp_path / ".SWE" / "reports" / "lint"
        report_dir.mkdir(parents=True)
        report_path = report_dir / "r.md"
        report_path.write_text("# Lint — FAIL\n\n3 errors")
        latest = report_dir / "latest.md"
        latest.symlink_to("r.md")

        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "report_path": str(latest),
                "agent": "FixAgent",
                "queue_dir": str(tmp_path / "queue"),
                "prompt_field": "content",
                "default_fields": {
                    "sender": "SWE_PIPELINE",
                    "runs_left": 3,
                },
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)

        # Mock Path.resolve to raise OSError for the report symlink only, then
        # fall back to the raw path. Other paths (e.g. the queue file) must
        # still resolve normally so the queue handler can write its entry.
        original_resolve = Path.resolve

        def resolve_with_broken_symlink(path, *args, **kwargs):
            if path == latest:
                raise OSError("broken symlink")
            return original_resolve(path, *args, **kwargs)

        with patch.object(
            Path,
            "resolve",
            autospec=True,
            side_effect=resolve_with_broken_symlink,
        ):
            result = queue_fix_agent(action, ctx)
        assert result.success is True


class TestBuildQueueHandler:
    """Tests for _build_queue_handler fallback logic."""

    def test_uses_pipeline_action_handler_fallback(self, tmp_path):
        """Covers line 233 — fallback from pipeline.config.action_handler.params."""
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)
        mock_pipeline = MagicMock()
        mock_pipeline.config.action_handler.params = {
            "queue_dir": str(tmp_path / "fallback_queue"),
            "prompt_field": "fallback_prompt",
        }
        ctx.pipeline = mock_pipeline
        params = {"queue_dir": str(tmp_path / "override_queue")}
        handler = _build_queue_handler(params, ctx)
        assert str(handler.queue_dir) == str(tmp_path / "override_queue")
        assert handler.prompt_field == "fallback_prompt"

    def test_no_pipeline_uses_empty_fallback(self, tmp_path):
        """No pipeline set — fallback stays empty, params used directly."""
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)
        params = {"queue_dir": str(tmp_path / "q")}
        handler = _build_queue_handler(params, ctx)
        assert str(handler.queue_dir) == str(tmp_path / "q")

    def test_pipeline_none_action_handler(self, tmp_path):
        """Pipeline exists but action_handler is None — fallback stays empty."""
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)
        mock_pipeline = MagicMock()
        mock_pipeline.config.action_handler = None
        ctx.pipeline = mock_pipeline
        params = {"queue_dir": str(tmp_path / "q")}
        handler = _build_queue_handler(params, ctx)
        assert str(handler.queue_dir) == str(tmp_path / "q")

    def test_raises_when_queue_dir_missing(self, tmp_path):
        """ValueError when queue_dir is missing from params and fallback."""
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)
        with pytest.raises(ValueError, match="queue_dir is required"):
            _build_queue_handler({}, ctx)

    def test_raises_when_queue_dir_empty_string(self, tmp_path):
        """ValueError when queue_dir is an empty string."""
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)
        with pytest.raises(ValueError, match="queue_dir is required"):
            _build_queue_handler({"queue_dir": ""}, ctx)

    def test_queue_dir_from_fallback_used_when_params_missing(self, tmp_path):
        """queue_dir from fallback config is used when params don't provide it."""
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)
        mock_pipeline = MagicMock()
        mock_pipeline.config.action_handler.params = {
            "queue_dir": str(tmp_path / "fallback_queue"),
        }
        ctx.pipeline = mock_pipeline
        handler = _build_queue_handler({}, ctx)
        assert str(handler.queue_dir) == str(tmp_path / "fallback_queue")

    def test_queue_dir_from_params_takes_precedence(self, tmp_path):
        """queue_dir from params takes precedence over fallback config."""
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)
        mock_pipeline = MagicMock()
        mock_pipeline.config.action_handler.params = {
            "queue_dir": str(tmp_path / "fallback_queue"),
        }
        ctx.pipeline = mock_pipeline
        params = {"queue_dir": str(tmp_path / "override_queue")}
        handler = _build_queue_handler(params, ctx)
        assert str(handler.queue_dir) == str(tmp_path / "override_queue")
