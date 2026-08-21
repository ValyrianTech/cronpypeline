"""Tests for cronpypeline.triggers — built-in trigger condition evaluators."""

import json
import os
import time

import pytest

from cronpypeline.config import TriggerCondition, TriggerType
from cronpypeline.triggers import evaluate_trigger, resolve_custom_callable


class TestFileMissingTrigger:
    """file_missing: fire if a file doesn't exist."""

    def test_fires_when_file_missing(self, tmp_path):
        trigger = TriggerCondition(type=TriggerType.FILE_MISSING, path="missing.md")
        assert evaluate_trigger(trigger, tmp_path) is True

    def test_does_not_fire_when_file_exists(self, tmp_path):
        (tmp_path / "exists.md").touch()
        trigger = TriggerCondition(type=TriggerType.FILE_MISSING, path="exists.md")
        assert evaluate_trigger(trigger, tmp_path) is False

    def test_works_with_nested_paths(self, tmp_path):
        trigger = TriggerCondition(type=TriggerType.FILE_MISSING, path=".SWE/reports/test-infra/latest.md")
        assert evaluate_trigger(trigger, tmp_path) is True
        # Create the nested file
        nested = tmp_path / ".SWE" / "reports" / "test-infra" / "latest.md"
        nested.parent.mkdir(parents=True)
        nested.touch()
        assert evaluate_trigger(trigger, tmp_path) is False


class TestFileExistsTrigger:
    """file_exists: fire if a file exists."""

    def test_fires_when_file_exists(self, tmp_path):
        (tmp_path / "marker.txt").touch()
        trigger = TriggerCondition(type=TriggerType.FILE_EXISTS, path="marker.txt")
        assert evaluate_trigger(trigger, tmp_path) is True

    def test_does_not_fire_when_file_missing(self, tmp_path):
        trigger = TriggerCondition(type=TriggerType.FILE_EXISTS, path="nonexistent.txt")
        assert evaluate_trigger(trigger, tmp_path) is False


class TestFileOlderThanTrigger:
    """file_older_than: fire if file is older than N minutes."""

    def test_fires_when_file_is_old(self, tmp_path):
        marker = tmp_path / "old.marker"
        marker.touch()
        # Set mtime to 60 minutes ago
        old_time = time.time() - 3600
        os.utime(marker, (old_time, old_time))
        trigger = TriggerCondition(type=TriggerType.FILE_OLDER_THAN, path="old.marker", minutes=30)
        assert evaluate_trigger(trigger, tmp_path) is True

    def test_does_not_fire_when_file_is_new(self, tmp_path):
        marker = tmp_path / "new.marker"
        marker.touch()
        trigger = TriggerCondition(type=TriggerType.FILE_OLDER_THAN, path="new.marker", minutes=30)
        assert evaluate_trigger(trigger, tmp_path) is False

    def test_does_not_fire_when_file_missing(self, tmp_path):
        trigger = TriggerCondition(type=TriggerType.FILE_OLDER_THAN, path="nonexistent.marker", minutes=30)
        assert evaluate_trigger(trigger, tmp_path) is False

    def test_fires_at_boundary(self, tmp_path):
        marker = tmp_path / "boundary.marker"
        marker.touch()
        # Set mtime to exactly 30 minutes ago
        boundary_time = time.time() - 30 * 60
        os.utime(marker, (boundary_time, boundary_time))
        trigger = TriggerCondition(type=TriggerType.FILE_OLDER_THAN, path="boundary.marker", minutes=30)
        assert evaluate_trigger(trigger, tmp_path) is True


class TestMarkerStateTrigger:
    """marker_state: fire based on marker JSON field value."""

    def test_fires_when_field_matches_lt(self, tmp_path):
        data = {"retry_count": 1}
        (tmp_path / "task.json").write_text(json.dumps(data))
        trigger = TriggerCondition(
            type=TriggerType.MARKER_STATE,
            path="task.json",
            field="retry_count",
            op="lt",
            value=3,
        )
        assert evaluate_trigger(trigger, tmp_path) is True

    def test_does_not_fire_when_field_does_not_match_lt(self, tmp_path):
        data = {"retry_count": 3}
        (tmp_path / "task.json").write_text(json.dumps(data))
        trigger = TriggerCondition(
            type=TriggerType.MARKER_STATE,
            path="task.json",
            field="retry_count",
            op="lt",
            value=3,
        )
        assert evaluate_trigger(trigger, tmp_path) is False

    def test_fires_when_field_matches_eq(self, tmp_path):
        data = {"status": "open"}
        (tmp_path / "task.json").write_text(json.dumps(data))
        trigger = TriggerCondition(
            type=TriggerType.MARKER_STATE,
            path="task.json",
            field="status",
            op="eq",
            value="open",
        )
        assert evaluate_trigger(trigger, tmp_path) is True

    def test_fires_when_field_matches_gte(self, tmp_path):
        data = {"attempts": 5}
        (tmp_path / "task.json").write_text(json.dumps(data))
        trigger = TriggerCondition(
            type=TriggerType.MARKER_STATE,
            path="task.json",
            field="attempts",
            op="gte",
            value=5,
        )
        assert evaluate_trigger(trigger, tmp_path) is True

    def test_does_not_fire_when_file_missing(self, tmp_path):
        trigger = TriggerCondition(
            type=TriggerType.MARKER_STATE,
            path="nonexistent.json",
            field="retry_count",
            op="lt",
            value=3,
        )
        assert evaluate_trigger(trigger, tmp_path) is False

    def test_fires_when_field_missing_in_json(self, tmp_path):
        data = {"other_field": "value"}
        (tmp_path / "task.json").write_text(json.dumps(data))
        trigger = TriggerCondition(
            type=TriggerType.MARKER_STATE,
            path="task.json",
            field="retry_count",
            op="lt",
            value=3,
        )
        # Missing field should be treated as 0/default for numeric comparisons
        assert evaluate_trigger(trigger, tmp_path) is True


class TestQueueEmptyTrigger:
    """queue_empty: fire if action queue is empty."""

    def test_fires_when_queue_dir_empty(self, tmp_path):
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        trigger = TriggerCondition(type=TriggerType.QUEUE_EMPTY, queue_dir=str(queue_dir))
        assert evaluate_trigger(trigger, tmp_path) is True

    def test_does_not_fire_when_queue_has_files(self, tmp_path):
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        (queue_dir / "task1.json").write_text("{}")
        trigger = TriggerCondition(type=TriggerType.QUEUE_EMPTY, queue_dir=str(queue_dir))
        assert evaluate_trigger(trigger, tmp_path) is False

    def test_fires_when_queue_dir_does_not_exist(self, tmp_path):
        trigger = TriggerCondition(type=TriggerType.QUEUE_EMPTY, queue_dir=str(tmp_path / "nonexistent_queue"))
        assert evaluate_trigger(trigger, tmp_path) is True


class TestAndCombinator:
    """and: fire if all conditions are true."""

    def test_fires_when_all_conditions_true(self, tmp_path):
        trigger = TriggerCondition(
            type=TriggerType.AND,
            conditions=[
                TriggerCondition(type=TriggerType.FILE_MISSING, path="a.md"),
                TriggerCondition(type=TriggerType.FILE_MISSING, path="b.md"),
            ],
        )
        assert evaluate_trigger(trigger, tmp_path) is True

    def test_does_not_fire_when_any_condition_false(self, tmp_path):
        (tmp_path / "a.md").touch()
        trigger = TriggerCondition(
            type=TriggerType.AND,
            conditions=[
                TriggerCondition(type=TriggerType.FILE_MISSING, path="a.md"),
                TriggerCondition(type=TriggerType.FILE_MISSING, path="b.md"),
            ],
        )
        assert evaluate_trigger(trigger, tmp_path) is False

    def test_empty_and_is_true(self, tmp_path):
        trigger = TriggerCondition(type=TriggerType.AND, conditions=[])
        assert evaluate_trigger(trigger, tmp_path) is True


class TestOrCombinator:
    """or: fire if any condition is true."""

    def test_fires_when_any_condition_true(self, tmp_path):
        (tmp_path / "a.md").touch()
        trigger = TriggerCondition(
            type=TriggerType.OR,
            conditions=[
                TriggerCondition(type=TriggerType.FILE_MISSING, path="a.md"),
                TriggerCondition(type=TriggerType.FILE_MISSING, path="b.md"),
            ],
        )
        assert evaluate_trigger(trigger, tmp_path) is True

    def test_does_not_fire_when_all_conditions_false(self, tmp_path):
        (tmp_path / "a.md").touch()
        (tmp_path / "b.md").touch()
        trigger = TriggerCondition(
            type=TriggerType.OR,
            conditions=[
                TriggerCondition(type=TriggerType.FILE_MISSING, path="a.md"),
                TriggerCondition(type=TriggerType.FILE_MISSING, path="b.md"),
            ],
        )
        assert evaluate_trigger(trigger, tmp_path) is False

    def test_empty_or_is_false(self, tmp_path):
        trigger = TriggerCondition(type=TriggerType.OR, conditions=[])
        assert evaluate_trigger(trigger, tmp_path) is False


class TestCustomTrigger:
    """custom: user-provided Python callable."""

    def test_custom_callable_resolves_and_executes(self, tmp_path):
        # Create a test module with a custom callable
        module_code = """
def my_trigger(context):
    return True
"""
        (tmp_path / "my_trigger_module.py").write_text(module_code)
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            trigger = TriggerCondition(
                type=TriggerType.CUSTOM,
                callable="my_trigger_module.my_trigger",
            )
            assert evaluate_trigger(trigger, tmp_path, context={"test": True}) is True
        finally:
            sys.path.remove(str(tmp_path))
            if "my_trigger_module" in sys.modules:
                del sys.modules["my_trigger_module"]

    def test_custom_callable_returns_false(self, tmp_path):
        module_code = """
def my_trigger(context):
    return False
"""
        (tmp_path / "my_trigger_module2.py").write_text(module_code)
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            trigger = TriggerCondition(
                type=TriggerType.CUSTOM,
                callable="my_trigger_module2.my_trigger",
            )
            assert evaluate_trigger(trigger, tmp_path) is False
        finally:
            sys.path.remove(str(tmp_path))
            if "my_trigger_module2" in sys.modules:
                del sys.modules["my_trigger_module2"]

    def test_resolve_custom_callable_invalid_module(self):
        with pytest.raises(ImportError):
            resolve_custom_callable("nonexistent_module_xyz.func")

    def test_resolve_custom_callable_invalid_attr(self):
        with pytest.raises(AttributeError):
            resolve_custom_callable("json.nonexistent_function_xyz")

    def test_custom_receives_enriched_context(self, tmp_path):
        """Custom trigger callable should receive target, target_dir, workspace_dir, target_config."""
        module_code = """
def check_context(context):
    assert context.get("target") == "my-repo"
    assert "target_dir" in context
    assert "workspace_dir" in context
    assert context.get("target_config", {}).get("test_cmd") == "pytest"
    return True
"""
        (tmp_path / "ctx_trigger_mod.py").write_text(module_code)
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            trigger = TriggerCondition(
                type=TriggerType.CUSTOM,
                callable="ctx_trigger_mod.check_context",
            )
            ctx = {
                "target": "my-repo",
                "target_dir": str(tmp_path),
                "workspace_dir": str(tmp_path),
                "target_config": {"test_cmd": "pytest"},
            }
            assert evaluate_trigger(trigger, tmp_path, context=ctx) is True
        finally:
            sys.path.remove(str(tmp_path))
            if "ctx_trigger_mod" in sys.modules:
                del sys.modules["ctx_trigger_mod"]
