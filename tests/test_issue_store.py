"""Tests for cronpypeline.plugins.issue_store — SWE issue store with YAML frontmatter."""

import json
from pathlib import Path

import pytest

from cronpypeline.plugins.issue_store import (
    Issue,
    load_issues,
    get_issue,
    set_issue_status,
    create_issue,
    finalize_issue_outcome,
    parse_frontmatter,
    serialize_frontmatter,
)


def _write_issue_file(issues_dir: Path, issue_id: str, frontmatter: dict, body: str = "") -> Path:
    """Helper: write a single issue .md file with YAML frontmatter."""
    issues_dir.mkdir(parents=True, exist_ok=True)
    path = issues_dir / f"{issue_id}.md"
    fm_text = serialize_frontmatter(frontmatter)
    path.write_text(f"---\n{fm_text}---\n{body}")
    return path


class TestParseFrontmatter:
    """Tests for YAML frontmatter parsing."""

    def test_parse_simple_frontmatter(self):
        text = "---\nid: 1\nstatus: open\n---\nBody text"
        fm, body = parse_frontmatter(text)
        assert fm["id"] == 1
        assert fm["status"] == "open"
        assert body == "Body text"

    def test_parse_string_id(self):
        text = "---\nid: issue-42\nstatus: open\n---\n"
        fm, body = parse_frontmatter(text)
        assert fm["id"] == "issue-42"
        assert fm["status"] == "open"

    def test_parse_float_value(self):
        text = "---\nhivemind_score: 0.85\n---\n"
        fm, _ = parse_frontmatter(text)
        assert fm["hivemind_score"] == 0.85

    def test_parse_list_value(self):
        text = "---\nlabels: [bug, fix, urgent]\n---\n"
        fm, _ = parse_frontmatter(text)
        assert fm["labels"] == ["bug", "fix", "urgent"]

    def test_parse_no_frontmatter(self):
        text = "Just body text, no frontmatter"
        fm, body = parse_frontmatter(text)
        assert fm == {}
        assert body == "Just body text, no frontmatter"

    def test_parse_empty_body(self):
        text = "---\nid: 1\n---\n"
        fm, body = parse_frontmatter(text)
        assert fm["id"] == 1
        assert body == ""


class TestSerializeFrontmatter:
    """Tests for YAML frontmatter serialization."""

    def test_serialize_simple(self):
        fm = {"id": 1, "status": "open", "attempts": 0}
        text = serialize_frontmatter(fm)
        assert "id: 1" in text
        assert "status: open" in text
        assert "attempts: 0" in text

    def test_serialize_string_values(self):
        fm = {"id": "issue-42", "source": "github"}
        text = serialize_frontmatter(fm)
        assert "id: issue-42" in text
        assert "source: github" in text

    def test_serialize_list_value(self):
        fm = {"labels": ["bug", "fix"]}
        text = serialize_frontmatter(fm)
        assert "labels: [bug, fix]" in text

    def test_serialize_float(self):
        fm = {"hivemind_score": 0.5}
        text = serialize_frontmatter(fm)
        assert "hivemind_score: 0.5" in text

    def test_roundtrip(self):
        fm = {"id": 42, "status": "open", "attempts": 2, "labels": ["bug"]}
        text = serialize_frontmatter(fm)
        parsed, _ = parse_frontmatter(f"---\n{text}---\nbody")
        assert parsed == fm


class TestLoadIssues:
    """Tests for load_issues."""

    def test_load_multiple_issues(self, tmp_path):
        issues_dir = tmp_path / ".SWE" / "issues"
        _write_issue_file(issues_dir, "issue-1", {"id": 1, "status": "open"})
        _write_issue_file(issues_dir, "issue-2", {"id": 2, "status": "closed"})

        issues = load_issues(tmp_path)
        assert len(issues) == 2
        ids = [i.id for i in issues]
        assert 1 in ids
        assert 2 in ids

    def test_load_empty_when_no_issues_dir(self, tmp_path):
        issues = load_issues(tmp_path)
        assert issues == []

    def test_load_empty_when_no_md_files(self, tmp_path):
        issues_dir = tmp_path / ".SWE" / "issues"
        issues_dir.mkdir(parents=True)
        issues = load_issues(tmp_path)
        assert issues == []

    def test_load_preserves_all_fields(self, tmp_path):
        issues_dir = tmp_path / ".SWE" / "issues"
        _write_issue_file(issues_dir, "issue-1", {
            "id": 1,
            "source": "dep-audit",
            "type": "bug",
            "status": "open",
            "attempts": 2,
            "hivemind_score": 0.5,
            "rank": 1,
            "repo": "org/repo",
            "labels": ["bug", "fix"],
            "github_number": 42,
            "github_url": "https://github.com/org/repo/issues/42",
            "created_at": "2024-01-01T12:00:00",
        }, body="Some issue description")

        issues = load_issues(tmp_path)
        issue = issues[0]
        assert issue.id == 1
        assert issue.source == "dep-audit"
        assert issue.type == "bug"
        assert issue.status == "open"
        assert issue.attempts == 2
        assert issue.hivemind_score == 0.5
        assert issue.rank == 1
        assert issue.repo == "org/repo"
        assert issue.labels == ["bug", "fix"]
        assert issue.github_number == 42
        assert issue.github_url == "https://github.com/org/repo/issues/42"
        assert issue.created_at == "2024-01-01T12:00:00"
        assert issue.body == "Some issue description"

    def test_load_skips_non_md_files(self, tmp_path):
        issues_dir = tmp_path / ".SWE" / "issues"
        issues_dir.mkdir(parents=True)
        _write_issue_file(issues_dir, "issue-1", {"id": 1, "status": "open"})
        (issues_dir / "README.txt").write_text("not an issue")

        issues = load_issues(tmp_path)
        assert len(issues) == 1


class TestGetIssue:
    """Tests for get_issue."""

    def test_get_existing_issue(self, tmp_path):
        issues_dir = tmp_path / ".SWE" / "issues"
        _write_issue_file(issues_dir, "issue-42", {"id": 42, "status": "open"})

        issue = get_issue(tmp_path, 42)
        assert issue is not None
        assert issue.id == 42
        assert issue.status == "open"

    def test_get_nonexistent_issue(self, tmp_path):
        issue = get_issue(tmp_path, 999)
        assert issue is None

    def test_get_issue_by_string_id(self, tmp_path):
        issues_dir = tmp_path / ".SWE" / "issues"
        _write_issue_file(issues_dir, "issue-abc", {"id": "abc", "status": "open"})

        issue = get_issue(tmp_path, "abc")
        assert issue is not None
        assert issue.id == "abc"


class TestSetIssueStatus:
    """Tests for set_issue_status."""

    def test_set_status_updates_file(self, tmp_path):
        issues_dir = tmp_path / ".SWE" / "issues"
        _write_issue_file(issues_dir, "issue-1", {"id": 1, "status": "open", "attempts": 0})

        result = set_issue_status(tmp_path, 1, "in_progress")
        assert result is True

        issue = get_issue(tmp_path, 1)
        assert issue.status == "in_progress"
        assert issue.attempts == 0  # other fields preserved

    def test_set_status_nonexistent_issue(self, tmp_path):
        result = set_issue_status(tmp_path, 999, "open")
        assert result is False

    def test_set_status_preserves_body(self, tmp_path):
        issues_dir = tmp_path / ".SWE" / "issues"
        _write_issue_file(issues_dir, "issue-1", {"id": 1, "status": "open"}, body="Issue body")

        set_issue_status(tmp_path, 1, "closed")
        issue = get_issue(tmp_path, 1)
        assert issue.body == "Issue body"


class TestCreateIssue:
    """Tests for create_issue."""

    def test_create_issue_writes_file(self, tmp_path):
        issue = create_issue(tmp_path, {
            "id": "issue-1",
            "source": "pipeline",
            "type": "bug",
            "status": "open",
            "attempts": 0,
            "repo": "org/repo",
        }, body="New issue description")

        assert issue is not None
        assert issue.id == "issue-1"
        assert issue.status == "open"

        # File exists
        issue_file = tmp_path / ".SWE" / "issues" / "issue-1.md"
        assert issue_file.exists()

        # Can be read back
        loaded = get_issue(tmp_path, "issue-1")
        assert loaded is not None
        assert loaded.source == "pipeline"
        assert loaded.body == "New issue description"

    def test_create_issue_generates_filename_from_id(self, tmp_path):
        create_issue(tmp_path, {"id": 42, "status": "open"})
        issue_file = tmp_path / ".SWE" / "issues" / "42.md"
        assert issue_file.exists()


class TestFinalizeIssueOutcome:
    """Tests for finalize_issue_outcome."""

    def test_finalize_sets_status_and_increments_attempts(self, tmp_path):
        issues_dir = tmp_path / ".SWE" / "issues"
        _write_issue_file(issues_dir, "issue-1", {"id": 1, "status": "in_progress", "attempts": 1})

        result = finalize_issue_outcome(tmp_path, 1, "done")
        assert result is True

        issue = get_issue(tmp_path, 1)
        assert issue.status == "done"
        assert issue.attempts == 2

    def test_finalize_nonexistent_issue(self, tmp_path):
        result = finalize_issue_outcome(tmp_path, 999, "done")
        assert result is False

    def test_finalize_discarded_status(self, tmp_path):
        issues_dir = tmp_path / ".SWE" / "issues"
        _write_issue_file(issues_dir, "issue-1", {"id": 1, "status": "in_progress", "attempts": 3})

        result = finalize_issue_outcome(tmp_path, 1, "discarded")
        assert result is True

        issue = get_issue(tmp_path, 1)
        assert issue.status == "discarded"
        assert issue.attempts == 4


class TestIssueDataclass:
    """Tests for the Issue dataclass."""

    def test_issue_to_dict(self):
        issue = Issue(id=1, status="open", source="github", attempts=0)
        d = issue.to_dict()
        assert d["id"] == 1
        assert d["status"] == "open"
        assert d["source"] == "github"
        assert d["attempts"] == 0

    def test_issue_from_dict(self):
        d = {"id": 42, "status": "closed", "source": "review", "attempts": 3}
        issue = Issue.from_dict(d)
        assert issue.id == 42
        assert issue.status == "closed"
        assert issue.source == "review"
        assert issue.attempts == 3

    def test_issue_defaults(self):
        issue = Issue(id=1)
        assert issue.status == "open"
        assert issue.attempts == 0
        assert issue.source is None
        assert issue.labels == []
        assert issue.body == ""
