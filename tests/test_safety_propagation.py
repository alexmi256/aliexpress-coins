"""Safety aborts must pass through executor and cleanup handlers (issue #20).

The broad handlers that return an executor to the coins page catch ``Exception``
so a popup or transport fault does not end the night. A safety abort is not a
task failure: these tests prove it is re-raised from each executor and from each
best-effort return helper instead of being converted into a failed task.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from PIL import Image

from aliexpress_coins.control.controller import Controller
from aliexpress_coins.control.popups import PopupCorpus, load_popup_corpus
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.runner.safety import AbortCode, RunAborted, ScreenReport
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks import browse, notifications, quiz, surprise
from aliexpress_coins.tasks.browse import return_to_coins_page, run_browse_task
from aliexpress_coins.tasks.catalogue import TaskCatalogue, load_task_catalogue
from aliexpress_coins.tasks.discovery import RosterEntry, enumerate_roster
from aliexpress_coins.tasks.dispatch import DispatchGate
from aliexpress_coins.tasks.surface import RouteFirer, SurfaceRoute

ROOT = Path(__file__).resolve().parents[1]
CAPTURED_AT = datetime(2026, 11, 22, 3, 30, tzinfo=UTC)


class _RecordingDevice:
    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))


def _abort() -> RunAborted:
    screen = ScreenReport(
        state='unknown',
        surface='unknown',
        labels=('Mystery checkout', 'Buy now'),
        fingerprint='unknown/unknown/test',
    )
    return RunAborted(code=AbortCode.BALANCE_DECREASED, reason='the balance decreased', screen=screen)


def _box(text: str, left: float, top: float, right: float, bottom: float) -> TextBox:
    return TextBox(
        text=text,
        box=((left, top), (right, top), (right, bottom), (left, bottom)),
        score=0.99,
    )


def _snapshot(*boxes: TextBox) -> Snapshot:
    return Snapshot(
        image=Image.new('RGB', (1200, 2000), 'white'),
        boxes=boxes,
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(1200, 2000, density=240, sdk=35),
    )


def _frame(title: str) -> Snapshot:
    return _snapshot(
        _box('Coins boost', 100, 100, 500, 150),
        _box(title, 100, 260, 900, 300),
        _box('GO', 980, 280, 1040, 320),
    )


def _entry(catalogue: TaskCatalogue, title: str) -> RosterEntry:
    entry = next(item for item in enumerate_roster(_frame(title), catalogue).entries if item.title == title)
    return DispatchGate(catalogue).authorize(entry)


def _controller(frame: Snapshot) -> Controller:
    return Controller.from_frame(_RecordingDevice(), frame, rng=random.Random(0))


def _raises_capture() -> Snapshot:
    raise _abort()


def _navigate(_route: SurfaceRoute) -> None:
    return None


@pytest.fixture(scope='module')
def catalogue() -> TaskCatalogue:
    return load_task_catalogue(ROOT / 'specs' / 'tasks.yaml')


@pytest.fixture(scope='module')
def corpus() -> PopupCorpus:
    return load_popup_corpus(ROOT / 'specs' / 'popups.yaml')


def test_browse_task_re_raises_a_safety_abort(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _frame('Explore sponsored items')
    monkeypatch.setattr(browse, '_sweep', lambda *args, **kwargs: (_ for _ in ()).throw(_abort()))

    with pytest.raises(RunAborted):
        run_browse_task(
            _controller(frame),
            _raises_capture,
            corpus,
            DailyStateStore(tmp_path / 'state'),
            _entry(catalogue, 'Explore sponsored items'),
            navigate=cast(RouteFirer, _navigate),
            snapshot=frame,
        )


def test_quiz_task_re_raises_a_safety_abort(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _frame('Take the coins quiz')
    monkeypatch.setattr(quiz, '_wait_for_surface', lambda *args, **kwargs: (_ for _ in ()).throw(_abort()))

    with pytest.raises(RunAborted):
        quiz.run_quiz_task(
            _controller(frame),
            _raises_capture,
            corpus,
            DailyStateStore(tmp_path / 'state'),
            _entry(catalogue, 'Take the coins quiz'),
            navigate=cast(RouteFirer, _navigate),
            snapshot=frame,
        )


def test_surprise_task_re_raises_a_safety_abort(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _frame('Browse surprise items')
    monkeypatch.setattr(surprise, '_wait_for_surface', lambda *args, **kwargs: (_ for _ in ()).throw(_abort()))

    with pytest.raises(RunAborted):
        surprise.run_surprise_task(
            _controller(frame),
            _raises_capture,
            corpus,
            DailyStateStore(tmp_path / 'state'),
            _entry(catalogue, 'Browse surprise items'),
            navigate=cast(RouteFirer, _navigate),
            snapshot=frame,
        )


def test_notifications_task_re_raises_a_safety_abort(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _frame('Turn on notifications')
    monkeypatch.setattr(notifications, '_wait_after_action', lambda *args, **kwargs: (_ for _ in ()).throw(_abort()))

    with pytest.raises(RunAborted):
        notifications.run_notifications_task(
            _controller(frame),
            _raises_capture,
            corpus,
            DailyStateStore(tmp_path / 'state'),
            _entry(catalogue, 'Turn on notifications'),
            navigate=cast(RouteFirer, _navigate),
            snapshot=frame,
        )


@pytest.mark.parametrize(
    'quiet_return',
    [browse._return_quietly, quiz._return_quietly, surprise._return_quietly, notifications._return_quietly],
    ids=['browse', 'quiz', 'surprise', 'notifications'],
)
def test_each_best_effort_return_re_raises_a_safety_abort(
    quiet_return: Callable[..., object],
    corpus: PopupCorpus,
) -> None:
    frame = _snapshot(_box('Coins', 100, 200, 300, 260))

    with pytest.raises(RunAborted):
        quiet_return(
            _controller(frame),
            _raises_capture,
            corpus,
            _navigate,
            templates=None,
            route=SurfaceRoute.SHORTCUT,
            timeout=1.0,
            interval=0.1,
            attempts=1,
            settle=0.0,
            clock=lambda: 0.0,
            sleep=lambda _seconds: None,
            logger=logging.getLogger('test.safety-propagation'),
        )


def test_a_best_effort_return_still_swallows_an_ordinary_fault(corpus: PopupCorpus) -> None:
    frame = _snapshot(_box('Coins', 100, 200, 300, 260))

    def broken_capture() -> Snapshot:
        raise RuntimeError('transport fault')

    result = browse._return_quietly(
        _controller(frame),
        broken_capture,
        corpus,
        cast(RouteFirer, _navigate),
        templates=None,
        route=SurfaceRoute.SHORTCUT,
        timeout=1.0,
        interval=0.1,
        attempts=1,
        settle=0.0,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        logger=logging.getLogger('test.safety-propagation'),
    )

    assert result is None


def test_the_return_route_re_raises_a_safety_abort_from_navigation(corpus: PopupCorpus) -> None:
    frame = _frame('Explore sponsored items')

    def aborting_navigate(_route: SurfaceRoute) -> None:
        raise _abort()

    with pytest.raises(RunAborted):
        return_to_coins_page(
            _controller(frame),
            _raises_capture,
            corpus,
            cast(RouteFirer, aborting_navigate),
            snapshot=frame,
        )
