"""Environment-backed runner configuration (issue #29).

Every value the nightly run needs is read here, once, with a clear default and
a typed error when the environment asks for something impossible. The container
sets the paths; a local run falls back to the repository layout.

The schedule is a wall-clock local time plus an explicit timezone. The timezone
defaults to the process's ``TZ`` (or the host's local zone), because a "nightly"
run is a local-day concept and the daily state store already rotates on the
local date.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aliexpress_coins.runner.errors import ConfigError

__all__ = [
    'DEFAULT_APP_SETTLE_SECONDS',
    'DEFAULT_CATALOGUE_PATH',
    'DEFAULT_CORPUS_PATH',
    'DEFAULT_JITTER_SECONDS',
    'DEFAULT_KEEP_RUNS',
    'DEFAULT_MAX_TASKS',
    'DEFAULT_NTFY_SERVER',
    'DEFAULT_RUNS_DIR',
    'DEFAULT_RUN_BUDGET_SECONDS',
    'DEFAULT_SCHEDULE_TIME',
    'DEFAULT_SETTLE_SECONDS',
    'DEFAULT_STATE_DIR',
    'RunConfig',
]

DEFAULT_SCHEDULE_TIME = time(3, 30)
"""The default local start time, 03:30."""

DEFAULT_KEEP_RUNS = 30
"""How many run directories retention keeps, including the current one."""

DEFAULT_NTFY_SERVER = 'https://ntfy.sh'
"""The public ntfy server; a self-hosted one is set with ``AECOINS_NTFY_SERVER``."""

DEFAULT_JITTER_SECONDS = 180
"""A three-minute jitter: a few minutes, not a different schedule."""

DEFAULT_RUN_BUDGET_SECONDS = 2700.0
"""Forty-five minutes of wall clock for one night's work."""

DEFAULT_MAX_TASKS = 12
"""The default number of task dispatches one run may make."""

DEFAULT_APP_SETTLE_SECONDS = 10.0
"""The measured warm-up after starting the app normally (docs/06 §1)."""

DEFAULT_SETTLE_SECONDS = 1.0
"""How long a live navigation waits for the UI to react."""

DEFAULT_RUNS_DIR = 'runs'
DEFAULT_STATE_DIR = 'state'
DEFAULT_CATALOGUE_PATH = 'specs/tasks.yaml'
DEFAULT_CORPUS_PATH = 'specs/popups.yaml'

_TIME_FORMAT = 'HH:MM'


@dataclass(frozen=True, slots=True)
class RunConfig:
    """Everything one run or one schedule process needs from the environment."""

    device: str | None
    budget_seconds: float
    max_tasks: int
    schedule_time: time
    timezone: tzinfo
    jitter_seconds: int
    runs_dir: Path
    state_dir: Path
    catalogue_path: Path
    corpus_path: Path
    settle_seconds: float
    app_settle_seconds: float
    dry_run: bool = False
    keep_runs: int = DEFAULT_KEEP_RUNS
    apprise_url: str | None = None
    apprise_key: str | None = None
    apprise_tag: str | None = None
    webhook_url: str | None = None
    ntfy_topic: str | None = None
    ntfy_server: str = DEFAULT_NTFY_SERVER
    notify_on_empty: bool = False
    notify_balance_delta: bool = False

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        root: str | Path | None = None,
    ) -> RunConfig:
        """Read the container's variables, validating each one.

        ``root`` is the base for the two spec paths; it defaults to the current
        directory, which is ``/app`` in the image and the repository elsewhere.
        """
        env = os.environ if environment is None else environment
        base = Path(root) if root is not None else Path.cwd()
        device = env.get('AECOINS_DEVICE', '').strip() or None
        return cls(
            device=device,
            budget_seconds=_positive_float(env, 'AECOINS_RUN_BUDGET_SECONDS', DEFAULT_RUN_BUDGET_SECONDS),
            max_tasks=_positive_int(env, 'AECOINS_MAX_TASKS_PER_RUN', DEFAULT_MAX_TASKS),
            schedule_time=_time_value(env.get('AECOINS_SCHEDULE_TIME', '').strip()),
            timezone=_timezone(env),
            jitter_seconds=_non_negative_int(env, 'AECOINS_SCHEDULE_JITTER_SECONDS', DEFAULT_JITTER_SECONDS),
            runs_dir=Path(env.get('AECOINS_RUNS_DIR', '').strip() or DEFAULT_RUNS_DIR),
            state_dir=Path(env.get('AECOINS_STATE_DIR', '').strip() or DEFAULT_STATE_DIR),
            catalogue_path=_spec_path(env, 'AECOINS_TASKS_PATH', DEFAULT_CATALOGUE_PATH, base),
            corpus_path=_spec_path(env, 'AECOINS_POPUPS_PATH', DEFAULT_CORPUS_PATH, base),
            settle_seconds=_non_negative_float(env, 'AECOINS_SETTLE_SECONDS', DEFAULT_SETTLE_SECONDS),
            app_settle_seconds=_non_negative_float(
                env,
                'AECOINS_APP_SETTLE_SECONDS',
                DEFAULT_APP_SETTLE_SECONDS,
            ),
            dry_run=_flag(env.get('AECOINS_DRY_RUN', ''), 'AECOINS_DRY_RUN'),
            keep_runs=_positive_int(env, 'AECOINS_KEEP_RUNS', DEFAULT_KEEP_RUNS),
            apprise_url=_optional(env, 'AECOINS_APPRISE_URL') or _optional(env, 'APPRISE_URL'),
            apprise_key=_optional(env, 'AECOINS_APPRISE_KEY') or _optional(env, 'APPRISE_KEY'),
            apprise_tag=_optional(env, 'AECOINS_APPRISE_TAG') or _optional(env, 'APPRISE_TAG'),
            webhook_url=_optional(env, 'AECOINS_WEBHOOK_URL'),
            ntfy_topic=_optional(env, 'AECOINS_NTFY_TOPIC'),
            ntfy_server=_optional(env, 'AECOINS_NTFY_SERVER') or DEFAULT_NTFY_SERVER,
            notify_on_empty=_flag(env.get('AECOINS_NOTIFY_ON_EMPTY', ''), 'AECOINS_NOTIFY_ON_EMPTY'),
            notify_balance_delta=_flag(env.get('AECOINS_NOTIFY_BALANCE_DELTA', ''), 'AECOINS_NOTIFY_BALANCE_DELTA'),
        )


def _optional(environment: Mapping[str, str], name: str) -> str | None:
    return environment.get(name, '').strip() or None


def _positive_float(environment: Mapping[str, str], name: str, default: float) -> float:
    raw = environment.get(name, '').strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f'{name} must be a number, got {raw!r}') from exc
    if value <= 0:
        raise ConfigError(f'{name} must be positive, got {value!r}')
    return value


def _non_negative_float(environment: Mapping[str, str], name: str, default: float) -> float:
    raw = environment.get(name, '').strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f'{name} must be a number, got {raw!r}') from exc
    if value < 0:
        raise ConfigError(f'{name} must not be negative, got {value!r}')
    return value


def _positive_int(environment: Mapping[str, str], name: str, default: int) -> int:
    raw = environment.get(name, '').strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f'{name} must be an integer, got {raw!r}') from exc
    if value < 1:
        raise ConfigError(f'{name} must be positive, got {value!r}')
    return value


def _non_negative_int(environment: Mapping[str, str], name: str, default: int) -> int:
    raw = environment.get(name, '').strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f'{name} must be an integer, got {raw!r}') from exc
    if value < 0:
        raise ConfigError(f'{name} must not be negative, got {value!r}')
    return value


def _time_value(raw: str) -> time:
    if not raw:
        return DEFAULT_SCHEDULE_TIME
    parts = raw.split(':')
    if len(parts) != 2:
        raise ConfigError(f'AECOINS_SCHEDULE_TIME must be {_TIME_FORMAT}, got {raw!r}')
    try:
        hour, minute = (int(part) for part in parts)
    except ValueError as exc:
        raise ConfigError(f'AECOINS_SCHEDULE_TIME must be {_TIME_FORMAT}, got {raw!r}') from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ConfigError(f'AECOINS_SCHEDULE_TIME is out of range: {raw!r}')
    return time(hour, minute)


def _timezone(environment: Mapping[str, str]) -> tzinfo:
    name = environment.get('AECOINS_TIMEZONE', '').strip() or environment.get('TZ', '').strip()
    if not name:
        return _local_timezone()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f'unknown timezone {name!r}; set AECOINS_TIMEZONE or TZ') from exc


def _local_timezone() -> tzinfo:
    from datetime import datetime

    zone = datetime.now().astimezone().tzinfo
    if zone is None:  # pragma: no cover - every supported platform has one
        raise ConfigError('the local timezone could not be determined; set AECOINS_TIMEZONE or TZ')
    return zone


def _spec_path(environment: Mapping[str, str], name: str, default: str, base: Path) -> Path:
    raw = environment.get(name, '').strip()
    path = Path(raw) if raw else Path(default)
    if path.is_absolute():
        return path
    return base / path


def _flag(raw: str, name: str = 'AECOINS_DRY_RUN') -> bool:
    value = raw.strip().casefold()
    if not value:
        return False
    if value in {'1', 'true', 'yes', 'on'}:
        return True
    if value in {'0', 'false', 'no', 'off'}:
        return False
    raise ConfigError(f'{name} must be a boolean, got {raw!r}')
