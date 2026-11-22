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
from aliexpress_coins.perception.errors import StateTimeout
from aliexpress_coins.perception.ocr import RapidOcrEngine
from aliexpress_coins.perception.snapshot import Snapshot
from aliexpress_coins.perception.state import CaptureSource
from aliexpress_coins.perception.templates import TemplateStore, load_templates
from aliexpress_coins.runner.config import RunConfig
from aliexpress_coins.runner.device import AdbDevice, HygieneDevice
from aliexpress_coins.runner.errors import ConfigError, DeviceError, RunInterrupted
from aliexpress_coins.runner.notify import Notification, NotificationSink, build_notifier
from aliexpress_coins.runner.report import RunEventLog, RunReport, RunStatus, prune_runs
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks.browse import return_to_coins_page, run_browse_task
from aliexpress_coins.tasks.catalogue import AllowedTaskSpec, TaskCatalogue, TaskCategory, load_task_catalogue
from aliexpress_coins.tasks.discovery import (
    DEFAULT_BOOST_INTERVAL,
    DEFAULT_BOOST_TIMEOUT,
    DiscoveryRoute,
    Roster,
    RosterEntry,
    RosterLog,
    RosterObservation,
    RosterObserver,
    discover_roster,
    enumerate_roster,
    open_boost_list,
)
from aliexpress_coins.tasks.notifications import run_notifications_task
from aliexpress_coins.tasks.quiz import run_quiz_task
from aliexpress_coins.tasks.signin import claim_daily_signins, read_coin_balance
from aliexpress_coins.tasks.surface import (
    COIN_PAGE_MARKERS,
    RouteFirer,
    SurfaceRoute,
    classify_surface,
    contains_text,
    is_boost_page,
)
from aliexpress_coins.tasks.surprise import run_surprise_task

__all__ = [
    'DISPATCH_TABLE',
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

    def as_dict(self) -> dict[str, Any]:
        """Render the normalised result for the report."""
        return {
            'outcome': self.outcome,
            'verification': self.verification,
            'detail': self.detail,
            'completed': self.completed,
            'payload': self.payload,
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


_EXECUTORS: frozenset[TaskCategory] = frozenset(
    {
        TaskCategory.SIGN_IN,
        TaskCategory.QUIZ,
        TaskCategory.BROWSE_WAIT,
        TaskCategory.ITEM_TAP,
        TaskCategory.NOTIFICATION,
    },
)

DISPATCH_TABLE: dict[TaskCategory, str] = {
    TaskCategory.SIGN_IN: 'sign-in',
    TaskCategory.QUIZ: 'quiz',
    TaskCategory.BROWSE_WAIT: 'browse-and-wait',
    TaskCategory.ITEM_TAP: 'surprise-items',
    TaskCategory.NOTIFICATION: 'notifications',
}
"""The catalogue categories this engine can dispatch, and their executor names.

``shortcut`` and ``widget`` are deliberately absent: their executors belong to
#27 and a row in those categories is reported as skipped rather than guessed at.
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
    capture = _guarded_call(snapshotter.capture, stop)
    device = AdbDevice(
        config.device,
        app_settle_seconds=config.app_settle_seconds,
        settle=sleep,
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
    is a run-level failure.
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
    artifacts = ArtifactStore(report.runs_dir, report.run_id)
    record_dismissal = _popup_recorder(report)
    event_log = RunEventLog(report.event_log_path)
    log.addHandler(event_log)
    try:
        event_log.record(
            {'event': 'run_started', 'run_id': report.run_id, 'started_at': report.started_at.isoformat()},
        )
    except Exception as exc:
        log.warning('%s', _event('run_log_failed', detail=f'{type(exc).__name__}: {exc}'))

    try:
        report.write()
        hygiene = context.hygiene.prepare()
        report.hygiene = hygiene.as_dict()
        frame = _capture(context.capture)
        report.geometry = _geometry(frame)
        _require_supported_geometry(frame)
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
        report.roster = roster.as_dict()
        report.surface = roster.surface_variant.value if roster.surface_variant is not None else None
        report.write()
        context.state.begin_run(at=context.wall_clock())
        observer = (
            observer if observer is not None else _observer(context, report, roster, max_observations=task_limit + 2)
        )
        _observe(context, controller, observer, route=roster.route, on_dismiss=record_dismissal)
        _record_policy_skips(report, roster)
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
                break
            if len(report.tasks) >= task_limit:
                status = RunStatus.BUDGET
                detail = f'task budget of {task_limit} dispatch(es) exhausted'
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
            try:
                result = _dispatch_entry(context, controller, entry)
            except RunInterrupted:
                raise
            except Exception as exc:
                result = EngineResult(
                    outcome='failed',
                    verification='error',
                    detail=f'{type(exc).__name__}: {exc}',
                    completed=False,
                    payload={'error': f'{type(exc).__name__}: {exc}'},
                )
            duration = max(0.0, context.clock() - task_started)
            attempted.add(identity)
            _record_dismissals(report, result.as_dict())
            report.record_task(
                title=entry.title,
                key=spec.key,
                category=spec.category.value,
                outcome=result.outcome,
                verification=result.verification,
                detail=result.detail,
                duration_seconds=duration,
                completed=result.completed,
                result=result.as_dict(),
            )
            log.info(
                '%s',
                _event(
                    'task_outcome',
                    key=spec.key,
                    title=entry.title,
                    outcome=result.outcome,
                    verification=result.verification,
                    completed=result.completed,
                    seconds=round(duration, 3),
                ),
            )
            if result.completed:
                _observe(context, controller, observer, route=None, on_dismiss=record_dismissal)
                _record_policy_skips(report, observer.roster or roster)
                report.observations = observer.sequence
            else:
                blocked.add(identity)
            report.write()

        final_roster = observer.roster if observer is not None and observer.roster is not None else roster
        _record_unrun(report, final_roster, context.state, attempted, status=status, detail=detail)
        if observer is not None:
            report.observations = observer.sequence
        if final_roster is not None:
            report.roster = final_roster.as_dict()
            report.surface = (
                final_roster.surface_variant.value if final_roster.surface_variant is not None else report.surface
            )
    except RunInterrupted as exc:
        status = RunStatus.STOPPED
        detail = f'stop requested: {exc}'
        log.warning('%s', _event('run_stopped', detail=detail))
    except Exception as exc:
        status = RunStatus.FAILED
        detail = f'{type(exc).__name__}: {exc}'
        report.error = detail
        log.exception('%s', _event('run_failed', detail=detail))
    finally:
        if controller is not None:
            _finish_on_coins_page(
                context,
                controller,
                report,
                artifacts,
                log,
                on_dismiss=record_dismissal,
            )
        if status is RunStatus.FAILED:
            _record_failure_frame(context, report, artifacts, log)
        if status is RunStatus.COMPLETED and not context.config.dry_run:
            try:
                context.state.complete_night()
            except Exception as exc:
                log.warning('%s', _event('state_complete_failed', detail=f'{type(exc).__name__}: {exc}'))
        finalized = False
        try:
            used = max(0.0, context.clock() - started)
            report.finish(status, detail=detail, budget_used_seconds=used, finished_at=context.wall_clock())
            report.write()
            finalized = True
            log.info(
                '%s',
                _event(
                    'run_finished',
                    run_id=report.run_id,
                    status=report.status.value,
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
    except (StateTimeout, PopupUnclosable) as exc:
        detail = f'{type(exc).__name__}: {exc}'
        log.warning('%s', _event('route_fallback', detail=detail))
        frame = _capture(context.capture)
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
        return _run_signin(context, controller, spec, common)
    if category is TaskCategory.QUIZ:
        return _from_executor(
            run_quiz_task(controller, context.capture, context.corpus, context.state, entry, **common)
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
    raise ValueError(f'{category.value!r} has no executor')


def _run_signin(
    context: RunContext,
    controller: Controller,
    spec: AllowedTaskSpec,
    common: dict[str, Any],
) -> EngineResult:
    """Run both independent daily claims from the coin page.

    A roster can be the boost list, where the streak card is not visible, so the
    sign-in executor is given the measured shortcut route first. It then claims
    the streak and the boost row itself, using its own state checks for each.
    """
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
        snapshot=None,
        templates=common['templates'],
        settle=common['settle'],
        clock=common['clock'],
        sleep=common['sleep'],
        logger=common['logger'],
    )
    claims = result.results
    completed = bool(result.completed)
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
    )


def _from_executor(result: ExecutorResult) -> EngineResult:
    return EngineResult(
        outcome=result.outcome.value,
        verification=result.verification,
        detail=result.detail,
        completed=result.completed,
        payload=result.as_dict(),
    )


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
        return observer.observe(
            opened.snapshot,
            route=opened.route if route is None else route,
            timestamp=context.wall_clock(),
        )
    except RunInterrupted:
        raise
    except Exception as exc:
        log.warning('%s', _event('roster_observe_failed', detail=f'{type(exc).__name__}: {exc}'))
        try:
            return observer.observe(_capture(context.capture), route=route, timestamp=context.wall_clock())
        except RunInterrupted:
            raise
        except Exception as inner:
            log.warning('%s', _event('roster_observe_failed', detail=f'{type(inner).__name__}: {inner}'))
            return None


def _record_policy_skips(report: RunReport, roster: Roster | None) -> None:
    if roster is None:
        return
    for entry in roster.entries:
        if entry.denied:
            report.skip(
                entry.title,
                key=None,
                category=None,
                reason='policy',
                detail=entry.reason or 'refused by the default-deny policy',
            )
        elif entry.spec is not None and entry.spec.category not in _EXECUTORS:
            report.skip(
                entry.title,
                key=entry.key,
                category=entry.spec.category.value,
                reason='no executor',
                detail=f'{entry.spec.category.value!r} has no executor in this issue',
            )
        elif entry.dispatch_target is None:
            report.skip(
                entry.title,
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
            report.skip(
                entry.title,
                key=spec.key,
                category=spec.category.value,
                reason='daily limit',
                detail=f'{spec.key!r} already reached its {spec.max_per_run} completion(s) today',
            )
            continue
        report.skip(
            entry.title,
            key=spec.key,
            category=spec.category.value,
            reason=reason,
            detail=why,
        )


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
    except RunInterrupted:
        raise
    except Exception as exc:
        report.balance_note = f'coins page not proven before the first task: {type(exc).__name__}: {exc}'
        log.warning('%s', _event('balance_before_unavailable', detail=report.balance_note))
        return
    if not result.returned or result.snapshot is None or not _on_coin_page(result.snapshot):
        report.balance_note = 'coins page not proven before the first task; balance before is unknown'
        log.warning('%s', _event('balance_before_unavailable', detail=report.balance_note))
        return
    recorded = _record_frame(report, artifacts, result.snapshot, 'balance-before', log)
    report.balance_before = read_coin_balance(recorded)
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
) -> None:
    """Best-effort final frame for a failed run; never mask the original error."""
    if 'failure' in report.shots:
        return
    try:
        frame = _capture(context.capture)
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


def _should_notify(report: RunReport, *, notify_on_empty: bool) -> bool:
    if not report.ok:
        return True
    return bool(notify_on_empty and not any(task.completed for task in report.tasks))


def _notification(report: RunReport) -> Notification:
    lines = [f'status: {report.status.value}']
    if report.error:
        lines.append(f'error: {report.error}')
    elif report.detail:
        lines.append(f'detail: {report.detail}')
    before = 'unknown' if report.balance_before is None else f'{report.balance_before:g}'
    after = 'unknown' if report.balance_after is None else f'{report.balance_after:g}'
    lines.append(f'balance: {before} -> {after}')
    completed = sum(1 for task in report.tasks if task.completed)
    lines.append(f'tasks: {completed}/{len(report.tasks)} completed')
    lines.append(f'report: {report.summary_path}')
    return Notification(
        title=f'aliexpress-coins {report.status.value} {report.run_id}',
        body='\n'.join(lines),
        run_id=report.run_id,
        status=report.status.value,
        tags=('coins', report.status.value),
    )


def _delivery_error(exc: Exception) -> str:
    """Name a delivery failure without recording its untrusted message.

    A target URL is a secret when it carries userinfo, and a custom sink can
    raise an exception whose text includes it. The class plus an HTTP status is
    enough to diagnose delivery without ever copying that text into artifacts.
    """
    status = getattr(exc, 'code', None)
    suffix = f' (HTTP {status})' if isinstance(status, int) else ''
    return f'{type(exc).__name__}{suffix}'


def _notify(context: RunContext, report: RunReport, log: logging.Logger) -> None:
    """Deliver one notification after the run's own outcome is already written."""
    notifier = context.notifier
    if notifier is None or not _should_notify(report, notify_on_empty=context.config.notify_on_empty):
        return
    name = getattr(notifier, 'name', type(notifier).__name__)
    record: dict[str, Any] = {'attempted': True, 'ok': False, 'sink': name, 'error': None}
    try:
        notifier.send(_notification(report))
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


def _require_supported_geometry(frame: Snapshot) -> None:
    """Refuse a frame the measured coordinate and template tables do not fit.

    The brief's FR-14 names an unexpected screen geometry as a refusal, not a
    guess. #42 and #20 extend this with real device profiles; until then the
    measured tablet profile is the only one this run may touch.
    """
    geometry = frame.geometry
    if (geometry.width, geometry.height, geometry.density) != (1200, 2000, 240):
        raise DeviceError(
            f'unsupported screen geometry: observed {geometry.width}x{geometry.height} '
            f'@ {geometry.density} dpi (profile {geometry.profile_id}); '
            'this build supports 1200x2000 @ 240 dpi',
        )


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
