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
from typing import Any, Optional


def _format_template(template: str, context: dict[str, Any]) -> str:
    """Substitute {key} placeholders in template using context dict."""
    if not context or "{" not in template:
        return template
    try:
        return template.format(**context)
    except (KeyError, IndexError, ValueError):
        return template


class MarkerType(str, Enum):
    FILE = "file"
    JSON = "json"
    SYMLINK = "symlink"


@dataclass
class MarkerSpec:
    """Specification for a filesystem marker.

    Attributes:
        name: Filename (e.g. "latest.md", ".processing")
        type: Marker type (file / json / symlink)
        directory: Directory relative to workspace/target dir
        content: For JSON markers — field values to write
        target: For symlink markers — target path
    """
    name: str
    type: MarkerType
    directory: str = "."
    content: dict[str, Any] = field(default_factory=dict)
    target: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MarkerSpec":
        """Create MarkerSpec from a JSON config dict."""
        return cls(
            name=data["name"],
            type=MarkerType(data["type"]),
            directory=data.get("directory", "."),
            content=data.get("content", {}),
            target=data.get("target"),
        )

    def resolve_path(self, base_dir: Path, context: Optional[dict[str, Any]] = None) -> Path:
        """Resolve the full path of this marker relative to base_dir.

        If context is provided, template-substitutes {key} placeholders
        in name and directory.
        """
        ctx = context or {}
        name = _format_template(self.name, ctx)
        directory = _format_template(self.directory, ctx)
        return base_dir / directory / name

    def resolve_target(self, context: Optional[dict[str, Any]] = None) -> Optional[str]:
        """Resolve symlink target with optional context substitution."""
        if self.target is None:
            return None
        return _format_template(self.target, context or {})


def create_marker(spec: MarkerSpec, base_dir: Path, context: Optional[dict[str, Any]] = None) -> None:
    """Create a marker on the filesystem."""
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


def read_marker(spec: MarkerSpec, base_dir: Path, context: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """Read marker content. Returns None if marker doesn't exist."""
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


def marker_exists(spec: MarkerSpec, base_dir: Path, context: Optional[dict[str, Any]] = None) -> bool:
    """Check if a marker exists on the filesystem."""
    path = spec.resolve_path(base_dir, context)
    return path.exists() or path.is_symlink()


def delete_marker(spec: MarkerSpec, base_dir: Path, context: Optional[dict[str, Any]] = None) -> None:
    """Delete a marker from the filesystem. No-op if it doesn't exist."""
    path = spec.resolve_path(base_dir, context)
    if path.is_symlink() or path.exists():
        path.unlink()


def marker_age_seconds(spec: MarkerSpec, base_dir: Path, context: Optional[dict[str, Any]] = None) -> Optional[float]:
    """Get the age of a marker in seconds. Returns None if marker doesn't exist."""
    path = spec.resolve_path(base_dir, context)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    return time.time() - mtime
