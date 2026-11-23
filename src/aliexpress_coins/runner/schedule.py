"""The nightly schedule: one CronTrigger, a jittered start and a clean stop.

APScheduler's :class:`~apscheduler.triggers.cron.CronTrigger` supplies the
calendar arithmetic (local wall clock, DST transitions) and the jitter. This
module deliberately does not use a ``BlockingScheduler``: the process must run
one night's engine synchronously and react to SIGTERM inside it, so a small loop
that asks the trigger for the next fire time is easier to test and to stop than
a scheduler thread.

A night already completed in the daily store is skipped. That is what makes
a manual ``run --once`` and the daemon coexist: the daemon does not start a
second walk over the same day's state.

The same store also owns the exclusive run lock (issue #78). A fire that finds
another run holding it waits up to the run budget plus a grace period, then
re-checks ``completed_night`` after taking the lock: a manual run that finished
its roster makes the night already complete, while one stopped by its budget
leaves it open for the daemon. If the wait expires, the night is skipped with
the named ``run lock held`` reason rather than starting a second adb client.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Literal, Protocol

from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from aliexpress_coins.runner.config import RunConfig
from aliexpress_coins.runner.errors import RunLockHeld
from aliexpress_coins.runner.lock import RunLock
from aliexpress_coins.runner.report import RunReport
from aliexpress_coins.runner.state import DailySnapshot

__all__ = [
    'LOCK_POLL_SECONDS',
    'LOGGER_NAME',
    'StopFlag',
    'nightly_trigger',
    'run_schedule',
    'should_skip_night',
]

LOGGER_NAME = 'aliexpress_coins.runner.schedule'

LOCK_POLL_SECONDS = 0.5
"""How often a waiting daemon re-tries the run lock."""

LOCK_WAIT_GRACE_SECONDS = 300.0
"""Extra wait beyond one run's budget, for finalization and report writes."""

LockAttempt = Literal['acquired', 'held', 'stopped']


class StopFlag(Protocol):
    """The subset of ``threading.Event`` the scheduler needs."""

    def is_set(self) -> bool:
        """Whether a stop has been requested."""
        ...

    def wait(self, timeout: float | None = None) -> bool:
        """Block at most ``timeout`` seconds; true when stopped."""
        ...


class NightlyState(Protocol):
    """The daily-store query the scheduler needs."""

    def snapshot(self) -> DailySnapshot:
        """Return today's state snapshot."""
        ...


def nightly_trigger(config: RunConfig) -> CronTrigger:
    """Build the one nightly trigger from the run configuration."""
    return CronTrigger(
        hour=config.schedule_time.hour,
        minute=config.schedule_time.minute,
        timezone=config.timezone,
        jitter=config.jitter_seconds or None,
    )


def should_skip_night(state: NightlyState) -> bool:
    """Whether today already has a completed night recorded by the daily store."""
    return state.snapshot().completed_night


def run_schedule(
    config: RunConfig,
    *,
    run_night: Callable[[], RunReport],
    state: NightlyState,
    clock: Callable[[], datetime],
    stop: StopFlag,
    logger: logging.Logger | None = None,
    lock: RunLock | None = None,
    lock_wait_seconds: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    """Fire ``run_night`` once per local night until ``stop`` is set.

    The trigger is asked for the next fire time after every attempt, so a run
    that overruns the window does not cause a second start in the same night:
    the next candidate is the following day. The wait is the injected stop
    flag's ``wait``, never a bare sleep, so SIGTERM wakes the loop immediately.

    ``lock`` is the daily store's exclusive run lock. When a fire cannot take it
    it waits up to ``lock_wait_seconds`` (default: the run budget plus five
    minutes) and then skips the night with a reason; after taking it, the
    completed-night check runs again, because the run it waited for may have
    finished the roster.
    """
    log = logger if logger is not None else logging.getLogger(LOGGER_NAME)
    trigger = nightly_trigger(config)
    now = clock()
    next_fire = trigger.get_next_fire_time(None, now)
    if next_fire is None:  # pragma: no cover - an unrestricted cron always has one
        return 0
    wait_for_lock = config.budget_seconds + LOCK_WAIT_GRACE_SECONDS if lock_wait_seconds is None else lock_wait_seconds

    while not stop.is_set():
        now = clock()
        delay = max(0.0, (next_fire - now).total_seconds())
        log.info(
            '%s',
            _event('schedule_wait', next_fire=next_fire.isoformat(), seconds=round(delay, 3)),
        )
        if stop.wait(delay):
            break
        try:
            skip = should_skip_night(state)
        except Exception as exc:
            # An unreadable daily store must not take the daemon down. Run the
            # night; a duplicate is recoverable, a dead scheduler is not.
            skip = False
            log.error(
                '%s',
                _event('schedule_error', phase='state_check', detail=f'{type(exc).__name__}: {exc}'),
            )
        if skip:
            log.info(
                '%s',
                _event('schedule_skipped', reason='already completed today', fire_time=next_fire.isoformat()),
            )
        else:
            acquired = lock is None
            if lock is not None:
                try:
                    attempt = _wait_for_lock(
                        lock,
                        stop,
                        wait_seconds=wait_for_lock,
                        monotonic=monotonic,
                        fire_time=next_fire,
                        logger=log,
                    )
                except Exception as exc:
                    # If coordination itself fails, do not start a second adb
                    # client: skip the night and keep the daemon alive.
                    log.error(
                        '%s',
                        _event('schedule_error', phase='lock', detail=f'{type(exc).__name__}: {exc}'),
                    )
                    attempt = 'held'
                if attempt == 'stopped':
                    break
                acquired = attempt == 'acquired'
            if acquired:
                try:
                    # The run we waited for may have just finished the roster.
                    # Re-read after taking the lock so a completed manual run
                    # does not get a duplicate night from the daemon.
                    try:
                        skip = should_skip_night(state)
                    except Exception as exc:
                        skip = False
                        log.error(
                            '%s',
                            _event('schedule_error', phase='state_check', detail=f'{type(exc).__name__}: {exc}'),
                        )
                    if skip:
                        log.info(
                            '%s',
                            _event(
                                'schedule_skipped',
                                reason='already completed today',
                                fire_time=next_fire.isoformat(),
                            ),
                        )
                    else:
                        log.info('%s', _event('schedule_start', fire_time=next_fire.isoformat()))
                        try:
                            report = run_night()
                        except Exception as exc:
                            # The night failed; the schedule did not. Keep the daemon alive
                            # and wait for tomorrow's fire instead of exiting the container.
                            log.error(
                                '%s',
                                _event('schedule_error', phase='run', detail=f'{type(exc).__name__}: {exc}'),
                            )
                        else:
                            log.info(
                                '%s',
                                _event(
                                    'schedule_finished',
                                    run_id=report.run_id,
                                    status=report.status.value,
                                    budget_used_seconds=report.budget_used_seconds,
                                ),
                            )
                finally:
                    if lock is not None:
                        lock.release()
        next_fire = trigger.get_next_fire_time(next_fire, clock())
        if next_fire is None:  # pragma: no cover
            break
    log.info('%s', _event('schedule_stopped'))
    return 0


def _wait_for_lock(
    lock: RunLock,
    stop: StopFlag,
    *,
    wait_seconds: float,
    monotonic: Callable[[], float],
    fire_time: datetime,
    logger: logging.Logger,
) -> LockAttempt:
    """Try the run lock until it is taken, the bound expires or stop is asked.

    The bound is measured with the injected monotonic clock and the pause is the
    stop flag's ``wait``, so a SIGTERM interrupts a waiting daemon immediately
    and the test never sleeps. The holding process is named in the skip event
    when the wait expires.
    """
    deadline = monotonic() + max(0.0, wait_seconds)
    announced = False
    while True:
        try:
            lock.try_acquire()
            return 'acquired'
        except RunLockHeld as exc:
            if stop.is_set():
                return 'stopped'
            remaining = deadline - monotonic()
            if remaining <= 0.0:
                logger.info(
                    '%s',
                    _event(
                        'schedule_skipped',
                        reason='run lock held by another run',
                        fire_time=fire_time.isoformat(),
                        detail=str(exc),
                    ),
                )
                return 'held'
            if not announced:
                logger.info(
                    '%s',
                    _event(
                        'schedule_lock_wait',
                        fire_time=fire_time.isoformat(),
                        seconds=round(remaining, 3),
                        detail=str(exc),
                    ),
                )
                announced = True
            if stop.wait(min(LOCK_POLL_SECONDS, remaining)):
                return 'stopped'


def _event(name: str, **fields: object) -> str:
    import json

    return json.dumps({'event': name, **fields}, separators=(',', ':'), sort_keys=True)
