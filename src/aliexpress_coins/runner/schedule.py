"""The nightly schedule: one CronTrigger, a jittered start and a clean stop.

APScheduler's :class:`~apscheduler.triggers.cron.CronTrigger` supplies the
calendar arithmetic (local wall clock, DST transitions) and the jitter. This
module deliberately does not use a ``BlockingScheduler``: the process must run
one night's engine synchronously and react to SIGTERM inside it, so a small loop
that asks the trigger for the next fire time is easier to test and to stop than
a scheduler thread.

A night that already has a run in the daily store is skipped. That is what makes
a manual ``run --once`` and the daemon coexist: the daemon does not start a
second walk over the same day's state.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from aliexpress_coins.runner.config import RunConfig
from aliexpress_coins.runner.report import RunReport
from aliexpress_coins.runner.state import DailySnapshot

__all__ = [
    'LOGGER_NAME',
    'StopFlag',
    'nightly_trigger',
    'run_schedule',
    'should_skip_night',
]

LOGGER_NAME = 'aliexpress_coins.runner.schedule'


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
    """Whether today already has a run recorded by the daily store."""
    return state.snapshot().run_count > 0


def run_schedule(
    config: RunConfig,
    *,
    run_night: Callable[[], RunReport],
    state: NightlyState,
    clock: Callable[[], datetime],
    stop: StopFlag,
    logger: logging.Logger | None = None,
) -> int:
    """Fire ``run_night`` once per local night until ``stop`` is set.

    The trigger is asked for the next fire time after every attempt, so a run
    that overruns the window does not cause a second start in the same night:
    the next candidate is the following day. The wait is the injected stop
    flag's ``wait``, never a bare sleep, so SIGTERM wakes the loop immediately.
    """
    log = logger if logger is not None else logging.getLogger(LOGGER_NAME)
    trigger = nightly_trigger(config)
    now = clock()
    next_fire = trigger.get_next_fire_time(None, now)
    if next_fire is None:  # pragma: no cover - an unrestricted cron always has one
        return 0

    while not stop.is_set():
        now = clock()
        delay = max(0.0, (next_fire - now).total_seconds())
        log.info(
            '%s',
            _event('schedule_wait', next_fire=next_fire.isoformat(), seconds=round(delay, 3)),
        )
        if stop.wait(delay):
            break
        if should_skip_night(state):
            log.info(
                '%s',
                _event('schedule_skipped', reason='already ran today', fire_time=next_fire.isoformat()),
            )
        else:
            log.info('%s', _event('schedule_start', fire_time=next_fire.isoformat()))
            report = run_night()
            log.info(
                '%s',
                _event(
                    'schedule_finished',
                    run_id=report.run_id,
                    status=report.status.value,
                    budget_used_seconds=report.budget_used_seconds,
                ),
            )
        next_fire = trigger.get_next_fire_time(next_fire, clock())
        if next_fire is None:  # pragma: no cover
            break
    log.info('%s', _event('schedule_stopped'))
    return 0


def _event(name: str, **fields: object) -> str:
    import json

    return json.dumps({'event': name, **fields}, separators=(',', ':'), sort_keys=True)
