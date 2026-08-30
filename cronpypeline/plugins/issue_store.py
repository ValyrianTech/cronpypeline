"""SWE issue store plugin — markdown files with YAML frontmatter.

Issues are stored as individual .md files in .SWE/issues/ with YAML frontmatter
containing metadata fields (id, source, type, status, attempts, etc.) and a
markdown body with the issue description.

This module uses a simple built-in frontmatter parser (no external YAML dependency).
"""

import argparse
import re
import sys
import warnings
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

# ─── Frontmatter parsing/serialization ──────────────────────────────────────


def _parse_value(raw: str) -> Any:
    """Parse a single YAML-like scalar value.

    :param raw: Raw string value to parse.
    :returns: Parsed value (bool, null, int, float, list, or string).
    """
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_parse_value(v.strip()) for v in inner.split(",")]
    if raw.lower() in ("true", "yes"):
        return True
    if raw.lower() in ("false", "no"):
        return False
    if raw.lower() in ("null", "none", "~"):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from a markdown string.

    :param text: Markdown text potentially starting with ``---`` frontmatter.
    :returns: Tuple of (frontmatter_dict, body_text).
    """
    if not text.startswith("---"):
        return {}, text

    lines = text.split("\n")
    # First line is ---
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        return {}, text

    fm_lines = lines[1:end_idx]
    body = "\n".join(lines[end_idx + 1:])

    fm: dict[str, Any] = {}
    for line in fm_lines:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        # If value starts with a quote, find the matching closing quote
        # to handle values containing colons (e.g., "Fix: Login bug")
        if len(value) >= 2 and value[0] in ("'", '"'):
            quote = value[0]
            if value[-1] == quote:
                close = value.rfind(quote)
            else:
                close = value.find(quote, 1)
            if close != -1:
                trailing = value[close + 1:]
                if trailing.strip():
                    warnings.warn(
                        f"Content after closing quote in frontmatter value for key {key!r} is ignored: {trailing!r}",
                        UserWarning,
                        stacklevel=2,
                    )
                value = value[:close + 1]
        fm[key] = _parse_value(value)

    return fm, body


def _needs_quoting(value: str) -> bool:
    """Return True if a string would be re-parsed as a non-string scalar.

    :param value: String value to inspect.
    :returns: True if the value looks like a bool, null, int, float, or list.
    """
    s = value.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return True
    if s.startswith("[") and s.endswith("]"):
        return True
    if s.lower() in ("true", "yes", "false", "no"):
        return True
    if s.lower() in ("null", "none", "~"):
        return True
    try:
        int(s)
        return True
    except ValueError:
        pass
    try:
        float(s)
        return True
    except ValueError:
        pass
    return False


def _serialize_value(value: Any) -> str:
    """Serialize a value to YAML-like scalar format.

    :param value: Value to serialize (list, bool, float, str, etc.).
    :returns: YAML-like string representation.
    """
    if isinstance(value, list):
        return "[" + ", ".join(_serialize_value(v) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return str(value)
    if value is None:
        return "null"
    if isinstance(value, str):
        if _needs_quoting(value):
            s = value.strip()
            if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
                if s[0] == "'":
                    return '"' + value + '"'
                return "'" + value + "'"
            return "'" + value + "'"
        return value
    return str(value)


def serialize_frontmatter(fm: dict[str, Any]) -> str:
    """Serialize a dict to YAML-like frontmatter text (without --- delimiters).

    :param fm: Frontmatter dict to serialize.
    :returns: YAML-like text string.
    """
    lines = []
    for key, value in fm.items():
        lines.append(f"{key}: {_serialize_value(value)}")
    return "\n".join(lines) + "\n"


# ─── Issue dataclass ────────────────────────────────────────────────────────


@dataclass
class Issue:
    """A single issue from the SWE issue store.

    :ivar id: Issue identifier.
    :ivar status: Issue status (e.g. ``"open"``, ``"fixed"``, ``"rejected"``).
    :ivar source: Source of the issue (e.g. ``"github"``, ``"manual"``).
    :ivar type: Issue type (e.g. ``"bug"``, ``"feature"``).
    :ivar attempts: Number of processing attempts.
    :ivar hivemind_score: Optional hivemind score.
    :ivar rank: Optional ranking.
    :ivar repo: Optional repository path.
    :ivar labels: List of labels.
    :ivar github_number: Optional GitHub issue number.
    :ivar github_url: Optional GitHub issue URL.
    :ivar created_at: Optional creation timestamp.
    :ivar body: Markdown body text of the issue.
    """

    id: Any
    status: str = "open"
    source: str | None = None
    type: str | None = None
    attempts: int = 0
    hivemind_score: float | None = None
    rank: int | None = None
    repo: str | None = None
    labels: list[str] = dc_field(default_factory=list)
    github_number: int | None = None
    github_url: str | None = None
    created_at: str | None = None
    body: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize the issue to a dict (excluding None optional fields).

        :returns: Dict with issue fields, omitting optional fields that are None.
        """
        d: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "attempts": self.attempts,
            "labels": self.labels,
            "body": self.body,
        }
        if self.source is not None:
            d["source"] = self.source
        if self.type is not None:
            d["type"] = self.type
        if self.hivemind_score is not None:
            d["hivemind_score"] = self.hivemind_score
        if self.rank is not None:
            d["rank"] = self.rank
        if self.repo is not None:
            d["repo"] = self.repo
        if self.github_number is not None:
            d["github_number"] = self.github_number
        if self.github_url is not None:
            d["github_url"] = self.github_url
        if self.created_at is not None:
            d["created_at"] = self.created_at
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Issue":
        """Create an Issue from a dict.

        :param d: Dictionary with issue fields.
        :returns: An :class:`Issue` instance.
        """
        return cls(
            id=d.get("id"),
            status=d.get("status", "open"),
            source=d.get("source"),
            type=d.get("type"),
            attempts=d.get("attempts", 0),
            hivemind_score=d.get("hivemind_score"),
            rank=d.get("rank"),
            repo=d.get("repo"),
            labels=d.get("labels", []),
            github_number=d.get("github_number"),
            github_url=d.get("github_url"),
            created_at=d.get("created_at"),
            body=d.get("body", ""),
        )


# ─── Issue store operations ─────────────────────────────────────────────────


def _issues_dir(target_dir: Path | str | None = None) -> Path:
    """Return the path to the issues directory for a target.

    :param target_dir: Target directory containing the ``.SWE/issues/`` folder.
    :returns: Path to the issues directory.
    :raises ValueError: If ``target_dir`` is None.
    """
    if target_dir is None:
        raise ValueError("target_dir is required")
    return Path(target_dir) / ".SWE" / "issues"


def _read_issue_file(path: Path) -> Issue:
    """Read a single issue file and parse frontmatter.

    :param path: Path to the issue ``.md`` file.
    :returns: An :class:`Issue` instance parsed from the file.
    """
    text = path.read_text()
    fm, body = parse_frontmatter(text)
    fm["body"] = body
    return Issue.from_dict(fm)


def _write_issue_file(path: Path, issue: Issue) -> None:
    """Write an issue to a file with frontmatter.

    :param path: Path to write the issue ``.md`` file to.
    :param issue: Issue instance to serialize.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = issue.to_dict()
    body = fm.pop("body", "")
    fm_text = serialize_frontmatter(fm)
    path.write_text(f"---\n{fm_text}---\n{body}")


def load_issues(target_dir: Path | str | None = None) -> list[Issue]:
    """Load all issues from ``.SWE/issues/*.md``.

    :param target_dir: Target directory containing the ``.SWE/issues/`` folder.
    :returns: List of :class:`Issue` objects.
    """
    issues_path = _issues_dir(target_dir)
    if not issues_path.exists():
        return []
    issues = []
    for path in sorted(issues_path.glob("*.md")):
        issue = _read_issue_file(path)
        if issue.id is None:
            continue
        issues.append(issue)
    return issues


def get_issue(target_dir: Path | str | None = None, issue_id: Any = None) -> Issue | None:
    """Get a single issue by id.

    :param target_dir: Target directory containing the ``.SWE/issues/`` folder.
    :param issue_id: Issue identifier to look up.
    :returns: The matching :class:`Issue`, or None if not found.
    """
    issues = load_issues(target_dir)
    for issue in issues:
        if issue.id == issue_id:
            return issue
    return None


def set_issue_status(target_dir: Path | str | None = None, issue_id: Any = None, status: str = "open") -> bool:
    """Update an issue's status field.

    :param target_dir: Target directory containing the ``.SWE/issues/`` folder.
    :param issue_id: Issue identifier to update.
    :param status: New status value.
    :returns: True if updated, False if issue not found.
    """
    issues_path = _issues_dir(target_dir)
    if not issues_path.exists():
        return False
    for path in issues_path.glob("*.md"):
        issue = _read_issue_file(path)
        if issue.id == issue_id:
            issue.status = status
            _write_issue_file(path, issue)
            return True
    return False


def issue_filename(issue_id: Any) -> str:
    """Return a safe filename (without ``.md`` extension) for an issue id.

    :param issue_id: Issue identifier to sanitize.
    :returns: Safe filename string, or ``"issue"`` if sanitization yields empty.
    """
    s = str(issue_id)
    if ".." in s:
        raise ValueError(f"Issue id contains '..': {issue_id!r}")
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")
    if not safe_id:
        safe_id = "issue"
    return safe_id


def create_issue(target_dir: Path | str | None = None, issue_data: dict[str, Any] | None = None, body: str = "") -> Issue:
    """Create a new issue .md file with frontmatter.

    :param target_dir: Target directory containing the ``.SWE/issues/`` folder.
    :param issue_data: Dict with issue fields (id, status, source, etc.).
    :param body: Markdown body text for the issue.
    :returns: The created :class:`Issue` instance.
    """
    if issue_data is None:
        issue_data = {}
    issue = Issue.from_dict(issue_data)
    issue.body = body
    safe_id = issue_filename(issue.id)
    issues_dir = _issues_dir(target_dir)
    filename = f"{safe_id}.md"
    path = (issues_dir / filename).resolve()
    if not path.is_relative_to(issues_dir.resolve()):
        raise ValueError(f"Issue path escapes issues directory: {filename}")
    if path.exists():
        existing = _read_issue_file(path)
        if existing.id != issue.id:
            warnings.warn(
                f"Issue id {issue.id!r} sanitizes to filename {filename!r}, which is "
                f"already used by issue id {existing.id!r}; the existing file will be "
                "overwritten.",
                UserWarning,
                stacklevel=2,
            )
    _write_issue_file(path, issue)
    return issue


def finalize_issue_outcome(target_dir: Path | str | None = None, issue_id: Any = None, outcome: str = "") -> bool:
    """Set final status and increment attempts counter.

    :param target_dir: Target directory containing the ``.SWE/issues/`` folder.
    :param issue_id: Issue identifier to finalize.
    :param outcome: Final status value (e.g. ``"fixed"``, ``"rejected"``).
    :returns: True if finalized, False if issue not found.
    """
    issues_path = _issues_dir(target_dir)
    if not issues_path.exists():
        return False
    for path in issues_path.glob("*.md"):
        issue = _read_issue_file(path)
        if issue.id == issue_id:
            issue.status = outcome
            issue.attempts += 1
            _write_issue_file(path, issue)
            return True
    return False


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _slugify(text: str) -> str:
    """Lowercase, collapse non-alphanumerics to single hyphens, strip ends.

    :param text: Text to slugify.
    :returns: Slug string suitable for use as an issue id or filename.
    """
    return re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for filing issues from agent review findings.

    Usage::

        python3 -m cronpypeline.plugins.issue_store file <repo_name> \\
            --type <bug|enhancement|refactor> --title "<title>" \\
            --body-file /tmp/swe_finding.md

    The target directory is assumed to be the current working directory
    (agents ``cd`` into the repo before running this command).

    :param argv: Optional argument list (defaults to ``sys.argv[1:]``).
    :returns: Exit code (0 on success, 1 on error).
    """
    parser = argparse.ArgumentParser(
        prog="issue_store",
        description="SWE issue store CLI — file issues from agent findings.",
    )
    subparsers = parser.add_subparsers(dest="command")

    file_parser = subparsers.add_parser("file", help="File a new issue")
    file_parser.add_argument("repo_name", help="Repository name")
    file_parser.add_argument(
        "--type", dest="issue_type",
        choices=["bug", "enhancement", "refactor"],
        default="bug",
        help="Issue type (default: bug)",
    )
    file_parser.add_argument("--title", required=True, help="Issue title")
    file_parser.add_argument(
        "--body-file", dest="body_file", required=True,
        help="Path to a file containing the issue body (markdown)",
    )

    args = parser.parse_args(argv)

    if args.command == "file":
        body_path = Path(args.body_file)
        if not body_path.exists():
            print(f"error: body file not found: {body_path}", file=sys.stderr)
            return 1
        body = body_path.read_text()

        issue_id = _slugify(args.title)
        if not issue_id:
            print("error: title produces empty slug", file=sys.stderr)
            return 1

        target_dir = Path.cwd()
        existing = get_issue(target_dir, issue_id)
        if existing is not None:
            print(f"issue already exists: {issue_id} (status: {existing.status})")
            return 0

        issue = create_issue(
            target_dir,
            issue_data={
                "id": issue_id,
                "status": "open",
                "source": "review",
                "type": args.issue_type,
                "attempts": 0,
                "repo": args.repo_name,
                "labels": [],
            },
            body=body,
        )
        print(f"filed issue: {issue.id} ({issue.type}) -> .SWE/issues/{issue.id}.md")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
