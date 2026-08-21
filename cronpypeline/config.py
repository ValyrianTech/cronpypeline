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
    """Supported trigger condition types for stage activation."""
    FILE_MISSING = "file_missing"
    FILE_EXISTS = "file_exists"
    FILE_OLDER_THAN = "file_older_than"
    MARKER_STATE = "marker_state"
    QUEUE_EMPTY = "queue_empty"
    CUSTOM = "custom"
    AND = "and"
    OR = "or"


class ActionType(str, Enum):
    """Supported action types for stage execution."""
    COMMAND = "command"
    QUEUE_AGENT = "queue_agent"
    SUBPROCESS = "subprocess"
    HTTP_REQUEST = "http_request"
    CUSTOM = "custom"


class TargetType(str, Enum):
    """Supported target specification types."""
    REGISTRY = "registry"
    STATIC = "static"
    SINGLE = "single"


# ─── Trigger Condition ──────────────────────────────────────────────────────


@dataclass
class TriggerCondition:
    """When a stage should fire.

    :ivar type: The trigger type (file_missing, file_exists, etc.).
    :ivar path: File path for file-based triggers.
    :ivar minutes: Threshold in minutes for file_older_than.
    :ivar field: JSON field name for marker_state.
    :ivar op: Comparison operator for marker_state (eq, ne, lt, lte, gt, gte).
    :ivar value: Expected value for marker_state comparison.
    :ivar queue_dir: Queue directory path for queue_empty.
    :ivar callable: Dotted path to custom callable for custom triggers.
    :ivar conditions: Sub-conditions for and/or composite triggers.
    """
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
    def from_dict(cls, data: dict[str, Any]) -> "TriggerCondition":
        """Create a TriggerCondition from a JSON config dict.

        :param data: Dictionary with ``type`` and type-specific fields.
        :returns: A :class:`TriggerCondition` instance.
        :raises ValueError: If the trigger type is unknown.
        """
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
    """What to do when a stage triggers.

    :ivar type: The action type (command, queue_agent, subprocess, etc.).
    :ivar params: Type-specific parameters.
    :ivar timeout_seconds: Execution timeout in seconds.
    :ivar produces: Markers created on success.
    """
    type: ActionType
    params: dict[str, Any] = dc_field(default_factory=dict)
    timeout_seconds: Optional[int] = None
    produces: list[MarkerSpec] = dc_field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionSpec":
        """Create an ActionSpec from a JSON config dict.

        :param data: Dictionary with ``type`` and optional ``params``, ``timeout_seconds``, ``produces``.
        :returns: An :class:`ActionSpec` instance.
        :raises ValueError: If the action type is unknown.
        """
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
    """A single stage in the pipeline detector chain.

    :ivar id: Unique stage identifier (e.g. ``"A0"``, ``"C-select"``).
    :ivar name: Human-readable name.
    :ivar trigger: When this stage should fire.
    :ivar action: What to do when the stage triggers.
    :ivar chain: Whether same-tick chaining is allowed (mechanical only).
    :ivar timeout_minutes: Per-stage timeout for stale detection.
    :ivar max_retries: Max attempts before give-up.
    :ivar enabled: Whether this stage is active.
    :ivar markers: Completion/processing/give_up/rejection marker specs.
    :ivar on_fail: Revert/rollback action on failure.
    :ivar invalidates: Markers from other stages to delete on success.
    :ivar modes: Active modes for this stage (empty = always active).
    :ivar max_rejections: Max rejections before give-up (0 = disabled).
    """
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
    invalidates: list[MarkerSpec] = dc_field(default_factory=list)
    modes: list[str] = dc_field(default_factory=list)
    max_rejections: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Stage":
        """Create a Stage from a JSON config dict.

        :param data: Dictionary with ``id``, ``name``, ``trigger``, ``action``, and optional fields.
        :returns: A :class:`Stage` instance.
        """
        markers = {}
        for marker_role, marker_data in data.get("markers", {}).items():
            markers[marker_role] = MarkerSpec.from_dict(marker_data)

        on_fail = None
        if data.get("on_fail"):
            on_fail = ActionSpec.from_dict(data["on_fail"])

        invalidates = [MarkerSpec.from_dict(m) for m in data.get("invalidates", [])]
        modes = list(data.get("modes", []))
        max_rejections = data.get("max_rejections", 0)

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
            invalidates=invalidates,
            modes=modes,
            max_rejections=max_rejections,
        )


# ─── Target Spec ────────────────────────────────────────────────────────────


@dataclass
class TargetSpec:
    """How targets (repos, countries, etc.) are loaded.

    :ivar type: Target type (registry, static, single).
    :ivar file: Path to registry JSON file (registry type).
    :ivar key: Key in registry file (registry type).
    :ivar filter: Filter criteria for registry items.
    :ivar items: Fixed list of target names (static type).
    :ivar name: Single target name (single type).
    """
    type: TargetType
    file: Optional[str] = None
    key: Optional[str] = None
    filter: Optional[dict[str, Any]] = None
    items: Optional[list[str]] = None
    name: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TargetSpec":
        """Create a TargetSpec from a JSON config dict.

        :param data: Dictionary with ``type`` and type-specific fields.
        :returns: A :class:`TargetSpec` instance.
        :raises ValueError: If the target type is unknown.
        """
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
    """Configuration for the action handler plugin.

    :ivar type: Handler type identifier (e.g. ``"conversation_queue"``).
    :ivar params: Handler-specific parameters.
    """
    type: str
    params: dict[str, Any] = dc_field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionHandlerConfig":
        """Create an ActionHandlerConfig from a JSON config dict.

        :param data: Dictionary with ``type`` and optional ``params``.
        :returns: An :class:`ActionHandlerConfig` instance.
        """
        params = data.get("params", {})
        if not params:
            params = {k: v for k, v in data.items() if k != "type"}
        return cls(
            type=data["type"],
            params=params,
        )


# ─── Pipeline Config ────────────────────────────────────────────────────────


@dataclass
class HookConfig:
    """Configuration for a pre-tick or post-tick hook.

    :ivar callable: Dotted path to the hook callable (e.g. ``"my_plugin.pre_tick_sync"``).
    """
    callable: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HookConfig":
        """Create a HookConfig from a JSON config dict.

        :param data: Dictionary with ``callable``.
        :returns: A :class:`HookConfig` instance.
        """
        return cls(callable=data["callable"])


@dataclass
class PipelineConfig:
    """Top-level pipeline configuration loaded from JSON.

    :ivar name: Pipeline name.
    :ivar workspace_dir: Root workspace directory.
    :ivar stages: Ordered list of stage definitions.
    :ivar lock_file: Lock file path (relative to workspace).
    :ivar config_file: Optional pipeline config toggle file.
    :ivar targets: Target specification.
    :ivar action_handler: Action handler plugin config.
    :ivar log_file: Optional log file path.
    :ivar pre_tick: Pre-tick hook config.
    :ivar post_tick: Post-tick hook config.
    :ivar mode_file: Path to JSON file with ``{"mode": "..."}`` for mode switching.
    :ivar target_lock: Cross-stage lock — blocks all stages for a target while any stage is processing.
    """
    name: str
    workspace_dir: str
    stages: list[Stage]
    lock_file: str = "pipeline.lock"
    config_file: Optional[str] = None
    targets: Optional[TargetSpec] = None
    action_handler: Optional[ActionHandlerConfig] = None
    log_file: Optional[str] = None
    pre_tick: Optional[HookConfig] = None
    post_tick: Optional[HookConfig] = None
    mode_file: Optional[str] = None
    target_lock: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PipelineConfig":
        """Create a PipelineConfig from a parsed JSON dict.

        :param data: Dictionary with ``name``, ``workspace_dir``, ``stages``, and optional fields.
        :returns: A :class:`PipelineConfig` instance.
        :raises ValueError: If duplicate stage IDs are found.
        """
        stages = [Stage.from_dict(s) for s in data.get("stages", [])]

        # Validate: no duplicate stage IDs
        stage_ids = [s.id for s in stages]
        if len(stage_ids) != len(set(stage_ids)):
            seen: set[str] = set()
            dupes: list[str] = []
            for sid in stage_ids:
                if sid in seen:
                    dupes.append(sid)
                else:
                    seen.add(sid)
            raise ValueError(f"Duplicate stage id(s): {dupes}")

        targets = None
        if data.get("targets"):
            targets = TargetSpec.from_dict(data["targets"])

        action_handler = None
        if data.get("action_handler"):
            action_handler = ActionHandlerConfig.from_dict(data["action_handler"])

        pre_tick = None
        if data.get("pre_tick"):
            pre_tick = HookConfig.from_dict(data["pre_tick"])

        post_tick = None
        if data.get("post_tick"):
            post_tick = HookConfig.from_dict(data["post_tick"])

        return cls(
            name=data["name"],
            workspace_dir=data["workspace_dir"],
            stages=stages,
            lock_file=data.get("lock_file", "pipeline.lock"),
            config_file=data.get("config_file"),
            targets=targets,
            action_handler=action_handler,
            log_file=data.get("log_file"),
            pre_tick=pre_tick,
            post_tick=post_tick,
            mode_file=data.get("mode_file"),
            target_lock=data.get("target_lock", False),
        )

    @classmethod
    def from_file(cls, path: Optional[Path | str] = None) -> "PipelineConfig":
        """Load a PipelineConfig from a JSON file.

        :param path: Path to the JSON config file.
        :returns: A :class:`PipelineConfig` instance.
        :raises ValueError: If path is None.
        """
        if path is None:
            raise ValueError("Config file path is required")
        path = Path(path)
        data = json.loads(path.read_text())
        return cls.from_dict(data)
