"""Environment-backed runner configuration (issue #29)."""

from __future__ import annotations

from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from aliexpress_coins.runner.config import (
    DEFAULT_APP_SETTLE_SECONDS,
    DEFAULT_JITTER_SECONDS,
    DEFAULT_MAX_TASKS,
    DEFAULT_RUN_BUDGET_SECONDS,
    DEFAULT_SCHEDULE_TIME,
    DEFAULT_SETTLE_SECONDS,
    RunConfig,
)
from aliexpress_coins.runner.errors import ConfigError


def test_defaults_keep_the_container_paths_and_the_local_zone() -> None:
    config = RunConfig.from_environment({}, root='/app')

    assert config.device is None
    assert config.budget_seconds == DEFAULT_RUN_BUDGET_SECONDS
    assert config.max_tasks == DEFAULT_MAX_TASKS
    assert config.schedule_time == DEFAULT_SCHEDULE_TIME
    assert config.jitter_seconds == DEFAULT_JITTER_SECONDS
    assert config.timezone is not None
    assert config.runs_dir == Path('runs')
    assert config.state_dir == Path('state')
    assert config.catalogue_path == Path('/app/specs/tasks.yaml')
    assert config.corpus_path == Path('/app/specs/popups.yaml')
    assert config.settle_seconds == DEFAULT_SETTLE_SECONDS
    assert config.app_settle_seconds == DEFAULT_APP_SETTLE_SECONDS
    assert config.dry_run is False


def test_every_documented_variable_is_read() -> None:
    config = RunConfig.from_environment(
        {
            'AECOINS_DEVICE': ' 192.168.8.65:5555 ',
            'AECOINS_RUN_BUDGET_SECONDS': '120',
            'AECOINS_MAX_TASKS_PER_RUN': '3',
            'AECOINS_SCHEDULE_TIME': '23:45',
            'AECOINS_TIMEZONE': 'Europe/Berlin',
            'AECOINS_SCHEDULE_JITTER_SECONDS': '0',
            'AECOINS_RUNS_DIR': '/tmp/runs',
            'AECOINS_STATE_DIR': '/tmp/state',
            'AECOINS_TASKS_PATH': '/tmp/tasks.yaml',
            'AECOINS_POPUPS_PATH': 'popups.yaml',
            'AECOINS_SETTLE_SECONDS': '2.5',
            'AECOINS_APP_SETTLE_SECONDS': '0',
            'AECOINS_DRY_RUN': 'yes',
        },
        root='/srv/app',
    )

    assert config.device == '192.168.8.65:5555'
    assert config.budget_seconds == 120.0
    assert config.max_tasks == 3
    assert config.schedule_time == time(23, 45)
    assert config.timezone == ZoneInfo('Europe/Berlin')
    assert config.jitter_seconds == 0
    assert config.runs_dir == Path('/tmp/runs')
    assert config.state_dir == Path('/tmp/state')
    assert config.catalogue_path == Path('/tmp/tasks.yaml')
    assert config.corpus_path == Path('/srv/app/popups.yaml')
    assert config.settle_seconds == 2.5
    assert config.app_settle_seconds == 0.0
    assert config.dry_run is True


@pytest.mark.parametrize('value', ['0', 'false', 'no', 'off'])
def test_boolean_flag_accepts_every_false_spelling(value: str) -> None:
    assert RunConfig.from_environment({'AECOINS_DRY_RUN': value}).dry_run is False


def test_tz_is_used_when_the_explicit_timezone_is_absent() -> None:
    config = RunConfig.from_environment({'TZ': 'America/New_York'})
    assert config.timezone == ZoneInfo('America/New_York')


def test_explicit_timezone_wins_over_tz() -> None:
    config = RunConfig.from_environment({'TZ': 'America/New_York', 'AECOINS_TIMEZONE': 'UTC'})
    assert config.timezone == ZoneInfo('UTC')


@pytest.mark.parametrize(
    ('environment', 'message'),
    [
        ({'AECOINS_RUN_BUDGET_SECONDS': 'soon'}, 'must be a number'),
        ({'AECOINS_RUN_BUDGET_SECONDS': '0'}, 'must be positive'),
        ({'AECOINS_MAX_TASKS_PER_RUN': 'many'}, 'must be an integer'),
        ({'AECOINS_MAX_TASKS_PER_RUN': '0'}, 'must be positive'),
        ({'AECOINS_SCHEDULE_TIME': '0330'}, 'must be HH:MM'),
        ({'AECOINS_SCHEDULE_TIME': 'aa:bb'}, 'must be HH:MM'),
        ({'AECOINS_SCHEDULE_TIME': '24:00'}, 'out of range'),
        ({'AECOINS_SCHEDULE_TIME': '03:60'}, 'out of range'),
        ({'AECOINS_SCHEDULE_JITTER_SECONDS': 'later'}, 'must be an integer'),
        ({'AECOINS_SCHEDULE_JITTER_SECONDS': '-1'}, 'must not be negative'),
        ({'AECOINS_SETTLE_SECONDS': '-0.1'}, 'must not be negative'),
        ({'AECOINS_APP_SETTLE_SECONDS': 'fast'}, 'must be a number'),
        ({'AECOINS_TIMEZONE': 'Mars/Olympus'}, 'unknown timezone'),
        ({'AECOINS_DRY_RUN': 'maybe'}, 'must be a boolean'),
    ],
)
def test_invalid_values_raise_a_configuration_error(environment: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        RunConfig.from_environment(environment)
