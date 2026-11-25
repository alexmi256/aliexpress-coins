"""The real-search executor: submit a query per round (#142).

``Search for what you love`` is a two-round browse task. Its ``GO`` opens a
search landing with a search field, an optional ``Search history`` section and a
server-rendered ``Discover more`` grid. The measured category-card tap did not
navigate or submit a search and did not advance the task; whether it changes the
field is unproven because the field's grey ``OPPO`` placeholder was already
present before the tap.

The measured contract is the boost row's own badge:

* it starts at ``0/2`` and advances to ``1/2`` after a submitted query reaches
  the product-results surface;
* the row disappears when the second round reaches ``2/2``;
* each verified search also pays ``+1``, so the two rounds pay ``+2`` in total.

The executor therefore focuses the field, types a benign product query, submits
it with the IME's enter action, and waits for a results surface showing both the
query and product prices. A landing or keyboard that remains on screen is not a
search and is never counted. If enter does not submit, one bounded retry taps
the field's own search control before the round fails honestly.

The executor never taps a product, price, cart or buy control. It re-enters the
boost list after each round and reads the badge; a badge that does not advance
is a failure with expected vs observed (#99), and only a verified round is
written to the daily state (#100).

The return route is the shared shortcut route. A second BACK from the landing
would leave the app (docs/07 §1/§5), so every round returns the same way a browse
task does, and the run always ends on the coins page.
"""

from __future__ import annotations

import json
import logging
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
from aliexpress_coins.perception.snapshot import Snapshot
from aliexpress_coins.perception.state import CaptureSource, ScreenState, classify
from aliexpress_coins.perception.templates import TemplateStore
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
    is_search_results,
)

__all__ = [
    'DEFAULT_ARRIVAL_TIMEOUT',
    'DEFAULT_BADGE_TIMEOUT',
    'DEFAULT_INTERVAL',
    'DEFAULT_LOAD_TIMEOUT',
    'DEFAULT_RESULT_TIMEOUT',
    'DEFAULT_RETURN_ATTEMPTS',
    'DEFAULT_RETURN_INTERVAL',
    'DEFAULT_RETURN_TIMEOUT',
    'MAX_ROUNDS',
    'SEARCH_QUERIES',
    'SEARCH_TITLE',
    'RoundRecord',
    'SearchField',
    'SearchOutcome',
    'SearchResult',
    'find_search_field',
    'run_search_task',
]

SEARCH_TITLE = 'Search for what you love'

SEARCH_QUERIES: tuple[str, ...] = ('wireless phone', 'phone case')
"""Benign product queries used for the two measured rounds.

They contain no account data and never name a purchase action. A second query is
used for the second round, so the app cannot treat a repeated request as the
same search and so the result frame carries a different query to verify.
"""

MAX_ROUNDS = 2
"""The measured round count on the row's ``N/2`` badge."""

DEFAULT_LOAD_TIMEOUT = 15.0
"""How long the task surface may take to stop loading before the executor gives up."""

DEFAULT_ARRIVAL_TIMEOUT = 15.0
"""How long the search landing may take to appear after the row's ``GO``."""

DEFAULT_RESULT_TIMEOUT = 20.0
"""How long one submitted query may take to paint a recognisable results surface.

The measured load showed a blank/skeleton frame for several seconds before the
first product cards painted, so the bound is longer than the landing's own
arrival wait. It is still a bound: an empty landing at the deadline is a failure,
not an optimistic count.
"""

SESSION_UI_TOP_FRACTION = 0.03
SEARCH_FIELD_BOTTOM_FRACTION = 0.15
"""The search field sits between the status bar and these frame fractions.

The landing's first heading (``Search history`` when present) normally sets the
lower bound; the fraction caps it when that section is absent so the executor
never taps into the history/category content or below the measured field bar.
"""

SEARCH_BUTTON_RIGHT_FRACTION = 0.88
"""The measured search control sits on the field's right edge (x ≈ 0.89 · width)."""

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


class SearchOutcome(StrEnum):
    """What one search-task visit actually did."""

    COMPLETED = 'completed'
    FAILED = 'failed'
    SKIPPED = 'skipped'


@dataclass(frozen=True, slots=True)
class SearchField:
    """The landing's search field, expressed as frame-relative tap points."""

    center: tuple[float, float]
    submit: tuple[float, float]
    top: float
    bottom: float

    def as_dict(self) -> dict[str, object]:
        """Render the field for the run report."""
        return {
            'center': list(self.center),
            'submit': list(self.submit),
            'top': self.top,
            'bottom': self.bottom,
        }


@dataclass(frozen=True, slots=True)
class RoundRecord:
    """One round's badge evidence and the query that earned it."""

    index: int
    query: str
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
            'query': self.query,
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
    ``landing_not_arrived``, ``search_field_not_found``, ``results_not_reached``,
    ``progress_unchanged``, ``return_failed`` or ``redeem_interstitial``.

    When the final round removes the row, ``badge_after`` stays at the last badge
    that was readable (``N-1/N``); ``verification='progress_complete'`` and the
    detail's expected ``N/N`` are the authoritative completion evidence.
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
    def queries(self) -> tuple[str, ...]:
        """The query submitted in each round, in walk order."""
        return tuple(record.query for record in self.round_records)

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
            'queries': list(self.queries),
            'badge_before': self.badge_before_text,
            'badge_after': self.badge_after_text,
            'badge_change': self.badge_change,
            'taps': [tap.as_dict() for tap in self.taps],
            'return': None if self.return_state is None else self.return_state.as_dict(),
            'dismissals': [report.as_dict() for report in self.dismissals],
            'stop': self.stop,
            'recorded': self.recorded,
        }


def find_search_field(snapshot: Snapshot) -> SearchField | None:
    """Return the landing's search field as frame-relative tap points.

    The field is the band between the status bar and the landing's first heading,
    capped at :data:`SEARCH_FIELD_BOTTOM_FRACTION` so a missing history section
    cannot make the executor tap into the category grid. Its visible hint or
    query text sets the vertical tap point; the capped band is only the fallback
    when OCR reads no hint. The submit point is on the field's right edge at the
    same height. All values derive from the frame, not from stored tablet pixels.
    """
    top = snapshot.height * SESSION_UI_TOP_FRACTION
    limit = snapshot.height * SEARCH_FIELD_BOTTOM_FRACTION
    heading = find_text(
        snapshot,
        'Search history',
        min_score=DEFAULT_MIN_SCORE,
        min_ocr_score=DEFAULT_MIN_OCR_SCORE,
    )
    if heading is None:
        heading = find_text(
            snapshot,
            'Discover more',
            min_score=DEFAULT_MIN_SCORE,
            min_ocr_score=DEFAULT_MIN_OCR_SCORE,
        )
    if heading is None:
        return None
    bottom = min(heading.box.bbox[1], limit)
    if bottom <= top:
        return None
    heading_labels = {normalize('Search history'), normalize('Discover more')}
    hints = [
        box
        for box in snapshot.boxes
        if box.score >= DEFAULT_MIN_OCR_SCORE
        and top <= box.bbox[1]
        and box.bbox[3] <= bottom
        and sum(character.isalpha() for character in box.text) >= 2
        and normalize(box.text) not in heading_labels
    ]
    if hints:
        hint = max(hints, key=lambda box: box.width)
        center_y = hint.center[1]
    else:
        center_y = top + (bottom - top) * 0.6
    return SearchField(
        center=(snapshot.width * 0.5, center_y),
        submit=(snapshot.width * SEARCH_BUTTON_RIGHT_FRACTION, center_y),
        top=top,
        bottom=bottom,
    )


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
    result_timeout: float = DEFAULT_RESULT_TIMEOUT,
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
    landing, types a benign query, submits it, waits for the product-results
    surface, returns through the shortcut route, and reads the row badge. The
    badge moving is the only proof that is recorded; an unchanged badge fails the
    visit with expected vs observed and leaves the daily state untouched so the
    night can retry.
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
    if result_timeout <= 0:
        raise ValueError(f'result_timeout must be positive, got {result_timeout!r}')
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
    queries_used: set[str] = set()
    last_query: str | None = None
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

            field = find_search_field(landing)
            if field is None:
                return _finish_failed(
                    controller,
                    capture,
                    corpus,
                    navigate,
                    spec,
                    entry,
                    done_before,
                    limit,
                    verification='search_field_not_found',
                    done_after=state.count(spec.key),
                    detail='the search landing showed no field above its first heading',
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
                controller.tap(
                    field.center,
                    why=f'search {spec.key}: focus the search field for round {round_index}',
                    label='search field',
                ),
            )
            if last_query is not None and _field_holds(landing, field, last_query):
                cleared = _clear_field(
                    controller,
                    landing,
                    field,
                    last_query,
                    why=f'search {spec.key}: clear the previous query before round {round_index}',
                )
                if cleared is not None:
                    taps.append(cleared)

            query = _query_for_round(rounds_done + round_index, queries_used)
            queries_used.add(query)
            controller.type_text(
                query,
                why=f'search {spec.key}: type the round {round_index} query',
            )
            results, reached, dismiss, stop = _submit_search(
                controller,
                capture,
                corpus,
                field,
                query,
                why=f'search {spec.key}: submit the round {round_index} query',
                timeout=result_timeout,
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
                    detail='the unclosable reward interstitial appeared after the search; the round was not claimed',
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
            if not reached:
                return _finish_failed(
                    controller,
                    capture,
                    corpus,
                    navigate,
                    spec,
                    entry,
                    done_before,
                    limit,
                    verification='results_not_reached',
                    done_after=state.count(spec.key),
                    detail=f'the {query!r} search did not reach a results surface; the round was not claimed',
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
            last_query = query

            returned = return_to_coins_page(
                controller,
                capture,
                corpus,
                navigate,
                snapshot=results,
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
                count = min(_advanced_count(previous_badge, after), limit - state.count(spec.key))
                record = RoundRecord(
                    index=round_index,
                    query=query,
                    badge_before=_badge_text(previous_badge),
                    badge_after=after.text,
                    total=after.total,
                    complete=True,
                    detail=f'the {query!r} search advanced the row to {after.text}',
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

            if not row_present and _trusted_surface(opened, current) and previous_badge is not None:
                expected = min(previous_badge.completed + 1, previous_badge.total)
                if expected >= previous_badge.total:
                    record = RoundRecord(
                        index=round_index,
                        query=query,
                        badge_before=previous_badge.text,
                        badge_after=None,
                        total=previous_badge.total,
                        complete=True,
                        detail=(
                            f'the final {query!r} search advanced to {expected}/{previous_badge.total}; '
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
                    query=query,
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


def _field_holds(snapshot: Snapshot, field: SearchField, query: str) -> bool:
    """Whether the field band already contains ``query`` from an earlier round."""
    match = find_text(
        snapshot,
        query,
        min_score=DEFAULT_MIN_SCORE,
        min_ocr_score=DEFAULT_MIN_OCR_SCORE,
    )
    if match is None:
        return False
    top, bottom = match.box.bbox[1], match.box.bbox[3]
    return field.top <= top and bottom <= field.bottom


def _clear_field(
    controller: Controller,
    snapshot: Snapshot,
    field: SearchField,
    query: str,
    *,
    why: str,
) -> TapRecord | None:
    """Clear a retained previous query, preferably with the field's own control."""
    match = find_text(
        snapshot,
        query,
        min_score=DEFAULT_MIN_SCORE,
        min_ocr_score=DEFAULT_MIN_OCR_SCORE,
    )
    if match is None:
        return None
    close = [
        box
        for box in snapshot.boxes
        if box.score >= DEFAULT_MIN_OCR_SCORE
        and field.top <= box.bbox[1]
        and box.bbox[3] <= field.bottom
        and box.center[0] > match.box.bbox[2]
        and normalize(box.text) == 'close'
    ]
    if close:
        target = min(close, key=lambda box: box.center[0])
        return controller.tap_box(target, why=why, label='clear search')
    for _ in range(len(query) + 1):
        controller.press_key('KEYCODE_DEL', why=why)
    return None


def _query_for_round(round_index: int, used: set[str]) -> str:
    """Pick a benign query the current visit has not submitted yet."""
    for offset in range(len(SEARCH_QUERIES)):
        query = SEARCH_QUERIES[(round_index - 1 + offset) % len(SEARCH_QUERIES)]
        if query not in used:
            return query
    return SEARCH_QUERIES[(round_index - 1) % len(SEARCH_QUERIES)]


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


def _trusted_surface(opened: BoostList, current: Snapshot) -> bool:
    """Whether the frame the missing row was read from proves the task finished.

    The route comes from ``open_boost_list``, but the *evidence* must be the
    frame that actually omitted the row. A transient blank or partially rendered
    frame is not a populated boost list and cannot turn an unread badge into a
    completion.
    """
    return (
        not opened.empty
        and opened.route is DiscoveryRoute.BOOST
        and is_boost_page(current)
        and has_task_rows(current)
        and classify(current).state is ScreenState.PAGE
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


def _wait_for_surface(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    *,
    target: Callable[[Snapshot], bool],
    initial: Snapshot | None = None,
    timeout: float,
    interval: float,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
    templates: TemplateStore | None,
) -> tuple[Snapshot, bool, DismissReport | None, str | None]:
    """Poll until ``target`` is true, sweeping popups on the way."""
    deadline = clock() + timeout
    current = initial if initial is not None else _capture(capture)
    dismiss: DismissReport | None = None
    while True:
        if target(current):
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
    return _wait_for_surface(
        controller,
        capture,
        corpus,
        target=is_search_landing,
        timeout=timeout,
        interval=interval,
        settle=settle,
        clock=clock,
        sleep=sleep,
        logger=logger,
        templates=templates,
    )


def _wait_for_results(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    *,
    query: str,
    timeout: float,
    interval: float,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
    templates: TemplateStore | None,
) -> tuple[Snapshot, bool, DismissReport | None, str | None]:
    """Poll until the submitted query's product results are visible."""
    return _wait_for_surface(
        controller,
        capture,
        corpus,
        target=lambda snapshot: is_search_results(snapshot, query=query),
        timeout=timeout,
        interval=interval,
        settle=settle,
        clock=clock,
        sleep=sleep,
        logger=logger,
        templates=templates,
    )


def _submit_search(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    field: SearchField,
    query: str,
    *,
    why: str,
    timeout: float,
    interval: float,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
    templates: TemplateStore | None,
) -> tuple[Snapshot, bool, DismissReport | None, str | None]:
    """Submit with the IME, then retry once with the field's search control.

    The measured enter action submits and paints results. A landing that is
    still on screen at the bound is not a search, so one different submit
    mechanism is tried before the round fails; neither attempt records progress
    by itself.
    """
    controller.press_key('KEYCODE_ENTER', why=f'{why} with the enter action')
    current, reached, dismiss, stop = _wait_for_results(
        controller,
        capture,
        corpus,
        query=query,
        timeout=timeout,
        interval=interval,
        settle=settle,
        clock=clock,
        sleep=sleep,
        logger=logger,
        templates=templates,
    )
    if reached or stop is not None:
        return current, reached, dismiss, stop

    controller.tap(
        field.submit,
        why=f'{why} with the field search control',
        label='search',
    )
    retry, retried, retry_dismiss, retry_stop = _wait_for_results(
        controller,
        capture,
        corpus,
        query=query,
        timeout=timeout,
        interval=interval,
        settle=settle,
        clock=clock,
        sleep=sleep,
        logger=logger,
        templates=templates,
    )
    return retry, retried, retry_dismiss if retry_dismiss is not None else dismiss, retry_stop


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
