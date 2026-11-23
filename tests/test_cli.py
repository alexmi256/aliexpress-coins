"""Tests for the command-line entry point (issue #34)."""

from __future__ import annotations

import importlib
import runpy
import signal
import sys
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

import pytest

import aliexpress_coins
from aliexpress_coins import cli
from aliexpress_coins.runner.errors import ConfigError, RunLockHeld
from aliexpress_coins.runner.lock import RunLock
from aliexpress_coins.runner.report import RunReport, RunStatus
from aliexpress_coins.runner.state import DailyStateStore

Handler = Callable[[int, FrameType | None], None]


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every manual run's state directory, and its lock, out of the repo."""
    monkeypatch.chdir(tmp_path)


def _report(tmp_path: Path, status: RunStatus = RunStatus.COMPLETED) -> RunReport:
    report = RunReport(
        tmp_path,
        'run-1',
        budget_seconds=60.0,
        started_at=datetime(2026, 11, 22, 3, 30, tzinfo=UTC),
    )
    report.finish(status, budget_used_seconds=1.0)
    return report


def test_main_dispatches_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(['version']) == 0
    assert capsys.readouterr().out.strip() == aliexpress_coins.__version__


def test_main_dispatches_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_doctor(_environment: Mapping[str, str]) -> int:
        return 7

    monkeypatch.setattr(cli, 'run_doctor', fake_doctor)
    assert cli.main(['doctor']) == 7


def test_main_dispatches_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, 'run_idle', lambda: 0)
    assert cli.main(['idle']) == 0


def test_main_without_a_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert 'usage:' in capsys.readouterr().out


def test_module_entrypoint_runs_main(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, 'argv', ['aliexpress-coins'])
    with pytest.raises(SystemExit) as raised:
        runpy.run_module('aliexpress_coins', run_name='__main__')
    assert raised.value.code == 2
    assert 'usage:' in capsys.readouterr().out


def test_importing_the_entrypoint_does_not_run_the_cli() -> None:
    module = importlib.import_module('aliexpress_coins.__main__')
    assert module.__name__ == 'aliexpress_coins.__main__'


def test_run_idle_stops_on_sigterm(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handlers: dict[int, Handler] = {}

    def fake_signal(signum: int, handler: Handler) -> None:
        handlers[signum] = handler
        if signum == signal.SIGTERM:
            handler(signum, None)

    monkeypatch.setattr(signal, 'signal', fake_signal)
    assert cli.run_idle() == 0
    assert set(handlers) == {signal.SIGTERM, signal.SIGINT}
    output = capsys.readouterr().out
    assert 'collector idle' in output
    assert 'collector stopped' in output


def test_main_dispatches_a_manual_run(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[bool, bool]] = []

    def fake(environment: Mapping[str, str], *, dry_run: bool = False, root: str | None = None) -> int:
        del environment, root
        seen.append((True, dry_run))
        return 9

    monkeypatch.setattr(cli, 'run_once_command', fake)
    assert cli.main(['run', '--once', '--dry-run']) == 9
    assert seen == [(True, True)]


def test_main_dispatches_the_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []

    def fake(environment: Mapping[str, str], *, dry_run: bool = False, root: str | None = None) -> int:
        del environment, root
        seen.append(dry_run)
        return 8

    monkeypatch.setattr(cli, 'run_schedule_command', fake)
    assert cli.main(['run', '--schedule']) == 8
    assert seen == [False]


def test_run_once_without_a_device_is_a_configuration_failure(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / 'state'
    assert (
        cli.run_once_command(
            {'TZ': 'UTC', 'AECOINS_STATE_DIR': str(state_dir)},
            root=str(tmp_path),
        )
        == 2
    )
    output = capsys.readouterr()
    assert 'AECOINS_DEVICE is not set' in output.err
    assert not state_dir.exists()


def test_an_invalid_manual_configuration_returns_two(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / 'state'
    assert (
        cli.run_once_command(
            {
                'AECOINS_DEVICE': 'tablet',
                'TZ': 'UTC',
                'AECOINS_RUN_BUDGET_SECONDS': '0',
                'AECOINS_STATE_DIR': str(state_dir),
            },
            root=str(tmp_path),
        )
        == 2
    )
    assert 'configuration: AECOINS_RUN_BUDGET_SECONDS must be positive' in capsys.readouterr().err
    assert not state_dir.exists()


def test_a_manual_run_refuses_when_another_run_holds_the_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir = tmp_path / 'state'
    holder = DailyStateStore(state_dir, clock=lambda: datetime(2026, 11, 22, 3, 30)).run_lock()
    holder.try_acquire()
    built: list[str] = []

    def fake_context(*_args: object, **_kwargs: object) -> object:  # pragma: no cover - must not run
        built.append('context')
        return object()

    def unreachable_timezone(_serial: str) -> str:  # pragma: no cover - must not run
        raise AssertionError('the refused run must not probe the device')

    monkeypatch.setattr(cli, 'make_live_context', fake_context)

    try:
        result = cli.run_once_command(
            {
                'AECOINS_DEVICE': 'tablet',
                'TZ': 'UTC',
                'AECOINS_STATE_DIR': str(state_dir),
            },
            root=str(tmp_path),
            device_timezone=unreachable_timezone,
        )
    finally:
        holder.release()

    assert result == cli.RUN_LOCK_HELD_EXIT == 3
    assert built == []
    error = capsys.readouterr().err
    assert 'another run holds' in error
    assert 'did not touch the device' in error


def test_a_manual_run_holds_the_lock_for_the_whole_run_and_releases_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir = tmp_path / 'state'
    report = _report(tmp_path)
    seen: list[str] = []

    def fake_run_once(_context: object) -> RunReport:
        contender = DailyStateStore(state_dir, clock=lambda: datetime(2026, 11, 22, 3, 30)).run_lock()
        with pytest.raises(RunLockHeld):
            contender.try_acquire()
        seen.append('ran')
        return report

    monkeypatch.setattr(cli, 'make_live_context', lambda config, stop: object())
    monkeypatch.setattr(cli, 'run_once', fake_run_once)
    monkeypatch.setattr(cli, '_install_stop_handlers', lambda stop: None)
    monkeypatch.setattr(cli, '_restore_stop_handlers', lambda: None)

    assert (
        cli.run_once_command(
            {'AECOINS_DEVICE': 'tablet', 'TZ': 'UTC', 'AECOINS_STATE_DIR': str(state_dir)},
            root=str(tmp_path),
            device_timezone=lambda _serial: 'UTC',
        )
        == 0
    )

    assert seen == ['ran']
    assert 'run run-1: completed' in capsys.readouterr().out
    after = DailyStateStore(state_dir, clock=lambda: datetime(2026, 11, 22, 3, 30)).run_lock()
    after.try_acquire()
    after.release()


def test_run_once_prints_the_report_and_returns_its_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _report(tmp_path)
    monkeypatch.setattr(cli, 'make_live_context', lambda config, stop: object())
    monkeypatch.setattr(cli, 'run_once', lambda context: report)
    monkeypatch.setattr(cli, '_install_stop_handlers', lambda stop: None)
    monkeypatch.setattr(cli, '_restore_stop_handlers', lambda: None)

    assert (
        cli.run_once_command(
            {'AECOINS_DEVICE': 'tablet', 'TZ': 'UTC'},
            root=str(tmp_path),
            device_timezone=lambda _serial: 'UTC',
        )
        == 0
    )
    output = capsys.readouterr().out
    assert 'run run-1: completed' in output
    assert f'summary: {report.summary_path}' in output


def test_a_dry_run_plan_and_an_abort_are_printed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _report(tmp_path, RunStatus.PLANNED)
    report.plan = {
        'surface': 'rows_present',
        'steps': [
            {'order': 1, 'label': 'Take the coins quiz', 'key': 'coins_quiz', 'category': 'quiz', 'rule': 'coins_quiz'},
        ],
        'refusals': [
            {
                'label': 'Play & Earn',
                'code': 'game_play_and_earn',
                'rule': 'game_play_and_earn',
                'reason': 'game: Play & Earn is a mini-game and is never played',
            },
        ],
        'taps_issued': 0,
        'detail': 'dry run: 1 task(s) planned, 1 refusal(s); no tap was issued',
    }
    report.plan_note = 'read from the visible surface'
    report.abort = {
        'code': 'dangerous_control',
        'reason': "a 'Buy now' control appeared",
        'screen': {'state': 'unknown', 'surface': 'unknown', 'labels': ['Buy now']},
    }
    monkeypatch.setattr(cli, 'make_live_context', lambda config, stop: object())
    monkeypatch.setattr(cli, 'run_once', lambda context: report)
    monkeypatch.setattr(cli, '_install_stop_handlers', lambda stop: None)
    monkeypatch.setattr(cli, '_restore_stop_handlers', lambda: None)

    cli.run_once_command(
        {'AECOINS_DEVICE': 'tablet', 'TZ': 'UTC'},
        dry_run=True,
        root=str(tmp_path),
        device_timezone=lambda _serial: 'UTC',
    )

    output = capsys.readouterr().out
    assert 'plan: dry run: 1 task(s) planned' in output
    assert '1. Take the coins quiz [coins_quiz]' in output
    assert 'refused: Play & Earn code=game_play_and_earn' in output
    assert 'taps issued: 0' in output
    assert 'plan note: read from the visible surface' in output
    assert 'abort: dangerous_control' in output
    assert "screen: unknown/unknown labels=['Buy now']" in output


def test_a_failed_night_returns_non_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    report = _report(tmp_path, RunStatus.FAILED)
    monkeypatch.setattr(cli, 'make_live_context', lambda config, stop: object())
    monkeypatch.setattr(cli, 'run_once', lambda context: report)
    monkeypatch.setattr(cli, '_install_stop_handlers', lambda stop: None)
    monkeypatch.setattr(cli, '_restore_stop_handlers', lambda: None)

    assert (
        cli.run_once_command(
            {'AECOINS_DEVICE': 'tablet', 'TZ': 'UTC'},
            root=str(tmp_path),
            device_timezone=lambda _serial: 'UTC',
        )
        == 1
    )


def test_an_unreadable_dry_run_prints_the_named_reason_and_returns_non_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _report(tmp_path, RunStatus.FAILED)
    report.detail = 'surface_unknown: the coin page did not render (states=loading)'
    report.plan = {
        'surface': 'unknown',
        'steps': [],
        'refusals': [],
        'taps_issued': 0,
        'detail': 'dry run: surface_unknown: the coin page did not render; no tap was issued',
    }
    monkeypatch.setattr(cli, 'make_live_context', lambda config, stop: object())
    monkeypatch.setattr(cli, 'run_once', lambda context: report)
    monkeypatch.setattr(cli, '_install_stop_handlers', lambda stop: None)
    monkeypatch.setattr(cli, '_restore_stop_handlers', lambda: None)

    assert (
        cli.run_once_command(
            {'AECOINS_DEVICE': 'tablet', 'TZ': 'UTC'},
            dry_run=True,
            root=str(tmp_path),
            device_timezone=lambda _serial: 'UTC',
        )
        == 1
    )
    output = capsys.readouterr().out
    assert 'run run-1: failed' in output
    assert 'surface_unknown: the coin page did not render' in output
    assert 'plan: dry run: surface_unknown' in output
    assert 'taps issued: 0' in output


def test_a_runner_error_is_reported_and_returns_non_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def broken(config: Any, stop: Any) -> None:
        raise OSError('no adb')

    monkeypatch.setattr(cli, 'make_live_context', broken)
    monkeypatch.setattr(cli, '_install_stop_handlers', lambda stop: None)
    monkeypatch.setattr(cli, '_restore_stop_handlers', lambda: None)

    assert (
        cli.run_once_command(
            {'AECOINS_DEVICE': 'tablet', 'TZ': 'UTC'},
            device_timezone=lambda _serial: 'UTC',
        )
        == 1
    )
    assert 'OSError: no adb' in capsys.readouterr().err


def test_the_schedule_command_builds_a_night_from_the_same_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    report = _report(tmp_path)
    built: list[object] = []

    def fake_live(config: Any, stop: Any, wall_clock: Any) -> object:
        built.append(config)
        return object()

    monkeypatch.setattr(cli, 'make_live_context', fake_live)
    monkeypatch.setattr(cli, 'run_once', lambda context: report)
    monkeypatch.setattr(cli, '_install_stop_handlers', lambda stop: None)
    monkeypatch.setattr(cli, '_restore_stop_handlers', lambda: None)

    def fake_schedule(config: Any, *, run_night: Any, state: Any, clock: Any, stop: Any, lock: Any) -> int:
        assert state.snapshot().run_count == 0
        assert isinstance(lock, RunLock)
        result = run_night()
        assert result is report
        assert clock() is not None
        assert stop is not None
        return 0

    monkeypatch.setattr(cli, 'run_schedule', fake_schedule)

    assert (
        cli.run_schedule_command(
            {
                'AECOINS_DEVICE': 'tablet',
                'TZ': 'UTC',
                'AECOINS_RUNS_DIR': str(tmp_path / 'runs'),
                'AECOINS_STATE_DIR': str(tmp_path / 'state'),
            },
            root=str(tmp_path),
            device_timezone=lambda _serial: 'UTC',
        )
        == 0
    )
    assert len(built) == 1


def test_schedule_configuration_failure_is_non_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.run_schedule_command({'TZ': 'UTC'}) == 2
    assert 'AECOINS_DEVICE is not set' in capsys.readouterr().err


def test_the_scheduler_refuses_a_timezone_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    def fake_schedule(*args: Any, **kwargs: Any) -> int:
        calls.append('run')
        return 0

    monkeypatch.setattr(cli, 'run_schedule', fake_schedule)

    result = cli.run_schedule_command(
        {'AECOINS_DEVICE': 'tablet', 'AECOINS_TIMEZONE': 'UTC'},
        device_timezone=lambda _serial: 'America/Toronto',
    )

    assert result == 2
    assert calls == []
    error = capsys.readouterr().err
    assert 'refusing to schedule' in error
    assert 'configured timezone UTC' in error
    assert 'device timezone America/Toronto' in error


def test_the_scheduler_keeps_running_when_the_startup_zone_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    def fake_schedule(*args: Any, **kwargs: Any) -> int:
        calls.append('run')
        return 0

    monkeypatch.setattr(cli, 'run_schedule', fake_schedule)

    result = cli.run_schedule_command(
        {'AECOINS_DEVICE': 'tablet', 'AECOINS_TIMEZONE': 'UTC'},
        device_timezone=lambda _serial: None,
    )

    assert result == 0
    assert calls == ['run']
    error = capsys.readouterr().err
    assert 'warning: timezone unverified' in error
    assert 'configured timezone UTC' in error
    assert 'could not be read' in error
    assert 'Keeping the schedule alive' in error


def test_the_scheduler_checks_the_zone_again_before_the_night(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refusals: list[str] = []

    def fake_schedule(config: Any, *, run_night: Any, state: Any, clock: Any, stop: Any, lock: Any) -> int:
        del config, state, clock, stop, lock
        with pytest.raises(ConfigError) as raised:
            run_night()
        refusals.append(str(raised.value))
        return 0

    monkeypatch.setattr(cli, 'run_schedule', fake_schedule)
    zones = iter(('UTC', 'America/Toronto'))

    result = cli.run_schedule_command(
        {'AECOINS_DEVICE': 'tablet', 'AECOINS_TIMEZONE': 'UTC'},
        device_timezone=lambda _serial: next(zones),
    )

    assert result == 0
    assert len(refusals) == 1
    assert 'refusing to schedule' in refusals[0]
    assert 'configured timezone UTC' in refusals[0]
    assert 'device timezone America/Toronto' in refusals[0]


def test_the_pre_night_check_bounds_the_default_reader(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    report = _report(tmp_path)
    seen: list[float] = []

    def fake_reader(serial: str, *, timeout: float = 10.0) -> str | None:
        del serial
        seen.append(timeout)
        return 'UTC'

    monkeypatch.setattr(cli, 'read_device_timezone', fake_reader)
    monkeypatch.setattr(cli, 'make_live_context', lambda config, stop, wall_clock: object())
    monkeypatch.setattr(cli, 'run_once', lambda context: report)
    monkeypatch.setattr(cli, '_install_stop_handlers', lambda stop: None)
    monkeypatch.setattr(cli, '_restore_stop_handlers', lambda: None)

    def fake_schedule(config: Any, *, run_night: Any, state: Any, clock: Any, stop: Any, lock: Any) -> int:
        del config, state, clock, stop, lock
        assert run_night() is report
        return 0

    monkeypatch.setattr(cli, 'run_schedule', fake_schedule)

    assert cli.run_schedule_command({'AECOINS_DEVICE': 'tablet', 'AECOINS_TIMEZONE': 'UTC'}) == 0
    assert seen == [10.0, 2.0]


def test_a_manual_run_warns_but_continues_on_a_timezone_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _report(tmp_path)
    monkeypatch.setattr(cli, 'make_live_context', lambda config, stop: object())
    monkeypatch.setattr(cli, 'run_once', lambda context: report)
    monkeypatch.setattr(cli, '_install_stop_handlers', lambda stop: None)
    monkeypatch.setattr(cli, '_restore_stop_handlers', lambda: None)

    assert (
        cli.run_once_command(
            {'AECOINS_DEVICE': 'tablet', 'AECOINS_TIMEZONE': 'UTC'},
            root=str(tmp_path),
            device_timezone=lambda _serial: 'America/Toronto',
        )
        == 0
    )
    output = capsys.readouterr()
    assert 'warning: timezone mismatch' in output.err
    assert 'configured timezone UTC' in output.err
    assert 'device timezone America/Toronto' in output.err
    assert 'run run-1: completed' in output.out


def test_run_once_honours_the_dry_run_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    report = _report(tmp_path)
    seen: list[bool] = []

    def fake_context(config: Any, stop: Any) -> object:
        seen.append(config.dry_run)
        return object()

    monkeypatch.setattr(cli, 'make_live_context', fake_context)
    monkeypatch.setattr(cli, 'run_once', lambda context: report)
    monkeypatch.setattr(cli, '_install_stop_handlers', lambda stop: None)
    monkeypatch.setattr(cli, '_restore_stop_handlers', lambda: None)

    assert (
        cli.run_once_command(
            {'AECOINS_DEVICE': 'tablet', 'TZ': 'UTC'},
            dry_run=True,
            root=str(tmp_path),
            device_timezone=lambda _serial: 'UTC',
        )
        == 0
    )
    assert seen == [True]


def test_a_report_detail_is_printed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = _report(tmp_path)
    report.detail = 'a useful detail'
    monkeypatch.setattr(cli, 'make_live_context', lambda config, stop: object())
    monkeypatch.setattr(cli, 'run_once', lambda context: report)
    monkeypatch.setattr(cli, '_install_stop_handlers', lambda stop: None)
    monkeypatch.setattr(cli, '_restore_stop_handlers', lambda: None)

    assert (
        cli.run_once_command(
            {'AECOINS_DEVICE': 'tablet', 'TZ': 'UTC'},
            root=str(tmp_path),
            device_timezone=lambda _serial: 'UTC',
        )
        == 0
    )
    assert 'detail: a useful detail' in capsys.readouterr().out


def test_a_schedule_failure_is_reported_and_returns_non_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, 'make_live_context', lambda config, stop, wall_clock: object())
    monkeypatch.setattr(cli, '_install_stop_handlers', lambda stop: None)
    monkeypatch.setattr(cli, '_restore_stop_handlers', lambda: None)
    monkeypatch.setattr(
        cli,
        'run_schedule',
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError('scheduler broke')),
    )

    assert (
        cli.run_schedule_command(
            {'AECOINS_DEVICE': 'tablet', 'TZ': 'UTC'},
            device_timezone=lambda _serial: 'UTC',
        )
        == 1
    )
    assert 'OSError: scheduler broke' in capsys.readouterr().err


def test_the_stop_handlers_set_the_event_and_restore_the_old_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: dict[int, Handler] = {}
    restored: dict[int, Any] = {}
    previous = object()

    monkeypatch.setattr(signal, 'getsignal', lambda name: previous)
    monkeypatch.setattr(signal, 'signal', lambda name, handler: installed.__setitem__(name, handler))
    stop = __import__('threading').Event()

    cli._install_stop_handlers(stop)
    assert set(installed) == {signal.SIGTERM, signal.SIGINT}
    installed[signal.SIGTERM](signal.SIGTERM, None)
    assert stop.is_set() is True

    monkeypatch.setattr(signal, 'signal', lambda name, handler: restored.__setitem__(name, handler))
    cli._restore_stop_handlers()
    assert restored == {signal.SIGTERM: previous, signal.SIGINT: previous}
    assert cli._HANDLERS == {}


def test_idle_waits_until_the_event_is_set(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeEvent:
        def __init__(self) -> None:
            self.calls = 0

        def wait(self, timeout: float) -> bool:
            assert timeout == 1.0
            self.calls += 1
            return self.calls > 1

        def set(self) -> None:
            raise AssertionError('the test does not send a signal')

    monkeypatch.setattr(threading, 'Event', FakeEvent)
    monkeypatch.setattr(signal, 'signal', lambda name, handler: None)

    assert cli.run_idle() == 0
    assert 'collector stopped' in capsys.readouterr().out
