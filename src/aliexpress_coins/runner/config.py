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
    'DEFAULT_FAMILY_DORMANCY_DAYS',
    'DEFAULT_FAMILY_RETRY_DAYS',
    'DEFAULT_JITTER_SECONDS',
    'DEFAULT_KEEP_RUNS',
    'DEFAULT_MAX_PASSES',
    'DEFAULT_MAX_TASKS',
    'DEFAULT_MAX_TASK_ATTEMPTS',
    'DEFAULT_NTFY_SERVER',
    'DEFAULT_RUNS_DIR',
    'DEFAULT_RUN_BUDGET_SECONDS',
    'DEFAULT_SCHEDULE_TIME',
    'DEFAULT_SETTLE_SECONDS',
    'DEFAULT_STATE_DIR',
    'MIN_FAMILY_DORMANCY_DAYS',
    'RunConfig',
    'timezone_from_environment',
]

DEFAULT_SCHEDULE_TIME = time(3, 30)
"""The default local start time, 03:30.

The reset is observed at 00:00 device-local (docs/05-app-knowledge.md §5c), so this slot is safely
after the roll. The late time is a choice rather than a reset estimate: it avoids competing with
evening tablet use, at the cost of same-night recovery. A failed run leaves the rest of the day in
which tasks are still claimable, but the daemon has no second scheduled fire; the planned in-run
retry loop (#123) will re-enumerate a retryable failure within the same night once it lands instead
of forfeiting it. Moving the fire closer to the reset (for example 00:20) would leave more room for a
supervised retry, at the risk of driving a tablet a human is still using. Changing the default is a
separate decision.
"""

DEFAULT_KEEP_RUNS = 30
"""How many run directories retention keeps, including the current one."""

DEFAULT_NTFY_SERVER = 'https://ntfy.sh'
"""The public ntfy server; a self-hosted one is set with ``AECOINS_NTFY_SERVER``."""

DEFAULT_JITTER_SECONDS = 180
"""A three-minute jitter: a few minutes, not a different schedule."""

DEFAULT_RUN_BUDGET_SECONDS = 1200.0
"""Twenty minutes of wall clock for one night's work: a safety net, not a guess.

The measured worst night (`20261124T083006Z`) took 386.7 s — 6.4 minutes — with
ten dispatches, and a night with nothing dispatchable exits in under a minute.
The in-night retry loop (#123) and the daily ``max_per_run`` limits allow about
twelve dispatches at roughly 25-40 s each, with one sweep per round for a
repeatable family, so a legitimate worst case is about 8-12 minutes. Twenty
minutes covers that with headroom while bounding a stuck run at the executor's
clock, sleep and capture seams; an in-flight adb command is bounded by its
subprocess timeout, and a native OCR/PIL call has no in-process timeout.
An operator may raise it for a slower device or more passes.
"""

DEFAULT_MAX_TASKS = 12
"""The default number of task dispatches one run may make."""

DEFAULT_MAX_PASSES = 12
"""The absolute ceiling on full re-enumerated roster sweeps one night may make (#123, #141).

The loop normally ends by progress, not by this number: it stops after two
consecutive sweeps that dispatch nothing and reveal nothing (#141). This
ceiling is a backstop for a surface that keeps changing without progress. A
sweep can dispatch at most one row per family, so a family with ``max_per_run``
rounds needs that many productive sweeps. A success resets the failure streak,
so the retry can repeat for every round: the safe worst case is
``max_per_run * max_task_attempts``. The largest shipped limit is six
(``sponsored_items``) with two attempts, so twelve covers the worst case while
``AECOINS_MAX_TASKS_PER_RUN`` caps the dispatches themselves.
"""

DEFAULT_MAX_TASK_ATTEMPTS = 2
"""How many failed attempts of one task a night may spend before blacklisting it.

A successful dispatch resets that task's failure streak, so repeatable rows are
bounded by their daily ``max_per_run`` rather than by this blacklist limit. A
task that keeps failing is skipped for the rest of the night after this many
failures.
"""

DEFAULT_FAMILY_DORMANCY_DAYS = 7
"""How many consecutive unsighted days make a family ``not offered`` (issue #161).

The ``Coins boost`` sheet rotates. #159 saw no ``learn_to_save`` row in 15 full
reads in one evening, so one clean miss is not a fact about the app. A week of
consecutive clean passes that each sampled the whole readable sheet is long
enough to accept that the family is not on offer *for this account right now*,
short enough that a genuinely removed row stops keeping every night open, and
it is reset by a single sighting. The threshold is per **day**: a run only adds
one unsighted day, and a pass that lost a surface or had unreadable rows adds
none. :data:`MIN_FAMILY_DORMANCY_DAYS` is the startup-checked floor so one bad
night can never write a family off.
"""

MIN_FAMILY_DORMANCY_DAYS = 3
"""The smallest threshold that can ignore one or two bad nights.

A single complete read can miss a rotating family — #159 saw zero sightings in
15 samples in one evening — so writing a family off after one unsighted day
would recreate the silent-drop defect this feature exists to prevent. Three is
the floor that admits no such single-night verdict.
"""

DEFAULT_FAMILY_RETRY_DAYS = 7
"""How often a dormant family is re-probed on the slower cadence.

Every night still reads the whole sheet, so a returning row is caught at once.
This interval is the scheduled verification that the absence is still real; the
report names the probe so dormancy cannot silently become forgetting (#161).
"""

DEFAULT_APP_SETTLE_SECONDS = 10.0
"""The measured warm-up after starting the app normally (docs/07 §1)."""

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
    family_dormancy_days: int = DEFAULT_FAMILY_DORMANCY_DAYS
    family_retry_days: int = DEFAULT_FAMILY_RETRY_DAYS
    dry_run: bool = False
    max_passes: int = DEFAULT_MAX_PASSES
    max_task_attempts: int = DEFAULT_MAX_TASK_ATTEMPTS
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
            timezone=timezone_from_environment(env),
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
            family_dormancy_days=_int_at_least(
                env,
                'AECOINS_FAMILY_DORMANCY_DAYS',
                DEFAULT_FAMILY_DORMANCY_DAYS,
                MIN_FAMILY_DORMANCY_DAYS,
            ),
            dry_run=_flag(env.get('AECOINS_DRY_RUN', ''), 'AECOINS_DRY_RUN'),
            max_passes=_positive_int(env, 'AECOINS_MAX_PASSES', DEFAULT_MAX_PASSES),
            max_task_attempts=_positive_int(env, 'AECOINS_MAX_TASK_ATTEMPTS', DEFAULT_MAX_TASK_ATTEMPTS),
            family_retry_days=_positive_int(env, 'AECOINS_FAMILY_RETRY_DAYS', DEFAULT_FAMILY_RETRY_DAYS),
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


def _int_at_least(environment: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    """Read an integer that may not be low enough to lose evidence.

    A startup rejection is deliberate: ``AECOINS_FAMILY_DORMANCY_DAYS=1`` would
    turn one rotating sheet read into a verdict that a family is not offered,
    which is the silent-drop defect #161 exists to prevent.
    """
    raw = environment.get(name, '').strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f'{name} must be an integer, got {raw!r}') from exc
    if value < minimum:
        raise ConfigError(
            f'{name} must be at least {minimum} consecutive unsighted days, got {value!r}; '
            'a smaller value would write a rotating family off after one or two clean misses',
        )
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


def timezone_from_environment(environment: Mapping[str, str]) -> tzinfo:
    """Resolve ``AECOINS_TIMEZONE`` (then ``TZ``, then the local zone)."""
    name = environment.get('AECOINS_TIMEZONE', '').strip() or environment.get('TZ', '').strip()
    if not name:
        return _local_timezone()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f'unknown timezone {name!r}; set AECOINS_TIMEZONE or TZ') from exc


def _local_timezone() -> tzinfo:
    from tzlocal import get_localzone

    try:
        # The system zone's IANA key is what the device comparison needs;
        # `datetime.now().astimezone()` only gives an abbreviation such as EST.
        return get_localzone()
    except Exception as exc:
        raise ConfigError('the local timezone could not be determined; set AECOINS_TIMEZONE or TZ') from exc


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
