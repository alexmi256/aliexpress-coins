"""The browse-and-wait executor: one page, one timer chip, one verification (#24).

Most paying tasks in the roster share a shape: open a page, wait out a dwell
requirement, collect if the page offers a control, come back, and verify that the
badge moved. The sponsored-items, recently-viewed, check-in-to-win and
learn-to-save families are all that shape, so they share this executor and only
the catalogue's ``wait_seconds`` differs.

The measured contract is the timer chip, not a fixed sleep
(docs/05-app-knowledge.md §5):

* a floating chip at the **right edge** of the page shows a red ``Ns`` pill while
  the dwell requirement runs, then a green pill with a check mark;
* OCR reads ``12s``, ``1s`` and then no digits, so the wait is a **poll**: look
  for ``\\b\\d+s\\b`` in the right-hand band, and proceed when it disappears;
* the timer starts during page load, so the first reading can be well below the
  catalogue's bound. ``wait_seconds`` therefore bounds the poll; it is never the
  sleep. A chip that never appears is a finding carried in the report, not a
  reason to wait longer.

Three rules keep the executor safe while it is on an ordinary shopping page:

* **the deny list never leaves the dispatch point** — this module only ever taps
  the row's own action (re-resolved from the current frame), a popup control, or
  a control whose label is an explicit ``Collect``/``Claim`` variant. It never
  scans for ``Buy``, ``Add to cart`` or a product tile;
* **no remembered point** — the action is found with
  :func:`~aliexpress_coins.perception.layout.find_action_in_row` after the row is
  located by its title in the frame that is about to be tapped;
* **the screen is the authority** — a tap is not a completion. The badge must
  advance, the row must disappear from a *trusted* task surface, or the row must
  read as completed. A tap that changed nothing is ``failed``.

The run always leaves the app on the coins page. The destination page's back
stack is not trustworthy (two BACK presses leave the app), so the return is the
measured shortcut route (docs/06-navigation-map.md §1), followed by a poll for
the coin-page markers. The same route is fired again after the verification
surface has been read.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from aliexpress_coins.control.controller import LOGGER_NAME, Controller, TapRecord
from aliexpress_coins.control.errors import PopupUnclosable
from aliexpress_coins.control.popups import DismissReport, PopupCorpus, dismiss_after
from aliexpress_coins.perception.errors import StateTimeout
from aliexpress_coins.perception.layout import (
    ProgressBadge,
    Row,
    find_action_in_row,
    find_progress_badge,
    group_rows,
    row_of,
)
from aliexpress_coins.perception.matching import (
    DEFAULT_MIN_OCR_SCORE,
    DEFAULT_MIN_SCORE,
    find_all,
    find_text,
    normalize,
    similarity,
)
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.state import CaptureSource, ScreenState, classify
from aliexpress_coins.perception.templates import TemplateStore
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks.catalogue import AllowedTaskSpec, TaskCategory
from aliexpress_coins.tasks.discovery import BoostList, DiscoveryRoute, RosterEntry, open_boost_list
from aliexpress_coins.tasks.errors import TaskError
from aliexpress_coins.tasks.surface import (
    COIN_PAGE_MARKERS,
    REDEEM_MARKER,
    RouteFirer,
    SurfaceRoute,
    classify_surface,
    contains_text,
    has_task_rows,
    is_boost_page,
    is_empty_boost,
)

__all__ = [
    'COLLECT_LABELS',
    'DONE_LABELS',
    'MAX_PRESSES',
    'REDEEM_INTERSTITIAL',
    'TIMER_CHIP_BOTTOM_FRACTION',
    'TIMER_CHIP_RIGHT_FRACTION',
    'TIMER_CHIP_TOP_FRACTION',
    'Arrival',
    'BrowseOutcome',
    'BrowseResult',
    'ChipObservation',
    'ChipReading',
    'ChipStatus',
    'ChipWait',
    'FrameEvidence',
    'ReturnResult',
    'find_collect_control',
    'find_task_row',
    'find_timer_chip',
    'return_to_coins_page',
    'run_browse_task',
    'wait_for_timer_chip',
]

MAX_PRESSES = 3
"""The most times one task page may be entered before the executor gives up.

A task that needs a second tap exists; a task that needs five is a broken page.
The budget counts entry presses only. Popup closes, the optional ``Collect`` tap
and the navigation back are separate and each bounded on its own.
"""

DEFAULT_LOAD_TIMEOUT = 15.0
"""How long the task surface may spend loading before the executor stops waiting."""

DEFAULT_ARRIVAL_TIMEOUT = 10.0
"""How long one press may take to leave the task list for the destination page."""

DEFAULT_LOAD_INTERVAL = 0.5
"""How often capture is polled while a page is loading."""

DEFAULT_CHIP_INTERVAL = 0.5
"""How often the timer chip is polled while its digits are visible."""

DEFAULT_RETURN_TIMEOUT = 20.0
"""How long one shortcut re-entry may take to show the coin-page markers."""

DEFAULT_RETURN_INTERVAL = 0.5
"""How often the return route is polled."""

DEFAULT_RETURN_ATTEMPTS = 2
"""The bounded number of shortcut re-entries spent trying to reach the coin page."""

TIMER_CHIP_RIGHT_FRACTION = 0.72
"""The left edge of the right-hand band that may hold the timer chip.

The measured chip sits at ``x ≈ 0.97 · width`` (docs/05 §5). The band is wide
enough for the digits box rather than the pill's artwork, and is a fraction of
the frame so it ports to another geometry.
"""

TIMER_CHIP_TOP_FRACTION = 0.12
TIMER_CHIP_BOTTOM_FRACTION = 0.88
"""The vertical band a timer chip may occupy, excluding the system-UI bars.

The measured chip is mid-height. The system bars carry a clock and a battery
percentage, and neither is a dwell indicator.
"""

COLLECT_LABELS: tuple[str, ...] = (
    'Collect',
    'Collect now',
    'Collect reward',
    'Claim',
    'Claim reward',
)
"""The controls a browse page may offer after the dwell requirement is met.

Only these labels are tapped on the destination page. A generic action word is
deliberately absent: the deny policy guards the task list, but the destination
is an ordinary shopping surface and this module must not learn to press a
purchase control because it happens to sit next to a ``Collect`` button.
"""

DONE_LABELS: tuple[str, ...] = ('Claimed', 'Collected', 'Completed', 'Done', 'Finished')
"""Wording a row uses after the app considers it finished."""

MAX_COLLECT_WIDTH_FRACTION = 0.35
MAX_COLLECT_HEIGHT_FRACTION = 0.12
"""A collect *control* is a button, not a full-width banner.

The coupon page's ``Collect all: C$…`` bar is deliberately excluded: it is not a
browse-task payout and a third of the frame is already wider than any measured
task control.
"""

REDEEM_INTERSTITIAL = 'redeem_interstitial'
"""The corpus family name of the unclosable reward interstitial."""

_TIMER_DIGITS = re.compile(r'^\s*(?P<seconds>\d{1,3})\s*s\s*$', re.IGNORECASE)
"""OCR reads the running chip as ``12s``; case and inner spaces are ignored."""

_CHECK_TOKENS = frozenset({'check'})


class BrowseOutcome(StrEnum):
    """What one browse-task attempt actually did."""

    COMPLETED = 'completed'
    FAILED = 'failed'
    SKIPPED = 'skipped'


class ChipStatus(StrEnum):
    """How the timer-chip poll ended."""

    CLEARED = 'cleared'
    ALREADY_CLEAR = 'already_clear'
    ABSENT = 'absent'
    TIMED_OUT = 'timed_out'
    NOT_WAITED = 'not_waited'
    NOT_ARRIVED = 'not_arrived'


@dataclass(frozen=True, slots=True)
class ChipReading:
    """One OCR read of the right-edge dwell chip."""

    text: str | None
    countdown: int | None
    checked: bool
    box: TextBox | None = None

    @property
    def running(self) -> bool:
        """Whether the chip is counting down (a ``Ns`` pill)."""
        return self.countdown is not None

    @property
    def clear(self) -> bool:
        """Whether the chip reads as the green check state."""
        return self.countdown is None and self.checked

    def as_dict(self) -> dict[str, object]:
        """Render the reading for the run report."""
        record: dict[str, object] = {
            'text': self.text,
            'countdown': self.countdown,
            'checked': self.checked,
        }
        if self.box is not None:
            record['box'] = list(self.box.bbox)
            record['target'] = list(self.box.center)
        return record


@dataclass(frozen=True, slots=True)
class ChipObservation:
    """One poll of the timer chip."""

    elapsed: float
    reading: ChipReading | None

    def as_dict(self) -> dict[str, object]:
        """Render the observation for the run report."""
        return {
            'elapsed': self.elapsed,
            'reading': None if self.reading is None else self.reading.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class ChipWait:
    """The whole timer-chip poll, including what ended it and when."""

    status: ChipStatus
    observations: tuple[ChipObservation, ...] = ()
    elapsed: float = 0.0
    bound: float = 0.0
    initial: ChipReading | None = None
    final: ChipReading | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'observations', tuple(self.observations))

    @property
    def seen(self) -> bool:
        """Whether a running countdown was observed at least once."""
        return any(item.reading is not None and item.reading.running for item in self.observations)

    @property
    def ready(self) -> bool:
        """Whether the chip positively reported the dwell requirement met."""
        return self.status in (ChipStatus.CLEARED, ChipStatus.ALREADY_CLEAR)

    def as_dict(self) -> dict[str, object]:
        """Render the poll for ``run.json``."""
        return {
            'status': self.status.value,
            'seen': self.seen,
            'ready': self.ready,
            'elapsed': self.elapsed,
            'bound': self.bound,
            'initial': None if self.initial is None else self.initial.as_dict(),
            'final': None if self.final is None else self.final.as_dict(),
            'observations': [item.as_dict() for item in self.observations],
        }


@dataclass(frozen=True, slots=True)
class FrameEvidence:
    """What one frame showed, kept for a failure report.

    The image itself belongs to the artifact store (#31). This is the text a
    human needs to understand why a verdict was reached: the surface variant, the
    row (if any), its badge, its action, and the labels OCR returned.
    """

    surface: str
    row: str | None
    badge: str | None
    action: str | None
    labels: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, 'labels', tuple(self.labels))

    def as_dict(self) -> dict[str, object]:
        """Render the evidence for the run report."""
        return {
            'surface': self.surface,
            'row': self.row,
            'badge': self.badge,
            'action': self.action,
            'labels': list(self.labels),
        }


@dataclass(frozen=True, slots=True)
class Arrival:
    """Whether a tap left the task list and reached the destination page."""

    snapshot: Snapshot
    arrived: bool
    on_task_list: bool
    on_coin_page: bool
    elapsed: float
    history: tuple[str, ...] = ()
    dismiss: DismissReport | None = None
    stop: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'history', tuple(self.history))

    def as_dict(self) -> dict[str, object]:
        """Render the arrival evidence for the run report."""
        record: dict[str, object] = {
            'arrived': self.arrived,
            'on_task_list': self.on_task_list,
            'on_coin_page': self.on_coin_page,
            'elapsed': self.elapsed,
            'history': list(self.history),
            'stop': self.stop,
        }
        if self.dismiss is not None:
            record['dismiss'] = self.dismiss.as_dict()
        return record


@dataclass(frozen=True, slots=True)
class ReturnResult:
    """The outcome of the guaranteed return to the coins page."""

    returned: bool
    reentered: bool
    attempts: int
    route: SurfaceRoute
    elapsed: float
    snapshot: Snapshot | None = None
    navigations: tuple[SurfaceRoute, ...] = ()
    history: tuple[str, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'navigations', tuple(self.navigations))
        object.__setattr__(self, 'history', tuple(self.history))

    def as_dict(self) -> dict[str, object]:
        """Render the return evidence for the run report."""
        return {
            'returned': self.returned,
            'reentered': self.reentered,
            'attempts': self.attempts,
            'route': self.route.value,
            'elapsed': self.elapsed,
            'navigations': [item.value for item in self.navigations],
            'history': list(self.history),
            'error': self.error,
        }


@dataclass(frozen=True, slots=True)
class BrowseResult:
    """One browse-task attempt, the evidence behind it and whether state changed.

    ``verification`` names the proof: ``progress_advanced``, ``progress_complete``,
    ``row_gone``, ``action_completed``, ``action_gone``, ``daily_limit``,
    ``row_complete``, ``row_vanished``, ``no_change``, ``no_observation``,
    ``return_failed``, ``redeem_interstitial`` or ``not_settled``. ``detail`` is
    the report-safe sentence.
    """

    key: str
    title: str
    outcome: BrowseOutcome
    verification: str
    detail: str
    limit: int
    done_today_before: int
    done_today_after: int
    wait_seconds: float
    progress_before: ProgressBadge | None = None
    progress_after: ProgressBadge | None = None
    before: FrameEvidence | None = None
    after: FrameEvidence | None = None
    presses: int = 0
    taps: tuple[TapRecord, ...] = ()
    collect: TapRecord | None = None
    chip: ChipWait | None = None
    arrival: Arrival | None = None
    return_state: ReturnResult | None = None
    dismissals: tuple[DismissReport, ...] = ()
    stop: str | None = None
    recorded: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, 'taps', tuple(self.taps))
        object.__setattr__(self, 'dismissals', tuple(self.dismissals))

    @property
    def completed(self) -> bool:
        """Whether the task was verified and the completion recorded."""
        return self.outcome is BrowseOutcome.COMPLETED

    @property
    def failed(self) -> bool:
        """Whether the screen did not prove the task."""
        return self.outcome is BrowseOutcome.FAILED

    @property
    def skipped(self) -> bool:
        """Whether the executor did no work because the spec or the app says stop."""
        return self.outcome is BrowseOutcome.SKIPPED

    @property
    def badge_before(self) -> str | None:
        """The progress badge exactly as OCR read it before the task."""
        return self.progress_before.text if self.progress_before is not None else None

    @property
    def badge_after(self) -> str | None:
        """The progress badge exactly as OCR read it after the task."""
        return self.progress_after.text if self.progress_after is not None else None

    @property
    def badge_change(self) -> str | None:
        """The ``before -> after`` badge pair the acceptance report quotes."""
        before = self.badge_before
        after = self.badge_after
        if before is None and after is None:
            return None
        return f'{before if before is not None else "?"} -> {after if after is not None else "?"}'

    @property
    def returned(self) -> bool:
        """Whether the run finished on the coins page."""
        return self.return_state is not None and self.return_state.returned

    def as_dict(self) -> dict[str, object]:
        """Render the result for ``run.json`` without losing the evidence."""
        return {
            'key': self.key,
            'title': self.title,
            'outcome': self.outcome.value,
            'verification': self.verification,
            'detail': self.detail,
            'limit': self.limit,
            'done_today_before': self.done_today_before,
            'done_today_after': self.done_today_after,
            'wait_seconds': self.wait_seconds,
            'badge_before': self.badge_before,
            'badge_after': self.badge_after,
            'badge_change': self.badge_change,
            'before': None if self.before is None else self.before.as_dict(),
            'after': None if self.after is None else self.after.as_dict(),
            'presses': self.presses,
            'taps': [tap.as_dict() for tap in self.taps],
            'collect': None if self.collect is None else self.collect.as_dict(),
            'chip': None if self.chip is None else self.chip.as_dict(),
            'arrival': None if self.arrival is None else self.arrival.as_dict(),
            'return': None if self.return_state is None else self.return_state.as_dict(),
            'dismissals': [report.as_dict() for report in self.dismissals],
            'stop': self.stop,
            'recorded': self.recorded,
        }


@dataclass(frozen=True, slots=True)
class _Verdict:
    """The internal answer from one verification frame."""

    status: str
    decisive: bool
    detail: str
    badge: ProgressBadge | None = None
    evidence: FrameEvidence | None = None


def find_timer_chip(snapshot: Snapshot) -> ChipReading | None:
    """Read the right-edge dwell chip, or return ``None``.

    Only boxes inside the measured right-hand band and the content height are
    considered, so the status-bar clock and a product title that happens to read
    ``12s`` are not mistaken for the timer. A running ``Ns`` label wins over a
    check mark; the check is the *clear* state and has no digits.
    """
    top = snapshot.height * TIMER_CHIP_TOP_FRACTION
    bottom = snapshot.height * TIMER_CHIP_BOTTOM_FRACTION
    left = snapshot.width * TIMER_CHIP_RIGHT_FRACTION

    running: TextBox | None = None
    seconds: int | None = None
    checked: TextBox | None = None

    for box in snapshot.boxes:
        if box.score < DEFAULT_MIN_OCR_SCORE:
            continue
        centre_x, centre_y = box.center
        if centre_x < left or not top <= centre_y <= bottom:
            continue
        text = unicodedata.normalize('NFKC', box.text)
        match = _TIMER_DIGITS.match(text)
        if match is not None:
            candidate = int(match.group('seconds'))
            if running is None or box.bbox[2] > running.bbox[2]:
                running = box
                seconds = candidate
            continue
        if normalize(text) in _CHECK_TOKENS and (checked is None or box.bbox[2] > checked.bbox[2]):
            checked = box

    if running is not None:
        return ChipReading(text=running.text, countdown=seconds, checked=False, box=running)
    if checked is not None:
        return ChipReading(text=checked.text, countdown=None, checked=True, box=checked)
    return None


def wait_for_timer_chip(
    capture: CaptureSource,
    *,
    timeout: float,
    interval: float = DEFAULT_CHIP_INTERVAL,
    initial: Snapshot | None = None,
    is_destination: Callable[[Snapshot], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ChipWait:
    """Poll the timer chip until its digits vanish, bounded by ``timeout``.

    The first observation may be supplied as ``initial`` so the frame that proved
    the destination arrived is not captured twice. If that frame has no chip the
    poll returns ``absent`` immediately: a chip that never appears is a finding,
    not a reason to keep sleeping. ``is_destination`` lets the caller stop before
    a chip could exist (the tap never left the task list).

    The wait is ended by a *read*, not by a sleep: every iteration captures,
    classifies the chip and only then sleeps at most ``interval``.
    """
    if timeout < 0:
        raise ValueError(f'timeout must not be negative, got {timeout!r}')
    if interval <= 0:
        raise ValueError(f'interval must be positive, got {interval!r}')
    if timeout == 0:
        return ChipWait(status=ChipStatus.NOT_WAITED, bound=0.0)

    started = clock()
    deadline = started + timeout
    observations: list[ChipObservation] = []
    first: ChipReading | None = None
    final: ChipReading | None = None
    status = ChipStatus.ABSENT
    seen_running = False
    frame = initial if initial is not None else _capture(capture)

    while True:
        now = clock()
        elapsed = max(0.0, now - started)
        if is_destination is not None and not is_destination(frame):
            return ChipWait(
                status=ChipStatus.NOT_ARRIVED,
                observations=tuple(observations),
                elapsed=elapsed,
                bound=timeout,
                initial=first,
                final=final,
            )

        reading = find_timer_chip(frame)
        observations.append(ChipObservation(elapsed=elapsed, reading=reading))
        if first is None:
            first = reading
        final = reading

        if reading is not None and reading.running:
            seen_running = True
        elif seen_running:
            status = ChipStatus.CLEARED
            break
        elif reading is not None and reading.checked:
            status = ChipStatus.ALREADY_CLEAR
            break
        else:
            status = ChipStatus.ABSENT
            break

        if now >= deadline:
            status = ChipStatus.TIMED_OUT
            break

        sleep(min(interval, max(0.0, deadline - now)))
        frame = _capture(capture)

    return ChipWait(
        status=status,
        observations=tuple(observations),
        elapsed=max(0.0, clock() - started),
        bound=timeout,
        initial=first,
        final=final,
    )


def find_collect_control(snapshot: Snapshot) -> TextBox | None:
    """Return a small ``Collect``/``Claim`` control the page offers, if any.

    A control has to be a button-sized box. The coupon page's full-width
    ``Collect all`` banner is excluded by both dimensions, and a label that only
    appears as ordinary body text is not tapped.
    """
    best: TextBox | None = None
    best_key: tuple[float, float, float] | None = None
    for label in COLLECT_LABELS:
        for match in find_all(snapshot, label, min_score=DEFAULT_MIN_SCORE, min_ocr_score=DEFAULT_MIN_OCR_SCORE):
            box = match.box
            if box.width > snapshot.width * MAX_COLLECT_WIDTH_FRACTION:
                continue
            if box.height > snapshot.height * MAX_COLLECT_HEIGHT_FRACTION:
                continue
            key = (match.score, match.ocr_score, box.bbox[2])
            if best_key is None or key > best_key:
                best_key = key
                best = box
    return best


def find_task_row(snapshot: Snapshot, title: str) -> tuple[Row | None, TextBox | None, ProgressBadge | None]:
    """Find ``title``'s row and resolve its own action from the current frame.

    The row is located by its label, then the action is read with
    :func:`find_action_in_row`. A remembered button position would point at
    another task as soon as the roster reorders.
    """
    match = find_text(snapshot, title, min_score=DEFAULT_MIN_SCORE, min_ocr_score=DEFAULT_MIN_OCR_SCORE)
    if match is None:
        return None, None, None
    row = row_of(group_rows(snapshot.boxes, snapshot.geometry), match)
    if row is None:
        return None, None, None
    return row, find_action_in_row(row), find_progress_badge(row)


def return_to_coins_page(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    navigate: RouteFirer,
    *,
    snapshot: Snapshot | None = None,
    templates: TemplateStore | None = None,
    route: SurfaceRoute = SurfaceRoute.SHORTCUT,
    timeout: float = DEFAULT_RETURN_TIMEOUT,
    interval: float = DEFAULT_RETURN_INTERVAL,
    attempts: int = DEFAULT_RETURN_ATTEMPTS,
    settle: float = 0.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    logger: logging.Logger | None = None,
) -> ReturnResult:
    """Re-enter the coins page through the shortcut route and prove it arrived.

    The route is a *navigation* command, not a BACK press: two BACKs leave the
    app (docs/05 §5b). Each attempt fires the route once and polls for the two
    coin-page markers. A popup that appears on arrival is swept through the one
    dismissal loop; the redeem interstitial is accepted as an arrival because the
    coins page is what it covers.
    """
    if timeout <= 0:
        raise ValueError(f'timeout must be positive, got {timeout!r}')
    if interval <= 0:
        raise ValueError(f'interval must be positive, got {interval!r}')
    if attempts < 1:
        raise ValueError(f'attempts must be positive, got {attempts!r}')
    if settle < 0:
        raise ValueError(f'settle must not be negative, got {settle!r}')

    log = logger if logger is not None else logging.getLogger(LOGGER_NAME)
    started = clock()
    navigations: list[SurfaceRoute] = []
    history: list[str] = []
    current = snapshot if snapshot is not None else _capture(capture)
    if _on_coins_page(current):
        return ReturnResult(
            returned=True,
            reentered=False,
            attempts=0,
            route=route,
            elapsed=max(0.0, clock() - started),
            snapshot=current,
            history=(classify(current).state.value,),
        )

    error: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            navigate(route)
        except Exception as exc:  # the return is a guarantee; record and retry
            error = f'{type(exc).__name__}: {exc}'
            _warn(log, 'return_navigation_failed', attempt=attempt, detail=error)
            continue
        navigations.append(route)
        deadline = clock() + timeout

        while True:
            frame = _capture(capture)
            classification = classify(frame)
            history.append(classification.state.value)
            if _on_coins_page(frame):
                return ReturnResult(
                    returned=True,
                    reentered=True,
                    attempts=attempt,
                    route=route,
                    elapsed=max(0.0, clock() - started),
                    snapshot=frame,
                    navigations=tuple(navigations),
                    history=tuple(history),
                )
            if classification.state is ScreenState.POPUP:
                try:
                    report = dismiss_after(
                        controller,
                        capture,
                        corpus,
                        templates=templates,
                        settle=settle,
                        clock=clock,
                        sleep=sleep,
                        logger=log,
                    )
                except PopupUnclosable:
                    if contains_text(frame, REDEEM_MARKER):
                        return ReturnResult(
                            returned=True,
                            reentered=True,
                            attempts=attempt,
                            route=route,
                            elapsed=max(0.0, clock() - started),
                            snapshot=frame,
                            navigations=tuple(navigations),
                            history=tuple(history),
                        )
                    raise
                if report.frames and _on_coins_page(report.frames[-1].snapshot):
                    return ReturnResult(
                        returned=True,
                        reentered=True,
                        attempts=attempt,
                        route=route,
                        elapsed=max(0.0, clock() - started),
                        snapshot=report.frames[-1].snapshot,
                        navigations=tuple(navigations),
                        history=tuple(history),
                    )
            now = clock()
            if now >= deadline:
                break
            sleep(min(interval, max(0.0, deadline - now)))
        if settle:
            sleep(settle)

    return ReturnResult(
        returned=False,
        reentered=bool(navigations),
        attempts=attempts,
        route=route,
        elapsed=max(0.0, clock() - started),
        navigations=tuple(navigations),
        history=tuple(history),
        error=error,
    )


def run_browse_task(
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
    load_timeout: float = DEFAULT_LOAD_TIMEOUT,
    arrival_timeout: float = DEFAULT_ARRIVAL_TIMEOUT,
    load_interval: float = DEFAULT_LOAD_INTERVAL,
    chip_interval: float = DEFAULT_CHIP_INTERVAL,
    return_timeout: float = DEFAULT_RETURN_TIMEOUT,
    return_interval: float = DEFAULT_RETURN_INTERVAL,
    return_attempts: int = DEFAULT_RETURN_ATTEMPTS,
    settle: float = 0.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    logger: logging.Logger | None = None,
) -> BrowseResult:
    """Run one browse-and-wait task from an allowed roster row.

    The executor re-resolves the row's action from the current frame, presses it
    up to :data:`MAX_PRESSES` times with a re-check between presses, sweeps
    popups, polls the timer chip inside the catalogue's ``wait_seconds`` bound,
    taps one small ``Collect``/``Claim`` control if the page offers one, returns
    to the coins page through the shortcut route, and verifies the result on the
    strongest available signal.

    State is checked before the first capture. A task whose daily count has
    reached ``max_per_run`` is ``skipped`` before any tap. Only a verified
    completion is recorded, so an unchanged badge stays a failure the next run
    retries.
    """
    log = logger if logger is not None else logging.getLogger(LOGGER_NAME)
    spec = _allowed_spec(entry)
    limit = spec.max_per_run
    done_before = state.count(spec.key)
    if done_before >= limit:
        result = _skipped(
            spec,
            entry.title,
            done_before,
            limit,
            verification='daily_limit',
            detail=f'{spec.key!r} already completed {done_before} time(s) today; the limit is {limit}',
        )
        _log_result(log, result)
        return result

    if load_timeout <= 0:
        raise ValueError(f'load_timeout must be positive, got {load_timeout!r}')
    if arrival_timeout <= 0:
        raise ValueError(f'arrival_timeout must be positive, got {arrival_timeout!r}')
    if load_interval <= 0:
        raise ValueError(f'load_interval must be positive, got {load_interval!r}')
    if chip_interval <= 0:
        raise ValueError(f'chip_interval must be positive, got {chip_interval!r}')
    if return_timeout <= 0:
        raise ValueError(f'return_timeout must be positive, got {return_timeout!r}')
    if return_interval <= 0:
        raise ValueError(f'return_interval must be positive, got {return_interval!r}')
    if return_attempts < 1:
        raise ValueError(f'return_attempts must be positive, got {return_attempts!r}')
    if settle < 0:
        raise ValueError(f'settle must not be negative, got {settle!r}')

    frame = snapshot if snapshot is not None else _capture(capture)
    frame, settled, entry_sweep = _settle_surface(
        controller,
        capture,
        corpus,
        frame,
        templates=templates,
        timeout=load_timeout,
        interval=load_interval,
        settle=settle,
        clock=clock,
        sleep=sleep,
        logger=log,
    )
    dismissals: list[DismissReport] = []
    if entry_sweep is not None:
        dismissals.append(entry_sweep)
    before_evidence = _evidence(frame, entry.title)
    row, action, fresh_badge = find_task_row(frame, entry.title)
    progress_before = fresh_badge if fresh_badge is not None else entry.progress

    if not settled:
        return _finish_failed(
            controller,
            capture,
            corpus,
            navigate,
            spec,
            entry,
            done_before,
            limit,
            verification='not_settled',
            detail='the task surface never stopped loading; no action was tapped',
            before=before_evidence,
            progress_before=progress_before,
            dismissals=dismissals,
            route=route,
            templates=templates,
            return_timeout=return_timeout,
            return_interval=return_interval,
            return_attempts=return_attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=log,
        )

    if not _is_task_surface(frame):
        return _finish_failed(
            controller,
            capture,
            corpus,
            navigate,
            spec,
            entry,
            done_before,
            limit,
            verification='task_surface_unavailable',
            detail=f'{entry.title!r} is not on a task surface in this frame',
            before=before_evidence,
            progress_before=progress_before,
            dismissals=dismissals,
            route=route,
            templates=templates,
            return_timeout=return_timeout,
            return_interval=return_interval,
            return_attempts=return_attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=log,
        )

    if row is None or action is None:
        status = 'row_vanished' if row is None else 'row_complete'
        detail = (
            f'{entry.title!r} is absent from the frame; the roster entry is stale'
            if row is None
            else f'{entry.title!r} has no action left; the app already considers it finished'
        )
        result = _skipped(
            spec,
            entry.title,
            done_before,
            limit,
            verification=status,
            detail=detail,
            before=before_evidence,
            progress_before=progress_before,
        )
        _log_result(log, result)
        return result

    if progress_before is None:
        progress_before = entry.progress

    taps: list[TapRecord] = []
    presses = 0
    arrival: Arrival | None = None
    chip_wait = ChipWait(status=ChipStatus.NOT_WAITED)
    collect_tap: TapRecord | None = None
    stop: str | None = None
    current = frame

    try:
        while presses < MAX_PRESSES:
            _, press_action, _ = find_task_row(current, entry.title)
            if press_action is None:
                break
            taps.append(
                controller.tap_box(
                    press_action,
                    why=f'browse {spec.key}: open the task page (press {presses + 1} of {MAX_PRESSES})',
                    label=press_action.text,
                ),
            )
            presses += 1

            sweep, stop = _sweep(
                controller,
                capture,
                corpus,
                templates=templates,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=log,
            )
            if sweep is not None:
                dismissals.append(sweep)
            if stop is not None:
                break

            current = sweep.frames[-1].snapshot if sweep is not None and sweep.frames else _capture(capture)
            arrival = _wait_for_arrival(
                controller,
                capture,
                corpus,
                current,
                templates=templates,
                timeout=arrival_timeout,
                interval=load_interval,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=log,
            )
            current = arrival.snapshot
            if arrival.dismiss is not None:
                dismissals.append(arrival.dismiss)
            if arrival.stop is not None:
                stop = arrival.stop
                break
            if arrival.arrived or arrival.on_coin_page:
                break

        if stop is None and arrival is not None and arrival.arrived:
            chip_wait = wait_for_timer_chip(
                capture,
                timeout=max(0.0, spec.wait_seconds),
                interval=chip_interval,
                initial=arrival.snapshot,
                clock=clock,
                sleep=sleep,
            )
            if chip_wait.status is ChipStatus.TIMED_OUT:
                _warn(
                    log,
                    'browse_chip_timeout',
                    key=spec.key,
                    detail='the countdown was still running at the catalogue bound',
                )
            elif chip_wait.status is ChipStatus.ABSENT:
                _warn(
                    log,
                    'browse_chip_absent',
                    key=spec.key,
                    detail='the destination page showed no timer chip; verification decides',
                )

            if stop is None:
                sweep, stop = _sweep(
                    controller,
                    capture,
                    corpus,
                    templates=templates,
                    settle=settle,
                    clock=clock,
                    sleep=sleep,
                    logger=log,
                )
                if sweep is not None:
                    dismissals.append(sweep)
                current = sweep.frames[-1].snapshot if sweep is not None and sweep.frames else _capture(capture)

            if stop is None:
                control = find_collect_control(current)
                if control is not None:
                    collect_tap = controller.tap_box(
                        control,
                        why=f'browse {spec.key}: collect the reward the page offered',
                        label=control.text,
                    )
                    taps.append(collect_tap)
                    sweep, stop = _sweep(
                        controller,
                        capture,
                        corpus,
                        templates=templates,
                        settle=settle,
                        clock=clock,
                        sleep=sleep,
                        logger=log,
                    )
                    if sweep is not None:
                        dismissals.append(sweep)
                    current = sweep.frames[-1].snapshot if sweep is not None and sweep.frames else _capture(capture)

        return_state = return_to_coins_page(
            controller,
            capture,
            corpus,
            navigate,
            snapshot=current,
            templates=templates,
            route=route,
            timeout=return_timeout,
            interval=return_interval,
            attempts=return_attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=log,
        )
        if not return_state.returned:
            return _result(
                spec,
                entry.title,
                BrowseOutcome.FAILED,
                verification='return_failed',
                detail='the shortcut route did not prove the coins page; the run is not reported complete',
                limit=limit,
                done_before=done_before,
                done_after=state.count(spec.key),
                wait_seconds=spec.wait_seconds,
                progress_before=progress_before,
                before=before_evidence,
                presses=presses,
                taps=taps,
                collect=collect_tap,
                chip=chip_wait,
                arrival=arrival,
                return_state=return_state,
                dismissals=dismissals,
                stop=stop,
                logger=log,
            )

        if stop == REDEEM_INTERSTITIAL:
            return _result(
                spec,
                entry.title,
                BrowseOutcome.FAILED,
                verification='redeem_interstitial',
                detail='the unclosable reward interstitial appeared; the run stops without claiming a completion',
                limit=limit,
                done_before=done_before,
                done_after=state.count(spec.key),
                wait_seconds=spec.wait_seconds,
                progress_before=progress_before,
                before=before_evidence,
                presses=presses,
                taps=taps,
                collect=collect_tap,
                chip=chip_wait,
                arrival=arrival,
                return_state=return_state,
                dismissals=dismissals,
                stop=stop,
                logger=log,
            )

        verdict, verify_sweep, verify_return = _verify_completion(
            controller,
            capture,
            corpus,
            navigate,
            entry.title,
            progress_before,
            return_state.snapshot if return_state is not None else None,
            templates=templates,
            route=route,
            timeout=load_timeout,
            return_timeout=return_timeout,
            return_interval=return_interval,
            return_attempts=return_attempts,
            interval=load_interval,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=log,
        )
        if verify_sweep is not None:
            dismissals.append(verify_sweep)
        if verify_return is not None:
            verify_return = replace(
                verify_return,
                attempts=return_state.attempts + verify_return.attempts,
                navigations=return_state.navigations + verify_return.navigations,
                history=return_state.history + verify_return.history,
                elapsed=return_state.elapsed + verify_return.elapsed,
            )
            return_state = verify_return
            if not verify_return.returned:
                return _result(
                    spec,
                    entry.title,
                    BrowseOutcome.FAILED,
                    verification='return_failed',
                    detail='the verification surface was read but the run could not return to the coins page',
                    limit=limit,
                    done_before=done_before,
                    done_after=state.count(spec.key),
                    wait_seconds=spec.wait_seconds,
                    progress_before=progress_before,
                    progress_after=verdict.badge,
                    before=before_evidence,
                    after=verdict.evidence,
                    presses=presses,
                    taps=taps,
                    collect=collect_tap,
                    chip=chip_wait,
                    arrival=arrival,
                    return_state=verify_return,
                    dismissals=dismissals,
                    stop=stop,
                    logger=log,
                )

        if verdict.decisive:
            state.record(spec.key)
            return _result(
                spec,
                entry.title,
                BrowseOutcome.COMPLETED,
                verification=verdict.status,
                detail=verdict.detail,
                limit=limit,
                done_before=done_before,
                done_after=state.count(spec.key),
                wait_seconds=spec.wait_seconds,
                progress_before=progress_before,
                progress_after=verdict.badge,
                before=before_evidence,
                after=verdict.evidence,
                presses=presses,
                taps=taps,
                collect=collect_tap,
                chip=chip_wait,
                arrival=arrival,
                return_state=return_state,
                dismissals=dismissals,
                stop=stop,
                recorded=True,
                logger=log,
            )

        return _result(
            spec,
            entry.title,
            BrowseOutcome.FAILED,
            verification=verdict.status,
            detail=verdict.detail,
            limit=limit,
            done_before=done_before,
            done_after=state.count(spec.key),
            wait_seconds=spec.wait_seconds,
            progress_before=progress_before,
            progress_after=verdict.badge,
            before=before_evidence,
            after=verdict.evidence,
            presses=presses,
            taps=taps,
            collect=collect_tap,
            chip=chip_wait,
            arrival=arrival,
            return_state=return_state,
            dismissals=dismissals,
            stop=stop,
            logger=log,
        )
    except Exception:
        # A popup the corpus cannot close, or a navigation that raised, must not
        # leave the app on a shopping page. The original exception still wins.
        _return_quietly(
            controller,
            capture,
            corpus,
            navigate,
            templates=templates,
            route=route,
            timeout=return_timeout,
            interval=return_interval,
            attempts=return_attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=log,
        )
        raise


def _allowed_spec(entry: RosterEntry) -> AllowedTaskSpec:
    """Return the entry's allowed spec, refusing anything the dispatch point missed."""
    spec = entry.spec
    if not entry.allowed or spec is None or entry.dispatch_target is None:
        reason = entry.reason or 'no allowed action is exposed'
        raise TaskError(f'{entry.title!r} is not dispatchable: {reason}')
    if spec.category is not TaskCategory.BROWSE_WAIT:
        raise TaskError(f'{entry.title!r} is {spec.category.value!r}, not a browse_wait task')
    return spec


def _skipped(
    spec: AllowedTaskSpec,
    title: str,
    done: int,
    limit: int,
    *,
    verification: str,
    detail: str,
    before: FrameEvidence | None = None,
    progress_before: ProgressBadge | None = None,
) -> BrowseResult:
    return BrowseResult(
        key=spec.key,
        title=title,
        outcome=BrowseOutcome.SKIPPED,
        verification=verification,
        detail=detail,
        limit=limit,
        done_today_before=done,
        done_today_after=done,
        wait_seconds=spec.wait_seconds,
        progress_before=progress_before,
        before=before,
    )


def _finish_failed(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    navigate: RouteFirer,
    spec: AllowedTaskSpec,
    entry: RosterEntry,
    done_before: int,
    limit: int,
    *,
    verification: str,
    detail: str,
    before: FrameEvidence,
    progress_before: ProgressBadge | None,
    dismissals: Sequence[DismissReport],
    route: SurfaceRoute,
    templates: TemplateStore | None,
    return_timeout: float,
    return_interval: float,
    return_attempts: int,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
) -> BrowseResult:
    """End an early failure on the coins page and build its result."""
    return_state = return_to_coins_page(
        controller,
        capture,
        corpus,
        navigate,
        snapshot=None,
        templates=templates,
        route=route,
        timeout=return_timeout,
        interval=return_interval,
        attempts=return_attempts,
        settle=settle,
        clock=clock,
        sleep=sleep,
        logger=logger,
    )
    if not return_state.returned:
        verification = 'return_failed'
        detail = f'{detail}; the shortcut route did not prove the coins page'
    result = BrowseResult(
        key=spec.key,
        title=entry.title,
        outcome=BrowseOutcome.FAILED,
        verification=verification,
        detail=detail,
        limit=limit,
        done_today_before=done_before,
        done_today_after=done_before,
        wait_seconds=spec.wait_seconds,
        progress_before=progress_before,
        before=before,
        return_state=return_state,
        dismissals=tuple(dismissals),
    )
    _log_result(logger, result)
    return result


def _result(
    spec: AllowedTaskSpec,
    title: str,
    outcome: BrowseOutcome,
    *,
    verification: str,
    detail: str,
    limit: int,
    done_before: int,
    done_after: int,
    wait_seconds: float,
    progress_before: ProgressBadge | None,
    progress_after: ProgressBadge | None = None,
    before: FrameEvidence | None = None,
    after: FrameEvidence | None = None,
    presses: int = 0,
    taps: Sequence[TapRecord] = (),
    collect: TapRecord | None = None,
    chip: ChipWait | None = None,
    arrival: Arrival | None = None,
    return_state: ReturnResult | None = None,
    dismissals: Sequence[DismissReport] = (),
    stop: str | None = None,
    recorded: bool = False,
    logger: logging.Logger,
) -> BrowseResult:
    result = BrowseResult(
        key=spec.key,
        title=title,
        outcome=outcome,
        verification=verification,
        detail=detail,
        limit=limit,
        done_today_before=done_before,
        done_today_after=done_after,
        wait_seconds=wait_seconds,
        progress_before=progress_before,
        progress_after=progress_after,
        before=before,
        after=after,
        presses=presses,
        taps=tuple(taps),
        collect=collect,
        chip=chip,
        arrival=arrival,
        return_state=return_state,
        dismissals=tuple(dismissals),
        stop=stop,
        recorded=recorded,
    )
    _log_result(logger, result)
    return result


def _settle_surface(
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
) -> tuple[Snapshot, bool, DismissReport | None]:
    """Resolve loading and popups before a row is read or pressed."""
    deadline = clock() + timeout
    dismiss: DismissReport | None = None
    while True:
        state = classify(frame).state
        if state is ScreenState.LOADING:
            now = clock()
            if now >= deadline:
                return frame, False, dismiss
            sleep(min(interval, max(0.0, deadline - now)))
            frame = _capture(capture)
            continue
        if state is ScreenState.POPUP:
            now = clock()
            if now >= deadline:
                return frame, False, dismiss
            report, stop = _sweep(
                controller,
                capture,
                corpus,
                templates=templates,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=logger,
            )
            dismiss = report
            if stop is not None:
                return frame, False, dismiss
            frame = report.frames[-1].snapshot if report is not None and report.frames else _capture(capture)
            continue
        return frame, True, dismiss


def _wait_for_arrival(
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
) -> Arrival:
    """Poll until the task list is left for a settled destination page.

    A chip on the frame is definitive. Otherwise the task list is the boost page;
    the coin page is a bounced tap, not a destination. Every other settled page
    (including an unknown feed) is allowed to be the destination, because the
    executor must not need to understand the shopping surface to wait on it.
    """
    started = clock()
    deadline = started + timeout
    history: list[str] = []
    dismiss: DismissReport | None = None
    current = frame

    while True:
        classification = classify(current)
        history.append(classification.state.value)
        if find_timer_chip(current) is not None:
            return Arrival(
                snapshot=current,
                arrived=True,
                on_task_list=is_boost_page(current),
                on_coin_page=_on_coins_page(current),
                elapsed=max(0.0, clock() - started),
                history=tuple(history),
                dismiss=dismiss,
            )

        if classification.state is ScreenState.POPUP:
            report, stop = _sweep(
                controller,
                capture,
                corpus,
                templates=templates,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=logger,
            )
            dismiss = report
            if stop is not None:
                return Arrival(
                    snapshot=current,
                    arrived=False,
                    on_task_list=is_boost_page(current),
                    on_coin_page=_on_coins_page(current),
                    elapsed=max(0.0, clock() - started),
                    history=tuple(history),
                    dismiss=dismiss,
                    stop=stop,
                )
            current = report.frames[-1].snapshot if report is not None and report.frames else _capture(capture)
            continue

        if classification.state is not ScreenState.LOADING:
            on_list = is_boost_page(current)
            on_coin = _on_coins_page(current)
            if not on_list and not on_coin:
                return Arrival(
                    snapshot=current,
                    arrived=True,
                    on_task_list=False,
                    on_coin_page=False,
                    elapsed=max(0.0, clock() - started),
                    history=tuple(history),
                    dismiss=dismiss,
                )
            if on_coin:
                return Arrival(
                    snapshot=current,
                    arrived=False,
                    on_task_list=False,
                    on_coin_page=True,
                    elapsed=max(0.0, clock() - started),
                    history=tuple(history),
                    dismiss=dismiss,
                )

        now = clock()
        if now >= deadline:
            return Arrival(
                snapshot=current,
                arrived=False,
                on_task_list=is_boost_page(current),
                on_coin_page=_on_coins_page(current),
                elapsed=max(0.0, clock() - started),
                history=tuple(history),
                dismiss=dismiss,
            )
        sleep(min(interval, max(0.0, deadline - now)))
        current = _capture(capture)


def _verify_completion(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    navigate: RouteFirer,
    title: str,
    before: ProgressBadge | None,
    frame: Snapshot | None,
    *,
    templates: TemplateStore | None,
    route: SurfaceRoute,
    timeout: float,
    return_timeout: float,
    return_interval: float,
    return_attempts: int,
    interval: float,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
) -> tuple[_Verdict, DismissReport | None, ReturnResult | None]:
    """Read the row on the returned coin page, then re-open the list if needed.

    The coin page's inline row is an opportunistic positive signal: when it shows
    a changed badge the executor does not need to navigate again. Its *absence*
    is not proof, because the plain/shortcut coin page may render no task rows at
    all. In that case the boost list is re-opened and read; after that read the
    run returns to the coins page once more.
    """
    current = frame if frame is not None else _capture(capture)
    first = _verdict(current, title, before, trusted=False)
    if first.decisive:
        return first, None, None

    try:
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
        )
    except (StateTimeout, PopupUnclosable) as exc:
        final = return_to_coins_page(
            controller,
            capture,
            corpus,
            navigate,
            snapshot=None,
            templates=templates,
            route=route,
            timeout=return_timeout,
            interval=return_interval,
            attempts=return_attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=logger,
        )
        verdict = _Verdict(
            status='no_observation',
            decisive=False,
            detail=f'the boost list could not be read for verification: {exc}',
            badge=None,
            evidence=_evidence(current, title),
        )
        return verdict, None, final

    trusted = _trusted_surface(opened)
    verdict = _verdict(opened.snapshot, title, before, trusted=trusted)
    if verdict.status == 'no_change' and not trusted:
        verdict = _Verdict(
            status='no_observation',
            decisive=False,
            detail='the verification frame showed no task rows, so the row could not be checked',
            badge=verdict.badge,
            evidence=verdict.evidence,
        )

    if _on_coins_page(opened.snapshot):
        # The inline route was already on the coins page; no second entry.
        return verdict, opened.dismiss, None

    final = return_to_coins_page(
        controller,
        capture,
        corpus,
        navigate,
        snapshot=opened.snapshot,
        templates=templates,
        route=route,
        timeout=return_timeout,
        interval=return_interval,
        attempts=return_attempts,
        settle=settle,
        clock=clock,
        sleep=sleep,
        logger=logger,
    )
    return verdict, opened.dismiss, final


def _verdict(
    snapshot: Snapshot,
    title: str,
    before: ProgressBadge | None,
    *,
    trusted: bool,
) -> _Verdict:
    """Turn one frame into a completion verdict, strongest signal first."""
    row, action, badge = find_task_row(snapshot, title)
    evidence = _evidence(snapshot, title)

    if badge is not None and before is not None and badge.completed > before.completed:
        return _Verdict(
            status='progress_advanced',
            decisive=True,
            detail=f'{title!r} advanced from {before.text!r} to {badge.text!r}',
            badge=badge,
            evidence=evidence,
        )
    if badge is not None and before is None and badge.complete:
        return _Verdict(
            status='progress_complete',
            decisive=True,
            detail=f'{title!r} is complete at {badge.text!r}',
            badge=badge,
            evidence=evidence,
        )
    if row is None:
        # Absence is only proof on the full boost list. The inline coin-page
        # rows are a conditional subset, so a missing row there proves nothing.
        if trusted and is_boost_page(snapshot) and has_task_rows(snapshot):
            return _Verdict(
                status='row_gone',
                decisive=True,
                detail=f'{title!r} is gone from the populated boost list',
                badge=badge,
                evidence=evidence,
            )
        return _Verdict(
            status='no_observation',
            decisive=False,
            detail=f'{title!r} was not found and the surface cannot prove it is gone',
            badge=badge,
            evidence=evidence,
        )
    if _row_has(row, DONE_LABELS):
        return _Verdict(
            status='action_completed',
            decisive=True,
            detail=f'{title!r} now reads as completed',
            badge=badge,
            evidence=evidence,
        )
    if action is None and trusted:
        return _Verdict(
            status='action_gone',
            decisive=True,
            detail=f'{title!r} no longer offers an action',
            badge=badge,
            evidence=evidence,
        )
    return _Verdict(
        status='no_change',
        decisive=False,
        detail=f'{title!r} is still present with an unchanged badge or action',
        badge=badge,
        evidence=evidence,
    )


def _trusted_surface(opened: BoostList) -> bool:
    """Whether the opened surface is a task list whose missing row means something."""
    if opened.empty or is_empty_boost(opened.snapshot):
        return False
    if opened.route is DiscoveryRoute.BOOST:
        return is_boost_page(opened.snapshot) and has_task_rows(opened.snapshot)
    return _is_coin_page(opened.snapshot) and has_task_rows(opened.snapshot)


def _is_task_surface(snapshot: Snapshot) -> bool:
    """Whether a frame is the task list the roster was read from."""
    return is_boost_page(snapshot) or _is_coin_page(snapshot)


def _is_coin_page(snapshot: Snapshot) -> bool:
    return all(contains_text(snapshot, marker) for marker in COIN_PAGE_MARKERS)


def _on_coins_page(snapshot: Snapshot) -> bool:
    """The coin page itself, not the boost sheet drawn over it.

    The ``Coins boost`` sheet leaves the coin-page markers visible underneath in
    OCR, so marker presence alone would call the sheet the coin page. The sheet's
    own header is the discriminator: the run is not finished until the header is
    gone and the coin page is the top surface.
    """
    return _is_coin_page(snapshot) and not is_boost_page(snapshot)


def _evidence(snapshot: Snapshot, title: str) -> FrameEvidence:
    row, action, badge = find_task_row(snapshot, title)
    return FrameEvidence(
        surface=classify_surface(snapshot).value,
        row=row.text if row is not None else None,
        badge=badge.text if badge is not None else None,
        action=action.text if action is not None else None,
        labels=tuple(box.text for box in snapshot.boxes),
    )


def _row_has(row: Row, labels: Sequence[str]) -> bool:
    return any(similarity(label, box.text) >= DEFAULT_MIN_SCORE for label in labels for box in row.boxes)


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
    """Run the one dismissal loop, treating the redeem interstitial as a stop."""
    try:
        report = dismiss_after(
            controller,
            capture,
            _without_boost_family(corpus),
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
    """Remove the family whose anchor is the task list itself.

    The browse executor may sweep while a failed press leaves it on the boost
    page. That page carries the ``Coins boost`` anchor, so a full-corpus sweep
    would try to close the very list the run is working from. The discovery and
    sign-in executors exclude this family for the same reason.
    """
    families = tuple(family for family in corpus.families if family.family != 'coins_boost_sheet')
    if len(families) == len(corpus.families):
        return corpus
    return replace(corpus, families=families)


def _return_quietly(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    navigate: RouteFirer,
    *,
    templates: TemplateStore | None,
    route: SurfaceRoute,
    timeout: float,
    interval: float,
    attempts: int,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
) -> ReturnResult | None:
    """Best-effort return used while another exception is in flight."""
    try:
        return return_to_coins_page(
            controller,
            capture,
            corpus,
            navigate,
            templates=templates,
            route=route,
            timeout=timeout,
            interval=interval,
            attempts=attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=logger,
        )
    except Exception as exc:
        _warn(logger, 'return_failed', detail=f'{type(exc).__name__}: {exc}')
        return None


def _capture(source: CaptureSource) -> Snapshot:
    if callable(source):
        return source()
    return source.capture()


def _warn(logger: logging.Logger, event: str, **fields: object) -> None:
    logger.warning(
        '%s',
        json.dumps({'event': event, **fields}, separators=(',', ':'), sort_keys=True),
    )


def _log_result(logger: logging.Logger, result: BrowseResult) -> None:
    logger.info(
        '%s',
        json.dumps({'event': 'browse_task', **result.as_dict()}, separators=(',', ':'), sort_keys=True),
    )
