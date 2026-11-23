"""An exclusive lock for one run against a daily-state directory (issue #78).

The daily state file is atomic, but atomically writing the *last* writer's
counters is not coordination: a manual ``run --once`` started near the 03:30
fire time and the nightly daemon can both read the day as unfinished, both walk
the roster, and overwrite each other's completion counts while the device sees
two adb clients. This module supplies the missing piece: one advisory lock per
state directory, held for the whole run.

``flock(2)`` is the authority, not a PID in a file. A killed holder's file
descriptor is closed by the kernel, which releases the lock immediately, so a
stale holder cannot lock the collector out forever. The owner record in the
file is only there to make a refusal and a takeover legible: when the lock is
free but the record is non-empty, the previous holder died without cleaning up,
so the new owner logs exactly whose stale record it took over. A clean release
truncates the record while the lock is still held, which is why leftover
content is a reliable "the holder was killed" marker.

The lock file lives beside the daily files (``<state_dir>/run.lock``) and is
deliberately never unlinked: unlinking a file another waiter has open is the
classic way to hand two processes different lock files with the same name.
Readers and writers are therefore the same inode for the directory's lifetime.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from aliexpress_coins.runner.errors import RunLockHeld, StateError

__all__ = [
    'LOCK_FILENAME',
    'LOGGER_NAME',
    'LockHolder',
    'RunLock',
]

LOCK_FILENAME = 'run.lock'
"""The lock file's name inside the daily-state directory."""

LOGGER_NAME = 'aliexpress_coins.runner.lock'
"""The logger used when the caller does not supply one."""

_OWNER_READ_BYTES = 64 * 1024
"""Owner records are tiny; this only bounds a file someone else corrupted."""

Clock = Callable[[], datetime]
"""Supplies the owner record's start time; injected in tests."""


def _local_now() -> datetime:
    return datetime.now()


@dataclass(frozen=True, slots=True)
class LockHolder:
    """The process named by a lock file's owner record."""

    pid: int
    host: str
    started_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            'pid': self.pid,
            'host': self.host,
            'started_at': self.started_at.isoformat(),
        }

    def describe(self) -> str:
        return f'pid {self.pid} on {self.host}, started {self.started_at.isoformat()}'

    @classmethod
    def from_bytes(cls, raw: bytes) -> LockHolder | None:
        """Parse an owner record, returning ``None`` for anything unusable."""
        if not raw.strip():
            return None
        try:
            payload: Any = json.loads(raw.decode('utf-8'))
        except UnicodeDecodeError, json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        data = cast(dict[str, Any], payload)
        pid = data.get('pid')
        host = data.get('host')
        started = data.get('started_at')
        if isinstance(pid, bool) or not isinstance(pid, int) or pid < 0:
            return None
        if not isinstance(host, str) or not host.strip():
            return None
        if not isinstance(started, str):
            return None
        try:
            started_at = datetime.fromisoformat(started)
        except ValueError:
            return None
        return cls(pid=pid, host=host, started_at=started_at)


class RunLock:
    """One exclusive, non-blocking run lock for a daily-state directory.

    ``try_acquire`` takes the kernel lock without waiting. The caller decides
    how long to wait (a manual run refuses at once, the daemon waits bounded),
    which keeps the waiting policy in the entry point that knows about SIGTERM.
    A successful acquisition that finds a non-empty owner record logs a
    ``run_lock_takeover`` event before replacing it.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Clock = _local_now,
        logger: logging.Logger | None = None,
        pid: int | None = None,
        host: str | None = None,
    ) -> None:
        self.path = Path(path)
        self._clock = clock
        self._logger = logger if logger is not None else logging.getLogger(LOGGER_NAME)
        self._pid = os.getpid() if pid is None else pid
        self._host = socket.gethostname() if host is None else host
        self._fd: int | None = None
        self._holder: LockHolder | None = None
        self._recovered: LockHolder | None = None
        self._unreadable_owner: bool = False

    @property
    def held(self) -> bool:
        """Whether this object currently owns the lock."""
        return self._fd is not None

    @property
    def holder(self) -> LockHolder | None:
        """This object's owner record while it holds the lock."""
        return self._holder

    @property
    def recovered(self) -> LockHolder | None:
        """The stale owner record this acquisition took over, if any."""
        return self._recovered

    @property
    def unreadable_owner(self) -> bool:
        """Whether the previous owner record existed but could not be parsed."""
        return self._unreadable_owner

    def try_acquire(self) -> None:
        """Take the lock or raise :class:`RunLockHeld` without waiting.

        A free lock with a leftover owner record is a holder that was killed:
        the kernel already released its ``flock``, so takeover is safe and is
        logged. The method is deliberately not re-entrant; one run owns one
        :class:`RunLock`.
        """
        if self._fd is not None:
            raise StateError(f'the run lock at {self.path} is already held by this object')
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise StateError(f'could not open the run lock {self.path}: {exc}') from exc

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            holder = self._read_owner(fd)
            os.close(fd)
            detail = 'holder unknown' if holder is None else holder.describe()
            raise RunLockHeld(f'another run holds {self.path} ({detail})') from exc
        except OSError as exc:
            os.close(fd)
            raise StateError(f'could not lock {self.path}: {exc}') from exc

        previous = self._read_owner(fd)
        self._unreadable_owner = previous is None and self._raw_owner(fd).strip() != b''
        holder = LockHolder(pid=self._pid, host=self._host, started_at=self._clock())
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            self._write_all(fd, (json.dumps(holder.as_dict(), separators=(',', ':'), sort_keys=True) + '\n').encode())
            os.fsync(fd)
        except OSError as exc:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise StateError(f'could not write the run lock owner {self.path}: {exc}') from exc

        self._fd = fd
        self._holder = holder
        self._recovered = previous
        if previous is not None:
            self._warn(
                'run_lock_takeover',
                path=str(self.path),
                previous=previous.as_dict(),
                current=holder.as_dict(),
            )
        elif self._unreadable_owner:
            self._warn(
                'run_lock_takeover',
                path=str(self.path),
                previous=None,
                previous_detail='the owner record was unreadable',
                current=holder.as_dict(),
            )

    def release(self) -> None:
        """Release the lock and clear the owner record.

        Releasing a lock this object does not hold is a no-op, so a ``finally``
        block can call it on every path. A failed truncate is logged rather
        than raised: the lock is still released, and a stale record only costs
        the next run a ``run_lock_takeover`` warning.
        """
        fd = self._fd
        if fd is None:
            return
        try:
            os.ftruncate(fd, 0)
            os.fsync(fd)
        except OSError as exc:
            self._warn('run_lock_release_failed', path=str(self.path), detail=f'{type(exc).__name__}: {exc}')
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError as exc:  # pragma: no cover - closing the fd releases it anyway
                self._warn('run_lock_release_failed', path=str(self.path), detail=f'{type(exc).__name__}: {exc}')
            finally:
                os.close(fd)
                self._fd = None
                self._holder = None

    def __enter__(self) -> RunLock:
        self.try_acquire()
        return self

    def __exit__(self, *_exc: object) -> Literal[False]:
        self.release()
        return False

    @staticmethod
    def _read_owner(fd: int) -> LockHolder | None:
        return LockHolder.from_bytes(RunLock._raw_owner(fd))

    @staticmethod
    def _raw_owner(fd: int) -> bytes:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            return os.read(fd, _OWNER_READ_BYTES)
        except OSError:
            return b''

    @staticmethod
    def _write_all(fd: int, payload: bytes) -> None:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(fd, remaining)
            remaining = remaining[written:]

    def _warn(self, event: str, **fields: object) -> None:
        self._logger.warning(
            '%s',
            json.dumps({'event': event, **fields}, separators=(',', ':'), sort_keys=True),
        )
