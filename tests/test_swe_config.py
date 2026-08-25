"""Tests for the SWE pipeline JSON config."""

from pathlib import Path

from cronpypeline.config import PipelineConfig


class TestSWEPipelineConfig:
    """Tests for the SWE pipeline JSON config file."""

    CONFIG_PATH = Path(__file__).parent.parent / "configs" / "swe_pipeline.json"

    def test_config_file_exists(self):
        assert self.CONFIG_PATH.exists()

    def test_config_loads_without_error(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        assert config.name == "swe-pipeline"
        assert len(config.stages) > 0

    def test_config_has_pre_tick_hook(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        assert config.pre_tick is not None
        assert "sync_session_mode" in config.pre_tick.callable

    def test_config_has_action_handler(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        assert config.action_handler is not None
        assert config.action_handler.type == "conversation_queue"
        params = config.action_handler.params
        assert params.get("prompt_field") == "content"
        assert params.get("flatten_agent_settings") is True
        assert "sender" in params.get("default_fields", {})

    def test_config_has_mode_file(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        assert config.mode_file is not None

    def test_all_stage_ids_unique(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        ids = [s.id for s in config.stages]
        assert len(ids) == len(set(ids)), f"Duplicate stage IDs: {ids}"

    def test_diagnostic_stages_use_custom_action(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        diagnostic_stages = [
            s for s in config.stages
            if s.id.startswith("A") and "fix" not in s.id and s.id != "A0"
        ]
        assert len(diagnostic_stages) >= 7  # A1-A9 minus fix agents
        for stage in diagnostic_stages:
            assert stage.action.type.value == "custom"
            callable_name = stage.action.params.get("callable", "")
            # A5/A7/A8/A9 use venv-aware wrappers that call run_diagnostic internally
            assert (
                "run_diagnostic" in callable_name
                or "run_a5_bandit" in callable_name
                or "run_a7_coverage" in callable_name
                or "run_a8_radon" in callable_name
                or "run_a9_dep_audit" in callable_name
            )

    def test_fix_agent_stages_use_queue_fix_agent(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        fix_stages = [s for s in config.stages if "fix-agent" in s.id]
        assert len(fix_stages) >= 2  # A2-fix-agent, A6-fix-agent
        for stage in fix_stages:
            assert stage.action.params.get("callable") == "cronpypeline.plugins.swe_prompts.queue_fix_agent"

    def test_coder_stage_uses_queue_coder_agent(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        coder_stage = next(s for s in config.stages if s.id == "C-issue-fix")
        assert coder_stage.action.params.get("callable") == "cronpypeline.plugins.swe_plugin.run_c_issue_fix"

    def test_review_stage_uses_queue_review_agent(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        review_stage = next(s for s in config.stages if s.id == "C-pr-review")
        assert review_stage.action.params.get("callable") == "cronpypeline.plugins.swe_plugin.run_c_pr_review"

    def test_github_stages_have_github_mode(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        github_stages = [s for s in config.stages if "github" in s.modes]
        assert len(github_stages) >= 1  # C-session-terminal
        # C-pr-status and C-pr-review are NOT mode-restricted — the original
        # pipeline fires them whenever a PR exists, regardless of session mode.

    def test_fix_agent_stages_have_invalidates(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        for stage in config.stages:
            if "fix-agent" in stage.id:
                # Fix agents use queue_fix_agent which handles invalidation internally
                # via the invalidate_paths param, not via the stage-level invalidates list
                assert "invalidate_paths" in stage.action.params, \
                    f"{stage.id} should have invalidate_paths in action params"

    def test_coder_stage_has_on_fail(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        coder_stage = next(s for s in config.stages if s.id == "C-issue-fix")
        # C-issue-fix delegates to run_issue_fix.py subprocess which handles its own cleanup
        assert coder_stage.action.type.value == "custom"

    def test_stale_stage_uses_detect_agent_forgot_marker(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        # C-stale was replaced by C-issue-fix which delegates stale detection to run_issue_fix.py
        issue_fix_stage = next(s for s in config.stages if s.id == "C-issue-fix")
        assert "detect_c_issue_fix" in issue_fix_stage.trigger.callable

    def test_async_agent_stages_have_processing_markers(self):
        config = PipelineConfig.from_file(self.CONFIG_PATH)
        async_stage_ids = ["A2-fix-agent", "A3-fix-agent", "C-pr-review", "C-doc-sync"]
        for stage_id in async_stage_ids:
            stage = next(s for s in config.stages if s.id == stage_id)
            assert "processing" in stage.markers, \
                f"Stage {stage_id} missing processing marker"
            processing = stage.markers["processing"]
            assert processing.type.value == "json"
            assert processing.name.startswith(".processing_")
