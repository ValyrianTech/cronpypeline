"""Configuration dataclasses for cronpypeline.

PipelineConfig, Stage, TriggerCondition, ActionSpec, and related types
loaded from JSON configuration files.
"""

import json
from dataclasses import dataclass, field as dc_field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from cronpypeline.markers import MarkerSpec


# ─── Enums ──────────────────────────────────────────────────────────────────


class TriggerType(str, Enum):
    FILE_MISSING = "file_missing"
    FILE_EXISTS = "file_exists"
    FILE_OLDER_THAN = "file_older_than"
    MARKER_STATE = "marker_state"
    QUEUE_EMPTY = "queue_empty"
    CUSTOM = "custom"
    AND = "and"
    OR = "or"


class ActionType(str, Enum):
    COMMAND = "command"
    QUEUE_AGENT = "queue_agent"
    SUBPROCESS = "subprocess"
    HTTP_REQUEST = "http_request"
    CUSTOM = "custom"


class TargetType(str, Enum):
    REGISTRY = "registry"
    STATIC = "static"
    SINGLE = "single"


# ─── Trigger Condition ──────────────────────────────────────────────────────


@dataclass
class TriggerCondition:
    """When a stage should fire."""
    type: TriggerType
    path: Optional[str] = None
    minutes: Optional[int] = None
    field: Optional[str] = None
    op: Optional[str] = None
    value: Any = None
    queue_dir: Optional[str] = None
    callable: Optional[str] = None
    conditions: list["TriggerCondition"] = dc_field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "TriggerCondition":
        try:
            trigger_type = TriggerType(data["type"])
        except ValueError:
            raise ValueError(f"Unknown trigger type: {data['type']}")

        conditions = []
        if trigger_type in (TriggerType.AND, TriggerType.OR):
            conditions = [cls.from_dict(c) for c in data.get("conditions", [])]

        return cls(
            type=trigger_type,
            path=data.get("path"),
            minutes=data.get("minutes"),
            field=data.get("field"),
            op=data.get("op"),
            value=data.get("value"),
            queue_dir=data.get("queue_dir"),
            callable=data.get("callable"),
            conditions=conditions,
        )


# ─── Action Spec ────────────────────────────────────────────────────────────


@dataclass
class ActionSpec:
    """What to do when a stage triggers."""
    type: ActionType
    params: dict[str, Any] = dc_field(default_factory=dict)
    timeout_seconds: Optional[int] = None
    produces: list[MarkerSpec] = dc_field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "ActionSpec":
        try:
            action_type = ActionType(data["type"])
        except ValueError:
            raise ValueError(f"Unknown action type: {data['type']}")
        produces = [MarkerSpec.from_dict(m) for m in data.get("produces", [])]
        return cls(
            type=action_type,
            params=data.get("params", {}),
            timeout_seconds=data.get("timeout_seconds"),
            produces=produces,
        )


# ─── Stage ──────────────────────────────────────────────────────────────────


@dataclass
class Stage:
    """A single stage in the pipeline detector chain."""
    id: str
    name: str
    trigger: TriggerCondition
    action: ActionSpec
    chain: bool = False
    timeout_minutes: int = 30
    max_retries: int = 3
    enabled: bool = True
    markers: dict[str, MarkerSpec] = dc_field(default_factory=dict)
    on_fail: Optional[ActionSpec] = None

    @classmethod
    def from_dict(cls, data: dict) -> "Stage":
        markers = {}
        for marker_role, marker_data in data.get("markers", {}).items():
            markers[marker_role] = MarkerSpec.from_dict(marker_data)

        on_fail = None
        if data.get("on_fail"):
            on_fail = ActionSpec.from_dict(data["on_fail"])

        return cls(
            id=data["id"],
            name=data["name"],
            trigger=TriggerCondition.from_dict(data["trigger"]),
            action=ActionSpec.from_dict(data["action"]),
            chain=data.get("chain", False),
            timeout_minutes=data.get("timeout_minutes", 30),
            max_retries=data.get("max_retries", 3),
            enabled=data.get("enabled", True),
            markers=markers,
            on_fail=on_fail,
        )


# ─── Target Spec ────────────────────────────────────────────────────────────


@dataclass
class TargetSpec:
    """How targets (repos, countries, etc.) are loaded."""
    type: TargetType
    file: Optional[str] = None
    key: Optional[str] = None
    filter: Optional[dict] = None
    items: Optional[list[str]] = None
    name: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "TargetSpec":
        return cls(
            type=TargetType(data["type"]),
            file=data.get("file"),
            key=data.get("key"),
            filter=data.get("filter"),
            items=data.get("items"),
            name=data.get("name"),
        )


# ─── Action Handler Config ──────────────────────────────────────────────────


@dataclass
class ActionHandlerConfig:
    """Configuration for the action handler plugin."""
    type: str
    params: dict[str, Any] = dc_field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "ActionHandlerConfig":
        params = data.get("params", {})
        if not params:
            params = {k: v for k, v in data.items() if k != "type"}
        return cls(
            type=data["type"],
            params=params,
        )


# ─── Pipeline Config ────────────────────────────────────────────────────────


@dataclass
class PipelineConfig:
    """Top-level pipeline configuration loaded from JSON."""
    name: str
    workspace_dir: str
    stages: list[Stage]
    lock_file: str = "pipeline.lock"
    config_file: Optional[str] = None
    targets: Optional[TargetSpec] = None
    action_handler: Optional[ActionHandlerConfig] = None
    log_file: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "PipelineConfig":
        stages = [Stage.from_dict(s) for s in data.get("stages", [])]

        # Validate: no duplicate stage IDs
        stage_ids = [s.id for s in stages]
        if len(stage_ids) != len(set(stage_ids)):
            seen = set()
            dupes = [sid for sid in stage_ids if sid in seen or seen.add(sid)]
            raise ValueError(f"Duplicate stage id(s): {dupes}")

        targets = None
        if data.get("targets"):
            targets = TargetSpec.from_dict(data["targets"])

        action_handler = None
        if data.get("action_handler"):
            action_handler = ActionHandlerConfig.from_dict(data["action_handler"])

        return cls(
            name=data["name"],
            workspace_dir=data["workspace_dir"],
            stages=stages,
            lock_file=data.get("lock_file", "pipeline.lock"),
            config_file=data.get("config_file"),
            targets=targets,
            action_handler=action_handler,
            log_file=data.get("log_file"),
        )

    @classmethod
    def from_file(cls, path: Path | str) -> "PipelineConfig":
        path = Path(path)
        data = json.loads(path.read_text())
        return cls.from_dict(data)
