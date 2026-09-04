"""Tests for cronpypeline.markers — MarkerSpec creation/reading."""

import json
import os
import time
from pathlib import Path

import pytest

from cronpypeline.markers import (
    MarkerSpec,
    MarkerType,
    create_marker,
    delete_marker,
    marker_age_seconds,
    marker_exists,
    read_marker,
)


class TestMarkerSpec:
    """Tests for MarkerSpec dataclass."""

    def test_file_marker_spec(self):
        m = MarkerSpec(name="coding_complete.marker", type=MarkerType.FILE, directory=".SWE")
        assert m.name == "coding_complete.marker"
        assert m.type == MarkerType.FILE
        assert m.directory == ".SWE"

    def test_json_marker_spec(self):
        m = MarkerSpec(
            name="task.json",
            type=MarkerType.JSON,
            directory=".",
            content={"issue_id": "123", "retry_count": 0},
        )
        assert m.type == MarkerType.JSON
        assert m.content == {"issue_id": "123", "retry_count": 0}

    def test_symlink_marker_spec(self):
        m = MarkerSpec(
            name="latest.md",
            type=MarkerType.SYMLINK,
            directory=".SWE/reports/test-infra",
            target="20240101_120000.md",
        )
        assert m.type == MarkerType.SYMLINK
        assert m.target == "20240101_120000.md"

    def test_from_dict_file(self):
        m = MarkerSpec.from_dict({
            "type": "file",
            "name": "done.marker",
            "directory": ".SWE",
        })
        assert m.type == MarkerType.FILE
        assert m.name == "done.marker"

    def test_from_dict_json(self):
        m = MarkerSpec.from_dict({
            "type": "json",
            "name": ".processing",
            "directory": ".",
            "content": {"agent": "CoderAgent", "retry_count": 0},
        })
        assert m.type == MarkerType.JSON
        assert m.content["agent"] == "CoderAgent"

    def test_from_dict_symlink(self):
        m = MarkerSpec.from_dict({
            "type": "symlink",
            "name": "latest.md",
            "directory": "reports",
            "target": "20240101.md",
        })
        assert m.type == MarkerType.SYMLINK
        assert m.target == "20240101.md"

    def test_from_dict_defaults_directory_to_dot(self):
        m = MarkerSpec.from_dict({"type": "file", "name": "done.marker"})
        assert m.directory == "."


class TestCreateMarker:
    """Tests for create_marker function."""

    def test_create_file_marker(self, tmp_path):
        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory=".SWE")
        create_marker(m, tmp_path)
        assert (tmp_path / ".SWE" / "done.marker").exists()

    def test_create_file_marker_creates_parent_dirs(self, tmp_path):
        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory=".SWE/reports/infra")
        create_marker(m, tmp_path)
        assert (tmp_path / ".SWE" / "reports" / "infra" / "done.marker").exists()

    def test_create_json_marker(self, tmp_path):
        m = MarkerSpec(
            name="task.json",
            type=MarkerType.JSON,
            directory=".",
            content={"issue_id": "42", "retry_count": 0},
        )
        create_marker(m, tmp_path)
        data = json.loads((tmp_path / "task.json").read_text())
        assert data["issue_id"] == "42"
        assert data["retry_count"] == 0

    def test_create_json_marker_includes_timestamp(self, tmp_path):
        m = MarkerSpec(name=".processing", type=MarkerType.JSON, directory=".", content={})
        create_marker(m, tmp_path)
        data = json.loads((tmp_path / ".processing").read_text())
        assert "timestamp" in data

    def test_create_symlink_marker(self, tmp_path):
        # Create the target file first
        target_file = tmp_path / "20240101_120000.md"
        target_file.write_text("# Report")
        m = MarkerSpec(
            name="latest.md",
            type=MarkerType.SYMLINK,
            directory=".SWE/reports",
            target="20240101_120000.md",
        )
        create_marker(m, tmp_path)
        link_path = tmp_path / ".SWE" / "reports" / "latest.md"
        assert link_path.is_symlink()
        assert os.readlink(link_path) == "20240101_120000.md"

    def test_create_symlink_marker_overwrites_existing(self, tmp_path):
        target1 = tmp_path / "v1.md"
        target1.write_text("v1")
        target2 = tmp_path / "v2.md"
        target2.write_text("v2")
        m = MarkerSpec(name="latest.md", type=MarkerType.SYMLINK, directory="reports", target="v1.md")
        create_marker(m, tmp_path)
        m2 = MarkerSpec(name="latest.md", type=MarkerType.SYMLINK, directory="reports", target="v2.md")
        create_marker(m2, tmp_path)
        link_path = tmp_path / "reports" / "latest.md"
        assert os.readlink(link_path) == "v2.md"


class TestReadMarker:
    """Tests for read_marker function."""

    def test_read_file_marker_exists(self, tmp_path):
        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory=".")
        create_marker(m, tmp_path)
        result = read_marker(m, tmp_path)
        assert result is not None
        assert result["exists"] is True

    def test_read_file_marker_not_exists(self, tmp_path):
        m = MarkerSpec(name="nonexistent.marker", type=MarkerType.FILE, directory=".")
        result = read_marker(m, tmp_path)
        assert result is None

    def test_read_json_marker(self, tmp_path):
        m = MarkerSpec(
            name="task.json",
            type=MarkerType.JSON,
            directory=".",
            content={"issue_id": "42", "retry_count": 2},
        )
        create_marker(m, tmp_path)
        result = read_marker(m, tmp_path)
        assert result is not None
        assert result["issue_id"] == "42"
        assert result["retry_count"] == 2

    def test_read_json_marker_not_exists(self, tmp_path):
        m = MarkerSpec(name="nonexistent.json", type=MarkerType.JSON, directory=".")
        result = read_marker(m, tmp_path)
        assert result is None

    def test_read_symlink_marker(self, tmp_path):
        target = tmp_path / "report.md"
        target.write_text("# Report")
        m = MarkerSpec(name="latest.md", type=MarkerType.SYMLINK, directory="reports", target="report.md")
        create_marker(m, tmp_path)
        result = read_marker(m, tmp_path)
        assert result is not None
        assert result["target"] == "report.md"


class TestMarkerExists:
    """Tests for marker_exists function."""

    def test_marker_exists_true(self, tmp_path):
        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory=".")
        create_marker(m, tmp_path)
        assert marker_exists(m, tmp_path) is True

    def test_marker_exists_false(self, tmp_path):
        m = MarkerSpec(name="nonexistent.marker", type=MarkerType.FILE, directory=".")
        assert marker_exists(m, tmp_path) is False

    def test_json_marker_exists_true(self, tmp_path):
        m = MarkerSpec(name="task.json", type=MarkerType.JSON, directory=".", content={})
        create_marker(m, tmp_path)
        assert marker_exists(m, tmp_path) is True

    def test_json_marker_exists_false(self, tmp_path):
        m = MarkerSpec(name="nonexistent.json", type=MarkerType.JSON, directory=".")
        assert marker_exists(m, tmp_path) is False


class TestDeleteMarker:
    """Tests for delete_marker function."""

    def test_delete_file_marker(self, tmp_path):
        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory=".")
        create_marker(m, tmp_path)
        delete_marker(m, tmp_path)
        assert not marker_exists(m, tmp_path)

    def test_delete_json_marker(self, tmp_path):
        m = MarkerSpec(name="task.json", type=MarkerType.JSON, directory=".", content={})
        create_marker(m, tmp_path)
        delete_marker(m, tmp_path)
        assert not marker_exists(m, tmp_path)

    def test_delete_nonexistent_marker_is_noop(self, tmp_path):
        m = MarkerSpec(name="nonexistent.marker", type=MarkerType.FILE, directory=".")
        delete_marker(m, tmp_path)  # Should not raise

    def test_delete_symlink_marker(self, tmp_path):
        target = tmp_path / "report.md"
        target.write_text("# Report")
        m = MarkerSpec(name="latest.md", type=MarkerType.SYMLINK, directory="reports", target="report.md")
        create_marker(m, tmp_path)
        delete_marker(m, tmp_path)
        assert not marker_exists(m, tmp_path)


class TestMarkerAge:
    """Tests for marker age / staleness checking."""

    def test_marker_age_seconds(self, tmp_path):
        m = MarkerSpec(name="task.json", type=MarkerType.JSON, directory=".", content={})
        create_marker(m, tmp_path)
        time.sleep(0.1)
        from cronpypeline.markers import marker_age_seconds
        age = marker_age_seconds(m, tmp_path)
        assert age is not None
        assert age >= 0.1

    def test_marker_age_seconds_nonexistent(self, tmp_path):
        m = MarkerSpec(name="nonexistent.marker", type=MarkerType.FILE, directory=".")
        from cronpypeline.markers import marker_age_seconds
        age = marker_age_seconds(m, tmp_path)
        assert age is None


class TestDynamicMarkerNaming:
    """Tests for template substitution in marker names/directories/targets."""

    def test_resolve_path_with_context(self, tmp_path):
        m = MarkerSpec(name="{target}_done.marker", type=MarkerType.FILE, directory=".")
        resolved = m.resolve_path(tmp_path, context={"target": "my-repo"})
        assert resolved == tmp_path / "my-repo_done.marker"

    def test_resolve_path_without_context_no_substitution(self, tmp_path):
        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory=".")
        resolved = m.resolve_path(tmp_path)
        assert resolved == tmp_path / "done.marker"

    def test_resolve_path_with_target_config(self, tmp_path):
        m = MarkerSpec(name="{slug}.marker", type=MarkerType.FILE, directory=".")
        # target_config keys are flattened into the context by the pipeline
        resolved = m.resolve_path(tmp_path, context={"target": "repo1", "slug": "my-slug"})
        assert resolved == tmp_path / "my-slug.marker"

    def test_create_marker_with_dynamic_name(self, tmp_path):
        m = MarkerSpec(name="{target}_done.marker", type=MarkerType.FILE, directory=".")
        create_marker(m, tmp_path, context={"target": "my-repo"})
        assert (tmp_path / "my-repo_done.marker").exists()

    def test_create_marker_with_dynamic_directory(self, tmp_path):
        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory="reports/{target}")
        create_marker(m, tmp_path, context={"target": "repo1"})
        assert (tmp_path / "reports" / "repo1" / "done.marker").exists()

    def test_create_symlink_with_dynamic_target(self, tmp_path):
        target_file = tmp_path / "20240101_report.md"
        target_file.write_text("# Report")
        m = MarkerSpec(
            name="latest.md",
            type=MarkerType.SYMLINK,
            directory="reports",
            target="{target}_report.md",
        )
        create_marker(m, tmp_path, context={"target": "20240101"})
        link_path = tmp_path / "reports" / "latest.md"
        assert link_path.is_symlink()
        assert os.readlink(link_path) == "20240101_report.md"

    def test_read_marker_with_dynamic_name(self, tmp_path):
        m = MarkerSpec(name="{target}_task.json", type=MarkerType.JSON, directory=".", content={"x": 1})
        create_marker(m, tmp_path, context={"target": "repo1"})
        result = read_marker(m, tmp_path, context={"target": "repo1"})
        assert result is not None
        assert result["x"] == 1

    def test_marker_exists_with_dynamic_name(self, tmp_path):
        m = MarkerSpec(name="{target}_done.marker", type=MarkerType.FILE, directory=".")
        create_marker(m, tmp_path, context={"target": "repo1"})
        assert marker_exists(m, tmp_path, context={"target": "repo1"}) is True
        assert marker_exists(m, tmp_path, context={"target": "repo2"}) is False

    def test_delete_marker_with_dynamic_name(self, tmp_path):
        m = MarkerSpec(name="{target}_done.marker", type=MarkerType.FILE, directory=".")
        create_marker(m, tmp_path, context={"target": "repo1"})
        assert (tmp_path / "repo1_done.marker").exists()
        delete_marker(m, tmp_path, context={"target": "repo1"})
        assert not (tmp_path / "repo1_done.marker").exists()


class TestMarkerSpecResolveTarget:
    """Tests for MarkerSpec.resolve_target edge cases."""

    def test_resolve_target_returns_none_when_no_target(self):
        """resolve_target should return None when target is not set."""
        m = MarkerSpec(name="test.marker", type=MarkerType.SYMLINK, directory=".")
        assert m.target is None
        assert m.resolve_target() is None
        assert m.resolve_target({"key": "val"}) is None


class TestCreateMarkerEdgeCases:
    """Tests for create_marker error paths."""

    def test_symlink_marker_no_target_raises_value_error(self, tmp_path):
        """Symlink marker with no target should raise ValueError."""
        import pytest
        m = MarkerSpec(name="link.marker", type=MarkerType.SYMLINK, directory=".")
        with pytest.raises(ValueError, match="no target"):
            create_marker(m, tmp_path)

    def test_unknown_marker_type_raises_value_error(self, tmp_path):
        """Unknown marker type should raise ValueError."""
        import pytest
        m = MarkerSpec(name="unknown.marker", type=MarkerType.FILE, directory=".")
        m.type = "unknown_type"
        with pytest.raises(ValueError, match="Unknown marker type"):
            create_marker(m, tmp_path)


class TestReadMarkerEdgeCases:
    """Tests for read_marker edge cases."""

    def test_read_json_marker_decode_error_returns_none(self, tmp_path):
        """read_marker on invalid JSON should return None."""
        (tmp_path / "bad.json").write_text("{invalid json")
        m = MarkerSpec(name="bad.json", type=MarkerType.JSON, directory=".")
        assert read_marker(m, tmp_path) is None

    def test_read_marker_unknown_type_returns_none(self, tmp_path):
        """read_marker for unknown type should return None."""
        (tmp_path / "test.marker").touch()
        m = MarkerSpec(name="test.marker", type=MarkerType.FILE, directory=".")
        m.type = "unknown_type"
        assert read_marker(m, tmp_path) is None


class TestMarkerAgeSecondsEdgeCases:
    """Tests for marker_age_seconds edge cases."""

    def test_marker_age_os_error_returns_none(self, tmp_path):
        """marker_age_seconds should return None on OSError."""
        from unittest.mock import patch
        m = MarkerSpec(name="test.marker", type=MarkerType.FILE, directory=".")
        create_marker(m, tmp_path)

        with patch("pathlib.Path.stat", side_effect=OSError("permission denied")), \
             patch("pathlib.Path.exists", return_value=True):
            assert marker_age_seconds(m, tmp_path) is None


class TestFormatTemplate:
    """Tests for _format_template."""

    def test_no_braces_returns_as_is(self):
        from cronpypeline.markers import _format_template
        assert _format_template("no placeholders", {"a": 1}) == "no placeholders"

    def test_empty_context_returns_as_is(self):
        from cronpypeline.markers import _format_template
        assert _format_template("no braces", {}) == "no braces"

    def test_valid_substitution(self):
        from cronpypeline.markers import _format_template
        assert _format_template("hello {name}", {"name": "world"}) == "hello world"

    def test_key_error_returns_template(self):
        from cronpypeline.markers import _format_template
        assert _format_template("hello {missing}", {"name": "world"}) == "hello {missing}"

    def test_index_error_returns_template(self):
        from cronpypeline.markers import _format_template
        assert _format_template("item {0}", {}) == "item {0}"

    def test_value_error_returns_template(self):
        from cronpypeline.markers import _format_template
        # Invalid format spec causes ValueError
        assert _format_template("{:bad}", {}) == "{:bad}"


class TestPathTraversalProtection:
    """Tests for path traversal protection in MarkerSpec.resolve_path."""

    def test_resolve_path_rejects_dotdot_in_directory(self, tmp_path):
        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory="../../etc")
        with pytest.raises(ValueError, match="contains '\\.\\.'"):
            m.resolve_path(tmp_path)

    def test_resolve_path_rejects_dotdot_in_name(self, tmp_path):
        m = MarkerSpec(name="../done.marker", type=MarkerType.FILE, directory=".")
        with pytest.raises(ValueError, match="contains '\\.\\.'"):
            m.resolve_path(tmp_path)

    def test_resolve_path_accepts_normal_paths(self, tmp_path):
        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory="reports")
        resolved = m.resolve_path(tmp_path)
        assert resolved == tmp_path / "reports" / "done.marker"

    def test_resolve_path_accepts_dot_directory(self, tmp_path):
        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory=".")
        resolved = m.resolve_path(tmp_path)
        assert resolved == tmp_path / "done.marker"

    def test_create_marker_rejects_dotdot_directory(self, tmp_path):
        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory="../../etc")
        with pytest.raises(ValueError, match="contains '\\.\\.'"):
            create_marker(m, tmp_path)
        assert not (tmp_path.parent.parent / "etc" / "done.marker").exists()
        assert list(tmp_path.iterdir()) == []

    def test_read_marker_rejects_dotdot_directory(self, tmp_path):
        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory="../../etc")
        with pytest.raises(ValueError, match="contains '\\.\\.'"):
            read_marker(m, tmp_path)

    def test_delete_marker_rejects_dotdot_directory(self, tmp_path):
        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory="../../etc")
        with pytest.raises(ValueError, match="contains '\\.\\.'"):
            delete_marker(m, tmp_path)

    def test_marker_exists_rejects_dotdot_directory(self, tmp_path):
        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory="../../etc")
        with pytest.raises(ValueError, match="contains '\\.\\.'"):
            marker_exists(m, tmp_path)

    def test_resolve_path_rejects_absolute_directory(self, tmp_path):
        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory="/etc")
        with pytest.raises(ValueError, match="must be relative"):
            m.resolve_path(tmp_path)

    def test_resolve_path_rejects_absolute_name(self, tmp_path):
        m = MarkerSpec(name="/etc/passwd", type=MarkerType.FILE, directory=".")
        with pytest.raises(ValueError, match="must be relative"):
            m.resolve_path(tmp_path)

    def test_create_marker_rejects_absolute_directory(self, tmp_path):
        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory="/etc")
        with pytest.raises(ValueError, match="must be relative"):
            create_marker(m, tmp_path)
        assert not Path("/etc/done.marker").exists()
        assert list(tmp_path.iterdir()) == []

    def test_read_marker_rejects_absolute_path(self, tmp_path):
        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory="/etc")
        with pytest.raises(ValueError, match="must be relative"):
            read_marker(m, tmp_path)

    def test_delete_marker_rejects_absolute_path(self, tmp_path):
        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory="/etc")
        with pytest.raises(ValueError, match="must be relative"):
            delete_marker(m, tmp_path)

    def test_marker_exists_rejects_absolute_path(self, tmp_path):
        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory="/etc")
        with pytest.raises(ValueError, match="must be relative"):
            marker_exists(m, tmp_path)

    def test_resolve_path_rejects_symlink_escape(self, tmp_path):
        outside_dir = tmp_path.parent / "outside_dir"
        outside_dir.mkdir()
        (tmp_path / "link").symlink_to(outside_dir, target_is_directory=True)
        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory="link")
        with pytest.raises(ValueError, match="escapes base directory"):
            m.resolve_path(tmp_path)

    def test_resolve_path_rejects_symlinked_name_escape(self, tmp_path):
        outside_file = tmp_path.parent / "outside_file.txt"
        outside_file.write_text("secret")
        (tmp_path / "reports").mkdir()
        (tmp_path / "reports" / "done.marker").symlink_to(outside_file)
        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory="reports")
        with pytest.raises(ValueError, match="escapes base directory"):
            m.resolve_path(tmp_path)

    def test_resolve_path_returns_resolved_path_for_symlinked_base(self, tmp_path):
        link_dir = tmp_path / "base_link"
        link_dir.symlink_to(tmp_path, target_is_directory=True)
        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory="reports")
        resolved = m.resolve_path(link_dir)
        assert resolved == (tmp_path / "reports" / "done.marker").resolve()


class TestSymlinkTOCTOUFix:
    """Tests for TOCTOU fix in symlink marker creation."""

    def test_resolve_path_returns_full_resolved_for_file_type(self, tmp_path):
        """resolve_path returns fully resolved path for non-symlink types."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link_dir = tmp_path / "base_link"
        link_dir.symlink_to(real_dir, target_is_directory=True)
        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory="reports")
        resolved = m.resolve_path(link_dir)
        # Should point to the real directory, not through the symlink
        assert resolved == (real_dir / "reports" / "done.marker").resolve()
        assert str(link_dir) not in str(resolved)

    def test_resolve_path_returns_symlink_path_for_symlink_type(self, tmp_path):
        """resolve_path returns the symlink path (not target) for SYMLINK type."""
        target_file = tmp_path / "report.md"
        target_file.write_text("# Report")
        m = MarkerSpec(name="latest.md", type=MarkerType.SYMLINK, directory="reports", target="report.md")
        create_marker(m, tmp_path)
        # Now resolve_path should return the symlink path itself, not the target
        resolved = m.resolve_path(tmp_path)
        # For SYMLINK type, resolve_path returns the path to the symlink itself
        # (not following the final symlink to its target).
        assert resolved == (tmp_path / "reports" / "latest.md")
        assert resolved.is_symlink()

    def test_create_symlink_replaces_regular_file(self, tmp_path):
        """create_marker SYMLINK should atomically replace an existing regular file."""
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        # Create a regular file at the marker path
        regular_file = reports_dir / "latest.md"
        regular_file.write_text("old content")
        assert not regular_file.is_symlink()

        m = MarkerSpec(name="latest.md", type=MarkerType.SYMLINK, directory="reports", target="report.md")
        create_marker(m, tmp_path)
        assert regular_file.is_symlink()
        assert os.readlink(regular_file) == "report.md"

    def test_create_symlink_replace_failure_cleans_up_temp(self, tmp_path):
        """create_marker SYMLINK should clean up temp file when os.replace fails."""
        from unittest.mock import patch

        m = MarkerSpec(name="latest.md", type=MarkerType.SYMLINK, directory="reports", target="report.md")
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()

        def failing_replace(src, dst, **kwargs):
            raise OSError("simulated replace failure")

        with patch("cronpypeline.markers.os.replace", side_effect=failing_replace), pytest.raises(OSError, match="simulated replace failure"):
            create_marker(m, tmp_path)

        # Verify no temp files are left behind
        leftovers = [f for f in reports_dir.iterdir() if ".tmp" in f.name]
        assert leftovers == []
        # Verify the marker was not created
        assert not (reports_dir / "latest.md").exists()

    def test_create_symlink_replace_failure_temp_cleanup_not_found(self, tmp_path):
        """create_marker SYMLINK should handle FileNotFoundError during temp cleanup."""
        from unittest.mock import patch

        m = MarkerSpec(name="latest.md", type=MarkerType.SYMLINK, directory="reports", target="report.md")
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()

        real_unlink = os.unlink
        unlink_calls = []

        def mock_unlink(name, **kwargs):
            unlink_calls.append(name)
            # First call (cleanup of pre-existing temp) succeeds.
            # The cleanup call after os.replace failure should raise FileNotFoundError.
            if len(unlink_calls) > 1:
                raise FileNotFoundError("temp already gone")
            return real_unlink(name, **kwargs)

        def failing_replace(src, dst, **kwargs):
            raise OSError("simulated replace failure")

        with patch("cronpypeline.markers.os.unlink", side_effect=mock_unlink),              patch("cronpypeline.markers.os.replace", side_effect=failing_replace), pytest.raises(OSError, match="simulated replace failure"):
            create_marker(m, tmp_path)

        # Verify the exception propagated
        assert len(unlink_calls) >= 2


class TestMarkerSymlinkHandling:
    """Tests for correct handling of symlinks at FILE/JSON marker paths."""

    def test_delete_file_marker_symlink_removes_only_symlink(self, tmp_path):
        """delete_marker on a FILE marker whose path is a symlink removes the
        symlink entry, leaving the target file intact."""
        target_file = tmp_path / "target_file.txt"
        target_file.write_text("original content")
        marker_path = tmp_path / "done.marker"
        marker_path.symlink_to(target_file)

        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory=".")
        delete_marker(m, tmp_path)

        assert not marker_path.exists()
        assert not marker_path.is_symlink()
        assert target_file.exists()
        assert target_file.read_text() == "original content"

    def test_delete_json_marker_symlink_removes_only_symlink(self, tmp_path):
        """delete_marker on a JSON marker whose path is a symlink removes the
        symlink entry, leaving the target file intact."""
        target_file = tmp_path / "target_data.json"
        target_file.write_text('{"key": "value"}')
        marker_path = tmp_path / "task.json"
        marker_path.symlink_to(target_file)

        m = MarkerSpec(name="task.json", type=MarkerType.JSON, directory=".")
        delete_marker(m, tmp_path)

        assert not marker_path.exists()
        assert not marker_path.is_symlink()
        assert target_file.exists()
        assert target_file.read_text() == '{"key": "value"}'

    def test_read_file_marker_symlink_reports_exists(self, tmp_path):
        """read_marker on a FILE marker whose path is a symlink reports exists."""
        target_file = tmp_path / "target_file.txt"
        target_file.write_text("content")
        marker_path = tmp_path / "done.marker"
        marker_path.symlink_to(target_file)

        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory=".")
        result = read_marker(m, tmp_path)

        assert result is not None
        assert result["exists"] is True

    def test_marker_exists_file_marker_symlink_returns_true(self, tmp_path):
        """marker_exists on a FILE marker whose path is a symlink returns True."""
        target_file = tmp_path / "target_file.txt"
        target_file.write_text("content")
        marker_path = tmp_path / "done.marker"
        marker_path.symlink_to(target_file)

        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory=".")
        assert marker_exists(m, tmp_path) is True

    def test_create_file_marker_replaces_symlink(self, tmp_path):
        """create_marker FILE type replaces a symlink at the marker path with a
        regular file instead of following the symlink."""
        target_file = tmp_path / "target_file.txt"
        target_file.write_text("should not be modified")
        marker_path = tmp_path / "done.marker"
        marker_path.symlink_to(target_file)

        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory=".")
        create_marker(m, tmp_path)

        assert marker_path.exists()
        assert not marker_path.is_symlink()
        assert target_file.read_text() == "should not be modified"

    def test_create_json_marker_replaces_symlink(self, tmp_path):
        """create_marker JSON type replaces a symlink at the marker path with a
        regular file instead of following the symlink."""
        target_file = tmp_path / "target_data.json"
        target_file.write_text('{"original": true}')
        marker_path = tmp_path / "task.json"
        marker_path.symlink_to(target_file)

        m = MarkerSpec(name="task.json", type=MarkerType.JSON, directory=".", content={"issue_id": "42"})
        create_marker(m, tmp_path)

        assert marker_path.exists()
        assert not marker_path.is_symlink()
        assert target_file.read_text() == '{"original": true}'
        data = json.loads(marker_path.read_text())
        assert data["issue_id"] == "42"

    def test_resolve_path_returns_symlink_path_for_broken_symlink(self, tmp_path):
        """resolve_path returns the symlink path (not the resolved target) for a
        broken symlink pointing outside the base directory."""
        outside_target = tmp_path.parent / "nonexistent_target.txt"
        marker_path = tmp_path / "done.marker"
        marker_path.symlink_to(outside_target)
        assert marker_path.is_symlink()
        assert not marker_path.exists()

        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory=".")
        resolved = m.resolve_path(tmp_path)

        assert resolved == marker_path
        assert resolved.is_symlink()
        assert str(outside_target) not in str(resolved)

    def test_marker_exists_file_marker_broken_symlink_returns_true(self, tmp_path):
        """marker_exists returns True for a FILE marker at a broken symlink path
        (symlink pointing outside the base directory)."""
        outside_target = tmp_path.parent / "nonexistent_target.txt"
        marker_path = tmp_path / "done.marker"
        marker_path.symlink_to(outside_target)

        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory=".")
        assert marker_exists(m, tmp_path) is True

    def test_delete_file_marker_broken_symlink_removes_symlink(self, tmp_path):
        """delete_marker removes a broken symlink at a FILE marker path (symlink
        pointing outside the base directory)."""
        outside_target = tmp_path.parent / "nonexistent_target.txt"
        marker_path = tmp_path / "done.marker"
        marker_path.symlink_to(outside_target)
        assert marker_path.is_symlink()

        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory=".")
        delete_marker(m, tmp_path)

        assert not marker_path.is_symlink()
        assert not marker_path.exists()

    def test_read_file_marker_broken_symlink_reports_exists(self, tmp_path):
        """read_marker returns {'exists': True} for a FILE marker at a broken
        symlink path (symlink pointing outside the base directory)."""
        outside_target = tmp_path.parent / "nonexistent_target.txt"
        marker_path = tmp_path / "done.marker"
        marker_path.symlink_to(outside_target)

        m = MarkerSpec(name="done.marker", type=MarkerType.FILE, directory=".")
        result = read_marker(m, tmp_path)

        assert result is not None
        assert result["exists"] is True

    def test_marker_exists_json_marker_broken_symlink_returns_true(self, tmp_path):
        """marker_exists returns True for a JSON marker at a broken symlink path
        (symlink pointing outside the base directory)."""
        outside_target = tmp_path.parent / "nonexistent_target.json"
        marker_path = tmp_path / "task.json"
        marker_path.symlink_to(outside_target)

        m = MarkerSpec(name="task.json", type=MarkerType.JSON, directory=".")
        assert marker_exists(m, tmp_path) is True

    def test_delete_json_marker_broken_symlink_removes_symlink(self, tmp_path):
        """delete_marker removes a broken symlink at a JSON marker path (symlink
        pointing outside the base directory)."""
        outside_target = tmp_path.parent / "nonexistent_target.json"
        marker_path = tmp_path / "task.json"
        marker_path.symlink_to(outside_target)
        assert marker_path.is_symlink()

        m = MarkerSpec(name="task.json", type=MarkerType.JSON, directory=".")
        delete_marker(m, tmp_path)

        assert not marker_path.is_symlink()
        assert not marker_path.exists()

    def test_read_json_marker_broken_symlink_does_not_raise(self, tmp_path):
        """read_marker for a JSON marker at a broken symlink path (symlink
        pointing outside the base directory) should not raise an error."""
        outside_target = tmp_path.parent / "nonexistent_target.json"
        marker_path = tmp_path / "task.json"
        marker_path.symlink_to(outside_target)

        m = MarkerSpec(name="task.json", type=MarkerType.JSON, directory=".")
        result = read_marker(m, tmp_path)

        assert result is None
