"""Per-day collector state that survives a kill (issue #30).

State answers two questions the screen cannot answer on its own: *what has
already been done today*, and *how many times*. A restart therefore does not
re-claim the sign-in or repeat a task until the app answers with "already
collected" modals for the rest of the night.

The file is ``<state_dir>/daily-<YYYY-MM-DD>.json`` where the date comes from
the injected clock. One file per local day gives the rotation rule for free: a
store looking at a new day reads a different path, so yesterday's file is never
mistaken for today's and stays on disk for the report.

.. code-block:: json

    {
      "schema": 1,
      "date": "2026-11-21",
      "completions": {"daily_checkin": 1, "sponsored_items": 3},
      "last_balance": 1240.0,
      "run_count": 2,
      "last_run_at": "2026-11-21T03:42:11+00:00",
      "completed_night": true
    }

Every update is staged in the same directory, fsynced, then renamed over the
target (:func:`aliexpress_coins.perception.artifacts.atomic_write_bytes`), so a
kill leaves the old file or the new one, never a truncated one. A file that is
not valid JSON, has the wrong schema or fails validation is moved aside as
``daily-<date>.json.corrupt-<timestamp>`` and the day starts empty with a
warning, because losing a night to a half-written file is worse than losing the
few counters it held.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from aliexpress_coins.perception.artifacts import atomic_write_bytes
from aliexpress_coins.runner.errors import StateError, StateReadError, StateWriteError

__all__ = [
    'DAILY_STATE_SCHEMA',
    'LOGGER_NAME',
    'DailySnapshot',
    'DailyStateStore',
    'StateError',
    'StateReadError',
    'StateWriteError',
]

LOGGER_NAME = 'aliexpress_coins.runner.state'
"""The logger used when the caller does not supply one."""

DAILY_STATE_SCHEMA = 1
"""The only on-disk schema this module reads or writes."""

_DEFAULT_STATE_DIR = 'state'
"""Fallback when ``AECOINS_STATE_DIR`` is unset, as runs/ is for artifacts."""

Clock = Callable[[], datetime]


def _local_now() -> datetime:
    """Today's local time; replaced by an injected clock in every test."""
    return datetime.now()


def _fresh(day: date) -> DailySnapshot:
    return DailySnapshot(day=day, completions={})


@dataclass(frozen=True, slots=True)
class DailySnapshot:
    """The whole day at once, for the run report (#31) and for diagnostics."""

    day: date
    completions: Mapping[str, int]
    last_balance: float | None = None
    run_count: int = 0
    last_run_at: datetime | None = None
    completed_night: bool = False

    def __post_init__(self) -> None:
        # A snapshot is a value: callers must not mutate the store through it.
        object.__setattr__(self, 'completions', MappingProxyType(dict(self.completions)))

    def as_dict(self) -> dict[str, Any]:
        """The on-disk/report payload, with the schema and ISO timestamp."""
        return {
            'schema': DAILY_STATE_SCHEMA,
            'date': self.day.isoformat(),
            'completions': dict(self.completions),
            'last_balance': self.last_balance,
            'run_count': self.run_count,
            'last_run_at': None if self.last_run_at is None else self.last_run_at.isoformat(),
            'completed_night': self.completed_night,
        }


class DailyStateStore:
    """Read and atomically update one local day's state.

    Construction creates the directory but does not read the file; the first
    query loads it. The day is re-checked on every operation, so a process that
    crosses midnight starts a new file instead of appending to yesterday's.
    """

    def __init__(
        self,
        state_dir: str | Path,
        *,
        clock: Clock = _local_now,
        logger: logging.Logger | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self._clock = clock
        self._logger = logger if logger is not None else logging.getLogger(LOGGER_NAME)
        self._day: date | None = None
        self._state: DailySnapshot | None = None
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StateError(f'could not create state directory {self.state_dir}: {exc}') from exc

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        clock: Clock = _local_now,
        logger: logging.Logger | None = None,
    ) -> DailyStateStore:
        """Use the container's ``AECOINS_STATE_DIR``, falling back to ``state/``."""
        env = os.environ if environment is None else environment
        root = env.get('AECOINS_STATE_DIR', '').strip() or _DEFAULT_STATE_DIR
        return cls(root, clock=clock, logger=logger)

    @property
    def path(self) -> Path:
        """The file today's state is written to (it may not exist yet)."""
        return self._path_for(self._clock().date())

    def record(self, task_key: str, *, balance: float | None = None) -> int:
        """Count one completion of ``task_key`` and return its new total.

        ``balance`` is optional so a task that does not read the balance can
        record without clobbering the last observed value. The file is written
        before the in-memory state changes: a failed write leaves the previous
        state authoritative and raises :class:`StateWriteError`.
        """
        key = _task_key(task_key)
        current = self._loaded()
        completions = dict(current.completions)
        completions[key] = completions.get(key, 0) + 1
        last_balance = current.last_balance if balance is None else _balance(balance)
        self._commit(replace(current, completions=completions, last_balance=last_balance))
        return completions[key]

    def count(self, task_key: str) -> int:
        """How many times ``task_key`` has completed today (0 when unseen)."""
        return self._loaded().completions.get(_task_key(task_key), 0)

    def done_today(self, task_key: str) -> bool:
        """Whether ``task_key`` has completed at least once today."""
        return self.count(task_key) > 0

    def begin_run(self, *, at: datetime | None = None) -> int:
        """Count this run and stamp its start, returning the day's run number."""
        current = self._loaded()
        run_at = self._clock() if at is None else at
        run_count = current.run_count + 1
        self._commit(replace(current, run_count=run_count, last_run_at=run_at))
        return run_count

    def complete_night(self) -> None:
        """Mark the night as finished, which is what the scheduler skips on.

        ``begin_run`` counts every attempt, including a manual run stopped by its
        budget. Only a run that walked the roster to the end calls this, so a
        small manual run cannot swallow the nightly window.
        """
        current = self._loaded()
        self._commit(replace(current, completed_night=True))

    def observe_balance(self, balance: float) -> None:
        """Store the last balance seen, without recording a task completion."""
        current = self._loaded()
        self._commit(replace(current, last_balance=_balance(balance)))

    def snapshot(self) -> DailySnapshot:
        """Give #31 the whole day: counters, balance, run count and timestamp."""
        return self._loaded()

    def _path_for(self, day: date) -> Path:
        return self.state_dir / f'daily-{day.isoformat()}.json'

    def _loaded(self) -> DailySnapshot:
        day = self._clock().date()
        state = self._state
        if self._day != day or state is None:
            state = self._load(day)
            self._state = state
            self._day = day
        return state

    def _load(self, day: date) -> DailySnapshot:
        path = self._path_for(day)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return _fresh(day)
        except OSError as exc:
            raise StateReadError(f'could not read {path}: {exc}') from exc

        try:
            return _parse(raw, day)
        except _CorruptState as exc:
            self._quarantine(path, exc)
            return _fresh(day)

    def _commit(self, state: DailySnapshot) -> None:
        path = self._path_for(state.day)
        payload = (json.dumps(state.as_dict(), ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')
        try:
            atomic_write_bytes(path, payload)
        except OSError as exc:
            raise StateWriteError(f'could not write {path}: {exc}') from exc
        self._day = state.day
        self._state = state

    def _quarantine(self, path: Path, reason: _CorruptState) -> None:
        stamp = self._clock().strftime('%Y%m%dT%H%M%S%fZ')
        target = _unique_path(path.with_name(f'{path.name}.corrupt-{stamp}'))
        fields: dict[str, object] = {'path': str(path), 'reason': str(reason)}
        try:
            os.replace(path, target)
        except OSError as exc:
            # The run must still proceed: the next commit replaces the bad file.
            fields['quarantine_error'] = str(exc)
        else:
            fields['quarantined'] = str(target)
        self._warn('state_corrupt', **fields)

    def _warn(self, event: str, **fields: object) -> None:
        self._logger.warning(
            '%s',
            json.dumps({'event': event, **fields}, separators=(',', ':'), sort_keys=True),
        )


class _CorruptState(Exception):
    """Internal marker: the file is unparseable or fails validation."""


def _parse(raw: bytes, day: date) -> DailySnapshot:
    try:
        payload: Any = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _CorruptState(f'not valid UTF-8 JSON: {exc}') from exc

    if not isinstance(payload, dict):
        raise _CorruptState('state must be a JSON object')
    data = cast(dict[str, Any], payload)

    schema = _field(data, 'schema')
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != DAILY_STATE_SCHEMA:
        raise _CorruptState(f'unsupported schema: {schema!r}')

    recorded_day = _field(data, 'date')
    if recorded_day != day.isoformat():
        raise _CorruptState(f'date {recorded_day!r} does not match {day.isoformat()!r}')

    completions_raw = _field(data, 'completions')
    if not isinstance(completions_raw, dict):
        raise _CorruptState("'completions' must be a JSON object")
    completions: dict[str, int] = {}
    for key, value in cast(dict[str, Any], completions_raw).items():
        if not key.strip():
            raise _CorruptState('completion key must be non-empty')
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _CorruptState(f'completion count for {key!r} must be a non-negative integer')
        completions[key] = value

    last_balance_raw = _field(data, 'last_balance')
    if last_balance_raw is None:
        last_balance = None
    elif (
        isinstance(last_balance_raw, bool)
        or not isinstance(last_balance_raw, (int, float))
        or not math.isfinite(last_balance_raw)
    ):
        raise _CorruptState("'last_balance' must be a finite number or null")
    else:
        last_balance = float(last_balance_raw)

    run_count = _field(data, 'run_count')
    if isinstance(run_count, bool) or not isinstance(run_count, int) or run_count < 0:
        raise _CorruptState("'run_count' must be a non-negative integer")

    last_run_raw = _field(data, 'last_run_at')
    if last_run_raw is None:
        last_run_at = None
    elif isinstance(last_run_raw, str):
        try:
            last_run_at = datetime.fromisoformat(last_run_raw)
        except ValueError as exc:
            raise _CorruptState(f"'last_run_at' is not an ISO timestamp: {last_run_raw!r}") from exc
    else:
        raise _CorruptState("'last_run_at' must be an ISO timestamp or null")

    completed_night = data.get('completed_night', False)
    if not isinstance(completed_night, bool):
        raise _CorruptState("'completed_night' must be a boolean")

    return DailySnapshot(
        day=day,
        completions=completions,
        last_balance=last_balance,
        run_count=run_count,
        last_run_at=last_run_at,
        completed_night=completed_night,
    )


def _field(data: Mapping[str, Any], key: str) -> Any:
    missing = object()
    value = data.get(key, missing)
    if value is missing:
        raise _CorruptState(f'missing field {key!r}')
    return value


def _task_key(task_key: str) -> str:
    if not isinstance(task_key, str):
        raise StateError(f'task key must be a string, got {type(task_key).__name__}')
    if not task_key or task_key != task_key.strip():
        raise StateError(f'task key must be non-empty without surrounding whitespace: {task_key!r}')
    return task_key


def _balance(balance: float) -> float:
    if isinstance(balance, bool) or not isinstance(balance, (int, float)):
        raise StateError(f'balance must be a finite number, got {balance!r}')
    value = float(balance)
    if not math.isfinite(value):
        raise StateError(f'balance must be a finite number, got {balance!r}')
    return value


def _unique_path(path: Path) -> Path:
    """Do not overwrite an earlier quarantine from the same clock tick."""
    candidate = path
    suffix = 1
    while candidate.exists():
        candidate = path.with_name(f'{path.name}.{suffix}')
        suffix += 1
    return candidate
