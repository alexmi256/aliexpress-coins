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
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from aliexpress_coins.control.controller import Controller
from aliexpress_coins.control.popups import DismissReport, DismissStop, PopupCorpus, load_popup_corpus
from aliexpress_coins.perception.artifacts import ArtifactStore
from aliexpress_coins.perception.errors import StateTimeout
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.runner import engine
from aliexpress_coins.runner.config import RunConfig
from aliexpress_coins.runner.device import HygieneReport
from aliexpress_coins.runner.engine import EngineResult, RunContext
from aliexpress_coins.runner.errors import DeviceError, ReportError, RunInterrupted
from aliexpress_coins.runner.notify import Notification, NotificationSink, WebhookSink
from aliexpress_coins.runner.report import RunEventLog, RunReport, RunStatus
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks.browse import ReturnResult
from aliexpress_coins.tasks.catalogue import TaskCatalogue, TaskCategory, load_task_catalogue
from aliexpress_coins.tasks.discovery import (
    BoostList,
    DiscoveryRoute,
    Roster,
    RosterEntry,
    enumerate_roster,
)
from aliexpress_coins.tasks.signin import ClaimResult, SigninOutcome, SigninReport, read_coin_balance
from aliexpress_coins.tasks.surface import SHORTCUT_DEEP_LINK, Surface, SurfaceRoute

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
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.prepared = 0
        self.restored = 0

    def prepare(self) -> HygieneReport:
        self.prepared += 1
        if self.error is not None:
            raise self.error
        return HygieneReport(
            serial='192.168.8.65:5555',
            state='device',
            awake=True,
            app_started=True,
            detail='ready',
        )

    def navigate(self, route: SurfaceRoute) -> None:
        raise AssertionError(f'unexpected hygiene navigation {route}')

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


def _home_frame() -> Snapshot:
    return _snapshot(_box('For you', 100, 200, 400, 260), _box('Search', 100, 300, 300, 360))


def _dismiss(*families: str) -> DismissReport:
    return DismissReport(
        closed=families,
        attempts=(),
        frames=(),
        remaining=False,
        stop=DismissStop.CLEAR,
    )


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
            return entry
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
    hygiene: _Hygiene | None = None,
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
    assert _report_payload(tmp_path)['status'] == 'budget'


def test_policy_and_missing_executor_are_reported_as_skips(
    tmp_path: Path,
    full_roster: Roster,
) -> None:
    report = RunReport(tmp_path, 'run-1', budget_seconds=60.0, started_at=CAPTURED_AT)

    engine._record_policy_skips(report, full_roster)

    assert any(skip.title == 'Play & Earn' and skip.reason == 'policy' for skip in report.skipped)
    assert any(skip.title == 'Add a coins shortcut' and skip.reason == 'no executor' for skip in report.skipped)


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
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)

    report = engine.run_once(context)

    assert report.status is RunStatus.COMPLETED
    assert context.state.snapshot().completed_night is False


def test_an_unexpected_frame_geometry_is_refused_and_reported(
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

    assert report.status is RunStatus.FAILED
    assert report.error is not None
    assert 'unsupported screen geometry' in report.error
    assert '1080x1920' in report.error
    assert report.geometry is not None
    assert report.geometry['width'] == 1080
    assert report.geometry['height'] == 1920
    assert context.state.snapshot().completed_night is False
    assert hygiene.restored == 1
    payload = _report_payload(tmp_path)
    assert payload['status'] == 'failed'
    assert 'unsupported screen geometry' in payload['error']


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

    result = engine._run_signin(context, context.controller, engine._spec(entry), common)

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

    result = engine._run_signin(context, context.controller, engine._spec(entry), common)

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

    result = engine._run_signin(context, context.controller, engine._spec(entry), common)

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
        'error': 'RuntimeError: webhook down',
    }
    events = [
        json.loads(line)['event']
        for line in report.event_log_path.read_text(encoding='utf-8').splitlines()
        if line.strip()
    ]
    assert events.index('run_finished') < events.index('notification_failed')
    payload = _report_payload(tmp_path)
    assert payload['notification']['ok'] is False
    assert '**Notification:** failed via webhook: RuntimeError: webhook down' in report.summary_path.read_text(
        encoding='utf-8',
    )


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

    def broken(request: Any, *, timeout: float) -> Any:
        del request, timeout
        raise RuntimeError(f"nonnumeric port: '{secret}@example.invalid'")

    notifier = WebhookSink(f'https://alice:{secret}@example.invalid/hook', opener=broken)
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
    assert report.notification['error'] == 'NotificationError: webhook delivery failed: RuntimeError'
    assert secret not in caplog.text
    assert secret not in report.summary_path.read_text(encoding='utf-8')
    assert secret not in report.json_path.read_text(encoding='utf-8')
    assert secret not in report.event_log_path.read_text(encoding='utf-8')


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
        },
    )

    assert report.popups_closed == {'launch_modal': 2, 'bundle_badge': 1, 'promo': 1}


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
