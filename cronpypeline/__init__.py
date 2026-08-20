"""cronpypeline — cron-friendly, stateful, multi-stage agentic pipelines driven by JSON configuration."""

__version__ = "0.1.0"

from cronpypeline.pipeline import Pipeline, TickResult, TickResultStatus
from cronpypeline.config import (
    PipelineConfig,
    Stage,
    TriggerCondition,
    TriggerType,
    ActionSpec,
    ActionType,
    MarkerSpec,
    TargetSpec,
    TargetType,
    ActionHandlerConfig,
)
from cronpypeline.lock import FileLock
from cronpypeline.state import PipelineState, StageState, TargetState
from cronpypeline.markers import MarkerType, create_marker, read_marker, marker_exists, delete_marker
from cronpypeline.actions import ActionHandler, TickContext, ActionResult, execute_action, register_handler
from cronpypeline.triggers import evaluate_trigger
from cronpypeline.targets import load_targets, load_targets_with_config, Target

__all__ = [
    "Pipeline",
    "TickResult",
    "TickResultStatus",
    "PipelineConfig",
    "Stage",
    "TriggerCondition",
    "TriggerType",
    "ActionSpec",
    "ActionType",
    "MarkerSpec",
    "TargetSpec",
    "TargetType",
    "ActionHandlerConfig",
    "FileLock",
    "PipelineState",
    "StageState",
    "TargetState",
    "MarkerType",
    "create_marker",
    "read_marker",
    "marker_exists",
    "delete_marker",
    "ActionHandler",
    "TickContext",
    "ActionResult",
    "execute_action",
    "register_handler",
    "evaluate_trigger",
    "load_targets",
    "load_targets_with_config",
    "Target",
]
