"""Surface variants and the one re-entry route policy (issue #38).

The device is simulated at the capture and input seams: frames come from the
recorded fixtures or a hand-built box list, and the route firer is a recorder.
No test reaches adb.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

from aliexpress_coins.control.controller import Controller
from aliexpress_coins.control.popups import PopupCorpus, load_popup_corpus
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.ocr import RapidOcrEngine
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.tasks.catalogue import TaskCatalogue, load_task_catalogue
from aliexpress_coins.tasks.discovery import (
    EMPTY_BOOST_MARKER,
    DiscoveryRoute,
    RosterStatus,
    discover_roster,
    is_empty_boost,
)
from aliexpress_coins.tasks.surface import (
    SHORTCUT_DEEP_LINK,
    SURFACE_REFRESH_LIMIT,
    Surface,
    SurfaceRoute,
    classify_surface,
    has_task_rows,
    is_logged_out,
    is_non_app_surface,
    is_search_landing,
    is_surprise_grid,
)

ROOT = Path(__file__).resolve().parents[1]
STATES = ROOT / 'tests' / 'data' / 'states'
CORPUS_PATH = ROOT / 'specs' / 'popups.yaml'
CATALOGUE_PATH = ROOT / 'specs' / 'tasks.yaml'
CAPTURED_AT = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


class _RecordingDevice:
    """The input seam: record taps and swipes instead of spawning adb."""

    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []
        self.swipes: list[tuple[int, int, int, int, int]] = []

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        self.swipes.append((x1, y1, x2, y2, duration_ms))


class _RouteRecorder:
    """The route seam: record each intent the policy fires."""

    def __init__(self) -> None:
        self.routes: list[SurfaceRoute] = []

    def __call__(self, route: SurfaceRoute) -> None:
        self.routes.append(route)


class _Capture:
    """A scripted frame sequence; it holds the last frame once exhausted."""

    def __init__(self, frames: Sequence[Snapshot]) -> None:
        if not frames:
            raise ValueError('a capture sequence needs at least one frame')
        self._frames = tuple(frames)
        self._index = 0

    def capture(self) -> Snapshot:
        frame = self._frames[min(self._index, len(self._frames) - 1)]
        self._index += 1
        return frame


class _SteppingClock:
    """An injected clock that advances on every read and sleep."""

    def __init__(self, step: float = 1.0) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        current = self.value
        self.value += self.step
        return current

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def _box(
    text: str,
    left: float,
    top: float,
    right: float,
    bottom: float,
    score: float = 0.99,
) -> TextBox:
    return TextBox(
        text=text,
        box=((left, top), (right, top), (right, bottom), (left, bottom)),
        score=score,
    )


def _snapshot(*boxes: TextBox, size: tuple[int, int] = (1200, 2000)) -> Snapshot:
    return Snapshot(
        image=Image.new('RGB', size, 'white'),
        boxes=boxes,
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(*size, density=240, sdk=35),
    )


def _load(path: Path, engine: RapidOcrEngine) -> Snapshot:
    with Image.open(path) as opened:
        image = opened.convert('RGB')
    return Snapshot(
        image=image,
        boxes=engine.read(image),
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(image.width, image.height, density=240, sdk=35),
    )


def _rebox(snapshot: Snapshot, boxes: Sequence[TextBox]) -> Snapshot:
    return Snapshot(
        image=snapshot.image,
        boxes=tuple(boxes),
        captured_at=snapshot.captured_at,
        geometry=snapshot.geometry,
    )


def _empty_boost(snapshot: Snapshot) -> Snapshot:
    return _rebox(
        snapshot,
        (
            _box('Coins boost', 100, 450, 500, 540),
            _box(EMPTY_BOOST_MARKER, 100, 600, 1100, 650),
        ),
    )


def _coin_page_without_rows(snapshot: Snapshot) -> Snapshot:
    """The recorded coin page with every task row and its control removed."""
    return _rebox(
        snapshot,
        (
            _box('Coins', 100, 220, 400, 290),
            _box('Coins credits to redeem', 100, 330, 800, 380),
            _box('Daily coins', 100, 560, 500, 610),
        ),
    )


def _controller(device: _RecordingDevice, frame: Snapshot) -> Controller:
    return Controller.from_frame(device, frame, rng=random.Random(0))


@pytest.fixture(scope='module')
def engine() -> RapidOcrEngine:
    return RapidOcrEngine()


@pytest.fixture(scope='module')
def catalogue() -> TaskCatalogue:
    return load_task_catalogue(CATALOGUE_PATH)


@pytest.fixture(scope='module')
def corpus() -> PopupCorpus:
    return load_popup_corpus(CORPUS_PATH)


@pytest.fixture(scope='module')
def frames(engine: RapidOcrEngine) -> dict[str, Snapshot]:
    return {
        'boost': _load(STATES / 'boost.png', engine),
        'coins': _load(STATES / 'coins.png', engine),
        'coin_store': _load(STATES / 'coin_store.png', engine),
        'launcher': _load(STATES / 'launcher.png', engine),
    }


@pytest.mark.slow
def test_classify_surface_distinguishes_the_recorded_variants(frames: dict[str, Snapshot]) -> None:
    assert classify_surface(frames['boost']) is Surface.BOOST_PAGE
    assert classify_surface(frames['coins']) is Surface.ROWS_PRESENT
    assert has_task_rows(frames['boost'])
    assert has_task_rows(frames['coins'])


def test_classify_surface_names_the_recorded_coin_store(frames: dict[str, Snapshot]) -> None:
    surface = classify_surface(frames['coin_store'])

    assert surface is Surface.COIN_STORE
    assert not has_task_rows(frames['coin_store'])


def test_the_recorded_launcher_with_icons_is_not_mistaken_for_a_bare_clock_surface(
    frames: dict[str, Snapshot],
) -> None:
    # This recon frame proves why a clock alone is not a launcher marker. Its
    # readable app icons keep it UNKNOWN; the package probe owns that case.
    assert classify_surface(frames['launcher']) is Surface.UNKNOWN
    assert not is_non_app_surface(frames['launcher'])


def test_the_retained_post_tap_labels_in_synthetic_boxes_classify_as_non_app() -> None:
    # Only the labels are retained; the boxes are synthetic, not a captured
    # PNG. The recogniser is therefore not proved against the missing frame.
    clock_surface = _snapshot(
        _box('3:36', 20, 5, 100, 35),
        _box('0', 1120, 5, 1160, 35),
        _box('日', 500, 700, 560, 760),
        _box('24', 580, 700, 640, 760),
    )

    assert is_non_app_surface(clock_surface)
    assert classify_surface(clock_surface) is Surface.NON_APP


@pytest.mark.parametrize(
    'boxes',
    [
        (_box('3:36', 20, 5, 100, 35),),
        (_box('3:36', 20, 5, 100, 35), _box('24', 580, 700, 640, 760)),
        (
            _box('3:36', 20, 5, 100, 35),
            _box('日', 500, 700, 560, 760),
            _box('24', 580, 900, 640, 960),
        ),
        (_box('3:36', 20, 5, 100, 35), _box('日', 500, 700, 560, 760)),
        (
            _box('3:36', 20, 5, 100, 35),
            _box('日', 500, 700, 560, 760),
            _box('24', 580, 1200, 640, 1260),
        ),
        (
            _box('3:36', 20, 5, 100, 35),
            _box('日', 500, 700, 560, 760),
            _box('24', 580, 700, 640, 760),
            _box('Shopping feed', 100, 900, 600, 950),
        ),
    ],
)
def test_a_clock_without_the_complete_date_widget_is_not_non_app(boxes: tuple[TextBox, ...]) -> None:
    assert not is_non_app_surface(_snapshot(*boxes))
    assert classify_surface(_snapshot(*boxes)) is not Surface.NON_APP


def test_known_app_markers_win_over_a_date_widget() -> None:
    app_page = _snapshot(
        _box('3:36', 20, 5, 100, 35),
        _box('日', 500, 700, 560, 760),
        _box('24', 580, 700, 640, 760),
        _box('Coins boost', 80, 90, 400, 150),
        _box('Daily check-in', 60, 420, 700, 470),
        _box('GO', 1000, 480, 1080, 530),
    )

    assert classify_surface(app_page) is Surface.BOOST_PAGE
    assert not is_non_app_surface(app_page)


@pytest.mark.slow
def test_classify_surface_names_the_recorded_search_landing(engine: RapidOcrEngine) -> None:
    landing = _load(STATES / 'search_landing.png', engine)

    assert is_search_landing(landing)
    assert classify_surface(landing) is Surface.SEARCH_LANDING
    assert classify_surface(_snapshot(_box('Search for what you love', 60, 400, 700, 450))) is Surface.UNKNOWN


def test_discover_more_without_a_category_grid_is_not_the_search_landing() -> None:
    heading_only = _snapshot(_box('Discover more', 60, 660, 400, 710))

    assert not is_search_landing(heading_only)
    assert classify_surface(heading_only) is Surface.UNKNOWN

    anchored = _snapshot(
        _box('Discover more', 60, 660, 400, 710),
        _box('Sneakers', 200, 800, 460, 850),
        _box('74k+ items', 200, 860, 380, 900),
    )
    assert is_search_landing(anchored)
    assert classify_surface(anchored) is Surface.SEARCH_LANDING


@pytest.mark.slow
def test_classify_surface_names_the_recorded_surprise_detail_and_grid(engine: RapidOcrEngine) -> None:
    detail = _load(ROOT / 'tests' / 'data' / 'flows' / 'surprise_items_item_detail.png', engine)
    grid = _load(ROOT / 'tests' / 'data' / 'flows' / 'surprise_items_round_grid.png', engine)

    assert classify_surface(detail) is Surface.PRODUCT_DETAIL
    assert classify_surface(grid) is Surface.SURPRISE_GRID


def test_generic_discount_copy_is_not_the_surprise_grid() -> None:
    coupon_page = _snapshot(
        _box('Disc:C$31.27', 60, 300, 330, 340),
        _box('Applied', 350, 300, 520, 340),
        _box('C$30.00', 60, 350, 300, 390),
        _box('Black Friday', 60, 420, 300, 460),
        _box('Free shipping', 60, 480, 300, 520),
        _box('7 sold', 60, 540, 240, 580),
        _box('Wireless CarPlay interface', 60, 600, 560, 640),
    )

    assert classify_surface(coupon_page) is Surface.UNKNOWN
    assert not is_surprise_grid(coupon_page)
    assert not is_surprise_grid(_snapshot(_box('Item 1/6', 40, 100, 300, 140)))
    assert not is_surprise_grid(
        _snapshot(
            _box('Coins boost', 80, 90, 400, 150),
            _box('Browse surprise items', 60, 420, 700, 470),
            _box('Tap 3 items on this page to earn', 60, 480, 700, 530),
        )
    )
    assert classify_surface(_snapshot(_box('Item 1/6', 40, 100, 300, 140))) is Surface.PRODUCT_DETAIL


def test_the_global_classifier_leaves_headerless_grids_unknown() -> None:
    grid = _snapshot(
        _box('Product A', 90, 300, 490, 355),
        _box('Product B', 690, 300, 1090, 355),
        _box('C$12.99', 90, 420, 300, 470),
        _box('C$13.99', 690, 420, 900, 470),
    )

    assert classify_surface(grid) is Surface.UNKNOWN


def test_classify_surface_recognises_the_empty_boost_recording(frames: dict[str, Snapshot]) -> None:
    empty = _empty_boost(frames['boost'])

    assert is_empty_boost(empty)
    assert classify_surface(empty) is Surface.EMPTY_BOOST
    assert not has_task_rows(empty)


def test_classify_surface_recognises_a_known_title_even_before_its_control(
    frames: dict[str, Snapshot],
) -> None:
    title_only = _rebox(
        frames['boost'], (_box('Coins boost', 100, 450, 500, 540), _box('Daily check-in', 100, 600, 700, 650))
    )

    assert classify_surface(title_only) is Surface.BOOST_PAGE


def test_classify_surface_recognises_the_redeem_interstitial_before_the_page_beneath(
    frames: dict[str, Snapshot],
) -> None:
    redeem_over_page = _rebox(
        frames['coins'],
        (*frames['coins'].boxes, _box('YOU CAN NOW REDEEM!', 100, 800, 1100, 900)),
    )

    assert classify_surface(redeem_over_page) is Surface.REDEEM_INTERSTITIAL


def test_classify_surface_reports_rows_absent_only_on_a_coin_page(frames: dict[str, Snapshot]) -> None:
    absent = _coin_page_without_rows(frames['coins'])

    assert classify_surface(absent) is Surface.ROWS_ABSENT
    assert classify_surface(_snapshot(_box('AliExpress', 100, 100, 400, 150))) is Surface.UNKNOWN
    assert classify_surface(_snapshot(_box('Coins boost', 100, 450, 500, 540))) is Surface.UNKNOWN


def test_the_shortcut_route_and_refresh_budget_are_the_measured_ones() -> None:
    assert 'aliexpress://goto?url=' in SHORTCUT_DEEP_LINK
    assert 'coin-index' in SHORTCUT_DEEP_LINK
    assert SURFACE_REFRESH_LIMIT == 1


def test_a_page_with_rows_fires_the_shortcut_once_without_re_navigation(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    catalogue: TaskCatalogue,
) -> None:
    device = _RecordingDevice()
    controller = _controller(device, frames['coins'])
    routes = _RouteRecorder()
    clock = _SteppingClock()

    roster = discover_roster(
        controller,
        _Capture((frames['coins'], frames['boost'])),
        corpus,
        catalogue,
        navigate=routes,
        timeout=5,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert routes.routes == [SurfaceRoute.SHORTCUT]
    assert roster.route is DiscoveryRoute.BOOST
    assert roster.surface_variant is Surface.BOOST_PAGE
    assert roster.routes_taken == (SurfaceRoute.SHORTCUT,)
    assert roster.refresh_attempted is False
    assert roster.variants_seen == (Surface.BOOST_PAGE,)
    assert roster.count == 4
    assert roster.status is RosterStatus.AVAILABLE
    assert len(device.taps) == 1

    payload = roster.as_dict()
    assert payload['surface_variant'] == 'boost_page'
    assert payload['routes_taken'] == ['shortcut']
    assert payload['refresh_attempted'] is False
    assert payload['variants_seen'] == ['boost_page']


def test_a_page_with_no_rows_re_fires_once_then_reports_nothing_available(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    catalogue: TaskCatalogue,
) -> None:
    absent = _coin_page_without_rows(frames['coins'])
    device = _RecordingDevice()
    controller = _controller(device, absent)
    routes = _RouteRecorder()
    clock = _SteppingClock()

    roster = discover_roster(
        controller,
        _Capture((absent, absent)),
        corpus,
        catalogue,
        navigate=routes,
        timeout=5,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert routes.routes == [SurfaceRoute.SHORTCUT, SurfaceRoute.SHORTCUT]
    assert roster.route is DiscoveryRoute.INLINE
    assert roster.surface_variant is Surface.ROWS_ABSENT
    assert roster.routes_taken == (SurfaceRoute.SHORTCUT, SurfaceRoute.SHORTCUT)
    assert roster.refresh_attempted is True
    assert roster.variants_seen == (Surface.ROWS_ABSENT, Surface.ROWS_ABSENT)
    assert roster.nothing_available
    assert roster.status is RosterStatus.NOTHING_AVAILABLE
    assert device.taps == []

    payload = roster.as_dict()
    assert payload['status'] == 'nothing available'
    assert payload['surface_variant'] == 'rows_absent'
    assert payload['routes_taken'] == ['shortcut', 'shortcut']
    assert payload['refresh_attempted'] is True


def test_a_visible_title_without_a_legible_control_still_gets_the_one_refresh(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    catalogue: TaskCatalogue,
) -> None:
    title_only = _rebox(
        frames['coins'],
        (
            _box('Coins', 100, 220, 400, 290),
            _box('Coins credits to redeem', 100, 330, 800, 380),
            _box('Daily coins', 100, 560, 500, 610),
            _box('Daily check-in', 100, 700, 700, 750),
        ),
    )
    device = _RecordingDevice()
    controller = _controller(device, title_only)
    routes = _RouteRecorder()
    clock = _SteppingClock()

    roster = discover_roster(
        controller,
        _Capture((title_only, title_only)),
        corpus,
        catalogue,
        navigate=routes,
        timeout=5,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert routes.routes == [SurfaceRoute.SHORTCUT, SurfaceRoute.SHORTCUT]
    assert roster.surface_variant is Surface.ROWS_PRESENT
    assert roster.variants_seen == (Surface.ROWS_PRESENT, Surface.ROWS_PRESENT)
    assert roster.refresh_attempted is True
    assert roster.nothing_available


def test_an_empty_boost_page_refreshes_once_and_uses_the_warm_re_entry(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    catalogue: TaskCatalogue,
) -> None:
    empty = _empty_boost(frames['boost'])
    device = _RecordingDevice()
    controller = _controller(device, frames['coins'])
    routes = _RouteRecorder()
    clock = _SteppingClock()

    roster = discover_roster(
        controller,
        _Capture((frames['coins'], empty, frames['coins'], frames['boost'])),
        corpus,
        catalogue,
        navigate=routes,
        timeout=5,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert routes.routes == [SurfaceRoute.SHORTCUT, SurfaceRoute.SHORTCUT]
    assert roster.surface_variant is Surface.BOOST_PAGE
    assert roster.variants_seen == (Surface.EMPTY_BOOST, Surface.BOOST_PAGE)
    assert roster.refresh_attempted is True
    assert roster.count == 4
    assert roster.status is RosterStatus.AVAILABLE
    assert len(device.taps) == 2


def test_an_empty_boost_page_that_stays_empty_is_nothing_available_after_one_refresh(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    catalogue: TaskCatalogue,
) -> None:
    empty = _empty_boost(frames['boost'])
    device = _RecordingDevice()
    controller = _controller(device, frames['coins'])
    routes = _RouteRecorder()
    clock = _SteppingClock()

    roster = discover_roster(
        controller,
        _Capture((frames['coins'], empty, frames['coins'], empty)),
        corpus,
        catalogue,
        navigate=routes,
        timeout=5,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert routes.routes == [SurfaceRoute.SHORTCUT, SurfaceRoute.SHORTCUT]
    assert roster.surface_variant is Surface.EMPTY_BOOST
    assert roster.variants_seen == (Surface.EMPTY_BOOST, Surface.EMPTY_BOOST)
    assert roster.refresh_attempted is True
    assert roster.nothing_available
    assert roster.status is RosterStatus.NOTHING_AVAILABLE
    assert len(device.taps) == 2


def test_the_refresh_can_be_disabled_for_a_caller_that_owns_the_reset(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    catalogue: TaskCatalogue,
) -> None:
    empty = _empty_boost(frames['boost'])
    device = _RecordingDevice()
    controller = _controller(device, frames['coins'])
    routes = _RouteRecorder()
    clock = _SteppingClock()

    roster = discover_roster(
        controller,
        _Capture((frames['coins'], empty)),
        corpus,
        catalogue,
        navigate=routes,
        refresh=False,
        timeout=5,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert routes.routes == [SurfaceRoute.SHORTCUT]
    assert roster.surface_variant is Surface.EMPTY_BOOST
    assert roster.refresh_attempted is False
    assert roster.nothing_available


def test_without_a_route_firer_arrival_is_classified_but_not_re_navigated(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    catalogue: TaskCatalogue,
) -> None:
    empty = _empty_boost(frames['boost'])
    device = _RecordingDevice()
    controller = _controller(device, frames['coins'])
    clock = _SteppingClock()

    roster = discover_roster(
        controller,
        _Capture((frames['coins'], empty)),
        corpus,
        catalogue,
        timeout=5,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert roster.surface_variant is Surface.EMPTY_BOOST
    assert roster.routes_taken == ()
    assert roster.refresh_attempted is False
    assert roster.nothing_available
    assert len(device.taps) == 1


def test_an_unknown_arrival_gets_the_same_single_warm_re_entry(
    corpus: PopupCorpus,
    catalogue: TaskCatalogue,
) -> None:
    unknown = _snapshot(_box('AliExpress', 100, 100, 400, 150))
    device = _RecordingDevice()
    controller = _controller(device, unknown)
    routes = _RouteRecorder()
    clock = _SteppingClock()

    roster = discover_roster(
        controller,
        _Capture((unknown,)),
        corpus,
        catalogue,
        navigate=routes,
        timeout=2,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert routes.routes == [SurfaceRoute.SHORTCUT, SurfaceRoute.SHORTCUT]
    assert roster.surface_variant is Surface.UNKNOWN
    assert roster.variants_seen == (Surface.UNKNOWN, Surface.UNKNOWN)
    assert roster.refresh_attempted is True
    assert roster.nothing_available
    assert device.taps == []


@pytest.mark.parametrize(
    ('boxes', 'expected'),
    [
        ((_box('Sign in', 100, 300, 300, 360), _box('Welcome to AliExpress', 100, 400, 800, 460)), True),
        ((_box('Log in', 100, 300, 300, 360),), True),
        ((_box('Sign in / Sign up', 100, 300, 400, 360),), True),
        (
            (
                _box('Daily coins', 100, 560, 500, 610),
                _box('Coins credits to redeem', 100, 330, 700, 380),
                _box('Sign in', 900, 200, 1040, 240),
            ),
            False,
        ),
        ((_box('Coins boost', 100, 100, 500, 150), _box('Sign in', 900, 200, 1040, 240)), False),
        ((_box('For you', 100, 200, 400, 260),), False),
    ],
)
def test_logged_out_is_a_prompt_without_a_signed_in_surface(
    boxes: tuple[TextBox, ...],
    expected: bool,
) -> None:
    assert is_logged_out(_snapshot(*boxes)) is expected
