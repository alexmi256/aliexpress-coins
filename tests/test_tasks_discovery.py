"""Task discovery over the committed boost and coin-page fixtures (issue #22).

The device is simulated at the capture and input seams only: the real controller
runs with a recording fake device, and every frame comes from a reviewed fixture
or a hand-built box list. No test reaches adb.
"""

from __future__ import annotations

import json
import logging
import random
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image, ImageDraw

from aliexpress_coins.control.controller import Controller
from aliexpress_coins.control.popups import DismissReport, PopupCorpus, load_popup_corpus
from aliexpress_coins.perception.errors import SnapshotError, StateTimeout
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.matching import find_text
from aliexpress_coins.perception.ocr import RapidOcrEngine
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.state import ScreenState, classify
from aliexpress_coins.tasks.catalogue import (
    DecisionKind,
    TaskCatalogue,
    TaskCategory,
    TaskPolicyContext,
    load_task_catalogue,
)
from aliexpress_coins.tasks.discovery import (
    DEFAULT_BOOST_INTERVAL,
    DEFAULT_BOOST_TIMEOUT,
    DEFAULT_OBSERVATION_CAP,
    EARN_MORE_LABEL,
    EMPTY_BOOST_MARKER,
    ROSTER_LOG_NAME,
    ROSTER_OBSERVATION_SCHEMA,
    BoostList,
    DiscoveryRoute,
    Roster,
    RosterEntry,
    RosterLog,
    RosterObserver,
    RosterStatus,
    discover_roster,
    enumerate_roster,
    is_boost_page,
    is_empty_boost,
    observe_roster,
    open_boost_list,
    read_roster_log,
    wait_for_coin_page,
)
from aliexpress_coins.tasks.dispatch import DispatchGate
from aliexpress_coins.tasks.errors import RosterLogError
from aliexpress_coins.tasks.surface import Surface, classify_surface, has_task_rows

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


def _with_image_change(snapshot: Snapshot, rect: tuple[int, int, int, int], fill: tuple[int, int, int]) -> Snapshot:
    """A distinct capture of the same screen with a changed pixel region.

    The OCR boxes are kept, so the frame still classifies as the same page; only
    the image signature changes, which is what the re-tap threshold reads.
    """
    image = snapshot.image.copy()
    ImageDraw.Draw(image).rectangle(rect, fill=fill)
    return Snapshot(
        image=image,
        boxes=snapshot.boxes,
        captured_at=snapshot.captured_at,
        geometry=snapshot.geometry,
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
        'real_boost': _load(STATES / 'boost_daily_checkin.png', engine),
        'coins': _load(STATES / 'coins.png', engine),
        'merged_card': _load(STATES / 'coins_merged_card.png', engine),
        'search_row': _load(STATES / 'coins_search_row.png', engine),
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
@pytest.mark.slow
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


@pytest.mark.slow
def test_the_search_row_capture_enumerates_the_card_and_every_task_separately(
    frames: dict[str, Snapshot],
    catalogue: TaskCatalogue,
) -> None:
    """The exact 2026-11-24 23:25 EST frame from issue #107."""
    roster = enumerate_roster(frames['search_row'], catalogue, route=DiscoveryRoute.INLINE)

    assert roster.titles == (
        'Daily coins',
        'Search for what you love',
        'Place an order on this page',
        'View Super discounts',
    )
    daily, search, order, discounts = roster.entries
    assert daily.key == 'daily_checkin'
    assert daily.action is None  # Today was already claimed; the card has no Collect.
    assert daily.subtitle is not None
    assert daily.subtitle.endswith('Worth C$0.01')
    assert 'Search for what you love' not in daily.subtitle
    assert search.allowed
    assert search.key == 'search_for_what_you_love'
    assert search.category is TaskCategory.BROWSE
    assert search.progress_text == '0/2'
    assert search.action is not None
    assert search.action.text == 'GO'
    assert search.dispatch_target is not None
    authorized_search = DispatchGate(catalogue, surface='rows_present').authorize(search)
    assert authorized_search.key == 'search_for_what_you_love'
    assert authorized_search.category is TaskCategory.BROWSE
    assert order.rule == 'spending_order'
    assert order.progress_text == '0/3'
    # `View Super discounts` is a timed view, not a mini-game. The strip of
    # tiles below it must not become its subtitle.
    assert discounts.allowed
    assert discounts.key == 'super_discounts'
    assert discounts.category is TaskCategory.BROWSE_WAIT
    assert discounts.subtitle is None or 'Play & Earn' not in discounts.subtitle
    assert discounts.dispatch_target is not None
    # Its own copy is the title, GO and +1 reward. The shortcut strip below is
    # not part of this row and is not a task row of its own.
    assert '+1' in discounts.row.text
    assert all(tile not in discounts.row.text for tile in ('Merge Boss', 'Play & Earn', 'Prize Land'))
    assert discounts.policy_context is not None
    assert discounts.policy_context.title_elements == ('View Super discounts',)
    assert discounts.policy_context.subtitle_elements == ()
    assert {'Merge Boss', 'Play & Earn', 'Prize Land'} <= set(discounts.policy_context.neighbouring_elements)
    # Replay the exact bad parse from #109 through the dispatch gate. The tile
    # text is still a separate element in this captured frame, so it cannot
    # become evidence about the row even if a future splitter absorbs it again.
    tiles = tuple(
        box.text for box in frames['search_row'].boxes if box.text in {'Merge Boss', 'Play & Earn', 'Prize Land'}
    )
    polluted_subtitle = ' '.join(tiles)
    polluted_context = TaskPolicyContext(
        title_elements=('View Super discounts',),
        subtitle_elements=(polluted_subtitle,),
        neighbouring_elements=tiles,
    )
    polluted = replace(discounts, subtitle=polluted_subtitle, policy_context=polluted_context)
    authorized = DispatchGate(catalogue).authorize(polluted)
    assert authorized.key == 'super_discounts'
    assert authorized.category is TaskCategory.BROWSE_WAIT
    assert authorized.dispatch_target is not None
    assert roster.visible_action_count == 3
    assert roster.unassigned_action_count == 0
    assert roster.incomplete is False
    absorbed = {(failure.title, failure.container_title) for failure in roster.split_failures}
    assert ('Search for what you love', 'Daily coins') in absorbed
    assert ('Play & Earn', 'View Super discounts') in absorbed
    assert all(failure.recovered for failure in roster.split_failures)
    assert roster.warning == f'{len(roster.split_failures)} split failure(s) were recovered'
    # The run report must carry the repaired absorption, not only the final rows.
    payload = roster.as_dict()
    assert payload['warning'] == roster.warning
    assert len(cast('list[object]', payload['split_failures'])) == len(roster.split_failures)


@pytest.mark.slow
def test_the_merged_card_capture_pairs_collect_and_daily_checkin_with_their_own_controls(
    frames: dict[str, Snapshot],
    catalogue: TaskCatalogue,
) -> None:
    roster = enumerate_roster(frames['merged_card'], catalogue, route=DiscoveryRoute.INLINE)

    assert roster.titles == (
        'Daily coins',
        'Daily check-in',
        'Explore sponsored items',
        'Browse surprise items',
    )
    daily, checkin, sponsored, surprise = roster.entries
    assert daily.action is not None
    assert daily.action.text == 'Collect'
    assert daily.subtitle is not None
    assert daily.subtitle.endswith('Worth C$0.01')
    assert 'Daily check-in' not in daily.subtitle
    assert checkin.key == 'boost_checkin'
    assert checkin.action is not None
    assert checkin.action.text == 'GO'
    assert sponsored.progress_text == '0/6'
    assert surprise.progress_text == '0/2'
    assert roster.visible_action_count == 3
    assert roster.unassigned_action_count == 0
    assert roster.incomplete is False
    assert [(failure.title, failure.container_title) for failure in roster.split_failures] == [
        ('Daily check-in', 'Daily coins'),
    ]
    assert all(failure.recovered for failure in roster.split_failures)


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


def test_a_card_control_does_not_mask_an_unassigned_go() -> None:
    snapshot = _snapshot(
        _box('Daily coins', 100, 500, 500, 540),
        _box('Worth C$0.01', 100, 600, 400, 640),
        _box('Collect', 900, 600, 1000, 640),
        _box('Take the coins quiz', 80, 800, 700, 840),
        _box('GO', 980, 831, 1040, 861),
        _box('GO', 980, 1000, 1040, 1030),
    )

    roster = enumerate_roster(snapshot, load_task_catalogue(CATALOGUE_PATH))

    assert roster.entries[0].title == 'Daily coins'
    assert roster.entries[0].action is not None
    assert roster.entries[0].action.text == 'Collect'
    # The Collect control is not a GO button, so two visible GO buttons with
    # one row means one unassigned action, not a clean read.
    assert roster.visible_action_count == 2
    assert roster.unassigned_action_count == 1
    assert roster.incomplete


def test_an_actual_mini_game_row_is_still_refused_by_policy() -> None:
    snapshot = _boost_frame(_task_row('Play & Earn', 100, subtitle='Win rewards'))

    roster = enumerate_roster(snapshot, load_task_catalogue(CATALOGUE_PATH))

    assert roster.titles == ('Play & Earn',)
    entry = roster.entries[0]
    assert entry.denied
    assert entry.rule == 'game_play_and_earn'
    assert entry.dispatch_target is None
    assert roster.split_failures == ()


def test_a_legitimate_subtitle_that_mentions_a_tile_is_not_a_split_failure() -> None:
    snapshot = _boost_frame(
        _task_row('Water to win a US $0.10 prize', 100, subtitle='Water your tree in Prize Land'),
    )

    roster = enumerate_roster(snapshot, load_task_catalogue(CATALOGUE_PATH))

    assert roster.titles == ('Water to win a US $0.10 prize',)
    assert roster.split_failures == ()
    assert roster.incomplete is False


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
    assert roster.visible_action_count == 2
    assert roster.unassigned_action_count == 1
    assert roster.incomplete


def test_a_known_title_in_another_rows_copy_is_split_back_apart_and_reported() -> None:
    snapshot = _snapshot(
        _box('Daily coins', 80, 1245, 500, 1290),
        _box('Worth C$0.01', 80, 1248, 400, 1292),
        _box('Search for what you love', 80, 1250, 700, 1295),
        _box('0/2', 900, 1235, 1000, 1280),
        _box('GO', 980, 1270, 1040, 1310),
    )

    roster = enumerate_roster(snapshot, load_task_catalogue(CATALOGUE_PATH))

    assert roster.titles == ('Daily coins', 'Search for what you love')
    assert roster.entries[0].subtitle == 'Worth C$0.01'
    assert roster.entries[1].progress_text == '0/2'
    assert roster.entries[1].action is not None
    assert len(roster.split_failures) == 1
    failure = roster.split_failures[0]
    assert failure.title == 'Search for what you love'
    assert failure.container_title == 'Daily coins'
    assert failure.recovered
    assert roster.unassigned_action_count == 0
    assert roster.incomplete is False
    assert roster.warning == '1 split failure(s) were recovered'


def test_a_known_title_merged_into_one_ocr_box_leaves_the_action_unassigned() -> None:
    snapshot = _snapshot(
        _box('Worth C$0.01 Search for what you love', 80, 1245, 800, 1295),
        _box('GO', 980, 1270, 1040, 1310),
    )

    roster = enumerate_roster(snapshot, load_task_catalogue(CATALOGUE_PATH))

    assert roster.count == 1
    assert roster.entries[0].action is None
    assert roster.entries[0].dispatch_target is None
    assert len(roster.split_failures) == 1
    assert roster.split_failures[0].title == 'Search for what you love'
    assert roster.split_failures[0].recovered is False
    assert roster.visible_action_count == 1
    assert roster.unassigned_action_count == 1
    assert roster.incomplete
    assert 'not enumerated' in (roster.warning or '')


def test_a_task_title_merged_into_a_subtitle_box_is_an_unrecovered_split_failure() -> None:
    snapshot = _snapshot(
        _box('Daily coins', 80, 1200, 500, 1240),
        _box('Worth C$0.01 Search for what you love', 80, 1250, 800, 1295),
        _box('GO', 980, 1270, 1040, 1310),
    )

    roster = enumerate_roster(snapshot, load_task_catalogue(CATALOGUE_PATH))

    assert roster.titles == ('Daily coins',)
    entry = roster.entries[0]
    assert entry.action is None
    assert entry.dispatch_target is None
    assert len(roster.split_failures) == 1
    failure = roster.split_failures[0]
    assert failure.title == 'Search for what you love'
    assert failure.container_title == 'Daily coins'
    assert failure.recovered is False
    assert roster.visible_action_count == 1
    assert roster.unassigned_action_count == 1
    assert roster.incomplete
    assert 'not enumerated' in (roster.warning or '')
    assert 'known task title' in (roster.warning or '')


def test_a_clean_two_row_chain_is_not_a_split_failure() -> None:
    snapshot = _snapshot(
        _box('Daily check-in', 80, 100, 700, 140),
        _box('GO', 900, 131, 960, 161),
        _box('Take the coins quiz', 80, 180, 700, 220),
        _box('GO', 1000, 211, 1060, 241),
    )

    roster = enumerate_roster(snapshot, load_task_catalogue(CATALOGUE_PATH))

    assert roster.titles == ('Daily check-in', 'Take the coins quiz')
    assert roster.split_failures == ()
    assert roster.unassigned_action_count == 0
    assert roster.incomplete is False
    assert roster.warning is None


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
    assert payload['visible_action_count'] == 4
    assert payload['unassigned_action_count'] == 0
    assert payload['incomplete'] is False
    assert payload['warning'] is None
    assert payload['split_failures'] == []
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
        'Daily coins',
        'Daily check-in',
        'Explore sponsored items',
        'Browse surprise items',
    )
    assert roster.count == 4
    assert roster.as_dict()['route'] == 'inline'

    opened_payload = opened.as_dict()
    assert opened_payload['route'] == 'inline'
    assert 'tap' not in opened_payload
    assert 'dismiss' not in opened_payload


def test_a_claimed_card_without_any_task_action_is_not_a_usable_roster() -> None:
    snapshot = _snapshot(
        _box('Daily coins', 100, 500, 500, 540),
        _box('Worth C$0.01', 100, 600, 400, 640),
    )

    roster = enumerate_roster(snapshot, load_task_catalogue(CATALOGUE_PATH))

    assert roster.empty
    assert roster.visible_action_count == 0
    assert roster.incomplete is False


def test_a_known_title_with_different_case_is_not_a_split_failure() -> None:
    snapshot = _boost_frame(_task_row('search for what you love', 100, badge='0/2'))

    roster = enumerate_roster(snapshot, load_task_catalogue(CATALOGUE_PATH))

    assert roster.titles == ('search for what you love',)
    assert roster.split_failures == ()
    assert roster.incomplete is False


def test_a_row_with_no_title_is_not_guessed() -> None:
    roster = enumerate_roster(
        _snapshot(_box('GO', 980, 100, 1040, 130)),
        load_task_catalogue(CATALOGUE_PATH),
    )

    assert roster.count == 0
    assert roster.nothing_available
    assert roster.visible_action_count == 1
    assert roster.unassigned_action_count == 1
    assert roster.incomplete
    assert roster.warning == '1 visible GO button(s) but 1 task action(s) were not enumerated'


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


@pytest.mark.slow
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


def test_wait_for_coin_page_reads_through_the_skeleton_to_the_page(
    frames: dict[str, Snapshot],
) -> None:
    clock = _SteppingClock(step=0.5)
    capture = _Capture((frames['skeleton'], frames['coins']))

    arrival = wait_for_coin_page(
        capture,
        timeout=5,
        interval=0.5,
        clock=clock,
        sleep=clock.sleep,
    )

    assert arrival.settled is True
    assert arrival.timed_out is False
    assert arrival.classification.state is ScreenState.PAGE
    assert arrival.history[0] is ScreenState.LOADING
    assert arrival.history[-1] is ScreenState.PAGE


def test_wait_for_coin_page_waits_out_an_unknown_pre_render_frame(
    frames: dict[str, Snapshot],
    engine: RapidOcrEngine,
) -> None:
    ambiguous = _load(STATES / 'ambiguous_feed.png', engine)
    assert classify(ambiguous).state is ScreenState.UNKNOWN
    clock = _SteppingClock(step=0.5)
    capture = _Capture((ambiguous, frames['coins']))

    arrival = wait_for_coin_page(
        capture,
        timeout=5,
        interval=0.5,
        clock=clock,
        sleep=clock.sleep,
    )

    assert arrival.settled is True
    assert arrival.timed_out is False
    assert arrival.classification.state is ScreenState.PAGE
    assert arrival.history[0] is ScreenState.UNKNOWN
    assert arrival.history[-1] is ScreenState.PAGE


def test_wait_for_coin_page_names_a_skeleton_that_never_renders(
    frames: dict[str, Snapshot],
) -> None:
    clock = _SteppingClock(step=0.5)
    capture = _Capture((frames['skeleton'],))

    arrival = wait_for_coin_page(
        capture,
        timeout=2,
        interval=0.5,
        clock=clock,
        sleep=clock.sleep,
    )

    assert arrival.settled is False
    assert arrival.timed_out is True
    assert arrival.classification.state is ScreenState.LOADING
    assert set(arrival.history) == {ScreenState.LOADING}


def test_wait_for_coin_page_returns_a_popup_without_dismissing_it_for_a_dry_run(
    frames: dict[str, Snapshot],
) -> None:
    overlay = _rebox(frames['launch'], (*frames['coins'].boxes, *frames['launch'].boxes))
    clock = _SteppingClock()
    capture = _Capture((overlay, frames['coins']))

    arrival = wait_for_coin_page(
        capture,
        timeout=5,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert arrival.settled is False
    assert arrival.timed_out is False
    assert arrival.classification.state is ScreenState.POPUP
    assert arrival.history == (ScreenState.POPUP,)


@pytest.mark.parametrize(('timeout', 'interval'), [(0, 1), (1, 0)])
def test_wait_for_coin_page_rejects_a_non_positive_poll_budget(
    frames: dict[str, Snapshot],
    timeout: float,
    interval: float,
) -> None:
    with pytest.raises(ValueError, match='positive'):
        wait_for_coin_page(
            _Capture((frames['coins'],)),
            timeout=timeout,
            interval=interval,
        )


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


@pytest.mark.slow
def test_open_boost_list_sweeps_an_initial_popup_before_reading_the_coin_page(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
) -> None:
    overlay = _rebox(frames['launch'], (*frames['coins'].boxes, *frames['launch'].boxes))
    assert classify(overlay).state is ScreenState.POPUP
    assert find_text(overlay, EARN_MORE_LABEL) is not None

    device = _RecordingDevice()
    controller = _controller(device, overlay)
    clock = _SteppingClock()
    capture = _Capture((overlay, overlay, frames['coins'], frames['coins'], frames['state_boost']))
    dismissals: list[DismissReport] = []

    opened = open_boost_list(
        controller,
        capture,
        corpus,
        timeout=10,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
        on_dismiss=dismissals.append,
    )

    assert opened.route is DiscoveryRoute.BOOST
    assert opened.dismiss is not None
    assert opened.dismiss.closed == ('launch_welcome_gifts',)
    assert [report.closed for report in dismissals] == [('launch_welcome_gifts',)]
    assert opened.tap is not None
    assert len(device.taps) == 2


@pytest.mark.slow
def test_open_boost_list_does_not_tap_through_a_stuck_initial_popup(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
) -> None:
    overlay = _rebox(frames['launch'], (*frames['coins'].boxes, *frames['launch'].boxes))
    earn_more = find_text(overlay, EARN_MORE_LABEL)
    assert earn_more is not None

    device = _RecordingDevice()
    controller = _controller(device, overlay)
    clock = _SteppingClock()
    dismissals: list[DismissReport] = []

    with pytest.raises(StateTimeout, match='initial popup'):
        open_boost_list(
            controller,
            _Capture((overlay,)),
            corpus,
            timeout=2,
            interval=1,
            clock=clock,
            sleep=clock.sleep,
            on_dismiss=dismissals.append,
        )

    assert len(dismissals) == 1
    assert dismissals[0].closed == ()
    assert device.taps
    assert controller.tap_count == len(device.taps)
    assert all(abs(x - earn_more.center[0]) > 100 or abs(y - earn_more.center[1]) > 100 for x, y in device.taps)


def test_open_boost_list_accepts_a_popup_classified_destination_without_another_family(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    catalogue: TaskCatalogue,
) -> None:
    destination = _rebox(frames['launch'], frames['state_boost'].boxes)
    assert classify(destination).state is ScreenState.POPUP

    device = _RecordingDevice()
    controller = _controller(device, frames['coins'])
    clock = _SteppingClock()
    capture = _Capture((frames['coins'], destination))

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
    assert opened.dismiss is None
    assert opened.classification is not None
    assert opened.classification.state is ScreenState.POPUP
    assert len(device.taps) == 1
    assert enumerate_roster(opened.snapshot, catalogue).count == 4


@pytest.mark.slow
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


def test_the_recorded_boost_page_is_recognised_as_the_destination(
    frames: dict[str, Snapshot],
    catalogue: TaskCatalogue,
) -> None:
    """The 2026-11-23 nightly frame: real marker, real Daily check-in row (#84).

    The committed older boost fixtures start with the quiz, so the marker and
    the first-row control were never proven against the page the run actually
    opens. This frame records ``Coins boost`` over ``Daily check-in`` with its
    own ``GO`` and must classify without any hand-built boxes.
    """
    snapshot = frames['real_boost']

    classification = classify(snapshot)
    assert classification.state is ScreenState.PAGE
    assert 'boost page' in classification.describe()
    assert classify_surface(snapshot) is Surface.BOOST_PAGE
    assert is_boost_page(snapshot)
    assert has_task_rows(snapshot)

    roster = enumerate_roster(snapshot, catalogue, route=DiscoveryRoute.BOOST)
    assert roster.route is DiscoveryRoute.BOOST
    assert roster.titles[0] == 'Daily check-in'
    assert roster.entries[0].key == 'boost_checkin'
    assert roster.entries[0].action is not None
    assert roster.entries[0].dispatchable


def test_open_boost_list_accepts_an_arrival_that_crosses_the_deadline(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
) -> None:
    """The destination on screen ends the wait even on the deadline frame.

    This is the #84 false timeout in miniature: the old loop reached the clock
    check on the first boost frame, saw ``now >= deadline`` and raised while
    naming the boost page as its "last" frame. A page that is visible is not a
    page that was not observed.
    """
    device = _RecordingDevice()
    controller = _controller(device, frames['coins'])
    clock = _SteppingClock()
    capture = _Capture((frames['coins'], frames['real_boost']))

    opened = open_boost_list(
        controller,
        capture,
        corpus,
        timeout=1,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert opened.route is DiscoveryRoute.BOOST
    assert opened.snapshot is frames['real_boost']
    assert opened.history == (ScreenState.PAGE,)
    assert len(device.taps) == 1


def test_open_boost_list_retaps_once_when_the_first_tap_had_no_effect(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A swallowed tap leaves the coin page identical; one bounded retry is allowed.

    The measured cold-start failure was the app ignoring the tap: the frame
    stayed byte-identical to the pre-tap coin page. The unchanged, stable frame
    is evidence that nothing happened, so the tap is repeated once. The retry
    and its frame difference are logged.
    """
    device = _RecordingDevice()
    controller = _controller(device, frames['coins'])
    clock = _SteppingClock()
    capture = _Capture((frames['coins'], frames['coins'], frames['coins'], frames['coins'], frames['real_boost']))
    logger = logging.getLogger('test.boost_retap')

    with caplog.at_level(logging.WARNING, logger='test.boost_retap'):
        opened = open_boost_list(
            controller,
            capture,
            corpus,
            timeout=6,
            interval=1,
            clock=clock,
            sleep=clock.sleep,
            logger=logger,
        )

    assert opened.route is DiscoveryRoute.BOOST
    assert opened.tap_attempts == 2
    assert opened.retap is not None
    assert opened.retap_difference is not None
    assert opened.retap_difference <= 0.01
    assert len(device.taps) == 2
    assert opened.as_dict()['tap_attempts'] == 2
    events = [
        cast('dict[str, object]', json.loads(record.message))
        for record in caplog.records
        if record.message.startswith('{')
    ]
    retap = next(event for event in events if event.get('event') == 'boost_retap')
    assert retap['attempt'] == 2
    assert cast('float', retap['difference']) == 0.0
    assert cast('float', retap['fresh_difference']) == 0.0
    assert retap['state'] == 'page'


def test_open_boost_list_tolerates_capture_noise_before_retapping(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
) -> None:
    """Two independent captures that differ only by sensor noise are still "unchanged"."""
    initial = frames['coins']
    noisy_a = _with_image_change(initial, (0, 0, 12, 12), (140, 140, 140))
    noisy_b = _with_image_change(initial, (0, 0, 12, 9), (150, 150, 150))
    device = _RecordingDevice()
    controller = _controller(device, initial)
    clock = _SteppingClock()

    opened = open_boost_list(
        controller,
        _Capture((initial, noisy_a, noisy_b, noisy_a, frames['real_boost'])),
        corpus,
        timeout=6,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert opened.route is DiscoveryRoute.BOOST
    assert opened.tap_attempts == 2
    assert opened.retap_difference is not None
    assert opened.retap_difference <= 0.01
    assert len(device.taps) == 2


def test_open_boost_list_does_not_retap_when_the_fresh_frame_is_the_destination(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
) -> None:
    """A recheck that sees the boost page (with Earn more still OCR-visible) is the destination."""
    destination = _rebox(
        frames['coins'],
        (*frames['real_boost'].boxes, _box('Earn more >', 916, 926, 1133, 967)),
    )
    assert is_boost_page(destination)
    device = _RecordingDevice()
    controller = _controller(device, frames['coins'])
    clock = _SteppingClock()

    opened = open_boost_list(
        controller,
        _Capture((frames['coins'], frames['coins'], frames['coins'], destination)),
        corpus,
        timeout=10,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert opened.route is DiscoveryRoute.BOOST
    assert opened.snapshot is destination
    assert opened.tap_attempts == 1
    assert len(device.taps) == 1


def test_open_boost_list_does_not_retap_a_changing_page(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
) -> None:
    """A visibly changing coin page (the sheet mid-render) is not tapped twice."""
    initial = frames['coins']
    changed = _with_image_change(initial, (100, 600, 700, 1200), (0, 0, 0))
    device = _RecordingDevice()
    controller = _controller(device, initial)
    clock = _SteppingClock()

    opened = open_boost_list(
        controller,
        _Capture((initial, changed, changed, changed, frames['real_boost'])),
        corpus,
        timeout=6,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert opened.route is DiscoveryRoute.BOOST
    assert opened.tap_attempts == 1
    assert len(device.taps) == 1


def test_open_boost_list_skips_the_retap_when_the_fresh_frame_changed(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
) -> None:
    """The pre-tap re-capture is the safety check: a changed fresh frame cancels the retry."""
    initial = frames['coins']
    changed = _with_image_change(initial, (100, 600, 700, 1200), (0, 0, 0))
    device = _RecordingDevice()
    controller = _controller(device, initial)
    clock = _SteppingClock()

    opened = open_boost_list(
        controller,
        _Capture((initial, initial, initial, changed, initial, frames['real_boost'])),
        corpus,
        timeout=10,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert opened.route is DiscoveryRoute.BOOST
    assert opened.tap_attempts == 1
    assert len(device.taps) == 1


def test_open_boost_list_requires_consecutive_unchanged_polls(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
) -> None:
    """A loading frame between two coin-page polls breaks the consecutive evidence."""
    initial = frames['coins']
    device = _RecordingDevice()
    controller = _controller(device, initial)
    clock = _SteppingClock()

    opened = open_boost_list(
        controller,
        _Capture((initial, initial, frames['skeleton'], initial, initial, frames['real_boost'])),
        corpus,
        timeout=10,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert opened.route is DiscoveryRoute.BOOST
    assert opened.tap_attempts == 1
    assert len(device.taps) == 1


def test_open_boost_list_does_not_retap_more_than_once(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
) -> None:
    device = _RecordingDevice()
    controller = _controller(device, frames['coins'])
    clock = _SteppingClock()

    with pytest.raises(StateTimeout, match='taps=2'):
        open_boost_list(
            controller,
            _Capture((frames['coins'],)),
            corpus,
            timeout=6,
            interval=1,
            clock=clock,
            sleep=clock.sleep,
        )

    assert len(device.taps) == 2


def test_open_boost_list_does_not_retap_without_a_fresh_label(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
) -> None:
    """An unchanged frame whose OCR lost the label is not a licence to tap blind."""
    initial = frames['coins']
    unreadable = _rebox(initial, tuple(box for box in initial.boxes if box.text != 'Earn more >'))
    device = _RecordingDevice()
    controller = _controller(device, initial)
    clock = _SteppingClock()

    opened = open_boost_list(
        controller,
        _Capture((initial, unreadable, unreadable, frames['real_boost'])),
        corpus,
        timeout=6,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert opened.route is DiscoveryRoute.BOOST
    assert opened.tap_attempts == 1
    assert len(device.taps) == 1


def test_open_boost_list_reports_a_popup_that_never_clears(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
) -> None:
    """A modal that stays after the tap is a timeout with the attempt count in the log."""
    device = _RecordingDevice()
    controller = _controller(device, frames['coins'])
    clock = _SteppingClock()

    with pytest.raises(StateTimeout, match='taps=1') as raised:
        open_boost_list(
            controller,
            _Capture((frames['coins'], frames['launch'], frames['launch'])),
            corpus,
            timeout=2,
            interval=1,
            clock=clock,
            sleep=clock.sleep,
        )

    assert 'last=popup' in str(raised.value)
    assert len(device.taps) >= 1


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


def _task_row(
    title: str,
    top: int,
    *,
    action: str = 'GO',
    badge: str | None = None,
    subtitle: str | None = None,
) -> tuple[TextBox, ...]:
    """One measured-shape task row: title left, action right, badge between."""
    boxes = [
        _box(title, 80, top, 700, top + 40),
        _box(action, 980, top + 31, 1040, top + 61),
    ]
    if badge is not None:
        boxes.append(_box(badge, 900, top, 1000, top + 30))
    if subtitle is not None:
        boxes.append(_box(subtitle, 80, top + 50, 700, top + 80))
    return tuple(boxes)


def _boost_frame(*rows: tuple[TextBox, ...]) -> Snapshot:
    """A synthetic boost page: header plus the supplied task rows."""
    boxes = (_box('Coins boost', 100, 10, 500, 40), *[box for row in rows for box in row])
    return _snapshot(*boxes)


def _observe(
    catalogue: TaskCatalogue,
    frame: Snapshot,
    *,
    previous: Roster | None = None,
    action_labels: Sequence[str] = ('GO',),
) -> Any:
    return observe_roster(
        frame,
        catalogue,
        previous=previous,
        sequence=1,
        timestamp=CAPTURED_AT,
        action_labels=action_labels,
    )


def test_an_observation_logs_a_task_that_appears_after_a_completion(
    tmp_path: Path,
    catalogue: TaskCatalogue,
) -> None:
    before = _boost_frame(
        _task_row('Daily check-in', 100),
        _task_row('Explore sponsored items', 300, badge='0/6'),
    )
    after = _boost_frame(
        _task_row('Take the coins quiz', 100),
        _task_row('Daily check-in', 300),
        _task_row('Explore sponsored items', 500, badge='0/6'),
    )
    log = RosterLog.for_run(tmp_path, 'run-1')
    observer = RosterObserver(catalogue, log=log, clock=lambda: CAPTURED_AT)

    first = observer.observe(_Capture((before,)))
    second = observer.observe(lambda: after)

    assert [row.label for row in first.rows] == ['Daily check-in', 'Explore sponsored items']
    assert [row.label for row in first.diff.added] == ['Daily check-in', 'Explore sponsored items']
    assert [row.label for row in second.diff.added] == ['Take the coins quiz']
    assert second.diff.removed == ()
    assert [row.label for row in second.rows] == [
        'Take the coins quiz',
        'Daily check-in',
        'Explore sponsored items',
    ]
    assert observer.sequence == 2
    assert observer.roster is second.roster

    records = read_roster_log(log.path)
    assert [record['observation_seq'] for record in records] == [1, 2]
    assert len(cast('list[object]', records[0]['rows'])) == 2
    assert len(cast('list[object]', records[1]['rows'])) == 3
    assert records[1]['surface_variant'] == 'boost_page'
    assert [row['label'] for row in cast('list[dict[str, object]]', records[1]['diff']['added'])] == [
        'Take the coins quiz'
    ]


def test_a_removed_row_is_logged_with_its_label_and_previous_progress(
    catalogue: TaskCatalogue,
) -> None:
    before = _boost_frame(
        _task_row('Daily check-in', 100),
        _task_row('Explore sponsored items', 300, badge='0/6'),
        _task_row('Browse surprise items', 500, badge='0/2'),
    )
    after = _boost_frame(
        _task_row('Daily check-in', 100),
        _task_row('Explore sponsored items', 300, badge='2/6'),
    )

    first = _observe(catalogue, before)
    second = _observe(catalogue, after, previous=first.roster)

    assert [row.label for row in second.diff.removed] == ['Browse surprise items']
    assert second.diff.removed[0].badge == '0/2'
    assert second.diff.added == ()
    assert len(second.diff.progress_moved) == 1
    moved = second.diff.progress_moved[0]
    assert (moved.key, moved.label, moved.before, moved.after) == (
        'sponsored_items',
        'Explore sponsored items',
        '0/6',
        '2/6',
    )
    changes = cast('list[dict[str, object]]', second.diff.as_dict()['progress_moved'])
    assert changes[0] == {
        'key': 'sponsored_items',
        'label': 'Explore sponsored items',
        'before': '0/6',
        'after': '2/6',
    }
    assert second.diff.changed


def test_an_action_change_is_logged_with_before_and_after(
    catalogue: TaskCatalogue,
) -> None:
    before = _boost_frame(
        _task_row('Daily check-in', 100, action='GO'),
        _task_row('Explore sponsored items', 300, badge='0/6'),
    )
    after = _boost_frame(
        _task_row('Daily check-in', 100, action='Claimed'),
        _task_row('Explore sponsored items', 300, badge='0/6'),
    )
    labels = ('GO', 'Claimed')

    first = _observe(catalogue, before, action_labels=labels)
    second = _observe(catalogue, after, previous=first.roster, action_labels=labels)

    assert second.diff.added == ()
    assert second.diff.removed == ()
    assert second.diff.progress_moved == ()
    changed = second.diff.action_changed[0]
    assert (changed.key, changed.label, changed.before, changed.after) == (
        'boost_checkin',
        'Daily check-in',
        'GO',
        'Claimed',
    )
    assert second.rows[0].action == 'Claimed'


def test_ocr_spacing_and_case_are_not_progress_or_action_changes(
    catalogue: TaskCatalogue,
) -> None:
    before = _boost_frame(
        _task_row('Daily check-in', 100),
        _task_row('Explore sponsored items', 300, badge='0 / 6', action='go'),
    )
    after = _boost_frame(
        _task_row('Daily check-in', 100),
        _task_row('Explore sponsored items', 300, badge='0/6', action='GO'),
    )

    first = _observe(catalogue, before)
    second = _observe(catalogue, after, previous=first.roster)

    assert second.diff.progress_moved == ()
    assert second.diff.action_changed == ()
    assert second.rows[1].badge == '0/6'
    assert second.rows[1].action == 'GO'


def test_an_unknown_title_is_a_candidate_that_is_never_dispatchable(
    catalogue: TaskCatalogue,
) -> None:
    unknown = _boost_frame(
        _task_row('Daily check-in', 100),
        _task_row('Invent a new task', 300, badge='0/6', subtitle='Try the mystery box'),
    )
    observer = RosterObserver(catalogue, clock=lambda: CAPTURED_AT)

    first = observer.observe(unknown)
    second = observer.observe(unknown)

    candidate = next(row for row in first.rows if row.label == 'Invent a new task')
    assert candidate.key is None
    assert candidate.subtitle == 'Try the mystery box'
    assert candidate.badge == '0/6'
    assert candidate.decision is DecisionKind.DENY
    assert candidate.reason == 'unrecognised'
    assert candidate.candidate is True
    assert candidate.action == 'GO'
    assert candidate.dispatch_target is None
    assert [row.label for row in first.diff.candidates] == ['Invent a new task']
    assert [row.label for row in second.diff.candidates] == []
    assert [row.label for row in observer.candidates] == ['Invent a new task']


def test_an_unassigned_go_is_durable_and_logged_as_a_warning(
    caplog: pytest.LogCaptureFixture,
    catalogue: TaskCatalogue,
) -> None:
    frame = _boost_frame(_task_row('Daily check-in', 100), (_box('GO', 980, 500, 1040, 530),))
    logger = logging.getLogger('test.roster.diagnostics')
    observer = RosterObserver(catalogue, clock=lambda: CAPTURED_AT, logger=logger)

    with caplog.at_level(logging.WARNING, logger=logger.name):
        observation = observer.observe(frame)

    assert observation.roster is not None
    assert observation.roster.incomplete
    payload = observation.as_dict()
    diagnostics = cast('dict[str, object]', payload['roster_diagnostics'])
    assert diagnostics['incomplete'] is True
    assert diagnostics['unassigned_action_count'] == 1
    assert 'not enumerated' in str(diagnostics['warning'])
    records = [record for record in caplog.records if record.message.startswith('{')]
    assert records[-1].levelno == logging.WARNING
    event = cast('dict[str, object]', json.loads(records[-1].message))
    assert event['event'] == 'roster_observation'
    assert cast('dict[str, object]', event['roster_diagnostics'])['incomplete'] is True


def test_a_failed_observation_keeps_the_previous_roster_and_the_walk_continues(
    tmp_path: Path,
    frames: dict[str, Snapshot],
    catalogue: TaskCatalogue,
) -> None:
    good = _boost_frame(_task_row('Daily check-in', 100), _task_row('Explore sponsored items', 300, badge='0/6'))
    log = RosterLog.for_run(tmp_path, 'run-1')
    observer = RosterObserver(catalogue, log=log, clock=lambda: CAPTURED_AT)
    first = observer.observe(good)

    failed = observer.observe(lambda: (_ for _ in ()).throw(SnapshotError('capture died')))

    assert failed.failed
    assert failed.error is not None
    assert 'capture died' in failed.error
    assert [row.label for row in failed.rows] == [row.label for row in first.rows]
    assert failed.diff.changed is False
    assert observer.roster is first.roster
    assert observer.sequence == 2

    records = read_roster_log(log.path)
    assert len(records) == 2
    assert records[1]['error'] is not None
    assert [row['label'] for row in cast('list[dict[str, object]]', records[1]['rows'])] == [
        'Daily check-in',
        'Explore sponsored items',
    ]


def test_an_unexpected_observation_error_is_not_hidden(
    catalogue: TaskCatalogue,
) -> None:
    with pytest.raises(RuntimeError, match='programming bug'):
        observe_roster(
            lambda: (_ for _ in ()).throw(RuntimeError('programming bug')),
            catalogue,
            timestamp=CAPTURED_AT,
        )


def test_an_empty_boost_observation_keeps_the_previous_roster(
    frames: dict[str, Snapshot],
    catalogue: TaskCatalogue,
) -> None:
    good = _boost_frame(_task_row('Daily check-in', 100), _task_row('Explore sponsored items', 300, badge='0/6'))
    observer = RosterObserver(catalogue, clock=lambda: CAPTURED_AT)
    first = observer.observe(good)

    failed = observer.observe(_empty_boost(frames['state_boost']))

    assert failed.failed
    assert failed.error is not None
    assert failed.rows == first.rows
    assert failed.diff.changed is False
    assert observer.roster is first.roster


def test_a_changed_surface_does_not_erase_the_boost_roster(
    frames: dict[str, Snapshot],
    catalogue: TaskCatalogue,
) -> None:
    good = _boost_frame(_task_row('Daily check-in', 100), _task_row('Explore sponsored items', 300, badge='0/6'))
    observer = RosterObserver(catalogue, clock=lambda: CAPTURED_AT)
    first = observer.observe(good)

    failed = observer.observe(frames['coins'])

    assert failed.failed
    assert failed.error is not None
    assert 'surface changed' in failed.error
    assert failed.rows == first.rows
    assert observer.roster is first.roster


def test_an_empty_boost_seed_can_recover_to_a_populated_boost(
    catalogue: TaskCatalogue,
) -> None:
    observer = RosterObserver(
        catalogue,
        previous=Roster(entries=(), surface_variant=Surface.EMPTY_BOOST),
        clock=lambda: CAPTURED_AT,
    )

    observation = observer.observe(
        _boost_frame(
            _task_row('Daily check-in', 100),
            _task_row('Explore sponsored items', 300, badge='0/6'),
        ),
    )

    assert observation.failed is False
    assert observation.surface_variant is Surface.BOOST_PAGE
    assert [row.label for row in observation.diff.added] == ['Daily check-in', 'Explore sponsored items']


def test_inline_rows_disappearing_are_logged_as_removals(
    frames: dict[str, Snapshot],
    catalogue: TaskCatalogue,
) -> None:
    observer = RosterObserver(catalogue, clock=lambda: CAPTURED_AT)
    first = observer.observe(frames['coins'])
    no_rows = _snapshot(
        _box('Daily coins', 100, 500, 500, 540),
        _box('Coins credits to redeem >', 100, 300, 800, 340),
    )

    second = observer.observe(no_rows)

    assert first.surface_variant is Surface.ROWS_PRESENT
    assert first.rows
    assert second.failed is False
    assert second.surface_variant is Surface.ROWS_ABSENT
    assert second.rows == ()
    assert [row.label for row in second.diff.removed] == [row.label for row in first.rows]


def test_a_candidate_in_the_seeded_previous_roster_is_reported(
    catalogue: TaskCatalogue,
) -> None:
    frame = _boost_frame(
        _task_row('Daily check-in', 100),
        _task_row('Invent a new task', 300, badge='0/6', subtitle='Try the mystery box'),
    )
    initial = _observe(catalogue, frame)
    observer = RosterObserver(catalogue, previous=initial.roster, clock=lambda: CAPTURED_AT)

    first = observer.observe(frame)
    second = observer.observe(frame)

    assert [row.label for row in first.diff.candidates] == ['Invent a new task']
    assert [row.label for row in first.diff.added] == []
    assert [row.label for row in second.diff.candidates] == []


@pytest.mark.parametrize(
    ('frame_name', 'error_part'),
    [
        ('skeleton', 'loading'),
        ('launch', 'popup'),
    ],
)
def test_an_unreadable_surface_is_recorded_not_raised(
    frames: dict[str, Snapshot],
    catalogue: TaskCatalogue,
    frame_name: str,
    error_part: str,
) -> None:
    observer = RosterObserver(catalogue, clock=lambda: CAPTURED_AT)
    observation = observer.observe(frames[frame_name])

    assert observation.failed
    assert observation.error is not None
    assert error_part in observation.error
    assert observation.rows == ()
    assert observation.roster is None
    assert observer.roster is None
    assert observer.observations == (observation,)


@pytest.mark.parametrize('variant', [Surface.UNKNOWN, Surface.REDEEM_INTERSTITIAL])
def test_an_unknown_or_interstitial_variant_is_a_failed_observation(
    frames: dict[str, Snapshot],
    catalogue: TaskCatalogue,
    variant: Surface,
) -> None:
    observation = observe_roster(
        frames['state_boost'],
        catalogue,
        sequence=1,
        timestamp=CAPTURED_AT,
        surface_variant=variant,
    )

    assert observation.failed
    assert observation.surface_variant is variant
    assert observation.error is not None
    assert 'variant' in observation.error or 'interstitial' in observation.error


def test_the_observation_cap_is_logged_once_and_stops_capture(
    tmp_path: Path,
    catalogue: TaskCatalogue,
) -> None:
    first_frame = _boost_frame(
        _task_row('Daily check-in', 100),
        _task_row('Explore sponsored items', 300, badge='0/6'),
    )
    second_frame = _boost_frame(
        _task_row('Daily check-in', 100),
        _task_row('Take the coins quiz', 300),
        _task_row('Explore sponsored items', 500, badge='0/6'),
    )
    captures: list[Snapshot] = []

    def capture(frame: Snapshot) -> Any:
        def _capture_frame() -> Snapshot:
            captures.append(frame)
            return frame

        return _capture_frame

    observer = RosterObserver(
        catalogue,
        log=RosterLog.for_run(tmp_path, 'run-1'),
        max_observations=1,
        clock=lambda: CAPTURED_AT,
    )

    observer.observe(capture(first_frame))
    capped = observer.observe(capture(second_frame))
    again = observer.observe(capture(second_frame))

    assert capped.cap_reached
    assert capped.failed is False
    assert capped.error is None
    assert [row.label for row in capped.rows] == ['Daily check-in', 'Explore sponsored items']
    assert again is capped
    assert len(captures) == 1
    assert observer.cap_reached

    records = read_roster_log(observer.log.path) if observer.log is not None else ()
    assert [record['observation_seq'] for record in records] == [1, 2]
    assert records[1]['cap_reached'] is True
    assert records[1]['error'] is None


def test_the_log_is_append_only_jsonl_across_restarts(
    tmp_path: Path,
    catalogue: TaskCatalogue,
) -> None:
    path = tmp_path / 'run-1' / ROSTER_LOG_NAME
    path.parent.mkdir(parents=True)
    sentinel = {'schema': ROSTER_OBSERVATION_SCHEMA, 'observation_seq': 0, 'rows': [], 'diff': {}}
    path.write_text(json.dumps(sentinel) + '\n', encoding='utf-8')

    observer = RosterObserver(
        catalogue,
        log=RosterLog.for_run(tmp_path, 'run-1'),
        clock=lambda: CAPTURED_AT,
    )
    observation = observer.observe(
        _boost_frame(
            _task_row('Daily check-in', 100),
            _task_row('Explore sponsored items', 300, badge='0/6'),
        ),
    )

    lines = path.read_text(encoding='utf-8').splitlines()
    assert len(lines) == 2
    assert all(isinstance(json.loads(line), dict) for line in lines)
    assert json.loads(lines[0]) == sentinel
    assert json.loads(lines[1])['observation_seq'] == 1
    assert observation.sequence == 1
    assert read_roster_log(path)[1]['rows'] == [row.as_dict() for row in observation.rows]


@pytest.mark.parametrize('run_id', ['', '.', '..', 'a/b', '..\\escape', '   '])
def test_roster_log_run_ids_cannot_escape_the_runs_directory(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(ValueError, match='single path component'):
        RosterLog.for_run(tmp_path, run_id)


def test_roster_log_reports_an_unreadable_or_invalid_file(tmp_path: Path) -> None:
    path = tmp_path / 'roster.jsonl'
    assert read_roster_log(path) == ()

    path.write_text('{not json}\n', encoding='utf-8')
    with pytest.raises(RosterLogError, match='not valid JSON'):
        read_roster_log(path)

    path.write_text('[1, 2]\n', encoding='utf-8')
    with pytest.raises(RosterLogError, match='not a JSON object'):
        read_roster_log(path)


def test_blank_log_lines_are_ignored_and_an_unreadable_path_is_typed(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'roster.jsonl'
    record = {'observation_seq': 1, 'rows': [], 'diff': {}}
    path.write_text(f'\n{json.dumps(record)}\n\n', encoding='utf-8')
    assert read_roster_log(path) == (record,)

    directory = tmp_path / 'a-directory'
    directory.mkdir()
    with pytest.raises(RosterLogError, match='could not read'):
        read_roster_log(directory)


def test_the_observation_cap_must_be_positive(catalogue: TaskCatalogue) -> None:
    with pytest.raises(ValueError, match='max_observations'):
        RosterObserver(catalogue, max_observations=0)


def test_roster_log_append_failure_is_typed_and_does_not_stop_the_observer(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    caplog: pytest.LogCaptureFixture,
) -> None:
    blocker = tmp_path / 'not-a-directory'
    blocker.write_text('x', encoding='utf-8')
    log = RosterLog(blocker / 'roster.jsonl')
    observation = _observe(catalogue, _boost_frame(_task_row('Daily check-in', 100)))

    with pytest.raises(RosterLogError, match='could not append'):
        log.append(observation)

    observer = RosterObserver(catalogue, log=log, clock=lambda: CAPTURED_AT)
    with caplog.at_level('INFO', logger='aliexpress_coins.tasks.discovery'):
        logged = observer.observe(_boost_frame(_task_row('Daily check-in', 100)))
    assert logged.sequence == 1
    assert any('roster log write failed' in record.message for record in caplog.records)


def test_the_observer_reads_the_run_directory_and_cap_from_the_environment(
    tmp_path: Path,
    catalogue: TaskCatalogue,
) -> None:
    observer = RosterObserver.from_environment(
        catalogue,
        run_id='run-7',
        environment={
            'AECOINS_RUNS_DIR': str(tmp_path / 'artifacts'),
            'AECOINS_MAX_ROSTER_OBSERVATIONS': '3',
        },
        clock=lambda: CAPTURED_AT,
    )
    assert observer.log is not None
    assert observer.log.path == tmp_path / 'artifacts' / 'run-7' / ROSTER_LOG_NAME
    assert observer.max_observations == 3

    from_tasks = RosterObserver.from_environment(
        catalogue,
        run_id='run-8',
        environment={'AECOINS_MAX_TASKS_PER_RUN': '5'},
        clock=lambda: CAPTURED_AT,
    )
    assert from_tasks.max_observations == 6

    default = RosterObserver.from_environment(
        catalogue,
        run_id='run-9',
        environment={},
        clock=lambda: CAPTURED_AT,
    )
    assert default.max_observations == DEFAULT_OBSERVATION_CAP == 13

    unnamed = RosterLog.from_environment({'AECOINS_RUNS_DIR': str(tmp_path)}, run_id='', clock=lambda: CAPTURED_AT)
    assert unnamed.path == tmp_path / '20261121T120000Z' / ROSTER_LOG_NAME

    unnamed_observer = RosterObserver.from_environment(
        catalogue,
        run_id='   ',
        environment={'AECOINS_RUNS_DIR': str(tmp_path)},
        clock=lambda: CAPTURED_AT,
    )
    assert unnamed_observer.log is not None
    assert unnamed_observer.log.path == tmp_path / '20261121T120000Z' / ROSTER_LOG_NAME


@pytest.mark.parametrize(
    ('value', 'message'),
    [('many', 'must be an integer'), ('0', 'must be positive'), ('-2', 'must be positive')],
)
def test_the_environment_cap_must_be_a_positive_integer(
    catalogue: TaskCatalogue,
    value: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        RosterObserver.from_environment(
            catalogue,
            run_id='run-1',
            environment={'AECOINS_MAX_ROSTER_OBSERVATIONS': value},
        )


def test_the_task_cap_error_names_the_task_environment_variable(
    catalogue: TaskCatalogue,
) -> None:
    with pytest.raises(ValueError, match='AECOINS_MAX_TASKS_PER_RUN'):
        RosterObserver.from_environment(
            catalogue,
            run_id='run-1',
            environment={'AECOINS_MAX_TASKS_PER_RUN': 'many'},
        )


def test_observe_roster_rejects_an_impossible_sequence(catalogue: TaskCatalogue) -> None:
    with pytest.raises(ValueError, match='sequence'):
        observe_roster(_boost_frame(_task_row('Daily check-in', 100)), catalogue, sequence=0)


def test_the_observation_schema_names_the_record_and_log(
    catalogue: TaskCatalogue,
) -> None:
    observation = _observe(
        catalogue,
        _boost_frame(
            _task_row('Daily check-in', 100),
            _task_row('Explore sponsored items', 300, badge='0/6'),
        ),
    )
    payload = observation.as_dict()

    assert ROSTER_OBSERVATION_SCHEMA == 1
    assert ROSTER_LOG_NAME == 'roster.jsonl'
    assert payload['schema'] == ROSTER_OBSERVATION_SCHEMA
    assert payload['observation_seq'] == 1
    assert payload['timestamp'] == CAPTURED_AT.isoformat()
    assert payload['error'] is None
    assert payload['cap_reached'] is False
    assert payload['diff']['added'][0]['label'] == 'Daily check-in'
    assert observation.added == observation.diff.added
    assert observation.removed == ()
    assert observation.failed is False
