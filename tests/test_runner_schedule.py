"""The nightly schedule, tested with an injected clock rather than sleeping."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from aliexpress_coins.runner.config import RunConfig
from aliexpress_coins.runner.errors import RunLockHeld, StateError
from aliexpress_coins.runner.lock import RunLock
from aliexpress_coins.runner.report import RunReport, RunStatus
from aliexpress_coins.runner.schedule import nightly_trigger, run_schedule, should_skip_night
from aliexpress_coins.runner.state import DailySnapshot, DailyStateStore

START = datetime(2026, 11, 22, 3, 29, tzinfo=UTC)


def _config(tmp_path: Path, *, jitter: int = 180) -> RunConfig:
    return RunConfig.from_environment(
        {
            'TZ': 'UTC',
            'AECOINS_TIMEZONE': 'UTC',
            'AECOINS_SCHEDULE_TIME': '03:30',
            'AECOINS_SCHEDULE_JITTER_SECONDS': str(jitter),
            'AECOINS_RUNS_DIR': str(tmp_path / 'runs'),
            'AECOINS_STATE_DIR': str(tmp_path / 'state'),
        },
        root=tmp_path,
    )


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _Stop:
    def __init__(self, clock: _Clock, *, stop_after_waits: int = 2, immediate: bool = False) -> None:
        self.clock = clock
        self.stop_after_waits = stop_after_waits
        self.immediate = immediate
        self.waits = 0

    def is_set(self) -> bool:
        return self.immediate

    def wait(self, timeout: float | None = None) -> bool:
        self.waits += 1
        if self.waits >= self.stop_after_waits:
            return True
        self.clock.now += timedelta(seconds=timeout or 0.0)
        return False


class _State:
    def __init__(self, completed_night: bool) -> None:
        self.completed_night = completed_night

    def snapshot(self) -> DailySnapshot:
        return DailySnapshot(day=date(2026, 11, 22), completions={}, completed_night=self.completed_night)


def _monotonic(clock: _Clock) -> Any:
    """A monotonic reading derived from the test clock, so waits move it."""
    return lambda: clock.now.timestamp()


def _store(tmp_path: Path, clock: _Clock) -> DailyStateStore:
    return DailyStateStore(tmp_path / 'state', clock=lambda: clock.now)


class _ReleasingStop(_Stop):
    """A stop flag that releases the other run on the first lock wait."""

    def __init__(self, clock: _Clock, other: RunLock, store: DailyStateStore, *, complete: bool) -> None:
        super().__init__(clock, stop_after_waits=3)
        self.other = other
        self.store = store
        self.complete = complete

    def wait(self, timeout: float | None = None) -> bool:
        stopped = super().wait(timeout)
        if self.waits == 2:
            if self.complete:
                self.store.complete_night()
            self.other.release()
        return stopped


class _StopDuringLockWait:
    """Stops while the daemon is waiting for another run's lock."""

    def __init__(self) -> None:
        self.waits = 0

    def is_set(self) -> bool:
        return self.waits >= 2

    def wait(self, timeout: float | None = None) -> bool:
        del timeout
        self.waits += 1
        return self.is_set()


def test_the_trigger_fires_once_a_night_inside_the_jitter_window(tmp_path: Path) -> None:
    config = _config(tmp_path, jitter=180)
    trigger = nightly_trigger(config)

    fire = trigger.get_next_fire_time(None, START)

    assert fire is not None
    assert fire.date() == date(2026, 11, 22)
    assert datetime(2026, 11, 22, 3, 30, tzinfo=UTC) <= fire <= datetime(2026, 11, 22, 3, 33, tzinfo=UTC)


def test_a_zero_jitter_is_not_passed_to_the_trigger(tmp_path: Path) -> None:
    trigger = nightly_trigger(_config(tmp_path, jitter=0))
    fire = trigger.get_next_fire_time(None, START)
    assert fire == datetime(2026, 11, 22, 3, 30, tzinfo=UTC)


def test_only_a_completed_night_is_skipped() -> None:
    assert should_skip_night(_State(False)) is False
    assert should_skip_night(_State(True)) is True


def test_a_real_daily_store_is_the_skip_source(tmp_path: Path) -> None:
    store = DailyStateStore(tmp_path / 'state', clock=lambda: datetime(2026, 11, 22, 4, 0))

    assert should_skip_night(store) is False
    store.complete_night()
    assert should_skip_night(store) is True


def test_the_loop_runs_one_night_then_stops_taking_the_next_fire_time(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    config = _config(tmp_path, jitter=0)
    clock = _Clock(START)
    stop = _Stop(clock)
    calls: list[str] = []

    def night() -> RunReport:
        calls.append('run')
        report = RunReport(config.runs_dir, 'run-1', budget_seconds=60.0, started_at=clock.now)
        report.finish(RunStatus.COMPLETED, budget_used_seconds=12.0, finished_at=clock.now)
        return report

    assert run_schedule(config, run_night=night, state=_State(False), clock=clock, stop=stop) == 0
    assert calls == ['run']
    assert stop.waits == 2
    assert clock.now == START + timedelta(seconds=60)
    events = caplog.text
    assert 'schedule_start' in events
    assert 'schedule_finished' in events
    assert 'schedule_stopped' in events


def test_a_night_already_in_the_daily_store_is_not_started(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    config = _config(tmp_path)
    clock = _Clock(START)
    stop = _Stop(clock)
    calls: list[str] = []

    def night() -> RunReport:  # pragma: no cover - the assertion is that this is not called
        calls.append('run')
        raise AssertionError('the night should have been skipped')

    assert run_schedule(config, run_night=night, state=_State(True), clock=clock, stop=stop) == 0
    assert calls == []
    assert 'schedule_skipped' in caplog.text
    assert 'already completed today' in caplog.text


def test_a_stop_before_the_first_fire_returns_without_waiting(tmp_path: Path) -> None:
    config = _config(tmp_path)
    clock = _Clock(START)
    stop = _Stop(clock, immediate=True)
    calls: list[str] = []

    def night() -> RunReport:
        calls.append('run')
        raise AssertionError('the loop should not have fired')

    assert run_schedule(config, run_night=night, state=_State(False), clock=clock, stop=stop) == 0
    assert calls == []
    assert stop.waits == 0


def test_a_failed_night_does_not_kill_the_daemon(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    config = _config(tmp_path, jitter=0)
    clock = _Clock(START)
    stop = _Stop(clock, stop_after_waits=3)
    calls: list[int] = []

    def night() -> RunReport:
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise RuntimeError('runs volume full')
        report = RunReport(config.runs_dir, 'run-2', budget_seconds=60.0, started_at=clock.now)
        report.finish(RunStatus.COMPLETED, budget_used_seconds=5.0, finished_at=clock.now)
        return report

    assert run_schedule(config, run_night=night, state=_State(False), clock=clock, stop=stop) == 0
    assert len(calls) == 2
    assert stop.waits == 3
    assert 'schedule_error' in caplog.text
    assert 'runs volume full' in caplog.text
    assert 'schedule_finished' in caplog.text


def test_a_failed_state_check_still_runs_the_night(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    config = _config(tmp_path, jitter=0)
    clock = _Clock(START)
    stop = _Stop(clock)
    calls: list[str] = []

    class BrokenState:
        def snapshot(self) -> DailySnapshot:
            raise OSError('state volume unreadable')

    def night() -> RunReport:
        calls.append('run')
        report = RunReport(config.runs_dir, 'run-3', budget_seconds=60.0, started_at=clock.now)
        report.finish(RunStatus.COMPLETED, budget_used_seconds=1.0, finished_at=clock.now)
        return report

    assert run_schedule(config, run_night=night, state=BrokenState(), clock=clock, stop=stop) == 0
    assert calls == ['run']
    assert 'schedule_error' in caplog.text
    assert 'state volume unreadable' in caplog.text


def test_a_held_run_lock_skips_the_night_with_a_named_reason(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    config = _config(tmp_path, jitter=0)
    clock = _Clock(START)
    stop = _Stop(clock)
    store = _store(tmp_path, clock)
    holder = store.run_lock()
    holder.try_acquire()
    calls: list[str] = []

    def night() -> RunReport:  # pragma: no cover - the assertion is that this is not called
        calls.append('run')
        raise AssertionError('the night should have been skipped')

    try:
        assert (
            run_schedule(
                config,
                run_night=night,
                state=store,
                clock=clock,
                stop=stop,
                lock=store.run_lock(),
                lock_wait_seconds=0,
            )
            == 0
        )
    finally:
        holder.release()

    assert calls == []
    assert 'schedule_skipped' in caplog.text
    assert 'run lock held by another run' in caplog.text


def test_the_daemon_waits_for_a_manual_run_and_then_skips_a_completed_day(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    config = _config(tmp_path, jitter=0)
    clock = _Clock(START)
    store = _store(tmp_path, clock)
    manual = store.run_lock()
    manual.try_acquire()
    stop = _ReleasingStop(clock, manual, store, complete=True)
    calls: list[str] = []

    def night() -> RunReport:  # pragma: no cover - the assertion is that this is not called
        calls.append('run')
        raise AssertionError('the completed night should have been skipped')

    assert (
        run_schedule(
            config,
            run_night=night,
            state=store,
            clock=clock,
            stop=stop,
            lock=store.run_lock(),
            lock_wait_seconds=60,
            monotonic=_monotonic(clock),
        )
        == 0
    )
    assert calls == []
    assert 'schedule_lock_wait' in caplog.text
    assert 'already completed today' in caplog.text


def test_a_budget_stopped_manual_run_leaves_the_night_for_the_daemon(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    config = _config(tmp_path, jitter=0)
    clock = _Clock(START)
    store = _store(tmp_path, clock)
    manual = store.run_lock()
    manual.try_acquire()
    stop = _ReleasingStop(clock, manual, store, complete=False)
    calls: list[str] = []

    def night() -> RunReport:
        calls.append('run')
        report = RunReport(config.runs_dir, 'run-after-manual', budget_seconds=60.0, started_at=clock.now)
        report.finish(RunStatus.COMPLETED, budget_used_seconds=1.0, finished_at=clock.now)
        return report

    assert (
        run_schedule(
            config,
            run_night=night,
            state=store,
            clock=clock,
            stop=stop,
            lock=store.run_lock(),
            lock_wait_seconds=60,
            monotonic=_monotonic(clock),
        )
        == 0
    )
    assert calls == ['run']
    assert 'schedule_start' in caplog.text
    assert 'already completed today' not in caplog.text


def test_a_lock_that_never_releases_is_skipped_after_the_bound(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    config = _config(tmp_path, jitter=0)
    clock = _Clock(START)
    stop = _Stop(clock, stop_after_waits=5)
    store = _store(tmp_path, clock)
    holder = store.run_lock()
    holder.try_acquire()
    calls: list[str] = []

    def night() -> RunReport:  # pragma: no cover - the assertion is that this is not called
        calls.append('run')
        raise AssertionError('the night should have been skipped')

    try:
        assert (
            run_schedule(
                config,
                run_night=night,
                state=store,
                clock=clock,
                stop=stop,
                lock=store.run_lock(),
                lock_wait_seconds=1,
                monotonic=_monotonic(clock),
            )
            == 0
        )
    finally:
        holder.release()

    assert calls == []
    assert 'run lock held by another run' in caplog.text


def test_a_stop_while_waiting_for_the_lock_ends_the_daemon_without_a_night(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, jitter=0)
    clock = _Clock(START)
    stop = _StopDuringLockWait()
    store = DailyStateStore(tmp_path / 'state', clock=lambda: clock.now)
    holder = store.run_lock()
    holder.try_acquire()
    calls: list[str] = []

    def night() -> RunReport:  # pragma: no cover - the assertion is that this is not called
        calls.append('run')
        raise AssertionError('stop was requested while waiting for the lock')

    try:
        assert (
            run_schedule(
                config,
                run_night=night,
                state=store,
                clock=clock,
                stop=stop,
                lock=store.run_lock(),
                lock_wait_seconds=60,
                monotonic=_monotonic(clock),
            )
            == 0
        )
    finally:
        holder.release()

    assert calls == []


def test_a_stop_between_the_outer_check_and_the_lock_attempt_ends_the_daemon(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, jitter=0)
    clock = _Clock(START)
    stop = _Stop(clock)
    calls: list[str] = []

    class StopSettingLock:
        def try_acquire(self) -> None:
            stop.immediate = True
            raise RunLockHeld('another run holds the state directory')

        def release(self) -> None:  # pragma: no cover - never acquired
            raise AssertionError('release should not be called')

    def night() -> RunReport:  # pragma: no cover - the assertion is that this is not called
        calls.append('run')
        raise AssertionError('stop was requested before the lock was taken')

    assert (
        run_schedule(
            config,
            run_night=night,
            state=_State(False),
            clock=clock,
            stop=stop,
            lock=cast(RunLock, StopSettingLock()),
            lock_wait_seconds=60,
            monotonic=_monotonic(clock),
        )
        == 0
    )
    assert calls == []


def test_a_lock_error_does_not_start_a_second_run(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    config = _config(tmp_path, jitter=0)
    clock = _Clock(START)
    stop = _Stop(clock)
    calls: list[str] = []

    class BrokenLock:
        def try_acquire(self) -> None:
            raise StateError('lock volume unreadable')

        def release(self) -> None:  # pragma: no cover - never acquired
            raise AssertionError('release should not be called')

    def night() -> RunReport:  # pragma: no cover - the assertion is that this is not called
        calls.append('run')
        raise AssertionError('the night should have been skipped')

    assert (
        run_schedule(
            config,
            run_night=night,
            state=_State(False),
            clock=clock,
            stop=stop,
            lock=cast(RunLock, BrokenLock()),
            lock_wait_seconds=0,
        )
        == 0
    )
    assert calls == []
    assert 'schedule_error' in caplog.text
    assert 'lock volume unreadable' in caplog.text
