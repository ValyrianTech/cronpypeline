"""Tests for cronpypeline.__main__ — module entry point."""

import subprocess
import sys
from unittest.mock import patch


class TestMainEntryPoint:
    """Tests for __main__.py entry point."""

    def test_main_returns_zero_on_success(self, tmp_path):
        """Running python -m cronpypeline with valid config should exit 0."""
        import json

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "my-repo").mkdir()

        config_data = {
            "name": "test",
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

        result = subprocess.run(
            [sys.executable, "-m", "cronpypeline", "--config", str(config_file), "--target", "my-repo"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert (workspace / "my-repo" / "a.md").exists()

    def test_main_returns_nonzero_on_missing_config(self, tmp_path):
        """Running with a missing config file should exit non-zero."""
        result = subprocess.run(
            [sys.executable, "-m", "cronpypeline", "--config", str(tmp_path / "nonexistent.json")],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0

    def test_main_module_calls_sys_exit(self):
        """Importing __main__ should call sys.exit with main()'s return value."""
        from pathlib import Path

        main_path = Path(__file__).parent.parent / "cronpypeline" / "__main__.py"
        source = main_path.read_text()

        with patch("sys.exit") as mock_exit, \
             patch("cronpypeline.cli.main", return_value=0) as mock_main:
            exec(compile(source, str(main_path), "exec"), {"__name__": "__main__"})
            mock_main.assert_called_once()
            mock_exit.assert_called_once_with(0)
