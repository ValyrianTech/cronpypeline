"""Tests for cronpypeline.targets — target registry loading."""

import json

import pytest

from cronpypeline.config import TargetSpec, TargetType
from cronpypeline.targets import Target, load_targets, load_targets_with_config


class TestRegistryTarget:
    """Tests for registry-based target loading."""

    def test_load_registry_targets(self, tmp_path):
        registry = {"repos": [
            {"name": "repo1", "enabled": True},
            {"name": "repo2", "enabled": True},
            {"name": "repo3", "enabled": False},
        ]}
        registry_file = tmp_path / "repos.json"
        registry_file.write_text(json.dumps(registry))

        spec = TargetSpec(
            type=TargetType.REGISTRY,
            file=str(registry_file),
            key="repos",
        )
        targets = load_targets(spec)
        assert "repo1" in targets
        assert "repo2" in targets
        assert "repo3" in targets

    def test_load_registry_with_filter(self, tmp_path):
        registry = {"repos": [
            {"name": "repo1", "enabled": True},
            {"name": "repo2", "enabled": False},
            {"name": "repo3", "enabled": True},
        ]}
        registry_file = tmp_path / "repos.json"
        registry_file.write_text(json.dumps(registry))

        spec = TargetSpec(
            type=TargetType.REGISTRY,
            file=str(registry_file),
            key="repos",
            filter={"enabled": True},
        )
        targets = load_targets(spec)
        assert "repo1" in targets
        assert "repo3" in targets
        assert "repo2" not in targets

    def test_load_registry_empty(self, tmp_path):
        registry = {"repos": []}
        registry_file = tmp_path / "repos.json"
        registry_file.write_text(json.dumps(registry))

        spec = TargetSpec(type=TargetType.REGISTRY, file=str(registry_file), key="repos")
        targets = load_targets(spec)
        assert targets == []

    def test_load_registry_missing_file_raises(self, tmp_path):
        spec = TargetSpec(
            type=TargetType.REGISTRY,
            file=str(tmp_path / "nonexistent.json"),
            key="repos",
        )
        with pytest.raises((FileNotFoundError, IOError)):
            load_targets(spec)

    def test_load_registry_missing_key_raises(self, tmp_path):
        registry = {"other_key": []}
        registry_file = tmp_path / "repos.json"
        registry_file.write_text(json.dumps(registry))

        spec = TargetSpec(type=TargetType.REGISTRY, file=str(registry_file), key="repos")
        with pytest.raises(KeyError):
            load_targets(spec)

    def test_load_registry_with_multiple_filters(self, tmp_path):
        registry = {"repos": [
            {"name": "repo1", "enabled": True, "priority": "high"},
            {"name": "repo2", "enabled": True, "priority": "low"},
            {"name": "repo3", "enabled": False, "priority": "high"},
        ]}
        registry_file = tmp_path / "repos.json"
        registry_file.write_text(json.dumps(registry))

        spec = TargetSpec(
            type=TargetType.REGISTRY,
            file=str(registry_file),
            key="repos",
            filter={"enabled": True, "priority": "high"},
        )
        targets = load_targets(spec)
        assert targets == ["repo1"]


class TestStaticTarget:
    """Tests for static target lists."""

    def test_load_static_targets(self):
        spec = TargetSpec(type=TargetType.STATIC, items=["repo1", "repo2", "repo3"])
        targets = load_targets(spec)
        assert targets == ["repo1", "repo2", "repo3"]

    def test_load_static_empty(self):
        spec = TargetSpec(type=TargetType.STATIC, items=[])
        targets = load_targets(spec)
        assert targets == []


class TestSingleTarget:
    """Tests for single target mode."""

    def test_load_single_target(self):
        spec = TargetSpec(type=TargetType.SINGLE, name="my-repo")
        targets = load_targets(spec)
        assert targets == ["my-repo"]


class TestNoTargetSpec:
    """Tests when no target spec is configured (single default target)."""

    def test_load_none_returns_default(self):
        targets = load_targets(None)
        assert targets == ["."]


class TestLoadTargetsWithConfig:
    """Tests for load_targets_with_config — returns Target objects with config."""

    def test_registry_returns_target_objects_with_config(self, tmp_path):
        registry = {"repos": [
            {"name": "repo1", "enabled": True, "test_cmd": "pytest"},
            {"name": "repo2", "enabled": True, "test_cmd": "tox"},
        ]}
        registry_file = tmp_path / "repos.json"
        registry_file.write_text(json.dumps(registry))

        spec = TargetSpec(
            type=TargetType.REGISTRY,
            file=str(registry_file),
            key="repos",
        )
        targets = load_targets_with_config(spec)
        assert len(targets) == 2
        assert all(isinstance(t, Target) for t in targets)
        assert targets[0].name == "repo1"
        assert targets[0].config["test_cmd"] == "pytest"
        assert targets[1].name == "repo2"
        assert targets[1].config["test_cmd"] == "tox"

    def test_registry_with_filter(self, tmp_path):
        registry = {"repos": [
            {"name": "repo1", "enabled": True, "slug": "a"},
            {"name": "repo2", "enabled": False, "slug": "b"},
        ]}
        registry_file = tmp_path / "repos.json"
        registry_file.write_text(json.dumps(registry))

        spec = TargetSpec(
            type=TargetType.REGISTRY,
            file=str(registry_file),
            key="repos",
            filter={"enabled": True},
        )
        targets = load_targets_with_config(spec)
        assert len(targets) == 1
        assert targets[0].name == "repo1"
        assert targets[0].config["slug"] == "a"

    def test_static_returns_empty_config(self):
        spec = TargetSpec(type=TargetType.STATIC, items=["repo1", "repo2"])
        targets = load_targets_with_config(spec)
        assert len(targets) == 2
        assert targets[0].name == "repo1"
        assert targets[0].config == {}
        assert targets[1].name == "repo2"
        assert targets[1].config == {}

    def test_single_returns_empty_config(self):
        spec = TargetSpec(type=TargetType.SINGLE, name="my-repo")
        targets = load_targets_with_config(spec)
        assert len(targets) == 1
        assert targets[0].name == "my-repo"
        assert targets[0].config == {}

    def test_none_returns_default_with_empty_config(self):
        targets = load_targets_with_config(None)
        assert len(targets) == 1
        assert targets[0].name == "."
        assert targets[0].config == {}


class TestTargetDataclass:
    """Tests for the Target dataclass."""

    def test_target_creation(self):
        t = Target(name="repo1", config={"test_cmd": "pytest"})
        assert t.name == "repo1"
        assert t.config["test_cmd"] == "pytest"

    def test_target_default_config(self):
        t = Target(name="repo1")
        assert t.config == {}


class TestUnknownTargetType:
    """Tests for unknown target type handling."""

    def test_load_targets_unknown_type_raises_value_error(self):
        """Unknown target type should raise ValueError."""
        spec = TargetSpec(type=TargetType.STATIC, items=[])
        spec.type = "unknown_type"
        with pytest.raises(ValueError, match="Unknown target type"):
            load_targets(spec)

    def test_load_targets_with_config_unknown_type_raises_value_error(self):
        """Unknown target type should raise ValueError in load_targets_with_config."""
        spec = TargetSpec(type=TargetType.STATIC, items=[])
        spec.type = "unknown_type"
        with pytest.raises(ValueError, match="Unknown target type"):
            load_targets_with_config(spec)

    def test_load_targets_with_config_registry_missing_file_raises(self, tmp_path):
        """Registry file not found should raise FileNotFoundError in load_targets_with_config."""
        spec = TargetSpec(
            type=TargetType.REGISTRY,
            file=str(tmp_path / "nonexistent.json"),
            key="repos",
        )
        with pytest.raises((FileNotFoundError, IOError)):
            load_targets_with_config(spec)
