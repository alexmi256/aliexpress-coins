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
from aliexpress_coins.perception.matching import find_text
from aliexpress_coins.perception.scroll import MAX_SCROLL_ATTEMPTS, scroll_until_found
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.state import CaptureSource
from aliexpress_coins.runner.errors import RunInterrupted
from aliexpress_coins.runner.safety import RunAborted
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks.browse import find_task_row
from aliexpress_coins.tasks.catalogue import AllowedTaskSpec, TaskCategory
from aliexpress_coins.tasks.discovery import RosterEntry, wait_for_coin_page
from aliexpress_coins.tasks.dispatch import authorization_of
from aliexpress_coins.tasks.errors import TaskError
from aliexpress_coins.tasks.signin import read_coin_balance
from aliexpress_coins.tasks.surface import RouteFirer, SurfaceRoute, contains_text

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
    templates: object | None = None,
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
    del corpus, templates
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
    balance_before = read_coin_balance(initial)
    found = scroll_until_found(
        controller,
        lambda: _capture(capture),
        lambda frame: _action_for(frame, entry.title),
        initial=initial,
        max_attempts=MAX_SCROLL_ATTEMPTS,
        why='find the shortcut-row GO below the boost fold',
    )
    action = found.target
    if action is None:
        result = _result(
            spec,
            entry.title,
            ShortcutVisitOutcome.SKIPPED,
            verification='row_vanished',
            detail=f'{entry.title!r} is not on the scrolled boost list; no tap was issued',
            done_before=done_before,
            balance_before=balance_before,
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
    balance_after = read_coin_balance(arrival.snapshot)
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
