"""Tests for cronpypeline.lock — FileLock (fcntl-based, non-blocking)."""

import os
import time

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
