"""Tests for SWE pipeline plugin triggers and actions.

Covers functions in cronpypeline/plugins/swe_plugin.py that are not
already tested in test_plugins.py.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from cronpypeline.actions import ActionResult, ActionSpec, ActionType, TickContext
from cronpypeline.plugins.issue_store import create_issue
from cronpypeline.plugins.swe_plugin import (
    INTEGRATION_BRANCH,
    _a1_is_pass,
    _a7_coverage_pct,
    _batch_fixed_count,
    _batch_is_full,
    _batch_marker_path,
    _build_doc_sync_prompt,
    _build_pr_body,
    _build_pr_review_prompt,
    _build_pr_title_prompt,
    _close_and_comment_github_issue,
    _compute_review_generation,
    _count_done_review_issues,
    _count_open_review_issues,
    _detect_report_fail,
    _find_active_task,
    _find_issue_by_id,
    _find_previous_review_sha,
    _gh_api_get_list,
    _gh_api_patch,
    _gh_api_post,
    _git,
    _git_issue_already_ingested,
    _git_issue_type_from_labels,
    _has_open_coverage_issues,
    _increment_batch_fixed_count,
    _issues_per_pr,
    _load_env_file,
    _load_github_token,
    _normalize_pkg_name,
    _open_issue_count,
    _ordinal_suffix,
    _parse_pip_audit_vulnerabilities,
    _parse_utc_datetime,
    _read_batch_marker,
    _read_github_session,
    _resolve_latest_report,
    _sha_is_ancestor,
    _should_block_on_open_issues,
    _slugify,
    _venv_binary,
    _write_batch_marker,
    _write_pipeline_issue,
    cleanup_git_branch,
    commit_phase_a_change,
    detect_agent_forgot_marker,
    detect_b1_issue_gathering,
    detect_c_coverage_issue,
    detect_c_doc_sync,
    detect_c_issue_fix,
    detect_c_pr_publish,
    detect_c_pr_review,
    detect_c_pr_status,
    detect_c_pr_title,
    detect_c_review_issue,
    detect_c_review_ranking,
    detect_coverage_fail,
    detect_deadcode_trigger,
    detect_docstring_fail,
    detect_lint_autofix,
    detect_lint_fail,
    detect_open_issue,
    detect_security_fail,
    detect_session_complete,
    detect_typecheck_fail,
    detect_vulture_fail,
    ensure_phase_a_branch,
    finalize_session,
    integration_head_sha,
    reset_issue_status,
    run_a5_bandit,
    run_a6_vulture,
    run_a7_coverage,
    run_a8_radon,
    run_a9_dep_audit,
    run_b1_issue_gathering,
    run_c_coverage_issue,
    run_c_doc_sync,
    run_c_issue_fix,
    run_c_pr_publish,
    run_c_pr_review,
    run_c_pr_status,
    run_c_pr_title,
    run_c_review_issue,
    run_c_review_ranking,
    run_lint_autofix,
    select_issue,
    sync_session_mode,
)

# ─── Helpers ────────────────────────────────────────────────────────────────


def _make_target_dir(tmp_path: Path) -> Path:
    """Create a target repo directory with .SWE structure."""
    target = tmp_path / "repo"
    (target / ".SWE" / "reports").mkdir(parents=True)
    (target / ".SWE" / "issues").mkdir(parents=True)
    (target / ".SWE" / "markers").mkdir(parents=True)
    return target


def _write_report(target_dir: Path, subdir: str, name: str, content: str) -> Path:
    """Write a report file and create latest.md symlink."""
    report_dir = target_dir / ".SWE" / "reports" / subdir
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / name
    report_path.write_text(content, encoding="utf-8")
    latest = report_dir / "latest.md"
    if latest.exists() or latest.is_symlink():
        latest.unlink()
    latest.symlink_to(name)
    return report_path


def _make_tick_context(
    target_dir: Path, target: str = "repo", dry_run: bool = False,
    **config_kwargs: Any,
) -> TickContext:
    """Create a TickContext for testing.

    target_dir must be workspace_dir / target (the convention for TickContext).
    """
    return TickContext(
        target=target,
        workspace_dir=target_dir.parent,
        dry_run=dry_run,
        target_config=config_kwargs,
    )


def _long_frontmatter(extra_fields: dict[str, str], pad_key: str = "padding") -> str:
    """Create frontmatter text where the given fields appear after 800+ chars."""
    lines = ["---"]
    # Add many padding fields to push content past 800 chars
    for i in range(50):
        lines.append(f"{pad_key}_{i}: value_{i}")
    # Add the actual fields
    for k, v in extra_fields.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines) + "\n"


# ─── detect_deadcode_trigger ────────────────────────────────────────────────


class TestDetectDeadcodeTrigger:
    def test_fires_when_report_missing(self, tmp_path):
        target = _make_target_dir(tmp_path)
        (target / ".SWE" / "repo_briefing.md").write_text("briefing")
        ctx = {"target_dir": str(target), "target_config": {}}
        assert detect_deadcode_trigger(ctx) is True

    def test_does_not_fire_when_report_exists(self, tmp_path):
        target = _make_target_dir(tmp_path)
        (target / ".SWE" / "repo_briefing.md").write_text("briefing")
        _write_report(target, "deadcode", "r.md", "# Deadcode — PASS")
        ctx = {"target_dir": str(target), "target_config": {}}
        assert detect_deadcode_trigger(ctx) is False

    def test_does_not_fire_when_skip_deadcode_set(self, tmp_path):
        target = _make_target_dir(tmp_path)
        (target / ".SWE" / "repo_briefing.md").write_text("briefing")
        ctx = {"target_dir": str(target), "target_config": {"skip_deadcode": True}}
        assert detect_deadcode_trigger(ctx) is False

    def test_does_not_fire_when_briefing_missing(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = {"target_dir": str(target), "target_config": {}}
        assert detect_deadcode_trigger(ctx) is False


# ─── _resolve_latest_report ─────────────────────────────────────────────────


class TestResolveLatestReport:
    def test_resolves_symlink(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "lint", "report.md", "# Lint — FAIL")
        result = _resolve_latest_report(target, "lint")
        assert result is not None
        assert result.name == "report.md"

    def test_returns_none_when_missing(self, tmp_path):
        target = _make_target_dir(tmp_path)
        assert _resolve_latest_report(target, "lint") is None

    def test_returns_none_when_symlink_target_missing(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_dir = target / ".SWE" / "reports" / "lint"
        report_dir.mkdir(parents=True, exist_ok=True)
        latest = report_dir / "latest.md"
        latest.symlink_to("nonexistent.md")
        assert _resolve_latest_report(target, "lint") is None

    def test_resolves_regular_file(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_dir = target / ".SWE" / "reports" / "lint"
        report_dir.mkdir(parents=True, exist_ok=True)
        latest = report_dir / "latest.md"
        latest.write_text("# Lint — FAIL")
        result = _resolve_latest_report(target, "lint")
        assert result is not None
        assert result.name == "latest.md"


# ─── detect_lint_fail ───────────────────────────────────────────────────────


class TestDetectLintFail:
    def test_fires_when_errors_and_no_fixable(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "lint", "r.md", "# Lint — FAIL\n\n- **errors**: 5\n- **fixable**: 0\n")
        ctx = {"target_dir": str(target)}
        assert detect_lint_fail(ctx) is True

    def test_does_not_fire_when_fixable_remaining(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "lint", "r.md", "# Lint — FAIL\n\n- **errors**: 5\n- **fixable**: 3\n")
        ctx = {"target_dir": str(target)}
        assert detect_lint_fail(ctx) is False

    def test_fires_when_fixable_remaining_but_autofix_attempted(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_path = _write_report(target, "lint", "r.md", "# Lint — FAIL\n\n- **errors**: 5\n- **fixable**: 3\n")
        autofix_dir = target / ".SWE" / "reports" / "lint-autofix"
        autofix_dir.mkdir(parents=True, exist_ok=True)
        (autofix_dir / f"applied_for_{report_path.stem}.marker").write_text("done")
        ctx = {"target_dir": str(target)}
        assert detect_lint_fail(ctx) is True

    def test_does_not_fire_when_no_errors(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "lint", "r.md", "# Lint — PASS\n\n- **errors**: 0\n- **fixable**: 0\n")
        ctx = {"target_dir": str(target)}
        assert detect_lint_fail(ctx) is False

    def test_does_not_fire_when_no_report(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = {"target_dir": str(target)}
        assert detect_lint_fail(ctx) is False

    def test_does_not_fire_when_dedup_marker_exists(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_path = _write_report(target, "lint", "r.md", "# Lint — FAIL\n\n- **errors**: 5\n- **fixable**: 0\n")
        marker = target / ".SWE" / "markers" / f"queued_for_{report_path.stem}.marker"
        marker.write_text("queued")
        ctx = {"target_dir": str(target)}
        assert detect_lint_fail(ctx) is False

    def test_fires_with_old_format_fallback(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "lint", "r.md", "# Lint — FAIL\n\n5 error(s), 0 auto-fixable\n")
        ctx = {"target_dir": str(target)}
        assert detect_lint_fail(ctx) is True


# ─── _detect_report_fail ────────────────────────────────────────────────────


class TestDetectReportFail:
    def test_fires_on_fail_header(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "docstrings", "r.md", "# Docstring Coverage — FAIL\n")
        assert _detect_report_fail(target, "docstrings") is True

    def test_does_not_fire_on_pass(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "docstrings", "r.md", "# Docstring Coverage — PASS\n")
        assert _detect_report_fail(target, "docstrings") is False

    def test_does_not_fire_when_no_report(self, tmp_path):
        target = _make_target_dir(tmp_path)
        assert _detect_report_fail(target, "docstrings") is False

    def test_does_not_fire_when_dedup_marker_exists(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_path = _write_report(target, "docstrings", "r.md", "# Docstring Coverage — FAIL\n")
        marker = target / ".SWE" / "markers" / f"queued_for_{report_path.stem}.marker"
        marker.write_text("queued")
        assert _detect_report_fail(target, "docstrings") is False


# ─── detect_*_fail wrappers ─────────────────────────────────────────────────


class TestDetectReportFailWrappers:
    def test_detect_docstring_fail(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "docstrings", "r.md", "# Docstring — FAIL\n")
        assert detect_docstring_fail({"target_dir": str(target)}) is True

    def test_detect_typecheck_fail(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "typecheck", "r.md", "# Typecheck — FAIL\n")
        assert detect_typecheck_fail({"target_dir": str(target)}) is True

    def test_detect_security_fail(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "security", "r.md", "# Security — FAIL\n")
        assert detect_security_fail({"target_dir": str(target)}) is True

    def test_detect_coverage_fail(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "coverage", "r.md", "# Coverage — FAIL\n")
        assert detect_coverage_fail({"target_dir": str(target)}) is True


# ─── detect_vulture_fail ────────────────────────────────────────────────────


class TestDetectVultureFail:
    def test_fires_on_fail(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "deadcode", "r.md", "# Deadcode — FAIL\n")
        assert detect_vulture_fail({"target_dir": str(target)}) is True

    def test_does_not_fire_on_pass(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "deadcode", "r.md", "# Deadcode — PASS\n")
        assert detect_vulture_fail({"target_dir": str(target)}) is False

    def test_does_not_fire_when_no_report(self, tmp_path):
        target = _make_target_dir(tmp_path)
        assert detect_vulture_fail({"target_dir": str(target)}) is False

    def test_does_not_fire_when_dedup_marker_exists(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_path = _write_report(target, "deadcode", "r.md", "# Deadcode — FAIL\n")
        marker = target / ".SWE" / "markers" / f"queued_for_{report_path.stem}.marker"
        marker.write_text("queued")
        assert detect_vulture_fail({"target_dir": str(target)}) is False


# ─── detect_session_complete ────────────────────────────────────────────────


class TestDetectSessionComplete:
    def _setup_session(self, target: Path, issue_id: str = "github-1", active: bool = True) -> None:
        session = {"active": active, "issue_id": issue_id}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))

    def _setup_issue(self, target: Path, issue_id: str, status: str = "discarded") -> None:
        issue_path = target / ".SWE" / "issues" / f"{issue_id}.md"
        issue_path.write_text(f"---\nid: {issue_id}\nstatus: {status}\n---\n# Issue\n")

    def test_fires_when_discarded_and_no_pr(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_session(target)
        self._setup_issue(target, "github-1", "discarded")
        assert detect_session_complete({"target_dir": str(target)}) is True

    def test_does_not_fire_when_no_session(self, tmp_path):
        target = _make_target_dir(tmp_path)
        assert detect_session_complete({"target_dir": str(target)}) is False

    def test_does_not_fire_when_session_inactive(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_session(target, active=False)
        assert detect_session_complete({"target_dir": str(target)}) is False

    def test_does_not_fire_when_pr_published(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_session(target)
        self._setup_issue(target, "github-1", "discarded")
        (target / ".SWE" / "pr_published.json").write_text("{}")
        assert detect_session_complete({"target_dir": str(target)}) is False

    def test_does_not_fire_when_no_issue_id(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_session(target, issue_id="")
        assert detect_session_complete({"target_dir": str(target)}) is False

    def test_does_not_fire_when_issue_file_missing(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_session(target, issue_id="github-99")
        assert detect_session_complete({"target_dir": str(target)}) is False

    def test_does_not_fire_when_issue_not_discarded(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_session(target)
        self._setup_issue(target, "github-1", "open")
        assert detect_session_complete({"target_dir": str(target)}) is False

    def test_does_not_fire_on_corrupt_session(self, tmp_path):
        target = _make_target_dir(tmp_path)
        (target / ".SWE" / "github_session.json").write_text("not json")
        assert detect_session_complete({"target_dir": str(target)}) is False

    def test_does_not_fire_on_corrupt_issue(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_session(target)
        issue_path = target / ".SWE" / "issues" / "github-1.md"
        issue_path.write_text("not frontmatter")
        assert detect_session_complete({"target_dir": str(target)}) is False


# ─── select_issue ───────────────────────────────────────────────────────────


class TestSelectIssue:
    def test_selects_first_open_issue(self, tmp_path):
        target = _make_target_dir(tmp_path)
        create_issue(target, issue_data={"id": "i1", "status": "open"}, body="# Issue 1")
        ctx = _make_tick_context(target)
        success, msg = select_issue(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert success is True
        assert "i1" in msg

    def test_returns_false_when_no_open_issues(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target)
        success, msg = select_issue(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert success is False
        assert "No open" in msg

    def test_selects_ranked_issue_first(self, tmp_path):
        target = _make_target_dir(tmp_path)
        create_issue(target, issue_data={"id": "b", "status": "open"}, body="# B")
        create_issue(target, issue_data={"id": "a", "status": "open"}, body="# A")
        markers_dir = target / ".SWE" / "markers"
        markers_dir.mkdir(parents=True, exist_ok=True)
        (markers_dir / "review_ranked.json").write_text(json.dumps({"issue_id": "a"}))
        ctx = _make_tick_context(target)
        success, msg = select_issue(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert success is True
        assert "a" in msg

    def test_corrupted_ranking_marker_falls_through(self, tmp_path):
        target = _make_target_dir(tmp_path)
        create_issue(target, issue_data={"id": "i1", "status": "open"}, body="# Issue 1")
        markers_dir = target / ".SWE" / "markers"
        markers_dir.mkdir(parents=True, exist_ok=True)
        (markers_dir / "review_ranked.json").write_text("not valid json{")
        ctx = _make_tick_context(target)
        success, msg = select_issue(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert success is True
        assert "i1" in msg


# ─── finalize_session ───────────────────────────────────────────────────────


class TestFinalizeSession:
    def test_dry_run_returns_success(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target, dry_run=True)
        result = finalize_session(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.dry_run is True

    def test_no_session_file_returns_failure(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target)
        result = finalize_session(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False

    def test_corrupt_session_returns_failure(self, tmp_path):
        target = _make_target_dir(tmp_path)
        (target / ".SWE" / "github_session.json").write_text("bad json")
        ctx = _make_tick_context(target)
        result = finalize_session(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False

    def test_finalizes_session_without_github(self, tmp_path):
        target = _make_target_dir(tmp_path)
        session = {"active": True, "issue_id": "github-1"}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        ctx = _make_tick_context(target)
        result = finalize_session(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        saved = json.loads((target / ".SWE" / "github_session.json").read_text())
        assert saved["active"] is False
        assert saved["completed"] is True

    def test_finalizes_session_with_github(self, tmp_path):
        target = _make_target_dir(tmp_path)
        session = {"active": True, "issue_id": "github-1"}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        issue_path = target / ".SWE" / "issues" / "github-1.md"
        issue_path.write_text("---\nid: github-1\nstatus: discarded\ngithub_number: 42\n---\n# Issue\n")
        ctx = _make_tick_context(target, github_token="fake", slug="owner/repo")
        with patch("cronpypeline.plugins.swe_plugin._gh_api_post"), \
             patch("cronpypeline.plugins.swe_plugin._gh_api_patch"):
            result = finalize_session(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["gh_number"] == 42


# ─── _git ───────────────────────────────────────────────────────────────────


class TestGitHelper:
    def test_git_runs_successfully(self, tmp_path):
        result = _git(tmp_path, "init")
        assert result.returncode == 0

    def test_git_raises_on_failure(self, tmp_path):
        with pytest.raises(subprocess.CalledProcessError):
            _git(tmp_path, "bad-command")

    def test_git_no_check_does_not_raise(self, tmp_path):
        result = _git(tmp_path, "bad-command", check=False)
        assert result.returncode != 0

    def test_git_raises_timeout_expired(self, tmp_path):
        with patch(
            "cronpypeline.plugins.swe_plugin.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=60),
        ), pytest.raises(subprocess.TimeoutExpired):
            _git(tmp_path, "status")

    def test_git_passes_timeout_to_subprocess_run(self, tmp_path):
        mock_result = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")
        with patch(
            "cronpypeline.plugins.swe_plugin.subprocess.run",
            return_value=mock_result,
        ) as mock_run:
            _git(tmp_path, "status", timeout=30)
        assert mock_run.call_args.kwargs["timeout"] == 30

    def test_git_decodes_bytes_output_in_timeout(self, tmp_path):
        with patch(
            "cronpypeline.plugins.swe_plugin.subprocess.run",
            side_effect=subprocess.TimeoutExpired(
                cmd=["git"], timeout=60, output=b"some output", stderr=b"some error",
            ),
        ), pytest.raises(subprocess.TimeoutExpired) as exc_info:
            _git(tmp_path, "status")
        assert exc_info.value.stdout == "some output"
        assert exc_info.value.stderr == "some error"


# ─── ensure_phase_a_branch ──────────────────────────────────────────────────


class TestEnsurePhaseABranch:
    def test_returns_false_for_non_git_repo(self, tmp_path):
        assert ensure_phase_a_branch(tmp_path) is False

    def test_creates_branch_and_gitignore(self, tmp_path):
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
            capture_output=True, check=True,
        )
        (tmp_path / "README").write_text("init")
        subprocess.run(["git", "-C", str(tmp_path), "add", "README"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], capture_output=True, check=True)
        result = ensure_phase_a_branch(tmp_path)
        assert result is True
        gitignore = (tmp_path / ".gitignore").read_text()
        assert ".SWE/" in gitignore

    def test_idempotent_when_already_on_branch(self, tmp_path):
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], capture_output=True, check=True)
        (tmp_path / "README").write_text("init")
        subprocess.run(["git", "-C", str(tmp_path), "add", "README"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], capture_output=True, check=True)
        ensure_phase_a_branch(tmp_path)
        # Second call should be idempotent
        result = ensure_phase_a_branch(tmp_path)
        assert result is True

    def test_gitignore_already_has_swe(self, tmp_path):
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], capture_output=True, check=True)
        (tmp_path / ".gitignore").write_text(".SWE/\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", ".gitignore"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], capture_output=True, check=True)
        result = ensure_phase_a_branch(tmp_path)
        assert result is True

    def test_returns_false_on_timeout(self, tmp_path):
        with patch(
            "cronpypeline.plugins.swe_plugin._git",
            side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=60),
        ):
            assert ensure_phase_a_branch(tmp_path) is False


# ─── commit_phase_a_change ──────────────────────────────────────────────────


class TestCommitPhaseAChange:
    def _init_repo(self, path: Path) -> None:
        subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], capture_output=True, check=True)
        (path / "file.txt").write_text("hello")
        subprocess.run(["git", "-C", str(path), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], capture_output=True, check=True)

    def test_commits_changes(self, tmp_path):
        self._init_repo(tmp_path)
        (tmp_path / "new.txt").write_text("new")
        sha = commit_phase_a_change(tmp_path, "test: add new file")
        assert sha is not None
        log = subprocess.run(
            ["git", "-C", str(tmp_path), "log", "--oneline", "-1"],
            capture_output=True, text=True, check=True,
        )
        assert "test: add new file" in log.stdout

    def test_returns_none_when_nothing_to_commit(self, tmp_path):
        self._init_repo(tmp_path)
        sha = commit_phase_a_change(tmp_path, "nothing")
        assert sha is None

    def test_commits_specific_paths(self, tmp_path):
        self._init_repo(tmp_path)
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        sha = commit_phase_a_change(tmp_path, "test: add a", paths=["a.txt"])
        assert sha is not None
        staged = subprocess.run(
            ["git", "-C", str(tmp_path), "diff", "--name-only"],
            capture_output=True, text=True, check=True,
        )
        # b.txt should still be uncommitted
        assert "b.txt" in staged.stdout or (tmp_path / "b.txt").exists()

    def test_returns_none_on_timeout(self, tmp_path):
        with patch(
            "cronpypeline.plugins.swe_plugin._git",
            side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=60),
        ):
            assert commit_phase_a_change(tmp_path, "timeout") is None

    def test_commit_passes_timeout_to_subprocess_run(self, tmp_path):
        self._init_repo(tmp_path)
        (tmp_path / "new.txt").write_text("new")

        def fake_run(args, **kwargs):
            if "diff" in args:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="new.txt", stderr="")
            if "rev-parse" in args:
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="abc123\n", stderr="")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with patch(
            "cronpypeline.plugins.swe_plugin.subprocess.run",
            side_effect=fake_run,
        ) as mock_run:
            commit_phase_a_change(tmp_path, "test: commit with timeout")

        commit_calls = [c for c in mock_run.call_args_list if "commit" in c.args[0]]
        assert commit_calls, "no git commit subprocess.run call captured"
        assert commit_calls[-1].kwargs["timeout"] == 60


# ─── detect_lint_autofix ────────────────────────────────────────────────────


class TestDetectLintAutofix:
    def test_fires_when_fixable_errors(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "lint", "r.md", "# Lint — FAIL\n\n- **fixable**: 3\n")
        assert detect_lint_autofix({"target_dir": str(target)}) is True

    def test_does_not_fire_when_no_fixable(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "lint", "r.md", "# Lint — FAIL\n\n- **fixable**: 0\n")
        assert detect_lint_autofix({"target_dir": str(target)}) is False

    def test_does_not_fire_when_no_report(self, tmp_path):
        target = _make_target_dir(tmp_path)
        assert detect_lint_autofix({"target_dir": str(target)}) is False

    def test_does_not_fire_when_marker_exists(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_path = _write_report(target, "lint", "r.md", "# Lint — FAIL\n\n- **fixable**: 3\n")
        autofix_dir = target / ".SWE" / "reports" / "lint-autofix"
        autofix_dir.mkdir(parents=True, exist_ok=True)
        (autofix_dir / f"applied_for_{report_path.stem}.marker").write_text("done")
        assert detect_lint_autofix({"target_dir": str(target)}) is False

    def test_fires_with_old_format_fallback(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "lint", "r.md", "# Lint — FAIL\n\n3 auto-fixable\n")
        assert detect_lint_autofix({"target_dir": str(target)}) is True


# ─── run_lint_autofix ───────────────────────────────────────────────────────


class TestRunLintAutofix:
    def test_dry_run_returns_success(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target, dry_run=True)
        result = run_lint_autofix(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.dry_run is True

    def test_no_report_returns_failure(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target)
        result = run_lint_autofix(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False

    def test_runs_autofix_no_fixes_returns_success(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "lint", "r.md", "# Lint — FAIL\n\n- **fixable**: 0\n")
        # Init git so ensure_phase_a_branch works
        subprocess.run(["git", "init", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        ctx = _make_tick_context(target)
        action = ActionSpec(type=ActionType.CUSTOM, params={"command": "echo 'no fixes'"})
        result = run_lint_autofix(action, ctx)
        assert result.success is True  # no fixes, but not a failure
        assert result.data["fixed_count"] == 0
        # Check report was written
        autofix_dir = target / ".SWE" / "reports" / "lint-autofix"
        assert autofix_dir.exists()
        # Check marker was written
        markers = list(autofix_dir.glob("applied_for_*.marker"))
        assert len(markers) == 1
        # A2 latest.md should NOT be deleted when no fixes applied
        assert (target / ".SWE" / "reports" / "lint" / "latest.md").exists()

    def test_parses_fixed_count(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "lint", "r.md", "# Lint — FAIL\n\n- **fixable**: 0\n")
        subprocess.run(["git", "init", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        ctx = _make_tick_context(target)
        action = ActionSpec(type=ActionType.CUSTOM, params={"command": "echo '(2 fixed)'"})
        result = run_lint_autofix(action, ctx)
        assert result.success is True
        assert result.data["fixed_count"] == 2

    def test_custom_command_stdout_captured(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "lint", "r.md", "# Lint — FAIL\n\n- **fixable**: 0\n")
        subprocess.run(["git", "init", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        ctx = _make_tick_context(target)
        action = ActionSpec(type=ActionType.CUSTOM, params={"command": "echo marker-xyz"})
        result = run_lint_autofix(action, ctx)
        assert result.success is True
        latest = (target / ".SWE" / "reports" / "lint-autofix" / "latest.md").read_text()
        assert "marker-xyz" in latest

    def test_invalid_command_returns_failure(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "lint", "r.md", "# Lint — FAIL\n\n- **fixable**: 0\n")
        ctx = _make_tick_context(target)
        action = ActionSpec(type=ActionType.CUSTOM, params={"command": 'echo "unterminated'})
        result = run_lint_autofix(action, ctx)
        assert result.success is False
        assert "Invalid command" in result.stderr

    def test_empty_command_returns_failure(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "lint", "r.md", "# Lint — FAIL\n\n- **fixable**: 0\n")
        ctx = _make_tick_context(target)
        action = ActionSpec(type=ActionType.CUSTOM, params={"command": ""})
        result = run_lint_autofix(action, ctx)
        assert result.success is False
        assert "Empty command string" in result.stderr

    def test_whitespace_command_returns_failure(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "lint", "r.md", "# Lint — FAIL\n\n- **fixable**: 0\n")
        ctx = _make_tick_context(target)
        action = ActionSpec(type=ActionType.CUSTOM, params={"command": "   "})
        result = run_lint_autofix(action, ctx)
        assert result.success is False
        assert "Empty command string" in result.stderr

    def test_file_not_found_returns_failure(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "lint", "r.md", "# Lint — FAIL\n\n- **fixable**: 0\n")
        subprocess.run(["git", "init", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        ctx = _make_tick_context(target)
        action = ActionSpec(type=ActionType.CUSTOM, params={"command": "nonexistent-cmd"})
        real_run = subprocess.run

        def _mock_run(*args, **kwargs):
            if args and args[0] == ["nonexistent-cmd"]:
                raise FileNotFoundError()
            return real_run(*args, **kwargs)

        with patch("cronpypeline.plugins.swe_plugin.subprocess.run", side_effect=_mock_run):
            result = run_lint_autofix(action, ctx)
        assert result.success is False
        assert result.stderr == "Command not found: nonexistent-cmd"


# ─── _load_github_token ─────────────────────────────────────────────────────


class TestLoadGithubToken:
    def test_returns_token_from_config(self):
        assert _load_github_token({"github_token": "cfg-token"}) == "cfg-token"

    def test_returns_token_from_swe_env(self, monkeypatch):
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "swe-token")
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert _load_github_token({}) == "swe-token"

    def test_returns_token_from_github_env(self, monkeypatch):
        monkeypatch.delenv("SWE_GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
        assert _load_github_token({}) == "gh-token"

    def test_returns_none_when_no_token(self, monkeypatch):
        monkeypatch.delenv("SWE_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin._load_env_file", lambda path: None)
        assert _load_github_token({}) is None

    def test_config_takes_priority_over_env(self, monkeypatch):
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "env-token")
        assert _load_github_token({"github_token": "cfg-token"}) == "cfg-token"


# ─── _gh_api_get_list ───────────────────────────────────────────────────────


class TestGhApiGetList:
    def test_returns_list_on_success(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'[{"id": 1}]'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", return_value=mock_resp):
            result = _gh_api_get_list("owner", "repo", "issues", "token")
        assert result == [{"id": 1}]

    def test_returns_none_on_non_list_response(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"id": 1}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", return_value=mock_resp):
            result = _gh_api_get_list("owner", "repo", "issues", "token")
        assert result is None

    def test_returns_none_on_http_error(self):
        from urllib.error import HTTPError
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=HTTPError("url", 404, "Not Found", {}, None)):
            result = _gh_api_get_list("owner", "repo", "issues", "token")
        assert result is None

    def test_returns_none_on_url_error(self):
        from urllib.error import URLError
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=URLError("conn refused")):
            result = _gh_api_get_list("owner", "repo", "issues", "token")
        assert result is None

    def test_passes_params_in_url(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'[]'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", return_value=mock_resp) as mock_open:
            _gh_api_get_list("owner", "repo", "issues", "token", params={"state": "open"})
        url = mock_open.call_args[0][0].get_full_url()
        assert "state=open" in url


# ─── _gh_api_post ───────────────────────────────────────────────────────────


class TestGhApiPost:
    def test_returns_dict_on_success(self):
        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_resp.read.return_value = b'{"number": 42}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", return_value=mock_resp):
            result = _gh_api_post("owner", "repo", "issues", {"title": "test"}, "token")
        assert result == {"number": 42}

    def test_returns_none_on_unexpected_status(self):
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_resp.read.return_value = b'{"error": "server"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", return_value=mock_resp):
            result = _gh_api_post("owner", "repo", "issues", {"title": "test"}, "token")
        assert result is None

    def test_returns_none_on_http_error(self):
        from urllib.error import HTTPError
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=HTTPError("url", 403, "Forbidden", {}, None)):
            result = _gh_api_post("owner", "repo", "issues", {}, "token")
        assert result is None


# ─── _gh_api_patch ──────────────────────────────────────────────────────────


class TestGhApiPatch:
    def test_returns_dict_on_success(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"state": "closed"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", return_value=mock_resp):
            result = _gh_api_patch("owner", "repo", "issues/1", {"state": "closed"}, "token")
        assert result == {"state": "closed"}

    def test_returns_none_on_http_error(self):
        from urllib.error import HTTPError
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=HTTPError("url", 404, "Not Found", {}, None)):
            result = _gh_api_patch("owner", "repo", "issues/1", {}, "token")
        assert result is None


# ─── _NoRedirectHandler / _GH_OPENER ────────────────────────────────────────


class TestGhApiRedirectProtection:
    def test_no_redirect_handler_returns_none(self):
        from cronpypeline.plugins import swe_plugin

        handler = swe_plugin.NoRedirectHandler()
        assert handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/") is None

    def test_gh_opener_used_instead_of_urlopen(self):
        from cronpypeline.plugins import swe_plugin

        mock_resp = MagicMock()
        mock_resp.read.return_value = b"[]"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch.object(swe_plugin._GH_OPENER, "open", return_value=mock_resp) as mock_open, \
             patch("urllib.request.urlopen") as mock_urlopen:
            result = swe_plugin._gh_api_get_list("owner", "repo", "issues", "token")
        assert result == []
        mock_open.assert_called_once()
        mock_urlopen.assert_not_called()

    def test_redirect_does_not_leak_token_on_get(self):
        from urllib.error import HTTPError

        from cronpypeline.plugins import swe_plugin

        evil_url = "https://evil.example/steal-token"
        original_url = "https://api.github.com/repos/owner/repo/issues"
        redirect = HTTPError(original_url, 302, "Found", {"Location": evil_url}, None)

        with patch.object(swe_plugin._GH_OPENER, "open", side_effect=redirect) as mock_open:
            result = swe_plugin._gh_api_get_list("owner", "repo", "issues", "secret-token")

        assert result is None
        mock_open.assert_called_once()

        req = mock_open.call_args.args[0]
        assert req.headers.get("Authorization") == "Bearer secret-token"
        assert req.full_url == original_url
        assert evil_url not in req.full_url

    def test_redirect_does_not_leak_token_on_post(self):
        from urllib.error import HTTPError

        from cronpypeline.plugins import swe_plugin

        evil_url = "https://evil.example/steal-token"
        original_url = "https://api.github.com/repos/owner/repo/issues"
        redirect = HTTPError(original_url, 302, "Found", {"Location": evil_url}, None)

        with patch.object(swe_plugin._GH_OPENER, "open", side_effect=redirect) as mock_open:
            result = swe_plugin._gh_api_post(
                "owner", "repo", "issues", {"title": "test"}, "secret-token"
            )

        assert result is None
        mock_open.assert_called_once()

        req = mock_open.call_args.args[0]
        assert req.headers.get("Authorization") == "Bearer secret-token"
        assert req.full_url == original_url
        assert evil_url not in req.full_url


# ─── _read_github_session ───────────────────────────────────────────────────


class TestReadGithubSession:
    def test_returns_session_dict(self, tmp_path):
        target = _make_target_dir(tmp_path)
        session = {"active": True, "issue_id": "github-1"}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        result = _read_github_session(target)
        assert result == session

    def test_returns_none_when_no_file(self, tmp_path):
        target = _make_target_dir(tmp_path)
        assert _read_github_session(target) is None

    def test_returns_none_on_corrupt_json(self, tmp_path):
        target = _make_target_dir(tmp_path)
        (target / ".SWE" / "github_session.json").write_text("not json")
        assert _read_github_session(target) is None


# ─── _git_issue_type_from_labels ────────────────────────────────────────────


class TestGitIssueTypeFromLabels:
    def test_returns_bug_for_bug_label(self):
        assert _git_issue_type_from_labels([{"name": "bug"}]) == "bug"

    def test_returns_enhancement_for_enhancement_label(self):
        assert _git_issue_type_from_labels([{"name": "enhancement"}]) == "enhancement"

    def test_returns_refactor_for_refactor_label(self):
        assert _git_issue_type_from_labels([{"name": "refactor"}]) == "refactor"

    def test_returns_enhancement_as_default(self):
        assert _git_issue_type_from_labels([{"name": "other"}]) == "enhancement"

    def test_returns_enhancement_for_empty_labels(self):
        assert _git_issue_type_from_labels([]) == "enhancement"

    def test_label_matching_is_case_insensitive(self):
        assert _git_issue_type_from_labels([{"name": "BUG"}]) == "bug"


# ─── _git_issue_already_ingested ────────────────────────────────────────────


class TestGitIssueAlreadyIngested:
    def test_returns_true_when_issue_exists(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "github-1.md"
        issue_path.write_text("---\nsource: github\ngithub_number: 1\n---\n# Issue\n")
        assert _git_issue_already_ingested(target, 1) is True

    def test_returns_false_when_different_number(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "github-1.md"
        issue_path.write_text("---\nsource: github\ngithub_number: 1\n---\n# Issue\n")
        assert _git_issue_already_ingested(target, 2) is False

    def test_returns_false_when_no_issues_dir(self, tmp_path):
        target = tmp_path / "repo"
        assert _git_issue_already_ingested(target, 1) is False

    def test_returns_false_when_non_github_source(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "pipeline-1.md"
        issue_path.write_text("---\nsource: pipeline\ngithub_number: 1\n---\n# Issue\n")
        assert _git_issue_already_ingested(target, 1) is False

    def test_returns_false_when_no_frontmatter(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "github-1.md"
        issue_path.write_text("no frontmatter here")
        assert _git_issue_already_ingested(target, 1) is False

    def test_finds_issue_with_long_frontmatter(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "github-1.md"
        issue_path.write_text(_long_frontmatter({"source": "github", "github_number": "1"}) + "# Issue\n")
        assert _git_issue_already_ingested(target, 1) is True

    def test_quoted_github_number_is_detected(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "github-42.md"
        issue_path.write_text('---\nsource: github\ngithub_number: "42"\n---\n# Issue\n')
        assert _git_issue_already_ingested(target, 42) is True


# ─── detect_b1_issue_gathering ──────────────────────────────────────────────


class TestDetectB1IssueGathering:
    def test_fires_when_no_session_and_token_and_slug(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo"}}
        assert detect_b1_issue_gathering(ctx) is True

    def test_does_not_fire_when_active_session(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        session = {"active": True, "issue_id": "github-1"}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo"}}
        assert detect_b1_issue_gathering(ctx) is False

    def test_does_not_fire_when_completed_session(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        now = datetime.now(timezone.utc).isoformat()
        session = {"active": False, "completed": True, "completed_at": now}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo"}}
        assert detect_b1_issue_gathering(ctx) is False

    def test_fires_when_completed_session_recheck_interval_elapsed(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        old = (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat()
        session = {"active": False, "completed": True, "completed_at": old}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo"}}
        assert detect_b1_issue_gathering(ctx) is True

    def test_does_not_fire_when_recheck_interval_not_elapsed(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        now = datetime.now(timezone.utc).isoformat()
        session = {"active": False, "completed": False, "checked_at": now}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo"}}
        assert detect_b1_issue_gathering(ctx) is False

    def test_fires_when_recheck_interval_elapsed(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        old = (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat()
        session = {"active": False, "completed": False, "checked_at": old}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo"}}
        assert detect_b1_issue_gathering(ctx) is True

    def test_does_not_fire_when_no_token(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.delenv("SWE_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin._load_env_file", lambda path: None)
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo"}}
        assert detect_b1_issue_gathering(ctx) is False

    def test_does_not_fire_when_no_slug(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {}}
        assert detect_b1_issue_gathering(ctx) is False

    def test_fires_when_checked_at_invalid(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        session = {"active": False, "completed": False, "checked_at": "bad-date"}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo"}}
        assert detect_b1_issue_gathering(ctx) is True

    def test_fires_when_checked_at_naive(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        session = {"active": False, "completed": False, "checked_at": "2025-01-01T00:00:00"}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo"}}
        assert detect_b1_issue_gathering(ctx) is True


# ─── run_b1_issue_gathering ─────────────────────────────────────────────────


class TestRunB1IssueGathering:
    def test_dry_run_returns_success(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target, dry_run=True)
        result = run_b1_issue_gathering(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.dry_run is True

    def test_no_token_returns_failure(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.delenv("SWE_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin._load_env_file", lambda path: None)
        ctx = _make_tick_context(target)
        result = run_b1_issue_gathering(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False

    def test_no_slug_returns_failure(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = _make_tick_context(target)
        result = run_b1_issue_gathering(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False

    def test_api_failure_returns_failure(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = _make_tick_context(target, slug="owner/repo")
        with patch("cronpypeline.plugins.swe_plugin._gh_api_get_list", return_value=None):
            result = run_b1_issue_gathering(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False

    def test_no_issues_writes_idle_session(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = _make_tick_context(target, slug="owner/repo")
        with patch("cronpypeline.plugins.swe_plugin._gh_api_get_list", return_value=[]):
            result = run_b1_issue_gathering(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["issues_found"] == 0
        session = json.loads((target / ".SWE" / "github_session.json").read_text())
        assert session["active"] is False
        assert "checked_at" in session

    def test_ingests_new_issue(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = _make_tick_context(target, slug="owner/repo", issue_label="swe-pipeline")
        gh_issue = {
            "number": 42,
            "title": "Bug found",
            "body": "Something is broken",
            "html_url": "https://github.com/owner/repo/issues/42",
            "created_at": "2025-01-01T00:00:00Z",
            "labels": [{"name": "bug"}],
        }
        with patch("cronpypeline.plugins.swe_plugin._gh_api_get_list", return_value=[gh_issue]):
            result = run_b1_issue_gathering(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["issue_id"] == "github-42"
        assert result.data["gh_number"] == 42
        session = json.loads((target / ".SWE" / "github_session.json").read_text())
        assert session["active"] is True
        assert session["issue_id"] == "github-42"
        issue_file = target / ".SWE" / "issues" / "github-42.md"
        assert issue_file.exists()

    def test_already_ingested_creates_session(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = _make_tick_context(target, slug="owner/repo")
        # Pre-create the issue file
        issue_path = target / ".SWE" / "issues" / "github-42.md"
        issue_path.write_text("---\nsource: github\ngithub_number: 42\n---\n# Issue\n")
        gh_issue = {
            "number": 42,
            "title": "Bug found",
            "body": "Something is broken",
            "html_url": "https://github.com/owner/repo/issues/42",
            "created_at": "2025-01-01T00:00:00Z",
            "labels": [],
        }
        with patch("cronpypeline.plugins.swe_plugin._gh_api_get_list", return_value=[gh_issue]):
            result = run_b1_issue_gathering(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["already_ingested"] is True

    def test_no_issue_number_returns_failure(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = _make_tick_context(target, slug="owner/repo")
        gh_issue = {"number": None, "title": "Bug", "created_at": "2025-01-01T00:00:00Z", "labels": []}
        with patch("cronpypeline.plugins.swe_plugin._gh_api_get_list", return_value=[gh_issue]):
            result = run_b1_issue_gathering(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False


# ─── _find_active_task ──────────────────────────────────────────────────────


class TestFindActiveTask:
    def test_returns_none_when_tasks_dir_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path / "nonexistent")
        assert _find_active_task("repo") is None

    def test_returns_none_when_no_tasks(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path)
        assert _find_active_task("repo") is None

    def test_finds_active_task(self, tmp_path, monkeypatch):
        tasks_dir = tmp_path / "2025-01-01"
        task_dir = tasks_dir / "task-001"
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(json.dumps({"repo_name": "repo"}))
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path)
        result = _find_active_task("repo")
        assert result == task_dir

    def test_skips_tasks_with_gate_json(self, tmp_path, monkeypatch):
        tasks_dir = tmp_path / "2025-01-01"
        task_dir = tasks_dir / "task-001"
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(json.dumps({"repo_name": "repo"}))
        (task_dir / "gate.json").write_text("{}")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path)
        assert _find_active_task("repo") is None

    def test_skips_tasks_for_different_repo(self, tmp_path, monkeypatch):
        tasks_dir = tmp_path / "2025-01-01"
        task_dir = tasks_dir / "task-001"
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(json.dumps({"repo_name": "other"}))
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path)
        assert _find_active_task("repo") is None

    def test_skips_corrupt_task_json(self, tmp_path, monkeypatch):
        tasks_dir = tmp_path / "2025-01-01"
        task_dir = tasks_dir / "task-001"
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text("not json")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path)
        assert _find_active_task("repo") is None

    def test_skips_non_dir_entries(self, tmp_path, monkeypatch):
        (tmp_path / "file.txt").write_text("not a dir")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path)
        assert _find_active_task("repo") is None

    def test_returns_most_recent_when_multiple(self, tmp_path, monkeypatch):
        for date in ("2025-01-01", "2025-01-02"):
            task_dir = tmp_path / date / "task-001"
            task_dir.mkdir(parents=True)
            (task_dir / "task.json").write_text(json.dumps({"repo_name": "repo"}))
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path)
        result = _find_active_task("repo")
        assert "2025-01-02" in str(result)


# ─── _count_open_review_issues ──────────────────────────────────────────────


class TestCountOpenReviewIssues:
    def test_returns_zero_when_no_issues_dir(self, tmp_path):
        target = tmp_path / "repo"
        assert _count_open_review_issues(target) == 0

    def test_counts_open_review_issues(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "review-1.md"
        issue_path.write_text("---\nstatus: open\nsource: review\n---\n# Review\n")
        assert _count_open_review_issues(target) == 1

    def test_does_not_count_non_open_issues(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "review-1.md"
        issue_path.write_text("---\nstatus: done\nsource: review\n---\n# Review\n")
        assert _count_open_review_issues(target) == 0

    def test_does_not_count_non_review_sources(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "github-1.md"
        issue_path.write_text("---\nstatus: open\nsource: github\n---\n# Issue\n")
        assert _count_open_review_issues(target) == 0

    def test_skips_non_frontmatter_files(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "review-1.md"
        issue_path.write_text("no frontmatter")
        assert _count_open_review_issues(target) == 0

    def test_counts_open_review_with_long_frontmatter(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "review-1.md"
        issue_path.write_text(_long_frontmatter({"status": "open", "source": "review"}) + "# Review\n")
        assert _count_open_review_issues(target) == 1


# ─── detect_c_review_ranking ────────────────────────────────────────────────


class TestDetectCReviewRanking:
    def test_fires_when_2plus_open_review_and_no_session(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        for i in range(2):
            issue_path = target / ".SWE" / "issues" / f"review-{i}.md"
            issue_path.write_text("---\nstatus: open\nsource: review\n---\n# Review\n")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path / "no-tasks")
        ctx = {"target_dir": str(target), "target": "repo"}
        assert detect_c_review_ranking(ctx) is True

    def test_does_not_fire_when_active_session(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        session = {"active": True}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        for i in range(2):
            issue_path = target / ".SWE" / "issues" / f"review-{i}.md"
            issue_path.write_text("---\nstatus: open\nsource: review\n---\n# Review\n")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path / "no-tasks")
        ctx = {"target_dir": str(target), "target": "repo"}
        assert detect_c_review_ranking(ctx) is False

    def test_does_not_fire_when_active_task(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        for i in range(2):
            issue_path = target / ".SWE" / "issues" / f"review-{i}.md"
            issue_path.write_text("---\nstatus: open\nsource: review\n---\n# Review\n")
        tasks_dir = tmp_path / "tasks"
        task_dir = tasks_dir / "2025-01-01" / "task-001"
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(json.dumps({"repo_name": "repo"}))
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tasks_dir)
        ctx = {"target_dir": str(target), "target": "repo"}
        assert detect_c_review_ranking(ctx) is False

    def test_does_not_fire_when_less_than_2_open_review(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "review-0.md"
        issue_path.write_text("---\nstatus: open\nsource: review\n---\n# Review\n")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path / "no-tasks")
        ctx = {"target_dir": str(target), "target": "repo"}
        assert detect_c_review_ranking(ctx) is False


# ─── run_c_review_ranking ───────────────────────────────────────────────────


class TestRunCReviewRanking:
    def test_dry_run_returns_success(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "review-0.md"
        issue_path.write_text("---\nstatus: open\nsource: review\n---\n# Review\n")
        ctx = _make_tick_context(target, dry_run=True)
        result = run_c_review_ranking(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.dry_run is True

    def test_success_returns_async(self, tmp_path):
        target = _make_target_dir(tmp_path)
        for i in range(2):
            issue_path = target / ".SWE" / "issues" / f"review-{i}.md"
            issue_path.write_text(f"---\nid: review-{i}\nstatus: open\nsource: review\n---\n# Review\n")
        ctx = _make_tick_context(target)
        mock_handler = MagicMock()
        mock_handler.execute.return_value = ActionResult(success=True, data={"queue_file": "/tmp/q.json"})
        with patch("cronpypeline.plugins.swe_prompts._build_queue_handler", return_value=mock_handler):
            result = run_c_review_ranking(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data.get("async") is True
        assert result.data.get("queue_file") == "/tmp/q.json"

    def test_returns_failure_when_no_open_review_issues(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target)
        result = run_c_review_ranking(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False
        assert "No open review issues" in result.stderr


# ─── detect_c_issue_fix ─────────────────────────────────────────────────────


class TestDetectCIssueFix:
    def test_fires_when_open_issue_and_no_session(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        create_issue(target, issue_data={"id": "i1", "status": "open"}, body="# Issue")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path / "no-tasks")
        ctx = {"target_dir": str(target), "target": "repo"}
        assert detect_c_issue_fix(ctx) is True

    def test_fires_when_active_task_exists(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        tasks_dir = tmp_path / "tasks"
        task_dir = tasks_dir / "2025-01-01" / "task-001"
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(json.dumps({"repo_name": "repo"}))
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tasks_dir)
        ctx = {"target_dir": str(target), "target": "repo"}
        assert detect_c_issue_fix(ctx) is True

    def test_does_not_fire_when_active_session(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        session = {"active": True}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        create_issue(target, issue_data={"id": "i1", "status": "open"}, body="# Issue")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path / "no-tasks")
        ctx = {"target_dir": str(target), "target": "repo"}
        assert detect_c_issue_fix(ctx) is False

    def test_fires_when_active_session_and_revision_issue(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        session = {"active": True}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        create_issue(target, issue_data={"id": "rev1", "status": "open", "type": "revision"}, body="# Revision")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path / "no-tasks")
        ctx = {"target_dir": str(target), "target": "repo"}
        assert detect_c_issue_fix(ctx) is True

    def test_fires_when_active_session_and_session_issue_open(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        session = {"active": True, "issue_id": "github-49"}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        create_issue(target, issue_data={"id": "github-49", "status": "open", "source": "github", "type": "enhancement"}, body="# Issue")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path / "no-tasks")
        ctx = {"target_dir": str(target), "target": "repo"}
        assert detect_c_issue_fix(ctx) is True

    def test_does_not_fire_when_active_session_and_session_issue_closed(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        session = {"active": True, "issue_id": "github-49"}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        create_issue(target, issue_data={"id": "github-49", "status": "fixed", "source": "github"}, body="# Issue")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path / "no-tasks")
        ctx = {"target_dir": str(target), "target": "repo"}
        assert detect_c_issue_fix(ctx) is False

    def test_fires_when_active_task_and_active_session(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        session = {"active": True, "issue_id": "github-49"}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        create_issue(target, issue_data={"id": "github-49", "status": "triaged", "source": "github"}, body="# Issue")
        tasks_dir = tmp_path / "tasks"
        task_dir = tasks_dir / "2025-01-01" / "task-001"
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(json.dumps({"repo_name": "repo"}))
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tasks_dir)
        ctx = {"target_dir": str(target), "target": "repo"}
        assert detect_c_issue_fix(ctx) is True

    def test_does_not_fire_when_no_issues_and_no_task(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path / "no-tasks")
        ctx = {"target_dir": str(target), "target": "repo"}
        assert detect_c_issue_fix(ctx) is False


# ─── run_c_issue_fix ────────────────────────────────────────────────────────


class TestRunCIssueFix:
    def test_dry_run_returns_success(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target, dry_run=True)
        result = run_c_issue_fix(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.dry_run is True

    def test_success_returns_stdout(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target)
        with patch("cronpypeline.plugins.issue_fix.run_issue_fix_state_machine", return_value=True):
            result = run_c_issue_fix(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True

    def test_failure_returns_stderr(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target)
        with patch("cronpypeline.plugins.issue_fix.run_issue_fix_state_machine", return_value=False):
            result = run_c_issue_fix(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False
        assert "returned False" in result.stderr

    def test_exception_returns_failure(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target)
        with patch("cronpypeline.plugins.issue_fix.run_issue_fix_state_machine", side_effect=RuntimeError("boom")):
            result = run_c_issue_fix(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False
        assert "boom" in result.stderr


# ─── _open_issue_count ──────────────────────────────────────────────────────


class TestOpenIssueCount:
    def test_returns_zero_when_no_issues_dir(self, tmp_path):
        target = tmp_path / "repo"
        assert _open_issue_count(target) == 0

    def test_counts_open_issues(self, tmp_path):
        target = _make_target_dir(tmp_path)
        create_issue(target, issue_data={"id": "i1", "status": "open"}, body="# I1")
        create_issue(target, issue_data={"id": "i2", "status": "done"}, body="# I2")
        assert _open_issue_count(target) == 1

    def test_counts_only_github_when_session_active(self, tmp_path):
        target = _make_target_dir(tmp_path)
        session = {"active": True}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        issue1 = target / ".SWE" / "issues" / "github-1.md"
        issue1.write_text("---\nstatus: open\nsource: github\n---\n# GH\n")
        issue2 = target / ".SWE" / "issues" / "pipeline-1.md"
        issue2.write_text("---\nstatus: open\nsource: pipeline\n---\n# PL\n")
        assert _open_issue_count(target) == 1

    def test_skips_non_frontmatter(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "bad.md"
        issue_path.write_text("no frontmatter")
        assert _open_issue_count(target) == 0

    def test_counts_open_with_long_frontmatter(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "i1.md"
        issue_path.write_text(_long_frontmatter({"status": "open"}) + "# Issue\n")
        assert _open_issue_count(target) == 1


# ─── _a1_is_pass ────────────────────────────────────────────────────────────


class TestA1IsPass:
    def test_returns_true_when_pass(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        assert _a1_is_pass(target) is True

    def test_returns_false_when_fail(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — FAIL\n")
        assert _a1_is_pass(target) is False

    def test_returns_false_when_no_report(self, tmp_path):
        target = _make_target_dir(tmp_path)
        assert _a1_is_pass(target) is False

    def test_returns_false_on_corrupt_report(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_dir = target / ".SWE" / "reports" / "test-infra"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "latest.md").write_text("")
        assert _a1_is_pass(target) is False


# ─── _a7_coverage_pct ───────────────────────────────────────────────────────


class TestA7CoveragePct:
    def test_returns_pct_when_found(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 95.5%\n")
        assert _a7_coverage_pct(target) == 95.5

    def test_returns_none_when_no_report(self, tmp_path):
        target = _make_target_dir(tmp_path)
        assert _a7_coverage_pct(target) is None

    def test_returns_none_when_no_match(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\nNo coverage data\n")
        assert _a7_coverage_pct(target) is None

    def test_returns_pct_from_run_diagnostic_format(self, tmp_path):
        """Report format from run_diagnostic: lowercase key, no % suffix."""
        target = _make_target_dir(tmp_path)
        _write_report(target, "coverage", "r.md", "# Test Coverage — PASS\n\n**Status**: PASS\n\n- **coverage**: 99.0\n- **threshold**: 80.0\n")
        assert _a7_coverage_pct(target) == 99.0


# ─── _find_issue_by_id ──────────────────────────────────────────────────────


class TestFindIssueById:
    def test_returns_path_when_exists(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "i1.md"
        issue_path.write_text("---\nid: i1\n---\n# Issue\n")
        result = _find_issue_by_id(target, "i1")
        assert result == issue_path

    def test_returns_none_when_missing(self, tmp_path):
        target = _make_target_dir(tmp_path)
        assert _find_issue_by_id(target, "nonexistent") is None

    def test_finds_issue_with_special_chars(self, tmp_path):
        target = _make_target_dir(tmp_path)
        create_issue(target, issue_data={"id": "foo/bar", "status": "open"}, body="# Issue")
        result = _find_issue_by_id(target, "foo/bar")
        assert result is not None
        assert result.name == "foo-bar.md"
        assert result.exists()

    def test_finds_issue_with_spaces(self, tmp_path):
        target = _make_target_dir(tmp_path)
        create_issue(target, issue_data={"id": "my issue!", "status": "open"}, body="# Issue")
        result = _find_issue_by_id(target, "my issue!")
        assert result is not None
        assert result.name == "my-issue.md"


# ─── _write_pipeline_issue ──────────────────────────────────────────────────


class TestWritePipelineIssue:
    def test_writes_issue_file(self, tmp_path):
        target = _make_target_dir(tmp_path)
        path = _write_pipeline_issue(
            target, "repo", "test-1", "coverage", "Test Issue", "Body text",
        )
        assert path.exists()
        content = path.read_text()
        assert "id: test-1" in content
        assert "source: pipeline" in content
        assert "type: coverage" in content
        assert "# Test Issue" in content

    def test_writes_with_labels(self, tmp_path):
        target = _make_target_dir(tmp_path)
        path = _write_pipeline_issue(
            target, "repo", "test-1", "review", "Review", "Body",
            labels=["review", "pipeline"],
        )
        content = path.read_text()
        assert "review" in content
        assert "pipeline" in content

    def test_writes_with_extra_fields(self, tmp_path):
        target = _make_target_dir(tmp_path)
        path = _write_pipeline_issue(
            target, "repo", "test-1", "review", "Review", "Body",
            extra=[("review_generation", 2), ("previous_review_sha", "abc12345")],
        )
        # Extra fields are passed to create_issue but Issue.from_dict only
        # preserves known fields. The issue is still written successfully.
        assert path.exists()
        content = path.read_text()
        assert "id: test-1" in content
        assert "type: review" in content

    def test_path_matches_create_issue_with_special_chars(self, tmp_path):
        target = _make_target_dir(tmp_path)
        path = _write_pipeline_issue(
            target, "repo", "foo/bar", "coverage", "Special", "Body",
        )
        assert path.exists()
        assert path.name == "foo-bar.md"
        create_issue(
            target, issue_data={"id": "foo/bar", "status": "open"}, body="x",
        )
        assert (target / ".SWE" / "issues" / "foo-bar.md").exists()

    def test_path_matches_create_issue_with_spaces(self, tmp_path):
        target = _make_target_dir(tmp_path)
        path = _write_pipeline_issue(
            target, "repo", "my issue!", "review", "Special", "Body",
        )
        assert path.name == "my-issue.md"
        assert path.exists()
        assert (target / ".SWE" / "issues" / "my-issue.md").exists()


# ─── _close_and_comment_github_issue ────────────────────────────────────────


class TestCloseAndCommentGithubIssue:
    def test_merged_posts_comment_and_closes(self):
        with patch("cronpypeline.plugins.swe_plugin._gh_api_post") as mock_post, \
             patch("cronpypeline.plugins.swe_plugin._gh_api_patch") as mock_patch:
            _close_and_comment_github_issue("owner", "repo", 42, 7, "https://github.com/owner/repo/pull/7", "token", merged=True)
        mock_post.assert_called_once()
        mock_patch.assert_called_once()
        post_payload = mock_post.call_args[0][3]
        assert "merged" in post_payload["body"].lower()

    def test_not_merged_posts_comment_only(self):
        with patch("cronpypeline.plugins.swe_plugin._gh_api_post") as mock_post, \
             patch("cronpypeline.plugins.swe_plugin._gh_api_patch") as mock_patch:
            _close_and_comment_github_issue("owner", "repo", 42, 7, "https://github.com/owner/repo/pull/7", "token", merged=False)
        mock_post.assert_called_once()
        mock_patch.assert_not_called()
        post_payload = mock_post.call_args[0][3]
        assert "closed without merging" in post_payload["body"]


# ─── integration_head_sha ───────────────────────────────────────────────────


class TestIntegrationHeadSha:
    def test_returns_sha_for_integration_branch(self, tmp_path):
        subprocess.run(["git", "init", "-b", "main", str(tmp_path)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], capture_output=True, check=True)
        (tmp_path / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        sha = integration_head_sha(tmp_path, "main")
        assert sha is not None
        assert len(sha) == 40

    def test_returns_default_branch_sha_when_no_integration(self, tmp_path):
        subprocess.run(["git", "init", "-b", "main", str(tmp_path)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], capture_output=True, check=True)
        (tmp_path / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], capture_output=True, check=True)
        sha = integration_head_sha(tmp_path, "main")
        assert sha is not None

    def test_returns_none_when_no_branches_resolve(self, tmp_path):
        subprocess.run(["git", "init", "-b", "main", str(tmp_path)], capture_output=True, check=True)
        sha = integration_head_sha(tmp_path, "main")
        assert sha is None

    def test_returns_none_on_git_timeout(self, tmp_path):
        with patch(
            "cronpypeline.plugins.swe_plugin._git",
            side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=60),
        ):
            sha = integration_head_sha(tmp_path, "main")
        assert sha is None

    def test_returns_none_when_default_branch_times_out(self, tmp_path):
        not_found = subprocess.CompletedProcess(args=["git"], returncode=1, stdout="", stderr="")
        with patch(
            "cronpypeline.plugins.swe_plugin._git",
            side_effect=[not_found, subprocess.TimeoutExpired(cmd=["git"], timeout=60)],
        ):
            sha = integration_head_sha(tmp_path, "main")
        assert sha is None


# ─── _sha_is_ancestor ────────────────────────────────────────────────────────


class TestShaIsAncestor:
    def test_returns_false_for_empty_sha(self, tmp_path):
        subprocess.run(["git", "init", "-b", "main", str(tmp_path)], capture_output=True, check=True)
        assert _sha_is_ancestor(tmp_path, "", "main") is False

    def test_returns_false_for_none_sha(self, tmp_path):
        subprocess.run(["git", "init", "-b", "main", str(tmp_path)], capture_output=True, check=True)
        assert _sha_is_ancestor(tmp_path, None, "main") is False  # type: ignore[arg-type]

    def test_returns_true_for_ancestor(self, tmp_path):
        subprocess.run(["git", "init", "-b", "main", str(tmp_path)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], capture_output=True, check=True)
        (tmp_path / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], capture_output=True, check=True)
        old_sha = subprocess.run(
            ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        (tmp_path / "g.txt").write_text("y")
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "second"], capture_output=True, check=True)
        assert _sha_is_ancestor(tmp_path, old_sha, "main") is True

    def test_returns_false_for_nonexistent_sha(self, tmp_path):
        subprocess.run(["git", "init", "-b", "main", str(tmp_path)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], capture_output=True, check=True)
        (tmp_path / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], capture_output=True, check=True)
        assert _sha_is_ancestor(tmp_path, "0" * 40, "main") is False

    def test_returns_false_on_timeout(self, tmp_path):
        subprocess.run(["git", "init", "-b", "main", str(tmp_path)], capture_output=True, check=True)
        with patch(
            "cronpypeline.plugins.swe_plugin.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=60),
        ):
            assert _sha_is_ancestor(tmp_path, "abc123", "main") is False


# ─── _build_pr_body ─────────────────────────────────────────────────────────


class TestBuildPrBody:
    def test_builds_body_with_no_issues(self, tmp_path):
        target = _make_target_dir(tmp_path)
        body = _build_pr_body(target, "repo", "main")
        assert "SWE Pipeline" in body
        assert "0 issues fixed" in body

    def test_builds_body_with_done_issues(self, tmp_path):
        target = _make_target_dir(tmp_path)
        for i, itype in enumerate(["bug", "refactor", "enhancement"]):
            issue_path = target / ".SWE" / "issues" / f"i{i}.md"
            issue_path.write_text(f"---\nstatus: done\ntype: {itype}\n---\n# Issue\n")
        body = _build_pr_body(target, "repo", "main")
        assert "1 bugs" in body
        assert "1 refactors" in body
        assert "1 enhancements" in body
        assert "3 issues fixed" in body

    def test_skips_non_done_issues(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "i0.md"
        issue_path.write_text("---\nstatus: open\ntype: bug\n---\n# Issue\n")
        body = _build_pr_body(target, "repo", "main")
        assert "0 issues fixed" in body

    def test_includes_coverage_pct(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 90.0%\n")
        body = _build_pr_body(target, "repo", "main")
        assert "90%" in body

    def test_counts_done_issues_with_long_frontmatter(self, tmp_path):
        target = _make_target_dir(tmp_path)
        for i, itype in enumerate(["bug", "refactor", "enhancement"]):
            issue_path = target / ".SWE" / "issues" / f"i{i}.md"
            issue_path.write_text(_long_frontmatter({"status": "done", "type": itype}) + "# Issue\n")
        body = _build_pr_body(target, "repo", "main")
        assert "1 bugs" in body
        assert "1 refactors" in body
        assert "1 enhancements" in body
        assert "3 issues fixed" in body


# ─── _build_doc_sync_prompt ─────────────────────────────────────────────────


class TestBuildDocSyncPrompt:
    def test_builds_prompt_without_pr(self, tmp_path):
        target = _make_target_dir(tmp_path)
        prompt = _build_doc_sync_prompt(target, "repo", "main", "abc12345", pr_exists=False)
        assert "Documentation Synchronization" in prompt
        assert str(target) in prompt
        # No push instruction when no PR exists
        assert "update the existing PR" not in prompt

    def test_builds_prompt_with_pr(self, tmp_path):
        target = _make_target_dir(tmp_path)
        prompt = _build_doc_sync_prompt(target, "repo", "main", "abc12345", pr_exists=True)
        assert "push" in prompt.lower()
        assert INTEGRATION_BRANCH in prompt

    def test_prompt_contains_marker_path(self, tmp_path):
        target = _make_target_dir(tmp_path)
        prompt = _build_doc_sync_prompt(target, "repo", "main", "abc12345", pr_exists=False)
        assert "doc_sync.json" in prompt

    def test_quotes_target_dir_with_spaces(self, tmp_path):
        target = tmp_path / "repo with space"
        prompt = _build_doc_sync_prompt(target, "repo", "main", "abc12345", pr_exists=False)
        assert f"cd '{target}'" in prompt
        assert f"cd {target}" not in prompt


# ─── _build_pr_review_prompt ────────────────────────────────────────────────


class TestBuildPrReviewPrompt:
    def test_builds_basic_prompt(self, tmp_path):
        target = _make_target_dir(tmp_path)
        prompt = _build_pr_review_prompt(
            target, "repo", "main", "abc123def456", 42, "owner", "repo",
        )
        assert "PR #42" in prompt
        assert "owner/repo" in prompt
        assert "pr_reviewed.json" in prompt

    def test_includes_cycle_guidance_when_max_cycles_set(self, tmp_path):
        target = _make_target_dir(tmp_path)
        prompt = _build_pr_review_prompt(
            target, "repo", "main", "abc123def456", 42, "owner", "repo",
            review_cycle=1, max_cycles=3,
        )
        assert "1st" in prompt
        assert "3 cycles" in prompt

    def test_final_cycle_marker(self, tmp_path):
        target = _make_target_dir(tmp_path)
        prompt = _build_pr_review_prompt(
            target, "repo", "main", "abc123def456", 42, "owner", "repo",
            review_cycle=3, max_cycles=3,
        )
        assert "final" in prompt.lower()

    def test_late_stage_guidance_at_cycle_2(self, tmp_path):
        target = _make_target_dir(tmp_path)
        prompt = _build_pr_review_prompt(
            target, "repo", "main", "abc123def456", 42, "owner", "repo",
            review_cycle=2, max_cycles=3,
        )
        assert "critical issues" in prompt.lower()

    def test_late_stage_guidance_at_cycle_3(self, tmp_path):
        target = _make_target_dir(tmp_path)
        prompt = _build_pr_review_prompt(
            target, "repo", "main", "abc123def456", 42, "owner", "repo",
            review_cycle=3, max_cycles=3,
        )
        assert "showstopper" in prompt.lower()

    def test_11th_uses_th_suffix(self, tmp_path):
        target = _make_target_dir(tmp_path)
        prompt = _build_pr_review_prompt(
            target, "repo", "main", "abc123def456", 42, "owner", "repo",
            review_cycle=11, max_cycles=15,
        )
        assert "11th" in prompt

    def test_12th_uses_th_suffix(self, tmp_path):
        target = _make_target_dir(tmp_path)
        prompt = _build_pr_review_prompt(
            target, "repo", "main", "abc123def456", 42, "owner", "repo",
            review_cycle=12, max_cycles=15,
        )
        assert "12th" in prompt

    def test_2nd_uses_nd_suffix(self, tmp_path):
        target = _make_target_dir(tmp_path)
        prompt = _build_pr_review_prompt(
            target, "repo", "main", "abc123def456", 42, "owner", "repo",
            review_cycle=2, max_cycles=5,
        )
        assert "2nd" in prompt

    def test_3rd_uses_rd_suffix(self, tmp_path):
        target = _make_target_dir(tmp_path)
        prompt = _build_pr_review_prompt(
            target, "repo", "main", "abc123def456", 42, "owner", "repo",
            review_cycle=3, max_cycles=5,
        )
        assert "3rd" in prompt

    def test_quotes_target_dir_with_spaces(self, tmp_path):
        target = tmp_path / "repo with space"
        prompt = _build_pr_review_prompt(
            target, "repo", "main", "abc123def456", 42, "owner", "repo",
        )
        assert f"cd '{target}'" in prompt
        assert f"cd {target}" not in prompt


# ─── _build_pr_title_prompt ─────────────────────────────────────────────────


class TestBuildPrTitlePrompt:
    def test_builds_basic_prompt(self, tmp_path):
        target = _make_target_dir(tmp_path)
        prompt = _build_pr_title_prompt(target, "repo", "main", "abc123def456")
        assert "pull request title" in prompt
        assert INTEGRATION_BRANCH in prompt
        assert "pr_title.json" in prompt

    def test_quotes_target_dir_with_spaces(self, tmp_path):
        target = tmp_path / "repo with space"
        prompt = _build_pr_title_prompt(target, "repo", "main", "abc123def456")
        assert f"cd '{target}'" in prompt
        assert f"cd {target}" not in prompt


# ─── _count_done_review_issues ──────────────────────────────────────────────


class TestCountDoneReviewIssues:
    def test_returns_zero_when_no_issues_dir(self, tmp_path):
        target = tmp_path / "repo"
        assert _count_done_review_issues(target) == 0

    def test_counts_done_review_issues(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "review-abc12345.md"
        issue_path.write_text("---\nstatus: done\ntype: review\n---\n# Review\n")
        assert _count_done_review_issues(target) == 1

    def test_skips_non_done_issues(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "review-1.md"
        issue_path.write_text("---\nstatus: open\ntype: review\n---\n# Review\n")
        assert _count_done_review_issues(target) == 0

    def test_skips_non_review_types(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "coverage-1.md"
        issue_path.write_text("---\nstatus: done\ntype: coverage\n---\n# Coverage\n")
        assert _count_done_review_issues(target) == 0

    def test_skips_non_frontmatter(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "review-1.md"
        issue_path.write_text("no frontmatter")
        assert _count_done_review_issues(target) == 0

    def test_counts_done_review_with_long_frontmatter(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "review-abc12345.md"
        issue_path.write_text(_long_frontmatter({"status": "done", "type": "review"}) + "# Review\n")
        assert _count_done_review_issues(target) == 1


# ─── _find_previous_review_sha ──────────────────────────────────────────────


class TestFindPreviousReviewSha:
    def test_returns_none_when_no_issues_dir(self, tmp_path):
        target = tmp_path / "repo"
        assert _find_previous_review_sha(target) is None

    def test_returns_sha_from_done_review(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "review-abc12345.md"
        issue_path.write_text("---\nstatus: done\ntype: review\n---\n# Review\n")
        assert _find_previous_review_sha(target) == "abc12345"

    def test_returns_none_when_no_done_reviews(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "review-abc12345.md"
        issue_path.write_text("---\nstatus: open\ntype: review\n---\n# Review\n")
        assert _find_previous_review_sha(target) is None

    def test_returns_sha_from_most_recent(self, tmp_path):
        target = _make_target_dir(tmp_path)
        for sha in ["aaa11111", "bbb22222"]:
            issue_path = target / ".SWE" / "issues" / f"review-{sha}.md"
            issue_path.write_text("---\nstatus: done\ntype: review\n---\n# Review\n")
        # sorted() returns alphabetical, so bbb22222 is last
        assert _find_previous_review_sha(target) == "bbb22222"

    def test_returns_none_when_sha_pattern_not_matched(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "review-badname.md"
        issue_path.write_text("---\nstatus: done\ntype: review\n---\n# Review\n")
        assert _find_previous_review_sha(target) is None

    def test_returns_sha_with_long_frontmatter(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "review-abc12345.md"
        issue_path.write_text(_long_frontmatter({"status": "done", "type": "review"}) + "# Review\n")
        assert _find_previous_review_sha(target) == "abc12345"


# ─── detect_c_coverage_issue ────────────────────────────────────────────────


class TestDetectCCoverageIssue:
    def _setup_passing(self, target: Path, pct: float = 80.0) -> None:
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", f"# Coverage — PASS\n\n- **Coverage:** {pct}%\n")

    def _setup_git(self, target: Path) -> None:
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)

    def test_fires_when_coverage_below_target(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_passing(target, pct=80.0)
        self._setup_git(target)
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_coverage_issue(ctx) is True

    def test_does_not_fire_when_open_issues(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_passing(target, pct=80.0)
        self._setup_git(target)
        create_issue(target, issue_data={"id": "i1", "status": "open"}, body="# Issue")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_coverage_issue(ctx) is False

    def test_does_not_fire_when_a1_not_pass(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — FAIL\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 80.0%\n")
        self._setup_git(target)
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_coverage_issue(ctx) is False

    def test_does_not_fire_when_coverage_at_target(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_passing(target, pct=100.0)
        self._setup_git(target)
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_coverage_issue(ctx) is False

    def test_does_not_fire_with_custom_threshold(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_passing(target, pct=90.0)
        self._setup_git(target)
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main", "coverage_threshold": 80.0}}
        assert detect_c_coverage_issue(ctx) is False

    def test_does_not_fire_when_no_coverage_report(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        self._setup_git(target)
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_coverage_issue(ctx) is False

    def test_does_not_fire_when_existing_non_discarded(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_passing(target, pct=80.0)
        self._setup_git(target)
        sha = integration_head_sha(target, "main")
        issue_id = f"coverage-{sha[:8]}"
        issue_path = target / ".SWE" / "issues" / f"{issue_id}.md"
        issue_path.write_text("---\nstatus: open\n---\n# Coverage\n")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_coverage_issue(ctx) is False

    def test_fires_when_existing_discarded(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_passing(target, pct=80.0)
        self._setup_git(target)
        sha = integration_head_sha(target, "main")
        issue_id = f"coverage-{sha[:8]}"
        issue_path = target / ".SWE" / "issues" / f"{issue_id}.md"
        issue_path.write_text("---\nstatus: discarded\n---\n# Coverage\n")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_coverage_issue(ctx) is True

    def test_does_not_fire_when_existing_non_discarded_long_frontmatter(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_passing(target, pct=80.0)
        self._setup_git(target)
        sha = integration_head_sha(target, "main")
        issue_id = f"coverage-{sha[:8]}"
        issue_path = target / ".SWE" / "issues" / f"{issue_id}.md"
        issue_path.write_text(_long_frontmatter({"status": "open"}) + "# Coverage\n")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_coverage_issue(ctx) is False

    def test_fires_when_existing_discarded_long_frontmatter(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_passing(target, pct=80.0)
        self._setup_git(target)
        sha = integration_head_sha(target, "main")
        issue_id = f"coverage-{sha[:8]}"
        issue_path = target / ".SWE" / "issues" / f"{issue_id}.md"
        issue_path.write_text(_long_frontmatter({"status": "discarded"}) + "# Coverage\n")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_coverage_issue(ctx) is True

    def test_defers_when_pr_not_reviewed(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_passing(target, pct=80.0)
        self._setup_git(target)
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 1}))
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_coverage_issue(ctx) is False

    def test_fires_when_pr_reviewed(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_passing(target, pct=80.0)
        self._setup_git(target)
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 1}))
        (target / ".SWE" / "pr_reviewed.json").write_text(json.dumps({"pr_number": 1}))
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_coverage_issue(ctx) is True


# ─── run_c_coverage_issue ───────────────────────────────────────────────────


class TestRunCCoverageIssue:
    def test_dry_run_returns_success(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target, dry_run=True)
        result = run_c_coverage_issue(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.dry_run is True

    def test_creates_coverage_issue(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 80.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        ctx = _make_tick_context(target, default_branch="main")
        result = run_c_coverage_issue(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert "issue_id" in result.data
        issue_file = target / ".SWE" / "issues" / f"{result.data['issue_id']}.md"
        assert issue_file.exists()

    def test_creates_coverage_issue_with_custom_threshold(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 90.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        ctx = _make_tick_context(target, default_branch="main", coverage_threshold=80.0)
        result = run_c_coverage_issue(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        issue_file = target / ".SWE" / "issues" / f"{result.data['issue_id']}.md"
        content = issue_file.read_text()
        assert "80%" in content
        assert "100%" not in content


# ─── detect_c_review_issue ──────────────────────────────────────────────────


class TestDetectCReviewIssue:
    def _setup_passing_with_coverage(self, target: Path) -> None:
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")

    def _setup_git(self, target: Path) -> None:
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)

    def test_fires_when_coverage_at_target(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_passing_with_coverage(target)
        self._setup_git(target)
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_review_issue(ctx) is True

    def test_fires_with_custom_threshold(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 90.0%\n")
        self._setup_git(target)
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main", "coverage_threshold": 80.0}}
        assert detect_c_review_issue(ctx) is True

    def test_does_not_fire_when_coverage_below_target(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 80.0%\n")
        self._setup_git(target)
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_review_issue(ctx) is False

    def test_does_not_fire_when_open_issues(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_passing_with_coverage(target)
        self._setup_git(target)
        create_issue(target, issue_data={"id": "i1", "status": "open"}, body="# Issue")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_review_issue(ctx) is False

    def test_does_not_fire_when_max_generations_reached(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_passing_with_coverage(target)
        self._setup_git(target)
        # Create 3 done review issues (max is 3)
        for i in range(3):
            issue_path = target / ".SWE" / "issues" / f"review-sha{i:08d}.md"
            issue_path.write_text("---\nstatus: done\ntype: review\n---\n# Review\n")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main", "max_review_generations": 3}}
        assert detect_c_review_issue(ctx) is False

    def test_does_not_fire_when_existing_non_discarded(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_passing_with_coverage(target)
        self._setup_git(target)
        sha = integration_head_sha(target, "main")
        issue_id = f"review-{sha[:8]}"
        issue_path = target / ".SWE" / "issues" / f"{issue_id}.md"
        issue_path.write_text("---\nstatus: open\n---\n# Review\n")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_review_issue(ctx) is False

    def test_fires_when_existing_discarded(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_passing_with_coverage(target)
        self._setup_git(target)
        sha = integration_head_sha(target, "main")
        issue_id = f"review-{sha[:8]}"
        issue_path = target / ".SWE" / "issues" / f"{issue_id}.md"
        issue_path.write_text("---\nstatus: discarded\n---\n# Review\n")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_review_issue(ctx) is True

    def test_does_not_fire_when_existing_non_discarded_long_frontmatter(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_passing_with_coverage(target)
        self._setup_git(target)
        sha = integration_head_sha(target, "main")
        issue_id = f"review-{sha[:8]}"
        issue_path = target / ".SWE" / "issues" / f"{issue_id}.md"
        issue_path.write_text(_long_frontmatter({"status": "open"}) + "# Review\n")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_review_issue(ctx) is False

    def test_fires_when_existing_discarded_long_frontmatter(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_passing_with_coverage(target)
        self._setup_git(target)
        sha = integration_head_sha(target, "main")
        issue_id = f"review-{sha[:8]}"
        issue_path = target / ".SWE" / "issues" / f"{issue_id}.md"
        issue_path.write_text(_long_frontmatter({"status": "discarded"}) + "# Review\n")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_review_issue(ctx) is True


# ─── run_c_review_issue ─────────────────────────────────────────────────────


class TestRunCReviewIssue:
    def test_dry_run_returns_success(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target, dry_run=True)
        result = run_c_review_issue(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.dry_run is True

    def test_creates_first_review_issue(self, tmp_path):
        target = _make_target_dir(tmp_path)
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        ctx = _make_tick_context(target, default_branch="main")
        result = run_c_review_issue(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["review_generation"] == 1
        issue_file = target / ".SWE" / "issues" / f"{result.data['issue_id']}.md"
        assert issue_file.exists()

    def test_creates_subsequent_review_issue(self, tmp_path):
        target = _make_target_dir(tmp_path)
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        # Create a done review issue
        issue_path = target / ".SWE" / "issues" / "review-oldsha12.md"
        issue_path.write_text("---\nstatus: done\ntype: review\n---\n# Review\n")
        ctx = _make_tick_context(target, default_branch="main")
        result = run_c_review_issue(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["review_generation"] == 2


# ─── detect_c_pr_status ─────────────────────────────────────────────────────


class TestDetectCPrStatus:
    def test_fires_when_pr_published_and_open(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 1, "pr_state": "open"}))
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo"}}
        assert detect_c_pr_status(ctx) is True

    def test_does_not_fire_when_no_pr_marker(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo"}}
        assert detect_c_pr_status(ctx) is False

    def test_does_not_fire_when_no_pr_number(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 0}))
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo"}}
        assert detect_c_pr_status(ctx) is False

    def test_does_not_fire_when_pr_merged(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 1, "pr_state": "merged"}))
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo"}}
        assert detect_c_pr_status(ctx) is False

    def test_does_not_fire_when_pr_rejected(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 1, "pr_state": "rejected"}))
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo"}}
        assert detect_c_pr_status(ctx) is False

    def test_does_not_fire_when_no_token(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.delenv("SWE_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin._load_env_file", lambda path: None)
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 1}))
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo"}}
        assert detect_c_pr_status(ctx) is False

    def test_does_not_fire_when_no_slug(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 1}))
        ctx = {"target_dir": str(target), "target_config": {}}
        assert detect_c_pr_status(ctx) is False

    def test_does_not_fire_on_corrupt_pr_marker(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text("bad json")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo"}}
        assert detect_c_pr_status(ctx) is False


# ─── run_c_pr_status ────────────────────────────────────────────────────────


class TestRunCPrStatus:
    def test_dry_run_returns_success(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target, dry_run=True)
        result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.dry_run is True

    def test_merged_updates_marker_and_closes_issue(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7}))
        session = {"active": True, "github_number": 42, "issue_id": "github-42"}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        ctx = _make_tick_context(target, slug="owner/repo")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "state": "closed", "merged": True, "merged_at": "2025-01-01T00:00:00Z",
            "html_url": "https://github.com/owner/repo/pull/7",
        }).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", return_value=mock_resp), \
             patch("cronpypeline.plugins.swe_plugin._gh_api_post") as mock_post, \
             patch("cronpypeline.plugins.swe_plugin._gh_api_patch") as mock_patch:
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["pr_state"] == "merged"
        pr_data = json.loads((target / ".SWE" / "pr_published.json").read_text())
        assert pr_data["pr_state"] == "merged"
        mock_post.assert_called_once()
        mock_patch.assert_called_once()
        saved_session = json.loads((target / ".SWE" / "github_session.json").read_text())
        assert saved_session["active"] is False

    def test_rejected_updates_marker(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7}))
        session = {"active": True, "github_number": 42, "issue_id": "github-42"}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        ctx = _make_tick_context(target, slug="owner/repo")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "state": "closed", "merged": False, "closed_at": "2025-01-01T00:00:00Z",
            "html_url": "https://github.com/owner/repo/pull/7",
        }).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", return_value=mock_resp), \
             patch("cronpypeline.plugins.swe_plugin._gh_api_post") as mock_post, \
             patch("cronpypeline.plugins.swe_plugin._gh_api_patch"):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["pr_state"] == "rejected"
        mock_post.assert_called_once()

    def test_open_pr_returns_open(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7}))
        ctx = _make_tick_context(target, slug="owner/repo")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "state": "open", "merged": False, "html_url": "https://github.com/owner/repo/pull/7",
        }).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        # Reviews fetch also needs to return
        mock_reviews = MagicMock()
        mock_reviews.read.return_value = b'[]'
        mock_reviews.__enter__ = MagicMock(return_value=mock_reviews)
        mock_reviews.__exit__ = MagicMock(return_value=False)
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=[mock_resp, mock_reviews]):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["pr_state"] == "open"

    def test_api_failure_returns_failure(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7}))
        ctx = _make_tick_context(target, slug="owner/repo")
        from urllib.error import HTTPError
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=HTTPError("url", 404, "Not Found", {}, None)):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False


# ─── detect_c_pr_publish ────────────────────────────────────────────────────


class TestDetectCPrPublish:
    def _setup(self, target: Path, pct: float = 100.0) -> None:
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", f"# Coverage — PASS\n\n- **Coverage:** {pct}%\n")

    def _setup_git(self, target: Path) -> None:
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)

    def test_fires_when_all_conditions_met(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        # Make integration branch ahead of main
        subprocess.run(["git", "-C", str(target), "checkout", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / "new.txt").write_text("new")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "new"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "main"], capture_output=True, check=True)
        # Write doc_sync marker with matching SHA
        sha = integration_head_sha(target, "main")
        (target / ".SWE" / "doc_sync.json").write_text(json.dumps({"sha": sha}))
        (target / ".SWE" / "pr_title.json").write_text(json.dumps({"title": "SWE Pipeline: test"}))
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_pr_publish(ctx) is True

    def test_does_not_fire_when_pr_already_published(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        subprocess.run(["git", "-C", str(target), "checkout", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / "new.txt").write_text("new")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "new"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "main"], capture_output=True, check=True)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 1}))
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_pr_publish(ctx) is False

    def test_does_not_fire_when_open_issues(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        subprocess.run(["git", "-C", str(target), "checkout", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / "new.txt").write_text("new")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "new"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "main"], capture_output=True, check=True)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        create_issue(target, issue_data={"id": "i1", "status": "open"}, body="# Issue")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_pr_publish(ctx) is False

    def test_does_not_fire_when_a1_not_pass(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — FAIL\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        self._setup_git(target)
        subprocess.run(["git", "-C", str(target), "checkout", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / "new.txt").write_text("new")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "new"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "main"], capture_output=True, check=True)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_pr_publish(ctx) is False

    def test_does_not_fire_when_no_token(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        subprocess.run(["git", "-C", str(target), "checkout", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / "new.txt").write_text("new")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "new"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "main"], capture_output=True, check=True)
        monkeypatch.delenv("SWE_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin._load_env_file", lambda path: None)
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_pr_publish(ctx) is False

    def test_does_not_fire_when_no_slug(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        subprocess.run(["git", "-C", str(target), "checkout", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / "new.txt").write_text("new")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "new"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "main"], capture_output=True, check=True)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_pr_publish(ctx) is False

    def test_does_not_fire_when_delivery_not_open_pr(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        subprocess.run(["git", "-C", str(target), "checkout", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / "new.txt").write_text("new")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "new"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "main"], capture_output=True, check=True)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main"}}
        assert detect_c_pr_publish(ctx) is False

    def test_does_not_fire_when_integration_not_ahead(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_pr_publish(ctx) is False


# ─── run_c_pr_publish ───────────────────────────────────────────────────────


class TestRunCPrPublish:
    def test_dry_run_returns_success(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target, dry_run=True)
        result = run_c_pr_publish(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.dry_run is True

    def test_no_token_returns_failure(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.delenv("SWE_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin._load_env_file", lambda path: None)
        ctx = _make_tick_context(target, slug="owner/repo")
        result = run_c_pr_publish(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False

    def test_publishes_pr(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / ".SWE" / "pr_title.json").write_text(json.dumps({"title": "SWE Pipeline: fix login bug"}))
        ctx = _make_tick_context(target, slug="owner/repo", default_branch="main")
        mock_push = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        captured_args = {}
        def mock_post(owner, repo, endpoint, data, token, **kwargs):
            captured_args.update(data)
            return {"number": 42, "html_url": "https://github.com/owner/repo/pull/42"}
        with patch("cronpypeline.plugins.swe_plugin.subprocess.run", return_value=mock_push), \
             patch("cronpypeline.plugins.swe_plugin._gh_api_post", side_effect=mock_post):
            result = run_c_pr_publish(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["pr_number"] == 42
        assert captured_args["title"] == "SWE Pipeline: fix login bug"
        pr_data = json.loads((target / ".SWE" / "pr_published.json").read_text())
        assert pr_data["pr_number"] == 42

    def test_api_failure_returns_failure(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        ctx = _make_tick_context(target, slug="owner/repo", default_branch="main")
        mock_push = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("cronpypeline.plugins.swe_plugin.subprocess.run", return_value=mock_push), \
             patch("cronpypeline.plugins.swe_plugin._gh_api_post", return_value=None):
            result = run_c_pr_publish(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False


# ─── detect_c_pr_title ──────────────────────────────────────────────────────


class TestDetectCPrTitle:
    def _setup(self, target: Path, pct: float = 100.0) -> None:
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", f"# Coverage — PASS\n\n- **Coverage:** {pct}%\n")

    def _setup_git(self, target: Path) -> None:
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)

    def _make_ahead(self, target: Path) -> None:
        subprocess.run(["git", "-C", str(target), "checkout", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / "new.txt").write_text("new")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "new"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "main"], capture_output=True, check=True)

    def test_fires_when_all_conditions_met(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        self._make_ahead(target)
        sha = integration_head_sha(target, "main")
        (target / ".SWE" / "doc_sync.json").write_text(json.dumps({"sha": sha}))
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_pr_title(ctx) is True

    def test_does_not_fire_when_title_already_exists(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        self._make_ahead(target)
        sha = integration_head_sha(target, "main")
        (target / ".SWE" / "doc_sync.json").write_text(json.dumps({"sha": sha}))
        (target / ".SWE" / "pr_title.json").write_text(json.dumps({"title": "SWE Pipeline: test"}))
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_pr_title(ctx) is False

    def test_does_not_fire_when_processing(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        self._make_ahead(target)
        sha = integration_head_sha(target, "main")
        (target / ".SWE" / "doc_sync.json").write_text(json.dumps({"sha": sha}))
        (target / ".SWE" / "markers" / ".processing_c_pr_title").write_text("{}")
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_pr_title(ctx) is False

    def test_does_not_fire_when_publish_preconditions_not_met(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        self._make_ahead(target)
        # No doc_sync marker — publish precondition fails
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_pr_title(ctx) is False


# ─── run_c_pr_title ─────────────────────────────────────────────────────────


class TestRunCPrTitle:
    def test_dry_run_returns_success(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target, dry_run=True)
        result = run_c_pr_title(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.dry_run is True

    def test_no_sha_returns_failure(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target, slug="owner/repo", default_branch="main")
        result = run_c_pr_title(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False

    def test_queues_agent(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        ctx = _make_tick_context(target, slug="owner/repo", default_branch="main")
        mock_handler = MagicMock()
        mock_handler.execute.return_value = ActionResult(success=True, data={"entry_id": "abc", "queue_file": "/q/abc.json"})
        with patch("cronpypeline.plugins.swe_prompts._build_queue_handler", return_value=mock_handler):
            result = run_c_pr_title(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["async"] is True
        assert result.data["entry_id"] == "abc"
        mock_handler.execute.assert_called_once()
        queued_action = mock_handler.execute.call_args[0][0]
        assert queued_action.params["agent"] == "PRReviewAgent"
        assert "SWE Pipeline:" in queued_action.params["prompt"]

    def test_queue_handler_failure(self, tmp_path, monkeypatch):
        """Covers line 3034 — handler.execute returns failure."""
        target = _make_target_dir(tmp_path)
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        ctx = _make_tick_context(target, slug="owner/repo", default_branch="main")
        mock_handler = MagicMock()
        mock_handler.execute.return_value = ActionResult(success=False)
        with patch("cronpypeline.plugins.swe_prompts._build_queue_handler", return_value=mock_handler):
            result = run_c_pr_title(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False


# ─── detect_c_pr_review ─────────────────────────────────────────────────────


class TestDetectCPrReview:
    def test_fires_when_pr_published_and_not_reviewed(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7, "pr_state": "open"}))
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "delivery": "open_pr"}}
        assert detect_c_pr_review(ctx) is True

    def test_does_not_fire_when_already_reviewed(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7, "pr_state": "open"}))
        (target / ".SWE" / "pr_reviewed.json").write_text(json.dumps({"pr_number": 7}))
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "delivery": "open_pr"}}
        assert detect_c_pr_review(ctx) is False

    def test_does_not_fire_when_no_pr(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "delivery": "open_pr"}}
        assert detect_c_pr_review(ctx) is False

    def test_does_not_fire_when_no_token(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.delenv("SWE_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin._load_env_file", lambda path: None)
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7, "pr_state": "open"}))
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "delivery": "open_pr"}}
        assert detect_c_pr_review(ctx) is False

    def test_does_not_fire_when_delivery_not_open_pr(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7, "pr_state": "open"}))
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo"}}
        assert detect_c_pr_review(ctx) is False

    def test_does_not_fire_when_max_cycles_reached(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7, "pr_state": "open", "pr_review_cycles": 3}))
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "delivery": "open_pr", "max_pr_review_cycles": 3}}
        assert detect_c_pr_review(ctx) is False

    def test_fires_when_queued_marker_old(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7, "pr_state": "open"}))
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
        (target / ".SWE" / "pr_review_queued.json").write_text(json.dumps({"queued_at": old_time}))
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "delivery": "open_pr"}}
        assert detect_c_pr_review(ctx) is True

    def test_fires_when_queued_marker_naive(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7, "pr_state": "open"}))
        (target / ".SWE" / "pr_review_queued.json").write_text(json.dumps({"queued_at": "2025-01-01T00:00:00"}))
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "delivery": "open_pr"}}
        assert detect_c_pr_review(ctx) is True

    def test_does_not_fire_when_queued_marker_recent(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7, "pr_state": "open"}))
        recent_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        (target / ".SWE" / "pr_review_queued.json").write_text(json.dumps({"queued_at": recent_time}))
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "delivery": "open_pr"}}
        assert detect_c_pr_review(ctx) is False

    def test_fires_when_queued_marker_corrupt(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7, "pr_state": "open"}))
        (target / ".SWE" / "pr_review_queued.json").write_text("bad json")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "delivery": "open_pr"}}
        assert detect_c_pr_review(ctx) is True


# ─── run_c_pr_review ────────────────────────────────────────────────────────


class TestRunCPrReview:
    def test_dry_run_returns_success(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target, dry_run=True)
        result = run_c_pr_review(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.dry_run is True

    def test_no_pr_marker_raises_error(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target, slug="owner/repo")
        with pytest.raises(FileNotFoundError):
            run_c_pr_review(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)

    def test_no_sha_returns_failure(self, tmp_path):
        target = _make_target_dir(tmp_path)
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7}))
        ctx = _make_tick_context(target, slug="owner/repo", default_branch="main")
        result = run_c_pr_review(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False
        assert "SHA" in result.stderr

    def test_reviews_pr_and_writes_marker(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7}))
        ctx = _make_tick_context(target, slug="owner/repo", default_branch="main")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        with patch("cronpypeline.plugins.swe_plugin._build_pr_review_prompt", return_value="review prompt"):
            result = run_c_pr_review(ActionSpec(type=ActionType.CUSTOM, params={"queue_dir": str(tmp_path / "queue")}), ctx)
        assert result.success is True
        marker = target / ".SWE" / "pr_review_queued.json"
        assert marker.exists()
        data = json.loads(marker.read_text())
        assert data["pr_number"] == 7


# ─── detect_c_doc_sync ──────────────────────────────────────────────────────


class TestDetectCDocSync:
    def _setup(self, target: Path) -> None:
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")

    def _setup_git(self, target: Path) -> None:
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / "new.txt").write_text("new")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "new"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "main"], capture_output=True, check=True)

    def test_fires_when_all_conditions_met(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_doc_sync(ctx) is True

    def test_does_not_fire_when_already_synced(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        sha = integration_head_sha(target, "main")
        (target / ".SWE" / "doc_sync.json").write_text(json.dumps({"sha": sha}))
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_doc_sync(ctx) is False

    def test_does_not_fire_when_no_pr(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "delivery": "open_pr"}}
        assert detect_c_doc_sync(ctx) is False

    def test_does_not_fire_when_open_issues(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        create_issue(target, issue_data={"id": "i1", "status": "open"}, body="# Issue")
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_doc_sync(ctx) is False

    def test_does_not_fire_when_no_token(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        monkeypatch.delenv("SWE_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin._load_env_file", lambda path: None)
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_doc_sync(ctx) is False

    def test_does_not_fire_when_no_delivery(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main"}}
        assert detect_c_doc_sync(ctx) is False

    def test_fires_when_queued_marker_old(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
        (target / ".SWE" / "doc_sync_queued.json").write_text(json.dumps({"queued_at": old_time}))
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_doc_sync(ctx) is True

    def test_fires_when_queued_marker_naive(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        (target / ".SWE" / "doc_sync_queued.json").write_text(json.dumps({"queued_at": "2025-01-01T00:00:00"}))
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_doc_sync(ctx) is True

    def test_does_not_fire_when_queued_marker_recent(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        recent_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        (target / ".SWE" / "doc_sync_queued.json").write_text(json.dumps({"queued_at": recent_time}))
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_doc_sync(ctx) is False

    def test_fires_when_queued_marker_corrupt(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        (target / ".SWE" / "doc_sync_queued.json").write_text("bad json")
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_doc_sync(ctx) is True

    def test_fires_when_doc_sync_sha_mismatch(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        (target / ".SWE" / "doc_sync.json").write_text(json.dumps({"sha": "different_sha"}))
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_doc_sync(ctx) is True

    def test_does_not_fire_when_doc_sync_sha_is_ancestor(self, tmp_path, monkeypatch):
        """Doc-sync agent committed on top of synced SHA, advancing HEAD.

        The old SHA in doc_sync.json is an ancestor of the current HEAD,
        so the marker should still count as done.
        """
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        # Record the SHA before the doc-sync agent's commit
        old_sha = integration_head_sha(target, "main")
        (target / ".SWE" / "doc_sync.json").write_text(json.dumps({"sha": old_sha}))
        # Simulate the doc-sync agent committing on top
        subprocess.run(["git", "-C", str(target), "checkout", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / "docs.md").write_text("# Docs\n")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "docs: sync"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "main"], capture_output=True, check=True)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_doc_sync(ctx) is False


# ─── run_c_doc_sync ─────────────────────────────────────────────────────────


class TestRunCDocSync:
    def test_dry_run_returns_success(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target, dry_run=True)
        result = run_c_doc_sync(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.dry_run is True

    def test_no_pr_marker_returns_failure(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target)
        result = run_c_doc_sync(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False

    def test_queues_doc_sync_agent(self, tmp_path):
        target = _make_target_dir(tmp_path)
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7}))
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        ctx = _make_tick_context(target, default_branch="main")
        result = run_c_doc_sync(ActionSpec(type=ActionType.CUSTOM, params={"queue_dir": str(tmp_path / "queue")}), ctx)
        assert result.success is True
        # Check that a conversation queue file was created
        queue_dir = tmp_path / "queue"
        files = list(queue_dir.glob("*.json"))
        assert len(files) > 0


# ─── detect_open_issues ─────────────────────────────────────────────────────


class TestDetectOpenIssue:
    def test_returns_true_when_open_issue_exists(self, tmp_path):
        target = _make_target_dir(tmp_path)
        create_issue(target, issue_data={"id": "i1", "status": "open"}, body="# Issue")
        ctx = {"target_dir": str(target)}
        assert detect_open_issue(ctx) is True

    def test_returns_false_when_no_open_issues(self, tmp_path):
        target = _make_target_dir(tmp_path)
        create_issue(target, issue_data={"id": "i1", "status": "done"}, body="# Issue")
        ctx = {"target_dir": str(target)}
        assert detect_open_issue(ctx) is False

    def test_returns_false_when_no_issues(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = {"target_dir": str(target)}
        assert detect_open_issue(ctx) is False


# ─── cleanup_git_branch ─────────────────────────────────────────────────────


class TestCleanupGitBranch:
    def test_cleans_up_branch(self, tmp_path):
        target = _make_target_dir(tmp_path)
        subprocess.run(["git", "init", "-b", "integration", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "task-branch"], capture_output=True, check=True)
        ctx = _make_tick_context(target)
        action = ActionSpec(type=ActionType.CUSTOM, params={"task_branch": "task-branch"})
        success, msg = cleanup_git_branch(action, ctx)
        assert success is True
        assert "task-branch" in msg

    def test_succeeds_even_without_branch(self, tmp_path):
        target = _make_target_dir(tmp_path)
        subprocess.run(["git", "init", "-b", "integration", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        ctx = _make_tick_context(target)
        action = ActionSpec(type=ActionType.CUSTOM, params={"task_branch": "nonexistent"})
        success, _msg = cleanup_git_branch(action, ctx)
        assert success is True


# ─── reset_issue_status ─────────────────────────────────────────────────────


class TestResetIssueStatus:
    def test_resets_to_open(self, tmp_path):
        target = _make_target_dir(tmp_path)
        create_issue(target, issue_data={"id": "i1", "status": "in_progress"}, body="# Issue")
        ctx = _make_tick_context(target)
        action = ActionSpec(type=ActionType.CUSTOM, params={"issue_id": "i1"})
        success, msg = reset_issue_status(action, ctx)
        assert success is True
        assert "i1" in msg

    def test_fails_when_issue_not_found(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target)
        action = ActionSpec(type=ActionType.CUSTOM, params={"issue_id": "nonexistent"})
        success, _msg = reset_issue_status(action, ctx)
        assert success is False


# ─── sync_session_mode ──────────────────────────────────────────────────────


class TestSyncSessionMode:
    def test_no_mode_file_returns_true(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = {"target_dir": str(target), "target_config": {}}
        assert sync_session_mode(ctx) is True

    def test_syncs_active_session_to_github_mode(self, tmp_path):
        target = _make_target_dir(tmp_path)
        session = {"active": True, "github_number": 42}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        mode_file = tmp_path / "mode.json"
        ctx = {"target_dir": str(target), "target_config": {"mode_file": str(mode_file)}}
        assert sync_session_mode(ctx) is True
        data = json.loads(mode_file.read_text())
        assert data["mode"] == "github"

    def test_syncs_inactive_session_to_default_mode(self, tmp_path):
        target = _make_target_dir(tmp_path)
        session = {"active": False}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        mode_file = tmp_path / "mode.json"
        ctx = {"target_dir": str(target), "target_config": {"mode_file": str(mode_file)}}
        assert sync_session_mode(ctx) is True
        data = json.loads(mode_file.read_text())
        assert data["mode"] == "default"

    def test_syncs_no_session_to_default_mode(self, tmp_path):
        target = _make_target_dir(tmp_path)
        mode_file = tmp_path / "mode.json"
        ctx = {"target_dir": str(target), "target_config": {"mode_file": str(mode_file)}}
        assert sync_session_mode(ctx) is True
        data = json.loads(mode_file.read_text())
        assert data["mode"] == "default"

    def test_syncs_corrupt_session_to_default_mode(self, tmp_path):
        target = _make_target_dir(tmp_path)
        (target / ".SWE" / "github_session.json").write_text("bad json")
        mode_file = tmp_path / "mode.json"
        ctx = {"target_dir": str(target), "target_config": {"mode_file": str(mode_file)}}
        assert sync_session_mode(ctx) is True
        data = json.loads(mode_file.read_text())
        assert data["mode"] == "default"

    def test_explicit_mode_file_param(self, tmp_path):
        target = _make_target_dir(tmp_path)
        session = {"active": True}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        mode_file = tmp_path / "explicit_mode.json"
        ctx = {"target_dir": str(target), "target_config": {}}
        assert sync_session_mode(ctx, mode_file=str(mode_file)) is True
        data = json.loads(mode_file.read_text())
        assert data["mode"] == "github"


# ─── Exception path tests ───────────────────────────────────────────────────


class TestDetectLintFailExceptionPath:
    def test_returns_false_on_read_error(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_dir = target / ".SWE" / "reports" / "lint"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "r.md"
        report_path.write_text("# Lint — FAIL\n")
        latest = report_dir / "latest.md"
        latest.symlink_to("r.md")
        # Make the report unreadable by replacing with a broken symlink
        latest.unlink()
        latest.symlink_to("/nonexistent/path")
        ctx = {"target_dir": str(target)}
        assert detect_lint_fail(ctx) is False


class TestDetectLintAutofixExceptionPath:
    def test_returns_false_on_read_error(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_dir = target / ".SWE" / "reports" / "lint"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "r.md"
        report_path.write_text("# Lint — PASS\n\n- **fixable**: 5\n")
        latest = report_dir / "latest.md"
        latest.symlink_to("r.md")
        latest.unlink()
        latest.symlink_to("/nonexistent/path")
        ctx = {"target_dir": str(target)}
        assert detect_lint_autofix(ctx) is False


class TestRunLintAutofixTimeout:
    def test_timeout_returns_failure(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "lint", "r.md", "# Lint — FAIL\n\n- **errors**: 5\n- **fixable**: 3\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        ctx = _make_tick_context(target)
        action = ActionSpec(type=ActionType.CUSTOM, params={"command": "sleep 999"})

        real_run = subprocess.run

        def _mock_run(*args, **kwargs):
            if args and args[0] == ["sleep", "999"]:
                raise subprocess.TimeoutExpired(cmd="sleep 999", timeout=600)
            return real_run(*args, **kwargs)

        with patch("cronpypeline.plugins.swe_plugin.subprocess.run", side_effect=_mock_run):
            result = run_lint_autofix(action, ctx)
        assert result.success is False
        assert "timed out" in result.stderr.lower()


class TestCommitPhaseAChangeException:
    def test_returns_none_on_git_error(self, tmp_path):
        target = _make_target_dir(tmp_path)
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        ctx = _make_tick_context(target)
        # No changes to commit → CalledProcessError
        result = commit_phase_a_change(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result is None


class TestA7CoveragePctException:
    def test_returns_none_on_read_error(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_dir = target / ".SWE" / "reports" / "coverage"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "r.md"
        report_path.write_text("# Coverage — PASS\n\n- **Coverage:** 95.0%\n")
        latest = report_dir / "latest.md"
        latest.symlink_to("r.md")
        latest.unlink()
        latest.symlink_to("/nonexistent/path")
        assert _a7_coverage_pct(target) is None


class TestOpenIssueCountException:
    def test_skips_unreadable_file(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "i1.md"
        # Create a file that will cause an exception when read
        issue_path.write_text("---\nstatus: open\n---\n# Issue")
        # Replace with broken symlink
        issue_path.unlink()
        issue_path.symlink_to("/nonexistent/path")
        # Should not crash, should return 0 since the file is unreadable
        assert _open_issue_count(target) == 0


class TestGitIssueAlreadyIngestedException:
    def test_skips_unreadable_file(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "github-1.md"
        issue_path.write_text("---\nsource: github\n---\n# Issue")
        issue_path.unlink()
        issue_path.symlink_to("/nonexistent/path")
        assert _git_issue_already_ingested(target, 1) is False


class TestFindPreviousReviewShaException:
    def test_skips_unreadable_file(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "review-abc12345.md"
        issue_path.write_text("---\nstatus: done\ntype: review\n---\n# Review")
        issue_path.unlink()
        issue_path.symlink_to("/nonexistent/path")
        assert _find_previous_review_sha(target) is None


class TestCountDoneReviewIssuesException:
    def test_skips_unreadable_file(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "review-abc12345.md"
        issue_path.write_text("---\nstatus: done\ntype: review\n---\n# Review")
        issue_path.unlink()
        issue_path.symlink_to("/nonexistent/path")
        assert _count_done_review_issues(target) == 0


class TestBuildPrBodyException:
    def test_skips_unreadable_file(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "bug-1.md"
        issue_path.write_text("---\nstatus: done\ntype: bug\n---\n# Bug")
        issue_path.unlink()
        issue_path.symlink_to("/nonexistent/path")
        body = _build_pr_body(target, "repo", "main")
        assert "## Summary" in body


class TestEnsurePhaseABranchExistingBranch:
    def test_checks_out_existing_branch(self, tmp_path):
        target = _make_target_dir(tmp_path)
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/phase-a-hygiene"], capture_output=True, check=True)
        assert ensure_phase_a_branch(target) is True
        cur = subprocess.run(["git", "-C", str(target), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, check=False)
        assert cur.stdout.strip() == "swe-pipeline/phase-a-hygiene"


class TestDetectCPrStatusCorruptReviewed:
    def test_does_not_fire_when_corrupt_reviewed_marker(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 1, "pr_state": "open"}))
        (target / ".SWE" / "pr_reviewed.json").write_text("bad json")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "delivery": "open_pr"}}
        # Corrupt reviewed marker is ignored, so it should still fire
        assert detect_c_pr_review(ctx) is True


class TestDetectCPrPublishCorruptDocSync:
    def test_does_not_fire_when_doc_sync_corrupt(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / "new.txt").write_text("new")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "new"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "main"], capture_output=True, check=True)
        (target / ".SWE" / "doc_sync.json").write_text("bad json")
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_pr_publish(ctx) is False


class TestRunCDocSyncCheckoutFailure:
    def test_checkout_failure_returns_failure(self, tmp_path):
        target = _make_target_dir(tmp_path)
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7}))
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        # No integration branch exists
        ctx = _make_tick_context(target, default_branch="main")
        result = run_c_doc_sync(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False

    def test_checkout_timeout_returns_failure(self, tmp_path):
        target = _make_target_dir(tmp_path)
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7}))
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        ctx = _make_tick_context(target, default_branch="main")

        def fake_git(repo_dir, *args, **kwargs):
            if args and args[0] == "checkout":
                raise subprocess.TimeoutExpired(cmd=["git"], timeout=60)
            if args and args[0] == "rev-parse":
                return subprocess.CompletedProcess(args=["git"], returncode=0, stdout="abcdef\n", stderr="")
            raise AssertionError(f"Unexpected git call: {args}")  # pragma: no cover

        with patch("cronpypeline.plugins.swe_plugin._git", side_effect=fake_git):
            result = run_c_doc_sync(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False
        assert "Failed to checkout" in result.stderr


class TestRunCPrPublishPushFailure:
    def test_push_failure_returns_failure(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        ctx = _make_tick_context(target, slug="owner/repo", default_branch="main")
        mock_push = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="error")
        with patch("cronpypeline.plugins.swe_plugin.subprocess.run", return_value=mock_push):
            result = run_c_pr_publish(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False
        assert "push" in result.stderr.lower()


class TestDetectCPrStatusReviewsException:
    def test_open_pr_with_review_fetch_error(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7}))
        ctx = _make_tick_context(target, slug="owner/repo")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "state": "open", "merged": False, "html_url": "https://github.com/owner/repo/pull/7",
        }).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        from urllib.error import URLError
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=[mock_resp, URLError("fail")]):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["pr_state"] == "open"


class TestDetectCPrStatusChangesRequested:
    def test_open_pr_with_changes_requested_creates_issues(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7}))
        ctx = _make_tick_context(target, slug="owner/repo")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "state": "open", "merged": False, "html_url": "https://github.com/owner/repo/pull/7",
        }).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_reviews = MagicMock()
        mock_reviews.read.return_value = json.dumps([
            {"id": 12345, "state": "CHANGES_REQUESTED", "submitted_at": "2025-01-01T00:00:00Z", "body": "## Change Requests\n\n1. Fix the bug in foo.py\n2. Add tests for bar.py"},
        ]).encode()
        mock_reviews.__enter__ = MagicMock(return_value=mock_reviews)
        mock_reviews.__exit__ = MagicMock(return_value=False)
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=[mock_resp, mock_reviews]):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["pr_state"] == "changes_requested"
        # Check that revision issues were created
        issues_dir = target / ".SWE" / "issues"
        revision_files = list(issues_dir.glob("pr-revision-7-*.md"))
        assert len(revision_files) >= 2
        # Check cycle count was updated
        pr_data = json.loads((target / ".SWE" / "pr_published.json").read_text())
        assert pr_data["pr_review_cycles"] == 1


class TestDetectCPrStatusChangesRequestedNoBody:
    def test_open_pr_with_changes_requested_empty_body(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7}))
        ctx = _make_tick_context(target, slug="owner/repo")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "state": "open", "merged": False, "html_url": "https://github.com/owner/repo/pull/7",
        }).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_reviews = MagicMock()
        mock_reviews.read.return_value = json.dumps([
            {"id": 12345, "state": "CHANGES_REQUESTED", "submitted_at": "2025-01-01T00:00:00Z", "body": ""},
        ]).encode()
        mock_reviews.__enter__ = MagicMock(return_value=mock_reviews)
        mock_reviews.__exit__ = MagicMock(return_value=False)
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=[mock_resp, mock_reviews]):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        # Cycle count still updated even with empty body
        pr_data = json.loads((target / ".SWE" / "pr_published.json").read_text())
        assert pr_data["pr_review_cycles"] == 1


class TestDetectCPrStatusChangesRequestedExistingIssues:
    def test_does_not_duplicate_existing_revision_issues(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7}))
        # Create existing revision issue
        existing = target / ".SWE" / "issues" / "pr-revision-7-1.md"
        existing.write_text("---\nstatus: open\n---\n# Existing")
        ctx = _make_tick_context(target, slug="owner/repo")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "state": "open", "merged": False, "html_url": "https://github.com/owner/repo/pull/7",
        }).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_reviews = MagicMock()
        mock_reviews.read.return_value = json.dumps([
            {"id": 12345, "state": "CHANGES_REQUESTED", "submitted_at": "2025-01-01T00:00:00Z", "body": "## Change Requests\n\n1. Fix the bug in foo.py\n2. Add tests for bar.py"},
        ]).encode()
        mock_reviews.__enter__ = MagicMock(return_value=mock_reviews)
        mock_reviews.__exit__ = MagicMock(return_value=False)
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=[mock_resp, mock_reviews]):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["pr_state"] == "changes_requested"
        # Should only create issue 2, not duplicate issue 1
        issues_dir = target / ".SWE" / "issues"
        revision_files = list(issues_dir.glob("pr-revision-7-*.md"))
        assert len(revision_files) == 3  # existing 1 + new 2 and 3


class TestRunCPrStatusMergedNoSession:
    def test_merged_without_session(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7}))
        # No github_session.json
        ctx = _make_tick_context(target, slug="owner/repo")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "state": "closed", "merged": True, "merged_at": "2025-01-01T00:00:00Z",
            "html_url": "https://github.com/owner/repo/pull/7",
        }).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", return_value=mock_resp), \
             patch("cronpypeline.plugins.swe_plugin._gh_api_post"), \
             patch("cronpypeline.plugins.swe_plugin._gh_api_patch"):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["pr_state"] == "merged"


# ─── detect_agent_forgot_marker ─────────────────────────────────────────────


class TestDetectAgentForgotMarker:
    def test_fires_when_all_conditions_met(self, tmp_path):
        target = _make_target_dir(tmp_path)
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        (target / "task.json").write_text("{}")
        ctx = {"target_dir": str(target)}
        assert detect_agent_forgot_marker(ctx) is True

    def test_does_not_fire_when_complete_marker_exists(self, tmp_path):
        target = _make_target_dir(tmp_path)
        (target / "coding_complete.marker").write_text("")
        (target / "task.json").write_text("{}")
        ctx = {"target_dir": str(target)}
        assert detect_agent_forgot_marker(ctx) is False

    def test_does_not_fire_when_no_task_json(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = {"target_dir": str(target)}
        assert detect_agent_forgot_marker(ctx) is False

    def test_does_not_fire_when_no_git_commits(self, tmp_path):
        target = _make_target_dir(tmp_path)
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        (target / "task.json").write_text("{}")
        ctx = {"target_dir": str(target)}
        assert detect_agent_forgot_marker(ctx) is False

    def test_does_not_fire_when_queue_not_empty(self, tmp_path):
        target = _make_target_dir(tmp_path)
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        (target / "task.json").write_text("{}")
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        (queue_dir / "item.json").write_text("{}")
        ctx = {"target_dir": str(target), "queue_dir": str(queue_dir)}
        assert detect_agent_forgot_marker(ctx) is False

    def test_fires_when_queue_dir_empty(self, tmp_path):
        target = _make_target_dir(tmp_path)
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        (target / "task.json").write_text("{}")
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        ctx = {"target_dir": str(target), "queue_dir": str(queue_dir)}
        assert detect_agent_forgot_marker(ctx) is True

    def test_fires_when_queue_dir_missing(self, tmp_path):
        target = _make_target_dir(tmp_path)
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        (target / "task.json").write_text("{}")
        ctx = {"target_dir": str(target), "queue_dir": str(tmp_path / "nonexistent")}
        assert detect_agent_forgot_marker(ctx) is True


# ─── Remaining exception path tests ──────────────────────────────────────────


class TestDetectReportFailExceptionPath:
    def test_returns_false_on_read_error(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_dir = target / ".SWE" / "reports" / "vulture"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "r.md"
        report_path.write_text("# Deadcode — FAIL\n")
        latest = report_dir / "latest.md"
        latest.symlink_to("r.md")
        latest.unlink()
        latest.symlink_to("/nonexistent/path")
        assert _detect_report_fail(target, "vulture") is False


class TestGitIssueAlreadyIngestedCorruptFrontmatter:
    def test_corrupt_frontmatter_returns_false(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "github-1.md"
        issue_path.write_text("not frontmatter")
        assert _git_issue_already_ingested(target, 1) is False


class TestDetectCPrPublishShaMismatch:
    def test_does_not_fire_when_doc_sync_sha_mismatch(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / "new.txt").write_text("new")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "new"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "main"], capture_output=True, check=True)
        (target / ".SWE" / "doc_sync.json").write_text(json.dumps({"sha": "wrong_sha"}))
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_pr_publish(ctx) is False


class TestDetectCPrPublishAncestorDocSync:
    def test_fires_when_doc_sync_sha_is_ancestor(self, tmp_path, monkeypatch):
        """Doc-sync agent committed on top of synced SHA, advancing HEAD.

        The old SHA in doc_sync.json is an ancestor of the current HEAD,
        so publish should proceed.
        """
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / "new.txt").write_text("new")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "new"], capture_output=True, check=True)
        # Record SHA before doc-sync agent's commit
        old_sha = integration_head_sha(target, "main")
        (target / ".SWE" / "doc_sync.json").write_text(json.dumps({"sha": old_sha}))
        # Simulate doc-sync agent committing on top
        (target / "docs.md").write_text("# Docs\n")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "docs: sync"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "main"], capture_output=True, check=True)
        (target / ".SWE" / "pr_title.json").write_text(json.dumps({"title": "SWE Pipeline: test"}))
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_pr_publish(ctx) is True


class TestRunCPrPublishTimeout:
    def test_push_timeout_returns_failure(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        ctx = _make_tick_context(target, slug="owner/repo", default_branch="main")
        real_run = subprocess.run

        def _mock_run(*args, **kwargs):
            if args and isinstance(args[0], list) and "push" in args[0]:
                raise subprocess.TimeoutExpired(cmd="git push", timeout=120)
            return real_run(*args, **kwargs)

        with patch("cronpypeline.plugins.swe_plugin.subprocess.run", side_effect=_mock_run):
            result = run_c_pr_publish(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False
        assert "timed out" in result.stderr.lower()


class TestDetectCPrReviewCorruptPrMarker:
    def test_corrupt_pr_marker_returns_false(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text("bad json")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "delivery": "open_pr"}}
        assert detect_c_pr_review(ctx) is False


class TestDetectCPrReviewNoPrNumber:
    def test_no_pr_number_returns_false(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_state": "open"}))
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "delivery": "open_pr"}}
        assert detect_c_pr_review(ctx) is False


class TestRunCPrStatusNoPrMarker:
    def test_no_pr_marker_returns_failure(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target, slug="owner/repo")
        result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False
        assert result.stderr == "PR marker file not found"

    def test_missing_pr_marker_file_returns_failure_with_stderr(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target, slug="owner/repo")
        result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert isinstance(result, ActionResult)
        assert result.success is False
        assert "marker file not found" in result.stderr


class TestRunCPrStatusNoToken:
    def test_no_token_proceeds_with_bearer_none(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.delenv("SWE_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin._load_env_file", lambda path: None)
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7}))
        ctx = _make_tick_context(target, slug="owner/repo")
        from urllib.error import URLError
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=URLError("fail")):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False


class TestRunCPrStatusNoSlug:
    def test_no_slug_raises_error(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7}))
        ctx = _make_tick_context(target)
        with pytest.raises(ValueError):
            run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)


class TestRunCPrStatusPrFetchError:
    def test_pr_fetch_error_returns_failure(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7}))
        ctx = _make_tick_context(target, slug="owner/repo")
        from urllib.error import URLError
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=URLError("fail")):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False


class TestDetectCCoverageIssuePrNotReviewed:
    def test_does_not_fire_when_pr_not_reviewed(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 50.0%\n")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7}))
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_coverage_issue(ctx) is False


class TestDetectCCoverageIssueExistingDiscarded:
    def test_fires_when_existing_issue_discarded(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 50.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        sha = integration_head_sha(target, "main")
        issue_id = f"coverage-{sha[:8]}"
        create_issue(target, issue_data={"id": issue_id, "status": "discarded"}, body="# Coverage issue")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_coverage_issue(ctx) is True


class TestDetectCReviewIssueExistingDiscarded:
    def test_fires_when_existing_issue_discarded(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        sha = integration_head_sha(target, "main")
        issue_id = f"review-{sha[:8]}"
        create_issue(target, issue_data={"id": issue_id, "status": "discarded"}, body="# Review issue")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_review_issue(ctx) is True


class TestDetectCReviewIssueMaxGens:
    def test_does_not_fire_when_max_gens_reached(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        for i in range(3):
            create_issue(target, issue_data={"id": f"review-aaa{i}1111", "status": "done", "type": "review"}, body="# Review")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_review_issue(ctx) is False


class TestRunCReviewIssuePrevSha:
    def test_creates_issue_with_prev_sha(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        create_issue(target, issue_data={"id": "review-abc12345", "status": "done", "type": "review"}, body="# Review 1")
        ctx = _make_tick_context(target, default_branch="main")
        result = run_c_review_issue(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["review_generation"] == 2


class TestRunCCoverageIssueNoSha:
    def test_no_sha_returns_failure(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target, default_branch="main")
        result = run_c_coverage_issue(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False
        assert "SHA" in result.stderr


class TestRunCReviewIssueNoSha:
    def test_no_sha_returns_failure(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target, default_branch="main")
        result = run_c_review_issue(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False
        assert "SHA" in result.stderr


# ─── More edge case tests ────────────────────────────────────────────────────


class TestDetectCCoverageIssueNoSha:
    def test_does_not_fire_when_no_sha(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 50.0%\n")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_coverage_issue(ctx) is False


class TestDetectCReviewIssueNoSha:
    def test_does_not_fire_when_no_sha(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_review_issue(ctx) is False


class TestDetectCCoverageIssueCorruptExisting:
    def test_fires_when_existing_issue_corrupt(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 50.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        sha = integration_head_sha(target, "main")
        issue_id = f"coverage-{sha[:8]}"
        # Create issue with broken symlink (corrupt)
        issue_path = target / ".SWE" / "issues" / f"{issue_id}.md"
        issue_path.write_text("---\nstatus: open\n---\n# Issue")
        issue_path.unlink()
        issue_path.symlink_to("/nonexistent/path")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_coverage_issue(ctx) is True


class TestDetectCReviewIssueCorruptExisting:
    def test_fires_when_existing_issue_corrupt(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        sha = integration_head_sha(target, "main")
        issue_id = f"review-{sha[:8]}"
        issue_path = target / ".SWE" / "issues" / f"{issue_id}.md"
        issue_path.write_text("---\nstatus: open\n---\n# Issue")
        issue_path.unlink()
        issue_path.symlink_to("/nonexistent/path")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_review_issue(ctx) is True


class TestDetectCDocSyncNoSha:
    def test_does_not_fire_when_no_sha(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_doc_sync(ctx) is False


class TestDetectCDocSyncNoCoverage:
    def test_does_not_fire_when_no_coverage(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_doc_sync(ctx) is False


class TestDetectCPrPublishNoCoverage:
    def test_does_not_fire_when_no_coverage(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_pr_publish(ctx) is False


class TestDetectCPrPublishNoDocSync:
    def test_does_not_fire_when_no_doc_sync(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / "new.txt").write_text("new")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "new"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "main"], capture_output=True, check=True)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_pr_publish(ctx) is False


class TestRunCDocSyncHandlerFailure:
    def test_handler_failure_returns_failure(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        ctx = _make_tick_context(target, default_branch="main")
        mock_handler = MagicMock()
        mock_handler.execute.return_value = ActionResult(success=False, stderr="queue failed")
        with patch("cronpypeline.plugins.swe_plugin._build_doc_sync_prompt", return_value="prompt"), \
             patch("cronpypeline.plugins.swe_prompts._build_queue_handler", return_value=mock_handler):
            result = run_c_doc_sync(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False
        assert "queue failed" in result.stderr


class TestRunCPrReviewQueuedMarkerRecent:
    def test_does_not_fire_when_queued_recently(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7}))
        recent_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        (target / ".SWE" / "pr_review_queued.json").write_text(json.dumps({"queued_at": recent_time}))
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "delivery": "open_pr"}}
        assert detect_c_pr_review(ctx) is False


class TestRunCPrReviewQueuedMarkerOld:
    def test_fires_when_queued_old(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7}))
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
        (target / ".SWE" / "pr_review_queued.json").write_text(json.dumps({"queued_at": old_time}))
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "delivery": "open_pr"}}
        assert detect_c_pr_review(ctx) is True


class TestRunCPrReviewQueuedMarkerCorrupt:
    def test_fires_when_queued_corrupt(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7}))
        (target / ".SWE" / "pr_review_queued.json").write_text("bad json")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "delivery": "open_pr"}}
        assert detect_c_pr_review(ctx) is True


class TestRunCPrReviewCycleLimit:
    def test_does_not_fire_when_cycle_limit_reached(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7, "pr_review_cycles": 10}))
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "delivery": "open_pr", "max_pr_review_cycles": 3}}
        assert detect_c_pr_review(ctx) is False


class TestDetectCPrPublishNoSha:
    def test_does_not_fire_when_no_sha(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        # No integration branch → no sha
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_pr_publish(ctx) is False


class TestDetectCPrPublishCorruptPrMarker:
    def test_fires_when_pr_marker_corrupt(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / "new.txt").write_text("new")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "new"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "main"], capture_output=True, check=True)
        sha = integration_head_sha(target, "main")
        (target / ".SWE" / "doc_sync.json").write_text(json.dumps({"sha": sha}))
        (target / ".SWE" / "pr_published.json").write_text("bad json")
        (target / ".SWE" / "pr_title.json").write_text(json.dumps({"title": "SWE Pipeline: test"}))
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_pr_publish(ctx) is True


class TestDetectCPrPublishPrMarkerNoNumber:
    def test_fires_when_pr_marker_has_no_number(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / "new.txt").write_text("new")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "new"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "main"], capture_output=True, check=True)
        sha = integration_head_sha(target, "main")
        (target / ".SWE" / "doc_sync.json").write_text(json.dumps({"sha": sha}))
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_state": "open"}))
        (target / ".SWE" / "pr_title.json").write_text(json.dumps({"title": "SWE Pipeline: test"}))
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_pr_publish(ctx) is True


class TestDetectCDocSyncNoSlug:
    def test_does_not_fire_when_no_slug(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / "new.txt").write_text("new")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "new"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "main"], capture_output=True, check=True)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "no-slash", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_doc_sync(ctx) is False


class TestDetectCDocSyncCorruptDoneMarker:
    def test_fires_when_done_marker_corrupt(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / "new.txt").write_text("new")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "new"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "main"], capture_output=True, check=True)
        (target / ".SWE" / "doc_sync.json").write_text("bad json")
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_doc_sync(ctx) is True


class TestRunCPrReviewHandlerFailure:
    def test_handler_failure_returns_failure(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7}))
        ctx = _make_tick_context(target, slug="owner/repo")
        mock_handler = MagicMock()
        mock_handler.execute.return_value = ActionResult(success=False, stderr="review queue failed")
        with patch("cronpypeline.plugins.swe_prompts._build_queue_handler", return_value=mock_handler):
            result = run_c_pr_review(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False
        assert "review queue failed" in result.stderr


class TestRunCPrStatusChangesRequestedNonNumericIssue:
    def test_non_numeric_issue_suffix_handled(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7}))
        # Create existing issue with non-numeric suffix to trigger ValueError
        (target / ".SWE" / "issues" / "pr-revision-7-abc.md").write_text("---\nid: pr-revision-7-abc\nstatus: open\n---\n# Issue")
        ctx = _make_tick_context(target, slug="owner/repo")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "state": "open",
            "number": 7,
            "merged": False,
            "head": {"ref": "swe-pipeline/integration"},
            "html_url": "https://github.com/owner/repo/pull/7",
        }).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_reviews = MagicMock()
        mock_reviews.read.return_value = json.dumps([
            {"state": "CHANGES_REQUESTED", "submitted_at": "2025-01-01T00:00:00Z", "body": "## Change Requests\n\n1. Fix the bug"},
        ]).encode()
        mock_reviews.__enter__ = MagicMock(return_value=mock_reviews)
        mock_reviews.__exit__ = MagicMock(return_value=False)
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=[mock_resp, mock_reviews]):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["pr_state"] == "open"


class TestDetectLintAutofixReadError:
    def test_returns_false_on_read_error(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_dir = target / ".SWE" / "reports" / "lint"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "r.md"
        report_path.write_text("# Lint — FAIL\n\n- **errors**: 5\n- **fixable**: 3\n")
        latest = report_dir / "latest.md"
        latest.symlink_to("r.md")
        latest.unlink()
        latest.symlink_to("/nonexistent/path")
        ctx = {"target_dir": str(target)}
        assert detect_lint_autofix(ctx) is False


class TestA7CoveragePctReadError:
    def test_returns_none_on_read_error(self, tmp_path):
        from cronpypeline.plugins.swe_plugin import _a7_coverage_pct
        target = _make_target_dir(tmp_path)
        report_dir = target / ".SWE" / "reports" / "coverage"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "r.md"
        report_path.write_text("# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        latest = report_dir / "latest.md"
        latest.symlink_to("r.md")
        latest.unlink()
        latest.symlink_to("/nonexistent/path")
        assert _a7_coverage_pct(target) is None


class TestCountDoneReviewIssuesNonFrontmatter:
    def test_skips_non_frontmatter_files(self, tmp_path):
        from cronpypeline.plugins.swe_plugin import _count_done_review_issues
        target = _make_target_dir(tmp_path)
        # Create a non-frontmatter file that doesn't start with ---
        (target / ".SWE" / "issues" / "review-abc12345.md").write_text("not frontmatter")
        result = _count_done_review_issues(target)
        assert result == 0


class TestCountDoneReviewIssuesCorrupt:
    def test_skips_corrupt_files(self, tmp_path):
        from cronpypeline.plugins.swe_plugin import _count_done_review_issues
        target = _make_target_dir(tmp_path)
        # Create a broken symlink issue
        issue_path = target / ".SWE" / "issues" / "review-abc12345.md"
        issue_path.symlink_to("/nonexistent/path")
        result = _count_done_review_issues(target)
        assert result == 0


class TestDetectLintFailReadError:
    def test_returns_false_on_read_error(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_dir = target / ".SWE" / "reports" / "lint"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "r.md"
        report_path.write_text("# Lint — FAIL\n\n- **errors**: 5\n")
        latest = report_dir / "latest.md"
        latest.symlink_to("r.md")
        latest.unlink()
        latest.symlink_to("/nonexistent/path")
        ctx = {"target_dir": str(target)}
        assert detect_lint_fail(ctx) is False


class TestDetectDeadcodeFailReadError:
    def test_returns_false_on_read_error(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_dir = target / ".SWE" / "reports" / "vulture"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "r.md"
        report_path.write_text("# Deadcode — FAIL\n")
        latest = report_dir / "latest.md"
        latest.symlink_to("r.md")
        latest.unlink()
        latest.symlink_to("/nonexistent/path")
        ctx = {"target_dir": str(target)}
        assert detect_vulture_fail(ctx) is False


class TestDetectTypecheckFailReadError:
    def test_returns_false_on_read_error(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_dir = target / ".SWE" / "reports" / "typecheck"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "r.md"
        report_path.write_text("# Typecheck — FAIL\n")
        latest = report_dir / "latest.md"
        latest.symlink_to("r.md")
        latest.unlink()
        latest.symlink_to("/nonexistent/path")
        ctx = {"target_dir": str(target)}
        assert detect_typecheck_fail(ctx) is False


class TestDetectSecurityFailReadError:
    def test_returns_false_on_read_error(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_dir = target / ".SWE" / "reports" / "security"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "r.md"
        report_path.write_text("# Security — FAIL\n")
        latest = report_dir / "latest.md"
        latest.symlink_to("r.md")
        latest.unlink()
        latest.symlink_to("/nonexistent/path")
        ctx = {"target_dir": str(target)}
        assert detect_security_fail(ctx) is False


class TestDetectDocstringFailReadError:
    def test_returns_false_on_read_error(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_dir = target / ".SWE" / "reports" / "docstring"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / "r.md"
        report_path.write_text("# Docstring — FAIL\n")
        latest = report_dir / "latest.md"
        latest.symlink_to("r.md")
        latest.unlink()
        latest.symlink_to("/nonexistent/path")
        ctx = {"target_dir": str(target)}
        assert detect_docstring_fail(ctx) is False


class TestGitIssueAlreadyIngestedExceptionPath:
    def test_corrupt_frontmatter_exception(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "github-1.md"
        issue_path.symlink_to("/nonexistent/path")
        assert _git_issue_already_ingested(target, 1) is False


class TestOpenIssueCountCorrupt:
    def test_corrupt_issue_skipped(self, tmp_path):
        target = _make_target_dir(tmp_path)
        # Create a broken symlink issue
        issue_path = target / ".SWE" / "issues" / "github-1.md"
        issue_path.symlink_to("/nonexistent/path")
        # Should not crash, should return 0
        assert _open_issue_count(target) == 0


class TestDetectAgentForgotMarkerGitTimeout:
    def test_returns_false_on_git_timeout(self, tmp_path):
        target = _make_target_dir(tmp_path)
        (target / "task.json").write_text("{}")
        ctx = {"target_dir": str(target)}
        with patch("cronpypeline.plugins.swe_plugin.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=10)):
            assert detect_agent_forgot_marker(ctx) is False


class TestDetectAgentForgotMarkerFileNotFound:
    def test_returns_false_on_filenotfound(self, tmp_path):
        target = _make_target_dir(tmp_path)
        (target / "task.json").write_text("{}")
        ctx = {"target_dir": str(target)}
        with patch("cronpypeline.plugins.swe_plugin.subprocess.run", side_effect=FileNotFoundError()):
            assert detect_agent_forgot_marker(ctx) is False


class TestFindIssueByIdCorrupt:
    def test_broken_symlink_returns_none(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "i1.md"
        issue_path.symlink_to("/nonexistent/path")
        # _find_issue_by_id uses .exists() which returns False for broken symlinks
        result = _find_issue_by_id(target, "i1")
        assert result is None


class TestCleanupGitBranchTimeout:
    def test_timeout_does_not_crash(self, tmp_path):
        target = _make_target_dir(tmp_path)
        subprocess.run(["git", "init", "-b", "integration", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        ctx = _make_tick_context(target)
        with patch("cronpypeline.plugins.swe_plugin.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30)):
            success, _msg = cleanup_git_branch(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert success is True


class TestRunLintAutofixUnlinkOSError:
    def test_unlink_oserror_does_not_crash(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "lint", "r.md", "# Lint — FAIL\n\n- **errors**: 5\n- **fixable**: 3\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        # Make latest.md a directory to trigger OSError on unlink
        latest = target / ".SWE" / "reports" / "lint" / "latest.md"
        if latest.exists() or latest.is_symlink():
            latest.unlink()
        latest.mkdir()
        ctx = _make_tick_context(target)
        action = ActionSpec(type=ActionType.CUSTOM, params={"command": "true"})
        mock_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="Fixed 3 errors.", stderr="")
        real_run = subprocess.run

        def _mock_run(*args, **kwargs):
            if args and args[0] == ["true"]:
                return mock_proc
            return real_run(*args, **kwargs)

        with patch("cronpypeline.plugins.swe_plugin.subprocess.run", side_effect=_mock_run):
            result = run_lint_autofix(action, ctx)
        assert result.success is True


class TestFindPreviousReviewShaCorrupt:
    def test_skips_non_frontmatter(self, tmp_path):
        from cronpypeline.plugins.swe_plugin import _find_previous_review_sha
        target = _make_target_dir(tmp_path)
        (target / ".SWE" / "issues" / "review-abc12345.md").write_text("not frontmatter")
        result = _find_previous_review_sha(target)
        assert result is None


class TestFindPreviousReviewShaNoType:
    def test_skips_when_no_type_review(self, tmp_path):
        from cronpypeline.plugins.swe_plugin import _find_previous_review_sha
        target = _make_target_dir(tmp_path)
        create_issue(target, issue_data={"id": "review-abc12345", "status": "done", "type": "bug"}, body="# Review")
        result = _find_previous_review_sha(target)
        assert result is None


class TestDetectCCoverageIssueCorruptExistingException:
    def test_fires_when_existing_issue_read_exception(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 50.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        sha = integration_head_sha(target, "main")
        issue_id = f"coverage-{sha[:8]}"
        # Create a broken symlink
        issue_path = target / ".SWE" / "issues" / f"{issue_id}.md"
        issue_path.symlink_to("/nonexistent/path")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_coverage_issue(ctx) is True


class TestDetectCReviewIssueCorruptExistingException:
    def test_fires_when_existing_issue_read_exception(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        sha = integration_head_sha(target, "main")
        issue_id = f"review-{sha[:8]}"
        issue_path = target / ".SWE" / "issues" / f"{issue_id}.md"
        issue_path.symlink_to("/nonexistent/path")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_review_issue(ctx) is True


class TestDetectCPrPublishNotAhead:
    def test_does_not_fire_when_not_ahead(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        # No commits on integration branch → not ahead
        sha = integration_head_sha(target, "main")
        (target / ".SWE" / "doc_sync.json").write_text(json.dumps({"sha": sha}))
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_pr_publish(ctx) is False


class TestDetectCDocSyncNotAhead:
    def test_does_not_fire_when_not_ahead(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        assert detect_c_doc_sync(ctx) is False


# ─── Coverage gap tests ──────────────────────────────────────────────────────


class TestDetectLintFailDirectoryReport:
    """Cover lines 98-99: read_text raises IsADirectoryError when latest.md is a dir."""

    def test_returns_false_on_directory_report(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_dir = target / ".SWE" / "reports" / "lint"
        report_dir.mkdir(parents=True, exist_ok=True)
        latest = report_dir / "latest.md"
        latest.mkdir()
        ctx = {"target_dir": str(target)}
        assert detect_lint_fail(ctx) is False


class TestDetectReportFailDirectoryReport:
    """Cover lines 128-129: read_text raises IsADirectoryError when latest.md is a dir."""

    def test_returns_false_on_directory_report(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_dir = target / ".SWE" / "reports" / "vulture"
        report_dir.mkdir(parents=True, exist_ok=True)
        latest = report_dir / "latest.md"
        latest.mkdir()
        assert _detect_report_fail(target, "vulture") is False


class TestDetectVultureFailDirectoryReport:
    """Cover lines 195-196: read_text raises IsADirectoryError when latest.md is a dir."""

    def test_returns_false_on_directory_report(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_dir = target / ".SWE" / "reports" / "deadcode"
        report_dir.mkdir(parents=True, exist_ok=True)
        latest = report_dir / "latest.md"
        latest.mkdir()
        ctx = {"target_dir": str(target)}
        assert detect_vulture_fail(ctx) is False


class TestDetectSessionCompleteIssueDirectory:
    """Cover lines 237-238: read_text raises when issue_path is a directory."""

    def test_returns_false_when_issue_is_directory(self, tmp_path):
        target = _make_target_dir(tmp_path)
        session = {"active": True, "issue_id": "github-1"}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        issue_path = target / ".SWE" / "issues" / "github-1.md"
        issue_path.mkdir()
        assert detect_session_complete({"target_dir": str(target)}) is False


class TestFinalizeSessionIssueDirectory:
    """Cover lines 302-303: read_text raises when issue_path is a directory."""

    def test_finalizes_when_issue_is_directory(self, tmp_path):
        target = _make_target_dir(tmp_path)
        session = {"active": True, "issue_id": "github-1"}
        (target / ".SWE" / "github_session.json").write_text(json.dumps(session))
        issue_path = target / ".SWE" / "issues" / "github-1.md"
        issue_path.mkdir()
        ctx = _make_tick_context(target)
        result = finalize_session(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True


class TestDetectLintAutofixDirectoryReport:
    """Cover lines 532-533: read_text raises IsADirectoryError when latest.md is a dir."""

    def test_returns_false_on_directory_report(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_dir = target / ".SWE" / "reports" / "lint"
        report_dir.mkdir(parents=True, exist_ok=True)
        latest = report_dir / "latest.md"
        latest.mkdir()
        ctx = {"target_dir": str(target)}
        assert detect_lint_autofix(ctx) is False


class TestFindActiveTaskNonDirEntry:
    """Cover line 966: task_dir is not a directory (file inside date_dir)."""

    def test_skips_non_dir_task_entries(self, tmp_path, monkeypatch):
        date_dir = tmp_path / "2025-01-01"
        date_dir.mkdir()
        (date_dir / "file.txt").write_text("not a dir")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path)
        assert _find_active_task("repo") is None


class TestFindActiveTaskNoTaskJson:
    """Cover line 968: task_dir exists but has no task.json."""

    def test_skips_task_dir_without_task_json(self, tmp_path, monkeypatch):
        date_dir = tmp_path / "2025-01-01"
        task_dir = date_dir / "task-001"
        task_dir.mkdir(parents=True)
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path)
        assert _find_active_task("repo") is None


class TestCountOpenReviewIssuesDirectory:
    """Cover lines 995-996: read_text raises when issue path is a directory."""

    def test_skips_directory_issue(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "review-1.md"
        issue_path.mkdir()
        assert _count_open_review_issues(target) == 0


class TestA7CoveragePctDirectoryReport:
    """Cover lines 1248-1249: read_text raises IsADirectoryError when latest.md is a dir."""

    def test_returns_none_on_directory_report(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_dir = target / ".SWE" / "reports" / "coverage"
        report_dir.mkdir(parents=True, exist_ok=True)
        latest = report_dir / "latest.md"
        latest.mkdir()
        assert _a7_coverage_pct(target) is None


class TestBuildPrBodyNonFrontmatter:
    """Cover line 1352: issue file without frontmatter (doesn't start with ---)."""

    def test_skips_non_frontmatter_issue(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "bug-1.md"
        issue_path.write_text("just a plain markdown file without frontmatter")
        body = _build_pr_body(target, "repo", "main")
        assert "0 issues fixed" in body


class TestDetectCCoverageIssueDirectoryExisting:
    """Cover lines 1810-1812: existing issue is a directory, read_text raises."""

    def test_fires_when_existing_issue_is_directory(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 50.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        sha = integration_head_sha(target, "main")
        issue_id = f"coverage-{sha[:8]}"
        issue_path = target / ".SWE" / "issues" / f"{issue_id}.md"
        issue_path.mkdir()
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_coverage_issue(ctx) is True

    def test_does_not_fire_when_existing_issue_open(self, tmp_path):
        """Cover line 1810: existing issue has non-discarded status."""
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 50.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        sha = integration_head_sha(target, "main")
        issue_id = f"coverage-{sha[:8]}"
        issue_path = target / ".SWE" / "issues" / f"{issue_id}.md"
        issue_path.write_text("---\nstatus: done\n---\n# Coverage issue\n")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_coverage_issue(ctx) is False


class TestDetectCReviewIssueA1Fail:
    """Cover line 1925: _a1_is_pass returns False."""

    def test_does_not_fire_when_a1_fails(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — FAIL\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_review_issue(ctx) is False


class TestDetectCReviewIssueDirectoryExisting:
    """Cover lines 1942-1944: existing issue is a directory, read_text raises."""

    def test_fires_when_existing_issue_is_directory(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        sha = integration_head_sha(target, "main")
        issue_id = f"review-{sha[:8]}"
        issue_path = target / ".SWE" / "issues" / f"{issue_id}.md"
        issue_path.mkdir()
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_review_issue(ctx) is True

    def test_does_not_fire_when_existing_issue_open(self, tmp_path):
        """Cover line 1942: existing issue has non-discarded status."""
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        sha = integration_head_sha(target, "main")
        issue_id = f"review-{sha[:8]}"
        issue_path = target / ".SWE" / "issues" / f"{issue_id}.md"
        issue_path.write_text("---\nstatus: done\n---\n# Review issue\n")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main"}}
        assert detect_c_review_issue(ctx) is False


class TestDetectCDocSyncValueError:
    """Cover lines 2037-2038: ValueError when git rev-list returns non-integer."""

    def test_does_not_fire_on_value_error(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / "new.txt").write_text("new")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "new"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "main"], capture_output=True, check=True)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")

        real_run = subprocess.run

        def _mock_run(*args, **kwargs):
            if args and isinstance(args[0], list) and "rev-list" in args[0]:
                return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="not a number\n", stderr="")
            return real_run(*args, **kwargs)

        with patch("cronpypeline.plugins.swe_plugin.subprocess.run", side_effect=_mock_run):
            ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
            assert detect_c_doc_sync(ctx) is False


class TestDetectCPrPublishNoShaMocked:
    """Cover line 2155: integration_head_sha returns None (mocked)."""

    def test_does_not_fire_when_no_sha(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
        with patch("cronpypeline.plugins.swe_plugin.integration_head_sha", return_value=None):
            assert detect_c_pr_publish(ctx) is False


class TestDetectCPrPublishValueError:
    """Cover lines 2165-2166: ValueError when git rev-list returns non-integer."""

    def test_does_not_fire_on_value_error(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / "new.txt").write_text("new")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "new"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "main"], capture_output=True, check=True)
        sha = integration_head_sha(target, "main")
        (target / ".SWE" / "doc_sync.json").write_text(json.dumps({"sha": sha}))
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")

        real_run = subprocess.run

        def _mock_run(*args, **kwargs):
            if args and isinstance(args[0], list) and "rev-list" in args[0]:
                return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="not a number\n", stderr="")
            return real_run(*args, **kwargs)

        with patch("cronpypeline.plugins.swe_plugin.subprocess.run", side_effect=_mock_run):
            ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr"}}
            assert detect_c_pr_publish(ctx) is False


# ─── Helper: build a mocked urlopen response ────────────────────────────────


def _mock_http_response(payload: Any) -> MagicMock:
    """Build a mock HTTP response context manager for urlopen patching."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ─── _write_report helper (latest.md overwrite) ─────────────────────────────


class TestWriteReportHelper:
    def test_overwrites_existing_latest_symlink(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "lint", "r1.md", "# Lint 1\n")
        _write_report(target, "lint", "r2.md", "# Lint 2\n")
        latest = target / ".SWE" / "reports" / "lint" / "latest.md"
        assert latest.is_symlink()
        assert latest.resolve().name == "r2.md"


# ─── _load_github_token dotenv fallback ─────────────────────────────────────


class TestLoadGithubTokenDotenvFallback:
    def test_loads_token_from_dotenv(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SWE_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        from cronpypeline.plugins import swe_plugin
        swe_dir = tmp_path / "workspace" / "tasks" / "x"
        swe_dir.mkdir(parents=True)
        env_file = swe_dir.parent.parent / ".env"
        env_file.write_text("SWE_GITHUB_TOKEN=dotenv-token\n")
        monkeypatch.setattr(swe_plugin, "SWE_WORKSPACE_DIR", swe_dir)

        import types
        dotenv_mod = types.ModuleType("dotenv")

        def load_dotenv(env_file, override=False):
            monkeypatch.setenv("SWE_GITHUB_TOKEN", "dotenv-token")

        dotenv_mod.load_dotenv = load_dotenv
        with patch.dict("sys.modules", {"dotenv": dotenv_mod}):
            assert _load_github_token({}) == "dotenv-token"

    def test_fallback_parser_when_dotenv_not_installed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SWE_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        from cronpypeline.plugins import swe_plugin
        swe_dir = tmp_path / "workspace" / "tasks" / "x"
        swe_dir.mkdir(parents=True)
        env_file = swe_dir.parent.parent / ".env"
        env_file.write_text("SWE_GITHUB_TOKEN=dotenv-token\n")
        monkeypatch.setattr(swe_plugin, "SWE_WORKSPACE_DIR", swe_dir)
        # dotenv is not installed → inline fallback parser still reads .env
        with patch.dict("sys.modules", {"dotenv": None}):
            assert _load_github_token({}) == "dotenv-token"

    def test_load_env_file_fallback_parser(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SWE_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("OTHER_VAR", raising=False)
        monkeypatch.delenv("PLAIN_VAR", raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text(
            "\n"
            "# a comment line\n"
            "SOME_MALFORMED_LINE\n"
            'SWE_GITHUB_TOKEN="quoted-token"\n'
            "OTHER_VAR='single-quoted'\n"
            "PLAIN_VAR=plain-token\n",
            encoding="utf-8",
        )
        with patch.dict("sys.modules", {"dotenv": None}):
            _load_env_file(env_file)
        assert os.environ["SWE_GITHUB_TOKEN"] == "quoted-token"
        assert os.environ["OTHER_VAR"] == "single-quoted"
        assert os.environ["PLAIN_VAR"] == "plain-token"
        assert "SOME_MALFORMED_LINE" not in os.environ


# ─── run_c_pr_status review branches ────────────────────────────────────────


class TestRunCPrStatusReviews:
    def test_approved_review_updates_marker(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7, "pr_state": "open"}))
        ctx = _make_tick_context(target, slug="owner/repo")
        pr_resp = _mock_http_response({"state": "open", "merged": False})
        reviews_resp = _mock_http_response([{"id": 100, "state": "APPROVED", "body": "LGTM"}])
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=[pr_resp, reviews_resp]):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["pr_state"] == "approved"
        pr_data = json.loads((target / ".SWE" / "pr_published.json").read_text())
        assert pr_data["pr_state"] == "approved"
        assert pr_data["last_review_id"] == 100

    def test_commented_review_without_changes_keywords(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7, "pr_state": "open"}))
        ctx = _make_tick_context(target, slug="owner/repo")
        pr_resp = _mock_http_response({"state": "open", "merged": False})
        reviews_resp = _mock_http_response([{"id": 101, "state": "COMMENTED", "body": "Just a note"}])
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=[pr_resp, reviews_resp]):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["pr_state"] == "open"

    def test_commented_review_requesting_changes_treated_as_changes_requested(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7, "pr_state": "open"}))
        ctx = _make_tick_context(target, slug="owner/repo")
        pr_resp = _mock_http_response({"state": "open", "merged": False})
        reviews_resp = _mock_http_response([{"id": 102, "state": "COMMENTED", "body": "Please request changes before merge"}])
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=[pr_resp, reviews_resp]):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["pr_state"] == "changes_requested"

    def test_changes_requested_cycle_limit(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(
            json.dumps({"pr_number": 7, "pr_state": "open", "pr_review_cycles": 2})
        )
        ctx = _make_tick_context(target, slug="owner/repo", max_pr_review_cycles=2)
        pr_resp = _mock_http_response({"state": "open", "merged": False})
        reviews_resp = _mock_http_response([{"id": 200, "state": "CHANGES_REQUESTED", "body": "## Change Requests\n\n1. Fix bug"}])
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=[pr_resp, reviews_resp]):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["pr_state"] == "changes_requested"
        assert not list((target / ".SWE" / "issues").glob("pr-revision-*.md"))
        pr_data = json.loads((target / ".SWE" / "pr_published.json").read_text())
        assert pr_data["last_review_id"] == 200

    def test_changes_requested_with_non_numeric_existing_issue(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({"pr_number": 7, "pr_state": "open"}))
        (target / ".SWE" / "issues" / "pr-revision-7-abc.md").write_text("---\nstatus: open\n---\n# Existing")
        ctx = _make_tick_context(target, slug="owner/repo")
        pr_resp = _mock_http_response({"state": "open", "merged": False})
        reviews_resp = _mock_http_response([{"id": 300, "state": "CHANGES_REQUESTED", "body": "## Change Requests\n\n1. Fix bug"}])
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=[pr_resp, reviews_resp]):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["pr_state"] == "changes_requested"

    def test_changes_requested_already_handled_all_done_pushes(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({
            "pr_number": 7,
            "pr_state": "changes_requested",
            "last_review_id": 400,
            "filed_issues": ["pr-revision-7-1"],
            "pr_review_cycles": 1,
        }))
        (target / ".SWE" / "issues" / "pr-revision-7-1.md").write_text("---\nstatus: done\n---\n# Done")
        ctx = _make_tick_context(target, slug="owner/repo")
        pr_resp = _mock_http_response({"state": "open", "merged": False})
        reviews_resp = _mock_http_response([{"id": 400, "state": "CHANGES_REQUESTED", "body": "needs work"}])

        def _mock_run(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=[pr_resp, reviews_resp]), \
             patch("cronpypeline.plugins.swe_plugin.integration_head_sha", return_value="abc12345"), \
             patch("cronpypeline.plugins.swe_plugin.subprocess.run", side_effect=_mock_run):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["pr_state"] == "open"

    def test_changes_requested_all_done_push_failure(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({
            "pr_number": 7,
            "pr_state": "changes_requested",
            "last_review_id": 400,
            "filed_issues": ["pr-revision-7-1"],
            "pr_review_cycles": 1,
        }))
        (target / ".SWE" / "issues" / "pr-revision-7-1.md").write_text("---\nstatus: done\n---\n# Done")
        ctx = _make_tick_context(target, slug="owner/repo")
        pr_resp = _mock_http_response({"state": "open", "merged": False})
        reviews_resp = _mock_http_response([{"id": 400, "state": "CHANGES_REQUESTED", "body": "needs work"}])
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=[pr_resp, reviews_resp]), \
             patch("cronpypeline.plugins.swe_plugin.integration_head_sha", return_value="abc12345"), \
             patch("cronpypeline.plugins.swe_plugin.subprocess.run",
                   return_value=subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="rejected")):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False
        assert "Push failed" in result.stderr


# ─── run_c_coverage_issue per-file gaps ─────────────────────────────────────


class TestRunCCoverageIssueGaps:
    def test_parses_per_file_gaps(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report = (
            "# Coverage — PASS\n\n- **Coverage:** 80.0%\n\n"
            "## stdout\n```\n"
            "foo.py    10    2    80%    5-6\n"
            "bar.py    20    0    100%\n"
            "TOTAL     30    2    93%\n"
            "```\n"
        )
        _write_report(target, "coverage", "r.md", report)
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        ctx = _make_tick_context(target, default_branch="main")
        result = run_c_coverage_issue(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        issue_file = target / ".SWE" / "issues" / f"{result.data['issue_id']}.md"
        content = issue_file.read_text()
        assert "foo.py" in content
        assert "80%" in content

    def test_skips_total_line_via_finditer_mock(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 80.0%\n\n## stdout\n```\nfoo.py    10    2    80%\n```\n")
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        ctx = _make_tick_context(target, default_branch="main")

        fake_match = MagicMock()
        fake_match.group.side_effect = lambda g: "TOTAL" if g == 1 else "10"

        with patch("cronpypeline.plugins.swe_plugin.re.finditer", return_value=[fake_match]):
            result = run_c_coverage_issue(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True

    def test_oserror_reading_report_is_ignored(self, tmp_path):
        target = _make_target_dir(tmp_path)
        report_dir = target / ".SWE" / "reports" / "coverage"
        report_dir.mkdir(parents=True, exist_ok=True)
        latest = report_dir / "latest.md"
        latest.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        ctx = _make_tick_context(target, default_branch="main")
        result = run_c_coverage_issue(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True


# ─── dependency audit helpers ───────────────────────────────────────────────


class TestNormalizePkgName:
    def test_normalizes(self):
        assert _normalize_pkg_name("Some.Pkg_Name") == "some-pkg-name"


class TestSlugify:
    def test_slugifies(self):
        assert _slugify("Some Pkg!!!") == "some-pkg"
        assert _slugify("  Hello World  ") == "hello-world"


class TestVenvBinary:
    def test_returns_venv_binary_when_present(self, tmp_path):
        target = _make_target_dir(tmp_path)
        (target / ".venv" / "bin").mkdir(parents=True)
        (target / ".venv" / "bin" / "bandit").write_text("")
        assert _venv_binary(target, "bandit") == str(target / ".venv" / "bin" / "bandit")

    def test_returns_bare_name_when_missing(self, tmp_path):
        target = _make_target_dir(tmp_path)
        assert _venv_binary(target, "bandit") == "bandit"


class TestParsePipAuditVulnerabilities:
    def test_parses_vulns(self):
        output = (
            "Name      Version  ID                  Fix Versions\n"
            "--------  -------  ------------------  ------------\n"
            "requests  2.31.0   PYSEC-2023-74       2.32.0\n"
            "urllib3   1.26.15  GHSA-v845-jxx5-vc9f 1.26.16, 2.0.4\n"
            "Found 2 known vulnerabilities in 2 packages\n"
        )
        vulns = _parse_pip_audit_vulnerabilities(output)
        assert len(vulns) == 2
        assert vulns[0]["name"] == "requests"
        assert vulns[0]["fix_versions"] == ["2.32.0"]
        assert vulns[1]["id"] == "GHSA-v845-jxx5-vc9f"
        assert vulns[1]["fix_versions"] == ["1.26.16", "2.0.4"]

    def test_returns_empty_without_header(self):
        assert _parse_pip_audit_vulnerabilities("no table here") == []

    def test_skips_separator_and_short_lines(self):
        output = (
            "Name    Version  ID\n"
            "------- -------- ---\n"
            "---\n"
            "short\n"
            "No known vulnerabilities found\n"
        )
        assert _parse_pip_audit_vulnerabilities(output) == []

    def test_stops_on_warning_prefix(self):
        output = (
            "Name    Version  ID\n"
            "------- -------- ---\n"
            "WARNING: something\n"
            "pkg 1.0 ID-1\n"
        )
        assert _parse_pip_audit_vulnerabilities(output) == []


# ─── diagnostic runner wrappers (A5-A9) ─────────────────────────────────────


class TestRunDiagnosticWrappers:
    def _capture(self):
        captured = {}

        def fake_run_diagnostic(action, context):
            captured["action"] = action
            return ActionResult(success=True, data={"report_path": "/tmp/r.md"})

        return captured, fake_run_diagnostic

    def test_run_a7_coverage_default_threshold(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target)
        captured, fake = self._capture()
        with patch("cronpypeline.plugins.swe_diagnostics.run_diagnostic", side_effect=fake):
            result = run_a7_coverage(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert captured["action"].params["parser_kwargs"]["threshold"] == 80.0

    def test_run_a7_coverage_custom_threshold(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target, coverage_threshold=75.5)
        captured, fake = self._capture()
        with patch("cronpypeline.plugins.swe_diagnostics.run_diagnostic", side_effect=fake):
            run_a7_coverage(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert captured["action"].params["parser_kwargs"]["threshold"] == 75.5

    def test_run_a7_coverage_uses_coverage_cmd(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target, coverage_cmd="pytest --cov=foo .")
        captured, fake = self._capture()
        with patch("cronpypeline.plugins.swe_diagnostics.run_diagnostic", side_effect=fake):
            run_a7_coverage(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert captured["action"].params["command"] == "pytest --cov=foo ."

    def test_run_a7_coverage_venv_binary_when_present(self, tmp_path):
        target = _make_target_dir(tmp_path)
        (target / ".venv" / "bin").mkdir(parents=True)
        (target / ".venv" / "bin" / "pytest").write_text("")
        ctx = _make_tick_context(target)
        captured, fake = self._capture()
        with patch("cronpypeline.plugins.swe_diagnostics.run_diagnostic", side_effect=fake):
            run_a7_coverage(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        venv = str(target / ".venv" / "bin" / "pytest")
        assert captured["action"].params["command"] == f"{venv} --cov=cronpypeline --cov-report=term-missing"

    def test_run_a7_coverage_falls_back_to_pytest(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target)
        captured, fake = self._capture()
        with patch("cronpypeline.plugins.swe_diagnostics.run_diagnostic", side_effect=fake):
            run_a7_coverage(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert captured["action"].params["command"] == "pytest --cov=cronpypeline --cov-report=term-missing"

    def test_run_a5_bandit_uses_security_cmd(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target, security_cmd="bandit -r .")
        captured, fake = self._capture()
        with patch("cronpypeline.plugins.swe_diagnostics.run_diagnostic", side_effect=fake):
            run_a5_bandit(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert captured["action"].params["command"] == "bandit -r ."

    def test_run_a5_bandit_venv_binary_with_target(self, tmp_path):
        target = _make_target_dir(tmp_path)
        (target / "repo").mkdir()
        (target / ".venv" / "bin").mkdir(parents=True)
        (target / ".venv" / "bin" / "bandit").write_text("")
        ctx = _make_tick_context(target)
        captured, fake = self._capture()
        with patch("cronpypeline.plugins.swe_diagnostics.run_diagnostic", side_effect=fake):
            run_a5_bandit(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        venv = str(target / ".venv" / "bin" / "bandit")
        assert captured["action"].params["command"] == f"{venv} -r repo -f txt"

    def test_run_a5_bandit_venv_binary_uses_target_name(self, tmp_path):
        """target_dir exists, so the target name is used."""
        target = _make_target_dir(tmp_path)
        (target / ".venv" / "bin").mkdir(parents=True)
        (target / ".venv" / "bin" / "bandit").write_text("")
        ctx = _make_tick_context(target)
        captured, fake = self._capture()
        with patch("cronpypeline.plugins.swe_diagnostics.run_diagnostic", side_effect=fake):
            run_a5_bandit(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        venv = str(target / ".venv" / "bin" / "bandit")
        assert captured["action"].params["command"] == f"{venv} -r repo -f txt"

    def test_run_a5_bandit_venv_binary_without_target_dir(self, tmp_path):
        """target_dir does not exist, so the target falls back to '.'."""
        ctx = _make_tick_context(tmp_path)
        captured, fake = self._capture()
        with patch("cronpypeline.plugins.swe_diagnostics.run_diagnostic", side_effect=fake):
            run_a5_bandit(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert captured["action"].params["command"] == "bandit -r . -f txt"

    def test_run_a6_vulture_venv_binary(self, tmp_path):
        target = _make_target_dir(tmp_path)
        (target / "repo").mkdir()
        (target / ".venv" / "bin").mkdir(parents=True)
        (target / ".venv" / "bin" / "vulture").write_text("")
        ctx = _make_tick_context(target)
        captured, fake = self._capture()
        with patch("cronpypeline.plugins.swe_diagnostics.run_diagnostic", side_effect=fake):
            run_a6_vulture(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        venv = str(target / ".venv" / "bin" / "vulture")
        assert captured["action"].params["command"] == f"{venv} repo"

    def test_run_a6_vulture_without_target_dir(self, tmp_path):
        """target_dir does not exist, so the target falls back to '.'."""
        ctx = _make_tick_context(tmp_path)
        captured, fake = self._capture()
        with patch("cronpypeline.plugins.swe_diagnostics.run_diagnostic", side_effect=fake):
            run_a6_vulture(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert captured["action"].params["command"] == "vulture ."

    def test_run_a8_radon_venv_binary(self, tmp_path):
        target = _make_target_dir(tmp_path)
        (target / "repo").mkdir()
        (target / ".venv" / "bin").mkdir(parents=True)
        (target / ".venv" / "bin" / "radon").write_text("")
        ctx = _make_tick_context(target)
        captured, fake = self._capture()
        with patch("cronpypeline.plugins.swe_diagnostics.run_diagnostic", side_effect=fake):
            run_a8_radon(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        venv = str(target / ".venv" / "bin" / "radon")
        assert captured["action"].params["command"] == f"{venv} cc repo -s -a"

    def test_run_a8_radon_without_target_dir(self, tmp_path):
        """target_dir does not exist, so the target falls back to '.'."""
        ctx = _make_tick_context(tmp_path)
        captured, fake = self._capture()
        with patch("cronpypeline.plugins.swe_diagnostics.run_diagnostic", side_effect=fake):
            run_a8_radon(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert captured["action"].params["command"] == "radon cc . -s -a"


class TestRunA9DepAudit:
    _STDOUT = (
        "Name      Version  ID                  Fix Versions\n"
        "--------  -------  ------------------  ------------\n"
        "requests  2.31.0   PYSEC-2023-74       2.32.0\n"
        "weirdlib  1.0.0    GHSA-1234-5678\n"
        "pip       22.0.0   PYSEC-2022-000      22.1.0\n"
        "Found 3 known vulnerabilities in 3 packages\n"
    )

    def _mock_result(self, success=True, status="FAIL", stdout=None, data=None):
        base = {"parsed": {"status": status}, "report_path": "/tmp/report.md"}
        if data:
            base.update(data)
        return ActionResult(success=success, stdout=stdout if stdout is not None else self._STDOUT, data=base)

    def test_creates_issues_for_vulnerabilities(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target)
        with patch("cronpypeline.plugins.swe_diagnostics.run_diagnostic",
                   return_value=self._mock_result()):
            result = run_a9_dep_audit(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["issues_created"] == 2  # pip is skipped as tooling
        assert (target / ".SWE" / "issues" / "dep-audit-requests-pysec-2023-74.md").exists()
        assert (target / ".SWE" / "issues" / "dep-audit-weirdlib-ghsa-1234-5678.md").exists()

    def test_returns_early_on_success_with_non_fail_status(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target)
        with patch("cronpypeline.plugins.swe_diagnostics.run_diagnostic",
                   return_value=self._mock_result(status="PASS")):
            result = run_a9_dep_audit(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert not list((target / ".SWE" / "issues").glob("dep-audit-*.md"))

    def test_returns_early_on_failed_diagnostic(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target)
        with patch("cronpypeline.plugins.swe_diagnostics.run_diagnostic",
                   return_value=self._mock_result(success=False)):
            result = run_a9_dep_audit(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["issues_created"] == 2  # pip is skipped as tooling
        assert (target / ".SWE" / "issues" / "dep-audit-requests-pysec-2023-74.md").exists()
        assert (target / ".SWE" / "issues" / "dep-audit-weirdlib-ghsa-1234-5678.md").exists()

    def test_returns_early_when_status_not_fail(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target)
        with patch("cronpypeline.plugins.swe_diagnostics.run_diagnostic",
                   return_value=self._mock_result(success=False, status="PASS")):
            result = run_a9_dep_audit(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False
        assert not list((target / ".SWE" / "issues").glob("dep-audit-*.md"))

    def test_returns_early_on_dry_run(self, tmp_path):
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target, dry_run=True)
        result = run_a9_dep_audit(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.dry_run is True

    def test_skips_existing_issue(self, tmp_path):
        target = _make_target_dir(tmp_path)
        (target / ".SWE" / "issues" / "dep-audit-requests-pysec-2023-74.md").write_text("---\nstatus: open\n---\n# Existing")
        ctx = _make_tick_context(target)
        with patch("cronpypeline.plugins.swe_diagnostics.run_diagnostic",
                   return_value=self._mock_result()):
            result = run_a9_dep_audit(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.data["issues_created"] == 1  # only weirdlib

    def test_failed_audit_with_no_issues_returns_failure(self, tmp_path):
        """When audit fails but no issues can be created, preserve failure."""
        target = _make_target_dir(tmp_path)
        ctx = _make_tick_context(target)
        # Empty stdout means no vulnerabilities parsed, issues_created = 0
        result_mock = self._mock_result(stdout="No table here\n")
        with patch("cronpypeline.plugins.swe_diagnostics.run_diagnostic",
                   return_value=result_mock):
            result = run_a9_dep_audit(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False
        assert "No vulnerability issues could be created" in result.stderr
        assert result.data["issues_created"] == 0


# ─── review issue counting/finding with since_dt ────────────────────────────


    def test_mock_result_with_data(self):
        result = self._mock_result(data={"extra": "value"})
        assert result.data["extra"] == "value"
        assert result.data["parsed"]["status"] == "FAIL"


class TestParseUtcDatetime:
    def test_aware_datetime_returns_utc(self):
        result = _parse_utc_datetime("2025-01-01T00:00:00+00:00")
        assert result is not None
        assert result.tzinfo == timezone.utc

    def test_naive_datetime_normalized_to_utc(self):
        result = _parse_utc_datetime("2025-01-01T00:00:00")
        assert result is not None
        assert result.tzinfo == timezone.utc

    def test_invalid_string_returns_none(self):
        assert _parse_utc_datetime("not-a-date") is None

    def test_none_returns_none(self):
        assert _parse_utc_datetime(None) is None


class TestCountDoneReviewIssuesSinceDt:
    def _issue(self, target, name, created_at):
        path = target / ".SWE" / "issues" / name
        path.write_text(f"---\nstatus: done\ntype: review\ncreated_at: {created_at}\n---\n# Review\n")

    def test_counts_only_after_since(self, tmp_path):
        target = _make_target_dir(tmp_path)
        since = datetime.fromisoformat("2025-01-01T00:00:00+00:00")
        self._issue(target, "review-new00000.md", "2025-06-01T00:00:00+00:00")
        self._issue(target, "review-old00000.md", "2024-01-01T00:00:00+00:00")
        self._issue(target, "review-bad00000.md", "not-a-date")
        assert _count_done_review_issues(target, since_dt=since) == 2

    def test_counts_naive_created_at_normalized_to_utc(self, tmp_path):
        target = _make_target_dir(tmp_path)
        since = datetime(2025, 1, 1, tzinfo=timezone.utc)
        create_issue(
            target,
            issue_data={"id": "review-abc12345", "status": "done", "type": "review", "created_at": "2025-06-01T00:00:00"},
            body="# Review",
        )
        assert _count_done_review_issues(target, since_dt=since) == 1


class TestFindPreviousReviewShaSinceDt:
    def test_finds_sha_after_since(self, tmp_path):
        target = _make_target_dir(tmp_path)
        since = datetime.fromisoformat("2025-01-01T00:00:00+00:00")
        (target / ".SWE" / "issues" / "review-abc12345.md").write_text(
            "---\nstatus: done\ntype: review\ncreated_at: 2025-06-01T00:00:00+00:00\n---\n# Review\n"
        )
        assert _find_previous_review_sha(target, since_dt=since) == "abc12345"

    def test_skips_old_invalid_and_missing_created_at(self, tmp_path):
        target = _make_target_dir(tmp_path)
        since = datetime.fromisoformat("2025-01-01T00:00:00+00:00")
        (target / ".SWE" / "issues" / "review-old11111.md").write_text(
            "---\nstatus: done\ntype: review\ncreated_at: 2024-01-01T00:00:00+00:00\n---\n# Review\n"
        )
        (target / ".SWE" / "issues" / "review-bad22222.md").write_text(
            "---\nstatus: done\ntype: review\ncreated_at: not-a-date\n---\n# Review\n"
        )
        (target / ".SWE" / "issues" / "review-noc33333.md").write_text(
            "---\nstatus: done\ntype: review\n---\n# Review\n"
        )
        assert _find_previous_review_sha(target, since_dt=since) is None

    def test_finds_sha_with_naive_created_at(self, tmp_path):
        target = _make_target_dir(tmp_path)
        since = datetime(2025, 1, 1, tzinfo=timezone.utc)
        create_issue(
            target,
            issue_data={"id": "review-abc12345", "status": "done", "type": "review", "created_at": "2025-06-01T00:00:00"},
            body="# Review",
        )
        assert _find_previous_review_sha(target, since_dt=since) == "abc12345"


# ─── _ordinal_suffix ────────────────────────────────────────────────────────


class TestOrdinalSuffix:
    def test_teens_return_th(self):
        assert _ordinal_suffix(11) == "th"
        assert _ordinal_suffix(12) == "th"
        assert _ordinal_suffix(13) == "th"
        assert _ordinal_suffix(111) == "th"

    def test_regular_suffixes(self):
        assert _ordinal_suffix(1) == "st"
        assert _ordinal_suffix(2) == "nd"
        assert _ordinal_suffix(3) == "rd"
        assert _ordinal_suffix(4) == "th"


# ─── _compute_review_generation ─────────────────────────────────────────────


class TestComputeReviewGeneration:
    def _write_session(self, target, started_at="2025-01-01T00:00:00+00:00", active=True):
        (target / ".SWE" / "github_session.json").write_text(
            json.dumps({"active": active, "started_at": started_at})
        )

    def test_active_session_no_reviews_uses_git_sha(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._write_session(target)
        with patch("cronpypeline.plugins.swe_plugin.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="abc12345\n", stderr="")
            gen, prev, exceeded = _compute_review_generation(target, {}, "main")
        assert gen == 2
        assert prev == "abc12345"
        assert exceeded is False

    def test_active_session_no_reviews_git_fails(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._write_session(target)
        with patch("cronpypeline.plugins.swe_plugin.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="err")
            gen, prev, _exceeded = _compute_review_generation(target, {}, "main")
        assert gen == 1
        assert prev is None

    def test_active_session_git_timeout(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._write_session(target)
        with patch("cronpypeline.plugins.swe_plugin.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="git", timeout=10)):
            gen, prev, _exceeded = _compute_review_generation(target, {}, "main")
        assert gen == 1
        assert prev is None

    def test_active_session_with_prior_review(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._write_session(target)
        (target / ".SWE" / "issues" / "review-abc12345.md").write_text(
            "---\nstatus: done\ntype: review\ncreated_at: 2025-06-01T00:00:00+00:00\n---\n# Review\n"
        )
        gen, prev, _exceeded = _compute_review_generation(target, {}, "main")
        assert gen == 2
        assert prev == "abc12345"

    def test_active_session_max_gens_exceeded(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._write_session(target)
        for i in range(3):
            (target / ".SWE" / "issues" / f"review-s{i}000000.md").write_text(
                "---\nstatus: done\ntype: review\ncreated_at: 2025-06-01T00:00:00+00:00\n---\n# Review\n"
            )
        gen, prev, exceeded = _compute_review_generation(target, {"max_review_generations": 3}, "main")
        assert gen == 4
        assert prev is None
        assert exceeded is True

    def test_invalid_session_started_at(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._write_session(target, started_at="not-a-date")
        with patch("cronpypeline.plugins.swe_plugin.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="abc12345\n", stderr="")
            gen, prev, exceeded = _compute_review_generation(target, {}, "main")
        assert gen == 2
        assert prev == "abc12345"
        assert exceeded is False

    def test_naive_session_start_normalized_to_utc(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._write_session(target, started_at="2025-01-01T00:00:00")
        create_issue(
            target,
            issue_data={"id": "review-abc12345", "status": "done", "type": "review", "created_at": "2025-06-01T00:00:00+00:00"},
            body="# Review",
        )
        gen, prev, exceeded = _compute_review_generation(target, {}, "main")
        assert gen == 2
        assert prev == "abc12345"
        assert exceeded is False


# ─── run_c_review_issue max_gens / generation >= 3 ──────────────────────────


class TestRunCReviewIssueGenerations:
    def _setup_git(self, target):
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)

    def test_max_gens_exceeded_returns_failure(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_git(target)
        for i in range(3):
            (target / ".SWE" / "issues" / f"review-{i:08d}.md").write_text("---\nstatus: done\ntype: review\n---\n# Review\n")
        ctx = _make_tick_context(target, default_branch="main", max_review_generations=3)
        result = run_c_review_issue(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is False
        assert "Max review generations" in result.stderr

    def test_third_generation_includes_bugs_only_guidance(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_git(target)
        for i in range(2):
            (target / ".SWE" / "issues" / f"review-{i:08d}.md").write_text("---\nstatus: done\ntype: review\n---\n# Review\n")
        ctx = _make_tick_context(target, default_branch="main", max_review_generations=3)
        result = run_c_review_issue(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["review_generation"] == 3
        issue_file = target / ".SWE" / "issues" / f"{result.data['issue_id']}.md"
        assert "File **only bugs**" in issue_file.read_text()


# ─── sync_session_mode completed ────────────────────────────────────────────


class TestSyncSessionModeCompleted:
    def test_completed_session_returns_true(self, tmp_path):
        target = _make_target_dir(tmp_path)
        (target / ".SWE" / "github_session.json").write_text(json.dumps({"active": False, "completed": True}))
        mode_file = tmp_path / "mode.json"
        ctx = {"target_dir": str(target), "target_config": {"mode_file": str(mode_file)}}
        assert sync_session_mode(ctx) is True
        assert json.loads(mode_file.read_text()) == {"mode": "default"}

    def test_completed_previously_active_session_writes_default(self, tmp_path):
        target = _make_target_dir(tmp_path)
        (target / ".SWE" / "github_session.json").write_text(json.dumps({"active": True, "completed": True}))
        mode_file = tmp_path / "mode.json"
        mode_file.write_text(json.dumps({"mode": "github"}))
        ctx = {"target_dir": str(target), "target_config": {"mode_file": str(mode_file)}}
        assert sync_session_mode(ctx) is True
        assert json.loads(mode_file.read_text()) == {"mode": "default"}


# ─── run_c_pr_status: already-handled cycle limit + not-all-done ─────────────


class TestRunCPrStatusAlreadyHandled:
    def test_cycle_limit_reached_idles(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({
            "pr_number": 7, "pr_state": "changes_requested",
            "last_review_id": 500, "pr_review_cycles": 2,
        }))
        ctx = _make_tick_context(target, slug="owner/repo", max_pr_review_cycles=2)
        pr_resp = _mock_http_response({"state": "open", "merged": False})
        reviews_resp = _mock_http_response([{"id": 500, "state": "CHANGES_REQUESTED", "body": "needs work"}])
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=[pr_resp, reviews_resp]):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["pr_state"] == "open"

    def test_filed_issue_not_done_idles(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({
            "pr_number": 7, "pr_state": "changes_requested",
            "last_review_id": 500, "filed_issues": ["pr-revision-7-1"],
            "pr_review_cycles": 1,
        }))
        (target / ".SWE" / "issues" / "pr-revision-7-1.md").write_text("---\nstatus: open\n---\n# Not done")
        ctx = _make_tick_context(target, slug="owner/repo")
        pr_resp = _mock_http_response({"state": "open", "merged": False})
        reviews_resp = _mock_http_response([{"id": 500, "state": "CHANGES_REQUESTED", "body": "needs work"}])
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=[pr_resp, reviews_resp]):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["pr_state"] == "open"

    def test_filed_issue_missing_idles(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({
            "pr_number": 7, "pr_state": "changes_requested",
            "last_review_id": 500, "filed_issues": ["pr-revision-7-1"],
            "pr_review_cycles": 1,
        }))
        ctx = _make_tick_context(target, slug="owner/repo")
        pr_resp = _mock_http_response({"state": "open", "merged": False})
        reviews_resp = _mock_http_response([{"id": 500, "state": "CHANGES_REQUESTED", "body": "needs work"}])
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=[pr_resp, reviews_resp]):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["pr_state"] == "open"

    def test_marker_unlink_oserror_ignored(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({
            "pr_number": 7, "pr_state": "changes_requested",
            "last_review_id": 400, "filed_issues": ["pr-revision-7-1"],
            "pr_review_cycles": 1,
        }))
        (target / ".SWE" / "issues" / "pr-revision-7-1.md").write_text("---\nstatus: done\n---\n# Done")
        (target / ".SWE" / "pr_reviewed.json").mkdir()
        ctx = _make_tick_context(target, slug="owner/repo")
        pr_resp = _mock_http_response({"state": "open", "merged": False})
        reviews_resp = _mock_http_response([{"id": 400, "state": "CHANGES_REQUESTED", "body": "needs work"}])
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=[pr_resp, reviews_resp]), \
             patch("cronpypeline.plugins.swe_plugin.integration_head_sha", return_value="abc12345"), \
             patch("cronpypeline.plugins.swe_plugin.subprocess.run",
                   return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["pr_state"] == "open"

    def test_filed_issue_done_with_long_frontmatter_pushes(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({
            "pr_number": 7, "pr_state": "changes_requested",
            "last_review_id": 400, "filed_issues": ["pr-revision-7-1"],
            "pr_review_cycles": 1,
        }))
        (target / ".SWE" / "issues" / "pr-revision-7-1.md").write_text(
            _long_frontmatter({"status": "done"}) + "# Done\n"
        )
        ctx = _make_tick_context(target, slug="owner/repo")
        pr_resp = _mock_http_response({"state": "open", "merged": False})
        reviews_resp = _mock_http_response([{"id": 400, "state": "CHANGES_REQUESTED", "body": "needs work"}])
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=[pr_resp, reviews_resp]), \
             patch("cronpypeline.plugins.swe_plugin.integration_head_sha", return_value="abc12345"), \
             patch("cronpypeline.plugins.swe_plugin.subprocess.run",
                   return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["pr_state"] == "open"

    def test_filed_issue_not_done_with_long_frontmatter_idles(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        (target / ".SWE" / "pr_published.json").write_text(json.dumps({
            "pr_number": 7, "pr_state": "changes_requested",
            "last_review_id": 500, "filed_issues": ["pr-revision-7-1"],
            "pr_review_cycles": 1,
        }))
        (target / ".SWE" / "issues" / "pr-revision-7-1.md").write_text(
            _long_frontmatter({"status": "open"}) + "# Not done\n"
        )
        ctx = _make_tick_context(target, slug="owner/repo")
        pr_resp = _mock_http_response({"state": "open", "merged": False})
        reviews_resp = _mock_http_response([{"id": 500, "state": "CHANGES_REQUESTED", "body": "needs work"}])
        with patch("cronpypeline.plugins.swe_plugin._GH_OPENER.open", side_effect=[pr_resp, reviews_resp]):
            result = run_c_pr_status(ActionSpec(type=ActionType.CUSTOM, params={}), ctx)
        assert result.success is True
        assert result.data["pr_state"] == "open"


class TestParsePipAuditVulnerabilitiesEmptyLine:
    def test_breaks_on_empty_line_after_header(self):
        output = (
            "Name    Version  ID\n"
            "------- -------- ---\n"
            "\n"
            "pkg 1.0 ID-1\n"
        )
        assert _parse_pip_audit_vulnerabilities(output) == []


# ─── Batch marker helpers ────────────────────────────────────────────────────


def _write_batch(target: Path, fixed_count: int) -> None:
    """Write a batch marker with the given fixed_count."""
    _write_batch_marker(target, {"fixed_count": fixed_count, "batch_started_at": "2026-01-01T00:00:00+00:00"})


class TestBatchMarkerHelpers:
    def test_read_batch_marker_returns_none_when_absent(self, tmp_path):
        target = _make_target_dir(tmp_path)
        assert _read_batch_marker(target) is None

    def test_read_batch_marker_returns_data_when_present(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_batch(target, 2)
        marker = _read_batch_marker(target)
        assert marker is not None
        assert marker["fixed_count"] == 2

    def test_read_batch_marker_returns_none_on_corrupt_json(self, tmp_path):
        target = _make_target_dir(tmp_path)
        marker_path = _batch_marker_path(target)
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text("not valid json", encoding="utf-8")
        assert _read_batch_marker(target) is None

    def test_batch_fixed_count_zero_when_no_marker(self, tmp_path):
        target = _make_target_dir(tmp_path)
        assert _batch_fixed_count(target) == 0

    def test_batch_fixed_count_returns_value(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_batch(target, 3)
        assert _batch_fixed_count(target) == 3

    def test_issues_per_pr_defaults_to_1(self):
        assert _issues_per_pr({}) == 1

    def test_issues_per_pr_reads_config(self):
        assert _issues_per_pr({"issues_per_pr": 5}) == 5

    def test_batch_is_full_false_when_below_limit(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_batch(target, 0)
        assert _batch_is_full(target, {"issues_per_pr": 2}) is False

    def test_batch_is_full_true_when_at_limit(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_batch(target, 2)
        assert _batch_is_full(target, {"issues_per_pr": 2}) is True

    def test_batch_is_full_true_when_above_limit(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_batch(target, 3)
        assert _batch_is_full(target, {"issues_per_pr": 2}) is True

    def test_batch_is_full_true_when_default_1_and_count_1(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_batch(target, 1)
        assert _batch_is_full(target, {}) is True

    def test_increment_creates_marker_if_absent(self, tmp_path):
        target = _make_target_dir(tmp_path)
        assert _batch_fixed_count(target) == 0
        result = _increment_batch_fixed_count(target)
        assert result == 1
        assert _batch_fixed_count(target) == 1

    def test_increment_increments_existing(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_batch(target, 2)
        result = _increment_batch_fixed_count(target)
        assert result == 3
        assert _batch_fixed_count(target) == 3


class TestHasOpenCoverageIssues:
    def test_true_when_open_coverage_issue(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "cov1.md"
        issue_path.write_text("---\nstatus: open\ntype: coverage\n---\n# Coverage\n")
        assert _has_open_coverage_issues(target) is True

    def test_false_when_no_coverage_issues(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "bug1.md"
        issue_path.write_text("---\nstatus: open\ntype: bug\n---\n# Bug\n")
        assert _has_open_coverage_issues(target) is False

    def test_false_when_coverage_issue_not_open(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "cov1.md"
        issue_path.write_text("---\nstatus: done\ntype: coverage\n---\n# Coverage\n")
        assert _has_open_coverage_issues(target) is False

    def test_false_when_no_issues_dir(self, tmp_path):
        target = tmp_path / "repo"
        assert _has_open_coverage_issues(target) is False

    def test_false_when_issue_has_no_frontmatter(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "plain.md"
        issue_path.write_text("# Just a heading, no frontmatter\n")
        assert _has_open_coverage_issues(target) is False

    def test_ignores_unreadable_issue_file(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "cov1.md"
        issue_path.write_text("---\nstatus: open\ntype: coverage\n---\n# Coverage\n")
        with patch("pathlib.Path.read_text", side_effect=OSError("permission denied")):
            assert _has_open_coverage_issues(target) is False

    def test_true_with_long_frontmatter(self, tmp_path):
        target = _make_target_dir(tmp_path)
        issue_path = target / ".SWE" / "issues" / "cov1.md"
        issue_path.write_text(_long_frontmatter({"status": "open", "type": "coverage"}) + "# Coverage\n")
        assert _has_open_coverage_issues(target) is True


class TestShouldBlockOnOpenIssues:
    def test_blocks_when_batch_not_full_and_open_issues(self, tmp_path):
        target = _make_target_dir(tmp_path)
        create_issue(target, issue_data={"id": "i1", "status": "open"}, body="# Issue")
        assert _should_block_on_open_issues(target, {"issues_per_pr": 2}) is True

    def test_does_not_block_when_batch_not_full_and_no_issues(self, tmp_path):
        target = _make_target_dir(tmp_path)
        assert _should_block_on_open_issues(target, {"issues_per_pr": 2}) is False

    def test_blocks_when_batch_full_and_coverage_issue_open(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_batch(target, 1)
        issue_path = target / ".SWE" / "issues" / "cov1.md"
        issue_path.write_text("---\nstatus: open\ntype: coverage\n---\n# Coverage\n")
        assert _should_block_on_open_issues(target, {"issues_per_pr": 1}) is True

    def test_does_not_block_when_batch_full_and_only_non_coverage_open(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_batch(target, 1)
        create_issue(target, issue_data={"id": "i1", "status": "open"}, body="# Issue")
        assert _should_block_on_open_issues(target, {"issues_per_pr": 1}) is False

    def test_does_not_block_when_batch_full_and_no_issues(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_batch(target, 1)
        assert _should_block_on_open_issues(target, {"issues_per_pr": 1}) is False


# ─── Batch-aware trigger tests ───────────────────────────────────────────────


class TestDetectCIssueFixBatch:
    def test_fires_when_batch_full_and_coverage_issue_open(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        _write_batch(target, 1)
        issue_path = target / ".SWE" / "issues" / "cov1.md"
        issue_path.write_text("---\nstatus: open\ntype: coverage\n---\n# Coverage\n")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path / "no-tasks")
        ctx = {"target_dir": str(target), "target": "repo", "target_config": {"issues_per_pr": 1}}
        assert detect_c_issue_fix(ctx) is True

    def test_does_not_fire_when_batch_full_and_only_non_coverage_open(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        _write_batch(target, 1)
        create_issue(target, issue_data={"id": "i1", "status": "open"}, body="# Issue")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path / "no-tasks")
        ctx = {"target_dir": str(target), "target": "repo", "target_config": {"issues_per_pr": 1}}
        assert detect_c_issue_fix(ctx) is False

    def test_fires_when_batch_not_full_and_open_issue(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        create_issue(target, issue_data={"id": "i1", "status": "open"}, body="# Issue")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path / "no-tasks")
        ctx = {"target_dir": str(target), "target": "repo", "target_config": {"issues_per_pr": 3}}
        assert detect_c_issue_fix(ctx) is True

    def test_fires_when_active_task_even_if_batch_full(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        _write_batch(target, 1)
        tasks_dir = tmp_path / "tasks"
        task_dir = tasks_dir / "2025-01-01" / "task-001"
        task_dir.mkdir(parents=True)
        (task_dir / "task.json").write_text(json.dumps({"repo_name": "repo"}))
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tasks_dir)
        ctx = {"target_dir": str(target), "target": "repo", "target_config": {"issues_per_pr": 1}}
        assert detect_c_issue_fix(ctx) is True


class TestDetectCReviewIssueBatch:
    def _setup_passing_with_coverage(self, target: Path) -> None:
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")

    def _setup_git(self, target: Path) -> None:
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)

    def test_does_not_fire_when_batch_full(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_passing_with_coverage(target)
        self._setup_git(target)
        _write_batch(target, 1)
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main", "issues_per_pr": 1}}
        assert detect_c_review_issue(ctx) is False

    def test_fires_when_batch_not_full(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_passing_with_coverage(target)
        self._setup_git(target)
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main", "issues_per_pr": 3}}
        assert detect_c_review_issue(ctx) is True


class TestDetectCCoverageIssueBatch:
    def _setup_passing(self, target: Path, pct: float = 80.0) -> None:
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", f"# Coverage — PASS\n\n- **Coverage:** {pct}%\n")

    def _setup_git(self, target: Path) -> None:
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)

    def test_fires_when_batch_full_and_only_non_coverage_open(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_passing(target, pct=80.0)
        self._setup_git(target)
        _write_batch(target, 1)
        create_issue(target, issue_data={"id": "i1", "status": "open"}, body="# Issue")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main", "issues_per_pr": 1}}
        assert detect_c_coverage_issue(ctx) is True

    def test_does_not_fire_when_batch_full_and_coverage_issue_open(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_passing(target, pct=80.0)
        self._setup_git(target)
        _write_batch(target, 1)
        issue_path = target / ".SWE" / "issues" / "cov1.md"
        issue_path.write_text("---\nstatus: open\ntype: coverage\n---\n# Coverage\n")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main", "issues_per_pr": 1}}
        assert detect_c_coverage_issue(ctx) is False

    def test_does_not_fire_when_batch_not_full_and_open_issues(self, tmp_path):
        target = _make_target_dir(tmp_path)
        self._setup_passing(target, pct=80.0)
        self._setup_git(target)
        create_issue(target, issue_data={"id": "i1", "status": "open"}, body="# Issue")
        ctx = {"target_dir": str(target), "target_config": {"default_branch": "main", "issues_per_pr": 3}}
        assert detect_c_coverage_issue(ctx) is False


class TestDetectCDocSyncBatch:
    def _setup(self, target: Path) -> None:
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", "# Coverage — PASS\n\n- **Coverage:** 100.0%\n")

    def _setup_git(self, target: Path) -> None:
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / "new.txt").write_text("new")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "new"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "main"], capture_output=True, check=True)

    def test_fires_when_batch_full_and_only_non_coverage_open(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        _write_batch(target, 1)
        create_issue(target, issue_data={"id": "i1", "status": "open"}, body="# Issue")
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr", "issues_per_pr": 1}}
        assert detect_c_doc_sync(ctx) is True

    def test_does_not_fire_when_batch_full_and_coverage_issue_open(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        _write_batch(target, 1)
        issue_path = target / ".SWE" / "issues" / "cov1.md"
        issue_path.write_text("---\nstatus: open\ntype: coverage\n---\n# Coverage\n")
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr", "issues_per_pr": 1}}
        assert detect_c_doc_sync(ctx) is False


class TestDetectCPrPublishBatch:
    def _setup(self, target: Path, pct: float = 100.0) -> None:
        _write_report(target, "test-infra", "r.md", "# Test Infra — PASS\n")
        _write_report(target, "coverage", "r.md", f"# Coverage — PASS\n\n- **Coverage:** {pct}%\n")

    def _setup_git(self, target: Path) -> None:
        subprocess.run(["git", "init", "-b", "main", str(target)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.email", "t@t.com"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "config", "user.name", "T"], capture_output=True, check=True)
        (target / ".gitignore").write_text(".SWE/\n")
        (target / "f.txt").write_text("x")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "init"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "branch", "swe-pipeline/integration"], capture_output=True, check=True)

    def _make_ahead(self, target: Path) -> None:
        subprocess.run(["git", "-C", str(target), "checkout", "swe-pipeline/integration"], capture_output=True, check=True)
        (target / "new.txt").write_text("new")
        subprocess.run(["git", "-C", str(target), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "commit", "-m", "new"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "main"], capture_output=True, check=True)

    def test_fires_when_batch_full_and_only_non_coverage_open(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        self._make_ahead(target)
        _write_batch(target, 1)
        create_issue(target, issue_data={"id": "i1", "status": "open"}, body="# Issue")
        sha = integration_head_sha(target, "main")
        (target / ".SWE" / "doc_sync.json").write_text(json.dumps({"sha": sha}))
        (target / ".SWE" / "pr_title.json").write_text(json.dumps({"title": "SWE Pipeline: test"}))
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr", "issues_per_pr": 1}}
        assert detect_c_pr_publish(ctx) is True

    def test_does_not_fire_when_batch_full_and_coverage_issue_open(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        self._setup(target)
        self._setup_git(target)
        self._make_ahead(target)
        _write_batch(target, 1)
        issue_path = target / ".SWE" / "issues" / "cov1.md"
        issue_path.write_text("---\nstatus: open\ntype: coverage\n---\n# Coverage\n")
        sha = integration_head_sha(target, "main")
        (target / ".SWE" / "doc_sync.json").write_text(json.dumps({"sha": sha}))
        monkeypatch.setenv("SWE_GITHUB_TOKEN", "token")
        ctx = {"target_dir": str(target), "target_config": {"slug": "owner/repo", "default_branch": "main", "delivery": "open_pr", "issues_per_pr": 1}}
        assert detect_c_pr_publish(ctx) is False
