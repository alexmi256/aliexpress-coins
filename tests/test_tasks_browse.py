"""The browse-and-wait executor over measured and synthetic frames (issue #24).

The device is simulated at the capture and input seams only: taps are recorded by
a fake device that drives a small screen graph, captures come from that graph,
and time is injected. No test reaches adb and no test sleeps.

The synthetic frames are built from the measured layout: the timer chip is at
the right edge and reads ``12s`` / ``6s``; the boost row carries its own progress
badge and ``GO``; the coin page carries ``Daily coins`` and ``Coins credits to
redeem``. The popup fixtures are the reviewed captures the corpus tests already
use.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from aliexpress_coins.control.controller import Controller
from aliexpress_coins.control.errors import InputError, PopupUnclosable
from aliexpress_coins.control.popups import DismissReport, DismissStop, PopupCorpus, load_popup_corpus
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.matching import normalize
from aliexpress_coins.perception.ocr import RapidOcrEngine
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.waits import CountdownEnding
from aliexpress_coins.runner.safety import SafetyGuard
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks.browse import (
    MAX_PRESSES,
    Arrival,
    BrowseOutcome,
    BrowseResult,
    ChipReading,
    ChipStatus,
    ChipWait,
    ReturnResult,
    find_collect_control,
    find_task_row,
    find_timer_chip,
    return_to_coins_page,
    run_browse_task,
    wait_for_timer_chip,
)
from aliexpress_coins.tasks.catalogue import TaskCatalogue, load_task_catalogue
from aliexpress_coins.tasks.discovery import RosterEntry, enumerate_roster
from aliexpress_coins.tasks.dispatch import DispatchGate
from aliexpress_coins.tasks.errors import TaskError
from aliexpress_coins.tasks.surface import RouteFirer, SurfaceRoute

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'tests' / 'data'
CORPUS_PATH = ROOT / 'specs' / 'popups.yaml'
CATALOGUE_PATH = ROOT / 'specs' / 'tasks.yaml'
CAPTURED_AT = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
TITLE = 'Explore sponsored items'


class _RecordingDevice:
    """The input seam: record taps and let the world react to them."""

    def __init__(self, world: _World) -> None:
        self._world = world

    def tap(self, x: int, y: int) -> None:
        self._world.tap(x, y)


class _ScrollingDevice(_RecordingDevice):
    """A device that also records search swipes."""

    def __init__(self, world: _World) -> None:
        super().__init__(world)
        self.swipes: list[tuple[int, int, int, int, int]] = []

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        self.swipes.append((x1, y1, x2, y2, duration_ms))


class _FailingSwipeDevice(_ScrollingDevice):
    """A device whose search swipe fails after one successful move."""

    def __init__(self, world: _World) -> None:
        super().__init__(world)
        self.calls = 0

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        self.calls += 1
        if self.calls == 2:
            raise InputError('adb input failed')
        super().swipe(x1, y1, x2, y2, duration_ms)


class _PersistentSwipeFailure(_ScrollingDevice):
    """A device that moves once and then fails every later gesture."""

    def __init__(self, world: _World) -> None:
        super().__init__(world)
        self.calls = 0

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        self.calls += 1
        if self.calls > 1:
            raise InputError('adb input failed')
        super().swipe(x1, y1, x2, y2, duration_ms)


class _QueueCapture:
    """A plain frame sequence that holds its last frame once exhausted."""

    def __init__(self, frames: Sequence[Snapshot]) -> None:
        if not frames:
            raise ValueError('a capture sequence needs at least one frame')
        self._frames = tuple(frames)
        self._index = 0
        self.calls = 0

    def capture(self) -> Snapshot:
        self.calls += 1
        frame = self._frames[min(self._index, len(self._frames) - 1)]
        self._index += 1
        return frame


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
    """A tiny screen graph driven by taps and navigation, not a frame queue."""

    def __init__(
        self,
        series: dict[str, Sequence[Snapshot]],
        transitions: dict[tuple[str, str | None], str],
        *,
        start: str | None = None,
        on_tap: Callable[[str, str | None, int], str | None] | None = None,
    ) -> None:
        if start is None:
            start = next(iter(series), '')
        if start not in series:
            raise ValueError(f'start screen {start!r} has no frames')
        self._series = {name: tuple(frames) for name, frames in series.items()}
        self._index = dict.fromkeys(series, 0)
        self._transitions = transitions
        self._on_tap = on_tap
        self.current = start
        self.taps: list[tuple[int, int]] = []
        self.navigations: list[SurfaceRoute] = []
        self.captures = 0

    @property
    def frame(self) -> Snapshot:
        """The frame the current screen starts with."""
        return self._series[self.current][0]

    @property
    def current_frame(self) -> Snapshot:
        """The frame the current screen last showed (or its first frame)."""
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

    def navigate(self, route: SurfaceRoute) -> None:
        self.navigations.append(route)
        self.go('coins')

    def go(self, screen: str) -> None:
        if screen not in self._series:
            raise AssertionError(f'unknown screen {screen!r}')
        self.current = screen
        self._index[screen] = 0


class _RaisingNavigate:
    """A route firer that always fails, to prove the return reports it."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _route: SurfaceRoute) -> None:
        self.calls += 1
        raise OSError('adb route unavailable')


class _ForbiddenNavigate:
    """A route firer that must not be used on a no-work path."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _route: SurfaceRoute) -> None:
        self.calls += 1
        raise AssertionError('this path must not navigate')


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


def _load(path: Path, engine: RapidOcrEngine) -> Snapshot:
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


def _boost_frame(
    *,
    badge: str | None = '0/6',
    with_row: bool = True,
    action: str | None = 'GO',
    done_label: str | None = None,
) -> Snapshot:
    boxes = [
        _box('Coins boost', 80, 90, 400, 150),
        _box('Browse surprise items', 60, 700, 500, 750),
        _box('Tap 3 items on this page', 60, 760, 500, 800),
        _box('0/2', 980, 710, 1080, 750),
        _box('GO', 1000, 760, 1080, 810),
        _box('Place an order on this page', 60, 1000, 500, 1050),
        _box('GO', 1000, 1050, 1080, 1100),
        _box('Share coins with friends', 60, 1300, 500, 1350),
        _box('Win rewards together', 60, 1360, 500, 1400),
        _box('GO', 1000, 1360, 1080, 1410),
    ]
    if with_row:
        boxes.extend(
            [
                _box(TITLE, 60, 420, 700, 470),
                _box('Browse, shop and save', 60, 480, 500, 520),
                *([] if badge is None else [_box(badge, 980, 430, 1080, 470)]),
                *([] if action is None else [_box(action, 1000, 480, 1080, 530)]),
                *([] if done_label is None else [_box(done_label, 820, 480, 980, 520)]),
            ],
        )
    return _snapshot(*boxes)


def _super_discounts_frame(*, with_row: bool = True) -> Snapshot:
    """The measured no-badge row shape: title, GO and its own +1 reward."""
    boxes = [
        _box('Coins boost', 80, 90, 400, 150),
        _box('Place an order on this page', 60, 1000, 500, 1050),
        _box('GO', 1000, 1050, 1080, 1100),
    ]
    if with_row:
        boxes.extend(
            [
                _box('View Super discounts', 60, 420, 700, 470),
                _box('+1', 200, 480, 300, 520),
                _box('GO', 1000, 480, 1080, 530),
            ],
        )
    return _snapshot(*boxes)


def _coin_frame(*, inline: bool = False, badge: str = '1/6', earn_more: bool = True) -> Snapshot:
    boxes = [
        _box('Coins', 101, 194, 254, 242),
        _box('Coins credits to redeem', 59, 567, 552, 617),
        _box('Daily coins', 66, 919, 320, 978),
        _box('Today', 109, 1013, 210, 1063),
        _box('Day 5', 634, 1015, 731, 1063),
    ]
    if earn_more:
        boxes.append(_box('Earn more >', 917, 926, 1133, 967))
    if inline:
        boxes.extend(
            [
                _box(TITLE, 60, 1200, 700, 1250),
                _box(badge, 980, 1210, 1080, 1250),
                _box('GO', 1000, 1260, 1080, 1310),
            ],
        )
    return _snapshot(*boxes)


def _destination(*, chip: str | None = '12s', collect: bool = False) -> Snapshot:
    boxes = [
        _box('Bundle Deals', 60, 100, 500, 160),
        _box('Extra 20% off', 60, 180, 400, 230),
        _box('Hot picks', 60, 300, 300, 350),
        _box('Wireless Earbuds', 60, 400, 500, 450),
        _box('C$12.99', 60, 460, 300, 510),
        _box('Phone Case', 620, 400, 1000, 450),
        _box('C$4.99', 620, 460, 800, 510),
        _box('USB Cable', 60, 700, 400, 750),
        _box('C$7.99', 60, 760, 300, 810),
        _box('Backpack', 620, 700, 1000, 750),
        _box('C$29.99', 620, 760, 800, 810),
    ]
    if chip is not None:
        boxes.append(_box(chip, 1100, 1000, 1160, 1040))
    if collect:
        boxes.append(_box('Collect', 900, 1200, 1080, 1260))
    return _snapshot(*boxes)


def _wrong_page() -> Snapshot:
    return _snapshot(
        _box('AliExpress', 60, 100, 400, 160),
        _box('Explore', 60, 200, 300, 250),
        _box('Wireless Earbuds', 60, 400, 500, 450),
        _box('C$12.99', 60, 460, 300, 510),
        _box('Phone Case', 620, 400, 1000, 450),
        _box('C$4.99', 620, 460, 800, 510),
        _box('USB Cable', 60, 700, 400, 750),
        _box('C$7.99', 60, 760, 300, 810),
    )


def _without_timer(snapshot: Snapshot) -> Snapshot:
    """The same frame after the dwell chip cleared; only the chip box is gone."""

    def is_timer(box: TextBox) -> bool:
        text = normalize(box.text)
        return text.endswith('s') and text[:-1].isdigit()

    return replace(snapshot, boxes=tuple(box for box in snapshot.boxes if not is_timer(box)))


def _loading_frame() -> Snapshot:
    return _snapshot(_box('Loading', 100, 100, 300, 150))


def _empty_boost_frame() -> Snapshot:
    return _snapshot(
        _box('Coins boost', 80, 90, 400, 150),
        _box("There's nothing here yet", 60, 700, 800, 760),
    )


def _redeem_frame() -> Snapshot:
    return _snapshot(
        _box('YOU CAN NOW REDEEM!', 80, 700, 1120, 780),
        _box('Coins', 101, 194, 254, 242),
        _box('Daily coins', 66, 919, 320, 978),
    )


def _popup_frame() -> Snapshot:
    return _snapshot(
        _box('Tap to claim', 80, 700, 600, 780),
        _box('Wireless Earbuds', 60, 400, 500, 450),
        _box('C$12.99', 60, 460, 300, 510),
        _box('Phone Case', 620, 400, 1000, 450),
        _box('C$4.99', 620, 460, 800, 510),
    )


def _controller(world: _World, frame: Snapshot) -> Controller:
    return Controller.from_frame(_RecordingDevice(world), frame, rng=random.Random(0))


def _state(tmp_path: Path) -> DailyStateStore:
    return DailyStateStore(tmp_path / 'state', clock=lambda: CAPTURED_AT)


def _entry(catalogue: TaskCatalogue, key: str = 'sponsored_items') -> RosterEntry:
    roster = enumerate_roster(_boost_frame(), catalogue)
    for entry in roster.entries:
        if entry.key == key:
            return DispatchGate(catalogue).authorize(entry)
    raise AssertionError(f'{key!r} is not in the synthetic roster')


def _success_world(*, after_badge: str = '1/6', after_action: str | None = 'GO') -> _World:
    return _World(
        series={
            'boost': (_boost_frame(),),
            'destination': (
                _destination(chip='12s'),
                _destination(chip='12s'),
                _destination(chip='6s'),
                _destination(chip=None, collect=True),
                _destination(chip=None, collect=True),
            ),
            'collected': (_destination(chip=None),),
            'coins': (_coin_frame(),),
            'boost_after': (_boost_frame(badge=after_badge, action=after_action),),
        },
        transitions={
            ('boost', 'go'): 'destination',
            ('destination', 'collect'): 'collected',
            ('coins', 'earn more'): 'boost_after',
        },
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


@pytest.fixture(scope='module')
def launch(engine: RapidOcrEngine) -> Snapshot:
    return _load(DATA / 'popups' / 'launch_welcome_gifts.png', engine)


@pytest.fixture(scope='module')
def coin_store(engine: RapidOcrEngine) -> Snapshot:
    return _load(DATA / 'states' / 'coin_store.png', engine)


def test_find_timer_chip_reads_the_right_edge_countdown() -> None:
    frame = _destination(chip='12s')

    reading = find_timer_chip(frame)

    assert reading is not None
    assert reading.countdown == 12
    assert reading.running is True
    assert reading.checked is False
    assert reading.text == '12s'


def test_find_timer_chip_ignores_left_edge_and_system_bar_digits() -> None:
    left = _snapshot(_box('12s', 100, 1000, 160, 1040))
    status_bar = _snapshot(_box('12s', 1100, 10, 1160, 50))
    tiny = _snapshot(_box('12s', 1100, 1000, 1160, 1040, score=0.2))

    assert find_timer_chip(left) is None
    assert find_timer_chip(status_bar) is None
    assert find_timer_chip(tiny) is None


def test_find_timer_chip_reads_the_check_glyph_and_prefers_the_rightmost() -> None:
    frame = _snapshot(
        _box('✓', 1050, 1000, 1090, 1040),
        _box('check', 1120, 1000, 1160, 1040),
        _box('9s', 1000, 1000, 1030, 1040),
        _box('12s', 1100, 1000, 1140, 1040),
    )

    running = find_timer_chip(frame)
    assert running is not None
    assert running.countdown == 12

    clear = find_timer_chip(_snapshot(_box('✓', 1100, 1000, 1160, 1040)))
    assert clear is not None
    assert clear.checked is True
    assert clear.clear is True
    assert clear.countdown is None


def test_wait_for_timer_chip_is_ended_by_the_read_not_the_bound() -> None:
    clock = _SteppingClock(step=0.5)
    capture = _QueueCapture(
        [
            _destination(chip='12s'),
            _destination(chip='6s'),
            _destination(chip=None),
            _destination(chip=None),
        ],
    )

    wait = wait_for_timer_chip(capture, timeout=15.0, interval=0.5, clock=clock, sleep=clock.sleep)

    assert wait.status is ChipStatus.CLEARED
    assert wait.seen is True
    assert wait.ready is True
    assert wait.elapsed < 15.0
    assert wait.bound == 15.0
    assert len(wait.observations) == 3
    assert wait.initial is not None
    assert wait.initial.countdown == 12
    assert wait.final is None
    assert wait.ended_by is CountdownEnding.FRAME
    assert wait.as_dict()['ended_by'] == 'frame'


def test_wait_for_timer_chip_accepts_the_arrival_frame_as_the_initial_read() -> None:
    clock = _SteppingClock(step=0.5)
    capture = _QueueCapture([_destination(chip='6s'), _destination(chip=None)])

    wait = wait_for_timer_chip(
        capture,
        timeout=15.0,
        interval=0.5,
        initial=_destination(chip='12s'),
        clock=clock,
        sleep=clock.sleep,
    )

    assert wait.status is ChipStatus.CLEARED
    assert capture.calls == 2
    assert wait.initial is not None
    assert wait.initial.countdown == 12


def test_wait_for_timer_chip_absent_is_a_finding_not_a_long_sleep() -> None:
    clock = _SteppingClock(step=1.0)
    capture = _QueueCapture([_destination(chip=None)])

    wait = wait_for_timer_chip(capture, timeout=15.0, interval=0.5, clock=clock, sleep=clock.sleep)

    assert wait.status is ChipStatus.ABSENT
    assert wait.seen is False
    assert wait.ready is False
    assert len(wait.observations) == 1
    assert capture.calls == 1
    assert wait.elapsed < 15.0


def test_wait_for_timer_chip_already_clear_and_not_arrived() -> None:
    clock = _SteppingClock()
    clear = wait_for_timer_chip(
        _QueueCapture([_destination(chip='✓')]),
        timeout=5.0,
        clock=clock,
        sleep=clock.sleep,
    )
    assert clear.status is ChipStatus.ALREADY_CLEAR
    assert clear.ready is True

    not_arrived = wait_for_timer_chip(
        _QueueCapture([_destination(chip=None)]),
        timeout=5.0,
        is_destination=lambda _frame: False,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )
    assert not_arrived.status is ChipStatus.NOT_ARRIVED


def test_wait_for_timer_chip_times_out_while_the_digits_remain() -> None:
    clock = _SteppingClock(step=1.0)
    capture = _QueueCapture([_destination(chip='9s')])

    wait = wait_for_timer_chip(capture, timeout=2.0, interval=1.0, clock=clock, sleep=clock.sleep)

    assert wait.status is ChipStatus.TIMED_OUT
    assert wait.seen is True
    assert wait.ready is False
    assert wait.elapsed >= 2.0


def test_wait_for_timer_chip_stops_when_the_destination_bounces() -> None:
    clock = _SteppingClock(step=0.5)
    capture = _QueueCapture([_destination(chip='9s'), _wrong_page(), _wrong_page()])

    wait = wait_for_timer_chip(
        capture,
        timeout=5.0,
        interval=0.5,
        is_destination=lambda frame: frame.has('Bundle Deals'),
        clock=clock,
        sleep=clock.sleep,
    )

    assert wait.status is ChipStatus.NOT_ARRIVED
    assert wait.elapsed < 5.0


def test_wait_for_timer_chip_zero_timeout_does_not_capture() -> None:
    capture = _ForbiddenCapture()

    wait = wait_for_timer_chip(capture, timeout=0.0)

    assert wait.status is ChipStatus.NOT_WAITED
    assert capture.calls == 0


@pytest.mark.parametrize(('timeout', 'interval'), [(-1.0, 1.0), (1.0, 0.0)])
def test_wait_for_timer_chip_rejects_invalid_parameters(timeout: float, interval: float) -> None:
    with pytest.raises(ValueError, match=r'must be positive|must not be negative'):
        wait_for_timer_chip(_QueueCapture([_destination()]), timeout=timeout, interval=interval)


def test_find_collect_control_prefers_a_button_and_skips_a_banner() -> None:
    frame = _snapshot(
        _box('Collect all: C$5.00', 60, 600, 1140, 660),
        _box('Collect', 900, 1200, 1080, 1260),
    )

    control = find_collect_control(frame)

    assert control is not None
    assert control.text == 'Collect'


def test_find_collect_control_skips_a_wide_collect_control() -> None:
    frame = _snapshot(_box('Collect', 60, 600, 1140, 660))

    assert find_collect_control(frame) is None


def test_find_collect_control_is_none_for_body_text_and_empty_frames() -> None:
    assert find_collect_control(_snapshot(_box('Wireless Earbuds', 60, 400, 500, 450))) is None
    assert find_collect_control(_snapshot()) is None


def test_find_task_row_resolves_the_rows_own_action() -> None:
    row, action, badge = find_task_row(_boost_frame(), TITLE)

    assert row is not None
    assert TITLE in row.text
    assert action is not None
    assert action.text == 'GO'
    assert badge is not None
    assert badge.text == '0/6'


def test_find_task_row_returns_none_for_a_missing_title(monkeypatch: pytest.MonkeyPatch) -> None:
    assert find_task_row(_boost_frame(), 'Not a task') == (None, None, None)

    monkeypatch.setattr('aliexpress_coins.tasks.browse.group_rows', lambda *_args, **_kwargs: ())
    assert find_task_row(_boost_frame(), TITLE) == (None, None, None)


def test_browse_surface_settle_waits_out_loading_before_reading_rows(corpus: PopupCorpus) -> None:
    from aliexpress_coins.tasks.browse import _settle_surface

    world = _World(
        {'frames': (_loading_frame(), _boost_frame()), 'coins': (_coin_frame(),)},
        {},
        start='frames',
    )
    clock = _SteppingClock(step=0.25)

    frame, settled, dismiss = _settle_surface(
        _controller(world, world.frame),
        world.capture,
        corpus,
        world.frame,
        templates=None,
        timeout=2.0,
        interval=0.5,
        settle=0.0,
        clock=clock,
        sleep=clock.sleep,
        logger=logging.getLogger('test'),
    )

    assert settled is True
    assert dismiss is None
    assert frame.has('Coins boost')


def test_return_to_coins_page_already_there_uses_no_route(
    corpus: PopupCorpus,
    catalogue: TaskCatalogue,
) -> None:
    world = _World({'coins': (_coin_frame(),)}, {})
    navigate = _ForbiddenNavigate()

    result = return_to_coins_page(
        _controller(world, world.frame),
        world.capture,
        corpus,
        cast(RouteFirer, navigate),
        snapshot=world.frame,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.returned is True
    assert result.reentered is False
    assert result.attempts == 0
    assert result.snapshot is not None
    assert navigate.calls == 0


def test_return_to_coins_page_fires_the_shortcut_and_proves_it(corpus: PopupCorpus) -> None:
    world = _World({'boost': (_boost_frame(),), 'coins': (_coin_frame(),)}, {})
    result = return_to_coins_page(
        _controller(world, world.frame),
        world.capture,
        corpus,
        world.navigate,
        snapshot=world.frame,
        timeout=2.0,
        interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.returned is True
    assert result.reentered is True
    assert result.navigations == (SurfaceRoute.SHORTCUT,)
    assert result.snapshot is not None
    assert world.current == 'coins'


def test_return_to_coins_page_retries_then_reports_failure(corpus: PopupCorpus) -> None:
    world = _World({'boost': (_boost_frame(),), 'wrong': (_wrong_page(),)}, {})

    def navigate(_route: SurfaceRoute) -> None:
        world.go('wrong')

    result = return_to_coins_page(
        _controller(world, world.frame),
        world.capture,
        corpus,
        cast(RouteFirer, navigate),
        snapshot=world.frame,
        timeout=0.5,
        interval=0.5,
        attempts=2,
        clock=_SteppingClock(step=1.0),
        sleep=_SteppingClock().sleep,
    )

    assert result.returned is False
    assert result.attempts == 2
    assert len(result.navigations) == 2
    assert result.error is None


def test_return_to_coins_page_records_a_navigation_error(corpus: PopupCorpus) -> None:
    world = _World({'boost': (_boost_frame(),), 'coins': (_coin_frame(),)}, {})
    navigate = _RaisingNavigate()

    result = return_to_coins_page(
        _controller(world, world.frame),
        world.capture,
        corpus,
        cast(RouteFirer, navigate),
        snapshot=world.frame,
        timeout=0.5,
        interval=0.5,
        attempts=2,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.returned is False
    assert navigate.calls == 2
    assert result.error is not None
    assert 'adb route unavailable' in result.error


@pytest.mark.slow
def test_return_to_coins_page_sweeps_a_popup_on_arrival(
    corpus: PopupCorpus,
    launch: Snapshot,
) -> None:
    def on_tap(screen: str, _label: str | None, _count: int) -> str | None:
        return 'coins' if screen == 'popup' else None

    world = _World(
        {'boost': (_boost_frame(),), 'popup': (launch,), 'coins': (_coin_frame(),)},
        {},
        on_tap=on_tap,
    )

    def navigate(_route: SurfaceRoute) -> None:
        world.go('popup')

    dismissals: list[DismissReport] = []
    result = return_to_coins_page(
        _controller(world, world.frame),
        world.capture,
        corpus,
        cast(RouteFirer, navigate),
        snapshot=world.frame,
        timeout=5.0,
        interval=0.5,
        clock=_SteppingClock(step=0.5),
        sleep=_SteppingClock().sleep,
        on_dismiss=dismissals.append,
    )

    assert result.returned is True
    assert world.current == 'coins'
    assert [report.closed for report in dismissals] == [('launch_welcome_gifts',)]
    assert [item['closed'] for item in cast(list[dict[str, Any]], result.as_dict()['dismissals'])] == [
        ['launch_welcome_gifts'],
    ]


def test_return_to_coins_page_accepts_the_redeem_interstitial_as_arrival(corpus: PopupCorpus) -> None:
    world = _World({'boost': (_boost_frame(),), 'redeem': (_redeem_frame(),)}, {})

    def navigate(_route: SurfaceRoute) -> None:
        world.go('redeem')

    result = return_to_coins_page(
        _controller(world, world.frame),
        world.capture,
        corpus,
        cast(RouteFirer, navigate),
        snapshot=world.frame,
        timeout=2.0,
        interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.returned is True
    assert result.reentered is True


@pytest.mark.parametrize(
    ('timeout', 'interval', 'attempts', 'settle'),
    [(0.0, 1.0, 1, 0.0), (1.0, 0.0, 1, 0.0), (1.0, 1.0, 0, 0.0), (1.0, 1.0, 1, -1.0)],
)
def test_return_to_coins_page_rejects_invalid_parameters(
    corpus: PopupCorpus,
    timeout: float,
    interval: float,
    attempts: int,
    settle: float,
) -> None:
    world = _World({'coins': (_coin_frame(),)}, {})

    with pytest.raises(ValueError, match=r'must be positive|must not be negative'):
        return_to_coins_page(
            _controller(world, world.frame),
            world.capture,
            corpus,
            world.navigate,
            timeout=timeout,
            interval=interval,
            attempts=attempts,
            settle=settle,
        )


@pytest.mark.slow
def test_run_browse_task_completes_and_quotes_the_badge_change(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _success_world()
    state = _state(tmp_path)
    entry = _entry(catalogue)
    clock = _SteppingClock()

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        state,
        entry,
        navigate=world.navigate,
        arrival_timeout=1.0,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=2.0,
        return_interval=0.5,
        clock=clock,
        sleep=clock.sleep,
    )

    assert result.outcome is BrowseOutcome.COMPLETED
    assert result.verification == 'progress_advanced'
    assert result.badge_before == '0/6'
    assert result.badge_after == '1/6'
    assert result.badge_change == '0/6 -> 1/6'
    assert result.recorded is True
    assert result.returned is True
    assert result.return_state is not None
    assert result.return_state.attempts == 2
    assert result.return_state.navigations == (SurfaceRoute.SHORTCUT, SurfaceRoute.SHORTCUT)
    assert result.completed is True
    assert result.failed is False
    assert result.skipped is False
    assert result.presses == 1
    assert result.collect is not None
    assert result.chip is not None
    assert result.chip.status is ChipStatus.CLEARED
    assert result.chip.elapsed < result.wait_seconds
    assert result.arrival is not None
    assert result.arrival.arrived is True
    # GO, the offered Collect, and the verification's own Earn more tap.
    assert len(world.taps) == 3
    assert world.current == 'coins'
    assert state.count('sponsored_items') == 1
    payload = result.as_dict()
    assert payload['badge_change'] == '0/6 -> 1/6'
    assert payload['outcome'] == 'completed'


@pytest.mark.slow
def test_run_super_discounts_dwells_and_verifies_the_plus_one_row_is_gone(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_super_discounts_frame(),),
            'destination': (
                _destination(chip='12s'),
                _destination(chip='6s'),
                _destination(chip=None),
                _destination(chip=None),
            ),
            'coins': (_coin_frame(),),
            'boost_after': (_super_discounts_frame(with_row=False),),
        },
        transitions={
            ('boost', 'go'): 'destination',
            ('coins', 'earn more'): 'boost_after',
        },
    )
    state = _state(tmp_path)
    entry = DispatchGate(catalogue).authorize(
        next(entry for entry in enumerate_roster(world.frame, catalogue).entries if entry.key == 'super_discounts')
    )

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        state,
        entry,
        navigate=world.navigate,
        arrival_timeout=1.0,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.COMPLETED
    assert result.verification == 'row_gone'
    assert result.detail == "'View Super discounts' is gone from the populated boost list"
    assert result.before is not None
    assert '+1' in (result.before.row or '')
    assert result.badge_before is None
    assert result.badge_after is None
    assert result.chip is not None
    assert result.chip.status is ChipStatus.CLEARED
    assert result.chip.seen is True
    assert result.wait_seconds == 15.0
    assert result.chip.elapsed < result.wait_seconds
    assert result.presses == 1
    assert result.recorded is True
    assert result.returned is True
    assert state.count('super_discounts') == 1
    assert world.current == 'coins'


def test_run_browse_task_accepts_an_inline_badge_advance_without_reopening(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(),),
            'destination': (
                _destination(chip='12s'),
                _destination(chip=None),
                _destination(chip=None),
            ),
            'coins': (_coin_frame(inline=True, badge='1/6'),),
        },
        transitions={('boost', 'go'): 'destination'},
    )

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.COMPLETED
    assert result.verification == 'progress_advanced'
    assert result.returned is True
    assert len(world.navigations) == 1
    assert world.current == 'coins'


@pytest.mark.slow
def test_run_browse_task_fails_when_the_taps_change_nothing(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(),),
            'coins': (_coin_frame(),),
            'boost_after': (_boost_frame(),),
        },
        transitions={('coins', 'earn more'): 'boost_after'},
    )
    state = _state(tmp_path)

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        state,
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=0.5,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.FAILED
    assert result.verification == 'no_change'
    assert result.completed is False
    assert result.recorded is False
    assert result.presses == MAX_PRESSES
    assert len(world.taps) == MAX_PRESSES + 1  # three presses and the verification's Earn more
    assert result.badge_before == '0/6'
    assert result.badge_after == '0/6'
    assert result.badge_change == '0/6 -> 0/6'
    assert result.returned is True
    assert world.current == 'coins'
    assert state.count('sponsored_items') == 0


@pytest.mark.slow
def test_run_browse_task_returns_to_coins_when_the_destination_is_wrong(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(),),
            'wrong': (_wrong_page(),),
            'coins': (_coin_frame(),),
            'boost_after': (_boost_frame(),),
        },
        transitions={
            ('boost', 'go'): 'wrong',
            ('coins', 'earn more'): 'boost_after',
        },
    )

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.FAILED
    assert result.verification == 'no_change'
    assert result.returned is True
    assert result.arrival is not None
    assert result.arrival.arrived is True
    assert result.chip is not None
    assert result.chip.status is ChipStatus.ABSENT
    assert world.current == 'coins'


@pytest.mark.slow
def test_run_browse_task_dwells_on_the_recorded_coin_store_and_returns(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    coin_store: Snapshot,
) -> None:
    cleared = _without_timer(coin_store)
    guard = SafetyGuard(catalogue=catalogue)
    world = _World(
        series={
            'boost': (_boost_frame(),),
            'store': (coin_store, coin_store, cleared, cleared),
            'coins': (_coin_frame(),),
            'boost_after': (_boost_frame(badge='1/6'),),
        },
        transitions={
            ('boost', 'go'): 'store',
            ('coins', 'earn more'): 'boost_after',
        },
    )

    with guard.task(TITLE):
        result = run_browse_task(
            _controller(world, world.frame),
            guard.wrap(world.capture),
            corpus,
            _state(tmp_path),
            _entry(catalogue),
            navigate=world.navigate,
            arrival_timeout=1.0,
            load_timeout=1.0,
            chip_interval=0.5,
            return_timeout=2.0,
            return_interval=0.5,
            clock=_SteppingClock(),
            sleep=_SteppingClock().sleep,
        )

    assert guard.frames > 0
    assert guard.task_running is False
    assert result.outcome is BrowseOutcome.COMPLETED
    assert result.verification == 'progress_advanced'
    assert result.presses == 1
    assert len(result.taps) == 1
    assert result.arrival is not None
    assert result.arrival.arrived is True
    assert result.chip is not None
    assert result.chip.seen is True
    assert result.returned is True
    assert world.current == 'coins'


@pytest.mark.slow
def test_run_browse_task_completes_when_the_row_disappears(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(),),
            'destination': (_destination(chip=None), _destination(chip=None)),
            'coins': (_coin_frame(),),
            'boost_after': (_boost_frame(with_row=False),),
        },
        transitions={
            ('boost', 'go'): 'destination',
            ('coins', 'earn more'): 'boost_after',
        },
    )

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.COMPLETED
    assert result.verification == 'row_gone'
    assert result.recorded is True
    assert result.badge_after is None
    assert result.returned is True


@pytest.mark.slow
def test_run_browse_task_completes_on_a_done_label(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(),),
            'destination': (_destination(chip=None), _destination(chip=None)),
            'coins': (_coin_frame(),),
            'boost_after': (_boost_frame(action=None, done_label='Claimed'),),
        },
        transitions={
            ('boost', 'go'): 'destination',
            ('coins', 'earn more'): 'boost_after',
        },
    )

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.COMPLETED
    assert result.verification == 'action_completed'
    assert result.recorded is True


@pytest.mark.slow
def test_run_browse_task_completes_when_the_action_is_gone(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(),),
            'destination': (_destination(chip=None), _destination(chip=None)),
            'coins': (_coin_frame(),),
            'boost_after': (_boost_frame(action=None),),
        },
        transitions={
            ('boost', 'go'): 'destination',
            ('coins', 'earn more'): 'boost_after',
        },
    )

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.COMPLETED
    assert result.verification == 'action_gone'


@pytest.mark.slow
def test_run_browse_task_completes_when_the_badge_is_complete_and_before_was_unread(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before_frame = _boost_frame(badge=None)
    entry = DispatchGate(catalogue).authorize(
        next(entry for entry in enumerate_roster(before_frame, catalogue).entries if entry.key == 'sponsored_items')
    )
    world = _World(
        series={
            'boost': (before_frame,),
            'destination': (_destination(chip=None), _destination(chip=None)),
            'coins': (_coin_frame(),),
            'boost_after': (_boost_frame(badge='6/6'),),
        },
        transitions={
            ('boost', 'go'): 'destination',
            ('coins', 'earn more'): 'boost_after',
        },
    )

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        entry,
        navigate=world.navigate,
        arrival_timeout=1.0,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.COMPLETED
    assert result.verification == 'progress_complete'
    assert result.badge_before is None
    assert result.badge_after == '6/6'


@pytest.mark.slow
def test_run_browse_task_retries_the_press_up_to_the_budget(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    def on_tap(screen: str, label: str | None, count: int) -> str | None:
        if screen == 'boost' and label == 'go' and count == 3:
            return 'destination'
        return None

    world = _World(
        series={
            'boost': (_boost_frame(),),
            'destination': (
                _destination(chip='12s'),
                _destination(chip=None),
                _destination(chip=None),
            ),
            'coins': (_coin_frame(),),
            'boost_after': (_boost_frame(badge='1/6'),),
        },
        transitions={('coins', 'earn more'): 'boost_after'},
        on_tap=on_tap,
    )

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=0.5,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.presses == 3
    assert result.outcome is BrowseOutcome.COMPLETED


def test_run_browse_task_skips_at_the_daily_limit_before_any_capture(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    for _ in range(6):
        state.record('sponsored_items')
    capture = _ForbiddenCapture()
    navigate = _ForbiddenNavigate()
    world = _World({'boost': (_boost_frame(),)}, {})

    result = run_browse_task(
        _controller(world, world.frame),
        capture,
        corpus,
        state,
        _entry(catalogue),
        navigate=cast(RouteFirer, navigate),
    )

    assert result.outcome is BrowseOutcome.SKIPPED
    assert result.verification == 'daily_limit'
    assert result.done_today_before == 6
    assert result.done_today_after == 6
    assert result.limit == 6
    assert capture.calls == 0
    assert navigate.calls == 0
    assert result.returned is False


def test_run_browse_task_skips_a_vanished_row(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World({'boost': (_boost_frame(with_row=False),)}, {})
    state = _state(tmp_path)

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        state,
        _entry(catalogue),
        navigate=world.navigate,
        load_timeout=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.SKIPPED
    assert result.verification == 'row_vanished'
    assert result.before is not None
    assert result.presses == 0
    assert world.taps == []
    assert state.count('sponsored_items') == 0


@pytest.mark.parametrize('badge', ['0/6', None], ids=['with-badge', 'without-badge'])
@pytest.mark.slow
def test_run_browse_task_scrolls_to_a_row_below_the_fold(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    badge: str | None,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(with_row=False), _boost_frame(badge=badge)),
            'destination': (
                _destination(chip='12s'),
                _destination(chip='12s'),
                _destination(chip=None, collect=True),
                _destination(chip=None, collect=True),
            ),
            'collected': (_destination(chip=None),),
            'coins': (_coin_frame(),),
            'boost_after': (_boost_frame(badge='1/6'),),
        },
        transitions={
            ('boost', 'go'): 'destination',
            ('destination', 'collect'): 'collected',
            ('coins', 'earn more'): 'boost_after',
        },
    )
    device = _ScrollingDevice(world)
    controller = Controller.from_frame(device, world.frame, rng=random.Random(0))

    result = run_browse_task(
        controller,
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        load_timeout=1.0,
        arrival_timeout=1.0,
        chip_interval=0.5,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert device.swipes
    assert result.verification != 'row_vanished'
    assert result.presses == 1


def test_a_stable_noop_search_does_not_add_a_restore_swipe(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={'boost': (_boost_frame(with_row=False), _boost_frame(with_row=False))},
        transitions={},
    )
    device = _ScrollingDevice(world)
    controller = Controller.from_frame(device, world.frame, rng=random.Random(0))

    result = run_browse_task(
        controller,
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        load_timeout=1.0,
        settle=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.verification == 'row_vanished'
    assert len(device.swipes) == 1
    assert device.swipes[0][1] > device.swipes[0][3]


def test_a_failed_search_restores_only_the_moves_that_changed_the_frame(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    first = _boost_frame(with_row=False)
    moved = replace(first, image=Image.new('RGB', first.image.size, (80, 80, 80)))
    stable = replace(moved, image=Image.new('RGB', moved.image.size, (80, 80, 80)))
    world = _World(series={'boost': (first, moved, stable)}, transitions={})
    device = _ScrollingDevice(world)
    controller = Controller.from_frame(device, world.frame, rng=random.Random(0))

    result = run_browse_task(
        controller,
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        load_timeout=1.0,
        settle=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.verification == 'row_vanished'
    assert len(device.swipes) == 3
    first_move, second_move, restore = device.swipes
    assert first_move[1] > first_move[3]
    assert second_move[1] > second_move[3]
    assert restore[1] < restore[3]
    assert restore[0] == first_move[0]
    assert restore[2] == first_move[2]


def test_a_swipe_error_mid_search_restores_every_counted_move(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    first = _boost_frame(with_row=False)
    moved = replace(first, image=Image.new('RGB', first.image.size, (80, 80, 80)))
    world = _World(series={'boost': (first, moved)}, transitions={})
    device = _FailingSwipeDevice(world)
    controller = Controller.from_frame(device, world.frame, rng=random.Random(0))

    result = run_browse_task(
        controller,
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        load_timeout=1.0,
        settle=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.verification == 'row_vanished'
    assert len(device.swipes) == 3
    forward, first_restore, second_restore = device.swipes
    assert forward[1] > forward[3]
    assert first_restore[1] < first_restore[3]
    assert second_restore[1] < second_restore[3]


def test_a_restore_swipe_error_does_not_escape_the_executor(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    first = _boost_frame(with_row=False)
    moved = replace(first, image=Image.new('RGB', first.image.size, (80, 80, 80)))
    world = _World(series={'boost': (first, moved)}, transitions={})
    device = _PersistentSwipeFailure(world)
    controller = Controller.from_frame(device, world.frame, rng=random.Random(0))

    result = run_browse_task(
        controller,
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        load_timeout=1.0,
        settle=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.verification == 'row_vanished'
    assert len(device.swipes) == 1
    assert device.swipes[0][1] > device.swipes[0][3]


def test_run_browse_task_skips_a_row_with_no_action(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World({'boost': (_boost_frame(action=None),)}, {})

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        load_timeout=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.SKIPPED
    assert result.verification == 'row_complete'
    assert world.taps == []


def test_run_browse_task_fails_and_returns_when_the_entry_never_settles(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World({'loading': (_loading_frame(),), 'coins': (_coin_frame(),)}, {})

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        load_timeout=2.0,
        load_interval=0.5,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(step=1.0),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.FAILED
    assert result.verification == 'not_settled'
    assert result.returned is True
    assert world.current == 'coins'


def test_run_browse_task_fails_and_returns_on_a_non_task_surface(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World({'wrong': (_wrong_page(),), 'coins': (_coin_frame(),)}, {})

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        load_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.FAILED
    assert result.verification == 'task_surface_unavailable'
    assert result.returned is True
    assert world.current == 'coins'


def test_run_browse_task_stops_on_the_redeem_interstitial_and_still_returns(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(),),
            'redeem': (_redeem_frame(),),
            'coins': (_coin_frame(),),
        },
        transitions={('boost', 'go'): 'redeem'},
    )

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        load_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.FAILED
    assert result.verification == 'redeem_interstitial'
    assert result.stop == 'redeem_interstitial'
    assert result.returned is True
    assert world.current == 'coins'


def test_run_browse_task_returns_before_re_raising_an_unclosable_popup(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(),),
            'popup': (_popup_frame(),),
            'coins': (_coin_frame(),),
        },
        transitions={('boost', 'go'): 'popup'},
    )

    with pytest.raises(PopupUnclosable):
        run_browse_task(
            _controller(world, world.frame),
            world.capture,
            corpus,
            _state(tmp_path),
            _entry(catalogue),
            navigate=world.navigate,
            arrival_timeout=1.0,
            load_timeout=1.0,
            return_timeout=2.0,
            return_interval=0.5,
            clock=_SteppingClock(),
            sleep=_SteppingClock().sleep,
        )

    assert world.current == 'coins'
    assert len(world.navigations) >= 1


@pytest.mark.slow
def test_run_browse_task_reports_no_observation_when_the_list_cannot_be_read(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(),),
            'destination': (_destination(chip=None), _destination(chip=None)),
            'coins': (_coin_frame(),),
            'broken': (_wrong_page(),),
        },
        transitions={
            ('boost', 'go'): 'destination',
            ('coins', 'earn more'): 'broken',
        },
    )

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=1.0,
        return_interval=0.5,
        clock=_SteppingClock(step=1.0),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.FAILED
    assert result.verification == 'no_observation'
    assert result.returned is True


@pytest.mark.slow
def test_run_browse_task_fails_when_the_verification_return_fails(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(),),
            'destination': (_destination(chip=None), _destination(chip=None)),
            'coins': (_coin_frame(),),
            'boost_after': (_boost_frame(badge='1/6'),),
            'wrong': (_wrong_page(),),
        },
        transitions={
            ('boost', 'go'): 'destination',
            ('coins', 'earn more'): 'boost_after',
        },
    )

    def navigate(route: SurfaceRoute) -> None:
        world.navigations.append(route)
        if len(world.navigations) == 1:
            world.go('coins')
        else:
            world.go('wrong')

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=navigate,
        arrival_timeout=1.0,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=0.5,
        return_interval=0.5,
        clock=_SteppingClock(step=1.0),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.FAILED
    assert result.verification == 'return_failed'
    assert result.recorded is False


def test_a_failed_verification_return_keeps_the_first_return_dismissals(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def dismissal(family: str) -> DismissReport:
        return DismissReport(
            closed=(family,),
            attempts=(),
            frames=(),
            remaining=False,
            stop=DismissStop.CLEAR,
        )

    first = ReturnResult(
        returned=True,
        reentered=True,
        attempts=1,
        route=SurfaceRoute.SHORTCUT,
        elapsed=1.0,
        snapshot=_coin_frame(),
        navigations=(SurfaceRoute.SHORTCUT,),
        history=('page',),
        dismissals=(dismissal('first_modal'),),
    )
    second = ReturnResult(
        returned=False,
        reentered=True,
        attempts=1,
        route=SurfaceRoute.SHORTCUT,
        elapsed=2.0,
        navigations=(SurfaceRoute.SHORTCUT,),
        history=('wrong',),
        error='no coin markers',
        dismissals=(dismissal('second_modal'),),
    )
    returns = iter((first, second))
    monkeypatch.setattr(
        'aliexpress_coins.tasks.browse.return_to_coins_page',
        lambda *args, **kwargs: next(returns),
    )
    world = _World(
        series={
            'boost': (_boost_frame(),),
            'destination': (_destination(chip=None), _destination(chip=None)),
        },
        transitions={('boost', 'go'): 'destination'},
    )

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=0.5,
        return_interval=0.5,
        clock=_SteppingClock(step=1.0),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.FAILED
    assert result.verification == 'return_failed'
    assert result.return_state is not None
    assert result.return_state.attempts == 2
    assert [report.closed for report in result.return_state.dismissals] == [
        ('first_modal',),
        ('second_modal',),
    ]


def test_run_browse_task_rejects_a_denied_entry(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    roster = enumerate_roster(_boost_frame(), catalogue)
    denied = next(entry for entry in roster.entries if entry.denied)
    world = _World({'boost': (_boost_frame(),)}, {})

    with pytest.raises(TaskError, match='not dispatchable'):
        run_browse_task(
            _controller(world, world.frame),
            _ForbiddenCapture(),
            corpus,
            _state(tmp_path),
            denied,
            navigate=cast(RouteFirer, _ForbiddenNavigate()),
        )


def test_run_browse_task_rejects_a_non_browse_category(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    entry = _entry(catalogue)
    sign_in = catalogue.allowed_by_key('daily_checkin')
    wrong = replace(entry, decision=replace(entry.decision, spec=sign_in))
    world = _World({'boost': (_boost_frame(),)}, {})

    with pytest.raises(TaskError, match='not a browse_wait task'):
        run_browse_task(
            _controller(world, world.frame),
            _ForbiddenCapture(),
            corpus,
            _state(tmp_path),
            wrong,
            navigate=cast(RouteFirer, _ForbiddenNavigate()),
        )


@pytest.mark.parametrize(
    ('name', 'value'),
    [
        ('load_timeout', 0.0),
        ('arrival_timeout', 0.0),
        ('load_interval', 0.0),
        ('chip_interval', 0.0),
        ('return_timeout', 0.0),
        ('return_interval', 0.0),
        ('return_attempts', 0),
        ('settle', -1.0),
    ],
)
def test_run_browse_task_rejects_invalid_parameters(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    name: str,
    value: float,
) -> None:
    world = _World({'boost': (_boost_frame(),)}, {})

    with pytest.raises(ValueError, match=r'must be positive|must not be negative'):
        run_browse_task(
            _controller(world, world.frame),
            world.capture,
            corpus,
            _state(tmp_path),
            _entry(catalogue),
            navigate=world.navigate,
            **{name: value},  # type: ignore[arg-type]
        )


def test_browse_result_properties_and_dict_shape() -> None:
    result = BrowseResult(
        key='sponsored_items',
        title=TITLE,
        outcome=BrowseOutcome.FAILED,
        verification='no_change',
        detail='unchanged',
        limit=6,
        done_today_before=0,
        done_today_after=0,
        wait_seconds=15.0,
    )

    assert result.failed is True
    assert result.completed is False
    assert result.skipped is False
    assert result.badge_change is None
    assert result.returned is False
    payload = result.as_dict()
    assert payload['badge_change'] is None
    assert payload['chip'] is None
    assert payload['return'] is None


def test_arrival_and_return_result_render_their_evidence() -> None:
    arrival = Arrival(
        snapshot=_destination(),
        arrived=True,
        on_task_list=False,
        on_coin_page=False,
        elapsed=1.0,
        history=('page',),
    )
    returned = ReturnResult(
        returned=True,
        reentered=True,
        attempts=1,
        route=SurfaceRoute.SHORTCUT,
        elapsed=2.0,
        snapshot=_coin_frame(),
        navigations=(SurfaceRoute.SHORTCUT,),
        history=('page',),
    )
    skipped = ChipWait(status=ChipStatus.ABSENT)

    assert arrival.as_dict()['arrived'] is True
    assert returned.as_dict()['navigations'] == ['shortcut']
    assert skipped.as_dict()['status'] == 'absent'


def test_merged_return_keeps_the_earlier_sweeps_dismissals() -> None:
    def dismissal(family: str) -> DismissReport:
        return DismissReport(
            closed=(family,),
            attempts=(),
            frames=(),
            remaining=False,
            stop=DismissStop.CLEAR,
        )

    first = ReturnResult(
        returned=False,
        reentered=True,
        attempts=1,
        route=SurfaceRoute.SHORTCUT,
        elapsed=1.0,
        navigations=(SurfaceRoute.SHORTCUT,),
        history=('popup',),
        dismissals=(dismissal('launch_modal'),),
    )
    later = ReturnResult(
        returned=True,
        reentered=True,
        attempts=2,
        route=SurfaceRoute.SHORTCUT,
        elapsed=2.0,
        snapshot=_coin_frame(),
        navigations=(SurfaceRoute.WIDGET,),
        history=('page', 'page'),
        dismissals=(dismissal('promo_modal'),),
    )

    merged = first.merged(later)

    assert merged.returned is True
    assert merged.snapshot == later.snapshot
    assert merged.attempts == 3
    assert merged.navigations == (SurfaceRoute.SHORTCUT, SurfaceRoute.WIDGET)
    assert merged.history == ('popup', 'page', 'page')
    assert merged.elapsed == 3.0
    assert [report.closed for report in merged.dismissals] == [('launch_modal',), ('promo_modal',)]


def test_chip_reading_without_a_box_has_no_target_fields() -> None:
    reading = ChipReading(text=None, countdown=None, checked=False)

    payload = reading.as_dict()

    assert payload['countdown'] is None
    assert 'box' not in payload
    assert 'target' not in payload


def test_find_timer_chip_keeps_the_rightmost_reading_and_ignores_band_text() -> None:
    frame = _snapshot(
        _box('Hot picks', 1100, 900, 1180, 940),
        _box('12s', 1100, 1000, 1140, 1040),
        _box('12s', 1050, 1000, 1090, 1040),
        _box('✓', 1100, 1100, 1140, 1140),
        _box('check', 1050, 1100, 1090, 1140),
    )

    reading = find_timer_chip(frame)
    assert reading is not None
    assert reading.countdown == 12
    assert reading.box is not None
    assert reading.box.bbox[2] == 1140

    clear = find_timer_chip(
        _snapshot(
            _box('✓', 1100, 1100, 1140, 1140),
            _box('check', 1050, 1100, 1090, 1140),
        ),
    )
    assert clear is not None
    assert clear.box is not None
    assert clear.box.bbox[2] == 1140


def test_find_collect_control_skips_a_tall_box_and_keeps_the_rightmost() -> None:
    frame = _snapshot(
        _box('Collect', 60, 100, 300, 500),
        _box('Collect', 900, 1200, 1080, 1260),
        _box('Collect', 850, 1200, 1000, 1260),
    )

    control = find_collect_control(frame)

    assert control is not None
    assert control.bbox[2] == 1080


def test_return_to_coins_page_re_raises_an_unidentified_popup(corpus: PopupCorpus) -> None:
    world = _World({'boost': (_boost_frame(),), 'popup': (_popup_frame(),)}, {})

    def navigate(_route: SurfaceRoute) -> None:
        world.go('popup')

    with pytest.raises(PopupUnclosable):
        return_to_coins_page(
            _controller(world, world.frame),
            world.capture,
            corpus,
            cast(RouteFirer, navigate),
            snapshot=world.frame,
            timeout=0.5,
            interval=0.5,
            clock=_SteppingClock(),
            sleep=_SteppingClock().sleep,
        )


def test_return_to_coins_page_settles_between_attempts(corpus: PopupCorpus) -> None:
    world = _World({'boost': (_boost_frame(),), 'wrong': (_wrong_page(),)}, {})

    def navigate(_route: SurfaceRoute) -> None:
        world.go('wrong')

    result = return_to_coins_page(
        _controller(world, world.frame),
        world.capture,
        corpus,
        cast(RouteFirer, navigate),
        snapshot=world.frame,
        timeout=0.5,
        interval=0.5,
        attempts=1,
        settle=0.5,
        clock=_SteppingClock(step=1.0),
        sleep=_SteppingClock().sleep,
    )

    assert result.returned is False
    assert result.attempts == 1


@pytest.mark.slow
def test_run_browse_task_sweeps_a_popup_on_entry(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    launch: Snapshot,
    tmp_path: Path,
) -> None:
    def on_tap(screen: str, _label: str | None, _count: int) -> str | None:
        return 'boost' if screen == 'popup' else None

    world = _World(
        series={
            'popup': (launch,),
            'boost': (_boost_frame(),),
            'destination': (
                _destination(chip='12s'),
                _destination(chip=None),
                _destination(chip=None),
            ),
            'coins': (_coin_frame(),),
            'boost_after': (_boost_frame(badge='1/6'),),
        },
        transitions={
            ('boost', 'go'): 'destination',
            ('coins', 'earn more'): 'boost_after',
        },
        on_tap=on_tap,
    )

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        load_timeout=2.0,
        chip_interval=0.5,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.COMPLETED
    assert result.dismissals
    assert any('launch_welcome_gifts' in report.closed for report in result.dismissals)


@pytest.mark.slow
def test_run_browse_task_stops_pressing_when_the_action_disappears(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    def on_tap(screen: str, label: str | None, count: int) -> str | None:
        if screen == 'boost' and label == 'go' and count == 1:
            return 'no_action'
        return None

    world = _World(
        series={
            'boost': (_boost_frame(),),
            'no_action': (_boost_frame(action=None),),
            'coins': (_coin_frame(),),
            'boost_after': (_boost_frame(),),
        },
        transitions={('coins', 'earn more'): 'boost_after'},
        on_tap=on_tap,
    )

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=0.5,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=1.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.presses == 1
    assert result.outcome is BrowseOutcome.FAILED


@pytest.mark.slow
def test_run_browse_task_sweeps_a_popup_on_the_way_to_the_destination(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    launch: Snapshot,
    tmp_path: Path,
) -> None:
    def on_tap(screen: str, _label: str | None, count: int) -> str | None:
        # The GO tap does nothing; the popup appears while the arrival poll is
        # running, and closing it reveals the destination page.
        return 'destination' if screen == 'boost' and count == 2 else None

    world = _World(
        series={
            # Entry capture, the post-tap sweep, then the popup that the arrival
            # poll sees. The GO tap itself does nothing.
            'boost': (_boost_frame(), _boost_frame(), launch),
            'destination': (_destination(chip=None),),
            'coins': (_coin_frame(),),
            'boost_after': (_boost_frame(badge='1/6'),),
        },
        transitions={('coins', 'earn more'): 'boost_after'},
        on_tap=on_tap,
    )

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=2.0,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(step=0.5),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.COMPLETED
    assert result.arrival is not None
    assert result.arrival.dismiss is not None


@pytest.mark.slow
def test_run_browse_task_reports_a_chip_timeout_but_still_verifies(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    entry = _entry(catalogue)
    spec = entry.spec
    assert spec is not None
    short = replace(entry, decision=replace(entry.decision, spec=spec.model_copy(update={'wait_seconds': 1.0})))
    world = _World(
        series={
            'boost': (_boost_frame(),),
            'destination': (_destination(chip='9s'),),
            'coins': (_coin_frame(),),
            'boost_after': (_boost_frame(badge='1/6'),),
        },
        transitions={
            ('boost', 'go'): 'destination',
            ('coins', 'earn more'): 'boost_after',
        },
    )

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        short,
        navigate=world.navigate,
        arrival_timeout=1.0,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(step=0.5),
        sleep=_SteppingClock().sleep,
    )

    assert result.chip is not None
    assert result.chip.status is ChipStatus.TIMED_OUT
    assert result.outcome is BrowseOutcome.COMPLETED


def test_run_browse_task_returns_failed_when_the_first_return_fails(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(),),
            'destination': (_destination(chip=None), _destination(chip=None)),
            'wrong': (_wrong_page(),),
        },
        transitions={('boost', 'go'): 'destination'},
    )

    def navigate(_route: SurfaceRoute) -> None:
        world.go('wrong')

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=cast(RouteFirer, navigate),
        arrival_timeout=1.0,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=0.5,
        return_interval=0.5,
        return_attempts=1,
        clock=_SteppingClock(step=1.0),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.FAILED
    assert result.verification == 'return_failed'


@pytest.mark.slow
def test_run_browse_task_sweeps_a_popup_before_verification(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    launch: Snapshot,
    tmp_path: Path,
) -> None:
    def on_tap(screen: str, _label: str | None, _count: int) -> str | None:
        return 'boost_after' if screen == 'verify_popup' else None

    world = _World(
        series={
            'boost': (_boost_frame(),),
            'destination': (_destination(chip=None), _destination(chip=None)),
            'coins': (_coin_frame(),),
            'verify_popup': (launch,),
            'boost_after': (_boost_frame(badge='1/6'),),
        },
        transitions={
            ('boost', 'go'): 'destination',
            ('coins', 'earn more'): 'verify_popup',
        },
        on_tap=on_tap,
    )

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        load_timeout=20.0,
        chip_interval=0.5,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(step=0.5),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.COMPLETED
    assert any('launch_welcome_gifts' in report.closed for report in result.dismissals)


def test_run_browse_task_reports_return_failed_when_an_early_failure_cannot_return(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World({'loading': (_loading_frame(),), 'wrong': (_wrong_page(),)}, {})

    def navigate(_route: SurfaceRoute) -> None:
        world.go('wrong')

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=cast(RouteFirer, navigate),
        load_timeout=0.5,
        load_interval=0.5,
        return_timeout=0.5,
        return_interval=0.5,
        return_attempts=1,
        clock=_SteppingClock(step=1.0),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.FAILED
    assert result.verification == 'return_failed'


@pytest.mark.slow
def test_run_browse_task_fails_when_the_press_bounces_to_the_coin_page(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(),),
            'coins': (_coin_frame(),),
            'boost_after': (_boost_frame(),),
        },
        transitions={
            ('boost', 'go'): 'coins',
            ('coins', 'earn more'): 'boost_after',
        },
    )

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=1.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.FAILED
    assert result.arrival is not None
    assert result.arrival.on_coin_page is True
    assert result.returned is True


@pytest.mark.slow
def test_run_browse_task_fails_on_the_inline_route_when_the_row_is_unchanged(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(),),
            'destination': (_destination(chip=None), _destination(chip=None)),
            'coins': (_coin_frame(inline=True, badge='0/6', earn_more=False),),
        },
        transitions={('boost', 'go'): 'destination'},
    )

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=1.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.FAILED
    assert result.verification == 'no_change'
    assert result.returned is True


@pytest.mark.slow
def test_run_browse_task_does_not_trust_row_absence_on_the_inline_coin_page(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    other_inline = _snapshot(
        _box('Coins', 101, 194, 254, 242),
        _box('Coins credits to redeem', 59, 567, 552, 617),
        _box('Daily coins', 66, 919, 320, 978),
        _box('Take the coins quiz', 60, 1200, 700, 1250),
        _box('GO', 1000, 1260, 1080, 1310),
    )
    world = _World(
        series={
            'boost': (_boost_frame(),),
            'destination': (_destination(chip=None), _destination(chip=None)),
            'coins': (other_inline,),
        },
        transitions={('boost', 'go'): 'destination'},
    )
    state = _state(tmp_path)

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        state,
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=1.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.FAILED
    assert result.verification == 'no_observation'
    assert result.recorded is False
    assert result.returned is True
    assert state.count('sponsored_items') == 0


@pytest.mark.slow
def test_run_browse_task_reports_no_observation_on_an_empty_boost_list(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(),),
            'destination': (_destination(chip=None), _destination(chip=None)),
            'coins': (_coin_frame(),),
            'empty': (_empty_boost_frame(),),
        },
        transitions={
            ('boost', 'go'): 'destination',
            ('coins', 'earn more'): 'empty',
        },
    )

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=1.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.FAILED
    assert result.verification == 'no_observation'
    assert result.returned is True


@pytest.mark.slow
def test_run_browse_task_works_without_the_boost_family_in_the_corpus(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    trimmed = replace(
        corpus,
        families=tuple(family for family in corpus.families if family.family != 'coins_boost_sheet'),
    )
    world = _success_world()

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        trimmed,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.COMPLETED


def test_run_browse_task_returns_before_re_raising_when_the_return_also_fails(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(),),
            'popup': (_popup_frame(),),
        },
        transitions={('boost', 'go'): 'popup'},
    )
    navigate = _RaisingNavigate()

    with pytest.raises(PopupUnclosable):
        run_browse_task(
            _controller(world, world.frame),
            world.capture,
            corpus,
            _state(tmp_path),
            _entry(catalogue),
            navigate=cast(RouteFirer, navigate),
            arrival_timeout=1.0,
            load_timeout=1.0,
            return_timeout=0.5,
            return_interval=0.5,
            return_attempts=1,
            clock=_SteppingClock(),
            sleep=_SteppingClock().sleep,
        )

    assert navigate.calls >= 1


def test_run_browse_task_stops_on_a_redeem_that_appears_after_the_chip(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(),),
            'destination': (
                _destination(chip='12s'),
                _destination(chip='12s'),
                _redeem_frame(),
            ),
            'coins': (_coin_frame(),),
        },
        transitions={('boost', 'go'): 'destination'},
    )

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=1.0,
        return_interval=0.5,
        clock=_SteppingClock(step=0.5),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.FAILED
    assert result.verification == 'redeem_interstitial'
    assert result.returned is True


@pytest.mark.slow
def test_run_browse_task_stops_on_a_redeem_that_appears_after_collect(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(),),
            'destination': (
                _destination(chip='12s'),
                _destination(chip=None, collect=True),
                _destination(chip=None, collect=True),
            ),
            'redeem': (_redeem_frame(),),
            'coins': (_coin_frame(),),
        },
        transitions={
            ('boost', 'go'): 'destination',
            ('destination', 'collect'): 'redeem',
        },
    )

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=1.0,
        return_interval=0.5,
        clock=_SteppingClock(step=0.5),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.FAILED
    assert result.verification == 'redeem_interstitial'
    assert result.collect is not None
    assert result.returned is True


def test_run_browse_task_fails_when_an_entry_popup_outlives_the_budget(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    launch: Snapshot,
    tmp_path: Path,
) -> None:
    world = _World({'popup': (launch,), 'coins': (_coin_frame(),)}, {})

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        load_timeout=0.5,
        load_interval=0.5,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(step=1.0),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.FAILED
    assert result.verification == 'not_settled'
    assert world.current == 'coins'


def test_run_browse_task_stops_on_a_redeem_at_entry(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World({'redeem': (_redeem_frame(),), 'coins': (_coin_frame(),)}, {})

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        load_timeout=2.0,
        load_interval=0.5,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.FAILED
    assert result.verification == 'not_settled'
    assert world.current == 'coins'


@pytest.mark.slow
def test_run_browse_task_stops_when_a_redeem_appears_during_arrival(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(), _boost_frame(), _redeem_frame()),
            'coins': (_coin_frame(),),
        },
        transitions={},
    )

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=2.0,
        load_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.FAILED
    assert result.verification == 'redeem_interstitial'
    assert result.returned is True


@pytest.mark.slow
def test_run_browse_task_waits_out_a_loading_frame_on_arrival(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_boost_frame(),),
            'loading': (_loading_frame(), _destination(chip=None), _destination(chip=None)),
            'coins': (_coin_frame(),),
            'boost_after': (_boost_frame(badge='1/6'),),
        },
        transitions={
            ('boost', 'go'): 'loading',
            ('coins', 'earn more'): 'boost_after',
        },
    )

    result = run_browse_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=2.0,
        load_timeout=1.0,
        chip_interval=0.5,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is BrowseOutcome.COMPLETED
    assert result.arrival is not None
    assert result.arrival.history[0] == 'loading'
    assert result.arrival.arrived is True


@pytest.mark.slow
def test_return_to_coins_page_sweeps_a_popup_that_reveals_a_wrong_page(
    corpus: PopupCorpus,
    launch: Snapshot,
) -> None:
    def on_tap(screen: str, _label: str | None, _count: int) -> str | None:
        return 'wrong' if screen == 'popup' else None

    world = _World(
        {'boost': (_boost_frame(),), 'popup': (launch,), 'wrong': (_wrong_page(),)},
        {},
        on_tap=on_tap,
    )

    def navigate(_route: SurfaceRoute) -> None:
        world.go('popup')

    result = return_to_coins_page(
        _controller(world, world.frame),
        world.capture,
        corpus,
        cast(RouteFirer, navigate),
        snapshot=world.frame,
        timeout=2.0,
        interval=0.5,
        attempts=1,
        clock=_SteppingClock(step=0.5),
        sleep=_SteppingClock().sleep,
    )

    assert result.returned is False
    assert result.attempts == 1
