"""The run loop: per-day state, the engine, its report and the nightly schedule.

:class:`~aliexpress_coins.runner.state.DailyStateStore` is the day's memory.
:func:`~aliexpress_coins.runner.engine.run_once` walks the roster and dispatches
each allowed row to its executor, bounded by the task and wall-clock budgets.
:class:`~aliexpress_coins.runner.report.RunReport` is written atomically on every
path, and :func:`~aliexpress_coins.runner.schedule.run_schedule` asks APScheduler
for the next jittered local fire time until SIGTERM.
"""

from aliexpress_coins.runner.errors import RunnerError, StateError, StateReadError, StateWriteError
from aliexpress_coins.runner.state import (
    DAILY_STATE_SCHEMA,
    LOGGER_NAME,
    DailySnapshot,
    DailyStateStore,
)

__all__ = [
    'DAILY_STATE_SCHEMA',
    'LOGGER_NAME',
    'DailySnapshot',
    'DailyStateStore',
    'RunnerError',
    'StateError',
    'StateReadError',
    'StateWriteError',
]
