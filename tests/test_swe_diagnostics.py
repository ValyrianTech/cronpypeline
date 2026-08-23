"""Tests for SWE diagnostic report action handler and parsers."""

from pathlib import Path

from cronpypeline.actions import TickContext
from cronpypeline.config import ActionSpec, ActionType
from cronpypeline.plugins.swe_diagnostics import (
    parse_bandit_output,
    parse_coverage_output,
    parse_interrogate_output,
    parse_mypy_output,
    parse_pip_audit_output,
    parse_pydocstyle_output,
    parse_pytest_output,
    parse_radon_output,
    parse_ruff_output,
    parse_vulture_output,
    run_diagnostic,
)

# ─── Parser tests ───────────────────────────────────────────────────────────


class TestParsePytestOutput:
    """Tests for parse_pytest_output."""

    def test_parse_passing_tests(self):
        output = "===== 5 passed in 2.3s ====="
        result = parse_pytest_output(output)
        assert result["passed"] == 5
        assert result["failed"] == 0
        assert result["errors"] == 0
        assert result["status"] == "PASS"

    def test_parse_failing_tests(self):
        output = "===== 3 failed, 7 passed in 5.0s ====="
        result = parse_pytest_output(output)
        assert result["passed"] == 7
        assert result["failed"] == 3
        assert result["status"] == "FAIL"

    def test_parse_errors(self):
        output = "===== 2 errors, 4 passed in 3.0s ====="
        result = parse_pytest_output(output)
        assert result["errors"] == 2
        assert result["passed"] == 4
        assert result["status"] == "FAIL"

    def test_parse_no_tests(self):
        output = "no tests ran"
        result = parse_pytest_output(output)
        assert result["status"] == "PASS"
        assert result["passed"] == 0
        assert result["failed"] == 0

    def test_parse_skipped(self):
        output = "===== 1 passed, 2 skipped in 1.0s ====="
        result = parse_pytest_output(output)
        assert result["passed"] == 1
        assert result["skipped"] == 2
        assert result["status"] == "PASS"


class TestParseRuffOutput:
    """Tests for parse_ruff_output."""

    def test_parse_no_issues(self):
        output = "All checks passed!"
        result = parse_ruff_output(output)
        assert result["errors"] == 0
        assert result["fixable"] == 0
        assert result["status"] == "PASS"

    def test_parse_with_issues(self):
        output = "Found 5 errors.\n[+] 3 fixable with --fix"
        result = parse_ruff_output(output)
        assert result["errors"] == 5
        assert result["fixable"] == 3
        assert result["status"] == "FAIL"

    def test_parse_empty_output(self):
        result = parse_ruff_output("")
        assert result["status"] == "PASS"
        assert result["errors"] == 0


class TestParseMypyOutput:
    """Tests for parse_mypy_output."""

    def test_parse_no_errors(self):
        output = "Success: no issues found in 10 source files"
        result = parse_mypy_output(output)
        assert result["errors"] == 0
        assert result["status"] == "PASS"

    def test_parse_with_errors(self):
        output = "Found 3 errors in 2 files"
        result = parse_mypy_output(output)
        assert result["errors"] == 3
        assert result["status"] == "FAIL"


class TestParseInterrogateOutput:
    """Tests for parse_interrogate_output."""

    def test_parse_passed(self):
        output = (
            "| TOTAL                  | 100   |  13   |  87   |  87.0% |\n"
            "------- RESULT: PASSED (minimum: 80.0%, actual: 87.0%) -------"
        )
        result = parse_interrogate_output(output)
        assert result["status"] == "PASS"
        assert result["coverage"] == 87.0
        assert result["covered"] == 87
        assert result["missing"] == 13
        assert result["total"] == 100

    def test_parse_failed(self):
        output = (
            "| TOTAL                  | 100   |  50   |  50   |  50.0% |\n"
            "------- RESULT: FAILED (minimum: 80.0%, actual: 50.0%) -------"
        )
        result = parse_interrogate_output(output)
        assert result["status"] == "FAIL"
        assert result["coverage"] == 50.0
        assert result["covered"] == 50
        assert result["missing"] == 50

    def test_parse_no_result_footer(self):
        output = "| TOTAL                  | 100   |  13   |  87   |  87.0% |"
        result = parse_interrogate_output(output)
        assert result["status"] == "FAIL"
        assert result["coverage"] == 87.0


class TestParsePydocstyleOutput:
    """Tests for parse_pydocstyle_output."""

    def test_parse_no_issues(self):
        result = parse_pydocstyle_output("")
        assert result["errors"] == 0
        assert result["status"] == "PASS"

    def test_parse_with_violations(self):
        output = "src/main.py:1 at module level:\n  D100: Missing docstring\nsrc/utils.py:42:\n  D205: 1 blank line required"
        result = parse_pydocstyle_output(output)
        assert result["errors"] == 2
        assert result["status"] == "FAIL"


class TestParseVultureOutput:
    """Tests for parse_vulture_output."""

    def test_parse_no_items(self):
        result = parse_vulture_output("")
        assert result["items"] == 0
        assert result["status"] == "PASS"

    def test_parse_with_items(self):
        output = "src/main.py:42: unused function 'foo' (60%)\nsrc/utils.py:10: unused variable 'bar' (20%)"
        result = parse_vulture_output(output)
        assert result["items"] == 2
        assert result["status"] == "FAIL"


class TestParseCoverageOutput:
    """Tests for parse_coverage_output."""

    def test_parse_full_coverage(self):
        output = "TOTAL 100%"
        result = parse_coverage_output(output)
        assert result["coverage"] == 100.0
        assert result["status"] == "PASS"

    def test_parse_partial_coverage(self):
        output = "Name    Stmts   Miss  Cover\nTOTAL    50     10    80%"
        result = parse_coverage_output(output)
        assert result["coverage"] == 80.0
        assert result["status"] == "FAIL"

    def test_parse_with_threshold(self):
        output = "TOTAL    50     5    90%"
        result = parse_coverage_output(output, threshold=95.0)
        assert result["coverage"] == 90.0
        assert result["status"] == "FAIL"

    def test_parse_meets_threshold(self):
        output = "TOTAL    50     0    100%"
        result = parse_coverage_output(output, threshold=95.0)
        assert result["coverage"] == 100.0
        assert result["status"] == "PASS"


class TestParseBanditOutput:
    """Tests for parse_bandit_output."""

    def test_parse_no_issues(self):
        output = "Total issues: 0"
        result = parse_bandit_output(output)
        assert result["issues"] == 0
        assert result["status"] == "PASS"

    def test_parse_with_issues(self):
        output = "Total issues: 3\nHigh: 1\nMedium: 2"
        result = parse_bandit_output(output)
        assert result["issues"] == 3
        assert result["status"] == "FAIL"


class TestParsePipAuditOutput:
    """Tests for parse_pip_audit_output."""

    def test_parse_no_vulns(self):
        output = "No known vulnerabilities found"
        result = parse_pip_audit_output(output)
        assert result["vulnerabilities"] == 0
        assert result["status"] == "PASS"

    def test_parse_with_vulns(self):
        output = "Found 2 vulnerabilities"
        result = parse_pip_audit_output(output)
        assert result["vulnerabilities"] == 2
        assert result["status"] == "FAIL"


class TestParseRadonOutput:
    """Tests for parse_radon_output."""

    def test_parse_complexity(self):
        output = "src/main.py A 1.0\nsrc/utils.py B 3.5"
        result = parse_radon_output(output)
        assert result["status"] == "PASS"
        assert "average_complexity" in result

    def test_parse_high_complexity(self):
        output = "src/main.py F 15.0"
        result = parse_radon_output(output, threshold="C")
        assert result["status"] == "FAIL"


# ─── run_diagnostic tests ──────────────────────────────────────────────────


class TestRunDiagnostic:
    """Tests for the run_diagnostic custom action handler."""

    def test_runs_command_and_writes_report(self, tmp_path):
        """run_diagnostic should run command, parse output, write report + symlink."""
        report_dir = tmp_path / ".SWE" / "reports" / "test-infra"
        target_dir = tmp_path / "repo"
        target_dir.mkdir()

        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "callable": "cronpypeline.plugins.swe_diagnostics.run_diagnostic",
                "command": "echo '===== 5 passed in 2.3s ====='",
                "report_dir": str(report_dir),
                "parser": "cronpypeline.plugins.swe_diagnostics.parse_pytest_output",
                "report_name": "test_report_{timestamp}.md",
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)

        result = run_diagnostic(action, ctx)

        assert result.success is True
        assert "report_path" in result.data
        report_path = Path(result.data["report_path"])
        assert report_path.exists()
        content = report_path.read_text()
        assert "PASS" in content or "passed" in content

        # latest.md symlink should exist
        latest = report_dir / "latest.md"
        assert latest.is_symlink()

    def test_dry_run_skips_execution(self, tmp_path):
        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "command": "echo test",
                "report_dir": str(tmp_path / "reports"),
                "parser": "cronpypeline.plugins.swe_diagnostics.parse_pytest_output",
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=True)

        result = run_diagnostic(action, ctx)
        assert result.success is True
        assert result.dry_run is True

    def test_failed_command_still_writes_report(self, tmp_path):
        """Even if the command fails (non-zero exit), a report should be written."""
        report_dir = tmp_path / ".SWE" / "reports" / "lint"
        target_dir = tmp_path / "repo"
        target_dir.mkdir()

        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "command": "echo 'Found 3 errors.' && false",
                "report_dir": str(report_dir),
                "parser": "cronpypeline.plugins.swe_diagnostics.parse_ruff_output",
                "report_name": "lint_report_{timestamp}.md",
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)

        result = run_diagnostic(action, ctx)

        assert result.success is True  # diagnostic itself succeeded
        report_path = Path(result.data["report_path"])
        assert report_path.exists()
        content = report_path.read_text()
        assert "FAIL" in content

    def test_no_parser_writes_raw_output(self, tmp_path):
        """When no parser is specified, raw stdout should be in the report."""
        report_dir = tmp_path / "reports"
        target_dir = tmp_path / "repo"
        target_dir.mkdir()

        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "command": "echo 'raw output here'",
                "report_dir": str(report_dir),
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)

        result = run_diagnostic(action, ctx)
        assert result.success is True
        content = Path(result.data["report_path"]).read_text()
        assert "raw output here" in content

    def test_report_header_includes_title_and_status(self, tmp_path):
        """Report first line should be '# {title} — {status}' for fix-agent triggers."""
        report_dir = tmp_path / "reports"
        target_dir = tmp_path / "repo"
        target_dir.mkdir()

        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "command": "echo 'Found 3 errors.'",
                "report_dir": str(report_dir),
                "parser": "cronpypeline.plugins.swe_diagnostics.parse_ruff_output",
                "report_title": "Lint Check",
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)

        result = run_diagnostic(action, ctx)
        content = Path(result.data["report_path"]).read_text()
        first_line = content.splitlines()[0]
        assert first_line == "# Lint Check — FAIL"

    def test_report_default_title_when_not_specified(self, tmp_path):
        """Report should use 'Diagnostic Report' as default title."""
        report_dir = tmp_path / "reports"
        target_dir = tmp_path / "repo"
        target_dir.mkdir()

        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "command": "echo test",
                "report_dir": str(report_dir),
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)

        result = run_diagnostic(action, ctx)
        first_line = Path(result.data["report_path"]).read_text().splitlines()[0]
        assert first_line.startswith("# Diagnostic Report —")

    def test_report_includes_metadata(self, tmp_path):
        """Report should include command, exit code, and timestamp metadata."""
        report_dir = tmp_path / "reports"
        target_dir = tmp_path / "repo"
        target_dir.mkdir()

        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "command": "echo '===== 5 passed ====='",
                "report_dir": str(report_dir),
                "parser": "cronpypeline.plugins.swe_diagnostics.parse_pytest_output",
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False)

        result = run_diagnostic(action, ctx)
        content = Path(result.data["report_path"]).read_text()
        assert "exit_code" in content.lower() or "exit" in content.lower()


class TestParseCoverageFallback:
    """Tests for coverage parser fallback regex."""

    def test_parse_coverage_fallback_regex(self):
        """When TOTAL line not found, should use fallback percentage regex."""
        output = "Coverage: 75.5%"
        result = parse_coverage_output(output)
        assert result["coverage"] == 75.5
        assert result["status"] == "FAIL"


class TestParseBanditNoMatch:
    """Tests for bandit parser with no match."""

    def test_parse_no_match(self):
        """When 'Total issues' not found, should default to 0."""
        output = "Some other bandit output"
        result = parse_bandit_output(output)
        assert result["issues"] == 0
        assert result["status"] == "PASS"


class TestParseRadonEdgeCases:
    """Tests for radon parser edge cases."""

    def test_line_with_less_than_three_parts(self):
        """Lines with fewer than 3 parts should be skipped."""
        output = "src/main.py\nsrc/utils.py A 2.0"
        result = parse_radon_output(output)
        assert result["average_complexity"] == 2.0
        assert result["status"] == "PASS"

    def test_value_error_on_complexity(self):
        """Lines where complexity can't be parsed should be skipped."""
        output = "src/main.py A not_a_number"
        result = parse_radon_output(output)
        assert result["average_complexity"] == 0.0
        assert result["status"] == "PASS"


class TestResolveParser:
    """Tests for _resolve_parser."""

    def test_empty_path_returns_none(self):
        from cronpypeline.plugins.swe_diagnostics import _resolve_parser
        assert _resolve_parser("") is None

    def test_no_module_path_returns_none(self):
        from cronpypeline.plugins.swe_diagnostics import _resolve_parser
        assert _resolve_parser("nofunc") is None

    def test_import_error_returns_none(self):
        from cronpypeline.plugins.swe_diagnostics import _resolve_parser
        assert _resolve_parser("nonexistent_module_xyz.func") is None

    def test_attribute_error_returns_none(self):
        from cronpypeline.plugins.swe_diagnostics import _resolve_parser
        assert _resolve_parser("cronpypeline.plugins.swe_diagnostics.nonexistent_func") is None


class TestRunDiagnosticEdgeCases:
    """Tests for run_diagnostic edge cases."""

    def test_target_config_variables_in_command(self, tmp_path):
        """Target config keys should be available as template variables in command."""
        report_dir = tmp_path / "reports"
        target_dir = tmp_path / "repo"
        target_dir.mkdir()

        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "command": "echo {test_cmd}",
                "report_dir": str(report_dir),
            },
        )
        ctx = TickContext(
            target="repo",
            workspace_dir=tmp_path,
            dry_run=False,
            verbose=False,
            target_config={"test_cmd": "pytest"},
        )

        result = run_diagnostic(action, ctx)
        assert result.success is True
        content = Path(result.data["report_path"]).read_text()
        assert "pytest" in content

    def test_command_timeout(self, tmp_path):
        """Command timeout should return failure with timeout message."""
        report_dir = tmp_path / "reports"
        target_dir = tmp_path / "repo"
        target_dir.mkdir()

        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "command": "sleep 300",
                "report_dir": str(report_dir),
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False, verbose=False)

        import subprocess as sp
        from unittest.mock import patch
        with patch("subprocess.run", side_effect=sp.TimeoutExpired(cmd="sleep 300", timeout=300)):
            result = run_diagnostic(action, ctx)

        assert result.success is False
        assert "timed out" in result.stderr.lower()

    def test_os_error_from_command(self, tmp_path):
        """OSError from command should return failure."""
        report_dir = tmp_path / "reports"
        target_dir = tmp_path / "repo"
        target_dir.mkdir()

        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "command": "echo test",
                "report_dir": str(report_dir),
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False, verbose=False)

        from unittest.mock import patch
        with patch("subprocess.run", side_effect=OSError("command failed")):
            result = run_diagnostic(action, ctx)

        assert result.success is False
        assert "command failed" in result.stderr

    def test_parser_exception_returns_parse_error(self, tmp_path):
        """When parser raises an exception, parsed should have parse_error."""
        report_dir = tmp_path / "reports"
        target_dir = tmp_path / "repo"
        target_dir.mkdir()

        # Create a module with a bad parser
        import sys
        bad_parser_mod = tmp_path / "bad_parser_mod.py"
        bad_parser_mod.write_text("""
def bad_parser(output):
    raise ValueError("bad parser")
""")
        sys.path.insert(0, str(tmp_path))
        try:
            action = ActionSpec(
                type=ActionType.CUSTOM,
                params={
                    "command": "echo test",
                    "report_dir": str(report_dir),
                    "parser": "bad_parser_mod.bad_parser",
                },
            )
            ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False, verbose=False)

            result = run_diagnostic(action, ctx)
            assert result.success is True
            content = Path(result.data["report_path"]).read_text()
            assert "parse_error" in content or "UNKNOWN" in content
        finally:
            sys.path.remove(str(tmp_path))
            if "bad_parser_mod" in sys.modules:
                del sys.modules["bad_parser_mod"]

    def test_report_name_without_timestamp(self, tmp_path):
        """Report name without {timestamp} should be used as-is."""
        report_dir = tmp_path / "reports"
        target_dir = tmp_path / "repo"
        target_dir.mkdir()

        action = ActionSpec(
            type=ActionType.CUSTOM,
            params={
                "command": "echo test",
                "report_dir": str(report_dir),
                "report_name": "fixed_name.md",
            },
        )
        ctx = TickContext(target="repo", workspace_dir=tmp_path, dry_run=False, verbose=False)

        result = run_diagnostic(action, ctx)
        assert result.success is True
        report_path = Path(result.data["report_path"])
        assert report_path.name == "fixed_name.md"
