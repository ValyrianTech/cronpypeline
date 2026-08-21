"""Report writing and symlink management for cronpypeline.

Provides utilities for writing markdown reports and managing "latest" symlinks.
"""

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


def generate_timestamp() -> str:
    """Generate a timestamp string in YYYYMMDD_HHMMSS format."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


@dataclass
class ReportConfig:
    """Configuration for report writing."""
    template: str = "# Report — {timestamp}\n\n{content}\n"
    extension: str = ".md"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReportConfig":
        return cls(
            template=data.get("template", "# Report — {timestamp}\n\n{content}\n"),
            extension=data.get("extension", ".md"),
        )


def format_report(config: ReportConfig, **variables: Any) -> str:
    """Format a report using the config template and provided variables."""
    defaults = {
        "timestamp": generate_timestamp(),
        "content": "",
        "title": "Report",
    }
    defaults.update(variables)
    try:
        return config.template.format(**defaults)
    except (KeyError, IndexError):
        return config.template


def write_report(
    directory: Path,
    filename: str,
    content: str,
) -> Path:
    """Write a report file to the given directory.

    Args:
        directory: Directory to write to (created if needed).
        filename: Filename, supports {timestamp} substitution.
        content: Report content.

    Returns:
        Path to the written report.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    if "{timestamp}" in filename:
        filename = filename.replace("{timestamp}", generate_timestamp())

    report_path = directory / filename
    report_path.write_text(content)
    return report_path


def update_latest_symlink(
    directory: Path,
    symlink_name: str,
    target_name: str,
) -> Path:
    """Create or update a 'latest' symlink pointing to the target file.

    Args:
        directory: Directory containing the symlink and target.
        symlink_name: Name of the symlink (e.g. "latest.md").
        target_name: Name of the target file (relative to directory).

    Returns:
        Path to the symlink.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    symlink_path = directory / symlink_name

    if symlink_path.is_symlink() or symlink_path.exists():
        symlink_path.unlink()

    symlink_path.symlink_to(target_name)
    return symlink_path
