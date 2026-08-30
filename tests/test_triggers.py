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

    def test_dotdot_queue_dir_raises_value_error(self, tmp_path):
        trigger = TriggerCondition(type=TriggerType.QUEUE_EMPTY, queue_dir="../outside")
        with pytest.raises(ValueError, match="contains '\\.\\.'"):
            evaluate_trigger(trigger, tmp_path)

    def test_absolute_queue_dir_outside_base_raises_value_error(self, tmp_path):
        trigger = TriggerCondition(type=TriggerType.QUEUE_EMPTY, queue_dir="/etc")
        with pytest.raises(ValueError, match="escapes base directory"):
            evaluate_trigger(trigger, tmp_path)

    def test_symlink_escape_queue_dir_raises_value_error(self, tmp_path, tmp_path_factory):
        outside = tmp_path_factory.mktemp("outside")
        (outside / "secret.txt").touch()
        link = tmp_path / "link"
        link.symlink_to(outside, target_is_directory=True)
        trigger = TriggerCondition(type=TriggerType.QUEUE_EMPTY, queue_dir=str(tmp_path / "link" / "queue"))
        with pytest.raises(ValueError, match="escapes base directory"):
            evaluate_trigger(trigger, tmp_path)

    def test_relative_queue_dir_resolves(self, tmp_path):
        (tmp_path / "queue").mkdir()
        trigger = TriggerCondition(type=TriggerType.QUEUE_EMPTY, queue_dir="queue")
        assert evaluate_trigger(trigger, tmp_path) is True

    def test_relative_queue_dir_does_not_fire_when_has_files(self, tmp_path):
        queue_dir = tmp_path / "queue"
        queue_dir.mkdir()
        (queue_dir / "task1.json").write_text("{}")
        trigger = TriggerCondition(type=TriggerType.QUEUE_EMPTY, queue_dir="queue")
        assert evaluate_trigger(trigger, tmp_path) is False


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


class TestResolveCustomCallableErrors:
    """Tests for resolve_custom_callable error paths."""

    def test_resolve_no_dot_raises_value_error(self):
        """Path without a dot should raise ValueError."""
        with pytest.raises(ValueError, match="Invalid callable path"):
            resolve_custom_callable("nomoduleseparator")


class TestMarkerStateEdgeCases:
    """Tests for marker_state trigger edge cases."""

    def test_json_decode_error_returns_false(self, tmp_path):
        """Invalid JSON in marker file should return False."""
        (tmp_path / "bad.json").write_text("{invalid json")
        trigger = TriggerCondition(
            type=TriggerType.MARKER_STATE,
            path="bad.json",
            field="status",
            op="eq",
            value="open",
        )
        assert evaluate_trigger(trigger, tmp_path) is False

    def test_os_error_returns_false(self, tmp_path):
        """OSError reading marker file should return False."""
        import json
        data = {"status": "open"}
        (tmp_path / "task.json").write_text(json.dumps(data))
        trigger = TriggerCondition(
            type=TriggerType.MARKER_STATE,
            path="task.json",
            field="status",
            op="eq",
            value="open",
        )
        # Make file unreadable
        import os
        os.chmod(tmp_path / "task.json", 0o000)
        try:
            assert evaluate_trigger(trigger, tmp_path) is False
        finally:
            os.chmod(tmp_path / "task.json", 0o644)

    def test_ne_operator(self, tmp_path):
        """ne operator should return True when field doesn't match."""
        (tmp_path / "task.json").write_text(json.dumps({"status": "closed"}))
        trigger = TriggerCondition(
            type=TriggerType.MARKER_STATE,
            path="task.json",
            field="status",
            op="ne",
            value="open",
        )
        assert evaluate_trigger(trigger, tmp_path) is True

    def test_gt_operator(self, tmp_path):
        """gt operator should return True when field is greater than value."""
        (tmp_path / "task.json").write_text(json.dumps({"count": 10}))
        trigger = TriggerCondition(
            type=TriggerType.MARKER_STATE,
            path="task.json",
            field="count",
            op="gt",
            value=5,
        )
        assert evaluate_trigger(trigger, tmp_path) is True

    def test_lte_operator(self, tmp_path):
        """lte operator should return True when field is less than or equal."""
        (tmp_path / "task.json").write_text(json.dumps({"count": 5}))
        trigger = TriggerCondition(
            type=TriggerType.MARKER_STATE,
            path="task.json",
            field="count",
            op="lte",
            value=5,
        )
        assert evaluate_trigger(trigger, tmp_path) is True

    def test_unknown_operator_raises_value_error(self, tmp_path):
        """Unknown operator should raise ValueError."""
        (tmp_path / "task.json").write_text(json.dumps({"status": "open"}))
        trigger = TriggerCondition(
            type=TriggerType.MARKER_STATE,
            path="task.json",
            field="status",
            op="contains",
            value="open",
        )
        with pytest.raises(ValueError, match="Unknown operator"):
            evaluate_trigger(trigger, tmp_path)


class TestEvaluateTriggerUnknownType:
    """Tests for evaluate_trigger with unknown trigger type."""

    def test_unknown_trigger_type_raises_value_error(self, tmp_path):
        """Unknown trigger type should raise ValueError."""
        # Create a trigger with an invalid type by bypassing the enum
        trigger = TriggerCondition(type=TriggerType.FILE_MISSING, path="test.md")
        # Monkey-patch the type to an unknown value
        trigger.type = "unknown_type"
        with pytest.raises(ValueError, match="No evaluator"):
            evaluate_trigger(trigger, tmp_path)


_PATH_TRIGGER_TYPES = [
    (TriggerType.FILE_MISSING, {}),
    (TriggerType.FILE_EXISTS, {}),
    (TriggerType.FILE_OLDER_THAN, {"minutes": 30}),
    (TriggerType.MARKER_STATE, {"field": "status", "op": "eq", "value": "open"}),
]


class TestTriggerPathValidation:
    """Path traversal validation for file-based trigger conditions."""

    @pytest.mark.parametrize("trigger_type, kwargs", _PATH_TRIGGER_TYPES)
    def test_dotdot_path_raises_value_error(self, tmp_path, trigger_type, kwargs):
        """A path containing '..' should raise ValueError for every evaluator."""
        trigger = TriggerCondition(type=trigger_type, path="../outside.txt", **kwargs)
        with pytest.raises(ValueError, match="contains '\\.\\.' or is absolute"):
            evaluate_trigger(trigger, tmp_path)

    @pytest.mark.parametrize("trigger_type, kwargs", _PATH_TRIGGER_TYPES)
    def test_absolute_path_raises_value_error(self, tmp_path, trigger_type, kwargs):
        """An absolute path should raise ValueError for every evaluator."""
        trigger = TriggerCondition(type=trigger_type, path="/etc/passwd", **kwargs)
        with pytest.raises(ValueError, match="contains '\\.\\.' or is absolute"):
            evaluate_trigger(trigger, tmp_path)

    @pytest.mark.parametrize("trigger_type, kwargs", _PATH_TRIGGER_TYPES)
    def test_symlink_escape_raises_value_error(
        self, tmp_path, tmp_path_factory, trigger_type, kwargs
    ):
        """A path escaping the base dir via symlink should raise ValueError."""
        outside = tmp_path_factory.mktemp("outside")
        (outside / "secret.txt").touch()
        link = tmp_path / "link"
        link.symlink_to(outside, target_is_directory=True)
        trigger = TriggerCondition(type=trigger_type, path="link/secret.txt", **kwargs)
        with pytest.raises(ValueError, match="escapes base directory"):
            evaluate_trigger(trigger, tmp_path)

    @pytest.mark.parametrize("trigger_type, kwargs", _PATH_TRIGGER_TYPES)
    def test_valid_relative_path_resolves(self, tmp_path, trigger_type, kwargs):
        """A normal relative path should resolve without error."""
        (tmp_path / "ok.txt").touch()
        trigger = TriggerCondition(type=trigger_type, path="ok.txt", **kwargs)
        # Valid paths must not raise; the boolean result is asserted by the
        # individual evaluator tests above.
        assert evaluate_trigger(trigger, tmp_path) in (True, False)
