"""The shortcut-visit row: recognise the sheet, never infer a launcher claim (#130)."""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from aliexpress_coins.control.controller import Controller
from aliexpress_coins.control.popups import PopupCorpus, load_popup_corpus
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.runner.errors import RunInterrupted
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks.catalogue import TaskCatalogue, load_task_catalogue
from aliexpress_coins.tasks.discovery import RosterEntry, enumerate_roster
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

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        self.swipes.append((x1, y1, x2, y2, duration_ms))


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


def _coin_page(balance: str = '87') -> Snapshot:
    return _snapshot(
        _box('Coins', 100, 220, 300, 280),
        _box('Coins credits to redeem', 100, 330, 700, 380),
        _box('Daily coins', 100, 560, 500, 610),
        _box(balance, 100, 400, 260, 460),
    )


def _shortcut_sheet() -> Snapshot:
    return _snapshot(
        _box("Once you've created a shortcut, find it", 183, 471, 1020, 526),
        _box('on your home screen. Tap it to enter', 204, 530, 997, 594),
        _box('Got it', 516, 1235, 686, 1298),
        _box('Create shortcut', 386, 1398, 816, 1456),
        shade='#d0d0d0',
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


def test_the_go_sheet_is_named_and_no_launcher_item_is_created(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost()
    entry = _entry(frame, catalogue)
    device = _Device()
    controller = Controller.from_frame(device, frame, rng=random.Random(0))
    routes = _Routes()
    state = _state(tmp_path)

    result = run_shortcut_visit_task(
        controller,
        _Capture((frame, _shortcut_sheet(), _coin_page('87'))),
        corpus,
        state,
        entry,
        navigate=routes,
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.FAILED
    assert result.verification == 'shortcut_sheet_no_credit'
    assert 'Create shortcut' in result.detail
    assert result.modal_labels == (
        "Once you've created a shortcut, find it",
        'on your home screen. Tap it to enter',
        'Got it',
        'Create shortcut',
    )
    assert [tap.label for tap in result.taps] == [entry.title, 'Got it']
    assert len(device.taps) == 2
    assert routes.routes == [SurfaceRoute.SHORTCUT]
    assert state.count('coins_shortcut_visit') == 0
    assert result.completed is False


def test_a_readable_balance_increase_is_the_only_completion_evidence(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost('87')
    entry = _entry(frame, catalogue)
    state = _state(tmp_path)

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture((frame, _shortcut_sheet(), _coin_page('88'))),
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
    assert result.recorded is True
    assert state.count('coins_shortcut_visit') == 1


def test_a_two_coin_balance_change_is_not_accepted_as_the_advertised_plus_one(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost('87')
    entry = _entry(frame, catalogue)
    state = _state(tmp_path)

    result = run_shortcut_visit_task(
        Controller.from_frame(_Device(), frame, rng=random.Random(0)),
        _Capture((frame, _shortcut_sheet(), _coin_page('89'))),
        corpus,
        state,
        entry,
        navigate=_Routes(),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.FAILED
    assert result.verification == 'shortcut_sheet_no_credit'
    assert (result.balance_before, result.balance_after) == (87.0, 89.0)
    assert result.recorded is False
    assert state.count('coins_shortcut_visit') == 0


def test_the_row_is_found_below_the_fold_by_scrolling(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    top = _boost(with_row=False, shade='white')
    below = _boost(with_row=True, shade='#e0e0e0')
    entry = _entry(below, catalogue)
    device = _Device()

    result = run_shortcut_visit_task(
        Controller.from_frame(device, top, rng=random.Random(0)),
        _Capture((top, below, _shortcut_sheet(), _coin_page('87'))),
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


def test_an_absent_row_is_a_stale_roster_and_never_tapped(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    top = _boost(with_row=False)
    frame_with_row = _boost()
    entry = _entry(frame_with_row, catalogue)
    device = _Device()

    result = run_shortcut_visit_task(
        Controller.from_frame(device, top, rng=random.Random(0)),
        _Capture((top,)),
        corpus,
        _state(tmp_path),
        entry,
        navigate=_Routes(),
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert result.outcome is ShortcutVisitOutcome.SKIPPED
    assert result.verification == 'row_vanished'
    assert result.taps == ()
    assert device.taps == []


def test_a_sheet_without_got_it_and_a_failed_return_are_still_recorded(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost('87')
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
    assert result.verification == 'shortcut_sheet_no_credit'
    assert result.modal_labels == ('Create shortcut',)
    assert len(result.taps) == 1


def test_a_stop_during_navigation_is_never_swallowed(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost('87')
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
    frame = _boost('87')
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
