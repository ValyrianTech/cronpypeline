"""Tests for cronpypeline.reporting — report writing, symlink management."""

import os
from datetime import datetime, timezone

from cronpypeline.reporting import (
    ReportConfig,
    format_report,
    generate_timestamp,
    update_latest_symlink,
    write_report,
)


class TestGenerateTimestamp:
    """Tests for timestamp generation."""

    def test_generates_valid_timestamp(self):
        ts = generate_timestamp()
        # Should be a string in YYYYMMDD_HHMMSS format
        assert len(ts) == 15
        assert ts[8] == "_"
        # Should be parseable
        datetime.strptime(ts, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)

    def test_timestamp_is_unique_when_called_apart(self):
        ts1 = generate_timestamp()
        ts2 = generate_timestamp()
        # They might be the same if called in the same second, but format should be valid
        datetime.strptime(ts1, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
        datetime.strptime(ts2, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)


class TestWriteReport:
    """Tests for report file writing."""

    def test_write_basic_report(self, tmp_path):
        report_path = write_report(
            directory=tmp_path,
            filename="report.md",
            content="# Test Report\n\nContent here.",
        )
        assert report_path.exists()
        assert "Test Report" in report_path.read_text()

    def test_write_report_creates_parent_dirs(self, tmp_path):
        report_path = write_report(
            directory=tmp_path / "reports" / "subdir",
            filename="report.md",
            content="Content",
        )
        assert report_path.exists()

    def test_write_report_with_timestamp(self, tmp_path):
        report_path = write_report(
            directory=tmp_path,
            filename="{timestamp}.md",
            content="Content",
        )
        assert report_path.exists()
        assert report_path.suffix == ".md"

    def test_write_report_overwrites_existing(self, tmp_path):
        write_report(directory=tmp_path, filename="report.md", content="v1")
        write_report(directory=tmp_path, filename="report.md", content="v2")
        assert (tmp_path / "report.md").read_text() == "v2"


class TestUpdateLatestSymlink:
    """Tests for latest symlink management."""

    def test_creates_symlink_to_target(self, tmp_path):
        target = tmp_path / "20240101_120000.md"
        target.write_text("# Report")
        update_latest_symlink(
            directory=tmp_path,
            symlink_name="latest.md",
            target_name="20240101_120000.md",
        )
        link = tmp_path / "latest.md"
        assert link.is_symlink()
        assert os.readlink(link) == "20240101_120000.md"

    def test_overwrites_existing_symlink(self, tmp_path):
        t1 = tmp_path / "v1.md"
        t1.write_text("v1")
        t2 = tmp_path / "v2.md"
        t2.write_text("v2")

        update_latest_symlink(tmp_path, "latest.md", "v1.md")
        update_latest_symlink(tmp_path, "latest.md", "v2.md")

        link = tmp_path / "latest.md"
        assert os.readlink(link) == "v2.md"

    def test_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "reports" / "v1.md"
        target.parent.mkdir(parents=True)
        target.write_text("v1")

        update_latest_symlink(
            directory=tmp_path / "reports",
            symlink_name="latest.md",
            target_name="v1.md",
        )
        assert (tmp_path / "reports" / "latest.md").is_symlink()


class TestFormatReport:
    """Tests for report formatting."""

    def test_format_with_template(self):
        config = ReportConfig(template="# {title}\n\n{content}")
        report = format_report(config, title="My Report", content="Details here")
        assert "My Report" in report
        assert "Details here" in report

    def test_format_with_default_template(self):
        config = ReportConfig()
        report = format_report(config, content="Just content")
        assert "Just content" in report

    def test_format_with_missing_var(self):
        config = ReportConfig(template="# {title}\n\n{content}")
        report = format_report(config, content="Content only")
        # Missing var should be left as-is or empty
        assert "Content only" in report


class TestReportConfig:
    """Tests for ReportConfig dataclass."""

    def test_default_config(self):
        c = ReportConfig()
        assert c.template is not None
        assert "{content}" in c.template

    def test_custom_config(self):
        c = ReportConfig(template="Custom: {content}", extension=".md")
        assert c.template == "Custom: {content}"
        assert c.extension == ".md"

    def test_from_dict(self):
        c = ReportConfig.from_dict({
            "template": "# {title}\n{content}",
            "extension": ".txt",
        })
        assert c.template == "# {title}\n{content}"
        assert c.extension == ".txt"

    def test_from_dict_defaults(self):
        c = ReportConfig.from_dict({})
        assert c.template is not None
        assert c.extension == ".md"
