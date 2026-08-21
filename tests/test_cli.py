"""Tests for cronpypeline.cli — argparse CLI entry point."""

import json

import pytest

from cronpypeline.cli import build_parser, main


class TestBuildParser:
    """Tests for argument parser construction."""

    def test_parser_requires_config(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_parser_with_config(self):
        parser = build_parser()
        args = parser.parse_args(["--config", "pipeline.json"])
        assert args.config == "pipeline.json"

    def test_parser_with_target(self):
        parser = build_parser()
        args = parser.parse_args(["--config", "p.json", "--target", "my-repo"])
        assert args.target == "my-repo"

    def test_parser_with_all(self):
        parser = build_parser()
        args = parser.parse_args(["--config", "p.json", "--all"])
        assert args.all is True

    def test_parser_with_dry_run(self):
        parser = build_parser()
        args = parser.parse_args(["--config", "p.json", "--dry-run"])
        assert args.dry_run is True

    def test_parser_with_verbose(self):
        parser = build_parser()
        args = parser.parse_args(["--config", "p.json", "--verbose"])
        assert args.verbose is True

    def test_parser_with_status(self):
        parser = build_parser()
        args = parser.parse_args(["--config", "p.json", "--status"])
        assert args.status is True

    def test_parser_with_reset_stage(self):
        parser = build_parser()
        args = parser.parse_args(["--config", "p.json", "--reset-stage", "A0"])
        assert args.reset_stage == "A0"

    def test_parser_with_reset_target(self):
        parser = build_parser()
        args = parser.parse_args(["--config", "p.json", "--reset-target", "my-repo"])
        assert args.reset_target == "my-repo"

    def test_verbose_short_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--config", "p.json", "-v"])
        assert args.verbose is True


class TestCLIMain:
    """Tests for CLI main() function."""

    def _make_config_file(self, tmp_path, workspace=None):
        workspace = workspace or tmp_path / "workspace"
        workspace.mkdir(exist_ok=True)
        config_data = {
            "name": "cli-test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo a"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        }
        config_file = tmp_path / "pipeline.json"
        config_file.write_text(json.dumps(config_data))
        return config_file, workspace

    def test_cli_executes_tick(self, tmp_path):
        config_file, workspace = self._make_config_file(tmp_path)
        (workspace / "my-repo").mkdir()
        exit_code = main(["--config", str(config_file), "--target", "my-repo"])
        assert exit_code == 0
        assert (workspace / "my-repo" / "a.md").exists()

    def test_cli_dry_run(self, tmp_path):
        config_file, workspace = self._make_config_file(tmp_path)
        (workspace / "my-repo").mkdir()
        exit_code = main(["--config", str(config_file), "--target", "my-repo", "--dry-run"])
        assert exit_code == 0
        assert not (workspace / "my-repo" / "a.md").exists()

    def test_cli_verbose(self, tmp_path):
        config_file, workspace = self._make_config_file(tmp_path)
        (workspace / "my-repo").mkdir()
        exit_code = main(["--config", str(config_file), "--target", "my-repo", "--verbose"])
        assert exit_code == 0

    def test_cli_status(self, tmp_path):
        config_file, workspace = self._make_config_file(tmp_path)
        (workspace / "my-repo").mkdir()
        exit_code = main(["--config", str(config_file), "--target", "my-repo", "--status"])
        assert exit_code == 0

    def test_cli_all_targets(self, tmp_path):
        config_file, workspace = self._make_config_file(tmp_path)
        (workspace / "repo1").mkdir()
        (workspace / "repo2").mkdir()
        # Need targets config
        config_data = json.loads(config_file.read_text())
        config_data["targets"] = {"type": "static", "items": ["repo1", "repo2"]}
        config_file.write_text(json.dumps(config_data))

        exit_code = main(["--config", str(config_file), "--all"])
        assert exit_code == 0
        assert (workspace / "repo1" / "a.md").exists()
        assert (workspace / "repo2" / "a.md").exists()

    def test_cli_no_work_returns_zero(self, tmp_path):
        config_file, workspace = self._make_config_file(tmp_path)
        (workspace / "my-repo").mkdir()
        (workspace / "my-repo" / "a.md").touch()
        exit_code = main(["--config", str(config_file), "--target", "my-repo"])
        assert exit_code == 0

    def test_cli_missing_config_file(self, tmp_path):
        exit_code = main(["--config", str(tmp_path / "nonexistent.json")])
        assert exit_code != 0

    def test_cli_invalid_config_returns_error(self, tmp_path):
        """Config file with invalid JSON should return error code 1."""
        config_file = tmp_path / "bad.json"
        config_file.write_text("{invalid json")
        exit_code = main(["--config", str(config_file)])
        assert exit_code == 1

    def test_cli_reset_stage(self, tmp_path):
        """--reset-stage should delete the completion marker."""
        config_file, workspace = self._make_config_file(tmp_path)
        (workspace / "my-repo").mkdir()
        (workspace / "my-repo" / "a.md").touch()
        exit_code = main(["--config", str(config_file), "--target", "my-repo", "--reset-stage", "A0"])
        assert exit_code == 0
        assert not (workspace / "my-repo" / "a.md").exists()

    def test_cli_reset_stage_no_target_defaults_to_dot(self, tmp_path):
        """--reset-stage without --target should use '.' as target."""
        config_file, workspace = self._make_config_file(tmp_path)
        (workspace / "a.md").touch()
        exit_code = main(["--config", str(config_file), "--reset-stage", "A0"])
        assert exit_code == 0
        assert not (workspace / "a.md").exists()

    def test_cli_reset_stage_unknown_stage(self, tmp_path):
        """--reset-stage with unknown stage ID should still return 0."""
        config_file, workspace = self._make_config_file(tmp_path)
        (workspace / "my-repo").mkdir()
        exit_code = main(["--config", str(config_file), "--target", "my-repo", "--reset-stage", "UNKNOWN"])
        assert exit_code == 0

    def test_cli_reset_target(self, tmp_path):
        """--reset-target should delete all markers for the target."""
        config_file, workspace = self._make_config_file(tmp_path)
        (workspace / "my-repo").mkdir()
        (workspace / "my-repo" / "a.md").touch()
        exit_code = main(["--config", str(config_file), "--reset-target", "my-repo"])
        assert exit_code == 0
        assert not (workspace / "my-repo" / "a.md").exists()

    def test_cli_all_targets_action_failed_returns_error(self, tmp_path):
        """--all with a failing action should return error code 1."""
        config_file, workspace = self._make_config_file(tmp_path)
        config_data = json.loads(config_file.read_text())
        config_data["stages"][0]["action"]["params"]["command"] = "false"
        config_data["targets"] = {"type": "static", "items": ["repo1"]}
        config_file.write_text(json.dumps(config_data))
        (workspace / "repo1").mkdir()
        exit_code = main(["--config", str(config_file), "--all"])
        assert exit_code == 1

    def test_cli_single_tick_action_failed_returns_error(self, tmp_path):
        """Single tick with a failing action should return error code 1."""
        config_file, workspace = self._make_config_file(tmp_path)
        config_data = json.loads(config_file.read_text())
        config_data["stages"][0]["action"]["params"]["command"] = "false"
        config_file.write_text(json.dumps(config_data))
        (workspace / "my-repo").mkdir()
        exit_code = main(["--config", str(config_file), "--target", "my-repo"])
        assert exit_code == 1

    def test_cli_status_all_targets(self, tmp_path):
        """--status without --target should show status for all targets."""
        config_file, workspace = self._make_config_file(tmp_path)
        (workspace / "my-repo").mkdir()
        exit_code = main(["--config", str(config_file), "--status"])
        assert exit_code == 0

    def test_cli_module_guard_calls_sys_exit(self, tmp_path):
        """The if __name__ == '__main__' guard should call sys.exit(main())."""
        from pathlib import Path
        from unittest.mock import patch

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()
        config_data = {
            "name": "test",
            "workspace_dir": str(workspace),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step",
                    "trigger": {"type": "file_missing", "path": "a.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                    "markers": {"completion": {"type": "file", "name": "a.md"}},
                },
            ],
        }
        config_file = tmp_path / "pipeline.json"
        config_file.write_text(json.dumps(config_data))

        cli_path = Path(__file__).parent.parent / "cronpypeline" / "cli.py"
        source = cli_path.read_text()

        with patch("sys.exit") as mock_exit, \
             patch("sys.argv", ["cronpypeline", "--config", str(config_file), "--target", "my-repo"]):
            exec(compile(source, str(cli_path), "exec"), {"__name__": "__main__"})
            mock_exit.assert_called_once_with(0)
