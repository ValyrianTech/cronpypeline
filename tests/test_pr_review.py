"""Tests for cronpypeline.plugins.pr_review — CLI to post a GitHub PR review."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cronpypeline.plugins.pr_review import (
    DEFAULT_REPOS_FILE,
    VALID_EVENTS,
    _load_repo_registry,
    build_parser,
    cmd_post,
    main,
)


# ─── Helpers ────────────────────────────────────────────────────────────────


def _write_repos_file(tmp_path: Path, repos: list[dict]) -> str:
    """Write a repos.json file and return its path."""
    path = tmp_path / "repos.json"
    path.write_text(json.dumps({"repos": repos}), encoding="utf-8")
    return str(path)


def _make_args(**kwargs) -> argparse.Namespace:
    """Build a Namespace with default fields for cmd_post."""
    defaults = {
        "repo": "myrepo",
        "repos_file": DEFAULT_REPOS_FILE,
        "pr_number": 5,
        "event": "COMMENT",
        "body_file": None,
        "body": None,
        "dry_run": False,
        "json": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ─── _load_repo_registry ─────────────────────────────────────────────────────


class TestLoadRepoRegistry:
    def test_loads_repos_from_valid_file(self, tmp_path):
        path = _write_repos_file(tmp_path, [{"name": "repo1"}, {"name": "repo2"}])
        result = _load_repo_registry(path)
        assert len(result) == 2
        assert result[0]["name"] == "repo1"

    def test_returns_empty_list_when_file_missing(self, tmp_path):
        result = _load_repo_registry(str(tmp_path / "nonexistent.json"))
        assert result == []

    def test_returns_empty_list_on_corrupt_json(self, tmp_path, capsys):
        path = tmp_path / "bad.json"
        path.write_text("not json", encoding="utf-8")
        result = _load_repo_registry(str(path))
        assert result == []
        captured = capsys.readouterr()
        assert "ERROR" in captured.err

    def test_returns_empty_list_when_no_repos_key(self, tmp_path):
        path = tmp_path / "repos.json"
        path.write_text(json.dumps({"other": []}), encoding="utf-8")
        result = _load_repo_registry(str(path))
        assert result == []


# ─── build_parser ────────────────────────────────────────────────────────────


class TestBuildParser:
    def test_parses_required_args(self):
        parser = build_parser()
        args = parser.parse_args(["myrepo", "--pr-number", "3", "--event", "COMMENT"])
        assert args.repo == "myrepo"
        assert args.pr_number == 3
        assert args.event == "COMMENT"

    def test_event_choices_are_validated(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["myrepo", "--pr-number", "3", "--event", "INVALID"])

    def test_pr_number_is_required(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["myrepo", "--event", "COMMENT"])

    def test_dry_run_flag(self):
        parser = build_parser()
        args = parser.parse_args(["myrepo", "--pr-number", "1", "--event", "APPROVE", "--dry-run"])
        assert args.dry_run is True

    def test_json_flag(self):
        parser = build_parser()
        args = parser.parse_args(["myrepo", "--pr-number", "1", "--event", "APPROVE", "--json"])
        assert args.json is True

    def test_body_and_body_file_options(self):
        parser = build_parser()
        args = parser.parse_args([
            "myrepo", "--pr-number", "1", "--event", "COMMENT",
            "--body", "inline body",
        ])
        assert args.body == "inline body"
        args2 = parser.parse_args([
            "myrepo", "--pr-number", "1", "--event", "COMMENT",
            "--body-file", "/tmp/review.md",
        ])
        assert args2.body_file == "/tmp/review.md"

    def test_repos_file_default(self):
        parser = build_parser()
        args = parser.parse_args(["myrepo", "--pr-number", "1", "--event", "COMMENT"])
        assert args.repos_file == DEFAULT_REPOS_FILE

    def test_valid_events_constant(self):
        assert "COMMENT" in VALID_EVENTS
        assert "APPROVE" in VALID_EVENTS
        assert "REQUEST_CHANGES" in VALID_EVENTS


# ─── cmd_post ────────────────────────────────────────────────────────────────


class TestCmdPost:
    def test_repo_not_found_returns_1(self, tmp_path, capsys):
        path = _write_repos_file(tmp_path, [{"name": "other"}])
        args = _make_args(repos_file=path, repo="myrepo", body="review body")
        assert cmd_post(args) == 1
        assert "not found in registry" in capsys.readouterr().err

    def test_body_file_not_found_returns_1(self, tmp_path, capsys):
        path = _write_repos_file(tmp_path, [{"name": "myrepo", "slug": "org/myrepo"}])
        args = _make_args(repos_file=path, body_file=str(tmp_path / "noexist.md"))
        assert cmd_post(args) == 1
        assert "not found" in capsys.readouterr().err

    def test_body_from_file(self, tmp_path):
        path = _write_repos_file(tmp_path, [{"name": "myrepo", "slug": "org/myrepo", "github_token": "tok"}])
        body_file = tmp_path / "review.md"
        body_file.write_text("Great work!\n", encoding="utf-8")
        args = _make_args(repos_file=path, body_file=str(body_file), dry_run=True)
        assert cmd_post(args) == 0

    def test_body_from_inline(self, tmp_path):
        path = _write_repos_file(tmp_path, [{"name": "myrepo", "slug": "org/myrepo", "github_token": "tok"}])
        args = _make_args(repos_file=path, body="LGTM", dry_run=True)
        assert cmd_post(args) == 0

    def test_empty_body_returns_1(self, tmp_path, capsys):
        path = _write_repos_file(tmp_path, [{"name": "myrepo", "slug": "org/myrepo", "github_token": "tok"}])
        args = _make_args(repos_file=path, body="  ")
        assert cmd_post(args) == 1
        assert "non-empty" in capsys.readouterr().err

    def test_no_body_at_all_returns_1(self, tmp_path, capsys):
        path = _write_repos_file(tmp_path, [{"name": "myrepo", "slug": "org/myrepo", "github_token": "tok"}])
        args = _make_args(repos_file=path, body=None, body_file=None)
        assert cmd_post(args) == 1
        assert "non-empty" in capsys.readouterr().err

    def test_invalid_pr_number_returns_1(self, tmp_path, capsys):
        path = _write_repos_file(tmp_path, [{"name": "myrepo", "slug": "org/myrepo", "github_token": "tok"}])
        args = _make_args(repos_file=path, body="review", pr_number=0)
        assert cmd_post(args) == 1
        assert "positive integer" in capsys.readouterr().err

    def test_no_token_returns_1(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("SWE_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        path = _write_repos_file(tmp_path, [{"name": "myrepo", "slug": "org/myrepo"}])
        args = _make_args(repos_file=path, body="review")
        with patch("cronpypeline.plugins.pr_review._load_github_token", return_value=None):
            assert cmd_post(args) == 1
        assert "no GitHub token" in capsys.readouterr().err

    def test_invalid_slug_returns_1(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("SWE_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        path = _write_repos_file(tmp_path, [{"name": "myrepo", "slug": "invalidslug"}])
        args = _make_args(repos_file=path, body="review")
        with patch("cronpypeline.plugins.pr_review._load_github_token", return_value="tok"):
            assert cmd_post(args) == 1
        assert "slug" in capsys.readouterr().err

    def test_dry_run_prints_summary(self, tmp_path, capsys):
        path = _write_repos_file(tmp_path, [{"name": "myrepo", "slug": "org/myrepo", "github_token": "tok"}])
        args = _make_args(repos_file=path, body="Looks good", dry_run=True)
        assert cmd_post(args) == 0
        out = capsys.readouterr().out
        assert "[DRY-RUN]" in out
        assert "COMMENT" in out
        assert "org/myrepo#5" in out

    def test_dry_run_truncates_long_body(self, tmp_path, capsys):
        path = _write_repos_file(tmp_path, [{"name": "myrepo", "slug": "org/myrepo", "github_token": "tok"}])
        long_body = "x" * 600
        args = _make_args(repos_file=path, body=long_body, dry_run=True)
        assert cmd_post(args) == 0
        out = capsys.readouterr().out
        assert "600 chars" in out

    def test_successful_post_returns_0(self, tmp_path, capsys):
        path = _write_repos_file(tmp_path, [{"name": "myrepo", "slug": "org/myrepo", "github_token": "tok"}])
        args = _make_args(repos_file=path, body="Nice!")
        mock_result = {"id": 42, "state": "commented"}
        with patch("cronpypeline.plugins.pr_review._gh_api_post", return_value=mock_result):
            assert cmd_post(args) == 0
        out = capsys.readouterr().out
        assert "id=42" in out
        assert "org/myrepo#5" in out

    def test_failed_post_returns_1(self, tmp_path, capsys):
        path = _write_repos_file(tmp_path, [{"name": "myrepo", "slug": "org/myrepo", "github_token": "tok"}])
        args = _make_args(repos_file=path, body="Nice!")
        with patch("cronpypeline.plugins.pr_review._gh_api_post", return_value=None):
            assert cmd_post(args) == 1
        assert "failed to post" in capsys.readouterr().err

    def test_json_flag_prints_response(self, tmp_path, capsys):
        path = _write_repos_file(tmp_path, [{"name": "myrepo", "slug": "org/myrepo", "github_token": "tok"}])
        args = _make_args(repos_file=path, body="Nice!", json=True)
        mock_result = {"id": 99, "state": "approved"}
        with patch("cronpypeline.plugins.pr_review._gh_api_post", return_value=mock_result):
            assert cmd_post(args) == 0
        out = capsys.readouterr().out
        assert '"id": 99' in out

    def test_approve_event(self, tmp_path, capsys):
        path = _write_repos_file(tmp_path, [{"name": "myrepo", "slug": "org/myrepo", "github_token": "tok"}])
        args = _make_args(repos_file=path, body="Approved", event="APPROVE", dry_run=True)
        assert cmd_post(args) == 0
        assert "APPROVE" in capsys.readouterr().out

    def test_request_changes_event(self, tmp_path, capsys):
        path = _write_repos_file(tmp_path, [{"name": "myrepo", "slug": "org/myrepo", "github_token": "tok"}])
        args = _make_args(repos_file=path, body="Please fix", event="REQUEST_CHANGES", dry_run=True)
        assert cmd_post(args) == 0
        assert "REQUEST_CHANGES" in capsys.readouterr().out

    def test_invalid_event_returns_1(self, tmp_path, capsys):
        """Test the redundant event validation in cmd_post (bypasses argparse choices)."""
        path = _write_repos_file(tmp_path, [{"name": "myrepo", "slug": "org/myrepo", "github_token": "tok"}])
        args = _make_args(repos_file=path, body="review", event="")
        assert cmd_post(args) == 1
        assert "--event" in capsys.readouterr().err

    def test_empty_slug_returns_1(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("SWE_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        path = _write_repos_file(tmp_path, [{"name": "myrepo", "slug": ""}])
        args = _make_args(repos_file=path, body="review")
        with patch("cronpypeline.plugins.pr_review._load_github_token", return_value="tok"):
            assert cmd_post(args) == 1
        assert "slug" in capsys.readouterr().err

    def test_body_file_strips_whitespace(self, tmp_path, capsys):
        path = _write_repos_file(tmp_path, [{"name": "myrepo", "slug": "org/myrepo", "github_token": "tok"}])
        body_file = tmp_path / "review.md"
        body_file.write_text("  padded body  \n", encoding="utf-8")
        args = _make_args(repos_file=path, body_file=str(body_file), dry_run=True)
        assert cmd_post(args) == 0
        out = capsys.readouterr().out
        assert "padded body" in out


# ─── main ────────────────────────────────────────────────────────────────────


class TestMain:
    def test_main_calls_cmd_post(self, tmp_path):
        path = _write_repos_file(tmp_path, [{"name": "myrepo", "slug": "org/myrepo", "github_token": "tok"}])
        body_file = tmp_path / "review.md"
        body_file.write_text("body", encoding="utf-8")
        with patch("sys.argv", ["pr_review", "myrepo", "--pr-number", "1",
                                "--event", "COMMENT", "--body-file", str(body_file),
                                "--repos-file", path, "--dry-run"]):
            assert main() == 0

    def test_main_module_entrypoint(self, tmp_path):
        """Covers the `if __name__ == "__main__"` guard."""
        import runpy
        path = _write_repos_file(tmp_path, [{"name": "myrepo", "slug": "org/myrepo", "github_token": "tok"}])
        body_file = tmp_path / "review.md"
        body_file.write_text("body", encoding="utf-8")
        with patch("sys.argv", ["pr_review", "myrepo", "--pr-number", "1",
                                "--event", "COMMENT", "--body-file", str(body_file),
                                "--repos-file", path, "--dry-run"]):
            with pytest.raises(SystemExit) as exc_info:
                runpy.run_path(
                    str(Path(__import__("cronpypeline.plugins.pr_review", fromlist=["__file__"]).__file__)),
                    run_name="__main__",
                )
            assert exc_info.value.code == 0
