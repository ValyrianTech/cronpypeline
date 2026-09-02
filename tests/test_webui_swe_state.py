"""Tests for webui SWE plugin state helpers (_swe_state, _read_json, _swe_issue_counts)."""

import importlib
import json
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

from cronpypeline.config import PipelineConfig, Stage

# webui/ is not a package — add it to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webui"))

import app


def _make_stage(stage_id, enabled=True, modes=None, **extra):
    """Build a minimal Stage for webui helper tests."""
    data = {
        "id": stage_id,
        "name": f"Stage {stage_id}",
        "trigger": {"type": "file_missing", "path": f"{stage_id}.md"},
        "action": {"type": "command", "params": {"command": "echo hi"}},
        "enabled": enabled,
        "modes": modes or [],
    }
    data.update(extra)
    return Stage.from_dict(data)


def _make_config(workspace_dir, stages=None, **kwargs):
    """Build a minimal PipelineConfig for webui helper tests."""
    return PipelineConfig(
        name="test",
        workspace_dir=str(workspace_dir),
        stages=stages or [],
        **kwargs,
    )


class TestReadJson:
    def test_missing_file(self, tmp_path):
        assert app._read_json(tmp_path / "nope.json") is None

    def test_invalid_json(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{broken")
        assert app._read_json(f) is None

    def test_non_dict(self, tmp_path):
        f = tmp_path / "list.json"
        f.write_text("[1, 2, 3]")
        assert app._read_json(f) is None

    def test_valid_dict(self, tmp_path):
        f = tmp_path / "ok.json"
        f.write_text('{"a": 1}')
        assert app._read_json(f) == {"a": 1}


class TestSweIssueCounts:
    def test_no_dir(self, tmp_path):
        assert app._swe_issue_counts(tmp_path / "issues") is None

    def test_empty_dir(self, tmp_path):
        (tmp_path / "issues").mkdir()
        assert app._swe_issue_counts(tmp_path / "issues") is None

    def test_counts_by_status(self, tmp_path):
        issues = tmp_path / "issues"
        issues.mkdir()
        (issues / "a.md").write_text("---\nstatus: open\n---\nbody")
        (issues / "b.md").write_text("---\nstatus: done\n---\nbody")
        (issues / "c.md").write_text("---\nstatus: open\n---\nbody")
        (issues / "d.md").write_text("---\ntitle: no status\n---\nbody")
        counts = app._swe_issue_counts(issues)
        assert counts == {"open": 2, "done": 1, "unknown": 1}

    def test_skips_file_without_frontmatter(self, tmp_path):
        """Files not starting with '---' are skipped."""
        issues = tmp_path / "issues"
        issues.mkdir()
        (issues / "plain.md").write_text("just a body without frontmatter")
        assert app._swe_issue_counts(issues) is None

    def test_skips_file_with_read_error(self, tmp_path):
        """OSError while reading a file is swallowed (line 152-153)."""
        issues = tmp_path / "issues"
        issues.mkdir()
        (issues / "bad.md").write_text("---\nstatus: open\n---")
        with mock.patch("pathlib.Path.read_text", side_effect=OSError("boom")):
            assert app._swe_issue_counts(issues) is None


class TestSweState:
    def test_no_swe_dir(self, tmp_path):
        assert app._swe_state(tmp_path) is None

    def test_empty_swe_dir(self, tmp_path):
        (tmp_path / ".SWE").mkdir()
        assert app._swe_state(tmp_path) is None

    def test_with_pr(self, tmp_path):
        swe = tmp_path / ".SWE"
        swe.mkdir()
        (swe / "pr_published.json").write_text(json.dumps({
            "pr_number": 42,
            "pr_url": "https://github.com/o/r/pull/42",
            "pr_state": "open",
            "pr_review_cycles": 1,
        }))
        result = app._swe_state(tmp_path)
        assert result is not None
        assert result["pr"]["pr_number"] == 42
        assert result["pr"]["pr_state"] == "open"
        assert result["pr"]["pr_review_cycles"] == 1
        assert result["session"] is None
        assert result["issues"] is None

    def test_with_session(self, tmp_path):
        swe = tmp_path / ".SWE"
        swe.mkdir()
        (swe / "github_session.json").write_text(json.dumps({
            "active": True,
            "issue_id": "github-5",
            "github_number": 5,
        }))
        result = app._swe_state(tmp_path)
        assert result is not None
        assert result["session"]["active"] is True
        assert result["session"]["github_number"] == 5
        assert result["pr"] is None

    def test_with_issues(self, tmp_path):
        swe = tmp_path / ".SWE"
        (swe / "issues").mkdir(parents=True)
        (swe / "issues" / "a.md").write_text("---\nstatus: open\n---\nbody")
        result = app._swe_state(tmp_path)
        assert result is not None
        assert result["issues"] == {"open": 1}

    def test_full_state(self, tmp_path):
        swe = tmp_path / ".SWE"
        (swe / "issues").mkdir(parents=True)
        (swe / "pr_published.json").write_text(json.dumps({
            "pr_number": 7,
            "pr_state": "approved",
            "pr_url": "https://github.com/o/r/pull/7",
            "pr_review_cycles": 2,
        }))
        (swe / "github_session.json").write_text(json.dumps({
            "active": True,
            "issue_id": "github-3",
            "github_number": 3,
        }))
        (swe / "issues" / "a.md").write_text("---\nstatus: open\n---\nbody")
        (swe / "issues" / "b.md").write_text("---\nstatus: done\n---\nbody")
        result = app._swe_state(tmp_path)
        assert result is not None
        assert result["pr"]["pr_number"] == 7
        assert result["pr"]["pr_state"] == "approved"
        assert result["session"]["active"] is True
        assert result["issues"] == {"open": 1, "done": 1}

    def test_pr_defaults(self, tmp_path):
        """PR with minimal data should get default field values."""
        swe = tmp_path / ".SWE"
        swe.mkdir()
        (swe / "pr_published.json").write_text(json.dumps({"pr_number": 1}))
        result = app._swe_state(tmp_path)
        assert result["pr"]["pr_state"] == "open"
        assert result["pr"]["pr_review_cycles"] == 0
        assert result["pr"]["filed_issues"] == []
        assert result["pr"]["pr_url"] == ""


class TestConfigTogglePath:
    def test_no_config_file(self, tmp_path):
        cfg = _make_config(tmp_path)
        assert app._config_toggle_path(cfg) is None

    def test_relative_config_file(self, tmp_path):
        cfg = _make_config(tmp_path, config_file="toggle.json")
        assert app._config_toggle_path(cfg) == tmp_path / "toggle.json"

    def test_absolute_config_file(self, tmp_path):
        abs_path = tmp_path / "sub" / "toggle.json"
        cfg = _make_config(tmp_path, config_file=str(abs_path))
        assert app._config_toggle_path(cfg) == abs_path


class TestReadEnabled:
    def test_no_toggle_file(self, tmp_path):
        cfg = _make_config(tmp_path)
        assert app._read_enabled(cfg) is None

    def test_missing_toggle_file(self, tmp_path):
        cfg = _make_config(tmp_path, config_file="toggle.json")
        assert app._read_enabled(cfg) is True

    def test_enabled_true(self, tmp_path):
        cfg = _make_config(tmp_path, config_file="toggle.json")
        (tmp_path / "toggle.json").write_text('{"enabled": true}')
        assert app._read_enabled(cfg) is True

    def test_enabled_false(self, tmp_path):
        cfg = _make_config(tmp_path, config_file="toggle.json")
        (tmp_path / "toggle.json").write_text('{"enabled": false}')
        assert app._read_enabled(cfg) is False

    def test_invalid_json(self, tmp_path):
        cfg = _make_config(tmp_path, config_file="toggle.json")
        (tmp_path / "toggle.json").write_text("{broken")
        assert app._read_enabled(cfg) is True


class TestReadMode:
    def test_no_mode_file(self, tmp_path):
        cfg = _make_config(tmp_path)
        assert app._read_mode(cfg) is None

    def test_missing_mode_file(self, tmp_path):
        cfg = _make_config(tmp_path, mode_file="mode.json")
        assert app._read_mode(cfg) is None

    def test_valid_mode_file(self, tmp_path):
        cfg = _make_config(tmp_path, mode_file="mode.json")
        (tmp_path / "mode.json").write_text('{"mode": "github"}')
        assert app._read_mode(cfg) == "github"

    def test_relative_mode_file(self, tmp_path):
        cfg = _make_config(tmp_path, mode_file="sub/mode.json")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "mode.json").write_text('{"mode": "default"}')
        assert app._read_mode(cfg) == "default"

    def test_invalid_json_mode_file(self, tmp_path):
        cfg = _make_config(tmp_path, mode_file="mode.json")
        (tmp_path / "mode.json").write_text("{broken")
        assert app._read_mode(cfg) is None


class TestActiveStages:
    def test_enabled_and_disabled(self, tmp_path):
        s1 = _make_stage("A", enabled=True)
        s2 = _make_stage("B", enabled=False)
        cfg = _make_config(tmp_path, stages=[s1, s2])
        assert [s.id for s in app._active_stages(cfg, None)] == ["A"]

    def test_modes_match(self, tmp_path):
        s1 = _make_stage("A", modes=["github"])
        s2 = _make_stage("B", modes=["default"])
        s3 = _make_stage("C", modes=[])
        cfg = _make_config(tmp_path, stages=[s1, s2, s3])
        assert [s.id for s in app._active_stages(cfg, "github")] == ["A", "C"]

    def test_mode_none_excludes_mode_stages(self, tmp_path):
        s1 = _make_stage("A", modes=["github"])
        s2 = _make_stage("C", modes=[])
        cfg = _make_config(tmp_path, stages=[s1, s2])
        assert [s.id for s in app._active_stages(cfg, None)] == ["C"]


class TestSerializeStage:
    def test_basic_stage(self):
        stage = _make_stage("A0")
        result = app._serialize_stage(stage)
        assert result["id"] == "A0"
        assert result["name"] == "Stage A0"
        assert result["trigger_type"] == "file_missing"
        assert result["action_type"] == "command"
        assert result["chain"] is False
        assert result["timeout_minutes"] == 30
        assert result["max_retries"] == 3
        assert result["max_rejections"] == 0
        assert result["enabled"] is True
        assert result["modes"] == []
        assert result["has_markers"] is False
        assert result["marker_roles"] == []
        assert result["callable"] is None
        assert result["agent"] is None

    def test_full_stage(self):
        stage = _make_stage(
            "B1",
            modes=["github"],
            chain=True,
            timeout_minutes=5,
            max_retries=7,
            max_rejections=2,
            trigger={"type": "custom", "callable": "x.y"},
            action={"type": "custom", "params": {"callable": "z.w", "agent": "fixer"}},
            markers={
                "completion": {"type": "file", "name": "done.md"},
                "processing": {"type": "json", "name": ".proc"},
            },
        )
        result = app._serialize_stage(stage)
        assert result["trigger_type"] == "custom"
        assert result["action_type"] == "custom"
        assert result["chain"] is True
        assert result["timeout_minutes"] == 5
        assert result["max_retries"] == 7
        assert result["max_rejections"] == 2
        assert result["modes"] == ["github"]
        assert result["has_markers"] is True
        assert result["marker_roles"] == ["completion", "processing"]
        assert result["callable"] == "z.w"
        assert result["agent"] == "fixer"


class TestBuildApp:
    """Tests for _build_app with mocked fastapi/pydantic modules."""

    def _make_mock_modules(self):
        class MockHTTPException(Exception):
            def __init__(self, status_code=500, detail=""):
                self.status_code = status_code
                self.detail = detail
                super().__init__(detail)

        class MockFastAPI:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.routes = []
                self.mounts = []

            def get(self, path):
                def decorator(func):
                    self.routes.append(("GET", path, func))
                    return func

                return decorator

            def post(self, path):
                def decorator(func):
                    self.routes.append(("POST", path, func))
                    return func

                return decorator

            def mount(self, path, target, **kwargs):
                self.mounts.append((path, target, kwargs))

        class MockBaseModel:
            def __init__(self, **kwargs):
                for k, v in kwargs.items():
                    setattr(self, k, v)

        fastapi_module = types.ModuleType("fastapi")
        fastapi_module.FastAPI = MockFastAPI
        fastapi_module.HTTPException = MockHTTPException
        fastapi_module.Query = lambda default=None: default

        responses_module = types.ModuleType("fastapi.responses")
        responses_module.FileResponse = mock.Mock()

        staticfiles_module = types.ModuleType("fastapi.staticfiles")
        staticfiles_module.StaticFiles = mock.Mock()

        pydantic_module = types.ModuleType("pydantic")
        pydantic_module.BaseModel = MockBaseModel

        modules = {
            "fastapi": fastapi_module,
            "fastapi.responses": responses_module,
            "fastapi.staticfiles": staticfiles_module,
            "pydantic": pydantic_module,
        }
        return modules, fastapi_module, responses_module, staticfiles_module, pydantic_module

    def _build_app(self):
        modules, fastapi_module, responses_module, staticfiles_module, pydantic_module = self._make_mock_modules()
        with mock.patch.dict(sys.modules, modules):
            built = app._build_app()
        return built, fastapi_module, responses_module, staticfiles_module, pydantic_module

    def _get_routes(self, built):
        return {f"{method} {path}": func for method, path, func in built.routes}

    def _write_config(self, configs_dir, name, data):
        path = configs_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path

    def test_mock_base_model_init(self):
        """MockBaseModel.__init__ sets attributes from kwargs."""
        _modules, _fastapi_module, _responses_module, _staticfiles_module, pydantic_module = self._make_mock_modules()
        instance = pydantic_module.BaseModel(enabled=True)
        assert instance.enabled is True

    def test_returns_app_and_registers_routes(self):
        built, fastapi_module, _responses_module, staticfiles_module, _pydantic_module = self._build_app()
        assert isinstance(built, fastapi_module.FastAPI)
        paths = sorted(f"{m} {p}" for m, p, _ in built.routes)
        assert paths == [
            "GET /",
            "GET /api/configs",
            "GET /api/pipeline",
            "GET /api/status",
            "POST /api/toggle",
        ]
        assert len(built.mounts) == 1
        assert built.mounts[0][0] == "/static"
        staticfiles_module.StaticFiles.assert_called_once_with(directory=app.HERE / "static")

    def test_list_configs_empty_dir(self, tmp_path):
        built, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["GET /api/configs"]
        with mock.patch.object(app, "CONFIGS_DIR", tmp_path):
            result = handler()
        assert result == {"configs_dir": str(tmp_path), "configs": []}

    def test_list_configs_missing_dir(self, tmp_path):
        built, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["GET /api/configs"]
        with mock.patch.object(app, "CONFIGS_DIR", tmp_path / "nope"):
            result = handler()
        assert result == {"configs_dir": str(tmp_path / "nope"), "configs": []}

    def test_list_configs_with_files(self, tmp_path):
        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        (configs_dir / "b.json").write_text("{}")
        (configs_dir / "a.json").write_text("{}")
        (configs_dir / "readme.txt").write_text("hi")
        built, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["GET /api/configs"]
        with mock.patch.object(app, "CONFIGS_DIR", configs_dir):
            result = handler()
        assert result["configs"] == ["a.json", "b.json"]

    def test_pipeline_info_valid(self, tmp_path):
        configs_dir = tmp_path / "configs"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        self._write_config(configs_dir, "swe.json", {
            "name": "swe",
            "workspace_dir": str(workspace),
            "stages": [
                {"id": "A0", "name": "Step", "trigger": {"type": "file_missing", "path": "a.md"},
                 "action": {"type": "command", "params": {"command": "echo a"}}},
            ],
        })
        built, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["GET /api/pipeline"]
        with mock.patch.object(app, "CONFIGS_DIR", configs_dir):
            result = handler(config="swe.json")
        assert result["name"] == "swe"
        assert result["workspace_exists"] is True
        assert result["enabled"] is None
        assert result["targets"] == [{"name": ".", "config": {}}]
        assert result["targets_error"] is None
        assert len(result["stages"]) == 1
        assert result["stages"][0]["active"] is True

    def test_pipeline_info_invalid_name(self, tmp_path):
        built, fastapi_module, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["GET /api/pipeline"]
        with pytest.raises(fastapi_module.HTTPException) as excinfo:
            handler(config="notjson")
        assert excinfo.value.status_code == 400

    def test_pipeline_info_not_found(self, tmp_path):
        built, fastapi_module, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["GET /api/pipeline"]
        with mock.patch.object(app, "CONFIGS_DIR", tmp_path), pytest.raises(fastapi_module.HTTPException) as excinfo:
            handler(config="missing.json")
        assert excinfo.value.status_code == 404

    def test_pipeline_info_parse_error(self, tmp_path):
        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        (configs_dir / "bad.json").write_text("{broken")
        built, fastapi_module, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["GET /api/pipeline"]
        with mock.patch.object(app, "CONFIGS_DIR", configs_dir), pytest.raises(fastapi_module.HTTPException) as excinfo:
            handler(config="bad.json")
        assert excinfo.value.status_code == 422

    def test_pipeline_info_traversal(self, tmp_path):
        built, fastapi_module, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["GET /api/pipeline"]
        with mock.patch.object(app, "CONFIGS_DIR", tmp_path), pytest.raises(fastapi_module.HTTPException) as excinfo:
            handler(config="../outside.json")
        assert excinfo.value.status_code == 400

    def test_pipeline_info_targets_error(self, tmp_path):
        configs_dir = tmp_path / "configs"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        self._write_config(configs_dir, "swe.json", {
            "name": "swe",
            "workspace_dir": str(workspace),
            "targets": {"type": "registry", "file": str(tmp_path / "missing.json"), "key": "repos"},
            "stages": [],
        })
        built, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["GET /api/pipeline"]
        with mock.patch.object(app, "CONFIGS_DIR", configs_dir):
            result = handler(config="swe.json")
        assert result["targets"] == []
        assert result["targets_error"] is not None

    def test_pipeline_status_missing_workspace(self, tmp_path):
        configs_dir = tmp_path / "configs"
        self._write_config(configs_dir, "swe.json", {
            "name": "swe",
            "workspace_dir": str(tmp_path / "nope"),
            "stages": [],
        })
        built, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["GET /api/status"]
        with mock.patch.object(app, "CONFIGS_DIR", configs_dir):
            result = handler(config="swe.json")
        assert "Workspace directory not found" in result["error"]
        assert result["targets"] == {}

    def test_pipeline_status_load_failure(self, tmp_path):
        configs_dir = tmp_path / "configs"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        self._write_config(configs_dir, "swe.json", {
            "name": "swe",
            "workspace_dir": str(workspace),
            "targets": {"type": "registry", "file": str(tmp_path / "missing.json"), "key": "repos"},
            "stages": [],
        })
        built, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["GET /api/status"]
        with mock.patch.object(app, "CONFIGS_DIR", configs_dir):
            result = handler(config="swe.json")
        assert "Failed to load targets" in result["error"]

    def test_pipeline_status_valid(self, tmp_path):
        configs_dir = tmp_path / "configs"
        workspace = tmp_path / "workspace"
        (workspace / "repo1").mkdir(parents=True)
        self._write_config(configs_dir, "swe.json", {
            "name": "swe",
            "workspace_dir": str(workspace),
            "targets": {"type": "static", "items": ["repo1"]},
            "stages": [
                {"id": "A0", "name": "Step", "trigger": {"type": "file_missing", "path": "a.md"},
                 "action": {"type": "command", "params": {"command": "echo a"}}},
            ],
        })
        built, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["GET /api/status"]
        with mock.patch.object(app, "CONFIGS_DIR", configs_dir):
            result = handler(config="swe.json")
        assert result["error"] is None
        assert "repo1" in result["targets"]
        assert result["targets"]["repo1"]["next_actionable"] == "A0"
        assert result["summary"]["targets"] == 1

    def test_pipeline_status_tracked_stage(self, tmp_path):
        configs_dir = tmp_path / "configs"
        workspace = tmp_path / "workspace"
        (workspace / "repo1").mkdir(parents=True)
        self._write_config(configs_dir, "swe.json", {
            "name": "swe",
            "workspace_dir": str(workspace),
            "targets": {"type": "static", "items": ["repo1"]},
            "stages": [
                {"id": "A0", "name": "Step", "trigger": {"type": "file_missing", "path": "a.md"},
                 "action": {"type": "command", "params": {"command": "echo a"}},
                 "markers": {"completion": {"type": "file", "name": "done.md"}}},
            ],
        })
        built, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["GET /api/status"]
        with mock.patch.object(app, "CONFIGS_DIR", configs_dir):
            result = handler(config="swe.json")
        assert result["error"] is None
        stage_state = result["targets"]["repo1"]["stages"]["A0"]
        assert stage_state["stateless"] is False
        assert stage_state["complete"] is False
        assert result["summary"]["tracked_stages"] == 1

    def test_toggle_no_config_file(self, tmp_path):
        configs_dir = tmp_path / "configs"
        self._write_config(configs_dir, "swe.json", {
            "name": "swe",
            "workspace_dir": str(tmp_path / "workspace"),
            "stages": [],
        })
        built, fastapi_module, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["POST /api/toggle"]
        with mock.patch.object(app, "CONFIGS_DIR", configs_dir), \
                mock.patch.object(app, "WEBUI_TOKEN", "test-token"), \
                pytest.raises(fastapi_module.HTTPException) as excinfo:
            handler(types.SimpleNamespace(enabled=True), config="swe.json", token="test-token")
        assert excinfo.value.status_code == 409

    def test_toggle_write_success(self, tmp_path):
        configs_dir = tmp_path / "configs"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        self._write_config(configs_dir, "swe.json", {
            "name": "swe",
            "workspace_dir": str(workspace),
            "config_file": "toggle.json",
            "stages": [],
        })
        built, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["POST /api/toggle"]
        with mock.patch.object(app, "CONFIGS_DIR", configs_dir), mock.patch.object(app, "WEBUI_TOKEN", "test-token"):
            result = handler(types.SimpleNamespace(enabled=False), config="swe.json", token="test-token")
        assert result["enabled"] is False
        assert (workspace / "toggle.json").exists()
        assert json.loads((workspace / "toggle.json").read_text())["enabled"] is False

    def test_toggle_preserves_existing(self, tmp_path):
        configs_dir = tmp_path / "configs"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "toggle.json").write_text('{"other": "keep"}')
        self._write_config(configs_dir, "swe.json", {
            "name": "swe",
            "workspace_dir": str(workspace),
            "config_file": "toggle.json",
            "stages": [],
        })
        built, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["POST /api/toggle"]
        with mock.patch.object(app, "CONFIGS_DIR", configs_dir), mock.patch.object(app, "WEBUI_TOKEN", "test-token"):
            handler(types.SimpleNamespace(enabled=True), config="swe.json", token="test-token")
        data = json.loads((workspace / "toggle.json").read_text())
        assert data["other"] == "keep"
        assert data["enabled"] is True

    def test_toggle_invalid_existing_json(self, tmp_path):
        configs_dir = tmp_path / "configs"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "toggle.json").write_text("{broken")
        self._write_config(configs_dir, "swe.json", {
            "name": "swe",
            "workspace_dir": str(workspace),
            "config_file": "toggle.json",
            "stages": [],
        })
        built, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["POST /api/toggle"]
        with mock.patch.object(app, "CONFIGS_DIR", configs_dir), mock.patch.object(app, "WEBUI_TOKEN", "test-token"):
            handler(types.SimpleNamespace(enabled=True), config="swe.json", token="test-token")
        data = json.loads((workspace / "toggle.json").read_text())
        assert data == {"enabled": True}

    def test_toggle_write_failure(self, tmp_path):
        configs_dir = tmp_path / "configs"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        self._write_config(configs_dir, "swe.json", {
            "name": "swe",
            "workspace_dir": str(workspace),
            "config_file": "toggle.json",
            "stages": [],
        })
        built, fastapi_module, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["POST /api/toggle"]
        with mock.patch.object(app, "CONFIGS_DIR", configs_dir), \
                mock.patch.object(app, "WEBUI_TOKEN", "test-token"), \
                mock.patch("pathlib.Path.write_text", side_effect=OSError("disk full")), \
                pytest.raises(fastapi_module.HTTPException) as excinfo:
            handler(types.SimpleNamespace(enabled=True), config="swe.json", token="test-token")
        assert excinfo.value.status_code == 500

    def test_toggle_allows_path_outside_safe_dirs(self, tmp_path):
        configs_dir = tmp_path / "configs"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        self._write_config(configs_dir, "swe.json", {
            "name": "swe",
            "workspace_dir": str(workspace),
            "config_file": str(outside / "toggle.json"),
            "stages": [],
        })
        built, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["POST /api/toggle"]
        with mock.patch.object(app, "CONFIGS_DIR", configs_dir), mock.patch.object(app, "WEBUI_TOKEN", "test-token"):
            result = handler(types.SimpleNamespace(enabled=True), config="swe.json", token="test-token")
        assert result["enabled"] is True
        assert (outside / "toggle.json").exists()
        assert json.loads((outside / "toggle.json").read_text())["enabled"] is True

    def test_toggle_allows_path_in_configs_dir(self, tmp_path):
        configs_dir = tmp_path / "configs"
        configs_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        self._write_config(configs_dir, "swe.json", {
            "name": "swe",
            "workspace_dir": str(workspace),
            "config_file": str(configs_dir / "toggle.json"),
            "stages": [],
        })
        built, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["POST /api/toggle"]
        with mock.patch.object(app, "CONFIGS_DIR", configs_dir), mock.patch.object(app, "WEBUI_TOKEN", "test-token"):
            result = handler(types.SimpleNamespace(enabled=False), config="swe.json", token="test-token")
        assert result["enabled"] is False
        assert (configs_dir / "toggle.json").exists()
        assert json.loads((configs_dir / "toggle.json").read_text())["enabled"] is False

    def test_toggle_allows_path_in_workspace(self, tmp_path):
        configs_dir = tmp_path / "configs"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        self._write_config(configs_dir, "swe.json", {
            "name": "swe",
            "workspace_dir": str(workspace),
            "config_file": str(workspace / "toggle.json"),
            "stages": [],
        })
        built, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["POST /api/toggle"]
        with mock.patch.object(app, "CONFIGS_DIR", configs_dir), mock.patch.object(app, "WEBUI_TOKEN", "test-token"):
            result = handler(types.SimpleNamespace(enabled=True), config="swe.json", token="test-token")
        assert result["enabled"] is True
        assert (workspace / "toggle.json").exists()

    def test_toggle_no_token_configured(self, tmp_path):
        """Toggle returns 403 when no auth token is configured."""
        configs_dir = tmp_path / "configs"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        self._write_config(configs_dir, "swe.json", {
            "name": "swe",
            "workspace_dir": str(workspace),
            "config_file": "toggle.json",
            "stages": [],
        })
        built, fastapi_module, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["POST /api/toggle"]
        with mock.patch.object(app, "CONFIGS_DIR", configs_dir), mock.patch.object(app, "WEBUI_TOKEN", ""):
            with pytest.raises(fastapi_module.HTTPException) as excinfo:
                handler(types.SimpleNamespace(enabled=True), config="swe.json", token="")
        assert excinfo.value.status_code == 403

    def test_toggle_wrong_token(self, tmp_path):
        """Toggle returns 401 when the provided token is wrong."""
        configs_dir = tmp_path / "configs"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        self._write_config(configs_dir, "swe.json", {
            "name": "swe",
            "workspace_dir": str(workspace),
            "config_file": "toggle.json",
            "stages": [],
        })
        built, fastapi_module, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["POST /api/toggle"]
        with mock.patch.object(app, "CONFIGS_DIR", configs_dir), mock.patch.object(app, "WEBUI_TOKEN", "secret"):
            with pytest.raises(fastapi_module.HTTPException) as excinfo:
                handler(types.SimpleNamespace(enabled=True), config="swe.json", token="wrong")
        assert excinfo.value.status_code == 401

    def test_index_serves_frontend(self):
        built, _fastapi_module, responses_module, *_ = self._build_app()
        routes = self._get_routes(built)
        handler = routes["GET /"]
        result = handler()
        responses_module.FileResponse.assert_called_once_with(app.HERE / "static" / "index.html")
        assert result is responses_module.FileResponse.return_value


class TestModuleAppFallback:
    def test_app_none_when_build_raises_non_import_error(self):
        """Module-level ``app`` should be None when _build_app raises a non-ImportError."""
        blocker_names = ["fastapi", "fastapi.responses", "fastapi.staticfiles", "pydantic"]
        saved_modules = {}
        try:
            # Pre-populate so the restore branch (saved_modules[mod_name] is not None) runs.
            sys.modules["fastapi"] = types.ModuleType("fastapi")
            for mod_name in blocker_names:
                saved_modules[mod_name] = sys.modules.get(mod_name)
                sys.modules[mod_name] = types.ModuleType(mod_name)

            # fastapi.FastAPI() raises RuntimeError; other attrs are dummies
            def _raise_runtime(*a, **kw):
                raise RuntimeError("boom")
            sys.modules["fastapi"].FastAPI = _raise_runtime
            sys.modules["fastapi"].HTTPException = type("HTTPException", (Exception,), {})
            sys.modules["fastapi"].Query = lambda *a, **kw: None
            sys.modules["fastapi.responses"].FileResponse = _raise_runtime
            sys.modules["fastapi.staticfiles"].StaticFiles = _raise_runtime
            sys.modules["pydantic"].BaseModel = type("BaseModel", (), {})

            importlib.reload(app)
            assert app.app is None
        finally:
            for mod_name in blocker_names:
                if saved_modules[mod_name] is not None:
                    sys.modules[mod_name] = saved_modules[mod_name]
                else:
                    sys.modules.pop(mod_name, None)
            importlib.reload(app)

    def test_app_none_when_fastapi_missing(self):
        """Module-level app should be None when fastapi is not installed."""
        blocker_names = ["fastapi", "fastapi.responses", "fastapi.staticfiles", "pydantic"]
        saved_modules = {}
        try:
            # Pre-populate so the restore branch (saved_modules[mod_name] is not None) runs.
            sys.modules["fastapi"] = types.ModuleType("fastapi")
            for mod_name in blocker_names:
                saved_modules[mod_name] = sys.modules.get(mod_name)
                sys.modules[mod_name] = types.ModuleType(mod_name)

            importlib.reload(app)
            assert app.app is None
        finally:
            for mod_name in blocker_names:
                if saved_modules[mod_name] is not None:
                    sys.modules[mod_name] = saved_modules[mod_name]
                else:
                    sys.modules.pop(mod_name, None)
            importlib.reload(app)


class TestMain:
    def test_main_exits_when_app_none(self, capsys):
        """main() should exit with code 1 when app is None."""
        original = app.app
        try:
            app.app = None
            with pytest.raises(SystemExit) as excinfo:
                app.main()
            assert excinfo.value.code == 1
            captured = capsys.readouterr()
            assert "fastapi" in captured.err
            assert "pydantic" in captured.err
        finally:
            app.app = original

    def test_main_runs_uvicorn_when_app_available(self):
        """main() should call uvicorn.run when app is not None."""
        original = app.app
        original_configs_dir = app.CONFIGS_DIR
        try:
            app.app = mock.Mock()
            fake_uvicorn = mock.Mock()
            with mock.patch.dict(sys.modules, {"uvicorn": fake_uvicorn}), mock.patch("sys.argv", ["app.py", "--host", "0.0.0.0", "--port", "9999"]):
                app.main()
            fake_uvicorn.run.assert_called_once()
            args, kwargs = fake_uvicorn.run.call_args
            assert args[0] is app.app
            assert kwargs["host"] == "0.0.0.0"
            assert kwargs["port"] == 9999
        finally:
            app.app = original
            app.CONFIGS_DIR = original_configs_dir

    def test_main_accepts_token_arg(self):
        """main() should accept --token and update WEBUI_TOKEN."""
        original = app.app
        original_configs_dir = app.CONFIGS_DIR
        original_token = app.WEBUI_TOKEN
        try:
            app.app = mock.Mock()
            fake_uvicorn = mock.Mock()
            with mock.patch.dict(sys.modules, {"uvicorn": fake_uvicorn}), mock.patch("sys.argv", ["app.py", "--token", "my-secret"]):
                app.main()
            assert app.WEBUI_TOKEN == "my-secret"
            fake_uvicorn.run.assert_called_once()
        finally:
            app.app = original
            app.CONFIGS_DIR = original_configs_dir
            app.WEBUI_TOKEN = original_token

    def test_main_block_runs_when_module_executed_as_script(self, capsys):
        """The `if __name__ == "__main__"` block should call main()."""
        import runpy
        module_path = str(Path(__file__).resolve().parent.parent / "webui" / "app.py")
        original_argv = sys.argv
        blocker_names = ["fastapi", "fastapi.responses", "fastapi.staticfiles", "pydantic"]
        saved_modules = {}
        try:
            # Pre-populate so the restore branch (saved_modules[mod_name] is not None) runs.
            sys.modules["fastapi"] = types.ModuleType("fastapi")
            for mod_name in blocker_names:
                saved_modules[mod_name] = sys.modules.get(mod_name)
                sys.modules[mod_name] = types.ModuleType(mod_name)
            sys.argv = ["app.py"]
            with pytest.raises(SystemExit) as excinfo:
                runpy.run_path(module_path, run_name="__main__")
            assert excinfo.value.code == 1
            captured = capsys.readouterr()
            assert "fastapi" in captured.err
        finally:
            sys.argv = original_argv
            for mod_name in blocker_names:
                if saved_modules[mod_name] is not None:
                    sys.modules[mod_name] = saved_modules[mod_name]
                else:
                    sys.modules.pop(mod_name, None)
