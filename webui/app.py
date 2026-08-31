"""Standalone FastAPI web UI for visualizing cronpypeline status.

Read-only dashboard over pipeline JSON configs. The only mutation is the
enable/disable toggle, which writes ``{"enabled": bool}`` to the pipeline's
``config_file``.

Run with:
    uvicorn app:app --port 8600
or:
    python app.py --configs-dir ../configs --port 8600
"""

import argparse
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from cronpypeline.config import PipelineConfig
from cronpypeline.state import PipelineState
from cronpypeline.targets import load_targets_with_config

HERE = Path(__file__).resolve().parent
CONFIGS_DIR = Path(os.environ.get("CRONPYPELINE_CONFIGS_DIR", HERE.parent / "configs")).resolve()

app = FastAPI(title="cronpypeline dashboard", version="0.1.0")


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _resolve_config_path(name: str) -> Path:
    """Resolve a config name to a path inside CONFIGS_DIR, rejecting traversal.

    :param name: Config filename (e.g. ``swe_pipeline.json``).
    :returns: Resolved absolute path.
    :raises HTTPException: If the name is invalid or the file does not exist.
    """
    if not name.endswith(".json"):
        raise HTTPException(status_code=400, detail="Config name must end with .json")
    path = (CONFIGS_DIR / name).resolve()
    if not path.is_relative_to(CONFIGS_DIR):
        raise HTTPException(status_code=400, detail="Invalid config name")
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Config not found: {name}")
    return path


def _load_config(name: str) -> PipelineConfig:
    """Load a PipelineConfig by name.

    :param name: Config filename.
    :returns: Parsed pipeline config.
    :raises HTTPException: If the config cannot be parsed.
    """
    path = _resolve_config_path(name)
    try:
        return PipelineConfig.from_file(path)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Failed to parse config: {e}")


def _config_toggle_path(config: PipelineConfig) -> Path | None:
    """Resolve the enable/disable toggle file path for a pipeline config.

    :param config: Pipeline config.
    :returns: Absolute toggle file path, or None if no config_file is set.
    """
    if not config.config_file:
        return None
    path = Path(config.config_file)
    if not path.is_absolute():
        path = Path(config.workspace_dir) / path
    return path


def _read_enabled(config: PipelineConfig) -> bool | None:
    """Read the enabled state from the pipeline's config_file toggle.

    :param config: Pipeline config.
    :returns: True/False, or None if no toggle file is configured.
    """
    toggle = _config_toggle_path(config)
    if toggle is None:
        return None
    if not toggle.exists():
        return True  # missing toggle file == enabled (matches tick behavior)
    try:
        data = json.loads(toggle.read_text())
        return data.get("enabled") is not False
    except (json.JSONDecodeError, OSError):
        return True  # unreadable toggle == enabled (matches tick behavior)


def _read_mode(config: PipelineConfig) -> str | None:
    """Read the current mode from the pipeline's mode_file.

    :param config: Pipeline config.
    :returns: Current mode string, or None.
    """
    if not config.mode_file:
        return None
    path = Path(config.mode_file)
    if not path.is_absolute():
        path = Path(config.workspace_dir) / path
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return data.get("mode")
    except (json.JSONDecodeError, OSError):
        return None


def _active_stages(config: PipelineConfig, mode: str | None) -> list:
    """Return enabled stages active in the given mode.

    :param config: Pipeline config.
    :param mode: Current pipeline mode, or None.
    :returns: List of active Stage objects.
    """
    active = []
    for stage in config.stages:
        if not stage.enabled:
            continue
        if stage.modes and (mode is None or mode not in stage.modes):
            continue
        active.append(stage)
    return active


def _serialize_stage(stage: Any) -> dict[str, Any]:
    """Serialize a Stage for the frontend.

    :param stage: Stage object.
    :returns: JSON-safe dict of stage metadata.
    """
    return {
        "id": stage.id,
        "name": stage.name,
        "trigger_type": stage.trigger.type.value,
        "action_type": stage.action.type.value,
        "chain": stage.chain,
        "timeout_minutes": stage.timeout_minutes,
        "max_retries": stage.max_retries,
        "max_rejections": stage.max_rejections,
        "enabled": stage.enabled,
        "modes": stage.modes,
        "has_markers": bool(stage.markers),
        "marker_roles": sorted(stage.markers.keys()),
        "callable": stage.action.params.get("callable"),
        "agent": stage.action.params.get("agent"),
    }


# ─── API routes ──────────────────────────────────────────────────────────────


@app.get("/api/configs")
def list_configs() -> dict[str, Any]:
    """List available pipeline config JSON files.

    :returns: Dict with configs dir and sorted list of config filenames.
    """
    if not CONFIGS_DIR.is_dir():
        return {"configs_dir": str(CONFIGS_DIR), "configs": []}
    configs = sorted(p.name for p in CONFIGS_DIR.glob("*.json") if p.is_file())
    return {"configs_dir": str(CONFIGS_DIR), "configs": configs}


@app.get("/api/pipeline")
def pipeline_info(config: str = Query(...)) -> dict[str, Any]:
    """Static pipeline metadata for a config: name, stages, targets, mode, enabled.

    :param config: Config filename.
    :returns: Pipeline metadata dict.
    """
    cfg = _load_config(config)
    mode = _read_mode(cfg)
    active_ids = {s.id for s in _active_stages(cfg, mode)}

    targets: list[dict[str, Any]] = []
    targets_error: str | None = None
    try:
        targets = [{"name": t.name, "config": t.config} for t in load_targets_with_config(cfg.targets)]
    except Exception as e:  # noqa: BLE001
        targets_error = str(e)

    return {
        "name": cfg.name,
        "workspace_dir": cfg.workspace_dir,
        "workspace_exists": Path(cfg.workspace_dir).is_dir(),
        "mode": mode,
        "mode_file": cfg.mode_file,
        "target_lock": cfg.target_lock,
        "has_toggle": cfg.config_file is not None,
        "enabled": _read_enabled(cfg),
        "stages": [
            {**_serialize_stage(s), "active": s.id in active_ids}
            for s in cfg.stages
        ],
        "targets": targets,
        "targets_error": targets_error,
    }


@app.get("/api/status")
def pipeline_status(config: str = Query(...)) -> dict[str, Any]:
    """Live per-target, per-stage state derived from filesystem markers.

    :param config: Config filename.
    :returns: Dict with per-target stage states and summary counts.
    """
    cfg = _load_config(config)
    mode = _read_mode(cfg)
    stages = _active_stages(cfg, mode)

    workspace = Path(cfg.workspace_dir)
    if not workspace.is_dir():
        return {
            "error": f"Workspace directory not found: {workspace}",
            "targets": {},
            "summary": {},
            "mode": mode,
            "enabled": _read_enabled(cfg),
        }

    try:
        target_objs = load_targets_with_config(cfg.targets)
    except Exception as e:  # noqa: BLE001
        return {
            "error": f"Failed to load targets: {e}",
            "targets": {},
            "summary": {},
            "mode": mode,
            "enabled": _read_enabled(cfg),
        }

    target_names = [t.name for t in target_objs]
    target_configs = {t.name: t.config for t in target_objs}

    state = PipelineState(workspace_dir=workspace, stages=stages, target_lock=cfg.target_lock)
    state.derive(target_names, target_configs=target_configs)

    result: dict[str, Any] = {}
    n_processing = n_stale = n_given_up = n_complete = n_total = 0
    for target, ts in state.target_states.items():
        stage_states: dict[str, Any] = {}
        for stage_id, ss in ts.stage_states.items():
            if not ss.stage.markers:
                stage_states[stage_id] = {"stateless": True}
                continue
            n_total += 1
            n_complete += ss.is_complete
            n_processing += ss.is_processing
            n_stale += ss.is_stale
            n_given_up += ss.is_given_up
            stage_states[stage_id] = {
                "stateless": False,
                "complete": ss.is_complete,
                "processing": ss.is_processing,
                "given_up": ss.is_given_up,
                "stale": ss.is_stale,
                "rejected": ss.is_rejected,
                "retry_count": ss.retry_count,
                "rejection_count": ss.rejection_count,
                "processing_data": ss.processing_data,
            }
        first = ts.first_actionable_stage
        result[target] = {
            "stages": stage_states,
            "has_processing": ts.has_processing,
            "next_actionable": first.stage.id if first else None,
            "target_dir_exists": (workspace / target).is_dir(),
        }

    return {
        "error": None,
        "targets": result,
        "summary": {
            "targets": len(target_names),
            "tracked_stages": n_total,
            "complete": n_complete,
            "processing": n_processing,
            "stale": n_stale,
            "given_up": n_given_up,
        },
        "mode": mode,
        "enabled": _read_enabled(cfg),
    }


class ToggleRequest(BaseModel):
    """Request body for the enable/disable toggle.

    :ivar enabled: Desired pipeline enabled state.
    """

    enabled: bool


@app.post("/api/toggle")
def toggle_pipeline(body: ToggleRequest, config: str = Query(...)) -> dict[str, Any]:
    """Enable or disable a pipeline by writing its config_file toggle.

    :param body: Request body with the desired enabled state.
    :param config: Config filename.
    :returns: Dict with the new enabled state.
    :raises HTTPException: If the pipeline has no config_file toggle.
    """
    cfg = _load_config(config)
    toggle = _config_toggle_path(cfg)
    if toggle is None:
        raise HTTPException(status_code=409, detail="This pipeline has no config_file toggle")

    existing: dict[str, Any] = {}
    if toggle.exists():
        try:
            data = json.loads(toggle.read_text())
            if isinstance(data, dict):
                existing = data
        except (json.JSONDecodeError, OSError):
            pass
    existing["enabled"] = body.enabled

    try:
        toggle.parent.mkdir(parents=True, exist_ok=True)
        toggle.write_text(json.dumps(existing, indent=2) + "\n")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Failed to write toggle file: {e}")

    return {"enabled": body.enabled, "toggle_file": str(toggle)}


# ─── Static frontend ─────────────────────────────────────────────────────────


@app.get("/")
def index() -> FileResponse:
    """Serve the dashboard page.

    :returns: The index.html file response.
    """
    return FileResponse(HERE / "static" / "index.html")


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="cronpypeline dashboard")
    parser.add_argument("--configs-dir", default=str(CONFIGS_DIR), help="Directory with pipeline JSON configs")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8600)
    args = parser.parse_args()

    CONFIGS_DIR = Path(args.configs_dir).resolve()
    uvicorn.run(app, host=args.host, port=args.port)
