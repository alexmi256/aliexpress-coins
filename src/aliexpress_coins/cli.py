"""Command-line entry point for the collector (issues #29 and #34)."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from types import FrameType
from typing import Any

from aliexpress_coins import __version__
from aliexpress_coins.doctor import run_doctor
from aliexpress_coins.runner.config import RunConfig
from aliexpress_coins.runner.engine import make_live_context, run_once
from aliexpress_coins.runner.errors import ConfigError
from aliexpress_coins.runner.report import RunReport
from aliexpress_coins.runner.schedule import run_schedule
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.runner.timezone import (
    DeviceTimezoneReader,
    TimezoneCheck,
    timezone_check_from_environment,
)

LOGGER_NAME = 'aliexpress_coins.cli'


def run_idle() -> int:
    """Block until SIGTERM or SIGINT.

    This remains the container smoke-test command; the nightly entry point is
    ``run --schedule``. The signal handling is the same real behaviour a
    ``docker stop`` relies on.
    """
    stop = threading.Event()

    def _request_stop(signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    print('collector idle: send SIGTERM to stop', flush=True)
    while not stop.wait(1.0):
        pass
    print('collector stopped', flush=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line and dispatch one subcommand."""
    _configure_logging()
    parser = argparse.ArgumentParser(prog='aliexpress-coins')
    subparsers = parser.add_subparsers(dest='command')
    subparsers.add_parser('version', help='print the installed version')
    subparsers.add_parser('doctor', help='run read-only pre-flight checks')
    subparsers.add_parser('idle', help='block until SIGTERM (container smoke test)')
    run_parser = subparsers.add_parser('run', help='run one night, or run the nightly daemon')
    group = run_parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--once', action='store_true', help='perform exactly one night and exit')
    group.add_argument('--schedule', action='store_true', help='wait for the configured nightly time')
    run_parser.add_argument('--dry-run', action='store_true', help='plan taps without sending them to the device')
    args = parser.parse_args(argv)

    if args.command == 'version':
        print(__version__)
        return 0
    if args.command == 'doctor':
        return run_doctor(os.environ)
    if args.command == 'idle':
        return run_idle()
    if args.command == 'run':
        if args.once:
            return run_once_command(os.environ, dry_run=args.dry_run)
        return run_schedule_command(os.environ, dry_run=args.dry_run)

    parser.print_help()
    return 2


def run_once_command(
    environment: Mapping[str, str] | None = None,
    *,
    dry_run: bool = False,
    root: str | None = None,
    device_timezone: DeviceTimezoneReader | None = None,
) -> int:
    """Perform one night and return the process exit code.

    A manual run is supervised, so a timezone disagreement is a loud warning
    rather than a refusal: the operator may deliberately be taking one night
    from another zone. The scheduler, which is unattended, refuses instead.
    """
    try:
        config = _config(environment, dry_run=dry_run, root=root)
        _warn_timezone(_timezone_check(environment, config.device, device_timezone))
        stop = threading.Event()
        _install_stop_handlers(stop)
        try:
            context = make_live_context(config, stop=stop)
            report = run_once(context)
        finally:
            _restore_stop_handlers()
    except ConfigError as exc:
        _error(f'configuration: {exc}')
        return 2
    except Exception as exc:
        _error(f'{type(exc).__name__}: {exc}')
        return 1
    _print_report(report)
    return 0 if report.ok else 1


def run_schedule_command(
    environment: Mapping[str, str] | None = None,
    *,
    dry_run: bool = False,
    root: str | None = None,
    device_timezone: DeviceTimezoneReader | None = None,
) -> int:
    """Run the nightly daemon until SIGTERM/SIGINT, then return its exit code."""
    try:
        config = _config(environment, dry_run=dry_run, root=root)
        if config.device is None:
            raise ConfigError('AECOINS_DEVICE is not set; the scheduler needs a device serial')
        startup_check = _timezone_check(environment, config.device, device_timezone)
        if startup_check is not None and startup_check.status == 'unreadable':
            _warning(
                f'timezone unverified: {startup_check.message()}. Keeping the schedule alive; '
                'the check runs again before each night.',
            )
        else:
            _refuse_bad_timezone(startup_check)
        stop = threading.Event()
        _install_stop_handlers(stop)

        def wall_clock() -> datetime:
            return datetime.now(UTC)

        state = DailyStateStore(
            config.state_dir,
            clock=lambda: wall_clock().astimezone(config.timezone),
        )

        def night() -> RunReport:
            # A transient adb outage at startup must not cost the night. This
            # second check is the fail-closed one: it runs immediately before
            # the walk, so a mismatch or an unreadable zone refuses that night
            # with a clear reason while the daemon survives for tomorrow.
            _refuse_bad_timezone(_timezone_check(environment, config.device, device_timezone))
            context = make_live_context(config, stop=stop, wall_clock=wall_clock)
            return run_once(context)

        try:
            return run_schedule(
                config,
                run_night=night,
                state=state,
                clock=wall_clock,
                stop=stop,
            )
        finally:
            _restore_stop_handlers()
    except ConfigError as exc:
        _error(f'configuration: {exc}')
        return 2
    except Exception as exc:
        _error(f'{type(exc).__name__}: {exc}')
        return 1


def _configure_logging() -> None:
    """Send the runner's structured events to the container's log stream."""
    level = os.environ.get('AECOINS_LOG_LEVEL', 'INFO').strip().upper() or 'INFO'
    logging.basicConfig(
        level=level,
        format='%(asctime)s %(levelname)s %(message)s',
    )


def _config(
    environment: Mapping[str, str] | None,
    *,
    dry_run: bool,
    root: str | None,
) -> RunConfig:
    config = RunConfig.from_environment(environment, root=root)
    if dry_run:
        config = replace(config, dry_run=True)
    return config


def _timezone_check(
    environment: Mapping[str, str] | None,
    device: str | None,
    reader: DeviceTimezoneReader | None,
) -> TimezoneCheck | None:
    env = os.environ if environment is None else environment
    return timezone_check_from_environment(env, device, reader=reader)


def _warn_timezone(check: TimezoneCheck | None) -> None:
    if check is None or check.matches:
        return
    subject = 'timezone unverified' if check.status == 'unreadable' else 'timezone mismatch'
    _warning(
        f'{subject}: {check.message()}. Set AECOINS_TIMEZONE to the device zone '
        '(`adb -s <serial> shell getprop persist.sys.timezone`) before the unattended schedule.',
    )


def _refuse_bad_timezone(check: TimezoneCheck | None) -> None:
    if check is None or check.matches:
        return
    raise ConfigError(
        f'refusing to schedule: {check.message()}. '
        'Set AECOINS_TIMEZONE to the device zone (`adb -s <serial> shell getprop persist.sys.timezone`).',
    )


_HANDLERS: dict[int, Any] = {}


def _install_stop_handlers(stop: threading.Event) -> None:
    def _request_stop(signum: int, _frame: FrameType | None) -> None:
        stop.set()

    for name in (signal.SIGTERM, signal.SIGINT):
        _HANDLERS[name] = signal.getsignal(name)
        signal.signal(name, _request_stop)


def _restore_stop_handlers() -> None:
    for name, previous in _HANDLERS.items():
        signal.signal(name, previous)
    _HANDLERS.clear()


def _print_report(report: RunReport) -> None:
    print(f'run {report.run_id}: {report.status.value}', flush=True)
    if report.detail:
        print(f'detail: {report.detail}', flush=True)
    if report.plan is not None:
        _print_plan(report.plan)
    if report.plan_note:
        print(f'plan note: {report.plan_note}', flush=True)
    if report.abort is not None:
        _print_abort(report.abort)
    print(f'summary: {report.summary_path}', flush=True)
    print(f'run.json: {report.json_path}', flush=True)


def _print_plan(plan: dict[str, Any]) -> None:
    print(f'plan: {plan.get("detail", "dry run")}', flush=True)
    for step in plan.get('steps') or []:
        print(
            f'  {step.get("order")}. {step.get("label")} [{step.get("key")}] '
            f'category={step.get("category")} rule={step.get("rule")}',
            flush=True,
        )
    for refusal in plan.get('refusals') or []:
        print(
            f'  refused: {refusal.get("label")} code={refusal.get("code")} '
            f'rule={refusal.get("rule")} reason={refusal.get("reason")}',
            flush=True,
        )
    print(f'  taps issued: {plan.get("taps_issued", 0)}', flush=True)


def _print_abort(abort: dict[str, Any]) -> None:
    screen = abort.get('screen') or {}
    print(
        f'abort: {abort.get("code", "unknown")} — {abort.get("reason", "no reason recorded")}',
        flush=True,
    )
    print(
        f'  screen: {screen.get("state", "?")}/{screen.get("surface", "?")} labels={screen.get("labels", [])}',
        flush=True,
    )


def _error(message: str) -> None:
    logging.getLogger(LOGGER_NAME).error('%s', message)
    print(f'error: {message}', file=sys.stderr, flush=True)


def _warning(message: str) -> None:
    logging.getLogger(LOGGER_NAME).warning('%s', message)
    print(f'warning: {message}', file=sys.stderr, flush=True)


__all__ = [
    'main',
    'run_idle',
    'run_once_command',
    'run_schedule_command',
]
