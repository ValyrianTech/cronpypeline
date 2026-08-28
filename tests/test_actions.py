"""Tests for cronpypeline.actions — built-in action handlers and TickContext."""

from unittest.mock import MagicMock, patch

import pytest

from cronpypeline.actions import (
    ActionResult,
    CommandActionHandler,
    CustomActionHandler,
    HttpRequestActionHandler,
    SubprocessActionHandler,
    TickContext,
    _redact_url,
    execute_action,
    format_template,
)
from cronpypeline.config import ActionSpec, ActionType


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

    def test_context_with_target_config(self, tmp_path):
        ctx = TickContext(
            target="my-repo",
            workspace_dir=tmp_path,
            target_config={"test_cmd": "pytest", "coverage_threshold": 90},
        )
        assert ctx.target_config["test_cmd"] == "pytest"
        assert ctx.target_config["coverage_threshold"] == 90

    def test_context_target_config_defaults_empty(self, tmp_path):
        ctx = TickContext(target="my-repo", workspace_dir=tmp_path)
        assert ctx.target_config == {}


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

    def test_cwd_with_spaces_is_not_quoted(self, tmp_path):
        workspace_dir = tmp_path / "my workspace"
        action = ActionSpec(
            type=ActionType.COMMAND,
            params={"command": "pwd", "cwd": "{target_dir}"},
        )
        ctx = TickContext(
            target="repo",
            workspace_dir=workspace_dir,
            dry_run=False,
            verbose=False,
        )
        handler = CommandActionHandler()
        result = handler.execute(action, ctx)
        assert result.success is True
        assert str(ctx.target_dir) in result.stdout
        assert f"'{ctx.target_dir!s}'" not in result.stdout
        assert ctx.target_dir.exists()
        quoted_dir = workspace_dir / "'repo'"
        assert not quoted_dir.exists()

    def test_target_shell_metacharacters_are_quoted(self, tmp_path):
        action = ActionSpec(
            type=ActionType.COMMAND,
            params={"command": "echo {target}"},
        )
        ctx = TickContext(
            target="repo; echo INJECTED",
            workspace_dir=tmp_path,
            dry_run=False,
            verbose=False,
        )
        handler = CommandActionHandler()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            handler.execute(action, ctx)
        cmd = mock_run.call_args.args[0]
        assert cmd == ["echo", "repo; echo INJECTED"]

    def test_format_template_failure_returns_error(self, tmp_path):
        """When format_template fails, should return failure ActionResult."""
        action = ActionSpec(
            type=ActionType.COMMAND,
            params={"command": "echo {missing_var}"},
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = CommandActionHandler()
        result = handler.execute(action, ctx)
        assert result.success is False
        assert "Template substitution failed" in result.stderr

    def test_shlex_split_failure_returns_error(self, tmp_path):
        """When shlex.split fails (unterminated quote), should return failure."""
        action = ActionSpec(
            type=ActionType.COMMAND,
            params={"command": "echo 'unterminated"},
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = CommandActionHandler()
        result = handler.execute(action, ctx)
        assert result.success is False
        assert "Invalid command" in result.stderr


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


class TestHttpRequestActionHandler:
    """Tests for the HTTP request action handler."""

    def test_successful_get(self, tmp_path):
        action = ActionSpec(
            type=ActionType.HTTP_REQUEST,
            params={"url": "http://localhost:12345/api", "method": "GET"},
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = HttpRequestActionHandler()

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.getheaders.return_value = [("Content-Type", "application/json")]

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = handler.execute(action, ctx)

        assert result.success is True
        assert result.exit_code == 200
        assert "ok" in result.stdout

    def test_successful_post_with_body(self, tmp_path):
        action = ActionSpec(
            type=ActionType.HTTP_REQUEST,
            params={
                "url": "http://localhost:12345/api",
                "method": "POST",
                "body": '{"title": "test"}',
                "headers": {"Content-Type": "application/json"},
            },
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = HttpRequestActionHandler()

        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_resp.read.return_value = b'{"id": 1}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            result = handler.execute(action, ctx)

        assert result.success is True
        assert result.exit_code == 201
        # Verify the request was built with POST method and body
        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "POST"

    def test_dry_run_does_not_execute(self, tmp_path):
        action = ActionSpec(
            type=ActionType.HTTP_REQUEST,
            params={"url": "http://localhost:12345/api", "method": "GET"},
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=True, verbose=False)
        handler = HttpRequestActionHandler()

        with patch("urllib.request.urlopen") as mock_urlopen:
            result = handler.execute(action, ctx)

        assert result.success is True
        assert result.dry_run is True
        mock_urlopen.assert_not_called()

    def test_non_2xx_response_is_failure(self, tmp_path):
        action = ActionSpec(
            type=ActionType.HTTP_REQUEST,
            params={"url": "http://localhost:12345/api", "method": "GET"},
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = HttpRequestActionHandler()

        mock_resp = MagicMock()
        mock_resp.status = 404
        mock_resp.read.return_value = b'{"error": "not found"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = handler.execute(action, ctx)

        assert result.success is False
        assert result.exit_code == 404

    def test_auth_token_from_env(self, tmp_path):
        action = ActionSpec(
            type=ActionType.HTTP_REQUEST,
            params={
                "url": "http://localhost:12345/api",
                "method": "GET",
                "auth_token_env": "GITHUB_TOKEN",
            },
        )
        ctx = TickContext(
            target="test", workspace_dir=tmp_path, dry_run=False, verbose=False,
            env={"GITHUB_TOKEN": "ghp_secret123"},
        )
        handler = HttpRequestActionHandler()

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            result = handler.execute(action, ctx)

        assert result.success is True
        req = mock_urlopen.call_args[0][0]
        assert req.headers.get("Authorization") == "token ghp_secret123"

    def test_auth_token_direct(self, tmp_path):
        action = ActionSpec(
            type=ActionType.HTTP_REQUEST,
            params={
                "url": "http://localhost:12345/api",
                "method": "GET",
                "auth_token": "ghp_direct_token",
            },
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = HttpRequestActionHandler()

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            result = handler.execute(action, ctx)

        assert result.success is True
        req = mock_urlopen.call_args[0][0]
        assert req.headers.get("Authorization") == "token ghp_direct_token"

    def test_patch_method(self, tmp_path):
        action = ActionSpec(
            type=ActionType.HTTP_REQUEST,
            params={
                "url": "http://localhost:12345/api/1",
                "method": "PATCH",
                "body": '{"state": "closed"}',
                "headers": {"Content-Type": "application/json"},
            },
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = HttpRequestActionHandler()

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            result = handler.execute(action, ctx)

        assert result.success is True
        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "PATCH"

    def test_url_error_is_failure(self, tmp_path):
        action = ActionSpec(
            type=ActionType.HTTP_REQUEST,
            params={"url": "http://localhost:12345/api", "method": "GET"},
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = HttpRequestActionHandler()

        from urllib.error import URLError
        with patch("urllib.request.urlopen", side_effect=URLError("Connection refused")):
            result = handler.execute(action, ctx)

        assert result.success is False
        assert "Connection refused" in result.stderr

    def test_default_method_is_get(self, tmp_path):
        action = ActionSpec(
            type=ActionType.HTTP_REQUEST,
            params={"url": "http://localhost:12345/api"},
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = HttpRequestActionHandler()

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            result = handler.execute(action, ctx)

        assert result.success is True
        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "GET"

    def test_response_data_included(self, tmp_path):
        action = ActionSpec(
            type=ActionType.HTTP_REQUEST,
            params={"url": "http://localhost:12345/api", "method": "GET"},
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = HttpRequestActionHandler()

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"id": 42, "name": "test"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = handler.execute(action, ctx)

        assert result.success is True
        assert result.data["status_code"] == 200

    def test_timeout(self, tmp_path):
        action = ActionSpec(
            type=ActionType.HTTP_REQUEST,
            params={"url": "http://localhost:12345/api", "method": "GET"},
            timeout_seconds=1,
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = HttpRequestActionHandler()

        from urllib.error import URLError
        with patch("urllib.request.urlopen", side_effect=URLError(TimeoutError("timed out"))):
            result = handler.execute(action, ctx)

        assert result.success is False
        assert result.timed_out is True


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


class TestFormatTemplateEdgeCases:
    """Tests for format_template error handling."""

    def test_keyerror_raises(self):
        with pytest.raises(ValueError, match="Template substitution failed"):
            format_template("Hello {missing}", {"name": "world"})

    def test_indexerror_raises(self):
        with pytest.raises(ValueError, match="Template substitution failed"):
            format_template("Hello {0}", {})

    def test_valueerror_raises(self):
        with pytest.raises(ValueError, match="Template substitution failed"):
            format_template("Hello {name!x}", {"name": "world"})


class TestActionHandlerBase:
    """Tests for ActionHandler base class."""

    def test_base_execute_raises_not_implemented(self, tmp_path):
        from cronpypeline.actions import ActionHandler
        base = ActionHandler()
        ctx = TickContext(target="test", workspace_dir=tmp_path)
        action = ActionSpec(type=ActionType.COMMAND, params={})
        with pytest.raises(NotImplementedError):
            base.execute(action, ctx)

    def test_base_check_complete_raises_not_implemented(self, tmp_path):
        from cronpypeline.actions import ActionHandler
        base = ActionHandler()
        ctx = TickContext(target="test", workspace_dir=tmp_path)
        action = ActionSpec(type=ActionType.COMMAND, params={})
        with pytest.raises(NotImplementedError):
            base.check_complete(action, ctx)


class TestSubprocessActionHandlerEdgeCases:
    """Tests for subprocess handler edge cases."""

    def test_subprocess_timeout(self, tmp_path):
        """Subprocess that times out should return timed_out result."""
        script = tmp_path / "slow_script.py"
        script.write_text("import time; time.sleep(10)\n")
        action = ActionSpec(
            type=ActionType.SUBPROCESS,
            params={"script": str(script), "args": []},
            timeout_seconds=1,
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = SubprocessActionHandler()
        result = handler.execute(action, ctx)
        assert result.success is False
        assert result.timed_out is True

    def test_subprocess_non_py_script(self, tmp_path):
        """Non-.py script should be executed directly."""
        script = tmp_path / "script.sh"
        script.write_text("#!/bin/bash\necho 'shell works'\n")
        script.chmod(0o755)
        action = ActionSpec(
            type=ActionType.SUBPROCESS,
            params={"script": str(script), "args": []},
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = SubprocessActionHandler()
        result = handler.execute(action, ctx)
        assert result.success is True
        assert "shell works" in result.stdout


class TestCustomActionHandlerReturnTypes:
    """Tests for custom action handler return type adaptation."""

    def test_custom_action_returns_dict(self, tmp_path):
        module_code = """
def my_action(action, context):
    return {"success": True, "output": "dict output", "extra": "data"}
"""
        (tmp_path / "dict_mod.py").write_text(module_code)
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            action = ActionSpec(
                type=ActionType.CUSTOM,
                params={"callable": "dict_mod.my_action"},
            )
            ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
            handler = CustomActionHandler()
            result = handler.execute(action, ctx)
            assert result.success is True
            assert "dict output" in result.stdout
            assert result.data["extra"] == "data"
        finally:
            sys.path.remove(str(tmp_path))
            if "dict_mod" in sys.modules:
                del sys.modules["dict_mod"]

    def test_custom_action_returns_bool_true(self, tmp_path):
        module_code = """
def my_action(action, context):
    return True
"""
        (tmp_path / "bool_mod.py").write_text(module_code)
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            action = ActionSpec(
                type=ActionType.CUSTOM,
                params={"callable": "bool_mod.my_action"},
            )
            ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
            handler = CustomActionHandler()
            result = handler.execute(action, ctx)
            assert result.success is True
        finally:
            sys.path.remove(str(tmp_path))
            if "bool_mod" in sys.modules:
                del sys.modules["bool_mod"]

    def test_custom_action_returns_string(self, tmp_path):
        module_code = """
def my_action(action, context):
    return "just a string"
"""
        (tmp_path / "str_mod.py").write_text(module_code)
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            action = ActionSpec(
                type=ActionType.CUSTOM,
                params={"callable": "str_mod.my_action"},
            )
            ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
            handler = CustomActionHandler()
            result = handler.execute(action, ctx)
            assert result.success is True
            assert "just a string" in result.stdout
        finally:
            sys.path.remove(str(tmp_path))
            if "str_mod" in sys.modules:
                del sys.modules["str_mod"]

    def test_custom_action_returns_action_result(self, tmp_path):
        module_code = """
from cronpypeline.actions import ActionResult

def my_action(action, context):
    return ActionResult(success=True, data={"async": True, "queue_file": "/tmp/x.json"})
"""
        (tmp_path / "actionresult_mod.py").write_text(module_code)
        import sys
        sys.path.insert(0, str(tmp_path))
        try:
            action = ActionSpec(
                type=ActionType.CUSTOM,
                params={"callable": "actionresult_mod.my_action"},
            )
            ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
            handler = CustomActionHandler()
            result = handler.execute(action, ctx)
            assert result.success is True
            assert result.data.get("async") is True
            assert result.data.get("queue_file") == "/tmp/x.json"
        finally:
            sys.path.remove(str(tmp_path))
            if "actionresult_mod" in sys.modules:
                del sys.modules["actionresult_mod"]


class TestHttpRequestActionHandlerErrors:
    """Tests for HTTP request handler error handling."""

    def test_unsupported_url_scheme_is_rejected(self, tmp_path):
        """Non-http(s) URL schemes should be rejected before any request."""
        action = ActionSpec(
            type=ActionType.HTTP_REQUEST,
            params={"url": "file:///etc/passwd", "method": "GET"},
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = HttpRequestActionHandler()

        with patch("urllib.request.urlopen") as mock_urlopen:
            result = handler.execute(action, ctx)

        assert result.success is False
        assert result.exit_code == -1
        assert "Unsupported URL scheme" in result.stderr
        mock_urlopen.assert_not_called()

    def test_http_error_is_failure(self, tmp_path):
        """HTTPError (e.g. 500) should be caught and return failure."""
        from unittest.mock import patch
        from urllib.error import HTTPError

        action = ActionSpec(
            type=ActionType.HTTP_REQUEST,
            params={"url": "http://localhost:12345/api", "method": "GET"},
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = HttpRequestActionHandler()

        error = HTTPError("http://localhost:12345/api", 500, "Internal Server Error", {}, None)

        with patch("urllib.request.urlopen", side_effect=error):
            result = handler.execute(action, ctx)

        assert result.success is False
        assert result.exit_code == 500
        assert "HTTP 500" in result.stderr

    def test_http_error_read_failure(self, tmp_path):
        """HTTPError where reading body also fails should still return failure."""
        from unittest.mock import MagicMock, patch
        from urllib.error import HTTPError

        action = ActionSpec(
            type=ActionType.HTTP_REQUEST,
            params={"url": "http://localhost:12345/api", "method": "GET"},
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = HttpRequestActionHandler()

        error = HTTPError("http://localhost:12345/api", 503, "Service Unavailable", {}, None)
        error.read = MagicMock(side_effect=OSError("read failed"))

        with patch("urllib.request.urlopen", side_effect=error):
            result = handler.execute(action, ctx)

        assert result.success is False
        assert result.exit_code == 503

    def test_url_error_socket_timeout(self, tmp_path):
        """URLError with socket.timeout reason should set timed_out."""
        from unittest.mock import patch
        from urllib.error import URLError

        action = ActionSpec(
            type=ActionType.HTTP_REQUEST,
            params={"url": "http://localhost:12345/api", "method": "GET"},
            timeout_seconds=1,
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = HttpRequestActionHandler()

        with patch("urllib.request.urlopen", side_effect=URLError(TimeoutError("timed out"))):
            result = handler.execute(action, ctx)

        assert result.success is False
        assert result.timed_out is True


class TestExecuteActionErrors:
    """Tests for execute_action dispatcher error handling."""

    def test_no_handler_registered_raises_error(self, tmp_path):
        """Unregistered action type should raise ValueError."""
        from cronpypeline.actions import _HANDLERS
        from cronpypeline.config import ActionType as AT

        action = ActionSpec(type=AT.COMMAND, params={"command": "echo test"})
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)

        # Temporarily remove the command handler
        saved = _HANDLERS.pop(AT.COMMAND)
        try:
            with pytest.raises(ValueError, match="No handler registered"):
                execute_action(action, ctx)
        finally:
            _HANDLERS[AT.COMMAND] = saved


class TestRedactUrl:
    """Tests for the _redact_url URL redaction helper."""

    def test_removes_query_params(self):
        assert _redact_url("https://example.com/api?api_key=secret&token=abc") == "https://example.com/api"

    def test_removes_userinfo(self):
        assert _redact_url("https://user:pass@example.com/api") == "https://example.com/api"

    def test_removes_both_userinfo_and_query(self):
        assert _redact_url("https://user:pass@example.com/api?key=secret") == "https://example.com/api"

    def test_keeps_path_and_scheme(self):
        assert _redact_url("http://example.com/path/to/resource") == "http://example.com/path/to/resource"

    def test_no_credentials_unchanged(self):
        assert _redact_url("https://example.com/plain") == "https://example.com/plain"


class TestHttpRequestActionHandlerRedaction:
    """Tests for URL redaction in HTTP request handler result data."""

    def test_success_result_redacts_url(self, tmp_path):
        action = ActionSpec(
            type=ActionType.HTTP_REQUEST,
            params={"url": "https://user:pass@localhost:12345/api?api_key=secret", "method": "GET"},
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = HttpRequestActionHandler()

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"ok": true}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = handler.execute(action, ctx)

        assert result.success is True
        assert result.data["url"] == "https://localhost:12345/api"

    def test_http_error_result_redacts_url(self, tmp_path):
        from urllib.error import HTTPError

        action = ActionSpec(
            type=ActionType.HTTP_REQUEST,
            params={"url": "https://example.com/api?api_key=secret&token=abc", "method": "GET"},
        )
        ctx = TickContext(target="test", workspace_dir=tmp_path, dry_run=False, verbose=False)
        handler = HttpRequestActionHandler()

        error = HTTPError("https://example.com/api", 500, "Internal Server Error", {}, None)

        with patch("urllib.request.urlopen", side_effect=error):
            result = handler.execute(action, ctx)

        assert result.success is False
        assert result.data["url"] == "https://example.com/api"


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

    def test_data_none_normalized_to_empty_dict(self):
        r = ActionResult(success=True, data=None)
        assert r.data == {}

    def test_data_defaults_to_empty_dict(self):
        r = ActionResult(success=True)
        assert r.data == {}

    def test_data_dict_preserved(self):
        r = ActionResult(success=True, data={"async": True})
        assert r.data == {"async": True}

    def test_data_none_get_async_returns_false(self):
        r = ActionResult(success=True, data=None)
        assert r.data.get("async", False) is False
