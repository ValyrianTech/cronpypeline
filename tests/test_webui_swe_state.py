"""Tests for webui SWE plugin state helpers (_swe_state, _read_json, _swe_issue_counts)."""

import json
import sys
from pathlib import Path

# webui/ is not a package — add it to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webui"))

import app


class TestReadJson:
    def test_missing_file(self, tmp_path):
        assert app._read_json(tmp_path / "nope.json") is None

    def test_invalid_json(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{broken")
        assert app._read_json(f) is None

    def test_non_dict(self, tmp_path):
        f = tmp_path / "list.json"
        f.write_text("[1, 2, 3]")
        assert app._read_json(f) is None

    def test_valid_dict(self, tmp_path):
        f = tmp_path / "ok.json"
        f.write_text('{"a": 1}')
        assert app._read_json(f) == {"a": 1}


class TestSweIssueCounts:
    def test_no_dir(self, tmp_path):
        assert app._swe_issue_counts(tmp_path / "issues") is None

    def test_empty_dir(self, tmp_path):
        (tmp_path / "issues").mkdir()
        assert app._swe_issue_counts(tmp_path / "issues") is None

    def test_counts_by_status(self, tmp_path):
        issues = tmp_path / "issues"
        issues.mkdir()
        (issues / "a.md").write_text("---\nstatus: open\n---\nbody")
        (issues / "b.md").write_text("---\nstatus: done\n---\nbody")
        (issues / "c.md").write_text("---\nstatus: open\n---\nbody")
        (issues / "d.md").write_text("---\ntitle: no status\n---\nbody")
        counts = app._swe_issue_counts(issues)
        assert counts == {"open": 2, "done": 1, "unknown": 1}


class TestSweState:
    def test_no_swe_dir(self, tmp_path):
        assert app._swe_state(tmp_path) is None

    def test_empty_swe_dir(self, tmp_path):
        (tmp_path / ".SWE").mkdir()
        assert app._swe_state(tmp_path) is None

    def test_with_pr(self, tmp_path):
        swe = tmp_path / ".SWE"
        swe.mkdir()
        (swe / "pr_published.json").write_text(json.dumps({
            "pr_number": 42,
            "pr_url": "https://github.com/o/r/pull/42",
            "pr_state": "open",
            "pr_review_cycles": 1,
        }))
        result = app._swe_state(tmp_path)
        assert result is not None
        assert result["pr"]["pr_number"] == 42
        assert result["pr"]["pr_state"] == "open"
        assert result["pr"]["pr_review_cycles"] == 1
        assert result["session"] is None
        assert result["issues"] is None

    def test_with_session(self, tmp_path):
        swe = tmp_path / ".SWE"
        swe.mkdir()
        (swe / "github_session.json").write_text(json.dumps({
            "active": True,
            "issue_id": "github-5",
            "github_number": 5,
        }))
        result = app._swe_state(tmp_path)
        assert result is not None
        assert result["session"]["active"] is True
        assert result["session"]["github_number"] == 5
        assert result["pr"] is None

    def test_with_issues(self, tmp_path):
        swe = tmp_path / ".SWE"
        (swe / "issues").mkdir(parents=True)
        (swe / "issues" / "a.md").write_text("---\nstatus: open\n---\nbody")
        result = app._swe_state(tmp_path)
        assert result is not None
        assert result["issues"] == {"open": 1}

    def test_full_state(self, tmp_path):
        swe = tmp_path / ".SWE"
        (swe / "issues").mkdir(parents=True)
        (swe / "pr_published.json").write_text(json.dumps({
            "pr_number": 7,
            "pr_state": "approved",
            "pr_url": "https://github.com/o/r/pull/7",
            "pr_review_cycles": 2,
        }))
        (swe / "github_session.json").write_text(json.dumps({
            "active": True,
            "issue_id": "github-3",
            "github_number": 3,
        }))
        (swe / "issues" / "a.md").write_text("---\nstatus: open\n---\nbody")
        (swe / "issues" / "b.md").write_text("---\nstatus: done\n---\nbody")
        result = app._swe_state(tmp_path)
        assert result is not None
        assert result["pr"]["pr_number"] == 7
        assert result["pr"]["pr_state"] == "approved"
        assert result["session"]["active"] is True
        assert result["issues"] == {"open": 1, "done": 1}

    def test_pr_defaults(self, tmp_path):
        """PR with minimal data should get default field values."""
        swe = tmp_path / ".SWE"
        swe.mkdir()
        (swe / "pr_published.json").write_text(json.dumps({"pr_number": 1}))
        result = app._swe_state(tmp_path)
        assert result["pr"]["pr_state"] == "open"
        assert result["pr"]["pr_review_cycles"] == 0
        assert result["pr"]["filed_issues"] == []
        assert result["pr"]["pr_url"] == ""
