"""Tests for cronpypeline.config — PipelineConfig, Stage, TriggerCondition, ActionSpec."""

import json
import pytest
from pathlib import Path

from cronpypeline.config import (
    PipelineConfig,
    Stage,
    TriggerCondition,
    TriggerType,
    ActionSpec,
    ActionType,
    TargetSpec,
    TargetType,
    ActionHandlerConfig,
)


class TestTriggerCondition:
    """Tests for TriggerCondition parsing."""

    def test_file_missing_trigger(self):
        t = TriggerCondition.from_dict({"type": "file_missing", "path": ".SWE/repo_briefing.md"})
        assert t.type == TriggerType.FILE_MISSING
        assert t.path == ".SWE/repo_briefing.md"

    def test_file_exists_trigger(self):
        t = TriggerCondition.from_dict({"type": "file_exists", "path": "coding_complete.marker"})
        assert t.type == TriggerType.FILE_EXISTS
        assert t.path == "coding_complete.marker"

    def test_file_older_than_trigger(self):
        t = TriggerCondition.from_dict({"type": "file_older_than", "path": ".processing", "minutes": 30})
        assert t.type == TriggerType.FILE_OLDER_THAN
        assert t.minutes == 30

    def test_marker_state_trigger(self):
        t = TriggerCondition.from_dict({
            "type": "marker_state",
            "path": "task.json",
            "field": "retry_count",
            "op": "lt",
            "value": 3,
        })
        assert t.type == TriggerType.MARKER_STATE
        assert t.field == "retry_count"
        assert t.op == "lt"
        assert t.value == 3

    def test_queue_empty_trigger(self):
        t = TriggerCondition.from_dict({"type": "queue_empty", "queue_dir": "/tmp/queue"})
        assert t.type == TriggerType.QUEUE_EMPTY
        assert t.queue_dir == "/tmp/queue"

    def test_custom_trigger(self):
        t = TriggerCondition.from_dict({
            "type": "custom",
            "callable": "swe_plugin.detect_open_issue",
        })
        assert t.type == TriggerType.CUSTOM
        assert t.callable == "swe_plugin.detect_open_issue"

    def test_and_combinator(self):
        t = TriggerCondition.from_dict({
            "type": "and",
            "conditions": [
                {"type": "file_missing", "path": "a.marker"},
                {"type": "file_missing", "path": "b.marker"},
            ],
        })
        assert t.type == TriggerType.AND
        assert len(t.conditions) == 2

    def test_or_combinator(self):
        t = TriggerCondition.from_dict({
            "type": "or",
            "conditions": [
                {"type": "file_exists", "path": "a.marker"},
                {"type": "file_exists", "path": "b.marker"},
            ],
        })
        assert t.type == TriggerType.OR
        assert len(t.conditions) == 2

    def test_invalid_trigger_type_raises(self):
        with pytest.raises(ValueError, match="Unknown trigger type"):
            TriggerCondition.from_dict({"type": "invalid_type"})


class TestActionSpec:
    """Tests for ActionSpec parsing."""

    def test_command_action(self):
        a = ActionSpec.from_dict({
            "type": "command",
            "params": {"command": ".venv/bin/pytest -q", "cwd": "{target_dir}"},
            "timeout_seconds": 900,
        })
        assert a.type == ActionType.COMMAND
        assert a.params["command"] == ".venv/bin/pytest -q"
        assert a.timeout_seconds == 900

    def test_queue_agent_action(self):
        a = ActionSpec.from_dict({
            "type": "queue_agent",
            "params": {"agent": "CoderAgent", "prompt": "Fix issue..."},
        })
        assert a.type == ActionType.QUEUE_AGENT
        assert a.params["agent"] == "CoderAgent"

    def test_subprocess_action(self):
        a = ActionSpec.from_dict({
            "type": "subprocess",
            "params": {"script": "run_check.py", "args": ["--verbose"]},
        })
        assert a.type == ActionType.SUBPROCESS
        assert a.params["script"] == "run_check.py"

    def test_http_request_action(self):
        a = ActionSpec.from_dict({
            "type": "http_request",
            "params": {"url": "http://localhost:8080/run", "method": "POST"},
        })
        assert a.type == ActionType.HTTP_REQUEST
        assert a.params["url"] == "http://localhost:8080/run"

    def test_custom_action(self):
        a = ActionSpec.from_dict({
            "type": "custom",
            "params": {"callable": "my_module.my_func"},
        })
        assert a.type == ActionType.CUSTOM

    def test_action_with_produces(self):
        a = ActionSpec.from_dict({
            "type": "command",
            "params": {"command": "echo hello"},
            "produces": [
                {"type": "symlink", "name": "latest.md", "target": "report.md", "directory": "reports"},
            ],
        })
        assert len(a.produces) == 1
        assert a.produces[0].name == "latest.md"

    def test_default_timeout_is_none(self):
        a = ActionSpec.from_dict({"type": "command", "params": {"command": "echo hello"}})
        assert a.timeout_seconds is None

    def test_invalid_action_type_raises(self):
        with pytest.raises(ValueError, match="Unknown action type"):
            ActionSpec.from_dict({"type": "invalid_action"})


class TestStage:
    """Tests for Stage parsing."""

    def test_minimal_stage(self):
        s = Stage.from_dict({
            "id": "A0",
            "name": "Onboarding",
            "trigger": {"type": "file_missing", "path": "briefing.md"},
            "action": {"type": "command", "params": {"command": "echo hi"}},
        })
        assert s.id == "A0"
        assert s.name == "Onboarding"
        assert s.trigger.type == TriggerType.FILE_MISSING
        assert s.action.type == ActionType.COMMAND
        assert s.chain is False
        assert s.timeout_minutes == 30  # default
        assert s.max_retries == 3  # default
        assert s.enabled is True

    def test_full_stage(self):
        s = Stage.from_dict({
            "id": "C-select",
            "name": "Select and Fix Issue",
            "trigger": {"type": "custom", "callable": "swe_plugin.detect_open_issue"},
            "action": {"type": "queue_agent", "params": {"agent": "CoderAgent"}},
            "markers": {
                "completion": {"type": "file", "name": "coding_complete.marker"},
                "processing": {"type": "json", "name": "task.json", "content": {}},
            },
            "chain": False,
            "timeout_minutes": 30,
            "max_retries": 3,
            "enabled": True,
            "on_fail": {"type": "command", "params": {"command": "git checkout main"}},
        })
        assert s.id == "C-select"
        assert s.timeout_minutes == 30
        assert s.max_retries == 3
        assert s.on_fail is not None
        assert s.on_fail.type == ActionType.COMMAND
        assert "completion" in s.markers
        assert "processing" in s.markers

    def test_stage_with_give_up_marker(self):
        s = Stage.from_dict({
            "id": "A1",
            "name": "Test",
            "trigger": {"type": "file_missing", "path": "test.md"},
            "action": {"type": "command", "params": {"command": "pytest"}},
            "markers": {
                "give_up": {"type": "file", "name": ".gave_up"},
            },
        })
        assert "give_up" in s.markers

    def test_disabled_stage(self):
        s = Stage.from_dict({
            "id": "A1",
            "name": "Test",
            "trigger": {"type": "file_missing", "path": "test.md"},
            "action": {"type": "command", "params": {"command": "pytest"}},
            "enabled": False,
        })
        assert s.enabled is False


class TestPipelineConfig:
    """Tests for PipelineConfig parsing."""

    def test_minimal_config(self):
        c = PipelineConfig.from_dict({
            "name": "test-pipeline",
            "workspace_dir": "/tmp/workspace",
            "stages": [],
        })
        assert c.name == "test-pipeline"
        assert c.workspace_dir == "/tmp/workspace"
        assert len(c.stages) == 0

    def test_config_with_stages(self):
        c = PipelineConfig.from_dict({
            "name": "test-pipeline",
            "workspace_dir": "/tmp/workspace",
            "stages": [
                {
                    "id": "A0",
                    "name": "Onboarding",
                    "trigger": {"type": "file_missing", "path": "briefing.md"},
                    "action": {"type": "command", "params": {"command": "echo hi"}},
                },
                {
                    "id": "A1",
                    "name": "Test",
                    "trigger": {"type": "file_missing", "path": "test.md"},
                    "action": {"type": "command", "params": {"command": "pytest"}},
                },
            ],
        })
        assert len(c.stages) == 2
        assert c.stages[0].id == "A0"
        assert c.stages[1].id == "A1"

    def test_config_with_lock_file(self):
        c = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": "/tmp",
            "lock_file": "pipeline.lock",
            "stages": [],
        })
        assert c.lock_file == "pipeline.lock"

    def test_config_default_lock_file(self):
        c = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": "/tmp",
            "stages": [],
        })
        assert c.lock_file == "pipeline.lock"

    def test_config_with_targets(self):
        c = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": "/tmp",
            "targets": {
                "type": "registry",
                "file": "repos.json",
                "key": "repos",
                "filter": {"enabled": True},
            },
            "stages": [],
        })
        assert c.targets is not None
        assert c.targets.type == TargetType.REGISTRY
        assert c.targets.file == "repos.json"
        assert c.targets.key == "repos"

    def test_config_with_action_handler(self):
        c = PipelineConfig.from_dict({
            "name": "test",
            "workspace_dir": "/tmp",
            "action_handler": {
                "type": "conversation_queue",
                "queue_dir": "/tmp/queue",
            },
            "stages": [],
        })
        assert c.action_handler is not None
        assert c.action_handler.type == "conversation_queue"
        assert c.action_handler.params["queue_dir"] == "/tmp/queue"

    def test_config_from_file(self, tmp_path):
        config_data = {
            "name": "file-pipeline",
            "workspace_dir": str(tmp_path),
            "stages": [
                {
                    "id": "A0",
                    "name": "Step 1",
                    "trigger": {"type": "file_missing", "path": "done.md"},
                    "action": {"type": "command", "params": {"command": "echo done"}},
                },
            ],
        }
        config_file = tmp_path / "pipeline.json"
        config_file.write_text(json.dumps(config_data))
        c = PipelineConfig.from_file(config_file)
        assert c.name == "file-pipeline"
        assert len(c.stages) == 1

    def test_config_validation_missing_name(self):
        with pytest.raises((KeyError, ValueError, TypeError)):
            PipelineConfig.from_dict({"workspace_dir": "/tmp", "stages": []})

    def test_config_validation_missing_workspace_dir(self):
        with pytest.raises((KeyError, ValueError, TypeError)):
            PipelineConfig.from_dict({"name": "test", "stages": []})

    def test_config_validation_duplicate_stage_ids(self):
        with pytest.raises(ValueError, match="Duplicate stage id"):
            PipelineConfig.from_dict({
                "name": "test",
                "workspace_dir": "/tmp",
                "stages": [
                    {
                        "id": "A0",
                        "name": "First",
                        "trigger": {"type": "file_missing", "path": "a.md"},
                        "action": {"type": "command", "params": {"command": "echo a"}},
                    },
                    {
                        "id": "A0",
                        "name": "Second",
                        "trigger": {"type": "file_missing", "path": "b.md"},
                        "action": {"type": "command", "params": {"command": "echo b"}},
                    },
                ],
            })


class TestTargetSpec:
    """Tests for TargetSpec parsing."""

    def test_registry_target(self):
        t = TargetSpec.from_dict({
            "type": "registry",
            "file": "repos.json",
            "key": "repos",
            "filter": {"enabled": True},
        })
        assert t.type == TargetType.REGISTRY
        assert t.file == "repos.json"
        assert t.key == "repos"
        assert t.filter == {"enabled": True}

    def test_static_target(self):
        t = TargetSpec.from_dict({
            "type": "static",
            "items": ["repo1", "repo2"],
        })
        assert t.type == TargetType.STATIC
        assert t.items == ["repo1", "repo2"]

    def test_single_target(self):
        t = TargetSpec.from_dict({
            "type": "single",
            "name": "my-repo",
        })
        assert t.type == TargetType.SINGLE
        assert t.name == "my-repo"
