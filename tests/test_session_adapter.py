"""Tests for SWE GitHub session adapter (sync_session_mode pre_tick hook)."""

import json

from cronpypeline.plugins.swe_plugin import sync_session_mode


class TestSyncSessionMode:
    """Tests for sync_session_mode pre_tick hook."""

    def test_syncs_github_session_to_mode_file(self, tmp_path):
        """When github_session.json has 'active: true', mode_file gets 'github'."""
        target_dir = tmp_path / "repo"
        target_dir.mkdir()
        swe_dir = target_dir / ".SWE"
        swe_dir.mkdir()

        session_file = swe_dir / "github_session.json"
        session_file.write_text(json.dumps({
            "active": True,
            "session_id": "sess-123",
            "started_at": "2024-01-01T12:00:00",
        }))

        mode_file = tmp_path / "mode.json"

        context = {
            "target": "repo",
            "target_dir": str(target_dir),
            "workspace_dir": str(tmp_path),
            "target_config": {},
        }

        result = sync_session_mode(context, mode_file=str(mode_file))
        assert result is not False  # hook should not skip tick

        mode_data = json.loads(mode_file.read_text())
        assert mode_data["mode"] == "github"

    def test_inactive_session_sets_default_mode(self, tmp_path):
        """When github_session.json has 'active: false', mode_file gets 'default'."""
        target_dir = tmp_path / "repo"
        target_dir.mkdir()
        swe_dir = target_dir / ".SWE"
        swe_dir.mkdir()

        session_file = swe_dir / "github_session.json"
        session_file.write_text(json.dumps({
            "active": False,
        }))

        mode_file = tmp_path / "mode.json"

        context = {
            "target": "repo",
            "target_dir": str(target_dir),
            "workspace_dir": str(tmp_path),
            "target_config": {},
        }

        sync_session_mode(context, mode_file=str(mode_file))

        mode_data = json.loads(mode_file.read_text())
        assert mode_data["mode"] == "default"

    def test_no_session_file_sets_default_mode(self, tmp_path):
        """When no github_session.json exists, mode_file gets 'default'."""
        target_dir = tmp_path / "repo"
        target_dir.mkdir()

        mode_file = tmp_path / "mode.json"

        context = {
            "target": "repo",
            "target_dir": str(target_dir),
            "workspace_dir": str(tmp_path),
            "target_config": {},
        }

        sync_session_mode(context, mode_file=str(mode_file))

        mode_data = json.loads(mode_file.read_text())
        assert mode_data["mode"] == "default"

    def test_corrupt_session_file_sets_default_mode(self, tmp_path):
        """When github_session.json is corrupt, mode_file gets 'default'."""
        target_dir = tmp_path / "repo"
        target_dir.mkdir()
        swe_dir = target_dir / ".SWE"
        swe_dir.mkdir()

        session_file = swe_dir / "github_session.json"
        session_file.write_text("not valid json")

        mode_file = tmp_path / "mode.json"

        context = {
            "target": "repo",
            "target_dir": str(target_dir),
            "workspace_dir": str(tmp_path),
            "target_config": {},
        }

        sync_session_mode(context, mode_file=str(mode_file))

        mode_data = json.loads(mode_file.read_text())
        assert mode_data["mode"] == "default"

    def test_uses_target_config_mode_file(self, tmp_path):
        """mode_file path can come from target_config."""
        target_dir = tmp_path / "repo"
        target_dir.mkdir()
        swe_dir = target_dir / ".SWE"
        swe_dir.mkdir()

        session_file = swe_dir / "github_session.json"
        session_file.write_text(json.dumps({"active": True}))

        mode_file = tmp_path / "custom_mode.json"

        context = {
            "target": "repo",
            "target_dir": str(target_dir),
            "workspace_dir": str(tmp_path),
            "target_config": {"mode_file": str(mode_file)},
        }

        sync_session_mode(context)

        mode_data = json.loads(mode_file.read_text())
        assert mode_data["mode"] == "github"

    def test_returns_true_to_proceed(self, tmp_path):
        """The hook should return True (or non-False) to not skip the tick."""
        target_dir = tmp_path / "repo"
        target_dir.mkdir()

        mode_file = tmp_path / "mode.json"

        context = {
            "target": "repo",
            "target_dir": str(target_dir),
            "workspace_dir": str(tmp_path),
            "target_config": {},
        }

        result = sync_session_mode(context, mode_file=str(mode_file))
        assert result is not False
