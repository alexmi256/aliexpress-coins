"""The surprise-items executor: open three distinct products per round (#26).

The measured flow is a grid of products behind the row's ``GO``. Tapping a tile
opens an ordinary product detail page; **BACK is the scoring event**, not the
tap. A round is three distinct products, and the page counter (``0/3`` through
``3/3``) is the only completion signal. At ``3/3`` the app may draw a
``Mission Completed`` overlay, which a tap dismisses, and a ``Next round``
control when the row still has rounds left.

Three facts keep this executor honest:

* **distinctness is per product, not per tap.** A tile that does not move the
  counter is marked tried and is not counted a second time. The page does not
  say which product was already credited, only that the *counter* was the
  observable effect.
* **the cross-round set is unresolved (#63).** The spec records both possible
  rules. This module therefore starts with the per-round interpretation —
  products opened in an earlier round are available again — and, after a
  bounded number of genuinely new products fail to move the counter, switches
  once to the whole-task set and records ``rule_path: reset``. When #63 lands,
  the losing branch can be deleted without changing the rest of the executor.
* **the ledger follows a successful, attributed, counter-proven task.** A round
  is recorded only when this visit moves its counter to the target and the
  returned row agrees. A counter seen before any tile tap, or read from a
  headerless crop, is not that evidence. A failed task writes no completion and
  cannot consume the day's limit or foreclose its retry.
* **a returned grid is recognised structurally, and other landings are
  re-entered.** BACK can return to the headerless scrolled grid, whose new cards
  were not in the labels read before the tap; the executor accepts that structure,
  restores the top and proves the surprise header before reading the counter. If
  BACK lands anywhere else, it re-opens the grid through the shortcut/boost route.
  A tap that never returns is not progress (#164).

The run never taps a purchase control: only a product card, the page's own
``Next round``, the overlay, and BACK. Every completion is recorded from a
counter that moved, never from a disappearing grid; absence is not evidence
(docs/03).
"""

from __future__ import annotations

import json
import logging
import re
import statistics
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum

from aliexpress_coins.control.controller import LOGGER_NAME, Controller, KeyRecord, SwipeRecord, TapRecord
from aliexpress_coins.control.errors import InputError, PopupUnclosable
from aliexpress_coins.control.popups import DismissReport, PopupCorpus, dismiss_after
from aliexpress_coins.perception.errors import OcrError, StateTimeout
from aliexpress_coins.perception.layout import ProgressBadge
from aliexpress_coins.perception.matching import DEFAULT_MIN_OCR_SCORE, normalize
from aliexpress_coins.perception.ocr import OcrEngine, RapidOcrEngine
from aliexpress_coins.perception.scroll import SCROLL_START_FRACTION, SCROLL_VIEWPORT_FRACTION, scroll_until_found
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.state import CaptureSource, ScreenState, classify
from aliexpress_coins.perception.templates import TemplateStore
from aliexpress_coins.perception.waits import wait_for_text_gone
from aliexpress_coins.runner.errors import RunInterrupted
from aliexpress_coins.runner.safety import RunAborted, find_control_captions
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks.browse import (
    ReturnResult,
    find_task_row,
    progress_unchanged_detail,
    return_to_coins_page,
)
from aliexpress_coins.tasks.catalogue import AllowedTaskSpec, TaskCategory
from aliexpress_coins.tasks.discovery import DEFAULT_BOOST_TIMEOUT, RosterEntry, open_boost_list
from aliexpress_coins.tasks.dispatch import authorization_of
from aliexpress_coins.tasks.errors import TaskError
from aliexpress_coins.tasks.surface import (
    BOOST_MARKER,
    COIN_PAGE_MARKERS,
    REDEEM_MARKER,
    RouteFirer,
    SurfaceRoute,
    contains_text,
    is_boost_page,
    is_coin_store,
    is_product_detail,
)

__all__ = [
    'DEFAULT_ARRIVAL_TIMEOUT',
    'DEFAULT_COUNTER_TIMEOUT',
    'DEFAULT_EXIT_TIMEOUT',
    'DEFAULT_INTERVAL',
    'DEFAULT_LOAD_TIMEOUT',
    'DEFAULT_RETURN_ATTEMPTS',
    'DEFAULT_RETURN_INTERVAL',
    'DEFAULT_RETURN_TIMEOUT',
    'DEFAULT_TILE_TIMEOUT',
    'MAX_INERT_TAPS',
    'MAX_ROUNDS',
    'MAX_TAPS_PER_ROUND',
    'MISSION_COMPLETED',
    'NEXT_ROUND',
    'REDEEM_INTERSTITIAL',
    'RULE_PATH_RESET',
    'RULE_PATH_REUSE',
    'SURPRISE_SUBTITLE',
    'SURPRISE_TITLE',
    'TILE_SKIPPED_DANGEROUS',
    'CounterState',
    'RoundRecord',
    'SurpriseOutcome',
    'SurpriseResult',
    'SurpriseTile',
    'find_back_control',
    'find_imagery_captions',
    'find_mission_overlay',
    'find_next_round',
    'find_surprise_tiles',
    'is_item_detail',
    'is_surprise_grid',
    'read_surprise_counter',
    'run_surprise',
    'run_surprise_task',
]

SURPRISE_TITLE = 'Browse surprise items'
SURPRISE_SUBTITLE = 'Tap 3 items on this page'
NEXT_ROUND = 'Next round'
MISSION_COMPLETED = 'Mission Completed'

RULE_PATH_REUSE = 'reuse'
RULE_PATH_RESET = 'reset'

TILE_SKIPPED_DANGEROUS = 'tile_skipped_dangerous'
"""The log event recorded when a product photo carries a purchase caption."""

REDEEM_INTERSTITIAL = 'redeem_interstitial'
"""The corpus family name of the unclosable reward interstitial."""

DEFAULT_LOAD_TIMEOUT = 15.0
"""How long the task surface may take to stop loading before the executor gives up."""

DEFAULT_ARRIVAL_TIMEOUT = 10.0
"""How long the grid may take to appear after ``GO`` or ``Next round``."""

DEFAULT_EXIT_TIMEOUT = 10.0
"""How long BACK may take to return from a product detail page to the grid."""

DEFAULT_INTERVAL = 0.5
"""How often capture is polled while a page is loading or settling."""

DEFAULT_COUNTER_TIMEOUT = 8.0
"""How long the round pill may keep changing before its reading is final.

A credit can arrive in more than one increment, and the last increment can land
after the old fixed two-second dwell. The executor therefore polls until the
counter value has been quiet for ``_COUNTER_STABLE_HOLD`` (or the target is
reached), with this deadline as the hard bound. A counter that stops short still
ends at the bound and is reported as unchanged for the expected-vs-observed
detail (#100, #115).
"""

_COUNTER_STABLE_HOLD = 3.0
"""How long the counter must keep one value before that value is final.

Longer than the old fixed two-second dwell, so an increment that used to arrive
after that dwell resets the quiet window instead of being missed (#115).
"""

DEFAULT_TILE_TIMEOUT = 5.0
"""How long a newly opened or scrolled grid may take to paint product tiles.

The header and counter can arrive before the product cards. An empty first
read is therefore polled, not treated as proof that the grid has no products
(#115). The measured gap between the header frame and the painted cards was
under a second; five seconds leaves room for a slow render without turning a
genuinely empty grid into a long stall.
"""

DEFAULT_BADGE_TIMEOUT = 2.0
"""How long the returned row badge may take to reflect a completed round.

The badge is server-driven; the counter is the immediate proof and the badge is
the independent positive signal. A badge that is still unchanged at the bound
fails the task instead of being recorded optimistically (#100).
"""

DEFAULT_RETURN_TIMEOUT = 20.0
"""How long one shortcut re-entry may take to show the coin-page markers."""

DEFAULT_RETURN_INTERVAL = 0.5
"""How often the return route is polled."""

DEFAULT_RETURN_ATTEMPTS = 2
"""The bounded number of shortcut re-entries spent trying to reach the coin page."""

MAX_ROUNDS = 2
"""The most rounds one visit may walk, matching the measured ``N/2`` badge."""

MAX_TAPS_PER_ROUND = 12
"""A hard bound on product taps in one round.

Three distinct products are the target; the budget leaves room for inert
tiles, a scroll and the one reset retry, but a grid that cannot produce a
credit stops instead of tapping forever.
"""

MAX_INERT_TAPS = 2
"""Inert taps on new products before the executor switches to the reset path.

The pressure-cooker tile was inert on every visit, so one inert tap is normal.
Two distinctly new products failing in a row is the observable signature of
"the earlier round's products are still excluded", which is what the reset path
handles.
"""

MAX_SCROLLS = 4
"""The most grid scrolls one round may spend searching for unseen products."""

_COUNTER = re.compile(r'(?P<done>\d{1,2})\s*/\s*(?P<total>\d{1,2})')
"""The counter as OCR sometimes keeps the slash (``2/3``)."""

_BARE_DIGIT = re.compile(r'^\s*(?P<value>\d{1,2})\s*$')
"""OCR often drops the slash and returns the two digits as separate boxes."""

_COUNTER_LEFT_FRACTION = 0.2
_COUNTER_RIGHT_FRACTION = 0.8
_COUNTER_TOP_FRACTION = 0.195
_COUNTER_BOTTOM_FRACTION = 0.27
"""The measured counter-pill crop from ``specs/flows.yaml`` (frame fractions)."""

_CONTENT_BOTTOM_FRACTION = 0.92
"""The Android taskbar band is never product content."""

_TILE_LINE_GAP_RATIO = 1.5

_ANCHOR_GROUP_GAP_RATIO = 4.0
"""Lines in one card's price/shipping block are closer than this multiple of the median height."""
"""Title lines within one card are closer than this multiple of the median height."""

_POSITION_BUCKET_FRACTION = 0.05
"""A fallback tile identity is a 5%-of-height band plus its column."""

_SCROLL_START_FRACTION = SCROLL_START_FRACTION
_SCROLL_END_FRACTION = SCROLL_START_FRACTION - SCROLL_VIEWPORT_FRACTION
"""Scroll up by the shared search distance, then re-capture and look again.

The constants come from ``perception.scroll`` so the grid and the executor use
one fraction rather than two copies of the same number (issue #42).
"""

_PRICE = re.compile(r'(?:C\$|US\s*\$|CS|\$)\s*\d', re.IGNORECASE)
"""A line carrying a price or a discount percentage is not a product title."""

_NON_PRODUCT_PARTS: tuple[str, ...] = (
    'browse surprise items',
    'tap 3 items',
    'free shipping',
    'black friday',
    'choice',
    'applied',
    'disc',
    'sold',
    'orders',
    'tax excluded',
    'add to cart',
    'buy now',
    'item 1',
    'item 2',
    'search for',
    'next round',
    'mission completed',
    'missioncompleted',
)
"""Overlay and toolbar text that shares the grid's rows but is not a tile title."""

_MIN_LABEL_CHARS = 3
"""A shorter OCR fragment is tracked by its position, not by its label."""

_DEFAULT_COUNTER_OCR: OcrEngine | None = None
"""The lazy engine used only when the full-frame boxes lost the counter digit."""


class SurpriseOutcome(StrEnum):
    """What one surprise-items visit actually did."""

    COMPLETED = 'completed'
    FAILED = 'failed'
    SKIPPED = 'skipped'


@dataclass(frozen=True, slots=True)
class CounterState:
    """One reading of the round pill, including whether it was actually read."""

    completed: int
    total: int
    box: TextBox | None = None

    @property
    def complete(self) -> bool:
        """Whether the counter says the round has reached its target."""
        return self.completed >= self.total

    @property
    def text(self) -> str:
        """The reading in the same ``N/3`` shape the page prints."""
        return f'{self.completed}/{self.total}'

    def as_dict(self) -> dict[str, object]:
        """Render the reading for the run report."""
        record: dict[str, object] = {
            'completed': self.completed,
            'total': self.total,
            'text': self.text,
            'complete': self.complete,
        }
        if self.box is not None:
            record['box'] = list(self.box.bbox)
            record['reading'] = self.box.text
        return record


@dataclass(frozen=True, slots=True)
class SurpriseTile:
    """One product card, resolved from the current frame's grid layout.

    ``box`` is the card's title area clipped to its own column, so a merged OCR
    line across two cards still yields two distinct taps. ``shared_label`` says
    the OCR line spanned both columns; its label alone cannot identify a card,
    so the column is part of the identity.
    """

    label: str
    box: TextBox
    column: int
    bucket: tuple[int, int]
    shared_label: bool = False

    @property
    def normalized_label(self) -> str:
        """The label in the matching layer's comparable form."""
        return normalize(self.label)

    @property
    def label_key(self) -> tuple[object, ...]:
        """The identity used when OCR read a usable label."""
        if self.shared_label or len(self.normalized_label) < _MIN_LABEL_CHARS:
            return ('label', self.normalized_label, self.column)
        return ('label', self.normalized_label)

    @property
    def position_key(self) -> tuple[object, ...]:
        """The fallback identity when OCR is poor or the label is shared."""
        return ('position', *self.bucket)

    @property
    def keys(self) -> tuple[tuple[object, ...], ...]:
        """Both identities, so tracking survives a re-render either way."""
        return (self.label_key, self.position_key)

    @property
    def report_label(self) -> str:
        """A label that stays distinct when OCR merged both columns into one line.

        The two halves of a shared OCR line carry the same text but are different
        cards. The column makes the report quote two distinct products instead of
        repeating the one label the issue flagged (#100).
        """
        if self.shared_label:
            return f'{self.label} (column {self.column + 1})'
        return self.label

    def as_dict(self) -> dict[str, object]:
        """Render the tile for the run report."""
        return {
            'label': self.label,
            'box': list(self.box.bbox),
            'target': list(self.box.center),
            'column': self.column,
            'bucket': list(self.bucket),
            'shared_label': self.shared_label,
        }


@dataclass(frozen=True, slots=True)
class RoundRecord:
    """One round's counter evidence and the rule path it took."""

    index: int
    counter_before: int | None
    counter_after: int | None
    total: int
    taps: int
    rule_path: str
    complete: bool
    detail: str
    scrolls: int = 0
    items: tuple[str, ...] = ()
    skipped_dangerous: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, 'items', tuple(self.items))
        object.__setattr__(self, 'skipped_dangerous', tuple(self.skipped_dangerous))

    @property
    def delta(self) -> int | None:
        """How many products the counter credited in this round."""
        if self.counter_before is None or self.counter_after is None:
            return None
        return self.counter_after - self.counter_before

    @property
    def proven(self) -> bool:
        """Whether this visit advanced the counter to a newly completed round."""
        return self.complete and self.taps > 0 and self.delta is not None and self.delta > 0

    @property
    def counter_change(self) -> str:
        """The ``before -> after`` pair the acceptance report quotes."""
        before = '?' if self.counter_before is None else str(self.counter_before)
        after = '?' if self.counter_after is None else str(self.counter_after)
        return f'{before}/{self.total} -> {after}/{self.total}'

    def as_dict(self) -> dict[str, object]:
        """Render the round for ``run.json`` without losing the evidence."""
        return {
            'index': self.index,
            'counter_before': self.counter_before,
            'counter_after': self.counter_after,
            'total': self.total,
            'counter': self.counter_change,
            'delta': self.delta,
            'taps': self.taps,
            'scrolls': self.scrolls,
            'rule_path': self.rule_path,
            'complete': self.complete,
            'detail': self.detail,
            'items': list(self.items),
            'skipped_dangerous': list(self.skipped_dangerous),
        }


@dataclass(frozen=True, slots=True)
class SurpriseResult:
    """One surprise-items visit, the evidence behind it and whether state changed.

    ``verification`` names the proof: ``counter_complete`` on the success path;
    ``daily_limit``, ``row_complete`` or ``row_vanished`` when no work was
    possible; ``not_settled``, ``task_surface_unavailable``, ``grid_not_arrived``,
    ``counter_unreadable``, ``counter_stalled``, ``tile_not_opened``,
    ``grid_disappeared``, ``back_not_reached``, ``next_round_not_arrived``,
    ``return_failed``, ``redeem_interstitial`` or ``no_observation`` on the others.
    """

    key: str
    title: str
    outcome: SurpriseOutcome
    verification: str
    detail: str
    limit: int
    done_today_before: int
    done_today_after: int
    rounds: int = 0
    round_records: tuple[RoundRecord, ...] = ()
    rule_path: str = RULE_PATH_REUSE
    taps: tuple[TapRecord, ...] = ()
    keys: tuple[KeyRecord, ...] = ()
    swipes: tuple[SwipeRecord, ...] = ()
    items: tuple[str, ...] = ()
    badge_before: ProgressBadge | None = None
    badge_after: ProgressBadge | None = None
    return_state: ReturnResult | None = None
    dismissals: tuple[DismissReport, ...] = ()
    stop: str | None = None
    recorded: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, 'round_records', tuple(self.round_records))
        object.__setattr__(self, 'taps', tuple(self.taps))
        object.__setattr__(self, 'keys', tuple(self.keys))
        object.__setattr__(self, 'swipes', tuple(self.swipes))
        object.__setattr__(self, 'items', tuple(self.items))
        object.__setattr__(self, 'dismissals', tuple(self.dismissals))

    @property
    def completed(self) -> bool:
        """Whether at least one round was verified and recorded."""
        return self.outcome is SurpriseOutcome.COMPLETED

    @property
    def failed(self) -> bool:
        """Whether the screen did not prove a completed round."""
        return self.outcome is SurpriseOutcome.FAILED

    @property
    def skipped(self) -> bool:
        """Whether the executor did no work because the spec or app says stop."""
        return self.outcome is SurpriseOutcome.SKIPPED

    @property
    def returned(self) -> bool:
        """Whether the run finished on the coins page."""
        return self.return_state is not None and self.return_state.returned

    @property
    def counter_deltas(self) -> tuple[str, ...]:
        """The counter pair for every round, in walk order."""
        return tuple(record.counter_change for record in self.round_records)

    @property
    def skipped_dangerous(self) -> tuple[str, ...]:
        """Every product card skipped because its photograph carried a caption."""
        return tuple(label for record in self.round_records for label in record.skipped_dangerous)

    @property
    def interstitial(self) -> str | None:
        """The unclosable reward overlay that ended the flow, if any."""
        return self.stop

    @property
    def badge_before_text(self) -> str | None:
        """The returned row's badge before the task, exactly as OCR read it."""
        return self.badge_before.text if self.badge_before is not None else None

    @property
    def badge_after_text(self) -> str | None:
        """The returned row's badge after the task, exactly as OCR read it."""
        return self.badge_after.text if self.badge_after is not None else None

    @property
    def badge_change(self) -> str | None:
        """The ``before -> after`` badge pair the acceptance report quotes."""
        before = self.badge_before_text
        after = self.badge_after_text
        if before is None and after is None:
            return None
        return f'{before if before is not None else "?"} -> {after if after is not None else "?"}'

    def as_dict(self) -> dict[str, object]:
        """Render the result for ``run.json`` without losing the evidence."""
        record: dict[str, object] = {
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
            'counter_deltas': list(self.counter_deltas),
            'skipped_dangerous': list(self.skipped_dangerous),
            'rule_path': self.rule_path,
            'items': list(self.items),
            'badge_before': self.badge_before_text,
            'badge_after': self.badge_after_text,
            'badge_change': self.badge_change,
            'taps': [tap.as_dict() for tap in self.taps],
            'keys': [key.as_dict() for key in self.keys],
            'swipes': [swipe.as_dict() for swipe in self.swipes],
            'return': None if self.return_state is None else self.return_state.as_dict(),
            'dismissals': [report.as_dict() for report in self.dismissals],
            'stop': self.stop,
            'recorded': self.recorded,
        }
        return record


@dataclass(slots=True)
class _TriedTiles:
    """The identities already tapped in one scope (a round or the whole task)."""

    keys: set[tuple[object, ...]] = field(default_factory=set)

    def add(self, tile: SurpriseTile) -> None:
        self.keys.update(tile.keys)

    def contains(self, tile: SurpriseTile) -> bool:
        return any(key in self.keys for key in tile.keys)


@dataclass(frozen=True, slots=True)
class _TileChoice:
    """The next safe card and the cards skipped because a photo looked dangerous."""

    tile: SurpriseTile | None
    skipped: tuple[tuple[SurpriseTile, TextBox], ...] = ()


def find_surprise_tiles(snapshot: Snapshot) -> tuple[SurpriseTile, ...]:
    """Resolve product cards from the grid's OCR layout, in reading order.

    The grid is two columns. Every text line below the header that looks like a
    product title is clustered into its column's card by the frame's median text
    height, never by a stored coordinate. A title OCR sometimes merges the two
    columns into one line; it is clipped at the column boundary so each card
    still gets its own tap target, and its label is marked shared so tracking
    includes the column.
    """
    header_bottom = _header_bottom(snapshot)
    content_bottom = snapshot.height * _CONTENT_BOTTOM_FRACTION
    heights = [box.height for box in snapshot.boxes if box.height > 0]
    median_height = float(statistics.median(heights)) if heights else 0.0
    gap_limit = max(1.0, median_height * _TILE_LINE_GAP_RATIO)
    split = snapshot.width / 2.0

    titles_by_column: dict[int, list[TextBox]] = {0: [], 1: []}
    anchors_by_column: dict[int, list[TextBox]] = {0: [], 1: []}
    for box in snapshot.boxes:
        top = box.bbox[1]
        bottom = box.bbox[3]
        if bottom <= header_bottom or top >= content_bottom:
            continue
        if not (_is_product_title(box) or _is_card_anchor(box)):
            continue
        for column, (band_left, band_right) in enumerate(((0.0, split), (split, float(snapshot.width)))):
            overlap = min(box.bbox[2], band_right) - max(box.bbox[0], band_left)
            if overlap <= 0:
                continue
            if overlap < min(box.width, band_right - band_left) * 0.2:
                continue
            if _is_product_title(box):
                titles_by_column[column].append(box)
            else:
                anchors_by_column[column].append(box)

    tiles: list[SurpriseTile] = []
    for column, anchors in anchors_by_column.items():
        band_left = 0.0 if column == 0 else split
        band_right = split if column == 0 else float(snapshot.width)
        titles = titles_by_column[column]
        if anchors:
            # The product title is the text line immediately above a price,
            # shipping or sold line. Branding painted on the image sits above
            # it and is not a link; choosing it is how a tap becomes inert.
            sources = _titles_above_anchors(titles, anchors, group_gap=median_height * _ANCHOR_GROUP_GAP_RATIO)
            below = _first_title_below(titles, max(anchor.bbox[3] for anchor in anchors))
            if below is not None:
                sources.append(below)
        else:
            sources = [cluster[-1] for cluster in _cluster_titles(titles, gap_limit)]
        for source in sources:
            tile = _tile_from_source(snapshot, source, column, band_left, band_right, split)
            if tile is None:
                continue
            tiles.append(tile)

    return tuple(sorted(tiles, key=lambda tile: (tile.box.bbox[1], tile.box.bbox[0])))


def find_imagery_captions(
    snapshot: Snapshot,
) -> tuple[tuple[TextBox, SurpriseTile], ...]:
    """Pair dangerous captions painted inside a product photo with their card.

    A product card is an image above its title. A purchase caption inside that
    image is merchant artwork, not page chrome or a card action (#139), so the
    guard can exempt it and the executor can skip the card. A caption below the
    title, in the price/shipping block, between cards at page level, or outside
    every card band is deliberately not paired and stays a live control.
    """
    tiles = find_surprise_tiles(snapshot)
    if not tiles:
        return ()
    bands = _card_image_bands(snapshot, tiles)
    boxes = find_control_captions(snapshot)
    split = snapshot.width / 2.0
    matches: list[tuple[TextBox, SurpriseTile]] = []
    for caption in boxes:
        centre_x, centre_y = caption.center
        column = 0 if centre_x < split else 1
        for tile, top, bottom in bands:
            if tile.column == column and top <= centre_y <= bottom:
                matches.append((caption, tile))
                break
    return tuple(matches)


def _card_image_bands(
    snapshot: Snapshot,
    tiles: Sequence[SurpriseTile],
) -> tuple[tuple[SurpriseTile, float, float], ...]:
    """The vertical image band above each card's title, from the card above it.

    The top of a card is the previous card's price/shipping block; the first
    card starts below the header (or the measured header fraction on a scrolled
    grid). The bottom is the card's own title, because the photograph sits
    above that title on the measured page. No pixel constant is stored: every
    bound comes from this frame's boxes.
    """
    header_bottom = _header_bottom(snapshot)
    content_bottom = snapshot.height * _CONTENT_BOTTOM_FRACTION
    anchors_by_column: dict[int, list[TextBox]] = {0: [], 1: []}
    for box in snapshot.boxes:
        if box.score < DEFAULT_MIN_OCR_SCORE or not _is_card_anchor(box):
            continue
        if box.bbox[3] <= header_bottom or box.bbox[1] >= content_bottom:
            continue
        column = _box_column(snapshot, box)
        anchors_by_column[column].append(box)

    ordered_by_column: dict[int, list[SurpriseTile]] = {0: [], 1: []}
    for tile in tiles:
        ordered_by_column[tile.column].append(tile)

    bands: list[tuple[SurpriseTile, float, float]] = []
    for column, column_tiles in ordered_by_column.items():
        column_tiles.sort(key=lambda tile: (tile.box.bbox[1], tile.box.bbox[0]))
        previous_bottom = header_bottom
        previous_has_bottom = True
        for index, tile in enumerate(column_tiles):
            title_top = tile.box.bbox[1]
            if previous_has_bottom:
                bands.append((tile, previous_bottom, title_top))
            next_top = column_tiles[index + 1].box.bbox[1] if index + 1 < len(column_tiles) else content_bottom
            anchors_below = [
                anchor.bbox[3] for anchor in anchors_by_column[column] if title_top < anchor.center[1] < next_top
            ]
            if anchors_below:
                previous_bottom = max(anchors_below)
                previous_has_bottom = True
            else:
                # The previous card's action area has no OCR'd bottom. Without
                # that evidence its text must not become the next card's photo
                # band, so the next tile gets no image band at all (#139).
                previous_has_bottom = False
    return tuple(bands)


def _box_column(snapshot: Snapshot, box: TextBox) -> int:
    """The card column a box belongs to, using the grid's own centre split."""
    return 0 if box.center[0] < snapshot.width / 2.0 else 1


def read_surprise_counter(
    snapshot: Snapshot,
    *,
    total: int = 3,
    ocr: OcrEngine | None = None,
) -> CounterState | None:
    """Read the round counter, re-OCRing the measured pill crop when needed.

    The full-frame OCR sometimes returns ``2/3`` as one box; on other frames it
    drops the slash and returns ``2`` and ``3`` separately. On the measured
    ``1/3`` frame it drops the orange digit altogether, which is why
    ``specs/flows.yaml`` gives the pill an ``ocr_crop``. The full-frame read is
    tried first; only when it fails is the crop extracted and read again.
    """
    found = _counter_from_boxes(_counter_boxes(snapshot), total)
    if found is not None:
        return found

    left = round(snapshot.width * _COUNTER_LEFT_FRACTION)
    top = round(snapshot.height * _COUNTER_TOP_FRACTION)
    right = round(snapshot.width * _COUNTER_RIGHT_FRACTION)
    bottom = round(snapshot.height * _COUNTER_BOTTOM_FRACTION)
    cropped = snapshot.crop((left, top, right, bottom))
    engine = ocr if ocr is not None else _counter_ocr()
    try:
        boxes = tuple(box.shifted(left, top) for box in engine.read(cropped.image))
    except OcrError:
        return None
    return _counter_from_boxes(boxes, total)


def _read_grid_counter(snapshot: Snapshot) -> CounterState | None:
    """Read the pill only while the grid header proves it is still on screen.

    A scrolled grid can put a product's bare ``3`` under the fixed pill crop.
    Accepting that as ``3/3`` once turned a zero-tap scroll into a completed
    round (#115). The public counter reader remains useful on a pill crop; the
    executor uses this header-guarded wrapper.
    """
    if not is_surprise_grid(snapshot):
        return None
    return read_surprise_counter(snapshot)


def _counter_ocr() -> OcrEngine:
    """Load the crop reader once; it is only needed on frames full OCR fails."""
    global _DEFAULT_COUNTER_OCR
    if _DEFAULT_COUNTER_OCR is None:
        _DEFAULT_COUNTER_OCR = RapidOcrEngine()
    return _DEFAULT_COUNTER_OCR


def _counter_from_boxes(boxes: Sequence[TextBox], total: int) -> CounterState | None:
    for box in boxes:
        match = _COUNTER.search(unicodedata.normalize('NFKC', box.text))
        if match is not None:
            done = int(match.group('done'))
            found_total = int(match.group('total'))
            if found_total > 0 and done <= found_total:
                return CounterState(completed=done, total=found_total, box=box)

    digits = [box for box in boxes if _BARE_DIGIT.match(unicodedata.normalize('NFKC', box.text))]
    if len(digits) == 2:
        digits.sort(key=lambda box: box.center[0])
        matches = [_BARE_DIGIT.match(unicodedata.normalize('NFKC', box.text)) for box in digits]
        if all(match is not None for match in matches):
            value, found_total = (int(match.group('value')) for match in matches if match is not None)
            if found_total == total and 0 <= value <= total:
                return CounterState(completed=value, total=total, box=digits[0])
    return None


def find_next_round(snapshot: Snapshot) -> TextBox | None:
    """Return the page's ``Next round`` control, or ``None``."""
    for box in snapshot.boxes:
        if box.score < DEFAULT_MIN_OCR_SCORE:
            continue
        if normalize(box.text).startswith('next round'):
            return box
    return None


def find_mission_overlay(snapshot: Snapshot) -> TextBox | None:
    """Return the ``Mission Completed`` overlay's label box, or ``None``.

    OCR glues the campaign artwork to the words (``MissionCompletedAir``), so
    the match is on the compact alphanumeric form as well as the spaced label.
    """
    for box in snapshot.boxes:
        if box.score < DEFAULT_MIN_OCR_SCORE:
            continue
        compact = ''.join(character for character in normalize(box.text) if character.isalnum())
        if 'missioncompleted' in compact:
            return box
    return None


def find_back_control(snapshot: Snapshot) -> TextBox | None:
    """Return a visible back chevron when the device cannot press BACK."""
    for box in snapshot.boxes:
        if box.score < DEFAULT_MIN_OCR_SCORE:
            continue
        text = box.text.strip()
        if text in ('<', '\u2039', '\u2190') or normalize(text) in ('back', 'go back'):
            return box
    return None


def is_surprise_grid(snapshot: Snapshot) -> bool:
    """Whether the frame is the grid's page (header plus subtitle, not the row).

    The boost row carries the same title and subtitle, so the row is explicitly
    excluded. Without that, the frame captured before the grid paints answers
    "grid arrived", the row's ``1/2`` badge is not a round counter, and the
    executor reports ``counter_unreadable`` without ever seeing the grid (#100).
    """
    return (
        not is_boost_page(snapshot)
        and contains_text(snapshot, SURPRISE_TITLE)
        and contains_text(snapshot, SURPRISE_SUBTITLE)
    )


def is_item_detail(snapshot: Snapshot) -> bool:
    """Whether the frame is the product detail page opened by a tile.

    The shared surface matcher also accepts the page's ``Item N/n`` counter, so
    a detail page that scrolls its purchase buttons off-screen is still the
    legitimate destination of a tile tap (#100).
    """
    return is_product_detail(snapshot)


def run_surprise_task(
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
    exit_timeout: float = DEFAULT_EXIT_TIMEOUT,
    counter_timeout: float = DEFAULT_COUNTER_TIMEOUT,
    tile_timeout: float = DEFAULT_TILE_TIMEOUT,
    badge_timeout: float = DEFAULT_BADGE_TIMEOUT,
    interval: float = DEFAULT_INTERVAL,
    return_timeout: float = DEFAULT_RETURN_TIMEOUT,
    return_interval: float = DEFAULT_RETURN_INTERVAL,
    return_attempts: int = DEFAULT_RETURN_ATTEMPTS,
    settle: float = 0.0,
    max_rounds: int = MAX_ROUNDS,
    back: Callable[[], None] | None = None,
    scroll: Callable[[Snapshot], bool] | None = None,
    record_round_grid: Callable[[int, Snapshot], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    logger: logging.Logger | None = None,
) -> SurpriseResult:
    """Run one surprise-items task from an allowed roster row.

    The row's action is re-resolved from the current frame, pressed once, and
    the executor waits for the grid. Each round then taps an unopened tile,
    waits for the detail page, presses BACK, and reads the counter before the
    next choice. The counter moving is the round's proof; a grid that merely
    disappears is a failure. Up to ``max_rounds`` rounds are walked, bounded by
    the row's own ``N/2`` badge and the daily state limit.

    ``back`` and ``scroll`` are injectable seams. When they are omitted, BACK
    goes through :meth:`Controller.press_key` and scrolling through
    :meth:`Controller.swipe`, with a visible back chevron as the fallback for a
    device that cannot press keys. ``record_round_grid`` receives each round's
    first frame so a run can retain the image and OCR sidecar behind a tile
    perception failure.
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
    if exit_timeout <= 0:
        raise ValueError(f'exit_timeout must be positive, got {exit_timeout!r}')
    if counter_timeout <= 0:
        raise ValueError(f'counter_timeout must be positive, got {counter_timeout!r}')
    if tile_timeout <= 0:
        raise ValueError(f'tile_timeout must be positive, got {tile_timeout!r}')
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

    rounds_total = badge.total if badge is not None else max_rounds
    rounds_done = badge.completed if badge is not None else 0
    remaining = max(0, min(rounds_total - rounds_done, max_rounds))
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
    round_budget = min(remaining, limit - done_before)

    taps: list[TapRecord] = []
    keys: list[KeyRecord] = []
    swipes: list[SwipeRecord] = []
    round_records: list[RoundRecord] = []
    opened_items: list[str] = []
    completed_rounds = 0
    task_tried = _TriedTiles()
    stop: str | None = None
    current = frame

    try:
        taps.append(
            controller.tap_box(
                action,
                why=f'surprise {spec.key}: open the grid',
                label=action.text,
            ),
        )
        current, arrived, dismiss, stop = _wait_for_surface(
            controller,
            capture,
            corpus,
            _is_grid_surface,
            timeout=arrival_timeout,
            interval=interval,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=log,
            templates=templates,
            taps=taps,
        )
        if dismiss is not None:
            dismissals.append(dismiss)
        if stop is not None:
            return _finish_stopped(
                controller,
                capture,
                corpus,
                navigate,
                spec,
                entry,
                done_before,
                limit,
                stop,
                taps=taps,
                keys=keys,
                swipes=swipes,
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
                verification='grid_not_arrived',
                detail='the grid page never appeared after the row was pressed',
                taps=taps,
                keys=keys,
                swipes=swipes,
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
        if _read_grid_counter(current) is None:
            return _finish_failed(
                controller,
                capture,
                corpus,
                navigate,
                spec,
                entry,
                done_before,
                limit,
                verification='counter_unreadable',
                detail='the grid arrived but its round counter could not be read; no tile was tapped',
                taps=taps,
                keys=keys,
                swipes=swipes,
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

        for round_index in range(1, round_budget + 1):
            record, current, stop = _run_round(
                controller,
                capture,
                corpus,
                current,
                round_index,
                spec=spec,
                task_tried=task_tried,
                templates=templates,
                navigate=navigate,
                route=route,
                arrival_timeout=arrival_timeout,
                exit_timeout=exit_timeout,
                counter_timeout=counter_timeout,
                tile_timeout=tile_timeout,
                return_timeout=return_timeout,
                return_interval=return_interval,
                return_attempts=return_attempts,
                interval=interval,
                settle=settle,
                back=back,
                scroll=scroll,
                record_round_grid=record_round_grid,
                clock=clock,
                sleep=sleep,
                logger=log,
                taps=taps,
                keys=keys,
                swipes=swipes,
                dismissals=dismissals,
            )
            round_records.append(record)
            opened_items.extend(record.items)
            if not record.proven:
                break
            completed_rounds += 1
            if done_before + completed_rounds >= limit:
                break
            if round_index >= round_budget:
                break

            current, stop = _dismiss_mission(
                controller,
                capture,
                corpus,
                current,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=log,
                templates=templates,
                dismissals=dismissals,
                taps=taps,
            )
            next_round = find_next_round(current)
            if next_round is None:
                break
            taps.append(
                controller.tap_box(
                    next_round,
                    why=f'surprise {spec.key}: start the next round',
                    label=next_round.text,
                ),
            )
            current, arrived, dismiss, stop = _wait_for_surface(
                controller,
                capture,
                corpus,
                _is_grid_surface,
                timeout=arrival_timeout,
                interval=interval,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=log,
                templates=templates,
                taps=taps,
            )
            if dismiss is not None:
                dismissals.append(dismiss)
            if stop is not None:
                break
            if not arrived or _read_grid_counter(current) is None:
                return _finish_failed(
                    controller,
                    capture,
                    corpus,
                    navigate,
                    spec,
                    entry,
                    done_before,
                    limit,
                    verification='next_round_not_arrived',
                    detail='Next round was pressed but a fresh grid with a readable counter did not appear',
                    round_records=round_records,
                    rule_path=_rule_path(round_records),
                    taps=taps,
                    keys=keys,
                    swipes=swipes,
                    items=opened_items,
                    dismissals=dismissals,
                    stop=stop,
                    done_after=state.count(spec.key),
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
                SurpriseOutcome.FAILED,
                verification='return_failed',
                detail='the shortcut route did not prove the coins page; the run is not reported complete',
                limit=limit,
                done_before=done_before,
                done_after=state.count(spec.key),
                round_records=round_records,
                rule_path=_rule_path(round_records),
                taps=taps,
                keys=keys,
                swipes=swipes,
                items=opened_items,
                return_state=return_state,
                dismissals=dismissals,
                stop=stop,
                logger=log,
            )

        badge_after: ProgressBadge | None = None
        if completed_rounds > 0 and badge_before is not None:
            current, badge_after = _recheck_badge(
                capture,
                entry.title,
                badge_before,
                completed_rounds,
                snapshot=return_state.snapshot,
                timeout=badge_timeout,
                interval=interval,
                clock=clock,
                sleep=sleep,
            )
            expected = min(badge_before.completed + completed_rounds, badge_before.total)
            if badge_after is not None and badge_after.completed < expected:
                return _result(
                    spec,
                    entry.title,
                    SurpriseOutcome.FAILED,
                    verification='progress_unchanged',
                    detail=progress_unchanged_detail(
                        entry.title,
                        badge_before,
                        badge_after,
                        advanced=completed_rounds,
                    ),
                    limit=limit,
                    done_before=done_before,
                    done_after=state.count(spec.key),
                    round_records=round_records,
                    rule_path=_rule_path(round_records),
                    taps=taps,
                    keys=keys,
                    swipes=swipes,
                    items=opened_items,
                    badge_before=badge_before,
                    badge_after=badge_after,
                    return_state=return_state,
                    dismissals=dismissals,
                    stop=stop,
                    logger=log,
                )

        if completed_rounds > 0:
            for _ in range(completed_rounds):
                state.record(spec.key)
            return _result(
                spec,
                entry.title,
                SurpriseOutcome.COMPLETED,
                verification='counter_complete',
                detail=_completed_detail(round_records),
                limit=limit,
                done_before=done_before,
                done_after=state.count(spec.key),
                round_records=round_records,
                rule_path=_rule_path(round_records),
                taps=taps,
                keys=keys,
                swipes=swipes,
                items=opened_items,
                badge_before=badge_before,
                badge_after=badge_after,
                return_state=return_state,
                dismissals=dismissals,
                stop=stop,
                logger=log,
            )

        return _result(
            spec,
            entry.title,
            SurpriseOutcome.FAILED,
            verification=_failure_verification(round_records[-1]),
            detail=round_records[-1].detail,
            limit=limit,
            done_before=done_before,
            done_after=state.count(spec.key),
            round_records=round_records,
            rule_path=_rule_path(round_records),
            taps=taps,
            keys=keys,
            swipes=swipes,
            items=opened_items,
            return_state=return_state,
            dismissals=dismissals,
            stop=stop,
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


# The issue's public name; ``run_surprise_task`` says what it does at the dispatch point.
run_surprise = run_surprise_task


def _run_round(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    snapshot: Snapshot,
    index: int,
    *,
    spec: AllowedTaskSpec,
    task_tried: _TriedTiles,
    templates: TemplateStore | None,
    arrival_timeout: float,
    exit_timeout: float,
    counter_timeout: float,
    tile_timeout: float,
    interval: float,
    settle: float,
    back: Callable[[], None] | None,
    scroll: Callable[[Snapshot], bool] | None,
    record_round_grid: Callable[[int, Snapshot], None] | None,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
    taps: list[TapRecord],
    keys: list[KeyRecord],
    swipes: list[SwipeRecord],
    dismissals: list[DismissReport],
    navigate: RouteFirer | None = None,
    route: SurfaceRoute = SurfaceRoute.SHORTCUT,
    return_timeout: float = DEFAULT_RETURN_TIMEOUT,
    return_interval: float = DEFAULT_RETURN_INTERVAL,
    return_attempts: int = DEFAULT_RETURN_ATTEMPTS,
) -> tuple[RoundRecord, Snapshot, str | None]:
    """Walk one round until its counter is complete or the round proves stuck."""
    if record_round_grid is not None:
        record_round_grid(index, snapshot)
    first = _read_grid_counter(snapshot)
    total = first.total if first is not None else 3
    before = first.completed if first is not None else None
    current = snapshot
    round_tried = _TriedTiles()
    mode = RULE_PATH_REUSE
    reset_used = False
    inert = 0
    round_taps = 0
    scrolls = 0
    scroll_stable = False
    mission_seen = False
    items: list[str] = []
    skipped_dangerous: list[str] = []
    pending_item: str | None = None
    grid_labels = _grid_labels(snapshot)
    baseline_unknown = False
    last_miss: tuple[int, int] | None = None
    stop: str | None = None
    detail = 'the round counter advanced to its target'

    if first is None:
        return (
            RoundRecord(index, None, None, total, 0, mode, False, 'the round counter could not be read'),
            current,
            None,
        )
    if first.complete:
        return (
            RoundRecord(
                index,
                before,
                before,
                total,
                0,
                mode,
                True,
                'progress_unchanged: the round counter was already complete before a tile was opened',
            ),
            current,
            None,
        )

    current = _wait_for_tiles(
        capture,
        current,
        round_tried,
        timeout=tile_timeout,
        interval=interval,
        clock=clock,
        sleep=sleep,
    )
    grid_labels.update(_grid_labels(current))

    while True:
        mission_seen = mission_seen or find_mission_overlay(current) is not None
        current, stop = _dismiss_mission(
            controller,
            capture,
            corpus,
            current,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=logger,
            templates=templates,
            dismissals=dismissals,
            taps=taps,
        )
        if stop is not None:
            detail = 'the reward interstitial appeared before the round completed'
            break

        counter = _read_grid_counter(current)
        if counter is not None and counter.complete:
            before = counter.completed
            if round_taps == 0 and not mission_seen:
                detail = 'the round counter was complete but no tile was opened; no completion was proven'
            else:
                detail = 'the round counter advanced to its target'
            break

        if round_taps >= MAX_TAPS_PER_ROUND:
            detail = _round_stall_detail(last_miss, total, f'the round used its {MAX_TAPS_PER_ROUND}-tap budget')
            break

        tracker = task_tried if mode == RULE_PATH_RESET else round_tried
        choice = _next_tile(current, tracker)
        for skipped_tile, caption in choice.skipped:
            round_tried.add(skipped_tile)
            task_tried.add(skipped_tile)
            label = f'{skipped_tile.report_label}: {caption.text.strip()}'
            skipped_dangerous.append(label)
            logger.warning(
                '%s',
                json.dumps(
                    {
                        'event': TILE_SKIPPED_DANGEROUS,
                        'key': spec.key,
                        'round': index,
                        'tile': skipped_tile.report_label,
                        'caption': caption.text.strip(),
                        'reason': 'a dangerous caption is painted inside the product photo; the tile was not tapped',
                    },
                    separators=(',', ':'),
                    sort_keys=True,
                ),
            )
        tile = choice.tile
        if tile is None:
            if mode == RULE_PATH_REUSE and not reset_used:
                mode = RULE_PATH_RESET
                reset_used = True
                inert = 0
                continue
            if not scroll_stable:
                before_scroll = _grid_signature(current)
                scrolled, swipe = _scroll(controller, current, scroll, scrolls)
                if scrolled and scrolls < MAX_SCROLLS:
                    if swipe is not None:
                        swipes.append(swipe)
                    scrolls += 1
                    current = _capture(capture)
                    current = _wait_for_tiles(
                        capture,
                        current,
                        tracker,
                        timeout=tile_timeout,
                        interval=interval,
                        clock=clock,
                        sleep=sleep,
                    )
                    grid_labels.update(_grid_labels(current))
                    if before_scroll and _grid_signature(current) == before_scroll:
                        scroll_stable = True
                    continue
            if not grid_labels and round_taps == 0:
                detail = (
                    'the freshly opened grid showed no product tile after the settle; '
                    'no tile was opened and the counter did not advance'
                )
            else:
                detail = _round_stall_detail(
                    last_miss,
                    total,
                    'no unopened product tile was available and the grid could not be scrolled further',
                )
            break

        taps.append(
            controller.tap_box(
                tile.box,
                why=f'surprise {spec.key}: open an unopened product in round {index}',
                label=tile.label,
            ),
        )
        round_taps += 1
        pending_item = tile.report_label
        round_tried.add(tile)
        task_tried.add(tile)

        current, arrived, dismiss, stop = _wait_for_surface(
            controller,
            capture,
            corpus,
            is_item_detail,
            timeout=arrival_timeout,
            interval=interval,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=logger,
            templates=templates,
            taps=taps,
        )
        if dismiss is not None:
            dismissals.append(dismiss)
        if stop is not None:
            detail = 'the reward interstitial appeared before the detail page returned'
            break

        if arrived:
            if not _go_back(controller, current, back, keys=keys, taps=taps):
                detail = 'the product detail page was open but no BACK control was available'
                break
            current, arrived, dismiss, stop = _wait_for_surface(
                controller,
                capture,
                corpus,
                lambda frame: _is_return_surface(frame, grid_labels),
                timeout=exit_timeout,
                interval=interval,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=logger,
                templates=templates,
                taps=taps,
            )
            if dismiss is not None:
                dismissals.append(dismiss)
            if stop is not None:
                detail = 'the reward interstitial appeared while returning from a product'
                break
            if arrived and not is_surprise_grid(current) and find_next_round(current) is None:
                # A product return can land on the headerless scrolled grid
                # (the recorded #164 frames did exactly that). Restore its top
                # before the counter is read; only the surprise header proves
                # the page is the grid. An injected scroll seam is opaque, so it
                # keeps its existing trust of the caller-supplied grid.
                scrolled_return = _is_scrolled_grid(current)
                if scrolled_return:
                    current, restored = _restore_grid_top(controller, capture, current, scroll)
                    if restored is not None:
                        swipes.append(restored)
                known_scrolled = _is_scrolled_grid(current, grid_labels)
                if not is_surprise_grid(current) and not known_scrolled and (scroll is None or not scrolled_return):
                    arrived = False
            if not arrived:
                # BACK normally returns straight to the grid, but a measured
                # misroute can also leave the app on the coin page or the boost
                # sheet. Re-open the grid explicitly rather than spending the
                # round on whatever BACK happened to leave behind (#164).
                recovered, stop = _reenter_grid(
                    controller,
                    capture,
                    corpus,
                    navigate,
                    current,
                    spec=spec,
                    templates=templates,
                    route=route,
                    arrival_timeout=arrival_timeout,
                    return_timeout=return_timeout,
                    return_interval=return_interval,
                    return_attempts=return_attempts,
                    interval=interval,
                    settle=settle,
                    clock=clock,
                    sleep=sleep,
                    logger=logger,
                    taps=taps,
                    swipes=swipes,
                    dismissals=dismissals,
                )
                if stop is not None:
                    detail = 'the reward interstitial appeared while re-entering the grid after a misrouted BACK'
                    break
                if recovered is None:
                    detail = (
                        'BACK did not return to the grid and the shortcut/boost route could not re-enter it; '
                        'no completion was recorded'
                    )
                    break
                current = recovered
                reentered = _read_grid_counter(current)
                if before is not None and reentered is not None and reentered.completed < before:
                    # Re-entering through the shortcut can reset the page's
                    # counter. Keep only the post-reset credits; otherwise the
                    # round would report pre-reset products and miss the ones
                    # that actually completed it.
                    before = reentered.completed
                    items.clear()
                    round_tried = _TriedTiles()
                    inert = 0
                    pending_item = None
                    baseline_unknown = False
                    last_miss = None
            current, stop = _dismiss_mission(
                controller,
                capture,
                corpus,
                current,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=logger,
                templates=templates,
                dismissals=dismissals,
                taps=taps,
            )
        scrolled_grid = _is_scrolled_grid(current, grid_labels)
        if not arrived and not scrolled_grid and not is_surprise_grid(current) and find_next_round(current) is None:
            detail = 'the grid disappeared without the counter advancing; no completion was recorded'
            break

        if scrolled_grid and not is_surprise_grid(current):
            # The card grid scrolled its header and pill out of view. Bring the
            # observed grid back to the top so the counter can be read and the
            # next round starts from the same layout.
            current, restored = _restore_grid_top(controller, capture, current, scroll)
            if restored is not None:
                swipes.append(restored)

        current, after = _read_counter_after_dwell(
            capture,
            current,
            before,
            timeout=counter_timeout,
            interval=interval,
            clock=clock,
            sleep=sleep,
        )
        if after is None:
            # The missed read hides whether this tile credited. Do not let the
            # next successful read attribute that hidden credit to the next tap.
            pending_item = None
            baseline_unknown = True
            continue

        if baseline_unknown:
            # Re-establish the baseline without naming a tile: the advance may
            # belong to an earlier tap whose own read failed.
            before = after.completed
            baseline_unknown = False
            pending_item = None
            inert = 0
            if after.complete:
                detail = 'the round counter advanced to its target'
                break
            continue

        assert before is not None
        if after.completed > before:
            delta = after.completed - before
            if delta == 1 and pending_item is not None:
                items.append(pending_item)
            pending_item = None
            before = after.completed
            inert = 0
            last_miss = None
            if after.complete:
                detail = 'the round counter advanced to its target'
                break
            continue

        pending_item = None
        last_miss = (before + 1, after.completed)
        inert += 1
        if inert >= MAX_INERT_TAPS:
            if mode == RULE_PATH_REUSE and not reset_used:
                mode = RULE_PATH_RESET
                reset_used = True
                inert = 0
                continue
            if not scroll_stable:
                before_scroll = _grid_signature(current)
                scrolled, swipe = _scroll(controller, current, scroll, scrolls)
                if scrolled and scrolls < MAX_SCROLLS:
                    if swipe is not None:
                        swipes.append(swipe)
                    scrolls += 1
                    current = _capture(capture)
                    current = _wait_for_tiles(
                        capture,
                        current,
                        tracker,
                        timeout=tile_timeout,
                        interval=interval,
                        clock=clock,
                        sleep=sleep,
                    )
                    grid_labels.update(_grid_labels(current))
                    if before_scroll and _grid_signature(current) == before_scroll:
                        scroll_stable = True
                    inert = 0
                    continue

    return (
        RoundRecord(
            index=index,
            counter_before=first.completed if first is not None else None,
            counter_after=before,
            total=total,
            taps=round_taps,
            rule_path=mode,
            complete=before is not None and before >= total,
            detail=detail,
            scrolls=scrolls,
            items=tuple(items),
            skipped_dangerous=tuple(skipped_dangerous),
        ),
        current,
        stop,
    )


def _reenter_grid(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    navigate: RouteFirer | None,
    snapshot: Snapshot,
    *,
    spec: AllowedTaskSpec,
    templates: TemplateStore | None,
    route: SurfaceRoute,
    arrival_timeout: float,
    return_timeout: float,
    return_interval: float,
    return_attempts: int,
    interval: float,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
    taps: list[TapRecord],
    dismissals: list[DismissReport],
    swipes: list[SwipeRecord] | None = None,
) -> tuple[Snapshot | None, str | None]:
    """Re-open the surprise grid after BACK lands somewhere other than it.

    The measured product page returns to the grid with one BACK, but a misrouted
    BACK can leave the app on the coin page or the boost sheet. In that case the
    grid is re-entered through the same explicit route every task return uses:
    the shortcut proves the coin page, the boost sheet (or its inline rows)
    supplies the row, and the row's own ``GO`` opens the grid. No tile is tapped
    from a guessed coordinate and no purchase control is involved (#164).

    Returns the settled grid frame, or ``None`` when the route cannot prove one;
    a reward interstitial is returned as the caller's ``stop``.
    """
    swipe_records = [] if swipes is None else swipes
    if navigate is None:
        _warn_grid_reentry(logger, spec.key, 'no navigation route was supplied for the explicit grid re-entry')
        return None, None
    try:
        returned = return_to_coins_page(
            controller,
            capture,
            corpus,
            navigate,
            snapshot=snapshot,
            templates=templates,
            route=route,
            timeout=return_timeout,
            interval=return_interval,
            attempts=return_attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=logger,
            on_dismiss=dismissals.append,
        )
    except RunAborted, RunInterrupted:
        raise
    except (StateTimeout, InputError) as exc:
        _warn_grid_reentry(logger, spec.key, f'{type(exc).__name__}: {exc}')
        return None, None
    if not returned.returned or returned.snapshot is None:
        _warn_grid_reentry(logger, spec.key, 'the shortcut route did not prove the coins page')
        return None, None

    current = returned.snapshot
    if contains_text(current, REDEEM_MARKER):
        return current, REDEEM_INTERSTITIAL
    row, action, _ = find_task_row(current, SURPRISE_TITLE)
    if row is None or action is None:
        try:
            boost = open_boost_list(
                controller,
                capture,
                corpus,
                templates=templates,
                timeout=DEFAULT_BOOST_TIMEOUT,
                interval=interval,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=logger,
                on_dismiss=dismissals.append,
            )
        except RunAborted, RunInterrupted:
            raise
        except (StateTimeout, InputError) as exc:
            _warn_grid_reentry(logger, spec.key, f'{type(exc).__name__}: {exc}')
            return None, None
        if boost.tap is not None:
            taps.append(boost.tap)
        if boost.retap is not None:
            taps.append(boost.retap)
        current = boost.snapshot
        row, action, _ = find_task_row(current, SURPRISE_TITLE)
        if row is None or action is None:
            # The boost sheet scrolls and rotates; the row may be below the
            # first viewport even when the app still offers it.
            try:
                search = scroll_until_found(
                    controller,
                    lambda: _capture(capture),
                    lambda frame: find_task_row(frame, SURPRISE_TITLE)[1],
                    initial=current,
                    why=f'surprise {spec.key}: find the surprise row below the fold',
                )
            except InputError as exc:
                _warn_grid_reentry(logger, spec.key, f'{type(exc).__name__}: {exc}')
                return None, None
            swipe_records.extend(search.swipes)
            if search.target is not None:
                current = search.last
                row, action, _ = find_task_row(current, SURPRISE_TITLE)

    if row is None or action is None:
        _warn_grid_reentry(logger, spec.key, f'{SURPRISE_TITLE!r} is not on the returned task surface')
        return None, None

    taps.append(
        controller.tap_box(
            action,
            why=f'surprise {spec.key}: re-enter the grid after a misrouted BACK',
            label=action.text,
        ),
    )
    current, arrived, dismiss, stop = _wait_for_surface(
        controller,
        capture,
        corpus,
        _is_grid_surface,
        timeout=arrival_timeout,
        interval=interval,
        settle=settle,
        clock=clock,
        sleep=sleep,
        logger=logger,
        templates=templates,
        taps=taps,
    )
    if dismiss is not None:
        dismissals.append(dismiss)
    if stop is not None:
        return current, stop
    if not arrived or _read_grid_counter(current) is None:
        _warn_grid_reentry(logger, spec.key, 'the row did not open a grid with a readable counter')
        return None, None

    logger.info(
        '%s',
        json.dumps(
            {
                'event': 'surprise_grid_reentry',
                'key': spec.key,
                'detail': 'BACK left the grid; the shortcut/boost route re-entered it',
            },
            separators=(',', ':'),
            sort_keys=True,
        ),
    )
    return current, None


def _warn_grid_reentry(logger: logging.Logger, key: str | None, detail: str) -> None:
    """Record why the explicit route could not put the executor back on the grid."""
    logger.warning(
        '%s',
        json.dumps(
            {'event': 'surprise_grid_reentry_failed', 'key': key, 'detail': detail},
            separators=(',', ':'),
            sort_keys=True,
        ),
    )


def _next_tile(snapshot: Snapshot, tried: _TriedTiles) -> _TileChoice:
    """Pick the first untried card whose photograph carries no purchase caption.

    A dangerous caption on a card is merchant artwork, not a control (#139),
    but the card is still avoided: the executor taps another tile instead of
    aborting the night over a photograph.
    """
    dangerous = {tile: caption for caption, tile in find_imagery_captions(snapshot)}
    skipped: list[tuple[SurpriseTile, TextBox]] = []
    for tile in find_surprise_tiles(snapshot):
        if tried.contains(tile):
            continue
        caption = dangerous.get(tile)
        if caption is not None:
            skipped.append((tile, caption))
            continue
        return _TileChoice(tile, tuple(skipped))
    return _TileChoice(None, tuple(skipped))


def _wait_for_tiles(
    capture: CaptureSource,
    snapshot: Snapshot,
    tried: _TriedTiles,
    *,
    timeout: float,
    interval: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> Snapshot:
    """Let a newly opened or scrolled grid paint its cards before searching.

    The grid header and round pill are server-rendered before product tiles. A
    first frame with no tile is therefore inconclusive, not an empty grid. The
    wait is bounded and returns immediately as soon as an untried card appears.
    """
    current = snapshot
    if _next_tile(current, tried).tile is not None:
        return current
    deadline = clock() + timeout
    while clock() < deadline:
        remaining = deadline - clock()
        sleep(min(interval, max(0.0, remaining)))
        current = _capture(capture)
        if _next_tile(current, tried).tile is not None:
            break
    return current


def _round_stall_detail(last_miss: tuple[int, int] | None, total: int, fallback: str) -> str:
    """Name an unchanged counter with the expected and observed values.

    A tile that was tapped and did not move the counter is the evidence for
    ``progress_unchanged``; a round with no tap at all keeps its own sentence.
    """
    if last_miss is None:
        return fallback
    expected, observed = last_miss
    return f'progress_unchanged: expected {expected}/{total}, saw {observed}/{total}'


def _read_counter_after_dwell(
    capture: CaptureSource,
    snapshot: Snapshot,
    before: int | None,
    *,
    timeout: float,
    interval: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[Snapshot, CounterState | None]:
    """Poll the round pill until BACK's credit lands and stops moving.

    The first read is the frame that proved the grid returned. A credit can
    arrive in more than one increment, and the last increment can land after a
    fixed dwell, so the value must stay quiet for ``_COUNTER_STABLE_HOLD``
    before it is final. A target reading returns immediately: no further credit
    is needed. Reaching the deadline with an unchanged or short counter is still
    the evidence for ``progress_unchanged`` (#100, #115).
    """
    current = snapshot
    reading = _read_grid_counter(current)
    if before is None:
        return current, reading
    deadline = clock() + timeout
    last = reading.completed if reading is not None else None
    changed_at = clock() if last is not None and last > before else None
    while True:
        now = clock()
        if reading is not None and reading.complete:
            return current, reading
        if changed_at is not None and now - changed_at >= _COUNTER_STABLE_HOLD:
            return current, reading
        if now >= deadline:
            return current, reading
        sleep(min(interval, max(0.0, deadline - now)))
        current = _capture(capture)
        reading = _read_grid_counter(current)
        value = reading.completed if reading is not None else None
        if value != last:
            changed_at = clock() if value is not None and value > before else None
            last = value


def _restore_grid_top(
    controller: Controller,
    capture: CaptureSource,
    snapshot: Snapshot,
    scroll: Callable[[Snapshot], bool] | None,
) -> tuple[Snapshot, SwipeRecord | None]:
    """Scroll a scrolled grid back down until its header and pill are visible.

    The counter is at the top of the page, so a grid scrolled for unseen products
    must be restored before its credit can be read. An injected scroll seam is
    opaque in direction, so it is left alone; the executor still names the
    scrolled grid and does not burn safety sightings on it.
    """
    if scroll is not None or is_surprise_grid(snapshot):
        return snapshot, None
    current = snapshot
    for _ in range(MAX_SCROLLS):
        start = (current.width / 2.0, current.height * _SCROLL_END_FRACTION)
        end = (current.width / 2.0, current.height * _SCROLL_START_FRACTION)
        try:
            record = controller.swipe(
                start,
                end,
                why='surprise: restore the grid header after a scroll',
                label='grid restore',
            )
        except InputError:
            return current, None
        current = _capture(capture)
        if is_surprise_grid(current):
            return current, record
    return current, None


def _recheck_badge(
    capture: CaptureSource,
    title: str,
    before: ProgressBadge | None,
    advanced: int,
    *,
    snapshot: Snapshot | None,
    timeout: float,
    interval: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[Snapshot, ProgressBadge | None]:
    """Poll the returned row's badge until it advances, or the bound ends.

    The row can still show the pre-task badge on the first returned frame while
    the server catches up. The badge is the independent positive signal for the
    task, so it gets its own short dwell. An absent row stays ``None`` and is not
    invented: the counter evidence already proves the round, and absence on the
    partial inline coin page is not proof either way.
    """
    current = snapshot if snapshot is not None else _capture(capture)
    observed = _row_badge(current, title)
    if before is None:
        return current, observed
    expected = min(before.completed + advanced, before.total)
    deadline = clock() + timeout
    while observed is not None and observed.completed < expected and clock() < deadline:
        remaining = deadline - clock()
        sleep(min(interval, max(0.0, remaining)))
        current = _capture(capture)
        observed = _row_badge(current, title)
    return current, observed


def _row_badge(snapshot: Snapshot, title: str) -> ProgressBadge | None:
    _, _, badge = find_task_row(snapshot, title)
    return badge


def _leave_detail_if_open(controller: Controller, capture: CaptureSource, snapshot: Snapshot) -> Snapshot:
    """Return the grid frame after a mission-overlay tap that fell through to a product.

    The overlay is dismissed by a tap on its artwork. On the live page that tap
    can land on a product card underneath as the overlay disappears, opening a
    detail page. Treating the resulting page as "BACK did not return" would throw
    away a round whose counter is already complete, so BACK is issued once and
    the next frame is read.
    """
    if not is_item_detail(snapshot):
        return snapshot
    try:
        controller.press_key('KEYCODE_BACK', why='surprise: leave the product opened by the overlay tap')
    except InputError:
        control = find_back_control(snapshot)
        if control is None:
            return snapshot
        controller.tap_box(control, why='surprise: leave the product opened by the overlay tap', label=control.text)
    return _capture(capture)


def _go_back(
    controller: Controller,
    snapshot: Snapshot,
    back: Callable[[], None] | None,
    *,
    keys: list[KeyRecord],
    taps: list[TapRecord],
) -> bool:
    """Leave the product page by BACK, or by the visible chevron as a fallback."""
    if back is not None:
        back()
        return True
    try:
        keys.append(controller.press_key('KEYCODE_BACK', why='surprise: leave the product detail page'))
        return True
    except InputError:
        control = find_back_control(snapshot)
        if control is None:
            return False
        taps.append(controller.tap_box(control, why='surprise: leave the product detail page', label=control.text))
        return True


def _scroll(
    controller: Controller,
    snapshot: Snapshot,
    scroll: Callable[[Snapshot], bool] | None,
    already: int,
) -> tuple[bool, SwipeRecord | None]:
    """Scroll the grid once, returning whether it happened and its real record."""
    if already >= MAX_SCROLLS:
        return False, None
    if scroll is not None:
        return scroll(snapshot), None
    start = (snapshot.width / 2.0, snapshot.height * _SCROLL_START_FRACTION)
    end = (snapshot.width / 2.0, snapshot.height * _SCROLL_END_FRACTION)
    try:
        return True, controller.swipe(
            start,
            end,
            why='surprise: search the grid for unopened products',
            label='grid scroll',
        )
    except InputError:
        return False, None


def _grid_signature(snapshot: Snapshot) -> tuple[tuple[str, int, float], ...]:
    """What the grid shows now, so a scroll that changes nothing can stop."""
    return tuple((tile.normalized_label, tile.column, tile.box.bbox[1]) for tile in find_surprise_tiles(snapshot))


def _dismiss_mission(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    snapshot: Snapshot,
    *,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
    templates: TemplateStore | None,
    dismissals: list[DismissReport],
    taps: list[TapRecord],
) -> tuple[Snapshot, str | None]:
    """Tap the ``Mission Completed`` overlay away and keep the new frame.

    The overlay is a page state, not a corpus popup (the corpus has no family
    for it), so it is handled here with the spec's own action: a tap anywhere on
    the overlay.
    """
    current = snapshot
    for _ in range(3):
        overlay = find_mission_overlay(current)
        if overlay is None:
            return current, None
        taps.append(
            controller.tap_box(
                overlay,
                why='surprise: dismiss the Mission Completed overlay',
                label=overlay.text,
            ),
        )
        if settle:
            sleep(settle)
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
        if report is not None:
            dismissals.append(report)
        if stop is not None:
            return current, stop
        current = report.frames[-1].snapshot if report is not None and report.frames else _capture(capture)
        current = _leave_detail_if_open(controller, capture, current)
    return current, None


def _wait_for_surface(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    target: Callable[[Snapshot], bool],
    *,
    timeout: float,
    interval: float,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
    templates: TemplateStore | None,
    taps: list[TapRecord] | None = None,
) -> tuple[Snapshot, bool, DismissReport | None, str | None]:
    """Poll until the frame reaches ``target`` or the budget runs out.

    A ``Mission Completed`` overlay is dismissed as the page state it is; other
    popups go through the corpus. The redeem interstitial is surfaced as a stop
    rather than a failure, because it can follow a paid round.
    """
    started = clock()
    deadline = started + timeout
    current = _capture(capture)
    dismiss: DismissReport | None = None
    overlay_taps = 0
    while True:
        overlay = find_mission_overlay(current)
        if overlay is not None and overlay_taps < 3:
            tap = controller.tap_box(
                overlay,
                why='surprise: dismiss the Mission Completed overlay',
                label=overlay.text,
            )
            if taps is not None:
                taps.append(tap)
            overlay_taps += 1
            if settle:
                sleep(settle)
            current = _leave_detail_if_open(controller, capture, _capture(capture))
            continue

        if target(current):
            return current, True, dismiss, None
        classification = classify(current)
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
                return current, False, dismiss, stop
            current = report.frames[-1].snapshot if report is not None and report.frames else _capture(capture)
            continue
        if classification.state is ScreenState.LOADING:
            now = clock()
            if now >= deadline:
                return current, False, dismiss, None
            sleep(min(interval, max(0.0, deadline - now)))
            current = _capture(capture)
            continue
        now = clock()
        if now >= deadline:
            return current, False, dismiss, None
        sleep(min(interval, max(0.0, deadline - now)))
        current = _capture(capture)


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
        if find_mission_overlay(frame) is not None:
            return frame, True, dismiss
        state = classify(frame).state
        if state is ScreenState.LOADING:
            remaining = deadline - clock()
            if remaining <= 0:
                return frame, False, dismiss
            waiting = wait_for_text_gone(
                capture,
                _is_loading,
                timeout=remaining,
                interval=interval,
                initial=frame,
                clock=clock,
                sleep=sleep,
            )
            frame = waiting.frame
            if waiting.timed_out:
                return frame, False, dismiss
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
    """Run the one popup sweep and translate the unclosable case."""
    try:
        report = dismiss_after(
            controller,
            capture,
            _without_boost_sheet(corpus),
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


def _without_boost_sheet(corpus: PopupCorpus) -> PopupCorpus:
    """Never let a sweep close the task list the run was opened from."""
    families = tuple(family for family in corpus.families if family.anchor != BOOST_MARKER)
    if len(families) == len(corpus.families):
        return corpus
    return replace(corpus, families=families)


def _allowed_spec(entry: RosterEntry) -> AllowedTaskSpec:
    """Return the entry's allowed spec, refusing anything the dispatch point missed."""
    spec = entry.spec
    if not entry.allowed or spec is None or entry.dispatch_target is None:
        reason = entry.reason or 'no allowed action is exposed'
        raise TaskError(f'{entry.title!r} is not dispatchable: {reason}')
    if spec.category is not TaskCategory.ITEM_TAP:
        raise TaskError(f'{entry.title!r} is {spec.category.value!r}, not an item_tap task')
    authorization_of(entry)
    return spec


def _is_task_surface(snapshot: Snapshot) -> bool:
    return is_boost_page(snapshot) or _is_coin_page(snapshot)


def _is_coin_page(snapshot: Snapshot) -> bool:
    return all(contains_text(snapshot, marker) for marker in COIN_PAGE_MARKERS)


def _is_loading(snapshot: Snapshot) -> bool:
    """The predicate the settle wait polls: the frame is still rendering."""
    return classify(snapshot).state is ScreenState.LOADING


def _is_grid_surface(snapshot: Snapshot) -> bool:
    return is_surprise_grid(snapshot)


def _is_round_surface(snapshot: Snapshot, known_labels: set[str]) -> bool:
    return (
        is_surprise_grid(snapshot) or _is_scrolled_grid(snapshot, known_labels) or find_next_round(snapshot) is not None
    )


def _is_return_surface(snapshot: Snapshot, known_labels: set[str]) -> bool:
    """A product return may land on the header or a headerless scrolled grid.

    The recorded #164 miss was a valid scrolled grid whose new cards were not in
    the labels read before the tap. The structural check accepts it, and the
    caller restores the top and proves the surprise header before reading the
    counter. The explicit shortcut route remains the fallback for any other
    landing.
    """
    state = classify(snapshot).state
    return _is_round_surface(snapshot, known_labels) or (
        state in (ScreenState.PAGE, ScreenState.UNKNOWN) and _is_scrolled_grid(snapshot)
    )


def _grid_labels(snapshot: Snapshot) -> set[str]:
    """The card labels this frame shares with the grid already read."""
    return {tile.normalized_label for tile in find_surprise_tiles(snapshot)}


def _is_scrolled_grid(snapshot: Snapshot, known_labels: set[str] | None = None) -> bool:
    """Whether a headerless frame is the same grid, scrolled.

    The guard passes no known labels and checks only card structure: two columns
    with their price lines, or one bottom-column card with a price. The executor
    passes the labels it already read, so a coupon/product page it lands on is
    not a grid and cannot receive more title taps (#100).
    """
    if is_surprise_grid(snapshot) or is_product_detail(snapshot) or is_coin_store(snapshot) or is_boost_page(snapshot):
        return False
    tiles = find_surprise_tiles(snapshot)
    if not tiles:
        return False
    prices = sum(1 for box in snapshot.boxes if box.score >= DEFAULT_MIN_OCR_SCORE and _PRICE.search(box.text))
    two_columns = {tile.column for tile in tiles} == {0, 1}
    if known_labels is None:
        # The guard has no round history; card prices are the structure it can
        # verify without them.
        return prices >= (2 if two_columns else 1)
    if two_columns:
        return sum(1 for tile in tiles if tile.normalized_label in known_labels) >= 2
    # The bottom of an odd grid can show only one column. A known card is still
    # enough identity there; an unfamiliar page is not accepted on one label.
    # A known card may have no price yet: the tile resolver deliberately keeps a
    # title whose price/shipping anchor has not painted.
    return all(tile.normalized_label in known_labels for tile in tiles)


def _header_bottom(snapshot: Snapshot) -> float:
    bottom = 0.0
    for box in snapshot.boxes:
        text = normalize(box.text)
        if normalize(SURPRISE_TITLE) in text or 'tap 3 items' in text:
            bottom = max(bottom, box.bbox[3])
    return bottom if bottom > 0 else snapshot.height * 0.12


def _is_product_title(box: TextBox) -> bool:
    if box.score < DEFAULT_MIN_OCR_SCORE:
        return False
    text = box.text.strip()
    if not text or not any(character.isalpha() for character in text):
        return False
    if _PRICE.search(text):
        return False
    normalized = normalize(text)
    return not any(part in normalized for part in _NON_PRODUCT_PARTS)


def _is_card_anchor(box: TextBox) -> bool:
    """Whether a line belongs to the price/shipping row under a product title."""
    if box.score < DEFAULT_MIN_OCR_SCORE:
        return False
    text = box.text.strip()
    if not text:
        return False
    if _PRICE.search(text):
        return True
    normalized = normalize(text)
    return any(part in normalized for part in ('free shipping', 'orders', 'sold'))


def _titles_above_anchors(
    titles: Sequence[TextBox],
    anchors: Sequence[TextBox],
    *,
    group_gap: float,
) -> list[TextBox]:
    """Pick one title above each card's price/shipping block, without duplicates.

    A card has several anchor lines (``Free shipping``, the price, the discount)
    stacked together. Treating each as a separate card would pick the nearest
    branding label for the upper line and the real title for the lower one; the
    anchors are therefore grouped first, and the title above the group's lowest
    line is the card's title.
    """
    ordered_titles = sorted(titles, key=lambda box: (box.bbox[1], box.bbox[0]))
    ordered_anchors = sorted(anchors, key=lambda box: (box.bbox[1], box.bbox[0]))
    groups: list[list[TextBox]] = []
    for anchor in ordered_anchors:
        if groups and anchor.bbox[1] - max(item.bbox[3] for item in groups[-1]) <= group_gap:
            groups[-1].append(anchor)
        else:
            groups.append([anchor])

    chosen: list[TextBox] = []
    seen: set[tuple[str, int, int]] = set()
    for group in groups:
        lowest = max(group, key=lambda box: box.bbox[3])
        above = [box for box in ordered_titles if box.bbox[3] <= lowest.bbox[1]]
        if not above:
            continue
        source = above[-1]
        identity = (normalize(source.text), round(source.bbox[0]), round(source.bbox[1]))
        if identity in seen:
            continue
        seen.add(identity)
        chosen.append(source)
    return chosen


def _first_title_below(titles: Sequence[TextBox], anchor_bottom: float) -> TextBox | None:
    """The next card's title when its price row is below the visible grid.

    Only a multi-word line is taken: a lone uppercase token just below a card
    is usually branding on the next image, while the visible title of a card
    whose price has not painted yet carries the product name.
    """
    below = sorted(
        (box for box in titles if box.bbox[1] > anchor_bottom),
        key=lambda box: (box.bbox[1], box.bbox[0]),
    )
    for box in below:
        text = box.text.strip()
        if len(text) >= 12 and ' ' in text:
            return box
    return None


def _tile_from_source(
    snapshot: Snapshot,
    source: TextBox,
    column: int,
    band_left: float,
    band_right: float,
    split: float,
) -> SurpriseTile | None:
    """Clip one title to its column and make it a tap target."""
    left = max(source.bbox[0], band_left)
    right = min(source.bbox[2], band_right)
    if right - left <= 0 or source.height <= 0:
        return None
    clipped = TextBox(
        text=source.text,
        box=(
            (left, source.bbox[1]),
            (right, source.bbox[1]),
            (right, source.bbox[3]),
            (left, source.bbox[3]),
        ),
        score=source.score,
    )
    shared = source.bbox[0] < split < source.bbox[2]
    center_y = (clipped.bbox[1] + clipped.bbox[3]) / 2.0
    bucket = (
        round(center_y / snapshot.height / _POSITION_BUCKET_FRACTION),
        column,
    )
    return SurpriseTile(
        label=source.text.strip(),
        box=clipped,
        column=column,
        bucket=bucket,
        shared_label=shared,
    )


def _cluster_titles(boxes: Sequence[TextBox], gap_limit: float) -> tuple[tuple[TextBox, ...], ...]:
    ordered = sorted(boxes, key=lambda box: (box.bbox[1], box.bbox[0]))
    clusters: list[tuple[TextBox, ...]] = []
    current: list[TextBox] = []
    bottom = 0.0
    for box in ordered:
        if current and box.bbox[1] - bottom > gap_limit:
            clusters.append(tuple(current))
            current = []
        current.append(box)
        bottom = max(bottom, box.bbox[3]) if current else box.bbox[3]
    if current:
        clusters.append(tuple(current))
    return tuple(clusters)


def _counter_boxes(snapshot: Snapshot) -> list[TextBox]:
    left = snapshot.width * _COUNTER_LEFT_FRACTION
    right = snapshot.width * _COUNTER_RIGHT_FRACTION
    top = snapshot.height * _COUNTER_TOP_FRACTION
    bottom = snapshot.height * _COUNTER_BOTTOM_FRACTION
    boxes = [
        box
        for box in snapshot.boxes
        if box.score >= DEFAULT_MIN_OCR_SCORE and left <= box.center[0] <= right and top <= box.center[1] <= bottom
    ]
    return sorted(boxes, key=lambda box: box.center[0])


def _proven_rounds(records: Sequence[RoundRecord]) -> int:
    """Count only rounds whose counter advanced to the target during this visit."""
    return sum(record.proven for record in records)


def _rule_path(records: Sequence[RoundRecord]) -> str:
    return RULE_PATH_RESET if any(record.rule_path == RULE_PATH_RESET for record in records) else RULE_PATH_REUSE


def _failure_verification(record: RoundRecord) -> str:
    detail = record.detail.lower()
    if 'progress_unchanged' in detail:
        return 'progress_unchanged'
    if 'no tile was opened' in detail:
        return 'tile_not_opened'
    if 'counter could not be read' in detail:
        return 'counter_unreadable'
    if 'grid disappeared' in detail:
        return 'grid_disappeared'
    if 'back' in detail:
        return 'back_not_reached'
    if 'interstitial' in detail:
        return 'redeem_interstitial'
    if 'no unopened product tile' in detail:
        return 'counter_stalled'
    return 'counter_stalled'


def _completed_detail(records: Sequence[RoundRecord]) -> str:
    completed = [record for record in records if record.proven]
    deltas = ', '.join(record.counter_change for record in completed)
    labels = [label for record in completed for label in record.items]
    detail = f'{len(completed)} round(s) completed with counter evidence ({deltas})'
    if labels:
        quoted = ', '.join(repr(label) for label in labels)
        detail = f'{detail}; distinct items: {quoted}'
    return detail


def _skipped(
    spec: AllowedTaskSpec,
    title: str,
    done: int,
    limit: int,
    *,
    verification: str,
    detail: str,
) -> SurpriseResult:
    return SurpriseResult(
        key=spec.key,
        title=title,
        outcome=SurpriseOutcome.SKIPPED,
        verification=verification,
        detail=detail,
        limit=limit,
        done_today_before=done,
        done_today_after=done,
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
    route: SurfaceRoute,
    templates: TemplateStore | None,
    return_timeout: float,
    return_interval: float,
    return_attempts: int,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
    round_records: Sequence[RoundRecord] = (),
    rule_path: str = RULE_PATH_REUSE,
    taps: Sequence[TapRecord] = (),
    keys: Sequence[KeyRecord] = (),
    swipes: Sequence[SwipeRecord] = (),
    items: Sequence[str] = (),
    dismissals: Sequence[DismissReport] = (),
    stop: str | None = None,
    done_after: int | None = None,
) -> SurpriseResult:
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
    done_now = done_before if done_after is None else done_after
    result = SurpriseResult(
        key=spec.key,
        title=entry.title,
        outcome=SurpriseOutcome.FAILED,
        verification=verification,
        detail=detail,
        limit=limit,
        done_today_before=done_before,
        done_today_after=done_now,
        rounds=_proven_rounds(round_records),
        round_records=tuple(round_records),
        rule_path=rule_path,
        taps=tuple(taps),
        keys=tuple(keys),
        swipes=tuple(swipes),
        items=tuple(items),
        return_state=return_state,
        dismissals=tuple(dismissals),
        stop=stop,
        recorded=False,
    )
    _log_result(logger, result)
    return result


def _finish_stopped(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    navigate: RouteFirer,
    spec: AllowedTaskSpec,
    entry: RosterEntry,
    done_before: int,
    limit: int,
    stop: str,
    *,
    taps: Sequence[TapRecord],
    keys: Sequence[KeyRecord],
    swipes: Sequence[SwipeRecord],
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
) -> SurpriseResult:
    """Stop on the redeem interstitial without claiming a counter that was never read."""
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
    verification = 'redeem_interstitial'
    detail = 'the reward interstitial appeared before a round counter was proven'
    if not return_state.returned:
        verification = 'return_failed'
        detail = f'{detail}; the shortcut route did not prove the coins page'
    result = SurpriseResult(
        key=spec.key,
        title=entry.title,
        outcome=SurpriseOutcome.FAILED,
        verification=verification,
        detail=detail,
        limit=limit,
        done_today_before=done_before,
        done_today_after=done_before,
        taps=tuple(taps),
        keys=tuple(keys),
        swipes=tuple(swipes),
        return_state=return_state,
        dismissals=tuple(dismissals),
        stop=stop,
    )
    _log_result(logger, result)
    return result


def _result(
    spec: AllowedTaskSpec,
    title: str,
    outcome: SurpriseOutcome,
    *,
    verification: str,
    detail: str,
    limit: int,
    done_before: int,
    done_after: int,
    round_records: Sequence[RoundRecord],
    rule_path: str,
    taps: Sequence[TapRecord],
    keys: Sequence[KeyRecord],
    swipes: Sequence[SwipeRecord],
    items: Sequence[str],
    return_state: ReturnResult | None,
    dismissals: Sequence[DismissReport],
    stop: str | None,
    logger: logging.Logger,
    badge_before: ProgressBadge | None = None,
    badge_after: ProgressBadge | None = None,
) -> SurpriseResult:
    result = SurpriseResult(
        key=spec.key,
        title=title,
        outcome=outcome,
        verification=verification,
        detail=detail,
        limit=limit,
        done_today_before=done_before,
        done_today_after=done_after,
        rounds=_proven_rounds(round_records),
        round_records=tuple(round_records),
        rule_path=rule_path,
        taps=tuple(taps),
        keys=tuple(keys),
        swipes=tuple(swipes),
        items=tuple(items),
        badge_before=badge_before,
        badge_after=badge_after,
        return_state=return_state,
        dismissals=tuple(dismissals),
        stop=stop,
        recorded=outcome is SurpriseOutcome.COMPLETED and done_after > done_before,
    )
    _log_result(logger, result)
    return result


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
) -> None:
    try:
        return_to_coins_page(
            controller,
            capture,
            corpus,
            navigate,
            snapshot=None,
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
    except RunAborted, RunInterrupted:
        raise
    except Exception as exc:
        logger.warning(
            '%s',
            json.dumps(
                {'event': 'surprise_return_failed', 'error': f'{type(exc).__name__}: {exc}'},
                separators=(',', ':'),
                sort_keys=True,
            ),
        )


def _log_result(logger: logging.Logger, result: SurpriseResult) -> None:
    logger.info(
        '%s',
        json.dumps(
            {
                'event': 'task_result',
                'key': result.key,
                'outcome': result.outcome.value,
                'verification': result.verification,
                'rounds': result.rounds,
                'rule_path': result.rule_path,
                'recorded': result.recorded,
            },
            separators=(',', ':'),
            sort_keys=True,
        ),
    )


def _capture(source: CaptureSource) -> Snapshot:
    if callable(source):
        return source()
    return source.capture()
