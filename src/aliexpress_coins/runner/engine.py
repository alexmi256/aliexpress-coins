"""One night's work: hygiene, route, roster walk, budgets and a report (#29).

The engine is deliberately a coordinator. It does not tap a new path, parse a
new row or classify a new screen: it calls the existing sign-in, browse, quiz,
surprise-item and notification executors, and it hands each one the roster row it
was allowed to run. The surface functions (#22/#38/#43) decide what is allowed
and what is still available.

Two budgets bound the walk:

* ``AECOINS_MAX_TASKS_PER_RUN`` is the task budget, counted in dispatches;
* ``AECOINS_RUN_BUDGET_SECONDS`` is the wall-clock budget, checked before every
  dispatch and after every completion.

When either stops the walk, the remaining rows are written to the report with
the reason they were not attempted. The final ``finally`` asks the existing
return route to prove the coins page, so a budget stop is still a clean stop.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from aliexpress_coins.control.controller import Controller
from aliexpress_coins.control.device import AdbInputDevice
from aliexpress_coins.control.errors import PopupUnclosable
from aliexpress_coins.control.popups import DismissReport, PopupCorpus, load_popup_corpus
from aliexpress_coins.perception.artifacts import ArtifactStore
from aliexpress_coins.perception.capture import AdbSnapshotter
from aliexpress_coins.perception.errors import FrameDecodeError, SnapshotError, StateTimeout
from aliexpress_coins.perception.ocr import RapidOcrEngine
from aliexpress_coins.perception.snapshot import Snapshot
from aliexpress_coins.perception.state import CaptureSource, ScreenState, classify
from aliexpress_coins.perception.templates import TemplateStore, load_templates
from aliexpress_coins.perception.waits import wait_for_text
from aliexpress_coins.runner.config import RunConfig
from aliexpress_coins.runner.device import AdbDevice, HygieneDevice
from aliexpress_coins.runner.errors import AppLoggedOut, ConfigError, RunInterrupted
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
from aliexpress_coins.runner.report import RunEventLog, RunReport, RunStatus, TaskRecord, prune_runs
from aliexpress_coins.runner.safety import UNEXPECTED_SCREEN_LIMIT, AbortCode, RunAborted, SafetyGuard
from aliexpress_coins.runner.state import DailyStateStore, NightOutcome, NightRecord
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
    discover_roster,
    enumerate_roster,
    open_boost_list,
    wait_for_coin_page,
)
from aliexpress_coins.tasks.dispatch import (
    DispatchGate,
    PolicyRefused,
    authorization_of,
)
from aliexpress_coins.tasks.notifications import run_notifications_task
from aliexpress_coins.tasks.quiz import run_quiz_task
from aliexpress_coins.tasks.search import run_search_task
from aliexpress_coins.tasks.signin import claim_daily_signins, read_coin_balance
from aliexpress_coins.tasks.surface import (
    COIN_PAGE_MARKERS,
    RouteFirer,
    Surface,
    SurfaceRoute,
    classify_surface,
    contains_text,
    has_task_rows,
    is_boost_page,
    is_logged_out,
)
from aliexpress_coins.tasks.surprise import run_surprise_task

__all__ = [
    'DISPATCH_TABLE',
    'TASK_RECOVERY_ATTEMPTS',
    'EngineResult',
    'RunContext',
    'StopSignal',
    'make_live_context',
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
    controller: Controller | None = None
    controller_factory: Callable[[Snapshot], Controller] | None = None
    observer: RosterObserver | None = None
    templates: TemplateStore | None = None
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    wall_clock: Callable[[], datetime] = _utc_now
    stop: StopSignal = field(default_factory=_NeverStop)
    logger: logging.Logger | None = None
    notifier: NotificationSink | None = None
    gate: DispatchGate | None = None
    safety: SafetyGuard | None = None


_EXECUTORS: frozenset[TaskCategory] = frozenset(
    {
        TaskCategory.SIGN_IN,
        TaskCategory.QUIZ,
        TaskCategory.BROWSE,
        TaskCategory.BROWSE_WAIT,
        TaskCategory.ITEM_TAP,
        TaskCategory.NOTIFICATION,
    },
)

DISPATCH_TABLE: dict[TaskCategory, str] = {
    TaskCategory.SIGN_IN: 'sign-in',
    TaskCategory.QUIZ: 'quiz',
    TaskCategory.BROWSE: 'search',
    TaskCategory.BROWSE_WAIT: 'browse-and-wait',
    TaskCategory.ITEM_TAP: 'surprise-items',
    TaskCategory.NOTIFICATION: 'notifications',
}
"""The catalogue categories this engine can dispatch, and their executor names.

``shortcut`` and ``widget`` are deliberately absent: their executors belong to
#27 and a row in those categories is reported as skipped rather than guessed at.
"""

TASK_RECOVERY_ATTEMPTS = UNEXPECTED_SCREEN_LIMIT
"""How many shortcut entries a task's failed screen gets before the run aborts.

The same number as the sighting limit keeps the rule easy to state: three
sightings fail the task, three unsuccessful returns fail the night (#83).
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
    navigate = _guarded_call(device.navigate, stop)
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
    true for a completed or budget-ended run; a hygiene, route or report failure
    is a run-level failure. Every run-level failure is named by
    :mod:`aliexpress_coins.runner.failures` and recorded in ``run.json`` under
    ``failure``; a task that did not verify carries its own code in the task row.
    """
    log = context.logger if context.logger is not None else LOG
    budget = context.config.budget_seconds if budget_seconds is None else budget_seconds
    task_limit = context.config.max_tasks if max_tasks is None else max_tasks
    if budget <= 0:
        raise ValueError(f'budget_seconds must be positive, got {budget!r}')
    if task_limit < 1:
        raise ValueError(f'max_tasks must be positive, got {task_limit!r}')

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
    status = RunStatus.COMPLETED
    detail: str | None = None
    attempted: set[str] = set()
    blocked: set[str] = set()
    task_started: float | None = None
    artifacts = ArtifactStore(report.runs_dir, report.run_id)
    record_dismissal = _popup_recorder(report)
    event_log = RunEventLog(report.event_log_path)
    raw_capture = context.capture
    log.addHandler(event_log)
    try:
        event_log.record(
            {'event': 'run_started', 'run_id': report.run_id, 'started_at': report.started_at.isoformat()},
        )
    except Exception as exc:
        log.warning('%s', _event('run_log_failed', detail=f'{type(exc).__name__}: {exc}'))

    safety = context.safety if context.safety is not None else SafetyGuard(catalogue=context.catalogue)
    if context.safety is None:
        context.safety = safety
        context.capture = safety.wrap(context.capture)
    gate = context.gate if context.gate is not None else DispatchGate(context.catalogue, logger=log)
    context.gate = gate

    try:
        report.write()
        hygiene = _retry_stage(context, 'hygiene', context.hygiene.prepare)
        report.hygiene = hygiene.as_dict()
        frame = _capture(context.capture)
        _guard_signed_in(context, frame)
        if context.config.dry_run:
            _plan_dry_run(context, report, frame, gate, log, max_tasks=task_limit, budget_seconds=budget)
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
            context.state.begin_run(at=context.wall_clock())
            observer = (
                observer
                if observer is not None
                else _observer(context, report, roster, max_observations=task_limit + 2)
            )
            _observe(context, controller, observer, route=roster.route, on_dismiss=record_dismissal)
            _record_policy_skips(report, roster, logger=log, gate=gate)
            report.observations = observer.sequence
            report.write()

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
                    detail = f'task budget of {task_limit} dispatch(es) exhausted'
                    report.record_failure(failure(FailureCode.BUDGET_EXHAUSTED, detail=detail))
                    break

                current = observer.roster if observer.roster is not None else roster
                if current is None:  # pragma: no cover - set immediately above
                    break
                entry = _next_entry(current, context.state, blocked)
                if entry is None:
                    detail = 'no dispatchable row remains'
                    break

                spec = entry.spec
                if spec is None:  # pragma: no cover - _next_entry only returns allowed rows
                    continue
                identity = _identity(entry)
                task_started = context.clock()
                task_index = report.begin_task(
                    title=entry.title,
                    key=spec.key,
                    category=spec.category.value,
                )
                report.write()
                result = _dispatch_with_retry(context, controller, entry)
                duration = max(0.0, context.clock() - task_started)
                attempted.add(identity)
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
                    ),
                )
                if result.completed:
                    _observe(context, controller, observer, route=None, on_dismiss=record_dismissal)
                    _record_policy_skips(report, observer.roster or roster, logger=log, gate=gate)
                    report.observations = observer.sequence
                else:
                    blocked.add(identity)
                report.write()

            final_roster = observer.roster if observer is not None and observer.roster is not None else roster
            _record_unrun(report, final_roster, context.state, attempted, status=status, detail=detail, logger=log)
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
        if status is RunStatus.COMPLETED and not context.config.dry_run:
            night = _night_record(report)
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
    max_tasks: int,
    budget_seconds: float,
) -> None:
    """Plan the night from the settled visible surface without tapping.

    The route may be fired to reach the coins page (an ``am start``, not an
    in-app tap), then the same :func:`wait_for_coin_page` a live run uses must
    see the page stop loading before the roster is enumerated. That wait is the
    #19 primitive behind #76: classifying the skeleton once and calling the
    resulting zero a plan is the "absence is not evidence" failure in the
    planning path.

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
    surface = classify_surface(frame)
    initial_state = classify(frame).state
    routes: list[SurfaceRoute] = []
    waits: list[CoinPageWait] = []
    content_waits: list[dict[str, object]] = []
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

    def settle() -> None:
        nonlocal frame, surface
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
            # spend the inline-row wait on it; the scheduler below names it.
            return
        if surface is Surface.ROWS_ABSENT:
            # The coin-page markers can render before their task rows. The live
            # path waits for the boost list here; a dry run must not tap "Earn
            # more", so it waits on the same #19 text primitive for the inline
            # rows that are the only surface it may read. A timeout is not an
            # empty day; the row wait is part of the evidence below.
            content = wait_for_text(
                context.capture,
                has_task_rows,
                timeout=DEFAULT_BOOST_TIMEOUT,
                interval=DEFAULT_BOOST_INTERVAL,
                initial=frame,
                clock=context.clock,
                sleep=context.sleep,
            )
            content_waits.append(content.as_dict())
            frame = content.frame
            surface = classify_surface(frame)

    def read_roster() -> Roster:
        return replace(
            enumerate_roster(frame, context.catalogue, route=DiscoveryRoute.INLINE),
            surface_variant=surface,
            routes_taken=tuple(routes),
        )

    initial_roster = read_roster()
    if (
        (surface is Surface.UNKNOWN and initial_roster.empty)
        or initial_state is ScreenState.POPUP
        or surface in (Surface.ROWS_ABSENT, Surface.EMPTY_BOOST)
    ) and fire_route():
        # A popup may be cleared by the route, and an unclassified or empty
        # surface gets one shortcut attempt. A populated page is read as is.
        settle()
        roster = read_roster()
    else:
        roster = initial_roster

    unreadable = _unreadable_plan_reason(roster, surface, waits, content_waits, settle_note, initial_state)
    if unreadable is not None:
        last = waits[-1] if waits else None
        last_state = last.classification.state if last is not None else initial_state
        if last_state is ScreenState.POPUP:
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
    for entry in roster.entries:
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
                reason='no action',
                detail='the row has no dispatchable action in this frame',
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
                detail=f'the task budget of {max_tasks} would stop the walk here',
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
        plan_detail = f'dry run: {unreadable}; no tap was issued'
    else:
        report.status = RunStatus.PLANNED
        report.detail = f'dry run: {len(steps)} task(s) planned, {len(refusals)} refusal(s); no tap was issued'
        plan_detail = report.detail

    report.plan = {
        'dry_run': True,
        'surface': surface.value,
        'max_tasks': max_tasks,
        'budget_seconds': budget_seconds,
        'steps': steps,
        'refusals': refusals,
        'taps_issued': 0,
        'waits': [wait.as_dict() for wait in waits],
        'content_waits': content_waits,
        'detail': plan_detail,
    }
    log.log(
        logging.ERROR if unreadable is not None else logging.INFO,
        '%s',
        _event(
            'dry_run_plan',
            surface=surface.value,
            steps=[step['label'] for step in steps],
            refusals=[refusal['label'] for refusal in refusals],
            taps_issued=0,
            unreadable=unreadable,
        ),
    )
    report.write()


def _unreadable_plan_reason(
    roster: Roster,
    surface: Surface,
    waits: list[CoinPageWait],
    content_waits: list[dict[str, object]],
    settle_note: str | None,
    initial_state: ScreenState,
) -> str | None:
    """Name why an empty plan is not evidence about the day, or return ``None``.

    A dry run may only exit 0 after it read a complete roster. An unsettled
    frame, a popup cover, an unclassified page, the empty-boost load fault, or a
    coin page whose inline rows never appeared is an incomplete read, not an
    empty day.
    """
    last = waits[-1] if waits else None
    last_state = last.classification.state if last is not None else initial_state
    if last_state is ScreenState.POPUP:
        reason = 'surface_popup: a popup covers the coin page and a dry run cannot dismiss it'
    elif surface is Surface.EMPTY_BOOST:
        reason = 'surface_empty_boost: the Coins boost page was still the documented load fault'
    elif surface is Surface.UNKNOWN:
        if roster.empty:
            reason = 'surface_unknown: the coin page did not render'
        else:
            rows = ', '.join(entry.title for entry in roster.entries)
            reason = f'surface_unverified: task rows were read but the coin page marker was missing (rows: {rows})'
    elif not roster.empty:
        return None
    elif surface is Surface.ROWS_ABSENT:
        reason = 'surface_rows_incomplete: the coin page rendered but its task rows did not'
        if content_waits:
            reason = f'{reason} (row wait {content_waits[-1]["status"]})'
        else:
            reason = f'{reason} (no row wait was possible)'
    else:
        reason = 'surface_incomplete: the task surface rendered but no task row could be read'

    if last is not None:
        history = ' -> '.join(state.value for state in last.history)
        reason = f'{reason} (states={history}; settled={last.settled})'
    else:
        reason = f'{reason} (initial state {initial_state.value}; no settle wait)'
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
        roster = replace(
            enumerate_roster(frame, context.catalogue, route=DiscoveryRoute.INLINE),
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
        return _from_executor(
            run_browse_task(controller, context.capture, context.corpus, context.state, entry, **common),
        )
    if category is TaskCategory.ITEM_TAP:
        return _from_executor(
            run_surprise_task(controller, context.capture, context.corpus, context.state, entry, **common),
        )
    if category is TaskCategory.NOTIFICATION:
        return _from_executor(
            run_notifications_task(controller, context.capture, context.corpus, context.state, entry, **common),
        )
    raise ValueError(f'{category.value!r} has no executor')  # pragma: no cover - guarded above


def _dispatch_with_retry(
    context: RunContext,
    controller: Controller,
    entry: RosterEntry,
) -> EngineResult:
    """Dispatch one row, spending the task retry only when no action was taken.

    A transport failure is not retried here: the capture seam already spent the
    stage retry. A result that records a tap, key or page press is never re-run,
    because the first attempt may already have changed the account (FR-5's
    "never re-press a claimed card"). Only an unverified result with no action
    evidence is safe to repeat. A run-level class is re-raised so the top-level
    catch names it and stops the night.

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

    if authorized.key is not None and context.state.is_acted_unverified(authorized.key):
        readable, fresh = _recheck_acted_row(context, controller, authorized)
        if not readable:
            return EngineResult(
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
        if fresh is None:
            # Keep the marker: the row's control may still be present on a
            # partial/stale roster, so a later dispatch must re-check again
            # rather than tap without proof.
            return EngineResult(
                outcome='skipped',
                verification='already_done',
                detail=(
                    f'{authorized.title!r} acted without proof in an earlier run and the fresh surface '
                    'shows it already claimed; no tap was issued'
                ),
                completed=False,
                payload={'key': authorized.key, 'recheck': 'claimed'},
            )
        authorized = _authorize(context, fresh)
        log.info('%s', _event('acted_recheck', key=authorized.key, claimable=True))

    result = _attempt_dispatch(context, controller, authorized)
    # Persist the safety marker before the signed-out guard: if the session
    # expired after the task acted, the next run still needs the re-check.
    _remember_acted_result(context, authorized, result)
    if not result.completed:
        # A failed task is the first place a session that expired mid-walk is
        # visible. Detect it before spending the retry, so a signed-out app
        # stops the run instead of producing more task failures.
        _guard_signed_in(context, _capture(context.capture))
    named = result.failure
    if named is None or not named.retryable or named.retry is not RetryScope.TASK or result.acted:
        return result

    delay = retry_delay(0)
    log.warning(
        '%s',
        _event(
            'failure_retry',
            code=named.code.value,
            scope=named.retry.value,
            stage='task',
            key=entry.key,
            delay_seconds=delay,
        ),
    )
    context.sleep(delay)
    result = _attempt_dispatch(context, controller, authorized)
    _remember_acted_result(context, authorized, result)
    return result


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
    fresh = next(
        (
            candidate
            for candidate in enumerate_roster(opened.snapshot, context.catalogue, route=opened.route).entries
            if candidate.key == key
        ),
        None,
    )
    if fresh is not None:
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
    try:
        if terminal:
            context.state.clear_acted_unverified(key)
        elif result.acted:
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
    cap = context.config.max_tasks + 2 if max_observations is None else max_observations
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
        return observer.observe(
            opened.snapshot,
            route=opened.route if route is None else route,
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
) -> None:
    if roster is None:
        return
    if status is RunStatus.BUDGET:
        reason, why = 'run budget', detail or 'the run budget stopped the walk'
    elif status is RunStatus.STOPPED:
        reason, why = 'stopped', detail or 'a stop was requested'
    else:
        reason, why = 'not selected', detail or 'the roster was exhausted'
    for entry in roster.entries:
        if not entry.allowed or entry.spec is None:
            continue
        spec = entry.spec
        if spec.category not in _EXECUTORS:
            continue
        if _identity(entry) in attempted:
            continue
        if entry.dispatch_target is None:
            continue
        if state.count(spec.key) >= spec.max_per_run:
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
        _skip(
            report,
            logger,
            title=entry.title,
            key=spec.key,
            category=spec.category.value,
            reason=reason,
            detail=why,
        )


def _night_record(report: RunReport) -> NightRecord:
    """Decide what a finished roster walk leaves for the rest of the day (#97).

    A night is ``exhausted`` when nothing dispatched is worth retrying: every
    row completed, was skipped/refused (policy, daily limit, the app reporting
    it unavailable), or failed with a taxonomy entry that spends no retry.
    Terminal-versus-retryable comes from :class:`~aliexpress_coins.runner.failures.Failure`:
    ``TaskRecord.failure['retryable']`` is the taxonomy's own ``retries > 0``.
    A failed row with no named failure is treated as retryable because "unknown"
    must not consume the day.

    Any retryable failure keeps the day open even when another row completed:
    the retryable row is still unclaimed work, and the operator most needs the
    daemon to try it while asleep.
    """
    completed = 0
    retryable = 0
    for task in report.tasks:
        if task.completed:
            completed += 1
        elif _retryable_task_failure(task):
            retryable += 1
    roster_incomplete = bool(report.roster and report.roster.get('incomplete', False))
    if roster_incomplete:
        outcome = NightOutcome.INCOMPLETE_ROSTER
    elif retryable:
        outcome = NightOutcome.RETRYABLE_FAILURES
    else:
        outcome = NightOutcome.EXHAUSTED
    return NightRecord(
        outcome=outcome,
        dispatched=len(report.tasks),
        completed=completed,
        retryable_failures=retryable,
    )


def _retryable_task_failure(task: TaskRecord) -> bool:
    """Whether one dispatched row still has work worth another run.

    Only a ``failed`` outcome can be retryable. A skipped row, a refusal, an
    ``already_done``/``unavailable`` executor outcome and a completed row are
    terminal. A failed row is retryable when the taxonomy says its class spends
    a retry (``retries > 0``); an unnamed failure defaults to retryable.
    """
    if task.outcome != 'failed':
        return False
    if task.failure is None:
        return True
    return bool(task.failure.get('retryable', True))


def _next_entry(roster: Roster, state: DailyStateStore, blocked: set[str]) -> RosterEntry | None:
    for entry in roster.entries:
        if not entry.allowed or entry.spec is None or entry.dispatch_target is None:
            continue
        if entry.spec.category not in _EXECUTORS:
            continue
        if _identity(entry) in blocked:
            continue
        if state.count(entry.spec.key) >= entry.spec.max_per_run:
            continue
        return entry
    return None


def _popup_recorder(report: RunReport) -> Callable[[DismissReport], None]:
    def record(dismissal: DismissReport) -> None:
        report.record_popups(dismissal.closed)

    return record


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
