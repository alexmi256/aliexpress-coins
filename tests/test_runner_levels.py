"""The log-level table is the contract, so the test walks every row (#40)."""

from __future__ import annotations

import logging

import pytest

from aliexpress_coins.runner.levels import (
    DEFAULT_TASK_LOG_LEVEL,
    RUN_STATUS_LOG_LEVELS,
    SKIPPED_ROW_LOG_LEVEL,
    TASK_OUTCOME_LOG_LEVELS,
    run_log_level,
    task_log_level,
)
from aliexpress_coins.runner.report import RunStatus


@pytest.mark.parametrize(('outcome', 'expected'), sorted(TASK_OUTCOME_LOG_LEVELS.items()))
def test_every_task_outcome_row_selects_its_level(outcome: str, expected: int) -> None:
    assert task_log_level(outcome) == expected


def test_a_completed_task_is_info_and_an_unsuccessful_one_is_warning() -> None:
    assert TASK_OUTCOME_LOG_LEVELS['completed'] == logging.INFO
    assert TASK_OUTCOME_LOG_LEVELS['skipped'] == logging.WARNING
    assert TASK_OUTCOME_LOG_LEVELS['failed'] == logging.WARNING
    assert DEFAULT_TASK_LOG_LEVEL == logging.WARNING
    assert task_log_level('already_done') == logging.WARNING


@pytest.mark.parametrize(('status', 'expected'), sorted(RUN_STATUS_LOG_LEVELS.items()))
def test_every_run_status_row_selects_its_level(status: str, expected: int) -> None:
    assert run_log_level(status) == expected
    assert run_log_level(RunStatus(status)) == expected


def test_a_failed_run_is_the_only_error_and_nothing_is_critical() -> None:
    assert RUN_STATUS_LOG_LEVELS[RunStatus.FAILED.value] == logging.ERROR
    assert RUN_STATUS_LOG_LEVELS[RunStatus.STOPPED.value] == logging.WARNING
    assert all(level < logging.CRITICAL for level in RUN_STATUS_LOG_LEVELS.values())
    assert all(level < logging.CRITICAL for level in TASK_OUTCOME_LOG_LEVELS.values())
    assert SKIPPED_ROW_LOG_LEVEL == logging.WARNING


def test_every_run_status_has_a_table_row() -> None:
    assert set(RUN_STATUS_LOG_LEVELS) == {status.value for status in RunStatus}


def test_an_unknown_run_status_defaults_to_info() -> None:
    assert run_log_level('not-a-status') == logging.INFO
