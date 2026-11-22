"""The nightly schedule, tested with an injected clock rather than sleeping."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from aliexpress_coins.runner.config import RunConfig
from aliexpress_coins.runner.report import RunReport, RunStatus
from aliexpress_coins.runner.schedule import nightly_trigger, run_schedule, should_skip_night
from aliexpress_coins.runner.state import DailySnapshot

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
    def __init__(self, run_count: int) -> None:
        self.run_count = run_count

    def snapshot(self) -> DailySnapshot:
        return DailySnapshot(day=date(2026, 11, 22), completions={}, run_count=self.run_count)


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


def test_a_night_that_already_ran_is_skipped() -> None:
    assert should_skip_night(_State(0)) is False
    assert should_skip_night(_State(1)) is True


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

    assert run_schedule(config, run_night=night, state=_State(0), clock=clock, stop=stop) == 0
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

    assert run_schedule(config, run_night=night, state=_State(1), clock=clock, stop=stop) == 0
    assert calls == []
    assert 'schedule_skipped' in caplog.text
    assert 'already ran today' in caplog.text


def test_a_stop_before_the_first_fire_returns_without_waiting(tmp_path: Path) -> None:
    config = _config(tmp_path)
    clock = _Clock(START)
    stop = _Stop(clock, immediate=True)
    calls: list[str] = []

    def night() -> RunReport:
        calls.append('run')
        raise AssertionError('the loop should not have fired')

    assert run_schedule(config, run_night=night, state=_State(0), clock=clock, stop=stop) == 0
    assert calls == []
    assert stop.waits == 0
