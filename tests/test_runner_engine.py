"""The run engine: ordering, budgets, clean stops and the report (issue #29).

Every device and executor seam is a recording fake. Frames are hand-built from
the measured box layout, and the surface/re-enumeration code is the real one, so
the walk is exercised without OCR, adb or a tablet.
"""

from __future__ import annotations

import json
import logging
import random
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from enum import StrEnum
from http.client import HTTPMessage
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError

import pytest
from PIL import Image

from aliexpress_coins.control.controller import Controller
from aliexpress_coins.control.errors import PopupUnclosable
from aliexpress_coins.control.popups import DismissReport, DismissStop, PopupCorpus, load_popup_corpus
from aliexpress_coins.perception.artifacts import ArtifactStore
from aliexpress_coins.perception.errors import SnapshotError, StateTimeout
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.state import ScreenState, classify
from aliexpress_coins.runner import engine
from aliexpress_coins.runner.config import RunConfig
from aliexpress_coins.runner.device import AdbDevice, DeviceCommandResult, HygieneDevice, HygieneReport
from aliexpress_coins.runner.engine import EngineResult, RunContext
from aliexpress_coins.runner.errors import (
    AppLaunchFailed,
    AppLoggedOut,
    AppNotInstalled,
    DeviceError,
    DeviceLocked,
    DeviceSleeping,
    DeviceUnavailable,
    ReportError,
    RunInterrupted,
)
from aliexpress_coins.runner.failures import FailureCode, failure
from aliexpress_coins.runner.notify import AppriseSink, Notification, NotificationSink, WebhookSink
from aliexpress_coins.runner.report import RunEventLog, RunReport, RunStatus
from aliexpress_coins.runner.safety import AbortCode, RunAborted, ScreenReport
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks import notifications as notifications_module
from aliexpress_coins.tasks.browse import ReturnResult
from aliexpress_coins.tasks.catalogue import TaskCatalogue, TaskCategory, load_task_catalogue
from aliexpress_coins.tasks.discovery import (
    BoostList,
    DiscoveryRoute,
    Roster,
    RosterEntry,
    enumerate_roster,
)
from aliexpress_coins.tasks.dispatch import DispatchGate
from aliexpress_coins.tasks.signin import ClaimResult, SigninOutcome, SigninReport, read_coin_balance
from aliexpress_coins.tasks.surface import EMPTY_BOOST_MARKER, SHORTCUT_DEEP_LINK, Surface, SurfaceRoute
from tests.support.adb import ScriptedAdbRunner, ok

ROOT = Path(__file__).resolve().parents[1]
CAPTURED_AT = datetime(2026, 11, 22, 3, 30, tzinfo=UTC)


class _RecordingDevice:
    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))


class _Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class _Stop:
    def __init__(self, active: bool = False) -> None:
        self.active = active
        self.waits = 0

    def is_set(self) -> bool:
        return self.active

    def wait(self, timeout: float | None = None) -> bool:
        del timeout
        self.waits += 1
        return self.active

    def set(self) -> None:
        self.active = True


class _Notifier:
    name = 'webhook'

    def __init__(
        self,
        *,
        error: Exception | None = None,
        on_send: Callable[[], None] | None = None,
    ) -> None:
        self.error = error
        self.on_send = on_send
        self.messages: list[Notification] = []

    def send(self, notification: Notification) -> None:
        self.messages.append(notification)
        if self.on_send is not None:
            self.on_send()
        if self.error is not None:
            raise self.error


class _Hygiene:
    def __init__(
        self,
        error: Exception | None = None,
        *,
        was_asleep: bool = False,
        errors: list[Exception | None] | None = None,
    ) -> None:
        self.error = error
        self.was_asleep = was_asleep
        self.errors = list(errors or [])
        self.prepared = 0
        self.restored = 0

    def prepare(self) -> HygieneReport:
        self.prepared += 1
        if self.errors:
            raised = self.errors.pop(0)
            if raised is not None:
                raise raised
        if self.error is not None:
            raise self.error
        return HygieneReport(
            serial='192.168.8.65:5555',
            state='device',
            awake=True,
            app_started=True,
            was_asleep=self.was_asleep,
            kept_awake=True,
            detail='ready',
        )

    def navigate(self, route: SurfaceRoute) -> None:
        raise AssertionError(f'unexpected hygiene navigation {route}')

    def check_connected(self) -> None:
        if self.error is not None:
            raise self.error

    def restore(self) -> None:
        self.restored += 1


class _Navigate:
    def __init__(self) -> None:
        self.routes: list[SurfaceRoute] = []

    def __call__(self, route: SurfaceRoute) -> None:
        self.routes.append(route)


class _Capture:
    def __init__(self, *frames: Snapshot) -> None:
        self.frames = tuple(frames)
        self.index = 0

    def capture(self) -> Snapshot:
        frame = self.frames[min(self.index, len(self.frames) - 1)]
        self.index += 1
        return frame


class _FlakyCapture:
    """A device that answers the first ``fail_after`` captures, then loses adb."""

    def __init__(self, frame: Snapshot, *, fail_after: int) -> None:
        self.frame = frame
        self.fail_after = fail_after
        self.calls = 0

    def capture(self) -> Snapshot:
        self.calls += 1
        if self.calls > self.fail_after:
            raise SnapshotError('adb exec-out screencap: device offline')
        return self.frame


def _adb_ok(text: str = '') -> DeviceCommandResult:
    return ok(text)


class _FakeOutcome(StrEnum):
    COMPLETED = 'completed'
    SKIPPED = 'skipped'
    FAILED = 'failed'


class _FakeResult:
    def __init__(self, *, completed: bool, outcome: _FakeOutcome = _FakeOutcome.COMPLETED) -> None:
        self.completed = completed
        self.outcome = outcome
        self.verification = 'fake'
        self.detail = f'fake {outcome.value}'

    def as_dict(self) -> dict[str, Any]:
        return {'outcome': self.outcome.value, 'detail': self.detail}


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


_ROWS: tuple[tuple[str, str, str], ...] = (
    ('daily_checkin', 'Daily check-in', 'Finish today'),
    ('coins_quiz', 'Take the coins quiz', 'Answer one question'),
    ('sponsored_items', 'Explore sponsored items', 'Browse, shop and save'),
    ('surprise_items', 'Browse surprise items', 'Tap 3 items on this page'),
    ('notifications', 'Turn on notifications', 'Stay informed when rewards drop'),
    ('shortcut', 'Add a coins shortcut', 'One tap away from your daily rewards'),
    ('denied', 'Play & Earn', 'A mini-game'),
)


def _boost_header_frame() -> Snapshot:
    return _snapshot(_box('Coins boost', 100, 100, 500, 150))


def _empty_boost_frame() -> Snapshot:
    return _snapshot(
        _box('Coins boost', 100, 100, 500, 150),
        _box(EMPTY_BOOST_MARKER, 100, 600, 1100, 650),
    )


def _boost_frame(*keys: str) -> Snapshot:
    selected = _ROWS if not keys else tuple(row for row in _ROWS if row[0] in keys)
    boxes: list[TextBox] = [_box('Coins boost', 100, 100, 500, 150)]
    for index, (_key, title, subtitle) in enumerate(selected):
        top = 260 + index * 150
        boxes.append(_box(title, 100, top, 700, top + 40))
        boxes.append(_box(subtitle, 100, top + 45, 760, top + 75))
        boxes.append(_box('GO', 980, top + 20, 1040, top + 60))
    return _snapshot(*boxes)


def _coin_frame(balance: str = '1,240') -> Snapshot:
    return _snapshot(
        _box('Coins', 100, 220, 300, 280),
        _box('Coins credits to redeem', 100, 330, 700, 380),
        _box('Daily coins', 100, 560, 500, 610),
        _box(balance, 100, 380, 260, 440),
    )


def _coin_popup_frame() -> Snapshot:
    """A modal over a populated coin page; OCR still reads the rows beneath."""
    base = _coin_task_frame()
    return replace(base, boxes=(*base.boxes, _box('Welcome gifts', 200, 700, 1000, 780)))


def _coin_task_frame() -> Snapshot:
    return _snapshot(
        _box('Coins', 100, 220, 300, 280),
        _box('Coins credits to redeem', 100, 330, 700, 380),
        _box('Daily coins', 100, 560, 500, 610),
        _box('1,240', 100, 400, 260, 460),
        _box('Take the coins quiz', 180, 1269, 700, 1305),
        _box('GO', 982, 1273, 1040, 1310),
        _box('Play & Earn', 180, 1440, 700, 1476),
        _box('GO', 981, 1440, 1040, 1477),
    )


def _home_frame() -> Snapshot:
    return _snapshot(_box('For you', 100, 200, 400, 260), _box('Search', 100, 300, 300, 360))


def _skeleton_frame() -> Snapshot:
    """The measured loading skeleton, with no OCR boxes to classify a page."""
    with Image.open(ROOT / 'tests' / 'data' / 'states' / 'skeleton.png') as opened:
        image = opened.convert('RGB')
    return Snapshot(
        image=image,
        boxes=(),
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(image.width, image.height, density=240, sdk=35),
    )


def _logged_out_frame() -> Snapshot:
    return _snapshot(
        _box('Sign in', 100, 300, 300, 360),
        _box('Welcome to AliExpress', 100, 400, 800, 460),
    )


def _dismiss(*families: str) -> DismissReport:
    return DismissReport(
        closed=families,
        attempts=(),
        frames=(),
        remaining=False,
        stop=DismissStop.CLEAR,
    )


def _safety_abort(code: AbortCode = AbortCode.DANGEROUS_CONTROL) -> RunAborted:
    screen = ScreenReport(
        state='unknown',
        surface='unknown',
        labels=('Mystery checkout', 'Buy now'),
        fingerprint='unknown/unknown/test',
    )
    return RunAborted(code=code, reason="a 'Buy now' control appeared", screen=screen)


@pytest.fixture(scope='module')
def corpus() -> PopupCorpus:
    return load_popup_corpus(ROOT / 'specs' / 'popups.yaml')


@pytest.fixture(scope='module')
def catalogue() -> TaskCatalogue:
    return load_task_catalogue(ROOT / 'specs' / 'tasks.yaml')


@pytest.fixture(scope='module')
def boost_frame() -> Snapshot:
    return _boost_frame()


@pytest.fixture(scope='module')
def full_roster(boost_frame: Snapshot, catalogue: TaskCatalogue) -> Roster:
    return replace(
        enumerate_roster(boost_frame, catalogue, route=DiscoveryRoute.BOOST),
        surface_variant=Surface.BOOST_PAGE,
        routes_taken=(SurfaceRoute.SHORTCUT,),
    )


def _entry(roster: Roster, key: str) -> RosterEntry:
    for entry in roster.entries:
        if entry.key == key:
            return DispatchGate(load_task_catalogue(ROOT / 'specs' / 'tasks.yaml')).authorize(entry)
    raise AssertionError(f'{key!r} is not in the roster')


def _context(
    tmp_path: Path,
    *,
    capture: _Capture,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    clock: _Clock | None = None,
    stop: _Stop | None = None,
    budget: float = 60.0,
    max_tasks: int = 12,
    hygiene: HygieneDevice | None = None,
    notifier: NotificationSink | None = None,
    notify_on_empty: bool = False,
) -> RunContext:
    config = replace(
        RunConfig.from_environment(
            {
                'AECOINS_DEVICE': '192.168.8.65:5555',
                'AECOINS_RUNS_DIR': str(tmp_path / 'runs'),
                'AECOINS_STATE_DIR': str(tmp_path / 'state'),
                'TZ': 'UTC',
                'AECOINS_TIMEZONE': 'UTC',
            },
            root=tmp_path,
        ),
        budget_seconds=budget,
        max_tasks=max_tasks,
        notify_on_empty=notify_on_empty,
    )
    the_clock = clock if clock is not None else _Clock()
    return RunContext(
        config=config,
        capture=capture,
        corpus=corpus,
        catalogue=catalogue,
        hygiene=hygiene if hygiene is not None else _Hygiene(),
        navigate=_Navigate(),
        state=DailyStateStore(config.state_dir, clock=lambda: CAPTURED_AT),
        controller=Controller.from_frame(_RecordingDevice(), capture.frames[0], rng=random.Random(0)),
        clock=the_clock,
        sleep=the_clock.sleep,
        wall_clock=lambda: CAPTURED_AT,
        stop=stop if stop is not None else _Stop(),
        logger=_logger(),
        notifier=notifier,
    )


def _logger() -> logging.Logger:
    logger = logging.getLogger('test.engine')
    logger.setLevel(logging.INFO)
    return logger


def _surface(
    monkeypatch: pytest.MonkeyPatch,
    roster: Roster,
    snapshot: Snapshot,
) -> None:
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: roster)
    monkeypatch.setattr(
        engine,
        'open_boost_list',
        lambda *args, **kwargs: BoostList(snapshot=snapshot, route=DiscoveryRoute.BOOST, detail='test'),
    )


def _report_payload(tmp_path: Path) -> dict[str, Any]:
    directories = sorted((tmp_path / 'runs').iterdir())
    assert directories
    return cast(dict[str, Any], json.loads((directories[-1] / 'run.json').read_text(encoding='utf-8')))


def test_the_dispatch_table_covers_every_implemented_category(catalogue: TaskCatalogue) -> None:
    assert set(engine.DISPATCH_TABLE) == {
        TaskCategory.SIGN_IN,
        TaskCategory.QUIZ,
        TaskCategory.BROWSE_WAIT,
        TaskCategory.ITEM_TAP,
        TaskCategory.NOTIFICATION,
    }
    assert TaskCategory.SHORTCUT not in engine.DISPATCH_TABLE
    assert TaskCategory.WIDGET not in engine.DISPATCH_TABLE
    assert catalogue.allowed


@pytest.mark.slow
def test_a_full_walk_dispatches_in_reading_order_and_reports_the_task_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boost_frame: Snapshot,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    wanted = ('coins_quiz', 'notifications', 'sponsored_items')
    entries = tuple(entry for entry in full_roster.entries if entry.key in wanted)
    roster = replace(full_roster, entries=entries)
    frame = _boost_frame(*wanted)
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        max_tasks=2,
    )
    order: list[str] = []

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        order.append(entry.key or '')
        if entry.key is not None:
            _context.state.record(entry.key)
        return EngineResult('completed', 'fake', 'done', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert order == ['coins_quiz', 'sponsored_items']
    assert report.status is RunStatus.BUDGET
    assert report.detail is not None
    assert 'task budget of 2' in report.detail
    assert [task.key for task in report.tasks] == ['coins_quiz', 'sponsored_items']
    assert all(task.completed for task in report.tasks)
    assert report.returned_to_coins_page is True
    assert report.observations >= 3
    assert context.state.snapshot().completed_night is False
    assert any(skip.key == 'notifications' and skip.reason == 'run budget' for skip in report.skipped)
    assert report.summary_path.is_file()
    assert report.json_path.is_file()
    assert report.roster_log_path.is_file()
    assert report.failure_code == FailureCode.BUDGET_EXHAUSTED.value
    assert _report_payload(tmp_path)['status'] == 'budget'


def test_policy_and_missing_executor_are_reported_as_skips(
    tmp_path: Path,
    full_roster: Roster,
) -> None:
    report = RunReport(tmp_path, 'run-1', budget_seconds=60.0, started_at=CAPTURED_AT)

    engine._record_policy_skips(report, full_roster)

    assert any(skip.title == 'Play & Earn' and skip.reason == 'policy' for skip in report.skipped)
    assert any(skip.title == 'Add a coins shortcut' and skip.reason == 'no executor' for skip in report.skipped)


@pytest.mark.slow
def test_a_wall_clock_budget_stops_before_the_first_task_and_lists_why(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boost_frame: Snapshot,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    roster = replace(full_roster, entries=tuple(entry for entry in full_roster.entries if entry.key == 'coins_quiz'))
    frame = _boost_frame('coins_quiz')
    clock = _Clock()
    _surface(monkeypatch, roster, frame)

    def slow_discover(*args: Any, **kwargs: Any) -> Roster:
        clock.value = 10.0
        return roster

    monkeypatch.setattr(engine, 'discover_roster', slow_discover)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        clock=clock,
        budget=5.0,
    )

    report = engine.run_once(context)

    assert report.status is RunStatus.BUDGET
    assert report.detail is not None
    assert 'wall-clock budget of 5s' in report.detail
    assert report.tasks == []
    assert context.state.snapshot().completed_night is False
    assert any(skip.key == 'coins_quiz' and skip.reason == 'run budget' for skip in report.skipped)


@pytest.mark.slow
def test_a_stop_request_ends_between_tasks_and_the_rest_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boost_frame: Snapshot,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    wanted = ('coins_quiz', 'notifications')
    roster = replace(full_roster, entries=tuple(entry for entry in full_roster.entries if entry.key in wanted))
    frame = _boost_frame(*wanted)
    _surface(monkeypatch, roster, frame)
    stop = _Stop()
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        stop=stop,
    )

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        if entry.key is not None:
            _context.state.record(entry.key)
        stop.set()
        return EngineResult('completed', 'fake', 'done', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert report.status is RunStatus.STOPPED
    assert [task.key for task in report.tasks] == ['coins_quiz']
    assert any(skip.key == 'notifications' and skip.reason == 'stopped' for skip in report.skipped)
    assert context.state.snapshot().completed_night is False
    assert report.ok is False


@pytest.mark.slow
def test_an_executor_exception_is_a_failed_task_not_a_lost_night(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boost_frame: Snapshot,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    frame = _boost_frame('coins_quiz')
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )

    def dispatch(*args: Any, **kwargs: Any) -> EngineResult:
        raise RuntimeError('executor broke')

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert report.status is RunStatus.COMPLETED
    assert len(report.tasks) == 1
    assert report.tasks[0].outcome == 'failed'
    assert 'RuntimeError: executor broke' in report.tasks[0].detail
    assert report.ok is True


def test_a_run_level_failure_still_writes_a_parseable_report(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    hygiene = _Hygiene(DeviceError('tablet absent'))
    context = _context(
        tmp_path,
        capture=_Capture(_boost_frame()),
        catalogue=catalogue,
        corpus=corpus,
        hygiene=hygiene,
    )
    context.controller = None

    report = engine.run_once(context)

    assert hygiene.restored == 1
    assert context.state.snapshot().completed_night is False
    assert report.status is RunStatus.FAILED
    assert report.error is not None
    assert 'tablet absent' in report.error
    assert report.ok is False
    assert report.returned_to_coins_page is None
    payload = _report_payload(tmp_path)
    assert payload['status'] == 'failed'
    assert 'tablet absent' in payload['error']
    summary = report.summary_path.read_text(encoding='utf-8')
    assert '**Status:** failed' in summary
    assert '**Run error:** DeviceError: tablet absent' in summary
    assert payload['shots']['failure'] == 'shots/failure.png'


def test_a_cleanup_that_cannot_prove_the_coin_page_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    roster = replace(full_roster, entries=())
    frame = _boost_header_frame()
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame),
        catalogue=catalogue,
        corpus=corpus,
    )
    monkeypatch.setattr(
        engine,
        'return_to_coins_page',
        lambda *args, **kwargs: ReturnResult(
            returned=False,
            reentered=True,
            attempts=2,
            route=SurfaceRoute.SHORTCUT,
            elapsed=1.0,
            error='no coin markers',
        ),
    )

    report = engine.run_once(context)

    assert report.returned_to_coins_page is False
    assert report.return_error == 'no coin markers'


@pytest.mark.slow
def test_a_controller_factory_is_used_after_the_first_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    created: list[Snapshot] = []

    def factory(snapshot: Snapshot) -> Controller:
        created.append(snapshot)
        return Controller.from_frame(_RecordingDevice(), snapshot, rng=random.Random(0))

    context.controller = None
    context.controller_factory = factory

    report = engine.run_once(context)

    assert report.status is RunStatus.COMPLETED
    assert created == [frame]
    assert context.state.snapshot().completed_night is True
    assert isinstance(context.hygiene, _Hygiene)
    assert context.hygiene.restored == 1


def test_a_dry_run_does_not_mark_the_night_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_task_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)

    report = engine.run_once(context)

    assert report.status is RunStatus.PLANNED
    assert report.ok is True
    assert report.plan is not None
    assert report.plan['taps_issued'] == 0
    assert context.state.snapshot().completed_night is False


def test_a_dry_run_prints_the_ordered_plan_and_every_refusal_without_tapping(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    frame = _coin_task_frame()
    context = _context(
        tmp_path,
        capture=_Capture(frame, frame),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, frame, rng=random.Random(0))

    report = engine.run_once(context)

    assert report.status is RunStatus.PLANNED
    assert report.plan is not None
    steps = report.plan['steps']
    refusals = report.plan['refusals']
    assert [step['key'] for step in steps] == ['coins_quiz']
    assert [step['order'] for step in steps] == [1]
    assert [refusal['code'] for refusal in refusals] == ['game_play_and_earn']
    assert report.plan['taps_issued'] == 0
    assert context.controller.tap_count == 0
    assert device.taps == []
    assert report.balance_before == report.balance_after == 1240.0
    assert report.balance_delta == 0.0
    assert report.returned_to_coins_page is None
    assert 'policy_refusal' in caplog.text
    assert 'dry_run_plan' in caplog.text
    payload = _report_payload(tmp_path)
    assert payload['plan']['steps'][0]['key'] == 'coins_quiz'
    assert payload['plan']['refusals'][0]['code'] == 'game_play_and_earn'


def test_the_dry_run_plan_reports_every_reason_a_row_is_not_a_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_frame()
    quiz = _entry(full_roster, 'coins_quiz')
    no_action = replace(quiz, action=None)
    shortcut = _entry(full_roster, 'coins_shortcut')
    sponsored = _entry(full_roster, 'sponsored_items')
    surprise = _entry(full_roster, 'surprise_items')
    roster = replace(full_roster, entries=(no_action, shortcut, sponsored, quiz, surprise))
    monkeypatch.setattr(engine, 'enumerate_roster', lambda *args, **kwargs: roster)
    context = _context(
        tmp_path,
        capture=_Capture(frame),
        catalogue=catalogue,
        corpus=corpus,
        max_tasks=12,
    )
    context.config = replace(context.config, dry_run=True)
    for _ in range(sponsored.spec.max_per_run if sponsored.spec is not None else 1):
        context.state.record('sponsored_items')

    report = engine.run_once(context, max_tasks=1, budget_seconds=5.0)

    assert report.status is RunStatus.PLANNED
    assert report.plan is not None
    assert report.plan['max_tasks'] == 1
    assert report.plan['budget_seconds'] == 5.0
    assert [step['key'] for step in report.plan['steps']] == ['coins_quiz']
    reasons = {skip.reason for skip in report.skipped}
    assert {'no action', 'no executor', 'daily limit', 'run budget'} <= reasons


def test_a_dry_run_may_fire_the_shortcut_route_but_still_taps_nothing(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_home_frame(), _coin_task_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, _home_frame(), rng=random.Random(0))
    assert isinstance(context.navigate, _Navigate)

    report = engine.run_once(context)

    assert context.navigate.routes == [SurfaceRoute.SHORTCUT]
    assert report.status is RunStatus.PLANNED
    assert report.plan is not None
    assert [step['key'] for step in report.plan['steps']] == ['coins_quiz']
    assert context.controller.tap_count == 0
    assert device.taps == []


def test_a_dry_run_that_cannot_read_the_coins_page_fails_with_a_named_reason(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_home_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, _home_frame(), rng=random.Random(0))

    def fail(_route: SurfaceRoute) -> None:
        raise DeviceUnavailable('shortcut route unavailable')

    context.navigate = cast(Any, fail)

    report = engine.run_once(context)

    assert report.status is RunStatus.FAILED
    assert report.ok is False
    assert report.failure_code == FailureCode.NAVIGATION_FAILED.value
    assert report.plan_note == 'could not read the coins page: DeviceUnavailable: shortcut route unavailable'
    assert report.detail is not None
    assert 'surface_unknown' in report.detail
    assert report.plan is not None
    assert report.plan['steps'] == []
    assert report.plan['taps_issued'] == 0
    assert context.controller.tap_count == 0
    assert device.taps == []


def test_a_dry_run_waits_out_the_skeleton_before_planning_the_rendered_roster(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    skeleton = _skeleton_frame()
    content = _coin_task_frame()
    context = _context(
        tmp_path,
        capture=_Capture(skeleton, skeleton, content, content),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, skeleton, rng=random.Random(0))

    report = engine.run_once(context)

    assert report.status is RunStatus.PLANNED
    assert report.ok is True
    assert report.plan is not None
    assert [step['key'] for step in report.plan['steps']] == ['coins_quiz']
    assert [refusal['code'] for refusal in report.plan['refusals']] == ['game_play_and_earn']
    assert report.plan['taps_issued'] == 0
    waits = report.plan['waits']
    assert isinstance(waits, list)
    assert waits[-1]['state'] == 'page'
    assert waits[-1]['history'][0] == 'loading'
    assert context.controller.tap_count == 0
    assert device.taps == []


def test_a_dry_run_waits_for_the_inline_rows_after_the_coin_page_markers(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_home_frame(), _coin_frame(), _coin_task_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, _home_frame(), rng=random.Random(0))

    report = engine.run_once(context)

    assert report.status is RunStatus.PLANNED
    assert report.plan is not None
    assert [step['key'] for step in report.plan['steps']] == ['coins_quiz']
    assert [refusal['code'] for refusal in report.plan['refusals']] == ['game_play_and_earn']
    assert report.plan['taps_issued'] == 0
    content_waits = report.plan['content_waits']
    assert isinstance(content_waits, list)
    assert content_waits[-1]['status'] == 'satisfied'
    assert content_waits[-1]['history'][0] is False
    assert context.controller.tap_count == 0
    assert device.taps == []


def test_a_dry_run_that_never_renders_fails_instead_of_planning_nothing(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    skeleton = _skeleton_frame()
    context = _context(
        tmp_path,
        capture=_Capture(skeleton),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, skeleton, rng=random.Random(0))

    report = engine.run_once(context)

    assert report.status is RunStatus.FAILED
    assert report.ok is False
    assert report.failure_code == FailureCode.NAVIGATION_FAILED.value
    assert report.detail is not None
    assert report.detail.startswith('surface_unknown: the coin page did not render')
    assert 'states=loading' in report.detail
    assert report.plan is not None
    assert report.plan['surface'] == 'unknown'
    assert report.plan['steps'] == []
    assert report.plan['taps_issued'] == 0
    assert context.controller.tap_count == 0
    assert device.taps == []


def test_a_dry_run_warm_re_entry_resets_the_empty_boost_load_fault(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    empty = _empty_boost_frame()
    content = _coin_task_frame()
    context = _context(
        tmp_path,
        capture=_Capture(empty, content),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, empty, rng=random.Random(0))
    assert isinstance(context.navigate, _Navigate)
    assert context.navigate.routes == []

    report = engine.run_once(context)

    assert context.navigate.routes == [SurfaceRoute.SHORTCUT]
    assert report.status is RunStatus.PLANNED
    assert report.ok is True
    assert report.plan is not None
    assert [step['key'] for step in report.plan['steps']] == ['coins_quiz']
    assert report.plan['taps_issued'] == 0
    assert context.controller.tap_count == 0
    assert device.taps == []


def test_a_dry_run_that_stays_on_the_empty_boost_page_fails_with_its_own_reason(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    empty = _empty_boost_frame()
    context = _context(
        tmp_path,
        capture=_Capture(empty),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, empty, rng=random.Random(0))
    assert isinstance(context.navigate, _Navigate)

    report = engine.run_once(context)

    assert context.navigate.routes == [SurfaceRoute.SHORTCUT]
    assert report.status is RunStatus.FAILED
    assert report.ok is False
    assert report.failure_code == FailureCode.NAVIGATION_FAILED.value
    assert report.detail is not None
    assert report.detail.startswith('surface_empty_boost')
    assert report.plan is not None
    assert report.plan['surface'] == 'empty_boost'
    assert report.plan['steps'] == []
    assert report.plan['taps_issued'] == 0
    assert report.shots.get('failure') == 'shots/failure.png'
    assert (report.run_dir / 'shots' / 'failure.png').is_file()
    assert context.controller.tap_count == 0
    assert device.taps == []


def test_a_dry_run_fails_when_a_popup_covers_the_coin_page(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    covered = _coin_popup_frame()
    context = _context(
        tmp_path,
        capture=_Capture(covered),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, covered, rng=random.Random(0))
    assert isinstance(context.navigate, _Navigate)

    report = engine.run_once(context)

    assert context.navigate.routes == [SurfaceRoute.SHORTCUT]
    assert report.status is RunStatus.FAILED
    assert report.ok is False
    assert report.failure_code == FailureCode.NAVIGATION_FAILED.value
    assert report.detail is not None
    assert report.detail.startswith('surface_popup')
    assert report.plan is not None
    assert report.plan['surface'] == 'unknown'
    assert report.plan['steps'] == []
    assert report.plan['taps_issued'] == 0
    assert context.controller.tap_count == 0
    assert device.taps == []


def test_a_dry_run_fails_when_the_inline_rows_never_render(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, _coin_frame(), rng=random.Random(0))

    report = engine.run_once(context)

    assert report.status is RunStatus.FAILED
    assert report.ok is False
    assert report.failure_code == FailureCode.NAVIGATION_FAILED.value
    assert report.detail is not None
    assert report.detail.startswith('surface_rows_incomplete')
    assert '(row wait timed_out)' in report.detail
    assert report.plan is not None
    assert report.plan['surface'] == 'rows_absent'
    assert report.plan['steps'] == []
    assert report.plan['taps_issued'] == 0
    assert context.controller.tap_count == 0
    assert device.taps == []


def test_a_dry_run_keeps_rows_read_from_an_unverified_surface(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _snapshot(
        _box('Take the coins quiz', 180, 1269, 700, 1305),
        _box('GO', 982, 1273, 1040, 1310),
    )
    context = _context(
        tmp_path,
        capture=_Capture(frame),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, frame, rng=random.Random(0))
    assert isinstance(context.navigate, _Navigate)

    report = engine.run_once(context)

    assert context.navigate.routes == []
    assert report.status is RunStatus.FAILED
    assert report.failure_code == FailureCode.NAVIGATION_FAILED.value
    assert report.detail is not None
    assert report.detail.startswith('surface_unverified')
    assert 'rows: Take the coins quiz' in report.detail
    assert report.plan is not None
    assert report.plan['surface'] == 'unknown'
    assert [step['key'] for step in report.plan['steps']] == ['coins_quiz']
    assert report.plan['taps_issued'] == 0
    assert report.roster is not None
    assert report.roster['count'] == 1
    assert context.controller.tap_count == 0
    assert device.taps == []


def test_a_dry_run_keeps_post_route_rows_that_never_leave_loading(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    loading = _snapshot(
        _box('Take the coins quiz', 180, 1269, 700, 1305),
        _box('GO', 982, 1273, 1040, 1310),
    )
    assert classify(loading).state is ScreenState.LOADING
    context = _context(
        tmp_path,
        capture=_Capture(_home_frame(), loading),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, _home_frame(), rng=random.Random(0))
    assert isinstance(context.navigate, _Navigate)

    report = engine.run_once(context)

    assert context.navigate.routes == [SurfaceRoute.SHORTCUT]
    assert report.status is RunStatus.FAILED
    assert report.failure_code == FailureCode.NAVIGATION_FAILED.value
    assert report.detail is not None
    assert report.detail.startswith('surface_unverified')
    assert 'rows: Take the coins quiz' in report.detail
    assert 'states=loading' in report.detail
    assert report.plan is not None
    assert report.plan['surface'] == 'unknown'
    assert [step['key'] for step in report.plan['steps']] == ['coins_quiz']
    assert report.plan['taps_issued'] == 0
    assert report.roster is not None
    assert report.roster['count'] == 1
    assert context.controller.tap_count == 0
    assert device.taps == []


def test_a_dry_run_fails_when_the_coin_page_shows_a_title_without_a_row(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _snapshot(
        _box('Coins credits to redeem', 100, 330, 700, 380),
        _box('Daily coins', 100, 560, 500, 610),
        _box('Take the coins quiz', 180, 1269, 700, 1305),
    )
    context = _context(
        tmp_path,
        capture=_Capture(frame),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, frame, rng=random.Random(0))
    assert isinstance(context.navigate, _Navigate)

    report = engine.run_once(context)

    assert context.navigate.routes == []
    assert report.status is RunStatus.FAILED
    assert report.failure_code == FailureCode.NAVIGATION_FAILED.value
    assert report.detail is not None
    assert report.detail.startswith('surface_incomplete')
    assert report.plan is not None
    assert report.plan['surface'] == 'rows_present'
    assert report.plan['steps'] == []
    assert report.plan['taps_issued'] == 0
    assert context.controller.tap_count == 0
    assert device.taps == []


def test_a_dry_run_on_a_pre_render_unknown_sequence_aborts_with_the_guard(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_home_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, _home_frame(), rng=random.Random(0))
    assert isinstance(context.navigate, _Navigate)

    report = engine.run_once(context)

    assert context.navigate.routes == [SurfaceRoute.SHORTCUT]
    assert report.status is RunStatus.ABORTED
    assert report.ok is False
    assert report.failure_code == FailureCode.UNEXPECTED_SCREEN.value
    assert report.plan is None
    assert context.controller.tap_count == 0
    assert device.taps == []


def test_a_dry_run_route_failure_on_a_readable_empty_page_is_recorded(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_frame()
    context = _context(
        tmp_path,
        capture=_Capture(frame),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, frame, rng=random.Random(0))

    def fail(_route: SurfaceRoute) -> None:
        raise DeviceUnavailable('warm re-entry unavailable')

    context.navigate = cast(Any, fail)

    report = engine.run_once(context)

    assert report.status is RunStatus.FAILED
    assert report.ok is False
    assert report.failure_code == FailureCode.NAVIGATION_FAILED.value
    assert report.detail is not None
    assert report.detail.startswith('surface_rows_incomplete')
    assert report.plan_note == 'could not read the coins page: DeviceUnavailable: warm re-entry unavailable'
    assert report.plan is not None
    assert report.plan['surface'] == 'rows_absent'
    assert report.plan['steps'] == []
    assert report.plan['taps_issued'] == 0
    assert context.controller.tap_count == 0
    assert device.taps == []


def test_a_dry_run_without_a_readable_balance_says_so(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _snapshot(
        _box('Coins credits to redeem', 100, 330, 700, 380),
        _box('Daily coins', 100, 560, 500, 610),
    )
    context = _context(
        tmp_path,
        capture=_Capture(frame),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)

    report = engine.run_once(context)

    assert report.balance_before is None
    assert report.balance_note == 'the coins page did not render a readable balance for the dry-run baseline'


def test_a_row_refused_by_the_gate_is_reported_as_a_policy_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_frame()
    context = _context(
        tmp_path,
        capture=_Capture(frame),
        catalogue=catalogue,
        corpus=corpus,
    )
    assert context.controller is not None
    refused = replace(_entry(full_roster, 'coins_quiz'), title='Play & Earn')
    monkeypatch.setattr(
        engine,
        '_dispatch_entry',
        lambda *args, **kwargs: pytest.fail('a refused row reached an executor'),
    )

    result = engine._dispatch_with_retry(context, context.controller, refused)

    assert result.outcome == 'skipped'
    assert result.verification == 'policy'
    assert result.completed is False
    assert result.payload['code'] == 'game_play_and_earn'


@pytest.mark.slow
def test_a_balance_decrease_during_a_task_aborts_and_still_returns_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),), surface_variant=Surface.ROWS_PRESENT)
    _surface(monkeypatch, roster, _boost_frame('coins_quiz'))
    context = _context(
        tmp_path,
        capture=_Capture(_coin_frame('40'), _coin_frame('39'), _coin_frame('39')),
        catalogue=catalogue,
        corpus=corpus,
    )

    def spend(*_args: Any, **_kwargs: Any) -> Any:
        engine._capture(context.capture)
        return _FakeResult(completed=True)

    monkeypatch.setattr(engine, 'run_quiz_task', spend)

    report = engine.run_once(context)

    assert report.status is RunStatus.ABORTED
    assert report.failure_code == FailureCode.BALANCE_DECREASED.value
    assert report.abort is not None
    assert 'decreased from 40 to 39' in report.abort['reason']
    assert report.returned_to_coins_page is True


def test_an_aborted_run_reports_the_screen_and_returns_to_the_coins_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_frame()
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),), surface_variant=Surface.ROWS_PRESENT)
    _surface(monkeypatch, roster, _boost_frame('coins_quiz'))
    context = _context(
        tmp_path,
        capture=_Capture(frame, frame, frame, frame),
        catalogue=catalogue,
        corpus=corpus,
    )
    screen = ScreenReport(
        state='unknown',
        surface='unknown',
        labels=('Mystery checkout', 'Buy now'),
        fingerprint='unknown/unknown/mystery checkout|buy now',
    )
    abort = RunAborted(code=AbortCode.DANGEROUS_CONTROL, reason="a 'Buy now' control appeared", screen=screen)

    def abort_dispatch(*_args: Any, **_kwargs: Any) -> EngineResult:
        raise abort

    monkeypatch.setattr(engine, '_dispatch_entry', abort_dispatch)

    report = engine.run_once(context)

    assert report.status is RunStatus.ABORTED
    assert report.ok is False
    assert report.failure_code == FailureCode.DANGEROUS_CONTROL.value
    assert report.abort is not None
    assert report.abort['screen']['labels'] == ['Mystery checkout', 'Buy now']
    assert report.abort['reason'] == "a 'Buy now' control appeared"
    assert report.returned_to_coins_page is True
    summary = report.summary_path.read_text(encoding='utf-8')
    assert '**Abort:** dangerous_control' in summary
    assert 'Mystery checkout' in summary
    payload = _report_payload(tmp_path)
    assert payload['status'] == 'aborted'
    assert payload['abort']['screen']['surface'] == 'unknown'


@pytest.mark.slow
def test_a_phone_geometry_runs_and_records_its_profile(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _snapshot(
        _box('Coins credits to redeem', 100, 330, 700, 380),
        _box('Daily coins', 100, 560, 500, 610),
        size=(1080, 1920),
    )
    hygiene = _Hygiene()
    context = _context(
        tmp_path,
        capture=_Capture(frame),
        catalogue=catalogue,
        corpus=corpus,
        hygiene=hygiene,
    )

    report = engine.run_once(context)

    assert report.status is RunStatus.COMPLETED
    assert report.failure_code is None
    assert report.geometry is not None
    assert report.geometry['width'] == 1080
    assert report.geometry['height'] == 1920
    assert report.geometry['orientation'] == 'portrait'
    assert report.geometry['profile_id'] == '2d1b067c8854d499'
    assert hygiene.restored == 1
    payload = _report_payload(tmp_path)
    assert payload['status'] == 'completed'
    assert payload['geometry']['profile_id'] == '2d1b067c8854d499'


def test_dispatch_routes_each_category_to_its_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_frame()
    context = _context(
        tmp_path,
        capture=_Capture(frame),
        catalogue=catalogue,
        corpus=corpus,
    )
    assert context.controller is not None
    controller = context.controller
    called: list[str] = []

    def fake(name: str) -> Any:
        def executor(*args: Any, **kwargs: Any) -> _FakeResult:
            called.append(name)
            return _FakeResult(completed=True)

        return executor

    monkeypatch.setattr(engine, 'run_quiz_task', fake('quiz'))
    monkeypatch.setattr(engine, 'run_browse_task', fake('browse'))
    monkeypatch.setattr(engine, 'run_surprise_task', fake('surprise'))
    monkeypatch.setattr(engine, 'run_notifications_task', fake('notifications'))

    def signin(*args: Any, **kwargs: Any) -> SigninReport:
        called.append('signin')
        claim = ClaimResult(
            key='daily_checkin',
            outcome=SigninOutcome.COMPLETED,
            detail='claimed',
            verification='balance_delta',
            recorded=True,
        )
        return SigninReport(streak=claim)

    monkeypatch.setattr(engine, 'claim_daily_signins', signin)

    for key, name in (
        ('boost_checkin', 'signin'),
        ('coins_quiz', 'quiz'),
        ('sponsored_items', 'browse'),
        ('surprise_items', 'surprise'),
        ('notifications', 'notifications'),
    ):
        result = engine._dispatch_entry(context, controller, _entry(full_roster, key))
        assert result.completed is True
        assert called[-1] == name

    with pytest.raises(ValueError, match='no executor'):
        engine._dispatch_entry(context, controller, _entry(full_roster, 'coins_shortcut'))


@pytest.mark.parametrize(
    ('outcome', 'verification', 'expected'),
    [
        (SigninOutcome.COMPLETED, 'balance_delta', 'completed'),
        (SigninOutcome.ALREADY_DONE, 'state', 'skipped'),
        (SigninOutcome.UNVERIFIED, 'no_evidence', 'failed'),
        (SigninOutcome.UNAVAILABLE, 'not_found', 'skipped'),
    ],
)
def test_signin_normalises_both_claims_into_one_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    outcome: SigninOutcome,
    verification: str,
    expected: str,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_boost_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    entry = _entry(full_roster, 'boost_checkin')
    claim = ClaimResult(
        key='daily_checkin',
        outcome=outcome,
        detail='claim detail',
        verification=verification,
        recorded=outcome is SigninOutcome.COMPLETED,
    )
    monkeypatch.setattr(engine, 'claim_daily_signins', lambda *args, **kwargs: SigninReport(streak=claim))
    assert context.controller is not None
    common = {
        'navigate': context.navigate,
        'templates': context.templates,
        'settle': 0.0,
        'clock': context.clock,
        'sleep': context.sleep,
        'logger': context.logger,
    }

    result = engine._run_signin(context, context.controller, entry, engine._spec(entry), common)

    assert result.outcome == expected
    assert result.completed is (outcome is SigninOutcome.COMPLETED)
    assert result.verification == verification
    assert isinstance(context.navigate, _Navigate)
    assert context.navigate.routes == [SurfaceRoute.SHORTCUT]


def test_signin_does_nothing_when_both_state_keys_are_done(
    tmp_path: Path,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_boost_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.state.record('daily_checkin')
    context.state.record('boost_checkin')
    entry = _entry(full_roster, 'boost_checkin')
    assert context.controller is not None
    common = {
        'navigate': context.navigate,
        'templates': context.templates,
        'settle': 0.0,
        'clock': context.clock,
        'sleep': context.sleep,
        'logger': context.logger,
    }

    result = engine._run_signin(context, context.controller, entry, engine._spec(entry), common)

    assert result.outcome == 'skipped'
    assert result.completed is False
    assert isinstance(context.navigate, _Navigate)
    assert context.navigate.routes == []


def test_make_live_context_wraps_the_real_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_frame()
    navigated: list[SurfaceRoute] = []

    class FakeSnapshotter:
        def __init__(self, serial: str, *, ocr: object, clock: Any) -> None:
            assert serial == '192.168.8.65:5555'
            assert clock() == CAPTURED_AT

        def capture(self) -> Snapshot:
            return frame

    class FakeDevice:
        def __init__(self, serial: str, *, app_settle_seconds: float, settle: Any) -> None:
            assert serial == '192.168.8.65:5555'
            assert app_settle_seconds == 10.0

        def prepare(self) -> HygieneReport:
            return HygieneReport('192.168.8.65:5555', 'device', True, True, 'ready')

        def navigate(self, route: SurfaceRoute) -> None:
            navigated.append(route)

        def check_connected(self) -> None:
            return None

        def restore(self) -> None:
            navigated.append(SurfaceRoute.WIDGET)

    class FakeInput:
        def __init__(self, serial: str) -> None:
            assert serial == '192.168.8.65:5555'

        def tap(self, x: int, y: int) -> None:
            del x, y

    monkeypatch.setattr(engine, 'AdbSnapshotter', FakeSnapshotter)
    monkeypatch.setattr(engine, 'AdbDevice', FakeDevice)
    monkeypatch.setattr(engine, 'AdbInputDevice', FakeInput)
    monkeypatch.setattr(engine, 'RapidOcrEngine', lambda: object())
    monkeypatch.setattr(engine, 'load_popup_corpus', lambda path: corpus)
    monkeypatch.setattr(engine, 'load_task_catalogue', lambda path: catalogue)
    monkeypatch.setattr(engine, 'load_templates', lambda value: None)
    config = replace(
        RunConfig.from_environment(
            {
                'AECOINS_DEVICE': '192.168.8.65:5555',
                'AECOINS_RUNS_DIR': str(tmp_path / 'runs'),
                'AECOINS_STATE_DIR': str(tmp_path / 'state'),
                'TZ': 'UTC',
            },
            root=tmp_path,
        ),
        app_settle_seconds=10.0,
    )
    stop = _Stop()

    context = engine.make_live_context(config, stop=stop, wall_clock=lambda: CAPTURED_AT)

    assert engine._capture(context.capture) == frame
    context.navigate(SurfaceRoute.SHORTCUT)
    assert navigated == [SurfaceRoute.SHORTCUT]
    assert context.controller is None
    assert context.controller_factory is not None
    controller = context.controller_factory(frame)
    assert controller.geometry == frame.geometry
    assert context.hygiene.prepare().serial == config.device
    assert SHORTCUT_DEEP_LINK.startswith('aliexpress://')
    stop.set()
    with pytest.raises(RunInterrupted):
        context.sleep(1)
    context.hygiene.restore()
    assert navigated == [SurfaceRoute.SHORTCUT, SurfaceRoute.WIDGET]


def test_make_live_context_requires_a_device(tmp_path: Path) -> None:
    config = RunConfig.from_environment(
        {
            'AECOINS_RUNS_DIR': str(tmp_path / 'runs'),
            'AECOINS_STATE_DIR': str(tmp_path / 'state'),
            'TZ': 'UTC',
        },
        root=tmp_path,
    )
    with pytest.raises(Exception, match='AECOINS_DEVICE is not set'):
        engine.make_live_context(config, stop=_Stop())


def test_the_guarded_seams_raise_when_a_stop_is_already_requested() -> None:
    stop = _Stop(active=True)
    with pytest.raises(RunInterrupted):
        engine._guarded_sleep(stop)(0)
    with pytest.raises(RunInterrupted):
        engine._guarded_sleep(stop)(1)
    with pytest.raises(RunInterrupted):
        engine._guarded_call(lambda: 1, stop)()


def test_the_guarded_seams_pass_the_result_and_observe_a_late_stop() -> None:
    stop = _Stop()
    sleep = engine._guarded_sleep(stop)
    sleep(0)
    sleep(0.1)
    assert engine._guarded_call(lambda value: value + 1, stop)(1) == 2
    stop.set()
    with pytest.raises(RunInterrupted):
        engine._guarded_call(lambda: 1, stop)()


def test_no_stop_flag_never_stops() -> None:
    never = engine._NeverStop()
    assert never.is_set() is False
    assert never.wait(10) is False


def test_an_unknown_category_is_reported_as_a_bad_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_boost_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    entry = _entry(full_roster, 'coins_quiz')
    assert entry.spec is not None
    assert context.controller is not None
    spec = entry.spec.model_copy(update={'category': TaskCategory.WIDGET})
    fake_entry = replace(entry, decision=replace(entry.decision, spec=spec))
    with pytest.raises(ValueError, match='no executor'):
        engine._dispatch_entry(context, context.controller, fake_entry)


def test_run_once_validates_its_own_budget_arguments(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_boost_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    with pytest.raises(ValueError, match='budget_seconds must be positive'):
        engine.run_once(context, budget_seconds=0)
    with pytest.raises(ValueError, match='max_tasks must be positive'):
        engine.run_once(context, max_tasks=0)


@pytest.mark.slow
def test_a_stop_that_interrupts_a_task_is_reported_as_a_stopped_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_frame('coins_quiz')
    _surface(monkeypatch, replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),)), frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )

    def dispatch(*args: Any, **kwargs: Any) -> EngineResult:
        raise RunInterrupted('signal arrived mid-task')

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert report.status is RunStatus.STOPPED
    assert report.detail is not None
    assert 'signal arrived mid-task' in report.detail


def test_a_stop_during_hygiene_is_reported_without_a_controller(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_boost_frame()),
        catalogue=catalogue,
        corpus=corpus,
        hygiene=_Hygiene(RunInterrupted('stopped before capture')),
    )
    context.controller = None

    report = engine.run_once(context)

    assert report.status is RunStatus.STOPPED
    assert report.detail is not None
    assert 'stopped before capture' in report.detail
    assert report.returned_to_coins_page is None


def test_roster_observation_re_raises_a_safety_abort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_boost_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    assert context.controller is not None
    observer = engine._observer(
        context,
        RunReport(tmp_path, 'observe-abort', budget_seconds=60.0, started_at=CAPTURED_AT),
        full_roster,
    )
    monkeypatch.setattr(
        engine,
        'open_boost_list',
        lambda *args, **kwargs: (_ for _ in ()).throw(_safety_abort()),
    )

    with pytest.raises(RunAborted):
        engine._observe(context, context.controller, observer, route=None)


def test_balance_proof_re_raises_a_safety_abort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_frame()
    context = _context(
        tmp_path,
        capture=_Capture(frame),
        catalogue=catalogue,
        corpus=corpus,
    )
    assert context.controller is not None
    report = RunReport(tmp_path / 'runs', 'run-1', budget_seconds=60.0, started_at=CAPTURED_AT)
    monkeypatch.setattr(
        engine,
        'return_to_coins_page',
        lambda *args, **kwargs: (_ for _ in ()).throw(_safety_abort(AbortCode.BALANCE_DECREASED)),
    )

    with pytest.raises(RunAborted):
        engine._record_balance_before(
            context,
            context.controller,
            report,
            ArtifactStore(report.runs_dir, report.run_id),
            frame,
            _logger(),
        )


@pytest.mark.slow
def test_a_notification_task_abort_reaches_the_engine_and_returns_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    roster = replace(
        full_roster,
        entries=(_entry(full_roster, 'notifications'),),
        surface_variant=Surface.BOOST_PAGE,
    )
    _surface(monkeypatch, roster, _boost_frame('notifications'))
    context = _context(
        tmp_path,
        capture=_Capture(_coin_frame(), _boost_frame('notifications'), _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    monkeypatch.setattr(
        notifications_module,
        '_wait_after_action',
        lambda *args, **kwargs: (_ for _ in ()).throw(_safety_abort()),
    )

    report = engine.run_once(context)

    assert report.status is RunStatus.ABORTED
    assert report.failure_code == FailureCode.DANGEROUS_CONTROL.value
    assert report.abort is not None
    assert report.returned_to_coins_page is True


def test_roster_observation_falls_back_to_the_current_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_frame()
    context = _context(
        tmp_path,
        capture=_Capture(frame),
        catalogue=catalogue,
        corpus=corpus,
    )
    assert context.controller is not None
    observer = engine._observer(
        context,
        RunReport(tmp_path, 'observe-1', budget_seconds=60.0, started_at=CAPTURED_AT),
        full_roster,
    )
    monkeypatch.setattr(engine, 'open_boost_list', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('open')))

    observation = engine._observe(context, context.controller, observer, route=None)

    assert observation is not None
    assert observation.error is None


def test_roster_observation_returns_none_when_both_reads_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_boost_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    assert context.controller is not None
    observer = engine._observer(
        context,
        RunReport(tmp_path, 'observe-2', budget_seconds=60.0, started_at=CAPTURED_AT),
        full_roster,
    )
    monkeypatch.setattr(engine, 'open_boost_list', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('open')))

    def broken_capture() -> Snapshot:
        raise RuntimeError('capture')

    context.capture = broken_capture

    assert engine._observe(context, context.controller, observer, route=None) is None


def test_roster_observation_re_raises_a_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_boost_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    assert context.controller is not None
    observer = engine._observer(
        context,
        RunReport(tmp_path, 'observe-3', budget_seconds=60.0, started_at=CAPTURED_AT),
        full_roster,
    )
    monkeypatch.setattr(
        engine,
        'open_boost_list',
        lambda *args, **kwargs: (_ for _ in ()).throw(RunInterrupted('stop')),
    )
    with pytest.raises(RunInterrupted):
        engine._observe(context, context.controller, observer, route=None)


def test_policy_skips_include_a_row_with_no_dispatch_action(
    tmp_path: Path,
    full_roster: Roster,
) -> None:
    report = RunReport(tmp_path, 'run-1', budget_seconds=60.0, started_at=CAPTURED_AT)
    entry = replace(_entry(full_roster, 'coins_quiz'), action=None)

    engine._record_policy_skips(report, replace(full_roster, entries=(entry,)))

    assert [(skip.key, skip.reason) for skip in report.skipped] == [('coins_quiz', 'no action')]


def test_policy_and_unrun_helpers_accept_no_roster(
    tmp_path: Path, catalogue: TaskCatalogue, corpus: PopupCorpus
) -> None:
    report = RunReport(tmp_path, 'run-1', budget_seconds=60.0, started_at=CAPTURED_AT)
    context = _context(tmp_path, capture=_Capture(_boost_frame()), catalogue=catalogue, corpus=corpus)

    engine._record_policy_skips(report, None)
    engine._record_unrun(report, None, context.state, set(), status=RunStatus.COMPLETED, detail=None)

    assert report.skipped == []


def test_unrun_rows_distinguish_daily_limits_from_the_stop_reason(
    tmp_path: Path,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    report = RunReport(tmp_path, 'run-1', budget_seconds=60.0, started_at=CAPTURED_AT)
    context = _context(tmp_path, capture=_Capture(_boost_frame()), catalogue=catalogue, corpus=corpus)
    context.state.record('coins_quiz')
    entries = tuple(
        replace(entry, action=None) if entry.key == 'notifications' else entry for entry in full_roster.entries
    )
    roster = replace(full_roster, entries=entries)

    engine._record_unrun(report, roster, context.state, set(), status=RunStatus.COMPLETED, detail=None)

    assert any(skip.key == 'coins_quiz' and skip.reason == 'daily limit' for skip in report.skipped)
    assert any(skip.key == 'boost_checkin' and skip.reason == 'not selected' for skip in report.skipped)
    assert not any(skip.key == 'notifications' for skip in report.skipped)
    assert not any(skip.key == 'sponsored_items' and skip.reason == 'run budget' for skip in report.skipped)


def test_next_entry_skips_denied_unsupported_no_action_blocked_and_daily_limit_rows(
    tmp_path: Path,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(tmp_path, capture=_Capture(_boost_frame()), catalogue=catalogue, corpus=corpus)
    context.state.record('boost_checkin')
    entries = tuple(
        replace(entry, action=None) if entry.key == 'coins_quiz' else entry for entry in full_roster.entries
    )

    entry = engine._next_entry(
        replace(full_roster, entries=entries),
        context.state,
        {'notifications'},
    )

    assert entry is not None
    assert entry.key == 'sponsored_items'


def test_a_successful_cleanup_reads_the_final_balance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_boost_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    assert context.controller is not None
    coin = _coin_frame()
    report = RunReport(tmp_path, 'run-1', budget_seconds=60.0, started_at=CAPTURED_AT)
    monkeypatch.setattr(
        engine,
        'return_to_coins_page',
        lambda *args, **kwargs: ReturnResult(
            returned=True,
            reentered=True,
            attempts=1,
            route=SurfaceRoute.SHORTCUT,
            elapsed=1.0,
            snapshot=coin,
        ),
    )

    engine._finish_on_coins_page(
        context,
        context.controller,
        report,
        ArtifactStore(report.runs_dir, report.run_id),
        logging.getLogger('test.engine'),
    )

    assert report.returned_to_coins_page is True
    assert report.balance_after == 1240.0
    assert report.shots['balance-after'] == 'shots/balance-after.png'


@pytest.mark.parametrize('error', [RunInterrupted('stop'), RuntimeError('boom')])
def test_a_cleanup_error_is_reported_without_escaping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    error: Exception,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_boost_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    assert context.controller is not None
    report = RunReport(tmp_path, 'run-1', budget_seconds=60.0, started_at=CAPTURED_AT)
    monkeypatch.setattr(
        engine,
        'return_to_coins_page',
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )

    engine._finish_on_coins_page(
        context,
        context.controller,
        report,
        ArtifactStore(report.runs_dir, report.run_id),
        logging.getLogger('test.engine'),
    )

    assert report.returned_to_coins_page is False
    assert report.return_error is not None


def test_a_safety_abort_during_cleanup_is_recorded_as_aborted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame),
        catalogue=catalogue,
        corpus=corpus,
    )
    calls = {'count': 0}

    def capture() -> Snapshot:
        if calls['count'] == 0:
            calls['count'] += 1
            return frame
        raise _safety_abort(AbortCode.BALANCE_DECREASED)

    context.capture = capture

    report = engine.run_once(context)

    assert report.status is RunStatus.ABORTED
    assert report.failure_code == FailureCode.BALANCE_DECREASED.value
    assert report.abort is not None
    assert report.abort['code'] == 'balance_decreased'


def test_a_safety_abort_while_recording_a_failure_frame_is_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    monkeypatch.setattr(
        context.hygiene,
        'prepare',
        lambda: (_ for _ in ()).throw(DeviceUnavailable('device gone')),
    )
    context.controller = None
    context.capture = lambda: (_ for _ in ()).throw(_safety_abort())

    report = engine.run_once(context)

    assert report.status is RunStatus.ABORTED
    assert report.failure_code == FailureCode.DANGEROUS_CONTROL.value
    assert report.abort is not None


def test_a_stop_while_planning_a_dry_run_is_not_swallowed(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_home_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)

    def stop(_route: SurfaceRoute) -> None:
        raise RunInterrupted('stop during dry run')

    context.navigate = cast(Any, stop)

    report = engine.run_once(context)

    assert report.status is RunStatus.STOPPED
    assert report.detail is not None
    assert 'stop during dry run' in report.detail


def test_a_context_without_a_controller_factory_is_a_configuration_error(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(tmp_path, capture=_Capture(_boost_frame()), catalogue=catalogue, corpus=corpus)
    context.controller_factory = None
    with pytest.raises(Exception, match='neither a controller nor a controller factory'):
        engine._controller(context, _boost_frame())


def test_a_denied_row_is_not_a_dispatchable_spec(full_roster: Roster) -> None:
    denied = full_roster.denied_entries[0]
    with pytest.raises(ValueError, match='not an allowed row'):
        engine._spec(denied)


def test_the_default_wall_clock_is_utc() -> None:
    assert engine._utc_now().tzinfo is UTC


def test_a_guarded_call_observes_a_stop_requested_by_the_call() -> None:
    stop = _Stop()

    def stop_during_call() -> int:
        stop.set()
        return 1

    with pytest.raises(RunInterrupted):
        engine._guarded_call(stop_during_call, stop)()


def test_a_route_timeout_falls_back_to_the_current_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_frame()
    context = _context(tmp_path, capture=_Capture(frame), catalogue=catalogue, corpus=corpus)
    assert context.controller is not None
    monkeypatch.setattr(
        engine,
        'discover_roster',
        lambda *args, **kwargs: (_ for _ in ()).throw(StateTimeout('no boost page')),
    )

    roster, error = engine._discover_or_fallback(context, context.controller)

    assert error is not None
    assert 'StateTimeout' in error
    assert roster.route is DiscoveryRoute.INLINE
    assert roster.surface_variant is Surface.BOOST_PAGE
    assert roster.routes_taken == (SurfaceRoute.SHORTCUT,)
    assert roster.refresh_attempted is True
    assert roster.count == len(_ROWS)


def test_a_stop_during_route_discovery_is_not_swallowed_by_the_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(tmp_path, capture=_Capture(_boost_frame()), catalogue=catalogue, corpus=corpus)
    assert context.controller is not None
    monkeypatch.setattr(
        engine,
        'discover_roster',
        lambda *args, **kwargs: (_ for _ in ()).throw(RunInterrupted('stop during route')),
    )
    with pytest.raises(RunInterrupted):
        engine._discover_or_fallback(context, context.controller)


def test_signin_with_no_claims_is_reported_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    class EmptyReport:
        results: tuple[ClaimResult, ...] = ()
        completed: tuple[ClaimResult, ...] = ()

        def as_dict(self) -> dict[str, Any]:
            return {'streak': None, 'boost': None}

    context = _context(tmp_path, capture=_Capture(_boost_frame()), catalogue=catalogue, corpus=corpus)
    assert context.controller is not None
    entry = _entry(full_roster, 'boost_checkin')
    monkeypatch.setattr(engine, 'claim_daily_signins', lambda *args, **kwargs: EmptyReport())
    common = {
        'navigate': context.navigate,
        'templates': context.templates,
        'settle': 0.0,
        'clock': context.clock,
        'sleep': context.sleep,
        'logger': context.logger,
    }

    result = engine._run_signin(context, context.controller, entry, engine._spec(entry), common)

    assert result.outcome == 'failed'
    assert result.verification == 'state'


def test_roster_observation_re_raises_a_stop_from_the_fallback_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(tmp_path, capture=_Capture(_boost_frame()), catalogue=catalogue, corpus=corpus)
    assert context.controller is not None
    observer = engine._observer(
        context,
        RunReport(tmp_path, 'observe-4', budget_seconds=60.0, started_at=CAPTURED_AT),
        full_roster,
    )
    monkeypatch.setattr(engine, 'open_boost_list', lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('open')))

    def stopped_capture() -> Snapshot:
        raise RunInterrupted('stop after open')

    context.capture = stopped_capture

    with pytest.raises(RunInterrupted):
        engine._observe(context, context.controller, observer, route=None)


def test_next_entry_returns_none_for_only_an_unsupported_row(
    tmp_path: Path,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(tmp_path, capture=_Capture(_boost_frame()), catalogue=catalogue, corpus=corpus)
    shortcut = _entry(full_roster, 'coins_shortcut')
    assert engine._next_entry(replace(full_roster, entries=(shortcut,)), context.state, set()) is None


@pytest.mark.slow
def test_state_completion_failure_does_not_fail_a_finished_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )

    def broken_complete() -> None:
        raise OSError('state volume read-only')

    monkeypatch.setattr(context.state, 'complete_night', broken_complete)

    report = engine.run_once(context)

    assert report.status is RunStatus.COMPLETED
    assert report.ok is True


@pytest.mark.slow
def test_a_stopped_run_still_restores_rotation_after_the_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    hygiene = _Hygiene()
    stop = _Stop(active=True)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        hygiene=hygiene,
        stop=stop,
    )

    report = engine.run_once(context)

    assert report.status is RunStatus.STOPPED
    assert hygiene.restored == 1
    assert report.json_path.is_file()


@pytest.mark.slow
def test_a_final_report_write_failure_still_restores_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    hygiene = _Hygiene()
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        hygiene=hygiene,
    )
    report = RunReport(
        context.config.runs_dir,
        'report-failure',
        budget_seconds=60.0,
        started_at=CAPTURED_AT,
    )
    original_write = report.write

    def flaky_write() -> Path:
        if report.status is not RunStatus.RUNNING:
            raise ReportError('runs volume full')
        return original_write()

    monkeypatch.setattr(report, 'write', flaky_write)

    with pytest.raises(ReportError, match='runs volume full'):
        engine.run_once(context, report=report)

    assert hygiene.restored == 1


@pytest.mark.slow
def test_hygiene_restore_failure_does_not_fail_a_finished_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    hygiene = _Hygiene()
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        hygiene=hygiene,
    )

    def broken_restore() -> None:
        raise DeviceError('settings restore failed')

    monkeypatch.setattr(hygiene, 'restore', broken_restore)

    report = engine.run_once(context)

    assert report.status is RunStatus.COMPLETED
    assert report.ok is True


@pytest.mark.slow
def test_the_balance_in_the_summary_is_read_back_from_the_recorded_shots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    context = _context(
        tmp_path,
        capture=_Capture(_coin_frame('1,240'), _coin_frame('1,246')),
        catalogue=catalogue,
        corpus=corpus,
    )

    report = engine.run_once(context)

    summary = report.summary_path.read_text(encoding='utf-8')
    assert '**Balance:** 1240 -> 1246 (+6)' in summary
    assert report.balance_delta == 6.0
    store = ArtifactStore(report.runs_dir, report.run_id)
    before = read_coin_balance(store.load('balance-before'))
    after = read_coin_balance(store.load('balance-after'))
    assert (before, after) == (1240.0, 1246.0)
    assert report.shots == {
        'balance-before': 'shots/balance-before.png',
        'balance-after': 'shots/balance-after.png',
    }
    payload = _report_payload(tmp_path)
    assert payload['balance_delta'] == 6.0
    assert payload['shots'] == report.shots


@pytest.mark.slow
def test_a_cold_start_does_not_read_the_balance_from_the_home_feed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    context = _context(
        tmp_path,
        capture=_Capture(_home_frame(), _coin_frame('1,240'), _coin_frame('1,246')),
        catalogue=catalogue,
        corpus=corpus,
    )

    report = engine.run_once(context)

    assert report.balance_before == 1240.0
    assert report.balance_after == 1246.0
    assert report.balance_delta == 6.0
    assert '1240 -> 1246 (+6)' in report.summary_path.read_text(encoding='utf-8')
    store = ArtifactStore(report.runs_dir, report.run_id)
    assert read_coin_balance(store.load('balance-before')) == 1240.0


def test_an_unproven_coin_page_leaves_the_balance_unknown_and_says_why(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    monkeypatch.setattr(
        engine,
        'return_to_coins_page',
        lambda *args, **kwargs: ReturnResult(
            returned=False,
            reentered=True,
            attempts=2,
            route=SurfaceRoute.SHORTCUT,
            elapsed=1.0,
            error='no coin markers',
        ),
    )

    report = engine.run_once(context)

    assert report.balance_before is None
    assert report.balance_delta is None
    assert report.balance_note is not None
    assert 'not proven' in report.balance_note
    assert 'balance-before' not in report.shots
    summary = report.summary_path.read_text(encoding='utf-8')
    assert '**Balance:** unknown -> 1240' in summary
    assert '**Balance note:** coins page not proven before the first task' in summary


@pytest.mark.slow
def test_a_proven_coin_page_without_a_readable_balance_says_so(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    coin = _snapshot(
        _box('Coins', 100, 220, 300, 280),
        _box('Coins credits to redeem', 100, 330, 700, 380),
        _box('Daily coins', 100, 560, 500, 610),
    )
    context = _context(
        tmp_path,
        capture=_Capture(coin, coin),
        catalogue=catalogue,
        corpus=corpus,
    )

    report = engine.run_once(context)

    assert report.balance_before is None
    assert report.balance_delta is None
    assert report.balance_note == 'the coins page did not render a readable balance before the first task'
    assert 'balance-before' in report.shots
    summary = report.summary_path.read_text(encoding='utf-8')
    assert '**Balance note:** the coins page did not render a readable balance before the first task' in summary


def test_a_balance_capture_route_error_is_named_without_failing_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    monkeypatch.setattr(
        engine,
        'return_to_coins_page',
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('route died')),
    )

    report = engine.run_once(context)

    assert report.ok is True
    assert report.balance_before is None
    assert report.balance_note is not None
    assert 'route died' in report.balance_note
    assert '**Balance note:** coins page not proven before the first task: RuntimeError: route died' in (
        report.summary_path.read_text(encoding='utf-8')
    )


def test_the_balance_before_is_recorded_without_a_safety_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _coin_frame()
    context = _context(
        tmp_path,
        capture=_Capture(frame),
        catalogue=catalogue,
        corpus=corpus,
    )
    assert context.controller is not None
    report = RunReport(tmp_path / 'runs', 'run-1', budget_seconds=60.0, started_at=CAPTURED_AT)
    monkeypatch.setattr(
        engine,
        'return_to_coins_page',
        lambda *args, **kwargs: ReturnResult(
            returned=True,
            reentered=False,
            attempts=0,
            route=SurfaceRoute.SHORTCUT,
            elapsed=0.0,
            snapshot=frame,
        ),
    )

    engine._record_balance_before(
        context,
        context.controller,
        report,
        ArtifactStore(report.runs_dir, report.run_id),
        frame,
        _logger(),
    )

    assert report.balance_before == 1240.0
    assert report.balance_note is None


def test_a_stop_during_the_balance_proof_is_re_raised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    assert context.controller is not None
    report = RunReport(tmp_path, 'run-1', budget_seconds=60.0, started_at=CAPTURED_AT)
    monkeypatch.setattr(
        engine,
        'return_to_coins_page',
        lambda *args, **kwargs: (_ for _ in ()).throw(RunInterrupted('stop')),
    )

    with pytest.raises(RunInterrupted, match='stop'):
        engine._record_balance_before(
            context,
            context.controller,
            report,
            ArtifactStore(report.runs_dir, report.run_id),
            _coin_frame(),
            _logger(),
        )


def test_popups_closed_during_discovery_observation_and_cleanup_are_counted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    roster = replace(full_roster, entries=())

    def fake_discover(*args: Any, **kwargs: Any) -> Roster:
        on_dismiss = kwargs.get('on_dismiss')
        if on_dismiss is not None:
            on_dismiss(_dismiss('launch_modal'))
        return roster

    def fake_open(*args: Any, **kwargs: Any) -> BoostList:
        on_dismiss = kwargs.get('on_dismiss')
        if on_dismiss is not None:
            on_dismiss(_dismiss('observation_modal'))
        return BoostList(snapshot=_boost_header_frame(), route=DiscoveryRoute.BOOST, detail='test')

    def fake_return(*args: Any, **kwargs: Any) -> ReturnResult:
        on_dismiss = kwargs.get('on_dismiss')
        if on_dismiss is not None:
            on_dismiss(_dismiss('cleanup_modal'))
        return ReturnResult(
            returned=True,
            reentered=True,
            attempts=1,
            route=SurfaceRoute.SHORTCUT,
            elapsed=1.0,
            snapshot=_coin_frame(),
        )

    monkeypatch.setattr(engine, 'discover_roster', fake_discover)
    monkeypatch.setattr(engine, 'open_boost_list', fake_open)
    monkeypatch.setattr(engine, 'return_to_coins_page', fake_return)
    context = _context(
        tmp_path,
        capture=_Capture(_home_frame(), _boost_header_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )

    report = engine.run_once(context)

    assert report.popups_closed == {
        'launch_modal': 1,
        'observation_modal': 1,
        'cleanup_modal': 2,
    }
    assert '**Popups closed:** cleanup_modal: 2, launch_modal: 1, observation_modal: 1' in (
        report.summary_path.read_text(encoding='utf-8')
    )


@pytest.mark.slow
def test_a_broken_webhook_is_logged_after_the_run_and_keeps_the_night_successful(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    notifier = _Notifier(error=RuntimeError('webhook down'))
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        notifier=notifier,
        notify_on_empty=True,
    )

    report = engine.run_once(context)

    assert report.status is RunStatus.COMPLETED
    assert report.ok is True
    assert len(notifier.messages) == 1
    assert notifier.messages[0].status == 'completed'
    assert report.notification == {
        'attempted': True,
        'ok': False,
        'sink': 'webhook',
        'error': 'RuntimeError',
    }
    events = [
        json.loads(line)['event']
        for line in report.event_log_path.read_text(encoding='utf-8').splitlines()
        if line.strip()
    ]
    assert events.index('run_finished') < events.index('notification_failed')
    payload = _report_payload(tmp_path)
    assert payload['notification']['ok'] is False
    assert '**Notification:** failed via webhook: RuntimeError' in report.summary_path.read_text(
        encoding='utf-8',
    )


def test_a_failed_run_logs_error_and_sends_one_apprise_message(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[tuple[Any, float]] = []

    class _Response:
        def read(self) -> bytes:
            return b'ok'

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def opener(request: Any, *, timeout: float) -> _Response:
        requests.append((request, timeout))
        return _Response()

    context = _context(
        tmp_path,
        capture=_Capture(_boost_frame()),
        catalogue=catalogue,
        corpus=corpus,
        hygiene=_Hygiene(DeviceError('tablet absent')),
        notifier=AppriseSink('https://apprise.example', key='coins', opener=opener),
    )
    context.controller = None

    with caplog.at_level(logging.DEBUG, logger='test.engine'):
        report = engine.run_once(context)

    assert report.status is RunStatus.FAILED
    assert report.failure_code == FailureCode.DEVICE_OFFLINE.value
    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.full_url == 'https://apprise.example/notify/coins'
    assert timeout == 10.0
    payload = json.loads(request.data)
    assert payload['type'] == 'failure'
    assert FailureCode.DEVICE_OFFLINE.value in payload['body']
    assert report.notification == {'attempted': True, 'ok': True, 'sink': 'apprise', 'error': None}

    errors = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.levelno == logging.ERROR and record.name == 'test.engine'
    ]
    assert any(event['event'] == 'run_finished' and event['status'] == 'failed' for event in errors)

    logged = [json.loads(line) for line in report.event_log_path.read_text(encoding='utf-8').splitlines()]
    finished = next(event for event in logged if event['event'] == 'run_finished')
    assert finished['level'] == 'error'
    assert finished['failure_code'] == FailureCode.DEVICE_OFFLINE.value


@pytest.mark.slow
def test_a_broken_apprise_endpoint_keeps_the_night_successful_and_redacts_its_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    caplog: pytest.LogCaptureFixture,
) -> None:
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    secret = 'do-not-record-this'

    def broken(request: Any, *, timeout: float) -> Any:
        del request, timeout
        raise HTTPError(
            f'https://apprise.example/notify/{secret}',
            503,
            f'Service Unavailable {secret}',
            HTTPMessage(),
            None,
        )

    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        notifier=AppriseSink('https://apprise.example', opener=broken),
        notify_on_empty=True,
    )

    with caplog.at_level(logging.WARNING, logger='test.engine'):
        report = engine.run_once(context)

    assert report.status is RunStatus.COMPLETED
    assert report.ok is True
    assert report.notification == {
        'attempted': True,
        'ok': False,
        'sink': 'apprise',
        'error': 'NotificationError (HTTP 503)',
    }
    assert secret not in caplog.text
    assert secret not in report.summary_path.read_text(encoding='utf-8')
    assert secret not in report.json_path.read_text(encoding='utf-8')
    assert secret not in report.event_log_path.read_text(encoding='utf-8')


def test_a_refused_row_logs_task_skipped_at_warning_once(
    tmp_path: Path,
    full_roster: Roster,
    caplog: pytest.LogCaptureFixture,
) -> None:
    report = RunReport(tmp_path, 'run-1', budget_seconds=60.0, started_at=CAPTURED_AT)
    logger = _logger()

    with caplog.at_level(logging.DEBUG, logger='test.engine'):
        engine._record_policy_skips(report, full_roster, logger=logger)
        engine._record_policy_skips(report, full_roster, logger=logger)

    skipped = [
        (record.levelno, json.loads(record.getMessage()))
        for record in caplog.records
        if record.name == 'test.engine' and json.loads(record.getMessage()).get('event') == 'task_skipped'
    ]
    assert any(level == logging.WARNING and event['reason'] == 'policy' for level, event in skipped)
    assert len([event for _level, event in skipped if event['reason'] == 'policy']) == 1


def test_the_notification_body_has_counts_and_only_an_opted_in_balance_delta(tmp_path: Path) -> None:
    report = RunReport(tmp_path, 'run-1', budget_seconds=60.0, started_at=CAPTURED_AT)
    for outcome, completed in (('completed', True), ('skipped', False), ('failed', False)):
        report.record_task(
            title=f'{outcome} task',
            key=outcome,
            category='quiz',
            outcome=outcome,
            verification='fake',
            detail=outcome,
            duration_seconds=1.0,
            completed=completed,
            result={},
        )
    report.skip('Place an order', key=None, category=None, reason='policy', detail='refused')
    report.balance_before = 1240.0
    report.balance_after = 1246.0
    report.finish(RunStatus.COMPLETED, detail='roster exhausted', budget_used_seconds=1.0)

    body = engine._notification(report).body
    assert 'tasks: completed=1, skipped=1, failed=1' in body
    assert 'not run: policy=1' in body
    assert '1240' not in body
    assert '1246' not in body
    assert 'balance delta' not in body

    opted_in = engine._notification(report, include_balance_delta=True).body
    assert 'balance delta: +6' in opted_in
    assert '1240' not in opted_in
    assert '1246' not in opted_in


@pytest.mark.slow
def test_a_webhook_password_never_reaches_the_report_or_the_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    caplog: pytest.LogCaptureFixture,
) -> None:
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    secret = 'correct-horse-battery-staple'

    # No scheme: urllib raises while building the Request, before _post runs.
    notifier = WebhookSink(f'hooks.example/{secret}')
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        notifier=notifier,
        notify_on_empty=True,
    )

    with caplog.at_level(logging.WARNING, logger='test.engine'):
        report = engine.run_once(context)

    assert report.notification is not None
    assert report.notification['error'] == 'ValueError'
    assert secret not in caplog.text
    assert secret not in report.summary_path.read_text(encoding='utf-8')
    assert secret not in report.json_path.read_text(encoding='utf-8')
    assert secret not in report.event_log_path.read_text(encoding='utf-8')


@pytest.mark.slow
def test_a_notification_http_status_reaches_the_report_without_its_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    secret = 'do-not-record-this'

    def broken(request: Any, *, timeout: float) -> Any:
        del request, timeout
        raise HTTPError(
            f'https://hooks.example/{secret}',
            503,
            f'Service Unavailable {secret}',
            HTTPMessage(),
            None,
        )

    notifier = WebhookSink('https://hooks.example/coins', opener=broken)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        notifier=notifier,
        notify_on_empty=True,
    )

    report = engine.run_once(context)

    assert report.notification is not None
    assert report.notification['error'] == 'NotificationError (HTTP 503)'
    assert '**Notification:** failed via webhook: NotificationError (HTTP 503)' in report.summary_path.read_text(
        encoding='utf-8',
    )
    assert secret not in report.summary_path.read_text(encoding='utf-8')
    assert secret not in report.json_path.read_text(encoding='utf-8')
    assert secret not in report.event_log_path.read_text(encoding='utf-8')


@pytest.mark.slow
def test_rotation_is_restored_before_the_notification_is_delivered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    hygiene = _Hygiene()
    restored_when_sent: list[int] = []
    notifier = _Notifier(on_send=lambda: restored_when_sent.append(hygiene.restored))
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        hygiene=hygiene,
        notifier=notifier,
        notify_on_empty=True,
    )

    report = engine.run_once(context)

    assert report.ok is True
    assert hygiene.restored == 1
    assert restored_when_sent == [1]


@pytest.mark.slow
def test_a_night_that_claims_nothing_notifies_only_when_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    quiet = _Notifier()
    quiet_context = _context(
        tmp_path / 'quiet',
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        notifier=quiet,
    )
    quiet_report = engine.run_once(quiet_context)

    assert quiet_report.ok is True
    assert quiet.messages == []
    assert quiet_report.notification is None

    noisy = _Notifier()
    noisy_context = _context(
        tmp_path / 'noisy',
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        notifier=noisy,
        notify_on_empty=True,
    )
    noisy_report = engine.run_once(noisy_context)

    assert noisy_report.ok is True
    assert len(noisy.messages) == 1
    assert noisy_report.notification == {'attempted': True, 'ok': True, 'sink': 'webhook', 'error': None}


@pytest.mark.slow
def test_a_completed_run_with_a_task_sends_one_apprise_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boost_frame: Snapshot,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    wanted = ('coins_quiz',)
    roster = replace(full_roster, entries=tuple(entry for entry in full_roster.entries if entry.key in wanted))
    frame = _boost_frame(*wanted)
    _surface(monkeypatch, roster, frame)
    notifier = _Notifier()
    notifier.name = 'apprise'
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        notifier=notifier,
    )
    context.config = replace(context.config, apprise_url='https://apprise.example')

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        if entry.key is not None:
            _context.state.record(entry.key)
        return EngineResult('completed', 'fake', 'done', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert report.status is RunStatus.COMPLETED
    assert report.ok is True
    assert len(notifier.messages) == 1
    assert notifier.messages[0].status == 'completed'
    assert 'tasks: completed=1, skipped=0, failed=0' in notifier.messages[0].body
    assert report.notification == {'attempted': True, 'ok': True, 'sink': 'apprise', 'error': None}


@pytest.mark.slow
def test_retention_prunes_old_runs_after_the_current_report_is_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, keep_runs=2)
    for name in ('20260101T000000Z', '20260102T000000Z'):
        (context.config.runs_dir / name).mkdir(parents=True)
    (context.config.runs_dir / 'manual').mkdir()
    (context.config.runs_dir / 'stray.txt').write_text('keep me', encoding='utf-8')

    report = engine.run_once(context)

    kept = sorted(path.name for path in context.config.runs_dir.iterdir())
    assert kept == ['20260102T000000Z', report.run_id, 'manual', 'stray.txt']
    assert report.summary_path.is_file()
    assert report.event_log_path.is_file()
    assert 'runs_not_pruned' in report.event_log_path.read_text(encoding='utf-8')


@pytest.mark.slow
def test_a_run_log_start_failure_does_not_stop_the_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )

    def broken_record(self: Any, payload: dict[str, Any]) -> None:
        raise OSError('log volume full')

    monkeypatch.setattr(RunEventLog, 'record', broken_record)

    report = engine.run_once(context)

    assert report.status is RunStatus.COMPLETED
    assert report.summary_path.is_file()


def test_an_artifact_write_failure_is_recorded_but_the_run_still_reads_the_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = RunReport(tmp_path / 'runs', 'run-1', budget_seconds=60.0, started_at=CAPTURED_AT)
    store = ArtifactStore(report.runs_dir, report.run_id)
    frame = _coin_frame()

    def broken_write(*args: Any, **kwargs: Any) -> Any:
        raise OSError('disk full')

    monkeypatch.setattr(store, 'write', broken_write)

    returned = engine._record_frame(report, store, frame, 'balance-before', _logger())

    assert returned is frame
    assert report.shots == {}
    assert report.artifact_errors == ['balance-before: OSError: disk full']


def test_a_failure_frame_is_skipped_when_one_is_already_recorded(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    report = RunReport(tmp_path, 'run-1', budget_seconds=60.0, started_at=CAPTURED_AT)
    report.record_shot('failure', report.run_dir / 'shots' / 'failure.png')

    engine._record_failure_frame(
        context,
        report,
        ArtifactStore(report.runs_dir, report.run_id),
        _logger(),
    )

    assert report.artifact_errors == []


def test_a_failure_frame_capture_error_is_reported_without_escaping(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )

    def broken_capture() -> Snapshot:
        raise OSError('no frame available')

    context.capture = broken_capture
    report = RunReport(tmp_path, 'run-1', budget_seconds=60.0, started_at=CAPTURED_AT)

    engine._record_failure_frame(
        context,
        report,
        ArtifactStore(report.runs_dir, report.run_id),
        _logger(),
    )

    assert report.artifact_errors == ['failure frame: OSError: no frame available']


def test_dismissals_are_collected_from_every_executor_payload_shape(tmp_path: Path) -> None:
    report = RunReport(tmp_path, 'run-1', budget_seconds=60.0, started_at=CAPTURED_AT)

    engine._record_dismissals(
        report,
        {
            'streak': {'dismiss': {'closed': ['launch_modal', 'bundle_badge']}},
            'dismissals': [{'closed': ['launch_modal']}, {'closed': 'not-a-list'}, 'ignored'],
            'nested': [{'dismiss': {'closed': ['promo']}}],
            'return': {'dismissals': [{'closed': ['return_modal']}]},
        },
    )

    assert report.popups_closed == {
        'launch_modal': 2,
        'bundle_badge': 1,
        'promo': 1,
        'return_modal': 1,
    }


def test_notification_content_and_selection_cover_failed_and_completed_runs(tmp_path: Path) -> None:
    failed = RunReport(tmp_path, 'failed', budget_seconds=60.0, started_at=CAPTURED_AT)
    failed.error = 'DeviceError: tablet absent'
    failed.finish(RunStatus.FAILED, detail='failed', budget_used_seconds=1.0)

    assert engine._should_notify(failed, notify_on_empty=False) is True
    assert 'error: DeviceError: tablet absent' in engine._notification(failed).body

    completed = RunReport(tmp_path, 'completed', budget_seconds=60.0, started_at=CAPTURED_AT)
    completed.detail = 'roster exhausted'
    completed.finish(RunStatus.COMPLETED, detail='roster exhausted', budget_used_seconds=1.0)
    assert 'detail: roster exhausted' in engine._notification(completed).body
    assert engine._should_notify(completed, notify_on_empty=False) is False
    assert engine._should_notify(completed, notify_on_empty=True) is True

    completed.record_task(
        title='Take the coins quiz',
        key='coins_quiz',
        category='quiz',
        outcome='completed',
        verification='result_reward',
        detail='paid',
        duration_seconds=1.0,
        completed=True,
        result={},
    )
    assert engine._should_notify(completed, notify_on_empty=True) is False


def test_a_report_rewrite_failure_after_notification_does_not_escape(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        notifier=_Notifier(),
    )
    report = RunReport(context.config.runs_dir, 'run-1', budget_seconds=60.0, started_at=CAPTURED_AT)
    report.finish(RunStatus.FAILED, detail='boom', budget_used_seconds=1.0)

    def broken_write() -> Path:
        raise OSError('runs volume gone')

    monkeypatch.setattr(report, 'write', broken_write)

    engine._notify(context, report, _logger())

    assert report.notification == {'attempted': True, 'ok': True, 'sink': 'webhook', 'error': None}


def test_a_retention_failure_does_not_escape(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    report = RunReport(context.config.runs_dir, 'run-1', budget_seconds=60.0, started_at=CAPTURED_AT)
    monkeypatch.setattr(
        engine,
        'prune_runs',
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError('cannot list runs')),
    )

    engine._prune(context, report, _logger())

    assert report.status is RunStatus.RUNNING


def test_a_network_pull_mid_walk_is_a_named_failure_and_a_clean_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_frame('coins_quiz')
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    _surface(monkeypatch, roster, frame)
    clock = _Clock()
    flaky = _FlakyCapture(frame, fail_after=1)
    wrapped = engine._retrying_capture(flaky, sleep=clock.sleep, logger=_logger())
    notifier = _Notifier()
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        clock=clock,
        notifier=notifier,
    )
    context.capture = cast(Any, wrapped)
    monkeypatch.setattr(
        engine,
        'return_to_coins_page',
        lambda *args, **kwargs: ReturnResult(
            returned=True,
            reentered=False,
            attempts=1,
            route=SurfaceRoute.SHORTCUT,
            elapsed=0.1,
            snapshot=_coin_frame(),
        ),
    )

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        del entry
        engine._capture(_context.capture)  # the real seam, mid-task
        raise AssertionError('the capture should have failed')

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert report.status is RunStatus.FAILED
    assert report.failure_code == FailureCode.DEVICE_OFFLINE.value
    assert report.tasks == []
    assert flaky.calls >= 3
    assert clock.value >= 1.0  # the injected clock was advanced, no real wait
    assert isinstance(context.hygiene, _Hygiene)
    assert context.hygiene.restored == 1
    assert len(notifier.messages) == 1
    assert 'device_offline' in notifier.messages[0].body
    assert 'device_offline' in report.summary_path.read_text(encoding='utf-8')
    assert report.event_log_path.is_file()


@pytest.mark.slow
def test_a_sleeping_device_wakes_and_the_run_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    runner = ScriptedAdbRunner(
        _adb_ok('device'),
        _adb_ok('mWakefulness=Asleep'),
        _adb_ok('Physical size: 2000x1200'),
        _adb_ok('1'),
        _adb_ok('0'),
        _adb_ok(),
        _adb_ok(),
        _adb_ok(),
        _adb_ok('mWakefulness=Awake'),
        _adb_ok(),
        _adb_ok(),
        _adb_ok('isKeyguardShowing=false'),
        _adb_ok('15'),
        _adb_ok(),
        _adb_ok('package:/data/app/base.apk'),
        _adb_ok('Starting: Intent'),
        _adb_ok(),
        _adb_ok(),
        _adb_ok(),
    )
    sleeps: list[float] = []
    hygiene = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0, settle=sleeps.append)
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        hygiene=hygiene,
    )

    report = engine.run_once(context)

    assert report.status is RunStatus.COMPLETED
    assert report.hygiene is not None
    assert report.hygiene['was_asleep'] is True
    assert report.hygiene['kept_awake'] is True
    assert ('adb', '-s', 'usb-serial', 'shell', 'input', 'keyevent', 'KEYCODE_WAKEUP') in runner.calls
    assert any('swipe' in call for call in runner.calls)
    assert sleeps == [0.5]
    assert runner.calls[-3:] == [
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'put', 'global', 'stay_on_while_plugged_in', '15'),
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'put', 'system', 'accelerometer_rotation', '1'),
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'put', 'system', 'user_rotation', '0'),
    ]


@pytest.mark.parametrize(
    ('error', 'code', 'prepared', 'pauses'),
    [
        (DeviceUnavailable('device offline'), FailureCode.DEVICE_OFFLINE, 2, 1.0),
        (DeviceSleeping('did not wake'), FailureCode.DEVICE_SLEEPING, 2, 1.0),
        (DeviceLocked('PIN set'), FailureCode.DEVICE_LOCKED, 1, 0.0),
        (AppNotInstalled('package missing'), FailureCode.APP_NOT_INSTALLED, 1, 0.0),
        (AppLaunchFailed('launch failed'), FailureCode.APP_LAUNCH_FAILED, 2, 1.0),
        (RuntimeError('bug'), FailureCode.INTERNAL_ERROR, 1, 0.0),
    ],
)
@pytest.mark.slow
def test_device_level_failures_follow_the_retry_table(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    error: Exception,
    code: FailureCode,
    prepared: int,
    pauses: float,
) -> None:
    clock = _Clock()
    hygiene = _Hygiene(error)
    context = _context(
        tmp_path,
        capture=_Capture(_boost_header_frame()),
        catalogue=catalogue,
        corpus=corpus,
        clock=clock,
        hygiene=hygiene,
    )

    report = engine.run_once(context)

    assert report.status is RunStatus.FAILED
    assert report.failure_code == code.value
    assert hygiene.prepared == prepared
    assert clock.value >= pauses
    assert code.value in report.summary_path.read_text(encoding='utf-8')


@pytest.mark.slow
def test_a_sleeping_hygiene_pass_is_retried_once_and_then_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    clock = _Clock()
    hygiene = _Hygiene(errors=[DeviceSleeping('first wake failed'), None])
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        clock=clock,
        hygiene=hygiene,
    )

    report = engine.run_once(context)

    assert report.status is RunStatus.COMPLETED
    assert report.failure_code is None
    assert hygiene.prepared == 2
    assert clock.value == 1.0


def test_a_logged_out_device_stops_at_the_first_detection(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    notifier = _Notifier()
    hygiene = _Hygiene()
    context = _context(
        tmp_path,
        capture=_Capture(_logged_out_frame()),
        catalogue=catalogue,
        corpus=corpus,
        hygiene=hygiene,
        notifier=notifier,
    )

    report = engine.run_once(context)

    assert report.status is RunStatus.FAILED
    assert report.failure_code == FailureCode.APP_LOGGED_OUT.value
    assert report.tasks == []
    assert report.skipped == []
    assert report.returned_to_coins_page is None
    assert report.error is not None
    assert 'sign-in prompt' in report.error
    assert hygiene.prepared == 1
    assert hygiene.restored == 1
    summary = report.summary_path.read_text(encoding='utf-8')
    assert '**Failure:** app_logged_out' in summary
    assert 'a human must sign in' in summary
    assert context.state.snapshot().completed_night is False
    assert len(notifier.messages) == 1
    assert 'app_logged_out' in notifier.messages[0].body
    assert 'app_logged_out' in notifier.messages[0].tags


@pytest.mark.slow
def test_an_unverified_task_is_retried_once_and_the_second_result_stands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    frame = _boost_frame('coins_quiz')
    _surface(monkeypatch, roster, frame)
    clock = _Clock()
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        clock=clock,
    )
    attempts: list[int] = []

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        attempts.append(1)
        if len(attempts) == 1:
            return EngineResult(
                'failed',
                'no_change',
                'the row did not move',
                False,
                {},
                failure=failure(FailureCode.TASK_UNVERIFIED, detail='the row did not move'),
            )
        _context.state.record(entry.key or '')
        return EngineResult('completed', 'progress_badge', 'the row moved', True, {})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert len(attempts) == 2
    assert clock.value == 1.0
    assert report.tasks[0].completed is True
    assert report.tasks[0].code is None
    assert report.status is RunStatus.COMPLETED


@pytest.mark.slow
def test_an_unverified_task_that_stays_unverified_is_reported_and_the_walk_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    frame = _boost_frame('coins_quiz')
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    attempts: list[int] = []

    def dispatch(*args: Any, **kwargs: Any) -> EngineResult:
        attempts.append(1)
        return EngineResult(
            'failed',
            'no_change',
            'the row did not move',
            False,
            {},
            failure=failure(FailureCode.TASK_UNVERIFIED, detail='the row did not move'),
        )

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert len(attempts) == 2
    assert report.status is RunStatus.COMPLETED
    assert report.ok is True
    assert report.tasks[0].code == FailureCode.TASK_UNVERIFIED.value
    assert 'task_unverified' in report.summary_path.read_text(encoding='utf-8')


@pytest.mark.slow
def test_an_unclosable_popup_aborts_the_run_without_a_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    frame = _boost_frame('coins_quiz')
    _surface(monkeypatch, roster, frame)
    clock = _Clock()
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        clock=clock,
    )
    attempts: list[int] = []

    def dispatch(*args: Any, **kwargs: Any) -> EngineResult:
        attempts.append(1)
        raise PopupUnclosable('no executable close strategy')

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert report.status is RunStatus.FAILED
    assert report.failure_code == FailureCode.POPUP_UNCLOSABLE.value
    assert len(attempts) == 1
    assert clock.value == 0.0
    assert report.tasks == []


@pytest.mark.slow
def test_a_route_timeout_uses_the_route_policy_without_another_route_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    clock = _Clock()
    frame = _boost_header_frame()
    context = _context(
        tmp_path,
        capture=_Capture(frame),
        catalogue=catalogue,
        corpus=corpus,
        clock=clock,
    )
    calls: list[int] = []

    def discover(*args: Any, **kwargs: Any) -> Roster:
        calls.append(1)
        raise StateTimeout('the boost page did not appear')

    monkeypatch.setattr(engine, 'discover_roster', discover)
    monkeypatch.setattr(
        engine,
        'open_boost_list',
        lambda *args, **kwargs: BoostList(snapshot=frame, route=DiscoveryRoute.BOOST, detail='test'),
    )

    report = engine.run_once(context)

    assert len(calls) == 1
    assert report.route_error is not None
    assert 'StateTimeout' in report.route_error
    assert report.status is RunStatus.COMPLETED


def test_an_empty_unknown_surface_is_checked_for_a_sign_in_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    roster = replace(full_roster, entries=(), surface_variant=Surface.UNKNOWN)
    frame = _boost_header_frame()
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(_home_frame(), _logged_out_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    monkeypatch.setattr(
        engine,
        'return_to_coins_page',
        lambda *args, **kwargs: ReturnResult(
            returned=True,
            reentered=False,
            attempts=1,
            route=SurfaceRoute.SHORTCUT,
            elapsed=0.1,
            snapshot=_coin_frame(),
        ),
    )

    report = engine.run_once(context)

    assert report.failure_code == FailureCode.APP_LOGGED_OUT.value
    assert report.tasks == []


@pytest.mark.parametrize('error', [PopupUnclosable('no close'), AppLoggedOut('signed out')])
def test_route_level_abort_classes_are_not_swallowed_by_the_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    error: Exception,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_boost_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    assert context.controller is not None
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(type(error), match=str(error)):
        engine._discover_or_fallback(context, context.controller)


def test_a_task_navigation_timeout_is_a_task_failure_not_a_run_abort() -> None:
    named = engine._task_exception(StateTimeout('page did not open'))

    assert named.code is FailureCode.TASK_UNVERIFIED
    assert named.abort_run is False


def test_the_capture_retry_asks_adb_for_the_connection_before_giving_up() -> None:
    class _Failing:
        def capture(self) -> Snapshot:
            raise SnapshotError('adb exec-out screencap: failed')

    checks: list[int] = []

    def check_connected() -> None:
        checks.append(1)
        raise DeviceUnavailable('the serial is gone')

    capture = engine._retrying_capture(_Failing(), sleep=lambda _: None, check_connected=check_connected)

    with pytest.raises(DeviceUnavailable, match='serial is gone'):
        capture()

    assert checks == [1]


def test_a_sign_out_discovered_during_the_balance_proof_is_re_raised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    assert context.controller is not None
    report = RunReport(tmp_path / 'runs', 'run-1', budget_seconds=60.0, started_at=CAPTURED_AT)
    monkeypatch.setattr(
        engine,
        'return_to_coins_page',
        lambda *args, **kwargs: (_ for _ in ()).throw(AppLoggedOut('signed out')),
    )

    with pytest.raises(AppLoggedOut, match='signed out'):
        engine._record_balance_before(
            context,
            context.controller,
            report,
            ArtifactStore(report.runs_dir, report.run_id),
            _coin_frame(),
            _logger(),
        )


@pytest.mark.slow
def test_an_unverified_sign_in_is_dispatched_once_and_taps_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'boost_checkin'),))
    frame = _boost_frame('daily_checkin')
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    claims = _UnverifiedSignin()
    calls: list[int] = []

    def claim(*args: Any, **kwargs: Any) -> _UnverifiedSignin:
        calls.append(1)
        return claims

    monkeypatch.setattr(engine, 'claim_daily_signins', claim)

    report = engine.run_once(context)

    assert len(calls) == 1
    assert report.tasks[0].code == FailureCode.TASK_UNVERIFIED.value
    assert report.tasks[0].result['acted'] is True
    assert report.status is RunStatus.COMPLETED


class _UnverifiedClaim:
    key = 'daily_checkin'
    outcome = SigninOutcome.UNVERIFIED
    verification = 'no_evidence'
    detail = 'the control was tapped once but no fresh frame proved the claim'
    tapped = True

    @property
    def already_done(self) -> bool:
        return False

    @property
    def unverified(self) -> bool:
        return True

    @property
    def unavailable(self) -> bool:
        return False


class _UnverifiedSignin:
    results: tuple[_UnverifiedClaim, ...] = (_UnverifiedClaim(),)
    completed: tuple[_UnverifiedClaim, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {'streak': {'outcome': 'unverified', 'tapped': True}, 'boost': None}


@pytest.mark.slow
def test_a_sign_out_during_the_walk_stops_the_next_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    frame = _boost_frame('coins_quiz')
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _logged_out_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    attempts: list[int] = []

    def dispatch(*args: Any, **kwargs: Any) -> EngineResult:
        attempts.append(1)
        return EngineResult('failed', 'no_change', 'the row did not move', False, {})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert len(attempts) == 1
    assert report.failure_code == FailureCode.APP_LOGGED_OUT.value
    assert report.tasks == []


def test_an_undecodable_frame_is_named_and_not_retried(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    from aliexpress_coins.perception.errors import FrameDecodeError

    clock = _Clock()

    class _BadFrame:
        def capture(self) -> Snapshot:
            raise FrameDecodeError('screencap returned an undecodable PNG')

    context = _context(
        tmp_path,
        capture=_Capture(_boost_frame()),
        catalogue=catalogue,
        corpus=corpus,
        clock=clock,
    )
    context.capture = cast(Any, _BadFrame())

    report = engine.run_once(context)

    assert report.status is RunStatus.FAILED
    assert report.failure_code == FailureCode.FRAME_UNDECODABLE.value
    assert clock.value == 0.0


def test_a_decode_failure_is_not_retried_by_the_capture_wrapper() -> None:
    from aliexpress_coins.perception.errors import FrameDecodeError

    class _BadFrame:
        def capture(self) -> Snapshot:
            raise FrameDecodeError('not a PNG')

    sleeps: list[float] = []
    capture = engine._retrying_capture(_BadFrame(), sleep=sleeps.append)

    with pytest.raises(FrameDecodeError):
        capture()

    assert sleeps == []


def test_a_decode_failure_after_the_retry_is_also_not_retried() -> None:
    from aliexpress_coins.perception.errors import FrameDecodeError

    class _BlipThenBadFrame:
        def __init__(self) -> None:
            self.calls = 0

        def capture(self) -> Snapshot:
            self.calls += 1
            if self.calls == 1:
                raise SnapshotError('adb exec-out screencap: failed')
            raise FrameDecodeError('not a PNG')

    sleeps: list[float] = []
    capture = engine._retrying_capture(_BlipThenBadFrame(), sleep=sleeps.append)

    with pytest.raises(FrameDecodeError):
        capture()

    assert sleeps == [1.0]


def test_acted_evidence_includes_keys_and_taps() -> None:
    assert engine._payload_acted({'keys': [{'key': 'back'}]}) is True
    assert engine._payload_acted({'swipes': [{'from': [1, 2]}]}) is True
    assert engine._payload_acted({'collect': {'tap': 1}}) is True
    assert engine._payload_acted({'taps': [{'tap': 1}]}) is True
    assert engine._payload_acted({'presses': 1}) is True
    assert engine._payload_acted({}) is False


def test_observation_stops_the_run_when_the_opened_surface_is_signed_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(tmp_path, capture=_Capture(_boost_frame()), catalogue=catalogue, corpus=corpus)
    assert context.controller is not None
    observer = engine._observer(
        context,
        RunReport(tmp_path, 'observe-logged-out', budget_seconds=60.0, started_at=CAPTURED_AT),
        full_roster,
    )
    monkeypatch.setattr(
        engine,
        'open_boost_list',
        lambda *args, **kwargs: BoostList(
            snapshot=_logged_out_frame(),
            route=DiscoveryRoute.INLINE,
            detail='signed out',
        ),
    )

    with pytest.raises(AppLoggedOut):
        engine._observe(context, context.controller, observer, route=None)


def test_observation_stops_the_run_when_the_fallback_frame_is_signed_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(tmp_path, capture=_Capture(_logged_out_frame()), catalogue=catalogue, corpus=corpus)
    assert context.controller is not None
    observer = engine._observer(
        context,
        RunReport(tmp_path, 'observe-fallback-logged-out', budget_seconds=60.0, started_at=CAPTURED_AT),
        full_roster,
    )
    monkeypatch.setattr(
        engine,
        'open_boost_list',
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('open failed')),
    )

    with pytest.raises(AppLoggedOut):
        engine._observe(context, context.controller, observer, route=None)


def test_the_notification_carries_a_detail_only_run_and_no_invented_error(tmp_path: Path) -> None:
    report = RunReport(tmp_path, 'quiet-detail', budget_seconds=60.0, started_at=CAPTURED_AT)
    report.detail = 'roster exhausted'
    report.finish(RunStatus.COMPLETED, detail='roster exhausted', budget_used_seconds=1.0, finished_at=CAPTURED_AT)

    body = engine._notification(report).body

    assert 'detail: roster exhausted' in body
    assert 'error:' not in body
