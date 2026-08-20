"""Tests for the VNN pipeline JSON config."""

import json
from pathlib import Path

import pytest

from cronpypeline.config import PipelineConfig


class TestVNNPipelineConfig:
    """Tests for the VNN pipeline JSON config file."""

    CONFIG_PATH = Path(__file__).parent.parent / "configs" / "vnn_pipeline.json"

    def test_config_file_exists(self):
        assert self.CONFIG_PATH.exists()

    def test_config_loads_without_error(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        assert config.name == "vnn-pipeline"
        assert len(config.stages) > 0

    def test_config_has_pre_tick_hook(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        assert config.pre_tick is not None
        assert "vnn_pre_tick" in config.pre_tick.callable

    def test_config_has_post_tick_hook(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        assert config.post_tick is not None
        assert "vnn_post_tick" in config.post_tick.callable

    def test_config_has_action_handler(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        assert config.action_handler is not None
        assert config.action_handler.type == "conversation_queue"
        params = config.action_handler.params
        assert params.get("prompt_field") == "content"
        assert params.get("flatten_agent_settings") is True
        assert "sender" in params.get("default_fields", {})

    def test_config_has_target_lock(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        assert config.target_lock is True

    def test_config_uses_registry_targets(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        assert config.targets is not None
        assert config.targets.type.value == "registry"

    def test_all_stage_ids_unique(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        ids = [s.id for s in config.stages]
        assert len(ids) == len(set(ids)), f"Duplicate stage IDs: {ids}"

    def test_has_revision_stage(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        revision = next(s for s in config.stages if s.id == "revision")
        assert revision.max_rejections > 0
        assert revision.max_rejections == 5

    def test_has_publishing_stage(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        publishing = next(s for s in config.stages if s.id == "publishing")
        assert publishing.max_rejections > 0
        assert publishing.max_rejections == 3

    def test_has_writing_stage(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        writing = next(s for s in config.stages if s.id == "writing")
        assert writing.max_retries == 3

    def test_has_research_stage(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        research = next(s for s in config.stages if s.id == "research")
        assert research.timeout_minutes == 60

    def test_revision_stage_appears_before_publishing(self):
        """Revision must appear before publishing in the detector chain
        so rejected articles get revised before re-publishing."""
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        ids = [s.id for s in config.stages]
        rev_idx = ids.index("revision")
        pub_idx = ids.index("publishing")
        assert rev_idx < pub_idx

    def test_revision_stage_has_rejection_marker(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        revision = next(s for s in config.stages if s.id == "revision")
        assert revision.markers.get("rejection") is not None

    def test_revision_stage_invalidates_rejected_article(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        revision = next(s for s in config.stages if s.id == "revision")
        invalid_names = [m.name for m in revision.invalidates]
        assert "rejected-article.md" in invalid_names

    def test_publishing_stage_invalidates_rejected_article(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        publishing = next(s for s in config.stages if s.id == "publishing")
        invalid_names = [m.name for m in publishing.invalidates]
        assert "rejected-article.md" in invalid_names

    def test_all_agent_stages_use_queue_agent(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        for stage in config.stages:
            assert stage.action.type.value == "queue_agent", \
                f"Stage {stage.id} should use queue_agent action"

    def test_all_stages_have_processing_and_give_up_markers(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        for stage in config.stages:
            assert stage.markers.get("processing") is not None, \
                f"Stage {stage.id} missing processing marker"
            assert stage.markers.get("give_up") is not None, \
                f"Stage {stage.id} missing give_up marker"

    def test_all_stages_have_reminder_prompts(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        for stage in config.stages:
            params = stage.action.params
            assert "reminder_prompt_template" in params or "reminder_prompt" in params, \
                f"Stage {stage.id} missing reminder prompt"
