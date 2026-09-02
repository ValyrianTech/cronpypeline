"""Tests for cronpypeline.plugins.issue_store — SWE issue store with YAML frontmatter."""

from pathlib import Path

import pytest

from cronpypeline.plugins.issue_store import (
    Issue,
    create_issue,
    finalize_issue_outcome,
    get_issue,
    issue_filename,
    load_issues,
    parse_frontmatter,
    serialize_frontmatter,
    set_issue_status,
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
        fm, _body = parse_frontmatter(text)
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

    def test_parse_boolean_true(self):
        text = "---\nenabled: true\n---\n"
        fm, _ = parse_frontmatter(text)
        assert fm["enabled"] is True

    def test_parse_boolean_false(self):
        text = "---\nenabled: false\n---\n"
        fm, _ = parse_frontmatter(text)
        assert fm["enabled"] is False

    def test_parse_boolean_yes(self):
        text = "---\nenabled: yes\n---\n"
        fm, _ = parse_frontmatter(text)
        assert fm["enabled"] is True

    def test_parse_boolean_no(self):
        text = "---\nenabled: no\n---\n"
        fm, _ = parse_frontmatter(text)
        assert fm["enabled"] is False

    def test_parse_null(self):
        text = "---\nvalue: null\n---\n"
        fm, _ = parse_frontmatter(text)
        assert fm["value"] is None

    def test_parse_none(self):
        text = "---\nvalue: none\n---\n"
        fm, _ = parse_frontmatter(text)
        assert fm["value"] is None

    def test_parse_tilde(self):
        text = "---\nvalue: ~\n---\n"
        fm, _ = parse_frontmatter(text)
        assert fm["value"] is None

    def test_parse_boolean_in_list(self):
        text = "---\nflags: [true, false]\n---\n"
        fm, _ = parse_frontmatter(text)
        assert fm["flags"] == [True, False]

    def test_parse_null_in_list(self):
        text = "---\nitems: [null, 1]\n---\n"
        fm, _ = parse_frontmatter(text)
        assert fm["items"] == [None, 1]

    def test_roundtrip_boolean(self):
        fm = {"enabled": True, "disabled": False}
        text = serialize_frontmatter(fm)
        parsed, _ = parse_frontmatter(f"---\n{text}---\nbody")
        assert parsed == fm

    def test_roundtrip_null(self):
        fm = {"value": None}
        text = serialize_frontmatter(fm)
        parsed, _ = parse_frontmatter(f"---\n{text}---\nbody")
        assert parsed == fm

    def test_double_quoted_value_with_colon(self):
        fm, _ = parse_frontmatter('---\ntitle: "Fix: Login bug"\n---\n')
        assert fm["title"] == "Fix: Login bug"

    def test_single_quoted_value_with_colon(self):
        fm, _ = parse_frontmatter("---\ntitle: 'Refactor: Module X'\n---\n")
        assert fm["title"] == "Refactor: Module X"

    def test_single_quoted_value_with_apostrophe(self):
        fm, _ = parse_frontmatter("---\ntitle: 'It's a test'\n---\n")
        assert fm["title"] == "It's a test"

    def test_unquoted_url_value(self):
        fm, _ = parse_frontmatter("---\nurl: https://example.com/path\n---\n")
        assert fm["url"] == "https://example.com/path"

    def test_multiple_colons_in_quotes(self):
        fm, _ = parse_frontmatter('---\ntitle: "Bug: Fix: Login"\n---\n')
        assert fm["title"] == "Bug: Fix: Login"

    def test_regular_value_with_colon_fix(self):
        fm, _ = parse_frontmatter("---\nid: 42\n---\n")
        assert fm["id"] == 42

    def test_roundtrip_colon_value(self):
        fm = {"title": "Fix: Login bug"}
        text = serialize_frontmatter(fm)
        parsed, _ = parse_frontmatter(f"---\n{text}---\nbody")
        assert parsed == fm
        assert parsed["title"] == "Fix: Login bug"

    def test_unterminated_single_quote_value(self):
        fm, _ = parse_frontmatter("---\ntitle: 'unterminated\n---\n")
        assert fm["title"] == "'unterminated"

    def test_warns_on_content_after_closing_quote(self):
        with pytest.warns(UserWarning, match="Content after closing quote"):
            fm, _ = parse_frontmatter('---\ntitle: "Fix: Login bug" extra\n---\n')
        assert fm["title"] == "Fix: Login bug"

    def test_warning_includes_key_and_discarded_content(self):
        with pytest.warns(UserWarning, match=r"title.*extra"):
            fm, _ = parse_frontmatter('---\ntitle: "Fix: Login bug" extra\n---\n')
        assert fm["title"] == "Fix: Login bug"

    def test_no_warning_without_content_after_quote(self, recwarn):
        fm, _ = parse_frontmatter('---\ntitle: "Fix: Login bug"\n---\n')
        assert fm["title"] == "Fix: Login bug"
        assert len(recwarn) == 0

    def test_no_warning_with_trailing_whitespace_after_quote(self, recwarn):
        fm, _ = parse_frontmatter('---\ntitle: "Fix: Login bug"   \n---\n')
        assert fm["title"] == "Fix: Login bug"
        assert len(recwarn) == 0

    def test_single_quote_warns_on_content_after_quote(self):
        with pytest.warns(UserWarning, match="Content after closing quote"):
            fm, _ = parse_frontmatter("---\ntitle: 'Refactor: Module X' trailing\n---\n")
        assert fm["title"] == "Refactor: Module X"


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

    def test_create_issue_rejects_path_traversal(self, tmp_path):
        with pytest.raises(ValueError, match="Issue id contains '..'"):
            create_issue(tmp_path, {"id": "../../evil", "status": "open"})
        assert not (tmp_path / "evil.md").exists()
        assert not (tmp_path.parent / "evil.md").exists()

    def test_create_issue_special_chars_only_falls_back_to_issue(self, tmp_path):
        create_issue(tmp_path, {"id": "!!!", "status": "open"})
        issues_dir = tmp_path / ".SWE" / "issues"
        assert (issues_dir / "issue.md").exists()

    def test_create_issue_slashes_only_falls_back_to_issue(self, tmp_path):
        create_issue(tmp_path, {"id": "///", "status": "open"})
        issues_dir = tmp_path / ".SWE" / "issues"
        assert (issues_dir / "issue.md").exists()

    def test_create_issue_normal_id_still_works(self, tmp_path):
        create_issue(tmp_path, {"id": "issue-1", "status": "open"})
        issues_dir = tmp_path / ".SWE" / "issues"
        assert (issues_dir / "issue-1.md").exists()

    def test_create_issue_sanitizes_absolute_path(self, tmp_path):
        create_issue(tmp_path, {"id": "/etc/passwd", "status": "open"})
        issues_dir = tmp_path / ".SWE" / "issues"
        assert (issues_dir / "etc-passwd.md").exists()
        assert not (tmp_path.parent / "etc" / "passwd.md").exists()

    def test_create_issue_sanitizes_mixed_chars_and_slashes(self, tmp_path):
        create_issue(tmp_path, {"id": "foo/bar", "status": "open"})
        issues_dir = tmp_path / ".SWE" / "issues"
        assert (issues_dir / "foo-bar.md").exists()

    def test_create_issue_raises_on_collision(self, tmp_path):
        create_issue(tmp_path, {"id": "foo/bar", "status": "open"}, body="first")
        with pytest.raises(ValueError, match="refusing to overwrite"):
            create_issue(tmp_path, {"id": "foo-bar", "status": "open"}, body="second")

        loaded = get_issue(tmp_path, "foo/bar")
        assert loaded is not None
        assert loaded.id == "foo/bar"
        assert loaded.body == "first"

    def test_create_issue_no_warning_on_same_id(self, tmp_path, recwarn):
        create_issue(tmp_path, {"id": "foo-bar", "status": "open"}, body="first")
        create_issue(tmp_path, {"id": "foo-bar", "status": "open"}, body="second")

        assert len(recwarn) == 0

    def test_create_issue_no_warning_on_new_file(self, tmp_path, recwarn):
        create_issue(tmp_path, {"id": "new-issue", "status": "open"})

        assert len(recwarn) == 0

    def test_create_issue_path_escape_raises_value_error(self, tmp_path, monkeypatch):
        import cronpypeline.plugins.issue_store as issue_store_mod

        def fake_sub(pattern, repl, string):
            return "../evil"

        monkeypatch.setattr(issue_store_mod.re, "sub", fake_sub)

        with pytest.raises(ValueError, match="escapes issues directory"):
            create_issue(tmp_path, {"id": "anything", "status": "open"})


class TestIssueFilename:
    """Tests for issue_filename helper."""

    def test_normal_id_unchanged(self):
        assert issue_filename("issue-1") == "issue-1"

    def test_normal_id_with_dots_and_underscores(self):
        assert issue_filename("v1.2.3_rc") == "v1.2.3_rc"

    def test_special_chars_replaced_with_hyphen(self):
        assert issue_filename("my issue!") == "my-issue"

    def test_slashes_replaced_with_hyphen(self):
        assert issue_filename("foo/bar") == "foo-bar"

    def test_path_traversal_rejected(self):
        with pytest.raises(ValueError, match="Issue id contains '..'"):
            issue_filename("../../evil")

    def test_dotdot_rejected(self):
        with pytest.raises(ValueError, match="Issue id contains '..'"):
            issue_filename("..")

    def test_embedded_dotdot_rejected(self):
        with pytest.raises(ValueError, match="Issue id contains '..'"):
            issue_filename("foo..bar")

    def test_triple_dot_rejected(self):
        with pytest.raises(ValueError, match="Issue id contains '..'"):
            issue_filename("foo...bar")

    def test_single_dot_allowed(self):
        assert issue_filename("foo.bar") == "foo.bar"

    def test_absolute_path_sanitized(self):
        assert issue_filename("/etc/passwd") == "etc-passwd"

    def test_special_only_returns_issue(self):
        assert issue_filename("!!!") == "issue"

    def test_slashes_only_returns_issue(self):
        assert issue_filename("///") == "issue"

    def test_empty_string_returns_issue(self):
        assert issue_filename("") == "issue"

    def test_integer_id(self):
        assert issue_filename(42) == "42"

    def test_mixed_chars_and_slashes(self):
        assert issue_filename("foo/bar baz") == "foo-bar-baz"


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


class TestParseFrontmatterEdgeCases:
    """Tests for parse_frontmatter edge cases."""

    def test_no_closing_delimiter(self):
        """Frontmatter without closing --- should return empty dict and full text."""
        fm, body = parse_frontmatter("---\nid: 42\nstatus: open\nNo closing delimiter")
        assert fm == {}
        assert "No closing delimiter" in body

    def test_line_without_colon_skipped(self):
        """Lines without a colon in frontmatter should be skipped."""
        fm, _body = parse_frontmatter("---\nid: 42\nthis line has no colon\nstatus: open\n---\nBody")
        assert fm["id"] == 42
        assert fm["status"] == "open"
        assert "this line has no colon" not in fm


class TestIssueToDictAllFields:
    """Tests for Issue.to_dict with all optional fields."""

    def test_to_dict_includes_all_optional_fields(self):
        """All optional fields should be included when set."""
        issue = Issue(
            id=42,
            status="open",
            source="github",
            type="bug",
            hivemind_score=0.85,
            rank=3,
            repo="org/repo",
            github_number=123,
            github_url="https://github.com/org/repo/issues/123",
            created_at="2024-01-01",
            labels=["bug", "urgent"],
            body="Issue body",
        )
        d = issue.to_dict()
        assert d["hivemind_score"] == 0.85
        assert d["rank"] == 3
        assert d["repo"] == "org/repo"
        assert d["github_number"] == 123
        assert d["github_url"] == "https://github.com/org/repo/issues/123"
        assert d["created_at"] == "2024-01-01"


class TestIssuesDirRaises:
    """Tests for _issues_dir with None target_dir."""

    def test_none_target_dir_raises_value_error(self):
        """_issues_dir with None should raise ValueError."""
        import pytest

        from cronpypeline.plugins.issue_store import _issues_dir
        with pytest.raises(ValueError, match="target_dir is required"):
            _issues_dir(None)


class TestCreateIssueWithNoneData:
    """Tests for create_issue with None issue_data."""

    def test_create_issue_none_data_defaults_to_empty(self, tmp_path):
        """create_issue with None issue_data should use empty dict."""
        issue = create_issue(tmp_path, issue_data=None, body="Test body")
        assert issue.body == "Test body"


class TestFinalizeIssueOutcomeNotFound:
    """Tests for finalize_issue_outcome when issue not found."""

    def test_finalize_issue_not_found_returns_false(self, tmp_path):
        """finalize_issue_outcome with non-existent issue should return False."""
        issues_dir = tmp_path / ".SWE" / "issues"
        _write_issue_file(issues_dir, "issue-1", {"id": 1, "status": "open"})

        result = finalize_issue_outcome(tmp_path, 999, "done")
        assert result is False

    def test_finalize_issue_no_issues_dir_returns_false(self, tmp_path):
        """finalize_issue_outcome with no issues dir should return False."""
        result = finalize_issue_outcome(tmp_path, 1, "done")
        assert result is False


class TestGetIssueNotFound:
    """Tests for get_issue when issue not found."""

    def test_get_issue_not_found_returns_none(self, tmp_path):
        """get_issue with non-existent issue should return None."""
        issues_dir = tmp_path / ".SWE" / "issues"
        _write_issue_file(issues_dir, "issue-1", {"id": 1, "status": "open"})

        result = get_issue(tmp_path, 999)
        assert result is None


class TestSerializeValueBool:
    """Tests for _serialize_value with boolean values."""

    def test_serialize_true(self):
        from cronpypeline.plugins.issue_store import _serialize_value
        assert _serialize_value(True) == "true"

    def test_serialize_false(self):
        from cronpypeline.plugins.issue_store import _serialize_value
        assert _serialize_value(False) == "false"

    def test_serialize_float(self):
        from cronpypeline.plugins.issue_store import _serialize_value
        assert _serialize_value(3.14) == "3.14"

    def test_serialize_none(self):
        from cronpypeline.plugins.issue_store import _serialize_value
        assert _serialize_value(None) == "null"

    def test_serialize_none_in_list(self):
        from cronpypeline.plugins.issue_store import _serialize_value
        assert _serialize_value([None, 1]) == "[null, 1]"


class TestNeedsQuotingMatchingQuotes:
    """Tests for _needs_quoting detecting matching surrounding quotes."""

    def test_needs_quoting_double_quoted(self):
        from cronpypeline.plugins.issue_store import _needs_quoting
        assert _needs_quoting('"bug"') is True

    def test_needs_quoting_single_quoted(self):
        from cronpypeline.plugins.issue_store import _needs_quoting
        assert _needs_quoting("'bug'") is True


class TestSerializeValueQuoting:
    """Tests for _serialize_value quoting ambiguous string values."""

    def test_serialize_string_true(self):
        from cronpypeline.plugins.issue_store import _serialize_value
        assert _serialize_value("true") == "'true'"

    def test_serialize_string_null(self):
        from cronpypeline.plugins.issue_store import _serialize_value
        assert _serialize_value("null") == "'null'"

    def test_serialize_string_int(self):
        from cronpypeline.plugins.issue_store import _serialize_value
        assert _serialize_value("123") == "'123'"

    def test_serialize_string_float(self):
        from cronpypeline.plugins.issue_store import _serialize_value
        assert _serialize_value("3.14") == "'3.14'"

    def test_serialize_string_list(self):
        from cronpypeline.plugins.issue_store import _serialize_value
        assert _serialize_value("[a, b]") == "'[a, b]'"

    def test_serialize_string_yes_no(self):
        from cronpypeline.plugins.issue_store import _serialize_value
        assert _serialize_value("yes") == "'yes'"
        assert _serialize_value("no") == "'no'"

    def test_serialize_string_none_tilde(self):
        from cronpypeline.plugins.issue_store import _serialize_value
        assert _serialize_value("none") == "'none'"
        assert _serialize_value("~") == "'~'"

    def test_serialize_regular_string_unquoted(self):
        from cronpypeline.plugins.issue_store import _serialize_value
        assert _serialize_value("open") == "open"
        assert _serialize_value("issue-42") == "issue-42"

    def test_serialize_double_quoted_string(self):
        from cronpypeline.plugins.issue_store import _serialize_value
        assert _serialize_value('"bug"') == "'\"bug\"'"

    def test_serialize_single_quoted_string(self):
        from cronpypeline.plugins.issue_store import _serialize_value
        assert _serialize_value("'bug'") == '"\'bug\'"'


class TestParseValueQuoting:
    """Tests for _parse_value stripping surrounding quotes."""

    def test_parse_single_quoted_string(self):
        from cronpypeline.plugins.issue_store import _parse_value
        assert _parse_value("'true'") == "true"
        assert _parse_value("'null'") == "null"
        assert _parse_value("'123'") == "123"

    def test_parse_double_quoted_string(self):
        from cronpypeline.plugins.issue_store import _parse_value
        assert _parse_value('"true"') == "true"

    def test_parse_quoted_in_list(self):
        from cronpypeline.plugins.issue_store import _parse_value
        assert _parse_value("['true', 1]") == ["true", 1]

    def test_parse_unterminated_single_quote(self):
        from cronpypeline.plugins.issue_store import _parse_value
        assert _parse_value("'unterminated") == "'unterminated"

    def test_parse_unterminated_double_quote(self):
        from cronpypeline.plugins.issue_store import _parse_value
        assert _parse_value('"unterminated') == '"unterminated'


class TestRoundTripAmbiguousStrings:
    """Tests for round-tripping string values that look like other types."""

    def test_roundtrip_string_true(self):
        fm = {"value": "true"}
        text = serialize_frontmatter(fm)
        parsed, _ = parse_frontmatter(f"---\n{text}---\nbody")
        assert parsed == fm
        assert isinstance(parsed["value"], str)

    def test_roundtrip_string_null(self):
        fm = {"value": "null"}
        text = serialize_frontmatter(fm)
        parsed, _ = parse_frontmatter(f"---\n{text}---\nbody")
        assert parsed == fm
        assert isinstance(parsed["value"], str)

    def test_roundtrip_string_int_float(self):
        fm = {"a": "123", "b": "3.14"}
        text = serialize_frontmatter(fm)
        parsed, _ = parse_frontmatter(f"---\n{text}---\nbody")
        assert parsed == fm
        assert isinstance(parsed["a"], str)
        assert isinstance(parsed["b"], str)

    def test_roundtrip_labels_with_ambiguous_strings(self):
        fm = {"labels": ["true", "null", 1]}
        text = serialize_frontmatter(fm)
        parsed, _ = parse_frontmatter(f"---\n{text}---\nbody")
        assert parsed == fm
        assert isinstance(parsed["labels"][0], str)
        assert isinstance(parsed["labels"][1], str)
        assert parsed["labels"][2] == 1

    def test_roundtrip_labels_with_double_quotes(self):
        fm = {"labels": ['"bug"', "feature"]}
        text = serialize_frontmatter(fm)
        parsed, _ = parse_frontmatter(f"---\n{text}---\nbody")
        assert parsed == fm
        assert parsed["labels"][0] == '"bug"'

    def test_roundtrip_labels_with_single_quotes(self):
        fm = {"labels": ["'bug'", "feature"]}
        text = serialize_frontmatter(fm)
        parsed, _ = parse_frontmatter(f"---\n{text}---\nbody")
        assert parsed == fm
        assert parsed["labels"][0] == "'bug'"

    def test_roundtrip_quoted_ambiguous_string(self):
        fm = {"value": '"true"'}
        text = serialize_frontmatter(fm)
        parsed, _ = parse_frontmatter(f"---\n{text}---\nbody")
        assert parsed == fm
        assert parsed["value"] == '"true"'
        assert isinstance(parsed["value"], str)


class TestSlugify:
    """Tests for _slugify helper."""

    def test_basic_slug(self):
        from cronpypeline.plugins.issue_store import _slugify
        assert _slugify("Missing CLI entry point") == "missing-cli-entry-point"

    def test_special_chars(self):
        from cronpypeline.plugins.issue_store import _slugify
        assert _slugify("Fix: `code` bug (urgent!)") == "fix-code-bug-urgent"

    def test_empty_string(self):
        from cronpypeline.plugins.issue_store import _slugify
        assert _slugify("") == ""

    def test_only_special_chars(self):
        from cronpypeline.plugins.issue_store import _slugify
        assert _slugify("!!!") == ""


class TestMainFile:
    """Tests for the CLI main() 'file' subcommand."""

    def test_file_creates_issue(self, tmp_path, monkeypatch):
        from cronpypeline.plugins.issue_store import main

        monkeypatch.chdir(tmp_path)
        body_file = tmp_path / "finding.md"
        body_file.write_text("## Summary\n\nA bug was found.")

        rc = main(["file", "my-repo", "--type", "bug",
                   "--title", "Missing CLI entry point",
                   "--body-file", str(body_file)])

        assert rc == 0
        issue = get_issue(tmp_path, "missing-cli-entry-point")
        assert issue is not None
        assert issue.status == "open"
        assert issue.source == "review"
        assert issue.type == "bug"
        assert issue.repo == "my-repo"
        assert "A bug was found." in issue.body

    def test_file_default_type_is_bug(self, tmp_path, monkeypatch):
        from cronpypeline.plugins.issue_store import main

        monkeypatch.chdir(tmp_path)
        body_file = tmp_path / "finding.md"
        body_file.write_text("body text")

        rc = main(["file", "my-repo",
                   "--title", "Some issue",
                   "--body-file", str(body_file)])

        assert rc == 0
        issue = get_issue(tmp_path, "some-issue")
        assert issue is not None
        assert issue.type == "bug"

    def test_file_enhancement_type(self, tmp_path, monkeypatch):
        from cronpypeline.plugins.issue_store import main

        monkeypatch.chdir(tmp_path)
        body_file = tmp_path / "finding.md"
        body_file.write_text("body text")

        rc = main(["file", "my-repo", "--type", "enhancement",
                   "--title", "Add feature X",
                   "--body-file", str(body_file)])

        assert rc == 0
        issue = get_issue(tmp_path, "add-feature-x")
        assert issue is not None
        assert issue.type == "enhancement"

    def test_file_duplicate_returns_zero(self, tmp_path, monkeypatch):
        from cronpypeline.plugins.issue_store import main

        monkeypatch.chdir(tmp_path)
        body_file = tmp_path / "finding.md"
        body_file.write_text("body text")

        rc1 = main(["file", "my-repo", "--title", "Dup issue",
                    "--body-file", str(body_file)])
        assert rc1 == 0

        rc2 = main(["file", "my-repo", "--title", "Dup issue",
                    "--body-file", str(body_file)])
        assert rc2 == 0

    def test_file_missing_body_file(self, tmp_path, monkeypatch):
        from cronpypeline.plugins.issue_store import main

        monkeypatch.chdir(tmp_path)
        rc = main(["file", "my-repo", "--title", "Test",
                   "--body-file", str(tmp_path / "nonexistent.md")])
        assert rc == 1

    def test_file_empty_title_slug(self, tmp_path, monkeypatch):
        from cronpypeline.plugins.issue_store import main

        monkeypatch.chdir(tmp_path)
        body_file = tmp_path / "finding.md"
        body_file.write_text("body text")

        rc = main(["file", "my-repo", "--title", "!!!",
                   "--body-file", str(body_file)])
        assert rc == 1

    def test_no_command_prints_help(self, tmp_path, monkeypatch, capsys):
        from cronpypeline.plugins.issue_store import main

        monkeypatch.chdir(tmp_path)
        rc = main([])
        assert rc == 1
        captured = capsys.readouterr()
        assert "issue_store" in captured.out

    def test_file_creates_issues_dir(self, tmp_path, monkeypatch):
        from cronpypeline.plugins.issue_store import main

        monkeypatch.chdir(tmp_path)
        body_file = tmp_path / "finding.md"
        body_file.write_text("body")

        rc = main(["file", "my-repo", "--title", "Test issue",
                   "--body-file", str(body_file)])
        assert rc == 0
        assert (tmp_path / ".SWE" / "issues" / "test-issue.md").exists()
