"""The surprise-items executor over measured and synthetic frames (issue #26).

The device is simulated at the capture and input seams only: taps, key presses
and swipes are recorded by a fake device that drives a small screen graph;
captures come from that graph; and time is injected. No test reaches adb and no
test sleeps.

The synthetic grid uses the measured layout: a two-column product grid below a
header, a subtitle and the round pill. The world credits a product only when the
fixture says it should, which is how the reuse/reset paths and the "grid
disappeared" failure are exercised.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from aliexpress_coins.control.controller import Controller
from aliexpress_coins.control.errors import PopupUnclosable
from aliexpress_coins.control.popups import DismissReport, PopupCorpus, load_popup_corpus
from aliexpress_coins.perception.errors import OcrError
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.ocr import RapidOcrEngine
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks.catalogue import TaskCatalogue, load_task_catalogue
from aliexpress_coins.tasks.discovery import RosterEntry, enumerate_roster
from aliexpress_coins.tasks.dispatch import DispatchGate
from aliexpress_coins.tasks.errors import TaskError
from aliexpress_coins.tasks.surface import RouteFirer, SurfaceRoute
from aliexpress_coins.tasks.surprise import (
    MAX_ROUNDS,
    RULE_PATH_RESET,
    RULE_PATH_REUSE,
    SURPRISE_TITLE,
    CounterState,
    RoundRecord,
    SurpriseOutcome,
    SurpriseResult,
    find_back_control,
    find_mission_overlay,
    find_next_round,
    find_surprise_tiles,
    is_item_detail,
    is_surprise_grid,
    read_surprise_counter,
    run_surprise_task,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'tests' / 'data'
CORPUS_PATH = ROOT / 'specs' / 'popups.yaml'
CATALOGUE_PATH = ROOT / 'specs' / 'tasks.yaml'
CAPTURED_AT = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)

PRODUCTS = ('Product A', 'Product B', 'Product C', 'Product D', 'Product E', 'Product F')


class _RecordingDevice:
    """The input seam: record every gesture and let the world react to it."""

    def __init__(self, world: _World) -> None:
        self._world = world

    def tap(self, x: int, y: int) -> None:
        self._world.tap(x, y)

    def keyevent(self, key: str) -> None:
        self._world.keyevent(key)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        self._world.swipe(x1, y1, x2, y2, duration_ms)


class _NoKeyDevice:
    """A device that can tap but not press BACK, to prove the chevron fallback."""

    def __init__(self, world: _World) -> None:
        self._world = world

    def tap(self, x: int, y: int) -> None:
        self._world.tap(x, y)


class _ForbiddenCapture:
    """A capture that proves state or policy stopped the executor first."""

    def __init__(self) -> None:
        self.calls = 0

    def capture(self) -> Snapshot:
        self.calls += 1
        raise AssertionError('the executor must not capture on this path')


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
    """A tiny screen graph driven by taps, BACK and navigation, not a frame queue."""

    def __init__(
        self,
        series: dict[str, Sequence[Snapshot]],
        transitions: dict[tuple[str, str | None], str],
        *,
        start: str | None = None,
        on_tap: Callable[[str, str | None, int], str | None] | None = None,
        on_key: Callable[[str, str, int], str | None] | None = None,
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
        self.current = start
        self.taps: list[tuple[int, int]] = []
        self.keys: list[str] = []
        self.swipes: list[tuple[int, int, int, int, int]] = []
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
        target = None
        if self._on_key is not None:
            target = self._on_key(self.current, key, len(self.keys))
        if target is None:
            target = self._transitions.get((self.current, key))
        if target is not None:
            self.go(target)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        self.swipes.append((x1, y1, x2, y2, duration_ms))

    def navigate(self, route: SurfaceRoute) -> None:
        self.navigations.append(route)
        self.go('coins')

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


def _label_at(snapshot: Snapshot, x: float, y: float) -> str | None:
    best: TextBox | None = None
    for box in snapshot.boxes:
        left, top, right, bottom = box.bbox
        if not (left <= x <= right and top <= y <= bottom):
            continue
        if best is None or box.width * box.height < best.width * best.height:
            best = box
    return None if best is None else best.text


def _boost_frame(*, progress: str = '0/2') -> Snapshot:
    boxes = [
        _box('Coins boost', 80, 90, 400, 150),
        _box(SURPRISE_TITLE, 60, 420, 700, 470),
        _box('Tap 3 items on this page to earn coins', 60, 480, 700, 530),
        _box(progress, 1000, 480, 1080, 530),
        _box('GO', 1090, 480, 1160, 530),
        _box('Turn on notifications', 60, 800, 500, 850),
        _box('Stay informed when rewards drop', 60, 860, 500, 900),
        _box('GO', 1000, 860, 1080, 910),
        _box('Place an order on this page', 60, 1200, 500, 1250),
        _box('GO', 1000, 1200, 1080, 1250),
    ]
    return _snapshot(*boxes)


def _grid_frame(counter: int | None = 0, *, next_round: bool = False, mission: bool = False) -> Snapshot:
    boxes = [
        _box(SURPRISE_TITLE, 270, 200, 930, 270),
        _box('Tap 3 items on this page to earn coins', 253, 332, 946, 384),
        _box('+1', 345, 447, 395, 482),
    ]
    if counter is not None:
        boxes.append(_box(f'{counter}/3', 480, 440, 573, 490))
    for index, product in enumerate(PRODUCTS):
        column = index % 2
        row = index // 2
        left = 90.0 if column == 0 else 690.0
        top = 600.0 + 300.0 * row
        boxes.append(_box(product, left, top, left + 400, top + 55))
    if mission:
        boxes.append(_box('Mission Completed', 200, 1450, 1000, 1550))
    if next_round:
        boxes.append(_box('Next round', 500, 1700, 700, 1760))
    return _snapshot(*boxes)


def _detail_frame() -> Snapshot:
    return _snapshot(
        _box('Item 1/6', 40, 100, 300, 140),
        _box("T9 Vintage Men's Electric Hair Clipper", 40, 500, 1100, 550),
        _box('Add to cart', 393, 1860, 510, 1887),
        _box('Buy now', 895, 1859, 988, 1890),
    )


def _coins_frame() -> Snapshot:
    return _snapshot(
        _box('Coins', 101, 194, 254, 242),
        _box('Coins credits to redeem', 59, 567, 552, 617),
        _box('Daily coins', 66, 919, 320, 978),
        _box('Today', 109, 1013, 210, 1063),
        _box('Earn more >', 917, 926, 1133, 967),
    )


def _wrong_frame() -> Snapshot:
    return _snapshot(
        _box('AliExpress', 60, 100, 400, 160),
        _box('Wireless Earbuds', 60, 400, 500, 450),
        _box('C$12.99', 60, 460, 300, 510),
    )


def _controller(world: _World, frame: Snapshot) -> Controller:
    return Controller.from_frame(_RecordingDevice(world), frame, rng=random.Random(0))


def _state(tmp_path: Path) -> DailyStateStore:
    return DailyStateStore(tmp_path / 'state', clock=lambda: datetime(2026, 11, 22, tzinfo=UTC))


def _entry(catalogue: TaskCatalogue, *, progress: str = '0/2') -> RosterEntry:
    roster = enumerate_roster(_boost_frame(progress=progress), catalogue)
    for entry in roster.entries:
        if entry.key == 'surprise_items':
            return DispatchGate(catalogue).authorize(entry)
    raise AssertionError('surprise_items is not in the synthetic roster')


def _run(
    world: _World,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    state: DailyStateStore,
    *,
    entry: RosterEntry | None = None,
    controller: Controller | None = None,
    **kwargs: object,
) -> SurpriseResult:
    options: dict[str, object] = {
        'navigate': world.navigate,
        'arrival_timeout': 1.0,
        'exit_timeout': 1.0,
        'return_timeout': 2.0,
        'return_interval': 0.5,
        'clock': _SteppingClock(),
        'sleep': _SteppingClock().sleep,
    }
    options.update(kwargs)
    return run_surprise_task(
        controller if controller is not None else _controller(world, world.frame),
        world.capture,
        corpus,
        state,
        entry if entry is not None else _entry(catalogue),
        **options,  # type: ignore[arg-type]
    )


def _success_world(*, progress: str = '1/2', next_round: bool = False) -> _World:
    """A normal one-round visit: three distinct products credit A -> B -> C."""
    pending: dict[str, str] = {}

    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        if screen in ('grid0', 'grid1', 'grid2') and label in PRODUCTS[:3]:
            pending['return'] = f'grid{int(screen[-1]) + 1}'
            return 'detail'
        return None

    def on_key(screen: str, key: str, _count: int) -> str | None:
        if screen == 'detail' and key == 'KEYCODE_BACK':
            return pending.pop('return')
        return None

    return _World(
        series={
            'boost': (_boost_frame(progress=progress),),
            'grid0': (_grid_frame(0),),
            'grid1': (_grid_frame(1),),
            'grid2': (_grid_frame(2),),
            'grid3': (_grid_frame(3, next_round=next_round, mission=not next_round),),
            'detail': (_detail_frame(),),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'grid0'},
        on_tap=on_tap,
        on_key=on_key,
    )


def _two_round_world() -> _World:
    """Round 1 credits A/B/C; round 2 rejects them until the reset finds D/E/F."""
    pending: dict[str, str] = {}
    blocked = {'r2grid0': set(PRODUCTS[:3])}

    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        if screen == 'r1grid0' and label == 'Product A':
            pending['return'] = 'r1grid1'
            return 'detail'
        if screen == 'r1grid1' and label == 'Product B':
            pending['return'] = 'r1grid2'
            return 'detail'
        if screen == 'r1grid2' and label == 'Product C':
            pending['return'] = 'r1grid3'
            return 'detail'
        if screen == 'r1grid3' and label == 'Next round':
            return 'r2grid0'
        if screen == 'r2grid0':
            if label in blocked[screen]:
                pending['return'] = screen
                return 'detail'
            if label == 'Product D':
                pending['return'] = 'r2grid1'
                return 'detail'
        if screen == 'r2grid1' and label == 'Product E':
            pending['return'] = 'r2grid2'
            return 'detail'
        if screen == 'r2grid2' and label == 'Product F':
            pending['return'] = 'r2grid3'
            return 'detail'
        return None

    def on_key(screen: str, key: str, _count: int) -> str | None:
        if screen == 'detail' and key == 'KEYCODE_BACK':
            return pending.pop('return')
        return None

    return _World(
        series={
            'boost': (_boost_frame(progress='0/2'),),
            'r1grid0': (_grid_frame(0),),
            'r1grid1': (_grid_frame(1),),
            'r1grid2': (_grid_frame(2),),
            'r1grid3': (_grid_frame(3, next_round=True),),
            'r2grid0': (_grid_frame(0),),
            'r2grid1': (_grid_frame(1),),
            'r2grid2': (_grid_frame(2),),
            'r2grid3': (_grid_frame(3),),
            'detail': (_detail_frame(),),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'r1grid0'},
        on_tap=on_tap,
        on_key=on_key,
    )


@pytest.fixture(scope='module')
def catalogue() -> TaskCatalogue:
    return load_task_catalogue(CATALOGUE_PATH)


@pytest.fixture(scope='module')
def corpus() -> PopupCorpus:
    return load_popup_corpus(CORPUS_PATH)


@pytest.fixture(scope='module')
def engine() -> RapidOcrEngine:
    return RapidOcrEngine()


def _load(path: Path, engine: RapidOcrEngine) -> Snapshot:
    with Image.open(path) as opened:
        image = opened.convert('RGB')
    return Snapshot(
        image=image,
        boxes=tuple(engine.read(image)),
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(image.width, image.height, density=240, sdk=35),
    )


# --- tile and counter perception ---------------------------------------------


def test_find_surprise_tiles_splits_the_two_columns_from_the_grid() -> None:
    frame = _grid_frame(0)

    tiles = find_surprise_tiles(frame)

    assert [tile.label for tile in tiles] == list(PRODUCTS)
    assert [tile.column for tile in tiles] == [0, 1, 0, 1, 0, 1]
    assert all(tile.box.width > 0 and tile.box.height > 0 for tile in tiles)


def test_find_surprise_tiles_on_the_measured_grid_does_not_return_the_header(engine: RapidOcrEngine) -> None:
    frame = _load(DATA / 'flows' / 'surprise_items_round_grid.png', engine)

    tiles = find_surprise_tiles(frame)

    assert len(tiles) >= 3
    assert all('browse surprise items' not in tile.normalized_label for tile in tiles)
    assert all('tap 3 items' not in tile.normalized_label for tile in tiles)
    assert {tile.column for tile in tiles} == {0, 1}
    assert not any(tile.label == 'WEE' for tile in tiles)
    assert any('Luxury Silicone Case' in tile.label for tile in tiles)
    merged = [tile for tile in tiles if tile.shared_label]
    assert merged, 'the measured frame OCR merges the two row-1 titles into one line'
    assert any('T9 Vintage' in tile.label for tile in merged)


def test_the_measured_counter_fixtures_read_through_the_pill_crop(engine: RapidOcrEngine) -> None:
    grid = _load(DATA / 'flows' / 'surprise_items_round_grid.png', engine)
    one = _load(DATA / 'flows' / 'surprise_items_progress_1of3.png', engine)
    two = _load(DATA / 'flows' / 'surprise_items_progress_2of3.png', engine)
    complete = _load(DATA / 'flows' / 'surprise_items_round_complete.png', engine)

    grid_reading = read_surprise_counter(grid, ocr=engine)
    assert grid_reading == CounterState(0, 3, grid_reading.box)  # type: ignore[union-attr]
    assert read_surprise_counter(one, ocr=engine).completed == 1  # type: ignore[union-attr]
    assert read_surprise_counter(two, ocr=engine).completed == 2  # type: ignore[union-attr]
    assert read_surprise_counter(complete, ocr=engine).complete  # type: ignore[union-attr]


def test_read_surprise_counter_reads_a_slashed_box_and_returns_none_without_a_digit() -> None:
    slashed = _snapshot(_box('1 / 3', 480, 440, 573, 490))
    empty = _snapshot(_box('Browse surprise items', 270, 200, 930, 270))

    assert read_surprise_counter(slashed) == CounterState(1, 3, slashed.boxes[0])
    assert read_surprise_counter(empty) is None


def test_find_next_round_and_mission_overlay_on_the_measured_frames(engine: RapidOcrEngine) -> None:
    complete = _load(DATA / 'flows' / 'surprise_items_round_complete.png', engine)
    mission = _load(DATA / 'flows' / 'surprise_items_mission_completed.png', engine)

    assert find_next_round(complete) is None
    assert find_next_round(mission) is None
    assert find_mission_overlay(complete) is None
    assert find_mission_overlay(mission) is not None


def test_find_mission_overlay_accepts_the_glued_ocr_word() -> None:
    frame = _snapshot(_box('MissionCompletedAir', 140, 1188, 1142, 1300))

    assert find_mission_overlay(frame) is not None


def test_surface_helpers_recognise_the_grid_and_detail() -> None:
    assert is_surprise_grid(_grid_frame()) is True
    assert is_item_detail(_detail_frame()) is True
    assert is_surprise_grid(_wrong_frame()) is False
    assert is_item_detail(_grid_frame()) is False


def test_find_back_control_falls_back_to_the_visible_chevron() -> None:
    frame = _snapshot(_box('<', 45, 79, 61, 101))

    assert find_back_control(frame) is not None
    assert find_back_control(_snapshot(_box('Back', 45, 79, 100, 101, score=0.1))) is None


# --- argument, state and category guards -------------------------------------


def test_run_surprise_task_rejects_a_denied_entry(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    roster = enumerate_roster(_boost_frame(), catalogue)
    denied = next(entry for entry in roster.entries if entry.denied)
    world = _World({'boost': (_boost_frame(),)}, {})

    with pytest.raises(TaskError, match='not dispatchable'):
        run_surprise_task(
            _controller(world, world.frame),
            _ForbiddenCapture(),
            corpus,
            _state(tmp_path),
            denied,
            navigate=world.navigate,
        )


def test_run_surprise_task_rejects_a_non_item_tap_category(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    roster = enumerate_roster(_boost_frame(), catalogue)
    entry = next(entry for entry in roster.entries if entry.key == 'notifications')
    world = _World({'boost': (_boost_frame(),)}, {})

    with pytest.raises(TaskError, match='not an item_tap task'):
        run_surprise_task(
            _controller(world, world.frame),
            _ForbiddenCapture(),
            corpus,
            _state(tmp_path),
            entry,
            navigate=world.navigate,
        )


@pytest.mark.parametrize(
    ('name', 'value'),
    [
        ('load_timeout', 0.0),
        ('arrival_timeout', 0.0),
        ('exit_timeout', 0.0),
        ('interval', 0.0),
        ('return_timeout', 0.0),
        ('return_interval', 0.0),
        ('return_attempts', 0),
        ('settle', -1.0),
        ('max_rounds', 0),
    ],
)
def test_run_surprise_task_rejects_invalid_parameters(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    name: str,
    value: float,
) -> None:
    world = _World({'boost': (_boost_frame(),)}, {})

    with pytest.raises(ValueError, match=r'must be positive|must not be negative'):
        run_surprise_task(
            _controller(world, world.frame),
            world.capture,
            corpus,
            _state(tmp_path),
            _entry(catalogue),
            navigate=world.navigate,
            **{name: value},  # type: ignore[arg-type]
        )


def test_run_surprise_task_skips_at_the_daily_limit(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    state.record('surprise_items')
    state.record('surprise_items')
    world = _World({'boost': (_boost_frame(),)}, {})

    result = run_surprise_task(
        _controller(world, world.frame),
        _ForbiddenCapture(),
        corpus,
        state,
        _entry(catalogue),
        navigate=world.navigate,
    )

    assert result.outcome is SurpriseOutcome.SKIPPED
    assert result.verification == 'daily_limit'
    assert result.recorded is False


def test_run_surprise_task_skips_a_vanished_row(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    frame = _snapshot(
        _box('Coins boost', 80, 90, 400, 150),
        _box('Turn on notifications', 60, 800, 500, 850),
        _box('Stay informed when rewards drop', 60, 860, 500, 900),
        _box('GO', 1000, 860, 1080, 910),
    )
    world = _World({'boost': (frame,)}, {})

    result = run_surprise_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
    )

    assert result.outcome is SurpriseOutcome.SKIPPED
    assert result.verification == 'row_vanished'


def test_run_surprise_task_skips_a_row_with_no_action(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    frame = _snapshot(
        _box('Coins boost', 80, 90, 400, 150),
        _box(SURPRISE_TITLE, 60, 420, 700, 470),
        _box('Tap 3 items on this page to earn coins', 60, 480, 700, 530),
        _box('0/2', 1000, 480, 1080, 530),
    )
    world = _World({'boost': (frame,)}, {})

    result = run_surprise_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
    )

    assert result.outcome is SurpriseOutcome.SKIPPED
    assert result.verification == 'row_complete'


def test_run_surprise_task_skips_when_the_row_badge_has_no_rounds_left(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World({'boost': (_boost_frame(progress='2/2'),)}, {})

    result = run_surprise_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue, progress='2/2'),
        navigate=world.navigate,
    )

    assert result.outcome is SurpriseOutcome.SKIPPED
    assert result.verification == 'row_complete'


# --- the live-shaped flow -----------------------------------------------------


def test_run_surprise_task_completes_one_round_and_returns_from_every_detail(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _success_world(progress='1/2')
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state)

    assert result.outcome is SurpriseOutcome.COMPLETED
    assert result.verification == 'counter_complete'
    assert result.rounds == 1
    assert result.round_records[0].counter_change == '0/3 -> 3/3'
    assert result.round_records[0].delta == 3
    assert result.rule_path == RULE_PATH_REUSE
    assert result.recorded is True
    assert state.count('surprise_items') == 1

    # GO plus three product tiles. Mission-overlay taps and BACK are separate.
    assert [tap.label for tap in result.taps if tap.label in PRODUCTS] == ['Product A', 'Product B', 'Product C']
    assert result.taps[0].label == 'GO'
    assert world.keys == ['KEYCODE_BACK'] * 3
    assert all(key.key == 'KEYCODE_BACK' for key in result.keys)
    assert world.current == 'coins'
    assert result.returned is True

    payload = result.as_dict()
    assert payload['outcome'] == 'completed'
    assert payload['rounds'] == 1
    assert payload['counter_deltas'] == ['0/3 -> 3/3']
    assert payload['rule_path'] == 'reuse'


def test_run_surprise_task_handles_the_mission_overlay_before_next_round(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _success_world(progress='0/2', next_round=False)
    # The world's grid3 is the only frame with an overlay; make it the round end.
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state, max_rounds=1)

    assert result.outcome is SurpriseOutcome.COMPLETED
    assert any(tap.label == 'Mission Completed' for tap in result.taps)
    assert result.returned is True


def test_run_surprise_task_walks_two_rounds_and_resets_only_when_reuse_stalls(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _two_round_world()
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state)

    assert result.outcome is SurpriseOutcome.COMPLETED
    assert result.rounds == 2
    assert [record.counter_change for record in result.round_records] == ['0/3 -> 3/3', '0/3 -> 3/3']
    assert result.round_records[0].rule_path == RULE_PATH_REUSE
    assert result.round_records[1].rule_path == RULE_PATH_RESET
    assert result.rule_path == RULE_PATH_RESET
    assert result.recorded is True
    assert state.count('surprise_items') == 2

    reset_round = result.round_records[1]
    assert reset_round.items == ('Product D', 'Product E', 'Product F')
    assert 'Product A' not in reset_round.items
    assert 'Product B' not in reset_round.items
    assert world.current == 'coins'


def test_run_surprise_task_uses_the_visible_chevron_when_back_is_unavailable(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    frame = _detail_frame()
    frame = _snapshot(*frame.boxes, _box('<', 45, 79, 61, 101))
    pending: dict[str, str] = {}

    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        if screen == 'grid0' and label == 'Product A':
            pending['next'] = 'grid1'
            return 'detail'
        if screen == 'grid1' and label == 'Product B':
            pending['next'] = 'grid2'
            return 'detail'
        if screen == 'grid2' and label == 'Product C':
            pending['next'] = 'grid3'
            return 'detail'
        if screen == 'detail' and label == '<':
            return pending.pop('next')
        return None

    world = _World(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'grid0': (_grid_frame(0),),
            'grid1': (_grid_frame(1),),
            'grid2': (_grid_frame(2),),
            'grid3': (_grid_frame(3, mission=True),),
            'detail': (frame,),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'grid0'},
        on_tap=on_tap,
    )
    controller = Controller.from_frame(_NoKeyDevice(world), world.frame, rng=random.Random(0))

    result = _run(world, catalogue, corpus, _state(tmp_path), controller=controller)

    assert result.outcome is SurpriseOutcome.COMPLETED
    assert result.keys == ()
    assert [tap.label for tap in result.taps if tap.label in PRODUCTS] == ['Product A', 'Product B', 'Product C']
    assert any(tap.label == '<' for tap in result.taps)


# --- honest failure paths -----------------------------------------------------


def test_run_surprise_task_fails_when_the_grid_never_arrives(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'wrong': (_wrong_frame(),),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'wrong'},
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is SurpriseOutcome.FAILED
    assert result.verification == 'grid_not_arrived'
    assert result.recorded is False
    assert result.returned is True


def test_run_surprise_task_fails_without_tapping_when_the_counter_is_unreadable(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'grid': (_grid_frame(None),),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'grid'},
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is SurpriseOutcome.FAILED
    assert result.verification == 'counter_unreadable'
    assert len(world.taps) == 1  # only the row's GO
    assert result.recorded is False


def test_run_surprise_task_fails_when_the_grid_disappears_without_a_counter_move(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'grid': (_grid_frame(0),),
            'wrong': (_wrong_frame(),),
            'coins': (_coins_frame(),),
        },
        transitions={
            ('boost', 'GO'): 'grid',
            ('grid', 'Product A'): 'wrong',
        },
    )

    result = _run(world, catalogue, corpus, _state(tmp_path), arrival_timeout=0.5)

    assert result.outcome is SurpriseOutcome.FAILED
    assert result.verification == 'grid_disappeared'
    assert result.recorded is False
    assert result.round_records[0].complete is False


def test_run_surprise_task_fails_when_next_round_does_not_reach_a_fresh_grid(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    pending: dict[str, str] = {}

    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        if screen in ('grid0', 'grid1', 'grid2') and label in PRODUCTS[:3]:
            pending['return'] = f'grid{int(screen[-1]) + 1}'
            return 'detail'
        if screen == 'grid3' and label == 'Next round':
            return 'wrong'
        return None

    def on_key(screen: str, key: str, _count: int) -> str | None:
        if screen == 'detail' and key == 'KEYCODE_BACK':
            return pending.pop('return')
        return None

    world = _World(
        series={
            'boost': (_boost_frame(progress='0/2'),),
            'grid0': (_grid_frame(0),),
            'grid1': (_grid_frame(1),),
            'grid2': (_grid_frame(2),),
            'grid3': (_grid_frame(3, next_round=True),),
            'detail': (_detail_frame(),),
            'wrong': (_wrong_frame(),),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'grid0'},
        on_tap=on_tap,
        on_key=on_key,
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is SurpriseOutcome.FAILED
    assert result.verification == 'next_round_not_arrived'
    assert result.rounds == 1
    assert result.recorded is False


def test_run_surprise_task_fails_when_it_cannot_return_to_the_coins_page(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _success_world(progress='1/2')

    def navigate(_route: SurfaceRoute) -> None:
        world.navigations.append(_route)
        world.go('wrong')

    result = run_surprise_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=cast(RouteFirer, navigate),
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=0.5,
        return_interval=0.5,
        clock=_SteppingClock(step=1.0),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SurpriseOutcome.FAILED
    assert result.verification == 'return_failed'
    assert result.returned is False


def test_run_surprise_task_stops_without_a_completion_on_the_redeem_interstitial(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    redeem = _snapshot(
        _box('YOU CAN NOW REDEEM!', 80, 700, 1120, 780),
        _box('Coins', 101, 194, 254, 242),
        _box('Daily coins', 66, 919, 320, 978),
    )
    world = _World(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'grid': (_grid_frame(0),),
            'redeem': (redeem,),
            'coins': (_coins_frame(),),
        },
        transitions={
            ('boost', 'GO'): 'grid',
            ('grid', 'Product A'): 'redeem',
        },
    )

    result = _run(world, catalogue, corpus, _state(tmp_path), arrival_timeout=0.5)

    assert result.outcome is SurpriseOutcome.FAILED
    assert result.verification == 'redeem_interstitial'
    assert result.stop == 'redeem_interstitial'
    assert result.recorded is False


def test_run_surprise_task_reports_return_failed_when_an_early_failure_cannot_return(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'wrong': (_wrong_frame(),),
        },
        transitions={('boost', 'GO'): 'wrong'},
    )

    def navigate(_route: SurfaceRoute) -> None:
        world.navigations.append(_route)
        world.go('wrong')

    result = run_surprise_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=cast(RouteFirer, navigate),
        arrival_timeout=0.5,
        return_timeout=0.5,
        return_interval=0.5,
        clock=_SteppingClock(step=1.0),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SurpriseOutcome.FAILED
    assert result.verification == 'return_failed'


def test_run_surprise_task_reports_no_observation_when_no_round_ran(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    """A grid that cannot produce a tile is a failure, never a completion."""
    frame = _snapshot(
        _box(SURPRISE_TITLE, 270, 200, 930, 270),
        _box('Tap 3 items on this page to earn coins', 253, 332, 946, 384),
        _box('0/3', 480, 440, 573, 490),
    )
    world = _World(
        series={
            'boost': (_boost_frame(progress='0/2'),),
            'grid': (frame,),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'grid'},
    )

    result = _run(world, catalogue, corpus, _state(tmp_path), max_rounds=1)

    assert result.outcome is SurpriseOutcome.FAILED
    assert result.verification == 'counter_stalled'
    assert result.recorded is False


# --- result rendering and small helpers --------------------------------------


def test_surprise_result_as_dict_renders_the_round_evidence() -> None:
    record = RoundRecord(1, 0, 3, 3, 3, RULE_PATH_RESET, True, 'done', items=('A', 'B', 'C'))
    result = SurpriseResult(
        key='surprise_items',
        title=SURPRISE_TITLE,
        outcome=SurpriseOutcome.COMPLETED,
        verification='counter_complete',
        detail='detail',
        limit=2,
        done_today_before=0,
        done_today_after=1,
        rounds=1,
        round_records=(record,),
        rule_path=RULE_PATH_RESET,
        items=('A', 'B', 'C'),
        recorded=True,
    )

    assert result.completed is True
    assert result.failed is False
    assert result.skipped is False
    assert result.returned is False
    assert result.counter_deltas == ('0/3 -> 3/3',)
    assert record.delta == 3

    payload = result.as_dict()
    assert payload['outcome'] == 'completed'
    assert payload['rounds'] == 1
    assert payload['rule_path'] == 'reset'
    assert payload['counter_deltas'] == ['0/3 -> 3/3']
    assert payload['round_records'][0]['complete'] is True  # type: ignore[index]
    assert payload['taps'] == []
    assert payload['keys'] == []


def test_counter_state_renders_its_reading() -> None:
    box = _box('2/3', 480, 440, 573, 490)
    state = CounterState(2, 3, box)

    assert state.complete is False
    assert state.text == '2/3'
    assert state.as_dict()['reading'] == '2/3'


def test_round_record_without_a_reading_reports_unknown_counters() -> None:
    record = RoundRecord(1, None, None, 3, 0, RULE_PATH_REUSE, False, 'unreadable')

    assert record.delta is None
    assert record.counter_change == '?/3 -> ?/3'


def test_find_surprise_tiles_returns_an_empty_tuple_without_text() -> None:
    assert find_surprise_tiles(_snapshot()) == ()


def test_surprise_tile_low_score_boxes_are_not_tiles() -> None:
    frame = _snapshot(_box('Product A', 100, 600, 500, 650, score=0.1))

    assert find_surprise_tiles(frame) == ()


def test_surprise_round_bound_is_the_measured_two() -> None:
    assert MAX_ROUNDS == 2


# --- early surface states and the internal waits -----------------------------


def test_run_surprise_task_fails_and_returns_when_the_entry_never_settles(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    loading = _snapshot(_box('Loading', 100, 100, 300, 150))
    world = _World(
        series={'loading': (loading,), 'coins': (_coins_frame(),)},
        transitions={},
        start='loading',
    )

    result = run_surprise_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        load_timeout=0.5,
        return_timeout=1.0,
        return_interval=0.5,
        clock=_SteppingClock(step=1.0),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SurpriseOutcome.FAILED
    assert result.verification == 'not_settled'
    assert result.returned is True


def test_run_surprise_task_fails_and_returns_on_a_non_task_surface(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={'wrong': (_wrong_frame(),), 'coins': (_coins_frame(),)},
        transitions={},
        start='wrong',
    )

    result = run_surprise_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        load_timeout=0.5,
        return_timeout=1.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SurpriseOutcome.FAILED
    assert result.verification == 'task_surface_unavailable'
    assert result.returned is True


def test_run_surprise_task_stops_when_the_redeem_interstitial_follows_go(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    redeem = _snapshot(
        _box('YOU CAN NOW REDEEM!', 80, 700, 1120, 780),
        _box('Coins', 101, 194, 254, 242),
        _box('Daily coins', 66, 919, 320, 978),
    )
    world = _World(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'redeem': (redeem,),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'redeem'},
    )

    result = _run(world, catalogue, corpus, _state(tmp_path), arrival_timeout=0.5)

    assert result.outcome is SurpriseOutcome.FAILED
    assert result.verification == 'redeem_interstitial'
    assert result.stop == 'redeem_interstitial'
    assert result.returned is True


def test_run_surprise_task_records_a_round_that_was_already_complete(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'grid': (_grid_frame(3),),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'grid'},
    )

    result = _run(world, catalogue, corpus, _state(tmp_path), max_rounds=1)

    assert result.outcome is SurpriseOutcome.COMPLETED
    assert result.round_records[0].complete is True
    assert result.round_records[0].taps == 0
    assert result.recorded is True


def test_run_surprise_task_fails_when_back_has_no_key_and_no_visible_control(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'grid': (_grid_frame(0),),
            'detail': (_detail_frame(),),
            'coins': (_coins_frame(),),
        },
        transitions={
            ('boost', 'GO'): 'grid',
            ('grid', 'Product A'): 'detail',
        },
    )
    controller = Controller.from_frame(_NoKeyDevice(world), world.frame, rng=random.Random(0))

    result = _run(world, catalogue, corpus, _state(tmp_path), controller=controller)

    assert result.outcome is SurpriseOutcome.FAILED
    assert result.verification == 'back_not_reached'
    assert result.recorded is False


def test_run_surprise_task_returns_before_re_raising_an_unclosable_popup(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    popup = _snapshot(
        _box('Tap to claim', 80, 700, 600, 780),
        _box('Wireless Earbuds', 60, 400, 500, 450),
        _box('C$12.99', 60, 460, 300, 510),
    )
    world = _World(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'grid': (_grid_frame(0),),
            'popup': (popup,),
            'coins': (_coins_frame(),),
        },
        transitions={
            ('boost', 'GO'): 'grid',
            ('grid', 'Product A'): 'popup',
        },
    )

    def navigate(_route: SurfaceRoute) -> None:
        world.navigations.append(_route)
        raise OSError('adb route unavailable')

    with pytest.raises(PopupUnclosable):
        run_surprise_task(
            _controller(world, world.frame),
            world.capture,
            corpus,
            _state(tmp_path),
            _entry(catalogue),
            navigate=cast(RouteFirer, navigate),
            arrival_timeout=0.5,
            return_timeout=0.5,
            return_interval=0.5,
            return_attempts=1,
            clock=_SteppingClock(step=1.0),
            sleep=_SteppingClock().sleep,
        )

    assert world.navigations


def test_surprise_surface_settle_handles_loading_popup_and_overlay(
    corpus: PopupCorpus,
    engine: RapidOcrEngine,
) -> None:
    from aliexpress_coins.tasks.surprise import _settle_surface

    loading = _snapshot(_box('Loading', 100, 100, 300, 150))
    boost = _boost_frame()
    launch = _load(DATA / 'popups' / 'launch_welcome_gifts.png', engine)
    mission = _snapshot(_box('Mission Completed', 200, 1450, 1000, 1550))
    clock = _SteppingClock(step=0.5)

    def run(
        frames: Sequence[Snapshot],
        on_tap: Callable[[str, str | None, int], str | None] | None = None,
    ) -> tuple[Snapshot, bool, DismissReport | None]:
        world = _World({'frames': tuple(frames), 'boost': (boost,)}, {}, start='frames', on_tap=on_tap)
        controller = _controller(world, world.frame)
        return _settle_surface(
            controller,
            world.capture,
            corpus,
            world.frame,
            templates=None,
            timeout=3.0,
            interval=0.5,
            settle=0.0,
            clock=clock,
            sleep=clock.sleep,
            logger=_logger(),
        )

    _, settled, dismiss = run((loading, boost))
    assert settled is True
    assert dismiss is None

    _, settled, dismiss = run((launch, boost), on_tap=lambda _screen, _label, _count: 'boost')
    assert settled is True
    assert dismiss is not None
    assert 'launch_welcome_gifts' in dismiss.closed

    frame, settled, dismiss = run((mission,))
    assert frame is mission
    assert settled is True
    assert dismiss is None


def test_surprise_surface_settle_times_out_on_loading_and_popup(
    corpus: PopupCorpus,
    engine: RapidOcrEngine,
) -> None:
    from aliexpress_coins.tasks.surprise import _settle_surface

    loading = _snapshot(_box('Loading', 100, 100, 300, 150))
    launch = _load(DATA / 'popups' / 'launch_welcome_gifts.png', engine)
    clock = _SteppingClock(step=1.0)

    def run(frame: Snapshot) -> tuple[bool, DismissReport | None]:
        world = _World({'frames': (frame, frame)}, {}, start='frames')
        _, settled, dismiss = _settle_surface(
            _controller(world, world.frame),
            world.capture,
            corpus,
            world.frame,
            templates=None,
            timeout=0.5,
            interval=0.5,
            settle=0.0,
            clock=clock,
            sleep=clock.sleep,
            logger=_logger(),
        )
        return settled, dismiss

    settled, dismiss = run(loading)
    assert settled is False
    assert dismiss is None

    settled, dismiss = run(launch)
    assert settled is False or dismiss is not None


def test_surprise_surface_settle_times_out_inside_the_loading_wait(corpus: PopupCorpus) -> None:
    from aliexpress_coins.tasks.surprise import _settle_surface

    loading = _snapshot(_box('Loading', 100, 100, 300, 150))
    world = _World({'frames': (loading,), 'boost': (_boost_frame(),)}, {}, start='frames')
    clock = _SteppingClock(step=0.25)

    frame, settled, dismiss = _settle_surface(
        _controller(world, world.frame),
        world.capture,
        corpus,
        world.frame,
        templates=None,
        timeout=1.0,
        interval=0.5,
        settle=0.0,
        clock=clock,
        sleep=clock.sleep,
        logger=_logger(),
    )

    assert settled is False
    assert dismiss is None
    assert frame is loading


def test_surprise_surface_settle_returns_the_popup_report_on_timeout(
    corpus: PopupCorpus,
    engine: RapidOcrEngine,
) -> None:
    from aliexpress_coins.tasks.surprise import _settle_surface

    launch = _load(DATA / 'popups' / 'launch_welcome_gifts.png', engine)
    clock = _SteppingClock(step=1.0)
    world = _World({'frames': (launch,)}, {}, start='frames')

    _, settled, dismiss = _settle_surface(
        _controller(world, world.frame),
        world.capture,
        corpus,
        world.frame,
        templates=None,
        timeout=3.0,
        interval=0.5,
        settle=0.0,
        clock=clock,
        sleep=clock.sleep,
        logger=_logger(),
    )

    assert settled is False
    assert dismiss is not None


def test_surprise_wait_for_surface_covers_popup_loading_timeout_and_overlay(
    corpus: PopupCorpus,
    engine: RapidOcrEngine,
) -> None:
    from aliexpress_coins.tasks.surprise import _wait_for_surface

    loading = _snapshot(_box('Loading', 100, 100, 300, 150))
    launch = _load(DATA / 'popups' / 'launch_welcome_gifts.png', engine)
    mission = _snapshot(_box('Mission Completed', 200, 1450, 1000, 1550))
    boost = _boost_frame()
    clock = _SteppingClock(step=0.5)

    def wait(frames: Sequence[Snapshot], target: Callable[[Snapshot], bool]) -> tuple[Snapshot, bool, object, object]:
        world = _World({'frames': tuple(frames)}, {}, start='frames')
        return _wait_for_surface(
            _controller(world, world.frame),
            world.capture,
            corpus,
            target,
            timeout=1.0,
            interval=0.5,
            settle=0.0,
            clock=clock,
            sleep=clock.sleep,
            logger=_logger(),
            templates=None,
        )

    frame, arrived, dismiss, stop = wait((loading, boost), lambda frame: frame is boost)
    assert arrived is True
    assert dismiss is None
    assert stop is None

    frame, arrived, dismiss, stop = wait((launch, boost), lambda frame: frame is boost)
    assert arrived is True
    assert dismiss is not None
    assert stop is None

    frame, arrived, _, _ = wait((mission, boost), lambda frame: frame is boost)
    assert arrived is True
    assert frame is boost

    frame, arrived, _, _ = wait((boost,), lambda frame: False)
    assert arrived is False
    assert frame is boost


def test_surprise_private_helpers_cover_the_bounded_paths() -> None:
    from aliexpress_coins.tasks.surprise import (
        MAX_SCROLLS,
        _failure_verification,
        _scroll,
    )

    world = _World({'grid': (_grid_frame(0),)}, {})
    controller = _controller(world, world.frame)
    frame = world.frame

    assert _scroll(controller, frame, lambda snapshot: False, 0) == (False, None)
    assert _scroll(controller, frame, lambda snapshot: True, 0) == (True, None)
    assert _scroll(controller, frame, None, MAX_SCROLLS) == (False, None)
    scrolled, record = _scroll(controller, frame, None, 0)
    assert scrolled is True
    assert record is not None
    assert world.swipes

    no_swipe = Controller.from_frame(cast(Any, _TapOnlyDevice()), frame, rng=random.Random(0))
    assert _scroll(no_swipe, frame, None, 0) == (False, None)

    cases = {
        'the round counter could not be read': 'counter_unreadable',
        'the grid disappeared without the counter advancing; no completion was recorded': 'grid_disappeared',
        'BACK did not return to the grid; no completion was recorded': 'back_not_reached',
        'the reward interstitial appeared while returning from a product': 'redeem_interstitial',
        'no unopened product tile was available and the grid could not be scrolled further': 'counter_stalled',
        'something else': 'counter_stalled',
    }
    for detail, expected in cases.items():
        assert _failure_verification(RoundRecord(1, 0, 0, 3, 0, RULE_PATH_REUSE, False, detail)) == expected


class _TapOnlyDevice:
    def tap(self, x: int, y: int) -> None:
        del x, y


def test_surprise_tile_identity_and_dict_cover_short_and_shared_labels() -> None:
    short = _snapshot(_box('W', 100, 600, 500, 650))
    shared = _snapshot(_box('A merged title across both columns', 100, 600, 1100, 650))

    short_tile = find_surprise_tiles(short)[0]
    shared_tiles = find_surprise_tiles(shared)

    assert short_tile.label_key == ('label', 'w', 0)
    assert short_tile.position_key[0] == 'position'
    assert short_tile.as_dict()['label'] == 'W'
    assert len(short_tile.keys) == 2
    assert any(tile.shared_label for tile in shared_tiles)
    assert shared_tiles[0].label_key[2] == 0


def test_find_surprise_tiles_skips_non_product_and_out_of_grid_lines() -> None:
    frame = _snapshot(
        _box('Browse surprise items', 270, 200, 930, 270),
        _box('Tap 3 items on this page to earn coins', 253, 332, 946, 384),
        _box('Product C', 100, 500, 500, 550),
        _box('C$12.99', 100, 600, 500, 650),
        _box('Free shipping', 690, 600, 1100, 650),
        _box('Black Friday', 100, 900, 500, 950),
        _box('Product A', 100, 1900, 500, 1950),
        _box('1/3', 100, 1200, 200, 1250),
        _box('123', 100, 1400, 200, 1450),
        _box('Product B', 100, 1800, 500, 1850),
    )

    tiles = find_surprise_tiles(frame)

    assert [tile.label for tile in tiles] == ['Product C']


def test_read_surprise_counter_rejects_a_zero_total_and_a_too_large_digit() -> None:
    zero = _snapshot(_box('0/0', 480, 440, 573, 490))
    too_large = _snapshot(_box('9', 480, 440, 573, 490))

    assert read_surprise_counter(zero) is None
    assert read_surprise_counter(too_large) is None


def test_surprise_result_and_counter_render_missing_evidence() -> None:
    no_box = CounterState(0, 3)
    result = SurpriseResult(
        key='surprise_items',
        title=SURPRISE_TITLE,
        outcome=SurpriseOutcome.FAILED,
        verification='no_observation',
        detail='detail',
        limit=2,
        done_today_before=0,
        done_today_after=0,
        stop='redeem_interstitial',
    )

    assert no_box.as_dict() == {'completed': 0, 'total': 3, 'text': '0/3', 'complete': False}
    assert result.interstitial == 'redeem_interstitial'
    assert result.counter_deltas == ()
    assert result.as_dict()['return'] is None


def _logger() -> logging.Logger:
    return logging.getLogger('test.surprise')


def test_surprise_text_helpers_ignore_low_score_boxes() -> None:
    low_next = _snapshot(_box('Next round', 500, 1700, 700, 1760, score=0.1))
    low_mission = _snapshot(_box('Mission Completed', 200, 1450, 1000, 1550, score=0.1))

    assert find_next_round(low_next) is None
    assert find_mission_overlay(low_mission) is None


def test_find_surprise_tiles_skips_a_sliver_and_a_zero_height_line() -> None:
    frame = _snapshot(
        _box('Sliver Title', 590, 600, 690, 650),
        _box('Zero Height Title', 100, 800, 500, 800),
        _box('Wholly Left Title', 100, 900, 500, 950),
    )

    tiles = find_surprise_tiles(frame)

    assert [tile.label for tile in tiles] == ['Sliver Title', 'Wholly Left Title']
    assert all(tile.box.width > 0 for tile in tiles)


def test_run_surprise_task_appends_an_entry_popup_sweep(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from aliexpress_coins.tasks import surprise as surprise_module

    frame = _boost_frame(progress='2/2')
    world = _World({'boost': (frame,)}, {})

    def settle(*_args: object, **_kwargs: object) -> tuple[Snapshot, bool, object]:
        return frame, True, object()

    monkeypatch.setattr(surprise_module, '_settle_surface', settle)

    result = _run(
        world,
        catalogue,
        corpus,
        _state(tmp_path),
        entry=_entry(catalogue, progress='2/2'),
    )

    assert result.outcome is SurpriseOutcome.SKIPPED
    assert result.verification == 'row_complete'


def test_run_surprise_task_sweeps_a_popup_on_arrival_to_the_grid(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    engine: RapidOcrEngine,
    tmp_path: Path,
) -> None:
    launch = _load(DATA / 'popups' / 'launch_welcome_gifts.png', engine)
    world = _World(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'popup': (launch,),
            'grid': (_grid_frame(3),),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'popup'},
        on_tap=lambda screen, _label, _count: 'grid' if screen == 'popup' else None,
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is SurpriseOutcome.COMPLETED
    assert result.dismissals
    assert result.round_records[0].complete is True


def test_run_surprise_task_stops_when_no_next_round_is_offered(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _success_world(progress='0/2', next_round=False)

    result = _run(world, catalogue, corpus, _state(tmp_path), max_rounds=2)

    assert result.outcome is SurpriseOutcome.COMPLETED
    assert result.rounds == 1
    assert result.returned is True


def test_run_surprise_task_scrolls_for_new_products_before_giving_up(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    bare = _snapshot(
        _box(SURPRISE_TITLE, 270, 200, 930, 270),
        _box('Tap 3 items on this page to earn coins', 253, 332, 946, 384),
        _box('0/3', 480, 440, 573, 490),
    )
    world = _World(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'grid': (bare,),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'grid'},
    )

    result = _run(world, catalogue, corpus, _state(tmp_path), max_rounds=1, scroll=lambda _frame: True)

    assert result.outcome is SurpriseOutcome.FAILED
    assert result.verification == 'counter_stalled'
    assert result.round_records[0].scrolls == 1


def test_run_surprise_task_uses_an_injected_back_seam(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _success_world(progress='1/2')
    backs: list[int] = []

    def back() -> None:
        backs.append(1)
        world.keyevent('KEYCODE_BACK')

    result = _run(world, catalogue, corpus, _state(tmp_path), back=back)

    assert result.outcome is SurpriseOutcome.COMPLETED
    assert len(backs) == 3
    assert result.keys == ()


def test_run_surprise_task_settles_the_mission_overlay_with_a_dwell(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _success_world(progress='1/2')

    result = _run(world, catalogue, corpus, _state(tmp_path), settle=0.5)

    assert result.outcome is SurpriseOutcome.COMPLETED


def test_run_surprise_task_fails_returning_after_an_early_redeem_overlay(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    redeem = _snapshot(
        _box('YOU CAN NOW REDEEM!', 80, 700, 1120, 780),
        _box('Coins', 101, 194, 254, 242),
        _box('Daily coins', 66, 919, 320, 978),
    )
    world = _World(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'redeem': (redeem,),
            'wrong': (_wrong_frame(),),
        },
        transitions={('boost', 'GO'): 'redeem'},
    )

    def navigate(_route: SurfaceRoute) -> None:
        world.navigations.append(_route)
        world.go('wrong')

    result = run_surprise_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=cast(RouteFirer, navigate),
        arrival_timeout=0.5,
        return_timeout=0.5,
        return_interval=0.5,
        clock=_SteppingClock(step=1.0),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SurpriseOutcome.FAILED
    assert result.verification == 'return_failed'
    assert result.stop == 'redeem_interstitial'


def test_surprise_wait_for_surface_times_out_on_a_loading_frame(corpus: PopupCorpus) -> None:
    from aliexpress_coins.tasks.surprise import _wait_for_surface

    loading = _snapshot(_box('Loading', 100, 100, 300, 150))
    world = _World({'frames': (loading,)}, {}, start='frames')
    clock = _SteppingClock(step=1.0)

    frame, arrived, dismiss, stop = _wait_for_surface(
        _controller(world, world.frame),
        world.capture,
        corpus,
        lambda _frame: False,
        timeout=0.5,
        interval=0.5,
        settle=0.0,
        clock=clock,
        sleep=clock.sleep,
        logger=_logger(),
        templates=None,
    )

    assert arrived is False
    assert dismiss is None
    assert stop is None
    assert frame is loading


def test_surprise_settle_returns_early_on_the_redeem_overlay(corpus: PopupCorpus) -> None:
    from aliexpress_coins.tasks.surprise import _settle_surface

    redeem = _snapshot(
        _box('YOU CAN NOW REDEEM!', 80, 700, 1120, 780),
        _box('Coins', 101, 194, 254, 242),
        _box('Daily coins', 66, 919, 320, 978),
    )
    world = _World({'frames': (redeem,)}, {}, start='frames')
    clock = _SteppingClock(step=1.0)

    frame, settled, dismiss = _settle_surface(
        _controller(world, world.frame),
        world.capture,
        corpus,
        world.frame,
        templates=None,
        timeout=3.0,
        interval=0.5,
        settle=0.0,
        clock=clock,
        sleep=clock.sleep,
        logger=_logger(),
    )

    assert frame is redeem
    assert settled is False
    assert dismiss is None


def test_surprise_private_capture_and_return_helpers_cover_objects_and_errors(
    corpus: PopupCorpus,
) -> None:
    from aliexpress_coins.tasks.surprise import _capture, _return_quietly

    frame = _grid_frame(0)
    world = _World({'grid': (frame,)}, {})

    class _Source:
        def capture(self) -> Snapshot:
            return frame

    assert _capture(_Source()) is frame
    assert _capture(lambda: frame) is frame

    def navigate(_route: SurfaceRoute) -> None:
        raise OSError('route unavailable')

    _return_quietly(
        _controller(world, frame),
        _ForbiddenCapture(),
        corpus,
        cast(RouteFirer, navigate),
        templates=None,
        route=SurfaceRoute.SHORTCUT,
        timeout=0.5,
        interval=0.5,
        attempts=1,
        settle=0.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        logger=_logger(),
    )


def test_surprise_run_round_reports_an_unreadable_counter() -> None:
    from aliexpress_coins.tasks.surprise import _run_round, _TriedTiles

    frame = _grid_frame(None)
    world = _World({'grid': (frame,)}, {})
    catalogue = load_task_catalogue(CATALOGUE_PATH)
    entry = _entry(catalogue)
    spec = entry.spec
    assert spec is not None

    record, current, stop = _run_round(
        _controller(world, frame),
        world.capture,
        load_popup_corpus(CORPUS_PATH),
        frame,
        1,
        spec=spec,
        task_tried=_TriedTiles(),
        templates=None,
        arrival_timeout=0.5,
        exit_timeout=0.5,
        interval=0.5,
        settle=0.0,
        back=None,
        scroll=None,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        logger=_logger(),
        taps=[],
        keys=[],
        swipes=[],
        dismissals=[],
    )

    assert record.complete is False
    assert record.counter_after is None
    assert current is frame
    assert stop is None


def _many_tile_grid() -> Snapshot:
    boxes = [
        _box(SURPRISE_TITLE, 270, 200, 930, 270),
        _box('Tap 3 items on this page to earn coins', 253, 332, 946, 384),
        _box('0/3', 480, 440, 573, 490),
    ]
    for index in range(14):
        column = index % 2
        row = index // 2
        left = 90.0 if column == 0 else 690.0
        top = 520.0 + 170.0 * row
        boxes.append(_box(f'Product {index}', left, top, left + 400, top + 55))
    return _snapshot(*boxes)


def test_run_surprise_task_stops_at_the_round_tap_budget(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    from aliexpress_coins.tasks.surprise import MAX_TAPS_PER_ROUND

    world = _World(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'grid': (_many_tile_grid(),),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'grid'},
    )

    result = _run(
        world,
        catalogue,
        corpus,
        _state(tmp_path),
        max_rounds=1,
        arrival_timeout=0.2,
        scroll=lambda _frame: True,
    )

    assert result.outcome is SurpriseOutcome.FAILED
    assert result.verification == 'counter_stalled'
    assert result.round_records[0].taps == MAX_TAPS_PER_ROUND


def test_run_surprise_task_fails_when_back_returns_to_the_wrong_page(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'grid': (_grid_frame(0),),
            'detail': (_detail_frame(),),
            'wrong': (_wrong_frame(),),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'grid', ('grid', 'Product A'): 'detail'},
        on_key=lambda screen, _key, _count: 'wrong' if screen == 'detail' else None,
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is SurpriseOutcome.FAILED
    assert result.verification == 'back_not_reached'
    assert result.recorded is False


def test_run_surprise_task_sweeps_popups_on_the_detail_and_return_waits(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    engine: RapidOcrEngine,
    tmp_path: Path,
) -> None:
    launch = _load(DATA / 'popups' / 'launch_welcome_gifts.png', engine)
    pending: dict[str, str] = {}

    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        if screen == 'grid0' and label == 'Product A':
            return 'popup_detail'
        if screen == 'popup_detail':
            return 'detail'
        if screen == 'popup_return':
            return 'grid1'
        if screen == 'grid1' and label == 'Product B':
            pending['return'] = 'grid2'
            return 'detail'
        if screen == 'grid2' and label == 'Product C':
            pending['return'] = 'grid3'
            return 'detail'
        return None

    def on_key(screen: str, _key: str, _count: int) -> str | None:
        if screen == 'detail':
            return pending.pop('return', 'popup_return')
        return None

    world = _World(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'grid0': (_grid_frame(0),),
            'grid1': (_grid_frame(1),),
            'grid2': (_grid_frame(2),),
            'grid3': (_grid_frame(3, mission=True),),
            'popup_detail': (launch,),
            'popup_return': (launch,),
            'detail': (_detail_frame(),),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'grid0'},
        on_tap=on_tap,
        on_key=on_key,
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is SurpriseOutcome.COMPLETED
    assert len(result.dismissals) >= 2
    assert result.round_records[0].complete is True


def test_run_surprise_task_stops_when_the_return_reveals_the_redeem_overlay(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    redeem = _snapshot(
        _box('YOU CAN NOW REDEEM!', 80, 700, 1120, 780),
        _box('Coins', 101, 194, 254, 242),
        _box('Daily coins', 66, 919, 320, 978),
    )
    mission = _snapshot(
        _box(SURPRISE_TITLE, 270, 200, 930, 270),
        _box('Tap 3 items on this page to earn coins', 253, 332, 946, 384),
        _box('0/3', 480, 440, 573, 490),
        _box('Mission Completed', 200, 1450, 1000, 1550),
    )
    world = _World(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'grid': (_grid_frame(0),),
            'detail': (_detail_frame(),),
            'mission': (mission,),
            'redeem': (redeem,),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'grid', ('grid', 'Product A'): 'detail'},
        on_key=lambda screen, _key, _count: 'mission' if screen == 'detail' else None,
        on_tap=lambda screen, _label, _count: 'redeem' if screen == 'mission' else None,
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is SurpriseOutcome.FAILED
    assert result.verification == 'redeem_interstitial'
    assert result.stop == 'redeem_interstitial'


def test_surprise_run_round_stops_when_the_mission_leads_to_redeem() -> None:
    from aliexpress_coins.tasks.surprise import _run_round, _TriedTiles

    mission = _snapshot(
        _box(SURPRISE_TITLE, 270, 200, 930, 270),
        _box('Tap 3 items on this page to earn coins', 253, 332, 946, 384),
        _box('0/3', 480, 440, 573, 490),
        _box('Mission Completed', 200, 1450, 1000, 1550),
    )
    redeem = _snapshot(
        _box('YOU CAN NOW REDEEM!', 80, 700, 1120, 780),
        _box('Coins', 101, 194, 254, 242),
        _box('Daily coins', 66, 919, 320, 978),
    )
    world = _World(
        {'mission': (mission,), 'redeem': (redeem,)},
        {},
        start='mission',
        on_tap=lambda screen, _label, _count: 'redeem' if screen == 'mission' else None,
    )
    catalogue = load_task_catalogue(CATALOGUE_PATH)
    entry = _entry(catalogue)
    spec = entry.spec
    assert spec is not None

    record, _current, stop = _run_round(
        _controller(world, mission),
        world.capture,
        load_popup_corpus(CORPUS_PATH),
        mission,
        1,
        spec=spec,
        task_tried=_TriedTiles(),
        templates=None,
        arrival_timeout=0.5,
        exit_timeout=0.5,
        interval=0.5,
        settle=0.0,
        back=None,
        scroll=None,
        clock=_SteppingClock(step=0.5),
        sleep=_SteppingClock().sleep,
        logger=_logger(),
        taps=[],
        keys=[],
        swipes=[],
        dismissals=[],
    )

    assert record.complete is False
    assert stop == 'redeem_interstitial'


def test_surprise_run_round_reads_a_completed_counter_after_the_overlay() -> None:
    from aliexpress_coins.tasks.surprise import _run_round, _TriedTiles

    mission = _snapshot(
        _box(SURPRISE_TITLE, 270, 200, 930, 270),
        _box('Tap 3 items on this page to earn coins', 253, 332, 946, 384),
        _box('2/3', 480, 440, 573, 490),
        _box('Mission Completed', 200, 1450, 1000, 1550),
    )
    complete = _grid_frame(3)
    world = _World(
        {'mission': (mission,), 'complete': (complete,)},
        {},
        start='mission',
        on_tap=lambda screen, _label, _count: 'complete' if screen == 'mission' else None,
    )
    catalogue = load_task_catalogue(CATALOGUE_PATH)
    entry = _entry(catalogue)
    spec = entry.spec
    assert spec is not None

    record, _current, stop = _run_round(
        _controller(world, mission),
        world.capture,
        load_popup_corpus(CORPUS_PATH),
        mission,
        1,
        spec=spec,
        task_tried=_TriedTiles(),
        templates=None,
        arrival_timeout=0.5,
        exit_timeout=0.5,
        interval=0.5,
        settle=0.0,
        back=None,
        scroll=None,
        clock=_SteppingClock(step=0.5),
        sleep=_SteppingClock().sleep,
        logger=_logger(),
        taps=[],
        keys=[],
        swipes=[],
        dismissals=[],
    )

    assert record.complete is True
    assert record.counter_after == 3
    assert stop is None


def test_find_surprise_tiles_picks_the_title_not_branding_above_the_price_block() -> None:
    frame = _snapshot(
        _box(SURPRISE_TITLE, 270, 200, 930, 270),
        _box('Tap 3 items on this page to earn coins', 253, 332, 946, 384),
        _box('0/3', 480, 440, 573, 490),
        _box('PANIC', 375, 1022, 408, 1036),
        _box('Product Title Left', 46, 1364, 589, 1408),
        _box('Free shipping', 47, 1406, 288, 1457),
        _box('C$2.04', 69, 1493, 233, 1552),
        _box('Disc:C$1.38', 72, 1551, 281, 1596),
        _box('8Core 12G+256G', 617, 1182, 914, 1220),
        _box('Product Title Right', 611, 1366, 1137, 1405),
        _box('Orders 5.0', 610, 1412, 915, 1459),
        _box('C$102.67', 633, 1493, 846, 1554),
        _box('Disc:C$101.98', 636, 1551, 889, 1596),
    )

    tiles = find_surprise_tiles(frame)

    assert [tile.label for tile in tiles] == ['Product Title Left', 'Product Title Right']


def test_surprise_run_round_recovers_when_the_overlay_tap_opens_a_product() -> None:
    from aliexpress_coins.tasks.surprise import _run_round, _TriedTiles

    mission = _snapshot(
        _box(SURPRISE_TITLE, 270, 200, 930, 270),
        _box('Tap 3 items on this page to earn coins', 253, 332, 946, 384),
        _box('2/3', 480, 440, 573, 490),
        _box('Mission Completed', 200, 1450, 1000, 1550),
    )
    complete = _grid_frame(3)
    world = _World(
        {'mission': (mission,), 'detail': (_detail_frame(),), 'complete': (complete,)},
        {},
        start='mission',
        on_tap=lambda screen, _label, _count: 'detail' if screen == 'mission' else None,
        on_key=lambda screen, _key, _count: 'complete' if screen == 'detail' else None,
    )
    catalogue = load_task_catalogue(CATALOGUE_PATH)
    entry = _entry(catalogue)
    spec = entry.spec
    assert spec is not None

    record, _current, stop = _run_round(
        _controller(world, mission),
        world.capture,
        load_popup_corpus(CORPUS_PATH),
        mission,
        1,
        spec=spec,
        task_tried=_TriedTiles(),
        templates=None,
        arrival_timeout=0.5,
        exit_timeout=0.5,
        interval=0.5,
        settle=0.0,
        back=None,
        scroll=None,
        clock=_SteppingClock(step=0.5),
        sleep=_SteppingClock().sleep,
        logger=_logger(),
        taps=[],
        keys=[],
        swipes=[],
        dismissals=[],
    )

    assert record.complete is True
    assert record.counter_after == 3
    assert stop is None
    assert world.keys == ['KEYCODE_BACK']


def test_find_surprise_tiles_deduplicates_a_title_shared_by_two_anchor_groups() -> None:
    frame = _snapshot(
        _box(SURPRISE_TITLE, 270, 200, 930, 270),
        _box('Tap 3 items on this page to earn coins', 253, 332, 946, 384),
        _box('Same Product', 100, 500, 500, 550),
        _box('C$1.00', 100, 600, 300, 650),
        _box('C$2.00', 100, 900, 300, 950),
    )

    tiles = find_surprise_tiles(frame)

    assert [tile.label for tile in tiles] == ['Same Product']


def test_surprise_leave_detail_if_open_falls_back_to_the_chevron() -> None:
    from aliexpress_coins.tasks.surprise import _leave_detail_if_open

    detail = _snapshot(
        _box('<', 45, 79, 61, 101),
        _box('Add to cart', 393, 1860, 510, 1887),
        _box('Buy now', 895, 1859, 988, 1890),
    )
    complete = _grid_frame(3)
    world = _World(
        {'detail': (detail,), 'complete': (complete,)},
        {('detail', '<'): 'complete'},
        start='detail',
    )
    controller = Controller.from_frame(_NoKeyDevice(world), detail, rng=random.Random(0))

    frame = _leave_detail_if_open(controller, world.capture, detail)

    assert frame is complete
    assert world.taps

    no_control = _snapshot(
        _box('Add to cart', 393, 1860, 510, 1887),
        _box('Buy now', 895, 1859, 988, 1890),
    )
    world2 = _World({'detail': (no_control,)}, {}, start='detail')
    controller2 = Controller.from_frame(_NoKeyDevice(world2), no_control, rng=random.Random(0))

    assert _leave_detail_if_open(controller2, world2.capture, no_control) is no_control


def test_is_card_anchor_rejects_an_empty_line() -> None:
    from aliexpress_coins.tasks.surprise import _is_card_anchor

    assert _is_card_anchor(_box('   ', 100, 600, 300, 650)) is False


def test_run_surprise_task_sweeps_a_popup_after_next_round(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    engine: RapidOcrEngine,
    tmp_path: Path,
) -> None:
    launch = _load(DATA / 'popups' / 'launch_welcome_gifts.png', engine)
    pending: dict[str, str] = {}

    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        if screen == 'r1grid0' and label == 'Product A':
            pending['return'] = 'r1grid1'
            return 'detail'
        if screen == 'r1grid1' and label == 'Product B':
            pending['return'] = 'r1grid2'
            return 'detail'
        if screen == 'r1grid2' and label == 'Product C':
            pending['return'] = 'r1grid3'
            return 'detail'
        if screen == 'r1grid3' and label == 'Next round':
            return 'popup'
        if screen == 'popup':
            return 'r2grid0'
        if screen == 'r2grid0' and label == 'Product D':
            pending['return'] = 'r2grid1'
            return 'detail'
        if screen == 'r2grid1' and label == 'Product E':
            pending['return'] = 'r2grid2'
            return 'detail'
        if screen == 'r2grid2' and label == 'Product F':
            pending['return'] = 'r2grid3'
            return 'detail'
        return None

    def on_key(screen: str, _key: str, _count: int) -> str | None:
        if screen == 'detail':
            return pending.pop('return')
        return None

    world = _World(
        series={
            'boost': (_boost_frame(progress='0/2'),),
            'r1grid0': (_grid_frame(0),),
            'r1grid1': (_grid_frame(1),),
            'r1grid2': (_grid_frame(2),),
            'r1grid3': (_grid_frame(3, next_round=True),),
            'r2grid0': (_grid_frame(0),),
            'r2grid1': (_grid_frame(1),),
            'r2grid2': (_grid_frame(2),),
            'r2grid3': (_grid_frame(3),),
            'popup': (launch,),
            'detail': (_detail_frame(),),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'r1grid0'},
        on_tap=on_tap,
        on_key=on_key,
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is SurpriseOutcome.COMPLETED
    assert result.rounds == 2
    assert result.dismissals


def test_run_surprise_task_stops_when_next_round_reveals_the_redeem_overlay(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    redeem = _snapshot(
        _box('YOU CAN NOW REDEEM!', 80, 700, 1120, 780),
        _box('Coins', 101, 194, 254, 242),
        _box('Daily coins', 66, 919, 320, 978),
    )
    pending: dict[str, str] = {}

    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        if screen in ('grid0', 'grid1', 'grid2') and label in PRODUCTS[:3]:
            pending['return'] = f'grid{int(screen[-1]) + 1}'
            return 'detail'
        if screen == 'grid3' and label == 'Next round':
            return 'redeem'
        return None

    def on_key(screen: str, _key: str, _count: int) -> str | None:
        if screen == 'detail':
            return pending.pop('return')
        return None

    world = _World(
        series={
            'boost': (_boost_frame(progress='0/2'),),
            'grid0': (_grid_frame(0),),
            'grid1': (_grid_frame(1),),
            'grid2': (_grid_frame(2),),
            'grid3': (_grid_frame(3, next_round=True),),
            'redeem': (redeem,),
            'detail': (_detail_frame(),),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'grid0'},
        on_tap=on_tap,
        on_key=on_key,
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is SurpriseOutcome.COMPLETED
    assert result.rounds == 1
    assert result.returned is True


def test_surprise_sweep_does_not_close_the_boost_list_it_was_opened_from(
    corpus: PopupCorpus,
    engine: RapidOcrEngine,
) -> None:
    from aliexpress_coins.tasks.surprise import _sweep

    launch = _load(DATA / 'popups' / 'launch_welcome_gifts.png', engine)
    boost = _load(DATA / 'flows' / 'surprise_items_entry_row.png', engine)
    world = _World(
        {'popup': (launch,), 'boost': (boost,)},
        {},
        start='popup',
        on_tap=lambda screen, _label, _count: 'boost' if screen == 'popup' else None,
    )

    report, stop = _sweep(
        _controller(world, world.frame),
        world.capture,
        corpus,
        templates=None,
        settle=0.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        logger=_logger(),
    )

    assert report is not None
    assert stop is None
    assert world.current == 'boost'
    assert len(world.taps) == 1
    assert not any('coins_boost_sheet' in family for family in report.closed)


def test_surprise_completed_detail_counts_only_completed_rounds() -> None:
    from aliexpress_coins.tasks.surprise import _completed_detail

    records = (
        RoundRecord(1, 0, 3, 3, 3, RULE_PATH_REUSE, True, 'done'),
        RoundRecord(2, 0, 2, 3, 5, RULE_PATH_RESET, False, 'stalled'),
    )

    assert _completed_detail(records) == '1 round(s) completed with counter evidence (0/3 -> 3/3)'


def test_read_surprise_counter_returns_none_when_the_crop_ocr_fails() -> None:
    class _FailingOcr:
        def read(self, _image: object) -> object:
            raise OcrError('crop OCR failed')

    frame = _snapshot(_box(SURPRISE_TITLE, 270, 200, 930, 270))

    assert read_surprise_counter(frame, ocr=_FailingOcr()) is None  # type: ignore[arg-type]


def test_run_surprise_task_does_not_attribute_a_multi_credit_jump_to_one_tile(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    pending: dict[str, str] = {}

    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        if screen == 'grid0' and label == 'Product A':
            pending['return'] = 'after_a'
            return 'detail'
        if screen == 'after_a' and label == 'Product B':
            pending['return'] = 'grid2'
            return 'detail'
        if screen == 'grid2' and label == 'Product C':
            pending['return'] = 'grid3'
            return 'detail'
        return None

    def on_key(screen: str, _key: str, _count: int) -> str | None:
        if screen == 'detail':
            return pending.pop('return')
        return None

    world = _World(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'grid0': (_grid_frame(0),),
            'after_a': (_grid_frame(None),),
            'grid2': (_grid_frame(2),),
            'grid3': (_grid_frame(3),),
            'detail': (_detail_frame(),),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'grid0'},
        on_tap=on_tap,
        on_key=on_key,
    )

    result = _run(world, catalogue, corpus, _state(tmp_path), max_rounds=1)

    assert result.outcome is SurpriseOutcome.COMPLETED
    assert result.round_records[0].counter_change == '0/3 -> 3/3'
    assert result.round_records[0].items == ('Product C',)
    assert 'Product B' not in result.round_records[0].items


def test_run_surprise_task_does_not_attribute_a_delayed_credit_to_the_next_tile(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    pending: dict[str, str] = {}

    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        if screen == 'grid0' and label == 'Product A':
            pending['return'] = 'after_a'
            return 'detail'
        if screen == 'after_a' and label == 'Product B':
            pending['return'] = 'grid1'
            return 'detail'
        if screen == 'grid1' and label == 'Product C':
            pending['return'] = 'grid2'
            return 'detail'
        if screen == 'grid2' and label == 'Product D':
            pending['return'] = 'grid3'
            return 'detail'
        return None

    def on_key(screen: str, _key: str, _count: int) -> str | None:
        if screen == 'detail':
            return pending.pop('return')
        return None

    world = _World(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'grid0': (_grid_frame(0),),
            'after_a': (_grid_frame(None),),
            'grid1': (_grid_frame(1),),
            'grid2': (_grid_frame(2),),
            'grid3': (_grid_frame(3),),
            'detail': (_detail_frame(),),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'grid0'},
        on_tap=on_tap,
        on_key=on_key,
    )

    result = _run(world, catalogue, corpus, _state(tmp_path), max_rounds=1)

    assert result.outcome is SurpriseOutcome.COMPLETED
    assert result.round_records[0].counter_change == '0/3 -> 3/3'
    assert result.round_records[0].items == ('Product C', 'Product D')
    assert 'Product A' not in result.round_records[0].items
    assert 'Product B' not in result.round_records[0].items
