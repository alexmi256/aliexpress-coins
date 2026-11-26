"""Execute the ``Visit the page via coins shortcut`` task (#130).

The measured row sits below the first screenful of the boost list. Its ``GO``
does not open the coin page and does not pay immediately: it opens Android's
``Create shortcut`` sheet. In the supervised observation the +1 arrived only
after the created launcher shortcut was used, not on the first shortcut URI
fired after creation. The collector deliberately does not create or tap a
launcher item (#27), so this executor recognises the sheet, closes it, returns
to the measured coin page and records an honest unverified result rather than
inferring a claim from the row disappearing.

Completion would require a readable balance increase. A row that is absent
before any tap is a stale roster (``row_vanished``), not a completed task.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from aliexpress_coins.control.controller import Controller, TapRecord
from aliexpress_coins.control.popups import PopupCorpus
from aliexpress_coins.perception.matching import DEFAULT_MIN_OCR_SCORE, find_text
from aliexpress_coins.perception.scroll import (
    MAX_SCROLL_ATTEMPTS,
    STABLE_FRAME_MAX_DIFFERENCE,
    ScrollOutcome,
    ScrollResult,
    scroll_until_found,
)
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.state import CaptureSource
from aliexpress_coins.perception.templates import TemplateStore
from aliexpress_coins.runner.errors import RunInterrupted
from aliexpress_coins.runner.safety import RunAborted
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks.browse import find_task_row
from aliexpress_coins.tasks.catalogue import AllowedTaskSpec, TaskCategory
from aliexpress_coins.tasks.discovery import RosterEntry, open_boost_list, wait_for_coin_page
from aliexpress_coins.tasks.dispatch import authorization_of
from aliexpress_coins.tasks.errors import TaskError
from aliexpress_coins.tasks.signin import read_balance
from aliexpress_coins.tasks.surface import (
    RouteFirer,
    Surface,
    SurfaceRoute,
    classify_surface,
    contains_text,
    is_boost_page,
)

__all__ = [
    'DEFAULT_MODAL_TIMEOUT',
    'SHORTCUT_MODAL_MARKERS',
    'ShortcutVisitOutcome',
    'ShortcutVisitResult',
    'run_shortcut_visit_task',
]

DEFAULT_MODAL_TIMEOUT = 6.0
"""How long the shortcut sheet may take to appear after the row's ``GO``."""

DEFAULT_INTERVAL = 0.5
"""How often the post-tap frame is read."""

SHORTCUT_MODAL_MARKERS: tuple[str, ...] = (
    'Create shortcut',
    'Got it',
    "Once you've created a shortcut",
)
"""The measured Android shortcut-creation sheet opened by this row's ``GO``."""


class ShortcutVisitOutcome(StrEnum):
    """What one shortcut-row attempt established."""

    COMPLETED = 'completed'
    FAILED = 'failed'
    SKIPPED = 'skipped'


@dataclass(frozen=True, slots=True)
class ShortcutVisitResult:
    """The evidence behind one shortcut-row attempt."""

    key: str
    title: str
    outcome: ShortcutVisitOutcome
    verification: str
    detail: str
    limit: int
    done_today_before: int
    done_today_after: int
    balance_before: float | None = None
    balance_after: float | None = None
    modal_labels: tuple[str, ...] = ()
    taps: tuple[TapRecord, ...] = ()
    recorded: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, 'modal_labels', tuple(self.modal_labels))
        object.__setattr__(self, 'taps', tuple(self.taps))

    @property
    def completed(self) -> bool:
        """Whether a readable balance increase proved the claim and state was recorded."""
        return self.outcome is ShortcutVisitOutcome.COMPLETED

    def as_dict(self) -> dict[str, object]:
        """Render the attempt for ``run.json``."""
        return {
            'key': self.key,
            'title': self.title,
            'outcome': self.outcome.value,
            'verification': self.verification,
            'detail': self.detail,
            'limit': self.limit,
            'done_today_before': self.done_today_before,
            'done_today_after': self.done_today_after,
            'balance_before': self.balance_before,
            'balance_after': self.balance_after,
            'modal_labels': list(self.modal_labels),
            'taps': [tap.as_dict() for tap in self.taps],
            'recorded': self.recorded,
        }


def run_shortcut_visit_task(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    state: DailyStateStore,
    entry: RosterEntry,
    *,
    navigate: RouteFirer,
    snapshot: Snapshot | None = None,
    templates: TemplateStore | None = None,
    route: SurfaceRoute = SurfaceRoute.SHORTCUT,
    modal_timeout: float = DEFAULT_MODAL_TIMEOUT,
    interval: float = DEFAULT_INTERVAL,
    settle: float = 0.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    logger: logging.Logger | None = None,
) -> ShortcutVisitResult:
    """Tap the shortcut row's ``GO`` and verify a credit, or name why not.

    ``corpus``, ``templates``, ``route`` and ``settle`` are accepted for the
    engine's common executor signature. The task itself never creates a launcher
    shortcut and never leaves the app through the observed sheet.
    """
    log = logger if logger is not None else logging.getLogger(__name__)
    spec = _allowed_spec(entry)
    limit = spec.max_per_run
    done_before = state.count(spec.key)
    if done_before >= limit:
        result = _result(
            spec,
            entry.title,
            ShortcutVisitOutcome.SKIPPED,
            verification='daily_limit',
            detail=f'{spec.key!r} already completed {done_before} time(s) today; the limit is {limit}',
            done_before=done_before,
        )
        _log_result(log, result)
        return result
    if modal_timeout <= 0:
        raise ValueError(f'modal_timeout must be positive, got {modal_timeout!r}')
    if interval <= 0:
        raise ValueError(f'interval must be positive, got {interval!r}')
    if settle < 0:
        raise ValueError(f'settle must not be negative, got {settle!r}')

    initial = snapshot if snapshot is not None else _capture(capture)
    balance_before = _trusted_balance(initial)
    restart_confirmed = False
    found = _read_row(controller, capture, entry.title, initial)
    if found.target is None and not _absence_confirmed(found):
        # A no-op swipe or a frame that was not a readable boost list is not
        # evidence the row is gone. Re-read once from a fresh frame before
        # spending the one route restart.
        found = _read_row(controller, capture, entry.title, _capture(capture))
    if found.target is None and not _absence_confirmed(found):
        # Still unconfirmed: restart through the measured route and re-open the
        # Coins boost sheet, then read it again. Do not reimplement the route.
        try:
            navigate(route)
        except RunAborted, RunInterrupted:
            raise
        except Exception as exc:
            log.warning('%s', _json({'event': 'shortcut_restart_failed', 'detail': f'{type(exc).__name__}: {exc}'}))
        else:
            restart_confirmed = True
        try:
            opened = open_boost_list(
                controller,
                capture,
                corpus,
                templates=templates,
                timeout=max(modal_timeout, 10.0),
                interval=interval,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=log,
            )
            if not opened.opened or opened.tap_attempts == 0:
                restart_confirmed = False
            found = _read_row(controller, capture, entry.title, opened.snapshot)
        except RunAborted, RunInterrupted:
            raise
        except Exception as exc:
            result = _surface_failure(
                spec,
                entry.title,
                found,
                done_before=done_before,
                balance_before=balance_before,
                stage='one route restart',
                note=f'the restart failed: {type(exc).__name__}: {exc}',
            )
            _log_result(log, result)
            return result

    action = found.target
    if action is None:
        surface = classify_surface(found.last)
        if _absence_confirmed(found, settled=restart_confirmed):
            moved = sum(1 for attempt in found.attempts if attempt.difference > STABLE_FRAME_MAX_DIFFERENCE)
            result = _result(
                spec,
                entry.title,
                ShortcutVisitOutcome.SKIPPED,
                verification='row_vanished',
                detail=(
                    f'{entry.title!r} is not on the scrolled boost list: '
                    f'{moved} of {len(found.attempts)} swipe(s) moved the {surface.value} content and the row was '
                    'still absent; no tap was issued'
                ),
                done_before=done_before,
                balance_before=balance_before,
            )
        else:
            result = _surface_failure(
                spec,
                entry.title,
                found,
                done_before=done_before,
                balance_before=balance_before,
                stage='a bounded re-read and one route restart',
            )
        _log_result(log, result)
        return result

    tap = controller.tap_box(action, why='open the shortcut row and read what its GO does', label=entry.title)
    taps = [tap]
    modal = _wait_for_modal(capture, timeout=modal_timeout, interval=interval, clock=clock, sleep=sleep)
    modal_labels: tuple[str, ...] = ()
    if modal is not None:
        modal_labels = tuple(box.text for box in modal.boxes if box.score >= 0.5 and box.text.strip())
        got_it = find_text(modal, 'Got it')
        if got_it is not None:
            taps.append(
                controller.tap_box(
                    got_it.box,
                    why='close the shortcut-creation sheet without creating a launcher item',
                    label='Got it',
                ),
            )

    try:
        navigate(route)
    except RunAborted, RunInterrupted:
        raise
    except Exception as exc:
        log.warning('%s', _json({'event': 'shortcut_return_failed', 'detail': f'{type(exc).__name__}: {exc}'}))
    arrival = wait_for_coin_page(
        capture,
        timeout=max(modal_timeout, 10.0),
        interval=interval,
        clock=clock,
        sleep=sleep,
    )
    balance_after = _trusted_balance(arrival.snapshot)
    if balance_before is not None and balance_after is not None and balance_after - balance_before == 1.0:
        done_after = state.record(spec.key, balance=balance_after)
        result = _result(
            spec,
            entry.title,
            ShortcutVisitOutcome.COMPLETED,
            verification='balance_delta',
            detail=f"balance moved {balance_before:g} -> {balance_after:g} after the shortcut row's GO",
            done_before=done_before,
            done_after=done_after,
            balance_before=balance_before,
            balance_after=balance_after,
            modal_labels=modal_labels,
            taps=taps,
            recorded=True,
        )
        _log_result(log, result)
        return result

    if modal is not None:
        verification = 'shortcut_sheet_no_credit'
        detail = (
            f'{entry.title!r} opened the Android "Create shortcut" sheet; no +1 was observed '
            f'(balance {balance_before!r} -> {balance_after!r}). The collector does not create '
            'a launcher shortcut, so this row is left for the operator'
        )
    else:
        verification = 'no_credit'
        detail = f'{entry.title!r} was tapped but no +1 was observed (balance {balance_before!r} -> {balance_after!r})'
    result = _result(
        spec,
        entry.title,
        ShortcutVisitOutcome.FAILED,
        verification=verification,
        detail=detail,
        done_before=done_before,
        balance_before=balance_before,
        balance_after=balance_after,
        modal_labels=modal_labels,
        taps=taps,
    )
    _log_result(log, result)
    return result


def _allowed_spec(entry: RosterEntry) -> AllowedTaskSpec:
    spec = entry.spec
    if not entry.allowed or spec is None or entry.dispatch_target is None:
        reason = entry.reason or 'no allowed action is exposed'
        raise TaskError(f'{entry.title!r} is not dispatchable: {reason}')
    if spec.category is not TaskCategory.SHORTCUT_VISIT:
        raise TaskError(f'{entry.title!r} is {spec.category.value!r}, not a shortcut_visit task')
    authorization_of(entry)
    return spec


def _action_for(frame: Snapshot, title: str) -> TextBox | None:
    _, action, _ = find_task_row(frame, title)
    return action


def _read_row(
    controller: Controller,
    capture: CaptureSource,
    title: str,
    initial: Snapshot,
) -> ScrollResult[TextBox]:
    """Search one frame for the row's own ``GO`` and keep the scroll evidence."""
    return scroll_until_found(
        controller,
        lambda: _capture(capture),
        lambda frame: _action_for(frame, title),
        initial=initial,
        max_attempts=MAX_SCROLL_ATTEMPTS,
        why='find the shortcut-row GO below the boost fold',
    )


def _absence_confirmed(found: ScrollResult[TextBox], *, settled: bool = False) -> bool:
    """Whether a read proves the row is absent rather than unreadable.

    Absence is only reportable from a positively classified boost list whose
    swipes actually moved the content at least once. A read that never moved
    proves nothing, and a frame that is not a boost list says nothing about the
    boost row.

    ``settled`` accepts a list that moved and then stopped on the same frame
    (``STABLE`` at the end of the list). It is only passed after a successful
    route restart that actually tapped ``Earn more`` to re-open the boost list:
    an earlier or unconfirmed read can start below the row and miss it above, so
    reaching the bottom is not proof until the sheet was re-opened.
    """
    if found.target is not None or classify_surface(found.last) is not Surface.BOOST_PAGE:
        return False
    attempts = found.attempts
    if not attempts or not any(attempt.difference > STABLE_FRAME_MAX_DIFFERENCE for attempt in attempts):
        return False
    if found.outcome is ScrollOutcome.ATTEMPTS:
        return True
    return settled and found.outcome is ScrollOutcome.STABLE


def _surface_failure(
    spec: AllowedTaskSpec,
    title: str,
    found: ScrollResult[TextBox],
    *,
    done_before: int,
    balance_before: float | None,
    stage: str,
    note: str | None = None,
) -> ShortcutVisitResult:
    """Name what the unreadable frame showed instead of asserting the row is gone."""
    frame = found.last
    surface = classify_surface(frame)
    on_boost_page = is_boost_page(frame)
    verification = 'surface_incomplete' if on_boost_page else 'surface_lost'
    detail = (
        f'{title!r} could not be confirmed on the Coins boost list after {stage}: '
        f'surface={surface.value}, boost_header={on_boost_page}, scroll={found.outcome.value}, '
        f'attempts={len(found.attempts)}, labels={_label_count(frame)}; no tap was issued'
    )
    if note is not None:
        detail = f'{detail}; {note}'
    return _result(
        spec,
        title,
        ShortcutVisitOutcome.FAILED,
        verification=verification,
        detail=detail,
        done_before=done_before,
        balance_before=balance_before,
    )


def _trusted_balance(frame: Snapshot) -> float | None:
    """Keep a balance only when the frame is positively the coin page."""
    reading = read_balance(frame)
    return reading.value if reading.trusted else None


def _label_count(frame: Snapshot) -> int:
    """How many readable OCR labels the frame actually carried."""
    return sum(1 for box in frame.boxes if box.score >= DEFAULT_MIN_OCR_SCORE and box.text.strip())


def _wait_for_modal(
    capture: CaptureSource,
    *,
    timeout: float,
    interval: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> Snapshot | None:
    started = clock()
    deadline = started + timeout
    while True:
        frame = _capture(capture)
        if any(contains_text(frame, marker) for marker in SHORTCUT_MODAL_MARKERS):
            return frame
        if clock() >= deadline:
            return None
        sleep(min(interval, max(0.0, deadline - clock())))


def _result(
    spec: AllowedTaskSpec,
    title: str,
    outcome: ShortcutVisitOutcome,
    *,
    verification: str,
    detail: str,
    done_before: int,
    done_after: int | None = None,
    balance_before: float | None = None,
    balance_after: float | None = None,
    modal_labels: tuple[str, ...] = (),
    taps: list[TapRecord] | tuple[TapRecord, ...] = (),
    recorded: bool = False,
) -> ShortcutVisitResult:
    return ShortcutVisitResult(
        key=spec.key,
        title=title,
        outcome=outcome,
        verification=verification,
        detail=detail,
        limit=spec.max_per_run,
        done_today_before=done_before,
        done_today_after=done_before if done_after is None else done_after,
        balance_before=balance_before,
        balance_after=balance_after,
        modal_labels=modal_labels,
        taps=tuple(taps),
        recorded=recorded,
    )


def _log_result(logger: logging.Logger, result: ShortcutVisitResult) -> None:
    logger.info('%s', _json({'event': 'shortcut_visit_task', **result.as_dict()}))


def _json(fields: dict[str, object]) -> str:
    return json.dumps(fields, separators=(',', ':'), sort_keys=True, default=str)


def _capture(source: CaptureSource) -> Snapshot:
    return source() if callable(source) else source.capture()
