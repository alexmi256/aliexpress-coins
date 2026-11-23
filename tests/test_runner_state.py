"""Daily state survives a kill, a corrupt file and the midnight rollover (#30).

Every test injects a clock and a temporary directory. Nothing sleeps, nothing
touches a device, and a "restart" is a second store object on the same
directory — the same thing a killed container does.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from aliexpress_coins.runner import state as runner_state
from aliexpress_coins.runner.errors import StateError, StateReadError, StateWriteError
from aliexpress_coins.runner.state import (
    DAILY_STATE_SCHEMA,
    LOGGER_NAME,
    DailySnapshot,
    DailyStateStore,
)

DAY = date(2026, 11, 21)
NOW = datetime(2026, 11, 21, 21, 30)


class _Clock:
    """A clock whose value can be moved forward without sleeping."""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _fixed_clock(value: datetime = NOW) -> Callable[[], datetime]:
    return lambda: value


def _store(tmp_path: Path, clock: Callable[[], datetime] | None = None) -> DailyStateStore:
    return DailyStateStore(tmp_path, clock=_fixed_clock() if clock is None else clock)


def _path(tmp_path: Path) -> Path:
    return tmp_path / 'daily-2026-11-21.json'


def _valid_payload() -> dict[str, Any]:
    return {
        'schema': DAILY_STATE_SCHEMA,
        'date': DAY.isoformat(),
        'completions': {'daily_checkin': 1},
        'last_balance': 120.0,
        'run_count': 1,
        'last_run_at': NOW.isoformat(),
        'completed_night': False,
    }


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload) + '\n', encoding='utf-8')


def test_the_default_clock_uses_the_local_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> _FrozenDateTime:
            return cls(2026, 11, 21, 21, 30)

    monkeypatch.setattr(runner_state, 'datetime', _FrozenDateTime)

    store = DailyStateStore(tmp_path)

    assert store.snapshot().day == DAY
    assert store.path == tmp_path / 'daily-2026-11-21.json'


def test_a_missing_day_starts_empty_and_creates_the_directory(tmp_path: Path) -> None:
    state_dir = tmp_path / 'nested' / 'state'

    store = DailyStateStore(state_dir, clock=_fixed_clock())

    assert store.state_dir == state_dir
    assert store.path == state_dir / 'daily-2026-11-21.json'
    assert state_dir.is_dir()
    assert store.snapshot() == DailySnapshot(day=DAY, completions={})
    assert store.snapshot().as_dict() == {
        'schema': DAILY_STATE_SCHEMA,
        'date': DAY.isoformat(),
        'completions': {},
        'last_balance': None,
        'run_count': 0,
        'last_run_at': None,
        'completed_night': False,
    }
    assert store.count('daily_checkin') == 0
    assert store.done_today('daily_checkin') is False
    assert not store.path.exists()


def test_record_counts_a_task_and_writes_plain_json(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.record('sponsored_items') == 1
    assert store.record('sponsored_items') == 2

    assert store.count('sponsored_items') == 2
    assert store.done_today('sponsored_items') is True
    assert store.done_today('daily_checkin') is False
    assert json.loads(store.path.read_text(encoding='utf-8')) == {
        'schema': DAILY_STATE_SCHEMA,
        'date': DAY.isoformat(),
        'completions': {'sponsored_items': 2},
        'last_balance': None,
        'run_count': 0,
        'last_run_at': None,
        'completed_night': False,
    }
    assert store.path.read_text(encoding='utf-8').endswith('\n')
    assert [item.name for item in tmp_path.iterdir()] == ['daily-2026-11-21.json']


def test_a_fresh_store_resumes_without_repeating_a_completed_task(tmp_path: Path) -> None:
    first = _store(tmp_path)
    first.record('daily_checkin')

    second = _store(tmp_path)

    assert second.done_today('daily_checkin') is True
    assert second.count('daily_checkin') == 1
    assert second.snapshot() == first.snapshot()


def test_counters_survive_a_restart_within_the_same_day(tmp_path: Path) -> None:
    first = _store(tmp_path)
    for _ in range(3):
        first.record('coins_quiz')

    second = _store(tmp_path)

    assert second.count('coins_quiz') == 3
    assert second.done_today('coins_quiz') is True


def test_begin_run_counts_runs_and_stamps_the_start(tmp_path: Path) -> None:
    clock = _Clock(NOW)
    store = _store(tmp_path, clock=clock)

    assert store.begin_run() == 1
    later = datetime(2026, 11, 21, 22, 0)
    assert store.begin_run(at=later) == 2

    snapshot = store.snapshot()
    assert snapshot.run_count == 2
    assert snapshot.last_run_at == later
    assert _store(tmp_path, clock=clock).snapshot().last_run_at == later


def test_complete_night_is_the_schedulers_skip_flag_and_survives_a_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.begin_run(at=NOW)
    assert store.snapshot().completed_night is False

    store.complete_night()

    assert store.snapshot().completed_night is True
    assert _store(tmp_path).snapshot().completed_night is True


def test_reload_replaces_a_cached_snapshot_written_by_another_store(tmp_path: Path) -> None:
    waiter = _store(tmp_path)
    other = _store(tmp_path)
    assert waiter.snapshot().completed_night is False

    other.complete_night()

    assert waiter.snapshot().completed_night is False
    waiter.reload()
    assert waiter.snapshot().completed_night is True


def test_an_old_state_file_without_the_night_flag_still_loads(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload.pop('completed_night')
    _write_json(_path(tmp_path), payload)

    snapshot = _store(tmp_path).snapshot()

    assert snapshot.completed_night is False


def test_record_carries_the_last_balance_without_clobbering_it(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.record('daily_checkin', balance=120)
    assert store.snapshot().last_balance == 120.0

    store.record('boost_checkin')
    assert store.snapshot().last_balance == 120.0

    store.observe_balance(99.5)
    assert store.snapshot().last_balance == 99.5
    assert _store(tmp_path).snapshot().last_balance == 99.5


def test_snapshot_gives_the_report_the_whole_day(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.begin_run(at=NOW)
    store.record('daily_checkin', balance=120)

    snapshot = store.snapshot()

    assert snapshot.day == DAY
    assert snapshot.completions == {'daily_checkin': 1}
    assert snapshot.as_dict() == {
        'schema': DAILY_STATE_SCHEMA,
        'date': DAY.isoformat(),
        'completions': {'daily_checkin': 1},
        'last_balance': 120.0,
        'run_count': 1,
        'last_run_at': NOW.isoformat(),
        'completed_night': False,
    }


def test_state_dir_comes_from_the_environment(tmp_path: Path) -> None:
    target = tmp_path / 'from-env'

    store = DailyStateStore.from_environment({'AECOINS_STATE_DIR': str(target)}, clock=_fixed_clock())

    assert store.state_dir == target
    assert target.is_dir()
    assert store.path == target / 'daily-2026-11-21.json'


@pytest.mark.parametrize('environment', [{}, {'AECOINS_STATE_DIR': '   '}])
def test_an_unset_environment_falls_back_to_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment: dict[str, str],
) -> None:
    monkeypatch.chdir(tmp_path)

    store = DailyStateStore.from_environment(environment, clock=_fixed_clock())

    assert store.state_dir == Path('state')
    assert (tmp_path / 'state').is_dir()


def test_an_omitted_environment_reads_os_environ(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / 'os-env'
    monkeypatch.setenv('AECOINS_STATE_DIR', str(target))

    store = DailyStateStore.from_environment(clock=_fixed_clock())

    assert store.state_dir == target
    assert target.is_dir()


def test_the_clock_crossing_midnight_starts_a_new_day(tmp_path: Path) -> None:
    clock = _Clock(datetime(2026, 11, 21, 23, 59))
    store = _store(tmp_path, clock=clock)
    store.record('daily_checkin')
    yesterday = store.path

    clock.value = datetime(2026, 11, 22, 0, 1)

    assert store.path == tmp_path / 'daily-2026-11-22.json'
    assert store.count('daily_checkin') == 0
    assert store.snapshot().day == date(2026, 11, 22)

    store.record('daily_checkin')

    assert store.count('daily_checkin') == 1
    assert yesterday != store.path
    assert json.loads(yesterday.read_text(encoding='utf-8'))['completions'] == {'daily_checkin': 1}
    assert json.loads(store.path.read_text(encoding='utf-8'))['completions'] == {'daily_checkin': 1}


def test_yesterdays_file_is_not_todays_state(tmp_path: Path) -> None:
    old = _store(tmp_path, clock=_Clock(datetime(2026, 11, 20, 23, 30)))
    old.record('daily_checkin')
    old_path = old.path

    today = _store(tmp_path)

    assert today.count('daily_checkin') == 0
    assert today.path != old_path
    assert old_path.exists()
    assert json.loads(old_path.read_text(encoding='utf-8'))['completions'] == {'daily_checkin': 1}


def test_a_corrupt_file_is_quarantined_and_the_day_starts_fresh(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = _path(tmp_path)
    path.write_text('{not json', encoding='utf-8')

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        store = _store(tmp_path)
        assert store.snapshot() == DailySnapshot(day=DAY, completions={})
        assert store.record('daily_checkin') == 1

    quarantined = list(tmp_path.glob('daily-2026-11-21.json.corrupt-*'))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding='utf-8') == '{not json'
    assert json.loads(path.read_text(encoding='utf-8'))['completions'] == {'daily_checkin': 1}

    messages = [json.loads(record.getMessage()) for record in caplog.records if record.name == LOGGER_NAME]
    assert len(messages) == 1
    assert messages[0]['event'] == 'state_corrupt'
    assert messages[0]['path'] == str(path)
    assert messages[0]['quarantined'] == str(quarantined[0])
    assert str(messages[0]['reason']).startswith('not valid UTF-8 JSON')


@pytest.mark.parametrize(
    ('change', 'message'),
    [
        (lambda payload: payload.clear(), "missing field 'schema'"),
        (lambda payload: payload.pop('schema'), "missing field 'schema'"),
        (lambda payload: payload.__setitem__('schema', 99), 'unsupported schema'),
        (lambda payload: payload.__setitem__('schema', True), 'unsupported schema'),
        (lambda payload: payload.pop('date'), "missing field 'date'"),
        (lambda payload: payload.__setitem__('date', '2026-11-20'), 'does not match'),
        (lambda payload: payload.pop('completions'), "missing field 'completions'"),
        (lambda payload: payload.__setitem__('completions', []), "'completions' must be a JSON object"),
        (lambda payload: payload['completions'].__setitem__(' ', 1), 'completion key must be non-empty'),
        (lambda payload: payload['completions'].__setitem__('daily', -1), 'non-negative integer'),
        (lambda payload: payload['completions'].__setitem__('daily', True), 'non-negative integer'),
        (lambda payload: payload['completions'].__setitem__('daily', 1.5), 'non-negative integer'),
        (lambda payload: payload['completions'].__setitem__('daily', '1'), 'non-negative integer'),
        (lambda payload: payload.pop('last_balance'), "missing field 'last_balance'"),
        (lambda payload: payload.__setitem__('last_balance', True), 'finite number or null'),
        (lambda payload: payload.__setitem__('last_balance', '120'), 'finite number or null'),
        (lambda payload: payload.__setitem__('last_balance', float('nan')), 'finite number or null'),
        (lambda payload: payload.pop('run_count'), "missing field 'run_count'"),
        (lambda payload: payload.__setitem__('run_count', -1), 'non-negative integer'),
        (lambda payload: payload.__setitem__('run_count', True), 'non-negative integer'),
        (lambda payload: payload.__setitem__('run_count', 1.5), 'non-negative integer'),
        (lambda payload: payload.pop('last_run_at'), "missing field 'last_run_at'"),
        (lambda payload: payload.__setitem__('last_run_at', 5), 'ISO timestamp or null'),
        (lambda payload: payload.__setitem__('last_run_at', 'yesterday'), 'is not an ISO timestamp'),
        (lambda payload: payload.__setitem__('completed_night', 'yes'), 'must be a boolean'),
    ],
)
def test_a_malformed_state_file_resets_without_raising(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    change: Callable[[dict[str, Any]], object],
    message: str,
) -> None:
    path = _path(tmp_path)
    payload = _valid_payload()
    change(payload)
    _write_json(path, payload)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        store = _store(tmp_path)
        assert store.snapshot() == DailySnapshot(day=DAY, completions={})

    quarantined = list(tmp_path.glob('daily-2026-11-21.json.corrupt-*'))
    assert len(quarantined) == 1
    messages = [json.loads(record.getMessage()) for record in caplog.records if record.name == LOGGER_NAME]
    assert message in messages[0]['reason']


@pytest.mark.parametrize('raw', [b'[]', b'{not json', b'\xff\xfe'])
def test_an_unparseable_file_is_quarantined(tmp_path: Path, raw: bytes) -> None:
    path = _path(tmp_path)
    path.write_bytes(raw)

    store = _store(tmp_path)

    assert store.snapshot() == DailySnapshot(day=DAY, completions={})
    quarantined = list(tmp_path.glob('daily-2026-11-21.json.corrupt-*'))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == raw


def test_a_second_corruption_does_not_overwrite_the_first(tmp_path: Path) -> None:
    path = _path(tmp_path)

    path.write_text('first corruption', encoding='utf-8')
    _store(tmp_path).snapshot()
    path.write_text('second corruption', encoding='utf-8')
    _store(tmp_path).snapshot()

    quarantined = list(tmp_path.glob('daily-2026-11-21.json.corrupt-*'))
    assert len(quarantined) == 2
    assert {item.read_text(encoding='utf-8') for item in quarantined} == {'first corruption', 'second corruption'}


def test_a_failed_quarantine_still_starts_a_fresh_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = _path(tmp_path)
    path.write_text('{not json', encoding='utf-8')

    def broken_replace(_source: object, _target: object) -> None:
        raise OSError('read-only directory')

    monkeypatch.setattr(os, 'replace', broken_replace)

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        store = _store(tmp_path)
        assert store.snapshot() == DailySnapshot(day=DAY, completions={})

    assert path.read_text(encoding='utf-8') == '{not json'
    messages = [json.loads(record.getMessage()) for record in caplog.records if record.name == LOGGER_NAME]
    assert messages[0]['quarantine_error'] == 'read-only directory'


def test_the_injected_logger_receives_the_corruption_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = _path(tmp_path)
    path.write_text('{not json', encoding='utf-8')
    logger = logging.getLogger('tests.runner.state.injected')

    with caplog.at_level(logging.WARNING, logger=logger.name):
        store = DailyStateStore(tmp_path, clock=_fixed_clock(), logger=logger)
        store.snapshot()

    assert any(record.name == logger.name for record in caplog.records)


def test_a_state_file_that_cannot_be_read_is_a_typed_error(tmp_path: Path) -> None:
    _path(tmp_path).mkdir()

    with pytest.raises(StateReadError, match='could not read'):
        _store(tmp_path).snapshot()


def test_a_state_directory_that_cannot_be_created_is_a_typed_error(tmp_path: Path) -> None:
    blocker = tmp_path / 'state'
    blocker.write_text('not a directory', encoding='utf-8')

    with pytest.raises(StateError, match='could not create state directory'):
        DailyStateStore(blocker, clock=_fixed_clock())


def test_a_failed_write_is_typed_and_leaves_the_previous_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.record('daily_checkin')
    before = store.path.read_bytes()

    def broken_replace(_source: object, _target: object) -> None:
        raise OSError('disk full')

    monkeypatch.setattr(os, 'replace', broken_replace)

    with pytest.raises(StateWriteError, match='disk full'):
        store.record('daily_checkin')

    assert store.count('daily_checkin') == 1
    assert store.path.read_bytes() == before
    assert list(tmp_path.glob('.daily-*.json.*')) == []
    assert _store(tmp_path).count('daily_checkin') == 1


def test_a_staging_failure_is_a_typed_state_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)

    def broken_fsync(_fd: int) -> None:
        raise OSError('fsync failed')

    monkeypatch.setattr(os, 'fsync', broken_fsync)

    with pytest.raises(StateWriteError, match='fsync failed'):
        store.record('daily_checkin')

    assert store.count('daily_checkin') == 0
    assert list(tmp_path.iterdir()) == []


def test_a_temp_file_failure_is_a_typed_state_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)

    def broken_mkstemp(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise OSError('no temp files')

    monkeypatch.setattr(tempfile, 'mkstemp', broken_mkstemp)

    with pytest.raises(StateWriteError, match='no temp files'):
        store.record('daily_checkin')

    assert store.count('daily_checkin') == 0
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('task_key', ['', ' ', ' daily_checkin', 'daily_checkin '])
def test_invalid_task_keys_are_refused(tmp_path: Path, task_key: str) -> None:
    store = _store(tmp_path)

    with pytest.raises(StateError, match='task key'):
        store.record(task_key)
    with pytest.raises(StateError, match='task key'):
        store.count(task_key)
    with pytest.raises(StateError, match='task key'):
        store.done_today(task_key)

    assert not store.path.exists()


def test_a_non_string_task_key_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(StateError, match='task key must be a string'):
        store.count(cast(Any, 7))


@pytest.mark.parametrize('balance', [float('nan'), float('inf'), True, '120'])
def test_an_invalid_balance_is_refused(tmp_path: Path, balance: object) -> None:
    store = _store(tmp_path)
    invalid = cast(Any, balance)

    with pytest.raises(StateError, match='finite number'):
        store.record('daily_checkin', balance=invalid)
    with pytest.raises(StateError, match='finite number'):
        store.observe_balance(invalid)

    assert store.count('daily_checkin') == 0
    assert not store.path.exists()
