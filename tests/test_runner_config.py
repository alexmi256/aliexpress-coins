"""Environment-backed runner configuration (issue #29)."""

from __future__ import annotations

from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import tzlocal

from aliexpress_coins.runner.config import (
    DEFAULT_APP_SETTLE_SECONDS,
    DEFAULT_JITTER_SECONDS,
    DEFAULT_KEEP_RUNS,
    DEFAULT_MAX_TASKS,
    DEFAULT_NTFY_SERVER,
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
    assert config.keep_runs == DEFAULT_KEEP_RUNS
    assert config.apprise_url is None
    assert config.apprise_key is None
    assert config.apprise_tag is None
    assert config.webhook_url is None
    assert config.ntfy_topic is None
    assert config.ntfy_server == DEFAULT_NTFY_SERVER
    assert config.notify_on_empty is False
    assert config.notify_balance_delta is False


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
            'AECOINS_KEEP_RUNS': '7',
            'AECOINS_APPRISE_URL': ' https://apprise.example ',
            'AECOINS_APPRISE_KEY': 'coins-nightly',
            'AECOINS_APPRISE_TAG': 'ops',
            'AECOINS_WEBHOOK_URL': ' https://hooks.example/coins ',
            'AECOINS_NTFY_TOPIC': 'coins-nightly',
            'AECOINS_NTFY_SERVER': 'https://ntfy.example',
            'AECOINS_NOTIFY_ON_EMPTY': 'true',
            'AECOINS_NOTIFY_BALANCE_DELTA': 'yes',
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
    assert config.keep_runs == 7
    assert config.apprise_url == 'https://apprise.example'
    assert config.apprise_key == 'coins-nightly'
    assert config.apprise_tag == 'ops'
    assert config.webhook_url == 'https://hooks.example/coins'
    assert config.ntfy_topic == 'coins-nightly'
    assert config.ntfy_server == 'https://ntfy.example'
    assert config.notify_on_empty is True
    assert config.notify_balance_delta is True


@pytest.mark.parametrize('value', ['0', 'false', 'no', 'off'])
def test_boolean_flag_accepts_every_false_spelling(value: str) -> None:
    assert RunConfig.from_environment({'AECOINS_DRY_RUN': value}).dry_run is False


def test_apprise_accepts_the_unprefixed_names_and_prefers_the_project_prefix() -> None:
    plain = RunConfig.from_environment(
        {'APPRISE_URL': 'https://apprise.example', 'APPRISE_KEY': 'coins', 'APPRISE_TAG': 'ops'},
    )
    assert (plain.apprise_url, plain.apprise_key, plain.apprise_tag) == (
        'https://apprise.example',
        'coins',
        'ops',
    )

    prefixed = RunConfig.from_environment(
        {
            'APPRISE_URL': 'https://ignored.example',
            'AECOINS_APPRISE_URL': 'https://apprise.example',
        },
    )
    assert prefixed.apprise_url == 'https://apprise.example'


def test_a_missing_system_zone_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> ZoneInfo:
        raise RuntimeError('no system zone')

    monkeypatch.setattr(tzlocal, 'get_localzone', broken)

    with pytest.raises(ConfigError, match='local timezone could not be determined'):
        RunConfig.from_environment({})


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
        ({'AECOINS_KEEP_RUNS': 'many'}, 'must be an integer'),
        ({'AECOINS_KEEP_RUNS': '0'}, 'must be positive'),
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
        ({'AECOINS_NOTIFY_BALANCE_DELTA': 'sometimes'}, 'AECOINS_NOTIFY_BALANCE_DELTA must be a boolean'),
    ],
)
def test_invalid_values_raise_a_configuration_error(environment: dict[str, str], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        RunConfig.from_environment(environment)


def test_retry_loop_bounds_default_and_read_from_the_environment() -> None:
    plain = RunConfig.from_environment({})

    assert plain.max_passes == 4
    assert plain.max_task_attempts == 2

    configured = RunConfig.from_environment(
        {'AECOINS_MAX_PASSES': '6', 'AECOINS_MAX_TASK_ATTEMPTS': '3'},
    )

    assert configured.max_passes == 6
    assert configured.max_task_attempts == 3


@pytest.mark.parametrize('name', ['AECOINS_MAX_PASSES', 'AECOINS_MAX_TASK_ATTEMPTS'])
def test_retry_loop_bounds_reject_a_non_positive_value(name: str) -> None:
    with pytest.raises(ConfigError, match='must be positive'):
        RunConfig.from_environment({name: '0'})
