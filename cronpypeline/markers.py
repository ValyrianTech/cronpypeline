"""MarkerSpec — file-based state markers for pipeline stages.

Supports three marker types:
- FILE: empty file whose presence/absence indicates state
- JSON: JSON file with fields (e.g. retry_count, agent, timestamp)
- SYMLINK: symlink to latest report
"""

import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


def _format_template(template: str, context: dict[str, Any]) -> str:
    """Substitute {key} placeholders in template using context dict.

    :param template: Template string with ``{key}`` placeholders.
    :param context: Mapping of keys to substitution values.
    :returns: Formatted string, or the original template if substitution fails.
    """
    if not context or "{" not in template:
        return template
    try:
        return template.format(**context)
    except (KeyError, IndexError, ValueError):
        return template


class MarkerType(str, Enum):
    """Supported marker types on the filesystem."""

    FILE = "file"
    JSON = "json"
    SYMLINK = "symlink"


@dataclass
class MarkerSpec:
    """Specification for a filesystem marker.

    :ivar name: Filename (e.g. ``latest.md``, ``.processing``).
    :ivar type: Marker type (file / json / symlink).
    :ivar directory: Directory relative to workspace/target dir.
    :ivar content: For JSON markers — field values to write.
    :ivar target: For symlink markers — target path.
    """

    name: str
    type: MarkerType
    directory: str = "."
    content: dict[str, Any] = field(default_factory=dict)
    target: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MarkerSpec":
        """Create MarkerSpec from a JSON config dict.

        :param data: Dictionary with ``name``, ``type``, and optional keys.
        :returns: A :class:`MarkerSpec` instance.
        """
        return cls(
            name=data["name"],
            type=MarkerType(data["type"]),
            directory=data.get("directory", "."),
            content=data.get("content", {}),
            target=data.get("target"),
        )

    def resolve_path(self, base_dir: Path, context: dict[str, Any] | None = None) -> Path:
        """Resolve the full path of this marker relative to base_dir.

        If context is provided, template-substitutes ``{key}`` placeholders
        in name and directory.

        :param base_dir: Base directory to resolve against.
        :param context: Optional context dict for template substitution.
        :returns: Full :class:`~pathlib.Path` to the marker.
        """
        ctx = context or {}
        name = _format_template(self.name, ctx)
        directory = _format_template(self.directory, ctx)

        # Reject path traversal: no '..' segments and no absolute paths
        if ".." in Path(directory).parts or ".." in Path(name).parts:
            raise ValueError(f"Marker path contains '..': {directory}/{name}")
        if Path(directory).is_absolute() or Path(name).is_absolute():
            raise ValueError(f"Marker path must be relative: {directory}/{name}")

        resolved = (base_dir / directory).resolve()
        base_resolved = base_dir.resolve()
        if not resolved.is_relative_to(base_resolved):
            raise ValueError(f"Marker path escapes base directory: {directory}/{name}")
        return resolved / name

    def resolve_target(self, context: dict[str, Any] | None = None) -> str | None:
        """Resolve symlink target with optional context substitution.

        :param context: Optional context dict for template substitution.
        :returns: Resolved target string, or None if no target is set.
        """
        if self.target is None:
            return None
        return _format_template(self.target, context or {})


def create_marker(spec: MarkerSpec, base_dir: Path, context: dict[str, Any] | None = None) -> None:
    """Create a marker on the filesystem.

    :param spec: Marker specification describing what to create.
    :param base_dir: Base directory to create the marker in.
    :param context: Optional context dict for template substitution.
    :raises ValueError: If the marker type is a symlink with no target, or unknown type.
    """
    path = spec.resolve_path(base_dir, context)
    path.parent.mkdir(parents=True, exist_ok=True)

    if spec.type == MarkerType.FILE:
        path.touch()

    elif spec.type == MarkerType.JSON:
        content = dict(spec.content)
        content["timestamp"] = time.time()
        path.write_text(json.dumps(content, indent=2))

    elif spec.type == MarkerType.SYMLINK:
        if path.is_symlink() or path.exists():
            path.unlink()
        target = spec.resolve_target(context)
        if target is None:
            raise ValueError(f"Symlink marker has no target: {spec.name}")
        path.symlink_to(target)

    else:
        raise ValueError(f"Unknown marker type: {spec.type}")


def read_marker(spec: MarkerSpec, base_dir: Path, context: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Read marker content.

    :param spec: Marker specification to read.
    :param base_dir: Base directory to read from.
    :param context: Optional context dict for template substitution.
    :returns: Marker content dict, or None if the marker doesn't exist.
    """
    path = spec.resolve_path(base_dir, context)

    if not path.exists() and not path.is_symlink():
        return None

    if spec.type == MarkerType.FILE:
        return {"exists": True}

    elif spec.type == MarkerType.JSON:
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    elif spec.type == MarkerType.SYMLINK:
        target = os.readlink(path) if path.is_symlink() else None
        return {"target": target, "exists": True}

    return None


def marker_exists(spec: MarkerSpec, base_dir: Path, context: dict[str, Any] | None = None) -> bool:
    """Check if a marker exists on the filesystem.

    :param spec: Marker specification to check.
    :param base_dir: Base directory to check in.
    :param context: Optional context dict for template substitution.
    :returns: True if the marker exists, False otherwise.
    """
    path = spec.resolve_path(base_dir, context)
    return path.exists() or path.is_symlink()


def delete_marker(spec: MarkerSpec, base_dir: Path, context: dict[str, Any] | None = None) -> None:
    """Delete a marker from the filesystem. No-op if it doesn't exist.

    :param spec: Marker specification to delete.
    :param base_dir: Base directory to delete from.
    :param context: Optional context dict for template substitution.
    """
    path = spec.resolve_path(base_dir, context)
    if path.is_symlink() or path.exists():
        path.unlink()


def marker_age_seconds(spec: MarkerSpec, base_dir: Path, context: dict[str, Any] | None = None) -> float | None:
    """Get the age of a marker in seconds.

    :param spec: Marker specification to check.
    :param base_dir: Base directory to check in.
    :param context: Optional context dict for template substitution.
    :returns: Age in seconds, or None if the marker doesn't exist.
    """
    path = spec.resolve_path(base_dir, context)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return time.time() - mtime
