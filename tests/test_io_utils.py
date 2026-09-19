"""Tests for cronpypeline.io_utils — atomic JSON/text write helpers."""

import json
from unittest.mock import patch

import pytest

from cronpypeline.io_utils import write_json_atomic, write_text_atomic


class TestWriteJsonAtomic:
    """Tests for write_json_atomic."""

    def test_writes_valid_json(self, tmp_path):
        path = tmp_path / "state.json"
        write_json_atomic(path, {"a": 1, "b": [1, 2, 3]})
        assert json.loads(path.read_text()) == {"a": 1, "b": [1, 2, 3]}

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "nested" / "deep" / "state.json"
        write_json_atomic(path, {"k": "v"})
        assert path.exists()
        assert json.loads(path.read_text()) == {"k": "v"}

    def test_indent_none_is_compact(self, tmp_path):
        path = tmp_path / "compact.json"
        write_json_atomic(path, {"mode": "default"}, indent=None)
        assert path.read_text() == '{"mode": "default"}'

    def test_indent_two_is_pretty(self, tmp_path):
        path = tmp_path / "indented.json"
        write_json_atomic(path, {"a": 1}, indent=2)
        assert json.loads(path.read_text()) == {"a": 1}
        assert "\n" in path.read_text()

    def test_overwrites_existing(self, tmp_path):
        path = tmp_path / "state.json"
        write_json_atomic(path, {"version": 1})
        write_json_atomic(path, {"version": 2})
        assert json.loads(path.read_text()) == {"version": 2}

    def test_preserves_old_file_when_dump_fails(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text('{"old": true}')
        with pytest.raises(TypeError):
            write_json_atomic(path, object())
        assert path.read_text() == '{"old": true}'
        assert list(tmp_path.glob(".state.json.*.tmp")) == []

    def test_cleanup_temp_on_replace_failure(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text('{"old": true}')
        with patch("cronpypeline.io_utils.os.replace", side_effect=OSError("boom")), \
             pytest.raises(OSError, match="boom"):
            write_json_atomic(path, {"new": 1})
        assert path.read_text() == '{"old": true}'
        assert list(tmp_path.glob(".state.json.*.tmp")) == []

    def test_unlink_file_not_found_is_ignored(self, tmp_path):
        path = tmp_path / "state.json"
        with patch("cronpypeline.io_utils.os.unlink", side_effect=FileNotFoundError), \
             pytest.raises(TypeError):
            write_json_atomic(path, object())


class TestWriteTextAtomic:
    """Tests for write_text_atomic."""

    def test_writes_text_and_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "nested" / "report.md"
        write_text_atomic(path, "hello world")
        assert path.exists()
        assert path.read_text() == "hello world"

    def test_overwrites_existing(self, tmp_path):
        path = tmp_path / "report.md"
        write_text_atomic(path, "v1")
        write_text_atomic(path, "v2")
        assert path.read_text() == "v2"

    def test_cleanup_temp_on_replace_failure(self, tmp_path):
        path = tmp_path / "report.md"
        path.write_text("old")
        with patch("cronpypeline.io_utils.os.replace", side_effect=OSError("boom")), \
             pytest.raises(OSError, match="boom"):
            write_text_atomic(path, "new")
        assert path.read_text() == "old"
        assert list(tmp_path.glob(".report.md.*.tmp")) == []

    def test_unlink_file_not_found_is_ignored(self, tmp_path):
        path = tmp_path / "report.md"
        with patch("cronpypeline.io_utils.os.unlink", side_effect=FileNotFoundError), \
             patch("cronpypeline.io_utils.os.replace", side_effect=OSError("boom")), \
             pytest.raises(OSError, match="boom"):
            write_text_atomic(path, "new")
