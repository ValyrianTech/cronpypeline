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
import traceback
from dataclasses import dataclass, replace
from dataclasses import field as dc_field
from enum import Enum
from pathlib import Path
from typing import Any

from cronpypeline.actions import (
    ActionHandler,
    TickContext,
    execute_action,
    register_handler,
)
from cronpypeline.config import ActionType, PipelineConfig, Stage
from cronpypeline.lock import FileLock
from cronpypeline.markers import (
    MarkerType,
    create_marker,
    delete_marker,
    marker_exists,
    read_marker,
)
from cronpypeline.state import PipelineState, StageState, TargetState
from cronpypeline.targets import load_targets, load_targets_with_config
from cronpypeline.triggers import evaluate_trigger, resolve_custom_callable


def _validate_target_name(target: str) -> str:
    """Validate a target name to prevent path traversal.

    Rejects names containing '..' segments or absolute paths.

    :param target: Target name to validate.
    :returns: The validated target name.
    :raises ValueError: If the target name is invalid.
    """
    t = Path(target)
    if ".." in t.parts or t.is_absolute():
        raise ValueError(f"Invalid target name: {target!r}")
    return target


class TickResultStatus(str, Enum):
    """Status values for a tick result."""

    ACTION_EXECUTED = "action_executed"
    ACTION_FAILED = "action_failed"
    NO_WORK = "no_work"
    DRY_RUN = "dry_run"
    GAVE_UP = "gave_up"
    LOCK_FAILED = "lock_failed"
    DISABLED = "disabled"


@dataclass
class TickResult:
    """Result of a single tick for a single target.

    :ivar target: Target name.
    :ivar stage_id: Stage ID that was executed, or None.
    :ivar status: Tick result status.
    :ivar message: Human-readable result message.
    :ivar stdout: Captured stdout from the action.
    :ivar stderr: Captured stderr from the action.
    :ivar chained_stages: List of stage IDs chained through in this tick.
    :ivar failed_chained_stages: List of chained stage IDs whose actions failed.
    """

    target: str
    stage_id: str | None
    status: TickResultStatus
    message: str = ""
    stdout: str = ""
    stderr: str = ""
    chained_stages: list[str] = dc_field(default_factory=list)
    failed_chained_stages: list[str] = dc_field(default_factory=list)

    def __str__(self) -> str:
        """Return a human-readable string representation of the tick result.

        :returns: A string in the format ``"<target> | <stage> -> <status> | <message>"``.
        :rtype: str
        """
        stage = self.stage_id or "-"
        base = f"{self.target} | {stage} -> {self.status.value} | {self.message}"
        if self.stderr:
            return f"{base}\n{self.stderr}"
        return base


class PipelineTickError(Exception):
    """Raised when _tick_single fails for a specific target, carrying the target name."""

    def __init__(self, target: str, original: Exception) -> None:
        """Initialize the error with the target name and original exception.

        :param target: Target name that failed.
        :param original: The original exception that caused the failure.
        """
        self.target = target
        self.original = original
        super().__init__(f"{type(original).__name__}: {original}")


def _instantiate_action_handler(handler_type: str, params: dict[str, Any]) -> ActionHandler:
    """Instantiate an action handler from config type + params.

    :param handler_type: Handler type identifier (e.g. ``"conversation_queue"``).
    :param params: Handler-specific parameters.
    :returns: Instantiated action handler.
    :raises ValueError: If the handler type is unknown.
    """
    if handler_type == "conversation_queue":
        from cronpypeline.plugins.conversation_queue import ConversationQueueHandler
        return ConversationQueueHandler(**params)
    raise ValueError(f"Unknown action handler type: {handler_type}")


def _build_marker_context(target: str, target_dir: Path, workspace_dir: Path, target_config: dict[str, Any]) -> dict[str, Any]:
    """Build context dict for marker template substitution.

    Flattens target_config keys into the top-level context so they can be
    used directly in templates (e.g. {slug} instead of {target_config[slug]}).

    :param target: Target name.
    :param target_dir: Full path to target directory.
    :param workspace_dir: Full path to workspace.
    :param target_config: Per-target configuration dict.
    :returns: Context dict with flattened keys for template substitution.
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
    """Cron-friendly pipeline orchestrator.

    :param config: Pipeline configuration loaded from JSON.
    """

    def __init__(self, config: PipelineConfig) -> None:
        """Initialize a pipeline from a configuration.

        :param config: Pipeline configuration loaded from JSON.
        :type config: PipelineConfig
        """
        self.config = config
        self.workspace_dir = Path(config.workspace_dir)
        self.lock = FileLock(
            self.workspace_dir / config.lock_file,
            dry_run=False,
        )

        self.mode_file = Path(config.mode_file) if config.mode_file else None
        if self.mode_file and not self.mode_file.is_absolute():
            self.mode_file = self.workspace_dir / self.mode_file

        self.config_file = Path(config.config_file) if config.config_file else None
        if self.config_file and not self.config_file.is_absolute():
            self.config_file = self.workspace_dir / self.config_file

        # Wire action handler from config if present
        if config.action_handler:
            handler = _instantiate_action_handler(
                config.action_handler.type,
                config.action_handler.params,
            )
            register_handler(ActionType.QUEUE_AGENT, handler)

    @classmethod
    def from_config(cls, path: Path | str | None = None) -> "Pipeline":
        """Create a Pipeline from a JSON config file.

        :param path: Path to the JSON config file.
        :returns: A :class:`Pipeline` instance.
        """
        config = PipelineConfig.from_file(path)
        return cls(config)

    def tick(
        self,
        target: str | None = None,
        dry_run: bool = False,
        verbose: bool = False,
    ) -> TickResult:
        """Execute one tick of the pipeline.

        :param target: Limit to a single target. If None, picks first target with work.
        :param dry_run: Show planned action without executing.
        :param verbose: Verbose output.
        :returns: TickResult describing what happened.
        """
        # Determine targets with config
        if target is None:
            target_objs = load_targets_with_config(self.config.targets)
            targets = [t.name for t in target_objs]
            for name in targets:
                _validate_target_name(name)
            target_config_map = {t.name: t.config for t in target_objs}
        else:
            _validate_target_name(target)
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
        except PipelineTickError as e:
            return TickResult(
                target=e.target,
                stage_id=None,
                status=TickResultStatus.ACTION_FAILED,
                message=f"Unhandled {type(e.original).__name__}: {e.original}",
                stderr=traceback.format_exc(),
            )
        except Exception as e:  # noqa: BLE001
            return TickResult(
                target=target or "*",
                stage_id=None,
                status=TickResultStatus.ACTION_FAILED,
                message=f"Unhandled {type(e).__name__}: {e}",
                stderr=traceback.format_exc(),
            )
        finally:
            self.lock.release()

    def tick_all(
        self,
        dry_run: bool = False,
        verbose: bool = False,
    ) -> list[TickResult]:
        """Execute one tick per target (all targets with work).

        :param dry_run: Show planned actions without executing.
        :param verbose: Verbose output.
        :returns: List of :class:`TickResult`, one per target that had work.
        """
        target_objs = load_targets_with_config(self.config.targets)
        targets = [t.name for t in target_objs]
        for name in targets:
            _validate_target_name(name)
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
                try:
                    result = self._tick_single(t, target_config_map.get(t, {}), dry_run, verbose)
                    if result.status != TickResultStatus.NO_WORK:
                        results.append(result)
                except Exception as e:  # noqa: BLE001
                    results.append(TickResult(
                        target=t,
                        stage_id=None,
                        status=TickResultStatus.ACTION_FAILED,
                        message=f"Unhandled {type(e).__name__}: {e}",
                        stderr=traceback.format_exc(),
                    ))
            return results
        finally:
            self.lock.release()

    def _tick_inner(
        self,
        targets: list[str],
        target_config_map: dict[str, dict[str, Any]],
        dry_run: bool,
        verbose: bool,
    ) -> TickResult:
        """Inner tick logic after lock is acquired.

        :param targets: List of target names.
        :param target_config_map: Mapping of target name to config dict.
        :param dry_run: Whether this is a dry run.
        :param verbose: Whether verbose output is enabled.
        :returns: TickResult for the selected target.
        """
        # Check config_file enabled toggle
        if self.config_file:
            toggle_path = self.config_file
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

        if not targets:
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
                if stage.modes and (current_mode is None or current_mode not in stage.modes):
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

        try:
            return self._tick_single(target, target_config_map.get(target, {}), dry_run, verbose)
        except Exception as e:
            raise PipelineTickError(target, e) from e

    def _tick_single(
        self,
        target: str,
        target_config: dict[str, Any],
        dry_run: bool,
        verbose: bool,
    ) -> TickResult:
        """Execute a tick for a single target, with pre/post hooks.

        :param target: Target name.
        :param target_config: Per-target configuration dict.
        :param dry_run: Whether this is a dry run.
        :param verbose: Whether verbose output is enabled.
        :returns: TickResult for the target.
        """
        _validate_target_name(target)
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

    def _get_current_mode(self) -> str | None:
        """Read the current mode from mode_file.

        :returns: Current mode string, or None if no mode_file or unreadable.
        """
        if not self.mode_file:
            return None
        mode_path = self.mode_file
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
        target_config: dict[str, Any],
        dry_run: bool,
        verbose: bool,
    ) -> TickResult:
        """Execute a tick for a single target (without hooks).

        :param target: Target name.
        :param target_config: Per-target configuration dict.
        :param dry_run: Whether this is a dry run.
        :param verbose: Whether verbose output is enabled.
        :returns: TickResult for the target.
        """
        target_dir = self.workspace_dir / target
        target_dir.mkdir(parents=True, exist_ok=True)

        # Derive state
        current_mode = self._get_current_mode()
        active_stages = []
        for stage in self.config.stages:
            if not stage.enabled:
                continue
            if stage.modes and (current_mode is None or current_mode not in stage.modes):
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

        # Clean up orphaned processing markers for completed stages.
        # When a queue_agent action completes externally (the agent creates
        # the completion marker), the pipeline-created processing marker is
        # left behind.  This orphans the target when target_lock is enabled
        # (has_processing stays True forever).  Delete the stale processing
        # marker so downstream stages can proceed.
        marker_ctx = _build_marker_context(target, target_dir, self.workspace_dir, target_config)
        for ss in target_state.stage_states.values():
            if ss.is_complete and ss.is_processing and "processing" in ss.stage.markers:
                delete_marker(ss.stage.markers["processing"], target_dir, context=marker_ctx)
                ss.is_processing = False

        # Check for rejection give-up (rejection_count >= max_rejections)
        for ss in target_state.stage_states.values():
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
                    # Below max — only increment if the stage's trigger actually fires
                    # (i.e., the stage will actually be re-processed this tick)
                    trigger_context = {
                        "target": target,
                        "target_dir": str(target_dir),
                        "workspace_dir": str(self.workspace_dir),
                        "target_config": target_config,
                    }
                    if (
                        not ss.is_complete
                        and not ss.is_processing
                        and not ss.is_given_up
                        and evaluate_trigger(ss.stage.trigger, target_dir, context=trigger_context)
                    ):
                        # Increment rejection count and keep the marker so the count accumulates
                        if "rejection" in ss.stage.markers:
                            rej_spec = ss.stage.markers["rejection"]
                            if rej_spec.type == MarkerType.JSON:
                                rej_data = read_marker(rej_spec, target_dir, context=marker_ctx) or {}
                                rej_data["rejection_count"] = ss.rejection_count + 1
                                create_marker(replace(rej_spec, content=rej_data), target_dir, context=marker_ctx)
                            else:
                                delete_marker(rej_spec, target_dir, context=marker_ctx)
                        ss.is_rejected = False  # Allow re-processing

        # Check for stale processing markers and handle them
        # Skip stages that are already complete — a leftover processing marker
        # (e.g. agent finished and produced completion) should not trigger re-queue.
        for ss in target_state.stage_states.values():
            if ss.is_stale and ss.is_processing and not ss.is_complete:
                return self._handle_stale(ss, target, target_dir, target_config, target_state, active_stages, dry_run, verbose)

        # Find first actionable stage whose trigger condition is met
        trigger_context = {
            "target": target,
            "target_dir": str(target_dir),
            "workspace_dir": str(self.workspace_dir),
            "target_config": target_config,
        }
        stage_state: StageState | None = None
        # If target_lock is enabled, no stage is actionable while any stage is processing
        if not (self.config.target_lock and target_state.has_processing):
            for stage in active_stages:
                candidate: StageState | None = target_state.stage_states.get(stage.id)
                if candidate is not None and candidate.is_actionable and evaluate_trigger(
                    stage.trigger, target_dir, context=trigger_context
                ):
                    stage_state = candidate
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
            # Preserve retry count if re-queueing
            retry_count = 0
            if stage_state.processing_data and "retry_count" in stage_state.processing_data:
                retry_count = stage_state.processing_data["retry_count"]
            processing_spec = replace(stage.markers["processing"], content={
                **stage.markers["processing"].content,
                "retry_count": retry_count,
            })
            create_marker(processing_spec, target_dir, context=marker_ctx)

        # Execute action
        ctx = TickContext(
            target=target,
            workspace_dir=self.workspace_dir,
            dry_run=dry_run,
            verbose=verbose,
            target_config=target_config,
            pipeline=self,
        )
        result = execute_action(stage.action, ctx)

        # Update processing marker with result data (for stale detection and tracking)
        if stage.action.type == ActionType.QUEUE_AGENT and "processing" in stage.markers and result.success and result.data:
            processing_spec = replace(stage.markers["processing"], content={
                **stage.markers["processing"].content,
                "retry_count": retry_count,
                **result.data,
            })
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
                    pipeline=self,
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
        # Skip if the marker is a symlink with no target — the action created it
        # (e.g. run_diagnostic creates the symlink as part of its work).
        if (
            stage.action.type != ActionType.QUEUE_AGENT
            and "completion" in stage.markers
            and not result.data.get("async", False)
            and not (
                stage.markers["completion"].type == MarkerType.SYMLINK
                and stage.markers["completion"].target is None
                and marker_exists(stage.markers["completion"], target_dir, context=marker_ctx)
            )
        ):
            create_marker(stage.markers["completion"], target_dir, context=marker_ctx)
            # Clear rejection marker only when the work is actually completed
            if "rejection" in stage.markers:
                delete_marker(stage.markers["rejection"], target_dir, context=marker_ctx)

        # Create processing marker for async custom actions (non-chained)
        if (
            stage.action.type == ActionType.CUSTOM
            and "processing" in stage.markers
            and result.success
            and result.data.get("async", False)
        ):
            processing_spec = replace(stage.markers["processing"], content={
                **stage.markers["processing"].content,
                "retry_count": 0,
                **result.data,
            })
            create_marker(processing_spec, target_dir, context=marker_ctx)

        # Invalidate markers from other stages
        for inv_spec in stage.invalidates:
            delete_marker(inv_spec, target_dir, context=marker_ctx)

        # Handle chaining
        chained: list[str] = []
        if (
            stage.chain
            and stage.action.type != ActionType.QUEUE_AGENT
            and not result.data.get("async", False)
        ):
            chained_result = self._try_chain(target, target_dir, target_config, target_state, active_stages, dry_run, verbose, stage)
            if chained_result:
                final_stage_id, chained, failed_stage_id, failed_result = chained_result
                if failed_stage_id is not None:
                    detail = failed_result.stderr or failed_result.stdout if failed_result else None
                    return TickResult(
                        target=target,
                        stage_id=failed_stage_id,
                        status=TickResultStatus.ACTION_FAILED,
                        message=f"Chained stage {failed_stage_id} failed: {detail}" if detail else f"Chained stage {failed_stage_id} failed",
                        stdout=failed_result.stdout if failed_result else "",
                        stderr=failed_result.stderr if failed_result else "",
                        chained_stages=chained,
                        failed_chained_stages=[failed_stage_id],
                    )
                return TickResult(
                    target=target,
                    stage_id=final_stage_id,
                    status=TickResultStatus.ACTION_EXECUTED,
                    message="Chained through stages",
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
        target_config: dict[str, Any],
        target_state: TargetState,
        active_stages: list[Stage],
        dry_run: bool,
        verbose: bool,
        completed_stage: Stage,
    ) -> tuple[str, list[str], str | None, Any | None] | None:
        """Attempt to chain to the next stage in the same tick.

        Re-evaluates from the top of the stage list after each mechanical stage
        completes, matching the original pipeline's behavior of calling
        plan_next_action() in a loop. Skips stages whose trigger doesn't fire
        (continues to next stage) and stops when a non-mechanical stage fires.

        :param target: Target name.
        :param target_dir: Target directory path.
        :param target_config: Per-target configuration dict.
        :param active_stages: Mode-filtered list of active stages.
        :param dry_run: Whether this is a dry run.
        :param verbose: Whether verbose output is enabled.
        :param completed_stage: The stage that just completed.
        :returns: Tuple of (final_stage_id, list_of_chained_stage_ids,
            failed_stage_id, failed_result), or None if nothing chained.
        """
        chained: list[str] = []
        current_stage = completed_stage
        executed_ids: set[str] = {completed_stage.id}

        _MAX_CHAIN = 100  # safety cap
        for _ in range(_MAX_CHAIN):
            # Re-evaluate from the top of the stage list
            next_stage: Stage | None = None
            for stage in active_stages:
                if stage.id in executed_ids:
                    continue
                # Skip stages that are not actionable (complete, processing,
                # or given up) — same check as the normal detector chain.
                candidate_state = target_state.stage_states.get(stage.id)
                if candidate_state is not None and not candidate_state.is_actionable:
                    continue
                trigger_context = {
                    "target": target,
                    "target_dir": str(target_dir),
                    "workspace_dir": str(self.workspace_dir),
                    "target_config": target_config,
                }
                if not evaluate_trigger(stage.trigger, target_dir, context=trigger_context):
                    continue
                next_stage = stage
                break

            if next_stage is None:
                break

            # Only chain mechanical (non-queue_agent) actions
            if next_stage.action.type == ActionType.QUEUE_AGENT:
                break

            ctx = TickContext(
                target=target,
                workspace_dir=self.workspace_dir,
                dry_run=dry_run,
                verbose=verbose,
                target_config=target_config,
                pipeline=self,
            )
            result = execute_action(next_stage.action, ctx)

            if not result.success:
                if next_stage.on_fail:
                    fail_ctx = TickContext(
                        target=target,
                        workspace_dir=self.workspace_dir,
                        dry_run=dry_run,
                        verbose=verbose,
                        target_config=target_config,
                        pipeline=self,
                    )
                    fail_result = execute_action(next_stage.on_fail, fail_ctx)
                    if not fail_result.success:
                        on_fail_err = fail_result.stderr or fail_result.stdout
                        result.stderr = (result.stderr + "\n[on_fail] " + on_fail_err).strip() if on_fail_err else result.stderr
                return (current_stage.id, chained, next_stage.id, result)

            marker_ctx = _build_marker_context(target, target_dir, self.workspace_dir, target_config)

            # Create produced markers
            for marker_spec in next_stage.action.produces:
                create_marker(marker_spec, target_dir, context=marker_ctx)

            # Create completion marker
            # Skip if the marker is a symlink with no target — the action created it.
            if (
                "completion" in next_stage.markers
                and not result.data.get("async", False)
                and not (
                    next_stage.markers["completion"].type == MarkerType.SYMLINK
                    and next_stage.markers["completion"].target is None
                    and marker_exists(next_stage.markers["completion"], target_dir, context=marker_ctx)
                )
            ):
                create_marker(next_stage.markers["completion"], target_dir, context=marker_ctx)
                # Clear rejection marker only when the work is actually completed
                if "rejection" in next_stage.markers:
                    delete_marker(next_stage.markers["rejection"], target_dir, context=marker_ctx)

            # Create processing marker for async chained stages
            if result.data.get("async", False) and "processing" in next_stage.markers:
                processing_spec = replace(next_stage.markers["processing"], content={
                    **next_stage.markers["processing"].content,
                    "retry_count": 0,
                    **result.data,
                })
                create_marker(processing_spec, target_dir, context=marker_ctx)

            # Invalidate markers from other stages
            for inv_spec in next_stage.invalidates:
                delete_marker(inv_spec, target_dir, context=marker_ctx)

            chained.append(next_stage.id)
            executed_ids.add(next_stage.id)
            current_stage = next_stage

            # Stop chaining if this stage doesn't allow further chaining
            if not next_stage.chain:
                break

            # Stop if the action was async (agent queued)
            if result.data.get("async", False):
                break

        if chained:
            return (current_stage.id, chained, None, None)
        return None

    def _handle_stale(
        self,
        stage_state: StageState,
        target: str,
        target_dir: Path,
        target_config: dict[str, Any],
        target_state: TargetState,
        active_stages: list[Stage],
        dry_run: bool,
        verbose: bool,
    ) -> TickResult:
        """Handle a stale processing marker.

        :param stage_state: State of the stale stage.
        :param target: Target name.
        :param target_dir: Target directory path.
        :param target_config: Per-target configuration dict.
        :param target_state: State of the target being processed.
        :param active_stages: Mode-filtered list of active stages.
        :param dry_run: Whether this is a dry run.
        :param verbose: Whether verbose output is enabled.
        :returns: TickResult — either GAVE_UP, DRY_RUN, ACTION_EXECUTED, or
            ACTION_FAILED.
        """
        stage = stage_state.stage
        retry_count = stage_state.retry_count

        if dry_run:
            if retry_count >= stage.max_retries:
                message = f"Would give up on stale stage {stage.id} (retry {retry_count} >= max {stage.max_retries})"
            else:
                message = f"Would re-queue stale stage {stage.id} (retry {retry_count + 1})"
            return TickResult(
                target=target,
                stage_id=stage.id,
                status=TickResultStatus.DRY_RUN,
                message=message,
            )

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

        # Create new processing marker with incremented retry count
        if "processing" in stage.markers:
            processing_spec = replace(stage.markers["processing"], content={
                **stage.markers["processing"].content,
                "retry_count": retry_count + 1,
            })
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
            pipeline=self,
        )
        result = execute_action(stage.action, ctx)

        if not result.success:
            # Run on_fail if configured
            if stage.on_fail:
                fail_ctx = TickContext(
                    target=target,
                    workspace_dir=self.workspace_dir,
                    dry_run=dry_run,
                    verbose=verbose,
                    target_config=target_config,
                    pipeline=self,
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

        # Update processing marker with result data, or delete it for sync actions
        if result.success and "processing" in stage.markers:
            is_async = (
                stage.action.type == ActionType.QUEUE_AGENT
                or (stage.action.type == ActionType.CUSTOM and result.data.get("async", False))
            )
            if is_async:
                # Async custom actions are treated as a fresh start (retry_count reset to 0),
                # matching the normal execution path.
                new_retry_count = 0 if (
                    stage.action.type == ActionType.CUSTOM
                    and result.data.get("async", False)
                ) else retry_count + 1
                processing_spec = replace(stage.markers["processing"], content={
                    **stage.markers["processing"].content,
                    "retry_count": new_retry_count,
                    **result.data,
                })
                create_marker(processing_spec, target_dir, context=marker_ctx)
            else:
                # Sync actions never have processing markers in the normal path.
                # Delete the leftover processing marker after successful re-execution.
                delete_marker(stage.markers["processing"], target_dir, context=marker_ctx)

        # Create produced markers
        for marker_spec in stage.action.produces:
            create_marker(marker_spec, target_dir, context=marker_ctx)

        # Create completion marker for sync actions (command, subprocess, custom)
        # Skip if the marker is a symlink with no target — the action created it
        # (e.g. run_diagnostic creates the symlink as part of its work).
        if (
            stage.action.type != ActionType.QUEUE_AGENT
            and "completion" in stage.markers
            and not result.data.get("async", False)
            and not (
                stage.markers["completion"].type == MarkerType.SYMLINK
                and stage.markers["completion"].target is None
                and marker_exists(stage.markers["completion"], target_dir, context=marker_ctx)
            )
        ):
            create_marker(stage.markers["completion"], target_dir, context=marker_ctx)
            # Clear rejection marker only when the work is actually completed
            if "rejection" in stage.markers:
                delete_marker(stage.markers["rejection"], target_dir, context=marker_ctx)

        # Invalidate markers from other stages
        for inv_spec in stage.invalidates:
            delete_marker(inv_spec, target_dir, context=marker_ctx)

        # Handle chaining
        chained: list[str] = []
        if (
            stage.chain
            and stage.action.type != ActionType.QUEUE_AGENT
            and not result.data.get("async", False)
        ):
            chained_result = self._try_chain(target, target_dir, target_config, target_state, active_stages, dry_run, verbose, stage)
            if chained_result:
                final_stage_id, chained, failed_stage_id, failed_result = chained_result
                if failed_stage_id is not None:
                    detail = failed_result.stderr or failed_result.stdout if failed_result else None
                    return TickResult(
                        target=target,
                        stage_id=failed_stage_id,
                        status=TickResultStatus.ACTION_FAILED,
                        message=f"Chained stage {failed_stage_id} failed: {detail}" if detail else f"Chained stage {failed_stage_id} failed",
                        stdout=failed_result.stdout if failed_result else "",
                        stderr=failed_result.stderr if failed_result else "",
                        chained_stages=chained,
                        failed_chained_stages=[failed_stage_id],
                    )
                return TickResult(
                    target=target,
                    stage_id=final_stage_id,
                    status=TickResultStatus.ACTION_EXECUTED,
                    message="Chained through stages",
                    chained_stages=chained,
                )

        return TickResult(
            target=target,
            stage_id=stage.id,
            status=TickResultStatus.ACTION_EXECUTED,
            message=f"Re-queued stale stage (retry {retry_count + 1})",
            stdout=result.stdout,
            chained_stages=chained,
        )

    def status(self, targets: list[str] | None = None) -> dict[str, Any]:
        """Get pipeline state snapshot without executing actions.

        :param targets: Optional list of target names to check. If None, checks all.
        :returns: Dict mapping target names to stage state dicts.
        """
        if targets is None:
            targets = load_targets(self.config.targets)

        for name in targets:
            _validate_target_name(name)

        current_mode = self._get_current_mode()
        active_stages = []
        for stage in self.config.stages:
            if not stage.enabled:
                continue
            if stage.modes and (current_mode is None or current_mode not in stage.modes):
                continue
            active_stages.append(stage)

        state = PipelineState(workspace_dir=self.workspace_dir, stages=active_stages)
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
