"""The exclusive run lock that keeps a manual run and the daemon apart (#78).

Every test injects a clock and a temporary directory. A "second process" is a
second store/lock object on the same directory, which is the same kernel lock
the real daemon and a manual run would contend for; nothing sleeps and nothing
spawns a process.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from aliexpress_coins.runner.errors import RunLockHeld, StateError
from aliexpress_coins.runner.lock import LOCK_FILENAME, LOGGER_NAME, LockHolder, RunLock
from aliexpress_coins.runner.state import RUN_LOCK_FILENAME, DailyStateStore

NOW = datetime(2026, 11, 22, 3, 30)


def _store(tmp_path: Path) -> DailyStateStore:
    return DailyStateStore(tmp_path / 'state', clock=lambda: NOW)


def _lock(tmp_path: Path, **kwargs: Any) -> RunLock:
    return RunLock(tmp_path / 'state' / LOCK_FILENAME, clock=lambda: NOW, **kwargs)


def _events(caplog: pytest.LogCaptureFixture, name: str | None = None) -> list[dict[str, Any]]:
    return [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == (LOGGER_NAME if name is None else name)
    ]


def test_two_stores_cannot_hold_the_same_day(tmp_path: Path) -> None:
    first = _store(tmp_path)
    second = _store(tmp_path)
    assert first.run_lock_path == second.run_lock_path == tmp_path / 'state' / RUN_LOCK_FILENAME

    first_lock = first.run_lock()
    second_lock = second.run_lock()
    first_lock.try_acquire()

    with pytest.raises(RunLockHeld, match='another run holds') as raised:
        second_lock.try_acquire()
    assert f'pid {os.getpid()}' in str(raised.value)
    assert not second_lock.held
    assert not first.path.exists()

    first_lock.release()
    second_lock.try_acquire()
    assert second_lock.held
    second_lock.release()


def test_a_clean_release_truncates_the_owner_record(tmp_path: Path) -> None:
    lock = _lock(tmp_path)

    lock.try_acquire()

    assert lock.held
    assert lock.holder is not None
    assert json.loads(lock.path.read_text(encoding='utf-8'))['pid'] == os.getpid()

    lock.release()

    assert not lock.held
    assert lock.holder is None
    assert lock.path.read_bytes() == b''


def test_a_stale_owner_record_is_taken_over_and_logged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stale = LockHolder(pid=4242, host='dead-box', started_at=NOW)
    lock = _lock(tmp_path)
    lock.path.parent.mkdir(parents=True)
    lock.path.write_text(json.dumps(stale.as_dict()) + '\n', encoding='utf-8')

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        lock.try_acquire()

    assert lock.recovered == stale
    assert lock.unreadable_owner is False
    events = _events(caplog)
    assert [event['event'] for event in events] == ['run_lock_takeover']
    assert events[0]['previous'] == stale.as_dict()
    assert lock.holder is not None
    assert events[0]['current'] == lock.holder.as_dict()
    lock.release()


def test_an_unreadable_stale_record_is_taken_over(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    lock = _lock(tmp_path)
    lock.path.parent.mkdir(parents=True)
    lock.path.write_bytes(b'{not json')

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        lock.try_acquire()

    assert lock.recovered is None
    assert lock.unreadable_owner is True
    events = _events(caplog)
    assert [event['event'] for event in events] == ['run_lock_takeover']
    assert events[0]['previous_detail'] == 'the owner record was unreadable'
    lock.release()


def test_an_empty_owner_record_is_a_clean_acquisition(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    lock = _lock(tmp_path)
    lock.path.parent.mkdir(parents=True)
    lock.path.touch()

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        lock.try_acquire()

    assert lock.recovered is None
    assert lock.unreadable_owner is False
    assert _events(caplog) == []
    lock.release()


def test_a_lock_is_not_reentrant(tmp_path: Path) -> None:
    lock = _lock(tmp_path)
    lock.try_acquire()

    with pytest.raises(StateError, match='already held by this object'):
        lock.try_acquire()

    lock.release()


def test_release_is_idempotent_and_the_context_manager_holds_the_lock(tmp_path: Path) -> None:
    lock = _lock(tmp_path)

    with lock:
        assert lock.held
        second = _lock(tmp_path)
        with pytest.raises(RunLockHeld):
            second.try_acquire()

    assert not lock.held
    lock.release()
    assert not lock.held


def test_an_unopenable_lock_path_is_a_typed_error(tmp_path: Path) -> None:
    blocker = tmp_path / 'state'
    blocker.write_text('not a directory', encoding='utf-8')

    with pytest.raises(StateError, match='could not open the run lock'):
        _lock(tmp_path).try_acquire()


def test_a_flock_failure_is_a_typed_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: object, **_kwargs: object) -> None:
        raise OSError('flock unavailable')

    lock = _lock(tmp_path)
    monkeypatch.setattr(fcntl, 'flock', broken)

    with pytest.raises(StateError, match='could not lock'):
        lock.try_acquire()

    assert not lock.held


def test_a_failed_owner_write_releases_the_kernel_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = _lock(tmp_path)

    def broken(*_args: object, **_kwargs: object) -> None:
        raise OSError('disk full')

    with monkeypatch.context() as patch:
        patch.setattr(os, 'ftruncate', broken)
        with pytest.raises(StateError, match='could not write the run lock owner'):
            lock.try_acquire()

    assert not lock.held
    lock.try_acquire()
    lock.release()


def test_a_failed_release_truncate_is_logged_and_still_unlocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    lock = _lock(tmp_path)
    lock.try_acquire()

    def broken(*_args: object, **_kwargs: object) -> None:
        raise OSError('read-only')

    with monkeypatch.context() as patch:
        patch.setattr(os, 'ftruncate', broken)
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            lock.release()

    assert not lock.held
    events = _events(caplog)
    assert events[0]['event'] == 'run_lock_release_failed'
    assert 'read-only' in events[0]['detail']
    lock.try_acquire()
    lock.release()


def test_a_partial_owner_write_is_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = _lock(tmp_path)
    real_write = os.write

    def one_byte(fd: int, payload: Any) -> int:
        return real_write(fd, bytes(payload[:1]))

    with monkeypatch.context() as patch:
        patch.setattr(os, 'write', one_byte)
        lock.try_acquire()

    record = json.loads(lock.path.read_text(encoding='utf-8'))
    assert record['pid'] == os.getpid()
    lock.release()


def test_an_unreadable_owner_record_is_treated_as_empty_for_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = _lock(tmp_path)

    def broken(*_args: object, **_kwargs: object) -> bytes:
        raise OSError('unreadable')

    with monkeypatch.context() as patch:
        patch.setattr(os, 'read', broken)
        lock.try_acquire()

    assert lock.recovered is None
    assert lock.unreadable_owner is False
    lock.release()


def test_a_blocked_lock_with_an_empty_record_names_the_holder_as_unknown(tmp_path: Path) -> None:
    path = tmp_path / 'state' / LOCK_FILENAME
    path.parent.mkdir(parents=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(RunLockHeld, match='holder unknown'):
            _lock(tmp_path).try_acquire()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_the_default_identity_and_clock_are_used(tmp_path: Path) -> None:
    lock = RunLock(tmp_path / 'state' / LOCK_FILENAME)

    lock.try_acquire()

    assert lock.holder is not None
    assert lock.holder.pid == os.getpid()
    assert lock.holder.host
    assert lock.holder.started_at.tzinfo is None
    lock.release()


@pytest.mark.parametrize(
    'raw',
    [
        b'',
        b'   ',
        b'{not json',
        b'\xff\xfe',
        b'[]',
        b'{"pid": true, "host": "box", "started_at": "2026-11-22T03:30:00"}',
        b'{"pid": -1, "host": "box", "started_at": "2026-11-22T03:30:00"}',
        b'{"pid": "1", "host": "box", "started_at": "2026-11-22T03:30:00"}',
        b'{"pid": 1, "host": "", "started_at": "2026-11-22T03:30:00"}',
        b'{"pid": 1, "host": 5, "started_at": "2026-11-22T03:30:00"}',
        b'{"pid": 1, "host": "box", "started_at": 5}',
        b'{"pid": 1, "host": "box", "started_at": "yesterday"}',
        b'{"host": "box", "started_at": "2026-11-22T03:30:00"}',
    ],
)
def test_an_unusable_owner_record_parses_to_none(raw: bytes) -> None:
    assert LockHolder.from_bytes(raw) is None


def test_a_valid_owner_record_round_trips() -> None:
    holder = LockHolder(pid=7, host='box', started_at=NOW)

    assert LockHolder.from_bytes(json.dumps(holder.as_dict()).encode()) == holder
    assert holder.describe() == 'pid 7 on box, started 2026-11-22T03:30:00'
