"""PipelineState — filesystem-derived state for all targets and stages.

State is derived fresh on each tick from the filesystem. No in-memory state
persists between ticks. This makes the pipeline fully crash-safe.
"""

import json
import time
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Optional

from cronpypeline.config import Stage
from cronpypeline.markers import marker_exists, read_marker, marker_age_seconds


@dataclass
class StageState:
    """Derived state for a single stage of a single target."""
    stage: Stage
    is_complete: bool = False
    is_processing: bool = False
    is_given_up: bool = False
    is_stale: bool = False
    retry_count: int = 0
    processing_data: Optional[dict] = None

    def derive(self, base_dir: Path) -> None:
        """Derive state from the filesystem."""
        markers = self.stage.markers

        # Completion check
        if "completion" in markers:
            self.is_complete = marker_exists(markers["completion"], base_dir)

        # Give-up check
        if "give_up" in markers:
            self.is_given_up = marker_exists(markers["give_up"], base_dir)

        # Processing check
        if "processing" in markers:
            self.is_processing = marker_exists(markers["processing"], base_dir)
            if self.is_processing:
                data = read_marker(markers["processing"], base_dir)
                self.processing_data = data
                if data and "retry_count" in data:
                    self.retry_count = data["retry_count"]

                # Staleness check
                age = marker_age_seconds(markers["processing"], base_dir)
                if age is not None:
                    self.is_stale = age >= self.stage.timeout_minutes * 60

    @property
    def is_actionable(self) -> bool:
        """Whether this stage can be acted upon (not complete, not processing, not given up)."""
        return not self.is_complete and not self.is_processing and not self.is_given_up


@dataclass
class TargetState:
    """Derived state for all stages of a single target."""
    target: str
    stages: list[Stage]
    stage_states: dict[str, StageState] = dc_field(default_factory=dict)

    def derive(self, base_dir: Path) -> None:
        """Derive state for all stages."""
        self.stage_states = {}
        for stage in self.stages:
            if not stage.enabled:
                continue
            ss = StageState(stage=stage)
            ss.derive(base_dir)
            self.stage_states[stage.id] = ss

    @property
    def first_actionable_stage(self) -> Optional[StageState]:
        """Return the first stage that can be acted upon, or None."""
        for stage in self.stages:
            if not stage.enabled:
                continue
            ss = self.stage_states.get(stage.id)
            if ss and ss.is_actionable:
                return ss
        return None


@dataclass
class PipelineState:
    """Derived state for all targets in the pipeline."""
    workspace_dir: Path
    stages: list[Stage]
    target_states: dict[str, TargetState] = dc_field(default_factory=dict)

    def derive(self, targets: list[str]) -> None:
        """Derive state for all targets."""
        self.target_states = {}
        for target in targets:
            target_dir = self.workspace_dir / target
            target_state = TargetState(target=target, stages=self.stages)
            target_state.derive(target_dir)
            self.target_states[target] = target_state

    def get_target_with_work(self, targets: list[str]) -> Optional[str]:
        """Return the first target that has actionable work, or None."""
        for target in targets:
            ts = self.target_states.get(target)
            if ts and ts.first_actionable_stage is not None:
                return target
        return None

    def get_all_targets_with_work(self, targets: list[str]) -> list[str]:
        """Return all targets that have actionable work."""
        result = []
        for target in targets:
            ts = self.target_states.get(target)
            if ts and ts.first_actionable_stage is not None:
                result.append(target)
        return result
