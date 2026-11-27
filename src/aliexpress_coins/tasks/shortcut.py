"""Execute the ``Visit the page via coins shortcut`` task (#130, #158).

The measured row sits below the first screenful of the boost list. Its ``GO``
does not open the coin page and does not pay immediately: it opens Android's
``Create shortcut`` sheet. A human completes the row by accepting that sheet,
confirming the launcher's ``Add to home screen`` step, using the created shortcut
from the home screen and returning to the coins page. The first shortcut URI
fired after creation did not credit, while a home-screen launch followed by a
coin-page reload moved the balance by +1 (2026-11-24, issue #130).

This executor drives that measured path explicitly:

* The live dispatch reads the balance from the coin page before touching the
  row, then re-opens the boost list and re-resolves the row. The boost list's
  own balance band is not claim evidence, so it is not used as the "before".
* ``GO`` opens the app's shortcut sheet. The executor finds and taps its
  ``Create shortcut`` control; it never blind-taps the sheet.
* The launcher's ``Add to home screen`` control is accepted only after the
  shortcut sheet asked for it. A ``Shortcut has existed`` toast means an earlier
  run left the item behind; that item is reused instead of creating a duplicate.
* The executor uses the launcher item. The live path fires that item's own
  persisted intent (``SHORTCUT_LAUNCH_DEEP_LINK``, with ``from=shortcut`` and
  ``shortcutId=coinsPromotion``); without that seam it goes to the home screen,
  proves the ``Coins`` shortcut label is there beside the ``AliExpress`` label,
  and taps it.
* The coin page is reloaded once through the measured shortcut route so the
  server-side +1 can be read. Completion needs a readable balance increase; a
  launcher frame after the dispatch is a foreground loss, never a completion.
* The launcher item is removed afterwards through the injected cleanup seam
  (the live device runs ``adb shell cmd shortcut clear-shortcuts <package>``).

A row that is absent before any tap is a stale roster (``row_vanished``), not a
completed task.
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
    frame_difference,
    scroll_until_found,
)
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.state import CaptureSource, ScreenState, classify
from aliexpress_coins.perception.templates import TemplateStore
from aliexpress_coins.perception.waits import wait_until_stable
from aliexpress_coins.runner.errors import RunInterrupted
from aliexpress_coins.runner.safety import RunAborted
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks.browse import find_task_row
from aliexpress_coins.tasks.catalogue import AllowedTaskSpec, TaskCategory
from aliexpress_coins.tasks.discovery import CoinPageWait, RosterEntry, open_boost_list, wait_for_coin_page
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
"""How long the shortcut sheet or one launcher transition may take."""

DEFAULT_LAUNCH_TIMEOUT = 20.0
"""How long the home shortcut and the coin-page reload may take.

The measured home launch renders the coin-page skeleton for several seconds.
The bound is a ceiling, not a sleep: a rendered page ends the wait immediately.
"""

DEFAULT_INTERVAL = 0.5
"""How often the post-tap frame is read."""

DEFAULT_ROW_SETTLE_TIMEOUT = 6.0
"""How long the boost list may keep moving after a search swipe.

The run `20261127T140626Z` tapped a row that was still in the swipe's fling:
the frame that named the row was 1.4 s after the gesture, the next frame had
moved the row above the fold, and the tap opened nothing. A row is only a
target once the list has stopped moving.
"""

ROW_SCROLL_FRACTION = 0.4
"""A gentler search swipe than the roster's one-viewport gesture.

A full-height swipe carries enough momentum to overshoot the shortcut row and
hide it above the fold; 0.4 of the frame moves far enough to reveal the next
card without depending on the fling.
"""

ROW_SWIPE_DURATION_MS = 700
"""A slower search swipe keeps the list under control between captures."""

DEFAULT_BALANCE_GRACE = 6.0
"""How long the launched coin page may take to show the server-side +1."""

SHORTCUT_MODAL_MARKERS: tuple[str, ...] = (
    'Create shortcut',
    'Got it',
    "Once you've created a shortcut",
)
"""The measured Android shortcut-creation sheet opened by this row's ``GO``."""

CREATE_SHORTCUT_LABEL = 'Create shortcut'
"""The sheet's explicit accept control; ``Got it`` only closes the sheet."""

SHORTCUT_EXISTS_MARKERS: tuple[str, ...] = (
    'Shortcut has existed',
    'Shortcut already exists',
    'Shortcut exists',
)
"""The measured toast when a previous run left the launcher item behind."""

ADD_TO_HOME_LABEL = 'Add to home screen'
"""The launcher's explicit confirmation control for the shortcut sheet."""

GOT_IT_LABEL = 'Got it'
"""The app sheet's close control after the launcher item is ready."""

LAUNCHER_SHORTCUT_LABEL = 'Coins'
"""The created shortcut's label on the home screen."""

LAUNCHER_APP_LABEL = 'AliExpress'
"""The app's launcher label; it proves the frame is the home screen, not the app."""

MANUAL_SHORTCUT_CLEANUP = 'adb shell cmd shortcut clear-shortcuts com.alibaba.aliexpresshd'
"""The manual cleanup command reported when a pin could not be confirmed."""

LAUNCHER_PAGE_LABELS: tuple[str, ...] = (LAUNCHER_APP_LABEL, 'Settings')
"""Other home-screen labels accepted beside ``Coins`` as launcher evidence."""

APP_PAGE_MARKERS: tuple[str, ...] = (
    'Coins boost',
    'Daily coins',
    'Coins credits to redeem',
    'Coins credits to use',
)
"""App-page markers that keep an app sheet from being mistaken for the launcher."""

_RETURN_COIN_SURFACES = frozenset({Surface.ROWS_PRESENT, Surface.ROWS_ABSENT, Surface.BOOST_PAGE})
"""App surfaces that are still on the coins page after a shortcut dispatch."""


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
    shortcut_created: bool = False
    shortcut_removed: bool = False

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
            'shortcut_created': self.shortcut_created,
            'shortcut_removed': self.shortcut_removed,
        }


@dataclass(frozen=True, slots=True)
class _ShortcutUse:
    """The outcome of creating, launching and reloading through the shortcut."""

    created: bool
    used: bool
    failure: str | None
    detail: str
    labels: tuple[str, ...]
    taps: tuple[TapRecord, ...]
    balance_after: float | None
    final_surface: Surface
    pin_unconfirmed: bool = False


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
    shortcut_present: Callable[[], bool | None] | None = None,
    launch_shortcut: Callable[[], None] | None = None,
    remove_shortcut: Callable[[], object] | None = None,
    app_foreground: Callable[[], bool | None] | None = None,
) -> ShortcutVisitResult:
    """Tap the shortcut row's ``GO``, create/use its launcher item and verify +1.

    ``corpus``, ``templates``, ``route`` and ``settle`` are accepted for the
    engine's common executor signature. ``shortcut_present`` and
    ``launch_shortcut`` are the live launcher seams: the first reads whether the
    pinned item already exists, the second fires that item's own persisted
    intent (the measured launcher deep link). ``remove_shortcut`` removes the
    launcher item this row created (or the existing one it reused) before the
    result is returned.
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
    found = _read_row(
        controller,
        capture,
        entry.title,
        initial,
        clock=clock,
        sleep=sleep,
        settle=settle,
        interval=interval,
    )
    if found.target is None and not _absence_confirmed(found):
        # A no-op swipe or a frame that was not a readable boost list is not
        # evidence the row is gone. Re-read once from a fresh frame before
        # spending the one route restart.
        found = _read_row(
            controller,
            capture,
            entry.title,
            _capture(capture),
            clock=clock,
            sleep=sleep,
            settle=settle,
            interval=interval,
        )
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
            found = _read_row(
                controller,
                capture,
                entry.title,
                opened.snapshot,
                clock=clock,
                sleep=sleep,
                settle=settle,
                interval=interval,
            )
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

    if balance_before is None:
        # The live dispatch starts on the boost list, whose balance band is not
        # trusted for a claim (a lone control numeral can look like a balance).
        # Read the balance from a *freshly loaded* coin page first, then re-open
        # the list and re-resolve the row so the +1 is bracketed on the trusted
        # surface. A stale pre-route boost frame is not a coin page.
        balance_before, found, balance_confirmed, pre_read = _read_balance_and_row(
            controller,
            capture,
            corpus,
            navigate,
            route,
            entry.title,
            before_route=found.last,
            templates=templates,
            modal_timeout=modal_timeout,
            interval=interval,
            settle=settle,
            clock=clock,
            sleep=sleep,
            log=log,
        )
        restart_confirmed = restart_confirmed or balance_confirmed
        action = found.target
        if balance_before is None:
            result = _result(
                spec,
                entry.title,
                ShortcutVisitOutcome.FAILED,
                verification='balance_unreadable_before',
                detail=(
                    'the coin balance could not be read from a trusted coin page before the dispatch '
                    f'(surface={classify_surface(pre_read.snapshot).value}, '
                    f'settled={pre_read.settled}, history={_history(pre_read)}); no tap was issued'
                ),
                done_before=done_before,
            )
            _log_result(log, result)
            return result
        if action is None:
            result = _surface_failure(
                spec,
                entry.title,
                found,
                done_before=done_before,
                balance_before=balance_before,
                stage='the trusted balance pre-read',
                note='the row was not re-resolved after re-opening the boost list',
            )
            _log_result(log, result)
            return result

    action, found = _settled_action(
        controller,
        capture,
        entry.title,
        found,
        settle=settle,
        interval=interval,
        clock=clock,
        sleep=sleep,
    )
    if action is None:
        result = _result(
            spec,
            entry.title,
            ShortcutVisitOutcome.FAILED,
            verification='row_not_settled',
            detail=(
                f'{entry.title!r} could not be re-read on a settled frame before the tap '
                f'(surface={classify_surface(found.last).value}); no tap was issued'
            ),
            done_before=done_before,
            balance_before=balance_before,
        )
        _log_result(log, result)
        return result

    tap = controller.tap_box(action, why='open the shortcut row and read what its GO does', label=entry.title)
    taps = [tap]
    modal = _wait_for_modal(capture, timeout=modal_timeout, interval=interval, clock=clock, sleep=sleep)
    labels: list[str] = []
    if modal is not None:
        _remember_labels(labels, modal)
        use = _dispatch_launcher_shortcut(
            controller,
            capture,
            navigate,
            route,
            modal,
            labels=labels,
            modal_timeout=modal_timeout,
            interval=interval,
            clock=clock,
            sleep=sleep,
            log=log,
            balance_before=balance_before,
            preexisting=_read_shortcut_presence(shortcut_present, log),
            presence=shortcut_present,
            launch=launch_shortcut,
            foreground=app_foreground,
        )
        taps.extend(use.taps)
        removed = _remove_shortcut(remove_shortcut, log) if use.created and remove_shortcut is not None else False
        balance_after = use.balance_after
        final_surface = use.final_surface
        if (
            use.failure is None
            and _bright_increase(balance_before, balance_after)
            and final_surface is not Surface.NON_APP
        ):
            done_after = state.record(spec.key, balance=balance_after)
            used_phrase = (
                'the created shortcut'
                if use.created
                else ('the shortcut intent' if use.pin_unconfirmed else 'the reused shortcut')
            )
            detail = (
                f'balance moved {balance_before:g} -> {balance_after:g} after {used_phrase} was used'
                if balance_before is not None and balance_after is not None
                else f'balance moved by +1 after {used_phrase} was used'
            )
            if use.pin_unconfirmed:
                detail = (
                    f'{detail}; the Coins pin could not be confirmed, so a shortcut may remain on the device; '
                    f'remove it manually with `{MANUAL_SHORTCUT_CLEANUP}`'
                )
            elif use.created and not removed and remove_shortcut is not None:
                detail = f'{detail}; the app-level shortcut cleanup (all AliExpress shortcuts) could not run'
            result = _result(
                spec,
                entry.title,
                ShortcutVisitOutcome.COMPLETED,
                verification='balance_delta',
                detail=detail,
                done_before=done_before,
                done_after=done_after,
                balance_before=balance_before,
                balance_after=balance_after,
                modal_labels=tuple(labels),
                taps=taps,
                recorded=True,
                shortcut_created=use.created,
                shortcut_removed=removed,
            )
            _log_result(log, result)
            return result

        verification = use.failure or (
            'foreground_lost_after_action' if final_surface is Surface.NON_APP else 'shortcut_sheet_no_credit'
        )
        if use.used and final_surface not in _RETURN_COIN_SURFACES:
            # A dispatch that finished on a known non-coin app surface is not a
            # foreground loss; an UNKNOWN launcher frame is handled as one by
            # the helper, because it can never settle as a coin page.
            verification = (
                'foreground_lost_after_action'
                if final_surface is Surface.NON_APP or final_surface is Surface.UNKNOWN
                else 'return_to_coins_page_failed'
            )
        detail = use.detail
        if use.failure is None:
            detail = (
                f'{detail}; no readable +1 was observed after using the shortcut '
                f'(balance {balance_before!r} -> {balance_after!r})'
            )
        if use.pin_unconfirmed:
            detail = (
                f'{detail}; the Coins pin could not be confirmed, so a shortcut may remain on the device; '
                f'remove it manually with `{MANUAL_SHORTCUT_CLEANUP}`'
            )
        elif use.created and not removed and remove_shortcut is not None:
            detail = f'{detail}; the app-level shortcut cleanup (all AliExpress shortcuts) could not run'
        result = _result(
            spec,
            entry.title,
            ShortcutVisitOutcome.FAILED,
            verification=verification,
            detail=detail,
            done_before=done_before,
            balance_before=balance_before,
            balance_after=balance_after,
            modal_labels=tuple(labels),
            taps=taps,
            shortcut_created=use.created,
            shortcut_removed=removed,
        )
        _log_result(log, result)
        return result

    # No shortcut sheet appeared. Keep the historical direct-credit path: some
    # builds may pay the row without exposing the dialog. A +1 is still the only
    # completion evidence, but the failure names the missing step (the measured
    # sheet) instead of collapsing into a generic "no +1".
    arrival = _return_to_coin_page(
        capture,
        navigate,
        route,
        timeout=max(modal_timeout, 10.0),
        interval=interval,
        clock=clock,
        sleep=sleep,
        log=log,
    )
    balance_after = _trusted_balance(arrival.snapshot)
    if _bright_increase(balance_before, balance_after):
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
            taps=taps,
            recorded=True,
        )
        _log_result(log, result)
        return result

    if balance_after is None:
        verification = 'balance_unreadable_after'
        detail = (
            f"{entry.title!r}'s GO did not open the measured shortcut sheet, and the balance could not be "
            f'read afterwards (surface={classify_surface(arrival.snapshot).value}, '
            f'settled={arrival.settled}, history={_history(arrival)})'
        )
    else:
        verification = 'shortcut_sheet_absent'
        detail = (
            f'{entry.title!r} was tapped but the measured Create shortcut sheet did not appear; '
            f'no pinned intent was fired (balance {balance_before!r} -> {balance_after!r})'
        )
    result = _result(
        spec,
        entry.title,
        ShortcutVisitOutcome.FAILED,
        verification=verification,
        detail=detail,
        done_before=done_before,
        balance_before=balance_before,
        balance_after=balance_after,
        taps=taps,
    )
    _log_result(log, result)
    return result


def _dispatch_launcher_shortcut(
    controller: Controller,
    capture: CaptureSource,
    navigate: RouteFirer,
    route: SurfaceRoute,
    modal: Snapshot,
    *,
    labels: list[str],
    modal_timeout: float,
    interval: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    log: logging.Logger,
    balance_before: float | None,
    preexisting: bool | None,
    presence: Callable[[], bool | None] | None,
    launch: Callable[[], None] | None,
    foreground: Callable[[], bool | None] | None,
) -> _ShortcutUse:
    """Accept the shortcut sheet, launch the launcher item and reload the page."""
    taps: list[TapRecord] = []
    created = False
    used = False
    pin_unconfirmed = False

    def failed(verification: str, detail: str) -> _ShortcutUse:
        closed = _close_shortcut_sheet(
            controller,
            capture,
            labels=labels,
        )
        if closed is not None:
            taps.append(closed)
        arrival = _return_to_coin_page(
            capture,
            navigate,
            route,
            timeout=max(modal_timeout, 10.0),
            interval=interval,
            clock=clock,
            sleep=sleep,
            log=log,
        )
        return _ShortcutUse(
            created=created,
            used=used,
            failure=verification,
            detail=detail,
            labels=tuple(labels),
            taps=tuple(taps),
            balance_after=_trusted_balance(arrival.snapshot),
            final_surface=classify_surface(arrival.snapshot),
            pin_unconfirmed=pin_unconfirmed,
        )

    create = find_text(modal, CREATE_SHORTCUT_LABEL)
    if create is None:
        return failed(
            'shortcut_sheet_no_create',
            "the Android shortcut sheet was shown but its explicit 'Create shortcut' control was not read; "
            'the sheet was closed without guessing at a control',
        )
    taps.append(
        controller.tap_box(
            create.box,
            why='accept the Android shortcut sheet by its explicit Create shortcut control',
            label=CREATE_SHORTCUT_LABEL,
        ),
    )

    stage, stage_frame = _wait_for_creation_stage(
        capture,
        # The measured path leaves a ~6 s gap between accepting the sheet and
        # the launcher's Add step; the bound must not race that transition.
        timeout=max(modal_timeout, 10.0),
        interval=interval,
        clock=clock,
        sleep=sleep,
        labels=labels,
    )
    if stage == 'add':
        add = find_text(stage_frame, ADD_TO_HOME_LABEL)
        if add is None:  # pragma: no cover - stage detection read this same frame
            return failed(
                'shortcut_creation_uncertain',
                'the launcher sheet appeared but its Add to home screen control could not be read',
            )
        taps.append(
            controller.tap_box(
                add.box,
                why='confirm adding the Coins shortcut to the home screen',
                label=ADD_TO_HOME_LABEL,
            ),
        )
        _wait_for_add_to_finish(
            capture,
            timeout=max(modal_timeout, 10.0),
            interval=interval,
            clock=clock,
            sleep=sleep,
            labels=labels,
        )
        confirmed = _confirm_created_pin(
            presence,
            timeout=max(modal_timeout, 10.0),
            interval=interval,
            clock=clock,
            sleep=sleep,
            log=log,
        )
        if confirmed is False:
            return failed(
                'shortcut_creation_uncertain',
                'Add to home screen disappeared, but the shortcut service never showed the Coins pin',
            )
        created = confirmed is True
        pin_unconfirmed = confirmed is None
    elif stage == 'exists':
        # A previous run left the item behind (the measured toast). Reuse that
        # one; creating a second item would leave two shortcuts on the tablet.
        pass
    elif stage is None and preexisting is True:
        pass
    elif stage is None and _read_shortcut_presence(presence, log) is True:
        # The launcher was slower than the read, but the pin appeared during
        # the window: this run created it and must still clean it up.
        created = preexisting is not True
    else:
        return failed(
            'shortcut_creation_uncertain',
            "Create shortcut was tapped, but neither the launcher's Add to home screen sheet nor the "
            '"Shortcut has existed" confirmation appeared',
        )

    closed = _close_shortcut_sheet(controller, capture, labels=labels)
    if closed is not None:
        taps.append(closed)

    before_launch = _capture(capture)
    if launch is not None:
        try:
            launch()
        except RunAborted, RunInterrupted:
            raise
        except Exception as exc:
            return failed(
                'shortcut_launch_failed',
                f'the created Coins shortcut intent could not be fired: {type(exc).__name__}: {exc}',
            )
        used = True
    else:
        controller.press_key('KEYCODE_HOME', why='open the home screen to use the created Coins shortcut')
        home = _wait_for_home_shortcut(
            capture,
            timeout=max(modal_timeout, 10.0),
            interval=interval,
            clock=clock,
            sleep=sleep,
            labels=labels,
        )
        if home is None:
            return failed(
                'launcher_shortcut_not_found',
                'the home screen never showed the created Coins shortcut beside the AliExpress label',
            )
        shortcut = _home_shortcut_box(home)
        if shortcut is None:  # pragma: no cover - _wait_for_home_shortcut already checks this
            return failed('launcher_shortcut_not_found', 'the Coins shortcut label disappeared from the home screen')

        taps.append(
            controller.tap_box(
                shortcut,
                why='launch the created Coins shortcut from the home screen',
                label='Coins shortcut',
            ),
        )
        used = True

    arrival = _wait_for_launched_page(
        capture,
        before_launch,
        timeout=max(modal_timeout, DEFAULT_LAUNCH_TIMEOUT),
        interval=interval,
        clock=clock,
        sleep=sleep,
    )
    launch_surface = classify_surface(arrival.snapshot)
    in_app = foreground() if foreground is not None else None
    if (
        launch_surface is Surface.NON_APP
        or in_app is False
        or (launch_surface is Surface.UNKNOWN and not arrival.settled)
    ):
        # Keep the round-1 classification: a launcher frame after the dispatch
        # is a foreground loss, never completion. Recover only to leave the app
        # clean; the report keeps the launch surface that proved the loss.
        recovered = _return_to_coin_page(
            capture,
            navigate,
            route,
            timeout=max(modal_timeout, DEFAULT_LAUNCH_TIMEOUT),
            interval=interval,
            clock=clock,
            sleep=sleep,
            log=log,
        )
        return _ShortcutUse(
            created=created,
            used=used,
            failure='foreground_lost_after_action',
            detail=(
                'the shortcut was launched but the run did not finish on the coins page: '
                f'launch_surface={launch_surface.value}, '
                f'return_surface={classify_surface(recovered.snapshot).value}, settled={recovered.settled}, '
                f'app_foreground={in_app!r}'
            ),
            labels=tuple(labels),
            taps=tuple(taps),
            balance_after=_trusted_balance(recovered.snapshot),
            final_surface=classify_surface(recovered.snapshot),
            pin_unconfirmed=pin_unconfirmed,
        )

    final = arrival
    reloaded = False
    balance = _trusted_balance(final.snapshot)
    if not _is_trusted_coin_page(final.snapshot, balance):
        # The pinned intent landed on a surface whose balance band is not
        # trusted (or not on a readable coin page). One measured reload gets
        # the coin page back without firing a second task action.
        final = _return_to_coin_page(
            capture,
            navigate,
            route,
            timeout=max(modal_timeout, DEFAULT_LAUNCH_TIMEOUT),
            interval=interval,
            clock=clock,
            sleep=sleep,
            log=log,
        )
        reloaded = True
        balance = _trusted_balance(final.snapshot)

    if balance is not None and not _bright_increase(balance_before, balance):
        # Let the server-side credit land on the page the pinned intent opened
        # before touching the route again.
        final, balance = _wait_for_balance(
            capture,
            balance_before,
            timeout=DEFAULT_BALANCE_GRACE,
            interval=interval,
            clock=clock,
            sleep=sleep,
        )
        if not _bright_increase(balance_before, balance) and not reloaded:
            # The #130 observation needed a later coin-page reload to show the
            # credit. Spend exactly one, then read again.
            final = _return_to_coin_page(
                capture,
                navigate,
                route,
                timeout=max(modal_timeout, DEFAULT_LAUNCH_TIMEOUT),
                interval=interval,
                clock=clock,
                sleep=sleep,
                log=log,
            )
            reloaded = True
            final, balance = _wait_for_balance(
                capture,
                balance_before,
                timeout=DEFAULT_BALANCE_GRACE,
                interval=interval,
                clock=clock,
                sleep=sleep,
            )

    final_surface = classify_surface(final.snapshot)
    in_app = foreground() if foreground is not None else None
    if final_surface is Surface.NON_APP or in_app is False or (final_surface is Surface.UNKNOWN and not final.settled):
        return _ShortcutUse(
            created=created,
            used=used,
            failure='foreground_lost_after_action',
            detail=(
                'the shortcut was launched but the run did not finish on the coins page: '
                f'launch_surface={launch_surface.value}, '
                f'return_surface={final_surface.value}, settled={final.settled}, '
                f'app_foreground={in_app!r}'
            ),
            labels=tuple(labels),
            taps=tuple(taps),
            balance_after=balance,
            final_surface=final_surface,
            pin_unconfirmed=pin_unconfirmed,
        )
    if not final.settled or final_surface not in _RETURN_COIN_SURFACES:
        return _ShortcutUse(
            created=created,
            used=used,
            failure='return_to_coins_page_failed',
            detail=(
                'the shortcut was launched but the run did not return to the coins page: '
                f'launch_surface={launch_surface.value}, '
                f'return_surface={final_surface.value}, settled={final.settled}, '
                f'app_foreground={in_app!r}'
            ),
            labels=tuple(labels),
            taps=tuple(taps),
            balance_after=balance,
            final_surface=final_surface,
            pin_unconfirmed=pin_unconfirmed,
        )
    if balance is None:
        return _ShortcutUse(
            created=created,
            used=used,
            failure='balance_unreadable_after',
            detail=(
                'the shortcut was launched and the app returned to the coins page, but no trusted balance '
                f'was readable (surface={final_surface.value}, settled={final.settled}, '
                f'reloaded={reloaded}, history={_history(final)})'
            ),
            labels=tuple(labels),
            taps=tuple(taps),
            balance_after=None,
            final_surface=final_surface,
            pin_unconfirmed=pin_unconfirmed,
        )
    return _ShortcutUse(
        created=created,
        used=used,
        failure=None,
        detail=f'balance {balance_before!r} -> {balance!r}',
        labels=tuple(labels),
        taps=tuple(taps),
        balance_after=balance,
        final_surface=final_surface,
        pin_unconfirmed=pin_unconfirmed,
    )


def _wait_for_creation_stage(
    capture: CaptureSource,
    *,
    timeout: float,
    interval: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    labels: list[str],
) -> tuple[str | None, Snapshot]:
    """Wait for the launcher sheet or the measured 'already exists' toast."""
    started = clock()
    deadline = started + timeout
    frame = _capture(capture)
    while True:
        _remember_labels(labels, frame)
        if find_text(frame, ADD_TO_HOME_LABEL) is not None:
            return 'add', frame
        if any(contains_text(frame, marker) for marker in SHORTCUT_EXISTS_MARKERS):
            return 'exists', frame
        if clock() >= deadline:
            return None, frame
        sleep(min(interval, max(0.0, deadline - clock())))
        frame = _capture(capture)


def _wait_for_add_to_finish(
    capture: CaptureSource,
    *,
    timeout: float,
    interval: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    labels: list[str],
) -> Snapshot:
    """Wait for the launcher's add sheet to close after its confirm control."""
    started = clock()
    deadline = started + timeout
    frame = _capture(capture)
    while True:
        _remember_labels(labels, frame)
        if find_text(frame, ADD_TO_HOME_LABEL) is None:
            return frame
        if clock() >= deadline:
            return frame
        sleep(min(interval, max(0.0, deadline - clock())))
        frame = _capture(capture)


def _wait_for_home_shortcut(
    capture: CaptureSource,
    *,
    timeout: float,
    interval: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    labels: list[str],
) -> Snapshot | None:
    """Wait for the home screen carrying the created ``Coins`` shortcut."""
    started = clock()
    deadline = started + timeout
    frame = _capture(capture)
    while True:
        _remember_labels(labels, frame)
        if _home_shortcut_box(frame) is not None:
            return frame
        if clock() >= deadline:
            return None
        sleep(min(interval, max(0.0, deadline - clock())))
        frame = _capture(capture)


def _home_shortcut_box(frame: Snapshot) -> TextBox | None:
    """Return the home-screen ``Coins`` shortcut label, or ``None``.

    The app sheet also carries a ``Coins`` header, so a launcher label is only
    accepted when the frame has no app-page marker and does carry another known
    home-screen label (``AliExpress`` or ``Settings``).
    """
    if any(contains_text(frame, marker) for marker in APP_PAGE_MARKERS):
        return None
    coins = find_text(frame, LAUNCHER_SHORTCUT_LABEL)
    if coins is None:
        return None
    if any(find_text(frame, marker) is not None for marker in LAUNCHER_PAGE_LABELS):
        return coins.box
    return None


def _close_shortcut_sheet(
    controller: Controller,
    capture: CaptureSource,
    *,
    labels: list[str],
) -> TapRecord | None:
    """Close the app's shortcut sheet with its visible ``Got it`` control."""
    frame = _capture(capture)
    _remember_labels(labels, frame)
    if not any(contains_text(frame, marker) for marker in SHORTCUT_MODAL_MARKERS):
        return None
    got_it = find_text(frame, GOT_IT_LABEL)
    if got_it is None:
        return None
    return controller.tap_box(
        got_it.box,
        why='close the shortcut sheet after the launcher item is ready',
        label=GOT_IT_LABEL,
    )


def _read_balance_and_row(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    navigate: RouteFirer,
    route: SurfaceRoute,
    title: str,
    *,
    before_route: Snapshot,
    templates: TemplateStore | None,
    modal_timeout: float,
    interval: float,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    log: logging.Logger,
) -> tuple[float | None, ScrollResult[TextBox], bool, CoinPageWait]:
    """Bracket the balance on a fresh coin page, then re-resolve the boost row.

    A route fire is asynchronous: the first frame after ``navigate`` can still
    be the boost sheet that was on screen before it. The run
    `20261127T140626Z` read that stale frame as the coin page, so both balance
    reads were ``None`` and a claim/failure could not be told apart. The wait
    here therefore requires the frame to have changed from the pre-route frame
    (or to have passed through loading) before the balance is trusted.
    """
    try:
        navigate(route)
    except RunAborted, RunInterrupted:
        raise
    except Exception as exc:
        log.warning('%s', _json({'event': 'shortcut_balance_route_failed', 'detail': f'{type(exc).__name__}: {exc}'}))
    arrival = _wait_for_launched_page(
        capture,
        before_route,
        timeout=max(modal_timeout, 10.0),
        interval=interval,
        clock=clock,
        sleep=sleep,
    )
    balance = _trusted_balance(arrival.snapshot)
    confirmed = False
    found: ScrollResult[TextBox]
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
        confirmed = opened.opened and opened.tap_attempts != 0
        found = _read_row(
            controller,
            capture,
            title,
            opened.snapshot,
            clock=clock,
            sleep=sleep,
            settle=settle,
            interval=interval,
        )
    except RunAborted, RunInterrupted:
        raise
    except Exception as exc:
        log.warning('%s', _json({'event': 'shortcut_balance_reopen_failed', 'detail': f'{type(exc).__name__}: {exc}'}))
        found = _read_row(
            controller,
            capture,
            title,
            arrival.snapshot,
            clock=clock,
            sleep=sleep,
            settle=settle,
            interval=interval,
        )
    return balance, found, confirmed, arrival


def _wait_for_launched_page(
    capture: CaptureSource,
    before_launch: Snapshot,
    *,
    timeout: float,
    interval: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> CoinPageWait:
    """Wait for the shortcut's own page, not the stale pre-launch frame.

    The launcher intent can take several seconds to replace the current page.
    Returning on the first settled coin-page frame would accept the page that
    was already there and reload it before the shortcut visit was recorded.
    A settled frame is therefore accepted only after it differs from the
    immediate pre-launch frame; a loading or non-app frame keeps the bounded
    wait running.
    """
    started = clock()
    deadline = started + timeout
    while True:
        remaining = max(interval, deadline - clock())
        wait = wait_for_coin_page(
            capture,
            timeout=remaining,
            interval=interval,
            clock=clock,
            sleep=sleep,
        )
        if wait.timed_out or not wait.settled:
            return wait
        if any(state is not ScreenState.PAGE for state in wait.history):
            # A loading/unknown frame proves the launch replaced the stale
            # pre-launch page even when the settled page looks identical.
            return wait
        if classify_surface(wait.snapshot) is not classify_surface(before_launch):
            # A boost sheet and a coin page can share the same pixels in a
            # scripted frame; the named surface still proves the route moved.
            return wait
        if frame_difference(before_launch, wait.snapshot) > STABLE_FRAME_MAX_DIFFERENCE:
            return wait
        if clock() >= deadline:
            return wait
        sleep(min(interval, max(0.0, deadline - clock())))


def _wait_for_balance(
    capture: CaptureSource,
    before: float | None,
    *,
    timeout: float,
    interval: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[CoinPageWait, float | None]:
    """Poll a settled coin page for the server-side +1, bounded.

    The pinned intent can render the page before the credit is visible. This is
    a read poll, not a tap or a navigation: it stops as soon as a trusted frame
    shows the advertised increase, and otherwise returns the last frame and
    balance at the deadline so the caller can name which evidence is missing.
    """
    deadline = clock() + timeout
    frame = _capture(capture)
    history: list[ScreenState] = []
    while True:
        classification = classify(frame)
        history.append(classification.state)
        balance = _trusted_balance(frame)
        if _bright_increase(before, balance):
            return (
                CoinPageWait(
                    snapshot=frame,
                    classification=classification,
                    history=tuple(history),
                    timed_out=False,
                ),
                balance,
            )
        now = clock()
        if now >= deadline:
            unsettled = (
                classification.state
                in (
                    ScreenState.LOADING,
                    ScreenState.POPUP,
                    ScreenState.UNKNOWN,
                )
                or classify_surface(frame) is Surface.NON_APP
            )
            return (
                CoinPageWait(
                    snapshot=frame,
                    classification=classification,
                    history=tuple(history),
                    timed_out=unsettled,
                ),
                balance,
            )
        sleep(min(interval, max(0.0, deadline - now)))
        frame = _capture(capture)


def _is_trusted_coin_page(frame: Snapshot, balance: float | None) -> bool:
    """Whether ``frame`` is a coin page whose balance can bracket a claim."""
    return balance is not None and classify_surface(frame) in (Surface.ROWS_PRESENT, Surface.ROWS_ABSENT)


def _history(wait: CoinPageWait) -> str:
    """Render a wait's state history for a failure detail."""
    return '[' + ','.join(state.value for state in wait.history) + ']'


def _return_to_coin_page(
    capture: CaptureSource,
    navigate: RouteFirer,
    route: SurfaceRoute,
    *,
    timeout: float,
    interval: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    log: logging.Logger,
) -> CoinPageWait:
    """Fire the measured return route and wait for a settled app frame."""
    try:
        navigate(route)
    except RunAborted, RunInterrupted:
        raise
    except Exception as exc:
        log.warning('%s', _json({'event': 'shortcut_return_failed', 'detail': f'{type(exc).__name__}: {exc}'}))
    return wait_for_coin_page(
        capture,
        timeout=max(timeout, 10.0),
        interval=interval,
        clock=clock,
        sleep=sleep,
    )


def _confirm_created_pin(
    shortcut_present: Callable[[], bool | None] | None,
    *,
    timeout: float,
    interval: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    log: logging.Logger,
) -> bool | None:
    """Confirm the launcher's Add step actually produced the Coins pin.

    ``True`` means the pin is readable, ``False`` means the probe stayed
    readable but negative, and ``None`` means at least one read was unreadable.
    The caller continues on ``None`` without claiming the pin was created, so a
    transient unreadable probe never triggers the package-wide cleanup.
    """
    if shortcut_present is None:
        # No probe means no confirmation. A caller with only a cleanup seam must
        # not claim creation and then run the package-wide clear.
        return None
    deadline = clock() + timeout
    saw_unreadable = False
    while True:
        reading = _read_shortcut_presence(shortcut_present, log)
        if reading is True:
            return True
        if reading is None:
            saw_unreadable = True
        if clock() >= deadline:
            return None if saw_unreadable else False
        sleep(min(interval, max(0.0, deadline - clock())))


def _read_shortcut_presence(
    shortcut_present: Callable[[], bool | None] | None,
    log: logging.Logger,
) -> bool | None:
    """Read the launcher's pinned-shortcut probe without inventing an answer."""
    if shortcut_present is None:
        return None
    try:
        return shortcut_present()
    except RunAborted, RunInterrupted:
        raise
    except Exception as exc:
        log.warning('%s', _json({'event': 'shortcut_presence_failed', 'detail': f'{type(exc).__name__}: {exc}'}))
        return None


def _remove_shortcut(remove_shortcut: Callable[[], object], log: logging.Logger) -> bool:
    """Run the injected launcher-item cleanup without masking an interrupt."""
    try:
        result = remove_shortcut()
    except RunAborted, RunInterrupted:
        raise
    except Exception as exc:
        log.warning('%s', _json({'event': 'shortcut_cleanup_failed', 'detail': f'{type(exc).__name__}: {exc}'}))
        return False
    return result is not False


def _bright_increase(before: float | None, after: float | None) -> bool:
    """Whether the readable balance moved by exactly the advertised +1."""
    return before is not None and after is not None and after - before == 1.0


def _remember_labels(labels: list[str], frame: Snapshot) -> None:
    """Keep every readable label seen on the sheet path for the run report."""
    for text in _frame_labels(frame):
        if text not in labels:
            labels.append(text)


def _frame_labels(frame: Snapshot) -> tuple[str, ...]:
    return tuple(box.text for box in frame.boxes if box.score >= DEFAULT_MIN_OCR_SCORE and box.text.strip())


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
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    settle: float = 0.0,
    interval: float = DEFAULT_INTERVAL,
) -> ScrollResult[TextBox]:
    """Search for the row's own ``GO`` on settled frames only.

    The run `20261127T140626Z` named the row in a frame captured while the
    search swipe was still flinging, then tapped it after the list had moved.
    A live search therefore uses a gentler, slower swipe and waits for the
    frame to stop changing before the target is accepted. Tests with
    ``settle=0`` keep the fast, scripted path.
    """
    after_swipe: Callable[[], None] | None = None
    if settle > 0:

        def after_swipe() -> None:
            wait_until_stable(
                capture,
                timeout=max(settle, DEFAULT_ROW_SETTLE_TIMEOUT),
                interval=interval,
                clock=clock,
                sleep=sleep,
            )

    return scroll_until_found(
        controller,
        lambda: _capture(capture),
        lambda frame: _action_for(frame, title),
        initial=initial,
        max_attempts=MAX_SCROLL_ATTEMPTS,
        viewport_fraction=ROW_SCROLL_FRACTION,
        after_swipe=after_swipe,
        duration_ms=ROW_SWIPE_DURATION_MS,
        why='find the shortcut-row GO below the boost fold',
    )


def _settled_action(
    controller: Controller,
    capture: CaptureSource,
    title: str,
    found: ScrollResult[TextBox],
    *,
    settle: float,
    interval: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[TextBox | None, ScrollResult[TextBox]]:
    """Re-resolve the row on a frame that has stopped moving before tapping.

    This is the guard the failed run lacked: the row was named 1.4 s after a
    swipe while the list was still moving, and the next frame had carried the
    row above the fold. If the settled frame no longer carries the row, the
    caller fails with ``row_not_settled`` instead of tapping a stale point.
    """
    if settle <= 0:
        return found.target, found
    stable = wait_until_stable(
        capture,
        timeout=max(settle, DEFAULT_ROW_SETTLE_TIMEOUT),
        interval=interval,
        clock=clock,
        sleep=sleep,
        initial=_capture(capture),
    )
    action = _action_for(stable.frame, title)
    if stable.timed_out or action is None:
        return None, ScrollResult(
            outcome=ScrollOutcome.STABLE if stable.timed_out else found.outcome,
            target=None,
            frames=(*found.frames, stable.frame),
            attempts=found.attempts,
        )
    return action, ScrollResult(
        outcome=ScrollOutcome.FOUND,
        target=action,
        frames=(*found.frames, stable.frame),
        attempts=found.attempts,
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
    shortcut_created: bool = False,
    shortcut_removed: bool = False,
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
        shortcut_created=shortcut_created,
        shortcut_removed=shortcut_removed,
    )


def _log_result(logger: logging.Logger, result: ShortcutVisitResult) -> None:
    logger.info('%s', _json({'event': 'shortcut_visit_task', **result.as_dict()}))


def _json(fields: dict[str, object]) -> str:
    return json.dumps(fields, separators=(',', ':'), sort_keys=True, default=str)


def _capture(source: CaptureSource) -> Snapshot:
    return source() if callable(source) else source.capture()
