"""FileLock — fcntl-based, non-blocking single-instance lock for cron pipelines."""

import fcntl
import os
import time
from pathlib import Path
from types import TracebackType


class FileLock:
    """Non-blocking file lock using fcntl.flock.

    - Acquires ``LOCK_EX | LOCK_NB`` (fails immediately if already locked).
    - Writes PID + timestamp to the lock file for debugging stale locks.
    - Dry-run mode bypasses locking entirely.
    - Supports context manager protocol.

    :param lock_file: Path to the lock file.
    :param dry_run: If True, bypasses actual locking.
    """

    def __init__(self, lock_file: Path | str | None = None, dry_run: bool = False) -> None:
        """Initialize a file lock.

        :param lock_file: Path to the lock file.
        :type lock_file: Path | str | None
        :param dry_run: If True, bypasses actual locking.
        :type dry_run: bool
        :raises ValueError: If ``lock_file`` is None.
        """
        if lock_file is None:
            raise ValueError("lock_file is required")
        self.lock_file = Path(lock_file)
        self.dry_run = dry_run
        self._fd: int | None = None
        self._acquired = False

    def acquire(self) -> bool:
        """Attempt to acquire the lock.

        :returns: True on success, False if already locked by another process.
        """
        if self._acquired:
            return True

        if self.dry_run:
            self._acquired = True
            return True

        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR, 0o644)

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            os.close(fd)
            return False

        self._fd = fd
        self._acquired = True

        # Write PID and timestamp for debugging
        content = f"pid:{os.getpid()}\ntimestamp:{time.time()}\n"
        os.ftruncate(fd, 0)
        os.write(fd, content.encode())
        os.fsync(fd)

        return True

    def release(self) -> None:
        """Release the lock if held. No-op if not acquired."""
        if not self._acquired:
            return

        if self.dry_run:
            self._acquired = False
            return

        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

        self._acquired = False

    def __enter__(self) -> "FileLock":  # noqa: PYI034
        """Acquire the lock and return self for use as a context manager.

        :returns: The :class:`FileLock` instance.
        :rtype: FileLock
        """
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Release the lock when exiting the context manager.

        :param exc_type: Exception type, if an exception occurred.
        :type exc_type: type[BaseException] | None
        :param exc_val: Exception value, if an exception occurred.
        :type exc_val: BaseException | None
        :param exc_tb: Traceback, if an exception occurred.
        :type exc_tb: TracebackType | None
        """
        self.release()

    @property
    def is_acquired(self) -> bool:
        """Whether the lock is currently held."""
        return self._acquired
