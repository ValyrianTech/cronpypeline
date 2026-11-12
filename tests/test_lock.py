"""Tests for cronpypeline.lock — FileLock (cross-platform, non-blocking)."""

import errno
import importlib
import os
import sys
import time
from unittest import mock

import pytest

import cronpypeline.lock as lock_module
from cronpypeline.lock import FileLock


class TestFileLockAcquisition:
    """Tests for lock acquisition and release."""

    def test_acquire_lock_succeeds(self, tmp_path):
        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file)
        assert lock.acquire() is True
        lock.release()

    def test_lock_file_created_on_acquire(self, tmp_path):
        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file)
        lock.acquire()
        assert lock_file.exists()
        lock.release()

    def test_release_does_not_delete_lock_file(self, tmp_path):
        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file)
        lock.acquire()
        lock.release()
        assert lock_file.exists()

    def test_second_acquire_fails_when_locked(self, tmp_path):
        lock_file = tmp_path / "pipeline.lock"
        lock1 = FileLock(lock_file)
        lock2 = FileLock(lock_file)
        assert lock1.acquire() is True
        assert lock2.acquire() is False
        lock1.release()

    def test_acquire_after_release_succeeds(self, tmp_path):
        lock_file = tmp_path / "pipeline.lock"
        lock1 = FileLock(lock_file)
        lock1.acquire()
        lock1.release()
        lock2 = FileLock(lock_file)
        assert lock2.acquire() is True
        lock2.release()

    def test_reentrant_acquire_same_process_succeeds(self, tmp_path):
        """Same process re-acquiring should succeed (fcntl locks are per-process)."""
        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file)
        assert lock.acquire() is True
        # Re-acquire on same FileLock object should be idempotent
        assert lock.acquire() is True
        lock.release()


class TestFileLockDryRun:
    """Tests for dry-run bypass."""

    def test_dry_run_acquire_does_not_lock(self, tmp_path):
        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file, dry_run=True)
        assert lock.acquire() is True
        # A second lock should still be able to acquire
        lock2 = FileLock(lock_file)
        assert lock2.acquire() is True
        lock2.release()
        lock.release()

    def test_dry_run_does_not_create_lock_file(self, tmp_path):
        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file, dry_run=True)
        lock.acquire()
        assert not lock_file.exists()
        lock.release()


class TestFileLockPidTimestamp:
    """Tests for PID/timestamp recording in lock file."""

    def test_lock_file_contains_pid(self, tmp_path):
        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file)
        lock.acquire()
        content = lock_file.read_text()
        assert str(os.getpid()) in content
        lock.release()

    def test_lock_file_contains_timestamp(self, tmp_path):
        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file)
        before = time.time()
        lock.acquire()
        after = time.time()
        content = lock_file.read_text()
        # Prepend a malformed line to exercise the defensive parse branch
        content = "malformed-line-without-timestamp\n" + content
        # Should contain an ISO timestamp or epoch
        # Check that a number (timestamp) is present
        lines = content.strip().split("\n")
        found_ts = False
        for line in lines:
            try:
                val = float(line.split(":")[-1].strip())
                if before <= val <= after:
                    found_ts = True
                    break
            except (ValueError, IndexError):
                pass
        assert found_ts, f"No valid timestamp found in lock file: {content}"
        lock.release()


class TestFileLockContextManager:
    """Tests for context manager protocol."""

    def test_context_manager_acquires_and_releases(self, tmp_path):
        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file)
        with lock:
            assert lock_file.exists()
        # After context, a new lock should acquire
        lock2 = FileLock(lock_file)
        assert lock2.acquire() is True
        lock2.release()

    def test_context_manager_with_dry_run(self, tmp_path):
        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file, dry_run=True)
        with lock:
            pass
        assert not lock_file.exists()

    def test_context_manager_raises_when_lock_not_acquired(self, tmp_path):
        """Entering context manager should raise RuntimeError if lock is held."""
        import pytest
        lock_file = tmp_path / "pipeline.lock"
        lock1 = FileLock(lock_file)
        lock1.acquire()
        lock2 = FileLock(lock_file)
        with pytest.raises(RuntimeError, match="Could not acquire lock"):
            lock2.__enter__()
        lock1.release()


class TestFileLockReleaseWithoutAcquire:
    """Tests for edge cases."""

    def test_release_without_acquire_is_noop(self, tmp_path):
        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file)
        # Should not raise
        lock.release()

    def test_double_release_is_noop(self, tmp_path):
        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file)
        lock.acquire()
        lock.release()
        # Second release should not raise
        lock.release()


class TestFileLockConstructor:
    """Tests for FileLock constructor edge cases."""

    def test_none_lock_file_raises_value_error(self):
        """None lock_file should raise ValueError."""
        import pytest
        with pytest.raises(ValueError, match="lock_file is required"):
            FileLock(None)


class TestFileLockReleaseWithFd:
    """Tests for release path with actual fd."""

    def test_release_closes_fd(self, tmp_path):
        """Release should close the file descriptor and set _fd to None."""
        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file)
        lock.acquire()
        assert lock._fd is not None
        lock.release()
        assert lock._fd is None
        assert lock._acquired is False

    def test_context_manager_exit_calls_release(self, tmp_path):
        """__exit__ should call release and clean up."""
        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file)
        with lock:
            assert lock._acquired is True
        assert lock._acquired is False


class TestFileLockIsAcquired:
    """Tests for is_acquired property."""

    def test_is_acquired_false_before_acquire(self, tmp_path):
        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file)
        assert lock.is_acquired is False

    def test_is_acquired_true_after_acquire(self, tmp_path):
        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file)
        lock.acquire()
        assert lock.is_acquired is True
        lock.release()

    def test_is_acquired_false_after_release(self, tmp_path):
        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file)
        lock.acquire()
        lock.release()
        assert lock.is_acquired is False


class TestFileLockErrorHandling:
    """Tests for flock error handling in FileLock.acquire."""

    def test_flock_eintr_is_retried_then_succeeds(self, tmp_path):
        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file)
        with mock.patch(
            "cronpypeline.lock.fcntl.flock",
            side_effect=[OSError(errno.EINTR, "Interrupted system call"), None],
        ) as mocked_flock:
            assert lock.acquire() is True
            assert lock.is_acquired is True
            assert mocked_flock.call_count == 2
        lock.release()

    def test_flock_unexpected_oserror_is_reraised(self, tmp_path):
        import pytest
        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file)
        with mock.patch(
            "cronpypeline.lock.fcntl.flock",
            side_effect=OSError(errno.ENOLCK, "No locks available"),
        ):
            with pytest.raises(OSError) as exc_info:
                lock.acquire()
            assert exc_info.value.errno == errno.ENOLCK
        assert lock.is_acquired is False

    def test_flock_badf_is_reraised(self, tmp_path):
        import pytest
        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file)
        with mock.patch(
            "cronpypeline.lock.fcntl.flock",
            side_effect=OSError(errno.EBADF, "Bad file descriptor"),
        ):
            with pytest.raises(OSError) as exc_info:
                lock.acquire()
            assert exc_info.value.errno == errno.EBADF
        assert lock.is_acquired is False

    def test_flock_contention_returns_false(self, tmp_path):
        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file)
        with mock.patch(
            "cronpypeline.lock.fcntl.flock",
            side_effect=BlockingIOError(errno.EAGAIN, "Try again"),
        ):
            assert lock.acquire() is False
        assert lock.is_acquired is False


class TestFileLockMetadataWriteFailure:
    """Tests for rollback when writing PID/timestamp metadata fails."""

    def _recording_close(self):
        """Patch os.close to record closed fds while still closing them."""
        closed_fds = []
        real_close = os.close

        def recording_close(fd):
            closed_fds.append(fd)
            real_close(fd)

        return closed_fds, mock.patch(
            "cronpypeline.lock.os.close", side_effect=recording_close
        )

    def _assert_fd_closed(self, fd):
        import pytest

        with pytest.raises(OSError) as exc_info:
            os.fstat(fd)
        assert exc_info.value.errno == errno.EBADF

    def test_write_oserror_rolls_back_and_closes_fd(self, tmp_path):
        import pytest

        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file)
        closed_fds, close_patch = self._recording_close()
        with mock.patch(
            "cronpypeline.lock.os.write",
            side_effect=OSError(errno.ENOSPC, "No space left on device"),
        ), close_patch:
            with pytest.raises(OSError) as exc_info:
                lock.acquire()
            assert exc_info.value.errno == errno.ENOSPC

        assert lock.is_acquired is False
        assert lock._fd is None
        assert len(closed_fds) == 1
        self._assert_fd_closed(closed_fds[0])

    def test_ftruncate_oserror_rolls_back_and_closes_fd(self, tmp_path):
        import pytest

        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file)
        closed_fds, close_patch = self._recording_close()
        with mock.patch(
            "cronpypeline.lock.os.ftruncate",
            side_effect=OSError(errno.ENOSPC, "No space left on device"),
        ), close_patch:
            with pytest.raises(OSError) as exc_info:
                lock.acquire()
            assert exc_info.value.errno == errno.ENOSPC

        assert lock.is_acquired is False
        assert lock._fd is None
        assert len(closed_fds) == 1
        self._assert_fd_closed(closed_fds[0])

    def test_fsync_oserror_rolls_back_and_closes_fd(self, tmp_path):
        import pytest

        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file)
        closed_fds, close_patch = self._recording_close()
        with mock.patch(
            "cronpypeline.lock.os.fsync",
            side_effect=OSError(errno.ENOSPC, "No space left on device"),
        ), close_patch:
            with pytest.raises(OSError) as exc_info:
                lock.acquire()
            assert exc_info.value.errno == errno.ENOSPC

        assert lock.is_acquired is False
        assert lock._fd is None
        assert len(closed_fds) == 1
        self._assert_fd_closed(closed_fds[0])

    def test_acquire_succeeds_after_failed_metadata_write(self, tmp_path):
        import pytest

        lock_file = tmp_path / "pipeline.lock"
        lock = FileLock(lock_file)
        with mock.patch(
            "cronpypeline.lock.os.write",
            side_effect=OSError(errno.ENOSPC, "No space left on device"),
        ), pytest.raises(OSError):
            lock.acquire()
        assert lock.is_acquired is False
        assert lock._fd is None

        lock2 = FileLock(lock_file)
        assert lock2.acquire() is True
        assert lock2.is_acquired is True
        lock2.release()


class _FakeMsvcrt:
    """Stand-in for the Windows-only ``msvcrt`` module."""

    LK_NBLCK = 1
    LK_UNLCK = 2

    def __init__(self, fail_lock=False):
        self.fail_lock = fail_lock
        self.calls = []
        self.positions = []
        self.locked_range = None
        self.locked_fd = None

    def locking(self, fd, mode, nbytes):
        pos = os.lseek(fd, 0, os.SEEK_CUR)
        self.calls.append((fd, mode, nbytes))
        self.positions.append((mode, pos))
        if self.fail_lock:
            raise OSError(errno.EACCES, "Permission denied")
        if mode == self.LK_NBLCK:
            start = pos
            self.locked_range = (start, nbytes)
            self.locked_fd = fd
        elif mode == self.LK_UNLCK:
            self.assert_locked_range_intact()
            if self.locked_range is not None:
                locked_start, _ = self.locked_range
                if pos != locked_start:
                    raise AssertionError(
                        f"unlock performed at position {pos} but lock held at "
                        f"offset {locked_start}"
                    )
            self.locked_range = None
            self.locked_fd = None

    def assert_locked_range_intact(self):
        if self.locked_range is None:
            return
        start, nbytes = self.locked_range
        size = os.fstat(self.locked_fd).st_size
        if size < start + nbytes:
            raise AssertionError(
                f"file truncated below locked range "
                f"(size {size} < {start + nbytes})"
            )


class TestFileLockWindowsMsvcrt:
    """Tests for the Windows ``msvcrt.locking`` backend (faked on POSIX)."""

    def test_msvcrt_acquire_and_release(self, tmp_path):
        fake = _FakeMsvcrt()
        lock = FileLock(tmp_path / "pipeline.lock")
        with mock.patch.object(lock_module, "fcntl", None), mock.patch.object(
            lock_module, "msvcrt", fake
        ):
            assert lock.acquire() is True
            fd = lock._fd
            assert fake.calls == [(fd, _FakeMsvcrt.LK_NBLCK, 1)]
            lock.release()
            assert fake.calls == [
                (fd, _FakeMsvcrt.LK_NBLCK, 1),
                (fd, _FakeMsvcrt.LK_UNLCK, 1),
            ]
        assert lock.is_acquired is False
        assert lock._fd is None

    def test_msvcrt_contention_returns_false(self, tmp_path):
        fake = _FakeMsvcrt(fail_lock=True)
        lock = FileLock(tmp_path / "pipeline.lock")
        with mock.patch.object(lock_module, "fcntl", None), mock.patch.object(
            lock_module, "msvcrt", fake
        ):
            assert lock.acquire() is False
            assert lock.is_acquired is False
            assert lock._fd is None

    def test_msvcrt_acquire_nonempty_file_skips_placeholder(self, tmp_path):
        lock_file = tmp_path / "pipeline.lock"
        lock_file.write_text("existing content")
        fake = _FakeMsvcrt()
        lock = FileLock(lock_file)
        with mock.patch.object(lock_module, "fcntl", None), mock.patch.object(
            lock_module, "msvcrt", fake
        ):
            assert lock.acquire() is True
            fd = lock._fd
            assert fake.calls == [(fd, _FakeMsvcrt.LK_NBLCK, 1)]
            lock.release()

    def test_msvcrt_metadata_write_never_truncates_below_locked_byte(self, tmp_path):
        lock_file = tmp_path / "pipeline.lock"
        fake = _FakeMsvcrt()
        lock = FileLock(lock_file)
        truncate_lengths = []
        real_ftruncate = os.ftruncate

        def recording_ftruncate(fd, length):
            truncate_lengths.append(length)
            return real_ftruncate(fd, length)

        with mock.patch.object(lock_module, "fcntl", None), mock.patch.object(
            lock_module, "msvcrt", fake
        ), mock.patch.object(
            lock_module.os, "ftruncate", side_effect=recording_ftruncate
        ):
            assert lock.acquire() is True
            fake.assert_locked_range_intact()
            lock.release()

        assert str(os.getpid()) in lock_file.read_text()
        assert all(length >= 1 for length in truncate_lengths)
        assert lock_file.stat().st_size >= 1

    def test_fake_msvcrt_detects_unlock_at_wrong_position(self, tmp_path):
        lock_file = tmp_path / "pipeline.lock"
        fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            fake = _FakeMsvcrt()
            fake.locking(fd, _FakeMsvcrt.LK_NBLCK, 1)
            assert fake.locked_range == (0, 1)
            os.lseek(fd, 5, os.SEEK_SET)
            with pytest.raises(
                AssertionError, match="unlock performed at position"
            ):
                fake.locking(fd, _FakeMsvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)

    def test_fake_msvcrt_detects_truncation_below_locked_range(self, tmp_path):
        lock_file = tmp_path / "pipeline.lock"
        fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            fake = _FakeMsvcrt()
            fake.assert_locked_range_intact()
            fake.locking(fd, _FakeMsvcrt.LK_NBLCK, 1)
            assert fake.locked_range == (0, 1)
            os.ftruncate(fd, 0)
            with pytest.raises(AssertionError):
                fake.assert_locked_range_intact()
            with pytest.raises(AssertionError):
                fake.locking(fd, _FakeMsvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)


class TestFileLockNoBackend:
    """Tests for the no-locking-backend error path."""

    def test_acquire_raises_runtime_error_without_backend(self, tmp_path):
        lock = FileLock(tmp_path / "pipeline.lock")
        with mock.patch.object(lock_module, "fcntl", None), mock.patch.object(
            lock_module, "msvcrt", None
        ):
            with pytest.raises(RuntimeError, match="No file-locking backend"):
                lock.acquire()
        assert lock.is_acquired is False


class TestFileLockImportFallback:
    """Tests for the guarded import fallback paths."""

    def test_fcntl_import_fallback_sets_none(self):
        try:
            with mock.patch.dict(sys.modules, {"fcntl": None}):
                importlib.reload(lock_module)
                assert lock_module.fcntl is None
        finally:
            importlib.reload(lock_module)
