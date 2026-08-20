"""Tests for cronpypeline.targets — target registry loading."""

import json
from pathlib import Path

import pytest

from cronpypeline.config import TargetSpec, TargetType
from cronpypeline.targets import load_targets


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
