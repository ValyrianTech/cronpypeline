"""Pipeline — core orchestration class for cronpypeline.

Each tick:
1. Acquire lock
2. Check enabled
3. Derive state from filesystem
4. Walk stages in order → first match executes one action
5. Chain if configured + mechanical
6. Release lock → exit
"""

import json
import time
from dataclasses import dataclass, field as dc_field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from cronpypeline.config import PipelineConfig, Stage, ActionType
from cronpypeline.lock import FileLock
from cronpypeline.markers import create_marker, delete_marker, marker_exists, read_marker
from cronpypeline.state import PipelineState, StageState
from cronpypeline.targets import load_targets, load_targets_with_config
from cronpypeline.triggers import evaluate_trigger, resolve_custom_callable
from cronpypeline.actions import TickContext, ActionResult, execute_action, register_handler, ActionHandler


class TickResultStatus(str, Enum):
    ACTION_EXECUTED = "action_executed"
    ACTION_FAILED = "action_failed"
    NO_WORK = "no_work"
    DRY_RUN = "dry_run"
    GAVE_UP = "gave_up"
    LOCK_FAILED = "lock_failed"
    DISABLED = "disabled"


@dataclass
class TickResult:
    """Result of a single tick for a single target."""
    target: str
    stage_id: Optional[str]
    status: TickResultStatus
    message: str = ""
    stdout: str = ""
    stderr: str = ""
    chained_stages: list[str] = dc_field(default_factory=list)

    def __str__(self) -> str:
        stage = self.stage_id or "-"
        return f"{self.target} | {stage} -> {self.status.value} | {self.message}"


def _instantiate_action_handler(handler_type: str, params: dict) -> ActionHandler:
    """Factory: instantiate an action handler from config type + params."""
    if handler_type == "conversation_queue":
        from cronpypeline.plugins.conversation_queue import ConversationQueueHandler
        return ConversationQueueHandler(**params)
    raise ValueError(f"Unknown action handler type: {handler_type}")


def _build_marker_context(target: str, target_dir: Path, workspace_dir: Path, target_config: dict) -> dict:
    """Build context dict for marker template substitution.

    Flattens target_config keys into the top-level context so they can be
    used directly in templates (e.g. {slug} instead of {target_config[slug]}).
    """
    ctx = {
        "target": target,
        "target_dir": str(target_dir),
        "workspace_dir": str(workspace_dir),
        "target_config": target_config,
    }
    # Flatten target_config keys (non-conflicting ones only)
    for k, v in target_config.items():
        if k not in ctx:
            ctx[k] = v
    return ctx


class Pipeline:
    """Cron-friendly pipeline orchestrator."""

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self.workspace_dir = Path(config.workspace_dir)
        self.lock = FileLock(
            self.workspace_dir / config.lock_file,
            dry_run=False,
        )

        # Wire action handler from config if present
        if config.action_handler:
            handler = _instantiate_action_handler(
                config.action_handler.type,
                config.action_handler.params,
            )
            register_handler(ActionType.QUEUE_AGENT, handler)

    @classmethod
    def from_config(cls, path: Path | str) -> "Pipeline":
        """Create a Pipeline from a JSON config file."""
        config = PipelineConfig.from_file(path)
        return cls(config)

    def tick(
        self,
        target: Optional[str] = None,
        dry_run: bool = False,
        verbose: bool = False,
    ) -> TickResult:
        """Execute one tick of the pipeline.

        Args:
            target: Limit to a single target. If None, picks first target with work.
            dry_run: Show planned action without executing.
            verbose: Verbose output.

        Returns:
            TickResult describing what happened.
        """
        # Determine targets with config
        if target is None:
            target_objs = load_targets_with_config(self.config.targets)
            targets = [t.name for t in target_objs]
            target_config_map = {t.name: t.config for t in target_objs}
        else:
            targets = [target]
            target_config_map = {target: {}}
            # Try to enrich config from registry
            all_targets = load_targets_with_config(self.config.targets)
            for t in all_targets:
                if t.name == target:
                    target_config_map[target] = t.config
                    break

        # Acquire lock (skip in dry-run)
        self.lock.dry_run = dry_run
        if not self.lock.acquire():
            return TickResult(
                target=target or "*",
                stage_id=None,
                status=TickResultStatus.LOCK_FAILED,
                message="Could not acquire lock",
            )

        try:
            return self._tick_inner(targets, target_config_map, dry_run, verbose)
        finally:
            self.lock.release()

    def tick_all(
        self,
        dry_run: bool = False,
        verbose: bool = False,
    ) -> list[TickResult]:
        """Execute one tick per target (all targets with work).

        Returns:
            List of TickResults, one per target that had work.
        """
        target_objs = load_targets_with_config(self.config.targets)
        targets = [t.name for t in target_objs]
        target_config_map = {t.name: t.config for t in target_objs}

        self.lock.dry_run = dry_run
        if not self.lock.acquire():
            return [TickResult(
                target="*",
                stage_id=None,
                status=TickResultStatus.LOCK_FAILED,
                message="Could not acquire lock",
            )]

        try:
            results = []
            for t in targets:
                result = self._tick_single(t, target_config_map.get(t, {}), dry_run, verbose)
                if result.status != TickResultStatus.NO_WORK:
                    results.append(result)
            return results
        finally:
            self.lock.release()

    def _tick_inner(
        self,
        targets: list[str],
        target_config_map: dict[str, dict],
        dry_run: bool,
        verbose: bool,
    ) -> TickResult:
        """Inner tick logic after lock is acquired."""
        # Check config_file enabled toggle
        if self.config.config_file:
            toggle_path = Path(self.config.config_file)
            if toggle_path.exists():
                try:
                    toggle_data = json.loads(toggle_path.read_text())
                    if toggle_data.get("enabled") is False:
                        return TickResult(
                            target="*",
                            stage_id=None,
                            status=TickResultStatus.DISABLED,
                            message="Pipeline disabled by config_file",
                        )
                except (json.JSONDecodeError, OSError):
                    pass  # Treat unreadable config as enabled

        if target_is_none := not targets:
            return TickResult(
                target="*",
                stage_id=None,
                status=TickResultStatus.NO_WORK,
                message="No targets configured",
            )

        # If multiple targets, pick the first one with work
        if len(targets) > 1:
            current_mode = self._get_current_mode()
            active_stages = []
            for stage in self.config.stages:
                if not stage.enabled:
                    continue
                if stage.modes and current_mode is not None and current_mode not in stage.modes:
                    continue
                active_stages.append(stage)
            state = PipelineState(workspace_dir=self.workspace_dir, stages=active_stages, target_lock=self.config.target_lock)
            state.derive(targets, target_configs=target_config_map)
            target = state.get_target_with_work(targets)
            if target is None:
                return TickResult(
                    target="*",
                    stage_id=None,
                    status=TickResultStatus.NO_WORK,
                    message="No targets with pending work",
                )
        else:
            target = targets[0]

        return self._tick_single(target, target_config_map.get(target, {}), dry_run, verbose)

    def _tick_single(
        self,
        target: str,
        target_config: dict,
        dry_run: bool,
        verbose: bool,
    ) -> TickResult:
        """Execute a tick for a single target, with pre/post hooks."""
        target_dir = self.workspace_dir / target
        target_dir.mkdir(parents=True, exist_ok=True)

        hook_context = {
            "target": target,
            "target_dir": str(target_dir),
            "workspace_dir": str(self.workspace_dir),
            "target_config": target_config,
            "stage_id": None,
        }

        # Pre-tick hook
        if self.config.pre_tick:
            hook_fn = resolve_custom_callable(self.config.pre_tick.callable)
            proceed = hook_fn(hook_context)
            if proceed is False:
                return TickResult(
                    target=target,
                    stage_id=None,
                    status=TickResultStatus.NO_WORK,
                    message="Tick skipped by pre_tick hook",
                )

        result = self._tick_single_inner(target, target_config, dry_run, verbose)

        # Post-tick hook
        if self.config.post_tick:
            hook_context["stage_id"] = result.stage_id
            hook_fn = resolve_custom_callable(self.config.post_tick.callable)
            hook_fn(hook_context, result)

        return result

    def _get_current_mode(self) -> Optional[str]:
        """Read the current mode from mode_file. Returns None if no mode_file or unreadable."""
        if not self.config.mode_file:
            return None
        mode_path = Path(self.config.mode_file)
        if not mode_path.exists():
            return None
        try:
            data = json.loads(mode_path.read_text())
            return data.get("mode")
        except (json.JSONDecodeError, OSError):
            return None

    def _tick_single_inner(
        self,
        target: str,
        target_config: dict,
        dry_run: bool,
        verbose: bool,
    ) -> TickResult:
        """Execute a tick for a single target (without hooks)."""
        target_dir = self.workspace_dir / target
        target_dir.mkdir(parents=True, exist_ok=True)

        # Derive state
        current_mode = self._get_current_mode()
        active_stages = []
        for stage in self.config.stages:
            if not stage.enabled:
                continue
            if stage.modes and current_mode is not None and current_mode not in stage.modes:
                continue
            active_stages.append(stage)

        state = PipelineState(workspace_dir=self.workspace_dir, stages=active_stages, target_lock=self.config.target_lock)
        state.derive([target], target_configs={target: target_config})
        target_state = state.target_states.get(target)

        if target_state is None:
            return TickResult(
                target=target,
                stage_id=None,
                status=TickResultStatus.NO_WORK,
                message="No state derived",
            )

        # Check for rejection give-up (rejection_count >= max_rejections)
        for stage_id, ss in target_state.stage_states.items():
            if ss.is_rejected and ss.stage.max_rejections > 0:
                marker_ctx = _build_marker_context(target, target_dir, self.workspace_dir, target_config)
                if ss.rejection_count >= ss.stage.max_rejections:
                    # Give up
                    if "give_up" in ss.stage.markers:
                        create_marker(ss.stage.markers["give_up"], target_dir, context=marker_ctx)
                    if "rejection" in ss.stage.markers:
                        delete_marker(ss.stage.markers["rejection"], target_dir, context=marker_ctx)
                    return TickResult(
                        target=target,
                        stage_id=ss.stage.id,
                        status=TickResultStatus.GAVE_UP,
                        message=f"Stage {ss.stage.id} gave up after {ss.rejection_count} rejections",
                    )
                else:
                    # Below max — clear rejection marker so stage can be re-processed
                    if "rejection" in ss.stage.markers:
                        delete_marker(ss.stage.markers["rejection"], target_dir, context=marker_ctx)
                    ss.is_rejected = False  # Allow re-processing

        # Check for stale processing markers and handle them
        for stage_id, ss in target_state.stage_states.items():
            if ss.is_stale and ss.is_processing:
                return self._handle_stale(ss, target, target_dir, target_config, dry_run, verbose)

        # Find first actionable stage whose trigger condition is met
        trigger_context = {
            "target": target,
            "target_dir": str(target_dir),
            "workspace_dir": str(self.workspace_dir),
            "target_config": target_config,
        }
        stage_state = None
        # If target_lock is enabled, no stage is actionable while any stage is processing
        if not (self.config.target_lock and target_state.has_processing):
            for stage in active_stages:
                ss = target_state.stage_states.get(stage.id)
                if ss and ss.is_actionable:
                    if evaluate_trigger(stage.trigger, target_dir, context=trigger_context):
                        stage_state = ss
                        break
        if stage_state is None:
            return TickResult(
                target=target,
                stage_id=None,
                status=TickResultStatus.NO_WORK,
                message="All stages complete or blocked",
            )

        # Dry run
        if dry_run:
            return TickResult(
                target=target,
                stage_id=stage_state.stage.id,
                status=TickResultStatus.DRY_RUN,
                message=f"Would execute {stage_state.stage.name}",
            )

        # Create processing marker for async actions (queue_agent)
        stage = stage_state.stage
        marker_ctx = _build_marker_context(target, target_dir, self.workspace_dir, target_config)
        if stage.action.type == ActionType.QUEUE_AGENT and "processing" in stage.markers:
            processing_spec = stage.markers["processing"]
            # Preserve retry count if re-queueing
            retry_count = 0
            if stage_state.processing_data and "retry_count" in stage_state.processing_data:
                retry_count = stage_state.processing_data["retry_count"]
            processing_spec.content = {**processing_spec.content, "retry_count": retry_count}
            create_marker(processing_spec, target_dir, context=marker_ctx)

        # Execute action
        ctx = TickContext(
            target=target,
            workspace_dir=self.workspace_dir,
            dry_run=dry_run,
            verbose=verbose,
            target_config=target_config,
        )
        result = execute_action(stage.action, ctx)

        # Update processing marker with result data (for stale detection and tracking)
        if stage.action.type == ActionType.QUEUE_AGENT and "processing" in stage.markers and result.success:
            if result.data:
                processing_spec = stage.markers["processing"]
                retry_count = processing_spec.content.get("retry_count", 0)
                processing_spec.content = {
                    **processing_spec.content,
                    "retry_count": retry_count,
                    **result.data,
                }
                create_marker(processing_spec, target_dir, context=marker_ctx)

        if not result.success:
            # Run on_fail if configured
            if stage.on_fail:
                fail_ctx = TickContext(
                    target=target,
                    workspace_dir=self.workspace_dir,
                    dry_run=dry_run,
                    verbose=verbose,
                    target_config=target_config,
                )
                execute_action(stage.on_fail, fail_ctx)
            return TickResult(
                target=target,
                stage_id=stage.id,
                status=TickResultStatus.ACTION_FAILED,
                message=f"Action failed: {result.stderr or result.stdout}",
                stdout=result.stdout,
                stderr=result.stderr,
            )

        # Create produced markers
        for marker_spec in stage.action.produces:
            create_marker(marker_spec, target_dir, context=marker_ctx)

        # Create completion marker for sync actions (command, subprocess, custom)
        if stage.action.type != ActionType.QUEUE_AGENT and "completion" in stage.markers:
            create_marker(stage.markers["completion"], target_dir, context=marker_ctx)

        # Invalidate markers from other stages
        for inv_spec in stage.invalidates:
            delete_marker(inv_spec, target_dir, context=marker_ctx)

        # Handle chaining
        chained = []
        if stage.chain and stage.action.type != ActionType.QUEUE_AGENT:
            chained_result = self._try_chain(target, target_dir, dry_run, verbose, stage)
            if chained_result:
                chained = chained_result[1]
                final_stage_id = chained_result[0]
                return TickResult(
                    target=target,
                    stage_id=final_stage_id,
                    status=TickResultStatus.ACTION_EXECUTED,
                    message=f"Chained through stages",
                    chained_stages=chained,
                )

        return TickResult(
            target=target,
            stage_id=stage.id,
            status=TickResultStatus.ACTION_EXECUTED,
            message=f"Executed {stage.name}",
            stdout=result.stdout,
            chained_stages=chained,
        )

    def _try_chain(
        self,
        target: str,
        target_dir: Path,
        dry_run: bool,
        verbose: bool,
        completed_stage: Stage,
    ) -> Optional[tuple[str, list[str]]]:
        """Attempt to chain to the next stage in the same tick.

        Returns (final_stage_id, list_of_chained_stage_ids) or None.
        """
        stages = self.config.stages
        completed_idx = next(
            (i for i, s in enumerate(stages) if s.id == completed_stage.id), -1
        )

        chained = []
        current_stage = completed_stage

        for i in range(completed_idx + 1, len(stages)):
            next_stage = stages[i]
            if not next_stage.enabled:
                continue

            # Check if trigger fires
            trigger_context = {
                "target": target,
                "target_dir": str(target_dir),
                "workspace_dir": str(self.workspace_dir),
                "target_config": {},
            }
            if not evaluate_trigger(next_stage.trigger, target_dir, context=trigger_context):
                break

            # Only chain mechanical (non-queue_agent) actions
            if next_stage.action.type == ActionType.QUEUE_AGENT:
                break

            ctx = TickContext(
                target=target,
                workspace_dir=self.workspace_dir,
                dry_run=dry_run,
                verbose=verbose,
            )
            result = execute_action(next_stage.action, ctx)

            if not result.success:
                break

            # Create produced markers
            for marker_spec in next_stage.action.produces:
                create_marker(marker_spec, target_dir, context=_build_marker_context(target, target_dir, self.workspace_dir, {}))

            # Create completion marker
            if "completion" in next_stage.markers:
                create_marker(next_stage.markers["completion"], target_dir, context=_build_marker_context(target, target_dir, self.workspace_dir, {}))

            # Invalidate markers from other stages
            chain_marker_ctx = _build_marker_context(target, target_dir, self.workspace_dir, {})
            for inv_spec in next_stage.invalidates:
                delete_marker(inv_spec, target_dir, context=chain_marker_ctx)

            chained.append(next_stage.id)
            current_stage = next_stage

            if not next_stage.chain:
                break

        if chained:
            return (current_stage.id, chained)
        return None

    def _handle_stale(
        self,
        stage_state: StageState,
        target: str,
        target_dir: Path,
        target_config: dict,
        dry_run: bool,
        verbose: bool,
    ) -> TickResult:
        """Handle a stale processing marker."""
        stage = stage_state.stage
        retry_count = stage_state.retry_count

        # Clean up stale marker
        marker_ctx = _build_marker_context(target, target_dir, self.workspace_dir, target_config)
        if "processing" in stage.markers:
            delete_marker(stage.markers["processing"], target_dir, context=marker_ctx)

        if retry_count >= stage.max_retries:
            # Give up
            if "give_up" in stage.markers:
                create_marker(stage.markers["give_up"], target_dir, context=marker_ctx)
            return TickResult(
                target=target,
                stage_id=stage.id,
                status=TickResultStatus.GAVE_UP,
                message=f"Stage {stage.id} gave up after {retry_count} retries",
            )

        # Re-queue with incremented retry count
        if dry_run:
            return TickResult(
                target=target,
                stage_id=stage.id,
                status=TickResultStatus.DRY_RUN,
                message=f"Would re-queue stale stage {stage.id} (retry {retry_count + 1})",
            )

        # Create new processing marker with incremented retry count
        if "processing" in stage.markers:
            processing_spec = stage.markers["processing"]
            processing_spec.content = {**processing_spec.content, "retry_count": retry_count + 1}
            create_marker(processing_spec, target_dir, context=marker_ctx)

        # Re-execute the action
        ctx = TickContext(
            target=target,
            workspace_dir=self.workspace_dir,
            dry_run=dry_run,
            verbose=verbose,
            target_config=target_config,
            retry_count=retry_count + 1,
            retry_data=stage_state.processing_data,
        )
        result = execute_action(stage.action, ctx)

        # Update processing marker with result data
        if result.success and result.data and "processing" in stage.markers:
            processing_spec = stage.markers["processing"]
            processing_spec.content = {
                **processing_spec.content,
                "retry_count": retry_count + 1,
                **result.data,
            }
            create_marker(processing_spec, target_dir, context=marker_ctx)

        return TickResult(
            target=target,
            stage_id=stage.id,
            status=TickResultStatus.ACTION_EXECUTED,
            message=f"Re-queued stale stage (retry {retry_count + 1})",
            stdout=result.stdout,
        )

    def status(self, targets: Optional[list[str]] = None) -> dict:
        """Print pipeline state and exit (no actions)."""
        if targets is None:
            targets = load_targets(self.config.targets)

        state = PipelineState(workspace_dir=self.workspace_dir, stages=self.config.stages)
        state.derive(targets)

        result = {}
        for target, ts in state.target_states.items():
            stages = {}
            for stage_id, ss in ts.stage_states.items():
                stages[stage_id] = {
                    "complete": ss.is_complete,
                    "processing": ss.is_processing,
                    "given_up": ss.is_given_up,
                    "stale": ss.is_stale,
                    "retry_count": ss.retry_count,
                }
            result[target] = stages
        return result
