"""Action handlers and TickContext for cronpypeline.

Provides the pluggable ActionHandler interface and built-in handlers for
command, subprocess, and custom actions. The conversation_queue handler
lives in the plugins package.
"""

import errno
import fnmatch
import http.client
import ipaddress
import os
import shlex
import socket
import ssl
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
    :ivar command: The resolved action command/prompt/url/callable.
    """

    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    dry_run: bool = False
    data: dict[str, Any] = dc_field(default_factory=dict)
    command: str = ""

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


def _as_bool(value: Any) -> bool | None:
    """Interpret a value as a boolean.

    Handles string representations of booleans (commonly produced by YAML
    configs where booleans may be quoted), actual booleans, and falls back
    to Python's normal truthiness for any other value.

    :param value: The value to interpret as a boolean.
    :returns: ``True``/``False`` for recognized values, ``None`` for ``None``,
        and Python's normal truthiness for any other value.
    """
    if value is None:
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
    return bool(value)


# Private/reserved IPv4 networks blocked by SSRF protection.
_PRIVATE_V4_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
)

# Private/reserved IPv6 networks blocked by SSRF protection.
_PRIVATE_V6_NETWORKS = (
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
)


def _is_private_ip(ip_str: str) -> bool:
    """Return True if ``ip_str`` is in a private/reserved IP range.

    Handles both IPv4 and IPv6 addresses. IPv4-mapped IPv6 addresses are
    checked via their embedded IPv4 address. Unparseable inputs return False.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    if isinstance(ip, ipaddress.IPv4Address):
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return True
        return any(ip in net for net in _PRIVATE_V4_NETWORKS)

    # IPv4-mapped IPv6 addresses (::ffff:x.x.x.x) must be checked via their
    # embedded IPv4 address.  In Python 3.12+ IPv6Address.is_private returns
    # True for the entire ::ffff:0:0/96 range, so checking is_private before
    # extracting the mapped IPv4 would incorrectly block public addresses.
    mapped = ip.ipv4_mapped
    if mapped is not None:
        return _is_private_ip(str(mapped))

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True

    return any(ip in net for net in _PRIVATE_V6_NETWORKS)


def _host_matches(host: str, pattern: str) -> bool:
    """Match a hostname against a pattern (case-insensitive).

    Supports ``*`` wildcards (e.g. ``*.example.com``) via :func:`fnmatch.fnmatchcase`.
    """
    return fnmatch.fnmatchcase(host.lower(), pattern.lower())


def _validate_ssrf(url: str, params: dict) -> tuple[list[str] | None, str | None]:
    """Validate a URL against SSRF protections.

    Returns a tuple of (validated_ips, error_message). On success, validated_ips
    is a list of resolved public IP addresses (or None if ``pin_to_validated_ips``
    is disabled, meaning the connection is not pinned) and error_message is None.
    On failure, validated_ips is None and error_message describes the failure.

    DNS resolution and private-IP validation always run to prevent SSRF. The
    ``pin_to_validated_ips`` option only controls whether the connection is pinned
    to the validated public IPs (to prevent DNS rebinding) or left to normal DNS
    resolution at connection time. Private IPs are always blocked. The old name
    ``resolve_private_ip`` is a deprecated alias for backward compatibility; when
    both are present, ``pin_to_validated_ips`` takes precedence.
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    if host is None:
        return None, f"Invalid URL: no hostname in {url!r}"

    allowed_hosts = params.get("allowed_hosts")
    if isinstance(allowed_hosts, str):
        return None, "allowed_hosts must be a list of hostname patterns, got str"
    if allowed_hosts and not any(_host_matches(host, pattern) for pattern in allowed_hosts):
        return None, f"Host not allowed: {host!r}"

    blocked_hosts = params.get("blocked_hosts")
    if isinstance(blocked_hosts, str):
        return None, "blocked_hosts must be a list of hostname patterns, got str"
    if blocked_hosts and any(_host_matches(host, pattern) for pattern in blocked_hosts):
        return None, f"Host blocked: {host!r}"

    # DNS resolution and private-IP validation always run to prevent SSRF.
    # pin_to_validated_ips (formerly resolve_private_ip) only controls whether
    # the connection is pinned to the validated public IPs (to prevent DNS
    # rebinding) or left to normal DNS.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return None, f"Could not resolve host: {host!r}"
    public_ips = []
    for info in infos:
        ip = str(info[4][0])
        if _is_private_ip(ip):
            return None, f"SSRF blocked: host {host!r} resolves to private IP {ip!r}"
        public_ips.append(ip)
    # All resolved IPs are public.
    if "pin_to_validated_ips" in params:
        pin = params["pin_to_validated_ips"]
    else:
        pin = params.get("resolve_private_ip", True)
    if _as_bool(pin) and public_ips:
        return public_ips, None
    return None, None


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that connects to a pre-validated IP address."""

    def __init__(self, host: str, port: int | None = None, timeout: float | None = socket._GLOBAL_DEFAULT_TIMEOUT,  # type: ignore[attr-defined]
                 source_address: tuple[str, int] | None = None, blocksize: int = 8192, *, validated_ips: list[str] | None = None) -> None:
        """Initialize a pinned HTTP connection.

        :param host: Hostname to connect to.
        :param port: Port to connect to, or ``None`` for the default.
        :param timeout: Connection timeout in seconds.
        :param source_address: Source address to bind to, or ``None``.
        :param blocksize: Buffer size for file reads in bytes.
        :param validated_ips: Pre-validated IP addresses to pin the connection to, or ``None``.
        """
        self._validated_ips = validated_ips
        super().__init__(host, port, timeout, source_address, blocksize=blocksize)

    def connect(self) -> None:
        """Connect to a validated IP instead of re-resolving the hostname."""
        sys.audit("http.client.connect", self, self.host, self.port)
        connect_hosts = self._validated_ips or [self.host]
        last_err: OSError | None = None
        for connect_host in connect_hosts:
            try:
                self.sock = self._create_connection(  # type: ignore[attr-defined]
                    (connect_host, self.port), self.timeout, self.source_address  # type: ignore[attr-defined]
                )
                break
            except OSError as e:
                last_err = e
        if self.sock is None:
            raise last_err if last_err is not None else OSError("connection failed")
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as e:
            if e.errno != errno.ENOPROTOOPT:
                raise
        if self._tunnel_host:  # type: ignore[attr-defined]
            self._tunnel()  # type: ignore[attr-defined]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that connects to a pre-validated IP address."""

    def __init__(self, host: str, port: int | None = None,
                 timeout: float | None = socket._GLOBAL_DEFAULT_TIMEOUT, source_address: tuple[str, int] | None = None,  # type: ignore[attr-defined]
                 blocksize: int = 8192, *, context: ssl.SSLContext | None = None, validated_ips: list[str] | None = None) -> None:
        """Initialize a pinned HTTPS connection.

        :param host: Hostname to connect to.
        :param port: Port to connect to, or ``None`` for the default.
        :param timeout: Connection timeout in seconds.
        :param source_address: Source address to bind to, or ``None``.
        :param blocksize: Buffer size for file reads in bytes.
        :param context: SSL context to use for TLS, or ``None`` for the default.
        :param validated_ips: Pre-validated IP addresses to pin the connection to, or ``None``.
        """
        self._validated_ips = validated_ips
        super().__init__(host, port, timeout=timeout,
                         source_address=source_address, blocksize=blocksize,
                         context=context)

    def connect(self) -> None:
        """Connect to a validated IP and do TLS with the original hostname."""
        sys.audit("http.client.connect", self, self.host, self.port)
        connect_hosts = self._validated_ips or [self.host]
        last_err: OSError | None = None
        for connect_host in connect_hosts:
            try:
                self.sock = self._create_connection(  # type: ignore[attr-defined]
                    (connect_host, self.port), self.timeout, self.source_address  # type: ignore[attr-defined]
                )
                break
            except OSError as e:
                last_err = e
        if self.sock is None:
            raise last_err if last_err is not None else OSError("connection failed")
        try:
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as e:
            if e.errno != errno.ENOPROTOOPT:
                raise
        if self._tunnel_host:  # type: ignore[attr-defined]
            self._tunnel()  # type: ignore[attr-defined]
        # Use the original hostname for SNI and certificate validation
        server_hostname = self._tunnel_host if self._tunnel_host else self.host  # type: ignore[attr-defined]
        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)  # type: ignore[attr-defined]


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    """HTTPHandler that pins connections to a pre-validated IP address."""

    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        """Open an HTTP connection pinned to the validated IPs from ``req``.

        :param req: The request to open a connection for.
        :returns: The opened HTTP response, or falls back to the default
            ``http_open`` behavior when no validated IPs are present.
        :rtype: http.client.HTTPResponse
        """
        validated_ips = getattr(req, "_validated_ips", None)
        if validated_ips:
            return self.do_open(
                lambda host, timeout=None, **kwargs: _PinnedHTTPConnection(
                    host, timeout=timeout, validated_ips=validated_ips, **kwargs
                ),
                req,
            )
        return super().http_open(req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """HTTPSHandler that pins connections to a pre-validated IP address."""

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        """Open an HTTPS connection pinned to the validated IPs from ``req``.

        :param req: The request to open a connection for.
        :returns: The opened HTTPS response, or falls back to the default
            ``https_open`` behavior when no validated IPs are present.
        :rtype: http.client.HTTPResponse
        """
        validated_ips = getattr(req, "_validated_ips", None)
        if validated_ips:
            return self.do_open(
                lambda host, timeout=None, **kwargs: _PinnedHTTPSConnection(
                    host, timeout=timeout, validated_ips=validated_ips, **kwargs
                ),
                req,
                context=self._context,  # type: ignore[attr-defined]
            )
        return super().https_open(req)


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """HTTP redirect handler that prevents automatic redirect following."""

    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        """Prevent automatic redirect following by always returning None.

        :param req: The original request being redirected.
        :param fp: File-like object for the response body.
        :param code: HTTP status code that triggered the redirect.
        :param msg: HTTP status message.
        :param headers: Response headers.
        :param newurl: URL the request would be redirected to.
        :returns: Always ``None`` to suppress automatic redirect handling.
        """
        return  # Don't follow redirects automatically


_HTTP_OPENER = urllib.request.build_opener(
    NoRedirectHandler(),
    _PinnedHTTPHandler(),
    _PinnedHTTPSHandler(),
)
_MAX_REDIRECTS = 5
_SENSITIVE_HEADER_PREFIXES = ("x-",)
_SENSITIVE_HEADERS = {"authorization", "cookie", "proxy-authorization"}


def format_template(template: str, variables: dict[str, Any]) -> str:
    """Format a template string with variable substitution.

    :param template: Template string with ``{key}`` placeholders.
    :param variables: Mapping of keys to substitution values.
    :returns: Formatted string.
    :raises ValueError: If substitution fails (missing key, bad format, etc.).
    """
    try:
        return template.format(**variables)
    except (KeyError, IndexError, ValueError) as e:
        raise ValueError(f"Template substitution failed for: {template!r}: {e}") from e


def resolved_command(action: ActionSpec, context: TickContext) -> str:
    """Resolve an action's command/prompt/url/callable for logging.

    Produces a human-readable string describing what the action will do, with
    ``{target}``, ``{target_dir}``, and ``{workspace_dir}`` template variables
    (plus flattened ``target_config`` keys) substituted. Substitution failures
    fall back to the raw template so logging never raises.

    :param action: Action specification.
    :param context: Tick context for template substitution.
    :returns: Resolved command string (empty for unknown action types).
    """
    params = action.params
    variables = {
        "target": context.target,
        "target_dir": str(context.target_dir),
        "workspace_dir": str(context.workspace_dir),
    }
    for k, v in context.target_config.items():
        if k not in variables:
            variables[k] = v

    def _subst(template: str) -> str:
        """Substitute template variables, returning the raw template on failure.

        :param template: Template string to substitute.
        :returns: Substituted string, or the original template if substitution fails.
        """
        try:
            return format_template(template, variables)
        except ValueError:
            return template

    if action.type == ActionType.COMMAND:
        return _subst(params.get("command", ""))
    if action.type == ActionType.SUBPROCESS:
        script = _subst(params.get("script", ""))
        args = [str(a) for a in params.get("args", [])]
        return shlex.join([script] + args)
    if action.type == ActionType.HTTP_REQUEST:
        return _subst(params.get("url", ""))
    if action.type == ActionType.CUSTOM:
        return params.get("callable", "")
    if action.type == ActionType.QUEUE_AGENT:
        agent = params.get("agent", "")
        prompt = params.get("prompt", "") or params.get("prompt_template", "")
        resolved_prompt = _subst(prompt)
        return resolved_prompt or agent
    return ""


class ActionHandler:
    """Base class / interface for action handlers."""

    def _validate_cwd(self, cwd: str, workspace_dir: Path) -> ActionResult | None:
        """Validate that cwd is inside workspace_dir.

        Relative cwd paths are resolved against workspace_dir; absolute cwd
        paths are resolved as-is.

        :param cwd: The working directory path to validate.
        :param workspace_dir: The workspace root directory.
        :returns: An error ActionResult if cwd escapes the workspace, else None.
        """
        cwd_path = Path(cwd)
        if not cwd_path.is_absolute():
            cwd_path = workspace_dir / cwd_path
        cwd_path = cwd_path.resolve()
        workspace_resolved = workspace_dir.resolve()
        if not cwd_path.is_relative_to(workspace_resolved):
            return ActionResult(
                success=False,
                stderr=f"cwd escapes workspace directory: {cwd}",
            )
        return None

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
        command_str = resolved_command(action, context)

        if context.dry_run:
            return ActionResult(success=True, dry_run=True, command=command_str)

        cmd = action.params.get("command", "")
        cwd = action.params.get("cwd", str(context.target_dir))

        # Substitute template variables
        # cmd is executed via an argument list (no shell) so variables must be shell-quoted
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
        try:
            cmd = format_template(cmd, cmd_variables)
            cwd = format_template(cwd, cwd_variables)
        except ValueError as e:
            return ActionResult(success=False, stderr=str(e), command=command_str)

        result = self._validate_cwd(cwd, context.workspace_dir)
        if result is not None:
            result.command = command_str
            return result

        timeout = action.timeout_seconds or 300  # Default to 5 minutes

        Path(cwd).mkdir(parents=True, exist_ok=True)

        try:
            cmd_args = shlex.split(cmd)
        except ValueError as e:
            return ActionResult(success=False, stderr=f"Invalid command: {e}", command=command_str)

        try:
            proc = subprocess.run(
                cmd_args,
                shell=False,
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
                command=command_str,
            )
        except subprocess.TimeoutExpired:
            return ActionResult(
                success=False,
                timed_out=True,
                exit_code=-1,
                stderr=f"Command timed out after {timeout}s",
                command=command_str,
            )


class SubprocessActionHandler(ActionHandler):
    """Runs a Python script as a subprocess."""

    def execute(self, action: ActionSpec, context: TickContext) -> ActionResult:
        """Execute a Python script as a subprocess.

        :param action: Action spec with ``script``, ``args``, and optional ``cwd`` params.
        :param context: Tick context for working directory and environment.
        :returns: Result with stdout, stderr, and exit code.
        """
        command_str = resolved_command(action, context)

        if context.dry_run:
            return ActionResult(success=True, dry_run=True, command=command_str)

        script = action.params.get("script", "")
        args = action.params.get("args", [])
        cwd = action.params.get("cwd", str(context.target_dir))

        cwd_variables = {
            "target": context.target,
            "target_dir": str(context.target_dir),
            "workspace_dir": str(context.workspace_dir),
        }
        try:
            cwd = format_template(cwd, cwd_variables)
        except ValueError as e:
            return ActionResult(success=False, stderr=str(e), command=command_str)

        result = self._validate_cwd(cwd, context.workspace_dir)
        if result is not None:
            result.command = command_str
            return result

        timeout = action.timeout_seconds or 300  # Default to 5 minutes

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
                command=command_str,
            )
        except subprocess.TimeoutExpired:
            return ActionResult(
                success=False,
                timed_out=True,
                exit_code=-1,
                stderr=f"Subprocess timed out after {timeout}s",
                command=command_str,
            )


class CustomActionHandler(ActionHandler):
    """Calls a user-provided Python callable."""

    def execute(self, action: ActionSpec, context: TickContext) -> ActionResult:
        """Execute a custom Python callable.

        :param action: Action spec with ``callable`` dotted path and params.
        :param context: Tick context passed to the callable.
        :returns: Result adapted from the callable's return value.
        """
        callable_path = action.params.get("callable", "")

        if context.dry_run:
            return ActionResult(success=True, dry_run=True, command=callable_path)

        func = resolve_custom_callable(callable_path)

        result = func(action, context)

        if isinstance(result, ActionResult):
            if not result.command:
                result.command = callable_path
            return result
        elif isinstance(result, tuple):
            success, output = result
            return ActionResult(success=success, stdout=str(output), command=callable_path)
        elif isinstance(result, bool):
            return ActionResult(success=result, command=callable_path)
        elif isinstance(result, dict):
            result_dict: dict[str, Any] = result
            return ActionResult(
                success=result_dict.get("success", True),
                stdout=str(result_dict.get("output", "")),
                data=result_dict,
                command=callable_path,
            )
        else:
            return ActionResult(success=True, stdout=str(result), command=callable_path)


class HttpRequestActionHandler(ActionHandler):
    """Makes HTTP requests using urllib from the stdlib."""

    def execute(self, action: ActionSpec, context: TickContext) -> ActionResult:
        """Execute an HTTP request.

        :param action: Action spec with ``url``, ``method``, ``headers``, ``body``,
            and auth params. SSRF protection is controlled by the optional
            ``allowed_hosts``, ``blocked_hosts``, and ``pin_to_validated_ips``
            params (``resolve_private_ip`` is a deprecated alias).
        :param context: Tick context for auth token resolution from env.
        :returns: Result with response body, status code, and request metadata.
        """
        command_str = resolved_command(action, context)

        if context.dry_run:
            return ActionResult(success=True, dry_run=True, command=command_str)

        params = action.params
        url = params.get("url", "")
        method = params.get("method", "GET").upper()
        headers = dict(params.get("headers", {}))
        body = params.get("body")
        timeout = action.timeout_seconds or 30  # Default to 30 seconds

        # Resolve auth token: direct value, then env var, then context env
        auth_token = params.get("auth_token")
        if not auth_token:
            auth_token_env = params.get("auth_token_env")
            if auth_token_env:
                auth_token = context.env.get(auth_token_env) or os.environ.get(auth_token_env)
        if auth_token:
            headers["Authorization"] = f"token {auth_token}"

        # Build request data
        data = body.encode("utf-8") if body else None

        # Restrict URL schemes to http/https only to prevent file:// or custom scheme access
        parsed_url = urllib.parse.urlparse(url)
        if parsed_url.scheme not in ("http", "https"):
            return ActionResult(
                success=False,
                exit_code=-1,
                stderr=f"Unsupported URL scheme: {parsed_url.scheme!r}",
                command=command_str,
            )

        current_url = url
        for _ in range(_MAX_REDIRECTS + 1):
            # Restrict URL schemes to http/https only for every hop
            parsed_url = urllib.parse.urlparse(current_url)
            if parsed_url.scheme not in ("http", "https"):
                return ActionResult(
                    success=False,
                    exit_code=-1,
                    stderr=f"Unsupported URL scheme: {parsed_url.scheme!r}",
                    command=command_str,
                )

            # Validate SSRF for the current URL
            validated_ips, ssrf_error = _validate_ssrf(current_url, params)
            if ssrf_error is not None:
                return ActionResult(
                    success=False,
                    exit_code=-1,
                    stderr=ssrf_error,
                    command=command_str,
                )

            req = urllib.request.Request(current_url, data=data, method=method, headers=headers)
            req._validated_ips = validated_ips  # type: ignore[attr-defined]

            try:
                with _HTTP_OPENER.open(req, timeout=timeout) as resp:  # nosec B310 - URL scheme is validated to http/https only just above
                    status = resp.status
                    body_bytes = resp.read()
                    body_str = body_bytes.decode("utf-8", errors="replace")
                    success = 200 <= status < 300
                    return ActionResult(
                        success=success,
                        stdout=body_str,
                        exit_code=status,
                        data={"status_code": status, "url": _redact_url(current_url), "method": method},
                        command=command_str,
                    )
            except urllib.error.HTTPError as e:
                if 300 <= e.code < 400:
                    # Redirect - validate and follow manually
                    resp_headers: Any = e.headers or {}
                    location = resp_headers.get("Location")
                    if not location:
                        return ActionResult(
                            success=False,
                            exit_code=e.code,
                            stderr=f"HTTP {e.code}: Redirect without Location header",
                            command=command_str,
                        )
                    original_url = current_url
                    current_url = urllib.parse.urljoin(current_url, location)
                    # Don't forward auth headers to a different host
                    redirect_parsed = urllib.parse.urlparse(current_url)
                    original_parsed = urllib.parse.urlparse(original_url)
                    if redirect_parsed.netloc != original_parsed.netloc:
                        headers = {
                            k: v for k, v in headers.items()
                            if k.lower() not in _SENSITIVE_HEADERS
                            and not k.lower().startswith(_SENSITIVE_HEADER_PREFIXES)
                        }
                    # Match urllib's default redirect behavior for method/body
                    if e.code in (301, 302, 303) and method == "POST":
                        method = "GET"
                        data = None
                    elif e.code in (307, 308) and method == "POST":
                        # urllib does not follow 307/308 redirects for POST
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
                            data={"status_code": e.code, "url": _redact_url(original_url), "method": method},
                            command=command_str,
                        )
                    continue
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
                    data={"status_code": e.code, "url": _redact_url(current_url), "method": method},
                    command=command_str,
                )
            except urllib.error.URLError as e:
                if isinstance(e.reason, socket.timeout):
                    return ActionResult(
                        success=False,
                        timed_out=True,
                        exit_code=-1,
                        stderr=f"Request timed out after {timeout}s",
                        command=command_str,
                    )
                return ActionResult(
                    success=False,
                    exit_code=-1,
                    stderr=str(e.reason),
                    command=command_str,
                )

        return ActionResult(
            success=False,
            exit_code=-1,
            stderr=f"Too many redirects (max {_MAX_REDIRECTS})",
            command=command_str,
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
