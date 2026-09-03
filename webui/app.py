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
import re
import sys
from pathlib import Path
from typing import Any

from cronpypeline.config import PipelineConfig
from cronpypeline.state import PipelineState
from cronpypeline.targets import load_targets_with_config

HERE = Path(__file__).resolve().parent
CONFIGS_DIR = Path(os.environ.get("CRONPYPELINE_CONFIGS_DIR", HERE.parent / "configs")).resolve()
WEBUI_TOKEN = os.environ.get("CRONPYPELINE_WEBUI_TOKEN", "")

# Cache for parsed log ticks, keyed by resolved log file path.
# Value: (mtime, size, sorted_ticks_list) where sorted_ticks_list is bounded to
# the most recent 500 ticks.
_log_ticks_cache: dict[str, tuple[float, int, list[dict[str, Any]]]] = {}


# ─── Helpers ─────────────────────────────────────────────────────────────────


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


def _is_path_within(path: Path, directory: Path) -> bool:
    """Return whether a path resides within a directory, resolving symlinks.

    Symlinks are resolved on both paths so that a symlink inside ``directory``
    pointing outside it is treated as outside, preventing symlink-based escapes.

    :param path: Path to check.
    :param directory: Directory that must contain the path.
    :returns: True if the resolved path is within the resolved directory.
    """
    resolved_path = Path(path).resolve()
    resolved_directory = Path(directory).resolve()
    return resolved_path.is_relative_to(resolved_directory)


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


def _read_log_ticks(config: PipelineConfig, limit: int = 50) -> list[dict[str, Any]]:
    """Read recent execution log ticks for a pipeline config.

    Parses the pipeline's JSONL execution log (``config.log_file``, resolved
    relative to ``config.workspace_dir``), groups entries by ``tick_id``, and
    returns the most recent ``limit`` ticks (sorted by tick_start timestamp,
    newest first).

    :param config: Pipeline config.
    :param limit: Maximum number of ticks to return.
    :returns: List of tick dicts, most recent first. Empty if no log file is
        configured, the file is missing, or it contains no ticks.
    """
    if not config.log_file:
        return []
    log_path = Path(config.log_file)
    if not log_path.is_absolute():
        log_path = Path(config.workspace_dir) / log_path

    cache_key = str(log_path)
    if not log_path.is_file():
        _log_ticks_cache.pop(cache_key, None)
        return []

    try:
        stat = log_path.stat()
    except OSError:
        _log_ticks_cache.pop(cache_key, None)
        return []
    mtime = stat.st_mtime
    size = stat.st_size

    cached = _log_ticks_cache.get(cache_key)
    if cached is not None:
        cached_mtime, cached_size, cached_ticks = cached
        if cached_mtime == mtime and cached_size == size:
            return cached_ticks[:limit]

    entries: list[dict[str, Any]] = []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
    except OSError:
        _log_ticks_cache.pop(cache_key, None)
        return []

    ticks: dict[str, dict[str, Any]] = {}
    for entry in entries:
        tick_id = entry.get("tick_id")
        if not tick_id:
            continue
        if tick_id not in ticks:
            ticks[tick_id] = {
                "tick_id": tick_id,
                "target": entry.get("target", ""),
                "start_time": "",
                "end_time": "",
                "total_duration_ms": 0,
                "stages_checked": 0,
                "actions_executed": 0,
                "failures": 0,
                "final_status": "",
                "final_stage_id": None,
                "dry_run": False,
                "stages": [],
            }
        tick = ticks[tick_id]
        event = entry.get("event")
        if event == "tick_start":
            tick["start_time"] = entry.get("timestamp", "")
            tick["dry_run"] = bool(entry.get("dry_run", False))
            tick["target"] = entry.get("target", tick["target"])
        elif event == "tick_end":
            tick["end_time"] = entry.get("timestamp", "")
            tick["total_duration_ms"] = entry.get("total_duration_ms", 0)
            tick["stages_checked"] = entry.get("stages_checked", 0)
            tick["actions_executed"] = entry.get("actions_executed", 0)
            tick["failures"] = entry.get("failures", 0)
            tick["final_status"] = entry.get("final_status", "")
            tick["final_stage_id"] = entry.get("final_stage_id")
        elif event == "stage":
            tick["stages"].append({
                "event": "stage",
                "stage_id": entry.get("stage_id", ""),
                "stage_name": entry.get("stage_name", ""),
                "result": entry.get("result", ""),
                "duration_ms": entry.get("duration_ms", 0),
                "stdout": entry.get("stdout", ""),
                "stderr": entry.get("stderr", ""),
                "action_command": entry.get("action_command", ""),
                "dry_run": entry.get("dry_run", False),
                "chained": entry.get("chained", False),
                "timestamp": entry.get("timestamp", ""),
            })

    result = list(ticks.values())
    result.sort(key=lambda t: t.get("start_time", ""), reverse=True)
    result = result[:500]
    _log_ticks_cache[cache_key] = (mtime, size, result)
    return result[:limit]


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


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON file, returning None if missing, unreadable, or not a dict.

    :param path: Path to the JSON file.
    :returns: Parsed dict, or None.
    """
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _swe_issue_counts(issues_dir: Path) -> dict[str, int] | None:
    """Count issues in .SWE/issues/ by frontmatter status.

    :param issues_dir: The .SWE/issues directory.
    :returns: Mapping of status -> count, or None if no issues.
    """
    if not issues_dir.is_dir():
        return None
    counts: dict[str, int] = {}
    for path in issues_dir.glob("*.md"):
        try:
            head = path.read_text(encoding="utf-8")[:800]
        except OSError:
            continue
        if not head.startswith("---"):
            continue
        m = re.search(r"(?m)^status:\s*(\S+)", head)
        status = m.group(1) if m else "unknown"
        counts[status] = counts.get(status, 0) + 1
    return counts or None


def _swe_state(target_dir: Path) -> dict[str, Any] | None:
    """Read SWE-pipeline plugin state for a target (PR, session, issues).

    These live in plugin-owned files under .SWE/ that the generic marker
    derivation does not see. Returns None for non-SWE targets.

    :param target_dir: Target repo directory.
    :returns: Dict with ``pr``, ``session``, and ``issues`` keys, or None.
    """
    swe_dir = target_dir / ".SWE"
    if not swe_dir.is_dir():
        return None

    pr = None
    pr_data = _read_json(swe_dir / "pr_published.json")
    if pr_data is not None:
        pr = {
            "pr_number": pr_data.get("pr_number"),
            "pr_url": pr_data.get("pr_url", ""),
            "pr_state": pr_data.get("pr_state", "open"),
            "pr_review_cycles": pr_data.get("pr_review_cycles", 0),
            "filed_issues": pr_data.get("filed_issues", []),
            "published_at": pr_data.get("published_at", ""),
            "merged_at": pr_data.get("merged_at", ""),
            "closed_at": pr_data.get("closed_at", ""),
        }

    session = None
    session_data = _read_json(swe_dir / "github_session.json")
    if session_data is not None:
        session = {
            "active": bool(session_data.get("active")),
            "issue_id": session_data.get("issue_id", ""),
            "github_number": session_data.get("github_number"),
            "completed": bool(session_data.get("completed")),
        }

    issues = _swe_issue_counts(swe_dir / "issues")

    if pr is None and session is None and issues is None:
        return None
    return {"pr": pr, "session": session, "issues": issues}


def _build_app():
    """Build the FastAPI application. Requires fastapi and pydantic installed."""
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel

    app = FastAPI(title="cronpypeline dashboard", version="0.1.0")

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

    # ─── API routes ──────────────────────────────────────────────────────────

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
                "swe": _swe_state(workspace / target),
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

    @app.get("/api/log")
    def pipeline_log(config: str = Query(...), limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        """Read recent execution log ticks for a pipeline config.

        :param config: Config filename.
        :param limit: Maximum number of ticks to return (1-500).
        :returns: Dict with ``ticks`` (most recent first) and ``count``.
        """
        cfg = _load_config(config)
        ticks = _read_log_ticks(cfg, limit=limit)
        return {"ticks": ticks, "count": len(ticks)}

    class ToggleRequest(BaseModel):
        """Request body for the enable/disable toggle.

        :ivar enabled: Desired pipeline enabled state.
        """

        enabled: bool

    @app.post("/api/toggle")
    def toggle_pipeline(body: ToggleRequest, config: str = Query(...), token: str = Query("")) -> dict[str, Any]:
        """Enable or disable a pipeline by writing its config_file toggle.

        :param body: Request body with the desired enabled state.
        :param config: Config filename.
        :param token: Auth token required to enable/disable a pipeline.
        :returns: Dict with the new enabled state.
        :raises HTTPException: If no auth token is configured (403), the token
            is invalid or missing (401), the pipeline has no config_file
            toggle (409), or the toggle path resolves outside the workspace
            and configs directory (400).
        """
        if not WEBUI_TOKEN:
            raise HTTPException(status_code=403, detail="Toggle is disabled: no auth token configured")
        if token != WEBUI_TOKEN:
            raise HTTPException(status_code=401, detail="Invalid or missing auth token")

        cfg = _load_config(config)
        toggle = _config_toggle_path(cfg)
        if toggle is None:
            raise HTTPException(status_code=409, detail="This pipeline has no config_file toggle")

        workspace_dir = Path(cfg.workspace_dir)
        if not _is_path_within(toggle, workspace_dir) and not _is_path_within(toggle, CONFIGS_DIR):
            raise HTTPException(status_code=400, detail="Toggle path is outside the workspace or configs directory")

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

    # ─── Static frontend ─────────────────────────────────────────────────────

    @app.get("/")
    def index() -> FileResponse:
        """Serve the dashboard page.

        :returns: The index.html file response.
        """
        return FileResponse(HERE / "static" / "index.html")

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    return app


try:
    app = _build_app()
except Exception:  # noqa: BLE001
    app = None


def main() -> None:
    """Run the dashboard server.

    Exits with a non-zero code if the app could not be built (e.g. missing
    fastapi/pydantic).
    """
    global CONFIGS_DIR, WEBUI_TOKEN

    if app is None:
        print("cronpypeline dashboard requires fastapi and pydantic. Install them with: pip install fastapi pydantic", file=sys.stderr)
        raise SystemExit(1)

    import uvicorn

    parser = argparse.ArgumentParser(description="cronpypeline dashboard")
    parser.add_argument("--configs-dir", default=str(CONFIGS_DIR), help="Directory with pipeline JSON configs")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8600)
    parser.add_argument("--token", default=WEBUI_TOKEN, help="Auth token required for the toggle endpoint")
    args = parser.parse_args()

    CONFIGS_DIR = Path(args.configs_dir).resolve()
    WEBUI_TOKEN = args.token
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
