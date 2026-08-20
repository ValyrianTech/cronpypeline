"""Target registry loading for cronpypeline.

Loads the list of targets (repos, countries, etc.) from a registry file,
static list, or single target.
"""

import json
from pathlib import Path
from typing import Optional

from cronpypeline.config import TargetSpec, TargetType


def _matches_filter(item: dict, filter_dict: dict) -> bool:
    """Check if a registry item matches all filter criteria."""
    for key, value in filter_dict.items():
        if item.get(key) != value:
            return False
    return True


def load_targets(spec: Optional[TargetSpec]) -> list[str]:
    """Load the list of target names from a TargetSpec.

    Args:
        spec: TargetSpec configuration, or None for default single target.

    Returns:
        List of target name strings.
    """
    if spec is None:
        return ["."]

    if spec.type == TargetType.STATIC:
        return list(spec.items or [])

    if spec.type == TargetType.SINGLE:
        return [spec.name] if spec.name else ["."]

    if spec.type == TargetType.REGISTRY:
        registry_path = Path(spec.file)
        if not registry_path.exists():
            raise FileNotFoundError(f"Registry file not found: {registry_path}")

        data = json.loads(registry_path.read_text())
        items = data[spec.key]

        if spec.filter:
            items = [item for item in items if _matches_filter(item, spec.filter)]

        return [item["name"] for item in items]

    raise ValueError(f"Unknown target type: {spec.type}")
