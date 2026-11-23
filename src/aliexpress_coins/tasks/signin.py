"""The daily sign-in executor: two independent claims, recorded separately (#23).

The coins surface has **two** daily sign-ins and neither proves the other is
done (docs/05-app-knowledge.md §5):

* the streak card's ``Collect`` control on the coin page, above the task rows;
* the boost page's ``Daily check-in`` row, with its own ``GO``.

The streak control is deliberately **not** a boost row, so it never goes through
:func:`~aliexpress_coins.tasks.discovery.enumerate_roster` / row dispatch. This
module owns that control directly. The boost claim does use the row layout: the
button is resolved from the current frame with
:func:`~aliexpress_coins.perception.layout.find_action_in_row`, never from a
remembered position.

Two rules shape every claim:

* **state first, tap second** — :meth:`DailyStateStore.done_today` is checked
  before a frame is captured or a tap is planned, and
  :meth:`DailyStateStore.record` only runs after the fresh frame agrees. A
  claim whose evidence is ambiguous is ``unverified`` and is *not* recorded, so
  the next run retries it instead of pretending the day is done.
* **the screen is the authority** — after one tap the executor sweeps popups
  through :func:`~aliexpress_coins.control.popups.dismiss_after` and captures a
  fresh frame. The control disappearing (or the card reading ``Tomorrow``) is
  the primary proof; a readable balance increase is only a secondary signal.

``redeem_interstitial`` is the one corpus row with no executable close. It can
follow a claim, and the coins were already taken before it appeared, so this
module records the claim as ``completed``, notes the interstitial and stops the
executor cleanly. It does not raise and it does not keep tapping behind the
overlay.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from aliexpress_coins.control.controller import LOGGER_NAME, Controller, TapRecord
from aliexpress_coins.control.errors import PopupUnclosable
from aliexpress_coins.control.popups import DismissReport, PopupCorpus, dismiss_after
from aliexpress_coins.perception.layout import Row, find_action_in_row, group_rows, group_sections, row_of
from aliexpress_coins.perception.matching import (
    DEFAULT_MIN_OCR_SCORE,
    DEFAULT_MIN_SCORE,
    find_text,
    similarity,
)
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.state import TASK_TITLES, CaptureSource, ScreenState, classify
from aliexpress_coins.perception.templates import TemplateStore
from aliexpress_coins.perception.waits import wait_for_text
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks.catalogue import DispatchAuthorization, TaskCategory
from aliexpress_coins.tasks.discovery import DiscoveryRoute, open_boost_list
from aliexpress_coins.tasks.errors import TaskError
from aliexpress_coins.tasks.surface import (
    BOOST_MARKER,
    COIN_PAGE_MARKERS,
    REDEEM_MARKER,
    Surface,
    classify_surface,
    contains_text,
    is_boost_page,
)

__all__ = [
    'BALANCE_BOTTOM_FRACTION',
    'BALANCE_TOP_FRACTION',
    'BALANCE_UNREADABLE',
    'BALANCE_ZERO_UNVERIFIED',
    'BOOST_CLAIMED_LABELS',
    'BOOST_KEY',
    'BOOST_ROW_LABELS',
    'DEFAULT_OPEN_TIMEOUT',
    'DEFAULT_VERIFY_INTERVAL',
    'DEFAULT_VERIFY_TIMEOUT',
    'REDEEM_INTERSTITIAL',
    'STREAK_CLAIMED_LABELS',
    'STREAK_CONTROL_LABELS',
    'STREAK_KEY',
    'STREAK_MARKER',
    'BalanceReading',
    'ClaimOutcome',
    'ClaimResult',
    'SigninOutcome',
    'SigninReport',
    'claim_boost_checkin',
    'claim_daily_signins',
    'claim_signins',
    'claim_streak',
    'find_boost_control',
    'find_streak_control',
    'read_balance',
    'read_coin_balance',
]

STREAK_KEY = 'daily_checkin'
"""The catalogue key of the streak ``Collect`` claim."""

BOOST_KEY = 'boost_checkin'
"""The catalogue key of the boost page's ``Daily check-in`` row."""

STREAK_MARKER = 'Daily coins'
"""The section marker above the streak card (docs/07 §3)."""

STREAK_CONTROL_LABELS: tuple[str, ...] = ('Collect', 'Claim', 'Sign in')
"""The label variants the streak claim control has been seen with."""

STREAK_CLAIMED_LABELS: tuple[str, ...] = ('Tomorrow', 'Claimed', 'Collected', 'Checked in')
"""Wording the streak card uses after today's claim."""

BOOST_ROW_LABELS: tuple[str, ...] = ('Daily check-in', "Finish today's check-in")
"""The boost row title and subtitle (docs/05 §4)."""

BOOST_CLAIMED_LABELS: tuple[str, ...] = ('Claimed', 'Collected', 'Checked in')
"""Wording a boost row uses when it stays on screen after a claim."""

REDEEM_INTERSTITIAL = 'redeem_interstitial'
"""The corpus family name of the unclosable reward interstitial."""

BALANCE_UNREADABLE = 'balance_unreadable'
"""The named event and reason for a frame with no readable coin balance.

A failed read is ``None``, never ``0``: the claim leaves the stored value alone
and records this code instead of inventing a number.
"""

BALANCE_ZERO_UNVERIFIED = 'balance_zero_unverified'
"""The named event and reason for a lone ``0`` the claim could not corroborate.

A genuine zero is a bare number on the coin surface, and a claim that started
from a readable zero keeps it. A ``0`` read after an unreadable or positive
starting balance is a control numeral or a redacted post-claim surface, so it
is treated as unreadable rather than persisted.
"""

DEFAULT_VERIFY_TIMEOUT = 8.0
"""How long to wait for a fresh frame to prove a claim before calling it unverified."""

DEFAULT_OPEN_TIMEOUT = 10.0
"""How long to wait for ``Earn more`` to open the boost list after the tap.

Measured on 2026-11-23: the warm destination rendered 2.4-5.4 s after the tap,
so 10 s is about twice the slowest measured navigation. This is deliberately
not the route's 30 s budget: the swallowed-tap case is covered by the bounded
re-tap in :func:`~aliexpress_coins.tasks.discovery.open_boost_list`, not by a
longer stare. The 8 s claim-verification budget is not a navigation budget, and
sharing it was the #84 failure.
"""

DEFAULT_VERIFY_INTERVAL = 1.0
"""How often to capture while waiting for the post-claim frame."""

BALANCE_TOP_FRACTION = 0.03
BALANCE_BOTTOM_FRACTION = 0.25
"""The measured balance band: a bare number in the top quarter of the coin page.

The lower bound skips the status bar. docs/01-recon-findings.md §3 records the
balance as the only bare number in the top 25% of the screen, which is what
keeps a ``+3~6`` reward badge from being read as a balance. Fractions of the
frame, not stored pixels, so the rule ports to another geometry.
"""

_COIN_PAGE_SURFACES = frozenset({Surface.ROWS_PRESENT, Surface.ROWS_ABSENT})
"""The only surfaces whose balance band can prove a genuine zero.

The boost page also renders the balance, but its top quarter can carry a
control numeral such as ``0``. A zero read there is not evidence (issue #94).
"""

_BARE_NUMBER = re.compile(r'^\s*(\d{1,3}(?:,\d{3})*|\d+)\s*$')

_STREAK_CARD_HEIGHT_RATIO = 5.0
"""How far below the marker row the streak card may reach, in row heights.

The measured card ends about 4.5 marker-row heights below the marker, and the
first task row starts about 5.8 below it. Scaling by the marker row's own
height keeps the band tied to the frame's text size rather than a pixel offset.
"""


class SigninOutcome(StrEnum):
    """What one sign-in claim actually did."""

    COMPLETED = 'completed'
    ALREADY_DONE = 'already done today'
    UNAVAILABLE = 'unavailable'
    UNVERIFIED = 'unverified'


# A readable alias: both names mean the same four outcomes.
ClaimOutcome = SigninOutcome


@dataclass(frozen=True, slots=True)
class BalanceReading:
    """One frame's balance and the surface that produced it.

    ``value`` is the raw bare number, so a caller can still report what the
    frame rendered. ``claim_value`` is what a claim may use: an untrusted zero
    becomes ``None`` because a lone ``0`` on the boost page or any non-coin
    surface is a control numeral, not evidence of a new account.
    """

    value: float | None
    surface: Surface

    @property
    def trusted(self) -> bool:
        """Whether the frame is a positively classified coin page."""
        return self.surface in _COIN_PAGE_SURFACES

    @property
    def claim_value(self) -> float | None:
        """The value a claim may use, treating an unproven zero as unreadable."""
        if self.value == 0.0 and not self.trusted:
            return None
        return self.value

    @property
    def code(self) -> str | None:
        """The named reason this reading cannot be used, or ``None`` when it can."""
        if self.value is None:
            return BALANCE_UNREADABLE
        if self.value == 0.0 and not self.trusted:
            return BALANCE_ZERO_UNVERIFIED
        return None


@dataclass(frozen=True, slots=True)
class ClaimResult:
    """One claim, the evidence behind it, and whether state was recorded.

    ``verification`` names the proof: ``state`` (already done), ``absent_control``
    (target gone on a fresh frame), ``claimed_marker`` (the card says Tomorrow /
    Claimed), ``balance_delta`` (secondary evidence), ``absent_boost_row``,
    ``not_found`` or ``no_evidence``. ``detail`` is the report-safe sentence.
    """

    key: str
    outcome: SigninOutcome
    detail: str
    verification: str
    balance_before: float | None = None
    balance_after: float | None = None
    control: TextBox | None = None
    tap: TapRecord | None = None
    dismiss: DismissReport | None = None
    interstitial: str | None = None
    recorded: bool = False

    @property
    def completed(self) -> bool:
        """Whether the claim succeeded and was recorded."""
        return self.outcome is SigninOutcome.COMPLETED

    @property
    def already_done(self) -> bool:
        """Whether state skipped the claim before any tap."""
        return self.outcome is SigninOutcome.ALREADY_DONE

    @property
    def unavailable(self) -> bool:
        """Whether the control was absent from the frame."""
        return self.outcome is SigninOutcome.UNAVAILABLE

    @property
    def unverified(self) -> bool:
        """Whether the tap was issued but the screen did not agree."""
        return self.outcome is SigninOutcome.UNVERIFIED

    @property
    def tapped(self) -> bool:
        """Whether the controller issued (or rehearsed) the claim tap."""
        return self.tap is not None

    def as_dict(self) -> dict[str, object]:
        """Render the result for ``run.json`` with the evidence retained."""
        record: dict[str, object] = {
            'key': self.key,
            'outcome': self.outcome.value,
            'detail': self.detail,
            'verification': self.verification,
            'balance_before': self.balance_before,
            'balance_after': self.balance_after,
            'recorded': self.recorded,
            'interstitial': self.interstitial,
            'tapped': self.tapped,
        }
        if self.control is not None:
            record['control'] = {
                'text': self.control.text,
                'box': list(self.control.bbox),
                'target': list(self.control.center),
            }
        if self.tap is not None:
            record['tap'] = self.tap.as_dict()
        if self.dismiss is not None:
            record['dismiss'] = self.dismiss.as_dict()
        return record


@dataclass(frozen=True, slots=True)
class SigninReport:
    """Both independent claims, plus the balances the run could actually read."""

    streak: ClaimResult
    boost: ClaimResult | None = None
    route: DiscoveryRoute | None = None
    balance_before: float | None = None
    balance_after: float | None = None
    stop: str | None = None

    @property
    def results(self) -> tuple[ClaimResult, ...]:
        """Every claim that was attempted, streak first."""
        if self.boost is None:
            return (self.streak,)
        return (self.streak, self.boost)

    @property
    def completed(self) -> tuple[ClaimResult, ...]:
        """The claims that succeeded this run."""
        return tuple(result for result in self.results if result.completed)

    @property
    def already_done(self) -> tuple[ClaimResult, ...]:
        """The claims that state skipped before any tap."""
        return tuple(result for result in self.results if result.already_done)

    @property
    def unverified(self) -> tuple[ClaimResult, ...]:
        """The claims that need a retry on the next run."""
        return tuple(result for result in self.results if result.unverified)

    @property
    def stopped_by_redeem(self) -> bool:
        """Whether the unclosable reward interstitial ended the executor cleanly."""
        return self.stop == REDEEM_INTERSTITIAL

    def as_dict(self) -> dict[str, object]:
        """Render the run summary for a report."""
        return {
            'streak': self.streak.as_dict(),
            'boost': None if self.boost is None else self.boost.as_dict(),
            'route': self.route.value if self.route is not None else None,
            'balance_before': self.balance_before,
            'balance_after': self.balance_after,
            'stop': self.stop,
        }


def find_streak_control(snapshot: Snapshot) -> TextBox | None:
    """Return the streak section's claim control, or ``None``.

    The control is looked up only inside the section below the ``Daily coins``
    marker. That is what keeps a generic ``Claim`` label elsewhere on the page
    out of this executor; the streak control is not a boost row and must never
    be routed through row dispatch.
    """
    rows = _streak_rows(snapshot)
    if not rows:
        return None
    for row in rows:
        action = find_action_in_row(row, STREAK_CONTROL_LABELS)
        if action is not None:
            return action
    return None


def find_boost_control(snapshot: Snapshot) -> tuple[Row | None, TextBox | None]:
    """Return the ``Daily check-in`` row and the action inside that row.

    The row is found by its title/subtitle first, then its own action is read
    from the same row with :func:`find_action_in_row`. A remembered button
    position would point at another task as soon as the roster reorders.
    """
    rows = group_rows(snapshot.boxes, snapshot.geometry)
    for label in BOOST_ROW_LABELS:
        match = find_text(
            snapshot,
            label,
            min_score=DEFAULT_MIN_SCORE,
            min_ocr_score=DEFAULT_MIN_OCR_SCORE,
        )
        if match is None:
            continue
        row = row_of(rows, match)
        if row is None:
            continue
        return row, find_action_in_row(row)
    return None, None


def read_coin_balance(snapshot: Snapshot) -> float | None:
    """Read the raw coin balance when the frame renders it, else ``None``.

    This keeps the measured rule — the only bare number in the top quarter —
    but drops the surface provenance. Claim paths use :func:`read_balance` so a
    zero can be trusted only when the frame is a validated coin page.
    """
    return read_balance(snapshot).value


def read_balance(snapshot: Snapshot) -> BalanceReading:
    """Read the balance and classify the surface that rendered it.

    The measured rule is the only bare number in the top quarter of the coin
    page (docs/01-recon-findings.md §3). The status-bar band is excluded, and a
    currency amount or reward badge is not a bare number. Masked or unreadable
    balances return ``None``; a readable bare ``0`` is returned as ``0.0`` but
    is only claim evidence on a positively classified coin page.
    """
    surface = classify_surface(snapshot)
    top = snapshot.height * BALANCE_TOP_FRACTION
    bottom = snapshot.height * BALANCE_BOTTOM_FRACTION
    for box in sorted(snapshot.boxes, key=lambda item: (item.bbox[1], item.bbox[0])):
        if box.score < DEFAULT_MIN_OCR_SCORE:
            continue
        if not top <= box.center[1] <= bottom:
            continue
        match = _BARE_NUMBER.match(box.text)
        if match is not None:
            return BalanceReading(value=float(match.group(1).replace(',', '')), surface=surface)
    return BalanceReading(value=None, surface=surface)


def _require_signin_authorization(authorization: DispatchAuthorization) -> None:
    """Refuse a sign-in claim that did not come through the dispatch gate."""
    if not isinstance(authorization, DispatchAuthorization):
        raise TaskError(
            'the daily sign-in is not dispatchable: no dispatch authorization (the policy gate was bypassed)'
        )
    if authorization.category is not TaskCategory.SIGN_IN or authorization.key not in {STREAK_KEY, BOOST_KEY}:
        raise TaskError(f'{authorization.title!r} is not a dispatchable daily sign-in')


def claim_streak(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    state: DailyStateStore,
    *,
    authorization: DispatchAuthorization,
    snapshot: Snapshot | None = None,
    templates: TemplateStore | None = None,
    timeout: float = DEFAULT_VERIFY_TIMEOUT,
    interval: float = DEFAULT_VERIFY_INTERVAL,
    settle: float = 0.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    logger: logging.Logger | None = None,
) -> ClaimResult:
    """Claim the streak ``Collect`` on the coin page, once, and verify it.

    State is checked before the first capture. If today's key is already done
    the function returns ``already done today`` without issuing any tap. A frame
    with no streak control is ``unavailable`` (never ``completed``). Otherwise
    the control is tapped exactly once, popups are swept, and a fresh frame must
    show the control gone / the card claimed; a positive, readable balance delta
    is accepted as secondary evidence. Anything else is ``unverified`` and is
    not recorded, so the next run retries.
    """
    _require_signin_authorization(authorization)
    log = logger if logger is not None else logging.getLogger(LOGGER_NAME)
    if state.done_today(STREAK_KEY):
        return _already_done(STREAK_KEY, STREAK_MARKER)
    if timeout <= 0:
        raise ValueError(f'timeout must be positive, got {timeout!r}')
    if interval <= 0:
        raise ValueError(f'interval must be positive, got {interval!r}')

    frame, ready = _prepare_entry(
        controller,
        capture,
        corpus,
        _frame(capture, snapshot),
        templates=templates,
        timeout=timeout,
        interval=interval,
        settle=settle,
        clock=clock,
        sleep=sleep,
        logger=log,
    )
    if not ready:
        result = ClaimResult(
            key=STREAK_KEY,
            outcome=SigninOutcome.UNAVAILABLE,
            detail='the coin page did not reach a settled state; no claim control was tapped',
            verification='not_settled',
            balance_before=read_coin_balance(frame),
        )
        _log_result(log, result)
        return result

    # The runner fires the shortcut immediately before this call, but the app
    # can still be on the boost page for a capture or two. On that frame no
    # Collect control is visible, and reading that absence as "unavailable" is
    # how #84 left the streak unclaimed. Wait for the measured streak surface
    # instead of deciding from whatever happened to be on screen.
    if not _streak_visible(frame):
        arrived = wait_for_text(
            capture,
            _streak_visible,
            timeout=timeout,
            interval=interval,
            initial=frame,
            clock=clock,
            sleep=sleep,
        )
        frame = arrived.frame

    control = find_streak_control(frame)
    if control is None:
        detail = (
            f'the coin page or the {STREAK_MARKER!r} streak card did not appear within {timeout:g}s; '
            'no claim control was tapped'
            if not _streak_visible(frame)
            else f'{STREAK_MARKER!r} has no Collect/Claim/Sign in control in this frame'
        )
        result = ClaimResult(
            key=STREAK_KEY,
            outcome=SigninOutcome.UNAVAILABLE,
            detail=detail,
            verification='not_found',
            balance_before=read_coin_balance(frame),
        )
        _log_result(log, result)
        return result

    before_reading = read_balance(frame)
    before = before_reading.claim_value
    if before_reading.code is not None:
        _log_balance(log, key=STREAK_KEY, code=before_reading.code, phase='before', reading=before_reading)
    tap = controller.tap_box(
        control,
        why='claim the daily sign-in streak once',
        label=control.text,
    )
    dismiss, interstitial = _sweep(
        controller,
        capture,
        corpus,
        templates=templates,
        settle=settle,
        clock=clock,
        sleep=sleep,
        logger=log,
    )
    if interstitial == REDEEM_INTERSTITIAL:
        # The coins were taken before the overlay appeared. The corpus marks it
        # unclosable, so record the claim and stop cleanly instead of raising.
        state.record(STREAK_KEY)
        result = ClaimResult(
            key=STREAK_KEY,
            outcome=SigninOutcome.COMPLETED,
            detail='streak claimed; the unclosable reward interstitial followed, so the run stops here',
            verification='interstitial',
            balance_before=before,
            control=control,
            tap=tap,
            dismiss=dismiss,
            interstitial=interstitial,
            recorded=True,
        )
        _log_result(log, result)
        return result

    verdict = _verify_streak_until(
        capture,
        before,
        timeout=timeout,
        interval=interval,
        clock=clock,
        sleep=sleep,
    )
    if verdict is not None:
        verification, after_reading = verdict
        after = after_reading.claim_value
        interstitial = REDEEM_INTERSTITIAL if verification == 'interstitial' else None
        recorded_balance = _claim_balance(before, after)
        balance_code = _balance_code(after_reading, recorded_balance)
        if balance_code is not None:
            _log_balance(
                log,
                key=STREAK_KEY,
                code=balance_code,
                phase='after',
                reading=after_reading,
                before=before,
                after=after,
                recorded=recorded_balance,
            )
        if interstitial is None:
            state.record(
                STREAK_KEY,
                balance=recorded_balance,
                balance_verified=_recorded_verified(before, before_reading, after, after_reading, recorded_balance),
            )
            detail = f'streak claim verified by {verification.replace("_", " ")}'
        else:
            state.record(STREAK_KEY)
            detail = 'streak claimed; the reward interstitial appeared during verification, so the run stops here'
        result = ClaimResult(
            key=STREAK_KEY,
            outcome=SigninOutcome.COMPLETED,
            detail=detail,
            verification=verification,
            balance_before=before,
            balance_after=recorded_balance,
            control=control,
            tap=tap,
            dismiss=dismiss,
            interstitial=interstitial,
            recorded=True,
        )
        _log_result(log, result)
        return result

    result = ClaimResult(
        key=STREAK_KEY,
        outcome=SigninOutcome.UNVERIFIED,
        detail='the streak control was tapped once but no fresh frame proved the claim; not recorded',
        verification='no_evidence',
        balance_before=before,
        control=control,
        tap=tap,
        dismiss=dismiss,
    )
    _log_result(log, result)
    return result


def claim_boost_checkin(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    state: DailyStateStore,
    *,
    authorization: DispatchAuthorization,
    snapshot: Snapshot | None = None,
    templates: TemplateStore | None = None,
    timeout: float = DEFAULT_VERIFY_TIMEOUT,
    interval: float = DEFAULT_VERIFY_INTERVAL,
    settle: float = 0.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    logger: logging.Logger | None = None,
) -> ClaimResult:
    """Claim the boost page's ``Daily check-in`` row, once, and verify it.

    The row's own ``GO`` is resolved from the current frame; no remembered
    position is used. State is checked before capture, an absent row is
    ``unavailable``, and the same post-claim proof as :func:`claim_streak`
    applies. The sweep runs without the ``coins_boost_sheet`` family, whose
    anchor is the destination's own ``Coins boost`` header.
    """
    _require_signin_authorization(authorization)
    log = logger if logger is not None else logging.getLogger(LOGGER_NAME)
    if state.done_today(BOOST_KEY):
        return _already_done(BOOST_KEY, BOOST_ROW_LABELS[0])
    if timeout <= 0:
        raise ValueError(f'timeout must be positive, got {timeout!r}')
    if interval <= 0:
        raise ValueError(f'interval must be positive, got {interval!r}')

    frame, ready = _prepare_entry(
        controller,
        capture,
        corpus,
        _frame(capture, snapshot),
        templates=templates,
        timeout=timeout,
        interval=interval,
        settle=settle,
        clock=clock,
        sleep=sleep,
        logger=log,
    )
    if not ready:
        result = ClaimResult(
            key=BOOST_KEY,
            outcome=SigninOutcome.UNAVAILABLE,
            detail='the boost page did not reach a settled state; no claim action was tapped',
            verification='not_settled',
            balance_before=read_coin_balance(frame),
        )
        _log_result(log, result)
        return result

    row, action = find_boost_control(frame)
    if row is None or action is None:
        result = ClaimResult(
            key=BOOST_KEY,
            outcome=SigninOutcome.UNAVAILABLE,
            detail='the boost Daily check-in row or its own action is absent from this frame',
            verification='not_found',
            balance_before=read_coin_balance(frame),
        )
        _log_result(log, result)
        return result

    before_reading = read_balance(frame)
    before = before_reading.claim_value
    if before_reading.code is not None:
        _log_balance(log, key=BOOST_KEY, code=before_reading.code, phase='before', reading=before_reading)
    tap = controller.tap_box(
        action,
        why='claim the boost Daily check-in row once',
        label=action.text,
    )
    dismiss, interstitial = _sweep(
        controller,
        capture,
        _without_boost_family(corpus),
        templates=templates,
        settle=settle,
        clock=clock,
        sleep=sleep,
        logger=log,
    )
    if interstitial == REDEEM_INTERSTITIAL:
        state.record(BOOST_KEY)
        result = ClaimResult(
            key=BOOST_KEY,
            outcome=SigninOutcome.COMPLETED,
            detail='check-in claimed; the unclosable reward interstitial followed, so the run stops here',
            verification='interstitial',
            balance_before=before,
            control=action,
            tap=tap,
            dismiss=dismiss,
            interstitial=interstitial,
            recorded=True,
        )
        _log_result(log, result)
        return result

    verdict = _verify_boost_until(
        capture,
        before,
        timeout=timeout,
        interval=interval,
        clock=clock,
        sleep=sleep,
    )
    if verdict is not None:
        verification, after_reading = verdict
        after = after_reading.claim_value
        interstitial = REDEEM_INTERSTITIAL if verification == 'interstitial' else None
        # A claimed reward never reduces the balance. The post-claim frame can
        # be a non-coin surface whose top quarter holds another bare number, so
        # an ``after`` below ``before`` is a misread, not news: recording it (a
        # live 2026-11-23 claim wrote 0.0 over a real 54) would poison
        # ``last_balance``. A genuine balance delta still replaces the value.
        recorded_balance = _claim_balance(before, after)
        balance_code = _balance_code(after_reading, recorded_balance)
        if balance_code is not None:
            _log_balance(
                log,
                key=BOOST_KEY,
                code=balance_code,
                phase='after',
                reading=after_reading,
                before=before,
                after=after,
                recorded=recorded_balance,
            )
        if interstitial is None:
            state.record(
                BOOST_KEY,
                balance=recorded_balance,
                balance_verified=_recorded_verified(before, before_reading, after, after_reading, recorded_balance),
            )
            detail = f'boost check-in verified by {verification.replace("_", " ")}'
        else:
            state.record(BOOST_KEY)
            detail = 'check-in claimed; the reward interstitial appeared during verification, so the run stops here'
        result = ClaimResult(
            key=BOOST_KEY,
            outcome=SigninOutcome.COMPLETED,
            detail=detail,
            verification=verification,
            balance_before=before,
            balance_after=recorded_balance,
            control=action,
            tap=tap,
            dismiss=dismiss,
            interstitial=interstitial,
            recorded=True,
        )
        _log_result(log, result)
        return result

    result = ClaimResult(
        key=BOOST_KEY,
        outcome=SigninOutcome.UNVERIFIED,
        detail='the boost action was tapped once but no fresh frame proved the claim; not recorded',
        verification='no_evidence',
        balance_before=before,
        control=action,
        tap=tap,
        dismiss=dismiss,
    )
    _log_result(log, result)
    return result


def claim_daily_signins(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    state: DailyStateStore,
    *,
    authorization: DispatchAuthorization,
    snapshot: Snapshot | None = None,
    templates: TemplateStore | None = None,
    timeout: float = DEFAULT_VERIFY_TIMEOUT,
    open_timeout: float = DEFAULT_OPEN_TIMEOUT,
    interval: float = DEFAULT_VERIFY_INTERVAL,
    settle: float = 0.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    logger: logging.Logger | None = None,
) -> SigninReport:
    """Claim both independent daily sign-ins and return one report.

    The call starts on the coin page (or accepts its ``snapshot``). If both
    state keys are already done, it returns before any capture or tap. If the
    boost key is still open it opens the boost list with
    :func:`~aliexpress_coins.tasks.discovery.open_boost_list`, then claims the
    row from that frame. A redeem interstitial after either claim ends the
    executor cleanly: there is nothing safe to do behind an unclosable overlay.

    ``timeout`` bounds each claim's verification and the wait for the streak
    surface; ``open_timeout`` bounds the navigation to the boost list. They are
    separate because the measured failure (#84) was a navigation still running
    when the 8 s verification budget expired, not a claim that needed longer to
    prove itself.

    ``authorization`` is the dispatch gate's sealed token. Without it this
    function refuses before the first capture, so the control cannot be claimed
    by a caller that bypassed the policy decision.
    """
    _require_signin_authorization(authorization)
    log = logger if logger is not None else logging.getLogger(LOGGER_NAME)
    if open_timeout <= 0:
        raise ValueError(f'open_timeout must be positive, got {open_timeout!r}')
    streak = claim_streak(
        controller,
        capture,
        corpus,
        state,
        authorization=authorization,
        snapshot=snapshot,
        templates=templates,
        timeout=timeout,
        interval=interval,
        settle=settle,
        clock=clock,
        sleep=sleep,
        logger=log,
    )
    if streak.interstitial == REDEEM_INTERSTITIAL:
        return SigninReport(
            streak=streak,
            balance_before=streak.balance_before,
            balance_after=streak.balance_after,
            stop=REDEEM_INTERSTITIAL,
        )

    boost: ClaimResult
    route: DiscoveryRoute | None = None
    if state.done_today(BOOST_KEY):
        boost = _already_done(BOOST_KEY, BOOST_ROW_LABELS[0])
    else:
        try:
            opened = open_boost_list(
                controller,
                capture,
                corpus,
                templates=templates,
                timeout=open_timeout,
                interval=interval,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=log,
            )
        except PopupUnclosable:
            frame = _capture(capture)
            if not contains_text(frame, REDEEM_MARKER):
                raise
            before = _first_balance(streak)
            latest = _latest_balance(streak)
            return SigninReport(
                streak=streak,
                balance_before=before,
                balance_after=before if latest is None else latest,
                stop=REDEEM_INTERSTITIAL,
            )
        route = opened.route
        if opened.empty:
            # ``There's nothing here yet`` is the documented cold-start load
            # fault, not an empty day. Handing it to claim_boost_checkin would
            # report an absent row and skip the claim silently; that is exactly
            # the cold variant the #84 probe measured, so fail with the reason.
            boost = ClaimResult(
                key=BOOST_KEY,
                outcome=SigninOutcome.UNVERIFIED,
                detail='the Coins boost page is empty (a load fault, not an empty day); '
                'the boost check-in was not attempted',
                verification='empty_boost',
                balance_before=_latest_balance(streak),
            )
        else:
            boost = claim_boost_checkin(
                controller,
                capture,
                corpus,
                state,
                authorization=authorization,
                snapshot=opened.snapshot,
                templates=templates,
                timeout=timeout,
                interval=interval,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=log,
            )

    before = _first_balance(streak, boost)
    latest = _latest_balance(streak, boost)
    after = before if latest is None else latest
    stop = REDEEM_INTERSTITIAL if boost.interstitial == REDEEM_INTERSTITIAL else None
    return SigninReport(
        streak=streak,
        boost=boost,
        route=route,
        balance_before=before,
        balance_after=after,
        stop=stop,
    )


# The shorter name reads naturally at the run loop: claim both daily sign-ins.
claim_signins = claim_daily_signins


def _prepare_entry(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    frame: Snapshot,
    *,
    templates: TemplateStore | None,
    timeout: float,
    interval: float,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
) -> tuple[Snapshot, bool]:
    """Resolve a loading page or popup before looking for a claim control.

    A popup is swept through the same dismissal loop. A loading frame is polled
    at the caller's interval. Both are bounded by ``timeout``; if the frame is
    still not settled the caller must not tap it. Returns the last frame and
    whether it reached a settled state.
    """
    deadline = clock() + timeout
    classification = classify(frame)
    while classification.state in (ScreenState.LOADING, ScreenState.POPUP):
        now = clock()
        if now >= deadline:
            return frame, False
        if classification.state is ScreenState.POPUP:
            report = dismiss_after(
                controller,
                capture,
                corpus,
                templates=templates,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=logger,
            )
            frame = report.frames[-1].snapshot
        else:
            sleep(min(interval, max(0.0, deadline - now)))
            frame = _capture(capture)
        classification = classify(frame)
    return frame, True


def _streak_rows(snapshot: Snapshot) -> tuple[Row, ...]:
    """Rows in the streak card headed by ``Daily coins``, in reading order.

    The next task row starts a new section, so the card never absorbs the task
    list below it. A marker-relative band is the second bound: it keeps a later
    row that happens to read ``Claimed`` from proving a claim it did not make.

    The header row is part of the card, not just its title. The card grew taller
    than the derived row tolerance (``Today`` / day chips / ``Worth C$0.01``),
    so on the measured 2026-11-23 frame the ``Collect`` control sits in the same
    OCR band as ``Daily coins`` (issue #84). Searching only the rows below the
    header was what made ``find_streak_control`` return ``None`` on the real
    coin page. The task controls are still excluded: matching is against
    :data:`STREAK_CONTROL_LABELS`, so the neighbouring ``GO`` never qualifies.
    """
    rows = group_rows(snapshot.boxes, snapshot.geometry)
    for section in group_sections(rows, header_labels=(STREAK_MARKER, *TASK_TITLES)):
        header = section.header
        if header is None or not _row_has(header, (STREAK_MARKER,)):
            continue
        limit = header.top + header.height * _STREAK_CARD_HEIGHT_RATIO
        card: list[Row] = [header]
        for row in section.rows:
            if row.center_y > limit or _is_task_row(row):
                break
            card.append(row)
        return tuple(card)
    return ()


def _streak_visible(snapshot: Snapshot) -> bool:
    """Whether the frame shows the coin page or the streak card.

    Used as a wait predicate, not as proof of a claim: it answers "has the app
    finished leaving the previous surface", which is the difference between a
    control that is absent and one that has not rendered yet (#84).
    """
    return bool(_streak_rows(snapshot)) or all(contains_text(snapshot, marker) for marker in COIN_PAGE_MARKERS)


def _is_task_row(row: Row) -> bool:
    """Whether a row belongs to the task list rather than the streak card."""
    return _row_has(row, TASK_TITLES) or find_action_in_row(row) is not None


def _verify_streak_until(
    capture: CaptureSource,
    before: float | None,
    *,
    timeout: float,
    interval: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[str, BalanceReading] | None:
    """Capture until the streak claim is proven, or return ``None`` at timeout."""
    deadline = clock() + timeout
    while True:
        frame = _capture(capture)
        after = read_balance(frame)
        if contains_text(frame, REDEEM_MARKER):
            return 'interstitial', after
        rows = _streak_rows(frame)
        if rows:
            control = find_streak_control(frame)
            if control is None:
                return 'absent_control', after
            if _rows_have(rows, STREAK_CLAIMED_LABELS):
                return 'claimed_marker', after
        if before is not None and after.claim_value is not None and after.claim_value > before:
            return 'balance_delta', after
        now = clock()
        if now >= deadline:
            return None
        sleep(min(interval, max(0.0, deadline - now)))


def _verify_boost_until(
    capture: CaptureSource,
    before: float | None,
    *,
    timeout: float,
    interval: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[str, BalanceReading] | None:
    """Capture until the boost claim is proven, or return ``None`` at timeout."""
    deadline = clock() + timeout
    while True:
        frame = _capture(capture)
        after = read_balance(frame)
        if contains_text(frame, REDEEM_MARKER):
            return 'interstitial', after
        row, action = find_boost_control(frame)
        if row is None:
            # A still-loaded boost page, or the coin page whose inline row is
            # now gone, is proof the row was claimed. Any other page is not.
            if is_boost_page(frame) or all(contains_text(frame, marker) for marker in COIN_PAGE_MARKERS):
                return 'absent_boost_row', after
        elif action is None or _row_has(row, BOOST_CLAIMED_LABELS):
            return 'claimed_marker', after
        if before is not None and after.claim_value is not None and after.claim_value > before:
            return 'balance_delta', after
        now = clock()
        if now >= deadline:
            return None
        sleep(min(interval, max(0.0, deadline - now)))


def _sweep(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    *,
    templates: TemplateStore | None,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
) -> tuple[DismissReport | None, str | None]:
    """Run the one dismissal loop, treating the redeem interstitial as success.

    Any other unclosable popup is re-raised: this executor has no safe way to
    continue past an unidentified overlay. The redeem interstitial is named in
    the corpus as deliberately unclosable, and it appears only after the coins
    have been taken, so it is returned to the caller and recorded as the claim's
    terminal evidence.
    """
    try:
        report = dismiss_after(
            controller,
            capture,
            corpus,
            templates=templates,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=logger,
        )
    except PopupUnclosable:
        frame = _capture(capture)
        if contains_text(frame, REDEEM_MARKER):
            return None, REDEEM_INTERSTITIAL
        raise
    return report, None


def _without_boost_family(corpus: PopupCorpus) -> PopupCorpus:
    """Remove the family whose anchor is the boost destination itself."""
    families = tuple(family for family in corpus.families if family.anchor != BOOST_MARKER)
    if len(families) == len(corpus.families):
        return corpus
    return replace(corpus, families=families)


def _already_done(key: str, label: str) -> ClaimResult:
    return ClaimResult(
        key=key,
        outcome=SigninOutcome.ALREADY_DONE,
        detail=f'{label!r} is recorded done for today; skipped before any capture or tap',
        verification='state',
    )


def _frame(capture: CaptureSource, snapshot: Snapshot | None) -> Snapshot:
    return snapshot if snapshot is not None else _capture(capture)


def _capture(source: CaptureSource) -> Snapshot:
    if callable(source):
        return source()
    return source.capture()


def _claim_balance(before: float | None, after: float | None) -> float | None:
    """The balance a claim may record: never an unreadable or dropped value.

    A claimed reward cannot reduce the balance, so an ``after`` below ``before``
    is a misread from a non-coin post-claim surface rather than news. An
    unreadable ``after`` keeps the balance the claim started from. A real
    increase (the ``balance_delta`` verdict) still wins.

    A zero is trustworthy only when the claim started from a readable zero: a
    lone ``0`` on a redacted or post-claim surface is a control numeral, not the
    balance. Callers pass a baseline from :class:`BalanceReading.claim_value`,
    which already refuses a zero from a surface that cannot prove it. That is
    why ``_claim_balance(None, 0.0)`` is ``None`` while ``_claim_balance(0.0,
    0.0)`` keeps a validated new account's genuine zero.
    """
    if after is None:
        return before
    if after == 0.0 and before != 0.0:
        return before
    if before is not None and after < before:
        return before
    return after


def _balance_code(reading: BalanceReading, recorded: float | None) -> str | None:
    """Name why a claim did not simply record the reading, or ``None`` when it did."""
    if reading.code is not None:
        return reading.code
    if reading.value == 0.0 and recorded != reading.value:
        return BALANCE_ZERO_UNVERIFIED
    return None


def _recorded_verified(
    before: float | None,
    before_reading: BalanceReading,
    after: float | None,
    after_reading: BalanceReading,
    recorded: float | None,
) -> bool:
    """Whether the value being stored came from a trusted coin-page reading."""
    if recorded is None:
        return False
    if after is not None and recorded == after and after_reading.trusted:
        return True
    return bool(before is not None and recorded == before and before_reading.trusted)


def _log_balance(
    logger: logging.Logger,
    *,
    key: str,
    code: str,
    phase: str,
    reading: BalanceReading,
    before: float | None = None,
    after: float | None = None,
    recorded: float | None = None,
) -> None:
    """Record a balance that was unknown or refused, with the frame's reason."""
    logger.warning(
        '%s',
        json.dumps(
            {
                'event': code,
                'code': code,
                'task': key,
                'phase': phase,
                'surface': reading.surface.value,
                'balance_read': reading.value,
                'balance_before': before,
                'balance_after': after,
                'balance_recorded': recorded,
                'detail': (
                    'the frame did not render a bare number in the balance band'
                    if code == BALANCE_UNREADABLE
                    else 'the frame read 0 without a validated coin-page surface; the previous balance is kept'
                ),
            },
            separators=(',', ':'),
            sort_keys=True,
        ),
    )


def _first_balance(*results: ClaimResult) -> float | None:
    for result in results:
        if result.balance_before is not None:
            return result.balance_before
    return None


def _latest_balance(*results: ClaimResult) -> float | None:
    latest: float | None = None
    for result in results:
        if result.balance_after is not None:
            latest = result.balance_after
    return latest


def _row_has(row: Row, labels: Sequence[str]) -> bool:
    return any(similarity(label, box.text) >= DEFAULT_MIN_SCORE for label in labels for box in row.boxes)


def _rows_have(rows: Sequence[Row], labels: Sequence[str]) -> bool:
    return any(_row_has(row, labels) for row in rows)


def _log_result(logger: logging.Logger, result: ClaimResult) -> None:
    logger.info(
        '%s',
        json.dumps({'event': 'signin_claim', **result.as_dict()}, separators=(',', ':'), sort_keys=True),
    )
