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
      "last_balance_verified": true,
      "run_count": 2,
      "last_run_at": "2026-11-21T03:42:11+00:00",
      "completed_night": true,
      "acted_unverified": [],
      "last_night": {
        "outcome": "exhausted",
        "dispatched": 4,
        "completed": 1,
        "retryable_failures": 0
      }
    }

``completed_night`` is not "the walk finished". It is "the walk finished and
nothing dispatched is worth retrying" (issue #97): every dispatched row either
completed, was skipped/refused, or failed with a taxonomy entry that spends no
retry. ``last_night`` records that decision so a later run and the report can
explain it. A run with a retryable failure leaves the day open even when another
row completed, because the retryable row is still unclaimed work. A task that
acted without proof is named in ``acted_unverified`` so its next dispatch
re-reads the fresh row before tapping.

``last_balance_verified`` records the provenance of the stored value: ``true``
means it came from a trusted coin-page balance band, ``false`` or ``null``
means a zero of unknown provenance (a legacy file has no field at all). A
verified zero is a real new account; an unverified zero is cleared the next
time the balance cannot be read (issue #94).

Every update is staged in the same directory, fsynced, then renamed over the
target (:func:`aliexpress_coins.perception.artifacts.atomic_write_bytes`), so a
kill leaves the old file or the new one, never a truncated one. A file that is
not valid JSON, has the wrong schema or fails validation is moved aside as
``daily-<date>.json.corrupt-<timestamp>`` and the day starts empty with a
warning, because losing a night to a half-written file is worse than losing the
few counters it held.

Atomic writes protect one file, not one day: a manual run and the nightly daemon
would still both read the day as unfinished and dispatch work at the same tablet.
:meth:`DailyStateStore.run_lock` supplies the exclusive ``run.lock`` that each
entry point holds for the whole run (issue #78); see
:mod:`aliexpress_coins.runner.lock`.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from aliexpress_coins.perception.artifacts import atomic_write_bytes
from aliexpress_coins.runner.errors import RunLockHeld, StateError, StateReadError, StateWriteError
from aliexpress_coins.runner.lock import LOCK_FILENAME, RunLock

__all__ = [
    'DAILY_STATE_SCHEMA',
    'LOGGER_NAME',
    'RUN_LOCK_FILENAME',
    'DailySnapshot',
    'DailyStateStore',
    'NightOutcome',
    'NightRecord',
    'RunLock',
    'RunLockHeld',
    'StateError',
    'StateReadError',
    'StateWriteError',
]

LOGGER_NAME = 'aliexpress_coins.runner.state'
"""The logger used when the caller does not supply one."""

DAILY_STATE_SCHEMA = 1
"""The only on-disk schema this module reads or writes."""

RUN_LOCK_FILENAME = LOCK_FILENAME
"""The exclusive run lock's name inside the state directory (issue #78)."""

_DEFAULT_STATE_DIR = 'state'
"""Fallback when ``AECOINS_STATE_DIR`` is unset, as runs/ is for artifacts."""

Clock = Callable[[], datetime]


def _local_now() -> datetime:
    """Today's local time; replaced by an injected clock in every test."""
    return datetime.now()


def _fresh(day: date) -> DailySnapshot:
    return DailySnapshot(day=day, completions={})


class NightOutcome(StrEnum):
    """Why a completed roster walk did or did not finish the day (#97)."""

    EXHAUSTED = 'exhausted'
    """The roster was walked and nothing dispatched is worth retrying."""

    RETRYABLE_FAILURES = 'retryable_failures'
    """At least one dispatched task failed with a taxonomy row that spends a retry."""


@dataclass(frozen=True, slots=True)
class NightRecord:
    """The completion decision a finished walk left for the rest of the day."""

    outcome: NightOutcome
    dispatched: int = 0
    completed: int = 0
    retryable_failures: int = 0

    @property
    def complete(self) -> bool:
        """Whether this night leaves nothing worth retrying."""
        return self.outcome is NightOutcome.EXHAUSTED

    def as_dict(self) -> dict[str, Any]:
        """Render the decision for the daily state and the run report."""
        return {
            'outcome': self.outcome.value,
            'dispatched': self.dispatched,
            'completed': self.completed,
            'retryable_failures': self.retryable_failures,
        }


@dataclass(frozen=True, slots=True)
class DailySnapshot:
    """The whole day at once, for the run report (#31) and for diagnostics."""

    day: date
    completions: Mapping[str, int]
    last_balance: float | None = None
    last_balance_verified: bool | None = None
    run_count: int = 0
    last_run_at: datetime | None = None
    completed_night: bool = False
    acted_unverified: tuple[str, ...] = ()
    last_night: NightRecord | None = None

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
            'last_balance_verified': self.last_balance_verified,
            'run_count': self.run_count,
            'last_run_at': None if self.last_run_at is None else self.last_run_at.isoformat(),
            'completed_night': self.completed_night,
            'acted_unverified': list(self.acted_unverified),
            'last_night': None if self.last_night is None else self.last_night.as_dict(),
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

    @property
    def run_lock_path(self) -> Path:
        """The exclusive run lock shared by every run on this state directory."""
        return self.state_dir / RUN_LOCK_FILENAME

    def run_lock(
        self,
        *,
        clock: Clock | None = None,
        logger: logging.Logger | None = None,
    ) -> RunLock:
        """Build the lock a run must hold for its whole duration.

        The lock is per state directory, not per day: a run that crosses
        midnight still owns the device, and the next day's fire waits for it
        just like the same day's would.
        """
        return RunLock(
            self.run_lock_path,
            clock=self._clock if clock is None else clock,
            logger=self._logger if logger is None else logger,
        )

    def record(
        self,
        task_key: str,
        *,
        balance: float | None = None,
        balance_verified: bool = True,
    ) -> int:
        """Count one completion of ``task_key`` and return its new total.

        ``balance`` is optional so a task that does not read the balance can
        record without clobbering the last observed value. ``balance_verified``
        says whether a supplied value came from a trusted coin-page reading; it
        is only meaningful for a zero, where it separates a new account from a
        misread control numeral. The file is written before the in-memory state
        changes: a failed write leaves the previous state authoritative and
        raises :class:`StateWriteError`.
        """
        key = _task_key(task_key)
        current = self._loaded()
        completions = dict(current.completions)
        completions[key] = completions.get(key, 0) + 1
        last_balance = current.last_balance if balance is None else _balance(balance)
        last_balance_verified = current.last_balance_verified if balance is None else balance_verified
        self._commit(
            replace(
                current,
                completions=completions,
                last_balance=last_balance,
                last_balance_verified=last_balance_verified,
            ),
        )
        return completions[key]

    def count(self, task_key: str) -> int:
        """How many times ``task_key`` has completed today (0 when unseen)."""
        return self._loaded().completions.get(_task_key(task_key), 0)

    def done_today(self, task_key: str) -> bool:
        """Whether ``task_key`` has completed at least once today."""
        return self.count(task_key) > 0

    def begin_run(self, *, at: datetime | None = None) -> int:
        """Count this run and stamp its start, returning the day's run number.

        This is called after the run lock is held, so it is also the first safe
        place to persist the one-time healing of a legacy zero. The pre-lock
        schedule read must stay side-effect free: another process may be
        writing the same day's state.
        """
        current = self._loaded()
        if current.last_balance == 0.0 and current.last_balance_verified is not True:
            self._warn(
                'balance_zero_unverified_cleared',
                stored_balance=0.0,
                detail='the stored zero had no trusted coin-page provenance',
            )
            current = replace(current, last_balance=None, last_balance_verified=None)
        run_at = self._clock() if at is None else at
        run_count = current.run_count + 1
        self._commit(replace(current, run_count=run_count, last_run_at=run_at))
        return run_count

    def record_night(self, night: NightRecord) -> None:
        """Record a finished walk's decision and complete the day only when exhausted.

        ``begin_run`` counts every attempt, including a manual run stopped by its
        budget. Only a run that walked the roster to the end calls this, and the
        day becomes skippable only when that walk left nothing worth retrying.
        A retryable failure therefore keeps ``completed_night`` false so a later
        run in the same day can try the row again.

        The latest finished walk owns the flag: a later run that finds retryable
        work reopens a day an earlier run had completed, so the state and the
        report always describe the same decision.
        """
        current = self._loaded()
        self._commit(replace(current, completed_night=night.complete, last_night=night))

    def is_acted_unverified(self, task_key: str) -> bool:
        """Whether ``task_key`` failed after acting and still needs a re-check."""
        return _task_key(task_key) in self._loaded().acted_unverified

    def mark_acted_unverified(self, task_key: str) -> None:
        """Remember that ``task_key`` acted without proof, so a later run re-reads the row."""
        key = _task_key(task_key)
        current = self._loaded()
        if key in current.acted_unverified:
            return
        keys = tuple(sorted((*current.acted_unverified, key)))
        self._commit(replace(current, acted_unverified=keys))

    def clear_acted_unverified(self, task_key: str) -> None:
        """Forget the marker once the row completed or was proven already done."""
        key = _task_key(task_key)
        current = self._loaded()
        if key not in current.acted_unverified:
            return
        keys = tuple(item for item in current.acted_unverified if item != key)
        self._commit(replace(current, acted_unverified=keys))

    def observe_balance(self, balance: float | None, *, verified: bool = True) -> None:
        """Store the last balance seen, without recording a task completion.

        ``None`` means this attempt could not read a balance. It keeps a
        known-good value, but clears a stored zero without trusted provenance,
        because keeping that would preserve the bug in issue #94. ``verified``
        is the provenance of a zero: true only when the reading came from a
        validated coin page, so a later unreadable run can tell a real new
        account from a legacy misread zero.
        """
        current = self._loaded()
        if balance is None:
            if current.last_balance == 0.0 and current.last_balance_verified is not True:
                self._warn(
                    'balance_zero_unverified_cleared',
                    stored_balance=0.0,
                    detail='the stored zero had no trusted coin-page provenance and this attempt could not read a balance',
                )
                self._commit(replace(current, last_balance=None, last_balance_verified=None))
            return
        self._commit(
            replace(
                current,
                last_balance=_balance(balance),
                last_balance_verified=verified,
            ),
        )

    def snapshot(self) -> DailySnapshot:
        """Give #31 the whole day: counters, balance, run count and timestamp."""
        return self._loaded()

    def reload(self) -> None:
        """Drop the cached snapshot so the next query reads the file again.

        A long-lived scheduler caches the day after its first completed-night
        check. Another process (a manual run, or a second daemon) can change the
        file while that scheduler waits for the run lock, so the post-lock
        re-check must not reuse the cache. Ordinary state updates stay in memory
        because they are this store's own writes.
        """
        self._day = None
        self._state = None

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

    acted_raw = data.get('acted_unverified', [])
    if not isinstance(acted_raw, list):
        raise _CorruptState("'acted_unverified' must be a JSON array")
    acted_unverified: list[str] = []
    for value in acted_raw:
        if not isinstance(value, str):
            raise _CorruptState("'acted_unverified' entries must be strings")
        key = value.strip()
        if not key or value != key:
            raise _CorruptState("'acted_unverified' entries must be non-empty without surrounding whitespace")
        if key in acted_unverified:
            raise _CorruptState(f"'acted_unverified' contains duplicate key {key!r}")
        acted_unverified.append(key)

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

    last_balance_verified_raw = data.get('last_balance_verified')
    if last_balance_verified_raw is None:
        last_balance_verified = None
    elif isinstance(last_balance_verified_raw, bool):
        last_balance_verified = last_balance_verified_raw
    else:
        raise _CorruptState("'last_balance_verified' must be a boolean or null")
    if last_balance is None:
        last_balance_verified = None

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

    last_night = _parse_night(data.get('last_night'))

    return DailySnapshot(
        day=day,
        completions=completions,
        last_balance=last_balance,
        last_balance_verified=last_balance_verified,
        run_count=run_count,
        last_run_at=last_run_at,
        completed_night=completed_night,
        acted_unverified=tuple(acted_unverified),
        last_night=last_night,
    )


def _parse_night(raw: Any) -> NightRecord | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _CorruptState("'last_night' must be a JSON object or null")
    data = cast(dict[str, Any], raw)

    outcome_raw = _field(data, 'outcome')
    if not isinstance(outcome_raw, str):
        raise _CorruptState("'last_night.outcome' must be a string")
    try:
        outcome = NightOutcome(outcome_raw)
    except ValueError as exc:
        raise _CorruptState(f"'last_night.outcome' is not a known outcome: {outcome_raw!r}") from exc

    return NightRecord(
        outcome=outcome,
        dispatched=_night_count(data, 'dispatched'),
        completed=_night_count(data, 'completed'),
        retryable_failures=_night_count(data, 'retryable_failures'),
    )


def _night_count(data: Mapping[str, Any], key: str) -> int:
    value = _field(data, key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _CorruptState(f"'last_night.{key}' must be a non-negative integer")
    return value


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
