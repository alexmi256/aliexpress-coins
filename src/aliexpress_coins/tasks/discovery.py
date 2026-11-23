"""Open the task list and turn the visible rows into an ordered roster (issue #22).

Discovery has two routes and records which one it took:

* **boost** — ``Earn more`` on the coin page opens the ``Coins boost`` page. The
  page is the fuller list, so it is preferred whenever the label is present.
* **inline** — when ``Earn more`` is absent, the task rows already rendered on
  the coin page are used instead. Missing ``Earn more`` is a normal page
  variant, not a failure.

The roster is a set of *decisions*, not a filtered list. Every task row that has
an action is kept, with the catalogue's allow/deny answer and its reason. A
denied row still records the action it was rendered with for diagnostics, but
:attr:`RosterEntry.dispatch_target` is ``None`` so an executor cannot be handed
a target for it.

The empty ``Coins boost`` page (``There's nothing here yet``) is an arrival, not
a timeout. :func:`discover_roster` applies the #38 route policy: one shortcut
route, at most one warm re-entry when the surface has no usable rows, then
``nothing available``. The variant and every route fired are kept on
:class:`Roster`, ready for the #31 run report.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from aliexpress_coins.control.controller import Controller, TapRecord
from aliexpress_coins.control.popups import DismissReport, PopupCorpus, _matching_family, dismiss_after
from aliexpress_coins.perception.errors import OcrError, SnapshotError, StateTimeout
from aliexpress_coins.perception.layout import (
    DEFAULT_ACTION_LABELS,
    ProgressBadge,
    Row,
    find_action_in_row,
    find_progress_badge,
    group_rows,
    parse_progress_badge,
)
from aliexpress_coins.perception.matching import (
    DEFAULT_MIN_SCORE,
    find_text,
    normalize,
    similarity,
)
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.state import (
    CaptureSource,
    Classification,
    Observation,
    ScreenState,
    classify,
)
from aliexpress_coins.perception.templates import TemplateStore
from aliexpress_coins.tasks.catalogue import (
    DEFAULT_DENY_REASON,
    AllowedTaskSpec,
    DecisionKind,
    DispatchAuthorization,
    TaskCatalogue,
    TaskCategory,
    TaskDecision,
)
from aliexpress_coins.tasks.errors import RosterLogError
from aliexpress_coins.tasks.surface import (
    BOOST_MARKER,
    EMPTY_BOOST_MARKER,
    SURFACE_REFRESH_LIMIT,
    RouteFirer,
    Surface,
    SurfaceRoute,
    classify_surface,
    has_task_rows,
    is_boost_page,
    is_empty_boost,
)

__all__ = [
    'BOOST_MARKER',
    'DEFAULT_BOOST_INTERVAL',
    'DEFAULT_BOOST_TIMEOUT',
    'DEFAULT_OBSERVATION_CAP',
    'EARN_MORE_LABEL',
    'EMPTY_BOOST_MARKER',
    'ROSTER_LOG_NAME',
    'ROSTER_OBSERVATION_SCHEMA',
    'BoostList',
    'BoostOpenResult',
    'CoinPageWait',
    'DiscoveryResult',
    'DiscoveryRoute',
    'Roster',
    'RosterChange',
    'RosterDiff',
    'RosterEntry',
    'RosterLog',
    'RosterObservation',
    'RosterObserver',
    'RosterRow',
    'RosterStatus',
    'diff_rosters',
    'discover_roster',
    'enumerate_roster',
    'is_boost_page',
    'is_empty_boost',
    'observe_roster',
    'open_boost_list',
    'read_roster_log',
    'wait_for_coin_page',
]

EARN_MORE_LABEL = 'Earn more'
"""The control that opens the full task list from the coin page."""

DEFAULT_BOOST_TIMEOUT = 30.0
"""How long to wait for the boost destination after tapping ``Earn more``."""

DEFAULT_BOOST_INTERVAL = 1.0
"""How often to capture while waiting for the boost destination."""

ROSTER_OBSERVATION_SCHEMA = 1
"""The schema written on every ``roster.jsonl`` line."""

ROSTER_LOG_NAME = 'roster.jsonl'
"""The append-only observation log inside one run directory."""

DEFAULT_OBSERVATION_CAP = 13
"""The default number of re-enumerations a run may make.

The measured ``AECOINS_MAX_TASKS_PER_RUN`` default is 12. One observation per
completed task plus the final capture needs 13, so the cap always leaves room
for that final capture. The cap is a churn guard, not a work budget.
"""

_MAX_TASKS_ENV = 'AECOINS_MAX_TASKS_PER_RUN'
_MAX_OBSERVATIONS_ENV = 'AECOINS_MAX_ROSTER_OBSERVATIONS'

_REWARD = re.compile(r'^\s*[+\uff0b]\s*\d')
_REWARD_WORDS = frozenset({'coin', 'coins'})


class DiscoveryRoute(StrEnum):
    """Which surface the roster came from."""

    BOOST = 'boost'
    INLINE = 'inline'


class RosterStatus(StrEnum):
    """Whether the roster contains anything worth dispatching."""

    AVAILABLE = 'available'
    NOTHING_AVAILABLE = 'nothing available'


@dataclass(frozen=True, slots=True)
class RosterEntry:
    """One task row and the policy decision attached to it.

    ``action`` is the row's own control box. ``dispatch_target`` is deliberately
    the only target an executor should read: it is ``None`` for a denied entry,
    so a refused row cannot be tapped by accident.
    """

    title: str
    subtitle: str | None
    action: TextBox | None
    progress: ProgressBadge | None
    decision: TaskDecision
    row: Row
    authorization: DispatchAuthorization | None = None

    @property
    def allowed(self) -> bool:
        """Whether the catalogue allows this row."""
        return self.decision.allowed

    @property
    def denied(self) -> bool:
        """Whether the catalogue refused this row."""
        return self.decision.denied

    @property
    def kind(self) -> DecisionKind:
        """The allow/deny decision for this row."""
        return self.decision.decision

    @property
    def key(self) -> str | None:
        """The allowed catalogue key, or ``None`` when refused."""
        return self.decision.spec.key if self.decision.spec is not None else None

    @property
    def category(self) -> TaskCategory | None:
        """The allowed task category, or ``None`` when refused."""
        return self.decision.spec.category if self.decision.spec is not None else None

    @property
    def spec(self) -> AllowedTaskSpec | None:
        """The allowed task spec, or ``None`` when refused."""
        return self.decision.spec

    @property
    def rule(self) -> str | None:
        """The catalogue rule that matched, including the default-deny miss."""
        return self.decision.rule

    @property
    def reason(self) -> str | None:
        """The refusal reason, or ``None`` when the row is allowed."""
        return self.decision.reason

    @property
    def deny_reason(self) -> str | None:
        """Alias for :attr:`reason`, in the catalogue's vocabulary."""
        return self.decision.deny_reason

    @property
    def action_target(self) -> TextBox | None:
        """The rendered action box, kept even for a refused row."""
        return self.action

    @property
    def dispatch_target(self) -> TextBox | None:
        """The action an executor may tap: never a refused row's control."""
        return self.action if self.allowed else None

    @property
    def authorized(self) -> bool:
        """Whether the dispatch gate minted a token for exactly this row.

        A roster row is a *candidate*. Only a row that carries a sealed
        :class:`~aliexpress_coins.tasks.catalogue.DispatchAuthorization` may be
        handed to an executor; that is how a bypass fails before a tap.
        """
        token = self.authorization
        return token is not None and token.covers(key=self.key, title=self.title, category=self.category)

    @property
    def dispatchable(self) -> bool:
        """Whether this row is both allowed and has an action to tap."""
        return self.allowed and self.action is not None

    @property
    def progress_text(self) -> str | None:
        """The progress badge exactly as OCR read it, if present."""
        return self.progress.text if self.progress is not None else None

    def as_dict(self) -> dict[str, object]:
        """Render the row for ``run.json`` without losing its decision."""
        record = self.decision.as_dict()
        record.update(
            {
                'row': self.row.text,
                'progress': (
                    {
                        'text': self.progress.text,
                        'completed': self.progress.completed,
                        'total': self.progress.total,
                    }
                    if self.progress is not None
                    else None
                ),
                'action': (
                    {
                        'text': self.action.text,
                        'box': list(self.action.bbox),
                        'target': list(self.action.center),
                    }
                    if self.action is not None
                    else None
                ),
                'dispatchable': self.dispatchable,
                'authorized': self.authorized,
                'authorization': (
                    {
                        'key': self.authorization.key,
                        'category': self.authorization.category.value,
                        'catalogue_version': self.authorization.catalogue_version,
                        'rule': self.authorization.rule,
                        'surface': self.authorization.surface,
                        'sequence': self.authorization.sequence,
                    }
                    if self.authorization is not None
                    else None
                ),
                'dispatch_target': list(self.dispatch_target.center) if self.dispatch_target is not None else None,
            },
        )
        return record


@dataclass(frozen=True, slots=True)
class Roster:
    """The ordered result of reading one task surface."""

    entries: tuple[RosterEntry, ...]
    route: DiscoveryRoute | None = None
    catalogue_version: int | None = None
    default_deny_reason: str = DEFAULT_DENY_REASON
    surface_variant: Surface | None = None
    routes_taken: tuple[SurfaceRoute, ...] = ()
    refresh_attempted: bool = False
    variants_seen: tuple[Surface, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, 'entries', tuple(self.entries))
        object.__setattr__(self, 'routes_taken', tuple(self.routes_taken))
        object.__setattr__(self, 'variants_seen', tuple(self.variants_seen))

    @property
    def tasks(self) -> tuple[RosterEntry, ...]:
        """Alias for :attr:`entries`."""
        return self.entries

    @property
    def count(self) -> int:
        """How many task rows the surface showed."""
        return len(self.entries)

    @property
    def empty(self) -> bool:
        """Whether no task row was found."""
        return not self.entries

    @property
    def nothing_available(self) -> bool:
        """The report wording for an empty surface, including the empty boost page."""
        return not self.entries

    @property
    def status(self) -> RosterStatus:
        """``available`` or ``nothing available``, ready for a report."""
        return RosterStatus.NOTHING_AVAILABLE if self.nothing_available else RosterStatus.AVAILABLE

    @property
    def allowed_entries(self) -> tuple[RosterEntry, ...]:
        """Every row the catalogue allows, in reading order."""
        return tuple(entry for entry in self.entries if entry.allowed)

    @property
    def denied_entries(self) -> tuple[RosterEntry, ...]:
        """Every refused or unrecognised row, in reading order."""
        return tuple(entry for entry in self.entries if entry.denied)

    @property
    def allowed(self) -> tuple[RosterEntry, ...]:
        """Alias for :attr:`allowed_entries`."""
        return self.allowed_entries

    @property
    def denied(self) -> tuple[RosterEntry, ...]:
        """Alias for :attr:`denied_entries`."""
        return self.denied_entries

    @property
    def titles(self) -> tuple[str, ...]:
        """The row titles in reading order."""
        return tuple(entry.title for entry in self.entries)

    @property
    def message(self) -> str:
        """The one-line status a report can print."""
        return self.status.value

    def as_dict(self) -> dict[str, object]:
        """Render the whole catalogue snapshot for the run report."""
        return {
            'route': self.route.value if self.route is not None else None,
            'surface_variant': self.surface_variant.value if self.surface_variant is not None else None,
            'routes_taken': [route.value for route in self.routes_taken],
            'refresh_attempted': self.refresh_attempted,
            'variants_seen': [variant.value for variant in self.variants_seen],
            'status': self.status.value,
            'count': self.count,
            'allowed_count': len(self.allowed_entries),
            'denied_count': len(self.denied_entries),
            'nothing_available': self.nothing_available,
            'catalogue': {
                'version': self.catalogue_version,
                'default_deny_reason': self.default_deny_reason,
            },
            'entries': [entry.as_dict() for entry in self.entries],
        }


@dataclass(frozen=True, slots=True)
class RosterRow:
    """One row as the observation log records it.

    ``key`` is the catalogue key when the row is allowed and ``None`` when it is
    refused. ``badge`` is the progress text exactly as OCR read it. A candidate
    is a row refused by the default-deny rule (``unrecognised``): its raw label
    and subtitle are kept so the next catalogue revision can be built from real
    observations, but its ``dispatch_target`` is always ``None``.
    """

    label: str
    subtitle: str | None
    key: str | None
    category: TaskCategory | None
    badge: str | None
    decision: DecisionKind
    reason: str | None
    action: str | None
    dispatch_target: tuple[float, float] | None = None
    candidate: bool = False

    @classmethod
    def from_entry(cls, entry: RosterEntry) -> RosterRow:
        """Project a roster entry onto the log's stable, small shape."""
        return cls(
            label=entry.title,
            subtitle=entry.subtitle,
            key=entry.key,
            category=entry.category,
            badge=entry.progress_text,
            decision=entry.kind,
            reason=entry.reason,
            action=entry.action.text if entry.action is not None else None,
            dispatch_target=entry.dispatch_target.center if entry.dispatch_target is not None else None,
            candidate=entry.key is None and entry.rule is None,
        )

    @property
    def identity(self) -> str:
        """The diff key: catalogue key when known, normalised label otherwise."""
        return self.key if self.key is not None else normalize(self.label)

    def as_dict(self) -> dict[str, object]:
        """Render one log row without giving a refused row a tap target."""
        return {
            'key': self.key,
            'label': self.label,
            'subtitle': self.subtitle,
            'badge': self.badge,
            'decision': self.decision.value,
            'reason': self.reason,
            'action': self.action,
            'candidate': self.candidate,
            'dispatch_target': list(self.dispatch_target) if self.dispatch_target is not None else None,
        }


@dataclass(frozen=True, slots=True)
class RosterChange:
    """One before -> after change on a row that exists in both observations."""

    identity: str
    key: str | None
    label: str
    before: str | None
    after: str | None

    def as_dict(self) -> dict[str, object]:
        """Render the change for the JSONL line."""
        return {
            'key': self.key,
            'label': self.label,
            'before': self.before,
            'after': self.after,
        }


@dataclass(frozen=True, slots=True)
class RosterDiff:
    """What changed between two roster observations."""

    added: tuple[RosterRow, ...] = ()
    removed: tuple[RosterRow, ...] = ()
    progress_moved: tuple[RosterChange, ...] = ()
    action_changed: tuple[RosterChange, ...] = ()
    candidates: tuple[RosterRow, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, 'added', tuple(self.added))
        object.__setattr__(self, 'removed', tuple(self.removed))
        object.__setattr__(self, 'progress_moved', tuple(self.progress_moved))
        object.__setattr__(self, 'action_changed', tuple(self.action_changed))
        object.__setattr__(self, 'candidates', tuple(self.candidates))

    @property
    def changed(self) -> bool:
        """Whether anything at all differs from the previous observation."""
        return any((self.added, self.removed, self.progress_moved, self.action_changed, self.candidates))

    def as_dict(self) -> dict[str, object]:
        """Render the diff for the JSONL line."""
        return {
            'added': [row.as_dict() for row in self.added],
            'removed': [row.as_dict() for row in self.removed],
            'progress_moved': [change.as_dict() for change in self.progress_moved],
            'action_changed': [change.as_dict() for change in self.action_changed],
            'candidates': [row.as_dict() for row in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class RosterObservation:
    """One capture of the task surface and its diff against the previous one.

    ``rows`` is the roster currently in force. A failed observation keeps the
    previous rows and records ``error`` instead of ending the walk. ``roster``
    carries the full typed value for the next observation; it is deliberately
    not serialised, because ``rows`` is the smaller durable record.
    """

    sequence: int
    timestamp: datetime
    rows: tuple[RosterRow, ...]
    diff: RosterDiff = field(default_factory=RosterDiff)
    surface_variant: Surface | None = None
    error: str | None = None
    cap_reached: bool = False
    roster: Roster | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, 'rows', tuple(self.rows))

    @property
    def failed(self) -> bool:
        """Whether the capture could not be read and the previous roster stands."""
        return self.error is not None

    @property
    def candidates(self) -> tuple[RosterRow, ...]:
        """Every unrecognised row in this observation, not only the new ones."""
        return tuple(row for row in self.rows if row.candidate)

    @property
    def added(self) -> tuple[RosterRow, ...]:
        """Rows that were not in the previous observation."""
        return self.diff.added

    @property
    def removed(self) -> tuple[RosterRow, ...]:
        """Rows that the previous observation had and this one does not."""
        return self.diff.removed

    def as_dict(self) -> dict[str, object]:
        """Render one durable ``roster.jsonl`` line."""
        return {
            'schema': ROSTER_OBSERVATION_SCHEMA,
            'observation_seq': self.sequence,
            'timestamp': self.timestamp.isoformat(),
            'surface_variant': self.surface_variant.value if self.surface_variant is not None else None,
            'error': self.error,
            'cap_reached': self.cap_reached,
            'rows': [row.as_dict() for row in self.rows],
            'diff': self.diff.as_dict(),
        }


def diff_rosters(
    previous: Roster | None,
    current: Roster,
    *,
    known_candidates: Collection[str] = (),
) -> RosterDiff:
    """Diff two rosters by catalogue key, falling back to the normalised label.

    Added and removed rows keep their full log row, so a removal carries its
    label, subtitle and previous progress badge. Progress and action changes
    carry the before -> after values. An unrecognised row is a *candidate* only
    the first time it is seen in a run; ``known_candidates`` lets the observer
    enforce "reported once per run" without losing it from ``rows``.
    """
    before = _rows_by_identity(previous)
    after = _rows_by_identity(current)
    added = tuple(row for identity, row in after.items() if identity not in before)
    removed = tuple(row for identity, row in before.items() if identity not in after)
    progress_moved: list[RosterChange] = []
    action_changed: list[RosterChange] = []

    for identity, row in after.items():
        old = before.get(identity)
        if old is None:
            continue
        if _progress_key(old.badge) != _progress_key(row.badge):
            progress_moved.append(
                RosterChange(
                    identity=identity,
                    key=row.key,
                    label=row.label,
                    before=old.badge,
                    after=row.badge,
                ),
            )
        if _action_key(old.action) != _action_key(row.action):
            action_changed.append(
                RosterChange(
                    identity=identity,
                    key=row.key,
                    label=row.label,
                    before=old.action,
                    after=row.action,
                ),
            )

    known = set(known_candidates)
    candidates = tuple(row for row in after.values() if row.candidate and row.identity not in known)
    return RosterDiff(
        added=added,
        removed=removed,
        progress_moved=tuple(progress_moved),
        action_changed=tuple(action_changed),
        candidates=candidates,
    )


def observe_roster(
    source: CaptureSource | Snapshot,
    catalogue: TaskCatalogue,
    *,
    previous: Roster | None = None,
    sequence: int = 1,
    timestamp: datetime | None = None,
    surface_variant: Surface | None = None,
    known_candidates: Collection[str] = (),
    action_labels: Sequence[str] = DEFAULT_ACTION_LABELS,
    route: DiscoveryRoute | None = None,
    logger: logging.Logger | None = None,
) -> RosterObservation:
    """Capture, classify and enumerate one fresh roster, then diff it.

    ``source`` is the capture seam (a callable or an object with ``capture``) or
    an already captured :class:`Snapshot`. The current frame is read with
    :func:`enumerate_roster`; no navigation is attempted here, because the task
    executor has already returned the app to the task surface. A loading page, a
    popup, the redeem interstitial or an ``UNKNOWN`` surface is a failed
    observation: it is recorded with ``error`` and the *previous* roster is kept,
    so one bad frame never ends the walk.
    """
    if sequence < 1:
        raise ValueError(f'sequence must be positive, got {sequence!r}')
    stamp = timestamp if timestamp is not None else datetime.now(UTC)
    variant = surface_variant
    try:
        snapshot = source if isinstance(source, Snapshot) else _capture(source)
        classification = classify(snapshot)
        if variant is None:
            variant = classify_surface(snapshot)
        problem = _observation_problem(classification.state, variant)
        if problem is not None:
            raise _ObservationFailure(problem)
        if (
            previous is not None
            and previous.entries
            and previous.surface_variant is not None
            and variant is not None
            and _surface_family(variant) != _surface_family(previous.surface_variant)
        ):
            raise _ObservationFailure(
                f'the surface changed from {previous.surface_variant.value} to {variant.value}; '
                'the previous roster is kept',
            )
        roster = replace(
            enumerate_roster(
                snapshot,
                catalogue,
                route=route if route is not None else (previous.route if previous is not None else None),
                action_labels=action_labels,
            ),
            surface_variant=variant,
        )
    except (OcrError, OSError, SnapshotError, StateTimeout, _ObservationFailure) as exc:
        rows = _rows_of(previous)
        observation = RosterObservation(
            sequence=sequence,
            timestamp=stamp,
            rows=rows,
            surface_variant=variant,
            error=f're-enumeration failed: {exc}',
            roster=previous,
        )
        _log_observation(logger, observation)
        return observation

    rows = tuple(RosterRow.from_entry(entry) for entry in roster.entries)
    observation = RosterObservation(
        sequence=sequence,
        timestamp=stamp,
        rows=rows,
        diff=diff_rosters(previous, roster, known_candidates=known_candidates),
        surface_variant=variant,
        roster=roster,
    )
    _log_observation(logger, observation)
    return observation


def _log_run_id(run_id: str) -> str:
    """Keep the log inside ``<runs_dir>/<run_id>``; no path escapes."""
    cleaned = run_id.strip()
    if not cleaned or cleaned in {'.', '..'} or Path(cleaned).name != cleaned or '/' in cleaned or '\\' in cleaned:
        raise ValueError(f'run id must be a single path component: {run_id!r}')
    return cleaned


class RosterLog:
    """Append-only ``roster.jsonl`` in one run directory.

    One JSON object per line, opened in append mode and fsynced: an observation
    is never rewritten, so the file accumulates across a run and stays greppable
    with ordinary line tools. Reading it back reconstructs the exact sequence.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @classmethod
    def for_run(cls, runs_dir: str | Path, run_id: str) -> RosterLog:
        """The log at ``<runs_dir>/<run_id>/roster.jsonl``."""
        return cls(Path(runs_dir) / _log_run_id(run_id) / ROSTER_LOG_NAME)

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        run_id: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> RosterLog:
        """Use the container's ``AECOINS_RUNS_DIR``, falling back to ``runs/``."""
        env = os.environ if environment is None else environment
        root = env.get('AECOINS_RUNS_DIR', '').strip() or 'runs'
        identifier = (run_id or '').strip() or clock().strftime('%Y%m%dT%H%M%SZ')
        return cls.for_run(root, identifier)

    def append(self, observation: RosterObservation) -> Path:
        """Append one line and fsync it; raise :class:`RosterLogError` on failure."""
        line = json.dumps(observation.as_dict(), ensure_ascii=False, separators=(',', ':'), sort_keys=True)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(line + '\n')
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise RosterLogError(f'could not append {self.path}: {exc}') from exc
        return self.path

    def read(self) -> tuple[dict[str, Any], ...]:
        """Read every valid JSONL record back, in file order."""
        try:
            text = self.path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise RosterLogError(f'could not read {self.path}: {exc}') from exc

        records: list[dict[str, Any]] = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RosterLogError(f'{self.path}:{number} is not valid JSON: {exc}') from exc
            if not isinstance(payload, dict):
                raise RosterLogError(f'{self.path}:{number} is not a JSON object')
            records.append(cast(dict[str, Any], payload))
        return tuple(records)


class RosterObserver:
    """Stateful observer: one re-enumeration per completion, capped per run.

    The run loop calls :meth:`observe` exactly once after each completed task
    and once more at the end. A failed observation is appended to the log but
    leaves :attr:`roster` unchanged, so the next task is still dispatched from
    the last readable roster. Once ``max_observations`` is reached, one final
    ``cap_reached`` line explains the stop; later calls neither capture nor log.
    """

    def __init__(
        self,
        catalogue: TaskCatalogue,
        *,
        previous: Roster | None = None,
        log: RosterLog | None = None,
        max_observations: int = DEFAULT_OBSERVATION_CAP,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        action_labels: Sequence[str] = DEFAULT_ACTION_LABELS,
        logger: logging.Logger | None = None,
    ) -> None:
        if max_observations < 1:
            raise ValueError(f'max_observations must be positive, got {max_observations!r}')
        self.catalogue = catalogue
        self.log = log
        self.max_observations = max_observations
        self._clock = clock
        self._action_labels = tuple(action_labels)
        self._logger = logger if logger is not None else logging.getLogger(__name__)
        self._roster = previous
        self._sequence = 0
        self._observations: list[RosterObservation] = []
        self._seen_candidates: set[str] = set()
        self._cap_observation: RosterObservation | None = None

    @classmethod
    def from_environment(
        cls,
        catalogue: TaskCatalogue,
        *,
        run_id: str | None = None,
        environment: Mapping[str, str] | None = None,
        previous: Roster | None = None,
        log: RosterLog | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        action_labels: Sequence[str] = DEFAULT_ACTION_LABELS,
        logger: logging.Logger | None = None,
    ) -> RosterObserver:
        """Build the observer from the container's run directory and task caps."""
        env = os.environ if environment is None else environment
        identifier = (run_id or '').strip() or clock().strftime('%Y%m%dT%H%M%SZ')
        observer_log = log if log is not None else RosterLog.from_environment(env, run_id=identifier)
        return cls(
            catalogue,
            previous=previous,
            log=observer_log,
            max_observations=_cap_from_environment(env),
            clock=clock,
            action_labels=action_labels,
            logger=logger,
        )

    @property
    def observations(self) -> tuple[RosterObservation, ...]:
        """Every logged observation this run, oldest first (the cap line excluded)."""
        return tuple(self._observations)

    @property
    def roster(self) -> Roster | None:
        """The roster currently in force: the last readable observation."""
        return self._roster

    @property
    def sequence(self) -> int:
        """How many observations have been made (the cap line excluded)."""
        return self._sequence

    @property
    def cap_reached(self) -> bool:
        """Whether the per-run re-enumeration cap has been reported."""
        return self._cap_observation is not None

    @property
    def candidates(self) -> tuple[RosterRow, ...]:
        """Every distinct unrecognised row seen this run, in first-seen order."""
        seen: dict[str, RosterRow] = {}
        for observation in self._observations:
            for row in observation.candidates:
                seen.setdefault(row.identity, row)
        return tuple(seen.values())

    def observe(
        self,
        source: CaptureSource | Snapshot,
        *,
        route: DiscoveryRoute | None = None,
        timestamp: datetime | None = None,
    ) -> RosterObservation:
        """Take the next observation, or report the cap without capturing."""
        if len(self._observations) >= self.max_observations:
            return self._report_cap(timestamp)
        observation = observe_roster(
            source,
            self.catalogue,
            previous=self._roster,
            sequence=self._sequence + 1,
            timestamp=timestamp if timestamp is not None else self._clock(),
            known_candidates=self._seen_candidates,
            action_labels=self._action_labels,
            route=route,
            logger=self._logger,
        )
        self._sequence += 1
        self._observations.append(observation)
        self._write(observation)
        if observation.roster is not None:
            self._roster = observation.roster
        self._seen_candidates.update(row.identity for row in observation.diff.candidates)
        return observation

    def _report_cap(self, timestamp: datetime | None) -> RosterObservation:
        if self._cap_observation is not None:
            return self._cap_observation
        observation = RosterObservation(
            sequence=self._sequence + 1,
            timestamp=timestamp if timestamp is not None else self._clock(),
            rows=_rows_of(self._roster),
            surface_variant=None,
            cap_reached=True,
            roster=self._roster,
        )
        self._cap_observation = observation
        self._write(observation)
        _log_observation(self._logger, observation)
        return observation

    def _write(self, observation: RosterObservation) -> None:
        if self.log is None:
            return
        try:
            self.log.append(observation)
        except RosterLogError as exc:
            # A run artifact failure must not end the walk; the in-memory
            # observation still advances the roster and the cap.
            _log_observation(self._logger, observation, note=f'roster log write failed: {exc}')


def read_roster_log(path: str | Path) -> tuple[dict[str, Any], ...]:
    """Read one ``roster.jsonl`` file back into its observation dictionaries."""
    return RosterLog(path).read()


def _progress_key(badge: str | None) -> object:
    """Compare parsed badges so OCR spacing is not a progress change."""
    if badge is None:
        return None
    parsed = parse_progress_badge(badge)
    return parsed if parsed is not None else badge


def _action_key(action: str | None) -> str | None:
    """Compare actions case/punctuation-insensitively; log the raw text."""
    return normalize(action) if action is not None else None


def _rows_by_identity(roster: Roster | None) -> dict[str, RosterRow]:
    rows: dict[str, RosterRow] = {}
    if roster is None:
        return rows
    for entry in roster.entries:
        row = RosterRow.from_entry(entry)
        rows.setdefault(row.identity, row)
    return rows


def _rows_of(roster: Roster | None) -> tuple[RosterRow, ...]:
    return tuple(_rows_by_identity(roster).values())


def _surface_family(variant: Surface) -> str:
    """Group the variants that describe the same task surface."""
    if variant in (Surface.BOOST_PAGE, Surface.EMPTY_BOOST):
        return 'boost'
    if variant in (Surface.ROWS_PRESENT, Surface.ROWS_ABSENT):
        return 'coin'
    return variant.value


def _observation_problem(state: ScreenState, variant: Surface | None) -> str | None:
    if state is ScreenState.LOADING:
        return 'the task surface is still loading'
    if state is ScreenState.POPUP:
        return 'a popup covers the task surface'
    if state is ScreenState.ERROR:
        return 'the task surface is in an error state'
    if variant is Surface.UNKNOWN:
        return 'the surface variant is UNKNOWN'
    if variant is Surface.REDEEM_INTERSTITIAL:
        return 'the reward interstitial covers the task surface'
    if variant is Surface.EMPTY_BOOST:
        return 'the empty boost page is a load fault, not an empty roster'
    return None


def _log_observation(
    logger: logging.Logger | None,
    observation: RosterObservation,
    *,
    note: str | None = None,
) -> None:
    if logger is None:
        return
    payload: dict[str, object] = {
        'event': 'roster_observation',
        'observation_seq': observation.sequence,
        'surface_variant': observation.surface_variant.value if observation.surface_variant is not None else None,
        'added': [row.label for row in observation.diff.added],
        'removed': [row.label for row in observation.diff.removed],
        'progress_moved': [change.as_dict() for change in observation.diff.progress_moved],
        'action_changed': [change.as_dict() for change in observation.diff.action_changed],
        'candidates': [row.label for row in observation.diff.candidates],
        'error': observation.error,
        'cap_reached': observation.cap_reached,
    }
    if note is not None:
        payload['note'] = note
    logger.info('%s', json.dumps(payload, separators=(',', ':'), sort_keys=True))


def _cap_from_environment(environment: Mapping[str, str]) -> int:
    name = _MAX_OBSERVATIONS_ENV
    raw = environment.get(name, '').strip()
    final_capture = False
    if not raw:
        name = _MAX_TASKS_ENV
        raw = environment.get(name, '').strip()
        final_capture = True
    if not raw:
        return DEFAULT_OBSERVATION_CAP
    try:
        cap = int(raw)
    except ValueError as exc:
        raise ValueError(f'{name} must be an integer, got {raw!r}') from exc
    if cap < 1:
        raise ValueError(f'{name} must be positive, got {cap!r}')
    return cap + 1 if final_capture else cap


class _ObservationFailure(Exception):
    """Internal marker: the frame is not a readable task surface."""


@dataclass(frozen=True, slots=True)
class BoostList:
    """The surface ``open_boost_list`` arrived at, and how it got there."""

    snapshot: Snapshot
    route: DiscoveryRoute
    detail: str
    classification: Classification | None = None
    tap: TapRecord | None = None
    dismiss: DismissReport | None = None
    elapsed: float = 0.0
    history: tuple[ScreenState, ...] = ()

    @property
    def opened(self) -> bool:
        """Whether the route is the full boost page."""
        return self.route is DiscoveryRoute.BOOST

    @property
    def inline(self) -> bool:
        """Whether discovery degraded to the coin page's inline rows."""
        return self.route is DiscoveryRoute.INLINE

    @property
    def empty(self) -> bool:
        """Whether the boost page is the honest-looking empty state."""
        return is_empty_boost(self.snapshot)

    @property
    def status(self) -> RosterStatus:
        """The open result's status, using the same wording as a roster."""
        return RosterStatus.NOTHING_AVAILABLE if self.empty else RosterStatus.AVAILABLE

    def as_dict(self) -> dict[str, object]:
        """Render the route decision for the run report."""
        record: dict[str, object] = {
            'route': self.route.value,
            'detail': self.detail,
            'state': self.classification.state.value if self.classification is not None else None,
            'empty': self.empty,
            'elapsed': self.elapsed,
            'history': [state.value for state in self.history],
        }
        if self.tap is not None:
            record['tap'] = self.tap.as_dict()
        if self.dismiss is not None:
            record['dismiss'] = self.dismiss.as_dict()
        return record


# Backwards-readable aliases for callers that name the result by its purpose.
BoostOpenResult = BoostList
DiscoveryResult = BoostList


@dataclass(frozen=True, slots=True)
class CoinPageWait:
    """The frame the pre-tap coin-page wait settled on, and how it got there.

    ``settled`` is true only when the last frame is neither ``LOADING`` nor
    ``POPUP``. A dry run cannot dismiss a popup — that is an in-app tap — so it
    calls :func:`wait_for_coin_page` without ``dismiss_popup`` and receives the
    ``POPUP`` frame immediately instead of waiting out the deadline for a screen
    it is not allowed to touch.
    """

    snapshot: Snapshot
    classification: Classification
    history: tuple[ScreenState, ...]
    timed_out: bool

    @property
    def settled(self) -> bool:
        """Whether the page stopped loading without a modal still covering it."""
        return not self.timed_out and self.classification.state not in (
            ScreenState.LOADING,
            ScreenState.POPUP,
            ScreenState.UNKNOWN,
        )

    def as_dict(self) -> dict[str, object]:
        """Render the wait for a run report."""
        return {
            'state': self.classification.state.value,
            'settled': self.settled,
            'timed_out': self.timed_out,
            'history': [state.value for state in self.history],
        }


def wait_for_coin_page(
    capture: CaptureSource,
    *,
    started: float | None = None,
    timeout: float = DEFAULT_BOOST_TIMEOUT,
    interval: float = DEFAULT_BOOST_INTERVAL,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    dismiss_popup: Callable[[Snapshot], None] | None = None,
) -> CoinPageWait:
    """Poll until the coin page is no longer the loading skeleton.

    This is the pre-tap wait :func:`open_boost_list` has always used: a loading
    hybrid page is not proof that ``Earn more`` is absent, so the frame is
    re-captured until it is no longer ``LOADING``. The measured failure in #76
    was the dry-run planner classifying the skeleton once and reporting a clean
    zero-step plan; the same helper now supplies both paths' settled frame.

    ``started`` lets a caller that already read the clock share its deadline
    without an extra read. ``dismiss_popup`` is the live path's in-app
    dismissal. It is ``None`` for a dry run, which returns on the first
    ``POPUP`` rather than pretending the page settled or spending a tap it must
    not issue. A deadline returns ``timed_out`` with the last frame and every
    state on the way, so a caller can name "still loading" or "not the coin
    page" instead of treating it as an empty page.

    ``UNKNOWN`` is deliberately not settled: the measured device sequence starts
    with the pre-transition frame, and returning on it was the #76 bug. The wait
    keeps polling until a coin-page or boost marker appears, the deadline ends,
    or a caller's capture guard names the repeating screen.
    """
    if timeout <= 0:
        raise ValueError('timeout must be positive')
    if interval <= 0:
        raise ValueError('interval must be positive')

    begin = clock() if started is None else started
    deadline = begin + timeout
    frame = _capture(capture)
    classification = classify(frame)
    history = [classification.state]

    while classification.state in (ScreenState.LOADING, ScreenState.POPUP, ScreenState.UNKNOWN):
        now = clock()
        if now >= deadline:
            return CoinPageWait(
                snapshot=frame,
                classification=classification,
                history=tuple(history),
                timed_out=True,
            )
        if classification.state is ScreenState.POPUP:
            if dismiss_popup is None:
                return CoinPageWait(
                    snapshot=frame,
                    classification=classification,
                    history=tuple(history),
                    timed_out=False,
                )
            dismiss_popup(frame)
        else:
            sleep(min(interval, max(0.0, deadline - now)))
        frame = _capture(capture)
        classification = classify(frame)
        history.append(classification.state)

    return CoinPageWait(
        snapshot=frame,
        classification=classification,
        history=tuple(history),
        timed_out=False,
    )


def open_boost_list(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    *,
    templates: TemplateStore | None = None,
    timeout: float = DEFAULT_BOOST_TIMEOUT,
    interval: float = DEFAULT_BOOST_INTERVAL,
    settle: float = 0.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    logger: logging.Logger | None = None,
    on_dismiss: Callable[[DismissReport], None] | None = None,
) -> BoostList:
    """Open ``Earn more`` and wait for the ``Coins boost`` destination.

    The initial frame is the coin page. If ``Earn more`` is not there after the
    page has stopped loading and any popup has been swept, the same frame is
    returned with :attr:`DiscoveryRoute.INLINE`; no tap is issued and no
    exception is raised. Otherwise the label is tapped through
    :class:`Controller`, any popup seen on the way is closed with
    :func:`~aliexpress_coins.control.popups.dismiss_after`, and capture is
    polled until the boost marker and the same non-empty task set are visible
    on two consecutive frames (or the empty sentence is visible).

    A frame that already contains the boost destination is *not* swept as a
    popup, and the sweep after a real popup runs with the ``coins_boost_sheet``
    family removed. That family's anchor is the destination's own
    ``Coins boost`` header, so sweeping it would close the list it was meant to
    open (including immediately after a different popup is dismissed). A
    genuine popup still classifies as ``POPUP`` and is swept normally, even
    when the header and some rows are still OCR-visible behind it. A
    destination-shaped ``POPUP`` frame with no other family is treated as the
    list rather than as an unclosable modal.
    """
    if timeout <= 0:
        raise ValueError('timeout must be positive')
    if interval <= 0:
        raise ValueError('interval must be positive')
    if settle < 0:
        raise ValueError('settle must not be negative')

    started = clock()
    deadline = started + timeout
    dismiss: DismissReport | None = None

    def _dismiss_initial_popup(_snapshot: Snapshot) -> None:
        nonlocal dismiss
        dismiss = dismiss_after(
            controller,
            capture,
            corpus,
            templates=templates,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=logger,
        )
        if on_dismiss is not None:
            on_dismiss(dismiss)

    # A loading coin page is not proof that the label is absent, and tapping
    # through a popup would land on the scrim even when OCR can still see the
    # label underneath it. Resolve those states first with the one wait the dry
    # run also uses; a settled page with no label is a real page variant and
    # degrades immediately.
    arrival = wait_for_coin_page(
        capture,
        started=started,
        timeout=timeout,
        interval=interval,
        clock=clock,
        sleep=sleep,
        dismiss_popup=_dismiss_initial_popup,
    )
    initial = arrival.snapshot
    initial_classification = arrival.classification

    if initial_classification.state is ScreenState.POPUP:
        raise StateTimeout(
            f'an initial popup still covers the coin page after {timeout:g}s; '
            f'no tap was issued for {EARN_MORE_LABEL!r}',
        )

    earn_more = find_text(initial, EARN_MORE_LABEL)
    if earn_more is None:
        return BoostList(
            snapshot=initial,
            route=DiscoveryRoute.INLINE,
            detail=f'{EARN_MORE_LABEL!r} is absent; using the inline task rows on the coin page',
            classification=initial_classification,
            dismiss=dismiss,
        )

    sweep_corpus = _without_destination_family(corpus)
    tap = controller.tap_box(
        earn_more.box,
        why=f'open the {BOOST_MARKER} task list',
        label=EARN_MORE_LABEL,
    )

    observations: list[Observation] = []
    previous_signature: tuple[tuple[str, str, tuple[int, int] | None], ...] | None = None

    while True:
        snapshot = _capture(capture)
        classification = classify(snapshot)
        now = clock()
        observations.append(
            Observation(elapsed=max(0.0, now - started), classification=classification),
        )

        # A modal over an already-rendered list still hides the rows beneath it.
        # Sweep it first; the destination family is already excluded so a real
        # popup cannot be mistaken for the list itself. A destination-shaped
        # POPUP frame with no other family is the list, not an unclosable modal.
        if classification.state is ScreenState.POPUP and not (
            _boost_arrived(snapshot) and _matching_family(snapshot, sweep_corpus) is None
        ):
            if now >= deadline:
                seen = ' -> '.join(item.classification.state.value for item in observations)
                raise StateTimeout(
                    f'{BOOST_MARKER!r} was not observed within {timeout:g}s after tapping '
                    f'{EARN_MORE_LABEL!r} (last={classification.describe()}; history={seen})',
                )
            dismiss = dismiss_after(
                controller,
                capture,
                sweep_corpus,
                templates=templates,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=logger,
            )
            if on_dismiss is not None:
                on_dismiss(dismiss)
            previous_signature = None
            continue

        if _boost_arrived(snapshot):
            if is_empty_boost(snapshot):
                return BoostList(
                    snapshot=snapshot,
                    route=DiscoveryRoute.BOOST,
                    detail=_arrival_detail(snapshot),
                    classification=classification,
                    tap=tap,
                    dismiss=dismiss,
                    elapsed=max(0.0, now - started),
                    history=tuple(item.classification.state for item in observations),
                )

            signature = _task_signature(snapshot)
            if not signature or signature == previous_signature:
                return BoostList(
                    snapshot=snapshot,
                    route=DiscoveryRoute.BOOST,
                    detail=_arrival_detail(snapshot),
                    classification=classification,
                    tap=tap,
                    dismiss=dismiss,
                    elapsed=max(0.0, now - started),
                    history=tuple(item.classification.state for item in observations),
                )
            previous_signature = signature

        if now >= deadline:
            seen = ' -> '.join(item.classification.state.value for item in observations)
            raise StateTimeout(
                f'{BOOST_MARKER!r} was not observed within {timeout:g}s after tapping '
                f'{EARN_MORE_LABEL!r} (last={classification.describe()}; history={seen})',
            )

        sleep(min(interval, max(0.0, deadline - now)))


def enumerate_roster(
    snapshot: Snapshot,
    catalogue: TaskCatalogue,
    *,
    route: DiscoveryRoute | None = None,
    action_labels: Sequence[str] = DEFAULT_ACTION_LABELS,
) -> Roster:
    """Read every action-bearing task row in ``snapshot`` in reading order.

    A row is a task row when :func:`find_action_in_row` finds its control. The
    title is the first meaningful label above that control; progress badges,
    rewards and the action itself are not part of the title or subtitle.
    Duplicate titles are kept once, so a row that appears twice in one frame is
    not dispatched twice.

    The policy is *not* used to filter: denied and unrecognised rows remain in
    the roster with their reason. Only an allowed row exposes
    :attr:`RosterEntry.dispatch_target`.
    """
    entries: list[RosterEntry] = []
    seen: set[str] = set()

    for row in group_rows(snapshot.boxes, snapshot.geometry):
        action = find_action_in_row(row, action_labels)
        if action is None:
            continue
        title, subtitle = _title_and_subtitle(row, action, action_labels)
        if title is None:
            continue
        identity = normalize(title)
        if not identity or identity in seen:
            continue
        seen.add(identity)

        decision = catalogue.decide(title, subtitle=subtitle)
        entries.append(
            RosterEntry(
                title=title,
                subtitle=subtitle,
                action=action,
                progress=find_progress_badge(row),
                decision=decision,
                row=row,
            ),
        )

    return Roster(
        entries=tuple(entries),
        route=route,
        catalogue_version=catalogue.version,
        default_deny_reason=catalogue.default_deny_reason,
    )


def discover_roster(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    catalogue: TaskCatalogue,
    *,
    navigate: RouteFirer | None = None,
    entry_route: SurfaceRoute = SurfaceRoute.SHORTCUT,
    refresh: bool = True,
    templates: TemplateStore | None = None,
    timeout: float = DEFAULT_BOOST_TIMEOUT,
    interval: float = DEFAULT_BOOST_INTERVAL,
    settle: float = 0.0,
    action_labels: Sequence[str] = DEFAULT_ACTION_LABELS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    logger: logging.Logger | None = None,
    on_dismiss: Callable[[DismissReport], None] | None = None,
) -> Roster:
    """Open the best available surface, apply the route policy and enumerate it.

    This is the integration point for the run loop and #38. It returns a roster
    whose :attr:`Roster.route` records ``boost`` or ``inline`` and whose
    :attr:`Roster.surface_variant` / :attr:`Roster.routes_taken` record what the
    page looked like and which route produced it.

    ``navigate`` is the flow layer's route firer. When supplied, the policy is:

    1. fire the shortcut route once (the app must already be started and warm);
    2. if the surface has no usable rows, fire it **once more** and try again —
       the measured failure is a cold-start first-load race, and a warm re-entry
       populates the list;
    3. if rows are still absent, return an empty roster. Its status is
       ``nothing available``, not an error.

    The budget is exactly :data:`SURFACE_REFRESH_LIMIT`. More attempts do not
    help: the roster is server-driven, so repeated navigation cannot create
    tasks and only resembles automation. When ``navigate`` is ``None`` the
    caller has already arrived; the function classifies and enumerates that
    single frame and records no route.

    Rows present are never re-navigated. The first route either works and the
    run proceeds, or it does not and the one permitted refresh runs. A needless
    relaunch is a chance to lose a page that was already loaded.
    """
    routes: list[SurfaceRoute] = []
    variants: list[Surface] = []
    refresh_attempted = False

    def _open() -> tuple[Roster, Surface]:
        opened = open_boost_list(
            controller,
            capture,
            corpus,
            templates=templates,
            timeout=timeout,
            interval=interval,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=logger,
            on_dismiss=on_dismiss,
        )
        variant = classify_surface(opened.snapshot)
        variants.append(variant)
        roster = enumerate_roster(
            opened.snapshot,
            catalogue,
            route=opened.route,
            action_labels=action_labels,
        )
        return roster, variant

    # TODO(#27): the widget-route fallback is deliberately not fired here. The
    # launcher items were removed from the reference device, and replaying the
    # widget intent without the widget installed is unresolved. Do not guess at
    # that action; #27 owns the measured fallback.
    if navigate is not None:
        navigate(entry_route)
        routes.append(entry_route)

    roster, variant = _open()

    if (
        refresh
        and navigate is not None
        and len(routes) < SURFACE_REFRESH_LIMIT + 1
        and roster.empty
        and variant is not Surface.REDEEM_INTERSTITIAL
    ):
        refresh_attempted = True
        navigate(entry_route)
        routes.append(entry_route)
        roster, variant = _open()

    return replace(
        roster,
        surface_variant=variant,
        routes_taken=tuple(routes),
        refresh_attempted=refresh_attempted,
        variants_seen=tuple(variants),
    )


def _boost_arrived(snapshot: Snapshot) -> bool:
    if not is_boost_page(snapshot):
        return False
    return is_empty_boost(snapshot) or has_task_rows(snapshot)


def _task_signature(snapshot: Snapshot) -> tuple[tuple[str, str, tuple[int, int] | None], ...]:
    """The title/action/progress set used to prove the list stopped changing."""
    signature: list[tuple[str, str, tuple[int, int] | None]] = []
    for row in group_rows(snapshot.boxes, snapshot.geometry):
        action = find_action_in_row(row)
        if action is None:
            continue
        title, _ = _title_and_subtitle(row, action, DEFAULT_ACTION_LABELS)
        if title is None:
            continue
        badge = find_progress_badge(row)
        progress = (badge.completed, badge.total) if badge is not None else None
        signature.append((normalize(title), normalize(action.text), progress))
    return tuple(signature)


def _arrival_detail(snapshot: Snapshot) -> str:
    if is_empty_boost(snapshot):
        return f'{BOOST_MARKER!r} is empty ({EMPTY_BOOST_MARKER!r}); report nothing available'
    return f'{EARN_MORE_LABEL!r} opened the {BOOST_MARKER!r} task list'


def _title_and_subtitle(
    row: Row,
    action: TextBox,
    action_labels: Sequence[str],
) -> tuple[str | None, str | None]:
    candidates = [
        box
        for box in row.boxes
        if box is not action
        and not _is_action_text(box.text, action_labels)
        and parse_progress_badge(box.text) is None
        and any(character.isalpha() for character in box.text)
        and not _is_reward_text(box.text)
    ]
    if not candidates:
        return None, None

    title = candidates[0].text.strip()
    if not title:
        return None, None
    subtitle = ' '.join(box.text.strip() for box in candidates[1:] if box.text.strip())
    return title, subtitle or None


def _is_action_text(text: str, action_labels: Sequence[str]) -> bool:
    return any(similarity(label, text) >= DEFAULT_MIN_SCORE for label in action_labels)


def _is_reward_text(text: str) -> bool:
    return bool(_REWARD.match(text)) or normalize(text) in _REWARD_WORDS


def _without_destination_family(corpus: PopupCorpus) -> PopupCorpus:
    """Remove the family that would close the boost destination itself."""
    families = tuple(family for family in corpus.families if family.anchor != BOOST_MARKER)
    if len(families) == len(corpus.families):
        return corpus
    return replace(corpus, families=families)


def _capture(source: CaptureSource) -> Snapshot:
    if callable(source):
        return source()
    return source.capture()
