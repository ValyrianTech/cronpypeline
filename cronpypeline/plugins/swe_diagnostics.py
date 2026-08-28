"""SWE diagnostic report action handlers and output parsers.

Provides:
- `run_diagnostic`: custom action callable that runs a command, parses output,
  writes a timestamped markdown report, and creates a `latest.md` symlink.
- Output parsers for common SWE diagnostic tools (pytest, ruff, mypy, etc.).
"""

import re
import shlex
import subprocess  # nosec B404 - subprocess is used by design to run diagnostic commands
from collections.abc import Callable
from typing import Any

from cronpypeline.actions import ActionResult, TickContext, format_template
from cronpypeline.config import ActionSpec
from cronpypeline.reporting import (
    generate_timestamp,
    update_latest_symlink,
    write_report,
)

# ─── Output parsers ─────────────────────────────────────────────────────────


def parse_pytest_output(output: str, **kwargs: Any) -> dict[str, Any]:
    """Parse pytest output for pass/fail/error/skip counts.

    :param output: Raw stdout from pytest.
    :returns: Dict with ``passed``, ``failed``, ``errors``, ``skipped``, and ``status``.
    """
    result: dict[str, Any] = {
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
    }
    m = re.search(r"(\d+) passed", output)
    if m:
        result["passed"] = int(m.group(1))
    m = re.search(r"(\d+) failed", output)
    if m:
        result["failed"] = int(m.group(1))
    m = re.search(r"(\d+) error", output)
    if m:
        result["errors"] = int(m.group(1))
    m = re.search(r"(\d+) skipped", output)
    if m:
        result["skipped"] = int(m.group(1))
    result["status"] = "FAIL" if result["failed"] > 0 or result["errors"] > 0 else "PASS"
    return result


def parse_ruff_output(output: str, **kwargs: Any) -> dict[str, Any]:
    """Parse ruff output for error count and fixable count.

    :param output: Raw stdout from ruff.
    :returns: Dict with ``errors``, ``fixable``, and ``status``.
    """
    result: dict[str, Any] = {"errors": 0, "fixable": 0}
    m = re.search(r"Found (\d+) error", output)
    if m:
        result["errors"] = int(m.group(1))
    m = re.search(r"(\d+) fixable", output)
    if m:
        result["fixable"] = int(m.group(1))
    if "All checks passed" in output or (result["errors"] == 0 and not output.strip()):
        result["status"] = "PASS"
    else:
        result["status"] = "FAIL" if result["errors"] > 0 else "PASS"
    return result


def parse_mypy_output(output: str, **kwargs: Any) -> dict[str, Any]:
    """Parse mypy output for error count.

    :param output: Raw stdout from mypy.
    :returns: Dict with ``errors`` and ``status``.
    """
    result: dict[str, Any] = {"errors": 0}
    m = re.search(r"Found (\d+) error", output)
    if m:
        result["errors"] = int(m.group(1))
    if "no issues found" in output or result["errors"] == 0:
        result["status"] = "PASS"
    else:
        result["status"] = "FAIL"
    return result


def parse_interrogate_output(output: str, **kwargs: Any) -> dict[str, Any]:
    """Parse interrogate output for docstring coverage.

    Interrogate's verbose output ends with a RESULT footer and a TOTAL row.

    :param output: Raw stdout from interrogate.
    :returns: Dict with ``coverage``, ``covered``, ``missing``, ``total``, and ``status``.
    """
    result: dict[str, Any] = {"coverage": 0.0, "covered": 0, "missing": 0, "total": 0}
    m = re.search(r"RESULT:\s+(PASSED|FAILED)", output)
    passed = bool(m and m.group(1) == "PASSED")
    m = re.search(r"actual:\s*([\d.]+)%", output)
    if m:
        result["coverage"] = float(m.group(1))
    for line in output.splitlines():
        if "TOTAL" not in line:
            continue
        nums = re.findall(r"(\d+(?:\.\d+)?)", line)
        if len(nums) >= 4:
            result["total"] = int(float(nums[0]))
            result["missing"] = int(float(nums[1]))
            result["covered"] = int(float(nums[2]))
            if result["coverage"] == 0.0:
                result["coverage"] = float(nums[3])
            break
    result["status"] = "PASS" if passed else "FAIL"
    return result


def parse_pydocstyle_output(output: str, **kwargs: Any) -> dict[str, Any]:
    """Parse pydocstyle output for violation count.

    :param output: Raw stdout from pydocstyle.
    :returns: Dict with ``errors`` and ``status``.
    """
    # Count lines that look like violations: "file:line: Dxxx: message"
    violations = len(re.findall(r"\bD\d{3}\b", output))
    result: dict[str, Any] = {"errors": violations}
    result["status"] = "FAIL" if violations > 0 else "PASS"
    return result


def parse_vulture_output(output: str, **kwargs: Any) -> dict[str, Any]:
    """Parse vulture output for unused item count.

    :param output: Raw stdout from vulture.
    :returns: Dict with ``items`` and ``status``.
    """
    items = len([line for line in output.strip().split("\n") if line.strip() and ":" in line])
    result: dict[str, Any] = {"items": items}
    result["status"] = "FAIL" if items > 0 else "PASS"
    return result


def parse_coverage_output(output: str, threshold: float = 100.0, **kwargs: Any) -> dict[str, Any]:
    """Parse pytest --cov / coverage output for coverage percentage.

    :param output: Raw stdout from coverage tool.
    :param threshold: Coverage percentage threshold for pass/fail.
    :returns: Dict with ``coverage``, ``threshold``, and ``status``.
    """
    result: dict[str, Any] = {"coverage": 0.0, "threshold": threshold}
    m = re.search(r"TOTAL\s+\d+\s+\d+\s+(\d+)%", output)
    if m:
        result["coverage"] = float(m.group(1))
    else:
        m = re.search(r"(\d+(?:\.\d+)?)%", output)
        if m:
            result["coverage"] = float(m.group(1))
    result["status"] = "PASS" if result["coverage"] >= threshold else "FAIL"
    return result


def parse_bandit_output(output: str, **kwargs: Any) -> dict[str, Any]:
    """Parse bandit output for issue count.

    :param output: Raw stdout from bandit.
    :returns: Dict with ``issues`` and ``status``.
    """
    result: dict[str, Any] = {"issues": 0}
    m = re.search(r"Total issues:\s*(\d+)", output)
    if m:
        result["issues"] = int(m.group(1))
    result["status"] = "FAIL" if result["issues"] > 0 else "PASS"
    return result


def parse_pip_audit_output(output: str, **kwargs: Any) -> dict[str, Any]:
    """Parse pip-audit output for vulnerability count.

    :param output: Raw stdout from pip-audit.
    :returns: Dict with ``vulnerabilities`` and ``status``.
    """
    result: dict[str, Any] = {"vulnerabilities": 0}
    m = re.search(r"Found (\d+) vulnerabilit", output)
    if m:
        result["vulnerabilities"] = int(m.group(1))
    if "No known vulnerabilities" in output or result["vulnerabilities"] == 0:
        result["status"] = "PASS"
    else:
        result["status"] = "FAIL"
    return result


def parse_radon_output(output: str, threshold: str = "C", **kwargs: Any) -> dict[str, Any]:
    """Parse radon complexity output.

    :param output: Raw stdout from radon.
    :param threshold: Grade threshold for pass/fail (A-F).
    :returns: Dict with ``average_complexity``, ``worst_grade``, ``threshold``, and ``status``.
    """
    grades_order = ["A", "B", "C", "D", "E", "F"]
    threshold_idx = grades_order.index(threshold) if threshold in grades_order else 2

    total_complexity = 0.0
    count = 0
    worst_grade = "A"
    for line in output.strip().split("\n"):
        parts = line.strip().split()
        if len(parts) >= 3:
            grade = parts[-2]
            try:
                complexity = float(parts[-1])
                total_complexity += complexity
                count += 1
                if grade in grades_order and grades_order.index(grade) > grades_order.index(worst_grade):
                    worst_grade = grade
            except ValueError:
                pass

    avg = total_complexity / count if count > 0 else 0.0
    result: dict[str, Any] = {
        "average_complexity": avg,
        "worst_grade": worst_grade,
        "threshold": threshold,
    }
    result["status"] = "FAIL" if grades_order.index(worst_grade) > threshold_idx else "PASS"
    return result


# ─── Parser resolution ──────────────────────────────────────────────────────


def _resolve_parser(parser_path: str) -> Callable[..., dict[str, Any]] | None:
    """Resolve a dotted path to a callable.

    :param parser_path: Dotted import path to the parser function.
    :returns: The resolved callable, or None if not found.
    """
    if not parser_path:
        return None
    module_path, _, func_name = parser_path.rpartition(".")
    if not module_path:
        return None
    try:
        import importlib
        module = importlib.import_module(module_path)
        return getattr(module, func_name)
    except (ImportError, AttributeError):
        return None


# ─── run_diagnostic action handler ──────────────────────────────────────────


def run_diagnostic(action: ActionSpec, context: TickContext) -> ActionResult:
    """Run a diagnostic command, parse output, write report + symlink.

    Expected action.params:
        - command: Shell command to run (supports template variables)
        - report_dir: Directory to write the report file
        - parser: Dotted path to a parser callable (optional)
        - report_name: Filename template (default: "report_{timestamp}.md")
        - parser_kwargs: Extra kwargs to pass to the parser (optional)

    :param action: Action spec with command, report_dir, and optional parser.
    :param context: Tick context with target and directories.
    :returns: Result with report path, exit code, status, and parsed data.
    """
    if context.dry_run:
        return ActionResult(success=True, dry_run=True)

    params = action.params
    command = params.get("command", "")
    report_dir = context.target_dir / params.get("report_dir", ".")
    parser_path = params.get("parser", "")
    report_name = params.get("report_name", "report_{timestamp}.md")
    report_title = params.get("report_title", "Diagnostic Report")
    parser_kwargs = params.get("parser_kwargs", {})

    # Format command with context variables
    variables = {
        "target": shlex.quote(context.target),
        "target_dir": shlex.quote(str(context.target_dir)),
        "workspace_dir": shlex.quote(str(context.workspace_dir)),
    }
    # target_config values that are full shell commands must NOT be quoted —
    # quoting turns the entire command string into a single path.
    _cmd_keys = frozenset({
        "test_cmd", "lint_cmd", "docstring_cmd", "typecheck_cmd",
        "coverage_cmd", "security_cmd", "deadcode_cmd", "build_cmd",
        "dep_audit_cmd",
    })
    for k, v in context.target_config.items():
        if k not in variables:
            if k in _cmd_keys:
                variables[k] = str(v)
            else:
                variables[k] = shlex.quote(str(v))
    command = format_template(command, variables)

    # Resolve timeout: action param → timeout_seconds → default 300s
    timeout = params.get("timeout", 300)
    if action.timeout_seconds is not None:
        timeout = action.timeout_seconds

    # Run command
    try:
        proc = subprocess.run(
            command,
            shell=True,  # nosec B602 - diagnostic commands come from trusted pipeline config
            cwd=str(context.target_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        stdout = proc.stdout
        stderr = proc.stderr
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        return ActionResult(
            success=False,
            stdout="",
            stderr=f"Command timed out: {command}",
        )
    except (OSError, subprocess.SubprocessError) as e:
        return ActionResult(success=False, stderr=str(e))

    # Parse output
    parsed: dict[str, Any] = {}
    parser = _resolve_parser(parser_path)
    if parser:
        try:
            parsed = parser(stdout, **parser_kwargs)
        except (ValueError, KeyError, IndexError, TypeError):
            parsed = {"parse_error": True}

    # Build report content
    timestamp = generate_timestamp()
    status = parsed.get("status", "UNKNOWN")
    report_lines = [
        f"# {report_title} — {status}",
        "",
        f"**Status**: {status}",
        f"**Command**: `{command}`",
        f"**Exit code**: {exit_code}",
        f"**Timestamp**: {timestamp}",
        "",
        "## Parsed Results",
        "",
    ]
    for key, value in parsed.items():
        if key != "status":
            report_lines.append(f"- **{key}**: {value}")
    report_lines.extend([
        "",
        "## stdout",
        "```",
        stdout,
        "```",
        "",
        "## stderr",
        "```",
        stderr,
        "```",
    ])
    content = "\n".join(report_lines)

    # Write report file
    if "{timestamp}" in report_name:
        report_name = report_name.replace("{timestamp}", timestamp)

    report_path = write_report(report_dir, report_name, content)

    # Create/update latest.md symlink
    update_latest_symlink(report_dir, "latest.md", report_name)

    return ActionResult(
        success=True,
        stdout=stdout,
        stderr=stderr,
        data={
            "report_path": str(report_path),
            "exit_code": exit_code,
            "status": status,
            "parsed": parsed,
        },
    )
