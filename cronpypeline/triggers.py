"""Built-in trigger condition evaluators.

Each evaluator takes a TriggerCondition and a base directory (workspace/target dir)
and returns True if the stage should fire, False otherwise.
"""

import importlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

from cronpypeline.config import TriggerCondition, TriggerType


def resolve_custom_callable(callable_path: str) -> Callable[..., Any]:
    """Resolve a dotted path like 'mymodule.myfunc' to a callable.

    :param callable_path: Dotted import path to the callable.
    :returns: The resolved callable object.
    :raises ValueError: If the path does not contain a dot separator.
    :raises AttributeError: If the module has no such attribute.
    """
    parts = callable_path.rsplit(".", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid callable path: {callable_path}")
    module_path, func_name = parts
    module = importlib.import_module(module_path)
    if not hasattr(module, func_name):
        raise AttributeError(f"Module '{module_path}' has no attribute '{func_name}'")
    return getattr(module, func_name)


def _eval_file_missing(trigger: TriggerCondition, base_dir: Path) -> bool:
    """Evaluate whether a file is missing."""
    path = base_dir / (trigger.path or "")
    return not path.exists()


def _eval_file_exists(trigger: TriggerCondition, base_dir: Path) -> bool:
    """Evaluate whether a file exists."""
    path = base_dir / (trigger.path or "")
    return path.exists()


def _eval_file_older_than(trigger: TriggerCondition, base_dir: Path) -> bool:
    """Evaluate whether a file is older than the configured threshold."""
    path = base_dir / (trigger.path or "")
    if not path.exists():
        return False
    age_seconds = time.time() - path.stat().st_mtime
    return age_seconds >= (trigger.minutes or 0) * 60


def _eval_marker_state(trigger: TriggerCondition, base_dir: Path) -> bool:
    """Evaluate a JSON marker field against an expected value.

    :raises ValueError: If the operator is not one of eq, ne, lt, lte, gt, gte.
    """
    path = base_dir / (trigger.path or "")
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return False

    field_value = data.get(trigger.field, 0)
    op = trigger.op
    expected = trigger.value

    if op == "eq":
        return field_value == expected
    elif op == "ne":
        return field_value != expected
    elif op == "lt":
        return field_value < expected
    elif op == "lte":
        return field_value <= expected
    elif op == "gt":
        return field_value > expected
    elif op == "gte":
        return field_value >= expected
    else:
        raise ValueError(f"Unknown operator: {op}")


def _eval_queue_empty(trigger: TriggerCondition, base_dir: Path) -> bool:
    """Evaluate whether a queue directory is empty."""
    queue_dir = Path(trigger.queue_dir or "")
    if not queue_dir.exists():
        return True
    return not any(queue_dir.iterdir())


def _eval_and(trigger: TriggerCondition, base_dir: Path, context: Optional[dict[str, Any]] = None) -> bool:
    """Evaluate whether all sub-conditions are true."""
    return all(evaluate_trigger(c, base_dir, context) for c in trigger.conditions)


def _eval_or(trigger: TriggerCondition, base_dir: Path, context: Optional[dict[str, Any]] = None) -> bool:
    """Evaluate whether any sub-condition is true."""
    return any(evaluate_trigger(c, base_dir, context) for c in trigger.conditions)


def _eval_custom(trigger: TriggerCondition, base_dir: Path, context: Optional[dict[str, Any]] = None) -> bool:
    """Evaluate a user-provided custom callable."""
    func = resolve_custom_callable(trigger.callable or "")
    ctx = context or {}
    return bool(func(ctx))


_EVALUATORS = {
    TriggerType.FILE_MISSING: _eval_file_missing,
    TriggerType.FILE_EXISTS: _eval_file_exists,
    TriggerType.FILE_OLDER_THAN: _eval_file_older_than,
    TriggerType.MARKER_STATE: _eval_marker_state,
    TriggerType.QUEUE_EMPTY: _eval_queue_empty,
}


def evaluate_trigger(
    trigger: TriggerCondition,
    base_dir: Path,
    context: Optional[dict[str, Any]] = None,
) -> bool:
    """Evaluate a trigger condition against the filesystem state.

    :param trigger: The trigger condition to evaluate.
    :param base_dir: The workspace/target directory to check against.
    :param context: Optional context dict passed to custom callables.
    :returns: True if the stage should fire, False otherwise.
    :raises ValueError: If no evaluator is registered for the trigger type.
    """
    if trigger.type in (TriggerType.AND, TriggerType.OR):
        if trigger.type == TriggerType.AND:
            return _eval_and(trigger, base_dir, context)
        else:
            return _eval_or(trigger, base_dir, context)

    if trigger.type == TriggerType.CUSTOM:
        return _eval_custom(trigger, base_dir, context)

    evaluator = _EVALUATORS.get(trigger.type)
    if evaluator is None:
        raise ValueError(f"No evaluator for trigger type: {trigger.type}")
    return evaluator(trigger, base_dir)
