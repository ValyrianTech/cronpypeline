"""Target registry loading for cronpypeline.

Loads the list of targets (repos, countries, etc.) from a registry file,
static list, or single target.
"""

import json
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

from cronpypeline.config import TargetSpec, TargetType


@dataclass
class Target:
    """A single target with its name and per-target config dict.

    :ivar name: Target name.
    :ivar config: Per-target configuration dict.
    """

    name: str
    config: dict[str, Any] = dc_field(default_factory=dict)


def _matches_filter(item: dict[str, Any], filter_dict: dict[str, Any]) -> bool:
    """Check if a registry item matches all filter criteria.

    :param item: Registry item dict.
    :param filter_dict: Filter criteria as key-value pairs.
    :returns: True if the item matches all criteria.
    """
    for key, value in filter_dict.items():
        if item.get(key) != value:
            return False
    return True


def load_targets(spec: TargetSpec | None) -> list[str]:
    """Load the list of target names from a TargetSpec.

    :param spec: TargetSpec configuration, or None for default single target.
    :returns: List of target name strings.
    :raises FileNotFoundError: If registry file is not found.
    :raises ValueError: If the target type is unknown.
    """
    if spec is None:
        return ["."]

    if spec.type == TargetType.STATIC:
        return list(spec.items or [])

    if spec.type == TargetType.SINGLE:
        return [spec.name] if spec.name else ["."]

    if spec.type == TargetType.REGISTRY:
        registry_path = Path(spec.file or "")
        if not registry_path.exists():
            raise FileNotFoundError(f"Registry file not found: {registry_path}")

        data = json.loads(registry_path.read_text())
        items = data[spec.key]

        if spec.filter:
            items = [item for item in items if _matches_filter(item, spec.filter)]

        return [item["name"] for item in items]

    raise ValueError(f"Unknown target type: {spec.type}")


def load_targets_with_config(spec: TargetSpec | None) -> list[Target]:
    """Load targets with their full config dicts from a TargetSpec.

    Like :func:`load_targets` but returns :class:`Target` objects that include
    per-target configuration from the registry (e.g. test_cmd, coverage_threshold, etc.).

    :param spec: TargetSpec configuration, or None for default single target.
    :returns: List of :class:`Target` objects with name and config.
    :raises FileNotFoundError: If registry file is not found.
    :raises ValueError: If the target type is unknown.
    """
    if spec is None:
        return [Target(name=".", config={})]

    if spec.type == TargetType.STATIC:
        return [Target(name=name, config={}) for name in (spec.items or [])]

    if spec.type == TargetType.SINGLE:
        return [Target(name=spec.name, config={})] if spec.name else [Target(name=".", config={})]

    if spec.type == TargetType.REGISTRY:
        registry_path = Path(spec.file or "")
        if not registry_path.exists():
            raise FileNotFoundError(f"Registry file not found: {registry_path}")

        data = json.loads(registry_path.read_text())
        items = data[spec.key]

        if spec.filter:
            items = [item for item in items if _matches_filter(item, spec.filter)]

        return [
            Target(name=item["name"], config={k: v for k, v in item.items() if k != "name"})
            for item in items
        ]

    raise ValueError(f"Unknown target type: {spec.type}")
