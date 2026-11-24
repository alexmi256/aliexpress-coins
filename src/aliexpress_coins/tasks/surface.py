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
from enum import StrEnum
from typing import Protocol

from aliexpress_coins.perception.layout import find_action_in_row, group_rows
from aliexpress_coins.perception.matching import (
    DEFAULT_MIN_OCR_SCORE,
    DEFAULT_MIN_SCORE,
    find_text,
    normalize,
)
from aliexpress_coins.perception.snapshot import Snapshot
from aliexpress_coins.perception.state import TASK_TITLES

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
``Search history`` section. The history is never used as an identifier; the
server-rendered ``Discover more`` heading is the stable page marker and is not
present on the boost row that opens the page.
"""

PRODUCT_DETAIL_ITEM = re.compile(r'\bitem\s+\d+\s*/\s*\d+\b', re.IGNORECASE)
"""The product page's own item counter; the stable detail-page marker."""

PRODUCT_DETAIL_MARKERS = ('Add to cart', 'Buy now')
"""The purchase captions every measured surprise product page carries.

They are named, never tapped (docs/07 §3). A detail page that is scrolled far
enough to hide them still carries ``Item N/n``.
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
    PRODUCT_DETAIL = 'product_detail'
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
    with no coin/boost marker is ``UNKNOWN`` rather than a guess, so a wrong
    screen cannot silently become ``ROWS_ABSENT``.
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

    return Surface.UNKNOWN


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
    page is explicitly excluded. The ``Discover more`` category heading is the
    landing's own marker; the search-history text above it is account data and
    deliberately not part of the match.
    """
    return not is_boost_page(snapshot) and contains_text(snapshot, SEARCH_LANDING_MARKER)


def is_empty_boost(snapshot: Snapshot) -> bool:
    """Whether the frame is the boost page's empty state.

    The empty sentence is checked with a substring fallback because OCR often
    reads the whole call-to-action on one line.
    """
    return is_boost_page(snapshot) and contains_text(snapshot, EMPTY_BOOST_MARKER) and not has_task_rows(snapshot)


def has_task_rows(snapshot: Snapshot) -> bool:
    """Whether the frame shows at least one task row.

    A row is normally proven by an action control (``GO`` or similar). A known
    task title also counts: the docs define the loaded boost page by the header
    plus a task title, and a row can be captured before its control is legible.
    The title list is the catalogue of *observed* tasks, not a closed schema;
    an unrecognised server-driven title still has its action control, so it is
    still seen as a row.
    """
    if any(find_action_in_row(row) is not None for row in group_rows(snapshot.boxes, snapshot.geometry)):
        return True
    return any(contains_text(snapshot, title) for title in TASK_TITLES)


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
    'is_product_detail',
    'is_search_landing',
    'is_surprise_grid',
]
