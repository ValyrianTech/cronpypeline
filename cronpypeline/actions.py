"""Action handlers and TickContext for cronpypeline.

Provides the pluggable ActionHandler interface and built-in handlers for
command, subprocess, and custom actions. The conversation_queue handler
lives in the plugins package.
"""

import importlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Optional

from cronpypeline.config import ActionSpec, ActionType
from cronpypeline.triggers import resolve_custom_callable


@dataclass
class TickContext:
    """Context passed to action handlers during a tick."""
    target: str
    workspace_dir: Path
    dry_run: bool = False
    verbose: bool = False
    env: dict[str, str] = dc_field(default_factory=dict)
    state: Any = None
    pipeline: Any = None

    @property
    def target_dir(self) -> Path:
        """Directory for this target: workspace_dir / target."""
        return self.workspace_dir / self.target


@dataclass
class ActionResult:
    """Result of executing an action."""
    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    timed_out: bool = False
    dry_run: bool = False
    data: dict[str, Any] = dc_field(default_factory=dict)


def format_template(template: str, variables: dict[str, Any]) -> str:
    """Format a template string with variable substitution."""
    try:
        return template.format(**variables)
    except (KeyError, IndexError, ValueError):
        return template


class ActionHandler:
    """Base class / interface for action handlers."""

    def execute(self, action: ActionSpec, context: TickContext) -> ActionResult:
        raise NotImplementedError

    def check_complete(self, action: ActionSpec, context: TickContext) -> bool:
        """Check if a previously dispatched action has completed."""
        raise NotImplementedError


class CommandActionHandler(ActionHandler):
    """Runs a shell command."""

    def execute(self, action: ActionSpec, context: TickContext) -> ActionResult:
        if context.dry_run:
            return ActionResult(success=True, dry_run=True)

        cmd = action.params.get("command", "")
        cwd = action.params.get("cwd", str(context.target_dir))

        # Substitute template variables
        variables = {
            "target": context.target,
            "target_dir": str(context.target_dir),
            "workspace_dir": str(context.workspace_dir),
        }
        cmd = format_template(cmd, variables)
        cwd = format_template(cwd, variables)

        timeout = action.timeout_seconds

        Path(cwd).mkdir(parents=True, exist_ok=True)

        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, **context.env},
            )
            return ActionResult(
                success=proc.returncode == 0,
                stdout=proc.stdout,
                stderr=proc.stderr,
                exit_code=proc.returncode,
            )
        except subprocess.TimeoutExpired:
            return ActionResult(
                success=False,
                timed_out=True,
                exit_code=-1,
                stderr=f"Command timed out after {timeout}s",
            )


class SubprocessActionHandler(ActionHandler):
    """Runs a Python script as a subprocess."""

    def execute(self, action: ActionSpec, context: TickContext) -> ActionResult:
        if context.dry_run:
            return ActionResult(success=True, dry_run=True)

        script = action.params.get("script", "")
        args = action.params.get("args", [])
        cwd = action.params.get("cwd", str(context.target_dir))
        timeout = action.timeout_seconds

        if script.endswith(".py"):
            cmd = [sys.executable, script] + list(args)
        else:
            cmd = [script] + list(args)

        Path(cwd).mkdir(parents=True, exist_ok=True)

        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, **context.env},
            )
            return ActionResult(
                success=proc.returncode == 0,
                stdout=proc.stdout,
                stderr=proc.stderr,
                exit_code=proc.returncode,
            )
        except subprocess.TimeoutExpired:
            return ActionResult(
                success=False,
                timed_out=True,
                exit_code=-1,
                stderr=f"Subprocess timed out after {timeout}s",
            )


class CustomActionHandler(ActionHandler):
    """Calls a user-provided Python callable."""

    def execute(self, action: ActionSpec, context: TickContext) -> ActionResult:
        if context.dry_run:
            return ActionResult(success=True, dry_run=True)

        callable_path = action.params.get("callable", "")
        func = resolve_custom_callable(callable_path)

        result = func(action, context)

        if isinstance(result, tuple):
            success, output = result
            return ActionResult(success=success, stdout=str(output))
        elif isinstance(result, bool):
            return ActionResult(success=result)
        elif isinstance(result, dict):
            return ActionResult(
                success=result.get("success", True),
                stdout=str(result.get("output", "")),
                data=result,
            )
        else:
            return ActionResult(success=True, stdout=str(result))


# ─── Registry ───────────────────────────────────────────────────────────────

_HANDLERS: dict[ActionType, ActionHandler] = {
    ActionType.COMMAND: CommandActionHandler(),
    ActionType.SUBPROCESS: SubprocessActionHandler(),
    ActionType.CUSTOM: CustomActionHandler(),
}


def register_handler(action_type: ActionType, handler: ActionHandler) -> None:
    """Register a custom action handler."""
    _HANDLERS[action_type] = handler


def execute_action(action: ActionSpec, context: TickContext) -> ActionResult:
    """Execute an action using the appropriate handler."""
    handler = _HANDLERS.get(action.type)
    if handler is None:
        raise ValueError(f"No handler registered for action type: {action.type}")
    return handler.execute(action, context)
