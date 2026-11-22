"""The run loop's local memory: per-day state, reports and recovery (#30).

The first piece is :class:`~aliexpress_coins.runner.state.DailyStateStore`.
Executors (#23, #24) call :meth:`~aliexpress_coins.runner.state.DailyStateStore.record`
after the screen has confirmed a completion, and the dispatch point asks
:meth:`~aliexpress_coins.runner.state.DailyStateStore.done_today` before
re-claiming a one-shot task.
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
