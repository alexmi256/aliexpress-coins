"""The minimal run report every night leaves behind (issue #29, extended by #31).

The report is a value object plus two atomic writes. ``run.json`` is the
machine-readable twin and ``summary.md`` is the human table; both live in the
run directory that also holds the roster observation log. Writing the pair is
deliberately boring: each file is staged and renamed, so a kill mid-write leaves
the previous complete file, never a truncated one.

The shape is intentionally small: what ran, what was skipped and why, the
outcomes, the budget, the popup families closed and the balance frames the
summary was read from. Delivery lives in ``runner/notify.py``; #40 replaces that
transport with Apprise without changing the report contract.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from aliexpress_coins.perception.artifacts import atomic_write_bytes
from aliexpress_coins.runner.errors import ReportError
from aliexpress_coins.runner.failures import Failure
from aliexpress_coins.runner.state import NightOutcome

__all__ = [
    'RUN_REPORT_SCHEMA',
    'RunEventLog',
    'RunReport',
    'RunStatus',
    'SkippedRecord',
    'TaskRecord',
    'TaskState',
    'prune_runs',
]

RUN_REPORT_SCHEMA = 5
"""The on-disk schema this module reads or writes; 5 adds the night completion record."""

_RUN_ID_PATTERN = re.compile(r'^\d{8}T\d{6}Z$')
"""The only directory names retention may delete."""


class RunStatus(StrEnum):
    """How one night ended at the run level."""

    RUNNING = 'running'
    COMPLETED = 'completed'
    BUDGET = 'budget'
    STOPPED = 'stopped'
    PLANNED = 'planned'
    ABORTED = 'aborted'
    FAILED = 'failed'


class TaskState(StrEnum):
    """The two run-controlled states; a finished row mirrors its executor outcome.

    A terminal ``TaskRecord.state`` is the executor's own outcome string (for
    example ``completed``, ``already_done`` or ``failed``), which is deliberately
    an open set rather than a member of this enum.
    """

    IN_FLIGHT = 'in_flight'
    ABORTED = 'aborted'


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """One dispatched row, from its dispatch until its terminal outcome."""

    title: str
    key: str
    category: str
    state: str
    outcome: str
    verification: str
    detail: str
    duration_seconds: float
    completed: bool
    result: dict[str, Any]
    failure: dict[str, Any] | None = None

    @property
    def code(self) -> str | None:
        """The stable failure code, or ``None`` for a completed/skipped task."""
        if self.failure is None:
            return None
        code = self.failure.get('code')
        return str(code) if code is not None else None

    def as_dict(self) -> dict[str, object]:
        """Render the task for ``run.json`` without dropping the evidence."""
        return {
            'title': self.title,
            'key': self.key,
            'category': self.category,
            'state': self.state,
            'code': self.code,
            'outcome': self.outcome,
            'verification': self.verification,
            'detail': self.detail,
            'duration_seconds': self.duration_seconds,
            'completed': self.completed,
            'result': self.result,
            'failure': self.failure,
        }


@dataclass(frozen=True, slots=True)
class SkippedRecord:
    """A row that was not dispatched, and the reason a report can group."""

    title: str
    key: str | None
    category: str | None
    reason: str
    detail: str

    def as_dict(self) -> dict[str, object]:
        """Render the skip for ``run.json``."""
        return {
            'title': self.title,
            'key': self.key,
            'category': self.category,
            'reason': self.reason,
            'detail': self.detail,
        }


def _validate_run_id(run_id: str) -> str:
    cleaned = run_id.strip()
    if not cleaned or cleaned in {'.', '..'} or Path(cleaned).name != cleaned or '/' in cleaned or '\\' in cleaned:
        raise ReportError(f'run id must be a single path component: {run_id!r}')
    return cleaned


class RunReport:
    """One night's report, written whole and atomically on every update."""

    def __init__(
        self,
        runs_dir: str | Path,
        run_id: str,
        *,
        budget_seconds: float,
        started_at: datetime,
    ) -> None:
        if budget_seconds <= 0:
            raise ValueError(f'budget_seconds must be positive, got {budget_seconds!r}')
        self.runs_dir = Path(runs_dir)
        self.run_id = _validate_run_id(run_id)
        self.budget_seconds = budget_seconds
        self.started_at = started_at
        self.finished_at: datetime | None = None
        self.status = RunStatus.RUNNING
        self.detail: str | None = None
        self.budget_used_seconds = 0.0
        self.hygiene: dict[str, Any] | None = None
        self.geometry: dict[str, Any] | None = None
        self.surface: str | None = None
        self.roster: dict[str, Any] | None = None
        self.route_error: str | None = None
        self.observations = 0
        self.balance_before: float | None = None
        self.balance_after: float | None = None
        self.balance_note: str | None = None
        self.returned_to_coins_page: bool | None = None
        self.return_error: str | None = None
        self.error: str | None = None
        self.failure: dict[str, Any] | None = None
        self.popups_closed: dict[str, int] = {}
        self.shots: dict[str, str] = {}
        self.notification: dict[str, Any] | None = None
        self.plan: dict[str, Any] | None = None
        self.plan_note: str | None = None
        self.abort: dict[str, Any] | None = None
        self.night: dict[str, Any] | None = None
        self.passes = 0
        self.max_passes: int | None = None
        self.max_task_attempts: int | None = None
        self.attempts: dict[str, int] = {}
        self.blacklist: dict[str, int] = {}
        self.artifact_errors: list[str] = []
        self.tasks: list[TaskRecord] = []
        self.skipped: list[SkippedRecord] = []
        self._skip_keys: set[tuple[str, str]] = set()

    @classmethod
    def for_run(
        cls,
        runs_dir: str | Path,
        *,
        budget_seconds: float,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> RunReport:
        """Create a report for a fresh UTC-timestamped run."""
        now = clock()
        return cls(
            runs_dir,
            now.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ'),
            budget_seconds=budget_seconds,
            started_at=now,
        )

    @property
    def run_dir(self) -> Path:
        """The directory that holds this run's artifacts."""
        return self.runs_dir / self.run_id

    @property
    def json_path(self) -> Path:
        """The machine-readable report."""
        return self.run_dir / 'run.json'

    @property
    def summary_path(self) -> Path:
        """The human report."""
        return self.run_dir / 'summary.md'

    @property
    def roster_log_path(self) -> Path:
        """Where the roster observer writes its JSONL timeline."""
        return self.run_dir / 'roster.jsonl'

    @property
    def event_log_path(self) -> Path:
        """The structured process log for this run."""
        return self.run_dir / 'run.jsonl'

    @property
    def shots_dir(self) -> Path:
        """Where the artifact store writes frames and OCR sidecars."""
        return self.run_dir / 'shots'

    @property
    def balance_delta(self) -> float | None:
        """The change the two recorded balance frames prove, when both are readable."""
        if self.balance_before is None or self.balance_after is None:
            return None
        return self.balance_after - self.balance_before

    @property
    def ok(self) -> bool:
        """Whether the run itself succeeded, even if a task did not."""
        return self.status in (RunStatus.COMPLETED, RunStatus.BUDGET, RunStatus.PLANNED)

    def record_shot(self, name: str, image: Path) -> None:
        """Remember one frame the artifact store wrote, relative to the run."""
        try:
            relative = image.relative_to(self.run_dir)
        except ValueError:
            relative = image
        self.shots[name] = str(relative)

    def record_popups(self, families: Iterable[str]) -> None:
        """Count verified popup closures by family across the run."""
        for family in families:
            self.popups_closed[family] = self.popups_closed.get(family, 0) + 1

    def record_artifact_error(self, detail: str) -> None:
        """Keep a best-effort artifact failure visible without failing the run."""
        self.artifact_errors.append(detail)

    def record_failure(self, failure: Failure) -> None:
        """Name the run-level failure once; the code is the report contract."""
        self.failure = failure.as_dict()

    @property
    def failure_code(self) -> str | None:
        """The stable run-level code, if this run failed or hit a budget."""
        if self.failure is None:
            return None
        code = self.failure.get('code')
        return str(code) if code is not None else None

    def begin_task(self, *, title: str, key: str, category: str) -> int:
        """Record a dispatched row before its executor has an outcome.

        Returns the row's index so :meth:`finish_task` can rewrite it in place.
        A report written while the task is running (or killed before it ends)
        therefore shows ``in_flight`` rather than pretending nothing was
        dispatched.
        """
        self.tasks.append(
            TaskRecord(
                title=title,
                key=key,
                category=category,
                state=TaskState.IN_FLIGHT.value,
                outcome=TaskState.IN_FLIGHT.value,
                verification='',
                detail='the executor is running; no outcome has been recorded yet',
                duration_seconds=0.0,
                completed=False,
                result={},
            ),
        )
        return len(self.tasks) - 1

    def finish_task(
        self,
        index: int,
        *,
        outcome: str,
        verification: str,
        detail: str,
        duration_seconds: float,
        completed: bool,
        result: dict[str, Any],
        failure: Failure | None = None,
    ) -> TaskRecord:
        """Rewrite one in-flight row with the executor's terminal outcome."""
        pending = self.tasks[index]
        record = TaskRecord(
            title=pending.title,
            key=pending.key,
            category=pending.category,
            state=outcome,
            outcome=outcome,
            verification=verification,
            detail=detail,
            duration_seconds=duration_seconds,
            completed=completed,
            result=result,
            failure=None if failure is None else failure.as_dict(),
        )
        self.tasks[index] = record
        return record

    def record_task(
        self,
        *,
        title: str,
        key: str,
        category: str,
        outcome: str,
        verification: str,
        detail: str,
        duration_seconds: float,
        completed: bool,
        result: dict[str, Any],
        failure: Failure | None = None,
    ) -> TaskRecord:
        """Append one already-dispatched task, with its class when it did not verify."""
        record = TaskRecord(
            title=title,
            key=key,
            category=category,
            state=outcome,
            outcome=outcome,
            verification=verification,
            detail=detail,
            duration_seconds=duration_seconds,
            completed=completed,
            result=result,
            failure=None if failure is None else failure.as_dict(),
        )
        self.tasks.append(record)
        return record

    def abort_in_flight(
        self,
        *,
        code: str,
        reason: str,
        failure: Failure | None = None,
        screen: dict[str, Any] | None = None,
        duration_seconds: float = 0.0,
    ) -> bool:
        """Mark every task still running as aborted, with the screen that stopped it.

        A run that aborts mid-task must not leave an ``in_flight`` row behind in
        its final report, and it must not hide that row either. The abort reason,
        code and screen are copied into the task's own row so the tasks table,
        the ``abort`` block and the event stream name the same thing.
        """
        changed = False
        detail = reason
        if screen is not None:
            detail = f'{reason}; screen={_screen_summary(screen)}'
        for index, task in enumerate(self.tasks):
            if task.state != TaskState.IN_FLIGHT.value:
                continue
            result = dict(task.result)
            abort_record: dict[str, Any] = {'code': code, 'reason': reason}
            if screen is not None:
                abort_record['screen'] = screen
            result['abort'] = abort_record
            self.tasks[index] = TaskRecord(
                title=task.title,
                key=task.key,
                category=task.category,
                state=TaskState.ABORTED.value,
                outcome=TaskState.ABORTED.value,
                verification=code,
                detail=detail,
                duration_seconds=max(0.0, duration_seconds),
                completed=False,
                result=result,
                failure=None if failure is None else failure.as_dict(),
            )
            changed = True
        return changed

    def skip(self, title: str, *, key: str | None, category: str | None, reason: str, detail: str) -> bool:
        """Record a row that was not dispatched; duplicate reason/title pairs collapse.

        Returns true only when a new skip was appended, so the caller can log the
        event once without repeating it on every roster observation.
        """
        identity = (title, reason)
        if identity in self._skip_keys:
            return False
        self._skip_keys.add(identity)
        self.skipped.append(
            SkippedRecord(
                title=title,
                key=key,
                category=category,
                reason=reason,
                detail=detail,
            ),
        )
        return True

    def finish(
        self,
        status: RunStatus,
        *,
        detail: str | None = None,
        budget_used_seconds: float,
        finished_at: datetime | None = None,
    ) -> None:
        """Stamp the terminal status before the final write."""
        self.status = status
        self.detail = detail
        self.budget_used_seconds = max(0.0, budget_used_seconds)
        self.finished_at = finished_at if finished_at is not None else datetime.now(UTC)

    def as_dict(self) -> dict[str, Any]:
        """The complete ``run.json`` payload."""
        return {
            'schema': RUN_REPORT_SCHEMA,
            'run_id': self.run_id,
            'status': self.status.value,
            'detail': self.detail,
            'started_at': self.started_at.isoformat(),
            'finished_at': None if self.finished_at is None else self.finished_at.isoformat(),
            'budget': {
                'seconds': self.budget_seconds,
                'used_seconds': self.budget_used_seconds,
            },
            'hygiene': self.hygiene,
            'geometry': self.geometry,
            'surface': self.surface,
            'roster': self.roster,
            'route_error': self.route_error,
            'observations': self.observations,
            'roster_log': str(self.roster_log_path),
            'balance_before': self.balance_before,
            'balance_after': self.balance_after,
            'balance_delta': self.balance_delta,
            'balance_note': self.balance_note,
            'popups_closed': dict(self.popups_closed),
            'shots': dict(self.shots),
            'notification': self.notification,
            'plan': self.plan,
            'plan_note': self.plan_note,
            'abort': self.abort,
            'night': self.night,
            'passes': self.passes,
            'max_passes': self.max_passes,
            'max_task_attempts': self.max_task_attempts,
            'attempts': dict(self.attempts),
            'blacklist': dict(self.blacklist),
            'artifact_errors': list(self.artifact_errors),
            'returned_to_coins_page': self.returned_to_coins_page,
            'return_error': self.return_error,
            'failure': self.failure,
            'error': self.error,
            'tasks': [task.as_dict() for task in self.tasks],
            'skipped': [skip.as_dict() for skip in self.skipped],
        }

    def write(self) -> Path:
        """Write ``run.json`` and ``summary.md`` atomically."""
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            payload = (
                json.dumps(self.as_dict(), ensure_ascii=False, indent=2, sort_keys=True, default=str) + '\n'
            ).encode(
                'utf-8',
            )
            atomic_write_bytes(self.json_path, payload)
            atomic_write_bytes(self.summary_path, self._summary().encode('utf-8'))
        except OSError as exc:
            raise ReportError(f'could not write the run report in {self.run_dir}: {exc}') from exc
        return self.json_path

    def _summary(self) -> str:
        finished = 'still running' if self.finished_at is None else self.finished_at.isoformat()
        lines = [
            f'# Run {self.run_id}',
            '',
            f'- **Status:** {self.status.value}',
            f'- **Started:** {self.started_at.isoformat()}',
            f'- **Finished:** {finished}',
            f'- **Budget:** {self.budget_used_seconds:.1f}s used of {self.budget_seconds:.1f}s',
            f'- **Passes:** {self.passes} of {_bound(self.max_passes)}',
            f'- **Attempts per task:** {_attempts_summary(self.attempts, self.max_task_attempts)}',
            f'- **Blacklist:** {_blacklist_summary(self.blacklist)}',
            f'- **Device:** {_device_summary(self.hygiene)}',
            f'- **Geometry:** {_geometry_summary(self.geometry)}',
            f'- **Surface:** {self.surface or "unknown"}',
            f'- **Route:** {_route_summary(self.roster)}',
            f'- **Roster:** {_roster_summary(self.roster)}',
            f'- **Observations:** {self.observations} (`roster.jsonl`)',
            f'- **Balance:** {_balance_delta(self.balance_before, self.balance_after, self.balance_delta)}',
            f'- **Popups closed:** {_popup_summary(self.popups_closed)}',
            f'- **Back on coins page:** {_returned(self.returned_to_coins_page, self.return_error)}',
        ]
        if self.balance_note:
            lines.append(f'- **Balance note:** {self.balance_note}')
        if self.detail:
            lines.append(f'- **Detail:** {self.detail}')
        if self.night is not None:
            lines.append(f'- **Night:** {_night_summary(self.night)}')
        if self.route_error:
            lines.append(f'- **Route fallback:** {self.route_error}')
        if self.plan is not None:
            lines.append(f'- **Plan (dry run):** {_plan_summary(self.plan)}')
        if self.plan_note:
            lines.append(f'- **Plan note:** {self.plan_note}')
        if self.abort is not None:
            lines.append(f'- **Abort:** {_abort_summary(self.abort)}')
        if self.failure is not None:
            lines.append(f'- **Failure:** {_failure_summary(self.failure)}')
        if self.error:
            lines.append(f'- **Run error:** {self.error}')
        if self.artifact_errors:
            lines.append(f'- **Artifact errors:** {"; ".join(self.artifact_errors)}')
        if self.notification is not None:
            lines.append(f'- **Notification:** {_notification_summary(self.notification)}')
        lines.extend(('', '## Tasks', ''))
        if self.tasks:
            lines.extend(
                (
                    '| # | task | key | code | outcome | evidence | seconds | detail |',
                    '|---|---|---|---|---|---|---|---|',
                ),
            )
            for index, task in enumerate(self.tasks, start=1):
                lines.append(
                    f'| {index} | {_cell(task.title)} | {_cell(task.key)} | {_cell(task.code or "")} | '
                    f'{_cell(task.outcome)} | {_cell(task.verification)} | {task.duration_seconds:.1f} | '
                    f'{_cell(task.detail)} |',
                )
        else:
            lines.append('No task was dispatched.')
        lines.extend(('', '## Skipped', ''))
        if self.skipped:
            lines.extend(('| task | reason | detail |', '|---|---|---|'))
            for skip in self.skipped:
                lines.append(
                    f'| {_cell(skip.title)} | {_cell(skip.reason)} | {_cell(skip.detail)} |',
                )
        else:
            lines.append('Nothing was skipped.')
        lines.append('')
        return '\n'.join(lines)


class RunEventLog(logging.Handler):
    """Append every runner event to ``run.jsonl`` as one JSON object per line.

    The engine's log messages are already JSON documents, so they are written
    unchanged with level/logger/time added when absent. Anything else is wrapped
    rather than dropped. A log write failure is reported through the logging
    machinery and never interrupts the run.
    """

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)

    def record(self, payload: dict[str, Any]) -> None:
        """Write one caller-built event; used for the first line of a run."""
        self._write(payload)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            try:
                payload = json.loads(message)
            except TypeError, json.JSONDecodeError:
                payload = {'event': 'log', 'message': message}
            if not isinstance(payload, dict):
                payload = {'event': 'log', 'value': payload}
            payload.setdefault('level', record.levelname.lower())
            payload.setdefault('logger', record.name)
            payload.setdefault('time', datetime.fromtimestamp(record.created, UTC).isoformat())
            self._write(payload)
        except Exception:
            self.handleError(record)

    def _write(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), sort_keys=True, default=str)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('a', encoding='utf-8') as stream:
            stream.write(line + '\n')


def prune_runs(
    runs_dir: str | Path,
    *,
    keep: int,
    current_run_id: str,
    logger: logging.Logger | None = None,
) -> tuple[Path, ...]:
    """Delete the oldest run directories so ``keep`` remain, including this run.

    The current run is excluded before pruning, so it is never a candidate even
    when its id sorts before an existing directory. Only directory names in the
    UTC run-id shape are candidates; files, symlinks and operator directories
    such as ``manual`` are left alone and logged as skipped.
    """
    if keep < 1:
        raise ValueError(f'keep must be positive, got {keep!r}')
    root = Path(runs_dir)
    directories: list[Path] = []
    skipped: list[str] = []
    try:
        for path in root.iterdir():
            if path.name == current_run_id:
                continue
            if path.is_dir() and not path.is_symlink() and _RUN_ID_PATTERN.fullmatch(path.name):
                directories.append(path)
            else:
                skipped.append(path.name)
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise ReportError(f'could not list run directories in {root}: {exc}') from exc

    log = logger if logger is not None else logging.getLogger(__name__)
    if skipped:
        log.info(
            '%s',
            json.dumps(
                {'event': 'runs_not_pruned', 'names': sorted(skipped)},
                separators=(',', ':'),
                sort_keys=True,
            ),
        )

    directories.sort(key=lambda path: path.name)
    excess = len(directories) - (keep - 1)
    if excess <= 0:
        return ()

    pruned: list[Path] = []
    failures: list[str] = []
    for path in directories[:excess]:
        try:
            shutil.rmtree(path)
        except OSError as exc:
            failures.append(f'{path}: {exc}')
        else:
            pruned.append(path)
    if failures:
        raise ReportError(f'could not prune every old run: {"; ".join(failures)}')
    return tuple(pruned)


def _screen_summary(screen: dict[str, Any]) -> str:
    labels = screen.get('labels') or []
    shown = ', '.join(str(label) for label in labels[:4]) or 'no readable labels'
    return f'{screen.get("state", "?")}/{screen.get("surface", "?")}: {shown}'


def _cell(value: str) -> str:
    return value.replace('|', '\\|').replace('\n', ' ')


def _bound(value: int | None) -> str:
    return 'an unbounded walk' if value is None else str(value)


def _attempts_summary(attempts: dict[str, int], limit: int | None) -> str:
    if not attempts:
        return 'none'
    listed = ', '.join(f'{key}: {count}' for key, count in sorted(attempts.items()))
    return listed if limit is None else f'{listed} (blacklist after {limit} failed attempt(s) per task)'


def _blacklist_summary(blacklist: dict[str, int]) -> str:
    if not blacklist:
        return 'none'
    return ', '.join(f'{key}: blacklisted after {count} attempt(s)' for key, count in sorted(blacklist.items()))


def _balance(value: float | None) -> str:
    return 'unknown' if value is None else f'{value:g}'


def _balance_delta(before: float | None, after: float | None, delta: float | None) -> str:
    line = f'{_balance(before)} -> {_balance(after)}'
    return line if delta is None else f'{line} ({delta:+g})'


def _popup_summary(closed: dict[str, int]) -> str:
    if not closed:
        return 'none'
    return ', '.join(f'{family}: {count}' for family, count in sorted(closed.items()))


def _failure_summary(failure: dict[str, Any]) -> str:
    code = failure.get('code', 'unknown')
    sentence = failure.get('sentence', '')
    detail = failure.get('detail') or failure.get('error')
    text = f'{code} — {sentence}' if sentence else str(code)
    return text if not detail else f'{text} ({detail})'


def _route_summary(roster: dict[str, Any] | None) -> str:
    if not roster:
        return 'not read'
    route = roster.get('route') or 'unknown'
    taken = roster.get('routes_taken') or []
    taken_text = ', '.join(str(item) for item in taken) if taken else 'none recorded'
    return f'{route} (tried: {taken_text})'


def _notification_summary(notification: dict[str, Any]) -> str:
    if notification.get('ok'):
        return f'delivered via {notification.get("sink", "unknown")}'
    if not notification.get('attempted', False):
        return f'not attempted ({notification.get("reason", "not requested")})'
    return f'failed via {notification.get("sink", "unknown")}: {notification.get("error", "unknown error")}'


def _plan_summary(plan: dict[str, Any]) -> str:
    steps = plan.get('steps') or []
    refusals = plan.get('refusals') or []
    detail = plan.get('detail') or 'no plan was produced'
    return f'{detail} (surface: {plan.get("surface", "unknown")}, {len(steps)} step(s), {len(refusals)} refusal(s))'


def _night_summary(night: dict[str, Any]) -> str:
    """The one line that explains whether the day is complete and why."""
    outcome = night.get('outcome', 'unknown')
    dispatched = night.get('dispatched', 0)
    completed = night.get('completed', 0)
    retryable = night.get('retryable_failures', 0)
    if outcome == NightOutcome.EXHAUSTED.value:
        return (
            'complete — the roster was walked and nothing dispatched is worth retrying '
            f'({completed}/{dispatched} task(s) completed)'
        )
    if outcome == NightOutcome.INCOMPLETE_ROSTER.value:
        return 'open — the roster read was incomplete; the day is not complete and a later run may retry'
    return (
        f'open — {retryable} retryable task failure(s) of {dispatched} dispatch(es) remain; '
        'the day is not complete and a later run may retry'
    )


def _abort_summary(abort: dict[str, Any]) -> str:
    code = abort.get('code', 'unknown')
    reason = abort.get('reason', 'no reason recorded')
    screen = abort.get('screen') or {}
    return f'{code} — {reason} (screen: {_screen_summary(screen)})'


def _device_summary(hygiene: dict[str, Any] | None) -> str:
    if not hygiene:
        return 'not prepared'
    sleep = '; was asleep' if hygiene.get('was_asleep') else ''
    awake = '; kept awake' if hygiene.get('kept_awake') else ''
    return f'{hygiene.get("serial", "?")} ({hygiene.get("state", "?")}{sleep}{awake})'


def _geometry_summary(geometry: dict[str, Any] | None) -> str:
    if not geometry:
        return 'not captured'
    return (
        f'{geometry.get("width", "?")}x{geometry.get("height", "?")} '
        f'@ {geometry.get("density", "?")} dpi (sdk {geometry.get("sdk", "?")}, '
        f'{geometry.get("orientation", "?")}, profile {geometry.get("profile_id", "?")})'
    )


def _roster_summary(roster: dict[str, Any] | None) -> str:
    if not roster:
        return 'not read'
    summary = (
        f'{roster.get("status", "?")}; {roster.get("count", 0)} row(s), '
        f'{roster.get("allowed_count", 0)} allowed, {roster.get("denied_count", 0)} refused'
    )
    warning = roster.get('warning')
    return summary if not warning else f'{summary}; warning: {warning}'


def _returned(returned: bool | None, error: str | None) -> str:
    if returned is None:
        return 'not attempted'
    if returned:
        return 'yes'
    return f'no ({error or "the route did not prove the page"})'
