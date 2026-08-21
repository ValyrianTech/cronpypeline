"""cronpypeline — cron-friendly, stateful, multi-stage agentic pipelines driven by JSON configuration."""

__version__ = "0.1.0"

from cronpypeline.actions import (
    ActionHandler,
    ActionResult,
    TickContext,
    execute_action,
    register_handler,
)
from cronpypeline.config import (
    ActionHandlerConfig,
    ActionSpec,
    ActionType,
    MarkerSpec,
    PipelineConfig,
    Stage,
    TargetSpec,
    TargetType,
    TriggerCondition,
    TriggerType,
)
from cronpypeline.lock import FileLock
from cronpypeline.markers import (
    MarkerType,
    create_marker,
    delete_marker,
    marker_exists,
    read_marker,
)
from cronpypeline.pipeline import Pipeline, TickResult, TickResultStatus
from cronpypeline.state import PipelineState, StageState, TargetState
from cronpypeline.targets import Target, load_targets, load_targets_with_config
from cronpypeline.triggers import evaluate_trigger

__all__ = [
    "ActionHandler",
    "ActionHandlerConfig",
    "ActionResult",
    "ActionSpec",
    "ActionType",
    "FileLock",
    "MarkerSpec",
    "MarkerType",
    "Pipeline",
    "PipelineConfig",
    "PipelineState",
    "Stage",
    "StageState",
    "Target",
    "TargetSpec",
    "TargetState",
    "TargetType",
    "TickContext",
    "TickResult",
    "TickResultStatus",
    "TriggerCondition",
    "TriggerType",
    "create_marker",
    "delete_marker",
    "evaluate_trigger",
    "execute_action",
    "load_targets",
    "load_targets_with_config",
    "marker_exists",
    "read_marker",
    "register_handler",
]
