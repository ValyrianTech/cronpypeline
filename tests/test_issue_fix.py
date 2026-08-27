"""Tests for cronpypeline.plugins.issue_fix — SWE issue-fix SELECT/GATE state machine."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from cronpypeline.actions import ActionResult, TickContext
from cronpypeline.plugins.issue_fix import (
    CODING_COMPLETE_MARKER,
    GATE_RESULT_FILE,
    MAX_ATTEMPTS,
    PIPELINE_EXCLUDES,
    TASK_FILE,
    TASK_TIMEOUT_MINUTES,
    _build_coder_prompt,
    _build_coverage_prompt,
    _build_review_prompt,
    _capture_diff,
    _cleanup_orphaned_task_dirs,
    _cleanup_stale_task,
    _closing_loop_instructions,
    _ensure_pipeline_excludes,
    _ensure_task_branch,
    _ensure_tooling_artifacts_untracked,
    _finalize_issue_outcome,
    _gate_review,
    _invalidate_reports,
    _is_task_stale,
    _iter_task_dirs,
    _parse_coverage_output,
    _queue_agent,
    _read_task,
    _recover_orphaned_triaged,
    _run,
    _safe_slug,
    _task_branch_name,
    ensure_integration_branch,
    merge_into_integration,
    run_gate,
    run_issue_fix_state_machine,
    run_select,
    select_open_issue,
)
from cronpypeline.plugins.issue_store import Issue, _write_issue_file
from cronpypeline.plugins.swe_plugin import INTEGRATION_BRANCH


def _init_git(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main", str(path)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.com"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], capture_output=True, check=True)
    (path / "README").write_text("init")
    subprocess.run(["git", "-C", str(path), "add", "README"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], capture_output=True, check=True)


def _make_target_dir(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    (target / ".SWE" / "issues").mkdir(parents=True)
    (target / ".SWE" / "reports").mkdir(parents=True)
    return target


def _write_issue(issues_dir: Path, issue_id: str, **kwargs: Any) -> Issue:
    issues_dir.mkdir(parents=True, exist_ok=True)
    issue = Issue(id=issue_id, **kwargs)
    _write_issue_file(issues_dir / f"{issue_id}.md", issue)
    return issue


def _make_tick_context(target_dir: Path, target: str = "repo") -> TickContext:
    return TickContext(target=target, workspace_dir=target_dir.parent, target_config={})


class TestSafeSlug:
    def test_simple(self):
        assert _safe_slug("hello") == "hello"

    def test_replaces_special(self):
        assert _safe_slug("hello world!@#") == "hello_world"

    def test_empty(self):
        assert _safe_slug("") == "unknown"

    def test_only_special(self):
        assert _safe_slug("!!!") == "unknown"


class TestParseCoverageOutput:
    def test_total_line(self):
        r = _parse_coverage_output("TOTAL 100 10 90%\n5 passed")
        assert r["total_stmts"] == 100
        assert r["total_miss"] == 10
        assert r["coverage_pct"] == 90.0
        assert r["tests_passed"] == 5

    def test_failed_tests(self):
        r = _parse_coverage_output("TOTAL 50 5 90%\n3 failed, 7 passed")
        assert r["tests_passed"] == 7
        assert r["tests_failed"] == 3

    def test_file_lines(self):
        r = _parse_coverage_output("module.py 20 5 75% 10-15\nTOTAL 20 5 75%")
        assert len(r["files"]) == 1
        assert r["files"][0]["file"] == "module.py"
        assert r["files"][0]["missing"] == "10-15"

    def test_file_no_missing(self):
        r = _parse_coverage_output("TOTAL 10 0 100%\nmodule.py 10 0 100%")
        assert r["files"][0]["missing"] == ""

    def test_empty(self):
        r = _parse_coverage_output("")
        assert r["total_stmts"] == 0
        assert r["coverage_pct"] == 0.0


class TestTaskBranchName:
    def test_prefixed(self):
        assert _task_branch_name("iss-1") == "swe-pipeline/task_iss-1"


class TestReadTask:
    def test_reads(self, tmp_path):
        d = tmp_path / "t"; d.mkdir()
        (d / TASK_FILE).write_text(json.dumps({"task_id": "x"}))
        assert _read_task(d) == {"task_id": "x"}


class TestIsTaskStale:
    def test_no_file(self, tmp_path):
        assert _is_task_stale(tmp_path) is True

    def test_no_created_at(self, tmp_path):
        d = tmp_path / "t"; d.mkdir()
        (d / TASK_FILE).write_text("{}")
        assert _is_task_stale(d) is True

    def test_old(self, tmp_path):
        d = tmp_path / "t"; d.mkdir()
        old = (datetime.now(timezone.utc) - timedelta(minutes=TASK_TIMEOUT_MINUTES + 10))
        (d / TASK_FILE).write_text(json.dumps({"created_at": old.isoformat()}))
        assert _is_task_stale(d) is True

    def test_recent(self, tmp_path):
        d = tmp_path / "t"; d.mkdir()
        (d / TASK_FILE).write_text(json.dumps({"created_at": datetime.now(timezone.utc).isoformat()}))
        assert _is_task_stale(d) is False

    def test_corrupt_json(self, tmp_path):
        d = tmp_path / "t"; d.mkdir()
        (d / TASK_FILE).write_text("bad")
        assert _is_task_stale(d) is True

    def test_bad_date(self, tmp_path):
        d = tmp_path / "t"; d.mkdir()
        (d / TASK_FILE).write_text(json.dumps({"created_at": "bad"}))
        assert _is_task_stale(d) is True


class TestIterTaskDirs:
    def test_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "nope")
        assert _iter_task_dirs() == []

    def test_returns_dirs(self, tmp_path, monkeypatch):
        for date in ("2025-01-01", "2025-01-02"):
            (tmp_path / date / "t1").mkdir(parents=True)
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path)
        assert len(_iter_task_dirs()) == 2

    def test_skips_files(self, tmp_path, monkeypatch):
        (tmp_path / "2025-01-01" / "t1").mkdir(parents=True)
        (tmp_path / "2025-01-01" / "f.txt").write_text("x")
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path)
        assert len(_iter_task_dirs()) == 1


class TestEnsurePipelineExcludes:
    def test_adds_missing(self, tmp_path):
        _init_git(tmp_path)
        _ensure_pipeline_excludes(tmp_path)
        content = (tmp_path / ".git" / "info" / "exclude").read_text()
        for e in PIPELINE_EXCLUDES:
            assert e in content

    def test_noop_when_present(self, tmp_path):
        _init_git(tmp_path)
        excl = tmp_path / ".git" / "info" / "exclude"
        excl.parent.mkdir(parents=True, exist_ok=True)
        excl.write_text("\n".join(PIPELINE_EXCLUDES) + "\n")
        _ensure_pipeline_excludes(tmp_path)
        for e in PIPELINE_EXCLUDES:
            assert excl.read_text().count(e) == 1

    def test_os_error(self, tmp_path, capsys):
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.read_text", side_effect=OSError("x")):
            _ensure_pipeline_excludes(tmp_path, verbose=True)
        assert "WARNING" in capsys.readouterr().err


class TestEnsureToolingArtifactsUntracked:
    def test_noop_when_nothing_tracked(self, tmp_path):
        _init_git(tmp_path)
        _ensure_tooling_artifacts_untracked(tmp_path)

    def test_untracks(self, tmp_path):
        _init_git(tmp_path)
        (tmp_path / ".coverage").write_text("d")
        subprocess.run(["git", "-C", str(tmp_path), "add", ".coverage"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "c"], capture_output=True, check=True)
        _ensure_tooling_artifacts_untracked(tmp_path, verbose=True)
        tracked = subprocess.run(["git", "-C", str(tmp_path), "ls-files", "--", ".coverage"], capture_output=True, text=True, check=False).stdout.strip()
        assert tracked == ""

    def test_git_error(self, tmp_path):
        with patch("cronpypeline.plugins.issue_fix._git", side_effect=subprocess.CalledProcessError(1, "git")):
            _ensure_tooling_artifacts_untracked(tmp_path)


class TestEnsureIntegrationBranch:
    def test_not_git(self, tmp_path):
        assert ensure_integration_branch(tmp_path, "main") is False

    def test_creates(self, tmp_path):
        _init_git(tmp_path)
        assert ensure_integration_branch(tmp_path, "main", verbose=True) is True
        assert INTEGRATION_BRANCH in subprocess.run(["git", "-C", str(tmp_path), "branch", "--list"], capture_output=True, text=True, check=False).stdout

    def test_existing(self, tmp_path):
        _init_git(tmp_path)
        subprocess.run(["git", "-C", str(tmp_path), "branch", INTEGRATION_BRANCH], capture_output=True, check=True)
        assert ensure_integration_branch(tmp_path, "main") is True

    def test_dirty(self, tmp_path):
        _init_git(tmp_path)
        (tmp_path / "f").write_text("x")
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], capture_output=True, check=True)
        assert ensure_integration_branch(tmp_path, "main", verbose=True) is False


class TestMergeIntoIntegration:
    def test_merges(self, tmp_path):
        _init_git(tmp_path)
        subprocess.run(["git", "-C", str(tmp_path), "branch", INTEGRATION_BRANCH], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "checkout", "-b", "t1"], capture_output=True, check=True)
        (tmp_path / "f").write_text("c")
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "t"], capture_output=True, check=True)
        assert merge_into_integration(tmp_path, "t1", verbose=True) is True

    def test_conflict(self, tmp_path):
        _init_git(tmp_path)
        subprocess.run(["git", "-C", str(tmp_path), "branch", INTEGRATION_BRANCH], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "checkout", "-b", "t1"], capture_output=True, check=True)
        (tmp_path / "README").write_text("t")
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "t"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "checkout", INTEGRATION_BRANCH], capture_output=True, check=True)
        (tmp_path / "README").write_text("i")
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "i"], capture_output=True, check=True)
        assert merge_into_integration(tmp_path, "t1", verbose=True) is False


class TestSelectOpenIssue:
    def test_no_issues(self, tmp_path):
        assert select_open_issue(_make_target_dir(tmp_path)) is None

    def test_no_open(self, tmp_path):
        t = _make_target_dir(tmp_path)
        _write_issue(t / ".SWE" / "issues", "1", status="done")
        assert select_open_issue(t) is None

    def test_open_issue(self, tmp_path):
        t = _make_target_dir(tmp_path)
        _write_issue(t / ".SWE" / "issues", "1", status="open")
        assert select_open_issue(t) is not None

    def test_by_id(self, tmp_path):
        t = _make_target_dir(tmp_path)
        _write_issue(t / ".SWE" / "issues", "1", status="open")
        _write_issue(t / ".SWE" / "issues", "2", status="open")
        r = select_open_issue(t, issue_id="2")
        assert r is not None and str(r.id) == "2"

    def test_id_not_found(self, tmp_path):
        t = _make_target_dir(tmp_path)
        _write_issue(t / ".SWE" / "issues", "1", status="open")
        assert select_open_issue(t, issue_id="9") is None

    def test_selects_ranked_issue_first(self, tmp_path):
        t = _make_target_dir(tmp_path)
        _write_issue(t / ".SWE" / "issues", "1", status="open")
        _write_issue(t / ".SWE" / "issues", "2", status="open")
        markers_dir = t / ".SWE" / "markers"
        markers_dir.mkdir(parents=True, exist_ok=True)
        (markers_dir / "review_ranked.json").write_text(json.dumps({"issue_id": "2"}))
        r = select_open_issue(t)
        assert r is not None and str(r.id) == "2"

    def test_session_filter(self, tmp_path):
        t = _make_target_dir(tmp_path)
        _write_issue(t / ".SWE" / "issues", "1", status="open", source="manual", created_at="2020-01-01T00:00:00")
        _write_issue(t / ".SWE" / "issues", "2", status="open", source="github")
        (t / ".SWE" / "github_session.json").write_text(json.dumps({"active": True, "started_at": "2025-01-01T00:00:00"}))
        r = select_open_issue(t)
        assert str(r.id) == "2"

    def test_session_no_match(self, tmp_path):
        t = _make_target_dir(tmp_path)
        _write_issue(t / ".SWE" / "issues", "1", status="open", source="manual", created_at="2020-01-01T00:00:00")
        (t / ".SWE" / "github_session.json").write_text(json.dumps({"active": True, "started_at": "2025-01-01T00:00:00"}))
        assert select_open_issue(t) is None


class TestFinalizeIssueOutcome:
    def test_passed(self, tmp_path):
        t = _make_target_dir(tmp_path)
        i = _write_issue(t / ".SWE" / "issues", "1", status="open")
        s, a = _finalize_issue_outcome(i, t, passed=True)
        assert s == "done" and a == 0

    def test_failed(self, tmp_path):
        t = _make_target_dir(tmp_path)
        i = _write_issue(t / ".SWE" / "issues", "1", status="open", attempts=1)
        s, a = _finalize_issue_outcome(i, t, passed=False)
        assert s == "open" and a == 2

    def test_max_discarded(self, tmp_path):
        t = _make_target_dir(tmp_path)
        i = _write_issue(t / ".SWE" / "issues", "1", status="open", attempts=MAX_ATTEMPTS - 1)
        s, a = _finalize_issue_outcome(i, t, passed=False, verbose=True)
        assert s == "discarded" and a == MAX_ATTEMPTS


class TestCaptureDiff:
    def test_empty(self, tmp_path):
        _init_git(tmp_path)
        assert _capture_diff(tmp_path, "main") == ("", [])

    def test_with_changes(self, tmp_path):
        _init_git(tmp_path)
        subprocess.run(["git", "-C", str(tmp_path), "checkout", "-b", "t"], capture_output=True, check=True)
        (tmp_path / "n.txt").write_text("n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "t"], capture_output=True, check=True)
        d, f = _capture_diff(tmp_path, "main")
        assert "n.txt" in d and "n.txt" in f


class TestInvalidateReports:
    def test_deletes_symlink(self, tmp_path):
        t = _make_target_dir(tmp_path)
        d = t / ".SWE" / "reports" / "test-infra"; d.mkdir(parents=True)
        (d / "r.md").write_text("x")
        (d / "latest.md").symlink_to("r.md")
        _invalidate_reports(t)
        assert not (d / "latest.md").exists()

    def test_noop(self, tmp_path):
        _invalidate_reports(_make_target_dir(tmp_path))

    def test_custom_subdirs(self, tmp_path):
        t = _make_target_dir(tmp_path)
        for s in ("custom", "cov"):
            d = t / ".SWE" / "reports" / s; d.mkdir(parents=True)
            (d / "r.md").write_text("x")
            (d / "latest.md").symlink_to("r.md")
        _invalidate_reports(t, subdirs=("custom",))
        assert not (t / ".SWE" / "reports" / "custom" / "latest.md").exists()
        assert (t / ".SWE" / "reports" / "cov" / "latest.md").exists()


class TestRun:
    def test_success(self, tmp_path):
        c, o, _ = _run("echo hi", tmp_path, 10)
        assert c == 0 and "hi" in o

    def test_fail(self, tmp_path):
        assert _run("exit 1", tmp_path, 10)[0] == 1

    def test_timeout(self, tmp_path):
        c, _, e = _run("sleep 5", tmp_path, 1)
        assert c == 124 and "TIMEOUT" in e


class TestPromptBuilders:
    def test_closing_loop(self, tmp_path):
        td = tmp_path / "t"; td.mkdir()
        r = _closing_loop_instructions(tmp_path, td, "fix: i1")
        assert "git add" in r and "commit" in r and "UNFIXABLE" in r

    def test_coder_prompt(self, tmp_path):
        td = tmp_path / "t"; td.mkdir()
        r = _build_coder_prompt(tmp_path, "repo", td, "br", Issue(id="1", body="Fix", type="bug"), "pytest", "pip-audit")
        assert "Fix" in r and "pytest" in r

    def test_coder_with_coverage(self, tmp_path):
        td = tmp_path / "t"; td.mkdir()
        r = _build_coder_prompt(tmp_path, "repo", td, "br", Issue(id="1", body="F", type="bug"), "pt", "pa", "cov")
        assert "cov" in r and "baseline coverage" in r

    def test_coder_no_coverage(self, tmp_path):
        td = tmp_path / "t"; td.mkdir()
        r = _build_coder_prompt(tmp_path, "repo", td, "br", Issue(id="1", body="F", type="bug"), "pt", "pa", "")
        assert "baseline coverage" not in r

    def test_coverage_prompt(self, tmp_path):
        td = tmp_path / "t"; td.mkdir()
        r = _build_coverage_prompt(tmp_path, "repo", td, "br", Issue(id="1", body="Cover", type="coverage"), "pt", "cov")
        assert "Cover" in r and "cov" in r

    def test_review_prompt(self, tmp_path):
        td = tmp_path / "t"; td.mkdir()
        r = _build_review_prompt(tmp_path, "repo", td, Issue(id="1", body="Review", type="review"))
        assert "CODE REVIEW" in r and "Review" in r and "Do NOT commit" in r


class TestQueueAgent:
    def test_success(self, tmp_path):
        t = _make_target_dir(tmp_path); td = tmp_path / "t"; td.mkdir()
        ctx = _make_tick_context(t)
        h = MagicMock(); h.execute.return_value = ActionResult(success=True)
        with patch("cronpypeline.plugins.swe_prompts._build_queue_handler", return_value=h):
            assert _queue_agent("A", "p", "repo", t, td, "t1", "i1", "C2", ctx) is True

    def test_failure(self, tmp_path):
        t = _make_target_dir(tmp_path); td = tmp_path / "t"; td.mkdir()
        ctx = _make_tick_context(t)
        h = MagicMock(); h.execute.return_value = ActionResult(success=False)
        with patch("cronpypeline.plugins.swe_prompts._build_queue_handler", return_value=h):
            assert _queue_agent("A", "p", "repo", t, td, "t1", "i1", "C2", ctx) is False


# ─── Edge case coverage ─────────────────────────────────────────────────────


class TestEnsurePipelineExcludesEdge:
    def test_adds_newline_before_excludes(self, tmp_path):
        _init_git(tmp_path)
        excl = tmp_path / ".git" / "info" / "exclude"
        excl.parent.mkdir(parents=True, exist_ok=True)
        excl.write_text("existing_rule")
        _ensure_pipeline_excludes(tmp_path)
        assert "existing_rule\n" in excl.read_text()


class TestEnsureIntegrationBranchEdge:
    def test_not_git_verbose(self, tmp_path, capsys):
        assert ensure_integration_branch(tmp_path, "main", verbose=True) is False
        assert "not a git repo" in capsys.readouterr().out

    def test_checkout_fails(self, tmp_path):
        _init_git(tmp_path)
        call_count = [0]
        def fake_git(repo, *args, check=True):
            call_count[0] += 1
            if args[0] == "checkout":
                raise subprocess.CalledProcessError(1, "git")
            from cronpypeline.plugins.swe_plugin import _git as real_git
            return real_git(repo, *args, check=check)
        with patch("cronpypeline.plugins.issue_fix._git", side_effect=fake_git):
            assert ensure_integration_branch(tmp_path, "main", verbose=True) is False


class TestSelectOpenIssueVerbose:
    def test_no_issues_verbose(self, tmp_path, capsys):
        assert select_open_issue(_make_target_dir(tmp_path), verbose=True) is None
        assert "no issues" in capsys.readouterr().out

    def test_id_not_found_verbose(self, tmp_path, capsys):
        t = _make_target_dir(tmp_path)
        _write_issue(t / ".SWE" / "issues", "1", status="open")
        assert select_open_issue(t, issue_id="9", verbose=True) is None
        assert "not found" in capsys.readouterr().out

    def test_no_open_verbose(self, tmp_path, capsys):
        t = _make_target_dir(tmp_path)
        _write_issue(t / ".SWE" / "issues", "1", status="done")
        assert select_open_issue(t, verbose=True) is None
        assert "no issues with status 'open'" in capsys.readouterr().out


class TestIterTaskDirsEdge:
    def test_skips_non_dir_in_date_dir(self, tmp_path, monkeypatch):
        (tmp_path / "2025-01-01" / "t1").mkdir(parents=True)
        (tmp_path / "2025-01-01" / "f.txt").write_text("x")
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path)
        assert len(_iter_task_dirs()) == 1


class TestInvalidateReportsEdge:
    def test_oserror_handled(self, tmp_path):
        t = _make_target_dir(tmp_path)
        d = t / ".SWE" / "reports" / "test-infra"; d.mkdir(parents=True)
        (d / "latest.md").write_text("not a symlink")
        with patch("pathlib.Path.unlink", side_effect=OSError("x")):
            _invalidate_reports(t)  # should not raise


class TestEnsureToolingArtifactsEdge:
    def test_adds_to_gitignore(self, tmp_path):
        _init_git(tmp_path)
        (tmp_path / ".coverage").write_text("d")
        subprocess.run(["git", "-C", str(tmp_path), "add", ".coverage"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "c"], capture_output=True, check=True)
        _ensure_tooling_artifacts_untracked(tmp_path, verbose=True)
        assert ".coverage" in (tmp_path / ".gitignore").read_text()


# ─── _cleanup_stale_task ─────────────────────────────────────────────────────


class TestCleanupStaleTask:
    def _setup(self, tmp_path, issue_id=""):
        target = _make_target_dir(tmp_path)
        _init_git(target)
        subprocess.run(["git", "-C", str(target), "branch", INTEGRATION_BRANCH], capture_output=True, check=True)
        task_dir = tmp_path / "tasks" / "2025-01-01" / "task1"
        task_dir.mkdir(parents=True)
        (task_dir / TASK_FILE).write_text(json.dumps({
            "task_id": "task1",
            "branch": "swe-pipeline/task_task1",
            "default_branch": "main",
            "source_issue_id": issue_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }))
        subprocess.run(["git", "-C", str(target), "checkout", "-b", "swe-pipeline/task_task1"], capture_output=True, check=True)
        return target, task_dir

    def test_cleans_dir_and_branch(self, tmp_path):
        target, task_dir = self._setup(tmp_path)
        assert _cleanup_stale_task(target, task_dir, verbose=True) is True
        assert not task_dir.exists()
        branches = subprocess.run(["git", "-C", str(target), "branch", "--list"], capture_output=True, text=True, check=False).stdout
        assert "swe-pipeline/task_task1" not in branches

    def test_with_source_issue(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_issue(target / ".SWE" / "issues", "iss-1", status="open")
        _init_git(target)
        (target / ".gitignore").write_text(".SWE/\n")
        subprocess.run(["git", "-C", str(target), "branch", INTEGRATION_BRANCH], capture_output=True, check=True)
        task_dir = tmp_path / "tasks" / "d" / "t"
        task_dir.mkdir(parents=True)
        (task_dir / TASK_FILE).write_text(json.dumps({
            "task_id": "t", "branch": "b", "default_branch": "main",
            "source_issue_id": "iss-1",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }))
        assert _cleanup_stale_task(target, task_dir, verbose=True) is True
        content = (target / ".SWE" / "issues" / "iss-1.md").read_text()
        assert "open" in content or "discarded" in content

    def test_creates_integration_if_missing(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _init_git(target)
        task_dir = tmp_path / "tasks" / "d" / "t"
        task_dir.mkdir(parents=True)
        (task_dir / TASK_FILE).write_text(json.dumps({
            "task_id": "t", "branch": "b", "default_branch": "main",
            "source_issue_id": "",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }))
        assert _cleanup_stale_task(target, task_dir) is True

    def test_rmtree_error(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _init_git(target)
        task_dir = tmp_path / "tasks" / "d" / "t"
        task_dir.mkdir(parents=True)
        (task_dir / TASK_FILE).write_text(json.dumps({
            "task_id": "t", "branch": "b", "default_branch": "main",
            "source_issue_id": "",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }))
        with patch("cronpypeline.plugins.issue_fix.shutil.rmtree", side_effect=OSError("x")):
            assert _cleanup_stale_task(target, task_dir) is False


# ─── _cleanup_orphaned_task_dirs ─────────────────────────────────────────────


class TestCleanupOrphanedTaskDirs:
    def test_removes_orphaned(self, tmp_path, monkeypatch):
        td = tmp_path / "2025-01-01" / "20250101_repo_iss1"
        td.mkdir(parents=True)
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path)
        _cleanup_orphaned_task_dirs("repo", verbose=True)
        assert not td.exists()

    def test_keeps_with_task_json(self, tmp_path, monkeypatch):
        td = tmp_path / "2025-01-01" / "20250101_repo_iss1"
        td.mkdir(parents=True)
        (td / TASK_FILE).write_text("{}")
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path)
        _cleanup_orphaned_task_dirs("repo")
        assert td.exists()

    def test_skips_other_repo(self, tmp_path, monkeypatch):
        td = tmp_path / "2025-01-01" / "20250101_other_iss1"
        td.mkdir(parents=True)
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path)
        _cleanup_orphaned_task_dirs("repo")
        assert td.exists()


# ─── _recover_orphaned_triaged ──────────────────────────────────────────────


class TestRecoverOrphanedTriaged:
    def test_resets_stale(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        old = (datetime.now(timezone.utc) - timedelta(minutes=TASK_TIMEOUT_MINUTES + 10))
        _write_issue(target / ".SWE" / "issues", "1", status="triaged", created_at=old.isoformat())
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        _recover_orphaned_triaged(target, "repo", verbose=True)
        assert "triaged" not in (target / ".SWE" / "issues" / "1.md").read_text()

    def test_keeps_with_task(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        old = (datetime.now(timezone.utc) - timedelta(minutes=TASK_TIMEOUT_MINUTES + 10))
        _write_issue(target / ".SWE" / "issues", "1", status="triaged", created_at=old.isoformat())
        td = tmp_path / "tasks" / "d" / "20250101_repo_1"
        td.mkdir(parents=True)
        (td / TASK_FILE).write_text(json.dumps({"source_issue_id": "1", "repo_name": "repo"}))
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        _recover_orphaned_triaged(target, "repo")
        assert "triaged" in (target / ".SWE" / "issues" / "1.md").read_text()

    def test_skips_recent(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        _write_issue(target / ".SWE" / "issues", "1", status="triaged",
                      created_at=datetime.now(timezone.utc).isoformat())
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        _recover_orphaned_triaged(target, "repo")
        assert "triaged" in (target / ".SWE" / "issues" / "1.md").read_text()

    def test_skips_non_triaged(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        old = (datetime.now(timezone.utc) - timedelta(minutes=TASK_TIMEOUT_MINUTES + 10))
        _write_issue(target / ".SWE" / "issues", "1", status="open", created_at=old.isoformat())
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        _recover_orphaned_triaged(target, "repo")
        assert "open" in (target / ".SWE" / "issues" / "1.md").read_text()

    def test_skips_no_created_at(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        _write_issue(target / ".SWE" / "issues", "1", status="triaged", created_at=None)
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        _recover_orphaned_triaged(target, "repo")
        assert "triaged" in (target / ".SWE" / "issues" / "1.md").read_text()

    def test_skips_bad_date(self, tmp_path, monkeypatch):
        target = _make_target_dir(tmp_path)
        _write_issue(target / ".SWE" / "issues", "1", status="triaged", created_at="bad")
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        _recover_orphaned_triaged(target, "repo")
        assert "triaged" in (target / ".SWE" / "issues" / "1.md").read_text()


# ─── _ensure_task_branch ─────────────────────────────────────────────────────


class TestEnsureTaskBranch:
    def test_creates(self, tmp_path):
        _init_git(tmp_path)
        assert _ensure_task_branch(tmp_path, "i1", "main", verbose=True) is True
        assert "swe-pipeline/task_i1" in subprocess.run(
            ["git", "-C", str(tmp_path), "branch", "--list"], capture_output=True, text=True, check=False).stdout

    def test_existing(self, tmp_path):
        _init_git(tmp_path)
        subprocess.run(["git", "-C", str(tmp_path), "branch", "swe-pipeline/task_i1"], capture_output=True, check=True)
        assert _ensure_task_branch(tmp_path, "i1", "main") is True

    def test_not_git(self, tmp_path):
        assert _ensure_task_branch(tmp_path, "i1", "main") is False


# ─── run_select ──────────────────────────────────────────────────────────────


class TestRunSelect:
    def _setup(self, tmp_path, issue_type="bug", **issue_kwargs):
        target = _make_target_dir(tmp_path)
        (target / ".SWE" / "repo_briefing.md").write_text("briefing")
        _write_issue(target / ".SWE" / "issues", "1", status="open", body="Fix", type=issue_type, **issue_kwargs)
        _init_git(target)
        return target

    def test_no_briefing(self, tmp_path):
        t = _make_target_dir(tmp_path)
        assert run_select(t, "repo", {}, _make_tick_context(t)) is False

    def test_no_open_issues(self, tmp_path):
        t = _make_target_dir(tmp_path)
        (t / ".SWE" / "repo_briefing.md").write_text("b")
        assert run_select(t, "repo", {}, _make_tick_context(t)) is False

    def test_dry_run(self, tmp_path, monkeypatch):
        t = self._setup(tmp_path)
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        assert run_select(t, "repo", {}, _make_tick_context(t), dry_run=True) is True

    def test_bug_issue(self, tmp_path, monkeypatch):
        t = self._setup(tmp_path)
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        h = MagicMock(); h.execute.return_value = ActionResult(success=True)
        with patch("cronpypeline.plugins.swe_prompts._build_queue_handler", return_value=h):
            assert run_select(t, "repo", {}, _make_tick_context(t), verbose=True) is True
        td = next(iter((tmp_path / "tasks").rglob(TASK_FILE)))
        task = json.loads(td.read_text())
        assert task["issue_type"] == "bug"

    def test_coverage_issue(self, tmp_path, monkeypatch):
        t = self._setup(tmp_path, issue_type="coverage")
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        h = MagicMock(); h.execute.return_value = ActionResult(success=True)
        with patch("cronpypeline.plugins.swe_prompts._build_queue_handler", return_value=h):
            assert run_select(t, "repo", {}, _make_tick_context(t)) is True

    def test_review_issue(self, tmp_path, monkeypatch):
        t = self._setup(tmp_path, issue_type="review")
        subprocess.run(["git", "-C", str(t), "branch", INTEGRATION_BRANCH], capture_output=True, check=True)
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        h = MagicMock(); h.execute.return_value = ActionResult(success=True)
        with patch("cronpypeline.plugins.swe_prompts._build_queue_handler", return_value=h):
            assert run_select(t, "repo", {}, _make_tick_context(t), verbose=True) is True
        task = json.loads(next(iter((tmp_path / "tasks").rglob(TASK_FILE))).read_text())
        assert task["issue_type"] == "review"

    def test_queue_fails(self, tmp_path, monkeypatch):
        t = self._setup(tmp_path)
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        h = MagicMock(); h.execute.return_value = ActionResult(success=False)
        with patch("cronpypeline.plugins.swe_prompts._build_queue_handler", return_value=h):
            assert run_select(t, "repo", {}, _make_tick_context(t)) is False

    def test_branch_fails(self, tmp_path, monkeypatch):
        self._setup(tmp_path)
        # Not a git repo — _ensure_task_branch will fail
        t2 = _make_target_dir(tmp_path / "other")
        (t2 / ".SWE" / "repo_briefing.md").write_text("b")
        _write_issue(t2 / ".SWE" / "issues", "1", status="open", body="F", type="bug")
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        assert run_select(t2, "repo", {}, _make_tick_context(t2)) is False

    def test_sets_triaged(self, tmp_path, monkeypatch):
        t = self._setup(tmp_path)
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        h = MagicMock(); h.execute.return_value = ActionResult(success=True)
        with patch("cronpypeline.plugins.swe_prompts._build_queue_handler", return_value=h):
            run_select(t, "repo", {}, _make_tick_context(t))
        assert "triaged" in (t / ".SWE" / "issues" / "1.md").read_text()

    def test_cleans_stale_artifacts(self, tmp_path, monkeypatch):
        t = self._setup(tmp_path)
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        h = MagicMock(); h.execute.return_value = ActionResult(success=True)
        with patch("cronpypeline.plugins.swe_prompts._build_queue_handler", return_value=h):
            run_select(t, "repo", {}, _make_tick_context(t))
        td = next(iter((tmp_path / "tasks").rglob(TASK_FILE))).parent
        assert not (td / GATE_RESULT_FILE).exists()
        assert not (td / CODING_COMPLETE_MARKER).exists()


# ─── _gate_review ────────────────────────────────────────────────────────────


class TestGateReview:
    def test_marks_done(self, tmp_path):
        target = _make_target_dir(tmp_path)
        _write_issue(target / ".SWE" / "issues", "rev-1", status="open", source="review")
        td = tmp_path / "t"; td.mkdir()
        assert _gate_review(target, td, {"task_id": "t", "source_issue_id": "rev-1"}, verbose=True) is True
        gate = json.loads((td / GATE_RESULT_FILE).read_text())
        assert gate["passed"] is True
        assert "done" in (target / ".SWE" / "issues" / "rev-1.md").read_text()

    def test_no_source_id(self, tmp_path):
        td = tmp_path / "t"; td.mkdir()
        assert _gate_review(_make_target_dir(tmp_path), td, {"task_id": "t", "source_issue_id": ""}) is True


# ─── run_gate ────────────────────────────────────────────────────────────────


class TestRunGate:
    def _make_task(self, tmp_path, task_dir, **overrides):
        task = {"task_id": "t1", "issue_type": "bug", "source_issue_id": "iss-1",
                "test_cmd": "true", "coverage_cmd": "", "branch": "swe-pipeline/task_t1"}
        task.update(overrides)
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / TASK_FILE).write_text(json.dumps(task))
        return task

    def test_dry_run(self, tmp_path):
        td = tmp_path / "t"; self._make_task(tmp_path, td)
        assert run_gate(tmp_path, td, dry_run=True) is True

    def test_unfixable(self, tmp_path):
        t = _make_target_dir(tmp_path)
        _write_issue(t / ".SWE" / "issues", "iss-1", status="open")
        td = tmp_path / "t"; self._make_task(tmp_path, td)
        (td / CODING_COMPLETE_MARKER).write_text("UNFIXABLE: nope")
        assert run_gate(t, td, verbose=True) is True
        gate = json.loads((td / GATE_RESULT_FILE).read_text())
        assert gate["passed"] is False and gate["unfixable"] is True
        assert "discarded" in (t / ".SWE" / "issues" / "iss-1.md").read_text()

    def test_unfixable_dry_run(self, tmp_path):
        td = tmp_path / "t"; self._make_task(tmp_path, td)
        (td / CODING_COMPLETE_MARKER).write_text("UNFIXABLE: nope")
        assert run_gate(tmp_path, td, dry_run=True) is True
        assert not (td / GATE_RESULT_FILE).exists()

    def test_review_gate(self, tmp_path):
        t = _make_target_dir(tmp_path)
        _write_issue(t / ".SWE" / "issues", "rev-1", status="open", source="review")
        td = tmp_path / "t"; self._make_task(tmp_path, td, issue_type="review", source_issue_id="rev-1")
        assert run_gate(t, td, verbose=True) is True
        assert json.loads((td / GATE_RESULT_FILE).read_text())["passed"] is True

    def test_review_gate_dry_run(self, tmp_path):
        td = tmp_path / "t"; self._make_task(tmp_path, td, issue_type="review")
        assert run_gate(tmp_path, td, dry_run=True) is True

    def _setup_git_with_branch(self, tmp_path):
        t = _make_target_dir(tmp_path)
        _write_issue(t / ".SWE" / "issues", "iss-1", status="open")
        _init_git(t)
        subprocess.run(["git", "-C", str(t), "branch", INTEGRATION_BRANCH], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(t), "checkout", "-b", "swe-pipeline/task_t1"], capture_output=True, check=True)
        return t

    def test_passes_with_diff(self, tmp_path):
        t = self._setup_git_with_branch(tmp_path)
        (t / "fix.txt").write_text("fix")
        subprocess.run(["git", "-C", str(t), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(t), "commit", "-m", "fix"], capture_output=True, check=True)
        td = tmp_path / "t"; self._make_task(tmp_path, td)
        with patch("cronpypeline.plugins.issue_fix._run", return_value=(0, "", "")):
            assert run_gate(t, td, verbose=True) is True
        gate = json.loads((td / GATE_RESULT_FILE).read_text())
        assert gate["passed"] is True and gate["merged"] is True

    def test_fails_tests_fail(self, tmp_path):
        t = self._setup_git_with_branch(tmp_path)
        (t / "fix.txt").write_text("fix")
        subprocess.run(["git", "-C", str(t), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(t), "commit", "-m", "fix"], capture_output=True, check=True)
        td = tmp_path / "t"; self._make_task(tmp_path, td)
        with patch("cronpypeline.plugins.issue_fix._run", return_value=(1, "", "err")):
            assert run_gate(t, td, verbose=True) is False
        assert json.loads((td / GATE_RESULT_FILE).read_text())["passed"] is False

    def test_resolved_out_of_tree(self, tmp_path):
        t = self._setup_git_with_branch(tmp_path)
        td = tmp_path / "t"; self._make_task(tmp_path, td)
        with patch("cronpypeline.plugins.issue_fix._run", return_value=(0, "", "")):
            assert run_gate(t, td, verbose=True) is True
        gate = json.loads((td / GATE_RESULT_FILE).read_text())
        assert gate["resolved_out_of_tree"] is True and gate["passed"] is False
        assert "discarded" in (t / ".SWE" / "issues" / "iss-1.md").read_text()

    def test_coverage_passes(self, tmp_path):
        t = self._setup_git_with_branch(tmp_path)
        (t / "test.txt").write_text("t")
        subprocess.run(["git", "-C", str(t), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(t), "commit", "-m", "t"], capture_output=True, check=True)
        td = tmp_path / "t"; self._make_task(tmp_path, td, issue_type="coverage", coverage_cmd="cov")
        with patch("cronpypeline.plugins.issue_fix._run", return_value=(0, "TOTAL 100 0 100%\n5 passed", "")):
            assert run_gate(t, td, verbose=True) is True
        gate = json.loads((td / GATE_RESULT_FILE).read_text())
        assert gate["passed"] is True and gate["coverage_pct"] == 100.0

    def test_coverage_fails_below_target(self, tmp_path):
        t = self._setup_git_with_branch(tmp_path)
        (t / "test.txt").write_text("t")
        subprocess.run(["git", "-C", str(t), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(t), "commit", "-m", "t"], capture_output=True, check=True)
        td = tmp_path / "t"; self._make_task(tmp_path, td, issue_type="coverage", coverage_cmd="cov")
        with patch("cronpypeline.plugins.issue_fix._run", return_value=(0, "TOTAL 100 50 50%\n5 passed", "")):
            assert run_gate(t, td, verbose=True) is False
        assert json.loads((td / GATE_RESULT_FILE).read_text())["passed"] is False

    def test_bug_with_coverage_cmd(self, tmp_path):
        t = self._setup_git_with_branch(tmp_path)
        (t / "fix.txt").write_text("f")
        subprocess.run(["git", "-C", str(t), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(t), "commit", "-m", "f"], capture_output=True, check=True)
        td = tmp_path / "t"; self._make_task(tmp_path, td, coverage_cmd="cov")
        cov = "TOTAL 100 0 100%\n5 passed"
        with patch("cronpypeline.plugins.issue_fix._run", return_value=(0, cov, "")):
            assert run_gate(t, td, verbose=True) is True
        gate = json.loads((td / GATE_RESULT_FILE).read_text())
        assert gate["passed"] is True and "baseline_pct" in gate

    def test_no_source_issue_id(self, tmp_path):
        t = self._setup_git_with_branch(tmp_path)
        (t / "fix.txt").write_text("f")
        subprocess.run(["git", "-C", str(t), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(t), "commit", "-m", "f"], capture_output=True, check=True)
        td = tmp_path / "t"; self._make_task(tmp_path, td, source_issue_id="")
        with patch("cronpypeline.plugins.issue_fix._run", return_value=(0, "", "")):
            assert run_gate(t, td) is True


# ─── run_issue_fix_state_machine ─────────────────────────────────────────────


class TestRunIssueFixStateMachine:
    def test_no_active_task_selects(self, tmp_path, monkeypatch):
        t = _make_target_dir(tmp_path)
        (t / ".SWE" / "repo_briefing.md").write_text("b")
        _write_issue(t / ".SWE" / "issues", "1", status="open", body="F", type="bug")
        _init_git(t)
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path / "tasks")
        h = MagicMock(); h.execute.return_value = ActionResult(success=True)
        with patch("cronpypeline.plugins.swe_prompts._build_queue_handler", return_value=h):
            assert run_issue_fix_state_machine(t, "repo", {}, _make_tick_context(t), verbose=True) is True

    def test_active_task_with_marker_gates(self, tmp_path, monkeypatch):
        t = _make_target_dir(tmp_path)
        _write_issue(t / ".SWE" / "issues", "iss-1", status="open")
        _init_git(t)
        subprocess.run(["git", "-C", str(t), "branch", INTEGRATION_BRANCH], capture_output=True, check=True)
        td = tmp_path / "tasks" / "d" / "20250101_repo_t1"
        td.mkdir(parents=True)
        (td / TASK_FILE).write_text(json.dumps({
            "task_id": "t1", "issue_type": "bug", "source_issue_id": "iss-1",
            "test_cmd": "true", "coverage_cmd": "", "branch": "swe-pipeline/task_t1",
            "repo_name": "repo",
        }))
        (td / CODING_COMPLETE_MARKER).write_text("done")
        subprocess.run(["git", "-C", str(t), "checkout", "-b", "swe-pipeline/task_t1"], capture_output=True, check=True)
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path / "tasks")
        with patch("cronpypeline.plugins.issue_fix._run", return_value=(0, "", "")):
            assert run_issue_fix_state_machine(t, "repo", {}, _make_tick_context(t), verbose=True) is True

    def test_active_task_stale_cleans_and_reslects(self, tmp_path, monkeypatch):
        t = _make_target_dir(tmp_path)
        (t / ".SWE" / "repo_briefing.md").write_text("b")
        _write_issue(t / ".SWE" / "issues", "1", status="open", body="F", type="bug")
        _init_git(t)
        (t / ".gitignore").write_text(".SWE/\n")
        subprocess.run(["git", "-C", str(t), "branch", INTEGRATION_BRANCH], capture_output=True, check=True)
        td = tmp_path / "tasks" / "d" / "20250101_repo_t1"
        td.mkdir(parents=True)
        old = (datetime.now(timezone.utc) - timedelta(minutes=TASK_TIMEOUT_MINUTES + 10))
        (td / TASK_FILE).write_text(json.dumps({
            "task_id": "t1", "issue_type": "bug", "source_issue_id": "",
            "test_cmd": "true", "coverage_cmd": "", "branch": "swe-pipeline/task_t1",
            "default_branch": "main", "created_at": old.isoformat(),
            "repo_name": "repo",
        }))
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path / "tasks")
        h = MagicMock(); h.execute.return_value = ActionResult(success=True)
        with patch("cronpypeline.plugins.swe_prompts._build_queue_handler", return_value=h):
            assert run_issue_fix_state_machine(t, "repo", {}, _make_tick_context(t), verbose=True) is True

    def test_active_task_stale_dry_run(self, tmp_path, monkeypatch):
        t = _make_target_dir(tmp_path)
        _init_git(t)
        td = tmp_path / "tasks" / "d" / "20250101_repo_t1"
        td.mkdir(parents=True)
        old = (datetime.now(timezone.utc) - timedelta(minutes=TASK_TIMEOUT_MINUTES + 10))
        (td / TASK_FILE).write_text(json.dumps({
            "task_id": "t1", "issue_type": "bug", "source_issue_id": "",
            "branch": "b", "default_branch": "main", "created_at": old.isoformat(),
            "repo_name": "repo",
        }))
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path / "tasks")
        assert run_issue_fix_state_machine(t, "repo", {}, _make_tick_context(t), dry_run=True) is True

    def test_active_task_waiting_on_agent(self, tmp_path, monkeypatch):
        t = _make_target_dir(tmp_path)
        _init_git(t)
        td = tmp_path / "tasks" / "d" / "20250101_repo_t1"
        td.mkdir(parents=True)
        recent = datetime.now(timezone.utc).isoformat()
        (td / TASK_FILE).write_text(json.dumps({
            "task_id": "t1", "issue_type": "bug", "source_issue_id": "",
            "branch": "swe-pipeline/task_t1", "created_at": recent,
            "repo_name": "repo",
        }))
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path / "tasks")
        assert run_issue_fix_state_machine(t, "repo", {}, _make_tick_context(t), verbose=True) is True

    def test_active_task_agent_forgot_marker(self, tmp_path, monkeypatch):
        t = _make_target_dir(tmp_path)
        _write_issue(t / ".SWE" / "issues", "iss-1", status="open")
        _init_git(t)
        subprocess.run(["git", "-C", str(t), "branch", INTEGRATION_BRANCH], capture_output=True, check=True)
        td = tmp_path / "tasks" / "d" / "20250101_repo_t1"
        td.mkdir(parents=True)
        old = (datetime.now(timezone.utc) - timedelta(minutes=5))
        (td / TASK_FILE).write_text(json.dumps({
            "task_id": "t1", "issue_type": "bug", "source_issue_id": "iss-1",
            "test_cmd": "true", "coverage_cmd": "", "branch": "swe-pipeline/task_t1",
            "created_at": old.isoformat(), "repo_name": "repo",
        }))
        subprocess.run(["git", "-C", str(t), "checkout", "-b", "swe-pipeline/task_t1"], capture_output=True, check=True)
        (t / "fix.txt").write_text("f")
        subprocess.run(["git", "-C", str(t), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(t), "commit", "-m", "f"], capture_output=True, check=True)
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path / "tasks")
        with patch("cronpypeline.plugins.issue_fix._run", return_value=(0, "", "")):
            assert run_issue_fix_state_machine(t, "repo", {}, _make_tick_context(t), verbose=True) is True

    def test_active_task_agent_forgot_marker_dry_run(self, tmp_path, monkeypatch):
        t = _make_target_dir(tmp_path)
        _init_git(t)
        subprocess.run(["git", "-C", str(t), "branch", INTEGRATION_BRANCH], capture_output=True, check=True)
        td = tmp_path / "tasks" / "d" / "20250101_repo_t1"
        td.mkdir(parents=True)
        old = (datetime.now(timezone.utc) - timedelta(minutes=5))
        (td / TASK_FILE).write_text(json.dumps({
            "task_id": "t1", "issue_type": "bug", "source_issue_id": "",
            "branch": "swe-pipeline/task_t1", "created_at": old.isoformat(),
            "repo_name": "repo",
        }))
        subprocess.run(["git", "-C", str(t), "checkout", "-b", "swe-pipeline/task_t1"], capture_output=True, check=True)
        (t / "fix.txt").write_text("f")
        subprocess.run(["git", "-C", str(t), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(t), "commit", "-m", "f"], capture_output=True, check=True)
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path / "tasks")
        assert run_issue_fix_state_machine(t, "repo", {}, _make_tick_context(t), dry_run=True) is True

    def test_no_active_task_dry_run(self, tmp_path, monkeypatch):
        t = _make_target_dir(tmp_path)
        (t / ".SWE" / "repo_briefing.md").write_text("b")
        _write_issue(t / ".SWE" / "issues", "1", status="open", body="F", type="bug")
        _init_git(t)
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path / "tasks")
        assert run_issue_fix_state_machine(t, "repo", {}, _make_tick_context(t), dry_run=True) is True

    def test_agent_forgot_marker_git_oserror(self, tmp_path, monkeypatch):
        """Covers the OSError except branch in the git log check (line 1108-1109)."""
        t = _make_target_dir(tmp_path)
        _init_git(t)
        subprocess.run(["git", "-C", str(t), "branch", INTEGRATION_BRANCH], capture_output=True, check=True)
        td = tmp_path / "tasks" / "d" / "20250101_repo_t1"
        td.mkdir(parents=True)
        old = (datetime.now(timezone.utc) - timedelta(minutes=5))
        (td / TASK_FILE).write_text(json.dumps({
            "task_id": "t1", "issue_type": "bug", "source_issue_id": "",
            "branch": "swe-pipeline/task_t1", "created_at": old.isoformat(),
            "repo_name": "repo",
        }))
        subprocess.run(["git", "-C", str(t), "checkout", "-b", "swe-pipeline/task_t1"], capture_output=True, check=True)
        (t / "fix.txt").write_text("f")
        subprocess.run(["git", "-C", str(t), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(t), "commit", "-m", "f"], capture_output=True, check=True)
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        monkeypatch.setattr("cronpypeline.plugins.swe_plugin.TASKS_DIR", tmp_path / "tasks")
        original_git = __import__("cronpypeline.plugins.swe_plugin", fromlist=["_git"])._git
        def fake_git(repo, *args, check=True):
            if args[0] == "log":
                raise OSError("boom")
            return original_git(repo, *args, check=check)
        with patch("cronpypeline.plugins.issue_fix._git", side_effect=fake_git):
            assert run_issue_fix_state_machine(t, "repo", {}, _make_tick_context(t), verbose=True) is True


# ─── Remaining edge cases for 100% coverage ──────────────────────────────────


class TestEnsureToolingArtifactsGitignoreNewline:
    def test_gitignore_without_trailing_newline(self, tmp_path):
        """Covers line 118 — adding newline when gitignore doesn't end with one."""
        _init_git(tmp_path)
        (tmp_path / ".coverage").write_text("d")
        subprocess.run(["git", "-C", str(tmp_path), "add", ".coverage"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "c"], capture_output=True, check=True)
        (tmp_path / ".gitignore").write_text("existing")  # no trailing newline
        _ensure_tooling_artifacts_untracked(tmp_path)
        content = (tmp_path / ".gitignore").read_text()
        assert "existing\n" in content
        assert ".coverage" in content


class TestEnsureToolingArtifactsGitError:
    def test_git_rm_fails(self, tmp_path, capsys):
        """Covers lines 128-130 — CalledProcessError when git rm fails."""
        _init_git(tmp_path)
        (tmp_path / ".coverage").write_text("d")
        subprocess.run(["git", "-C", str(tmp_path), "add", ".coverage"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "c"], capture_output=True, check=True)
        original_git = __import__("cronpypeline.plugins.swe_plugin", fromlist=["_git"])._git
        def fake_git(repo, *args, check=True):
            if args[0] == "rm":
                raise subprocess.CalledProcessError(1, "git", stderr="error")
            return original_git(repo, *args, check=check)
        with patch("cronpypeline.plugins.issue_fix._git", side_effect=fake_git):
            _ensure_tooling_artifacts_untracked(tmp_path, verbose=True)
        assert "WARNING" in capsys.readouterr().out


class TestIterTaskDirsFileInTasksDir:
    def test_file_in_tasks_dir(self, tmp_path, monkeypatch):
        """Covers line 399 — skipping non-dir entries in TASKS_DIR."""
        (tmp_path / "2025-01-01" / "t1").mkdir(parents=True)
        (tmp_path / "file.txt").write_text("x")
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path)
        assert len(_iter_task_dirs()) == 1


class TestCleanupStaleTaskGitError:
    def test_branch_delete_fails(self, tmp_path, capsys):
        """Covers lines 442-444 — CalledProcessError during git cleanup."""
        target = _make_target_dir(tmp_path)
        _init_git(target)
        (target / ".gitignore").write_text(".SWE/\n")
        subprocess.run(["git", "-C", str(target), "branch", INTEGRATION_BRANCH], capture_output=True, check=True)
        task_dir = tmp_path / "tasks" / "d" / "t"
        task_dir.mkdir(parents=True)
        (task_dir / TASK_FILE).write_text(json.dumps({
            "task_id": "t", "branch": "nonexistent-branch", "default_branch": "main",
            "source_issue_id": "",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }))
        original_git = __import__("cronpypeline.plugins.swe_plugin", fromlist=["_git"])._git
        def fake_git(repo, *args, check=True):
            # rev-parse is called with check=True, make it fail to trigger the except
            if args[0] == "rev-parse":
                raise subprocess.CalledProcessError(1, "git", stderr="err")
            return original_git(repo, *args, check=check)
        with patch("cronpypeline.plugins.issue_fix._git", side_effect=fake_git):
            assert _cleanup_stale_task(target, task_dir, verbose=True) is True
        assert "WARNING" in capsys.readouterr().out


class TestRecoverOrphanedTriagedCorruptTask:
    def test_corrupt_task_json(self, tmp_path, monkeypatch):
        """Covers lines 498-499 — OSError/json.JSONDecodeError when reading task.json."""
        target = _make_target_dir(tmp_path)
        old = (datetime.now(timezone.utc) - timedelta(minutes=TASK_TIMEOUT_MINUTES + 10))
        _write_issue(target / ".SWE" / "issues", "1", status="triaged", created_at=old.isoformat())
        td = tmp_path / "tasks" / "d" / "20250101_repo_1"
        td.mkdir(parents=True)
        (td / TASK_FILE).write_text("not json")
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        _recover_orphaned_triaged(target, "repo", verbose=True)
        # Issue should be reset since the corrupt task doesn't match
        assert "triaged" not in (target / ".SWE" / "issues" / "1.md").read_text()


class TestEnsureTaskBranchGitError:
    def test_checkout_fails(self, tmp_path, capsys):
        """Covers lines 541-543 — CalledProcessError in _ensure_task_branch checkout."""
        _init_git(tmp_path)
        subprocess.run(["git", "-C", str(tmp_path), "branch", INTEGRATION_BRANCH], capture_output=True, check=True)
        original_git = __import__("cronpypeline.plugins.swe_plugin", fromlist=["_git"])._git
        def fake_git(repo, *args, check=True):
            # Let integration branch setup succeed, but fail on task branch checkout
            if args[0] == "checkout" and len(args) > 1 and args[1] == "-b":
                raise subprocess.CalledProcessError(1, "git", stderr="err")
            return original_git(repo, *args, check=check)
        with patch("cronpypeline.plugins.issue_fix._git", side_effect=fake_git):
            assert _ensure_task_branch(tmp_path, "i1", "main", verbose=True) is False
        assert "failed to create" in capsys.readouterr().out


class TestRunSelectUnlinkError:
    def test_unlink_oserror(self, tmp_path, monkeypatch):
        """Covers lines 858-860 — OSError when unlinking stale artifacts."""
        t = _make_target_dir(tmp_path)
        (t / ".SWE" / "repo_briefing.md").write_text("b")
        _write_issue(t / ".SWE" / "issues", "1", status="open", body="F", type="bug")
        _init_git(t)
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        h = MagicMock(); h.execute.return_value = ActionResult(success=True)
        with patch("cronpypeline.plugins.swe_prompts._build_queue_handler", return_value=h), \
             patch("pathlib.Path.unlink", side_effect=OSError("x")):
            assert run_select(t, "repo", {}, _make_tick_context(t)) is True


class TestRunSelectReviewIntegrationFails:
    def test_integration_branch_fails(self, tmp_path, monkeypatch):
        """Covers lines 863-864 — ensure_integration_branch returns False for review issue."""
        t = _make_target_dir(tmp_path)
        (t / ".SWE" / "repo_briefing.md").write_text("b")
        _write_issue(t / ".SWE" / "issues", "1", status="open", body="R", type="review")
        # Not a git repo, so ensure_integration_branch will fail
        monkeypatch.setattr("cronpypeline.plugins.issue_fix.TASKS_DIR", tmp_path / "tasks")
        assert run_select(t, "repo", {}, _make_tick_context(t), verbose=True) is False
