"""safe_io — locked read-modify-write and atomic JSON writes for local state files.

Two problems this solves (pre_trade_check, scanner/journal_io, portfolio_store):

1. Torn / lost files: writing in place can leave a truncated file if the process dies
   mid-write. ``write_json_atomic`` writes a unique temp file in the same directory,
   fsyncs it, then ``os.replace``s it over the target.
2. Lost updates: Streamlit reruns, the Telegram bot and the scanner can each read a file,
   modify it and write it back at the same time; the last writer silently discards the
   others' rows. Wrap the whole read-modify-write in ``locked(path)``.

``locked`` uses ``fcntl.flock`` on ``<path>.lock`` (works across processes and threads,
macOS/Linux only). It is re-entrant per thread, so a locked function may call another
function that takes the same lock.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

_registry_guard = threading.Lock()
_locks: dict[str, "_PathLock"] = {}


class _PathLock:
    """One per path per process: a thread RLock plus a single flock'd file handle."""

    def __init__(self, path: str) -> None:
        self.lock_path = path + ".lock"
        self.rlock = threading.RLock()
        self.depth = 0
        self.fh = None

    def acquire(self) -> None:
        self.rlock.acquire()
        try:
            if self.depth == 0:
                os.makedirs(os.path.dirname(self.lock_path) or ".", exist_ok=True)
                fh = open(self.lock_path, "a")
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                except BaseException:
                    fh.close()
                    raise
                self.fh = fh
            self.depth += 1
        except BaseException:
            self.rlock.release()
            raise

    def release(self) -> None:
        try:
            self.depth -= 1
            if self.depth == 0 and self.fh is not None:
                try:
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
                finally:
                    self.fh.close()
                    self.fh = None
        finally:
            self.rlock.release()


def _path_lock(path: str) -> _PathLock:
    key = os.path.abspath(path)
    with _registry_guard:
        pl = _locks.get(key)
        if pl is None:
            pl = _locks[key] = _PathLock(key)
        return pl


@contextmanager
def locked(path: str) -> Iterator[None]:
    """Exclusive lock for a read-modify-write of ``path``. Re-entrant within a thread."""
    pl = _path_lock(path)
    pl.acquire()
    try:
        yield
    finally:
        pl.release()


def write_json_atomic(path: str, data: Any, *, indent: int = 2) -> None:
    """Write JSON so readers see either the old file or the complete new one, never a partial."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=indent)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
