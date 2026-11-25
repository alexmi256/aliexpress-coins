"""The real-search executor over measured and synthetic frames (issues #108, #142).

The device is simulated at the capture and input seams only. A fake device drives
a small screen graph, captures come from that graph, and time is injected; no
test reaches adb and no test sleeps.

The synthetic landing keeps the measured layout: the account's search history is
above the `Discover more` heading, the search field is the band above that
heading, and the results surface carries the submitted query plus product prices
and `N sold` copy. A category grid is not a search and is never tapped.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from aliexpress_coins.control.controller import Controller
from aliexpress_coins.control.popups import DismissReport, DismissStop, PopupCorpus, load_popup_corpus
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.matching import normalize
from aliexpress_coins.perception.ocr import RapidOcrEngine
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.runner.errors import RunInterrupted
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks.catalogue import TaskCatalogue, load_task_catalogue
from aliexpress_coins.tasks.discovery import BoostList, DiscoveryRoute, RosterEntry, enumerate_roster
from aliexpress_coins.tasks.dispatch import DispatchGate
from aliexpress_coins.tasks.errors import TaskError
from aliexpress_coins.tasks.search import (
    SEARCH_QUERIES,
    SearchOutcome,
    find_search_field,
    run_search_task,
)
from aliexpress_coins.tasks.surface import SurfaceRoute, is_search_results

ROOT = Path(__file__).resolve().parents[1]
STATES = ROOT / 'tests' / 'data' / 'states'
CORPUS_PATH = ROOT / 'specs' / 'popups.yaml'
CATALOGUE_PATH = ROOT / 'specs' / 'tasks.yaml'
CAPTURED_AT = datetime(2026, 11, 24, 6, 50, tzinfo=UTC)
SEARCH_TITLE = 'Search for what you love'


class _RecordingDevice:
    """The input seam: record taps, keys and text, and let the world react."""

    def __init__(self, world: _World) -> None:
        self._world = world

    def tap(self, x: int, y: int) -> None:
        self._world.tap(x, y)

    def keyevent(self, key: str) -> None:
        self._world.keyevent(key)

    def text(self, value: str) -> None:
        self._world.text(value)


class _ForbiddenCapture:
    """A capture that proves state or validation stopped the executor first."""

    def __init__(self) -> None:
        self.calls = 0

    def capture(self) -> Snapshot:
        self.calls += 1
        raise AssertionError('the executor must not capture on this path')


class _ForbiddenNavigate:
    """A route firer that must not be used on a no-work path."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _route: SurfaceRoute) -> None:
        self.calls += 1
        raise AssertionError('this path must not navigate')


class _SteppingClock:
    """An injected clock that advances on every read and sleep."""

    def __init__(self, step: float = 0.5) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        current = self.value
        self.value += self.step
        return current

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class _World:
    """A tiny screen graph driven by taps and the shortcut route."""

    def __init__(
        self,
        series: dict[str, Sequence[Snapshot]],
        transitions: dict[tuple[str, str | None], str],
        *,
        start: str | None = None,
        on_tap: Callable[[str, str | None, int], str | None] | None = None,
        on_key: Callable[[str, str, int], str | None] | None = None,
        on_navigate: Callable[[SurfaceRoute, int], str] | None = None,
        navigate_to: str = 'coins',
    ) -> None:
        if start is None:
            start = next(iter(series), '')
        if start not in series:
            raise ValueError(f'start screen {start!r} has no frames')
        self._series = {name: tuple(frames) for name, frames in series.items()}
        self._index = dict.fromkeys(series, 0)
        self._transitions = transitions
        self._on_tap = on_tap
        self._on_key = on_key
        self._on_navigate = on_navigate
        self.navigate_to = navigate_to
        self.current = start
        self.taps: list[tuple[int, int]] = []
        self.keys: list[str] = []
        self.texts: list[str] = []
        self.navigations: list[SurfaceRoute] = []
        self.captures = 0

    @property
    def frame(self) -> Snapshot:
        return self._series[self.current][0]

    @property
    def current_frame(self) -> Snapshot:
        frames = self._series[self.current]
        index = self._index[self.current]
        return frames[max(0, min(index - 1, len(frames) - 1))]

    def capture(self) -> Snapshot:
        self.captures += 1
        frames = self._series[self.current]
        index = min(self._index[self.current], len(frames) - 1)
        self._index[self.current] = index + 1
        return frames[index]

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))
        label = _label_at(self.current_frame, x, y)
        target = None
        if self._on_tap is not None:
            target = self._on_tap(self.current, label, len(self.taps))
        if target is None:
            target = self._transitions.get((self.current, label))
        if target is not None:
            self.go(target)

    def keyevent(self, key: str) -> None:
        self.keys.append(key)
        target = self._on_key(self.current, key, len(self.keys)) if self._on_key is not None else None
        if target is None and key == 'KEYCODE_ENTER' and self.current == 'landing' and 'results' in self._series:
            target = 'results'
        if target is not None:
            self.go(target)

    def text(self, value: str) -> None:
        self.texts.append(value)

    def navigate(self, route: SurfaceRoute) -> None:
        self.navigations.append(route)
        target = self._on_navigate(route, len(self.navigations)) if self._on_navigate is not None else self.navigate_to
        self.go(target)

    def go(self, screen: str) -> None:
        if screen not in self._series:
            raise AssertionError(f'unknown screen {screen!r}')
        self.current = screen
        self._index[screen] = 0


def _box(text: str, left: float, top: float, right: float, bottom: float, score: float = 0.99) -> TextBox:
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


def _load_snapshot(path: Path) -> Snapshot:
    engine = RapidOcrEngine()
    with Image.open(path) as opened:
        image = opened.convert('RGB')
    return Snapshot(
        image=image,
        boxes=engine.read(image),
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(image.width, image.height, density=240, sdk=35),
    )


def _label_at(snapshot: Snapshot, x: float, y: float) -> str | None:
    best: TextBox | None = None
    for box in snapshot.boxes:
        left, top, right, bottom = box.bbox
        if not (left <= x <= right and top <= y <= bottom):
            continue
        if best is None or box.width * box.height < best.width * best.height:
            best = box
    return None if best is None else normalize(best.text)


def _boost_frame(*, badge: str | None = '0/2', with_row: bool = True) -> Snapshot:
    boxes = [
        _box('Coins boost', 80, 90, 400, 150),
        _box('Check in to win Shopping credits', 60, 1200, 700, 1250),
        _box("Don't forget to check in today!", 60, 1260, 700, 1300),
        _box('GO', 1000, 1200, 1080, 1250),
    ]
    if with_row:
        boxes.extend(
            [
                _box(SEARCH_TITLE, 60, 420, 700, 470),
                _box('Earn by using keywords to explore items', 60, 480, 700, 530),
                *([] if badge is None else [_box(badge, 1000, 480, 1080, 530)]),
                _box('GO', 1000, 540, 1080, 590),
            ],
        )
    return _snapshot(*boxes)


def _landing_frame(*, categories: tuple[tuple[str, str], ...] | None = None) -> Snapshot:
    if categories is None:
        categories = (('Sneakers', '74k+ items'), ('Woman Clothing', '77k+ items'))
    boxes = [
        _box('Search', 200, 180, 900, 270),
        _box('Submit', 950, 180, 1150, 270),
        _box('Search history', 60, 320, 400, 370),
        _box('Man Shoes', 60, 450, 280, 500),
        _box('Discover more', 60, 660, 400, 710),
        _box('Phone Promo', 60, 1150, 500, 1200),
        _box('C$9.99', 60, 1210, 300, 1250),
    ]
    for index, (title, anchor) in enumerate(categories):
        column = index % 2
        left = 200.0 if column == 0 else 780.0
        top = 800.0 + (index // 2) * 240.0
        boxes.extend(
            [
                _box(title, left, top, left + 260, top + 50),
                _box(anchor, left, top + 60, left + 180, top + 100),
            ],
        )
    return _snapshot(*boxes)


def _results_frame() -> Snapshot:
    return _snapshot(
        _box(SEARCH_QUERIES[0], 235, 186, 413, 220),
        _box(SEARCH_QUERIES[1], 430, 186, 620, 220),
        _box('Choice', 50, 880, 180, 930),
        _box('Marathon Men Sports Running', 50, 945, 550, 995),
        _box('200 sold', 47, 940, 301, 980),
        _box('80 sold', 610, 940, 840, 980),
        _box('C$26.89', 70, 1126, 260, 1184),
        _box('C$24.58', 631, 1017, 821, 1076),
    )


def _coin_frame() -> Snapshot:
    return _snapshot(
        _box('Coins', 100, 200, 260, 250),
        _box('Coins credits to redeem', 60, 560, 600, 620),
        _box('Daily coins', 60, 920, 320, 980),
        _box('Earn more >', 920, 926, 1130, 967),
    )


def _wrong_frame() -> Snapshot:
    return _snapshot(
        _box('Bundle Deals', 60, 120, 500, 180),
        _box('C$9.99', 60, 200, 300, 240),
    )


def _dismiss(*families: str) -> DismissReport:
    return DismissReport(
        closed=families,
        attempts=(),
        frames=(),
        remaining=False,
        stop=DismissStop.CLEAR,
    )


def _controller(world: _World) -> Controller:
    return Controller.from_frame(_RecordingDevice(world), world.frame, rng=random.Random(0))


def _state(tmp_path: Path) -> DailyStateStore:
    return DailyStateStore(tmp_path / 'state', clock=lambda: CAPTURED_AT)


def _entry(
    catalogue: TaskCatalogue,
    frame: Snapshot,
    key: str = 'search_for_what_you_love',
    *,
    authorize: bool = True,
) -> RosterEntry:
    roster = enumerate_roster(frame, catalogue)
    for entry in roster.entries:
        if entry.key == key:
            return DispatchGate(catalogue).authorize(entry) if authorize else entry
    raise AssertionError(f'{key!r} is not in the synthetic roster')


@pytest.fixture(scope='module')
def catalogue() -> TaskCatalogue:
    return load_task_catalogue(CATALOGUE_PATH)


@pytest.fixture(scope='module')
def corpus() -> PopupCorpus:
    return load_popup_corpus(CORPUS_PATH)


def _run(
    world: _World,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    state: DailyStateStore,
    entry: RosterEntry,
    *,
    clock: _SteppingClock | None = None,
    **overrides: Any,
) -> Any:
    the_clock = clock if clock is not None else _SteppingClock()
    params: dict[str, Any] = {
        'snapshot': world.frame,
        'navigate': world.navigate,
        'load_timeout': 1.0,
        'arrival_timeout': 1.0,
        'result_timeout': 1.0,
        'badge_timeout': 1.0,
        'interval': 0.5,
        'return_timeout': 2.0,
        'return_interval': 0.5,
        'clock': the_clock,
        'sleep': the_clock.sleep,
    }
    params.update(overrides)
    return run_search_task(
        _controller(world),
        world.capture,
        corpus,
        state,
        entry,
        **params,
    )


def test_find_search_field_lands_on_the_measured_hint_and_search_control() -> None:
    hint = _box('OPPO', 237, 188, 309, 216)
    control = _box('Q', 1026, 182, 1095, 249)
    landing = _snapshot(
        hint,
        control,
        _box('Search history', 45, 335, 370, 392),
        _box('Discover more', 35, 736, 299, 777),
        _box('Sneakers', 200, 800, 460, 850),
        _box('74k+ items', 200, 860, 380, 900),
    )

    field = find_search_field(landing)

    assert field is not None
    assert field.center[0] == landing.width / 2
    assert hint.bbox[1] <= field.center[1] <= hint.bbox[3]
    assert control.bbox[0] <= field.submit[0] <= control.bbox[2]
    assert control.bbox[1] <= field.submit[1] <= control.bbox[3]
    assert field.as_dict()['center'] == list(field.center)


def test_find_search_field_uses_the_hint_without_a_history_heading() -> None:
    hint = _box('OPPO', 237, 188, 309, 216)
    control = _box('Q', 1026, 182, 1095, 249)
    landing = _snapshot(
        hint,
        control,
        _box('Discover more', 35, 736, 299, 777),
        _box('Sneakers', 200, 800, 460, 850),
        _box('74k+ items', 200, 860, 380, 900),
    )

    field = find_search_field(landing)

    assert field is not None
    assert field.center[1] == hint.center[1]
    assert control.bbox[1] <= field.submit[1] <= control.bbox[3]
    assert field.bottom == landing.height * 0.15


def test_find_search_field_requires_a_landing_heading() -> None:
    assert find_search_field(_snapshot(_box('Search', 200, 180, 900, 270))) is None


def test_search_results_require_the_submitted_query_and_a_price() -> None:
    results = _results_frame()

    assert is_search_results(results, query=SEARCH_QUERIES[0]) is True
    assert is_search_results(results) is True
    assert is_search_results(results, query='unsubmitted query') is False
    assert is_search_results(_landing_frame(), query=SEARCH_QUERIES[0]) is False
    assert is_search_results(_snapshot(_box(SEARCH_QUERIES[0], 235, 186, 413, 220))) is False


def test_find_search_field_rejects_a_heading_above_the_field_band() -> None:
    landing = _snapshot(
        _box('Discover more', 60, 40, 400, 90),
        _box('Sneakers', 200, 120, 460, 170),
        _box('74k+ items', 200, 180, 380, 220),
    )

    assert find_search_field(landing) is None


def test_field_holds_only_a_query_inside_the_field_band() -> None:
    from aliexpress_coins.tasks import search

    landing = _snapshot(
        _box('phone case', 200, 180, 900, 270),
        _box('Search history', 60, 320, 400, 370),
        _box('Discover more', 60, 660, 400, 710),
    )
    field = find_search_field(landing)

    assert field is not None
    assert search._field_holds(landing, field, 'phone case') is True
    assert search._field_holds(landing, field, 'wireless phone') is False


def test_clear_field_taps_a_visible_close_control() -> None:
    from aliexpress_coins.tasks import search

    landing = _snapshot(
        _box('phone case', 200, 180, 700, 270),
        _box('Close', 950, 180, 1100, 270),
        _box('Search history', 60, 320, 400, 370),
        _box('Discover more', 60, 660, 400, 710),
    )
    field = find_search_field(landing)
    assert field is not None
    world = _World({'landing': (landing,)}, {})
    controller = _controller(world)

    record = search._clear_field(controller, landing, field, 'phone case', why='clear the retained query')

    assert record is not None
    assert record.label == 'clear search'
    assert world.taps == [record.point]


def test_clear_field_deletes_the_query_when_no_close_control_is_visible() -> None:
    from aliexpress_coins.tasks import search

    landing = _snapshot(
        _box('phone case', 200, 180, 900, 270),
        _box('Search history', 60, 320, 400, 370),
        _box('Discover more', 60, 660, 400, 710),
    )
    field = find_search_field(landing)
    assert field is not None
    world = _World({'landing': (landing,)}, {})
    controller = _controller(world)

    record = search._clear_field(controller, landing, field, 'phone case', why='clear the retained query')
    assert search._clear_field(controller, landing, field, 'wireless phone', why='clear an absent query') is None

    assert record is None
    assert world.keys == ['KEYCODE_DEL'] * (len('phone case') + 1)
    assert world.taps == []


def test_query_for_round_reuses_a_query_only_after_all_are_used() -> None:
    from aliexpress_coins.tasks import search

    assert search._query_for_round(1, set()) == SEARCH_QUERIES[0]
    assert search._query_for_round(1, {SEARCH_QUERIES[0]}) == SEARCH_QUERIES[1]
    assert search._query_for_round(1, set(SEARCH_QUERIES)) == SEARCH_QUERIES[0]


@pytest.mark.parametrize('with_close', [True, False], ids=['close-control', 'delete-keys'])
def test_a_retained_previous_query_is_cleared_before_the_next_round(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    with_close: bool,
) -> None:
    boxes = [
        _box(SEARCH_QUERIES[0], 200, 180, 700, 270),
        _box('Search history', 60, 320, 400, 370),
        _box('Man Shoes', 60, 450, 280, 500),
        _box('Discover more', 60, 660, 400, 710),
        _box('Sneakers', 200, 800, 460, 850),
        _box('74k+ items', 200, 860, 380, 900),
    ]
    if with_close:
        boxes.insert(1, _box('Close', 950, 180, 1100, 270))
    landing = _snapshot(*boxes)
    earn_more = 0

    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        nonlocal earn_more
        if screen == 'coins' and label == 'earn more':
            earn_more += 1
            return 'boost1' if earn_more == 1 else 'boost_gone'
        return None

    world = _World(
        series={
            'boost0': (_boost_frame(badge='0/2'),),
            'landing': (_landing_frame(),),
            'results': (_results_frame(),),
            'coins': (_coin_frame(),),
            'boost1': (_boost_frame(badge='1/2'),),
            'landing2': (landing,),
            'boost_gone': (_boost_frame(with_row=False),),
        },
        transitions={
            ('boost0', 'go'): 'landing',
            ('boost1', 'go'): 'landing2',
        },
        on_tap=on_tap,
        on_key=lambda screen, key, _count: (
            'results' if key == 'KEYCODE_ENTER' and screen in ('landing', 'landing2') else None
        ),
    )
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state, _entry(catalogue, world.frame))

    assert result.outcome is SearchOutcome.COMPLETED
    assert result.queries == SEARCH_QUERIES
    assert any(tap.label == 'clear search' for tap in result.taps) is with_close
    assert world.keys.count('KEYCODE_ENTER') == 2
    if not with_close:
        assert world.keys.count('KEYCODE_DEL') == len(SEARCH_QUERIES[0]) + 1
    assert state.count('search_for_what_you_love') == 2


@pytest.mark.slow
def test_the_captured_landing_replays_two_real_search_rounds(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    landing = _load_snapshot(STATES / 'search_landing.png')
    assert find_search_field(landing) is not None

    earn_more = 0

    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        nonlocal earn_more
        if screen == 'coins' and label == 'earn more':
            earn_more += 1
            return 'boost1' if earn_more == 1 else 'boost_gone'
        return None

    world = _World(
        series={
            'boost0': (_boost_frame(badge='0/2'),),
            'landing': (landing,),
            'results': (_results_frame(),),
            'coins': (_coin_frame(),),
            'boost1': (_boost_frame(badge='1/2'),),
            'boost_gone': (_boost_frame(with_row=False),),
        },
        transitions={
            ('boost0', 'go'): 'landing',
            ('boost1', 'go'): 'landing',
        },
        on_tap=on_tap,
    )
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state, _entry(catalogue, world.frame))

    assert result.outcome is SearchOutcome.COMPLETED
    assert result.queries == SEARCH_QUERIES
    assert result.verification == 'progress_complete'
    assert world.texts == list(SEARCH_QUERIES)
    assert state.count('search_for_what_you_love') == 2


def test_a_two_round_visit_advances_the_badge_and_stops_when_the_row_is_gone(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    earn_more = 0

    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        nonlocal earn_more
        if screen == 'coins' and label == 'earn more':
            earn_more += 1
            return 'boost1' if earn_more == 1 else 'boost_gone'
        return None

    world = _World(
        series={
            'boost0': (_boost_frame(badge='0/2'),),
            'landing': (_landing_frame(),),
            'results': (_results_frame(),),
            'coins': (_coin_frame(),),
            'boost1': (_boost_frame(badge='1/2'),),
            'boost_gone': (_boost_frame(with_row=False),),
        },
        transitions={
            ('boost0', 'go'): 'landing',
            ('boost1', 'go'): 'landing',
            ('landing', 'sneakers'): 'results',
            ('landing', 'woman clothing'): 'results',
        },
        on_tap=on_tap,
    )
    state = _state(tmp_path)
    entry = _entry(catalogue, world.frame)

    result = _run(world, catalogue, corpus, state, entry)

    assert result.outcome is SearchOutcome.COMPLETED
    assert result.verification == 'progress_complete'
    assert result.rounds == 2
    assert result.queries == SEARCH_QUERIES
    assert [record.badge_change for record in result.round_records] == ['0/2 -> 1/2', '1/2 -> ?']
    assert result.badge_before_text == '0/2'
    assert result.badge_after_text == '1/2'
    assert 'expected 2/2' in result.detail
    assert result.recorded is True
    assert result.returned is True
    assert result.completed is True
    assert result.failed is False
    assert result.skipped is False
    assert state.count('search_for_what_you_love') == 2


def test_a_final_readable_two_of_two_badge_completes_the_visit(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        return 'boost2' if screen == 'coins' and label == 'earn more' else None

    world = _World(
        series={
            'boost1': (_boost_frame(badge='1/2'),),
            'landing': (_landing_frame(),),
            'results': (_results_frame(),),
            'coins': (_coin_frame(),),
            'boost2': (_boost_frame(badge='2/2'),),
        },
        transitions={
            ('boost1', 'go'): 'landing',
            ('landing', 'sneakers'): 'results',
        },
        on_tap=on_tap,
    )
    state = _state(tmp_path)
    entry = _entry(catalogue, world.frame)

    result = _run(world, catalogue, corpus, state, entry)

    assert result.outcome is SearchOutcome.COMPLETED
    assert result.verification == 'progress_complete'
    assert result.detail == f'{entry.title!r} reached 2/2 after 1 round(s)'
    assert result.badge_after_text == '2/2'
    assert state.count('search_for_what_you_love') == 1


def test_an_unreadable_entry_badge_can_complete_on_a_two_of_two_after_round(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        return 'boost2' if screen == 'coins' and label == 'earn more' else None

    world = _World(
        series={
            'boost_none': (_boost_frame(badge=None),),
            'landing': (_landing_frame(),),
            'results': (_results_frame(),),
            'coins': (_coin_frame(),),
            'boost2': (_boost_frame(badge='2/2'),),
        },
        transitions={
            ('boost_none', 'go'): 'landing',
            ('landing', 'sneakers'): 'results',
        },
        on_tap=on_tap,
    )
    state = _state(tmp_path)
    state.record('search_for_what_you_love')

    result = _run(world, catalogue, corpus, state, _entry(catalogue, world.frame))

    assert result.outcome is SearchOutcome.COMPLETED
    assert result.verification == 'progress_complete'
    assert result.done_today_before == 1
    assert state.count('search_for_what_you_love') == 2


def test_a_non_advancing_badge_fails_with_expected_vs_observed_and_leaves_state(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        return 'boost_same' if screen == 'coins' and label == 'earn more' else None

    world = _World(
        series={
            'boost0': (_boost_frame(badge='0/2'),),
            'landing': (_landing_frame(),),
            'results': (_results_frame(),),
            'coins': (_coin_frame(),),
            'boost_same': (_boost_frame(badge='0/2'),),
        },
        transitions={
            ('boost0', 'go'): 'landing',
            ('landing', 'sneakers'): 'results',
        },
        on_tap=on_tap,
    )
    state = _state(tmp_path)
    entry = _entry(catalogue, world.frame)

    result = _run(world, catalogue, corpus, state, entry)

    assert result.outcome is SearchOutcome.FAILED
    assert result.verification == 'progress_unchanged'
    assert result.detail == f'progress_unchanged: expected 1/2, saw 0/2 ({entry.title!r})'
    assert result.recorded is False
    assert result.rounds == 1
    assert result.round_records[-1].complete is False
    assert state.count('search_for_what_you_love') == 0
    assert result.returned is True
    assert result.completed is False
    assert result.failed is True
    assert result.skipped is False


def test_a_row_that_disappears_before_the_final_round_is_not_a_completion(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        return 'boost_gone' if screen == 'coins' and label == 'earn more' else None

    world = _World(
        series={
            'boost0': (_boost_frame(badge='0/2'),),
            'landing': (_landing_frame(),),
            'results': (_results_frame(),),
            'coins': (_coin_frame(),),
            'boost_gone': (_boost_frame(with_row=False),),
        },
        transitions={
            ('boost0', 'go'): 'landing',
            ('landing', 'sneakers'): 'results',
        },
        on_tap=on_tap,
    )
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state, _entry(catalogue, world.frame))

    assert result.outcome is SearchOutcome.FAILED
    assert result.verification == 'progress_unchanged'
    assert 'saw the row gone before the final round' in result.detail
    assert state.count('search_for_what_you_love') == 0


def test_a_row_absent_on_the_inline_coin_page_is_not_trusted_as_completion(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    inline_coin = _snapshot(
        _box('Coins', 100, 200, 260, 250),
        _box('Coins credits to redeem', 60, 560, 600, 620),
        _box('Daily coins', 60, 920, 320, 980),
    )
    world = _World(
        series={
            'boost0': (_boost_frame(badge='0/2'),),
            'landing': (_landing_frame(),),
            'results': (_results_frame(),),
            'coins': (inline_coin,),
        },
        transitions={
            ('boost0', 'go'): 'landing',
            ('landing', 'sneakers'): 'results',
        },
    )
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state, _entry(catalogue, world.frame))

    assert result.outcome is SearchOutcome.FAILED
    assert result.verification == 'progress_unchanged'
    assert 'saw the row gone before the final round' in result.detail


def test_a_transient_blank_final_frame_does_not_prove_the_row_is_gone(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aliexpress_coins.tasks import search

    world = _World(
        series={
            'boost1': (_boost_frame(badge='1/2'),),
            'landing': (_landing_frame(),),
            'results': (_results_frame(),),
            'coins': (_coin_frame(),),
            'blank': (_snapshot(),),
        },
        transitions={
            ('boost1', 'go'): 'landing',
            ('landing', 'sneakers'): 'results',
        },
    )
    opened = BoostList(
        snapshot=_boost_frame(badge='1/2'),
        route=DiscoveryRoute.BOOST,
        detail='test',
    )

    def open_list(*_args: object, **_kwargs: object) -> BoostList:
        world.go('blank')
        return opened

    monkeypatch.setattr(search, 'open_boost_list', open_list)
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state, _entry(catalogue, world.frame))

    assert result.outcome is SearchOutcome.FAILED
    assert result.verification == 'progress_unchanged'
    assert 'saw the row gone before the final round' in result.detail
    assert state.count('search_for_what_you_love') == 0


def test_a_missing_landing_fails_with_the_return_guaranteed(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={'boost0': (_boost_frame(),), 'coins': (_coin_frame(),)},
        transitions={},
    )

    result = _run(world, catalogue, corpus, _state(tmp_path), _entry(catalogue, world.frame))

    assert result.outcome is SearchOutcome.FAILED
    assert result.verification == 'landing_not_arrived'
    assert result.returned is True


def test_an_early_failure_that_cannot_return_is_reported_as_return_failed(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={'boost0': (_boost_frame(),), 'wrong': (_wrong_frame(),)},
        transitions={},
        navigate_to='wrong',
    )

    result = _run(world, catalogue, corpus, _state(tmp_path), _entry(catalogue, world.frame))

    assert result.outcome is SearchOutcome.FAILED
    assert result.verification == 'return_failed'
    assert 'did not prove the coins page' in result.detail
    assert result.returned is False


def test_a_landing_without_a_search_field_fails(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aliexpress_coins.tasks import search

    world = _World(
        series={
            'boost0': (_boost_frame(badge='0/2'),),
            'landing': (_landing_frame(),),
            'coins': (_coin_frame(),),
        },
        transitions={('boost0', 'go'): 'landing'},
    )
    monkeypatch.setattr(search, 'find_search_field', lambda _snapshot: None)

    result = _run(world, catalogue, corpus, _state(tmp_path), _entry(catalogue, world.frame))

    assert result.outcome is SearchOutcome.FAILED
    assert result.verification == 'search_field_not_found'
    assert result.returned is True


def test_a_search_that_does_not_leave_the_landing_fails_without_recording(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost0': (_boost_frame(),),
            'landing': (_landing_frame(),),
            'coins': (_coin_frame(),),
        },
        transitions={('boost0', 'go'): 'landing'},
    )
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state, _entry(catalogue, world.frame))

    assert result.outcome is SearchOutcome.FAILED
    assert result.verification == 'results_not_reached'
    assert 'did not reach a results surface' in result.detail
    assert result.recorded is False
    assert result.returned is True
    assert state.count('search_for_what_you_love') == 0
    assert world.texts == [SEARCH_QUERIES[0]]
    assert world.keys.count('KEYCODE_ENTER') == 1


def test_the_search_control_is_the_bounded_fallback_when_enter_does_not_submit(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        return 'boost2' if screen == 'coins' and label == 'earn more' else None

    world = _World(
        series={
            'boost1': (_boost_frame(badge='1/2'),),
            'landing': (_landing_frame(),),
            'results': (_results_frame(),),
            'coins': (_coin_frame(),),
            'boost2': (_boost_frame(badge='2/2'),),
        },
        transitions={
            ('boost1', 'go'): 'landing',
            ('landing', 'submit'): 'results',
        },
        on_tap=on_tap,
        on_key=lambda _screen, _key, _count: None,
    )
    state = _state(tmp_path)
    state.record('search_for_what_you_love')

    result = _run(world, catalogue, corpus, state, _entry(catalogue, world.frame))

    assert result.outcome is SearchOutcome.COMPLETED
    assert result.verification == 'progress_complete'
    assert result.queries == (SEARCH_QUERIES[1],)
    assert result.returned is True
    assert state.count('search_for_what_you_love') == 2


def test_a_popup_after_the_search_is_swept_before_the_return(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aliexpress_coins.tasks import search

    popup = _snapshot(_box('Welcome gifts', 200, 700, 1000, 780))

    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        return 'boost2' if screen == 'coins' and label == 'earn more' else None

    world = _World(
        series={
            'boost1': (_boost_frame(badge='1/2'),),
            'landing': (_landing_frame(),),
            'popup': (popup,),
            'results': (_results_frame(),),
            'coins': (_coin_frame(),),
            'boost2': (_boost_frame(badge='2/2'),),
        },
        transitions={('boost1', 'go'): 'landing'},
        on_tap=on_tap,
        on_key=lambda screen, key, _count: 'popup' if screen == 'landing' and key == 'KEYCODE_ENTER' else None,
    )
    report = _dismiss('launch_welcome_gifts')

    def sweep(*_args: object, **_kwargs: object) -> tuple[DismissReport, None]:
        world.go('results')
        return report, None

    monkeypatch.setattr(search, '_sweep', sweep)
    state = _state(tmp_path)
    state.record('search_for_what_you_love')

    result = _run(world, catalogue, corpus, state, _entry(catalogue, world.frame))

    assert result.outcome is SearchOutcome.COMPLETED
    assert report in result.dismissals


def test_a_redeem_interstitial_after_the_search_stops_the_round(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aliexpress_coins.tasks import search

    popup = _snapshot(_box('Welcome gifts', 200, 700, 1000, 780))
    world = _World(
        series={
            'boost0': (_boost_frame(badge='0/2'),),
            'landing': (_landing_frame(),),
            'popup': (popup,),
            'coins': (_coin_frame(),),
        },
        transitions={('boost0', 'go'): 'landing'},
        on_key=lambda screen, key, _count: 'popup' if screen == 'landing' and key == 'KEYCODE_ENTER' else None,
    )
    monkeypatch.setattr(search, '_sweep', lambda *args, **kwargs: (None, 'redeem_interstitial'))

    result = _run(world, catalogue, corpus, _state(tmp_path), _entry(catalogue, world.frame))

    assert result.outcome is SearchOutcome.FAILED
    assert result.verification == 'redeem_interstitial'
    assert result.stop == 'redeem_interstitial'


def test_the_round_return_is_reported_when_the_route_cannot_prove_the_coin_page(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost0': (_boost_frame(),),
            'landing': (_landing_frame(),),
            'results': (_results_frame(),),
            'wrong': (_wrong_frame(),),
        },
        transitions={
            ('boost0', 'go'): 'landing',
            ('landing', 'sneakers'): 'results',
        },
        navigate_to='wrong',
    )
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state, _entry(catalogue, world.frame))

    assert result.outcome is SearchOutcome.FAILED
    assert result.verification == 'return_failed'
    assert result.returned is False
    assert state.count('search_for_what_you_love') == 0


def test_a_popup_closed_while_reopening_the_list_is_kept_in_the_result(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aliexpress_coins.tasks import search

    world = _World(
        series={
            'boost1': (_boost_frame(badge='1/2'),),
            'landing': (_landing_frame(),),
            'results': (_results_frame(),),
            'coins': (_coin_frame(),),
        },
        transitions={
            ('boost1', 'go'): 'landing',
            ('landing', 'sneakers'): 'results',
        },
    )
    report = _dismiss('promo_coupon_unlocked')
    opened = BoostList(
        snapshot=_boost_frame(badge='2/2'),
        route=DiscoveryRoute.BOOST,
        detail='test',
        dismiss=report,
    )
    monkeypatch.setattr(search, 'open_boost_list', lambda *args, **kwargs: opened)

    result = _run(world, catalogue, corpus, _state(tmp_path), _entry(catalogue, world.frame))

    assert result.outcome is SearchOutcome.COMPLETED
    assert report in result.dismissals


def test_a_successful_visit_reports_return_failed_when_the_final_return_fails(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    earn_more = 0

    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        nonlocal earn_more
        if screen == 'coins' and label == 'earn more':
            earn_more += 1
            return 'boost1' if earn_more == 1 else 'boost_gone'
        return None

    world = _World(
        series={
            'boost0': (_boost_frame(badge='0/2'),),
            'landing': (_landing_frame(),),
            'results': (_results_frame(),),
            'coins': (_coin_frame(),),
            'wrong': (_wrong_frame(),),
            'boost1': (_boost_frame(badge='1/2'),),
            'boost_gone': (_boost_frame(with_row=False),),
        },
        transitions={
            ('boost0', 'go'): 'landing',
            ('boost1', 'go'): 'landing',
            ('landing', 'sneakers'): 'results',
            ('landing', 'woman clothing'): 'results',
        },
        on_tap=on_tap,
        on_navigate=lambda _route, count: 'coins' if count <= 2 else 'wrong',
    )
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state, _entry(catalogue, world.frame))

    assert result.outcome is SearchOutcome.FAILED
    assert result.verification == 'return_failed'
    assert result.returned is False
    assert state.count('search_for_what_you_love') == 2


def test_an_entry_popup_is_kept_in_the_result_dismissals(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aliexpress_coins.tasks import search

    landing = _snapshot(
        _box('Discover more', 60, 660, 400, 710),
        _box('Sneakers', 200, 800, 460, 850),
        _box('74k+ items', 200, 860, 380, 900),
    )
    world = _World(
        series={
            'boost0': (_boost_frame(),),
            'landing': (landing,),
            'coins': (_coin_frame(),),
        },
        transitions={('boost0', 'go'): 'landing'},
    )
    report = _dismiss('launch_welcome_gifts')
    monkeypatch.setattr(search, '_settle_surface', lambda *args, **kwargs: (world.frame, True, report))

    result = _run(world, catalogue, corpus, _state(tmp_path), _entry(catalogue, world.frame))

    assert result.verification == 'results_not_reached'
    assert [dismiss.closed for dismiss in result.dismissals] == [('launch_welcome_gifts',)]


def test_a_task_surface_that_never_settles_fails_before_a_tap(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aliexpress_coins.tasks import search

    world = _World({'boost0': (_boost_frame(),), 'coins': (_coin_frame(),)}, {})
    monkeypatch.setattr(search, '_settle_surface', lambda *args, **kwargs: (world.frame, False, None))

    result = _run(world, catalogue, corpus, _state(tmp_path), _entry(catalogue, world.frame))

    assert result.verification == 'not_settled'
    assert result.returned is True


def test_a_wrong_start_surface_fails_before_a_tap(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World({'wrong': (_wrong_frame(),), 'coins': (_coin_frame(),)}, {})
    entry = _entry(catalogue, _boost_frame())

    result = _run(world, catalogue, corpus, _state(tmp_path), entry)

    assert result.verification == 'task_surface_unavailable'
    assert result.returned is True


def test_a_second_round_with_no_action_fails_without_recording_it(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        return 'boost_no_action' if screen == 'coins' and label == 'earn more' else None

    world = _World(
        series={
            'boost0': (_boost_frame(badge='0/2'),),
            'landing': (_landing_frame(),),
            'results': (_results_frame(),),
            'coins': (_coin_frame(),),
            'boost_no_action': (
                _snapshot(
                    _box('Coins boost', 80, 90, 400, 150),
                    _box(SEARCH_TITLE, 60, 420, 700, 470),
                    _box('Earn by using keywords to explore items', 60, 480, 700, 530),
                    _box('1/2', 1000, 480, 1080, 530),
                ),
            ),
        },
        transitions={
            ('boost0', 'go'): 'landing',
            ('landing', 'sneakers'): 'results',
        },
        on_tap=on_tap,
    )
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state, _entry(catalogue, world.frame))

    assert result.outcome is SearchOutcome.FAILED
    assert result.verification == 'row_complete'
    assert result.rounds == 1
    assert state.count('search_for_what_you_love') == 1


def test_a_round_limit_stop_reports_progress_advanced_without_claiming_the_visit(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        return 'boost1' if screen == 'coins' and label == 'earn more' else None

    world = _World(
        series={
            'boost0': (_boost_frame(badge='0/2'),),
            'landing': (_landing_frame(),),
            'results': (_results_frame(),),
            'coins': (_coin_frame(),),
            'boost1': (_boost_frame(badge='1/2'),),
        },
        transitions={
            ('boost0', 'go'): 'landing',
            ('landing', 'sneakers'): 'results',
        },
        on_tap=on_tap,
    )
    state = _state(tmp_path)
    state.record('search_for_what_you_love')

    result = _run(world, catalogue, corpus, state, _entry(catalogue, world.frame))

    assert result.outcome is SearchOutcome.COMPLETED
    assert result.verification == 'progress_advanced'
    assert result.recorded is True
    assert 'advanced 1 round(s)' in result.detail
    assert state.count('search_for_what_you_love') == 2


def test_a_run_interrupt_is_re_raised_after_the_executor_starts(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aliexpress_coins.tasks import search

    world = _World({'boost0': (_boost_frame(),), 'coins': (_coin_frame(),)}, {})

    def interrupt(*_args: object, **_kwargs: object) -> None:
        raise RunInterrupted()

    monkeypatch.setattr(search, '_wait_for_landing', interrupt)

    with pytest.raises(RunInterrupted):
        _run(world, catalogue, corpus, _state(tmp_path), _entry(catalogue, world.frame))


def test_a_popup_on_the_way_to_the_landing_is_swept(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aliexpress_coins.tasks import search

    popup = _snapshot(_box('Welcome gifts', 200, 700, 1000, 780), _box('Close', 900, 1200, 1000, 1260))
    landing = _snapshot(
        _box('Discover more', 60, 660, 400, 710),
        _box('Sneakers', 200, 800, 460, 850),
        _box('74k+ items', 200, 860, 380, 900),
    )
    world = _World(
        series={
            'boost0': (_boost_frame(),),
            'popup': (popup,),
            'landing': (landing,),
            'coins': (_coin_frame(),),
        },
        transitions={('boost0', 'go'): 'popup'},
    )
    report = _dismiss('launch_welcome_gifts')

    def sweep(*_args: object, **_kwargs: object) -> tuple[DismissReport, None]:
        world.go('landing')
        return report, None

    monkeypatch.setattr(search, '_sweep', sweep)

    result = _run(world, catalogue, corpus, _state(tmp_path), _entry(catalogue, _boost_frame()))

    assert result.verification == 'results_not_reached'
    assert report in result.dismissals


def test_a_redeem_interstitial_before_the_landing_stops_the_round(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aliexpress_coins.tasks import search

    popup = _snapshot(_box('Welcome gifts', 200, 700, 1000, 780))
    world = _World(
        series={
            'boost0': (_boost_frame(),),
            'popup': (popup,),
            'coins': (_coin_frame(),),
        },
        transitions={('boost0', 'go'): 'popup'},
    )
    monkeypatch.setattr(search, '_sweep', lambda *args, **kwargs: (None, 'redeem_interstitial'))

    result = _run(world, catalogue, corpus, _state(tmp_path), _entry(catalogue, _boost_frame()))

    assert result.outcome is SearchOutcome.FAILED
    assert result.verification == 'redeem_interstitial'
    assert result.stop == 'redeem_interstitial'


def test_an_executor_exception_still_attempts_the_return(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aliexpress_coins.tasks import search

    world = _World(
        series={'boost0': (_boost_frame(),), 'coins': (_coin_frame(),)},
        transitions={},
    )

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError('landing wait exploded')

    monkeypatch.setattr(search, '_wait_for_landing', explode)

    with pytest.raises(RuntimeError, match='landing wait exploded'):
        _run(world, catalogue, corpus, _state(tmp_path), _entry(catalogue, world.frame))

    assert world.current == 'coins'
    assert world.navigations


def test_the_executor_skips_at_the_daily_limit_before_any_capture(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    for _ in range(2):
        state.record('search_for_what_you_love')
    world = _World({'boost0': (_boost_frame(),)}, {})
    navigate = _ForbiddenNavigate()

    result = run_search_task(
        _controller(world),
        _ForbiddenCapture(),
        corpus,
        state,
        _entry(catalogue, world.frame),
        navigate=cast(Any, navigate),
    )

    assert result.outcome is SearchOutcome.SKIPPED
    assert result.verification == 'daily_limit'
    assert result.done_today_before == 2
    assert result.skipped is True
    assert result.completed is False
    assert navigate.calls == 0


def test_the_executor_skips_a_vanished_row(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World({'boost0': (_boost_frame(with_row=False),)}, {})
    entry = _entry(catalogue, _boost_frame())

    result = _run(world, catalogue, corpus, _state(tmp_path), entry)

    assert result.outcome is SearchOutcome.SKIPPED
    assert result.verification == 'row_vanished'
    assert result.recorded is False


def test_the_executor_skips_a_row_whose_badge_has_no_rounds_left(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World({'boost2': (_boost_frame(badge='2/2'),)}, {})

    result = _run(world, catalogue, corpus, _state(tmp_path), _entry(catalogue, world.frame))

    assert result.outcome is SearchOutcome.SKIPPED
    assert result.verification == 'row_complete'
    assert 'all 2 round(s) already complete' in result.detail


def test_the_executor_rejects_a_denied_entry(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    frame = _snapshot(
        _box('Coins boost', 80, 90, 400, 150),
        _box('Play & Earn', 60, 420, 500, 470),
        _box('GO', 1000, 420, 1080, 470),
    )
    world = _World({'boost': (frame,)}, {})
    raw = enumerate_roster(frame, catalogue).entries[0]

    with pytest.raises(TaskError, match='not dispatchable'):
        _run(world, catalogue, corpus, _state(tmp_path), raw)


def test_the_executor_rejects_a_non_browse_category(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    frame = _snapshot(
        _box('Coins boost', 80, 90, 400, 150),
        _box('Explore sponsored items', 60, 420, 700, 470),
        _box('GO', 1000, 420, 1080, 470),
    )
    world = _World({'boost': (frame,)}, {})
    entry = _entry(catalogue, frame, key='sponsored_items')

    with pytest.raises(TaskError, match='not a browse task'):
        _run(world, catalogue, corpus, _state(tmp_path), entry)


@pytest.mark.parametrize(
    ('name', 'value'),
    [
        ('load_timeout', 0.0),
        ('arrival_timeout', 0.0),
        ('result_timeout', 0.0),
        ('badge_timeout', 0.0),
        ('interval', 0.0),
        ('return_timeout', 0.0),
        ('return_interval', 0.0),
        ('return_attempts', 0),
        ('settle', -1.0),
        ('max_rounds', 0),
    ],
)
def test_the_executor_rejects_invalid_parameters(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    name: str,
    value: Any,
) -> None:
    world = _World({'boost0': (_boost_frame(),)}, {})
    overrides: dict[str, Any] = {name: value}

    with pytest.raises(ValueError, match=r'must be positive|must not be negative'):
        _run(world, catalogue, corpus, _state(tmp_path), _entry(catalogue, world.frame), **overrides)
