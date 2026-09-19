"""Atomic filesystem write helpers.

State and marker files are the pipeline's source of truth. Writing them with a
plain ``Path.write_text`` truncates the file in place, so a crash mid-write
leaves a partially-written (corrupt) JSON file that readers then swallow as
'missing state'. ``write_json_atomic`` avoids this by writing to a temporary
file in the same directory, fsync-ing it, and atomically renaming it into place.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path, data: Any, *, indent: int | None = 2) -> None:
    """Atomically write ``data`` as JSON to ``path``.

    The data is serialized to a temporary file in the destination directory,
    flushed and fsync-ed, then atomically moved into place with
    :func:`os.replace`. A crash at any point leaves either the old file or the
    new file intact -- never a truncated/partial file.

    :param path: Destination file path. Parent directories are created if needed.
    :param data: JSON-serializable object to write.
    :param indent: JSON indentation passed to :func:`json.dump`, or None.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def write_text_atomic(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically write ``text`` to ``path`` (temp file + fsync + os.replace).

    :param path: Destination file path. Parent directories are created if needed.
    :param text: Text content to write.
    :param encoding: Text encoding used for the write.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
