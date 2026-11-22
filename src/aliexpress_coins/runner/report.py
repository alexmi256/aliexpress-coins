"""The minimal run report every night leaves behind (issue #29, extended by #31).

The report is a value object plus two atomic writes. ``run.json`` is the
machine-readable twin and ``summary.md`` is the human table; both live in the
run directory that also holds the roster observation log. Writing the pair is
deliberately boring: each file is staged and renamed, so a kill mid-write leaves
the previous complete file, never a truncated one.

The shape is intentionally small: what ran, what was skipped and why, the
outcomes, and how much of the wall-clock budget was used. Notifications and the
richer per-frame detail belong to #31.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from aliexpress_coins.perception.artifacts import atomic_write_bytes
from aliexpress_coins.runner.errors import ReportError

__all__ = [
    'RUN_REPORT_SCHEMA',
    'RunReport',
    'RunStatus',
    'SkippedRecord',
    'TaskRecord',
]

RUN_REPORT_SCHEMA = 1
"""The on-disk schema this module reads or writes."""


class RunStatus(StrEnum):
    """How one night ended at the run level."""

    RUNNING = 'running'
    COMPLETED = 'completed'
    BUDGET = 'budget'
    STOPPED = 'stopped'
    FAILED = 'failed'


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """One dispatched row and the executor's own result."""

    title: str
    key: str
    category: str
    outcome: str
    verification: str
    detail: str
    duration_seconds: float
    completed: bool
    result: dict[str, Any]

    def as_dict(self) -> dict[str, object]:
        """Render the task for ``run.json`` without dropping the evidence."""
        return {
            'title': self.title,
            'key': self.key,
            'category': self.category,
            'outcome': self.outcome,
            'verification': self.verification,
            'detail': self.detail,
            'duration_seconds': self.duration_seconds,
            'completed': self.completed,
            'result': self.result,
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
        self.returned_to_coins_page: bool | None = None
        self.return_error: str | None = None
        self.error: str | None = None
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
    def ok(self) -> bool:
        """Whether the run itself succeeded, even if a task did not."""
        return self.status in (RunStatus.COMPLETED, RunStatus.BUDGET)

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
    ) -> TaskRecord:
        """Append one dispatched task."""
        record = TaskRecord(
            title=title,
            key=key,
            category=category,
            outcome=outcome,
            verification=verification,
            detail=detail,
            duration_seconds=duration_seconds,
            completed=completed,
            result=result,
        )
        self.tasks.append(record)
        return record

    def skip(self, title: str, *, key: str | None, category: str | None, reason: str, detail: str) -> None:
        """Record a row that was not dispatched; duplicate reason/title pairs collapse."""
        identity = (title, reason)
        if identity in self._skip_keys:
            return
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
            'returned_to_coins_page': self.returned_to_coins_page,
            'return_error': self.return_error,
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
            f'- **Device:** {_device_summary(self.hygiene)}',
            f'- **Geometry:** {_geometry_summary(self.geometry)}',
            f'- **Surface:** {self.surface or "unknown"}',
            f'- **Roster:** {_roster_summary(self.roster)}',
            f'- **Observations:** {self.observations} (`roster.jsonl`)',
            f'- **Balance:** {_balance(self.balance_before)} -> {_balance(self.balance_after)}',
            f'- **Back on coins page:** {_returned(self.returned_to_coins_page, self.return_error)}',
        ]
        if self.detail:
            lines.append(f'- **Detail:** {self.detail}')
        if self.route_error:
            lines.append(f'- **Route fallback:** {self.route_error}')
        if self.error:
            lines.append(f'- **Run error:** {self.error}')
        lines.extend(('', '## Tasks', ''))
        if self.tasks:
            lines.extend(('| # | task | category | outcome | verification | detail |', '|---|---|---|---|---|---|'))
            for index, task in enumerate(self.tasks, start=1):
                lines.append(
                    f'| {index} | {_cell(task.title)} | {_cell(task.category)} | {_cell(task.outcome)} | {_cell(task.verification)} | {_cell(task.detail)} |',
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


def _cell(value: str) -> str:
    return value.replace('|', '\\|').replace('\n', ' ')


def _balance(value: float | None) -> str:
    return 'unknown' if value is None else f'{value:g}'


def _device_summary(hygiene: dict[str, Any] | None) -> str:
    if not hygiene:
        return 'not prepared'
    return f'{hygiene.get("serial", "?")} ({hygiene.get("state", "?")})'


def _geometry_summary(geometry: dict[str, Any] | None) -> str:
    if not geometry:
        return 'not captured'
    return (
        f'{geometry.get("width", "?")}x{geometry.get("height", "?")} '
        f'@ {geometry.get("density", "?")} dpi (sdk {geometry.get("sdk", "?")})'
    )


def _roster_summary(roster: dict[str, Any] | None) -> str:
    if not roster:
        return 'not read'
    return (
        f'{roster.get("status", "?")}; {roster.get("count", 0)} row(s), '
        f'{roster.get("allowed_count", 0)} allowed, {roster.get("denied_count", 0)} refused'
    )


def _returned(returned: bool | None, error: str | None) -> str:
    if returned is None:
        return 'not attempted'
    if returned:
        return 'yes'
    return f'no ({error or "the route did not prove the page"})'
