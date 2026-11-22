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
from aliexpress_coins.perception.state import CaptureSource
from aliexpress_coins.perception.templates import TemplateStore
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks.discovery import DiscoveryRoute, open_boost_list
from aliexpress_coins.tasks.surface import BOOST_MARKER, REDEEM_MARKER, contains_text, has_task_rows, is_boost_page

__all__ = [
    'BALANCE_BOTTOM_FRACTION',
    'BALANCE_TOP_FRACTION',
    'BOOST_CLAIMED_LABELS',
    'BOOST_KEY',
    'BOOST_ROW_LABELS',
    'DEFAULT_VERIFY_INTERVAL',
    'DEFAULT_VERIFY_TIMEOUT',
    'REDEEM_INTERSTITIAL',
    'STREAK_CLAIMED_LABELS',
    'STREAK_CONTROL_LABELS',
    'STREAK_KEY',
    'STREAK_MARKER',
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
    'read_coin_balance',
]

STREAK_KEY = 'daily_checkin'
"""The catalogue key of the streak ``Collect`` claim."""

BOOST_KEY = 'boost_checkin'
"""The catalogue key of the boost page's ``Daily check-in`` row."""

STREAK_MARKER = 'Daily coins'
"""The section marker above the streak card (docs/06 §3)."""

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

DEFAULT_VERIFY_TIMEOUT = 8.0
"""How long to wait for a fresh frame to prove a claim before calling it unverified."""

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

_BARE_NUMBER = re.compile(r'^\s*(\d{1,3}(?:,\d{3})*|\d+)\s*$')


class SigninOutcome(StrEnum):
    """What one sign-in claim actually did."""

    COMPLETED = 'completed'
    ALREADY_DONE = 'already done today'
    UNAVAILABLE = 'unavailable'
    UNVERIFIED = 'unverified'


# A readable alias: both names mean the same four outcomes.
ClaimOutcome = SigninOutcome


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
    """Read the coin balance when the frame renders it, else ``None``.

    The measured rule is the only bare number in the top quarter of the coin
    page (docs/01-recon-findings.md §3). The status-bar band is excluded, and a
    currency amount or reward badge is not a bare number. Masked or unreadable
    balances return ``None`` rather than a guess; verification then relies on
    the control and the card state.
    """
    top = snapshot.height * BALANCE_TOP_FRACTION
    bottom = snapshot.height * BALANCE_BOTTOM_FRACTION
    for box in sorted(snapshot.boxes, key=lambda item: (item.bbox[1], item.bbox[0])):
        if box.score < DEFAULT_MIN_OCR_SCORE:
            continue
        if not top <= box.center[1] <= bottom:
            continue
        match = _BARE_NUMBER.match(box.text)
        if match is not None:
            return float(match.group(1).replace(',', ''))
    return None


def claim_streak(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    state: DailyStateStore,
    *,
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
    log = logger if logger is not None else logging.getLogger(LOGGER_NAME)
    if state.done_today(STREAK_KEY):
        return _already_done(STREAK_KEY, STREAK_MARKER)
    if timeout <= 0:
        raise ValueError(f'timeout must be positive, got {timeout!r}')
    if interval <= 0:
        raise ValueError(f'interval must be positive, got {interval!r}')

    frame = _frame(capture, snapshot)
    control = find_streak_control(frame)
    if control is None:
        result = ClaimResult(
            key=STREAK_KEY,
            outcome=SigninOutcome.UNAVAILABLE,
            detail=f'{STREAK_MARKER!r} has no Collect/Claim/Sign in control in this frame',
            verification='not_found',
            balance_before=read_coin_balance(frame),
        )
        _log_result(log, result)
        return result

    before = read_coin_balance(frame)
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
        state.record(STREAK_KEY, balance=before)
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
        verification, after = verdict
        state.record(STREAK_KEY, balance=after if after is not None else before)
        result = ClaimResult(
            key=STREAK_KEY,
            outcome=SigninOutcome.COMPLETED,
            detail=f'streak claim verified by {verification.replace("_", " ")}',
            verification=verification,
            balance_before=before,
            balance_after=after,
            control=control,
            tap=tap,
            dismiss=dismiss,
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
    log = logger if logger is not None else logging.getLogger(LOGGER_NAME)
    if state.done_today(BOOST_KEY):
        return _already_done(BOOST_KEY, BOOST_ROW_LABELS[0])
    if timeout <= 0:
        raise ValueError(f'timeout must be positive, got {timeout!r}')
    if interval <= 0:
        raise ValueError(f'interval must be positive, got {interval!r}')

    frame = _frame(capture, snapshot)
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

    before = read_coin_balance(frame)
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
        state.record(BOOST_KEY, balance=before)
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
        verification, after = verdict
        state.record(BOOST_KEY, balance=after if after is not None else before)
        result = ClaimResult(
            key=BOOST_KEY,
            outcome=SigninOutcome.COMPLETED,
            detail=f'boost check-in verified by {verification.replace("_", " ")}',
            verification=verification,
            balance_before=before,
            balance_after=after,
            control=action,
            tap=tap,
            dismiss=dismiss,
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
    snapshot: Snapshot | None = None,
    templates: TemplateStore | None = None,
    timeout: float = DEFAULT_VERIFY_TIMEOUT,
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
    """
    log = logger if logger is not None else logging.getLogger(LOGGER_NAME)
    streak = claim_streak(
        controller,
        capture,
        corpus,
        state,
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
            logger=log,
        )
        route = opened.route
        boost = claim_boost_checkin(
            controller,
            capture,
            corpus,
            state,
            snapshot=opened.snapshot,
            templates=templates,
            timeout=timeout,
            interval=interval,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=log,
        )

    before = streak.balance_before
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


def _streak_rows(snapshot: Snapshot) -> tuple[Row, ...]:
    """Rows in the section headed by ``Daily coins``, in reading order."""
    rows = group_rows(snapshot.boxes, snapshot.geometry)
    for section in group_sections(rows, header_labels=(STREAK_MARKER,)):
        if section.header is None:
            continue
        if _row_has(section.header, (STREAK_MARKER,)):
            return section.rows
    return ()


def _verify_streak_until(
    capture: CaptureSource,
    before: float | None,
    *,
    timeout: float,
    interval: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[str, float | None] | None:
    """Capture until the streak claim is proven, or return ``None`` at timeout."""
    deadline = clock() + timeout
    while True:
        frame = _capture(capture)
        after = read_coin_balance(frame)
        rows = _streak_rows(frame)
        if rows:
            control = find_streak_control(frame)
            if control is None:
                return 'absent_control', after
            if _rows_have(rows, STREAK_CLAIMED_LABELS):
                return 'claimed_marker', after
        if before is not None and after is not None and after > before:
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
) -> tuple[str, float | None] | None:
    """Capture until the boost claim is proven, or return ``None`` at timeout."""
    deadline = clock() + timeout
    while True:
        frame = _capture(capture)
        after = read_coin_balance(frame)
        row, action = find_boost_control(frame)
        if row is None:
            # The row normally disappears; the page itself staying loaded is
            # what makes the absence meaningful rather than a stray capture.
            if is_boost_page(frame) or has_task_rows(frame):
                return 'absent_boost_row', after
        elif action is None or _row_has(row, BOOST_CLAIMED_LABELS):
            return 'claimed_marker', after
        if before is not None and after is not None and after > before:
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
