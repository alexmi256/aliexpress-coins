"""The shortcut-visit row: recognise the sheet, never infer a launcher claim (#130)."""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from aliexpress_coins.control.controller import Controller, TapRecord
from aliexpress_coins.control.popups import PopupCorpus, load_popup_corpus
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.runner.errors import RunInterrupted
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks import shortcut as shortcut_module
from aliexpress_coins.tasks.catalogue import TaskCatalogue, load_task_catalogue
from aliexpress_coins.tasks.discovery import BoostList, DiscoveryRoute, RosterEntry, enumerate_roster
from aliexpress_coins.tasks.dispatch import DispatchGate
from aliexpress_coins.tasks.errors import TaskError
from aliexpress_coins.tasks.shortcut import ShortcutVisitOutcome, run_shortcut_visit_task
from aliexpress_coins.tasks.surface import SurfaceRoute

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE_PATH = ROOT / 'specs' / 'tasks.yaml'
CORPUS_PATH = ROOT / 'specs' / 'popups.yaml'
CAPTURED_AT = datetime(2026, 11, 24, 18, 0, tzinfo=UTC)


class _Device:
    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []
        self.swipes: list[tuple[int, int, int, int, int]] = []
        self.keys: list[str] = []

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        self.swipes.append((x1, y1, x2, y2, duration_ms))

    def keyevent(self, key: str) -> None:
        self.keys.append(key)


class _Capture:
    def __init__(self, frames: Sequence[Snapshot]) -> None:
        self._frames = tuple(frames)
        self._index = 0

    def capture(self) -> Snapshot:
        frame = self._frames[min(self._index, len(self._frames) - 1)]
        self._index += 1
        return frame


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        self.value += 1.0
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class _Routes:
    def __init__(self) -> None:
        self.routes: list[SurfaceRoute] = []

    def __call__(self, route: SurfaceRoute) -> None:
        self.routes.append(route)


def _box(text: str, left: float, top: float, right: float, bottom: float) -> TextBox:
    return TextBox(
        text=text,
        box=((left, top), (right, top), (right, bottom), (left, bottom)),
        score=0.99,
    )


def _snapshot(*boxes: TextBox, shade: str = 'white') -> Snapshot:
    return Snapshot(
        image=Image.new('RGB', (1200, 2000), shade),
        boxes=boxes,
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(1200, 2000, density=240, sdk=35),
    )


def _boost(balance: str = '87', *, shade: str = 'white', with_row: bool = True) -> Snapshot:
    boxes = [_box('Coins boost', 100, 450, 500, 540), _box(balance, 100, 290, 300, 395)]
    if with_row:
        boxes.extend(
            [
                _box('Visit the page via coins', 246, 1723, 701, 1776),
                _box('shortcut', 249, 1781, 421, 1823),
                _box('Unlock a daily surprise', 247, 1834, 661, 1883),
                _box('GO', 965, 1824, 1049, 1878),
                _box('+1', 327, 1870, 376, 1911),
            ],
        )
    return _snapshot(*boxes, shade=shade)


def _coin_page(balance: str = '87', *, shade: str = 'white') -> Snapshot:
    return _snapshot(
        _box('Coins', 100, 220, 300, 280),
        _box('Coins credits to redeem', 100, 330, 700, 380),
        _box('Daily coins', 100, 560, 500, 610),
        _box(balance, 100, 400, 260, 460),
        shade=shade,
    )


def _coin_page_with_row(balance: str = '87', *, shade: str = 'white') -> Snapshot:
    """A coin page (not a boost sheet) that still carries the measured wrapped row."""
    return _snapshot(
        _box('Coins credits to redeem', 100, 330, 700, 380),
        _box('Daily coins', 100, 560, 500, 610),
        _box(balance, 100, 400, 260, 460),
        _box('Visit the page via coins', 246, 1723, 701, 1776),
        _box('shortcut', 249, 1781, 421, 1823),
        _box('Unlock a daily surprise', 247, 1834, 661, 1883),
        _box('GO', 965, 1824, 1049, 1878),
        _box('+1', 327, 1870, 376, 1911),
        shade=shade,
    )


def _boost_without_shortcut(balance: str = '87', *, shade: str = 'white') -> Snapshot:
    """A real boost list with a task row, but not the shortcut row."""
    return _snapshot(
        _box('Coins boost', 100, 450, 500, 540),
        _box(balance, 100, 290, 300, 395),
        _box('Place an order on this page', 246, 1723, 701, 1776),
        _box('GO', 965, 1824, 1049, 1878),
        shade=shade,
    )


def _reopened_boost(snapshot: Snapshot, *, fresh: bool = True) -> BoostList:
    """A boost open result; ``fresh=False`` models an already-open sheet."""
    tap = (
        TapRecord(
            index=1,
            label='Earn more',
            reason='test: re-open the Coins boost list',
            requested=(1, 1),
            clamped=(1, 1),
            jitter=(0, 0),
            point=(1, 1),
            suppressed=False,
        )
        if fresh
        else None
    )
    return BoostList(
        snapshot=snapshot,
        route=DiscoveryRoute.BOOST,
        detail='reopened' if fresh else 'the sheet was already open',
        tap=tap,
    )


def _shortcut_sheet() -> Snapshot:
    return _snapshot(
        _box("Once you've created a shortcut, find it", 183, 471, 1020, 526),
        _box('on your home screen. Tap it to enter', 204, 530, 997, 594),
        _box('Got it', 516, 1235, 686, 1298),
        _box('Create shortcut', 386, 1398, 816, 1456),
        shade='#d0d0d0',
    )


def _shortcut_exists_sheet() -> Snapshot:
    return _snapshot(
        _box("Once you've created a shortcut, find it", 183, 471, 1020, 526),
        _box('on your home screen. Tap it to enter', 204, 530, 997, 594),
        _box('Shortcut has existed', 480, 1040, 718, 1090),
        _box('Got it', 516, 1235, 686, 1298),
        _box('Create shortcut', 386, 1398, 816, 1456),
        shade='#d0d0d0',
    )


def _launcher_add_sheet() -> Snapshot:
    return _snapshot(
        _box("Once you've created a shortcut, find it", 183, 471, 1020, 526),
        _box('on your home screen. Tap it to enter', 204, 530, 997, 594),
        _box('AliExpress', 518, 1446, 680, 1488),
        _box('Coins', 568, 1762, 632, 1794),
        _box('Cancel', 662, 1905, 785, 1955),
        _box('Add to home screen', 810, 1905, 1084, 1955),
        shade='#d0d0d0',
    )


def _home_with_shortcut() -> Snapshot:
    return _snapshot(
        _box('2:10', 19, 5, 70, 34),
        _box('AliExpress', 633, 1203, 742, 1233),
        _box('Coins', 832, 1201, 897, 1233),
        _box('Settings', 996, 1480, 1086, 1510),
        shade='#3d6f70',
    )


def _created_shortcut_frames(initial: Snapshot, after: str = '88') -> tuple[Snapshot, ...]:
    """The measured sheet -> launcher -> app -> reload path for one attempt."""
    return (
        initial,
        _shortcut_sheet(),
        _launcher_add_sheet(),
        _shortcut_sheet(),
        _shortcut_sheet(),
        _coin_page(after),
    )


def _existing_shortcut_frames(initial: Snapshot, after: str = '88') -> tuple[Snapshot, ...]:
    return (
        initial,
        _shortcut_sheet(),
        _shortcut_exists_sheet(),
        _shortcut_exists_sheet(),
        _coin_page(after),
    )


@pytest.fixture(scope='module')
def catalogue() -> TaskCatalogue:
    return load_task_catalogue(CATALOGUE_PATH)


@pytest.fixture(scope='module')
def corpus() -> PopupCorpus:
    return load_popup_corpus(CORPUS_PATH)


def _entry(frame: Snapshot, catalogue: TaskCatalogue) -> RosterEntry:
    roster = enumerate_roster(frame, catalogue)
    entry = next(item for item in roster.entries if item.key == 'coins_shortcut_visit')
    return DispatchGate(catalogue).authorize(entry)


def _state(tmp_path: Path) -> DailyStateStore:
    return DailyStateStore(tmp_path / 'state', clock=lambda: CAPTURED_AT)


def test_the_sheet_is_accepted_and_the_created_shortcut_is_used_and_removed(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)
    device = _Device()
    controller = Controller.from_frame(device, frame, rng=random.Random(0))
    routes = _Routes()
    launched: list[str] = []
    removed: list[str] = []
    state = _state(tmp_path)

    result = run_shortcut_visit_task(
        controller,
        _Capture(_created_shortcut_frames(frame)),
        corpus,
        state,
        entry,
        navigate=routes,
        launch_shortcut=lambda: launched.append('fired'),
        remove_shortcut=lambda: removed.append('cleared'),
        app_foreground=lambda: True,
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.COMPLETED
    assert result.completed is True
    assert result.verification == 'balance_delta'
    assert (result.balance_before, result.balance_after) == (87.0, 88.0)
    assert result.shortcut_created is True
    assert result.shortcut_removed is True
    assert result.recorded is True
    assert launched == ['fired']
    assert removed == ['cleared']
    assert [tap.label for tap in result.taps] == [entry.title, 'Create shortcut', 'Add to home screen', 'Got it']
    assert 'Add to home screen' in result.modal_labels
    assert 'Coins' in result.modal_labels
    assert device.keys == []
    assert routes.routes == [SurfaceRoute.SHORTCUT]
    assert state.count('coins_shortcut_visit') == 1


def test_a_live_boost_start_brackets_the_balance_on_the_coin_page(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _boost('87')
    entry = _entry(frame, catalogue)
    routes = _Routes()
    reopened = _boost('87')
    monkeypatch.setattr(
        shortcut_module,
        'open_boost_list',
        lambda *_args, **_kwargs: _reopened_boost(reopened),
    )
    frames = (
        frame,
        _coin_page('87'),
        _shortcut_sheet(),
        _launcher_add_sheet(),
        _shortcut_sheet(),
        _shortcut_sheet(),
        _coin_page('88'),
    )

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture(frames),
        corpus,
        _state(tmp_path),
        entry,
        navigate=routes,
        launch_shortcut=lambda: None,
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.COMPLETED
    assert (result.balance_before, result.balance_after) == (87.0, 88.0)
    # One route reads the trusted balance before GO; the other reloads after the
    # shortcut launch so the server-side +1 can appear.
    assert routes.routes == [SurfaceRoute.SHORTCUT, SurfaceRoute.SHORTCUT]


def test_a_failed_preflight_route_still_brackets_and_uses_the_shortcut(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _boost('87')
    entry = _entry(frame, catalogue)
    reopened = _boost('87')
    monkeypatch.setattr(
        shortcut_module,
        'open_boost_list',
        lambda *_args, **_kwargs: _reopened_boost(reopened),
    )
    frames = (
        frame,
        _coin_page('87'),
        _shortcut_sheet(),
        _launcher_add_sheet(),
        _shortcut_sheet(),
        _shortcut_sheet(),
        _coin_page('88'),
    )

    def broken(_route: SurfaceRoute) -> None:
        raise RuntimeError('route unavailable')

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture(frames),
        corpus,
        _state(tmp_path),
        entry,
        navigate=cast(Any, broken),
        launch_shortcut=lambda: None,
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.COMPLETED
    assert (result.balance_before, result.balance_after) == (87.0, 88.0)


def test_a_failed_boost_reopen_during_the_balance_pre_read_fails_honestly(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _boost('87')
    entry = _entry(frame, catalogue)

    def broken(*_args: Any, **_kwargs: Any) -> BoostList:
        raise RuntimeError('boost route unavailable')

    monkeypatch.setattr(shortcut_module, 'open_boost_list', broken)

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture((frame, _coin_page('87'))),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.FAILED
    assert result.balance_before == 87.0
    assert result.taps == ()


def test_a_stop_during_the_balance_pre_read_boost_reopen_is_never_swallowed(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _boost('87')
    entry = _entry(frame, catalogue)

    def stop(*_args: Any, **_kwargs: Any) -> BoostList:
        raise RunInterrupted('stop requested during balance re-open')

    monkeypatch.setattr(shortcut_module, 'open_boost_list', stop)

    with pytest.raises(RunInterrupted, match='stop requested during balance re-open'):
        run_shortcut_visit_task(
            Controller.from_frame(_Device(), frame, rng=random.Random(0)),
            _Capture((frame, _coin_page('87'))),
            corpus,
            _state(tmp_path),
            entry,
            navigate=_Routes(),
            clock=_Clock(),
            sleep=_Clock().sleep,
        )


def test_a_stop_during_the_balance_pre_read_route_is_never_swallowed(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost('87')
    entry = _entry(frame, catalogue)

    def stop(_route: SurfaceRoute) -> None:
        raise RunInterrupted('stop requested during balance route')

    with pytest.raises(RunInterrupted, match='stop requested during balance route'):
        run_shortcut_visit_task(
            Controller.from_frame(_Device(), frame, rng=random.Random(0)),
            _Capture((frame,)),
            corpus,
            _state(tmp_path),
            entry,
            navigate=cast(Any, stop),
            clock=_Clock(),
            sleep=_Clock().sleep,
        )


def test_a_stop_during_the_shortcut_presence_probe_is_never_swallowed(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)

    def stop() -> bool | None:
        raise RunInterrupted('stop requested during presence probe')

    with pytest.raises(RunInterrupted, match='stop requested during presence probe'):
        run_shortcut_visit_task(
            Controller.from_frame(_Device(), frame, rng=random.Random(0)),
            _Capture(_created_shortcut_frames(frame)),
            corpus,
            _state(tmp_path),
            entry,
            navigate=_Routes(),
            shortcut_present=stop,
            launch_shortcut=lambda: None,
            clock=_Clock(),
            sleep=_Clock().sleep,
        )


def test_a_pin_that_appears_after_the_read_is_created_and_cleaned_up(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)
    readings = iter((False, True))
    removed: list[str] = []

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture((frame, _shortcut_sheet(), _coin_page('88'))),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        shortcut_present=lambda: next(readings, True),
        launch_shortcut=lambda: None,
        remove_shortcut=lambda: removed.append('cleared'),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.COMPLETED
    assert result.shortcut_created is True
    assert result.shortcut_removed is True
    assert removed == ['cleared']


def test_the_launch_waits_for_a_changed_page_before_the_reload(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)
    sleeps: list[float] = []
    frames = (
        frame,
        _shortcut_sheet(),
        _launcher_add_sheet(),
        _shortcut_sheet(),
        _shortcut_sheet(),
        _coin_page('87'),  # captured immediately before the launch
        _coin_page('87'),  # the stale page the launch has not replaced yet
        _snapshot(),  # loading
        _coin_page('88'),  # the shortcut's own settled page
    )

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture(frames),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        launch_shortcut=lambda: None,
        clock=_Clock(),
        sleep=sleeps.append,
    )

    assert result.outcome is ShortcutVisitOutcome.COMPLETED
    assert (result.balance_before, result.balance_after) == (87.0, 88.0)
    assert max(sleeps) < 6.0


def test_an_existing_shortcut_is_reused_without_a_second_add_tap(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)
    removed: list[str] = []

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture(_existing_shortcut_frames(frame)),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        launch_shortcut=lambda: None,
        remove_shortcut=lambda: removed.append('cleared'),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.COMPLETED
    assert result.shortcut_created is False
    assert result.shortcut_removed is False
    assert removed == []
    assert 'Add to home screen' not in [tap.label for tap in result.taps]
    assert [tap.label for tap in result.taps] == [entry.title, 'Create shortcut', 'Got it']
    assert 'Shortcut has existed' in result.modal_labels


def test_a_pinned_shortcut_without_a_toast_is_reused(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)
    removed: list[str] = []

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture((frame, _shortcut_sheet(), _coin_page('88'))),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        shortcut_present=lambda: True,
        launch_shortcut=lambda: None,
        remove_shortcut=lambda: removed.append('cleared'),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.COMPLETED
    assert result.shortcut_created is False
    assert result.shortcut_removed is False
    assert removed == []


@pytest.mark.parametrize(
    'home',
    [
        lambda: _snapshot(_box('AliExpress', 633, 1203, 742, 1233), _box('Settings', 996, 1480, 1086, 1510)),
        lambda: _snapshot(_box('Coins', 832, 1201, 897, 1233)),
        lambda: _snapshot(
            _box('Daily coins', 65, 917, 319, 979),
            _box('AliExpress', 633, 1203, 742, 1233),
            _box('Coins', 832, 1201, 897, 1233),
        ),
    ],
)
def test_a_home_screen_without_a_proven_coins_shortcut_fails_honestly(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    home: Callable[[], Snapshot],
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)
    frames = (
        frame,
        _shortcut_sheet(),
        _launcher_add_sheet(),
        _shortcut_sheet(),
        _shortcut_sheet(),
        home(),
    )

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture(frames),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.FAILED
    assert result.verification == 'launcher_shortcut_not_found'
    assert result.shortcut_created is True


def test_a_sheet_without_its_create_control_fails_without_a_guess(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)
    sheet = _snapshot(_box('Got it', 516, 1235, 686, 1298), shade='#d0d0d0')
    removed: list[str] = []

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture((frame, sheet, _shortcut_sheet(), _coin_page('87'))),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        remove_shortcut=lambda: removed.append('cleared'),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.FAILED
    assert result.verification == 'shortcut_sheet_no_create'
    assert result.shortcut_created is False
    assert result.shortcut_removed is False
    assert removed == []
    assert [tap.label for tap in result.taps] == [entry.title, 'Got it']


def test_a_failed_shortcut_launch_still_returns_to_the_coins_page_and_cleans_up(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)
    removed: list[str] = []

    def broken_launch() -> None:
        raise RuntimeError('intent unavailable')

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture(_created_shortcut_frames(frame)),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        launch_shortcut=broken_launch,
        remove_shortcut=lambda: removed.append('cleared'),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.FAILED
    assert result.verification == 'shortcut_launch_failed'
    assert 'intent unavailable' in result.detail
    assert result.shortcut_created is True
    assert result.shortcut_removed is True
    assert removed == ['cleared']


@pytest.mark.parametrize('focus', [False, None])
def test_a_launcher_frame_after_the_dispatch_is_a_foreground_loss(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    focus: bool | None,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)
    frames = (
        frame,
        _shortcut_sheet(),
        _launcher_add_sheet(),
        _shortcut_sheet(),
        _shortcut_sheet(),
        _home_with_shortcut(),
    )

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture(frames),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        launch_shortcut=lambda: None,
        app_foreground=lambda: focus,
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.FAILED
    assert result.verification == 'foreground_lost_after_action'
    assert f'app_foreground={focus!r}' in result.detail
    assert result.recorded is False


def test_a_boost_page_after_the_dispatch_is_not_a_foreground_loss(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)
    frames = (
        frame,
        _shortcut_sheet(),
        _launcher_add_sheet(),
        _shortcut_sheet(),
        _shortcut_sheet(),
        _coin_page('87'),
        _boost('88'),
    )

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture(frames),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        launch_shortcut=lambda: None,
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.FAILED
    assert result.verification != 'foreground_lost_after_action'
    assert result.verification == 'return_to_coins_page_failed'


def test_a_reward_interstitial_after_the_dispatch_is_not_a_foreground_loss(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)
    interstitial = _snapshot(_box('YOU CAN NOW REDEEM!', 100, 700, 900, 760))
    frames = (
        frame,
        _shortcut_sheet(),
        _launcher_add_sheet(),
        _shortcut_sheet(),
        _shortcut_sheet(),
        _coin_page('87'),
        interstitial,
    )

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture(frames),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        launch_shortcut=lambda: None,
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.FAILED
    assert result.verification == 'return_to_coins_page_failed'


def test_the_home_shortcut_is_used_when_no_launcher_intent_is_injected(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)
    device = _Device()
    frames = (
        frame,
        _shortcut_sheet(),
        _launcher_add_sheet(),
        _shortcut_sheet(),
        _shortcut_sheet(),
        _home_with_shortcut(),
        _home_with_shortcut(),
        _coin_page('88'),
    )

    result = run_shortcut_visit_task(
        Controller.from_frame(device, frame, rng=random.Random(0)),
        _Capture(frames),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.COMPLETED
    assert device.keys == ['KEYCODE_HOME']
    assert [tap.label for tap in result.taps] == [
        entry.title,
        'Create shortcut',
        'Add to home screen',
        'Got it',
        'Coins shortcut',
    ]


def test_a_cleanup_failure_does_not_turn_a_verified_credit_into_a_failure(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)

    def broken_cleanup() -> bool:
        raise RuntimeError('shortcut service busy')

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture(_created_shortcut_frames(frame)),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        launch_shortcut=lambda: None,
        remove_shortcut=broken_cleanup,
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.COMPLETED
    assert result.shortcut_removed is False
    assert 'app-level shortcut cleanup' in result.detail


def test_a_readable_balance_increase_is_the_only_completion_evidence(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)
    state = _state(tmp_path)

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture(_created_shortcut_frames(frame)),
        corpus,
        state,
        entry,
        navigate=_Routes(),
        launch_shortcut=lambda: None,
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.COMPLETED
    assert result.verification == 'balance_delta'
    assert (result.balance_before, result.balance_after) == (87.0, 88.0)
    assert result.recorded is True
    assert state.count('coins_shortcut_visit') == 1


def test_a_two_coin_balance_change_is_not_accepted_as_the_advertised_plus_one(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)
    state = _state(tmp_path)

    def broken_cleanup() -> bool:
        raise RuntimeError('shortcut service busy')

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture(_created_shortcut_frames(frame, after='89')),
        corpus,
        state,
        entry,
        navigate=_Routes(),
        launch_shortcut=lambda: None,
        remove_shortcut=broken_cleanup,
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.FAILED
    assert result.verification == 'shortcut_sheet_no_credit'
    assert (result.balance_before, result.balance_after) == (87.0, 89.0)
    assert result.recorded is False
    assert result.shortcut_removed is False
    assert 'app-level shortcut cleanup' in result.detail
    assert state.count('coins_shortcut_visit') == 0


def test_the_row_is_found_below_the_fold_by_scrolling(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    top = _boost(with_row=False, shade='white')
    below = _boost(with_row=True, shade='#e0e0e0')
    entry = _entry(below, catalogue)
    device = _Device()
    monkeypatch.setattr(
        shortcut_module,
        'open_boost_list',
        lambda *_args, **_kwargs: _reopened_boost(below),
    )

    result = run_shortcut_visit_task(
        Controller.from_frame(device, top, rng=random.Random(0)),
        _Capture((top, below, _coin_page('87'), _shortcut_sheet(), _coin_page('88'))),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.FAILED
    assert device.swipes
    assert result.taps[0].label == entry.title


def test_an_exhausted_scrolled_list_without_the_row_is_row_vanished(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    """Positive control: every swipe moved and the attempt bound ended the read."""
    entry = _entry(_boost(), catalogue)
    device = _Device()
    frames = (
        _boost_without_shortcut(shade='white'),
        _boost_without_shortcut(shade='#e0e0e0'),
        _boost_without_shortcut(shade='#d0d0d0'),
        _boost_without_shortcut(shade='#c0c0c0'),
        _boost_without_shortcut(shade='#b0b0b0'),
    )

    result = run_shortcut_visit_task(
        Controller.from_frame(device, frames[0], rng=random.Random(0)),
        _Capture(frames),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.SKIPPED
    assert result.verification == 'row_vanished'
    assert 'moved the boost_page content' in result.detail
    assert result.taps == ()
    assert device.taps == []


def test_a_settled_scrolled_list_after_restart_without_the_row_is_row_vanished(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: the list moved, then settled at its end, after a restart."""
    entry = _entry(_boost(), catalogue)
    routes = _Routes()
    reopened = _boost_without_shortcut(shade='#d0d0d0')
    frames = (
        _boost_without_shortcut(shade='white'),
        _boost_without_shortcut(shade='#e0e0e0'),
        _boost_without_shortcut(shade='#e0e0e0'),
        _boost_without_shortcut(shade='#e0e0e0'),
        _boost_without_shortcut(shade='#e0e0e0'),
        _boost_without_shortcut(shade='#c0c0c0'),
        _boost_without_shortcut(shade='#c0c0c0'),
    )
    monkeypatch.setattr(
        shortcut_module,
        'open_boost_list',
        lambda *_args, **_kwargs: _reopened_boost(reopened),
    )

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frames[0], rng=random.Random(0)),
        _Capture(frames),
        corpus,
        _state(tmp_path),
        entry,
        navigate=routes,
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.SKIPPED
    assert result.verification == 'row_vanished'
    assert routes.routes == [SurfaceRoute.SHORTCUT]
    assert result.taps == ()


def test_an_empty_modal_read_is_a_surface_failure_not_an_absent_row(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No rows and a no-op swipe prove nothing; name the surface instead."""
    entry = _entry(_boost(), catalogue)
    empty = _snapshot(_box('Coins boost', 100, 450, 500, 540))
    routes = _Routes()
    device = _Device()
    opened: list[str] = []

    def fake_open(*_args: Any, **_kwargs: Any) -> BoostList:
        opened.append('boost')
        return BoostList(snapshot=empty, route=DiscoveryRoute.BOOST, detail='the sheet is open but blank')

    monkeypatch.setattr(shortcut_module, 'open_boost_list', fake_open)

    result = run_shortcut_visit_task(
        Controller.from_frame(device, empty, rng=random.Random(0)),
        _Capture((empty,)),
        corpus,
        _state(tmp_path),
        entry,
        navigate=routes,
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.FAILED
    assert result.verification == 'surface_incomplete'
    assert 'surface=unknown' in result.detail
    assert 'boost_header=True' in result.detail
    assert 'scroll=stable' in result.detail
    assert 'labels=1' in result.detail
    assert 'not on the scrolled boost list' not in result.detail
    assert opened == ['boost']
    assert routes.routes == [SurfaceRoute.SHORTCUT]
    assert result.taps == ()
    assert device.taps == []


def test_a_stable_boost_read_is_not_an_absent_row(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live #148 shape: a boost page whose swipe changed nothing."""
    entry = _entry(_boost(), catalogue)
    frame = _boost_without_shortcut()
    monkeypatch.setattr(
        shortcut_module,
        'open_boost_list',
        lambda *_args, **_kwargs: BoostList(snapshot=frame, route=DiscoveryRoute.BOOST, detail='still the sheet'),
    )

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture((frame,)),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.FAILED
    assert result.verification == 'surface_incomplete'
    assert 'surface=boost_page' in result.detail
    assert 'scroll=stable' in result.detail
    assert 'not on the scrolled boost list' not in result.detail
    assert result.taps == ()


def test_a_moving_non_boost_surface_is_still_not_absence(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Movement on the wrong surface says nothing about the boost row."""
    entry = _entry(_boost(), catalogue)
    frames = (
        _coin_page('87', shade='white'),
        _coin_page('87', shade='#e0e0e0'),
        _coin_page('87', shade='#d0d0d0'),
        _coin_page('87', shade='#c0c0c0'),
        _coin_page('87', shade='#b0b0b0'),
    )
    monkeypatch.setattr(
        shortcut_module,
        'open_boost_list',
        lambda *_args, **_kwargs: BoostList(
            snapshot=frames[-1],
            route=DiscoveryRoute.INLINE,
            detail='the re-open degraded to the coin page',
        ),
    )

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frames[0], rng=random.Random(0)),
        _Capture(frames),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.FAILED
    assert result.verification == 'surface_lost'
    assert result.balance_before == 87.0
    assert 'surface=rows_absent' in result.detail
    assert 'not on the scrolled boost list' not in result.detail


def test_a_failed_restart_route_is_logged_and_the_reopen_still_runs(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settled read after a failed route fire is not proof of absence."""
    entry = _entry(_boost(), catalogue)
    reopened = _boost_without_shortcut(shade='#d0d0d0')
    frames = (
        _boost_without_shortcut(shade='white'),
        _boost_without_shortcut(shade='#e0e0e0'),
        _boost_without_shortcut(shade='#e0e0e0'),
        _boost_without_shortcut(shade='#e0e0e0'),
        _boost_without_shortcut(shade='#e0e0e0'),
        _boost_without_shortcut(shade='#c0c0c0'),
        _boost_without_shortcut(shade='#c0c0c0'),
    )

    def broken(_route: SurfaceRoute) -> None:
        raise RuntimeError('route unavailable')

    monkeypatch.setattr(
        shortcut_module,
        'open_boost_list',
        lambda *_args, **_kwargs: BoostList(snapshot=reopened, route=DiscoveryRoute.BOOST, detail='still open'),
    )

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frames[0], rng=random.Random(0)),
        _Capture(frames),
        corpus,
        _state(tmp_path),
        entry,
        navigate=cast(Any, broken),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.FAILED
    assert result.verification == 'surface_incomplete'
    assert result.verification != 'row_vanished'
    assert 'restart failed' not in result.detail


def test_an_already_open_sheet_does_not_confirm_a_settled_absence(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A returning no-op route plus an already-open sheet is not a re-open."""
    entry = _entry(_boost(), catalogue)
    reopened = _boost_without_shortcut(shade='#d0d0d0')
    frames = (
        _boost_without_shortcut(shade='white'),
        _boost_without_shortcut(shade='#e0e0e0'),
        _boost_without_shortcut(shade='#e0e0e0'),
        _boost_without_shortcut(shade='#e0e0e0'),
        _boost_without_shortcut(shade='#e0e0e0'),
        _boost_without_shortcut(shade='#c0c0c0'),
        _boost_without_shortcut(shade='#c0c0c0'),
    )
    monkeypatch.setattr(
        shortcut_module,
        'open_boost_list',
        lambda *_args, **_kwargs: _reopened_boost(reopened, fresh=False),
    )

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frames[0], rng=random.Random(0)),
        _Capture(frames),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.FAILED
    assert result.verification == 'surface_incomplete'
    assert result.verification != 'row_vanished'


def test_a_stop_during_the_restart_route_is_never_swallowed(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    entry = _entry(_boost(), catalogue)
    empty = _snapshot(_box('Coins boost', 100, 450, 500, 540))

    def stop(_route: SurfaceRoute) -> None:
        raise RunInterrupted('stop requested during restart')

    with pytest.raises(RunInterrupted, match='stop requested during restart'):
        run_shortcut_visit_task(
            Controller.from_frame(_Device(), empty, rng=random.Random(0)),
            _Capture((empty,)),
            corpus,
            _state(tmp_path),
            entry,
            navigate=cast(Any, stop),
            clock=_Clock(),
            sleep=_Clock().sleep,
        )


def test_a_stop_during_the_boost_reopen_is_never_swallowed(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry(_boost(), catalogue)
    empty = _snapshot(_box('Coins boost', 100, 450, 500, 540))

    def stop(*_args: Any, **_kwargs: Any) -> BoostList:
        raise RunInterrupted('stop requested while reopening')

    monkeypatch.setattr(shortcut_module, 'open_boost_list', stop)

    with pytest.raises(RunInterrupted, match='stop requested while reopening'):
        run_shortcut_visit_task(
            Controller.from_frame(_Device(), empty, rng=random.Random(0)),
            _Capture((empty,)),
            corpus,
            _state(tmp_path),
            entry,
            navigate=_Routes(),
            clock=_Clock(),
            sleep=_Clock().sleep,
        )


def test_a_failed_boost_reopen_is_named_as_a_surface_failure(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry(_boost(), catalogue)
    empty = _snapshot(_box('Coins boost', 100, 450, 500, 540))

    def broken(*_args: Any, **_kwargs: Any) -> BoostList:
        raise RuntimeError('boost route unavailable')

    monkeypatch.setattr(shortcut_module, 'open_boost_list', broken)

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), empty, rng=random.Random(0)),
        _Capture((empty,)),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.FAILED
    assert result.verification == 'surface_incomplete'
    assert 'the restart failed: RuntimeError: boost route unavailable' in result.detail


def test_a_498_decoy_on_a_non_coin_frame_is_not_reported_as_the_balance(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost('498')
    entry = _entry(frame, catalogue)

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture((frame, _coin_page('123'))),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    # The 498 in the boost header is not used: the live path brackets the
    # balance from the coin page instead, where the frame reads 123.
    assert result.balance_before == 123.0
    assert result.balance_after is None
    assert 498.0 not in (result.balance_before, result.balance_after)


def test_the_wrapped_title_is_found_on_a_populated_sheet(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _boost('87')
    entry = _entry(frame, catalogue)
    device = _Device()
    monkeypatch.setattr(
        shortcut_module,
        'open_boost_list',
        lambda *_args, **_kwargs: _reopened_boost(frame),
    )

    result = run_shortcut_visit_task(
        Controller.from_frame(device, frame, rng=random.Random(0)),
        _Capture((frame, _coin_page('87'), _shortcut_sheet(), _coin_page('88'))),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert entry.title == 'Visit the page via coins'
    assert result.taps[0].label == 'Visit the page via coins'
    assert entry.dispatch_target is not None
    left, top, right, bottom = entry.dispatch_target.bbox
    assert len(device.taps) >= 1
    assert left <= device.taps[0][0] <= right
    assert top <= device.taps[0][1] <= bottom


def test_a_sheet_without_a_launcher_step_and_a_failed_return_are_still_recorded(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)
    sheet = _snapshot(
        _box('Create shortcut', 386, 1398, 816, 1456),
        shade='#d0d0d0',
    )

    def broken(_route: SurfaceRoute) -> None:
        raise RuntimeError('route unavailable')

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture((frame, sheet, _coin_page('87'))),
        corpus,
        _state(tmp_path),
        entry,
        navigate=cast(Any, broken),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.FAILED
    assert result.verification == 'shortcut_creation_uncertain'
    assert 'Create shortcut' in result.modal_labels
    assert len(result.taps) == 2


def test_a_stop_during_navigation_is_never_swallowed(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)

    def stop(_route: SurfaceRoute) -> None:
        raise RunInterrupted('stop requested')

    with pytest.raises(RunInterrupted, match='stop requested'):
        run_shortcut_visit_task(
            Controller.from_frame(_Device(), frame, rng=random.Random(0)),
            _Capture((frame, _shortcut_sheet(), _coin_page('87'))),
            corpus,
            _state(tmp_path),
            entry,
            navigate=cast(Any, stop),
            clock=_Clock(),
            sleep=_Clock().sleep,
        )


def test_the_daily_limit_is_honoured_before_any_tap(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost()
    entry = _entry(frame, catalogue)
    state = _state(tmp_path)
    state.record('coins_shortcut_visit')
    device = _Device()

    result = run_shortcut_visit_task(
        Controller.from_frame(device, frame, rng=random.Random(0)),
        _Capture((frame,)),
        corpus,
        state,
        entry,
        navigate=_Routes(),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.SKIPPED
    assert result.verification == 'daily_limit'
    assert device.taps == []


def test_a_tap_without_the_sheet_still_requires_a_balance_increase(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture((frame, _coin_page('87'))),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.FAILED
    assert result.verification == 'no_credit'


def test_a_direct_credit_without_the_sheet_is_still_recorded(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)
    state = _state(tmp_path)

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture((frame, _coin_page('88'))),
        corpus,
        state,
        entry,
        navigate=_Routes(),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.COMPLETED
    assert result.verification == 'balance_delta'
    assert (result.balance_before, result.balance_after) == (87.0, 88.0)
    assert state.count('coins_shortcut_visit') == 1


def test_an_add_confirmed_by_the_pin_probe_is_created_and_cleared(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)
    readings = iter((False, True))
    removed: list[str] = []

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture(_created_shortcut_frames(frame)),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        shortcut_present=lambda: next(readings, True),
        launch_shortcut=lambda: None,
        remove_shortcut=lambda: removed.append('cleared'),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.COMPLETED
    assert result.shortcut_created is True
    assert result.shortcut_removed is True
    assert removed == ['cleared']


def test_a_swallowed_add_without_a_pin_is_not_created_or_cleared(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)
    removed: list[str] = []
    frames = (
        frame,
        _shortcut_sheet(),
        _launcher_add_sheet(),
        _shortcut_sheet(),
        _coin_page('87'),
    )

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture(frames),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        shortcut_present=lambda: False,
        launch_shortcut=lambda: None,
        remove_shortcut=lambda: removed.append('cleared'),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.FAILED
    assert result.verification == 'shortcut_creation_uncertain'
    assert 'never showed the Coins pin' in result.detail
    assert result.shortcut_created is False
    assert result.shortcut_removed is False
    assert removed == []


def test_an_unchanged_page_after_a_loading_transition_does_not_wait_the_full_bound(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)
    sleeps: list[float] = []
    frames = (
        frame,
        _shortcut_sheet(),
        _launcher_add_sheet(),
        _shortcut_sheet(),
        _shortcut_sheet(),
        _coin_page('87'),
        _coin_page('87'),
        _snapshot(),
        _coin_page('87'),
    )

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture(frames),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        launch_shortcut=lambda: None,
        clock=_Clock(),
        sleep=sleeps.append,
    )

    assert result.outcome is ShortcutVisitOutcome.FAILED
    assert result.verification == 'shortcut_sheet_no_credit'
    assert max(sleeps) < 6.0


def test_a_launcher_sheet_that_never_closes_does_not_claim_completion(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)
    frames = (
        frame,
        _shortcut_sheet(),
        _launcher_add_sheet(),
        _launcher_add_sheet(),
    )

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture(frames),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        launch_shortcut=lambda: None,
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.FAILED
    assert result.verification == 'foreground_lost_after_action'
    assert result.shortcut_created is True


def test_an_unreadable_shortcut_presence_probe_does_not_stop_the_task(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)

    def broken_presence() -> bool | None:
        raise RuntimeError('shortcut service unreadable')

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture(_created_shortcut_frames(frame)),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        shortcut_present=broken_presence,
        launch_shortcut=lambda: None,
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.COMPLETED


def test_a_stop_during_cleanup_is_never_swallowed(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)

    def stop() -> object:
        raise RunInterrupted('stop requested during cleanup')

    with pytest.raises(RunInterrupted, match='stop requested during cleanup'):
        run_shortcut_visit_task(
            Controller.from_frame(_Device(), frame, rng=random.Random(0)),
            _Capture(_created_shortcut_frames(frame)),
            corpus,
            _state(tmp_path),
            entry,
            navigate=_Routes(),
            launch_shortcut=lambda: None,
            remove_shortcut=stop,
            clock=_Clock(),
            sleep=_Clock().sleep,
        )


def test_a_stop_during_the_shortcut_launch_is_never_swallowed(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_page_with_row('87')
    entry = _entry(frame, catalogue)

    def stop() -> None:
        raise RunInterrupted('stop requested during launch')

    with pytest.raises(RunInterrupted, match='stop requested during launch'):
        run_shortcut_visit_task(
            Controller.from_frame(_Device(), frame, rng=random.Random(0)),
            _Capture(_created_shortcut_frames(frame)),
            corpus,
            _state(tmp_path),
            entry,
            navigate=_Routes(),
            launch_shortcut=stop,
            clock=_Clock(),
            sleep=_Clock().sleep,
        )


def test_the_executor_rejects_a_row_that_is_not_a_shortcut_visit(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost()
    entry = replace(
        _entry(frame, catalogue),
        decision=replace(_entry(frame, catalogue).decision, spec=catalogue.allowed_by_key('notifications')),
    )

    with pytest.raises(TaskError, match='not a shortcut_visit task'):
        run_shortcut_visit_task(
            Controller.from_frame(_Device(), frame, rng=random.Random(0)),
            _Capture((frame,)),
            corpus,
            _state(tmp_path),
            entry,
            navigate=_Routes(),
        )


@pytest.mark.parametrize(
    ('option', 'value'),
    [('modal_timeout', 0.0), ('interval', 0.0), ('settle', -1.0)],
)
def test_invalid_wait_options_are_rejected(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    option: str,
    value: float,
) -> None:
    frame = _boost()
    entry = _entry(frame, catalogue)
    options = cast(dict[str, Any], {option: value})

    with pytest.raises(ValueError, match=option):
        run_shortcut_visit_task(
            Controller.from_frame(_Device(), frame, rng=random.Random(0)),
            _Capture((frame,)),
            corpus,
            _state(tmp_path),
            entry,
            navigate=_Routes(),
            **options,
        )


def test_a_dispatch_without_an_action_or_authorization_is_refused(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost()
    entry = _entry(frame, catalogue)

    with pytest.raises(TaskError, match='not dispatchable'):
        run_shortcut_visit_task(
            Controller.from_frame(_Device(), frame, rng=random.Random(0)),
            _Capture((frame,)),
            corpus,
            _state(tmp_path),
            replace(entry, action=None),
            navigate=_Routes(),
        )
