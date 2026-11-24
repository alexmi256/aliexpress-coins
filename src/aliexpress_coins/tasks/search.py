"""The search-landing executor: one category tap per round (#108).

``Search for what you love`` is a two-round browse task. Its ``GO`` opens a
search landing with a search field, an account-specific ``Search history``
section and a server-rendered ``Discover more`` grid of category cards. A round
completes by tapping one category card in that grid; the landing by itself does
not advance, and a tap on a history entry only fills the search field.

The measured contract is the boost row's own badge:

* it starts at ``0/2`` and advances to ``1/2`` when the first category opens;
* the row disappears when the second round reaches ``2/2``;
* the category tap also pays ``+1``, so the two rounds pay ``+2`` in total.

The executor therefore never types anything, never taps a history entry and
never visits a product. It re-enters the boost list after each round and reads
the badge; a badge that does not advance is a failure with expected vs observed
(#99), and only a verified round is written to the daily state (#100).

The return route is the shared shortcut route. A second BACK from the landing
would leave the app (docs/07 §1/§5), so every round returns the same way a browse
task does, and the run always ends on the coins page.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from aliexpress_coins.control.controller import LOGGER_NAME, Controller, TapRecord
from aliexpress_coins.control.popups import DismissReport, PopupCorpus
from aliexpress_coins.perception.layout import ProgressBadge
from aliexpress_coins.perception.matching import (
    DEFAULT_MIN_OCR_SCORE,
    DEFAULT_MIN_SCORE,
    find_text,
    normalize,
)
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.state import CaptureSource, ScreenState, classify
from aliexpress_coins.perception.templates import TemplateStore
from aliexpress_coins.perception.waits import wait_for_text_gone
from aliexpress_coins.runner.errors import RunInterrupted
from aliexpress_coins.runner.safety import RunAborted
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks.browse import (
    ReturnResult,
    _capture,
    _return_quietly,
    _settle_surface,
    _sweep,
    find_task_row,
    progress_unchanged_detail,
    return_to_coins_page,
)
from aliexpress_coins.tasks.catalogue import AllowedTaskSpec, TaskCategory
from aliexpress_coins.tasks.discovery import BoostList, DiscoveryRoute, RosterEntry, open_boost_list
from aliexpress_coins.tasks.dispatch import authorization_of
from aliexpress_coins.tasks.errors import TaskError
from aliexpress_coins.tasks.surface import (
    COIN_PAGE_MARKERS,
    RouteFirer,
    SurfaceRoute,
    contains_text,
    has_task_rows,
    is_boost_page,
    is_search_landing,
)

__all__ = [
    'DEFAULT_ARRIVAL_TIMEOUT',
    'DEFAULT_BADGE_TIMEOUT',
    'DEFAULT_INTERVAL',
    'DEFAULT_LOAD_TIMEOUT',
    'DEFAULT_RETURN_ATTEMPTS',
    'DEFAULT_RETURN_INTERVAL',
    'DEFAULT_RETURN_TIMEOUT',
    'DEFAULT_TRANSITION_TIMEOUT',
    'DISCOVER_MORE',
    'MAX_ROUNDS',
    'SEARCH_TITLE',
    'RoundRecord',
    'SearchCategory',
    'SearchOutcome',
    'SearchResult',
    'find_search_categories',
    'run_search_task',
]

SEARCH_TITLE = 'Search for what you love'
DISCOVER_MORE = 'Discover more'

MAX_ROUNDS = 2
"""The measured round count on the row's ``N/2`` badge."""

DEFAULT_LOAD_TIMEOUT = 15.0
"""How long the task surface may take to stop loading before the executor gives up."""

DEFAULT_ARRIVAL_TIMEOUT = 15.0
"""How long the search landing may take to appear after the row's ``GO``."""

DEFAULT_TRANSITION_TIMEOUT = 3.0
"""How long a category tap may take to leave the landing page.

The measured transition completed within one second. The poll is evidence that
the tap took effect; the badge is still the completion signal, so a slow page
does not become a longer blind sleep.
"""

DEFAULT_BADGE_TIMEOUT = 3.0
"""How long the returned row badge may take to show the round's credit.

The badge is server-driven and can lag the returned frame by a moment (#100).
An unreadable or unchanged badge at the bound is a failure, never an optimistic
completion.
"""

DEFAULT_INTERVAL = 0.5
"""How often capture is polled while a page is loading or a badge settles."""

DEFAULT_RETURN_TIMEOUT = 20.0
"""How long one shortcut re-entry may take to show the coin-page markers."""

DEFAULT_RETURN_INTERVAL = 0.5
"""How often the return route is polled."""

DEFAULT_RETURN_ATTEMPTS = 2
"""The bounded number of shortcut re-entries spent trying to reach the coin page."""

_CONTENT_BOTTOM_FRACTION = 0.94
"""The Android taskbar band is never category content."""

_CATEGORY_TITLE_HEIGHT_RATIO = 0.6
"""A card title is at least this multiple of the frame's median text height.

Branding painted on a card image is smaller than the card's own title; keeping
the frame-relative scale avoids a stored pixel size.
"""

_CATEGORY_ANCHOR_GAP_RATIO = 4.0
"""A category's item-count line sits within this many median text heights below it."""

_ITEMS_ANCHOR = re.compile(r'\b\d[\d,.]*\s*[kKmM]?\+?\s*items?\b', re.IGNORECASE)
"""The ``41k+ items`` line under a category card, never a product price."""

_PRICE = re.compile(r'(?:C\$|US\s*\$|CS|\$)\s*\d', re.IGNORECASE)

_NON_CATEGORY_PARTS: tuple[str, ...] = (
    'black friday',
    'choice',
    'coupons eligible',
    'disc',
    'free shipping',
    'items',
    'low price',
    'off',
    'sold',
    'welcome deal',
)
"""Card decoration, prices and shipping copy that is not a category title."""


class SearchOutcome(StrEnum):
    """What one search-task visit actually did."""

    COMPLETED = 'completed'
    FAILED = 'failed'
    SKIPPED = 'skipped'


@dataclass(frozen=True, slots=True)
class SearchCategory:
    """One tappable category card under the landing's ``Discover more`` grid."""

    label: str
    box: TextBox

    @property
    def normalized_label(self) -> str:
        """The label in the matching layer's comparable form."""
        return normalize(self.label)

    def as_dict(self) -> dict[str, object]:
        """Render the card for the run report."""
        return {
            'label': self.label,
            'box': list(self.box.bbox),
            'target': list(self.box.center),
        }


@dataclass(frozen=True, slots=True)
class RoundRecord:
    """One round's badge evidence and the category that earned it."""

    index: int
    category: str
    badge_before: str | None
    badge_after: str | None
    total: int
    complete: bool
    detail: str

    @property
    def badge_change(self) -> str:
        """The ``before -> after`` pair the acceptance report quotes."""
        before = self.badge_before if self.badge_before is not None else '?'
        after = self.badge_after if self.badge_after is not None else '?'
        return f'{before} -> {after}'

    def as_dict(self) -> dict[str, object]:
        """Render the round for ``run.json`` without losing the evidence."""
        return {
            'index': self.index,
            'category': self.category,
            'badge_before': self.badge_before,
            'badge_after': self.badge_after,
            'badge_change': self.badge_change,
            'total': self.total,
            'complete': self.complete,
            'detail': self.detail,
        }


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One search-task visit, its round evidence and whether it was recorded.

    ``verification`` names the proof: ``progress_advanced`` for a round that moved
    the badge, ``progress_complete`` for the final round (including the measured
    case where a complete row disappears), and ``daily_limit``, ``row_complete``
    or ``row_vanished`` when no work was possible. A non-advancing badge is
    ``progress_unchanged`` with the expected and observed values; the other
    failures are ``not_settled``, ``task_surface_unavailable``,
    ``landing_not_arrived``, ``category_not_found``, ``transition_not_observed``,
    ``return_failed``, ``redeem_interstitial`` or ``no_observation``.
    """

    key: str
    title: str
    outcome: SearchOutcome
    verification: str
    detail: str
    limit: int
    done_today_before: int
    done_today_after: int
    rounds: int = 0
    round_records: tuple[RoundRecord, ...] = ()
    badge_before: ProgressBadge | None = None
    badge_after: ProgressBadge | None = None
    taps: tuple[TapRecord, ...] = ()
    return_state: ReturnResult | None = None
    dismissals: tuple[DismissReport, ...] = ()
    stop: str | None = None
    recorded: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, 'round_records', tuple(self.round_records))
        object.__setattr__(self, 'taps', tuple(self.taps))
        object.__setattr__(self, 'dismissals', tuple(self.dismissals))

    @property
    def completed(self) -> bool:
        """Whether every round this visit attempted was verified and recorded."""
        return self.outcome is SearchOutcome.COMPLETED

    @property
    def failed(self) -> bool:
        """Whether the screen did not prove a completed round."""
        return self.outcome is SearchOutcome.FAILED

    @property
    def skipped(self) -> bool:
        """Whether the executor did no work because state or the app says stop."""
        return self.outcome is SearchOutcome.SKIPPED

    @property
    def returned(self) -> bool:
        """Whether the run finished on the coins page."""
        return self.return_state is not None and self.return_state.returned

    @property
    def badge_before_text(self) -> str | None:
        """The row badge before the visit, exactly as OCR read it."""
        return self.badge_before.text if self.badge_before is not None else None

    @property
    def badge_after_text(self) -> str | None:
        """The row badge after the last round, exactly as OCR read it."""
        return self.badge_after.text if self.badge_after is not None else None

    @property
    def badge_change(self) -> str | None:
        """The ``before -> after`` badge pair the acceptance report quotes."""
        before = self.badge_before_text
        after = self.badge_after_text
        if before is None and after is None:
            return None
        return f'{before if before is not None else "?"} -> {after if after is not None else "?"}'

    @property
    def categories(self) -> tuple[str, ...]:
        """The category tapped in each round, in walk order."""
        return tuple(record.category for record in self.round_records)

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
            'rounds': self.rounds,
            'round_records': [item.as_dict() for item in self.round_records],
            'categories': list(self.categories),
            'badge_before': self.badge_before_text,
            'badge_after': self.badge_after_text,
            'badge_change': self.badge_change,
            'taps': [tap.as_dict() for tap in self.taps],
            'return': None if self.return_state is None else self.return_state.as_dict(),
            'dismissals': [report.as_dict() for report in self.dismissals],
            'stop': self.stop,
            'recorded': self.recorded,
        }


def find_search_categories(snapshot: Snapshot) -> tuple[SearchCategory, ...]:
    """Return the category cards below ``Discover more``, in reading order.

    Search-history entries sit above the section heading and are never considered;
    the executor must not tap account data. A card's own item-count line
    (``NNk+ items``) identifies the title above it, so branding painted on the
    card image and a product promo with a price are not mistaken for a category.
    """
    discover = find_text(
        snapshot,
        DISCOVER_MORE,
        min_score=DEFAULT_MIN_SCORE,
        min_ocr_score=DEFAULT_MIN_OCR_SCORE,
    )
    if discover is None:
        return ()

    top = discover.box.bbox[3]
    content_bottom = snapshot.height * _CONTENT_BOTTOM_FRACTION
    heights = [box.height for box in snapshot.boxes if box.score >= DEFAULT_MIN_OCR_SCORE and box.height > 0]
    median_height = float(statistics.median(heights))
    min_title_height = median_height * _CATEGORY_TITLE_HEIGHT_RATIO
    max_gap = median_height * _CATEGORY_ANCHOR_GAP_RATIO
    split = snapshot.width / 2.0

    titles: list[TextBox] = []
    anchors: list[TextBox] = []
    for box in snapshot.boxes:
        if box.score < DEFAULT_MIN_OCR_SCORE or box.bbox[1] <= top or box.bbox[3] >= content_bottom:
            continue
        if _ITEMS_ANCHOR.search(box.text):
            anchors.append(box)
        elif _is_category_title(box, min_height=min_title_height):
            titles.append(box)

    selected: list[SearchCategory] = []
    seen: set[str] = set()
    for title in sorted(titles, key=lambda item: (item.bbox[1], item.bbox[0])):
        column = 0 if title.center[0] < split else 1
        has_anchor = any(
            _column_of(anchor, split) == column
            and anchor.bbox[1] >= title.bbox[3]
            and anchor.bbox[1] - title.bbox[3] <= max_gap
            for anchor in anchors
        )
        label = normalize(title.text)
        if not has_anchor or not label or label in seen:
            continue
        seen.add(label)
        selected.append(SearchCategory(label=title.text, box=title))

    if selected:
        return tuple(selected)

    # Some layouts omit the item-count line. Fall back to title-sized lines
    # below `Discover more`; the history entries above it remain excluded.
    for title in sorted(titles, key=lambda item: (item.bbox[1], item.bbox[0])):
        label = normalize(title.text)
        if not label or label in seen:
            continue
        seen.add(label)
        selected.append(SearchCategory(label=title.text, box=title))
    return tuple(selected)


def run_search_task(
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
    transition_timeout: float = DEFAULT_TRANSITION_TIMEOUT,
    badge_timeout: float = DEFAULT_BADGE_TIMEOUT,
    interval: float = DEFAULT_INTERVAL,
    return_timeout: float = DEFAULT_RETURN_TIMEOUT,
    return_interval: float = DEFAULT_RETURN_INTERVAL,
    return_attempts: int = DEFAULT_RETURN_ATTEMPTS,
    settle: float = 0.0,
    max_rounds: int = MAX_ROUNDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    logger: logging.Logger | None = None,
) -> SearchResult:
    """Run the two-round search task from an allowed roster row.

    The row's ``GO`` is re-resolved from the current frame. Each round opens the
    landing, taps one untried ``Discover more`` category, returns through the
    shortcut route, and reads the row badge. The badge moving is the only proof
    that is recorded; an unchanged badge fails the visit with expected vs
    observed and leaves the daily state untouched so the night can retry.
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
    if transition_timeout <= 0:
        raise ValueError(f'transition_timeout must be positive, got {transition_timeout!r}')
    if badge_timeout <= 0:
        raise ValueError(f'badge_timeout must be positive, got {badge_timeout!r}')
    if interval <= 0:
        raise ValueError(f'interval must be positive, got {interval!r}')
    if return_timeout <= 0:
        raise ValueError(f'return_timeout must be positive, got {return_timeout!r}')
    if return_interval <= 0:
        raise ValueError(f'return_interval must be positive, got {return_interval!r}')
    if return_attempts < 1:
        raise ValueError(f'return_attempts must be positive, got {return_attempts!r}')
    if settle < 0:
        raise ValueError(f'settle must not be negative, got {settle!r}')
    if max_rounds < 1:
        raise ValueError(f'max_rounds must be positive, got {max_rounds!r}')

    frame = snapshot if snapshot is not None else _capture(capture)
    frame, settled, entry_sweep = _settle_surface(
        controller,
        capture,
        corpus,
        frame,
        templates=templates,
        timeout=load_timeout,
        interval=interval,
        settle=settle,
        clock=clock,
        sleep=sleep,
        logger=log,
    )
    dismissals: list[DismissReport] = []
    if entry_sweep is not None:
        dismissals.append(entry_sweep)

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
            done_after=state.count(spec.key),
            detail='the task surface never stopped loading; no action was tapped',
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
            done_after=state.count(spec.key),
            detail=f'{entry.title!r} is not on a task surface in this frame',
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

    row, action, fresh_badge = find_task_row(frame, entry.title)
    badge = fresh_badge if fresh_badge is not None else entry.progress
    badge_before = badge
    if row is None or action is None:
        status = 'row_vanished' if row is None else 'row_complete'
        detail = (
            f'{entry.title!r} is absent from the frame; the roster entry is stale'
            if row is None
            else f'{entry.title!r} has no action left; the app already considers it finished'
        )
        result = _skipped(spec, entry.title, done_before, limit, verification=status, detail=detail)
        _log_result(log, result)
        return result

    rounds_total = badge.total if badge is not None else MAX_ROUNDS
    rounds_done = badge.completed if badge is not None else 0
    remaining = min(max(0, rounds_total - rounds_done), max_rounds, limit - done_before)
    if remaining <= 0:
        result = _skipped(
            spec,
            entry.title,
            done_before,
            limit,
            verification='row_complete',
            detail=f'{entry.title!r} shows all {rounds_total} round(s) already complete',
        )
        _log_result(log, result)
        return result

    taps: list[TapRecord] = []
    round_records: list[RoundRecord] = []
    tried: set[str] = set()
    completed_rounds = 0
    current = frame
    current_badge = badge
    return_state: ReturnResult | None = None
    final_verification = 'progress_advanced'
    final_detail = ''
    stop: str | None = None

    try:
        for round_index in range(1, remaining + 1):
            row, action, fresh_badge = find_task_row(current, entry.title)
            if fresh_badge is not None:
                current_badge = fresh_badge
            if action is None:
                return _finish_failed(
                    controller,
                    capture,
                    corpus,
                    navigate,
                    spec,
                    entry,
                    done_before,
                    limit,
                    verification='row_complete' if row is not None else 'row_vanished',
                    done_after=state.count(spec.key),
                    detail=(
                        f'{entry.title!r} has no action left; the app already considers it finished'
                        if row is not None
                        else f'{entry.title!r} is absent from the frame; the roster entry is stale'
                    ),
                    rounds=len(round_records),
                    round_records=round_records,
                    badge_before=badge_before,
                    badge_after=current_badge,
                    taps=taps,
                    return_state=return_state,
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

            taps.append(
                controller.tap_box(
                    action,
                    why=f'search {spec.key}: open the search landing for round {round_index}',
                    label=action.text,
                ),
            )
            landing, arrived, dismiss, stop = _wait_for_landing(
                controller,
                capture,
                corpus,
                timeout=arrival_timeout,
                interval=interval,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=log,
                templates=templates,
            )
            if dismiss is not None:
                dismissals.append(dismiss)
            if stop is not None:
                return _finish_failed(
                    controller,
                    capture,
                    corpus,
                    navigate,
                    spec,
                    entry,
                    done_before,
                    limit,
                    verification='redeem_interstitial',
                    done_after=state.count(spec.key),
                    detail='the unclosable reward interstitial appeared; the round was not claimed',
                    rounds=len(round_records),
                    round_records=round_records,
                    badge_before=badge_before,
                    badge_after=current_badge,
                    taps=taps,
                    return_state=return_state,
                    dismissals=dismissals,
                    stop=stop,
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
            if not arrived:
                return _finish_failed(
                    controller,
                    capture,
                    corpus,
                    navigate,
                    spec,
                    entry,
                    done_before,
                    limit,
                    verification='landing_not_arrived',
                    done_after=state.count(spec.key),
                    detail='the search landing never appeared after the row was pressed',
                    rounds=len(round_records),
                    round_records=round_records,
                    badge_before=badge_before,
                    badge_after=current_badge,
                    taps=taps,
                    return_state=return_state,
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

            category = next(
                (item for item in find_search_categories(landing) if item.normalized_label not in tried), None
            )
            if category is None:
                return _finish_failed(
                    controller,
                    capture,
                    corpus,
                    navigate,
                    spec,
                    entry,
                    done_before,
                    limit,
                    verification='category_not_found',
                    done_after=state.count(spec.key),
                    detail='the landing showed no untried category card under Discover more',
                    rounds=len(round_records),
                    round_records=round_records,
                    badge_before=badge_before,
                    badge_after=current_badge,
                    taps=taps,
                    return_state=return_state,
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
            tried.add(category.normalized_label)
            taps.append(
                controller.tap_box(
                    category.box,
                    why=f'search {spec.key}: tap a Discover more category for round {round_index}',
                    label=category.label,
                ),
            )
            transition = wait_for_text_gone(
                capture,
                is_search_landing,
                timeout=transition_timeout,
                interval=interval,
                initial=landing,
                clock=clock,
                sleep=sleep,
            )
            if transition.found:
                return _finish_failed(
                    controller,
                    capture,
                    corpus,
                    navigate,
                    spec,
                    entry,
                    done_before,
                    limit,
                    verification='transition_not_observed',
                    done_after=state.count(spec.key),
                    detail=f'the {category.label!r} category tap did not leave the search landing',
                    rounds=len(round_records),
                    round_records=round_records,
                    badge_before=badge_before,
                    badge_after=current_badge,
                    taps=taps,
                    return_state=return_state,
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

            returned = return_to_coins_page(
                controller,
                capture,
                corpus,
                navigate,
                snapshot=transition.frame,
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
            return_state = returned if return_state is None else return_state.merged(returned)
            if not returned.returned:
                return _result(
                    spec,
                    entry.title,
                    SearchOutcome.FAILED,
                    verification='return_failed',
                    detail='the shortcut route did not prove the coins page; the round is not recorded',
                    limit=limit,
                    done_before=done_before,
                    done_after=state.count(spec.key),
                    rounds=len(round_records),
                    round_records=round_records,
                    badge_before=badge_before,
                    badge_after=current_badge,
                    taps=taps,
                    return_state=return_state,
                    dismissals=dismissals,
                    stop=stop,
                    logger=log,
                )

            opened = open_boost_list(
                controller,
                capture,
                corpus,
                templates=templates,
                timeout=load_timeout,
                interval=interval,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=log,
            )
            current = opened.snapshot
            if opened.dismiss is not None:
                dismissals.append(opened.dismiss)
            previous_badge = current_badge
            current, after, row_present = _await_badge(
                capture,
                current,
                entry.title,
                previous_badge,
                timeout=badge_timeout,
                interval=interval,
                clock=clock,
                sleep=sleep,
            )

            advanced = after is not None and (
                (previous_badge is not None and after.completed > previous_badge.completed)
                or (previous_badge is None and after.complete)
            )
            if advanced and after is not None:
                count = _advanced_count(previous_badge, after)
                record = RoundRecord(
                    index=round_index,
                    category=category.label,
                    badge_before=_badge_text(previous_badge),
                    badge_after=after.text,
                    total=after.total,
                    complete=True,
                    detail=f"'{category.label}' advanced the row to {after.text}",
                )
                round_records.append(record)
                for _ in range(count):
                    state.record(spec.key)
                completed_rounds += count
                current_badge = after
                if after.complete:
                    final_verification = 'progress_complete'
                    final_detail = f'{entry.title!r} reached {after.text} after {completed_rounds} round(s)'
                    break
                continue

            if not row_present and _trusted_surface(opened) and previous_badge is not None:
                expected = min(previous_badge.completed + 1, previous_badge.total)
                if expected >= previous_badge.total:
                    record = RoundRecord(
                        index=round_index,
                        category=category.label,
                        badge_before=previous_badge.text,
                        badge_after=None,
                        total=previous_badge.total,
                        complete=True,
                        detail=(
                            f'the final round advanced to {expected}/{previous_badge.total}; '
                            'the completed row is gone from the populated boost list'
                        ),
                    )
                    round_records.append(record)
                    state.record(spec.key)
                    completed_rounds += 1
                    final_verification = 'progress_complete'
                    final_detail = (
                        f'{entry.title!r} completed the final round; expected {expected}/{previous_badge.total}, '
                        'the row is gone from the populated boost list'
                    )
                    break

            detail = (
                progress_unchanged_detail(entry.title, previous_badge, after, advanced=1)
                if row_present
                else (
                    f'progress_unchanged: expected '
                    f'{min(previous_badge.completed + 1, previous_badge.total) if previous_badge is not None else "?"}/'
                    f'{previous_badge.total if previous_badge is not None else "?"}, '
                    f'saw the row gone before the final round ({entry.title!r})'
                )
            )
            round_records.append(
                RoundRecord(
                    index=round_index,
                    category=category.label,
                    badge_before=_badge_text(previous_badge),
                    badge_after=_badge_text(after),
                    total=previous_badge.total if previous_badge is not None else rounds_total,
                    complete=False,
                    detail=detail,
                ),
            )
            return _finish_failed(
                controller,
                capture,
                corpus,
                navigate,
                spec,
                entry,
                done_before,
                limit,
                verification='progress_unchanged',
                done_after=state.count(spec.key),
                detail=detail,
                rounds=len(round_records),
                round_records=round_records,
                badge_before=badge_before,
                badge_after=after,
                taps=taps,
                return_state=return_state,
                dismissals=dismissals,
                clock=clock,
                sleep=sleep,
                route=route,
                templates=templates,
                return_timeout=return_timeout,
                return_interval=return_interval,
                return_attempts=return_attempts,
                settle=settle,
                logger=log,
            )

        final_return = return_to_coins_page(
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
        return_state = final_return if return_state is None else return_state.merged(final_return)
        if not return_state.returned:
            return _result(
                spec,
                entry.title,
                SearchOutcome.FAILED,
                verification='return_failed',
                detail='the verification surface was read but the run could not return to the coins page',
                limit=limit,
                done_before=done_before,
                done_after=state.count(spec.key),
                rounds=len(round_records),
                round_records=round_records,
                badge_before=badge_before,
                badge_after=current_badge,
                taps=taps,
                return_state=return_state,
                dismissals=dismissals,
                stop=stop,
                logger=log,
            )

        if not final_detail:
            final_detail = (
                f'{entry.title!r} advanced {completed_rounds} round(s); '
                f'badge {_badge_text(badge_before)} -> {_badge_text(current_badge)}'
            )
        return _result(
            spec,
            entry.title,
            SearchOutcome.COMPLETED,
            verification=final_verification,
            detail=final_detail,
            limit=limit,
            done_before=done_before,
            done_after=state.count(spec.key),
            rounds=len(round_records),
            round_records=round_records,
            badge_before=badge_before,
            badge_after=current_badge,
            taps=taps,
            return_state=return_state,
            dismissals=dismissals,
            stop=stop,
            recorded=True,
            logger=log,
        )
    except RunAborted, RunInterrupted:
        raise
    except Exception:
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


def _is_category_title(box: TextBox, *, min_height: float) -> bool:
    """Whether an OCR line can be a category title rather than card decoration."""
    text = box.text.strip()
    if not text or box.height < min_height or not any(character.isalpha() for character in text):
        return False
    if _PRICE.search(text):
        return False
    normalized = normalize(text)
    return not any(part in normalized for part in _NON_CATEGORY_PARTS)


def _column_of(box: TextBox, split: float) -> int:
    return 0 if box.center[0] < split else 1


def _await_badge(
    capture: CaptureSource,
    snapshot: Snapshot,
    title: str,
    before: ProgressBadge | None,
    *,
    timeout: float,
    interval: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[Snapshot, ProgressBadge | None, bool]:
    """Poll the returned row until its badge advances or the bound ends.

    ``row_present`` is the last frame's answer, not an assumption. An absent row
    is only conclusive on a populated boost list, which the caller checks.
    """
    current = snapshot
    row, _, badge = find_task_row(current, title)
    if before is None:
        return current, badge, row is not None
    expected = min(before.completed + 1, before.total)
    deadline = clock() + timeout
    while badge is None or badge.completed < expected:
        now = clock()
        if now >= deadline:
            break
        sleep(min(interval, max(0.0, deadline - now)))
        current = _capture(capture)
        row, _, badge = find_task_row(current, title)
    return current, badge, row is not None


def _advanced_count(before: ProgressBadge | None, after: ProgressBadge) -> int:
    if before is None:
        return max(1, after.completed)
    return max(1, after.completed - before.completed)


def _badge_text(badge: ProgressBadge | None) -> str | None:
    return badge.text if badge is not None else None


def _trusted_surface(opened: BoostList) -> bool:
    """Whether a missing row on this surface proves the search task is finished."""
    return (
        not opened.empty
        and opened.route is DiscoveryRoute.BOOST
        and is_boost_page(opened.snapshot)
        and has_task_rows(opened.snapshot)
    )


def _allowed_spec(entry: RosterEntry) -> AllowedTaskSpec:
    """Return the entry's allowed spec, refusing anything the dispatch point missed."""
    spec = entry.spec
    if not entry.allowed or spec is None or entry.dispatch_target is None:
        reason = entry.reason or 'no allowed action is exposed'
        raise TaskError(f'{entry.title!r} is not dispatchable: {reason}')
    if spec.category is not TaskCategory.BROWSE:
        raise TaskError(f'{entry.title!r} is {spec.category.value!r}, not a browse task')
    authorization_of(entry)
    return spec


def _is_task_surface(snapshot: Snapshot) -> bool:
    return is_boost_page(snapshot) or _is_coin_page(snapshot)


def _is_coin_page(snapshot: Snapshot) -> bool:
    return all(contains_text(snapshot, marker) for marker in COIN_PAGE_MARKERS)


def _wait_for_landing(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    *,
    timeout: float,
    interval: float,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
    templates: TemplateStore | None,
) -> tuple[Snapshot, bool, DismissReport | None, str | None]:
    """Poll until the search landing is visible, sweeping popups on the way."""
    deadline = clock() + timeout
    current = _capture(capture)
    dismiss: DismissReport | None = None
    while True:
        if is_search_landing(current):
            return current, True, dismiss, None
        state = classify(current).state
        if state is ScreenState.POPUP:
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
                return current, False, dismiss, stop
            current = report.frames[-1].snapshot if report is not None and report.frames else _capture(capture)
            continue
        now = clock()
        if now >= deadline:
            return current, False, dismiss, None
        sleep(min(interval, max(0.0, deadline - now)))
        current = _capture(capture)


def _skipped(
    spec: AllowedTaskSpec,
    title: str,
    done: int,
    limit: int,
    *,
    verification: str,
    detail: str,
) -> SearchResult:
    return SearchResult(
        key=spec.key,
        title=title,
        outcome=SearchOutcome.SKIPPED,
        verification=verification,
        detail=detail,
        limit=limit,
        done_today_before=done,
        done_today_after=done,
    )


def _result(
    spec: AllowedTaskSpec,
    title: str,
    outcome: SearchOutcome,
    *,
    verification: str,
    detail: str,
    limit: int,
    done_before: int,
    done_after: int,
    rounds: int = 0,
    round_records: list[RoundRecord] | tuple[RoundRecord, ...] = (),
    badge_before: ProgressBadge | None = None,
    badge_after: ProgressBadge | None = None,
    taps: list[TapRecord] | tuple[TapRecord, ...] = (),
    return_state: ReturnResult | None = None,
    dismissals: list[DismissReport] | tuple[DismissReport, ...] = (),
    stop: str | None = None,
    recorded: bool = False,
    logger: logging.Logger,
) -> SearchResult:
    result = SearchResult(
        key=spec.key,
        title=title,
        outcome=outcome,
        verification=verification,
        detail=detail,
        limit=limit,
        done_today_before=done_before,
        done_today_after=done_after,
        rounds=rounds,
        round_records=tuple(round_records),
        badge_before=badge_before,
        badge_after=badge_after,
        taps=tuple(taps),
        return_state=return_state,
        dismissals=tuple(dismissals),
        stop=stop,
        recorded=recorded,
    )
    _log_result(logger, result)
    return result


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
    done_after: int | None = None,
    rounds: int = 0,
    round_records: list[RoundRecord] | tuple[RoundRecord, ...] = (),
    badge_before: ProgressBadge | None = None,
    badge_after: ProgressBadge | None = None,
    taps: list[TapRecord] | tuple[TapRecord, ...] = (),
    return_state: ReturnResult | None = None,
    dismissals: list[DismissReport] | tuple[DismissReport, ...] = (),
    stop: str | None = None,
    route: SurfaceRoute,
    templates: TemplateStore | None,
    return_timeout: float,
    return_interval: float,
    return_attempts: int,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
) -> SearchResult:
    """End a failed visit on the coins page and build its result."""
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
    merged = final if return_state is None else return_state.merged(final)
    if not merged.returned:
        verification = 'return_failed'
        detail = f'{detail}; the shortcut route did not prove the coins page'
    return _result(
        spec,
        entry.title,
        SearchOutcome.FAILED,
        verification=verification,
        detail=detail,
        limit=limit,
        done_before=done_before,
        done_after=done_before if done_after is None else done_after,
        rounds=rounds,
        round_records=round_records,
        badge_before=badge_before,
        badge_after=badge_after,
        taps=taps,
        return_state=merged,
        dismissals=dismissals,
        stop=stop,
        logger=logger,
    )


def _log_result(logger: logging.Logger, result: SearchResult) -> None:
    logger.info(
        '%s',
        json.dumps({'event': 'search_task', **result.as_dict()}, separators=(',', ':'), sort_keys=True),
    )
