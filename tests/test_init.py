"""Tests for the optional manual init/reinit feature.

Covers InitConfig parsing, Pipeline.run_init, the --init/--reinit CLI flags,
the standalone initialize_repo helper, and the SWE run_repo_init callable.
"""

import runpy
import subprocess
import sys
from pathlib import Path

import pytest

import cronpypeline.pipeline as pipeline_mod
from cronpypeline.actions import ActionSpec, TickContext
from cronpypeline.cli import build_parser, main
from cronpypeline.config import InitConfig, PipelineConfig, TargetSpec, TargetType
from cronpypeline.pipeline import Pipeline, TickResult, TickResultStatus
from cronpypeline.plugins import initialize_repo as ir
from cronpypeline.plugins import swe_plugin
from cronpypeline.plugins.issue_fix import _repo_dir_component

# ─── InitConfig parsing ──────────────────────────────────────────────────────


class TestInitConfig:
    def test_from_dict_with_marker(self):
        cfg = InitConfig.from_dict({
            "action": {"type": "command", "params": {"command": "echo hi"}},
            "marker": ".SWE/init_complete.marker",
        })
        assert cfg.action.type.value == "command"
        assert cfg.marker == ".SWE/init_complete.marker"

    def test_from_dict_without_marker(self):
        cfg = InitConfig.from_dict({
            "action": {"type": "command", "params": {"command": "echo hi"}},
        })
        assert cfg.marker is None

    def test_pipeline_config_parses_init(self):
        config = PipelineConfig.from_dict({
            "name": "p",
            "workspace_dir": "/ws",
            "stages": [],
            "init": {
                "action": {"type": "custom", "params": {"callable": "mod.fn"}},
                "marker": "init.marker",
            },
        })
        assert config.init is not None
        assert config.init.action.params["callable"] == "mod.fn"
        assert config.init.marker == "init.marker"

    def test_pipeline_config_no_init(self):
        config = PipelineConfig.from_dict({
            "name": "p",
            "workspace_dir": "/ws",
            "stages": [],
        })
        assert config.init is None


# ─── Pipeline.run_init ───────────────────────────────────────────────────────


def _make_init_config(workspace, action, marker=None):
    data = {
        "name": "init-test",
        "workspace_dir": str(workspace),
        "stages": [],
        "init": {"action": action},
    }
    if marker is not None:
        data["init"]["marker"] = marker
    return PipelineConfig.from_dict(data)


class TestRunInit:
    def test_make_init_config_with_marker(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = _make_init_config(
            workspace,
            {"type": "command", "params": {"command": "echo hi"}},
            marker="m.marker",
        )
        assert config.init.marker == "m.marker"

    def test_resolve_target_config_raises_returns_empty(self, tmp_path, monkeypatch):
        def boom(spec):
            raise ValueError("nope")

        monkeypatch.setattr(pipeline_mod, "load_targets_with_config", boom)
        config = PipelineConfig.from_dict({
            "name": "p",
            "workspace_dir": str(tmp_path),
            "stages": [],
        })
        pipeline = Pipeline(config)
        assert pipeline._resolve_target_config("x") == {}

    def test_resolve_target_config_matches_target(self, tmp_path, monkeypatch):
        class FakeTarget:
            def __init__(self, name, config):
                self.name = name
                self.config = config

        monkeypatch.setattr(
            pipeline_mod, "load_targets_with_config",
            lambda spec: [FakeTarget("x", {"a": 1})],
        )
        config = PipelineConfig.from_dict({
            "name": "p",
            "workspace_dir": str(tmp_path),
            "stages": [],
        })
        pipeline = Pipeline(config)
        assert pipeline._resolve_target_config("x") == {"a": 1}

    def test_run_init_lock_failed(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()
        config = _make_init_config(
            workspace,
            {"type": "command", "params": {"command": "echo hi"}},
        )
        pipeline = Pipeline(config)

        class FakeLock:
            dry_run = False

            def acquire(self):
                return False

        pipeline.lock = FakeLock()
        result = pipeline.run_init("repo1")
        assert result.status == TickResultStatus.LOCK_FAILED

    def test_no_init_config_returns_action_failed(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = PipelineConfig.from_dict({
            "name": "p",
            "workspace_dir": str(workspace),
            "stages": [],
        })
        pipeline = Pipeline(config)
        result = pipeline.run_init("repo1")
        assert result.status == TickResultStatus.ACTION_FAILED
        assert "No init action" in result.message

    def test_executes_command_action(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()
        config = _make_init_config(
            workspace,
            {"type": "command", "params": {"command": "echo init-ran"}},
        )
        pipeline = Pipeline(config)
        result = pipeline.run_init("repo1")
        assert result.status == TickResultStatus.ACTION_EXECUTED
        assert "init-ran" in result.stdout

    def test_dry_run_does_not_execute(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()
        config = _make_init_config(
            workspace,
            {"type": "command", "params": {"command": "echo init-ran"}},
        )
        pipeline = Pipeline(config)
        result = pipeline.run_init("repo1", dry_run=True)
        assert result.status == TickResultStatus.DRY_RUN
        assert "init-ran" not in result.stdout

    def test_failing_action_returns_action_failed(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()
        config = _make_init_config(
            workspace,
            {"type": "command", "params": {"command": "false"}},
        )
        pipeline = Pipeline(config)
        result = pipeline.run_init("repo1")
        assert result.status == TickResultStatus.ACTION_FAILED

    def test_invalid_target_raises(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = _make_init_config(
            workspace,
            {"type": "command", "params": {"command": "echo hi"}},
        )
        pipeline = Pipeline(config)
        with pytest.raises(ValueError):
            pipeline.run_init("../escape")

    def test_force_flag_passed_to_action(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "repo1").mkdir()
        module_body = (
            "from cronpypeline.actions import ActionResult\n"
            "\n"
            "def record(action, context):\n"
            "    (context.target_dir / 'force.txt').write_text(str(context.force))\n"
            "    return ActionResult(success=True)\n"
        )
        (tmp_path / "init_mod.py").write_text(module_body)
        sys.path.insert(0, str(tmp_path))
        try:
            config = _make_init_config(
                workspace,
                {"type": "custom", "params": {"callable": "init_mod.record"}},
            )
            pipeline = Pipeline(config)

            pipeline.run_init("repo1", force=False)
            assert (workspace / "repo1" / "force.txt").read_text() == "False"

            pipeline.run_init("repo1", force=True)
            assert (workspace / "repo1" / "force.txt").read_text() == "True"
        finally:
            sys.path.remove(str(tmp_path))
            sys.modules.pop("init_mod", None)


# ─── CLI flags ───────────────────────────────────────────────────────────────


class TestCLIInitFlags:
    def test_parser_with_init(self):
        parser = build_parser()
        args = parser.parse_args(["--config", "p.json", "--init"])
        assert args.init is True
        assert args.reinit is False

    def test_parser_with_reinit(self):
        parser = build_parser()
        args = parser.parse_args(["--config", "p.json", "--reinit"])
        assert args.reinit is True

    def _write_config(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config_data = {
            "name": "cli-init",
            "workspace_dir": str(workspace),
            "init": {
                "action": {"type": "command", "params": {"command": "touch init_done.txt"}},
                "marker": "init.marker",
            },
            "stages": [],
        }
        config_file = tmp_path / "pipeline.json"
        config_file.write_text(__import__("json").dumps(config_data))
        return config_file, workspace

    def test_init_requires_target(self, tmp_path):
        config_file, _workspace = self._write_config(tmp_path)
        assert main(["--config", str(config_file), "--init"]) == 1

    def test_init_runs_action(self, tmp_path):
        config_file, workspace = self._write_config(tmp_path)
        exit_code = main(["--config", str(config_file), "--init", "--target", "repo1"])
        assert exit_code == 0
        assert (workspace / "repo1" / "init_done.txt").exists()

    def test_reinit_passes_force(self, tmp_path, monkeypatch):
        config_file, _workspace = self._write_config(tmp_path)
        captured = {}

        def fake_run_init(self, target, force=False, dry_run=False, verbose=False):
            captured["target"] = target
            captured["force"] = force
            return TickResult(
                target=target,
                stage_id=None,
                status=TickResultStatus.ACTION_EXECUTED,
                message="ok",
            )

        monkeypatch.setattr(Pipeline, "run_init", fake_run_init)
        exit_code = main(["--config", str(config_file), "--reinit", "--target", "repo1"])
        assert exit_code == 0
        assert captured == {"target": "repo1", "force": True}

    def test_init_run_init_raises_value_error(self, tmp_path, monkeypatch):
        config_file, _workspace = self._write_config(tmp_path)

        def fake_run_init(self, target, force=False, dry_run=False, verbose=False):
            raise ValueError("bad target")

        monkeypatch.setattr(Pipeline, "run_init", fake_run_init)
        exit_code = main(["--config", str(config_file), "--init", "--target", "repo1"])
        assert exit_code == 1

    def test_init_action_failed_returns_one(self, tmp_path, monkeypatch):
        config_file, _workspace = self._write_config(tmp_path)

        def fake_run_init(self, target, force=False, dry_run=False, verbose=False):
            return TickResult(
                target=target,
                stage_id=None,
                status=TickResultStatus.ACTION_FAILED,
                message="boom",
            )

        monkeypatch.setattr(Pipeline, "run_init", fake_run_init)
        exit_code = main(["--config", str(config_file), "--init", "--target", "repo1"])
        assert exit_code == 1


# ─── Standalone initialize_repo helper ───────────────────────────────────────


class TestInitializeRepoScript:
    def test_default_paths_defaults(self, monkeypatch):
        for key in ("SWE_DATA_DIR", "SWE_REPOS_DIR", "SWE_TASKS_DIR", "SWE_REGISTRY_FILE"):
            monkeypatch.delenv(key, raising=False)
        repos, tasks, registry = ir.default_paths()
        assert repos == Path("/spellbook_data/Serendipity/swe/workspace/repos")
        assert tasks == Path("/spellbook_data/Serendipity/swe/workspace/tasks")
        assert registry == Path("/spellbook_data/Serendipity/swe/repos.json")

    def test_default_paths_env_data_dir(self, monkeypatch):
        monkeypatch.setenv("SWE_DATA_DIR", "/data")
        for key in ("SWE_REPOS_DIR", "SWE_TASKS_DIR", "SWE_REGISTRY_FILE"):
            monkeypatch.delenv(key, raising=False)
        repos, tasks, registry = ir.default_paths()
        assert repos == Path("/data/Serendipity/swe/workspace/repos")
        assert tasks == Path("/data/Serendipity/swe/workspace/tasks")
        assert registry == Path("/data/Serendipity/swe/repos.json")

    def test_default_paths_explicit_env(self, monkeypatch):
        monkeypatch.setenv("SWE_REPOS_DIR", "/r")
        monkeypatch.setenv("SWE_TASKS_DIR", "/t")
        monkeypatch.setenv("SWE_REGISTRY_FILE", "/reg.json")
        assert ir.default_paths() == (Path("/r"), Path("/t"), Path("/reg.json"))

    def test_derive_repo_name_https(self):
        assert ir.derive_repo_name("https://github.com/org/myrepo.git") == "myrepo"

    def test_derive_repo_name_ssh(self):
        assert ir.derive_repo_name("git@github.com:org/myrepo.git") == "myrepo"

    def test_resolve_python_explicit_wins(self, monkeypatch):
        monkeypatch.setenv("SWE_PYTHON", "/env/python")
        assert ir.resolve_python("/explicit/python", {"python": "/reg/python"}) == "/explicit/python"

    def test_resolve_python_registry_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("SWE_PYTHON", "/env/python")
        assert ir.resolve_python(None, {"python": "/reg/python"}) == "/reg/python"

    def test_resolve_python_env_wins_over_default(self, monkeypatch):
        monkeypatch.setenv("SWE_PYTHON", "/env/python")
        assert ir.resolve_python(None, {}) == "/env/python"

    def test_resolve_python_default_is_sys_executable(self, monkeypatch):
        monkeypatch.delenv("SWE_PYTHON", raising=False)
        assert ir.resolve_python(None, {}) == sys.executable

    def _paths(self, tmp_path):
        repos = tmp_path / "repos"
        tasks = tmp_path / "tasks"
        registry = tmp_path / "repos.json"
        registry.write_text('{"repos": []}')
        return repos, tasks, registry

    def test_initialize_repo_writes_marker(self, tmp_path, monkeypatch):
        repos, tasks, registry = self._paths(tmp_path)
        calls = []
        monkeypatch.setattr(ir, "run", lambda cmd, cwd=None, check=True: calls.append(cmd) or 0)
        summary = ir.initialize_repo(
            "https://github.com/org/myrepo.git",
            repos_dir=repos,
            tasks_dir=tasks,
            registry_file=registry,
        )
        assert summary["repo_name"] == "myrepo"
        assert (repos / "myrepo" / ".SWE" / "init_complete.marker").exists()
        assert any(c[0] == "git" for c in calls)

    def test_initialize_repo_existing_without_force_raises(self, tmp_path, monkeypatch):
        repos, tasks, registry = self._paths(tmp_path)
        (repos / "myrepo").mkdir(parents=True)
        monkeypatch.setattr(ir, "run", lambda cmd, cwd=None, check=True: 0)
        with pytest.raises(FileExistsError):
            ir.initialize_repo(
                "https://github.com/org/myrepo.git",
                repos_dir=repos,
                tasks_dir=tasks,
                registry_file=registry,
            )

    def test_initialize_repo_force_removes_repo_and_tasks(self, tmp_path, monkeypatch):
        repos, tasks, registry = self._paths(tmp_path)
        repo_dir = repos / "myrepo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "old.txt").write_text("old")
        date_dir = tasks / "20240101"
        date_dir.mkdir(parents=True)
        task_dir = date_dir / "20240101_myrepo_task1"
        task_dir.mkdir()
        monkeypatch.setattr(ir, "run", lambda cmd, cwd=None, check=True: 0)
        ir.initialize_repo(
            "https://github.com/org/myrepo.git",
            force=True,
            repos_dir=repos,
            tasks_dir=tasks,
            registry_file=registry,
        )
        assert not (repo_dir / "old.txt").exists()
        assert not task_dir.exists()
        assert (repo_dir / ".SWE" / "init_complete.marker").exists()

    def test_initialize_repo_uses_explicit_python(self, tmp_path, monkeypatch):
        repos, tasks, registry = self._paths(tmp_path)
        calls = []
        monkeypatch.setattr(ir, "run", lambda cmd, cwd=None, check=True: calls.append(cmd) or 0)
        ir.initialize_repo(
            "https://github.com/org/myrepo.git",
            python="/opt/py310/bin/python",
            repos_dir=repos,
            tasks_dir=tasks,
            registry_file=registry,
        )
        venv_calls = [c for c in calls if "venv" in c]
        assert venv_calls
        assert venv_calls[0][0] == "/opt/py310/bin/python"

    def test_initialize_repo_uses_registry_python(self, tmp_path, monkeypatch):
        repos = tmp_path / "repos"
        tasks = tmp_path / "tasks"
        registry = tmp_path / "repos.json"
        registry.write_text('{"repos": [{"name": "myrepo", "python": "/reg/py"}]}')
        calls = []
        monkeypatch.setattr(ir, "run", lambda cmd, cwd=None, check=True: calls.append(cmd) or 0)
        ir.initialize_repo(
            "https://github.com/org/myrepo.git",
            repos_dir=repos,
            tasks_dir=tasks,
            registry_file=registry,
        )
        venv_calls = [c for c in calls if "venv" in c]
        assert venv_calls
        assert venv_calls[0][0] == "/reg/py"

    def test_derive_repo_name_empty_raises(self):
        with pytest.raises(ValueError):
            ir.derive_repo_name("")

    def test_derive_repo_name_no_name_raises(self):
        with pytest.raises(ValueError):
            ir.derive_repo_name("https://github.com/")

    def test_run_prints_and_returns_returncode(self):
        assert ir.run(["echo", "hi"]) == 0
        assert ir.run(["echo", "hi"], cwd="/tmp") == 0

    def test_lookup_registry_entry_missing_file(self, tmp_path):
        assert ir.lookup_registry_entry(tmp_path / "nope.json", "myrepo") == {}

    def test_lookup_registry_entry_invalid_json(self, tmp_path, capsys):
        registry = tmp_path / "repos.json"
        registry.write_text("{broken")
        assert ir.lookup_registry_entry(registry, "myrepo") == {}
        assert "WARNING" in capsys.readouterr().err

    def test_lookup_registry_entry_by_slug(self, tmp_path):
        registry = tmp_path / "repos.json"
        registry.write_text('{"repos": [{"name": "other", "slug": "org/myrepo"}]}')
        entry = ir.lookup_registry_entry(registry, "myrepo")
        assert entry == {"name": "other", "slug": "org/myrepo"}

    def _no_tooling(self):
        return dict(
            install_ruff=False,
            install_interrogate=False,
            install_mypy=False,
            install_bandit=False,
            install_vulture=False,
            install_pip_audit=False,
            install_pytest_cov=False,
            install_radon=False,
        )

    def test_install_dependencies_requirements_txt(self, tmp_path, monkeypatch):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "requirements.txt").write_text("requests\n")
        venv_dir = repo_dir / ".venv"
        calls = []
        monkeypatch.setattr(ir, "run", lambda cmd, cwd=None, check=True: calls.append(cmd) or 0)
        summary = ir.install_dependencies(
            repo_dir, venv_dir, editable=None, extras=[], **self._no_tooling()
        )
        assert summary["requirements_txt"] is True
        assert any("-r" in c for c in calls)

    def test_install_dependencies_editable_without_setup(self, tmp_path, monkeypatch, capsys):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        venv_dir = repo_dir / ".venv"
        monkeypatch.setattr(ir, "run", lambda cmd, cwd=None, check=True: 0)
        summary = ir.install_dependencies(
            repo_dir, venv_dir, editable=True, extras=[], **self._no_tooling()
        )
        assert summary["editable_install"] is False
        assert "WARNING" in capsys.readouterr().err

    def test_install_dependencies_editable_with_setup(self, tmp_path, monkeypatch):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "setup.py").write_text("")
        venv_dir = repo_dir / ".venv"
        calls = []
        monkeypatch.setattr(ir, "run", lambda cmd, cwd=None, check=True: calls.append(cmd) or 0)
        summary = ir.install_dependencies(
            repo_dir, venv_dir, editable=True, extras=[], **self._no_tooling()
        )
        assert summary["editable_install"] is True
        assert any("-e" in c for c in calls)

    def test_install_dependencies_extras_without_setup(self, tmp_path, monkeypatch, capsys):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        venv_dir = repo_dir / ".venv"
        monkeypatch.setattr(ir, "run", lambda cmd, cwd=None, check=True: 0)
        summary = ir.install_dependencies(
            repo_dir, venv_dir, editable=False, extras=["dev"], **self._no_tooling()
        )
        assert summary["extras_installed"] == []
        assert "WARNING" in capsys.readouterr().err

    def test_install_dependencies_extras_with_setup(self, tmp_path, monkeypatch):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "setup.py").write_text("")
        venv_dir = repo_dir / ".venv"
        calls = []
        monkeypatch.setattr(ir, "run", lambda cmd, cwd=None, check=True: calls.append(cmd) or 0)
        summary = ir.install_dependencies(
            repo_dir, venv_dir, editable=False, extras=["dev", "test"], **self._no_tooling()
        )
        assert summary["extras_installed"] == ["dev", "test"]

    def test_cleanup_tasks_missing_dir(self, tmp_path):
        ir.cleanup_tasks(tmp_path / "nope", "myrepo")

    def test_cleanup_tasks_skips_non_dirs_and_no_match(self, tmp_path, capsys):
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        (tasks_dir / "20240101_notes.txt").write_text("plain")
        date_dir = tasks_dir / "20240101"
        date_dir.mkdir()
        (date_dir / "plainfile.txt").write_text("plain")
        (date_dir / "unrelated_task").mkdir()
        ir.cleanup_tasks(tasks_dir, "myrepo")
        assert "No task directories found" in capsys.readouterr().out

    def test_cleanup_tasks_substring_bug_not_prefix_ambiguous(self, tmp_path, capsys):
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        date_dir = tasks_dir / "20260101"
        date_dir.mkdir()
        my_repo_task = date_dir / "20260101_my_repo_task1"
        my_repo_task.mkdir()
        my_task = date_dir / "20260101_my_task1"
        my_task.mkdir()
        ir.cleanup_tasks(tasks_dir, "my")
        assert my_repo_task.exists()
        assert not my_task.exists()
        assert "Removed 1 task directories for my." in capsys.readouterr().out

    def test_cleanup_tasks_repo_does_not_match_prefix(self, tmp_path):
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        date_dir = tasks_dir / "20260101"
        date_dir.mkdir()
        my_repo_task = date_dir / "20260101_my_repo_task1"
        my_repo_task.mkdir()
        ir.cleanup_tasks(tasks_dir, "repo")
        assert my_repo_task.exists()

    def test_cleanup_tasks_injective_encoding(self, tmp_path):
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        date_dir = tasks_dir / "20240101"
        date_dir.mkdir()
        component = _repo_dir_component("owner/repo")
        assert "~" in component
        encoded = date_dir / f"20240101_{component}_task1"
        encoded.mkdir()
        plain = date_dir / "20240101_owner_repo_task1"
        plain.mkdir()
        ir.cleanup_tasks(tasks_dir, "owner/repo")
        assert not encoded.exists()
        assert plain.exists()

    def test_cleanup_tasks_symlink_outside_refused(self, tmp_path, capsys):
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        date_dir = tasks_dir / "20240101"
        date_dir.mkdir()
        link = date_dir / "20240101_myrepo_task1"
        link.symlink_to(outside, target_is_directory=True)
        ir.cleanup_tasks(tasks_dir, "myrepo")
        out = capsys.readouterr().out
        assert outside.exists()
        assert "refusing to remove task directory outside tasks dir" in out
        assert "No task directories found" in out

    def test_cleanup_tasks_resolve_oserror_skipped(self, tmp_path, monkeypatch, capsys):
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        date_dir = tasks_dir / "20240101"
        date_dir.mkdir()
        task_dir = date_dir / "20240101_myrepo_task1"
        task_dir.mkdir()

        real_resolve = Path.resolve

        def fake_resolve(self, strict=False):
            if self == task_dir:
                raise OSError("boom")
            return real_resolve(self, strict=strict)

        monkeypatch.setattr(Path, "resolve", fake_resolve)
        ir.cleanup_tasks(tasks_dir, "myrepo")
        assert task_dir.exists()
        assert "No task directories found" in capsys.readouterr().out

    def test_initialize_repo_no_install(self, tmp_path, monkeypatch, capsys):
        repos, tasks, registry = self._paths(tmp_path)
        monkeypatch.setattr(ir, "run", lambda cmd, cwd=None, check=True: 0)
        summary = ir.initialize_repo(
            "https://github.com/org/myrepo.git",
            no_install=True,
            repos_dir=repos,
            tasks_dir=tasks,
            registry_file=registry,
        )
        assert summary["install_summary"] == {"skipped": True}
        assert "--no-install set" in capsys.readouterr().out

    def test_main_cli_success(self, monkeypatch):
        captured = {}

        def fake_initialize_repo(git_url, **kwargs):
            captured["git_url"] = git_url
            captured.update(kwargs)
            return {"repo_name": "myrepo"}

        monkeypatch.setattr(ir, "initialize_repo", fake_initialize_repo)
        monkeypatch.setattr(sys, "argv", [
            "initialize_repo.py", "https://github.com/org/myrepo.git",
        ])
        ir.main()
        assert captured["git_url"] == "https://github.com/org/myrepo.git"

    def test_main_cli_explicit_dirs(self, tmp_path, monkeypatch):
        captured = {}

        def fake_initialize_repo(git_url, **kwargs):
            captured.update(kwargs)
            return {"repo_name": "myrepo"}

        monkeypatch.setattr(ir, "initialize_repo", fake_initialize_repo)
        repos = tmp_path / "repos"
        tasks = tmp_path / "tasks"
        registry = tmp_path / "repos.json"
        monkeypatch.setattr(sys, "argv", [
            "initialize_repo.py", "https://github.com/org/myrepo.git",
            "--repos-dir", str(repos),
            "--tasks-dir", str(tasks),
            "--registry-file", str(registry),
        ])
        ir.main()
        assert captured["repos_dir"] == repos
        assert captured["tasks_dir"] == tasks
        assert captured["registry_file"] == registry

    def test_main_file_exists_error(self, monkeypatch):
        def boom(*a, **k):
            raise FileExistsError("already exists")

        monkeypatch.setattr(ir, "initialize_repo", boom)
        monkeypatch.setattr(sys, "argv", [
            "initialize_repo.py", "https://github.com/org/myrepo.git",
        ])
        with pytest.raises(SystemExit) as excinfo:
            ir.main()
        assert excinfo.value.code == 1

    def test_main_called_process_error(self, monkeypatch):
        def boom(*a, **k):
            raise subprocess.CalledProcessError(2, "cmd")

        monkeypatch.setattr(ir, "initialize_repo", boom)
        monkeypatch.setattr(sys, "argv", [
            "initialize_repo.py", "https://github.com/org/myrepo.git",
        ])
        with pytest.raises(SystemExit) as excinfo:
            ir.main()
        assert excinfo.value.code == 2

    @pytest.mark.filterwarnings("ignore::RuntimeWarning")
    def test_main_guard(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["initialize_repo.py", "--help"])
        with pytest.raises(SystemExit) as excinfo:
            runpy.run_module("cronpypeline.plugins.initialize_repo", run_name="__main__")
        assert excinfo.value.code == 0


# ─── SWE run_repo_init callable ──────────────────────────────────────────────


def _init_action():
    return ActionSpec.from_dict({
        "type": "custom",
        "params": {"callable": "cronpypeline.plugins.swe_plugin.run_repo_init"},
    })


class TestRunRepoInit:
    def test_no_slug_or_git_url_fails(self):
        ctx = TickContext(target="repo1", workspace_dir=Path("/ws"), target_config={})
        result = swe_plugin.run_repo_init(_init_action(), ctx)
        assert result.success is False
        assert "git_url or slug" in result.stderr

    def test_dry_run_does_not_call_initialize(self, monkeypatch):
        called = []
        monkeypatch.setattr(ir, "initialize_repo", lambda *a, **k: called.append(1))
        ctx = TickContext(
            target="repo1",
            workspace_dir=Path("/ws"),
            target_config={"slug": "org/myrepo"},
            dry_run=True,
        )
        result = swe_plugin.run_repo_init(_init_action(), ctx)
        assert result.success is True
        assert result.dry_run is True
        assert called == []

    def test_derives_git_url_from_slug(self, tmp_path, monkeypatch):
        captured = {}

        def fake_initialize_repo(git_url, **kwargs):
            captured["git_url"] = git_url
            captured.update(kwargs)
            return {"repo_name": "myrepo"}

        monkeypatch.setattr(ir, "initialize_repo", fake_initialize_repo)
        workspace = tmp_path / "repos"
        ctx = TickContext(
            target="myrepo",
            workspace_dir=workspace,
            target_config={"slug": "org/myrepo"},
        )
        result = swe_plugin.run_repo_init(_init_action(), ctx)
        assert result.success is True
        assert captured["git_url"] == "https://github.com/org/myrepo.git"
        assert captured["repos_dir"] == workspace
        assert captured["tasks_dir"] == workspace.parent / "tasks"
        assert captured["force"] is False

    def test_force_passed_through(self, tmp_path, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            ir, "initialize_repo",
            lambda git_url, **kwargs: captured.update(kwargs) or {"repo_name": "myrepo"},
        )
        ctx = TickContext(
            target="myrepo",
            workspace_dir=tmp_path / "repos",
            target_config={"slug": "org/myrepo"},
            force=True,
        )
        swe_plugin.run_repo_init(_init_action(), ctx)
        assert captured["force"] is True

    def test_explicit_git_url_param_wins(self, tmp_path, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            ir, "initialize_repo",
            lambda git_url, **kwargs: captured.update({"git_url": git_url}) or {"repo_name": "x"},
        )
        action = ActionSpec.from_dict({
            "type": "custom",
            "params": {
                "callable": "cronpypeline.plugins.swe_plugin.run_repo_init",
                "git_url": "git@github.com:org/other.git",
            },
        })
        ctx = TickContext(
            target="other",
            workspace_dir=tmp_path / "repos",
            target_config={"slug": "org/myrepo"},
        )
        swe_plugin.run_repo_init(action, ctx)
        assert captured["git_url"] == "git@github.com:org/other.git"

    def test_file_exists_error_reported(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise FileExistsError("already exists")

        monkeypatch.setattr(ir, "initialize_repo", boom)
        ctx = TickContext(
            target="myrepo",
            workspace_dir=tmp_path / "repos",
            target_config={"slug": "org/myrepo"},
        )
        result = swe_plugin.run_repo_init(_init_action(), ctx)
        assert result.success is False
        assert "already exists" in result.stderr

    def test_registry_file_from_context(self, tmp_path):
        spec = TargetSpec(type=TargetType.REGISTRY, file="/path/repos.json", key="repos")
        config = PipelineConfig(
            name="p",
            workspace_dir=str(tmp_path),
            stages=[],
            targets=spec,
        )
        pipeline = Pipeline(config)
        ctx = TickContext(target="x", workspace_dir=tmp_path, pipeline=pipeline)
        assert swe_plugin._registry_file_from_context(ctx) == Path("/path/repos.json")

    def test_registry_file_from_context_default(self, tmp_path):
        ctx = TickContext(target="x", workspace_dir=tmp_path)
        assert swe_plugin._registry_file_from_context(ctx) == Path(
            "/spellbook_data/Serendipity/swe/repos.json"
        )

    def test_python_param_passed_through(self, tmp_path, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            ir, "initialize_repo",
            lambda git_url, **kwargs: captured.update(kwargs) or {"repo_name": "myrepo"},
        )
        action = ActionSpec.from_dict({
            "type": "custom",
            "params": {
                "callable": "cronpypeline.plugins.swe_plugin.run_repo_init",
                "python": "/opt/py310/bin/python",
            },
        })
        ctx = TickContext(
            target="myrepo",
            workspace_dir=tmp_path / "repos",
            target_config={"slug": "org/myrepo"},
        )
        swe_plugin.run_repo_init(action, ctx)
        assert captured["python"] == "/opt/py310/bin/python"

    def test_python_from_target_config(self, tmp_path, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            ir, "initialize_repo",
            lambda git_url, **kwargs: captured.update(kwargs) or {"repo_name": "myrepo"},
        )
        ctx = TickContext(
            target="myrepo",
            workspace_dir=tmp_path / "repos",
            target_config={"slug": "org/myrepo", "python": "/reg/py"},
        )
        swe_plugin.run_repo_init(_init_action(), ctx)
        assert captured["python"] == "/reg/py"

    def test_python_defaults_to_none(self, tmp_path, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            ir, "initialize_repo",
            lambda git_url, **kwargs: captured.update(kwargs) or {"repo_name": "myrepo"},
        )
        ctx = TickContext(
            target="myrepo",
            workspace_dir=tmp_path / "repos",
            target_config={"slug": "org/myrepo"},
        )
        swe_plugin.run_repo_init(_init_action(), ctx)
        assert captured["python"] is None

    def test_registry_file_param_passed_through(self, tmp_path, monkeypatch):
        captured = {}
        monkeypatch.setattr(
            ir, "initialize_repo",
            lambda git_url, **kwargs: captured.update(kwargs) or {"repo_name": "myrepo"},
        )
        action = ActionSpec.from_dict({
            "type": "custom",
            "params": {
                "callable": "cronpypeline.plugins.swe_plugin.run_repo_init",
                "registry_file": str(tmp_path / "custom.json"),
            },
        })
        ctx = TickContext(
            target="myrepo",
            workspace_dir=tmp_path / "repos",
            target_config={"slug": "org/myrepo"},
        )
        swe_plugin.run_repo_init(action, ctx)
        assert captured["registry_file"] == tmp_path / "custom.json"

    def test_called_process_error_reported(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise subprocess.CalledProcessError(2, "cmd")

        monkeypatch.setattr(ir, "initialize_repo", boom)
        ctx = TickContext(
            target="myrepo",
            workspace_dir=tmp_path / "repos",
            target_config={"slug": "org/myrepo"},
        )
        result = swe_plugin.run_repo_init(_init_action(), ctx)
        assert result.success is False
        assert "command failed:" in result.stderr
        assert result.exit_code == 2

    def test_generic_exception_reported(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(ir, "initialize_repo", boom)
        ctx = TickContext(
            target="myrepo",
            workspace_dir=tmp_path / "repos",
            target_config={"slug": "org/myrepo"},
        )
        result = swe_plugin.run_repo_init(_init_action(), ctx)
        assert result.success is False
        assert result.stderr == "RuntimeError: kaboom"
