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
)

ROOT = Path(__file__).resolve().parents[1]
STATES = ROOT / 'tests' / 'data' / 'states'
CORPUS_PATH = ROOT / 'specs' / 'popups.yaml'
CATALOGUE_PATH = ROOT / 'specs' / 'tasks.yaml'
CAPTURED_AT = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


class _RecordingDevice:
    """The input seam: record taps instead of spawning adb."""

    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))


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
    }


@pytest.mark.slow
def test_classify_surface_distinguishes_the_recorded_variants(frames: dict[str, Snapshot]) -> None:
    assert classify_surface(frames['boost']) is Surface.BOOST_PAGE
    assert classify_surface(frames['coins']) is Surface.ROWS_PRESENT
    assert has_task_rows(frames['boost'])
    assert has_task_rows(frames['coins'])


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
