"""Which task surface a frame shows, and which route produced it (issue #38).

The same ``AdcActivity`` renders different coin-page variants, and the task
rows are conditional. A runner that assumes rows are present waits on an empty
page and then reports an error; the honest answer is ``nothing available`` after
one warm re-entry. The variants therefore have names of their own, so #24/#25
can branch on what they need instead of treating every surprise as a failure.

Only text markers are used here. The red background on the boost page is a
style, not a marker (docs/07 §3), and there are no stored pixel constants.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol

from aliexpress_coins.perception.layout import (
    DEFAULT_ACTION_LABELS,
    Row,
    find_action_in_row,
    group_rows,
    parse_progress_badge,
)
from aliexpress_coins.perception.matching import (
    DEFAULT_MIN_OCR_SCORE,
    DEFAULT_MIN_SCORE,
    find_text,
    normalize,
    similarity,
)
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.state import TASK_TITLES, has_search_landing_markers

BOOST_MARKER = 'Coins boost'
"""The header shared by the populated boost page and its empty state."""

EMPTY_BOOST_MARKER = "There's nothing here yet"
"""The sentence the app renders when the boost list failed to populate."""

REDEEM_MARKER = 'YOU CAN NOW REDEEM!'
"""The post-task interstitial, which #24/#25 must exit rather than execute."""

SURPRISE_GRID_TITLE = 'Browse surprise items'
SURPRISE_GRID_SUBTITLE = 'Tap 3 items on this page'
"""The surprise grid's header and instruction (docs/07 §3)."""

SEARCH_LANDING_MARKER = 'Discover more'
"""The category-card section on the search landing (#108).

The landing also carries a search field and an account-specific
``Search history`` section. The history is never used as an identifier. The
heading alone is ordinary copy, so :func:`is_search_landing` also requires the
category grid's item-count lines below it.
"""

_SEARCH_RESULT_PRICE = re.compile(r'(?:C\$|US\s*\$|CS|\$)\s*\d', re.IGNORECASE)
_SEARCH_RESULT_SOLD = re.compile(r'\b\d[\d,.]*\s*sold\b', re.IGNORECASE)
_SEARCH_RESULT_FIELD_TEXT_TOP_FRACTION = 0.07
_SEARCH_RESULT_FIELD_TOP_FRACTION = 0.15
"""The search field text sits between these frame fractions."""

_QUERY_CONFUSABLES = str.maketrans({'0': 'o', '1': 'l', '2': 'z', '5': 's', '8': 'b'})
"""Letter/digit shapes OCR confuses inside the submitted query field."""


def _fold_query_confusables(text: str) -> str:
    return text.translate(_QUERY_CONFUSABLES)


def _read_field_band(
    snapshot: Snapshot,
    *,
    top_fraction: float = _SEARCH_RESULT_FIELD_TEXT_TOP_FRACTION,
    bottom_fraction: float = _SEARCH_RESULT_FIELD_TOP_FRACTION,
) -> str:
    """Join readable text in the field band, excluding the status bar and header."""
    top = snapshot.height * top_fraction
    bottom = snapshot.height * bottom_fraction
    boxes = [
        box
        for box in snapshot.boxes
        if box.score >= DEFAULT_MIN_OCR_SCORE and box.bbox[1] >= top and box.bbox[3] <= bottom and box.text.strip()
    ]
    boxes.sort(key=lambda box: (box.bbox[1], box.bbox[0]))
    return ' '.join(box.text for box in boxes)


SEARCH_RESULTS_MARKERS = ('sold',)
"""The product-card copy shared by the measured results surface (#142)."""

PRODUCT_DETAIL_ITEM = re.compile(r'\bitem\s+\d+\s*/\s*\d+\b', re.IGNORECASE)
"""The product page's own item counter; the stable detail-page marker."""

PRODUCT_DETAIL_MARKERS = ('Add to cart', 'Buy now')
"""The purchase captions every measured surprise product page carries.

They are named, never tapped (docs/07 §3). A detail page that is scrolled far
enough to hide them still carries ``Item N/n``.
"""

SYSTEM_UI_TOP_FRACTION = 0.03
SYSTEM_UI_BOTTOM_FRACTION = 0.10
"""Frame-relative bands occupied by Android's status bar and taskbar."""

_CLOCK = re.compile(r'^\d{1,2}:\d{2}$')
_DAY = re.compile(r'^\d{1,2}$')
_REWARD_LABEL = re.compile(r'^\s*[+\uff0b]\s*\d')
_REWARD_WORDS = frozenset({'coin', 'coins'})
_NON_TITLE_LABELS = frozenset({normalize(BOOST_MARKER)})
"""Labels that identify the surface but can never name a task row."""
_WEEKDAYS = frozenset(
    {
        'mon',
        'monday',
        'tue',
        'tues',
        'tuesday',
        'wed',
        'weds',
        'wednesday',
        'thu',
        'thur',
        'thurs',
        'thursday',
        'fri',
        'friday',
        'sat',
        'saturday',
        'sun',
        'sunday',
        '一',
        '二',
        '三',
        '四',
        '五',
        '六',
        '日',
    }
)
"""Weekday spellings accepted in the measured clock/date widget.

The reference tablet's Canada/en launcher rendered the weekday as ``日`` in
issue #116, so the recogniser cannot assume an English locale.
"""

COIN_PAGE_MARKERS = ('Daily coins', 'Coins credits to redeem')
"""The two labels that identify the coin page rather than the Account card."""

STREAK_MARKER = 'Daily coins'
"""The streak card's heading, which is also a roster task surface."""

STREAK_CONTROL_LABELS: tuple[str, ...] = ('Collect', 'Claim', 'Sign in')
"""The measured streak-card controls; ``GO`` belongs to the task rows below."""

STREAK_CARD_HEIGHT_RATIO = 5.0
"""How far below its heading the streak card may reach, in heading-row heights."""

COIN_STORE_MARKER = 'Use coins for discounts'
"""The header of the sponsored product list that ``Explore sponsored items`` opens.

``Applied`` and a price beside ``Disc:`` corroborate the page, but the header is
the stable marker: a variant with no discounted tile still carries it, while the
word ``discounts`` alone is too common in a feed. The page is a browse
destination with its own right-edge dwell chip, not an interstitial (#83).
"""

SIGN_IN_MARKERS = ('Sign in', 'Log in', 'Sign in / Sign up', 'Log in / Sign up')
"""The wording a signed-out app shows instead of the account nickname.

Checked only when no coin/boost marker is present, because ``Sign in`` is also
a legitimate control label on the signed-in streak card. The measured signed-in
coin page never carries one of these prompts, so the absence of both page and
prompt stays ``UNKNOWN`` rather than becoming a false ``app_logged_out``.
"""

SHORTCUT_DEEP_LINK = 'aliexpress://goto?url=https%3A%2F%2Fm.aliexpress.com%2Fp%2Fcoin-index%2Findex.html'
"""The app's own coins route (docs/07 §1).

The flow layer fires this; this module only names it, because a task module
must not learn how to spawn adb. It is a *navigation* command, not a launch
command: the app has to be started and settled first.
"""

SURFACE_REFRESH_LIMIT = 1
"""The number of warm re-entries a route may spend on an absent surface.

One is enough because the measured failure is a first-load race: a second
entry into the already-rendered process populates the list. More attempts do
not help — the roster is server-driven, so hammering the page changes nothing
and looks like automation.
"""


class Surface(StrEnum):
    """The task-surface variant visible on arrival."""

    ROWS_PRESENT = 'rows_present'
    ROWS_ABSENT = 'rows_absent'
    BOOST_PAGE = 'boost_page'
    REDEEM_INTERSTITIAL = 'redeem_interstitial'
    EMPTY_BOOST = 'empty_boost'
    COIN_STORE = 'coin_store'
    SURPRISE_GRID = 'surprise_grid'
    SEARCH_LANDING = 'search_landing'
    SEARCH_RESULTS = 'search_results'
    PRODUCT_DETAIL = 'product_detail'
    NON_APP = 'non_app'
    UNKNOWN = 'unknown'


class SurfaceRoute(StrEnum):
    """An entry route that can produce the task surface.

    ``WIDGET`` is named because docs/05 records it as the other entry point, and
    a future run may select it explicitly (#27). It is not fired automatically:
    the measured recovery from an absent surface is a warm re-entry through the
    same shortcut route, and the retry budget is one re-entry, not two routes.
    """

    SHORTCUT = 'shortcut'
    WIDGET = 'widget'


class RouteFirer(Protocol):
    """Fire one navigation route, without exposing how it is implemented."""

    def __call__(self, route: SurfaceRoute) -> None:
        """Navigate with ``route``; the app is expected to already be warm."""
        ...


def classify_surface(snapshot: Snapshot) -> Surface:
    """Name the page variant in ``snapshot``, or ``UNKNOWN`` when unsure.

    Precedence matters. A redeem interstitial is its own screen even if page
    markers remain visible underneath it. The empty boost page must be checked
    before the populated boost page because both carry the same header. A frame
    with no coin/boost marker is ``UNKNOWN`` rather than a guessed app page; the
    measured clock/date shape is the one ``NON_APP`` exception, so a wrong screen
    cannot silently become ``ROWS_ABSENT``.
    """
    if contains_text(snapshot, REDEEM_MARKER):
        return Surface.REDEEM_INTERSTITIAL

    if is_product_detail(snapshot):
        return Surface.PRODUCT_DETAIL

    if is_coin_store(snapshot):
        return Surface.COIN_STORE

    rows = has_task_rows(snapshot)

    if is_boost_page(snapshot):
        if is_empty_boost(snapshot):
            return Surface.EMPTY_BOOST
        if rows:
            return Surface.BOOST_PAGE
        return Surface.UNKNOWN

    if _is_coin_page(snapshot):
        return Surface.ROWS_PRESENT if rows else Surface.ROWS_ABSENT

    if is_surprise_grid(snapshot):
        return Surface.SURPRISE_GRID

    if is_search_landing(snapshot):
        return Surface.SEARCH_LANDING

    if is_search_results(snapshot):
        return Surface.SEARCH_RESULTS

    if is_non_app_surface(snapshot):
        return Surface.NON_APP

    return Surface.UNKNOWN


def is_non_app_surface(snapshot: Snapshot) -> bool:
    """Whether the frame is the measured launcher/clock surface, not an app page.

    Issue #116's post-tap frame contained only a status-bar clock, a small
    status glyph and the ``日`` / ``24`` date widget. That combination is evidence
    that AliExpress was not in the foreground; an ordinary unknown shopping page
    must not be recovered as though it were the launcher.

    The test is deliberately conservative: a clock in the status-bar band, a
    weekday/day pair in the content area, and no other readable content or known
    app marker. All bands are fractions of the frame, so no device pixels are
    stored. The date pair is also what keeps a loading skeleton or blank WebView
    (both carry a status-bar clock) out of this surface.
    """
    if (
        contains_text(snapshot, REDEEM_MARKER)
        or is_product_detail(snapshot)
        or is_coin_store(snapshot)
        or is_boost_page(snapshot)
        or _is_coin_page(snapshot)
        or is_surprise_grid(snapshot)
        or is_search_landing(snapshot)
        or contains_text(snapshot, 'Coins quiz')
    ):
        return False

    content_top = snapshot.height * SYSTEM_UI_TOP_FRACTION
    content_bottom = snapshot.height * (1.0 - SYSTEM_UI_BOTTOM_FRACTION)
    readable = tuple(box for box in snapshot.boxes if box.score >= DEFAULT_MIN_OCR_SCORE and normalize(box.text))
    if not any(
        box.center[1] < content_top and _CLOCK.fullmatch(unicodedata.normalize('NFKC', box.text).strip())
        for box in readable
    ):
        return False

    content = tuple(box for box in readable if content_top <= box.center[1] <= content_bottom)
    if not content or not all(_is_system_widget_label(box.text) for box in content):
        return False
    return _has_date_widget(content)


def _is_system_widget_label(text: str) -> bool:
    """Whether a content label can belong to the clock/date widget only."""
    label = unicodedata.normalize('NFKC', text).strip()
    return _DAY.fullmatch(label) is not None or normalize(label) in _WEEKDAYS


def _has_date_widget(boxes: Sequence[TextBox]) -> bool:
    """Whether a weekday and day number share one widget line."""
    weekdays = tuple(box for box in boxes if normalize(box.text) in _WEEKDAYS)
    days = tuple(box for box in boxes if _DAY.fullmatch(unicodedata.normalize('NFKC', box.text).strip()))
    for weekday in weekdays:
        for day in days:
            tolerance = max(weekday.height, day.height)
            if abs(weekday.center[1] - day.center[1]) <= tolerance:
                return True
    return False


def is_boost_page(snapshot: Snapshot) -> bool:
    """Whether the ``Coins boost`` header is in the frame."""
    return contains_text(snapshot, BOOST_MARKER)


def is_coin_store(snapshot: Snapshot) -> bool:
    """Whether the frame is the sponsored product list opened from the coin page."""
    return contains_text(snapshot, COIN_STORE_MARKER)


def is_product_detail(snapshot: Snapshot) -> bool:
    """Whether the frame is the product page a surprise tile opens.

    The measured page carries ``Add to cart`` and ``Buy now`` together; a
    scrolled detail page can hide both, so its ``Item N/n`` counter is accepted
    on its own. Naming the surface keeps a legitimate tile tap out of the
    unexpected-screen streak (#100).
    """
    if any(
        box.score >= DEFAULT_MIN_OCR_SCORE and PRODUCT_DETAIL_ITEM.search(unicodedata.normalize('NFKC', box.text))
        for box in snapshot.boxes
    ):
        return True
    return all(contains_text(snapshot, marker) for marker in PRODUCT_DETAIL_MARKERS)


def is_surprise_grid(snapshot: Snapshot) -> bool:
    """Whether the frame is the surprise grid's header page.

    The boost row carries the same title and subtitle, so the row is explicitly
    excluded. The headerless, scrolled grid is recognised by the surprise
    executor and its guard, not by this global surface classifier.
    """
    return (
        not is_boost_page(snapshot)
        and contains_text(snapshot, SURPRISE_GRID_TITLE)
        and contains_text(snapshot, SURPRISE_GRID_SUBTITLE)
    )


def is_search_landing(snapshot: Snapshot) -> bool:
    """Whether the frame is the search landing opened by the search task.

    The boost row repeats the title ``Search for what you love``, so the boost
    page is explicitly excluded. ``Discover more`` is only ordinary copy by
    itself; the landing must also show the category grid's item-count lines.
    """
    return not is_boost_page(snapshot) and has_search_landing_markers(snapshot)


def is_search_results(snapshot: Snapshot, *, query: str | None = None) -> bool:
    """Whether the frame is the product results surface of a submitted search.

    The measured results page keeps the query in its field near the top and
    paints product cards with prices and ``N sold`` copy. When the executor
    knows the query it submitted, the query is matched as normalised text across
    the measured field band, with the matching layer's fuzzy threshold, so OCR
    may split ``wireless phone`` into two boxes or misspell it slightly. Without
    a query (the generic classifier used by the safety guard), field-like text
    must sit below the header/status bands and the frame must show at least two
    product prices and two ``N sold`` lines. Landing, coin/boost, coin-store and
    product-detail frames are explicitly excluded.
    """
    if (
        is_search_landing(snapshot)
        or is_boost_page(snapshot)
        or _is_coin_page(snapshot)
        or is_coin_store(snapshot)
        or is_product_detail(snapshot)
    ):
        return False

    prices = [
        box
        for box in snapshot.boxes
        if box.score >= DEFAULT_MIN_OCR_SCORE
        and _SEARCH_RESULT_PRICE.search(box.text)
        and box.bbox[1] > snapshot.height * SYSTEM_UI_TOP_FRACTION
        and box.bbox[3] < snapshot.height * (1.0 - SYSTEM_UI_BOTTOM_FRACTION)
    ]
    if not prices:
        return False

    if query is not None:
        field_text = normalize(_read_field_band(snapshot))
        query_text = normalize(query)
        if not query_text:
            return False
        folded_field = _fold_query_confusables(field_text)
        folded_query = _fold_query_confusables(query_text)
        if query_text in field_text or folded_query in folded_field:
            return True
        return max(similarity(query_text, field_text), similarity(folded_query, folded_field)) >= DEFAULT_MIN_SCORE

    top_copy = _read_field_band(snapshot)
    if not any(character.isalpha() for character in top_copy):
        return False
    sold = [
        box for box in snapshot.boxes if box.score >= DEFAULT_MIN_OCR_SCORE and _SEARCH_RESULT_SOLD.search(box.text)
    ]
    return len(prices) >= 2 and len(sold) >= 2


def is_empty_boost(snapshot: Snapshot) -> bool:
    """Whether the frame is the boost page's empty state.

    The empty sentence is checked with a substring fallback because OCR often
    reads the whole call-to-action on one line.
    """
    return is_boost_page(snapshot) and contains_text(snapshot, EMPTY_BOOST_MARKER) and not has_task_rows(snapshot)


def has_task_rows(snapshot: Snapshot) -> bool:
    """Whether the frame shows at least one readable task row.

    A row is proven by a title, not merely by an action control. A bare ``GO``
    with a progress badge and no readable title is an unreadable row, not a task:
    accepting it is what let #137 report a completed night from a single
    title-less row. A known task title still counts on its own because the loaded
    boost page is measured by its header plus a task title, and a row can be
    captured before its control is legible.
    """
    for row in group_rows(snapshot.boxes, snapshot.geometry):
        action = find_action_in_row(row)
        if action is not None and _row_has_readable_title(row, action):
            return True
    return any(contains_text(snapshot, title) for title in TASK_TITLES)


def _row_has_readable_title(row: Row, action: TextBox) -> bool:
    """Whether ``row`` carries a label that can name the task it belongs to.

    The action itself, a progress badge, a reward and the surface header are
    never titles. This keeps a title-less ``GO`` row from confirming a list or a
    roster.
    """
    for box in row.boxes:
        if box is action or box.score < DEFAULT_MIN_OCR_SCORE:
            continue
        text = box.text.strip()
        if not text or not any(character.isalpha() for character in text):
            continue
        if parse_progress_badge(text) is not None:
            continue
        label = normalize(text)
        if label in _NON_TITLE_LABELS or label in _REWARD_WORDS:
            continue
        if _REWARD_LABEL.match(text):
            continue
        if any(similarity(action_label, text) >= DEFAULT_MIN_SCORE for action_label in DEFAULT_ACTION_LABELS):
            continue
        return True
    return False


def contains_text(snapshot: Snapshot, label: str) -> bool:
    """Find ``label`` by fuzzy lookup, then by a normalised substring fallback."""
    if find_text(snapshot, label, min_score=DEFAULT_MIN_SCORE, min_ocr_score=DEFAULT_MIN_OCR_SCORE) is not None:
        return True
    needle = normalize(label)
    return any(needle in normalize(box.text) for box in snapshot.boxes if box.score >= DEFAULT_MIN_OCR_SCORE)


def is_logged_out(snapshot: Snapshot) -> bool:
    """Whether the frame is a signed-out surface, not a coin/task page.

    A sign-in prompt is ordinary on the home feed before the account is reached,
    so detection is deliberately conditional: the coin page and the boost page
    are both trusted signed-in surfaces, and only their absence plus a prompt is
    evidence. A frame with neither is ``UNKNOWN``, never an accusation.
    """
    if _is_coin_page(snapshot) or is_boost_page(snapshot):
        return False
    return any(contains_text(snapshot, marker) for marker in SIGN_IN_MARKERS)


def _is_coin_page(snapshot: Snapshot) -> bool:
    return all(contains_text(snapshot, marker) for marker in COIN_PAGE_MARKERS)


__all__ = [
    'BOOST_MARKER',
    'COIN_PAGE_MARKERS',
    'COIN_STORE_MARKER',
    'EMPTY_BOOST_MARKER',
    'PRODUCT_DETAIL_ITEM',
    'PRODUCT_DETAIL_MARKERS',
    'REDEEM_MARKER',
    'SEARCH_LANDING_MARKER',
    'SHORTCUT_DEEP_LINK',
    'SIGN_IN_MARKERS',
    'STREAK_CARD_HEIGHT_RATIO',
    'STREAK_CONTROL_LABELS',
    'STREAK_MARKER',
    'SURFACE_REFRESH_LIMIT',
    'SURPRISE_GRID_SUBTITLE',
    'SURPRISE_GRID_TITLE',
    'SYSTEM_UI_BOTTOM_FRACTION',
    'SYSTEM_UI_TOP_FRACTION',
    'RouteFirer',
    'Surface',
    'SurfaceRoute',
    'classify_surface',
    'contains_text',
    'has_task_rows',
    'is_boost_page',
    'is_coin_store',
    'is_empty_boost',
    'is_logged_out',
    'is_non_app_surface',
    'is_product_detail',
    'is_search_landing',
    'is_search_results',
    'is_surprise_grid',
]
