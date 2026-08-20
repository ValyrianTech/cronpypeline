"""Tests for cronpypeline.actions — built-in action handlers and TickContext."""

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from cronpypeline.config import ActionSpec, ActionType
from cronpypeline.actions import (
    TickContext,
    ActionResult,
    ActionHandler,
    CommandActionHandler,
    SubprocessActionHandler,
    CustomActionHandler,
    execute_action,
    format_template,
)


class TestTickContext:
    """Tests for TickContext dataclass."""

    def test_basic_context(self, tmp_path):
        ctx = TickContext(
            target="my-repo",
            workspace_dir=tmp_path,
            dry_run=False,
            verbose=False,
        )
        assert ctx.target == "my-repo"
        assert ctx.workspace_dir == tmp_path
        assert ctx.dry_run is False

    def test_context_with_env(self, tmp_path):
        ctx = TickContext(
            target="my-repo",
            workspace_dir=tmp_path,
            dry_run=False,
            verbose=True,
            env={"MY_VAR": "value"},
        )
        assert ctx.env["MY_VAR"] == "value"

    def test_context_target_dir(self, tmp_path):
        ctx = TickContext(
            target="my-repo",
            workspace_dir=tmp_path / "workspace",
            dry_run=False,
            verbose=False,
        )
        # target_dir defaults to workspace_dir / target
        assert ctx.target_dir == tmp_path / "workspace" / "my-repo"


class TestFormatTemplate:
    """Tests for template string formatting."""

    def test_basic_substitution(self):
        result = format_template("Hello {name}", {"name": "world"})
        assert result == "Hello world"

    def test_target_dir_substitution(self):
        result = format_template("cd {target_dir}", {"target_dir": "/workspace/repo"})
        assert result == "cd /workspace/repo"

    def test_target_substitution(self):
        result = format_template("Processing {target}", {"target": "my-repo"})
        assert result == "Processing my-repo"

    def test_no_substitution_needed(self):
        result = format_template("echo hello", {})
        assert result == "echo hello"

    def test_multiple_substitutions(self):
        result = format_template("{target}: {target_dir}", {"target": "repo", "target_dir": "/path"})
        assert result == "repo: /path"

    def test_nested_dict_substitution(self):
        result = format_template("{issue_id}", {"issue_id": "123"})
        assert result == "123"


class TestCommandActionHandler:
    """Tests for command action handler."""

    def test_successful_command(self, tmp_path):
        action = ActionSpec(
            type=ActionType.COMMAND,
            params={"command": "echo hello", "cwd": str(tmp_path)},
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = CommandActionHandler()
        result = handler.execute(action, ctx)
        assert result.success is True
        assert "hello" in result.stdout

    def test_failed_command(self, tmp_path):
        action = ActionSpec(
            type=ActionType.COMMAND,
            params={"command": "false", "cwd": str(tmp_path)},
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = CommandActionHandler()
        result = handler.execute(action, ctx)
        assert result.success is False
        assert result.exit_code != 0

    def test_command_with_timeout(self, tmp_path):
        action = ActionSpec(
            type=ActionType.COMMAND,
            params={"command": "sleep 10", "cwd": str(tmp_path)},
            timeout_seconds=1,
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = CommandActionHandler()
        result = handler.execute(action, ctx)
        assert result.success is False
        assert result.timed_out is True

    def test_dry_run_does_not_execute(self, tmp_path):
        action = ActionSpec(
            type=ActionType.COMMAND,
            params={"command": "echo hello", "cwd": str(tmp_path)},
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=True, verbose=False)
        handler = CommandActionHandler()
        result = handler.execute(action, ctx)
        assert result.success is True
        assert result.dry_run is True
        assert result.stdout == ""

    def test_command_with_target_dir_substitution(self, tmp_path):
        action = ActionSpec(
            type=ActionType.COMMAND,
            params={"command": "pwd", "cwd": "{target_dir}"},
        )
        ctx = TickContext(target="my-repo", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = CommandActionHandler()
        result = handler.execute(action, ctx)
        assert result.success is True
        assert str(ctx.target_dir) in result.stdout

    def test_command_default_cwd_is_target_dir(self, tmp_path):
        action = ActionSpec(
            type=ActionType.COMMAND,
            params={"command": "pwd"},
        )
        ctx = TickContext(target="my-repo", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = CommandActionHandler()
        result = handler.execute(action, ctx)
        assert result.success is True
        assert str(ctx.target_dir) in result.stdout


class TestSubprocessActionHandler:
    """Tests for subprocess action handler."""

    def test_successful_subprocess(self, tmp_path):
        script = tmp_path / "test_script.py"
        script.write_text("print('subprocess works')\n")
        action = ActionSpec(
            type=ActionType.SUBPROCESS,
            params={"script": str(script), "args": []},
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = SubprocessActionHandler()
        result = handler.execute(action, ctx)
        assert result.success is True
        assert "subprocess works" in result.stdout

    def test_failed_subprocess(self, tmp_path):
        script = tmp_path / "fail_script.py"
        script.write_text("import sys; sys.exit(1)\n")
        action = ActionSpec(
            type=ActionType.SUBPROCESS,
            params={"script": str(script), "args": []},
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = SubprocessActionHandler()
        result = handler.execute(action, ctx)
        assert result.success is False
        assert result.exit_code == 1

    def test_dry_run_subprocess(self, tmp_path):
        action = ActionSpec(
            type=ActionType.SUBPROCESS,
            params={"script": "test.py", "args": []},
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=True, verbose=False)
        handler = SubprocessActionHandler()
        result = handler.execute(action, ctx)
        assert result.success is True
        assert result.dry_run is True


class TestCustomActionHandler:
    """Tests for custom action handler."""

    def test_custom_action_executes(self, tmp_path):
        module_code = """
def my_action(action, context):
    return True, "custom output"
"""
        (tmp_path / "custom_action_mod.py").write_text(module_code)
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            action = ActionSpec(
                type=ActionType.CUSTOM,
                params={"callable": "custom_action_mod.my_action"},
            )
            ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
            handler = CustomActionHandler()
            result = handler.execute(action, ctx)
            assert result.success is True
            assert "custom output" in result.stdout
        finally:
            sys.path.remove(str(tmp_path))
            if "custom_action_mod" in sys.modules:
                del sys.modules["custom_action_mod"]

    def test_custom_action_returns_false(self, tmp_path):
        module_code = """
def my_action(action, context):
    return False, "failed"
"""
        (tmp_path / "custom_action_mod2.py").write_text(module_code)
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            action = ActionSpec(
                type=ActionType.CUSTOM,
                params={"callable": "custom_action_mod2.my_action"},
            )
            ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
            handler = CustomActionHandler()
            result = handler.execute(action, ctx)
            assert result.success is False
        finally:
            sys.path.remove(str(tmp_path))
            if "custom_action_mod2" in sys.modules:
                del sys.modules["custom_action_mod2"]

    def test_dry_run_custom(self, tmp_path):
        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={"callable": "nonexistent.func"},
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=True, verbose=False)
        handler = CustomActionHandler()
        result = handler.execute(action, ctx)
        assert result.success is True
        assert result.dry_run is True


class TestExecuteAction:
    """Tests for the execute_action dispatcher."""

    def test_dispatches_command(self, tmp_path):
        action = ActionSpec(
            type=ActionType.COMMAND,
            params={"command": "echo test"},
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        result = execute_action(action, ctx)
        assert result.success is True
        assert "test" in result.stdout

    def test_dispatches_subprocess(self, tmp_path):
        script = tmp_path / "s.py"
        script.write_text("print('ok')\n")
        action = ActionSpec(
            type=ActionType.SUBPROCESS,
            params={"script": str(script)},
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        result = execute_action(action, ctx)
        assert result.success is True

    def test_dispatches_custom(self, tmp_path):
        module_code = """
def my_action(action, context):
    return True, "ok"
"""
        (tmp_path / "dispatch_mod.py").write_text(module_code)
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            action = ActionSpec(
                type=ActionType.CUSTOM,
                params={"callable": "dispatch_mod.my_action"},
            )
            ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
            result = execute_action(action, ctx)
            assert result.success is True
        finally:
            sys.path.remove(str(tmp_path))
            if "dispatch_mod" in sys.modules:
                del sys.modules["dispatch_mod"]


class TestActionResult:
    """Tests for ActionResult dataclass."""

    def test_success_result(self):
        r = ActionResult(success=True, stdout="output", exit_code=0)
        assert r.success is True
        assert r.stdout == "output"
        assert r.timed_out is False
        assert r.dry_run is False

    def test_failure_result(self):
        r = ActionResult(success=False, stdout="", stderr="error", exit_code=1)
        assert r.success is False
        assert r.stderr == "error"

    def test_dry_run_result(self):
        r = ActionResult(success=True, dry_run=True)
        assert r.dry_run is True
        assert r.stdout == ""

    def test_timeout_result(self):
        r = ActionResult(success=False, timed_out=True, exit_code=-1)
        assert r.timed_out is True
