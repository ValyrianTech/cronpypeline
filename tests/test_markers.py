"""Tests for cronpypeline.markers — MarkerSpec creation/reading."""

import json
import os
import time
from pathlib import Path

import pytest

from cronpypeline.markers import MarkerSpec, MarkerType, create_marker, read_marker, marker_exists, delete_marker


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
