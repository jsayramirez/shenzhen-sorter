"""
Single-writer protection with crash-safe stale-lock recovery (spec 21.3).

Why not just an OS file lock: Task Scheduler relaunches, duplicate watcher
events, and a manual double-click can all try to start a second writer.
An OS-level exclusive lock (msvcrt.locking / a lockfile opened without
delete-share) already prevents two *live* processes from writing at once -
that part is cheap and reliable. The harder problem this module solves is
the *stale* lock: a process that died (crash, kill, power loss) without
releasing it. We solve that with PID + heartbeat, not lock age alone,
because "old" and "dead" are different things (a legitimately slow big-file
hash could hold a lock for a long time without being stale).
"""

import json
import os
import time
from pathlib import Path

from config import settings


class LockHeld(RuntimeError):
    pass


def _pid_is_running(pid: int) -> bool:
    try:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    except Exception:
        # Non-Windows fallback (e.g. running unit tests off-Windows).
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
        except Exception:
            return True  # fail safe: assume alive if we truly can't tell


def _read_lock(path: Path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


class WriterLock:
    """Use as a context manager: `with WriterLock(): ...`"""

    def __init__(self, path: Path = None, stale_seconds: int = None):
        self.path = path if path is not None else settings.LOCK_FILE_PATH
        self.stale_seconds = stale_seconds if stale_seconds is not None else settings.LOCK_STALE_SECONDS
        self._acquired = False

    def _try_break_stale(self) -> None:
        existing = _read_lock(self.path)
        if existing is None:
            return
        age = time.time() - existing.get("heartbeat", 0)
        if age <= self.stale_seconds:
            return  # heartbeat still recent - never reclaim regardless of pid validity
        pid = existing.get("pid")
        if not isinstance(pid, int) or not _pid_is_running(pid):
            # Either confirmed dead (stale heartbeat + owning process gone),
            # or the lock file itself is malformed - acquire() always writes
            # a real int pid, so a missing/non-int one here means this file
            # was corrupted (e.g. a crash mid-write of the lock file itself),
            # not a normal live lock. A stale heartbeat plus an invalid pid
            # is strong enough evidence either way to reclaim rather than
            # block forever waiting for a pid that will never validate.
            self.path.unlink(missing_ok=True)

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._try_break_stale()
        try:
            # O_EXCL: atomically fails if the file already exists.
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing = _read_lock(self.path)
            raise LockHeld(
                f"Writer lock already held by pid={existing.get('pid') if existing else '?'}"
            )
        with os.fdopen(fd, "w") as f:
            json.dump({"pid": os.getpid(), "heartbeat": time.time()}, f)
        self._acquired = True

    def heartbeat(self) -> None:
        if not self._acquired:
            return
        self.path.write_text(json.dumps({"pid": os.getpid(), "heartbeat": time.time()}))

    def release(self) -> None:
        if self._acquired:
            self.path.unlink(missing_ok=True)
            self._acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False
