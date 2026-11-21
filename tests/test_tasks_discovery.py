"""Task discovery over the committed boost and coin-page fixtures (issue #22).

The device is simulated at the capture and input seams only: the real controller
runs with a recording fake device, and every frame comes from a reviewed fixture
or a hand-built box list. No test reaches adb.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from aliexpress_coins.control.controller import Controller
from aliexpress_coins.control.popups import PopupCorpus, load_popup_corpus
from aliexpress_coins.perception.errors import StateTimeout
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.ocr import RapidOcrEngine
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.state import ScreenState
from aliexpress_coins.tasks.catalogue import (
    DecisionKind,
    TaskCatalogue,
    TaskCategory,
    load_task_catalogue,
)
from aliexpress_coins.tasks.discovery import (
    DEFAULT_BOOST_INTERVAL,
    DEFAULT_BOOST_TIMEOUT,
    EARN_MORE_LABEL,
    EMPTY_BOOST_MARKER,
    BoostList,
    DiscoveryRoute,
    Roster,
    RosterEntry,
    RosterStatus,
    discover_roster,
    enumerate_roster,
    is_boost_page,
    is_empty_boost,
    open_boost_list,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'tests' / 'data'
STATES = DATA / 'states'
CORPUS_PATH = ROOT / 'specs' / 'popups.yaml'
CATALOGUE_PATH = ROOT / 'specs' / 'tasks.yaml'
CAPTURED_AT = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


class _RecordingDevice:
    """The input seam: record taps instead of spawning adb."""

    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))


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


def _without_earn_more(snapshot: Snapshot) -> Snapshot:
    return Snapshot(
        image=snapshot.image,
        boxes=tuple(box for box in snapshot.boxes if 'earn more' not in box.text.casefold()),
        captured_at=snapshot.captured_at,
        geometry=snapshot.geometry,
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
        'boost': _load(DATA / 'boost.png', engine),
        'state_boost': _load(STATES / 'boost.png', engine),
        'coins': _load(STATES / 'coins.png', engine),
        'launch': _load(DATA / 'popups' / 'launch_welcome_gifts.png', engine),
        'sheet': _load(DATA / 'popups' / 'coins_boost_sheet.png', engine),
        'skeleton': _load(STATES / 'skeleton.png', engine),
    }


def _empty_boost(snapshot: Snapshot) -> Snapshot:
    return Snapshot(
        image=snapshot.image,
        boxes=(
            _box('Coins boost', 100, 450, 500, 540),
            _box(EMPTY_BOOST_MARKER, 100, 600, 1100, 650),
        ),
        captured_at=snapshot.captured_at,
        geometry=snapshot.geometry,
    )


def _rebox(snapshot: Snapshot, boxes: Sequence[TextBox]) -> Snapshot:
    return Snapshot(
        image=snapshot.image,
        boxes=tuple(boxes),
        captured_at=snapshot.captured_at,
        geometry=snapshot.geometry,
    )


_EXPECTED_BOOST: dict[str, tuple[tuple[str, str, TaskCategory | None, str | None], ...]] = {
    'boost': (
        ('Daily check-in', 'boost_checkin', TaskCategory.SIGN_IN, None),
        ('Explore sponsored items', 'sponsored_items', TaskCategory.BROWSE_WAIT, None),
        ('Browse surprise items', 'surprise_items', TaskCategory.ITEM_TAP, None),
        ('Place an order on this page', 'spending_order', None, 'spending'),
    ),
    'state_boost': (
        ('Take the coins quiz', 'coins_quiz', TaskCategory.QUIZ, None),
        ('Explore sponsored items', 'sponsored_items', TaskCategory.BROWSE_WAIT, None),
        ('Browse surprise items', 'surprise_items', TaskCategory.ITEM_TAP, None),
        ('Place an order on this page', 'spending_order', None, 'spending'),
    ),
    'sheet': (
        ('Explore sponsored items', 'sponsored_items', TaskCategory.BROWSE_WAIT, None),
        ('Browse surprise items', 'surprise_items', TaskCategory.ITEM_TAP, None),
        ('Place an order on this page', 'spending_order', None, 'spending'),
        ('Browse recently viewed items', 'recently_viewed', TaskCategory.BROWSE_WAIT, None),
    ),
}


@pytest.mark.parametrize('fixture', tuple(_EXPECTED_BOOST))
def test_the_measured_boost_fixtures_enumerate_every_row_with_its_decision(
    frames: dict[str, Snapshot],
    catalogue: TaskCatalogue,
    fixture: str,
) -> None:
    snapshot = frames[fixture]
    roster = enumerate_roster(snapshot, catalogue, route=DiscoveryRoute.BOOST)

    assert is_boost_page(snapshot)
    assert is_empty_boost(snapshot) is False
    assert roster.count == 4
    assert roster.status is RosterStatus.AVAILABLE
    assert roster.route is DiscoveryRoute.BOOST
    assert roster.titles == tuple(item[0] for item in _EXPECTED_BOOST[fixture])

    for entry, (title, key, category, reason_part) in zip(roster.entries, _EXPECTED_BOOST[fixture], strict=True):
        assert entry.title == title
        assert entry.action is not None
        assert entry.decision.title == title
        if category is None:
            assert entry.denied
            assert entry.kind is DecisionKind.DENY
            assert entry.decision.decision is DecisionKind.DENY
            assert entry.key is None
            assert entry.category is None
            assert entry.spec is None
            assert entry.rule == key
            assert entry.reason is not None
            assert reason_part is not None
            assert reason_part in entry.reason
            assert entry.dispatch_target is None
            assert entry.dispatchable is False
        else:
            assert entry.allowed
            assert entry.kind is DecisionKind.ALLOW
            assert entry.decision.decision is DecisionKind.ALLOW
            assert entry.key == key
            assert entry.category is category
            assert entry.spec is not None
            assert entry.spec.key == key
            assert entry.reason is None
            assert entry.dispatch_target is entry.action
            assert entry.dispatchable is True

    assert any(entry.denied for entry in roster.entries)
    assert any(entry.title == 'Place an order on this page' for entry in roster.denied_entries)
    assert len(roster.allowed_entries) == 3
    assert len(roster.denied_entries) == 1


def test_the_measured_fixtures_keep_their_progress_badges(
    frames: dict[str, Snapshot],
    catalogue: TaskCatalogue,
) -> None:
    boost = enumerate_roster(frames['boost'], catalogue)
    state_boost = enumerate_roster(frames['state_boost'], catalogue)

    assert [(entry.title, entry.progress_text) for entry in boost.entries] == [
        ('Daily check-in', None),
        ('Explore sponsored items', '0/6'),
        ('Browse surprise items', '0/2'),
        ('Place an order on this page', '0/3'),
    ]
    assert [(entry.title, entry.progress_text) for entry in state_boost.entries] == [
        ('Take the coins quiz', None),
        ('Explore sponsored items', '0/6'),
        ('Browse surprise items', '0/2'),
        ('Place an order on this page', '0/3'),
    ]


def test_a_refused_row_is_reported_but_never_gets_a_dispatch_target(
    frames: dict[str, Snapshot],
    catalogue: TaskCatalogue,
) -> None:
    roster = enumerate_roster(frames['state_boost'], catalogue)
    refused = next(entry for entry in roster.entries if entry.title == 'Place an order on this page')

    assert refused.action is not None
    assert refused.action_target is refused.action
    assert refused.dispatch_target is None
    assert refused.dispatchable is False
    assert refused.deny_reason is not None
    assert 'refused' in refused.deny_reason

    payload = refused.as_dict()
    assert payload['allowed'] is False
    assert payload['reason'] == refused.reason
    assert payload['action'] is not None
    assert payload['dispatch_target'] is None


def test_an_unrecognised_row_is_kept_with_the_default_refusal_reason() -> None:
    snapshot = _snapshot(
        _box('0/6', 900, 100, 1050, 130),
        _box('Invent a new task', 80, 110, 700, 150),
        _box('GO', 980, 150, 1040, 180),
    )

    roster = enumerate_roster(snapshot, load_task_catalogue(CATALOGUE_PATH))

    assert roster.count == 1
    entry = roster.entries[0]
    assert entry.title == 'Invent a new task'
    assert entry.progress_text == '0/6'
    assert entry.denied
    assert entry.reason == 'unrecognised'
    assert entry.rule is None
    assert entry.dispatch_target is None


def test_duplicate_rows_with_the_same_title_are_listed_once() -> None:
    snapshot = _snapshot(
        _box('Explore sponsored items', 80, 100, 700, 140),
        _box('GO', 980, 150, 1040, 180),
        _box('Explore sponsored items', 80, 500, 700, 540),
        _box('GO', 980, 550, 1040, 580),
    )

    roster = enumerate_roster(snapshot, load_task_catalogue(CATALOGUE_PATH))

    assert roster.titles == ('Explore sponsored items',)


def test_the_roster_report_carries_the_route_status_and_catalogue_version(
    frames: dict[str, Snapshot],
    catalogue: TaskCatalogue,
) -> None:
    roster = enumerate_roster(frames['boost'], catalogue, route=DiscoveryRoute.BOOST)

    payload = roster.as_dict()

    assert payload['route'] == 'boost'
    assert payload['status'] == 'available'
    assert payload['count'] == 4
    assert payload['allowed_count'] == 3
    assert payload['denied_count'] == 1
    assert payload['nothing_available'] is False
    assert payload['catalogue'] == {
        'version': catalogue.version,
        'default_deny_reason': catalogue.default_deny_reason,
    }
    assert len(cast('list[object]', payload['entries'])) == 4
    assert roster.tasks == roster.entries
    assert roster.allowed == roster.allowed_entries
    assert roster.denied == roster.denied_entries
    assert roster.message == 'available'


def test_an_empty_surface_reports_nothing_available_not_an_error(
    frames: dict[str, Snapshot],
    catalogue: TaskCatalogue,
) -> None:
    empty = _empty_boost(frames['state_boost'])
    roster = enumerate_roster(empty, catalogue, route=DiscoveryRoute.BOOST)

    assert is_boost_page(empty)
    assert is_empty_boost(empty)
    assert roster.count == 0
    assert roster.empty
    assert roster.nothing_available
    assert roster.status is RosterStatus.NOTHING_AVAILABLE
    assert roster.message == 'nothing available'
    assert roster.as_dict()['status'] == 'nothing available'


def test_the_empty_marker_matches_when_ocr_keeps_the_whole_sentence(
    frames: dict[str, Snapshot],
) -> None:
    snapshot = Snapshot(
        image=frames['state_boost'].image,
        boxes=(
            _box('Coins boost', 100, 450, 500, 540),
            _box("There's nothing here yet, but come check back later!", 100, 600, 1100, 650),
        ),
        captured_at=frames['state_boost'].captured_at,
        geometry=frames['state_boost'].geometry,
    )

    assert is_empty_boost(snapshot)
    roster = enumerate_roster(snapshot, load_task_catalogue(CATALOGUE_PATH), route=DiscoveryRoute.BOOST)
    assert roster.nothing_available
    assert roster.status.value == 'nothing available'


def test_a_missing_earn_more_degrades_to_the_inline_rows(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    catalogue: TaskCatalogue,
) -> None:
    coin_page = _without_earn_more(frames['coins'])
    device = _RecordingDevice()
    controller = _controller(device, coin_page)

    opened = open_boost_list(controller, lambda: coin_page, corpus)

    assert opened.route is DiscoveryRoute.INLINE
    assert opened.inline
    assert opened.opened is False
    assert opened.tap is None
    assert opened.dismiss is None
    assert device.taps == []
    assert EARN_MORE_LABEL in opened.detail

    roster = enumerate_roster(opened.snapshot, catalogue, route=opened.route)
    assert roster.route is DiscoveryRoute.INLINE
    assert roster.titles == (
        'Daily check-in',
        'Explore sponsored items',
        'Browse surprise items',
    )
    assert roster.count == 3
    assert roster.as_dict()['route'] == 'inline'

    opened_payload = opened.as_dict()
    assert opened_payload['route'] == 'inline'
    assert 'tap' not in opened_payload
    assert 'dismiss' not in opened_payload


def test_a_row_with_no_title_is_not_guessed() -> None:
    roster = enumerate_roster(
        _snapshot(_box('GO', 980, 100, 1040, 130)),
        load_task_catalogue(CATALOGUE_PATH),
    )

    assert roster.count == 0
    assert roster.nothing_available


def test_open_boost_list_taps_earn_more_once_and_waits_for_the_list(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    catalogue: TaskCatalogue,
) -> None:
    device = _RecordingDevice()
    controller = _controller(device, frames['coins'])
    clock = _SteppingClock()
    capture = _Capture((frames['coins'], frames['state_boost']))

    opened = open_boost_list(
        controller,
        capture,
        corpus,
        timeout=5,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert isinstance(opened, BoostList)
    assert opened.route is DiscoveryRoute.BOOST
    assert opened.opened
    assert opened.inline is False
    assert opened.empty is False
    assert opened.tap is not None
    assert opened.tap.label == EARN_MORE_LABEL
    assert opened.dismiss is None
    assert opened.history == (ScreenState.PAGE, ScreenState.PAGE)
    assert opened.classification is not None
    assert opened.classification.state is ScreenState.PAGE
    assert len(device.taps) == 1
    assert controller.tap_count == 1

    roster = enumerate_roster(opened.snapshot, catalogue, route=opened.route)
    assert roster.count == 4
    assert roster.route is DiscoveryRoute.BOOST


def test_open_boost_list_waits_for_the_task_set_to_stop_changing(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    catalogue: TaskCatalogue,
) -> None:
    partial = _rebox(
        frames['state_boost'],
        tuple(box for box in frames['state_boost'].boxes if box.center[1] < 950),
    )
    device = _RecordingDevice()
    controller = _controller(device, frames['coins'])
    clock = _SteppingClock()
    capture = _Capture((frames['coins'], partial, frames['state_boost'], frames['state_boost']))

    opened = open_boost_list(
        controller,
        capture,
        corpus,
        timeout=10,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    roster = enumerate_roster(opened.snapshot, catalogue, route=opened.route)
    assert roster.titles == (
        'Take the coins quiz',
        'Explore sponsored items',
        'Browse surprise items',
        'Place an order on this page',
    )
    assert opened.history == (ScreenState.PAGE, ScreenState.PAGE, ScreenState.PAGE)


def test_open_boost_list_sweeps_a_popup_over_an_already_populated_list(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    catalogue: TaskCatalogue,
) -> None:
    overlay = _rebox(frames['launch'], (*frames['state_boost'].boxes, *frames['launch'].boxes))
    device = _RecordingDevice()
    controller = _controller(device, frames['coins'])
    clock = _SteppingClock()
    capture = _Capture((frames['coins'], overlay, overlay, frames['state_boost']))

    opened = open_boost_list(
        controller,
        capture,
        corpus,
        timeout=10,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert opened.classification is not None
    assert opened.classification.state is ScreenState.PAGE
    assert opened.dismiss is not None
    assert opened.dismiss.closed == ('launch_welcome_gifts',)
    assert ScreenState.POPUP in opened.history
    assert len(device.taps) == 2
    assert enumerate_roster(opened.snapshot, catalogue).count == 4


def test_open_boost_list_retries_a_loading_coin_page_before_choosing_inline(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
) -> None:
    device = _RecordingDevice()
    controller = _controller(device, frames['skeleton'])
    clock = _SteppingClock()
    capture = _Capture((frames['skeleton'], frames['coins'], frames['state_boost']))

    opened = open_boost_list(
        controller,
        capture,
        corpus,
        timeout=10,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert opened.route is DiscoveryRoute.BOOST
    assert opened.tap is not None
    assert opened.tap.label == EARN_MORE_LABEL
    assert len(device.taps) == 1


def test_open_boost_list_sweeps_an_initial_popup_before_reading_the_coin_page(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
) -> None:
    device = _RecordingDevice()
    controller = _controller(device, frames['launch'])
    clock = _SteppingClock()
    capture = _Capture((frames['launch'], frames['launch'], frames['coins'], frames['coins'], frames['state_boost']))

    opened = open_boost_list(
        controller,
        capture,
        corpus,
        timeout=10,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert opened.route is DiscoveryRoute.BOOST
    assert opened.dismiss is not None
    assert opened.dismiss.closed == ('launch_welcome_gifts',)
    assert opened.tap is not None
    assert len(device.taps) == 2


def test_open_boost_list_sweeps_a_popup_seen_on_the_way(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
) -> None:
    device = _RecordingDevice()
    controller = _controller(device, frames['coins'])
    clock = _SteppingClock()
    capture = _Capture((frames['coins'], frames['launch'], frames['launch'], frames['state_boost']))

    opened = open_boost_list(
        controller,
        capture,
        corpus,
        timeout=10,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert opened.route is DiscoveryRoute.BOOST
    assert opened.dismiss is not None
    assert opened.dismiss.closed == ('launch_welcome_gifts',)
    assert ScreenState.POPUP in opened.history
    assert len(device.taps) == 2
    assert controller.tap_count == 2
    payload = opened.as_dict()
    assert payload['dismiss'] is not None
    assert payload['tap'] is not None


def test_open_boost_list_reports_the_empty_destination(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
) -> None:
    empty = _empty_boost(frames['state_boost'])
    device = _RecordingDevice()
    controller = _controller(device, frames['coins'])
    clock = _SteppingClock()

    opened = open_boost_list(
        controller,
        _Capture((frames['coins'], empty)),
        corpus,
        timeout=5,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert opened.route is DiscoveryRoute.BOOST
    assert opened.empty
    assert opened.status is RosterStatus.NOTHING_AVAILABLE
    assert 'nothing available' in opened.detail
    assert len(device.taps) == 1


def test_open_boost_list_times_out_with_the_states_it_saw(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
) -> None:
    device = _RecordingDevice()
    controller = _controller(device, frames['coins'])
    clock = _SteppingClock()
    capture = _Capture((frames['coins'], frames['coins']))

    with pytest.raises(StateTimeout, match='Coins boost') as raised:
        open_boost_list(
            controller,
            capture,
            corpus,
            timeout=2,
            interval=1,
            clock=clock,
            sleep=clock.sleep,
        )

    assert 'history=' in str(raised.value)
    assert len(device.taps) == 1


def test_discover_roster_records_the_route_it_used(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    catalogue: TaskCatalogue,
) -> None:
    device = _RecordingDevice()
    controller = _controller(device, frames['coins'])
    clock = _SteppingClock()

    roster = discover_roster(
        controller,
        _Capture((frames['coins'], frames['state_boost'])),
        corpus,
        catalogue,
        timeout=5,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert isinstance(roster, Roster)
    assert roster.route is DiscoveryRoute.BOOST
    assert roster.count == 4
    assert len(device.taps) == 1


def test_open_result_renders_its_route_and_empty_state(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
) -> None:
    empty = _empty_boost(frames['state_boost'])
    device = _RecordingDevice()
    controller = _controller(device, frames['coins'])

    opened = open_boost_list(controller, _Capture((frames['coins'], empty)), corpus)

    payload = opened.as_dict()
    assert payload['route'] == 'boost'
    assert payload['empty'] is True
    assert payload['state'] == 'error'
    assert 'dismiss' not in payload


def test_roster_entry_and_roster_constructors_default_cleanly(
    frames: dict[str, Snapshot],
    catalogue: TaskCatalogue,
) -> None:
    entry = enumerate_roster(frames['boost'], catalogue).entries[0]
    assert isinstance(entry, RosterEntry)

    roster = Roster(entries=(entry,))
    assert roster.route is None
    assert roster.catalogue_version is None
    assert roster.count == 1
    assert roster.as_dict()['route'] is None


def test_discovery_constants_describe_the_measured_defaults() -> None:
    assert EARN_MORE_LABEL == 'Earn more'
    assert EMPTY_BOOST_MARKER == "There's nothing here yet"
    assert DEFAULT_BOOST_TIMEOUT > 0
    assert DEFAULT_BOOST_INTERVAL > 0


@pytest.mark.parametrize(
    ('options', 'message'),
    [
        ({'timeout': 0}, 'timeout'),
        ({'interval': 0}, 'interval'),
        ({'settle': -1}, 'settle'),
    ],
)
def test_open_boost_list_rejects_invalid_wait_options(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    options: dict[str, Any],
    message: str,
) -> None:
    device = _RecordingDevice()
    controller = _controller(device, frames['coins'])

    with pytest.raises(ValueError, match=message):
        open_boost_list(controller, _Capture((frames['coins'],)), corpus, **options)

    assert device.taps == []
