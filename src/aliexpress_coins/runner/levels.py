"""The one table that maps a run event to a Python log level (issue #40).

The engine must not decide a level at each call site: a task outcome, a skipped
row and the run's terminal status each have exactly one sensible level, and the
table here is the contract. ``run.jsonl`` records the level because
:class:`~aliexpress_coins.runner.report.RunEventLog` copies it from the logging
record, so a log pipeline can filter the structured event without parsing prose.

The levels are deliberately boring:

* a completed task is ``INFO``;
* a task that did not happen (skipped, refused, failed or unverified) is
  ``WARNING``;
* a run that stopped on purpose is ``WARNING``;
* a run that failed is ``ERROR``.

``CRITICAL`` is reserved for an unrecoverable process fault and is not used by a
normal run.
"""

from __future__ import annotations

import logging

from aliexpress_coins.runner.report import RunStatus

__all__ = [
    'DEFAULT_TASK_LOG_LEVEL',
    'RUN_STATUS_LOG_LEVELS',
    'SKIPPED_ROW_LOG_LEVEL',
    'TASK_OUTCOME_LOG_LEVELS',
    'run_log_level',
    'task_log_level',
]

TASK_OUTCOME_LOG_LEVELS: dict[str, int] = {
    'completed': logging.INFO,
    'skipped': logging.WARNING,
    'failed': logging.WARNING,
}
"""Dispatched task outcome -> level.

The executor outcomes that are not one of these three (``already_done``,
``unavailable``, ``permission_denied``, ``routed_to_settings``) are all forms of
"the task did not happen", so they fall through to the warning default rather
than being logged as a success.
"""

DEFAULT_TASK_LOG_LEVEL = logging.WARNING
"""An outcome outside the table did not complete the task."""

SKIPPED_ROW_LOG_LEVEL = logging.WARNING
"""A row that was never dispatched (policy, no executor, budget, stop)."""

RUN_STATUS_LOG_LEVELS: dict[str, int] = {
    RunStatus.RUNNING.value: logging.INFO,
    RunStatus.COMPLETED.value: logging.INFO,
    RunStatus.BUDGET.value: logging.INFO,
    RunStatus.PLANNED.value: logging.INFO,
    RunStatus.STOPPED.value: logging.WARNING,
    RunStatus.ABORTED.value: logging.ERROR,
    RunStatus.FAILED.value: logging.ERROR,
}
"""Terminal run status -> level. A budget stop is a clean end, not a fault."""


def task_log_level(outcome: str) -> int:
    """Return the level for one dispatched task outcome."""
    return TASK_OUTCOME_LOG_LEVELS.get(outcome, DEFAULT_TASK_LOG_LEVEL)


def run_log_level(status: RunStatus | str) -> int:
    """Return the level for a run status (an enum or its string value)."""
    value = status.value if isinstance(status, RunStatus) else status
    return RUN_STATUS_LOG_LEVELS.get(value, logging.INFO)
