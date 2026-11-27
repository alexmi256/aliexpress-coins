"""One night's work: hygiene, route, roster walk, budgets and a report (#29).

The engine is deliberately a coordinator. It does not tap a new path, parse a
new row or classify a new screen: it calls the existing sign-in, browse, quiz,
surprise-item and notification executors, and it hands each one the roster row it
was allowed to run. The surface functions (#22/#38/#43) decide what is allowed
and what is still available.

The walk is an outer loop (#123): after every real attempt the roster is
re-enumerated, and the next pass dispatches anything still allowed. Selection
uses an explicit cost order (:data:`_CATEGORY_COST`), not the reading order in
which the frame happened to enumerate its rows (#131). The loop is bounded by
progress, not a fixed pass count (#141): another sweep is allowed while the pass
dispatched a row or revealed new dispatchable rows, and the walk stops after
:data:`MAX_NO_PROGRESS_PASSES` consecutive sweeps that did neither. Four bounds
keep it finite:

* ``AECOINS_MAX_TASKS_PER_RUN`` is the task budget, counted in dispatches;
* ``AECOINS_RUN_BUDGET_SECONDS`` is the wall-clock budget, checked before every
  dispatch, after every completion, and at the running task's clock/sleep/capture
  seams so a task cannot overrun it silently;
* ``AECOINS_MAX_PASSES`` is the absolute ceiling on full re-enumerated sweeps,
  a backstop for a surface that keeps changing without progress;
* ``AECOINS_MAX_TASK_ATTEMPTS`` blacklists one task for the rest of the day.

Selection never inherits OCR reading order. Eligible rows are ordered by
category cost (:data:`_CATEGORY_COST`), then catalogue key, then case-folded
title and subtitle; :func:`_next_entry` takes the first row under that rule. The
roster itself stays in reading order for ``roster.jsonl`` and diagnostics.

When any bound stops the walk, every row still eligible under the daily,
blacklist, block and settle rules is written to the report with the bound that
stopped it, including repeatable rows already completed once and retryable rows
already attempted. The final ``finally`` asks the existing return route to prove
the coins page, so a budget stop is still a clean stop.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Protocol, cast

from aliexpress_coins.control.controller import Controller
from aliexpress_coins.control.device import AdbInputDevice
from aliexpress_coins.control.errors import PopupUnclosable
from aliexpress_coins.control.popups import DismissReport, PopupCorpus, load_popup_corpus
from aliexpress_coins.perception.artifacts import ArtifactStore
from aliexpress_coins.perception.capture import AdbSnapshotter
from aliexpress_coins.perception.errors import FrameDecodeError, SnapshotError, StateTimeout
from aliexpress_coins.perception.matching import normalize
from aliexpress_coins.perception.ocr import RapidOcrEngine
from aliexpress_coins.perception.snapshot import Snapshot
from aliexpress_coins.perception.state import CaptureSource, ScreenState, classify
from aliexpress_coins.perception.templates import TemplateStore, load_templates
from aliexpress_coins.perception.waits import wait_for_text, wait_until_stable
from aliexpress_coins.runner.config import MAX_TASK_DISPATCHES, RunConfig
from aliexpress_coins.runner.device import AdbDevice, HygieneDevice
from aliexpress_coins.runner.errors import AppLoggedOut, ConfigError, RunBudgetExceeded, RunInterrupted
from aliexpress_coins.runner.failures import (
    Failure,
    FailureCode,
    RetryScope,
    classify_exception,
    failure,
    retry_delay,
    task_failure,
)
from aliexpress_coins.runner.levels import SKIPPED_ROW_LOG_LEVEL, run_log_level, task_log_level
from aliexpress_coins.runner.notify import Notification, NotificationSink, build_notifier
from aliexpress_coins.runner.recording import FailureRecorder
from aliexpress_coins.runner.report import RunEventLog, RunReport, RunStatus, TaskRecord, prune_runs
from aliexpress_coins.runner.safety import (
    UNEXPECTED_SCREEN_LIMIT,
    AbortCode,
    RunAborted,
    SafetyGuard,
    unwrap_capture,
)
from aliexpress_coins.runner.state import DailyStateStore, FamilyObservationStore, NightOutcome, NightRecord
from aliexpress_coins.tasks.browse import return_to_coins_page, run_browse_task
from aliexpress_coins.tasks.catalogue import AllowedTaskSpec, TaskCatalogue, TaskCategory, load_task_catalogue
from aliexpress_coins.tasks.discovery import (
    DEFAULT_BOOST_INTERVAL,
    DEFAULT_BOOST_TIMEOUT,
    CoinPageWait,
    DiscoveryRoute,
    Roster,
    RosterEntry,
    RosterLog,
    RosterObservation,
    RosterObserver,
    collect_roster,
    discover_roster,
    enumerate_roster,
    merge_rosters,
    open_boost_list,
    wait_for_coin_page,
)
from aliexpress_coins.tasks.dispatch import (
    DispatchGate,
    PolicyRefused,
    authorization_of,
)
from aliexpress_coins.tasks.families import FamilyEvidence
from aliexpress_coins.tasks.notifications import run_notifications_task
from aliexpress_coins.tasks.quiz import run_quiz_task
from aliexpress_coins.tasks.search import run_search_task
from aliexpress_coins.tasks.shortcut import run_shortcut_visit_task
from aliexpress_coins.tasks.signin import claim_daily_signins, read_coin_balance
from aliexpress_coins.tasks.surface import (
    COIN_PAGE_MARKERS,
    RouteFirer,
    Surface,
    SurfaceRoute,
    classify_surface,
    contains_text,
    is_boost_page,
    is_logged_out,
)
from aliexpress_coins.tasks.surprise import run_surprise_task

__all__ = [
    'DISPATCH_TABLE',
    'MAX_NO_PROGRESS_PASSES',
    'TASK_RECOVERY_ATTEMPTS',
    'EngineResult',
    'RunContext',
    'StopSignal',
    'TaskBudget',
    'make_live_context',
    'resolve_task_budget',
    'run_once',
]

LOG = logging.getLogger('aliexpress_coins.runner.engine')


class StopSignal(Protocol):
    """The stop flag the engine and the live seams consult."""

    def is_set(self) -> bool:
        """Whether a stop has been requested."""
        ...

    def wait(self, timeout: float | None = None) -> bool:
        """Block at most ``timeout`` seconds; true when stopped."""
        ...


class _NeverStop:
    def is_set(self) -> bool:
        return False

    def wait(self, timeout: float | None = None) -> bool:
        del timeout
        return False


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ExecutorResult(Protocol):
    """The common fields of the four executor result values."""

    @property
    def completed(self) -> bool:
        """Whether the screen proved the work and state was recorded."""
        ...

    @property
    def verification(self) -> str:
        """The evidence that decided the outcome."""
        ...

    @property
    def detail(self) -> str:
        """The report-safe sentence."""
        ...

    @property
    def outcome(self) -> StrEnum:
        """The executor's own outcome value."""
        ...

    def as_dict(self) -> dict[str, Any]:
        """The full executor result for ``run.json``."""
        ...


@dataclass(frozen=True, slots=True)
class EngineResult:
    """One dispatched row, normalised across the five executor result types."""

    outcome: str
    verification: str
    detail: str
    completed: bool
    payload: dict[str, Any]
    failure: Failure | None = None
    acted: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Render the normalised result for the report."""
        return {
            'outcome': self.outcome,
            'verification': self.verification,
            'detail': self.detail,
            'completed': self.completed,
            'payload': self.payload,
            'failure': None if self.failure is None else self.failure.as_dict(),
            'acted': self.acted,
        }


@dataclass(frozen=True, slots=True)
class TaskBudget:
    """How many dispatches this night may make, and why (#166).

    ``need`` is the catalogue's safe worst case: every allowed family may
    complete ``max_per_run`` rounds, and each round may spend up to
    ``max_task_attempts`` dispatches before a success resets the failure streak.
    ``configured`` is the operator's explicit cap, or ``None`` when the run
    derived its allowance from the catalogue. ``ceiling`` is the unconditional
    backstop, so even a pathological catalogue terminates.
    """

    limit: int
    need: int
    ceiling: int
    configured: int | None
    max_task_attempts: int

    @property
    def derived(self) -> bool:
        """Whether the limit came from the catalogue rather than the operator."""
        return self.configured is None

    @property
    def below_need(self) -> bool:
        """Whether an explicit cap is smaller than the catalogue's safe need."""
        return self.configured is not None and self.configured < self.need

    def as_dict(self) -> dict[str, int | None]:
        """Render the budget for the report and structured log."""
        return {
            'limit': self.limit,
            'derived_need': self.need,
            'configured': self.configured,
            'absolute_ceiling': self.ceiling,
            'max_task_attempts': self.max_task_attempts,
        }

    def describe_stop(self) -> str:
        """Name the cap and the derived need when it stops the walk."""
        head = f'task budget of {self.limit} dispatch(es) exhausted'
        attempts = f'up to {self.max_task_attempts} attempt(s) per round'
        if self.configured is not None and self.configured > self.ceiling:
            return (
                f'{head}; the absolute ceiling is the reason: AECOINS_MAX_TASKS_PER_RUN={self.configured} '
                f"is above the {self.ceiling}-dispatch backstop; the catalogue's derived need was "
                f'{self.need} dispatch(es) ({attempts})'
            )
        if self.below_need:
            return (
                f"{head}; the configured cap is the reason: the catalogue's derived need was "
                f'{self.need} dispatch(es) ({attempts}), so AECOINS_MAX_TASKS_PER_RUN={self.configured} '
                'is below a legitimate day'
            )
        if self.need > self.ceiling:
            return (
                f"{head}; the absolute ceiling is the reason: the catalogue's derived need was "
                f"{self.need} dispatch(es) ({attempts}), above AECOINS_MAX_TASKS_PER_RUN's "
                f'{self.ceiling}-dispatch backstop'
            )
        return (
            f"{head}; the catalogue's derived need was {self.need} dispatch(es) ({attempts}), "
            'so the night used the full worst-case allowance'
        )


def resolve_task_budget(
    config: RunConfig,
    catalogue: TaskCatalogue,
    *,
    max_tasks: int | None = None,
) -> TaskBudget:
    """Derive the task allowance and apply the absolute ceiling (#166).

    An explicit ``max_tasks`` argument (a manual/dry-run override) wins over the
    configuration. An explicit environment cap is honoured up to
    :data:`MAX_TASK_DISPATCHES`; leaving it unset uses the catalogue's derived
    need. The caller warns when the configured value is below that need, because
    the cap can then skip a row a legitimate day would have claimed.
    """
    configured = config.max_tasks if max_tasks is None else max_tasks
    if configured is not None and configured < 1:
        raise ValueError(f'max_tasks must be positive, got {configured!r}')
    need = sum(spec.max_per_run * config.max_task_attempts for spec in catalogue.allowed)
    limit = min(MAX_TASK_DISPATCHES, need if configured is None else configured)
    return TaskBudget(
        limit=limit,
        need=need,
        ceiling=MAX_TASK_DISPATCHES,
        configured=configured,
        max_task_attempts=config.max_task_attempts,
    )


_RotationSignature = tuple[tuple[str, str | None, tuple[int, int] | None, str | None, bool], ...]


@dataclass(slots=True)
class _RotationTracker:
    """Count readable sheet samples and how long the latest content has agreed.

    The sheet rotates between loads, so one read is one sample. A night may only
    be called finished when the last two readable reads agree *and* one of them
    was a fresh re-open. ``agreeing`` is the trailing run length, including the
    first read of that run; a changed row set (even a pure reorder is ignored by
    the signature's sort) resets it to one.
    """

    samples: int = 0
    agreeing: int = 0
    signature: _RotationSignature | None = None

    def record(self, roster: Roster) -> None:
        """Record one readable roster read."""
        current = _rotation_signature(roster)
        self.samples += 1
        self.agreeing = self.agreeing + 1 if current == self.signature else 1
        self.signature = current

    @property
    def confirmed(self) -> bool:
        """Whether the latest reads agree on the visible row set."""
        return self.agreeing >= 2


def _rotation_signature(roster: Roster) -> _RotationSignature:
    """The content set two rotation samples must share before a night closes."""
    rows = tuple(
        (
            normalize(entry.title),
            entry.key,
            (entry.progress.completed, entry.progress.total) if entry.progress is not None else None,
            normalize(entry.action.text) if entry.action is not None else None,
            entry.claimed,
        )
        for entry in roster.entries
    )
    return tuple(
        sorted(
            rows,
            key=lambda row: (row[0], row[1] or '', row[2] or (-1, -1), row[3] or '', row[4]),
        ),
    )


@dataclass(slots=True)
class RunContext:
    """Everything one night's engine may use, with every live seam injectable."""

    config: RunConfig
    capture: CaptureSource
    corpus: PopupCorpus
    catalogue: TaskCatalogue
    hygiene: HygieneDevice
    navigate: RouteFirer
    state: DailyStateStore
    app_foreground: Callable[[], bool | None] | None = None
    warm_start: Callable[[], None] | None = None
    shortcut_present: Callable[[], bool | None] | None = None
    launch_shortcut: Callable[[], None] | None = None
    remove_shortcut: Callable[[], None] | None = None
    controller: Controller | None = None
    controller_factory: Callable[[Snapshot], Controller] | None = None
    observer: RosterObserver | None = None
    family_store: FamilyObservationStore | None = None
    templates: TemplateStore | None = None
    scroll_rosters: bool = True
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    wall_clock: Callable[[], datetime] = _utc_now
    stop: StopSignal = field(default_factory=_NeverStop)
    logger: logging.Logger | None = None
    notifier: NotificationSink | None = None
    gate: DispatchGate | None = None
    safety: SafetyGuard | None = None
    record_round_grid: Callable[[int, Snapshot], None] | None = None
    record_foreground_loss: Callable[[str, Snapshot], None] | None = None


_EXECUTORS: frozenset[TaskCategory] = frozenset(
    {
        TaskCategory.SIGN_IN,
        TaskCategory.QUIZ,
        TaskCategory.BROWSE,
        TaskCategory.BROWSE_WAIT,
        TaskCategory.ITEM_TAP,
        TaskCategory.SHORTCUT_VISIT,
        TaskCategory.NOTIFICATION,
    },
)

DISPATCH_TABLE: dict[TaskCategory, str] = {
    TaskCategory.SIGN_IN: 'sign-in',
    TaskCategory.QUIZ: 'quiz',
    TaskCategory.BROWSE: 'search',
    TaskCategory.BROWSE_WAIT: 'browse-and-wait',
    TaskCategory.ITEM_TAP: 'surprise-items',
    TaskCategory.SHORTCUT_VISIT: 'shortcut-visit',
    TaskCategory.NOTIFICATION: 'notifications',
}
"""The catalogue categories this engine can dispatch, and their executor names.

The measured `Visit the page via coins shortcut` row is dispatchable through its
own executor. The older `shortcut`/`widget` setup families remain absent: their
launcher-item actions belong to #27 and are reported as skipped rather than
guessed at.
"""

TASK_RECOVERY_ATTEMPTS = UNEXPECTED_SCREEN_LIMIT
"""How many shortcut entries a task's failed screen gets before the run aborts.

The same number as the sighting limit keeps the rule easy to state: three
sightings fail the task, three unsuccessful returns fail the night (#83).
"""

MAX_NO_PROGRESS_PASSES = 2
"""How many consecutive sweeps may dispatch and reveal nothing before the walk stops.

A repeatable family needs one re-enumerated sweep per round because completing a
round reveals the next, so the useful pass bound is the roster's progress rather
than a fixed count (#141). One idle sweep is tolerated so a surface restart gets
a chance to recover; a second one ends the walk as ``budget`` while any still
dispatchable row is named in the detail.
"""

_RETRYABLE_SKIP_VERIFICATIONS = frozenset({'row_vanished'})
"""Skips that mean the roster was stale, not that the app finished the task.

A ``row_vanished`` skip is re-enumerated and may be dispatched again in a later
pass. ``row_complete``, ``daily_limit`` and ``already_done`` are terminal: the
app or the day has already accounted for the row.
"""

_SIGNIN_KEYS = frozenset({'daily_checkin', 'boost_checkin'})
"""Catalogue keys the combined sign-in executor can claim in one dispatch.

The streak and the boost row are independent claims behind one task record, so
a failure on either key must be judged against both daily counters (#143).
"""

_CATEGORY_COST: dict[TaskCategory, int] = {
    TaskCategory.SIGN_IN: 0,
    TaskCategory.QUIZ: 1,
    TaskCategory.BROWSE: 2,
    TaskCategory.ITEM_TAP: 3,
    TaskCategory.BROWSE_WAIT: 4,
    TaskCategory.NOTIFICATION: 5,
    TaskCategory.SHORTCUT_VISIT: 6,
}
"""How expensive one attempt is expected to be, cheapest first.

The walk is bounded, so *which* rows are left undone when a budget or pass ends
must be a policy, not whichever row OCR happened to put at the bottom. The
ranking is deliberately coarse and independent of the frame:

* ``sign_in`` is one tap and the most reliable coin;
* ``quiz`` is one page transition;
* ``browse`` is one submitted search on the landing page;
* ``item_tap`` needs several item taps and BACKs;
* ``browse_wait`` must sit out a timer before it can be collected;
* ``notification`` crosses into an OS dialog and is the least predictable;
* ``shortcut_visit`` also crosses into an OS surface: its measured ``GO`` opens
  the ``Create shortcut`` sheet, and the observed credit needed a launcher
  launch, so it ranks last among dispatchable work.

Only categories this engine can dispatch are ranked here; a rank for an
unreachable category would be dead policy. An unsupported row still has to be
ordered in reports and dry-run skips, so it sorts after every dispatchable
category without pretending to have a cost of its own. Within a cost rank the
catalogue key is stable across runs; the title and subtitle only break the rare
tie where OCR enumerated two rows for the same family. Reading order is not part
of the key.
"""


def make_live_context(
    config: RunConfig,
    *,
    stop: StopSignal,
    wall_clock: Callable[[], datetime] = _utc_now,
    logger: logging.Logger | None = None,
) -> RunContext:
    """Build the real adb/OCR context, after the configuration has been read.

    The sleeping, capture and navigation seams are wrapped so a SIGTERM raises
    :class:`RunInterrupted` at the next seam instead of letting a long executor
    keep tapping through the container's five-second stop grace period.
    """
    if config.device is None:
        raise ConfigError('AECOINS_DEVICE is not set; a live run needs a device serial')

    log = logger if logger is not None else LOG
    sleep = _guarded_sleep(stop)
    snapshotter = AdbSnapshotter(
        config.device,
        ocr=RapidOcrEngine(),
        clock=wall_clock,
    )
    device = AdbDevice(
        config.device,
        app_settle_seconds=config.app_settle_seconds,
        settle=sleep,
    )
    capture = _retrying_capture(
        _guarded_call(snapshotter.capture, stop),
        sleep=sleep,
        logger=log,
        check_connected=device.check_connected,
    )
    foreground = _guarded_call(device.is_app_foreground, stop)
    warm_start = _guarded_call(device.warm_start, stop)
    fire_route = _guarded_call(device.navigate, stop)
    read_shortcut = _guarded_call(device.coins_shortcut_pinned, stop)
    launch_shortcut_call = _guarded_call(device.launch_coins_shortcut, stop)
    remove_shortcut_call = _guarded_call(device.clear_app_shortcuts, stop)

    def shortcut_present() -> bool | None:
        return cast(bool | None, read_shortcut())

    def launch_shortcut() -> None:
        launch_shortcut_call()

    def remove_shortcut() -> None:
        remove_shortcut_call()

    def navigate(route: SurfaceRoute) -> None:
        if route is SurfaceRoute.SHORTCUT and foreground() is False:
            # False proves another app is foregrounded. None is unreadable, not
            # a loss: a warm start there would add a settle and relaunch the app
            # on every route. Browse recovery forces its own warm start from
            # screenshot NON_APP evidence even when the probe is unreadable.
            warm_start()
        fire_route(route)

    navigate = _guarded_call(navigate, stop)
    corpus = load_popup_corpus(config.corpus_path)
    catalogue = load_task_catalogue(config.catalogue_path)
    templates = load_templates(corpus)
    state = DailyStateStore(
        config.state_dir,
        clock=lambda: wall_clock().astimezone(config.timezone),
        logger=log,
    )
    input_device = AdbInputDevice(config.device)

    def controller_for(frame: Snapshot) -> Controller:
        return Controller.from_frame(
            input_device,
            frame,
            dry_run=config.dry_run,
            logger=log,
        )

    return RunContext(
        config=config,
        capture=capture,
        corpus=corpus,
        catalogue=catalogue,
        hygiene=device,
        navigate=navigate,
        app_foreground=foreground,
        warm_start=warm_start,
        shortcut_present=shortcut_present,
        launch_shortcut=launch_shortcut,
        remove_shortcut=remove_shortcut,
        state=state,
        controller_factory=controller_for,
        templates=templates,
        clock=time.monotonic,
        sleep=sleep,
        wall_clock=wall_clock,
        stop=stop,
        logger=log,
        notifier=build_notifier(config),
    )


def run_once(
    context: RunContext,
    *,
    budget_seconds: float | None = None,
    max_tasks: int | None = None,
    report: RunReport | None = None,
) -> RunReport:
    """Perform exactly one night and leave a report, whatever happens.

    ``budget_seconds`` and ``max_tasks`` default to the configuration. The
    returned report's :attr:`~aliexpress_coins.runner.report.RunReport.ok` is
    true for a completed or budget-ended run **unless** an allowed task was lost;
    a hygiene, route or report failure is a run-level failure. A walk that ends
    while an allowed task failed and is still claimable is not a success: it ends
    ``failed`` with a run failure whose detail names the task and its reason,
    even though every row that still looked dispatchable was consumed. A
    budget-ended walk that lost a task keeps ``budget_exhausted`` as its run
    failure code but is also reported ``failed`` with the task named. Every
    run-level failure is named by :mod:`aliexpress_coins.runner.failures` and
    recorded in ``run.json`` under ``failure``; a task that did not verify also
    carries its own code in the task row.
    """
    log = context.logger if context.logger is not None else LOG
    budget = context.config.budget_seconds if budget_seconds is None else budget_seconds
    task_budget = resolve_task_budget(context.config, context.catalogue, max_tasks=max_tasks)
    task_limit = task_budget.limit
    if budget <= 0:
        raise ValueError(f'budget_seconds must be positive, got {budget!r}')

    started = context.clock()
    report = (
        report
        if report is not None
        else RunReport.for_run(
            context.config.runs_dir,
            budget_seconds=budget,
            clock=context.wall_clock,
        )
    )
    controller = context.controller
    observer = context.observer
    roster: Roster | None = None
    final_roster: Roster | None = None
    status = RunStatus.COMPLETED
    detail: str | None = None
    attempted: set[str] = set()
    blocked: set[str] = set()
    passes = 0
    attempts: dict[str, int] = {}
    failures: dict[str, int] = {}
    blacklist: dict[str, int] = {}
    task_started: float | None = None
    artifacts = ArtifactStore(report.runs_dir, report.run_id)
    recorder = FailureRecorder(
        report,
        clock=context.clock,
        sleep=context.sleep,
        wall_clock=context.wall_clock,
        logger=log,
    )
    record_dismissal = _popup_recorder(report)
    if context.record_round_grid is None:

        def record_round_grid(index: int, frame: Snapshot) -> None:
            _record_frame(report, artifacts, frame, f'surprise-items-round-{index}-grid', log)

        context.record_round_grid = record_round_grid
    if context.record_foreground_loss is None:
        loss_counts: dict[str, int] = {}

        def record_foreground_loss(key: str, frame: Snapshot) -> None:
            count = loss_counts.get(key, 0) + 1
            loss_counts[key] = count
            name = f'browse-{key}-foreground-lost' if count == 1 else f'browse-{key}-foreground-lost-{count}'
            _record_frame(report, artifacts, frame, name, log)

        context.record_foreground_loss = record_foreground_loss
    event_log = RunEventLog(report.event_log_path)
    raw_capture = unwrap_capture(context.capture)
    log.addHandler(event_log)
    try:
        event_log.record(
            {'event': 'run_started', 'run_id': report.run_id, 'started_at': report.started_at.isoformat()},
        )
    except Exception as exc:
        log.warning('%s', _event('run_log_failed', detail=f'{type(exc).__name__}: {exc}'))

    safety = context.safety if context.safety is not None else SafetyGuard(catalogue=context.catalogue)
    context.safety = safety
    context.capture = safety.wrap(recorder.wrap(raw_capture))

    def attach_action_hook(current: object) -> None:
        setter = getattr(current, 'set_action_hook', None)
        if callable(setter):
            setter(recorder.pin_action)

    if context.controller is not None:
        attach_action_hook(context.controller)
    if context.controller_factory is not None:
        factory = context.controller_factory

        def controller_factory(frame: Snapshot) -> Controller:
            created = factory(frame)
            attach_action_hook(created)
            return created

        context.controller_factory = controller_factory
    gate = context.gate if context.gate is not None else DispatchGate(context.catalogue, logger=log)
    context.gate = gate

    try:
        report.write()
        _warn_pass_ceiling(context.config, context.catalogue, log)
        _warn_task_ceiling(task_budget, log)
        hygiene = _retry_stage(context, 'hygiene', context.hygiene.prepare)
        report.hygiene = hygiene.as_dict()
        frame = _capture(context.capture)
        _guard_signed_in(context, frame)
        if context.config.dry_run:
            _plan_dry_run(context, report, frame, gate, log, task_budget=task_budget, budget_seconds=budget)
            status = report.status
            detail = report.detail or 'dry run planned no tasks'
        else:
            report.geometry = _geometry(frame)
            log.info('%s', _event('run_hygiene', **hygiene.as_dict()))

            controller = controller if controller is not None else _controller(context, frame)
            _record_balance_before(
                context,
                controller,
                report,
                artifacts,
                frame,
                log,
                record_dismissal,
            )
            roster, route_error = _discover_or_fallback(context, controller, on_dismiss=record_dismissal)
            report.route_error = route_error
            if roster.empty and roster.surface_variant is Surface.UNKNOWN:
                # An empty roster is a load fault when the signed-out screen is
                # underneath it. Detect the account once and stop the whole night:
                # no task can be collected until a human signs in.
                _guard_signed_in(context, _capture(context.capture))
            report.roster = roster.as_dict()
            _record_roster_diagnostics(roster, log)
            report.surface = roster.surface_variant.value if roster.surface_variant is not None else None
            gate.surface = report.surface
            report.write()
            if roster.empty and roster.surface_variant is Surface.NON_APP:
                raise StateTimeout('the shortcut route did not leave the non-app foreground surface')
            context.state.begin_run(at=context.wall_clock())
            observer = (
                observer
                if observer is not None
                else _observer(
                    context,
                    report,
                    roster,
                    max_observations=task_limit + context.config.max_passes + 2,
                )
            )
            family_store = context.family_store or FamilyObservationStore.from_state(
                context.state,
                clock=lambda: context.wall_clock().astimezone(context.config.timezone),
                logger=log,
            )
            union_roster: Roster | None = roster
            rotation = _RotationTracker()
            rotation.record(roster)
            confirmation_attempted = False
            confirmation_read = False
            rotation_incomplete = False
            last_observation: RosterObservation | None = None

            def absorb(observation: RosterObservation | None) -> bool:
                """Fold one readable observation into the night's union of rows."""
                nonlocal union_roster, last_observation
                last_observation = observation
                if observation is None:
                    return False
                if observation.roster is None or observation.failed or observation.cap_reached:
                    return False
                union_roster = merge_rosters(union_roster, observation.roster)
                rotation.record(observation.roster)
                return True

            absorb(
                _observe(
                    context,
                    controller,
                    observer,
                    route=roster.route,
                    on_dismiss=record_dismissal,
                ),
            )
            _record_policy_skips(report, roster, logger=log, gate=gate)
            report.observations = observer.sequence
            report.write()

            probe_day = context.wall_clock().astimezone(context.config.timezone).date()
            probe_due = _due_probe_keys(family_store, context.catalogue, probe_day, log)
            probed: set[str] = set()
            if probe_due:
                # A dormant family is due one explicit extra sample on the
                # slower cadence. Take it before selection so a row that is back
                # can be dispatched by this same run.
                if absorb(
                    _restart_surface(
                        context,
                        controller,
                        observer,
                        on_dismiss=record_dismissal,
                        reason=(
                            'dormant family probe due: take one extra sample for '
                            + ', '.join(repr(key) for key in probe_due)
                        ),
                    ),
                ):
                    probed.update(probe_due)
                else:
                    log.warning(
                        '%s',
                        _event(
                            'family_probe_failed',
                            families=list(probe_due),
                            detail='the extra probe sample was not readable; its cadence does not advance',
                        ),
                    )
                _record_policy_skips(
                    report,
                    observer.roster or roster,
                    logger=log,
                    gate=gate,
                )
                report.observations = observer.sequence
                report.write()

            max_passes = context.config.max_passes
            max_attempts = context.config.max_task_attempts
            report.max_passes = max_passes
            report.max_task_attempts = max_attempts
            blacklist = dict(context.state.snapshot().blacklist)
            report.blacklist = dict(blacklist)
            attempted_this_pass: set[str] = set()
            settled: set[str] = set()
            passes = 1
            report.passes = passes
            dispatched_this_pass = False
            no_progress_passes = 0
            pass_start_dispatchable = _dispatchable_identities(
                union_roster if union_roster is not None else roster,
                context.state,
                blocked=blocked,
                blacklisted=blacklist,
                settled=settled,
            )

            while True:
                if context.stop.is_set():
                    status = RunStatus.STOPPED
                    detail = 'stop requested between tasks'
                    break
                elapsed = max(0.0, context.clock() - started)
                if elapsed >= budget:
                    status = RunStatus.BUDGET
                    detail = f'wall-clock budget of {budget:g}s exhausted'
                    report.record_failure(failure(FailureCode.BUDGET_EXHAUSTED, detail=detail))
                    break
                if len(report.tasks) >= task_limit:
                    status = RunStatus.BUDGET
                    detail = task_budget.describe_stop()
                    report.record_failure(failure(FailureCode.BUDGET_EXHAUSTED, detail=detail))
                    break

                latest = observer.roster if observer.roster is not None else roster
                if latest is None:  # pragma: no cover - set immediately above
                    break
                union_roster = merge_rosters(union_roster, latest)
                current = union_roster
                current_dispatchable = _dispatchable_identities(
                    current,
                    context.state,
                    blocked=blocked,
                    blacklisted=blacklist,
                    settled=settled,
                )
                progressed = _pass_progressed(
                    dispatched_this_pass,
                    current_dispatchable,
                    pass_start_dispatchable,
                )
                surface_reason = _pass_restart_reason(latest, last_observation)
                if surface_reason is not None:
                    bound, no_progress_passes = _next_pass_bound(
                        passes=passes,
                        max_passes=max_passes,
                        no_progress_passes=no_progress_passes,
                        progressed=progressed,
                    )
                    if bound is not None:
                        status = RunStatus.BUDGET
                        detail = f'{bound}; {surface_reason}'
                        report.record_failure(failure(FailureCode.BUDGET_EXHAUSTED, detail=detail))
                        rotation_incomplete = confirmation_attempted and not confirmation_read
                        break
                    passes += 1
                    report.passes = passes
                    attempted_this_pass.clear()
                    dispatched_this_pass = False
                    confirmation_attempted = False
                    confirmation_read = False
                    pass_start_dispatchable = current_dispatchable
                    absorb(
                        _restart_surface(
                            context,
                            controller,
                            observer,
                            on_dismiss=record_dismissal,
                            reason=surface_reason,
                        ),
                    )
                    _record_policy_skips(
                        report,
                        observer.roster or current,
                        logger=log,
                        gate=gate,
                    )
                    report.observations = observer.sequence
                    report.write()
                    continue

                entry = _next_entry(
                    current,
                    context.state,
                    blocked,
                    blacklisted=blacklist,
                    attempted=attempted_this_pass,
                    settled=settled,
                )
                if entry is None:
                    pending = _next_entry(
                        current,
                        context.state,
                        blocked,
                        blacklisted=blacklist,
                        settled=settled,
                    )
                    restart_reason = _stuck_row_reason(
                        current,
                        context.state,
                        settled=settled,
                        blocked=blocked,
                        blacklisted=blacklist,
                    )
                    if pending is None and restart_reason is None:
                        if latest.incomplete:
                            # The read already proves the night is open. Another
                            # sample cannot turn an incomplete roster into proof.
                            detail = 'no dispatchable row remains'
                            break
                        if not confirmation_attempted:
                            # A finished pass is only one rotation sample. Re-open
                            # the sheet once inside this pass and compare what comes
                            # back before the night may be called complete. This is
                            # the bound: one proof read per pass, not a poll.
                            confirmation_attempted = True
                            confirmation_read = absorb(
                                _restart_surface(
                                    context,
                                    controller,
                                    observer,
                                    on_dismiss=record_dismissal,
                                    reason=(
                                        'one rotation sample is not proof the night is finished; '
                                        're-open the Coins boost sheet'
                                    ),
                                ),
                            )
                            _record_policy_skips(
                                report,
                                observer.roster or current,
                                logger=log,
                                gate=gate,
                            )
                            report.observations = observer.sequence
                            report.write()
                            continue
                        if (
                            confirmation_read
                            and rotation.confirmed
                            and last_observation is not None
                            and not last_observation.failed
                        ):
                            detail = 'no dispatchable row remains'
                            break
                        # The fresh open still disagrees with the read before it.
                        # Spend a pass, never a busy loop, and stop honestly if
                        # the sheet keeps rotating without two agreeing reads.
                        bound, no_progress_passes = _next_pass_bound(
                            passes=passes,
                            max_passes=max_passes,
                            no_progress_passes=no_progress_passes,
                            progressed=progressed,
                        )
                        if bound is not None:
                            status = RunStatus.BUDGET
                            detail = (
                                f'{bound}; the Coins boost sheet kept rotating without two agreeing reads '
                                f'({rotation.samples} sample(s), {rotation.agreeing} agreeing)'
                            )
                            report.record_failure(failure(FailureCode.BUDGET_EXHAUSTED, detail=detail))
                            rotation_incomplete = True
                            break
                        passes += 1
                        report.passes = passes
                        attempted_this_pass.clear()
                        dispatched_this_pass = False
                        confirmation_attempted = False
                        confirmation_read = False
                        pass_start_dispatchable = current_dispatchable
                        # The next loop's confirmation branch performs the fresh
                        # open for this pass; opening here too spent two reads per
                        # disagreeing pass.
                        report.write()
                        continue
                    bound, no_progress_passes = _next_pass_bound(
                        passes=passes,
                        max_passes=max_passes,
                        no_progress_passes=no_progress_passes,
                        progressed=progressed,
                    )
                    if bound is not None:
                        status = RunStatus.BUDGET
                        detail = bound
                        if pending is not None:
                            detail = f'{detail}; {_identity(pending)!r} is still dispatchable'
                        elif restart_reason is not None:
                            detail = f'{detail}; {restart_reason}'
                        report.record_failure(failure(FailureCode.BUDGET_EXHAUSTED, detail=detail))
                        break
                    passes += 1
                    report.passes = passes
                    attempted_this_pass.clear()
                    dispatched_this_pass = False
                    confirmation_attempted = False
                    confirmation_read = False
                    pass_start_dispatchable = current_dispatchable
                    if restart_reason is not None:
                        absorb(
                            _restart_surface(
                                context,
                                controller,
                                observer,
                                on_dismiss=record_dismissal,
                                reason=restart_reason,
                            ),
                        )
                        _record_policy_skips(
                            report,
                            observer.roster or current,
                            logger=log,
                            gate=gate,
                        )
                        report.observations = observer.sequence
                    report.write()
                    continue

                spec = entry.spec
                if spec is None:  # pragma: no cover - _next_entry only returns allowed rows
                    continue
                identity = _identity(entry)
                attempts_before = attempts.get(identity, 0)
                task_started = context.clock()
                task_index = report.begin_task(
                    title=entry.title,
                    key=spec.key,
                    category=spec.category.value,
                )
                report.write()

                def note_attempt(_identity: str = identity) -> None:
                    attempts[_identity] = attempts.get(_identity, 0) + 1

                try:
                    with _task_budget(context, started + budget):
                        result = _dispatch_with_retry(
                            context,
                            controller,
                            entry,
                            on_attempt=note_attempt,
                            recorder=recorder,
                        )
                except RunBudgetExceeded as exc:
                    # The task may have tapped before it was cut off, so a later
                    # run must re-check the fresh row instead of dispatching it
                    # blind. Mark conservatively; an unacted row simply re-checks.
                    _mark_acted_unverified(context, spec.key)
                    detail = f'wall-clock budget of {budget:g}s exhausted during {entry.title!r}'
                    report.abort_in_flight(
                        code=RunStatus.BUDGET.value,
                        reason=str(exc),
                        failure=failure(FailureCode.BUDGET_EXHAUSTED, detail=detail),
                        duration_seconds=_task_duration(context, task_started),
                    )
                    task_started = None
                    status = RunStatus.BUDGET
                    # The aborted task row is the record of this dispatch; do
                    # not also list the same row as a never-run budget skip.
                    blocked.add(identity)
                    report.record_failure(failure(FailureCode.BUDGET_EXHAUSTED, detail=detail))
                    report.write()
                    break
                made = attempts.get(identity, 0) - attempts_before
                if made > 0:
                    # A real attempt — completed, failed or a stale-row skip —
                    # is the progress that earns another sweep (#141).
                    dispatched_this_pass = True
                duration = max(0.0, context.clock() - task_started)
                attempted.add(identity)
                attempted_this_pass.add(identity)
                _record_dismissals(report, result.as_dict())
                report.finish_task(
                    task_index,
                    outcome=result.outcome,
                    verification=result.verification,
                    detail=result.detail,
                    duration_seconds=duration,
                    completed=result.completed,
                    result=result.as_dict(),
                    failure=result.failure,
                )
                task_started = None
                log.log(
                    task_log_level(result.outcome),
                    '%s',
                    _event(
                        'task_outcome',
                        key=spec.key,
                        title=entry.title,
                        outcome=result.outcome,
                        verification=result.verification,
                        completed=result.completed,
                        code=None if result.failure is None else result.failure.code.value,
                        seconds=round(duration, 3),
                        attempt=attempts.get(identity, 0),
                        pass_number=passes,
                    ),
                )
                if result.completed or made > 0:
                    # Re-enumerate after every real attempt, including a failure
                    # and a row that vanished between enumeration and dispatch.
                    # A no-tap refusal/re-check did not change the surface.
                    confirmation_attempted = False
                    confirmation_read = False
                    absorb(
                        _observe(
                            context,
                            controller,
                            observer,
                            route=None,
                            on_dismiss=record_dismissal,
                        ),
                    )
                    _record_policy_skips(report, observer.roster or current, logger=log, gate=gate)
                    report.observations = observer.sequence

                if result.completed:
                    # Completion is per-pass: a repeatable row below its daily
                    # limit is offered again on the next sweep. The daily limit
                    # itself is still state.count() in _next_entry. A success
                    # resets the failure streak; it is not an attempt against
                    # the blacklist bound.
                    failures.pop(identity, None)
                    blacklist.pop(identity, None)
                elif made == 0:
                    # Nothing was tapped and no attempt was spent. Keep the
                    # #93 safety rule and do not spin the same refusal in a
                    # later pass; the row's failure remains in the report.
                    blocked.add(identity)
                elif _needs_another_pass(result):
                    failed = failures.get(identity, 0) + 1
                    failures[identity] = failed
                    if result.acted:
                        # #93: an action whose effect is unknown is never repeated
                        # inside the same run. The next day re-checks the fresh row.
                        blocked.add(identity)
                    elif failed >= max_attempts:
                        blacklist = _blacklist_task(
                            report,
                            context.state,
                            entry,
                            attempts=failed,
                            blacklist=blacklist,
                            logger=log,
                        )
                    else:
                        named = result.failure
                        if named is not None and named.retryable:
                            delay = retry_delay(failed - 1)
                            log.warning(
                                '%s',
                                _event(
                                    'failure_retry',
                                    code=named.code.value,
                                    scope=named.retry.value,
                                    stage='task',
                                    key=spec.key,
                                    attempt=failed,
                                    delay_seconds=delay,
                                ),
                            )
                            context.sleep(delay)
                    # Otherwise the row stays pending for the next pass.
                else:
                    settled.add(identity)
                report.write()

            report.passes = passes
            report.attempts = dict(attempts)
            report.blacklist = dict(blacklist)
            latest_roster = observer.roster if observer is not None and observer.roster is not None else roster
            final_roster = merge_rosters(union_roster, latest_roster) if latest_roster is not None else union_roster
            if final_roster is not None:
                read_trustworthy = rotation.confirmed and not rotation_incomplete and not final_roster.incomplete
                unread_families, already_claimed_families, not_offered, dormant = _family_rotation(
                    final_roster,
                    context.catalogue,
                    context.state,
                    blacklist,
                    family_store,
                    day=context.wall_clock().astimezone(context.config.timezone).date(),
                    samples=rotation.samples,
                    dormancy_days=context.config.family_dormancy_days,
                    retry_days=context.config.family_retry_days,
                    probed=probed,
                    trustworthy=read_trustworthy,
                    logger=log,
                )
                final_roster = replace(
                    final_roster,
                    rotation_samples=rotation.samples,
                    rotation_agreeing_samples=rotation.agreeing,
                    rotation_confirmed=rotation.confirmed and not rotation_incomplete,
                    unread_families=unread_families,
                    not_offered_families=not_offered,
                    dormant_families=dormant,
                    already_claimed_families=already_claimed_families,
                )
            _record_unrun(
                report,
                final_roster,
                context.state,
                attempted,
                status=status,
                detail=detail,
                logger=log,
                blacklisted=blacklist,
                blocked=blocked,
                settled=settled,
            )
            if observer is not None:
                report.observations = observer.sequence
            if final_roster is not None:
                report.roster = final_roster.as_dict()
                _record_roster_diagnostics(final_roster, log)
                report.surface = (
                    final_roster.surface_variant.value if final_roster.surface_variant is not None else report.surface
                )
    except RunAborted as exc:
        status = RunStatus.ABORTED
        if context.safety is not None:
            context.safety.disarm()
        report.abort = exc.as_dict()
        report.record_failure(exc.failure)
        detail = exc.failure.describe()
        report.abort_in_flight(
            code=exc.code.value,
            reason=exc.reason,
            failure=exc.failure,
            screen=exc.screen.as_dict(),
            duration_seconds=_task_duration(context, task_started),
        )
        log.error('%s', _event('run_aborted', code=exc.code.value, detail=detail))
    except RunInterrupted as exc:
        status = RunStatus.STOPPED
        detail = f'stop requested: {exc}'
        report.abort_in_flight(
            code=RunStatus.STOPPED.value,
            reason=detail,
            duration_seconds=_task_duration(context, task_started),
        )
        log.warning('%s', _event('run_stopped', detail=detail))
    except Exception as exc:
        status = RunStatus.FAILED
        named = classify_exception(exc, default=FailureCode.INTERNAL_ERROR)
        if named is None:  # pragma: no cover - RunInterrupted is handled above
            raise
        report.record_failure(named)
        detail = named.describe()
        report.error = f'{type(exc).__name__}: {exc}'
        report.abort_in_flight(
            code=named.code.value,
            reason=detail,
            failure=named,
            duration_seconds=_task_duration(context, task_started),
        )
        log.exception('%s', _event('run_failed', detail=detail))
    finally:
        try:
            if (
                controller is not None
                and not context.config.dry_run
                and report.failure_code != FailureCode.APP_LOGGED_OUT.value
            ):
                _finish_on_coins_page(
                    context,
                    controller,
                    report,
                    artifacts,
                    log,
                    on_dismiss=record_dismissal,
                )
            if status is RunStatus.FAILED:
                # A dry run has already ended; use the unwrapped source so the
                # diagnostic frame cannot trip the guard and overwrite the
                # planner's named failure.
                _record_failure_frame(
                    context,
                    report,
                    artifacts,
                    log,
                    capture=raw_capture if context.config.dry_run else None,
                )
        except RunAborted as exc:
            status = RunStatus.ABORTED
            if context.safety is not None:
                context.safety.disarm()
            report.abort = exc.as_dict()
            report.record_failure(exc.failure)
            detail = exc.failure.describe()
            report.abort_in_flight(
                code=exc.code.value,
                reason=exc.reason,
                failure=exc.failure,
                screen=exc.screen.as_dict(),
                duration_seconds=_task_duration(context, task_started),
            )
            log.error('%s', _event('run_aborted', code=exc.code.value, detail=detail))
        if status in (RunStatus.COMPLETED, RunStatus.BUDGET) and not context.config.dry_run:
            finished_walk = status is RunStatus.COMPLETED
            rotation_open = bool(
                report.roster
                and cast(dict[str, Any], report.roster.get('rotation') or {}).get('confirmed') is False
                and not _roster_incomplete_without_rotation(report.roster),
            )
            incomplete = _incomplete_run_detail(report, context.state, context.catalogue)
            if incomplete is not None:
                # A finished walk is not a finished day. Every allowed row that
                # ended failed, or is still blacklisted, remains unclaimed work;
                # the run must not look like a clean night (#143). A budget stop
                # that already lost a task is not a clean budget stop either.
                if status is RunStatus.BUDGET:
                    detail = f'{detail}; {incomplete}' if detail else incomplete
                else:
                    detail = incomplete
                    report.record_failure(failure(FailureCode.TASK_UNVERIFIED, detail=incomplete))
                status = RunStatus.FAILED
            if finished_walk or incomplete is not None or rotation_open:
                night = _night_record(report, context.state, context.catalogue, roster=final_roster)
                try:
                    context.state.record_night(night)
                except Exception as exc:
                    # The report must not claim a day state the store never accepted.
                    log.warning('%s', _event('state_complete_failed', detail=f'{type(exc).__name__}: {exc}'))
                else:
                    report.night = night.as_dict()
                    log.log(
                        logging.INFO if night.complete else logging.WARNING,
                        '%s',
                        _event(
                            'night_decided',
                            outcome=night.outcome.value,
                            complete=night.complete,
                            dispatched=night.dispatched,
                            completed=night.completed,
                            retryable_failures=night.retryable_failures,
                        ),
                    )
        finalized = False
        try:
            report.passes = passes
            report.attempts = dict(attempts)
            report.blacklist = dict(blacklist)
            used = max(0.0, context.clock() - started)
            report.finish(status, detail=detail, budget_used_seconds=used, finished_at=context.wall_clock())
            report.write()
            finalized = True
            log.log(
                run_log_level(report.status),
                '%s',
                _event(
                    'run_finished',
                    run_id=report.run_id,
                    status=report.status.value,
                    failure_code=report.failure_code,
                    budget_used_seconds=round(report.budget_used_seconds, 3),
                    tasks=len(report.tasks),
                    skipped=len(report.skipped),
                ),
            )
        finally:
            # The report is final before cleanup touches adb. Restore comes
            # before delivery or retention so a slow webhook cannot outlive the
            # container's stop grace and leave rotation pinned.
            try:
                context.hygiene.restore()
            except Exception as exc:
                log.warning('%s', _event('hygiene_restore_failed', detail=f'{type(exc).__name__}: {exc}'))
            finally:
                try:
                    if finalized:
                        _notify(context, report, log)
                        _prune(context, report, log)
                finally:
                    log.removeHandler(event_log)
                    event_log.close()
    return report


def _plan_dry_run(
    context: RunContext,
    report: RunReport,
    frame: Snapshot,
    gate: DispatchGate,
    log: logging.Logger,
    *,
    task_budget: TaskBudget,
    budget_seconds: float,
) -> None:
    """Plan the night from the settled visible surface without tapping.

    The route may be fired to reach the coins page (an ``am start``, not an
    in-app tap), then the same :func:`wait_for_coin_page` a live run uses must
    see the page stop loading before the roster is enumerated. A recognized
    task surface with missing rows also waits for frame stability and performs
    a bounded re-read of the actual row set. Those are the #19 primitives behind
    #76/#120: classifying a partial render once and calling the resulting zero
    a plan is the "absence is not evidence" failure in the planning path.

    The #38 route budget still applies: a readable surface with no usable rows
    gets the same warm re-entry a live run gets, which is what resets the
    documented empty-``Coins boost`` load fault. Every row goes through the same
    :class:`DispatchGate`; allowed rows that have an executor, an action and
    daily budget left become ordered steps, and refusals carry the gate's code,
    rule and reason. No executor runs and the controller is never asked for a
    tap.

    A surface that never becomes readable is not a clean zero. It is reported
    as a ``navigation_failed`` dry run with ``surface_unknown`` in the detail,
    so the CLI exits non-zero instead of telling the operator the day is empty.
    """
    max_tasks = task_budget.limit
    surface = classify_surface(frame)
    initial_state = classify(frame).state
    routes: list[SurfaceRoute] = []
    waits: list[CoinPageWait] = []
    stable_waits: list[dict[str, object]] = []
    content_waits: list[dict[str, object]] = []
    roster_waits: list[dict[str, object]] = []
    settle_note: str | None = None

    def fire_route() -> bool:
        nonlocal settle_note
        try:
            context.navigate(SurfaceRoute.SHORTCUT)
        except RunAborted, RunInterrupted:
            raise
        except Exception as exc:
            settle_note = f'could not read the coins page: {type(exc).__name__}: {exc}'
            report.plan_note = settle_note
            return False
        routes.append(SurfaceRoute.SHORTCUT)
        return True

    def settle(*, await_page: bool) -> None:
        nonlocal frame, surface
        if await_page:
            arrival = wait_for_coin_page(
                context.capture,
                timeout=DEFAULT_BOOST_TIMEOUT,
                interval=DEFAULT_BOOST_INTERVAL,
                clock=context.clock,
                sleep=context.sleep,
            )
            waits.append(arrival)
            frame = arrival.snapshot
            surface = classify_surface(frame)
            if arrival.classification.state is ScreenState.POPUP or not arrival.settled:
                # A cover or a still-loading frame is not a readable page. Do not
                # spend the row wait on it; the scheduler below names it.
                return
        if surface not in (Surface.ROWS_ABSENT, Surface.ROWS_PRESENT, Surface.BOOST_PAGE, Surface.EMPTY_BOOST):
            return
        if surface is Surface.EMPTY_BOOST:
            # The empty sentence is already the named load fault. It classifies
            # as ERROR, so polling it through the safety guard would turn a
            # probe failure into an unexpected-screen abort before it is named.
            return

        # A page marker can arrive before its rows. Frame stability is the
        # evidence that rendering stopped; elapsed time alone is not (#120).
        stable = wait_until_stable(
            context.capture,
            initial=frame,
            clock=context.clock,
            sleep=context.sleep,
        )
        stable_waits.append(stable.as_dict())
        frame = stable.frame
        surface = classify_surface(frame)
        if classify(frame).state is ScreenState.POPUP:
            return
        if surface in (Surface.ROWS_ABSENT, Surface.ROWS_PRESENT, Surface.BOOST_PAGE):
            # Re-read the actual row set after settling. has_task_rows() also
            # accepts a bare title, which is exactly the partial render that
            # used to become a confident surface_incomplete (#120).
            content = wait_for_text(
                context.capture,
                lambda candidate: _row_wait_target(candidate, context.catalogue),
                timeout=DEFAULT_BOOST_TIMEOUT,
                interval=DEFAULT_BOOST_INTERVAL,
                initial=frame,
                clock=context.clock,
                sleep=context.sleep,
            )
            content_waits.append(content.as_dict())
            frame = content.frame
            surface = classify_surface(frame)

    def read_roster(*, scroll: bool = True) -> Roster:
        controller = context.controller
        if controller is None and context.controller_factory is not None:
            controller = _controller(context, frame)
        if controller is not None and scroll and surface is Surface.BOOST_PAGE and context.scroll_rosters:
            roster = collect_roster(
                controller,
                context.capture,
                context.catalogue,
                frame,
                route=DiscoveryRoute.INLINE,
                settle=context.config.settle_seconds,
                sleep=context.sleep,
            )
        else:
            roster = enumerate_roster(frame, context.catalogue, route=DiscoveryRoute.INLINE)
        return replace(
            roster,
            surface_variant=surface,
            routes_taken=tuple(routes),
        )

    def confirm_roster(current: Roster) -> Roster:
        """Read until two fresh captures agree on the non-empty row set.

        A list can paint row 1 before row 2. One enumerable row is not proof
        that the roster finished rendering, so compare the actual titles,
        actions and badges before planning. A popup or error frame ends the
        wait immediately and is named by the scheduler below instead of being
        polled until the safety guard aborts.
        """
        nonlocal frame, surface
        previous: tuple[tuple[str, str | None, tuple[int, int] | None, str | None], ...] | None = None
        if current.empty:
            return current
        if current.scroll_count > 0:
            # Collection already merged every frame until the list stopped
            # revealing new titles. Re-reading one frame here would throw the
            # first screenful away and plan only the bottom of the list.
            return current

        def settled(candidate: Snapshot) -> Snapshot | None:
            nonlocal previous
            if classify(candidate).state in (ScreenState.POPUP, ScreenState.ERROR):
                return candidate
            candidate_roster = enumerate_roster(candidate, context.catalogue, route=DiscoveryRoute.INLINE)
            if candidate_roster.empty:
                previous = None
                return None
            signature = _roster_signature(candidate_roster)
            if signature == previous:
                return candidate
            previous = signature
            return None

        result = wait_for_text(
            context.capture,
            settled,
            timeout=DEFAULT_BOOST_TIMEOUT,
            interval=DEFAULT_BOOST_INTERVAL,
            clock=context.clock,
            sleep=context.sleep,
        )
        roster_waits.append(result.as_dict())
        frame = result.frame
        surface = classify_surface(frame)
        return read_roster()

    # The probe decides which route to take; the whole-list scroll happens once
    # on the settled surface below. Scrolling the probe would spend a gesture on
    # a page that may be replaced by the route or settle wait.
    initial_roster = read_roster(scroll=False)
    needs_route = (
        (surface is Surface.UNKNOWN and initial_roster.empty)
        or (surface is Surface.NON_APP and initial_roster.empty)
        or initial_state is ScreenState.POPUP
        or surface in (Surface.ROWS_ABSENT, Surface.EMPTY_BOOST)
    )
    if needs_route:
        if fire_route():
            # A popup may be cleared by the route, and an unclassified or empty
            # surface gets one shortcut attempt. A populated page is read as is.
            settle(await_page=True)
            roster = read_roster()
        else:
            roster = initial_roster
    elif surface in (Surface.ROWS_ABSENT, Surface.ROWS_PRESENT, Surface.BOOST_PAGE):
        # The probe can arrive directly on a boost page whose header and titles
        # rendered before its controls, or with only the first row painted.
        # Settle that page in place instead of reporting the first partial frame
        # or needlessly re-navigating.
        settle(await_page=False)
        roster = read_roster()
    else:
        roster = initial_roster

    if surface in (Surface.ROWS_ABSENT, Surface.ROWS_PRESENT, Surface.BOOST_PAGE):
        roster = confirm_roster(roster)

    frame_state = classify(frame).state
    unreadable = _unreadable_plan_reason(
        roster,
        surface,
        waits,
        stable_waits,
        content_waits,
        roster_waits,
        settle_note,
        initial_state,
        frame_state,
    )
    if unreadable is not None and frame_state is ScreenState.POPUP:
        # The classifier can see coin markers and rows through a modal; a
        # popup is not a readable coin page, so report it as unread rather
        # than planning the rows under the scrim. A non-popup unreadable
        # frame keeps any rows it did show as evidence.
        surface = Surface.UNKNOWN
        roster = Roster(
            entries=(),
            route=DiscoveryRoute.INLINE,
            catalogue_version=context.catalogue.version,
            default_deny_reason=context.catalogue.default_deny_reason,
            surface_variant=surface,
            routes_taken=tuple(routes),
        )

    report.geometry = _geometry(frame)
    report.roster = roster.as_dict()
    _record_roster_diagnostics(roster, log)
    report.surface = surface.value
    gate.surface = surface.value
    if _on_coin_page(frame):
        balance = read_coin_balance(frame)
        report.balance_before = balance
        report.balance_after = balance
        if balance is None:
            report.balance_note = 'the coins page did not render a readable balance for the dry-run baseline'

    steps: list[dict[str, object]] = []
    refusals: list[dict[str, object]] = []
    for entry in _ordered_entries(roster.entries):
        try:
            authorized = gate.authorize(entry)
        except PolicyRefused as exc:
            refusal = exc.refusal
            refusals.append(refusal.as_dict())
            _skip(
                report,
                log,
                title=entry.title,
                key=None,
                category=None,
                reason='policy',
                detail=f'{refusal.code}: {refusal.reason}',
            )
            continue

        spec = authorized.spec
        if spec is None:  # pragma: no cover - authorize only returns allowed rows
            continue
        if spec.category not in _EXECUTORS:
            _skip(
                report,
                log,
                title=authorized.title,
                key=spec.key,
                category=spec.category.value,
                reason='no executor',
                detail=f'{spec.category.value!r} has no executor in this issue',
            )
            continue
        if authorized.dispatch_target is None:
            _skip(
                report,
                log,
                title=authorized.title,
                key=spec.key,
                category=spec.category.value,
                reason='already claimed' if entry.claimed else 'no action',
                detail=(
                    'the row is already claimed on this sheet; no action is available'
                    if entry.claimed
                    else 'the row has no dispatchable action in this frame'
                ),
            )
            continue
        if context.state.count(spec.key) >= spec.max_per_run:
            _skip(
                report,
                log,
                title=authorized.title,
                key=spec.key,
                category=spec.category.value,
                reason='daily limit',
                detail=f'{spec.key!r} already reached its {spec.max_per_run} completion(s) today',
            )
            continue
        if len(steps) >= max_tasks:
            _skip(
                report,
                log,
                title=authorized.title,
                key=spec.key,
                category=spec.category.value,
                reason='run budget',
                detail=(
                    f'the task budget of {max_tasks} would stop the walk here; '
                    f"the catalogue's derived need is {task_budget.need} dispatch(es)"
                ),
            )
            continue
        steps.append(
            {
                'order': len(steps) + 1,
                'label': authorized.title,
                'key': spec.key,
                'category': spec.category.value,
                'rule': spec.key,
                'surface': surface.value,
            },
        )

    if unreadable is not None:
        report.status = RunStatus.FAILED
        report.detail = unreadable
        report.record_failure(failure(FailureCode.NAVIGATION_FAILED, detail=unreadable))
        plan_detail = f'dry-run probe failed: {unreadable}; no tap was issued'
    else:
        report.status = RunStatus.PLANNED
        report.detail = f'dry run: {len(steps)} task(s) planned, {len(refusals)} refusal(s); no tap was issued'
        plan_detail = report.detail

    rows_read = roster.count
    rows_allowed = len(roster.allowed_entries)
    rows_refused = len(roster.denied_entries)
    rows_dispatchable = len(roster.dispatchable_entries)
    rows_claimed = len(roster.claimed_entries)
    report.plan = {
        'dry_run': True,
        'probe_failed': unreadable is not None,
        'surface': surface.value,
        'max_tasks': max_tasks,
        'task_budget': task_budget.as_dict(),
        'budget_seconds': budget_seconds,
        'rows_read': rows_read,
        'rows_allowed': rows_allowed,
        'rows_refused': rows_refused,
        'rows_dispatchable': rows_dispatchable,
        'rows_claimed': rows_claimed,
        'steps': steps,
        'refusals': refusals,
        'taps_issued': 0,
        'waits': [wait.as_dict() for wait in waits],
        'stable_waits': stable_waits,
        'content_waits': content_waits,
        'roster_waits': roster_waits,
        'detail': plan_detail,
    }
    log.log(
        logging.ERROR if unreadable is not None else logging.INFO,
        '%s',
        _event(
            'dry_run_plan',
            surface=surface.value,
            rows_read=rows_read,
            rows_allowed=rows_allowed,
            rows_refused=rows_refused,
            rows_dispatchable=rows_dispatchable,
            rows_claimed=rows_claimed,
            steps=[step['label'] for step in steps],
            refusals=[refusal['label'] for refusal in refusals],
            taps_issued=0,
            probe_failed=unreadable is not None,
            unreadable=unreadable,
        ),
    )
    report.write()


def _has_plan_rows(snapshot: Snapshot, catalogue: TaskCatalogue) -> bool:
    """Whether ``snapshot`` contains an enumerable row, not just a rendered title."""
    if classify(snapshot).state is ScreenState.POPUP:
        return False
    return not enumerate_roster(snapshot, catalogue, route=DiscoveryRoute.INLINE).empty


def _row_wait_target(snapshot: Snapshot, catalogue: TaskCatalogue) -> Snapshot | None:
    """Return a frame that has rows or names a terminal surface.

    The row wait must stop on a popup or an error page as well as on rows. A
    false-only matcher would keep capturing the same dead screen until the
    safety guard aborted the run before the planner could name it.
    """
    if classify(snapshot).state in (ScreenState.POPUP, ScreenState.ERROR):
        return snapshot
    return snapshot if _has_plan_rows(snapshot, catalogue) else None


def _roster_signature(roster: Roster) -> tuple[tuple[str, str | None, tuple[int, int] | None, str | None], ...]:
    """The title/key/badge/action set that must stay unchanged before planning.

    Sorting makes this a content signature: a pure frame reorder is not a
    rendering change. The roster's own reading order is deliberately untouched.
    """
    rows = tuple(
        (
            entry.title,
            entry.key,
            (entry.progress.completed, entry.progress.total) if entry.progress is not None else None,
            entry.action.text if entry.action is not None else None,
        )
        for entry in roster.entries
    )
    return tuple(
        sorted(
            rows,
            key=lambda row: (row[0], row[1] or '', row[2] or (-1, -1), row[3] or ''),
        ),
    )


def _unreadable_plan_reason(
    roster: Roster,
    surface: Surface,
    waits: list[CoinPageWait],
    stable_waits: list[dict[str, object]],
    content_waits: list[dict[str, object]],
    roster_waits: list[dict[str, object]],
    settle_note: str | None,
    initial_state: ScreenState,
    frame_state: ScreenState,
) -> str | None:
    """Name why an empty plan is not evidence about the day, or return ``None``.

    A dry run may only exit 0 after it read a complete roster. An unsettled
    frame, a popup cover, an unclassified page, the empty-boost load fault, or a
    coin page whose inline rows never appeared is an incomplete read, not an
    empty day.
    """
    last = waits[-1] if waits else None
    last_state = frame_state
    if last_state is ScreenState.POPUP:
        reason = 'surface_popup: a popup covers the coin page and a dry run cannot dismiss it'
    elif surface is Surface.EMPTY_BOOST:
        reason = 'surface_empty_boost: the Coins boost page was still the documented load fault'
    elif surface is Surface.NON_APP:
        reason = 'surface_non_app: a clock/launcher surface is in the foreground, not the AliExpress app'
    elif surface is Surface.UNKNOWN:
        if roster.empty:
            reason = 'surface_unknown: the coin page did not render'
        else:
            rows = ', '.join(entry.title for entry in roster.entries)
            reason = f'surface_unverified: task rows were read but the coin page marker was missing (rows: {rows})'
    elif roster.card_structure_failures and not roster.dispatchable_entries:
        reason = (
            'surface_incomplete: the task surface named task titles but no legible card structure '
            f'({roster.card_structure_failures} title(s) without subtitle, reward or checkmark)'
        )
        if content_waits:
            reason = (
                f'{reason} (row wait {content_waits[-1]["status"]}) '
                f'at the {cast(float, content_waits[-1]["bound"]):g}s deadline'
            )
    elif not roster.empty:
        if stable_waits and stable_waits[-1]['status'] != 'satisfied':
            reason = 'surface_unverified: task rows were read but the surface never settled'
        elif roster_waits and roster_waits[-1]['status'] != 'satisfied':
            reason = 'surface_unverified: task rows were read but the row set never settled'
        elif roster.scroll_attempts and roster.incomplete:
            # Absence is evidence only from a read that was proved complete.
            reason = (
                f'surface_incomplete: the scrolled boost list was not read completely '
                f'({roster.count} row(s) after {roster.scroll_attempts} scroll(s); '
                f'bound={roster.scroll_limit_reached}, surface_lost={roster.scroll_surface_lost})'
            )
        else:
            return None
    elif surface is Surface.ROWS_ABSENT:
        reason = 'surface_rows_incomplete: the coin page rendered but its task rows did not'
        if content_waits:
            reason = (
                f'{reason} (row wait {content_waits[-1]["status"]}) '
                f'at the {cast(float, content_waits[-1]["bound"]):g}s deadline'
            )
        else:
            reason = f'{reason} (no row wait was possible)'
    else:
        reason = 'surface_incomplete: the task surface rendered but no task row could be read'
        if content_waits:
            reason = (
                f'{reason} (row wait {content_waits[-1]["status"]}) '
                f'at the {cast(float, content_waits[-1]["bound"]):g}s deadline'
            )

    if last is not None:
        history = ' -> '.join(state.value for state in last.history)
        reason = f'{reason} (states={history}; settled={last.settled})'
    elif stable_waits:
        reason = f'{reason} (initial state {initial_state.value})'
    else:
        reason = f'{reason} (initial state {initial_state.value}; no settle wait)'
    if stable_waits:
        reason = (
            f'{reason}; stability wait {stable_waits[-1]["status"]} after '
            f'{cast(float, stable_waits[-1]["elapsed"]):g}s of '
            f'{cast(float, stable_waits[-1]["bound"]):g}s'
        )
    if roster_waits:
        reason = (
            f'{reason}; row-set wait {roster_waits[-1]["status"]} after '
            f'{cast(float, roster_waits[-1]["elapsed"]):g}s of '
            f'{cast(float, roster_waits[-1]["bound"]):g}s'
        )
    if settle_note is not None:
        reason = f'{reason}; {settle_note}'
    return reason


def _discover_or_fallback(
    context: RunContext,
    controller: Controller,
    *,
    on_dismiss: Callable[[DismissReport], None] | None = None,
) -> tuple[Roster, str | None]:
    """Open the best task surface, or enumerate what the current frame shows.

    The measured boost route occasionally ignores the first ``Earn more`` tap;
    the coin page still renders its inline rows. A route timeout must not throw
    away a night that can be worked, so the engine classifies the frame it is on
    and enumerates that surface instead. The fallback is recorded in the report
    so a systemic route failure is visible rather than silent.
    """
    log = context.logger if context.logger is not None else LOG
    try:
        return (
            discover_roster(
                controller,
                context.capture,
                context.corpus,
                context.catalogue,
                navigate=context.navigate,
                refresh=True,
                scroll=context.scroll_rosters,
                templates=context.templates,
                timeout=DEFAULT_BOOST_TIMEOUT,
                interval=DEFAULT_BOOST_INTERVAL,
                settle=context.config.settle_seconds,
                clock=context.clock,
                sleep=context.sleep,
                logger=log,
                on_dismiss=on_dismiss,
            ),
            None,
        )
    except RunInterrupted:
        raise
    except PopupUnclosable:
        # A modal that cannot be dismissed safely fails the run loudly; falling
        # back to the surface underneath it would be the blind tap the brief
        # forbids (FR-2).
        raise
    except AppLoggedOut:
        raise
    except StateTimeout as exc:
        detail = f'{type(exc).__name__}: {exc}'
        log.warning('%s', _event('route_fallback', detail=detail))
        frame = _capture(context.capture)
        _guard_signed_in(context, frame)
        surface = classify_surface(frame)
        if surface in (Surface.BOOST_PAGE, Surface.EMPTY_BOOST, Surface.ROWS_PRESENT, Surface.ROWS_ABSENT):
            roster = (
                collect_roster(
                    controller,
                    context.capture,
                    context.catalogue,
                    frame,
                    route=DiscoveryRoute.INLINE,
                    settle=context.config.settle_seconds,
                    sleep=context.sleep,
                    start_at_top=True,
                )
                if surface is Surface.BOOST_PAGE and context.scroll_rosters
                else enumerate_roster(frame, context.catalogue, route=DiscoveryRoute.INLINE)
            )
            roster = replace(
                roster,
                surface_variant=surface,
                routes_taken=(SurfaceRoute.SHORTCUT,),
                refresh_attempted=True,
            )
        else:
            roster = Roster(
                entries=(),
                route=None,
                catalogue_version=context.catalogue.version,
                default_deny_reason=context.catalogue.default_deny_reason,
                surface_variant=surface,
                routes_taken=(SurfaceRoute.SHORTCUT,),
                refresh_attempted=True,
            )
        return roster, detail


def _dispatch_entry(context: RunContext, controller: Controller, entry: RosterEntry) -> EngineResult:
    spec = _spec(entry)
    category = spec.category
    if category not in _EXECUTORS:
        raise ValueError(f'{category.value!r} has no executor')
    authorization_of(entry)
    common: dict[str, Any] = {
        'navigate': context.navigate,
        'templates': context.templates,
        'route': SurfaceRoute.SHORTCUT,
        'settle': context.config.settle_seconds,
        'clock': context.clock,
        'sleep': context.sleep,
        'logger': context.logger,
    }

    if category is TaskCategory.SIGN_IN:
        return _run_signin(context, controller, entry, spec, common)
    if category is TaskCategory.QUIZ:
        return _from_executor(
            run_quiz_task(controller, context.capture, context.corpus, context.state, entry, **common)
        )
    if category is TaskCategory.BROWSE:
        return _from_executor(
            run_search_task(controller, context.capture, context.corpus, context.state, entry, **common),
        )
    if category is TaskCategory.BROWSE_WAIT:
        browse_options = dict(common)
        if context.app_foreground is not None:
            browse_options['app_foreground'] = context.app_foreground
        if context.warm_start is not None:
            browse_options['warm_start'] = context.warm_start
        if context.record_foreground_loss is not None:
            browse_options['record_foreground_loss'] = context.record_foreground_loss
        return _from_executor(
            run_browse_task(controller, context.capture, context.corpus, context.state, entry, **browse_options),
        )
    if category is TaskCategory.ITEM_TAP:
        if context.record_round_grid is not None:
            common['record_round_grid'] = context.record_round_grid
        return _from_executor(
            run_surprise_task(controller, context.capture, context.corpus, context.state, entry, **common),
        )
    if category is TaskCategory.SHORTCUT_VISIT:
        shortcut_options = dict(common)
        if context.app_foreground is not None:
            shortcut_options['app_foreground'] = context.app_foreground
        if context.shortcut_present is not None:
            shortcut_options['shortcut_present'] = context.shortcut_present
        if context.launch_shortcut is not None:
            shortcut_options['launch_shortcut'] = context.launch_shortcut
        if context.remove_shortcut is not None:
            shortcut_options['remove_shortcut'] = context.remove_shortcut
        return _from_executor(
            run_shortcut_visit_task(
                controller,
                context.capture,
                context.corpus,
                context.state,
                entry,
                **shortcut_options,
            ),
        )
    if category is TaskCategory.NOTIFICATION:
        return _from_executor(
            run_notifications_task(controller, context.capture, context.corpus, context.state, entry, **common),
        )
    raise ValueError(f'{category.value!r} has no executor')  # pragma: no cover - guarded above


@contextmanager
def _task_budget(context: RunContext, deadline: float) -> Iterator[None]:
    """Make the run's wall-clock budget active inside one task dispatch.

    The outer loop can only check the budget between dispatches. An executor
    that keeps capturing and sleeping could therefore run far past it, so the
    same deadline is applied to the task's own injected seams: every clock read,
    sleep and capture checks it. A blocked adb call is separately bounded by its
    subprocess timeout (30 s for a capture, 10 s for an input command), so the
    worst adb overrun is one in-flight command rather than an arbitrary task
    tail. A native OCR/PIL call has no in-process timeout.
    """
    clock = context.clock
    sleep = context.sleep
    capture = context.capture

    def check() -> float:
        value = clock()
        if value >= deadline:
            raise RunBudgetExceeded('the wall-clock budget ended while a task was running')
        return value

    def guarded_sleep(seconds: float) -> None:
        remaining = deadline - clock()
        sleep(min(seconds, max(0.0, remaining)))
        if remaining <= seconds:
            raise RunBudgetExceeded('the wall-clock budget ended during a task sleep')

    def guarded_capture() -> Snapshot:
        check()
        frame = _capture(capture)
        check()
        return frame

    context.clock = check
    context.sleep = guarded_sleep
    context.capture = guarded_capture
    try:
        yield
    finally:
        context.clock = clock
        context.sleep = sleep
        context.capture = capture


def _dispatch_with_retry(
    context: RunContext,
    controller: Controller,
    entry: RosterEntry,
    *,
    on_attempt: Callable[[], None] | None = None,
    recorder: FailureRecorder | None = None,
) -> EngineResult:
    """Dispatch one row once; the outer pass loop owns the task retry.

    A result that records a tap, key or page press is never re-run, because the
    first attempt may already have changed the account (FR-5's "never re-press
    a claimed card"). The outer loop re-enumerates before a safe retry, so this
    function only dispatches and applies the signed-out guard.

    A row that acted without proof in an earlier run is marked in the daily
    state. Its next dispatch first re-reads the task surface: a fresh row with
    its unclaimed ``GO`` affordance may proceed, while a claimed/gone row is
    reported ``already_done`` without a tap. An unreadable re-check fails the
    task retryably instead of guessing.
    """
    log = context.logger if context.logger is not None else LOG
    try:
        authorized = _authorize(context, entry)
    except PolicyRefused as exc:
        _clear_acted_marker(context, entry.key)
        refusal = exc.refusal
        return EngineResult(
            outcome='skipped',
            verification='policy',
            detail=f'{refusal.code}: {refusal.reason}',
            completed=False,
            payload=refusal.as_dict(),
        )

    if recorder is not None:
        recorder.begin(
            key=authorized.key if authorized.key is not None else authorized.title.casefold(),
            title=authorized.title,
            category=authorized.spec.category.value if authorized.spec is not None else None,
        )

    if authorized.key is not None and context.state.is_acted_unverified(authorized.key):
        try:
            readable, fresh = _recheck_acted_row(context, controller, authorized)
        except RunAborted as exc:
            _flush_abort_recording(context, recorder, code=exc.code.value, detail=exc.reason)
            raise
        except RunInterrupted as exc:
            _flush_abort_recording(context, recorder, code=_interrupt_code(exc), detail=str(exc))
            raise
        except BaseException:
            if recorder is not None:
                recorder.abandon()
            raise
        if not readable:
            result = EngineResult(
                outcome='failed',
                verification='acted_recheck_unavailable',
                detail=(
                    f'{authorized.title!r} acted without proof in an earlier run and the fresh frame '
                    'could not prove the row is still claimable; no tap was issued'
                ),
                completed=False,
                payload={'key': authorized.key, 'recheck': 'unreadable'},
                failure=failure(FailureCode.TASK_UNVERIFIED, detail='the acted-row re-check was unreadable'),
                acted=False,
            )
            _record_attempt_result(context, recorder, result)
            return result
        if fresh is None:
            # Keep the marker: the row's control may still be present on a
            # partial/stale roster, so a later dispatch must re-check again
            # rather than tap without proof.
            result = EngineResult(
                outcome='skipped',
                verification='already_done',
                detail=(
                    f'{authorized.title!r} acted without proof in an earlier run and the fresh surface '
                    'shows it already claimed; no tap was issued'
                ),
                completed=False,
                payload={'key': authorized.key, 'recheck': 'claimed'},
            )
            _record_attempt_result(context, recorder, result)
            return result
        authorized = _authorize(context, fresh)
        log.info('%s', _event('acted_recheck', key=authorized.key, claimable=True))

    if on_attempt is not None:
        on_attempt()
    try:
        result = _attempt_dispatch(context, controller, authorized)
    except RunAborted as exc:
        _flush_abort_recording(context, recorder, code=exc.code.value, detail=exc.reason)
        raise
    except RunInterrupted as exc:
        _flush_abort_recording(context, recorder, code=_interrupt_code(exc), detail=str(exc))
        raise
    except BaseException:
        if recorder is not None:
            recorder.abandon()
        raise
    # Persist the safety marker before the signed-out guard: if the session
    # expired after the task acted, the next run still needs the re-check.
    _remember_acted_result(context, authorized, result)
    _record_attempt_result(context, recorder, result)
    if not result.completed:
        # A failed task is the first place a session that expired mid-walk is
        # visible. Detect it before the next pass, so a signed-out app stops
        # the run instead of producing more task failures.
        _guard_signed_in(context, _capture(context.capture))
    return result


def _record_attempt_result(context: RunContext, recorder: FailureRecorder | None, result: EngineResult) -> None:
    """Let the recorder discard a success/skip or write one failed attempt's artifact."""
    if recorder is None:
        return
    window = context.safety.recovering() if result.outcome == 'failed' and context.safety is not None else nullcontext()
    with window:
        recorder.finish(
            outcome=result.outcome,
            verification=result.verification,
            detail=result.detail,
            completed=result.completed,
            failure_code=None if result.failure is None else result.failure.code.value,
            payload=result.payload,
        )


def _interrupt_code(exc: RunInterrupted) -> str:
    """Name a budget stop separately from an operator/scheduler stop."""
    return FailureCode.BUDGET_EXHAUSTED.value if isinstance(exc, RunBudgetExceeded) else 'stopped'


def _flush_abort_recording(
    context: RunContext,
    recorder: FailureRecorder | None,
    *,
    code: str,
    detail: str,
) -> None:
    """Write the frames already buffered when a run-level abort or stop arrives."""
    if recorder is None:
        return
    window = context.safety.recovering() if context.safety is not None else nullcontext()
    with window:
        recorder.finish(
            outcome='failed',
            verification=code,
            detail=detail,
            completed=False,
            failure_code=code,
            payload={'abort': {'code': code, 'reason': detail}},
            aftermath=False,
        )


def _recheck_acted_row(
    context: RunContext,
    controller: Controller,
    entry: RosterEntry,
) -> tuple[bool, RosterEntry | None]:
    """Re-read the task surface before re-dispatching an acted-but-unproven row.

    Returns ``(readable, fresh)``. ``fresh`` is the entry to dispatch when the
    row still shows its unclaimed control. ``None`` with ``readable=True`` means
    the row is gone or checked on a complete surface, so it is already done.
    ``readable=False`` means absence is not evidence (an inline/partial surface
    or a failed read) and the task must stay retryable without a tap.
    """
    log = context.logger if context.logger is not None else LOG
    key = entry.key
    try:
        opened = open_boost_list(
            controller,
            context.capture,
            context.corpus,
            templates=context.templates,
            timeout=DEFAULT_BOOST_TIMEOUT,
            interval=DEFAULT_BOOST_INTERVAL,
            settle=context.config.settle_seconds,
            clock=context.clock,
            sleep=context.sleep,
            logger=log,
        )
        _guard_signed_in(context, opened.snapshot)
    except RunAborted, RunInterrupted:
        raise
    except AppLoggedOut:
        raise
    except Exception as exc:
        log.warning('%s', _event('acted_recheck_failed', key=key, detail=f'{type(exc).__name__}: {exc}'))
        return False, None

    surface = classify_surface(opened.snapshot)
    top_roster = enumerate_roster(opened.snapshot, context.catalogue, route=opened.route)
    fresh = next((candidate for candidate in top_roster.entries if candidate.key == key), None)
    if fresh is None and surface is Surface.BOOST_PAGE and context.scroll_rosters:
        fresh_roster = collect_roster(
            controller,
            context.capture,
            context.catalogue,
            opened.snapshot,
            route=opened.route,
            settle=context.config.settle_seconds,
            sleep=context.sleep,
        )
        fresh = next((candidate for candidate in fresh_roster.entries if candidate.key == key), None)
        if fresh is None and fresh_roster.incomplete:
            # A truncated or partly split list is not proof that the row is
            # gone. Keep the acted marker and the task retryable.
            return False, None
    if fresh is not None:
        if fresh.claimed:
            # The fresh card has no action because it is already claimed. That
            # is positive evidence the earlier action applied.
            return True, None
        if fresh.progress is not None and fresh.progress.complete:
            return True, None
        return True, fresh
    if surface is Surface.BOOST_PAGE:
        # The populated boost list is a complete surface: a missing row there
        # proves the task is gone/claimed, unlike the inline coin page.
        return True, None
    return False, None


def _remember_acted_result(context: RunContext, entry: RosterEntry, result: EngineResult) -> None:
    """Keep the acted-but-unproven marker only while the row is still retryable."""
    key = entry.key
    if key is None:  # pragma: no cover - _authorize only returns allowed rows
        return
    terminal = (
        result.completed or result.outcome != 'failed' or (result.failure is not None and not result.failure.retryable)
    )
    # A missing row without a cleared dwell chip is rotation, not proof the tap
    # completed. Do not mark it acted-unproven: that marker would turn the next
    # missing row into a terminal `already_done`, reintroducing the false claim.
    unproven_rotation = result.verification == 'chip_not_cleared'
    if terminal or unproven_rotation:
        _clear_acted_marker(context, key)
    elif result.acted:
        _mark_acted_unverified(context, key)


def _mark_acted_unverified(context: RunContext, key: str) -> None:
    """Record an action whose effect could not be proved, without failing the run."""
    try:
        context.state.mark_acted_unverified(key)
    except Exception as exc:
        _log_state_marker_failure(context, key, exc)


def _clear_acted_marker(context: RunContext, key: str | None) -> None:
    if key is None:
        return
    try:
        context.state.clear_acted_unverified(key)
    except Exception as exc:
        _log_state_marker_failure(context, key, exc)


def _log_state_marker_failure(context: RunContext, key: str, exc: Exception) -> None:
    log = context.logger if context.logger is not None else LOG
    log.warning(
        '%s',
        _event('state_acted_marker_failed', key=key, detail=f'{type(exc).__name__}: {exc}'),
    )


def _authorize(context: RunContext, entry: RosterEntry) -> RosterEntry:
    """Resolve, record and authorise one row at the single dispatch point."""
    gate = context.gate
    if gate is None:
        gate = DispatchGate(context.catalogue, logger=context.logger)
        context.gate = gate
    return gate.authorize(entry)


def _attempt_dispatch(
    context: RunContext,
    controller: Controller,
    entry: RosterEntry,
) -> EngineResult:
    try:
        guard = context.safety
        if guard is None:  # pragma: no cover - run_once always installs one
            return _dispatch_entry(context, controller, entry)
        with guard.task(entry.title, key=entry.key):
            return _dispatch_entry(context, controller, entry)
    except RunInterrupted:
        raise
    except RunAborted as exc:
        if exc.code is not AbortCode.UNEXPECTED_SCREEN:
            raise
        # A task that ran into a screen the classifier cannot name has already
        # failed. Drive back to the coins page first; only an unrecoverable
        # screen is the run's problem (#83).
        return _recover_task_screen(context, controller, entry, exc)
    except Exception as exc:
        named = _task_exception(exc)
        if named.abort_run:
            raise
        detail = f'{type(exc).__name__}: {exc}'
        return EngineResult(
            outcome='failed',
            verification='error',
            detail=detail,
            completed=False,
            payload={'error': detail},
            failure=named,
            acted=True,
        )


def _recover_task_screen(
    context: RunContext,
    controller: Controller,
    entry: RosterEntry,
    abort: RunAborted,
) -> EngineResult:
    """Fail one task on its unknown screen, or abort if the coins page is lost.

    The return is the same bounded shortcut route every executor uses, but the
    guard's unexpected-screen streak is suspended for the window: the screen has
    already been attributed to this task, and recovery must get all its attempts
    before the night is called unrecoverable. A successful return records
    ``task_unverified`` with the screen in the task payload and leaves
    ``report.abort`` unset; a failed one raises the run-level abort.
    """
    log = context.logger if context.logger is not None else LOG
    window = context.safety.recovering() if context.safety is not None else nullcontext()
    log.warning(
        '%s',
        _event(
            'task_screen_recovery',
            key=entry.key,
            title=entry.title,
            screen=abort.screen.as_dict(),
            attempts=TASK_RECOVERY_ATTEMPTS,
        ),
    )
    with window:
        recovered = return_to_coins_page(
            controller,
            context.capture,
            context.corpus,
            context.navigate,
            snapshot=None,
            templates=context.templates,
            route=SurfaceRoute.SHORTCUT,
            attempts=TASK_RECOVERY_ATTEMPTS,
            settle=context.config.settle_seconds,
            clock=context.clock,
            sleep=context.sleep,
            logger=log,
        )

    frame = recovered.snapshot
    recovered_state = classify(frame).state if frame is not None else ScreenState.UNKNOWN
    if not recovered.returned or frame is None or recovered_state is ScreenState.UNKNOWN:
        reason = (
            f'{entry.title!r} hit an unknown screen and the coins page was not recovered '
            f'after {TASK_RECOVERY_ATTEMPTS} shortcut attempts: {abort.reason}'
        )
        if recovered.error:
            reason = f'{reason}; return error={recovered.error}'
        raise RunAborted(code=AbortCode.UNEXPECTED_SCREEN, reason=reason, screen=abort.screen)

    screen_detail = abort.screen.summary
    detail = f'{abort.reason}; screen={screen_detail}; recovered to the coins page'
    return EngineResult(
        outcome='failed',
        verification=AbortCode.UNEXPECTED_SCREEN.value,
        detail=detail,
        completed=False,
        payload={
            'key': entry.key,
            'title': entry.title,
            'error': abort.reason,
            'screen': abort.screen.as_dict(),
            'recovery': recovered.as_dict(),
        },
        failure=failure(FailureCode.TASK_UNVERIFIED, detail=f'{abort.reason}; screen={screen_detail}'),
        acted=True,
    )


def _task_exception(exc: BaseException) -> Failure:
    """Name an executor exception without turning a task timeout into a night out."""
    named = classify_exception(exc, default=FailureCode.TASK_UNVERIFIED)
    if named is None:  # pragma: no cover - RunInterrupted is re-raised by the caller
        raise exc
    if named.code is FailureCode.NAVIGATION_FAILED:
        # A page that did not open inside a task is that task's failure; the
        # run-level route has its own retry and fallback in _discover_or_fallback.
        return task_failure(exc)
    return named


def _retry_stage(context: RunContext, stage: str, action: Callable[[], Any]) -> Any:
    """Run one stage, spending at most its policy's retry after an exponential pause."""
    log = context.logger if context.logger is not None else LOG
    try:
        return action()
    except RunAborted, RunInterrupted:
        raise
    except Exception as exc:
        named = classify_exception(exc, default=FailureCode.INTERNAL_ERROR)
        if named is None or not named.retryable or named.retry is not RetryScope.STAGE:
            raise
        delay = retry_delay(0)
        log.warning(
            '%s',
            _event(
                'failure_retry',
                code=named.code.value,
                scope=named.retry.value,
                stage=stage,
                delay_seconds=delay,
            ),
        )
        context.sleep(delay)
        return action()


def _guard_signed_in(context: RunContext, snapshot: Snapshot) -> None:
    """Stop the whole night at the first signed-out screen."""
    if not is_logged_out(snapshot):
        return
    log = context.logger if context.logger is not None else LOG
    log.warning('%s', _event('app_logged_out', detail='a sign-in prompt replaced the account surface'))
    raise AppLoggedOut('the app shows a sign-in prompt instead of the signed-in account')


def _retrying_capture(
    source: CaptureSource | Callable[[], Snapshot],
    *,
    sleep: Callable[[float], None],
    logger: logging.Logger | None = None,
    check_connected: Callable[[], None] | None = None,
) -> Callable[[], Snapshot]:
    """Wrap one capture seam with the taxonomy's single stage retry.

    A failed ``screencap`` is the mid-run network failure the issue names. The
    retry proves whether a blip recovered; when it does not, ``check_connected``
    asks adb for the serial's state so the failure is named ``device_offline``
    rather than an anonymous decode error.
    """
    log = logger if logger is not None else LOG

    def capture() -> Snapshot:
        try:
            return _capture(source)
        except FrameDecodeError:
            # The device answered; retrying the same undecodable frame helps
            # nobody and would only blur the cause.
            raise
        except SnapshotError as exc:
            delay = retry_delay(0)
            log.warning(
                '%s',
                _event(
                    'failure_retry',
                    code=FailureCode.DEVICE_OFFLINE.value,
                    scope=RetryScope.STAGE.value,
                    stage='capture',
                    delay_seconds=delay,
                    detail=f'{type(exc).__name__}: {exc}',
                ),
            )
            sleep(delay)
            try:
                return _capture(source)
            except FrameDecodeError:
                raise
            except SnapshotError:
                if check_connected is not None:
                    check_connected()
                raise

    return capture


def _run_signin(
    context: RunContext,
    controller: Controller,
    entry: RosterEntry,
    spec: AllowedTaskSpec,
    common: dict[str, Any],
) -> EngineResult:
    """Run both independent daily claims from the coin page.

    A roster can be the boost list, where the streak card is not visible, so the
    sign-in executor is given the measured shortcut route first. It then claims
    the streak and the boost row itself, using its own state checks for each.
    """
    authorization = authorization_of(entry)
    if context.state.done_today('daily_checkin') and context.state.done_today('boost_checkin'):
        return EngineResult(
            outcome='skipped',
            verification='state',
            detail='both daily sign-ins are already recorded done for today',
            completed=False,
            payload={'key': spec.key},
        )
    context.navigate(SurfaceRoute.SHORTCUT)
    result = claim_daily_signins(
        controller,
        context.capture,
        context.corpus,
        context.state,
        authorization=authorization,
        snapshot=None,
        templates=common['templates'],
        settle=common['settle'],
        clock=common['clock'],
        sleep=common['sleep'],
        logger=common['logger'],
    )
    claims = result.results
    # A completed claim must not hide an unverified one: the streak and the
    # boost check-in are independent, and the empty boost page makes the boost
    # claim unverified while the streak succeeds. Reporting ``completed`` from
    # the streak alone would swallow the miss (#84).
    completed = bool(result.completed) and not any(claim.unverified for claim in claims)
    if completed:
        outcome = 'completed'
    elif claims and all(claim.already_done for claim in claims):
        outcome = 'skipped'
    elif any(claim.unverified for claim in claims):
        outcome = 'failed'
    elif any(claim.unavailable for claim in claims):
        outcome = 'skipped'
    else:
        outcome = 'failed'
    verification = ','.join(sorted({claim.verification for claim in claims})) or 'state'
    detail = '; '.join(f'{claim.key}: {claim.outcome.value} ({claim.detail})' for claim in claims)
    return EngineResult(
        outcome=outcome,
        verification=verification,
        detail=detail,
        completed=completed,
        payload=result.as_dict(),
        failure=failure(FailureCode.TASK_UNVERIFIED, detail=detail) if outcome == 'failed' else None,
        acted=any(claim.tapped for claim in claims),
    )


def _from_executor(result: ExecutorResult) -> EngineResult:
    payload = result.as_dict()
    named = failure(FailureCode.TASK_UNVERIFIED, detail=result.detail) if result.outcome.value == 'failed' else None
    return EngineResult(
        outcome=result.outcome.value,
        verification=result.verification,
        detail=result.detail,
        completed=result.completed,
        payload=payload,
        failure=named,
        acted=_payload_acted(payload),
    )


def _payload_acted(payload: dict[str, Any]) -> bool:
    """Whether an executor result records any tap, key or entry press.

    An unproven task is only safe to re-run when it has not acted yet. The
    executors already record every tap in ``taps`` (and the browse executor its
    page presses), so this reads evidence that exists rather than guessing from
    the outcome string.
    """
    taps = payload.get('taps')
    if isinstance(taps, (list, tuple)) and taps:
        return True
    keys = payload.get('keys')
    if isinstance(keys, (list, tuple)) and keys:
        return True
    swipes = payload.get('swipes')
    if isinstance(swipes, (list, tuple)) and swipes:
        return True
    if payload.get('collect') is not None:
        return True
    presses = payload.get('presses')
    return isinstance(presses, int) and presses > 0


def _observer(
    context: RunContext,
    report: RunReport,
    roster: Roster,
    *,
    max_observations: int | None = None,
) -> RosterObserver:
    if max_observations is None:
        task_limit = resolve_task_budget(context.config, context.catalogue).limit
        cap = task_limit + context.config.max_passes + 2
    else:
        cap = max_observations
    return RosterObserver(
        context.catalogue,
        previous=roster,
        log=RosterLog.for_run(context.config.runs_dir, report.run_id),
        max_observations=max(2, cap),
        clock=context.wall_clock,
        logger=context.logger,
    )


def _observe(
    context: RunContext,
    controller: Controller,
    observer: RosterObserver,
    *,
    route: DiscoveryRoute | None,
    on_dismiss: Callable[[DismissReport], None] | None = None,
) -> RosterObservation | None:
    """Re-open the task surface and log one fresh observation.

    Executors return to the coin page, so observing the frame they left would
    change the surface family and fail the diff. Opening the best list first is
    the same route #38 uses; a failure is logged and the previous roster stays.
    """
    log = context.logger if context.logger is not None else LOG
    try:
        opened = open_boost_list(
            controller,
            context.capture,
            context.corpus,
            templates=context.templates,
            timeout=DEFAULT_BOOST_TIMEOUT,
            interval=DEFAULT_BOOST_INTERVAL,
            settle=context.config.settle_seconds,
            clock=context.clock,
            sleep=context.sleep,
            logger=log,
            on_dismiss=on_dismiss,
        )
        _guard_signed_in(context, opened.snapshot)
        previous = observer.roster
        collected = (
            collect_roster(
                controller,
                context.capture,
                context.catalogue,
                opened.snapshot,
                route=opened.route if route is None else route,
                settle=context.config.settle_seconds,
                sleep=context.sleep,
                # No tap means the sheet was already open, so it can be at any
                # scroll position from the previous read. Prove the top first;
                # reading from the bottom collapsed a fresh-day roster to one
                # row and let the night be called complete (#137).
                start_at_top=opened.tap is None,
            )
            if context.scroll_rosters and previous is not None and previous.scroll_attempts > 0
            else None
        )
        return observer.observe(
            opened.snapshot,
            route=opened.route if route is None else route,
            roster=collected,
            timestamp=context.wall_clock(),
        )
    except RunAborted, RunInterrupted:
        raise
    except AppLoggedOut:
        raise
    except Exception as exc:
        log.warning('%s', _event('roster_observe_failed', detail=f'{type(exc).__name__}: {exc}'))
        try:
            frame = _capture(context.capture)
            _guard_signed_in(context, frame)
            return observer.observe(frame, route=route, timestamp=context.wall_clock())
        except RunAborted, RunInterrupted:
            raise
        except AppLoggedOut:
            raise
        except Exception as inner:
            log.warning('%s', _event('roster_observe_failed', detail=f'{type(inner).__name__}: {inner}'))
            return None


def _pass_restart_reason(
    roster: Roster | None,
    observation: RosterObservation | None,
) -> str | None:
    """Name why a finished pass should re-fire the shortcut, or return ``None``.

    A normal pass that simply found no work is complete. An unreadable or
    unknown surface is not evidence that the day is empty, and an allowed row
    with no action is a stuck surface rather than a clean zero, so both restart
    from the shortcut before the night may end.
    """
    if roster is None:
        return 'the roster was not read'
    if observation is None:
        return 'the task surface has not been read since the last attempt'
    if observation.cap_reached:
        return 'the roster observation cap was reached before a fresh read'
    if observation.failed:
        return f'the re-enumerated surface could not be read: {observation.error}'
    if roster.surface_variant is Surface.UNKNOWN:
        return 'the task surface is unknown'
    if roster.surface_variant is Surface.EMPTY_BOOST:
        return 'the Coins boost page is the documented load fault'
    if roster.scroll_surface_unconfirmed or roster.scroll_surface_lost:
        return f'surface_lost: the Coins boost list was not read from a confirmed surface ({roster.warning})'
    if not roster.entries and roster.unreadable_count:
        # A frame with only unreadable action rows is not an empty day. Restart
        # by route rather than walking a roster that proves nothing (#137).
        return (
            'surface_incomplete: the roster read was incomplete because '
            f'{roster.unreadable_count} task row(s) had no readable title'
        )
    if roster.card_structure_failures and not roster.dispatchable_entries:
        # A visible task title without its card copy is a partial render, not a
        # claimed row. Restart only when there was nothing dispatchable to do.
        return (
            'surface_incomplete: the roster read was incomplete because '
            f'{roster.card_structure_failures} task title(s) had no readable card structure'
        )
    return None


def _pass_progressed(
    dispatched: bool,
    current_dispatchable: frozenset[str],
    pass_start_dispatchable: frozenset[str],
) -> bool:
    """Whether this sweep earned another one by dispatching or revealing work."""
    return dispatched or bool(current_dispatchable - pass_start_dispatchable)


def _next_pass_bound(
    *,
    passes: int,
    max_passes: int,
    no_progress_passes: int,
    progressed: bool,
) -> tuple[str | None, int]:
    """Decide whether another sweep is allowed and update the no-progress streak.

    ``max_passes`` is the absolute ceiling. The progress rule ends a walk that
    keeps sweeping without dispatching or revealing work, so the normal bound
    follows the roster's own daily capacity rather than a fixed count (#141).
    One idle sweep is tolerated so a surface restart can recover.
    """
    if passes >= max_passes:
        return f'pass budget of {max_passes} exhausted', no_progress_passes
    idle = 0 if progressed else no_progress_passes + 1
    if idle >= MAX_NO_PROGRESS_PASSES:
        return f'pass budget stopped after {idle} consecutive no-progress pass(es)', idle
    return None, idle


def _dispatchable_identities(
    roster: Roster | None,
    state: DailyStateStore,
    *,
    blocked: set[str],
    blacklisted: Mapping[str, int],
    settled: set[str],
) -> frozenset[str]:
    """Identity set of the rows a pass could dispatch right now.

    Two sweeps' sets are compared to tell a sweep that revealed new work from
    one that merely re-read the same exhausted surface. A "revealed" row that
    could not be dispatched must not buy another sweep, so this uses the same
    eligibility predicate as :func:`_next_entry`.
    """
    if roster is None:
        return frozenset()
    return frozenset(
        _identity(entry)
        for entry in roster.entries
        if _entry_dispatchable(
            entry,
            state,
            blocked=blocked,
            blacklisted=blacklisted,
            attempted=None,
            settled=settled,
        )
    )


def _warn_task_ceiling(task_budget: TaskBudget, log: logging.Logger) -> None:
    """Name a task cap that cannot finish the catalogue's legitimate day (#166).

    The default is the catalogue's derived need, so this warning is for an
    operator who set ``AECOINS_MAX_TASKS_PER_RUN`` below it (or above the
    absolute backstop). The run still starts: the cap is a real bound, and a
    task stop names every row it left plus the derived need.
    """
    configured = task_budget.configured
    if configured is not None and configured > task_budget.ceiling:
        log.warning(
            '%s',
            _event(
                'task_budget_above_absolute_ceiling',
                max_tasks=configured,
                effective_tasks=task_budget.limit,
                derived_task_need=task_budget.need,
                absolute_ceiling=task_budget.ceiling,
                detail=(
                    f'AECOINS_MAX_TASKS_PER_RUN={configured} is above the absolute ceiling; '
                    f'the run will stop at {task_budget.limit} dispatches while the catalogue derives '
                    f'{task_budget.need} dispatch(es)'
                ),
            ),
        )
    elif configured is not None and configured < task_budget.need:
        log.warning(
            '%s',
            _event(
                'task_budget_below_daily_capacity',
                max_tasks=configured,
                recommended_tasks=task_budget.need,
                derived_task_need=task_budget.need,
                max_task_attempts=task_budget.max_task_attempts,
                absolute_ceiling=task_budget.ceiling,
                detail=(
                    f'AECOINS_MAX_TASKS_PER_RUN={configured} is below the {task_budget.need} dispatches '
                    'needed by the catalogue (sum of max_per_run times '
                    f'{task_budget.max_task_attempts} attempt(s) per round over allowed families); '
                    'a legitimate day can leave an allowed row unclaimed at this cap'
                ),
            ),
        )
    elif configured is None and task_budget.need > task_budget.ceiling:
        log.warning(
            '%s',
            _event(
                'task_budget_ceiling_below_daily_capacity',
                derived_task_need=task_budget.need,
                absolute_ceiling=task_budget.ceiling,
                detail=(
                    f'the catalogue derives {task_budget.need} dispatches but the absolute ceiling is '
                    f'{task_budget.ceiling}; the backstop will stop the walk before a legitimate worst case'
                ),
            ),
        )


def _warn_pass_ceiling(config: RunConfig, catalogue: TaskCatalogue, log: logging.Logger) -> None:
    """Name a pass ceiling that cannot finish the largest daily family (#141).

    The safe worst case is ``max_per_run * max_task_attempts``: every round can
    spend a failed attempt before a later pass succeeds, and a success resets
    the failure streak, so the retry can repeat for every round. The shipped
    maximum is twelve (six ``sponsored_items`` rounds times two attempts), which
    is also the default. The run still starts: the ceiling is a hard bound, and
    the report will say which row is still dispatchable when it stops.
    """
    largest = max(catalogue.allowed, key=lambda spec: spec.max_per_run)
    recommended = largest.max_per_run * config.max_task_attempts
    if config.max_passes >= recommended:
        return
    log.warning(
        '%s',
        _event(
            'pass_budget_below_daily_capacity',
            max_passes=config.max_passes,
            recommended_passes=recommended,
            largest_max_per_run=largest.max_per_run,
            max_task_attempts=config.max_task_attempts,
            key=largest.key,
            detail=(
                f'AECOINS_MAX_PASSES={config.max_passes} is below the {recommended} sweeps needed '
                f'for the largest family ({largest.max_per_run} rounds for {largest.key!r} times '
                f'up to {config.max_task_attempts} attempt(s) per round); a success resets the '
                'failure streak, so a retry can spend a sweep before every round and the ceiling '
                'can end a night before that family finishes'
            ),
        ),
    )


def _stuck_row_reason(
    roster: Roster | None,
    state: DailyStateStore,
    *,
    settled: set[str],
    blocked: set[str],
    blacklisted: Mapping[str, int],
) -> str | None:
    """Name an allowed row a finished pass could not turn into a dispatch."""
    if roster is None:
        return None
    for entry in _ordered_entries(roster.entries):
        if not entry.allowed or entry.spec is None or entry.spec.category not in _EXECUTORS:
            continue
        if entry.claimed:
            # A claimed card is a read row with nothing to dispatch, not a
            # stuck surface. Treating it as stuck restarts the route forever.
            continue
        identity = _identity(entry)
        if identity in settled or identity in blocked or identity in blacklisted:
            continue
        if state.count(entry.spec.key) >= entry.spec.max_per_run:
            continue
        if entry.dispatch_target is None:
            return f'{entry.title!r} was allowed but had no dispatchable action'
    return None


def _restart_surface(
    context: RunContext,
    controller: Controller,
    observer: RosterObserver,
    *,
    on_dismiss: Callable[[DismissReport], None] | None,
    reason: str,
) -> RosterObservation | None:
    """Fire the shortcut again, then re-read the best task surface."""
    log = context.logger if context.logger is not None else LOG
    try:
        context.navigate(SurfaceRoute.SHORTCUT)
        _guard_signed_in(context, _capture(context.capture))
    except RunAborted, RunInterrupted, AppLoggedOut:
        raise
    except Exception as exc:
        detail = f'{type(exc).__name__}: {exc}'
        log.warning('%s', _event('surface_restart_failed', reason=reason, detail=detail))
        return None
    log.info('%s', _event('surface_restart', reason=reason, route=SurfaceRoute.SHORTCUT.value))
    return _observe(context, controller, observer, route=None, on_dismiss=on_dismiss)


def _needs_another_pass(result: EngineResult) -> bool:
    """Whether this outcome leaves the row worth dispatching in a later pass."""
    if result.completed:
        return False
    if result.outcome == 'failed':
        return result.failure is None or result.failure.retryable
    if result.outcome == 'skipped':
        return result.verification in _RETRYABLE_SKIP_VERIFICATIONS
    return False


def _blacklist_task(
    report: RunReport,
    state: DailyStateStore,
    entry: RosterEntry,
    *,
    attempts: int,
    blacklist: Mapping[str, int],
    logger: logging.Logger | None,
) -> dict[str, int]:
    """Skip one task for the rest of the night and say why in the report."""
    key = _identity(entry)
    updated = dict(blacklist)
    updated[key] = attempts
    spec = entry.spec
    if spec is not None:
        try:
            state.blacklist_task(spec.key, attempts)
        except Exception as exc:
            if logger is not None:
                logger.warning(
                    '%s',
                    _event(
                        'state_blacklist_failed',
                        key=spec.key,
                        detail=f'{type(exc).__name__}: {exc}',
                    ),
                )
    reason = f'blacklisted after {attempts} attempts'
    _skip(
        report,
        logger,
        title=entry.title,
        key=entry.key,
        category=spec.category.value if spec is not None else None,
        reason=reason,
        detail=(
            f'{entry.title!r} failed {attempts} time(s) in this night; '
            'it is skipped until the next local day so the rest of the roster continues'
        ),
    )
    return updated


def _skip(
    report: RunReport,
    logger: logging.Logger | None,
    *,
    title: str,
    key: str | None,
    category: str | None,
    reason: str,
    detail: str,
) -> None:
    """Record and log one row that was not dispatched.

    ``RunReport.skip`` collapses duplicates, so the WARNING event is emitted
    only when the row is new to this report.
    """
    if not report.skip(title, key=key, category=category, reason=reason, detail=detail):
        return
    if logger is not None:
        logger.log(
            SKIPPED_ROW_LOG_LEVEL,
            '%s',
            _event(
                'task_skipped',
                title=title,
                key=key,
                category=category,
                reason=reason,
                detail=detail,
            ),
        )


def _record_roster_diagnostics(roster: Roster, logger: logging.Logger) -> None:
    """Put a swallowed-row warning in the structured run log."""
    if roster.warning is None:
        return
    logger.log(
        logging.WARNING if roster.incomplete else logging.INFO,
        '%s',
        _event(
            'roster_incomplete' if roster.incomplete else 'roster_split_failure',
            detail=roster.warning,
            roster_count=roster.count,
            visible_action_count=roster.visible_action_count,
            unassigned_action_count=roster.unassigned_action_count,
            unreadable_count=roster.unreadable_count,
            surface_unconfirmed=roster.scroll_surface_unconfirmed,
            split_failures=[failure.as_dict() for failure in roster.split_failures],
        ),
    )


def _record_policy_skips(
    report: RunReport,
    roster: Roster | None,
    *,
    logger: logging.Logger | None = None,
    gate: DispatchGate | None = None,
) -> None:
    if roster is None:
        return
    for entry in roster.entries:
        if entry.denied:
            refusal = gate.refusal_for(entry) if gate is not None else None
            detail = (
                f'{refusal.code}: {refusal.reason}'
                if refusal is not None
                else (entry.reason or 'refused by the default-deny policy')
            )
            _skip(
                report,
                logger,
                title=entry.title,
                key=None,
                category=None,
                reason='policy',
                detail=detail,
            )
        elif entry.spec is not None and entry.spec.category not in _EXECUTORS:
            _skip(
                report,
                logger,
                title=entry.title,
                key=entry.key,
                category=entry.spec.category.value,
                reason='no executor',
                detail=f'{entry.spec.category.value!r} has no executor in this issue',
            )
        elif entry.claimed:
            _skip(
                report,
                logger,
                title=entry.title,
                key=entry.key,
                category=entry.spec.category.value if entry.spec is not None else None,
                reason='already claimed',
                detail='the row is already claimed on this sheet; no action is available',
            )
        elif entry.dispatch_target is None:
            _skip(
                report,
                logger,
                title=entry.title,
                key=entry.key,
                category=entry.spec.category.value if entry.spec is not None else None,
                reason='no action',
                detail='the row has no dispatchable action in this frame',
            )


def _record_unrun(
    report: RunReport,
    roster: Roster | None,
    state: DailyStateStore,
    attempted: set[str],
    *,
    status: RunStatus,
    detail: str | None,
    logger: logging.Logger | None = None,
    blacklisted: Mapping[str, int] | None = None,
    blocked: set[str] | None = None,
    settled: set[str] | None = None,
) -> None:
    """Name every row still eligible when the walk stops.

    An attempted row is not necessarily finished: a repeatable task can remain
    below its daily limit, and a retryable failure can still be pending when the
    pass bound wins. Blocked, settled and blacklisted rows are not eligible and
    keep their more specific reason. On a normally exhausted walk, attempted
    rows stay out of the ``not selected`` table as before.
    """
    if roster is None:
        return
    if status is RunStatus.BUDGET:
        if detail is not None and detail.startswith('pass budget'):
            reason, why = 'pass budget', detail
        else:
            reason, why = 'run budget', detail or 'the run budget stopped the walk'
    elif status is RunStatus.STOPPED:
        reason, why = 'stopped', detail or 'a stop was requested'
    else:
        reason, why = 'not selected', detail or 'the roster was exhausted'
    blocked = blocked or set()
    settled = settled or set()
    for entry in _ordered_entries(roster.entries):
        if not entry.allowed or entry.spec is None:
            continue
        spec = entry.spec
        if spec.category not in _EXECUTORS:
            continue
        identity = _identity(entry)
        if blacklisted is not None and identity in blacklisted:
            attempts = blacklisted[identity]
            _skip(
                report,
                logger,
                title=entry.title,
                key=spec.key,
                category=spec.category.value,
                reason=f'blacklisted after {attempts} attempts',
                detail=(
                    f'{entry.title!r} already spent {attempts} attempt(s) today; it is skipped until the next local day'
                ),
            )
            continue
        if identity in blocked or identity in settled or entry.dispatch_target is None:
            continue
        if state.count(spec.key) >= spec.max_per_run:
            if identity not in attempted:
                _skip(
                    report,
                    logger,
                    title=entry.title,
                    key=spec.key,
                    category=spec.category.value,
                    reason='daily limit',
                    detail=f'{spec.key!r} already reached its {spec.max_per_run} completion(s) today',
                )
            continue
        if identity in attempted and status not in (RunStatus.BUDGET, RunStatus.STOPPED):
            continue
        _skip(
            report,
            logger,
            title=entry.title,
            key=spec.key,
            category=spec.category.value,
            reason=reason,
            detail=why,
        )


def _roster_incomplete_without_rotation(roster: dict[str, Any] | None) -> bool:
    """Whether the roster is open for a reason other than the rotation sample."""
    if not roster:
        return False
    split_failures = cast(list[dict[str, Any]], roster.get('split_failures') or [])
    return bool(
        roster.get('surface_variant') in {'non_app', 'rows_absent'}
        or roster.get('unreadable_count')
        or roster.get('card_structure_failures')
        or roster.get('unassigned_action_count')
        or roster.get('scroll_limit_reached')
        or roster.get('scroll_surface_lost')
        or roster.get('scroll_surface_unconfirmed')
        or any(not failure.get('recovered', False) for failure in split_failures)
    )


def _night_record(
    report: RunReport,
    state: DailyStateStore,
    catalogue: TaskCatalogue,
    *,
    roster: Roster | None = None,
) -> NightRecord:
    """Decide what a finished roster walk leaves for the rest of the day (#97).

    A night is ``exhausted`` when nothing dispatched is worth retrying: every
    row completed or was skipped/refused (policy, daily limit, the app reporting
    it unavailable), and no claimable row's final dispatch ended ``failed``. A
    failed row is still unclaimed work for a later run while its daily limit has
    room; a row that failed and then completed, or whose card already reached its
    limit, is not unfinished work (#143).

    Any unresolved failure keeps the day open even when another row completed:
    the failed row is still unclaimed work, and the operator most needs the
    daemon to try it while asleep. A dormant family is not unfinished work, but
    it is recorded on the night so the day state names what it relied on (#161).
    """
    completed = sum(1 for task in report.tasks if task.completed)
    retryable = len(_claimable_task_failures(report, state, catalogue))
    roster_incomplete = bool(report.roster and report.roster.get('incomplete', False))
    if roster_incomplete:
        outcome = NightOutcome.INCOMPLETE_ROSTER
    elif retryable or report.blacklist:
        # A blacklisted task is an exhausted attempt bound, not proof that the
        # day is finished. Keep the day open so the next local day's fresh
        # blacklist can try it again.
        outcome = NightOutcome.RETRYABLE_FAILURES
        retryable = max(retryable, len(report.blacklist))
    else:
        outcome = NightOutcome.EXHAUSTED
    return NightRecord(
        outcome=outcome,
        dispatched=len(report.tasks),
        completed=completed,
        retryable_failures=retryable,
        dormant_families=() if roster is None else roster.dormant_families,
    )


def _final_task_records(tasks: Iterable[TaskRecord]) -> list[TaskRecord]:
    """The last dispatch of each task identity, in first-dispatch order.

    A row can be dispatched more than once in one night: a safe retry after a
    no-tap failure, or a repeatable task below its daily limit. Only the last
    dispatch says whether that row ended completed or failed.
    """
    latest: dict[str, TaskRecord] = {}
    for task in tasks:
        latest[task.key or task.title.casefold()] = task
    return list(latest.values())


def _claimable_task_failures(
    report: RunReport,
    state: DailyStateStore,
    catalogue: TaskCatalogue,
) -> list[TaskRecord]:
    """Final failed rows that still have room under their daily limit.

    A failed dispatch is not always unclaimed work: the search executor can
    record both rounds and then fail only on its return route. When the card is
    already at ``max_per_run``, the day has accounted for it and the failure must
    not keep the night open or be described as claimable (#143).

    A combined sign-in dispatch owns two independent claims, so it is only
    exhausted when both keys have reached their limit. A state read failure is
    treated as claimable: hiding a failed row behind an unreadable counter is the
    same defect this check exists to prevent.
    """
    failures: list[TaskRecord] = []
    for task in _final_task_records(report.tasks):
        if task.outcome != 'failed':
            continue
        keys = _SIGNIN_KEYS if task.key in _SIGNIN_KEYS else {task.key}
        specs = [candidate for candidate in catalogue.allowed if candidate.key in keys]
        exhausted = True
        for spec in specs:
            count = _daily_count(state, spec.key)
            if count is None or count < spec.max_per_run:
                exhausted = False
                break
        if not exhausted:
            failures.append(task)
    return failures


def _daily_count(state: DailyStateStore, key: str) -> int | None:
    """Read a daily completion count without letting a state fault escape finalisation."""
    try:
        return state.count(key)
    except Exception as exc:
        LOG.warning(
            '%s',
            _event('state_claimability_failed', key=key, detail=f'{type(exc).__name__}: {exc}'),
        )
        return None


def _due_probe_keys(
    store: FamilyObservationStore,
    catalogue: TaskCatalogue,
    day: date,
    logger: logging.Logger,
) -> tuple[str, ...]:
    """Dormant families whose periodic extra sample is due, if the ledger reads."""
    keys = tuple(spec.key for spec in catalogue.allowed if spec.category in _EXECUTORS)
    try:
        return store.due_for_probe(keys, day)
    except Exception as exc:
        logger.warning(
            '%s',
            _event('family_probe_check_failed', detail=f'{type(exc).__name__}: {exc}'),
        )
        return ()


def _family_rotation(
    roster: Roster,
    catalogue: TaskCatalogue,
    state: DailyStateStore,
    blacklisted: Mapping[str, int],
    store: FamilyObservationStore,
    *,
    day: date,
    samples: int,
    dormancy_days: int,
    retry_days: int,
    probed: Collection[str],
    trustworthy: bool,
    logger: logging.Logger,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[FamilyEvidence, ...], tuple[FamilyEvidence, ...]]:
    """Split unseen catalogue families into unread, claimed and not offered.

    The sheet rotates, so a family absent from the union of every readable sample
    is not automatically "unread". This function keeps the cross-day evidence:

    * ``unread`` — no complete absence streak yet, so the read may have missed
      the family behind another rotation. It keeps the night open (#149).
    * ``already claimed`` — the day state already reached the family's daily
      limit, so its absence is expected.
    * ``not offered`` — the family crossed :data:`family_dormancy_days`
      consecutive complete days without a sighting. It no longer blocks
      completion, but it is named with its streak and sample count. A dormant
      family also gets one explicit extra sample on the ``retry_days`` cadence,
      so a returning row is still caught even if the ordinary samples miss it
      (#161).

    A read with a lost surface or unreadable rows is *not* evidence of absence:
    ``trustworthy`` is false and the stored streak is left untouched, so the bad
    read keeps the night open without manufacturing dormancy.
    """
    seen = {entry.key for entry in roster.allowed_entries if entry.key is not None}
    unclaimed: list[str] = []
    claimed: list[str] = []
    tracked: list[str] = []
    for spec in catalogue.allowed:
        if spec.category not in _EXECUTORS:
            continue
        seen_key = spec.key in seen
        if spec.key in blacklisted:
            # A blacklisted family is not absence evidence, but a sighting is
            # still positive evidence and must be able to clear dormancy.
            if seen_key:
                tracked.append(spec.key)
            continue
        count = _daily_count(state, spec.key)
        if count is not None and count >= spec.max_per_run:
            claimed.append(spec.key)
            # A sighting still clears stale dormancy even when the card was
            # claimed earlier in the day.
            if seen_key:
                tracked.append(spec.key)
            continue
        unclaimed.append(spec.key)
        tracked.append(spec.key)

    missing_tracked = tuple(key for key in tracked if key not in seen)
    seen_tracked = tuple(key for key in tracked if key in seen)
    try:
        observations: tuple[FamilyEvidence, ...]
        if trustworthy:
            observations = store.observe(
                day=day,
                keys=tracked,
                seen=seen,
                samples=samples,
                dormancy_days=dormancy_days,
                retry_days=retry_days,
                probed=probed,
            )
        else:
            # A sighting is positive evidence even when another row made the
            # read incomplete, so clear dormancy for what was seen. A miss is
            # not evidence in this pass, so only peek at the missing families.
            sightings = (
                store.observe(
                    day=day,
                    keys=seen_tracked,
                    seen=seen,
                    samples=samples,
                    dormancy_days=dormancy_days,
                    retry_days=retry_days,
                    probed=probed,
                )
                if seen_tracked
                else ()
            )
            observations = tuple(sightings) + store.peek(missing_tracked)
    except Exception as exc:
        # Evidence storage is an artifact, not the task surface. A failed read
        # or write must not turn an unknown family into a missing one, so fall
        # back to the #149 rule: every unseen claimable family stays unread.
        logger.warning(
            '%s',
            _event(
                'family_observation_failed',
                detail=f'{type(exc).__name__}: {exc}',
            ),
        )
        observations = ()
    by_key = {evidence.key: evidence for evidence in observations}

    unread: list[str] = []
    not_offered: list[FamilyEvidence] = []
    for key in unclaimed:
        if key in seen:
            continue
        evidence = by_key.get(key, FamilyEvidence(key=key))
        if evidence.dormant:
            not_offered.append(evidence)
        else:
            unread.append(key)

    if not_offered:
        logger.info(
            '%s',
            _event(
                'families_not_offered',
                families=[evidence.as_dict() for evidence in not_offered],
                samples=samples,
                trustworthy=trustworthy,
            ),
        )
    for evidence in not_offered:
        if evidence.probe_due:
            logger.info(
                '%s',
                _event(
                    'family_probe_due',
                    key=evidence.key,
                    missed_days=evidence.missed_days,
                    samples=evidence.samples,
                    next_probe=None if evidence.next_probe is None else evidence.next_probe.isoformat(),
                ),
            )

    dormant = tuple(evidence for evidence in observations if evidence.dormant)
    return tuple(unread), tuple(claimed), tuple(not_offered), dormant


def _incomplete_run_detail(
    report: RunReport,
    state: DailyStateStore,
    catalogue: TaskCatalogue,
) -> str | None:
    """Name the allowed rows that make a finished walk an incomplete night.

    A failed row carries its taxonomy code and executor detail, so the report
    and the notification say which task failed and why. A row blacklisted in an
    earlier run is named by key and attempt count. Policy-excluded rows never
    enter either list, so exclusion remains a successful day; a failed row whose
    daily card is already exhausted is likewise not unfinished work (#143).
    """
    failed = _claimable_task_failures(report, state, catalogue)
    named = [f'{task.title!r} ({task.code or "failed"}: {task.detail})' for task in failed]
    failed_keys = {task.key or task.title.casefold() for task in failed}
    named.extend(
        f'{key!r} (blacklisted after {attempts} attempt(s))'
        for key, attempts in sorted(report.blacklist.items())
        if key not in failed_keys
    )
    if not named:
        return None
    count = len(named)
    noun = 'task' if count == 1 else 'tasks'
    return f'{count} allowed {noun} did not complete and remain claimable: ' + '; '.join(named)


def _selection_key(entry: RosterEntry) -> tuple[int, str, str, str]:
    """Order one roster row by policy, never by its position in the frame."""
    spec = entry.spec
    if spec is None:
        return (len(_CATEGORY_COST), '', entry.title.casefold(), (entry.subtitle or '').casefold())
    return (
        _CATEGORY_COST.get(spec.category, len(_CATEGORY_COST)),
        spec.key,
        entry.title.casefold(),
        (entry.subtitle or '').casefold(),
    )


def _ordered_entries(entries: Iterable[RosterEntry]) -> list[RosterEntry]:
    """Rank rows for selection without changing the roster's recorded order."""
    return sorted(entries, key=_selection_key)


def _entry_dispatchable(
    entry: RosterEntry,
    state: DailyStateStore,
    *,
    blocked: set[str],
    blacklisted: Mapping[str, int] | None,
    attempted: set[str] | None,
    settled: set[str] | None,
) -> bool:
    """Whether one row may be dispatched under the current pass's bounds."""
    if not entry.allowed or entry.spec is None or entry.dispatch_target is None:
        return False
    if entry.spec.category not in _EXECUTORS:
        return False
    identity = _identity(entry)
    if identity in blocked:
        return False
    if blacklisted is not None and identity in blacklisted:
        return False
    if attempted is not None and identity in attempted:
        return False
    if settled is not None and identity in settled:
        return False
    return state.count(entry.spec.key) < entry.spec.max_per_run


def _next_entry(
    roster: Roster,
    state: DailyStateStore,
    blocked: set[str],
    *,
    blacklisted: Mapping[str, int] | None = None,
    attempted: set[str] | None = None,
    settled: set[str] | None = None,
) -> RosterEntry | None:
    """Return the next dispatching row in explicit cost order.

    The roster is a description of a frame, so its order is reading order and is
    not stable between runs (rows rotate in place; a scrolled list will grow and
    reorder). Selection therefore sorts the eligible rows with
    :func:`_selection_key`: category cost first, then catalogue key, then the
    case-folded title and subtitle. Two runs over the same set of rows make the
    same choice even when OCR enumerated them in a different order.
    """
    for entry in _ordered_entries(roster.entries):
        if _entry_dispatchable(
            entry,
            state,
            blocked=blocked,
            blacklisted=blacklisted,
            attempted=attempted,
            settled=settled,
        ):
            return entry
    return None


def _popup_recorder(report: RunReport) -> Callable[[DismissReport], None]:
    def record(dismissal: DismissReport) -> None:
        report.record_popups(dismissal.closed)

    return record


def _observe_balance(context: RunContext, balance: float | None, log: logging.Logger) -> None:
    """Persist a verified coin-page reading without making a balance write fail the run.

    ``None`` means this attempt could not read the balance. The store keeps a
    previously verified value rather than inventing or erasing one (#94).
    """
    try:
        context.state.observe_balance(balance)
    except Exception as exc:
        log.warning('%s', _event('state_balance_failed', detail=f'{type(exc).__name__}: {exc}'))


def _record_balance_before(
    context: RunContext,
    controller: Controller,
    report: RunReport,
    artifacts: ArtifactStore,
    frame: Snapshot,
    log: logging.Logger,
    on_dismiss: Callable[[DismissReport], None] | None = None,
) -> None:
    """Prove the coin page before recording the balance the run started from.

    A cold start lands on the home feed. A number read there is not a coin
    balance, so the run leaves ``balance_before`` unknown (and says why) rather
    than computing a delta from the wrong screen.
    """
    try:
        result = return_to_coins_page(
            controller,
            context.capture,
            context.corpus,
            context.navigate,
            snapshot=frame,
            templates=context.templates,
            route=SurfaceRoute.SHORTCUT,
            settle=context.config.settle_seconds,
            clock=context.clock,
            sleep=context.sleep,
            logger=log,
            on_dismiss=on_dismiss,
        )
    except RunAborted, RunInterrupted:
        raise
    except AppLoggedOut:
        raise
    except Exception as exc:
        report.balance_note = f'coins page not proven before the first task: {type(exc).__name__}: {exc}'
        log.warning('%s', _event('balance_before_unavailable', detail=report.balance_note))
        return
    if result.snapshot is not None:
        _guard_signed_in(context, result.snapshot)
    if not result.returned or result.snapshot is None or not _on_coin_page(result.snapshot):
        report.balance_note = 'coins page not proven before the first task; balance before is unknown'
        log.warning('%s', _event('balance_before_unavailable', detail=report.balance_note))
        return
    recorded = _record_frame(report, artifacts, result.snapshot, 'balance-before', log)
    report.balance_before = read_coin_balance(recorded)
    if context.safety is not None:
        context.safety.expect_balance(report.balance_before)
    _observe_balance(context, report.balance_before, log)
    if report.balance_before is None:
        report.balance_note = 'the coins page did not render a readable balance before the first task'


def _finish_on_coins_page(
    context: RunContext,
    controller: Controller,
    report: RunReport,
    artifacts: ArtifactStore,
    log: logging.Logger,
    *,
    on_dismiss: Callable[[DismissReport], None] | None = None,
) -> None:
    try:
        frame = _capture(context.capture)
        if _on_coin_page(frame):
            report.returned_to_coins_page = True
            recorded = _record_frame(report, artifacts, frame, 'balance-after', log)
            report.balance_after = read_coin_balance(recorded)
            _observe_balance(context, report.balance_after, log)
            return
        result = return_to_coins_page(
            controller,
            context.capture,
            context.corpus,
            context.navigate,
            snapshot=frame,
            templates=context.templates,
            route=SurfaceRoute.SHORTCUT,
            settle=context.config.settle_seconds,
            clock=context.clock,
            sleep=context.sleep,
            logger=log,
            on_dismiss=on_dismiss,
        )
        report.returned_to_coins_page = result.returned
        report.return_error = result.error
        if result.returned and result.snapshot is not None:
            recorded = _record_frame(report, artifacts, result.snapshot, 'balance-after', log)
            report.balance_after = read_coin_balance(recorded)
            _observe_balance(context, report.balance_after, log)
    except RunAborted:
        raise
    except RunInterrupted as exc:
        report.returned_to_coins_page = False
        report.return_error = f'stop requested during cleanup: {exc}'
    except Exception as exc:
        report.returned_to_coins_page = False
        report.return_error = f'{type(exc).__name__}: {exc}'
        log.warning('%s', _event('return_failed', detail=report.return_error))


def _record_frame(
    report: RunReport,
    artifacts: ArtifactStore,
    frame: Snapshot,
    name: str,
    log: logging.Logger,
) -> Snapshot:
    """Write a frame and read the report balance back from the recorded copy.

    Reloading the PNG sidecar is deliberate: the summary's balance must be the
    number a human can recover from ``shots/`` with the artifact store, not a
    value that only existed in memory. A failed artifact write is reported but
    does not end the run; the in-memory frame is then the best available copy.
    """
    try:
        paths = artifacts.write(frame, name)
        report.record_shot(name, paths.image)
        return artifacts.load(name)
    except Exception as exc:
        detail = f'{name}: {type(exc).__name__}: {exc}'
        report.record_artifact_error(detail)
        log.warning('%s', _event('artifact_write_failed', detail=detail))
        return frame


def _record_failure_frame(
    context: RunContext,
    report: RunReport,
    artifacts: ArtifactStore,
    log: logging.Logger,
    *,
    capture: CaptureSource | None = None,
) -> None:
    """Best-effort final frame for a failed run; never mask the original error."""
    if 'failure' in report.shots:
        return
    try:
        frame = _capture(context.capture if capture is None else capture)
    except RunAborted:
        raise
    except Exception as exc:
        report.record_artifact_error(f'failure frame: {type(exc).__name__}: {exc}')
        log.warning('%s', _event('failure_frame_failed', detail=f'{type(exc).__name__}: {exc}'))
        return
    _record_frame(report, artifacts, frame, 'failure', log)


def _record_dismissals(report: RunReport, payload: dict[str, Any]) -> None:
    for dismissal in _dismissal_payloads(payload):
        closed = dismissal.get('closed')
        if isinstance(closed, list):
            report.record_popups(str(family) for family in closed)


def _dismissal_payloads(value: Any) -> list[dict[str, Any]]:
    """Find every embedded ``DismissReport.as_dict`` in an executor payload."""
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {'dismiss', 'dismissals'}:
                if isinstance(item, dict):
                    found.append(item)
                elif isinstance(item, list):
                    found.extend(entry for entry in item if isinstance(entry, dict))
            else:
                found.extend(_dismissal_payloads(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_dismissal_payloads(item))
    return found


def _should_notify(report: RunReport, *, notify_on_empty: bool, notify_every_run: bool = False) -> bool:
    if notify_every_run:
        return True
    if not report.ok:
        return True
    return bool(notify_on_empty and not any(task.completed for task in report.tasks))


def _notification(report: RunReport, *, include_balance_delta: bool = False) -> Notification:
    """Build the one message a run sends.

    The body deliberately carries no account data: no nickname, no absolute
    balance and no order/address text. The balance *delta* is included only when
    the operator opts in, because even a small change is account information
    they may not want in a third-party notification service.
    """
    lines = [f'status: {report.status.value}']
    if report.failure is not None:
        lines.append(f'failure: {_failure_line(report.failure)}')
    if report.error:
        lines.append(f'error: {report.error}')
    elif report.detail:
        lines.append(f'detail: {report.detail}')
    lines.append(f'tasks: {_task_count_line(report)}')
    if report.skipped:
        lines.append(f'not run: {_skip_count_line(report)}')
    if include_balance_delta:
        delta = report.balance_delta
        lines.append(f'balance delta: {"unknown" if delta is None else f"{delta:+g}"}')
    lines.append(f'report: {report.summary_path}')
    tags: tuple[str, ...] = ('coins', report.status.value)
    if report.failure_code:
        tags = (*tags, report.failure_code)
    return Notification(
        title=f'aliexpress-coins {report.status.value} {report.run_id}',
        body='\n'.join(lines),
        run_id=report.run_id,
        status=report.status.value,
        tags=tags,
    )


def _task_count_line(report: RunReport) -> str:
    """Counts by dispatched outcome, with the three common outcomes first."""
    counts: dict[str, int] = {}
    for task in report.tasks:
        counts[task.outcome] = counts.get(task.outcome, 0) + 1
    order = ('completed', 'skipped', 'failed')
    parts = [f'{outcome}={counts.pop(outcome, 0)}' for outcome in order]
    parts.extend(f'{outcome}={count}' for outcome, count in sorted(counts.items()))
    return ', '.join(parts)


def _skip_count_line(report: RunReport) -> str:
    """Counts for rows that were never dispatched, grouped by report reason."""
    counts: dict[str, int] = {}
    for skip in report.skipped:
        counts[skip.reason] = counts.get(skip.reason, 0) + 1
    return ', '.join(f'{reason}={count}' for reason, count in sorted(counts.items()))


def _failure_line(failure_dict: dict[str, Any]) -> str:
    code = failure_dict.get('code', 'unknown')
    sentence = failure_dict.get('sentence', '')
    detail = failure_dict.get('detail')
    text = f'{code}: {sentence}' if sentence else str(code)
    return text if not detail else f'{text} ({detail})'


def _delivery_error(exc: Exception) -> str:
    """Name a delivery failure without recording its untrusted message.

    A target URL is a secret when it carries userinfo, and a custom sink can
    raise an exception whose text includes it. The class plus an HTTP status is
    enough to diagnose delivery without ever copying that text into artifacts.
    """
    status = getattr(exc, 'code', None)
    if not isinstance(status, int):
        status = getattr(exc, 'status', None)
    suffix = f' (HTTP {status})' if isinstance(status, int) else ''
    return f'{type(exc).__name__}{suffix}'


def _notify(context: RunContext, report: RunReport, log: logging.Logger) -> None:
    """Deliver one notification after the run's own outcome is already written."""
    notifier = context.notifier
    if notifier is None or not _should_notify(
        report,
        notify_on_empty=context.config.notify_on_empty,
        notify_every_run=context.config.apprise_url is not None,
    ):
        return
    name = getattr(notifier, 'name', type(notifier).__name__)
    record: dict[str, Any] = {'attempted': True, 'ok': False, 'sink': name, 'error': None}
    try:
        notifier.send(_notification(report, include_balance_delta=context.config.notify_balance_delta))
    except Exception as exc:
        record['error'] = _delivery_error(exc)
        log.warning('%s', _event('notification_failed', sink=name, detail=record['error']))
    else:
        record['ok'] = True
        log.info('%s', _event('notification_sent', sink=name, status=report.status.value))
    report.notification = record
    try:
        report.write()
    except Exception as exc:
        log.warning('%s', _event('report_rewrite_failed', detail=f'{type(exc).__name__}: {exc}'))


def _prune(context: RunContext, report: RunReport, log: logging.Logger) -> None:
    """Apply the keep-last-N rule without letting retention fail the run."""
    try:
        pruned = prune_runs(
            report.runs_dir,
            keep=context.config.keep_runs,
            current_run_id=report.run_id,
            logger=log,
        )
    except Exception as exc:
        log.warning('%s', _event('run_prune_failed', detail=f'{type(exc).__name__}: {exc}'))
        return
    if pruned:
        log.info('%s', _event('runs_pruned', keep=context.config.keep_runs, pruned=[path.name for path in pruned]))


def _controller(context: RunContext, frame: Snapshot) -> Controller:
    if context.controller_factory is None:
        raise ConfigError('the run context has neither a controller nor a controller factory')
    return context.controller_factory(frame)


def _spec(entry: RosterEntry) -> AllowedTaskSpec:
    spec = entry.spec
    if spec is None or not entry.allowed:
        raise ValueError(f'{entry.title!r} is not an allowed row')
    return spec


def _identity(entry: RosterEntry) -> str:
    return entry.key if entry.key is not None else entry.title.casefold()


def _capture(source: CaptureSource) -> Snapshot:
    if callable(source):
        return source()
    return source.capture()


def _on_coin_page(snapshot: Snapshot) -> bool:
    return all(contains_text(snapshot, marker) for marker in COIN_PAGE_MARKERS) and not is_boost_page(snapshot)


def _geometry(snapshot: Snapshot) -> dict[str, object]:
    geometry = snapshot.geometry
    return {
        'width': geometry.width,
        'height': geometry.height,
        'density': geometry.density,
        'sdk': geometry.sdk,
        'orientation': geometry.orientation,
        'profile_id': geometry.profile_id,
    }


def _task_duration(context: RunContext, started: float | None) -> float:
    """How long the in-flight task ran before the run stopped, when known."""
    if started is None:
        return 0.0
    return max(0.0, context.clock() - started)


def _event(name: str, **fields: object) -> str:
    return json.dumps({'event': name, **fields}, separators=(',', ':'), sort_keys=True, default=str)


def _guarded_sleep(stop: StopSignal) -> Callable[[float], None]:
    def sleep(seconds: float) -> None:
        if seconds <= 0:
            if stop.is_set():
                raise RunInterrupted('stop requested')
            return
        if stop.wait(seconds):
            raise RunInterrupted('stop requested')

    return sleep


def _guarded_call(function: Callable[..., Any], stop: StopSignal) -> Callable[..., Any]:
    def call(*args: Any, **kwargs: Any) -> Any:
        if stop.is_set():
            raise RunInterrupted('stop requested')
        result = function(*args, **kwargs)
        if stop.is_set():
            raise RunInterrupted('stop requested')
        return result

    return call
