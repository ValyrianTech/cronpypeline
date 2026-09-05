"""PipelineState — filesystem-derived state for all targets and stages.

State is derived fresh on each tick from the filesystem. No in-memory state
persists between ticks. This makes the pipeline fully crash-safe.
"""

from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

from cronpypeline.config import Stage
from cronpypeline.markers import (
    delete_marker,
    marker_age_seconds,
    marker_exists,
    read_marker,
)
from cronpypeline.triggers import evaluate_trigger


@dataclass
class StageState:
    """Derived state for a single stage of a single target.

    :ivar stage: The :class:`Stage` this state belongs to.
    :ivar is_complete: Whether the completion marker exists.
    :ivar is_processing: Whether the processing marker exists.
    :ivar is_given_up: Whether the give-up marker exists.
    :ivar is_stale: Whether the processing marker is stale (timeout or queue file gone).
    :ivar retry_count: Number of retries so far (from processing marker).
    :ivar processing_data: Raw data from the processing marker, if present.
    :ivar rejection_count: Number of rejections so far (from rejection marker).
    :ivar is_rejected: Whether the rejection marker exists.
    """

    stage: Stage
    is_complete: bool = False
    is_processing: bool = False
    is_given_up: bool = False
    is_stale: bool = False
    retry_count: int = 0
    processing_data: dict[str, Any] | None = None
    rejection_count: int = 0
    is_rejected: bool = False
    _rejection_override: bool = dc_field(default=False, repr=False)

    def derive(self, base_dir: Path, context: dict[str, Any] | None = None) -> None:
        """Derive state from the filesystem.

        :param base_dir: Target directory to check markers in.
        :param context: Optional context dict for marker template substitution.
        """
        markers = self.stage.markers
        ctx = context or {}

        # Completion check
        if "completion" in markers:
            self.is_complete = marker_exists(markers["completion"], base_dir, context=ctx)

        # Give-up check
        if "give_up" in markers:
            self.is_given_up = marker_exists(markers["give_up"], base_dir, context=ctx)

        # Rejection check
        if "rejection" in markers:
            rej_exists = marker_exists(markers["rejection"], base_dir, context=ctx)
            if not self._rejection_override:
                self.is_rejected = rej_exists
            if rej_exists:
                rej_data = read_marker(markers["rejection"], base_dir, context=ctx)
                if rej_data and "rejection_count" in rej_data:
                    self.rejection_count = rej_data["rejection_count"]

        # Processing check
        if "processing" in markers:
            self.is_processing = marker_exists(markers["processing"], base_dir, context=ctx)
            if self.is_processing:
                data = read_marker(markers["processing"], base_dir, context=ctx)
                self.processing_data = data
                if data and "retry_count" in data:
                    self.retry_count = data["retry_count"]

                # Queue-file-based staleness: if queue_file is gone, agent finished
                # but didn't produce completion → immediately stale.
                # Exception: if reminder files exist in the queue directory, the
                # agent was cut off (e.g. tool-call limit) and is being restarted
                # by the reminder system — not stale.
                if data and "queue_file" in data:
                    from pathlib import Path as _P
                    if not _P(data["queue_file"]).exists():
                        queue_dir = _P(data["queue_file"]).parent
                        has_reminder = (
                            queue_dir.exists()
                            and any("_reminder_" in f.name for f in queue_dir.iterdir())
                        )
                        self.is_stale = not has_reminder
                    else:
                        self.is_stale = False
                else:
                    # Time-based staleness (fallback)
                    age = marker_age_seconds(markers["processing"], base_dir, context=ctx)
                    if age is not None:
                        self.is_stale = age >= self.stage.timeout_minutes * 60

    @property
    def is_actionable(self) -> bool:
        """Whether this stage can be acted upon (not complete, not processing, not given up).

        A rejected stage is actionable unless rejection tracking is enabled (max_rejections > 0).
        """
        if self.is_complete or self.is_processing or self.is_given_up:
            return False
        # Rejection only blocks if rejection tracking is enabled
        return not (self.is_rejected and self.stage.max_rejections > 0)


@dataclass
class TargetState:
    """Derived state for all stages of a single target.

    :ivar target: Target name.
    :ivar stages: List of :class:`Stage` objects for this target.
    :ivar stage_states: Mapping of stage ID to :class:`StageState`.
    :ivar target_lock: Whether cross-stage target locking is enabled.
    """

    target: str
    stages: list[Stage]
    stage_states: dict[str, StageState] = dc_field(default_factory=dict)
    target_lock: bool = False
    target_dir: Path | None = None
    context: dict[str, Any] | None = None
    _orphan_cleanup_done: bool = dc_field(default=False, repr=False)

    def derive(self, base_dir: Path, context: dict[str, Any] | None = None) -> None:
        """Derive state for all stages.

        Updates existing StageState objects in-place when they already exist so
        that in-memory overrides (e.g. the rejection re-process flag) are
        preserved across re-derivation calls within the same tick.

        :param base_dir: Target directory to check markers in.
        :param context: Optional context dict for marker template substitution.
        """
        self.target_dir = base_dir
        self.context = context
        for stage in self.stages:
            if not stage.enabled:
                continue
            ss = self.stage_states.get(stage.id)
            if ss is None:
                ss = StageState(stage=stage)
                self.stage_states[stage.id] = ss
            ss.derive(base_dir, context=context)

        # Clean up orphaned processing markers for completed stages.
        # When a queue_agent action completes externally (the agent creates
        # the completion marker), the pipeline-created processing marker is
        # left behind.  This orphans the target when target_lock is enabled
        # (has_processing stays True forever).  Delete the stale processing
        # marker so downstream stages can proceed.
        #
        # Only run once per TargetState instance — re-derivations (e.g. after
        # invalidation) must not trigger cleanup, because a processing marker
        # created by the current tick's action could belong to a different
        # stage that shares the same marker name.
        if not self._orphan_cleanup_done:
            for ss in self.stage_states.values():
                if ss.is_complete and ss.is_processing and "processing" in ss.stage.markers:
                    delete_marker(ss.stage.markers["processing"], base_dir, context=context)
                    ss.is_processing = False
            self._orphan_cleanup_done = True

    @property
    def has_processing(self) -> bool:
        """Whether any stage is currently processing."""
        return any(ss.is_processing for ss in self.stage_states.values())

    @property
    def has_active_processing(self) -> bool:
        """Whether any stage is processing and not stale.

        Stale processing markers indicate the agent is gone — they should
        not block target selection under target_lock, since the stale
        handler will re-queue or give up on the next tick.
        """
        return any(ss.is_processing and not ss.is_stale for ss in self.stage_states.values())

    @property
    def first_actionable_stage(self) -> StageState | None:
        """Return the first stage that can be acted upon, or None.

        If target_lock is enabled, no stage is actionable while any stage is processing.
        """
        if self.target_lock and self.has_processing:
            return None
        for stage in self.stages:
            if not stage.enabled:
                continue
            ss = self.stage_states.get(stage.id)
            if ss and ss.is_actionable:
                return ss
        return None

    @property
    def first_stage_with_work(self) -> StageState | None:
        """Return the first stage whose trigger actually fires, or None.

        Like first_actionable_stage but also evaluates trigger conditions,
        so markerless stages whose triggers return False are correctly
        excluded.  Requires derive() to have been called (which stores
        target_dir and context).
        """
        if self.target_lock and self.has_active_processing:
            return None
        if self.target_dir is None:
            return None
        for stage in self.stages:
            if not stage.enabled:
                continue
            ss = self.stage_states.get(stage.id)
            if ss and ss.is_actionable and evaluate_trigger(stage.trigger, self.target_dir, context=self.context):
                return ss
        return None


@dataclass
class PipelineState:
    """Derived state for all targets in the pipeline.

    :ivar workspace_dir: Root workspace directory.
    :ivar stages: List of :class:`Stage` objects in the pipeline.
    :ivar target_states: Mapping of target name to :class:`TargetState`.
    :ivar target_lock: Whether cross-stage target locking is enabled.
    """

    workspace_dir: Path
    stages: list[Stage]
    target_states: dict[str, TargetState] = dc_field(default_factory=dict)
    target_lock: bool = False

    def derive(self, targets: list[str], target_configs: dict[str, dict[str, Any]] | None = None) -> None:
        """Derive state for all targets.

        :param targets: List of target names.
        :param target_configs: Optional mapping of target name to per-target config dict.
        """
        self.target_states = {}
        target_configs = target_configs or {}
        for target in targets:
            target_dir = self.workspace_dir / target
            target_config = target_configs.get(target, {})
            ctx = {
                "target": target,
                "target_dir": str(target_dir),
                "workspace_dir": str(self.workspace_dir),
                "target_config": target_config,
            }
            # Flatten target_config keys
            for k, v in target_config.items():
                if k not in ctx:
                    ctx[k] = v
            target_state = TargetState(target=target, stages=self.stages, target_lock=self.target_lock)
            target_state.derive(target_dir, context=ctx)
            self.target_states[target] = target_state

    def get_target_with_work(self, targets: list[str]) -> str | None:
        """Return the first target that has actionable work, or None.

        Evaluates trigger conditions (not just marker state) so that
        markerless stages whose triggers return False are correctly
        excluded.

        :param targets: List of target names to check.
        :returns: First target name with work, or None.
        """
        for target in targets:
            ts = self.target_states.get(target)
            if ts and ts.first_stage_with_work is not None:
                return target
        return None

    def get_all_targets_with_work(self, targets: list[str]) -> list[str]:
        """Return all targets that have actionable work.

        Evaluates trigger conditions (not just marker state) so that
        markerless stages whose triggers return False are correctly
        excluded.

        :param targets: List of target names to check.
        :returns: List of target names with work.
        """
        result = []
        for target in targets:
            ts = self.target_states.get(target)
            if ts and ts.first_stage_with_work is not None:
                result.append(target)
        return result
