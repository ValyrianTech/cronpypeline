"""Action handlers and TickContext for cronpypeline.

Provides the pluggable ActionHandler interface and built-in handlers for
command, subprocess, and custom actions. The conversation_queue handler
lives in the plugins package.
"""

import os
import shlex
import socket
import subprocess  # nosec B404 - subprocess is used by design to run pipeline commands/scripts
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

from cronpypeline.config import ActionSpec, ActionType
from cronpypeline.triggers import resolve_custom_callable


@dataclass
class TickContext:
    """Context passed to action handlers during a tick.

    :ivar target: Target name.
    :ivar workspace_dir: Root workspace directory.
    :ivar dry_run: Whether this is a dry run.
    :ivar verbose: Whether verbose output is enabled.
    :ivar env: Environment variables for the action.
    :ivar state: Pipeline state object (optional).
    :ivar pipeline: Pipeline instance (optional).
    :ivar target_config: Per-target configuration dict.
    :ivar retry_count: Number of retries so far (0 for first attempt).
    :ivar retry_data: Data from the previous processing marker (for continuation).
    """

    target: str
    workspace_dir: Path
    dry_run: bool = False
    verbose: bool = False
    env: dict[str, str] = dc_field(default_factory=dict)
    state: Any = None
    pipeline: Any = None
    target_config: dict[str, Any] = dc_field(default_factory=dict)
    retry_count: int = 0
    retry_data: dict[str, Any] | None = None

    @property
    def target_dir(self) -> Path:
        """Directory for this target: workspace_dir / target."""
        return self.workspace_dir / self.target


@dataclass
class ActionResult:
    """Result of executing an action.

    :ivar success: Whether the action succeeded.
    :ivar stdout: Captured stdout output.
    :ivar stderr: Captured stderr output.
    :ivar exit_code: Process exit code.
    :ivar timed_out: Whether the action timed out.
    :ivar dry_run: Whether this was a dry run.
    :ivar data: Additional result data (e.g. queue_file, entry_id).
    """

    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    dry_run: bool = False
    data: dict[str, Any] = dc_field(default_factory=dict)

    def __post_init__(self) -> None:
        """Initialize the result after construction.

        Ensures ``data`` is never None by defaulting it to an empty dict.
        """
        if self.data is None:
            self.data = {}


def _redact_url(url: str) -> str:
    """Remove userinfo and query params from a URL for safe logging."""
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc.split("@")[-1], parsed.path, "", "", ""))


def format_template(template: str, variables: dict[str, Any]) -> str:
    """Format a template string with variable substitution.

    :param template: Template string with ``{key}`` placeholders.
    :param variables: Mapping of keys to substitution values.
    :returns: Formatted string, or the original template if substitution fails.
    """
    try:
        return template.format(**variables)
    except (KeyError, IndexError, ValueError):
        return template


class ActionHandler:
    """Base class / interface for action handlers."""

    def execute(self, action: ActionSpec, context: TickContext) -> ActionResult:
        """Execute the action.

        :param action: Action specification with parameters.
        :param context: Tick context with target, workspace, and config.
        :returns: Result of the action execution.
        :raises NotImplementedError: Always — subclasses must implement.
        """
        raise NotImplementedError

    def check_complete(self, action: ActionSpec, context: TickContext) -> bool:
        """Check if a previously dispatched action has completed.

        :param action: Action specification.
        :param context: Tick context.
        :returns: True if the action is complete.
        :raises NotImplementedError: Always — subclasses must implement.
        """
        raise NotImplementedError


class CommandActionHandler(ActionHandler):
    """Runs a shell command."""

    def execute(self, action: ActionSpec, context: TickContext) -> ActionResult:
        """Execute a shell command.

        :param action: Action spec with ``command`` and optional ``cwd`` params.
        :param context: Tick context for template substitution and working directory.
        :returns: Result with stdout, stderr, and exit code.
        """
        if context.dry_run:
            return ActionResult(success=True, dry_run=True)

        cmd = action.params.get("command", "")
        cwd = action.params.get("cwd", str(context.target_dir))

        # Substitute template variables
        # cmd is executed via shell=True so variables must be shell-quoted
        cmd_variables = {
            "target": shlex.quote(context.target),
            "target_dir": shlex.quote(str(context.target_dir)),
            "workspace_dir": shlex.quote(str(context.workspace_dir)),
        }
        # cwd is used as a filesystem path so variables must NOT be quoted
        cwd_variables = {
            "target": context.target,
            "target_dir": str(context.target_dir),
            "workspace_dir": str(context.workspace_dir),
        }
        cmd = format_template(cmd, cmd_variables)
        cwd = format_template(cwd, cwd_variables)

        timeout = action.timeout_seconds

        Path(cwd).mkdir(parents=True, exist_ok=True)

        try:
            proc = subprocess.run(
                cmd,
                shell=True,  # nosec B602 - shell command execution is the intended behavior of CommandActionHandler; commands come from trusted pipeline config
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, **context.env},
                check=False,
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
        """Execute a Python script as a subprocess.

        :param action: Action spec with ``script``, ``args``, and optional ``cwd`` params.
        :param context: Tick context for working directory and environment.
        :returns: Result with stdout, stderr, and exit code.
        """
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
            proc = subprocess.run(  # nosec B603 - runs an explicit executable/script without a shell; args are passed as a list
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, **context.env},
                check=False,
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
        """Execute a custom Python callable.

        :param action: Action spec with ``callable`` dotted path and params.
        :param context: Tick context passed to the callable.
        :returns: Result adapted from the callable's return value.
        """
        if context.dry_run:
            return ActionResult(success=True, dry_run=True)

        callable_path = action.params.get("callable", "")
        func = resolve_custom_callable(callable_path)

        result = func(action, context)

        if isinstance(result, ActionResult):
            return result
        elif isinstance(result, tuple):
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


class HttpRequestActionHandler(ActionHandler):
    """Makes HTTP requests using urllib from the stdlib."""

    def execute(self, action: ActionSpec, context: TickContext) -> ActionResult:
        """Execute an HTTP request.

        :param action: Action spec with ``url``, ``method``, ``headers``, ``body``, and auth params.
        :param context: Tick context for auth token resolution from env.
        :returns: Result with response body, status code, and request metadata.
        """
        if context.dry_run:
            return ActionResult(success=True, dry_run=True)

        params = action.params
        url = params.get("url", "")
        method = params.get("method", "GET").upper()
        headers = dict(params.get("headers", {}))
        body = params.get("body")
        timeout = action.timeout_seconds

        # Resolve auth token: direct value, then env var, then context env
        auth_token = params.get("auth_token")
        if not auth_token:
            auth_token_env = params.get("auth_token_env")
            if auth_token_env:
                auth_token = context.env.get(auth_token_env) or os.environ.get(auth_token_env)
        if auth_token:
            headers["Authorization"] = f"token {auth_token}"

        # Build request
        data = body.encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)

        # Restrict URL schemes to http/https only to prevent file:// or custom scheme access
        parsed_url = urllib.parse.urlparse(url)
        if parsed_url.scheme not in ("http", "https"):
            return ActionResult(
                success=False,
                exit_code=-1,
                stderr=f"Unsupported URL scheme: {parsed_url.scheme!r}",
            )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 - URL scheme is validated to http/https only just above
                status = resp.status
                body_bytes = resp.read()
                body_str = body_bytes.decode("utf-8", errors="replace")
                success = 200 <= status < 300
                return ActionResult(
                    success=success,
                    stdout=body_str,
                    exit_code=status,
                    data={"status_code": status, "url": _redact_url(url), "method": method},
                )
        except urllib.error.HTTPError as e:
            body_str = ""
            try:
                body_str = e.read().decode("utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                pass
            return ActionResult(
                success=False,
                stdout=body_str,
                stderr=f"HTTP {e.code}: {e.reason}",
                exit_code=e.code,
                data={"status_code": e.code, "url": _redact_url(url), "method": method},
            )
        except urllib.error.URLError as e:
            if isinstance(e.reason, socket.timeout):
                return ActionResult(
                    success=False,
                    timed_out=True,
                    exit_code=-1,
                    stderr=f"Request timed out after {timeout}s",
                )
            return ActionResult(
                success=False,
                exit_code=-1,
                stderr=str(e.reason),
            )


# ─── Registry ───────────────────────────────────────────────────────────────

_HANDLERS: dict[ActionType, ActionHandler] = {
    ActionType.COMMAND: CommandActionHandler(),
    ActionType.SUBPROCESS: SubprocessActionHandler(),
    ActionType.CUSTOM: CustomActionHandler(),
    ActionType.HTTP_REQUEST: HttpRequestActionHandler(),
}


def register_handler(action_type: ActionType, handler: ActionHandler) -> None:
    """Register a custom action handler.

    :param action_type: The action type to register the handler for.
    :param handler: The handler instance to register.
    """
    _HANDLERS[action_type] = handler


def execute_action(action: ActionSpec, context: TickContext) -> ActionResult:
    """Execute an action using the appropriate handler.

    :param action: Action specification to execute.
    :param context: Tick context for the action.
    :returns: Result of the action execution.
    :raises ValueError: If no handler is registered for the action type.
    """
    handler = _HANDLERS.get(action.type)
    if handler is None:
        raise ValueError(f"No handler registered for action type: {action.type}")
    return handler.execute(action, context)
