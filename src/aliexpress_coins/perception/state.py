"""What the frame currently is (issue #16).

The hybrid container renders in stages and can fail into a blank white page
without an error string. A run has to distinguish *still loading*, *arrived*,
*a popup is on top* and *this page is dead* before it asserts anything.

The classifier is deliberately conservative. It returns ``PAGE`` only when a
known screen marker is present; a frame that merely looks like content is
``UNKNOWN`` and the caller decides. A wrong ``PAGE`` is worse than an honest
``UNKNOWN`` because every wait and executor downstream asserts on it.

Every threshold here is a fraction of the frame, of the frame's own luminance
range, or of the text area. There are no stored coordinates and no pixel sizes
that depend on the tablet: the same values replay at another geometry (#42).

The image work is a single grayscale pass plus one small grid overlay. It never
runs OCR; it reuses the boxes already on the :class:`Snapshot`, so it costs a
few milliseconds next to a ~1.3 s OCR pass.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from aliexpress_coins.perception.errors import StateTimeout
from aliexpress_coins.perception.matching import (
    DEFAULT_MIN_OCR_SCORE,
    DEFAULT_MIN_SCORE,
    find_text,
    normalize,
)
from aliexpress_coins.perception.snapshot import Snapshot

_GRID_LONG_SIDE = 100
"""Cells on the longer side of the popup-panel analysis grid.

This is a sampling resolution, not a layout constant: the shorter side is
derived from the frame's aspect ratio, and every decision below is a fraction
of the resulting grid.
"""

_FLAT_RANGE_FRACTION = 0.02
"""A frame whose luminance range is below this fraction of 0..255 is 'flat'."""

_LOW_CONTRAST_RANGE_FRACTION = 0.15
"""Skeleton blocks are low contrast: their whole range is a small part of 0..255."""

_EDGE_STEP_FRACTION = 0.02
"""A neighbouring-pixel step counts as an edge above this part of the frame range."""

_MIN_STRUCTURE_EDGE_FRACTION = 0.005
"""A flat-but-not-uniform frame needs this many edge cells to be a skeleton."""

_FEW_TEXT_COVERAGE = 0.01
"""Text covering less than this fraction of the frame is a fragment, not a page."""

_MOSTLY_BLANK_FRACTION = 0.9
"""The dominant luminance must own this fraction before fragments mean 'loading'."""

_PANEL_MIN_AREA_FRACTION = 0.18
"""A popup panel covers at least this fraction of the frame."""

_PANEL_MIN_WIDTH_FRACTION = 0.5
_PANEL_MIN_HEIGHT_FRACTION = 0.2
_PANEL_MIN_FILL = 0.5
"""The panel's bounding box must be mostly filled by the bright region."""

_SYSTEM_UI_TOP_FRACTION = 0.03
_SYSTEM_UI_BOTTOM_FRACTION = 0.10
"""Status-bar and taskbar bands, as frame fractions.

Every real ``screencap`` carries a clock and taskbar glyphs. They are never
page content, so both the text signal and the luminance analysis ignore these
bands. A frame captured at another geometry scales the bands with it.
"""

_SCRIM_DIM_FRACTION = 0.8
_SCRIM_VARIANCE_FRACTION = 0.8
"""A dimmed scrim is a dark border whose texture is flatter than the frame."""

_COLLECTION_HEADER_TOP_FRACTION = 0.35
"""A product-collection header sits near the top; a feed section lower down does not."""

_PRICE = re.compile(r'(?:C\$|US\s*\$|CS|\$)\s*\d', re.IGNORECASE)

TASK_TITLES: tuple[str, ...] = (
    'Daily check-in',
    'Take the coins quiz',
    'Learn to save money with Coins Credits',
    'Explore sponsored items',
    'Browse surprise items',
    'Place an order on this page',
    'Add 5 items to your cart',
    'Check in to win Shopping credits',
    'Water to win a US $0.10 prize',
    'Share coins with friends',
    'Turn on notifications',
    'Add a coins shortcut',
    'Add & start using our widget',
    'Search for what you love',
    'Browse recently viewed items',
)
"""Task rows measured on the coin and boost pages (docs/05 §4)."""

_POPUP_LABELS: tuple[str, ...] = (
    'Welcome gifts',
    'YOU CAN NOW REDEEM!',
    'Tap to claim',
)
"""Headlines that are only ever drawn on a modal."""

_ERROR_LABELS: tuple[str, ...] = (
    '404 Page',
    "Sorry, we can't find the page",
    'Page not found',
    'Something went wrong',
    'No internet',
    'Network error',
    "There's nothing here yet",
)
"""The app's dead-page and stuck-list text (docs/06 §5)."""


class ScreenState(StrEnum):
    """The mutually exclusive states a captured frame can be in."""

    LOADING = 'loading'
    PAGE = 'page'
    POPUP = 'popup'
    ERROR = 'error'
    UNKNOWN = 'unknown'


@dataclass(frozen=True, slots=True)
class Evidence:
    """One signal that contributed to a classification."""

    signal: str
    score: float
    detail: str


@dataclass(frozen=True, slots=True)
class Classification:
    """A state, a confidence-ish score and the signals that produced it."""

    state: ScreenState
    score: float
    evidence: tuple[Evidence, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, 'evidence', tuple(self.evidence))
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f'classification score must be between 0 and 1, got {self.score!r}')

    @property
    def reasons(self) -> tuple[str, ...]:
        """One human-readable line per piece of evidence."""
        return tuple(f'{item.signal}: {item.detail}' for item in self.evidence)

    def describe(self) -> str:
        """Render the classification for a run report or a test failure."""
        reasons = '; '.join(self.reasons) if self.reasons else 'no evidence'
        return f'{self.state.value} (score={self.score:.2f}): {reasons}'


@dataclass(frozen=True, slots=True)
class Observation:
    """One poll of a wait, with the time since the wait started."""

    elapsed: float
    classification: Classification


@dataclass(frozen=True, slots=True)
class WaitResult:
    """The classification that satisfied a wait and the observations on the way."""

    classification: Classification
    observations: tuple[Observation, ...]
    elapsed: float

    @property
    def state(self) -> ScreenState:
        """The state that satisfied the wait."""
        return self.classification.state

    @property
    def history(self) -> tuple[ScreenState, ...]:
        """Every state seen, oldest first."""
        return tuple(item.classification.state for item in self.observations)


class Capturer(Protocol):
    """The capture seam a wait can poll."""

    def capture(self) -> Snapshot:
        """Capture one frame."""
        ...


CaptureSource = Capturer | Callable[[], Snapshot]


@dataclass(frozen=True, slots=True)
class _Panel:
    area_fraction: float
    left: float
    top: float
    right: float
    bottom: float
    fill: float
    mean: float
    border_mean: float
    border_std: float
    global_std: float


@dataclass(frozen=True, slots=True)
class _Scrim:
    border_mean: float
    border_std: float
    global_mean: float
    global_std: float
    center_mean: float


@dataclass(frozen=True, slots=True)
class _FrameAnalysis:
    dynamic_range: float
    edge_fraction: float
    dominant_fraction: float
    panel: _Panel | None
    scrim: _Scrim | None


@dataclass(frozen=True, slots=True)
class _TextSignal:
    count: int
    coverage: float


def classify(snapshot: Snapshot) -> Classification:
    """Classify one frame and attach the evidence that decided it.

    Precedence is deliberate: a modal on top is ``POPUP`` even when the page
    underneath still shows its markers; an error marker or a blank frame is
    ``ERROR`` even when the container's chrome is visible; only then are the
    loaded-screen markers consulted. A frame with no recognised signal is
    ``UNKNOWN``.
    """
    analysis = _analyse(snapshot)
    text = _text_signal(snapshot)

    popup = _popup_marker(snapshot)
    if popup is not None:
        return _result(
            ScreenState.POPUP,
            popup.score,
            Evidence('popup_marker', popup.score, f'modal headline {popup.text!r}'),
        )

    error = _first_match(snapshot, _ERROR_LABELS)
    if error is not None:
        return _result(
            ScreenState.ERROR,
            error.score,
            Evidence('error_marker', error.score, f'dead-page text {error.text!r}'),
        )

    if analysis.panel is not None:
        panel = analysis.panel
        return _result(
            ScreenState.POPUP,
            _panel_score(panel),
            Evidence(
                'popup_panel',
                _panel_score(panel),
                'bright panel inset from every edge '
                f'(area={panel.area_fraction:.2f}, x={panel.left:.2f}..{panel.right:.2f}, '
                f'y={panel.top:.2f}..{panel.bottom:.2f}, border/panel mean={panel.border_mean:.0f}/{panel.mean:.0f})',
            ),
        )

    if analysis.scrim is not None:
        scrim = analysis.scrim
        return _result(
            ScreenState.POPUP,
            _scrim_score(scrim),
            Evidence(
                'dimmed_scrim',
                _scrim_score(scrim),
                'dark low-variance border behind content '
                f'(border mean/std={scrim.border_mean:.0f}/{scrim.border_std:.0f}, '
                f'frame mean/std={scrim.global_mean:.0f}/{scrim.global_std:.0f}, '
                f'centre mean={scrim.center_mean:.0f})',
            ),
        )

    if _is_blank(analysis, text):
        return _result(
            ScreenState.ERROR,
            _blank_score(analysis),
            Evidence(
                'blank_frame',
                _blank_score(analysis),
                f'no text, luminance range={analysis.dynamic_range:.1f}, edge fraction={analysis.edge_fraction:.4f}',
            ),
        )

    loading = _loading_evidence(snapshot, analysis, text)
    if loading is not None:
        return _result(ScreenState.LOADING, loading.score, loading)

    page = _page_marker(snapshot)
    if page is not None:
        return _result(ScreenState.PAGE, page.score, page)

    return _result(
        ScreenState.UNKNOWN,
        0.0,
        Evidence(
            'no_signal',
            0.0,
            f'no known marker, panel, blank or skeleton '
            f'(boxes={text.count}, text={text.coverage:.3f}, '
            f'range={analysis.dynamic_range:.1f}, edge={analysis.edge_fraction:.4f})',
        ),
    )


def classify_state(snapshot: Snapshot) -> ScreenState:
    """The state alone, for callers that do not need the evidence."""
    return classify(snapshot).state


def wait_for_state(
    capture: CaptureSource,
    target: ScreenState | Iterable[ScreenState],
    *,
    timeout: float,
    interval: float,
    after: ScreenState | Iterable[ScreenState] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    on_frame: Callable[[Classification], None] | None = None,
) -> WaitResult:
    """Poll ``capture`` until ``target`` is seen, or raise :class:`StateTimeout`.

    ``timeout`` and ``interval`` are required parameters; the helper holds no
    polling constants of its own. Time is injected so a test never sleeps.

    ``after`` adds ordering. ``wait_for_state(capture, POPUP, after=PAGE, ...)``
    does not return a popup that appeared *during* ``LOADING``: the popup must be
    observed after the page was observed. That is the difference between a
    loading interstitial and a popup sitting on an arrived page.

    Every poll is kept in :attr:`WaitResult.observations`, so a report can show
    the sequence ``LOADING -> POPUP -> PAGE`` rather than only the final state.
    """
    wanted = _states(target, 'target')
    prior = _states(after, 'after') if after is not None else None
    if timeout <= 0:
        raise ValueError('timeout must be positive')
    if interval <= 0:
        raise ValueError('interval must be positive')

    started = clock()
    deadline = started + timeout
    observations: list[Observation] = []

    while True:
        frame = _capture(capture)
        classification = classify(frame)
        now = clock()
        observations.append(Observation(elapsed=max(0.0, now - started), classification=classification))
        if on_frame is not None:
            on_frame(classification)

        if classification.state in wanted and (
            prior is None or any(item.classification.state in prior for item in observations[:-1])
        ):
            return WaitResult(
                classification=classification,
                observations=tuple(observations),
                elapsed=max(0.0, now - started),
            )

        if now >= deadline:
            seen = ' -> '.join(item.classification.state.value for item in observations)
            raise StateTimeout(
                f'state {sorted(state.value for state in wanted)} was not observed within {timeout:g}s '
                f'(last={classification.describe()}; history={seen})',
            )

        sleep(min(interval, max(0.0, deadline - now)))


def _result(state: ScreenState, score: float, *evidence: Evidence) -> Classification:
    return Classification(state=state, score=max(0.0, min(1.0, score)), evidence=evidence)


def _states(value: ScreenState | Iterable[ScreenState], name: str) -> frozenset[ScreenState]:
    states = frozenset((value,)) if isinstance(value, ScreenState) else frozenset(value)
    if not states:
        raise ValueError(f'{name} must name at least one screen state')
    if not all(isinstance(state, ScreenState) for state in states):
        raise TypeError(f'{name} must contain ScreenState values')
    return states


def _capture(source: CaptureSource) -> Snapshot:
    if callable(source):
        return source()
    return source.capture()


def _content_bounds(width: int, height: int) -> tuple[int, int, int, int]:
    top = round(height * _SYSTEM_UI_TOP_FRACTION)
    bottom = height - round(height * _SYSTEM_UI_BOTTOM_FRACTION)
    if bottom <= top:
        return 0, 0, width, height
    return 0, top, width, bottom


def _text_signal(snapshot: Snapshot) -> _TextSignal:
    _, content_top, _, content_bottom = _content_bounds(snapshot.width, snapshot.height)
    frame_area = float(snapshot.width * max(0, content_bottom - content_top))
    covered = 0.0
    count = 0
    for box in snapshot.boxes:
        if box.score < DEFAULT_MIN_OCR_SCORE or not normalize(box.text):
            continue
        left, top, right, bottom = box.bbox
        if bottom <= content_top or top >= content_bottom:
            continue
        left = max(0.0, left)
        top = max(float(content_top), top)
        right = min(float(snapshot.width), right)
        bottom = min(float(content_bottom), bottom)
        covered += max(0.0, right - left) * max(0.0, bottom - top)
        count += 1
    coverage = covered / frame_area if frame_area > 0 else 0.0
    return _TextSignal(count=count, coverage=min(1.0, coverage))


def _is_blank(analysis: _FrameAnalysis, text: _TextSignal) -> bool:
    return (
        text.count == 0
        and analysis.dynamic_range <= _FLAT_RANGE_FRACTION * 255.0
        and analysis.edge_fraction <= _MIN_STRUCTURE_EDGE_FRACTION
    )


def _blank_score(analysis: _FrameAnalysis) -> float:
    limit = _FLAT_RANGE_FRACTION * 255.0
    return max(0.5, 1.0 - analysis.dynamic_range / limit)


def _panel_score(panel: _Panel) -> float:
    return min(1.0, 0.5 + panel.area_fraction)


def _scrim_score(scrim: _Scrim) -> float:
    dimming = 1.0 - scrim.border_mean / scrim.global_mean
    return min(1.0, 0.6 + max(0.0, dimming))


def _loading_evidence(
    snapshot: Snapshot,
    analysis: _FrameAnalysis,
    text: _TextSignal,
) -> Evidence | None:
    low_contrast = analysis.dynamic_range <= _LOW_CONTRAST_RANGE_FRACTION * 255.0
    if text.count == 0 and low_contrast and analysis.edge_fraction >= _MIN_STRUCTURE_EDGE_FRACTION:
        return Evidence(
            'skeleton',
            min(1.0, 0.6 + analysis.edge_fraction),
            f'no text, low-contrast structure '
            f'(range={analysis.dynamic_range:.1f}, edge fraction={analysis.edge_fraction:.4f})',
        )

    if text.count > 0 and text.coverage <= _FEW_TEXT_COVERAGE and analysis.dominant_fraction >= _MOSTLY_BLANK_FRACTION:
        return Evidence(
            'few_text_fragments',
            min(1.0, 0.5 + (1.0 - text.coverage)),
            f'{text.count} text fragment(s) on a mostly blank frame '
            f'(coverage={text.coverage:.4f}, dominant={analysis.dominant_fraction:.2f})',
        )

    boost = _match(snapshot, 'Coins boost')
    if boost is not None and not _any_match(snapshot, TASK_TITLES):
        return Evidence(
            'boost_without_rows',
            boost.score,
            'Coins boost header is present but no task row has rendered yet',
        )
    return None


def _popup_marker(snapshot: Snapshot) -> _Match | None:
    return _first_match(snapshot, _POPUP_LABELS)


def _page_marker(snapshot: Snapshot) -> Evidence | None:
    coin = _require(snapshot, 'Daily coins', 'Coins credits to redeem')
    if coin is not None:
        return _marker_evidence('coin page', coin)

    boost = _match(snapshot, 'Coins boost')
    if boost is not None:
        title = _first_match(snapshot, TASK_TITLES)
        if title is not None:
            score = min(boost.score, title.score)
            return Evidence('page_marker', score, f'boost page: Coins boost + task {title.text!r}')

    quiz = _match(snapshot, 'Coins quiz')
    if quiz is not None:
        return Evidence('page_marker', quiz.score, f'quiz page: {quiz.text!r}')

    result = _prefix_match(snapshot, 'Correct answer:')
    if result is not None:
        return Evidence('page_marker', result.score, f'quiz result: {result.text!r}')

    surprise = _match(snapshot, 'Browse surprise items')
    if surprise is not None:
        return Evidence('page_marker', surprise.score, f'surprise-items page: {surprise.text!r}')

    # A feed can contain a "Bundle deals" section too, so the header must be
    # near the top before it identifies a product-collection page. The home
    # feed itself is deliberately unmodelled: it has no stable whole-screen
    # marker, and returning UNKNOWN is safer than calling it a coins screen.
    collection = _match(snapshot, 'Bundle Deals')
    if collection is not None and collection.top < snapshot.height * _COLLECTION_HEADER_TOP_FRACTION:
        price = _first_price(snapshot)
        if price is not None:
            score = min(collection.score, price.score)
            return Evidence(
                'page_marker',
                score,
                f'product collection: Bundle Deals + {price.text!r}',
            )

    orders = _match(snapshot, 'My orders')
    if orders is not None:
        account = _first_match(snapshot, ('Wishlist', 'History', 'Coupons'))
        if account is not None:
            score = min(orders.score, account.score)
            return Evidence(
                'page_marker',
                score,
                f'account page: My orders + {account.text!r}',
            )

    return None


@dataclass(frozen=True, slots=True)
class _Match:
    text: str
    score: float
    top: float = 0.0


def _match(snapshot: Snapshot, label: str) -> _Match | None:
    found = find_text(
        snapshot,
        label,
        min_score=DEFAULT_MIN_SCORE,
        min_ocr_score=DEFAULT_MIN_OCR_SCORE,
    )
    if found is None:
        return None
    return _Match(text=found.text, score=found.score, top=found.box.bbox[1])


def _first_match(snapshot: Snapshot, labels: Iterable[str]) -> _Match | None:
    best: _Match | None = None
    for label in labels:
        found = _match(snapshot, label)
        if found is not None and (best is None or found.score > best.score):
            best = found
    return best


def _any_match(snapshot: Snapshot, labels: Iterable[str]) -> bool:
    return _first_match(snapshot, labels) is not None


def _prefix_match(snapshot: Snapshot, prefix: str) -> _Match | None:
    """Match a label whose OCR line continues past the marker.

    A quiz result is read as one line (``Correct answer: All of the above``),
    so exact and whole-label fuzzy matching cannot see the marker on its own.
    Normalisation still applies; the match requires the line to *start* with
    the marker, not merely contain it.
    """
    needle = normalize(prefix)
    if not needle:
        return None
    for box in snapshot.boxes:
        if box.score < DEFAULT_MIN_OCR_SCORE:
            continue
        label = normalize(box.text)
        if label.startswith(needle):
            return _Match(text=box.text, score=1.0, top=box.bbox[1])
    return None


def _first_price(snapshot: Snapshot) -> _Match | None:
    for box in snapshot.boxes:
        if box.score < DEFAULT_MIN_OCR_SCORE or not _PRICE.search(box.text):
            continue
        return _Match(text=box.text, score=1.0, top=box.bbox[1])
    return None


def _require(snapshot: Snapshot, *labels: str) -> _Match | None:
    matches = [_match(snapshot, label) for label in labels]
    if any(match is None for match in matches):
        return None
    found = [match for match in matches if match is not None]
    text = ' + '.join(match.text for match in found)
    return _Match(text=text, score=min(match.score for match in found))


def _marker_evidence(screen: str, match: _Match) -> Evidence:
    return Evidence('page_marker', match.score, f'{screen}: {match.text!r}')


def _analyse(snapshot: Snapshot) -> _FrameAnalysis:
    bounds = _content_bounds(snapshot.width, snapshot.height)
    values = _gray(snapshot.image, bounds)
    low, high = np.percentile(values, (1.0, 99.0))
    dynamic_range = float(high - low)
    edge_fraction = _edge_fraction(values, dynamic_range)
    dominant_fraction = _dominant_fraction(values)
    grid = _analysis_grid(values)
    panel = _find_panel(grid)
    scrim = _find_scrim(grid)
    return _FrameAnalysis(
        dynamic_range=dynamic_range,
        edge_fraction=edge_fraction,
        dominant_fraction=dominant_fraction,
        panel=panel,
        scrim=scrim,
    )


def _gray(image: Image.Image, bounds: tuple[int, int, int, int]) -> NDArray[np.float32]:
    return np.asarray(image.convert('L').crop(bounds), dtype=np.float32)


def _edge_fraction(values: NDArray[np.float32], dynamic_range: float) -> float:
    if values.size < 2:
        return 0.0
    step = max(dynamic_range * _EDGE_STEP_FRACTION, 0.0)
    vertical = np.abs(np.diff(values, axis=0))
    horizontal = np.abs(np.diff(values, axis=1))
    total = vertical.size + horizontal.size
    edges = int(np.count_nonzero(vertical > step)) + int(np.count_nonzero(horizontal > step))
    return edges / total


def _dominant_fraction(values: NDArray[np.float32]) -> float:
    histogram, _ = np.histogram(values, bins=256, range=(0.0, 255.0))
    return float(histogram.max()) / float(histogram.sum())


def _analysis_grid(values: NDArray[np.float32]) -> NDArray[np.float32]:
    height, width = values.shape
    if height >= width:
        grid_height = _GRID_LONG_SIDE
        grid_width = max(1, round(_GRID_LONG_SIDE * width / height))
    else:
        grid_width = _GRID_LONG_SIDE
        grid_height = max(1, round(_GRID_LONG_SIDE * height / width))
    return np.asarray(
        Image.fromarray(values.astype(np.uint8)).resize(
            (grid_width, grid_height),
            Image.Resampling.BOX,
        ),
        dtype=np.float32,
    )


def _find_panel(grid: NDArray[np.float32]) -> _Panel | None:
    grid_height, grid_width = grid.shape
    threshold = _otsu(grid)
    bright = grid >= threshold
    area, bbox = _largest_component(bright)
    top, bottom, left, right = bbox
    box_height = bottom - top + 1
    box_width = right - left + 1
    area_fraction = area / float(grid_width * grid_height)
    fill = area / float(box_height * box_width)
    touches_edge = top == 0 or left == 0 or bottom == grid_height - 1 or right == grid_width - 1
    if touches_edge:
        return None

    height_fraction = box_height / grid_height
    width_fraction = box_width / grid_width
    if (
        area_fraction < _PANEL_MIN_AREA_FRACTION
        or width_fraction < _PANEL_MIN_WIDTH_FRACTION
        or height_fraction < _PANEL_MIN_HEIGHT_FRACTION
        or fill < _PANEL_MIN_FILL
    ):
        return None

    border = _border_cells(grid)
    panel_mean = float(grid[bright].mean())
    border_mean = float(border.mean())
    border_std = float(border.std())
    global_std = float(grid.std())
    if border_std > global_std or panel_mean <= border_mean:
        return None

    return _Panel(
        area_fraction=area_fraction,
        left=left / grid_width,
        top=top / grid_height,
        right=(right + 1) / grid_width,
        bottom=(bottom + 1) / grid_height,
        fill=fill,
        mean=panel_mean,
        border_mean=border_mean,
        border_std=border_std,
        global_std=global_std,
    )


def _border_cells(grid: NDArray[np.float32]) -> NDArray[np.float32]:
    grid_height, grid_width = grid.shape
    band_y = max(1, grid_height // 12)
    band_x = max(1, grid_width // 12)
    return np.concatenate(
        (
            grid[:band_y].ravel(),
            grid[-band_y:].ravel(),
            grid[band_y:-band_y, :band_x].ravel(),
            grid[band_y:-band_y, -band_x:].ravel(),
        ),
    )


def _side_cells(grid: NDArray[np.float32]) -> NDArray[np.float32]:
    grid_height, grid_width = grid.shape
    band_y = max(1, grid_height // 12)
    band_x = max(1, grid_width // 12)
    return np.concatenate(
        (
            grid[band_y:-band_y, :band_x].ravel(),
            grid[band_y:-band_y, -band_x:].ravel(),
        ),
    )


def _find_scrim(grid: NDArray[np.float32]) -> _Scrim | None:
    global_mean = float(grid.mean())
    global_std = float(grid.std())
    if global_mean <= 0 or global_std <= 0:
        return None

    border = _side_cells(grid)
    border_mean = float(border.mean())
    border_std = float(border.std())
    if border_mean >= global_mean * _SCRIM_DIM_FRACTION:
        return None
    if border_std >= global_std * _SCRIM_VARIANCE_FRACTION:
        return None

    grid_height, grid_width = grid.shape
    centre = grid[grid_height // 4 : 3 * grid_height // 4, grid_width // 4 : 3 * grid_width // 4]
    centre_mean = float(centre.mean())
    if centre_mean <= border_mean:
        return None
    return _Scrim(
        border_mean=border_mean,
        border_std=border_std,
        global_mean=global_mean,
        global_std=global_std,
        center_mean=centre_mean,
    )


def _otsu(values: NDArray[np.float32]) -> float:
    histogram, _ = np.histogram(values, bins=256, range=(0.0, 255.0))
    total = int(histogram.sum())
    weights = histogram.astype(np.float64) / total
    levels = np.arange(256, dtype=np.float64)
    omega = np.cumsum(weights)
    mu = np.cumsum(weights * levels)
    mu_total = float(mu[-1])
    denominator = omega * (1.0 - omega)
    between = np.divide(
        (mu_total * omega - mu) ** 2,
        denominator,
        out=np.zeros(256, dtype=np.float64),
        where=denominator > 0,
    )
    return float(np.argmax(between))


def _largest_component(mask: NDArray[np.bool_]) -> tuple[int, tuple[int, int, int, int]]:
    height, width = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    best_area = 0
    best_box = (0, 0, 0, 0)

    for start_y in range(height):
        for start_x in range(width):
            if not mask[start_y, start_x] or seen[start_y, start_x]:
                continue
            stack = [(start_y, start_x)]
            seen[start_y, start_x] = True
            area = 0
            top = bottom = start_y
            left = right = start_x
            while stack:
                y, x = stack.pop()
                area += 1
                top = min(top, y)
                bottom = max(bottom, y)
                left = min(left, x)
                right = max(right, x)
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    next_y = y + dy
                    next_x = x + dx
                    if (
                        0 <= next_y < height
                        and 0 <= next_x < width
                        and mask[next_y, next_x]
                        and not seen[next_y, next_x]
                    ):
                        seen[next_y, next_x] = True
                        stack.append((next_y, next_x))
            if area > best_area:
                best_area = area
                best_box = (top, bottom, left, right)
    return best_area, best_box
