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
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import partial
from http.client import HTTPMessage
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError

import pytest
from PIL import Image

from aliexpress_coins.control.controller import Controller, SwipeRecord, TapRecord
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
    RunBudgetExceeded,
    RunInterrupted,
)
from aliexpress_coins.runner.failures import FailureCode, failure
from aliexpress_coins.runner.notify import AppriseSink, Notification, NotificationSink, WebhookSink
from aliexpress_coins.runner.report import RunEventLog, RunReport, RunStatus
from aliexpress_coins.runner.safety import AbortCode, RunAborted, SafetyGuard, ScreenReport
from aliexpress_coins.runner.state import DailyStateStore, NightOutcome
from aliexpress_coins.tasks import notifications as notifications_module
from aliexpress_coins.tasks.browse import ReturnResult
from aliexpress_coins.tasks.catalogue import TaskCatalogue, TaskCategory, load_task_catalogue
from aliexpress_coins.tasks.discovery import (
    BoostList,
    DiscoveryRoute,
    Roster,
    RosterEntry,
    RosterObservation,
    RosterObserver,
    collect_roster,
    enumerate_roster,
    read_roster_log,
)
from aliexpress_coins.tasks.dispatch import DispatchGate
from aliexpress_coins.tasks.signin import ClaimResult, SigninOutcome, SigninReport, read_coin_balance
from aliexpress_coins.tasks.surface import (
    EMPTY_BOOST_MARKER,
    SHORTCUT_DEEP_LINK,
    Surface,
    SurfaceRoute,
    classify_surface,
)
from tests.support.adb import ScriptedAdbRunner, ok

ROOT = Path(__file__).resolve().parents[1]
CAPTURED_AT = datetime(2026, 11, 22, 3, 30, tzinfo=UTC)


class _RecordingDevice:
    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []
        self.swipes: list[tuple[int, int, int, int, int]] = []

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        self.swipes.append((x1, y1, x2, y2, duration_ms))


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


class _SwipeSwitchCapture:
    """Hold the first frame until a roster swipe asks for the next one."""

    def __init__(self, first: Snapshot, second: Snapshot) -> None:
        self.frames = (first, second)
        self.first = first
        self.second = second
        self.swiped = False

    def capture(self) -> Snapshot:
        return self.second if self.swiped else self.first


class _SwipeSwitchController:
    """The one controller method roster collection needs, with a capture switch."""

    def __init__(self, capture: _SwipeSwitchCapture) -> None:
        self.capture = capture

    def swipe_for_observation(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        *,
        why: str,
        label: str | None = None,
        duration_ms: int = 300,
    ) -> SwipeRecord:
        del start, end, why, label, duration_ms
        self.capture.swiped = True
        return SwipeRecord(
            index=1,
            label='roster scroll',
            reason='test scroll',
            requested=((0, 0), (1, 1)),
            clamped=((0, 0), (1, 1)),
            duration_ms=300,
            suppressed=False,
        )


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


def _all_claimed_boost_frame() -> Snapshot:
    """The measured 2026-11-26 sheet: three cards, every control a checkmark.

    The label copy and box bands come from ``notifications_row_completed.png``;
    no ``GO`` is present because every row is already claimed (#150).
    """
    return _snapshot(
        _box('Coins boost', 52, 467, 420, 543),
        _box('Check in to win Shopping', 247, 694, 748, 751),
        _box('credits', 247, 753, 393, 798),
        _box("Don't forget to check in today!", 248, 807, 792, 857),
        _box('Add & start using our widget', 247, 1082, 805, 1135),
        _box('Add our widget to your', 246, 1138, 670, 1190),
        _box('homescreen to use', 249, 1195, 600, 1235),
        _box('+1', 327, 1283, 372, 1322),
        _box('Turn on notifications', 249, 1467, 657, 1513),
        _box('Stay informed when rewards', 248, 1522, 773, 1571),
        _box('drop', 243, 1571, 346, 1630),
        _box('+1', 327, 1664, 375, 1706),
    )


def _mixed_boost_frame() -> Snapshot:
    """A readable sheet with one extra title-only partial card below the task rows."""
    base = _boost_frame('coins_quiz', 'sponsored_items')
    return replace(
        base,
        boxes=(*base.boxes, _box('Turn on notifications', 100, 1000, 700, 1040)),
    )


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


def _incomplete_coin_frame() -> Snapshot:
    """One readable task plus a GO whose title the splitter cannot enumerate."""
    return _snapshot(
        _box('Coins', 100, 220, 300, 280),
        _box('Coins credits to redeem', 100, 330, 700, 380),
        _box('Daily coins', 100, 560, 500, 610),
        _box('Take the coins quiz', 180, 1269, 700, 1305),
        _box('GO', 982, 1273, 1040, 1310),
        _box('GO', 982, 1473, 1040, 1510),
    )


def _home_frame() -> Snapshot:
    return _snapshot(_box('For you', 100, 200, 400, 260), _box('Search', 100, 300, 300, 360))


def _non_app_clock_frame() -> Snapshot:
    return _snapshot(
        _box('3:36', 20, 5, 100, 35),
        _box('0', 1120, 5, 1160, 35),
        _box('日', 500, 700, 560, 760),
        _box('24', 580, 700, 640, 760),
    )


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


def _coin_store_frame() -> Snapshot:
    return _snapshot(_box('Use coins for discounts', 100, 100, 700, 160))


def _rendering_boost_frame() -> Snapshot:
    """The #120 probe shape: boost header and title, but no rendered row controls.

    The measured skeleton pixels keep this a changing frame for the stability
    wait; the OCR boxes name the boost page before its rows have finished.
    """
    return replace(
        _skeleton_frame(),
        boxes=(
            _box('Coins boost', 100, 100, 500, 150),
            _box('Take the coins quiz', 180, 1269, 700, 1305),
        ),
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
    scroll_rosters: bool = False,
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
        scroll_rosters=scroll_rosters,
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


def _seed_other_families(
    context: RunContext,
    catalogue: TaskCatalogue,
    roster: Roster,
    *,
    keep: set[str] | None = None,
) -> None:
    """Record daily limits for catalogue families a fixture deliberately omits.

    A clean completion now also requires that no unseen family is still
    claimable. These tests are about other walk behaviour, so mark omitted
    families as already earned today. ``keep`` names keys that must stay
    unclaimed even when they are not in the fixture roster.
    """
    present = {entry.key for entry in roster.allowed_entries if entry.key is not None}
    keep = keep or set()
    for spec in catalogue.allowed:
        if spec.key in present or spec.key in keep:
            continue
        for _ in range(spec.max_per_run):
            context.state.record(spec.key)


def _surface(
    monkeypatch: pytest.MonkeyPatch,
    roster: Roster,
    snapshot: Snapshot,
) -> None:
    if roster.empty and roster.surface_variant is not Surface.UNKNOWN:
        # An empty fixture is a readable empty day, not an unreadable surface.
        # #123 refuses to complete on a stale/failed observation, so use the
        # coin page's genuine zero-row surface for this convenience fixture.
        snapshot = _coin_frame()
    roster = replace(roster, surface_variant=classify_surface(snapshot))
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


def _events(report: RunReport) -> list[dict[str, Any]]:
    return [
        cast(dict[str, Any], json.loads(line))
        for line in report.event_log_path.read_text(encoding='utf-8').splitlines()
        if line.strip()
    ]


def test_the_dispatch_table_covers_every_implemented_category(catalogue: TaskCatalogue) -> None:
    assert set(engine.DISPATCH_TABLE) == {
        TaskCategory.SIGN_IN,
        TaskCategory.QUIZ,
        TaskCategory.BROWSE,
        TaskCategory.BROWSE_WAIT,
        TaskCategory.ITEM_TAP,
        TaskCategory.SHORTCUT_VISIT,
        TaskCategory.NOTIFICATION,
    }
    assert TaskCategory.SHORTCUT not in engine.DISPATCH_TABLE
    assert TaskCategory.WIDGET not in engine.DISPATCH_TABLE
    assert engine._CATEGORY_COST == {
        TaskCategory.SIGN_IN: 0,
        TaskCategory.QUIZ: 1,
        TaskCategory.BROWSE: 2,
        TaskCategory.ITEM_TAP: 3,
        TaskCategory.BROWSE_WAIT: 4,
        TaskCategory.NOTIFICATION: 5,
        TaskCategory.SHORTCUT_VISIT: 6,
    }
    assert catalogue.allowed


def test_every_dispatchable_category_has_exactly_one_cost_rank() -> None:
    assert set(engine._CATEGORY_COST) == engine._EXECUTORS
    assert set(engine.DISPATCH_TABLE) == engine._EXECUTORS


def test_a_dry_run_uses_a_factory_controller_to_collect_a_scrolled_roster(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_frame('coins_quiz')
    roster = replace(
        enumerate_roster(frame, catalogue),
        surface_variant=Surface.BOOST_PAGE,
        scroll_count=1,
        scroll_attempts=2,
    )
    context = _context(
        tmp_path,
        capture=_Capture(frame),
        catalogue=catalogue,
        corpus=corpus,
        scroll_rosters=True,
    )
    context.config = replace(context.config, dry_run=True)
    context.controller = None
    context.controller_factory = lambda snapshot: Controller.from_frame(
        _RecordingDevice(),
        snapshot,
        rng=random.Random(0),
    )
    monkeypatch.setattr(engine, 'collect_roster', lambda *args, **kwargs: roster)

    report = engine.run_once(context)

    assert report.status is RunStatus.PLANNED
    assert report.plan is not None
    assert [step['key'] for step in report.plan['steps']] == ['coins_quiz']
    assert report.roster is not None
    assert report.roster['scroll_count'] == 1


def test_a_dry_run_fails_when_real_multi_frame_collection_hits_the_scroll_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    first = replace(
        _snapshot(
            _box('Coins boost', 100, 100, 500, 150),
            _box('Place an order on this page', 100, 600, 700, 640),
            _box('GO', 980, 630, 1040, 670),
        ),
        image=Image.new('RGB', (1200, 2000), 'white'),
    )
    second = replace(
        _snapshot(
            _box('Coins boost', 100, 100, 500, 150),
            _box('Visit the page via coins', 100, 600, 700, 640),
            _box('shortcut', 100, 645, 300, 675),
            _box('Unlock a daily surprise', 100, 680, 700, 710),
            _box('GO', 980, 630, 1040, 670),
        ),
        image=Image.new('RGB', (1200, 2000), '#e0e0e0'),
    )
    capture = _SwipeSwitchCapture(first, second)
    controller = _SwipeSwitchController(capture)
    context = _context(
        tmp_path,
        capture=cast(Any, capture),
        catalogue=catalogue,
        corpus=corpus,
        scroll_rosters=True,
    )
    context.config = replace(context.config, dry_run=True)
    context.capture = cast(Any, capture)
    context.controller = cast(Any, controller)
    monkeypatch.setattr(engine, 'collect_roster', partial(collect_roster, max_scrolls=1))

    report = engine.run_once(context)

    assert report.status is RunStatus.FAILED
    assert report.detail is not None
    assert 'surface_incomplete' in report.detail
    assert report.plan is not None
    assert report.plan['probe_failed'] is True


def test_a_pass_restart_names_an_unknown_or_empty_boost_surface() -> None:
    observation = RosterObservation(
        sequence=1,
        timestamp=CAPTURED_AT,
        rows=(),
    )

    unknown = Roster(entries=(), surface_variant=Surface.UNKNOWN)
    empty_boost = Roster(entries=(), surface_variant=Surface.EMPTY_BOOST)

    assert engine._pass_restart_reason(unknown, observation) == 'the task surface is unknown'
    assert engine._pass_restart_reason(empty_boost, observation) == 'the Coins boost page is the documented load fault'


def test_an_observer_keeps_scrolling_after_a_no_op_first_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_frame('coins_quiz')
    previous = replace(
        enumerate_roster(frame, catalogue),
        surface_variant=Surface.BOOST_PAGE,
        scroll_count=0,
        scroll_attempts=1,
    )
    collected = replace(previous, scroll_count=0, scroll_attempts=1)
    context = _context(
        tmp_path,
        capture=_Capture(frame),
        catalogue=catalogue,
        corpus=corpus,
        scroll_rosters=True,
    )
    observer = RosterObserver(catalogue, previous=previous, log=None)
    calls: list[str] = []
    monkeypatch.setattr(
        engine,
        'open_boost_list',
        lambda *args, **kwargs: BoostList(snapshot=frame, route=DiscoveryRoute.BOOST, detail='test'),
    )

    def collect(*_args: Any, **_kwargs: Any) -> Roster:
        calls.append('collect')
        return collected

    monkeypatch.setattr(engine, 'collect_roster', collect)

    observation = engine._observe(context, cast(Controller, context.controller), observer, route=None)

    assert calls == ['collect']
    assert observation is not None
    assert observation.sequence == 1


def test_an_observer_restores_an_already_open_sheet_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    """Issue #137: the post-discovery read must ask for the top of the sheet."""
    frame = _boost_frame('coins_quiz')
    previous = replace(
        enumerate_roster(frame, catalogue),
        surface_variant=Surface.BOOST_PAGE,
        scroll_count=1,
        scroll_attempts=1,
    )
    collected = replace(previous, scroll_count=0, scroll_attempts=1)
    context = _context(
        tmp_path,
        capture=_Capture(frame),
        catalogue=catalogue,
        corpus=corpus,
        scroll_rosters=True,
    )
    observer = RosterObserver(catalogue, previous=previous, log=None)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        engine,
        'open_boost_list',
        lambda *args, **kwargs: BoostList(snapshot=frame, route=DiscoveryRoute.BOOST, detail='already open'),
    )

    def collect(*_args: Any, **kwargs: Any) -> Roster:
        seen.update(kwargs)
        return collected

    monkeypatch.setattr(engine, 'collect_roster', collect)

    observation = engine._observe(context, cast(Controller, context.controller), observer, route=None)

    assert observation is not None
    assert seen['start_at_top'] is True


def test_dispatch_entry_routes_the_shortcut_visit_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _snapshot(
        _box('Coins boost', 100, 100, 500, 150),
        _box('Visit the page via coins', 100, 600, 700, 640),
        _box('shortcut', 100, 645, 300, 675),
        _box('Unlock a daily surprise', 100, 680, 700, 710),
        _box('GO', 980, 630, 1040, 670),
    )
    entry = DispatchGate(catalogue).authorize(enumerate_roster(frame, catalogue).entries[0])
    context = _context(tmp_path, capture=_Capture(frame), catalogue=catalogue, corpus=corpus)
    called: list[str] = []
    seen: dict[str, object] = {}

    def executor(*_args: Any, **kwargs: Any) -> _FakeResult:
        called.append('shortcut')
        seen.clear()
        seen.update(kwargs)
        return _FakeResult(completed=True)

    monkeypatch.setattr(engine, 'run_shortcut_visit_task', executor)

    result = engine._dispatch_entry(context, cast(Controller, context.controller), entry)

    assert result.completed is True
    assert called == ['shortcut']
    assert 'shortcut_present' not in seen
    assert 'launch_shortcut' not in seen
    assert 'remove_shortcut' not in seen
    assert 'app_foreground' not in seen

    context.shortcut_present = lambda: False
    context.launch_shortcut = lambda: None
    context.remove_shortcut = lambda: None
    context.app_foreground = lambda: True
    result = engine._dispatch_entry(context, cast(Controller, context.controller), entry)

    assert result.completed is True
    assert called == ['shortcut', 'shortcut']
    assert seen['shortcut_present'] is context.shortcut_present
    assert seen['launch_shortcut'] is context.launch_shortcut
    assert seen['remove_shortcut'] is context.remove_shortcut
    assert seen['app_foreground'] is context.app_foreground


def test_an_acted_recheck_scrolls_for_a_key_below_the_fold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    top = replace(
        _snapshot(
            _box('Coins boost', 100, 100, 500, 150),
            _box('Place an order on this page', 100, 600, 700, 640),
            _box('GO', 980, 630, 1040, 670),
        ),
        image=Image.new('RGB', (1200, 2000), 'white'),
    )
    below = replace(
        _snapshot(
            _box('Coins boost', 100, 100, 500, 150),
            _box('Visit the page via coins', 100, 600, 700, 640),
            _box('shortcut', 100, 645, 300, 675),
            _box('Unlock a daily surprise', 100, 680, 700, 710),
            _box('GO', 980, 630, 1040, 670),
        ),
        image=Image.new('RGB', (1200, 2000), '#e0e0e0'),
    )
    entry = DispatchGate(catalogue).authorize(enumerate_roster(below, catalogue).entries[0])
    context = _context(
        tmp_path,
        capture=_Capture(below, below, below),
        catalogue=catalogue,
        corpus=corpus,
        scroll_rosters=True,
    )
    monkeypatch.setattr(
        engine,
        'open_boost_list',
        lambda *args, **kwargs: BoostList(snapshot=top, route=DiscoveryRoute.BOOST, detail='test'),
    )

    readable, fresh = engine._recheck_acted_row(context, cast(Controller, context.controller), entry)

    assert readable is True
    assert fresh is not None
    assert fresh.key == 'coins_shortcut_visit'


def test_an_acted_recheck_does_not_accept_absence_from_a_bound_limited_scroll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    first = replace(
        _snapshot(
            _box('Coins boost', 100, 100, 500, 150),
            _box('Place an order on this page', 100, 600, 700, 640),
            _box('GO', 980, 630, 1040, 670),
        ),
        image=Image.new('RGB', (1200, 2000), 'white'),
    )
    second = replace(
        _snapshot(
            _box('Coins boost', 100, 100, 500, 150),
            _box('Add 5 items to your cart', 100, 600, 700, 640),
            _box('GO', 980, 630, 1040, 670),
        ),
        image=Image.new('RGB', (1200, 2000), '#e0e0e0'),
    )
    target = _snapshot(
        _box('Coins boost', 100, 100, 500, 150),
        _box('Visit the page via coins', 100, 600, 700, 640),
        _box('shortcut', 100, 645, 300, 675),
        _box('Unlock a daily surprise', 100, 680, 700, 710),
        _box('GO', 980, 630, 1040, 670),
    )
    entry = DispatchGate(catalogue).authorize(enumerate_roster(target, catalogue).entries[0])
    context = _context(
        tmp_path,
        capture=_Capture(second, second),
        catalogue=catalogue,
        corpus=corpus,
        scroll_rosters=True,
    )
    monkeypatch.setattr(
        engine,
        'open_boost_list',
        lambda *args, **kwargs: BoostList(snapshot=first, route=DiscoveryRoute.BOOST, detail='test'),
    )
    monkeypatch.setattr(engine, 'collect_roster', partial(collect_roster, max_scrolls=1))

    readable, fresh = engine._recheck_acted_row(context, cast(Controller, context.controller), entry)

    assert readable is False
    assert fresh is None


@pytest.mark.slow
def test_a_full_walk_dispatches_in_policy_order_and_reports_the_task_budget(
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
    summary = report.summary_path.read_text(encoding='utf-8')
    assert '| Turn on notifications | run budget | task budget of 2 dispatch(es) exhausted |' in summary
    assert 'no dispatchable row remains' not in summary
    assert report.summary_path.is_file()
    assert report.json_path.is_file()
    assert report.roster_log_path.is_file()
    assert report.failure_code == FailureCode.BUDGET_EXHAUSTED.value
    assert _report_payload(tmp_path)['status'] == 'budget'


@pytest.mark.slow
def test_the_engine_attempts_every_category_in_policy_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boost_frame: Snapshot,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = replace(
        boost_frame,
        boxes=(
            *boost_frame.boxes,
            _box('Search for what you love', 100, 1310, 700, 1350),
            _box('Earn by using keywords to explore items', 100, 1355, 760, 1385),
            _box('GO', 980, 1330, 1040, 1370),
        ),
    )
    wanted = (
        'boost_checkin',
        'coins_quiz',
        'search_for_what_you_love',
        'surprise_items',
        'sponsored_items',
        'notifications',
    )
    enumerated = enumerate_roster(frame, catalogue, route=DiscoveryRoute.BOOST)
    roster = replace(
        enumerated,
        entries=tuple(entry for entry in enumerated.entries if entry.key in wanted),
        surface_variant=Surface.BOOST_PAGE,
        routes_taken=(SurfaceRoute.SHORTCUT,),
    )
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        max_tasks=5,
    )
    order: list[str] = []

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        order.append(entry.key or '')
        if entry.key is not None:
            _context.state.record(entry.key)
        return EngineResult('completed', 'fake', 'done', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert order == list(wanted[:5])
    assert [task.key for task in report.tasks] == list(wanted[:5])
    assert report.status is RunStatus.BUDGET
    assert any(skip.key == 'notifications' and skip.reason == 'run budget' for skip in report.skipped)


@pytest.mark.slow
def test_roster_permutations_make_the_same_dispatch_choices_and_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boost_frame: Snapshot,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    wanted = {'boost_checkin', 'coins_quiz', 'surprise_items', 'sponsored_items', 'notifications'}
    entries = tuple(entry for entry in full_roster.entries if entry.key in wanted or entry.denied)
    permutations = [entries, tuple(reversed(entries))]
    seen = {tuple(entry.title for entry in order) for order in permutations}
    rng = random.Random(131)
    while len(permutations) < 7:
        candidate = tuple(rng.sample(entries, len(entries)))
        titles = tuple(entry.title for entry in candidate)
        if titles not in seen:
            permutations.append(candidate)
            seen.add(titles)

    def fingerprint(report: RunReport, context: RunContext) -> tuple[object, ...]:
        payload = dict(report.as_dict())
        roster = dict(cast(dict[str, Any], payload['roster'] or {}))
        payload['roster'] = roster
        roster['entries'] = sorted(
            cast(list[dict[str, Any]], roster.get('entries') or []),
            key=lambda entry: str(entry['title']),
        )
        payload['roster_log'] = 'roster.jsonl'
        payload['skipped'] = sorted(
            cast(list[dict[str, Any]], payload['skipped']),
            key=lambda skip: (str(skip['title']), str(skip['reason']), str(skip['detail'])),
        )
        summary = report.summary_path.read_text(encoding='utf-8').splitlines()
        policy_events = tuple(
            sorted(
                json.dumps({key: value for key, value in event.items() if key != 'time'}, sort_keys=True)
                for event in _events(report)
                if event.get('event') in {'policy_decision', 'policy_refusal'}
            )
        )
        return (
            json.dumps(payload, sort_keys=True),
            tuple(line for line in summary if not line.startswith('|')),
            tuple(sorted(line for line in summary if line.startswith('|'))),
            context.state.path.read_bytes(),
            policy_events,
        )

    fingerprints: set[tuple[object, ...]] = set()
    orders: set[tuple[str, ...]] = set()
    expected = ('boost_checkin', 'coins_quiz', 'surprise_items')
    for index, shuffled in enumerate(permutations):
        roster = replace(full_roster, entries=shuffled)
        _surface(monkeypatch, roster, boost_frame)
        # Keep the real observer and JSONL writer, but replay this permutation on
        # every re-enumeration instead of restoring the fixture frame's order.
        monkeypatch.setattr(
            'aliexpress_coins.tasks.discovery.enumerate_roster',
            lambda *_args, _roster=roster, **_kwargs: _roster,
        )
        context = _context(
            tmp_path / f'permutation-{index}',
            capture=_Capture(boost_frame, _coin_frame()),
            catalogue=catalogue,
            corpus=corpus,
            max_tasks=len(expected),
        )

        def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
            if entry.key is not None:
                _context.state.record(entry.key)
            return EngineResult('completed', 'fake', 'done', True, {'key': entry.key})

        monkeypatch.setattr(engine, '_dispatch_entry', dispatch)
        report = engine.run_once(context)

        assert report.status is RunStatus.BUDGET
        assert {(skip.key, skip.reason) for skip in report.skipped} >= {
            ('surprise_items', 'run budget'),
            ('sponsored_items', 'run budget'),
            ('notifications', 'run budget'),
        }
        summary = report.summary_path.read_text(encoding='utf-8')
        assert '| Browse surprise items | run budget |' in summary
        assert '| Explore sponsored items | run budget |' in summary
        assert '| Turn on notifications | run budget |' in summary
        orders.add(tuple(task.key for task in report.tasks))
        fingerprints.add(fingerprint(report, context))
        expected_titles = tuple(entry.title for entry in shuffled)
        reported_roster = cast(dict[str, Any], report.as_dict()['roster'] or {})
        reported_entries = cast(list[dict[str, Any]], reported_roster.get('entries') or [])
        assert tuple(entry['title'] for entry in reported_entries) == expected_titles
        observations = read_roster_log(report.roster_log_path)
        assert observations
        for observation in observations:
            rows = cast(list[dict[str, Any]], observation['rows'])
            assert tuple(row['label'] for row in rows) == expected_titles

    assert orders == {expected}
    assert len(fingerprints) == 1


@pytest.mark.slow
def test_a_pass_bound_lists_the_still_allowed_row_with_the_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'surprise_items'),))
    frame = _boost_frame('surprise_items')
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, max_passes=1)

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        if entry.key is not None:
            _context.state.record(entry.key)
        return EngineResult('completed', 'fake', 'done', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert report.status is RunStatus.BUDGET
    assert report.detail is not None
    assert 'pass budget of 1 exhausted' in report.detail
    still_allowed = [skip for skip in report.skipped if skip.key == 'surprise_items']
    assert len(still_allowed) == 1
    skip = next(skip for skip in report.skipped if skip.key == 'surprise_items')
    assert skip.reason == 'pass budget'
    assert 'pass budget of 1 exhausted' in skip.detail
    summary = report.summary_path.read_text(encoding='utf-8')
    assert '| Browse surprise items | pass budget | pass budget of 1 exhausted' in summary


def test_roster_signature_ignores_reading_order(full_roster: Roster) -> None:
    reordered = replace(full_roster, entries=tuple(reversed(full_roster.entries)))

    assert full_roster.titles != reordered.titles
    assert engine._roster_signature(full_roster) == engine._roster_signature(reordered)


@pytest.mark.slow
def test_a_task_is_written_in_flight_before_its_executor_finishes(
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
    report = RunReport(context.config.runs_dir, 'in-flight-write', budget_seconds=60.0, started_at=CAPTURED_AT)
    observed: list[str] = []

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        payload = json.loads(report.json_path.read_text(encoding='utf-8'))
        summary = report.summary_path.read_text(encoding='utf-8')
        observed.append(payload['tasks'][0]['state'])
        assert 'Take the coins quiz' in summary
        assert 'No task was dispatched.' not in summary
        if entry.key is not None:
            _context.state.record(entry.key)
        return EngineResult('completed', 'fake', 'done', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    engine.run_once(context, report=report)

    assert observed == ['in_flight']
    assert report.tasks[0].state == 'completed'


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
    summary = report.summary_path.read_text(encoding='utf-8')
    assert '| Take the coins quiz | run budget | wall-clock budget of 5s exhausted |' in summary
    assert 'no dispatchable row remains' not in summary


@pytest.mark.slow
def test_a_wall_clock_budget_after_a_repeatable_completion_keeps_the_night_open_and_lists_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boost_frame: Snapshot,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    wanted = ('sponsored_items', 'notifications')
    roster = replace(full_roster, entries=tuple(entry for entry in full_roster.entries if entry.key in wanted))
    frame = _boost_frame(*wanted)
    clock = _Clock()
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        clock=clock,
        budget=50.0,
    )

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        assert entry.key is not None
        _context.state.record(entry.key)
        clock.value = 60.0
        return EngineResult('completed', 'fake', 'done', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert report.status is RunStatus.BUDGET
    assert report.detail is not None
    assert 'wall-clock budget of 50s exhausted' in report.detail
    assert [task.key for task in report.tasks] == ['sponsored_items']
    assert report.tasks[0].completed
    assert report.night is None
    assert context.state.snapshot().completed_night is False
    assert report.json_path.is_file()
    assert report.summary_path.is_file()
    payload = _report_payload(tmp_path)
    assert payload['status'] == 'budget'
    assert payload['budget']['seconds'] == 50.0
    assert payload['budget']['used_seconds'] >= 50.0
    # The repeatable row completed once but is still below its daily max_per_run,
    # so #131 names it alongside the untouched row at the wall-clock stop.
    skipped = {(skip.key, skip.reason, skip.detail) for skip in report.skipped}
    assert (
        'sponsored_items',
        'run budget',
        'wall-clock budget of 50s exhausted',
    ) in skipped
    assert (
        'notifications',
        'run budget',
        'wall-clock budget of 50s exhausted',
    ) in skipped
    summary = report.summary_path.read_text(encoding='utf-8')
    assert '| Explore sponsored items | run budget | wall-clock budget of 50s exhausted |' in summary
    assert '| Turn on notifications | run budget | wall-clock budget of 50s exhausted |' in summary


@pytest.mark.slow
def test_a_task_that_crosses_the_wall_clock_budget_ends_as_budget_with_its_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    wanted = ('coins_quiz', 'notifications')
    roster = replace(full_roster, entries=tuple(_entry(full_roster, key) for key in wanted))
    frame = _boost_frame(*wanted)
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        budget=30.0,
    )

    def slow_dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        # The task sleeps past the run deadline instead of returning normally.
        # The engine's task-budget seams must stop it before this returns.
        _context.sleep(31.0)
        raise AssertionError(f'{entry.key!r} outlived the wall-clock budget')

    monkeypatch.setattr(engine, '_dispatch_entry', slow_dispatch)

    report = engine.run_once(context)

    assert report.status is RunStatus.BUDGET
    assert report.detail is not None
    assert 'wall-clock budget of 30s exhausted during' in report.detail
    assert report.failure_code == FailureCode.BUDGET_EXHAUSTED.value
    assert [task.key for task in report.tasks] == ['coins_quiz']
    assert report.tasks[0].state == 'aborted'
    assert report.tasks[0].code == FailureCode.BUDGET_EXHAUSTED.value
    assert context.state.snapshot().completed_night is False
    # The task may have acted before the deadline, so the day must carry the
    # conservative marker even though this attempt never returned a result.
    assert context.state.is_acted_unverified('coins_quiz') is True
    still_allowed = {(skip.key, skip.reason) for skip in report.skipped}
    assert still_allowed == {('notifications', 'run budget')}
    assert not any(skip.key == 'coins_quiz' for skip in report.skipped)
    assert report.json_path.is_file()
    assert report.summary_path.is_file()
    summary = report.summary_path.read_text(encoding='utf-8')
    assert '| Turn on notifications | run budget | wall-clock budget of 30s exhausted during' in summary
    assert '| Take the coins quiz | run budget |' not in summary
    payload = _report_payload(tmp_path)
    assert payload['status'] == 'budget'
    assert payload['night'] is None
    assert payload['tasks'][0]['state'] == 'aborted'

    # A later run must take the acted re-check path, not dispatch the possibly
    # claimed row from a fresh roster.
    second_roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    second_frame = _boost_frame('coins_quiz')
    _surface(monkeypatch, second_roster, second_frame)
    second = _context(
        tmp_path,
        capture=_Capture(second_frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        budget=30.0,
    )
    second.wall_clock = lambda: CAPTURED_AT + timedelta(minutes=5)
    dispatches: list[str] = []

    def must_not_dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        dispatches.append(entry.key or '')
        if entry.key == 'coins_quiz':
            raise AssertionError('the possibly claimed row must not be dispatched again')
        if entry.key is not None:
            _context.state.record(entry.key)
        return EngineResult('completed', 'fake', 'done', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', must_not_dispatch)
    rechecks: list[str] = []

    def recheck(_context: RunContext, _controller: Controller, entry: RosterEntry) -> tuple[bool, RosterEntry | None]:
        rechecks.append(entry.key or '')
        return True, None

    monkeypatch.setattr(engine, '_recheck_acted_row', recheck)

    second_report = engine.run_once(second)

    assert rechecks == ['coins_quiz']
    assert 'coins_quiz' not in dispatches
    assert second_report.tasks[0].key == 'coins_quiz'
    assert second_report.tasks[0].verification == 'already_done'
    assert second.state.is_acted_unverified('coins_quiz') is True


def test_the_task_budget_guard_checks_the_clock_and_capture_seams(
    tmp_path: Path,
    boost_frame: Snapshot,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    clock = _Clock()
    context = _context(
        tmp_path / 'clock',
        capture=_Capture(boost_frame),
        catalogue=catalogue,
        corpus=corpus,
        clock=clock,
    )

    def read_after_deadline() -> float:
        clock.value = 5.0
        return context.clock()

    with pytest.raises(RunBudgetExceeded, match='wall-clock budget ended'), engine._task_budget(context, 5.0):
        read_after_deadline()

    def late_capture() -> Snapshot:
        clock.value = 6.0
        return boost_frame

    clock.value = 0.0
    context.capture = late_capture
    with pytest.raises(RunBudgetExceeded, match='wall-clock budget ended'), engine._task_budget(context, 5.0):
        engine._capture(context.capture)


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
def test_an_executor_exception_is_a_failed_task_and_an_incomplete_night(
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
    notifier = _Notifier()
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        notifier=notifier,
    )

    def dispatch(*args: Any, **kwargs: Any) -> EngineResult:
        raise RuntimeError('executor broke')

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    _seed_other_families(context, catalogue, roster)
    report = engine.run_once(context)

    assert report.status is RunStatus.FAILED
    assert len(report.tasks) == 1
    assert report.tasks[0].outcome == 'failed'
    assert 'RuntimeError: executor broke' in report.tasks[0].detail
    assert report.ok is False
    assert report.failure_code == FailureCode.TASK_UNVERIFIED.value
    assert report.detail is not None
    assert "'Take the coins quiz'" in report.detail
    assert 'RuntimeError: executor broke' in report.detail
    assert context.state.snapshot().completed_night is False
    night = context.state.snapshot().last_night
    assert night is not None
    assert night.outcome is NightOutcome.RETRYABLE_FAILURES
    assert night.retryable_failures == 1
    assert len(notifier.messages) == 1
    assert "'Take the coins quiz'" in notifier.messages[0].body
    assert 'task_unverified' in notifier.messages[0].body
    assert 'RuntimeError: executor broke' in notifier.messages[0].body


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
    frame = _coin_frame()
    _surface(
        monkeypatch,
        replace(full_roster, entries=(), surface_variant=Surface.ROWS_ABSENT),
        frame,
    )
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
    snapshot = context.state.snapshot()
    # An empty coin page is a load fault, not proof that the day has no work.
    assert snapshot.completed_night is False
    assert snapshot.last_night is not None
    assert snapshot.last_night.outcome is NightOutcome.INCOMPLETE_ROSTER
    assert report.night == snapshot.last_night.as_dict()
    assert isinstance(context.hygiene, _Hygiene)
    assert context.hygiene.restored == 1


def test_an_incomplete_roster_does_not_complete_the_night(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _incomplete_coin_frame()
    context = _context(tmp_path, capture=_Capture(frame), catalogue=catalogue, corpus=corpus)

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        assert entry.key is not None
        assert entry.key == 'coins_quiz'
        _context.state.record(entry.key)
        return EngineResult('completed', 'fake', 'done', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert report.status is RunStatus.COMPLETED
    assert len(report.tasks) == 1
    assert report.tasks[0].completed
    assert report.roster is not None
    assert report.roster['incomplete'] is True
    assert report.night is not None
    assert report.night['outcome'] == NightOutcome.INCOMPLETE_ROSTER.value
    snapshot = context.state.snapshot()
    assert snapshot.completed_night is False
    assert snapshot.last_night is not None
    assert snapshot.last_night.outcome is NightOutcome.INCOMPLETE_ROSTER
    assert '**Night:** open — the roster read was incomplete' in report.summary_path.read_text(encoding='utf-8')
    assert any(event['event'] == 'roster_incomplete' for event in _events(report))


def test_a_titleless_or_unconfirmed_roster_restarts_the_surface(
    full_roster: Roster,
) -> None:
    """Issue #137: zero allowed rows from an unconfirmed read is not a completed night."""
    roster = replace(full_roster, entries=(), unreadable_count=1)
    observation = RosterObservation(sequence=1, timestamp=CAPTURED_AT, rows=(), roster=roster)

    reason = engine._pass_restart_reason(roster, observation)

    assert reason is not None
    assert 'incomplete' in reason
    assert 'no readable title' in reason


def test_an_unconfirmed_or_lost_boost_surface_restarts_by_route(full_roster: Roster) -> None:
    observation = RosterObservation(sequence=1, timestamp=CAPTURED_AT, rows=(), roster=full_roster)

    unconfirmed = replace(full_roster, entries=(), scroll_surface_unconfirmed=True)
    lost = replace(full_roster, entries=(), scroll_surface_lost=True)

    for roster in (unconfirmed, lost):
        reason = engine._pass_restart_reason(roster, observation)
        assert reason is not None
        assert reason.startswith('surface_lost:')


def test_a_titleless_only_roster_does_not_complete_the_night(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _snapshot(
        _box('Coins boost', 100, 100, 500, 150),
        _box('0/3', 960, 620, 1060, 660),
        _box('GO', 980, 630, 1040, 670),
    )
    titleless = replace(
        enumerate_roster(frame, catalogue, route=DiscoveryRoute.BOOST),
        surface_variant=Surface.BOOST_PAGE,
    )
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: titleless)
    monkeypatch.setattr(
        engine,
        'open_boost_list',
        lambda *args, **kwargs: BoostList(snapshot=frame, route=DiscoveryRoute.BOOST, detail='test'),
    )
    context = _context(tmp_path, capture=_Capture(frame), catalogue=catalogue, corpus=corpus)

    report = engine.run_once(context)

    assert report.status is RunStatus.BUDGET
    assert report.night is None
    assert context.state.snapshot().completed_night is False
    assert report.roster is not None
    assert report.roster['unreadable_count'] == 1
    assert report.roster['incomplete'] is True


def test_a_rows_absent_coin_page_does_not_complete_the_night(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    """An empty coin page is a load fault, not positive evidence of no tasks."""
    frame = _coin_frame()
    roster = replace(
        enumerate_roster(frame, catalogue),
        surface_variant=Surface.ROWS_ABSENT,
    )
    _surface(monkeypatch, roster, frame)
    context = _context(tmp_path, capture=_Capture(frame), catalogue=catalogue, corpus=corpus)

    report = engine.run_once(context)

    assert report.status is RunStatus.COMPLETED
    assert report.roster is not None
    assert report.roster['incomplete'] is True
    assert report.night is not None
    assert report.night['outcome'] == NightOutcome.INCOMPLETE_ROSTER.value
    assert context.state.snapshot().completed_night is False


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


def test_a_dry_run_reads_an_all_claimed_boost_sheet_as_an_empty_roster(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    """Issue #150: claimed-only rows are read, reported, and never dispatched."""
    frame = _all_claimed_boost_frame()
    context = _context(
        tmp_path,
        capture=_Capture(*([frame] * 6)),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, frame, rng=random.Random(0))
    monkeypatch.setattr(
        engine,
        '_dispatch_entry',
        lambda *args, **kwargs: pytest.fail('a claimed row reached an executor'),
    )

    report = engine.run_once(context)

    assert report.status is RunStatus.PLANNED
    assert report.ok is True
    assert report.plan is not None
    assert report.plan['probe_failed'] is False
    assert report.plan['surface'] == 'boost_page'
    assert report.plan['steps'] == []
    assert report.plan['refusals'] == []
    assert report.plan['taps_issued'] == 0
    assert report.plan['rows_read'] == 3
    assert report.plan['rows_allowed'] == 3
    assert report.plan['rows_refused'] == 0
    assert report.plan['rows_dispatchable'] == 0
    assert report.plan['rows_claimed'] == 3
    assert report.roster is not None
    assert report.roster['count'] == 3
    assert report.roster['allowed_count'] == 3
    assert report.roster['denied_count'] == 0
    assert report.roster['dispatchable_count'] == 0
    assert report.roster['claimed_count'] == 3
    assert report.roster['incomplete'] is False
    summary = report.summary_path.read_text(encoding='utf-8')
    roster_line = next(line for line in summary.splitlines() if line.startswith('- **Roster:**'))
    plan_line = next(line for line in summary.splitlines() if line.startswith('- **Plan (dry run):**'))
    payload = _report_payload(tmp_path)
    plan = cast(dict[str, int], payload['plan'])
    expected = (
        f'{plan["rows_read"]} row(s) read, {plan["rows_allowed"]} allowed, '
        f'{plan["rows_refused"]} refused, {plan["rows_dispatchable"]} dispatchable, '
        f'{plan["rows_claimed"]} already claimed'
    )
    assert expected == '3 row(s) read, 3 allowed, 0 refused, 0 dispatchable, 3 already claimed'
    assert expected in roster_line
    assert expected in plan_line
    assert context.controller.tap_count == 0
    assert device.taps == []


def test_a_dry_run_keeps_a_mixed_sheet_with_one_partial_title(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    """A lone title beside readable rows is a warning, not a whole-surface failure."""
    frame = _mixed_boost_frame()
    context = _context(
        tmp_path,
        capture=_Capture(*([frame] * 8)),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, frame, rng=random.Random(0))

    report = engine.run_once(context)

    assert report.status is RunStatus.PLANNED
    assert report.ok is True
    assert report.plan is not None
    assert report.plan['probe_failed'] is False
    assert report.plan['rows_read'] == 2
    assert report.plan['rows_allowed'] == 2
    assert report.plan['rows_dispatchable'] == 2
    assert report.plan['rows_claimed'] == 0
    assert [step['key'] for step in cast('list[dict[str, object]]', report.plan['steps'])] == [
        'coins_quiz',
        'sponsored_items',
    ]
    assert report.roster is not None
    assert report.roster['card_structure_failures'] == 1
    assert report.roster['card_structure_titles'] == ['Turn on notifications']
    assert report.roster['incomplete'] is True
    summary = report.summary_path.read_text(encoding='utf-8')
    assert "'Turn on notifications' had no readable card structure" in summary
    assert context.controller.tap_count == 0
    assert device.taps == []


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


def test_a_dry_run_reports_a_visible_go_that_did_not_become_a_row(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _incomplete_coin_frame()
    context = _context(tmp_path, capture=_Capture(frame), catalogue=catalogue, corpus=corpus)
    context.config = replace(context.config, dry_run=True)

    report = engine.run_once(context)

    assert report.status is RunStatus.PLANNED
    assert report.plan is not None
    assert report.plan['taps_issued'] == 0
    assert report.roster is not None
    assert report.roster['count'] == 1
    assert report.roster['visible_action_count'] == 2
    assert report.roster['unassigned_action_count'] == 1
    assert report.roster['incomplete'] is True
    assert 'not enumerated' in str(report.roster['warning'])
    assert 'warning: 2 visible GO button(s)' in report.summary_path.read_text(encoding='utf-8')
    warning = next(event for event in _events(report) if event['event'] == 'roster_incomplete')
    assert warning['level'] == 'warning'
    assert warning['unassigned_action_count'] == 1
    assert context.state.snapshot().completed_night is False


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
    assert content_waits[-1]['history'] == [True]
    assert context.controller.tap_count == 0
    assert device.taps == []


def test_a_dry_run_settles_a_still_rendering_boost_page_before_planning(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    rendering = _rendering_boost_frame()
    content = _boost_frame('coins_quiz')
    context = _context(
        tmp_path,
        capture=_Capture(rendering, content, content),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, rendering, rng=random.Random(0))
    assert isinstance(context.navigate, _Navigate)

    report = engine.run_once(context)

    assert context.navigate.routes == []
    assert report.status is RunStatus.PLANNED
    assert report.ok is True
    assert report.plan is not None
    assert report.plan['probe_failed'] is False
    assert report.plan['surface'] == 'boost_page'
    assert [step['key'] for step in report.plan['steps']] == ['coins_quiz']
    assert report.plan['taps_issued'] == 0
    stable_waits = report.plan['stable_waits']
    assert isinstance(stable_waits, list)
    assert stable_waits[-1]['status'] == 'satisfied'
    assert stable_waits[-1]['differences'][0] > stable_waits[-1]['threshold']
    content_waits = report.plan['content_waits']
    assert isinstance(content_waits, list)
    assert content_waits[-1]['status'] == 'satisfied'
    assert content_waits[-1]['history'] == [True]
    assert context.controller.tap_count == 0
    assert device.taps == []


def test_a_dry_run_rereads_rows_that_appear_after_the_frame_settles(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    rendering = _rendering_boost_frame()
    content = _boost_frame('coins_quiz')
    context = _context(
        tmp_path,
        capture=_Capture(rendering, rendering, rendering, content),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, rendering, rng=random.Random(0))

    report = engine.run_once(context)

    assert report.status is RunStatus.PLANNED
    assert report.plan is not None
    assert [step['key'] for step in report.plan['steps']] == ['coins_quiz']
    stable_waits = report.plan['stable_waits']
    assert isinstance(stable_waits, list)
    assert stable_waits[-1]['status'] == 'satisfied'
    assert stable_waits[-1]['differences'] == [0.0, 0.0]
    content_waits = report.plan['content_waits']
    assert isinstance(content_waits, list)
    assert content_waits[-1]['status'] == 'satisfied'
    assert content_waits[-1]['history'] == [False, True]
    plan_event = next(event for event in _events(report) if event['event'] == 'dry_run_plan')
    assert plan_event['probe_failed'] is False
    payload = _report_payload(tmp_path)
    plan = payload['plan']
    assert plan['probe_failed'] is False
    assert plan['stable_waits'][-1]['status'] == 'satisfied'
    assert plan['content_waits'][-1]['history'] == [False, True]
    assert context.controller.tap_count == 0
    assert device.taps == []


def test_a_dry_run_waits_for_the_rest_of_a_partially_painted_roster(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    partial = _boost_frame('coins_quiz')
    blank = _rendering_boost_frame()
    complete = _boost_frame('coins_quiz', 'sponsored_items')
    context = _context(
        tmp_path,
        capture=_Capture(partial, partial, partial, blank, complete, complete),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, partial, rng=random.Random(0))

    report = engine.run_once(context)

    assert report.status is RunStatus.PLANNED
    assert report.plan is not None
    assert [step['key'] for step in report.plan['steps']] == ['coins_quiz', 'sponsored_items']
    roster_waits = report.plan['roster_waits']
    assert isinstance(roster_waits, list)
    assert roster_waits[-1]['status'] == 'satisfied'
    assert context.controller.tap_count == 0
    assert device.taps == []


def test_a_dry_run_names_a_terminal_state_that_arrives_during_the_row_reread(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    partial = _boost_frame('coins_quiz')
    terminal = _empty_boost_frame()
    context = _context(
        tmp_path,
        capture=_Capture(partial, partial, partial, terminal, terminal, terminal),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, partial, rng=random.Random(0))

    report = engine.run_once(context)

    assert report.status is RunStatus.FAILED
    assert report.failure_code == FailureCode.NAVIGATION_FAILED.value
    assert report.detail is not None
    assert report.detail.startswith('surface_empty_boost')
    assert report.plan is not None
    assert report.plan['surface'] == 'empty_boost'
    assert report.plan['probe_failed'] is True
    assert report.plan['steps'] == []
    assert report.plan['taps_issued'] == 0
    assert context.controller.tap_count == 0
    assert device.taps == []


def test_a_dry_run_names_a_terminal_state_that_arrives_during_the_row_wait(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    rendering = _rendering_boost_frame()
    terminal = _empty_boost_frame()
    context = _context(
        tmp_path,
        capture=_Capture(rendering, rendering, rendering, terminal, terminal, terminal),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, rendering, rng=random.Random(0))

    report = engine.run_once(context)

    assert report.status is RunStatus.FAILED
    assert report.failure_code == FailureCode.NAVIGATION_FAILED.value
    assert report.detail is not None
    assert report.detail.startswith('surface_empty_boost')
    assert report.plan is not None
    assert report.plan['surface'] == 'empty_boost'
    assert report.plan['probe_failed'] is True
    assert report.plan['steps'] == []
    assert report.plan['taps_issued'] == 0
    assert context.controller.tap_count == 0
    assert device.taps == []


def test_a_dry_run_fails_when_the_row_set_never_settles(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    partial = _boost_frame('coins_quiz')
    other = _boost_frame('sponsored_items')
    context = _context(
        tmp_path,
        capture=_Capture(partial, *([partial, other] * 40)),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, partial, rng=random.Random(0))

    report = engine.run_once(context)

    assert report.status is RunStatus.FAILED
    assert report.failure_code == FailureCode.NAVIGATION_FAILED.value
    assert report.detail is not None
    assert report.detail.startswith('surface_unverified: task rows were read but the row set never settled')
    assert report.plan is not None
    assert report.plan['probe_failed'] is True
    roster_waits = report.plan['roster_waits']
    assert isinstance(roster_waits, list)
    assert roster_waits[-1]['status'] == 'timed_out'
    assert context.controller.tap_count == 0
    assert device.taps == []


def test_a_dry_run_does_not_plan_rows_from_a_surface_that_never_settles(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    rendering = _rendering_boost_frame()
    changing = replace(rendering, image=Image.new('RGB', (1200, 2000), '#e0e0e0'))
    content = _boost_frame('coins_quiz')
    context = _context(
        tmp_path,
        capture=_Capture(*([rendering, changing] * 10), rendering, content),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, rendering, rng=random.Random(0))

    report = engine.run_once(context)

    assert report.status is RunStatus.FAILED
    assert report.failure_code == FailureCode.NAVIGATION_FAILED.value
    assert report.detail is not None
    assert report.detail.startswith('surface_unverified: task rows were read but the surface never settled')
    assert 'stability wait timed_out' in report.detail
    assert report.plan is not None
    assert report.plan['probe_failed'] is True
    stable_waits = report.plan['stable_waits']
    assert isinstance(stable_waits, list)
    assert stable_waits[-1]['status'] == 'timed_out'
    content_waits = report.plan['content_waits']
    assert isinstance(content_waits, list)
    assert content_waits[-1]['status'] == 'satisfied'
    assert [step['key'] for step in report.plan['steps']] == ['coins_quiz']
    assert context.controller.tap_count == 0
    assert device.taps == []


def test_a_dry_run_does_not_settle_a_route_that_arrived_on_a_non_task_surface(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    destination = _coin_store_frame()
    context = _context(
        tmp_path,
        capture=_Capture(_home_frame(), destination),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)

    report = engine.run_once(context)

    assert report.status is RunStatus.FAILED
    assert report.detail is not None
    assert report.detail.startswith('surface_incomplete')
    assert report.plan is not None
    assert report.plan['surface'] == 'coin_store'
    assert report.plan['stable_waits'] == []
    assert report.plan['content_waits'] == []
    assert report.plan['probe_failed'] is True


def test_a_dry_run_stops_settling_when_a_popup_appears(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    rendering = _rendering_boost_frame()
    covered = _coin_popup_frame()
    assert engine._has_plan_rows(covered, catalogue) is False
    context = _context(
        tmp_path,
        capture=_Capture(rendering, covered, covered, covered),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)
    context.safety = SafetyGuard(catalogue=catalogue, limit=4)

    report = engine.run_once(context)

    assert report.status is RunStatus.FAILED
    assert report.failure_code == FailureCode.NAVIGATION_FAILED.value
    assert report.detail is not None
    assert report.detail.startswith('surface_popup')
    assert report.plan is not None
    assert report.plan['surface'] == 'unknown'
    stable_waits = report.plan['stable_waits']
    assert isinstance(stable_waits, list)
    assert stable_waits[-1]['status'] == 'satisfied'
    assert report.plan['content_waits'] == []
    assert report.plan['taps_issued'] == 0
    assert context.controller is not None
    assert context.controller.tap_count == 0


def test_a_dry_run_does_not_reread_rows_after_settling_on_a_non_task_surface(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    destination = _coin_store_frame()
    context = _context(
        tmp_path,
        capture=_Capture(_rendering_boost_frame(), destination, destination, destination),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, dry_run=True)

    report = engine.run_once(context)

    assert report.status is RunStatus.FAILED
    assert report.plan is not None
    assert report.plan['surface'] == 'coin_store'
    stable_waits = report.plan['stable_waits']
    assert isinstance(stable_waits, list)
    assert stable_waits[-1]['status'] == 'satisfied'
    assert report.plan['content_waits'] == []
    assert report.plan['probe_failed'] is True


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
    assert report.plan['probe_failed'] is True
    assert report.plan['surface'] == 'empty_boost'
    assert report.plan['steps'] == []
    assert report.plan['taps_issued'] == 0
    plan_event = next(event for event in _events(report) if event['event'] == 'dry_run_plan')
    assert plan_event['probe_failed'] is True
    payload = _report_payload(tmp_path)
    assert payload['plan']['probe_failed'] is True
    assert '**Plan (dry run):** dry-run probe failed: surface_empty_boost' in report.summary_path.read_text(
        encoding='utf-8'
    )
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


def test_a_dry_run_recovers_from_a_non_app_surface_then_fails_without_a_plan(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _non_app_clock_frame()
    context = _context(tmp_path, capture=_Capture(frame), catalogue=catalogue, corpus=corpus)
    context.config = replace(context.config, dry_run=True)
    device = _RecordingDevice()
    context.controller = Controller.from_frame(device, frame, rng=random.Random(0))
    assert isinstance(context.navigate, _Navigate)

    report = engine.run_once(context)

    assert context.navigate.routes == [SurfaceRoute.SHORTCUT]
    assert report.status is RunStatus.FAILED
    assert report.failure_code == FailureCode.NAVIGATION_FAILED.value
    assert report.detail is not None
    assert report.detail.startswith('surface_non_app')
    assert report.plan is not None
    assert report.plan['surface'] == 'non_app'
    assert report.plan['steps'] == []
    assert report.plan['taps_issued'] == 0
    assert context.controller.tap_count == 0
    assert device.taps == []
    assert context.record_foreground_loss is not None
    context.record_foreground_loss('sponsored_items', frame)
    assert report.shots['browse-sponsored_items-foreground-lost'] == 'shots/browse-sponsored_items-foreground-lost.png'
    assert (report.run_dir / 'shots' / 'browse-sponsored_items-foreground-lost.png').is_file()


def test_a_live_run_fails_when_route_discovery_recovers_only_to_non_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _non_app_clock_frame()
    context = _context(tmp_path, capture=_Capture(frame), catalogue=catalogue, corpus=corpus)
    non_app_roster = Roster(
        entries=(),
        route=None,
        catalogue_version=catalogue.version,
        default_deny_reason=catalogue.default_deny_reason,
        surface_variant=Surface.NON_APP,
        routes_taken=(SurfaceRoute.SHORTCUT, SurfaceRoute.SHORTCUT),
        refresh_attempted=True,
    )
    monkeypatch.setattr(engine, 'discover_roster', lambda *_args, **_kwargs: non_app_roster)

    report = engine.run_once(context)

    assert report.status is RunStatus.FAILED
    assert report.failure_code == FailureCode.NAVIGATION_FAILED.value
    assert report.surface == 'non_app'
    assert report.roster is not None
    assert report.roster['incomplete'] is True


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
    assert 'stability wait satisfied' in report.detail
    assert '(row wait timed_out)' in report.detail
    assert report.plan is not None
    assert report.plan['probe_failed'] is True
    assert report.plan['surface'] == 'rows_present'
    stable_waits = report.plan['stable_waits']
    assert isinstance(stable_waits, list)
    assert stable_waits[-1]['status'] == 'satisfied'
    content_waits = report.plan['content_waits']
    assert isinstance(content_waits, list)
    assert content_waits[-1]['status'] == 'timed_out'
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
    assert '(no row wait was possible)' in report.detail
    assert report.plan_note == 'could not read the coins page: DeviceUnavailable: warm re-entry unavailable'
    assert report.plan is not None
    assert report.plan['surface'] == 'rows_absent'
    assert report.plan['stable_waits'] == []
    assert report.plan['content_waits'] == []
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
    assert len(report.tasks) == 1
    aborted_task = report.tasks[0]
    assert aborted_task.title == 'Take the coins quiz'
    assert aborted_task.key == 'coins_quiz'
    assert aborted_task.state == 'aborted'
    assert aborted_task.outcome == 'aborted'
    assert aborted_task.code == FailureCode.DANGEROUS_CONTROL.value
    assert aborted_task.result['abort']['screen']['labels'] == ['Mystery checkout', 'Buy now']
    summary = report.summary_path.read_text(encoding='utf-8')
    assert '**Abort:** dangerous_control' in summary
    assert 'Mystery checkout' in summary
    assert '| Take the coins quiz | coins_quiz | dangerous_control | aborted |' in summary
    assert 'No task was dispatched.' not in summary
    payload = _report_payload(tmp_path)
    assert payload['status'] == 'aborted'
    assert payload['abort']['screen']['surface'] == 'unknown'
    assert payload['tasks'][0]['state'] == 'aborted'
    assert payload['tasks'][0]['result']['abort']['screen']['labels'] == ['Mystery checkout', 'Buy now']


@pytest.mark.slow
def test_an_abort_while_routing_still_reports_no_task_dispatched(
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
        engine,
        'discover_roster',
        lambda *args, **kwargs: (_ for _ in ()).throw(_safety_abort()),
    )

    report = engine.run_once(context)

    assert report.status is RunStatus.ABORTED
    assert report.abort is not None
    assert report.tasks == []
    summary = report.summary_path.read_text(encoding='utf-8')
    assert '**Abort:** dangerous_control' in summary
    assert 'No task was dispatched.' in summary
    payload = _report_payload(tmp_path)
    assert payload['tasks'] == []


@pytest.mark.slow
def test_one_tasks_unknown_screen_fails_the_task_and_the_run_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    # The three rows from night 2026-11-23, in the order they were enumerated.
    keys = ('boost_checkin', 'sponsored_items', 'surprise_items')
    # Selection is by cost, not by that reading order: sign-in, item taps, then the dwell.
    selected = ('boost_checkin', 'surprise_items', 'sponsored_items')
    frame_rows = ('daily_checkin', 'sponsored_items', 'surprise_items')
    roster = replace(
        full_roster,
        entries=tuple(_entry(full_roster, key) for key in keys),
        surface_variant=Surface.ROWS_PRESENT,
    )
    frame = _boost_frame(*frame_rows)
    _surface(monkeypatch, roster, frame)
    unknown = _home_frame()
    notifier = _Notifier()
    context = _context(
        tmp_path,
        capture=_Capture(_coin_frame(), unknown, unknown, unknown, _coin_frame(), _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        max_tasks=3,
        notifier=notifier,
    )
    order: list[str] = []
    recovery_attempts: list[int | None] = []

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        order.append(entry.key or '')
        if entry.key == 'sponsored_items':
            # Three identical unknown captures inside the task trip the real guard.
            for _ in range(3):
                engine._capture(_context.capture)
            raise AssertionError('the unexpected-screen guard did not trip')
        if entry.key is not None:
            _context.state.record(entry.key)
        return EngineResult('completed', 'fake', 'done', True, {'key': entry.key})

    def recover(*_args: Any, **kwargs: Any) -> ReturnResult:
        recovery_attempts.append(kwargs.get('attempts'))
        return ReturnResult(
            returned=True,
            reentered=True,
            attempts=kwargs.get('attempts', 1),
            route=SurfaceRoute.SHORTCUT,
            elapsed=0.1,
            snapshot=_coin_frame(),
        )

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)
    monkeypatch.setattr(engine, 'return_to_coins_page', recover)

    report = engine.run_once(context)

    assert order == list(selected)
    assert report.status is RunStatus.FAILED
    assert report.ok is False
    assert report.abort is None
    assert report.failure_code == FailureCode.BUDGET_EXHAUSTED.value
    assert report.detail is not None
    assert 'task budget of 3 dispatch(es) exhausted' in report.detail
    assert "'Explore sponsored items'" in report.detail
    assert len(notifier.messages) == 1
    assert 'budget_exhausted' in notifier.messages[0].body
    assert "'Explore sponsored items'" in notifier.messages[0].body
    assert context.state.snapshot().completed_night is False
    assert [task.key for task in report.tasks] == list(selected)
    failed = next(task for task in report.tasks if task.key == 'sponsored_items')
    assert failed.outcome == 'failed'
    assert failed.code == FailureCode.TASK_UNVERIFIED.value
    assert failed.verification == 'unexpected_screen'
    assert failed.result['payload']['screen']['labels'] == ['For you', 'Search']
    assert failed.result['payload']['recovery']['returned'] is True
    assert engine.TASK_RECOVERY_ATTEMPTS in recovery_attempts
    assert context.state.count('sponsored_items') == 0
    assert context.state.count('boost_checkin') == 1
    assert context.state.count('surprise_items') == 1
    assert report.returned_to_coins_page is True
    summary = report.summary_path.read_text(encoding='utf-8')
    assert 'task_unverified' in summary
    assert '**Abort:**' not in summary
    assert not [
        event for event in _events(report) if event.get('event') == 'run_aborted' and event.get('level') == 'error'
    ]


@pytest.mark.slow
@pytest.mark.parametrize('returned', [False, True])
def test_a_run_still_aborts_when_recovery_cannot_reach_the_coins_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    returned: bool,
) -> None:
    roster = replace(
        full_roster,
        entries=(_entry(full_roster, 'sponsored_items'),),
        surface_variant=Surface.ROWS_PRESENT,
    )
    _surface(monkeypatch, roster, _boost_frame('sponsored_items'))
    unknown = _home_frame()
    context = _context(
        tmp_path,
        capture=_Capture(_coin_frame(), unknown, unknown, unknown, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    recovery_attempts: list[int | None] = []

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        assert entry.key == 'sponsored_items'
        for _ in range(3):
            engine._capture(_context.capture)
        raise AssertionError('the unexpected-screen guard did not trip')

    def recover(*_args: Any, **kwargs: Any) -> ReturnResult:
        recovery_attempts.append(kwargs.get('attempts'))
        return ReturnResult(
            returned=returned,
            reentered=returned,
            attempts=kwargs.get('attempts', 1),
            route=SurfaceRoute.SHORTCUT,
            elapsed=1.0,
            # ``returned=True`` with an unknown frame is the second failed-recovery
            # case: the route fired but the page it proved is not drivable.
            snapshot=None if not returned else unknown,
            error=None if returned else 'the shortcut route never proved the coin page',
        )

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)
    monkeypatch.setattr(engine, 'return_to_coins_page', recover)

    report = engine.run_once(context)

    assert report.status is RunStatus.ABORTED
    assert report.ok is False
    assert report.abort is not None
    assert report.abort['code'] == 'unexpected_screen'
    assert report.abort['screen']['labels'] == ['For you', 'Search']
    assert report.failure_code == FailureCode.UNEXPECTED_SCREEN.value
    assert engine.TASK_RECOVERY_ATTEMPTS in recovery_attempts
    assert len(report.tasks) == 1
    aborted_task = report.tasks[0]
    assert aborted_task.key == 'sponsored_items'
    assert aborted_task.title == 'Explore sponsored items'
    assert aborted_task.state == 'aborted'
    assert aborted_task.outcome == 'aborted'
    assert aborted_task.code == FailureCode.UNEXPECTED_SCREEN.value
    assert aborted_task.result['abort']['screen']['labels'] == ['For you', 'Search']
    summary = report.summary_path.read_text(encoding='utf-8')
    assert '| Explore sponsored items | sponsored_items | unexpected_screen | aborted |' in summary
    assert 'No task was dispatched.' not in summary
    payload = _report_payload(tmp_path)
    assert payload['tasks'][0]['state'] == 'aborted'
    assert any(event.get('event') == 'run_aborted' and event.get('level') == 'error' for event in _events(report))


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
    monkeypatch.setattr(engine, 'run_search_task', fake('search'))
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

    search_frame = _snapshot(
        _box('Coins boost', 100, 100, 500, 150),
        _box('Search for what you love', 100, 260, 900, 300),
        _box('Earn by using keywords to explore items', 100, 305, 1000, 335),
        _box('GO', 980, 280, 1040, 320),
    )
    search_entry = _entry(enumerate_roster(search_frame, catalogue), 'search_for_what_you_love')
    result = engine._dispatch_entry(context, controller, search_entry)
    assert result.completed is True
    assert called[-1] == 'search'

    with pytest.raises(ValueError, match='no executor'):
        engine._dispatch_entry(context, controller, _entry(full_roster, 'coins_shortcut'))


def test_browse_dispatch_receives_the_foreground_probe_and_frame_recorder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_frame()
    context = _context(tmp_path, capture=_Capture(frame), catalogue=catalogue, corpus=corpus)
    assert context.controller is not None
    probes: list[bool | None] = []
    recorded: list[tuple[str, Snapshot]] = []
    warm_starts: list[bool] = []

    def probe() -> bool | None:
        probes.append(True)
        return True

    context.app_foreground = probe
    context.warm_start = lambda: warm_starts.append(True)
    context.record_foreground_loss = lambda key, snapshot: recorded.append((key, snapshot))

    def executor(*_args: Any, **kwargs: Any) -> _FakeResult:
        foreground = cast(Callable[[], bool | None], kwargs['app_foreground'])
        warm_start = cast(Callable[[], None], kwargs['warm_start'])
        recorder = cast(Callable[[str, Snapshot], None], kwargs['record_foreground_loss'])
        assert foreground is context.app_foreground
        assert warm_start is context.warm_start
        assert recorder is context.record_foreground_loss
        assert foreground() is True
        warm_start()
        recorder('sponsored_items', frame)
        return _FakeResult(completed=True)

    monkeypatch.setattr(engine, 'run_browse_task', executor)

    result = engine._dispatch_entry(context, context.controller, _entry(full_roster, 'sponsored_items'))

    assert result.completed is True
    assert probes == [True]
    assert warm_starts == [True]
    assert recorded == [('sponsored_items', frame)]


def test_surprise_dispatch_receives_the_round_grid_recorder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_frame()
    context = _context(tmp_path, capture=_Capture(frame), catalogue=catalogue, corpus=corpus)
    assert context.controller is not None
    recorded: list[tuple[int, Snapshot]] = []

    def recorder(index: int, snapshot: Snapshot) -> None:
        recorded.append((index, snapshot))

    def executor(*_args: Any, **kwargs: Any) -> _FakeResult:
        callback = kwargs['record_round_grid']
        callback(1, frame)
        return _FakeResult(completed=True)

    context.record_round_grid = recorder
    monkeypatch.setattr(engine, 'run_surprise_task', executor)

    result = engine._dispatch_entry(context, context.controller, _entry(full_roster, 'surprise_items'))

    assert result.completed is True
    assert recorded == [(1, frame)]


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


def test_signin_fails_when_the_streak_completed_but_the_boost_claim_did_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    """A successful streak must not hide an unverified boost check-in (#84)."""
    context = _context(
        tmp_path,
        capture=_Capture(_boost_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    entry = _entry(full_roster, 'boost_checkin')
    streak = ClaimResult(
        key='daily_checkin',
        outcome=SigninOutcome.COMPLETED,
        detail='streak claimed',
        verification='absent_control',
        recorded=True,
    )
    boost = ClaimResult(
        key='boost_checkin',
        outcome=SigninOutcome.UNVERIFIED,
        detail='the Coins boost page is empty (a load fault, not an empty day); the boost check-in was not attempted',
        verification='empty_boost',
    )
    monkeypatch.setattr(
        engine,
        'claim_daily_signins',
        lambda *args, **kwargs: SigninReport(streak=streak, boost=boost),
    )
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

    assert result.outcome == 'failed'
    assert result.completed is False
    assert result.failure is not None
    assert result.failure.code is FailureCode.TASK_UNVERIFIED
    assert result.verification == 'absent_control,empty_boost'
    assert 'empty' in result.detail
    assert 'boost_checkin: unverified' in result.detail


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
        foreground = True

        def __init__(self, serial: str, *, app_settle_seconds: float, settle: Any) -> None:
            assert serial == '192.168.8.65:5555'
            assert app_settle_seconds == 10.0
            self.warm_starts = 0

        def prepare(self) -> HygieneReport:
            return HygieneReport('192.168.8.65:5555', 'device', True, True, 'ready')

        def navigate(self, route: SurfaceRoute) -> None:
            navigated.append(route)

        def is_app_foreground(self) -> bool | None:
            return self.foreground

        def coins_shortcut_pinned(self) -> bool | None:
            return False

        def launch_coins_shortcut(self) -> str:
            return 'Starting: Intent'

        def clear_app_shortcuts(self) -> str:
            return 'Success'

        def warm_start(self) -> str:
            self.warm_starts += 1
            return 'Starting: Intent'

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
    assert context.app_foreground is not None
    assert context.app_foreground() is True
    assert context.warm_start is not None
    assert context.shortcut_present is not None
    assert context.shortcut_present() is False
    assert context.launch_shortcut is not None
    context.launch_shortcut()
    assert context.remove_shortcut is not None
    context.remove_shortcut()
    context.navigate(SurfaceRoute.SHORTCUT)
    assert navigated == [SurfaceRoute.SHORTCUT]
    assert cast(Any, context.hygiene).warm_starts == 0
    cast(Any, context.hygiene).foreground = False
    context.navigate(SurfaceRoute.SHORTCUT)
    assert navigated == [SurfaceRoute.SHORTCUT, SurfaceRoute.SHORTCUT]
    assert cast(Any, context.hygiene).warm_starts == 1
    # Unreadable focus is not a proven loss: the route still fires, but no
    # relaunch or settle is added.
    cast(Any, context.hygiene).foreground = None
    context.navigate(SurfaceRoute.SHORTCUT)
    assert navigated == [SurfaceRoute.SHORTCUT, SurfaceRoute.SHORTCUT, SurfaceRoute.SHORTCUT]
    assert cast(Any, context.hygiene).warm_starts == 1
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
    assert navigated == [SurfaceRoute.SHORTCUT, SurfaceRoute.SHORTCUT, SurfaceRoute.SHORTCUT, SurfaceRoute.WIDGET]


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
    assert len(report.tasks) == 1
    assert report.tasks[0].state == 'aborted'
    assert report.tasks[0].outcome == 'aborted'


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
    # Item taps cost less than a dwell, so cost order picks surprise items even
    # though the sponsored row was enumerated above it.
    assert entry.key == 'surprise_items'


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
    assert context.state.snapshot().last_balance == 1240.0


def test_a_balance_state_write_failure_does_not_hide_the_verified_final_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    assert context.controller is not None
    report = RunReport(tmp_path, 'run-1', budget_seconds=60.0, started_at=CAPTURED_AT)

    def broken(_balance: float | None) -> None:
        raise OSError('state volume read-only')

    monkeypatch.setattr(context.state, 'observe_balance', broken)
    caplog.set_level(logging.WARNING)

    engine._finish_on_coins_page(
        context,
        context.controller,
        report,
        ArtifactStore(report.runs_dir, report.run_id),
        _logger(),
    )

    assert report.returned_to_coins_page is True
    assert report.balance_after == 1240.0
    assert 'state_balance_failed' in caplog.text


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


def test_a_route_timeout_on_a_non_app_surface_does_not_invent_an_inline_roster(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _non_app_clock_frame()
    context = _context(tmp_path, capture=_Capture(frame), catalogue=catalogue, corpus=corpus)
    assert context.controller is not None
    monkeypatch.setattr(
        engine,
        'discover_roster',
        lambda *args, **kwargs: (_ for _ in ()).throw(StateTimeout('no app page')),
    )

    roster, error = engine._discover_or_fallback(context, context.controller)

    assert error is not None
    assert roster.empty
    assert roster.route is None
    assert roster.surface_variant is Surface.NON_APP
    assert roster.routes_taken == (SurfaceRoute.SHORTCUT,)
    assert roster.refresh_attempted is True


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

    def broken_complete(_night: object) -> None:
        raise OSError('state volume read-only')

    monkeypatch.setattr(context.state, 'record_night', broken_complete)

    report = engine.run_once(context)

    assert report.status is RunStatus.COMPLETED
    assert report.ok is True
    # The state write failed, so neither the report nor its summary may claim
    # the day was completed; the warning and event log carry the failure.
    assert report.night is None
    assert '**Night:**' not in report.summary_path.read_text(encoding='utf-8')
    events = _events(report)
    assert any(event['event'] == 'state_complete_failed' for event in events)
    assert not any(event['event'] == 'night_decided' for event in events)


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
    assert context.state.snapshot().last_balance == 1246.0
    assert context.state.snapshot().last_balance_verified is True
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
def test_the_day_state_replaces_a_stale_balance_with_the_final_verified_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    unreadable = _snapshot(
        _box('Coins', 100, 220, 300, 280),
        _box('Coins credits to redeem', 100, 330, 700, 380),
        _box('Daily coins', 100, 560, 500, 610),
    )
    context = _context(
        tmp_path,
        capture=_Capture(frame, unreadable, _coin_frame('80')),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.state.observe_balance(70.0)

    report = engine.run_once(context)

    assert report.balance_before is None
    assert report.balance_after == 80.0
    assert '**Balance:** unknown -> 80' in report.summary_path.read_text(encoding='utf-8')
    snapshot = context.state.snapshot()
    assert snapshot.last_balance == 80.0
    assert snapshot.last_balance_verified is True
    assert json.loads(context.state.path.read_text(encoding='utf-8'))['last_balance'] == 80.0


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
    assert context.state.snapshot().last_balance is None
    assert json.loads(context.state.path.read_text(encoding='utf-8'))['last_balance'] is None
    summary = report.summary_path.read_text(encoding='utf-8')
    assert '**Balance note:** the coins page did not render a readable balance before the first task' in summary


@pytest.mark.slow
def test_an_unreadable_final_balance_keeps_the_last_verified_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    unreadable = _snapshot(
        _box('Coins', 100, 220, 300, 280),
        _box('Coins credits to redeem', 100, 330, 700, 380),
        _box('Daily coins', 100, 560, 500, 610),
    )
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame('70'), unreadable),
        catalogue=catalogue,
        corpus=corpus,
    )

    report = engine.run_once(context)

    assert report.balance_before == 70.0
    assert report.balance_after is None
    snapshot = context.state.snapshot()
    assert snapshot.last_balance == 70.0
    assert snapshot.last_balance_verified is True


@pytest.mark.slow
def test_both_unreadable_reads_keep_the_last_verified_balance_from_an_earlier_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    """An unreadable run preserves the last verified value rather than inventing or erasing one (#94).

    The issue's ``null`` means no verified balance has ever been read, not that
    a run with no readable frame should erase a value an earlier run proved.
    """
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    unreadable = _snapshot(
        _box('Coins', 100, 220, 300, 280),
        _box('Coins credits to redeem', 100, 330, 700, 380),
        _box('Daily coins', 100, 560, 500, 610),
    )
    context = _context(
        tmp_path,
        capture=_Capture(frame, unreadable, unreadable),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.state.observe_balance(70.0)

    report = engine.run_once(context)

    assert report.balance_before is None
    assert report.balance_after is None
    snapshot = context.state.snapshot()
    assert snapshot.last_balance == 70.0
    assert snapshot.last_balance_verified is True
    assert json.loads(context.state.path.read_text(encoding='utf-8'))['last_balance'] == 70.0


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
    roster = replace(full_roster, entries=(), surface_variant=Surface.ROWS_ABSENT)

    def fake_discover(*args: Any, **kwargs: Any) -> Roster:
        on_dismiss = kwargs.get('on_dismiss')
        if on_dismiss is not None:
            on_dismiss(_dismiss('launch_modal'))
        return roster

    def fake_open(*args: Any, **kwargs: Any) -> BoostList:
        on_dismiss = kwargs.get('on_dismiss')
        if on_dismiss is not None:
            on_dismiss(_dismiss('observation_modal'))
        return BoostList(snapshot=_coin_frame(), route=DiscoveryRoute.BOOST, detail='test')

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


def test_run_once_wires_round_grid_frames_into_the_artifact_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    context = _context(tmp_path, capture=_Capture(frame), catalogue=catalogue, corpus=corpus)

    report = engine.run_once(context)

    assert context.record_round_grid is not None
    context.record_round_grid(1, frame)
    name = 'surprise-items-round-1-grid'
    assert report.shots[name] == f'shots/{name}.png'
    store = ArtifactStore(report.runs_dir, report.run_id)
    assert store.load(name).boxes == frame.boxes


def test_run_once_wires_foreground_loss_frames_into_the_artifact_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    frame = _boost_header_frame()
    _surface(monkeypatch, replace(full_roster, entries=()), frame)
    context = _context(tmp_path, capture=_Capture(frame), catalogue=catalogue, corpus=corpus)

    report = engine.run_once(context)

    assert context.record_foreground_loss is not None
    second_frame = _coin_frame()
    assert second_frame.boxes != frame.boxes
    context.record_foreground_loss('sponsored_items', frame)
    context.record_foreground_loss('sponsored_items', second_frame)
    first = 'browse-sponsored_items-foreground-lost'
    second = 'browse-sponsored_items-foreground-lost-2'
    assert report.shots[first] == f'shots/{first}.png'
    assert report.shots[second] == f'shots/{second}.png'
    store = ArtifactStore(report.runs_dir, report.run_id)
    assert store.load(first).boxes == frame.boxes
    assert store.load(second).boxes == second_frame.boxes


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
    assert len(report.tasks) == 1
    assert report.tasks[0].state == 'aborted'
    assert report.tasks[0].code == FailureCode.DEVICE_OFFLINE.value
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

    _seed_other_families(context, catalogue, roster)
    report = engine.run_once(context)

    assert len(attempts) == 2
    # 1.2s of aftermath capture (3 frames x 0.4s) plus the taxonomy's 1s retry pause.
    assert clock.value == 2.2
    assert report.passes == 2
    assert [task.completed for task in report.tasks] == [False, True]
    assert report.tasks[1].code is None
    assert report.status is RunStatus.COMPLETED
    # The row's last dispatch completed, so the earlier failed attempt is not
    # unclaimed work and the night is genuinely complete (#143).
    snapshot = context.state.snapshot()
    assert snapshot.completed_night is True
    assert snapshot.last_night is not None
    assert snapshot.last_night.outcome is NightOutcome.EXHAUSTED


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
    assert report.passes == 2
    assert report.status is RunStatus.FAILED
    assert report.ok is False
    assert report.tasks[-1].code == FailureCode.TASK_UNVERIFIED.value
    assert report.detail is not None
    assert "'Take the coins quiz'" in report.detail
    assert 'the row did not move' in report.detail
    assert 'no dispatchable row remains' not in report.detail
    summary = report.summary_path.read_text(encoding='utf-8')
    assert 'task_unverified' in summary
    assert '**Night:** open' in summary
    assert context.state.snapshot().completed_night is False


@pytest.mark.slow
def test_all_retryable_failures_leave_the_night_open_for_a_later_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    frame = _boost_frame('coins_quiz')
    _surface(monkeypatch, roster, frame)
    now = [CAPTURED_AT]
    first = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    first.wall_clock = lambda: now[0]
    attempts: list[str] = []

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        attempts.append(entry.key or '')
        if len(attempts) == 1:
            return EngineResult(
                'failed',
                'question_not_arrived',
                'the quiz question never arrived',
                False,
                {'key': entry.key},
                failure=failure(FailureCode.TASK_UNVERIFIED, detail='question_not_arrived'),
                acted=True,
            )
        _context.state.record(entry.key or '')
        return EngineResult('completed', 'progress_badge', 'the row moved', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    _seed_other_families(first, catalogue, roster)
    first_report = engine.run_once(first)

    snapshot = first.state.snapshot()
    assert first_report.status is RunStatus.FAILED
    assert first_report.ok is False
    assert first_report.failure_code == FailureCode.TASK_UNVERIFIED.value
    assert first_report.detail is not None
    assert "'Take the coins quiz'" in first_report.detail
    assert 'the quiz question never arrived' in first_report.detail
    assert first_report.tasks[0].completed is False
    assert first_report.blacklist == {}
    assert snapshot.completed_night is False
    assert snapshot.acted_unverified == ('coins_quiz',)
    assert snapshot.blacklist == {}
    assert snapshot.last_night is not None
    assert snapshot.last_night.outcome is NightOutcome.RETRYABLE_FAILURES
    assert snapshot.last_night.dispatched == 1
    assert snapshot.last_night.completed == 0
    assert snapshot.last_night.retryable_failures == 1
    summary = first_report.summary_path.read_text(encoding='utf-8')
    assert '**Night:** open' in summary
    assert '1 retryable task failure(s) of 1 dispatch(es) remain' in summary

    # The same local day, a later run: the day is still open, so it dispatches
    # the row again instead of treating the failed night as finished.
    now[0] = CAPTURED_AT + timedelta(minutes=5)
    second = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    second.wall_clock = lambda: now[0]

    _seed_other_families(second, catalogue, roster)
    second_report = engine.run_once(second)

    assert len(attempts) == 2
    assert second_report.tasks[0].completed is True
    final = second.state.snapshot()
    assert final.completed_night is True
    assert final.acted_unverified == ()
    assert final.last_night is not None
    assert final.last_night.outcome is NightOutcome.EXHAUSTED
    assert '**Night:** complete' in second_report.summary_path.read_text(encoding='utf-8')


@pytest.mark.slow
def test_a_cross_run_acted_failure_rechecks_before_tapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    frame = _boost_frame('coins_quiz')
    _surface(monkeypatch, roster, frame)
    now = [CAPTURED_AT]
    first = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    first.wall_clock = lambda: now[0]

    def fail_acted(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        return EngineResult(
            'failed',
            'no_observation',
            'the row was tapped but no fresh frame proved it',
            False,
            {'key': entry.key},
            failure=failure(FailureCode.TASK_UNVERIFIED, detail='no_observation'),
            acted=True,
        )

    monkeypatch.setattr(engine, '_dispatch_entry', fail_acted)
    _seed_other_families(first, catalogue, roster)
    engine.run_once(first)

    snapshot = first.state.snapshot()
    assert snapshot.completed_night is False
    assert snapshot.acted_unverified == ('coins_quiz',)

    # A later run starts from a claimable roster, but the dispatch-time
    # re-check reads a complete boost list without the quiz row.
    now[0] = CAPTURED_AT + timedelta(minutes=5)
    second = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    second.wall_clock = lambda: now[0]
    dispatches: list[str] = []

    def must_not_dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        dispatches.append(entry.key or '')
        raise AssertionError('the claimed row must not be dispatched')

    monkeypatch.setattr(engine, '_dispatch_entry', must_not_dispatch)
    recheck_frames = [_boost_frame('coins_quiz'), _boost_frame('notifications'), _boost_frame('coins_quiz')]
    opens: list[int] = []

    def open_boost(*args: Any, **kwargs: Any) -> BoostList:
        snapshot_frame = recheck_frames[min(len(opens), len(recheck_frames) - 1)]
        opens.append(1)
        return BoostList(snapshot=snapshot_frame, route=DiscoveryRoute.BOOST, detail='test')

    monkeypatch.setattr(engine, 'open_boost_list', open_boost)

    _seed_other_families(second, catalogue, roster)
    report = engine.run_once(second)

    assert dispatches == []
    assert len(opens) >= 2
    assert report.tasks[0].outcome == 'skipped'
    assert report.tasks[0].verification == 'already_done'
    final = second.state.snapshot()
    assert final.acted_unverified == ('coins_quiz',)
    assert final.completed_night is True
    assert '**Night:** complete' in report.summary_path.read_text(encoding='utf-8')


@pytest.mark.slow
def test_a_proven_acted_row_stays_marked_so_a_third_run_rechecks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    wanted = _entry(full_roster, 'coins_quiz')
    roster = replace(full_roster, entries=(wanted,))
    frame = _boost_frame('coins_quiz')
    _surface(monkeypatch, roster, frame)
    now = [CAPTURED_AT]

    def fail_acted(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        return EngineResult(
            'failed',
            'no_observation',
            'the row was tapped but no fresh frame proved it',
            False,
            {'key': entry.key},
            failure=failure(FailureCode.TASK_UNVERIFIED, detail='no_observation'),
            acted=True,
        )

    monkeypatch.setattr(engine, '_dispatch_entry', fail_acted)
    first = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    first.wall_clock = lambda: now[0]

    first_report = engine.run_once(first)
    assert first_report.tasks[0].code == FailureCode.TASK_UNVERIFIED.value
    assert first.state.snapshot().acted_unverified == ('coins_quiz',)

    dispatches: list[str] = []

    def must_not_dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        dispatches.append(entry.key or '')
        raise AssertionError('the proven-already-done row must not be dispatched')

    monkeypatch.setattr(engine, '_dispatch_entry', must_not_dispatch)
    base = _boost_frame('coins_quiz')
    complete = replace(base, boxes=(*base.boxes, _box('6/6', 700, 280, 760, 320)))
    opens: list[int] = []

    def open_boost(*args: Any, **kwargs: Any) -> BoostList:
        snapshot_frame = base if len(opens) % 2 == 0 else complete
        opens.append(1)
        return BoostList(snapshot=snapshot_frame, route=DiscoveryRoute.BOOST, detail='test')

    monkeypatch.setattr(engine, 'open_boost_list', open_boost)

    now[0] = CAPTURED_AT + timedelta(minutes=5)
    second = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    second.wall_clock = lambda: now[0]

    second_report = engine.run_once(second)

    assert dispatches == []
    assert second.state.snapshot().acted_unverified == ('coins_quiz',)
    assert second_report.tasks[0].verification == 'already_done'

    opens.clear()
    now[0] = CAPTURED_AT + timedelta(minutes=10)
    third = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    third.wall_clock = lambda: now[0]

    third_report = engine.run_once(third)

    assert dispatches == []
    assert third.state.snapshot().acted_unverified == ('coins_quiz',)
    assert third_report.tasks[0].verification == 'already_done'


@pytest.mark.slow
def test_an_unreadable_acted_recheck_stays_retryable_without_tapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    frame = _boost_frame('coins_quiz')
    _surface(monkeypatch, roster, frame)
    now = [CAPTURED_AT]
    first = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    first.wall_clock = lambda: now[0]

    def fail_acted(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        return EngineResult(
            'failed',
            'no_observation',
            'the row was tapped but no fresh frame proved it',
            False,
            {'key': entry.key},
            failure=failure(FailureCode.TASK_UNVERIFIED, detail='no_observation'),
            acted=True,
        )

    monkeypatch.setattr(engine, '_dispatch_entry', fail_acted)
    engine.run_once(first)

    now[0] = CAPTURED_AT + timedelta(minutes=5)
    second = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    second.wall_clock = lambda: now[0]
    dispatches: list[str] = []

    def must_not_dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        dispatches.append(entry.key or '')
        raise AssertionError('the unreadable row must not be dispatched')

    monkeypatch.setattr(engine, '_dispatch_entry', must_not_dispatch)
    recheck_frames = [_boost_frame('coins_quiz'), _coin_frame()]
    opens: list[int] = []

    def open_boost(*args: Any, **kwargs: Any) -> BoostList:
        snapshot_frame = recheck_frames[min(len(opens), len(recheck_frames) - 1)]
        opens.append(1)
        route = DiscoveryRoute.BOOST if len(opens) == 1 else DiscoveryRoute.INLINE
        return BoostList(snapshot=snapshot_frame, route=route, detail='test')

    monkeypatch.setattr(engine, 'open_boost_list', open_boost)

    report = engine.run_once(second)

    assert dispatches == []
    assert report.tasks[0].outcome == 'failed'
    assert report.tasks[0].verification == 'acted_recheck_unavailable'
    final = second.state.snapshot()
    assert final.acted_unverified == ('coins_quiz',)
    assert final.completed_night is False
    assert '**Night:** open' in report.summary_path.read_text(encoding='utf-8')


def test_an_acted_recheck_that_cannot_read_the_surface_is_not_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    caplog: pytest.LogCaptureFixture,
) -> None:
    frame = _boost_frame('coins_quiz')
    context = _context(tmp_path, capture=_Capture(frame, _coin_frame()), catalogue=catalogue, corpus=corpus)
    entry = _entry(full_roster, 'coins_quiz')

    def broken(*args: Any, **kwargs: Any) -> BoostList:
        raise StateTimeout('the boost page did not appear')

    monkeypatch.setattr(engine, 'open_boost_list', broken)
    caplog.set_level(logging.WARNING)
    assert context.controller is not None

    readable, fresh = engine._recheck_acted_row(context, context.controller, entry)

    assert readable is False
    assert fresh is None
    assert 'acted_recheck_failed' in caplog.text


@pytest.mark.parametrize('error', [RunInterrupted('stop requested'), AppLoggedOut('signed out')])
def test_an_acted_recheck_re_raises_run_level_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    error: Exception,
) -> None:
    frame = _boost_frame('coins_quiz')
    context = _context(tmp_path, capture=_Capture(frame, _coin_frame()), catalogue=catalogue, corpus=corpus)
    entry = _entry(full_roster, 'coins_quiz')

    def broken(*args: Any, **kwargs: Any) -> BoostList:
        raise error

    monkeypatch.setattr(engine, 'open_boost_list', broken)
    assert context.controller is not None

    with pytest.raises(type(error)):
        engine._recheck_acted_row(context, context.controller, entry)


def test_a_completed_progress_badge_makes_an_acted_row_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    base = _boost_frame('coins_quiz')
    complete = replace(base, boxes=(*base.boxes, _box('6/6', 700, 280, 760, 320)))
    context = _context(tmp_path, capture=_Capture(base, _coin_frame()), catalogue=catalogue, corpus=corpus)
    entry = _entry(full_roster, 'coins_quiz')
    monkeypatch.setattr(
        engine,
        'open_boost_list',
        lambda *args, **kwargs: BoostList(snapshot=complete, route=DiscoveryRoute.BOOST, detail='test'),
    )
    assert context.controller is not None

    readable, fresh = engine._recheck_acted_row(context, context.controller, entry)

    assert readable is True
    assert fresh is None


def test_an_acted_failure_is_marked_before_a_signed_out_guard_stops_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_logged_out_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    entry = _entry(full_roster, 'coins_quiz')

    def fail_acted(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        return EngineResult(
            'failed',
            'no_observation',
            'the row acted but no fresh frame proved it',
            False,
            {'key': entry.key},
            failure=failure(FailureCode.TASK_UNVERIFIED, detail='no_observation'),
            acted=True,
        )

    monkeypatch.setattr(engine, '_dispatch_entry', fail_acted)
    assert context.controller is not None

    with pytest.raises(AppLoggedOut):
        engine._dispatch_with_retry(context, context.controller, entry)

    assert context.state.is_acted_unverified('coins_quiz') is True


def test_a_chip_not_cleared_failure_does_not_become_a_terminal_acted_marker(
    tmp_path: Path,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(tmp_path, capture=_Capture(_coin_frame()), catalogue=catalogue, corpus=corpus)
    entry = _entry(full_roster, 'coins_quiz')
    result = EngineResult(
        'failed',
        'chip_not_cleared',
        'the row disappeared without a cleared dwell chip; rotation is not completion',
        False,
        {'key': entry.key},
        failure=failure(FailureCode.TASK_UNVERIFIED, detail='chip_not_cleared'),
        acted=True,
    )

    engine._remember_acted_result(context, entry, result)

    assert context.state.is_acted_unverified('coins_quiz') is False


def test_acted_marker_write_failures_are_logged_without_failing_the_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    caplog: pytest.LogCaptureFixture,
) -> None:
    frame = _boost_frame('coins_quiz')
    context = _context(tmp_path, capture=_Capture(frame, _coin_frame()), catalogue=catalogue, corpus=corpus)
    entry = _entry(full_roster, 'coins_quiz')
    result = EngineResult(
        'failed',
        'no_observation',
        'the row was tapped but no fresh frame proved it',
        False,
        {'key': entry.key},
        failure=failure(FailureCode.TASK_UNVERIFIED, detail='no_observation'),
        acted=True,
    )

    def broken(_key: str) -> None:
        raise OSError('state volume read-only')

    monkeypatch.setattr(context.state, 'mark_acted_unverified', broken)
    monkeypatch.setattr(context.state, 'clear_acted_unverified', broken)
    caplog.set_level(logging.WARNING)

    engine._remember_acted_result(context, entry, result)
    engine._clear_acted_marker(context, 'coins_quiz')
    engine._clear_acted_marker(context, None)

    assert caplog.text.count('state_acted_marker_failed') == 2


@pytest.mark.slow
def test_a_mixed_night_with_one_retryable_failure_stays_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    """A completed row does not buy the day: the retryable row is still unclaimed work."""
    wanted = ('coins_quiz', 'notifications')
    roster = replace(full_roster, entries=tuple(_entry(full_roster, key) for key in wanted))
    frame = _boost_frame(*wanted)
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        if entry.key == 'coins_quiz':
            _context.state.record(entry.key)
            return EngineResult('completed', 'progress_badge', 'the row moved', True, {'key': entry.key})
        return EngineResult(
            'failed',
            'no_observation',
            'the notifications row was not observable',
            False,
            {'key': entry.key},
            failure=failure(FailureCode.TASK_UNVERIFIED, detail='no_observation'),
            acted=True,
        )

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    _seed_other_families(context, catalogue, roster)
    report = engine.run_once(context)

    snapshot = context.state.snapshot()
    assert report.status is RunStatus.FAILED
    assert report.ok is False
    assert report.failure_code == FailureCode.TASK_UNVERIFIED.value
    assert report.detail is not None
    assert "'Turn on notifications'" in report.detail
    assert 'the notifications row was not observable' in report.detail
    assert [task.completed for task in report.tasks] == [True, False]
    assert snapshot.completed_night is False
    assert snapshot.last_night is not None
    assert snapshot.last_night.outcome is NightOutcome.RETRYABLE_FAILURES
    assert snapshot.last_night.dispatched == 2
    assert snapshot.last_night.completed == 1
    assert snapshot.last_night.retryable_failures == 1
    summary = report.summary_path.read_text(encoding='utf-8')
    assert '**Night:** open' in summary
    assert '1 retryable task failure(s) of 2 dispatch(es) remain' in summary


@pytest.mark.slow
def test_a_night_with_only_terminal_outcomes_still_completes_the_day(
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

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        return EngineResult(
            'unavailable',
            'unavailable',
            'the app reports the row is unavailable today',
            False,
            {'key': entry.key},
        )

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    _seed_other_families(context, catalogue, roster)
    report = engine.run_once(context)

    snapshot = context.state.snapshot()
    assert report.status is RunStatus.COMPLETED
    assert snapshot.completed_night is True
    assert snapshot.last_night is not None
    assert snapshot.last_night.outcome is NightOutcome.EXHAUSTED
    assert snapshot.last_night.dispatched == 1
    assert snapshot.last_night.completed == 0
    assert snapshot.last_night.retryable_failures == 0
    assert '**Night:** complete' in report.summary_path.read_text(encoding='utf-8')


@pytest.mark.slow
def test_a_failed_task_at_its_daily_limit_is_not_unclaimed_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    """A card already recorded to its limit is not claimable just because the return failed."""
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    frame = _boost_frame('coins_quiz')
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        # Both rounds/claims are already recorded; only the return route fails.
        _context.state.record(entry.key or '')
        return EngineResult(
            'failed',
            'return_failed',
            'the card was claimed but the run could not return to the coins page',
            False,
            {'key': entry.key},
            failure=failure(FailureCode.TASK_UNVERIFIED, detail='return_failed'),
            acted=True,
        )

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    _seed_other_families(context, catalogue, roster)
    report = engine.run_once(context)

    assert report.tasks[0].outcome == 'failed'
    assert report.status is RunStatus.COMPLETED
    assert report.ok is True
    assert report.failure is None
    assert report.detail == 'no dispatchable row remains'
    snapshot = context.state.snapshot()
    assert snapshot.completed_night is True
    assert snapshot.last_night is not None
    assert snapshot.last_night.outcome is NightOutcome.EXHAUSTED


def test_a_claimability_read_failure_keeps_the_failed_row_visible(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unreadable day counter must not let finalisation hide a failed row or escape."""
    report = RunReport(tmp_path / 'runs', 'run-1', budget_seconds=60.0, started_at=CAPTURED_AT)
    report.record_task(
        title='Take the coins quiz',
        key='coins_quiz',
        category='quiz',
        outcome='failed',
        verification='no_change',
        detail='the row did not move',
        duration_seconds=0.0,
        completed=False,
        result={},
        failure=failure(FailureCode.TASK_UNVERIFIED, detail='the row did not move'),
    )

    class _BrokenState:
        def count(self, key: str) -> int:
            raise OSError(f'{key} state is unreadable')

    caplog.set_level(logging.WARNING)

    failures = engine._claimable_task_failures(report, cast(Any, _BrokenState()), catalogue)

    assert [task.key for task in failures] == ['coins_quiz']
    assert 'state_claimability_failed' in caplog.text


def _policy_refused_rows(catalogue: TaskCatalogue) -> tuple[tuple[str, str], ...]:
    """Derive one row per deny rule from the live policy.

    The title is the rule's first multi-word pattern. A single generic word
    such as ``order`` or ``game`` is trusted only in the title, so it is still
    safe there; the subtitle stays neutral and never carries a label the rule
    is not allowed to match. Both assertions below make a family whose status
    changes fail at construction instead of silently invalidating a typed list.
    """
    rows: list[tuple[str, str]] = []
    for rule in catalogue.denied:
        title = next(
            (pattern for pattern in rule.label_patterns if ' ' in pattern),
            rule.label_patterns[0],
        )
        subtitle = 'A neutral subtitle'
        decision = catalogue.decide(title, subtitle=subtitle)
        assert decision.denied, f'{rule.key!r} no longer refuses its own pattern {title!r}: {decision.as_dict()!r}'
        assert decision.rule == rule.key, f'{title!r} is now refused by {decision.rule!r}, not {rule.key!r}'
        rows.append((title, subtitle))
    return tuple(rows)


def _roster_frame(rows: tuple[tuple[str, str], ...]) -> Snapshot:
    """Build a readable boost frame from ``(title, subtitle)`` rows."""
    boxes: list[TextBox] = [_box('Coins boost', 100, 100, 500, 150)]
    for index, (title, subtitle) in enumerate(rows):
        top = 260 + index * 150
        boxes.append(_box(title, 100, top, 900, top + 40))
        boxes.append(_box(subtitle, 100, top + 45, 900, top + 75))
        boxes.append(_box('GO', 980, top + 20, 1040, top + 60))
    # One row per deny rule is listed here, so the frame grows with the
    # catalogue instead of clipping the last rows on a taller policy.
    size = (1200, max(2000, 260 + len(rows) * 150 + 100))
    return _snapshot(*boxes, size=size)


@pytest.mark.slow
def test_a_roster_with_only_policy_excluded_rows_still_completes_the_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    """Exclusion is success: a day whose only unclaimed rows are refused is finished."""
    excluded = _policy_refused_rows(catalogue)
    frame = _roster_frame(excluded)
    roster = replace(
        enumerate_roster(frame, catalogue, route=DiscoveryRoute.BOOST),
        surface_variant=Surface.BOOST_PAGE,
        routes_taken=(SurfaceRoute.SHORTCUT,),
    )
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    dispatched: list[str] = []

    def dispatch(*args: Any, **kwargs: Any) -> EngineResult:
        dispatched.append('called')
        return EngineResult('completed', 'fake', 'unexpected', True, {})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    _seed_other_families(context, catalogue, roster)
    report = engine.run_once(context)

    assert dispatched == []
    assert report.tasks == []
    assert report.status is RunStatus.COMPLETED
    assert report.ok is True
    assert report.failure is None
    assert {skip.title for skip in report.skipped} == {title for title, _ in excluded}
    assert all(skip.reason == 'policy' for skip in report.skipped)
    snapshot = context.state.snapshot()
    assert snapshot.completed_night is True
    assert snapshot.last_night is not None
    assert snapshot.last_night.outcome is NightOutcome.EXHAUSTED


@pytest.mark.slow
def test_a_recently_viewed_row_is_dispatched_instead_of_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    """#116 made the measured browse-and-wait row a real task, not a policy refusal."""
    spec = catalogue.allowed_by_key('recently_viewed')
    assert spec.label_patterns[0] == 'Browse recently viewed items'
    decision = catalogue.decide(spec.label_patterns[0])
    assert decision.allowed
    assert decision.spec is not None
    assert decision.spec.key == 'recently_viewed'
    assert decision.spec.category is TaskCategory.BROWSE_WAIT

    rows = (*_policy_refused_rows(catalogue), (spec.label_patterns[0], 'Pick up where you left off'))
    frame = _roster_frame(rows)
    roster = replace(
        enumerate_roster(frame, catalogue, route=DiscoveryRoute.BOOST),
        surface_variant=Surface.BOOST_PAGE,
        routes_taken=(SurfaceRoute.SHORTCUT,),
    )
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    dispatched: list[str] = []

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        dispatched.append(entry.key or '')
        _context.state.record(entry.key or '')
        return EngineResult('completed', 'fake', 'done', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    _seed_other_families(context, catalogue, roster)
    report = engine.run_once(context)

    assert dispatched == ['recently_viewed']
    assert [task.key for task in report.tasks] == ['recently_viewed']
    assert report.tasks[0].category == TaskCategory.BROWSE_WAIT.value
    assert report.tasks[0].completed is True
    assert report.status is RunStatus.COMPLETED
    assert report.ok is True
    assert report.failure is None
    assert all(skip.reason == 'policy' for skip in report.skipped)
    snapshot = context.state.snapshot()
    assert snapshot.completed_night is True
    assert snapshot.last_night is not None
    assert snapshot.last_night.outcome is NightOutcome.EXHAUSTED


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
    assert len(report.tasks) == 1
    assert report.tasks[0].state == 'aborted'
    assert report.tasks[0].code == FailureCode.POPUP_UNCLOSABLE.value


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
    assert report.status is RunStatus.BUDGET
    assert report.detail is not None
    assert 'pass budget' in report.detail
    assert SurfaceRoute.SHORTCUT in cast(_Navigate, context.navigate).routes


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
    assert report.status is RunStatus.FAILED
    assert report.ok is False
    assert report.detail is not None
    assert "'Daily check-in'" in report.detail
    assert context.state.snapshot().completed_night is False


@pytest.mark.slow
def test_a_completed_sign_in_key_does_not_hide_the_other_keys_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    """The combined sign-in row owns two keys; one completed key cannot hide the other's failure."""
    roster = replace(full_roster, entries=(_entry(full_roster, 'boost_checkin'),))
    frame = _boost_frame('daily_checkin')
    _surface(monkeypatch, roster, frame)
    notifier = _Notifier()
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        notifier=notifier,
    )
    streak = ClaimResult(
        key='daily_checkin',
        outcome=SigninOutcome.UNVERIFIED,
        detail='the streak was tapped but no fresh frame proved it',
        verification='no_evidence',
        tap=TapRecord(
            index=1,
            label='Collect',
            reason='claim the daily streak',
            requested=(100, 200),
            clamped=(100, 200),
            jitter=(0, 0),
            point=(100, 200),
            suppressed=False,
        ),
    )
    boost = ClaimResult(
        key='boost_checkin',
        outcome=SigninOutcome.COMPLETED,
        detail='the boost row was claimed and recorded',
        verification='claimed_marker',
        recorded=True,
    )
    claims = SigninReport(streak=streak, boost=boost)

    def claim(*args: Any, **kwargs: Any) -> SigninReport:
        context.state.record('boost_checkin')
        return claims

    monkeypatch.setattr(engine, 'claim_daily_signins', claim)

    _seed_other_families(context, catalogue, roster, keep={'daily_checkin', 'boost_checkin'})
    report = engine.run_once(context)

    assert report.tasks[0].key == 'boost_checkin'
    assert report.tasks[0].outcome == 'failed'
    assert report.status is RunStatus.FAILED
    assert report.ok is False
    assert report.detail is not None
    assert "'Daily check-in'" in report.detail
    assert 'daily_checkin' in report.detail
    assert 'no fresh frame proved it' in report.detail
    assert context.state.count('boost_checkin') == 1
    assert context.state.count('daily_checkin') == 0
    snapshot = context.state.snapshot()
    assert snapshot.completed_night is False
    assert snapshot.last_night is not None
    assert snapshot.last_night.outcome is NightOutcome.INCOMPLETE_ROSTER
    assert snapshot.last_night.retryable_failures == 1
    assert len(notifier.messages) == 1
    assert 'daily_checkin' in notifier.messages[0].body
    assert 'no fresh frame proved it' in notifier.messages[0].body


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
    assert len(report.tasks) == 1
    assert report.tasks[0].state == 'aborted'
    assert report.tasks[0].code == FailureCode.APP_LOGGED_OUT.value


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


@pytest.mark.slow
def test_a_row_that_vanished_is_dispatched_again_in_a_second_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    """A stale-roster skip is not terminal: pass two claims the row that moved."""
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    frame = _boost_frame('coins_quiz')
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    attempts: list[str] = []

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        attempts.append(entry.key or '')
        if len(attempts) == 1:
            return EngineResult(
                'skipped',
                'row_vanished',
                'the row was absent from the frame; the roster entry is stale',
                False,
                {'key': entry.key},
            )
        _context.state.record(entry.key or '')
        return EngineResult('completed', 'progress_badge', 'the second pass claimed it', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    _seed_other_families(context, catalogue, roster)
    report = engine.run_once(context)

    assert attempts == ['coins_quiz', 'coins_quiz']
    assert report.passes == 2
    assert [task.outcome for task in report.tasks] == ['skipped', 'completed']
    assert report.tasks[1].completed is True
    assert report.attempts == {'coins_quiz': 2}
    assert report.blacklist == {}
    assert report.status is RunStatus.COMPLETED
    assert context.state.snapshot().completed_night is True
    summary = report.summary_path.read_text(encoding='utf-8')
    assert '**Passes:** 2 of 12' in summary
    assert '**Attempts per task:** coins_quiz: 2 (blacklist after 2 failed attempt(s) per task)' in summary
    assert '**Blacklist:** none' in summary


@pytest.mark.slow
def test_a_repeatedly_failing_task_is_blacklisted_and_the_rest_of_the_roster_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    wanted = ('coins_quiz', 'notifications')
    roster = replace(full_roster, entries=tuple(_entry(full_roster, key) for key in wanted))
    frame = _boost_frame(*wanted)
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    attempts: list[str] = []

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        attempts.append(entry.key or '')
        if entry.key == 'coins_quiz':
            return EngineResult(
                'failed',
                'no_change',
                'the quiz row did not move',
                False,
                {'key': entry.key},
                failure=failure(FailureCode.TASK_UNVERIFIED, detail='the quiz row did not move'),
            )
        _context.state.record(entry.key or '')
        return EngineResult('completed', 'fake', 'done', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    _seed_other_families(context, catalogue, roster)
    report = engine.run_once(context)

    assert attempts == ['coins_quiz', 'notifications', 'coins_quiz']
    assert report.attempts['coins_quiz'] == 2
    assert report.blacklist == {'coins_quiz': 2}
    assert [task.key for task in report.tasks] == ['coins_quiz', 'notifications', 'coins_quiz']
    assert [task.completed for task in report.tasks] == [False, True, False]
    blacklisted = [skip for skip in report.skipped if skip.key == 'coins_quiz']
    assert [skip.reason for skip in blacklisted] == ['blacklisted after 2 attempts']
    assert context.state.is_blacklisted('coins_quiz') is True
    assert context.state.blacklist_attempts('coins_quiz') == 2
    snapshot = context.state.snapshot()
    assert snapshot.completed_night is False
    assert snapshot.last_night is not None
    assert snapshot.last_night.outcome is NightOutcome.RETRYABLE_FAILURES
    summary = report.summary_path.read_text(encoding='utf-8')
    assert 'blacklisted after 2 attempts' in summary
    assert '**Blacklist:** coins_quiz: blacklisted after 2 attempt(s)' in summary
    assert (
        '**Attempts per task:** coins_quiz: 2, notifications: 1 (blacklist after 2 failed attempt(s) per task)'
        in summary
    )


@pytest.mark.slow
def test_an_unknown_surface_after_a_task_restarts_from_the_shortcut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    wanted = ('coins_quiz', 'notifications')
    roster = replace(full_roster, entries=tuple(_entry(full_roster, key) for key in wanted))
    good = _boost_frame(*wanted)
    remaining = _boost_frame('notifications')
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: roster)
    opens: list[int] = []

    def open_boost(*args: Any, **kwargs: Any) -> BoostList:
        index = len(opens)
        opens.append(1)
        snapshot = good if index == 0 else (_home_frame() if index == 1 else remaining)
        return BoostList(snapshot=snapshot, route=DiscoveryRoute.BOOST, detail='test')

    monkeypatch.setattr(engine, 'open_boost_list', open_boost)
    context = _context(
        tmp_path,
        capture=_Capture(good, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    order: list[str] = []

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        order.append(entry.key or '')
        _context.state.record(entry.key or '')
        return EngineResult('completed', 'fake', 'done', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    _seed_other_families(context, catalogue, roster)
    report = engine.run_once(context)

    assert order == ['coins_quiz', 'notifications']
    assert report.passes == 2
    assert SurfaceRoute.SHORTCUT in cast(_Navigate, context.navigate).routes
    events = _events(report)
    assert any(
        event.get('event') == 'surface_restart' and 'could not be read' in str(event.get('reason')) for event in events
    )
    assert context.state.snapshot().completed_night is True


@pytest.mark.slow
def test_a_blacklist_from_an_earlier_same_day_run_is_still_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    wanted = ('coins_quiz', 'notifications')
    roster = replace(full_roster, entries=tuple(_entry(full_roster, key) for key in wanted))
    frame = _boost_frame(*wanted)
    _surface(monkeypatch, roster, frame)
    notifier = _Notifier()
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
        notifier=notifier,
    )
    context.state.blacklist_task('coins_quiz', 2)
    dispatched: list[str] = []

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        dispatched.append(entry.key or '')
        _context.state.record(entry.key or '')
        return EngineResult('completed', 'fake', 'done', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert dispatched == ['notifications']
    assert report.blacklist == {'coins_quiz': 2}
    assert any(skip.key == 'coins_quiz' and skip.reason == 'blacklisted after 2 attempts' for skip in report.skipped)
    assert report.status is RunStatus.FAILED
    assert report.ok is False
    assert report.failure_code == FailureCode.TASK_UNVERIFIED.value
    assert report.detail is not None
    assert "'coins_quiz'" in report.detail
    assert 'blacklisted after 2 attempt(s)' in report.detail
    assert len(notifier.messages) == 1
    assert "'coins_quiz'" in notifier.messages[0].body
    assert 'blacklisted after 2 attempt(s)' in notifier.messages[0].body
    assert context.state.snapshot().completed_night is False


def test_pass_restart_reasons_cover_unreadable_unknown_and_empty_surfaces(full_roster: Roster) -> None:
    assert engine._pass_restart_reason(None, None) == 'the roster was not read'
    assert (
        engine._pass_restart_reason(replace(full_roster, entries=()), None)
        == 'the task surface has not been read since the last attempt'
    )
    assert (
        engine._pass_restart_reason(replace(full_roster, surface_variant=Surface.UNKNOWN), None)
        == 'the task surface has not been read since the last attempt'
    )
    assert (
        engine._pass_restart_reason(replace(full_roster, surface_variant=Surface.EMPTY_BOOST), None)
        == 'the task surface has not been read since the last attempt'
    )


def test_a_pass_restarts_when_a_task_title_has_no_card_structure(full_roster: Roster) -> None:
    roster = replace(full_roster, entries=(), card_structure_failures=1)
    observation = RosterObservation(sequence=1, timestamp=CAPTURED_AT, rows=(), roster=roster)

    assert engine._pass_restart_reason(roster, observation) == (
        'surface_incomplete: the roster read was incomplete because 1 task title(s) had no readable card structure'
    )
    readable = replace(roster, entries=full_roster.entries)
    assert engine._pass_restart_reason(readable, observation) is None


def test_stuck_row_reason_ignores_an_already_claimed_row(
    tmp_path: Path,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(tmp_path, capture=_Capture(_all_claimed_boost_frame()), catalogue=catalogue, corpus=corpus)
    claimed = replace(_entry(full_roster, 'notifications'), action=None, claimed=True)
    roster = replace(full_roster, entries=(claimed,))

    assert (
        engine._stuck_row_reason(
            roster,
            context.state,
            settled=set(),
            blocked=set(),
            blacklisted={},
        )
        is None
    )


def test_stuck_row_reason_skips_unsupported_settled_and_limited_rows(
    tmp_path: Path,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(tmp_path, capture=_Capture(_boost_frame()), catalogue=catalogue, corpus=corpus)
    unsupported = replace(full_roster, entries=(_entry(full_roster, 'coins_shortcut'),))
    assert (
        engine._stuck_row_reason(
            unsupported,
            context.state,
            settled=set(),
            blocked=set(),
            blacklisted={},
        )
        is None
    )

    quiz = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    assert engine._stuck_row_reason(quiz, context.state, settled={'coins_quiz'}, blocked=set(), blacklisted={}) is None
    assert (
        engine._stuck_row_reason(quiz, context.state, settled=set(), blocked=set(), blacklisted={'coins_quiz': 2})
        is None
    )
    context.state.record('coins_quiz')
    assert engine._stuck_row_reason(quiz, context.state, settled=set(), blocked=set(), blacklisted={}) is None

    no_action = replace(full_roster, entries=(replace(_entry(full_roster, 'coins_quiz'), action=None),))
    assert engine._stuck_row_reason(no_action, context.state, settled=set(), blocked=set(), blacklisted={}) is None
    fresh = _context(tmp_path / 'fresh', capture=_Capture(_boost_frame()), catalogue=catalogue, corpus=corpus)
    assert (
        engine._stuck_row_reason(no_action, fresh.state, settled=set(), blocked=set(), blacklisted={})
        == "'Take the coins quiz' was allowed but had no dispatchable action"
    )


def test_a_stuck_allowed_row_restarts_then_hits_the_pass_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    no_action = replace(full_roster, entries=(replace(_entry(full_roster, 'coins_quiz'), action=None),))
    frame = _boost_frame('coins_quiz')
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: no_action)
    observation = RosterObservation(
        sequence=1,
        timestamp=CAPTURED_AT,
        rows=(),
        surface_variant=Surface.BOOST_PAGE,
        roster=no_action,
    )
    monkeypatch.setattr(engine, '_observe', lambda *args, **kwargs: observation)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, max_passes=2)

    report = engine.run_once(context)

    assert report.status is RunStatus.BUDGET
    assert report.detail is not None
    assert 'pass budget of 2 exhausted' in report.detail
    assert "'Take the coins quiz' was allowed but had no dispatchable action" in report.detail
    assert SurfaceRoute.SHORTCUT in cast(_Navigate, context.navigate).routes
    assert any(event.get('event') == 'surface_restart' for event in _events(report))


def test_a_retryable_row_left_after_the_last_pass_ends_as_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'surprise_items'),))
    frame = _boost_frame('surprise_items')
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, max_passes=1)
    monkeypatch.setattr(
        engine,
        '_dispatch_entry',
        lambda *args, **kwargs: EngineResult('skipped', 'row_vanished', 'stale row', False, {}),
    )

    report = engine.run_once(context)

    assert report.status is RunStatus.BUDGET
    assert report.passes == 1
    assert report.detail is not None
    assert 'pass budget of 1 exhausted' in report.detail
    summary = report.summary_path.read_text(encoding='utf-8')
    assert '| Browse surprise items | pass budget | pass budget of 1 exhausted' in summary
    assert 'no dispatchable row remains' not in summary


def test_an_unknown_surface_at_the_last_pass_ends_as_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    good = _boost_frame('coins_quiz')
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: roster)
    opens: list[int] = []

    def open_boost(*args: Any, **kwargs: Any) -> BoostList:
        index = len(opens)
        opens.append(1)
        snapshot = good if index == 0 else _home_frame()
        return BoostList(snapshot=snapshot, route=DiscoveryRoute.BOOST, detail='test')

    monkeypatch.setattr(engine, 'open_boost_list', open_boost)
    context = _context(
        tmp_path,
        capture=_Capture(good, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, max_passes=1)

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        _context.state.record(entry.key or '')
        return EngineResult('completed', 'fake', 'done', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert report.status is RunStatus.BUDGET
    assert report.detail is not None
    assert 'pass budget of 1 exhausted' in report.detail
    assert 'the task surface could not be read' in report.detail or 'surface variant is UNKNOWN' in report.detail


def test_a_restart_that_cannot_fire_the_shortcut_is_logged_and_returns_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(tmp_path, capture=_Capture(_boost_frame()), catalogue=catalogue, corpus=corpus)
    assert context.controller is not None
    report = RunReport(tmp_path, 'restart-failed', budget_seconds=60.0, started_at=CAPTURED_AT)
    observer = engine._observer(context, report, full_roster)

    def broken(_route: SurfaceRoute) -> None:
        raise RuntimeError('the shortcut intent did not start')

    context.navigate = cast(Any, broken)

    assert (
        engine._restart_surface(
            context,
            context.controller,
            observer,
            on_dismiss=None,
            reason='test restart',
        )
        is None
    )


def test_needs_another_pass_only_for_retryable_failures_and_stale_skips() -> None:
    assert engine._needs_another_pass(EngineResult('completed', 'fake', 'done', True, {})) is False
    assert engine._needs_another_pass(EngineResult('unavailable', 'unavailable', 'gone', False, {})) is False
    assert engine._needs_another_pass(EngineResult('skipped', 'row_vanished', 'stale', False, {})) is True
    assert engine._needs_another_pass(EngineResult('skipped', 'already_done', 'done', False, {})) is False


def test_pass_progress_and_bound_helpers() -> None:
    assert engine._pass_progressed(True, frozenset(), frozenset()) is True
    assert engine._pass_progressed(False, frozenset({'a'}), frozenset({'b'})) is True
    assert engine._pass_progressed(False, frozenset({'a'}), frozenset({'a'})) is False

    assert engine._next_pass_bound(
        passes=1,
        max_passes=12,
        no_progress_passes=1,
        progressed=True,
    ) == (None, 0)
    assert engine._next_pass_bound(
        passes=1,
        max_passes=12,
        no_progress_passes=0,
        progressed=False,
    ) == (None, 1)
    detail, idle = engine._next_pass_bound(
        passes=2,
        max_passes=12,
        no_progress_passes=1,
        progressed=False,
    )
    assert detail is not None
    assert 'no-progress pass(es)' in detail
    assert idle == engine.MAX_NO_PROGRESS_PASSES
    detail, idle = engine._next_pass_bound(
        passes=12,
        max_passes=12,
        no_progress_passes=0,
        progressed=True,
    )
    assert detail == 'pass budget of 12 exhausted'
    assert idle == 0


def test_dispatchable_identities_mirror_the_eligibility_rules(
    tmp_path: Path,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(tmp_path, capture=_Capture(_boost_frame()), catalogue=catalogue, corpus=corpus)
    everything = engine._dispatchable_identities(
        full_roster,
        context.state,
        blocked=set(),
        blacklisted={},
        settled=set(),
    )
    assert everything == frozenset(
        {'boost_checkin', 'coins_quiz', 'sponsored_items', 'surprise_items', 'notifications'},
    )
    assert (
        engine._dispatchable_identities(
            None,
            context.state,
            blocked=set(),
            blacklisted={},
            settled=set(),
        )
        == frozenset()
    )

    quiz = _entry(full_roster, 'coins_quiz')
    assert (
        engine._dispatchable_identities(
            replace(full_roster, entries=(quiz,)),
            context.state,
            blocked={'coins_quiz'},
            blacklisted={},
            settled=set(),
        )
        == frozenset()
    )
    assert (
        engine._dispatchable_identities(
            replace(full_roster, entries=(quiz,)),
            context.state,
            blocked=set(),
            blacklisted={'coins_quiz': 2},
            settled=set(),
        )
        == frozenset()
    )
    assert (
        engine._dispatchable_identities(
            replace(full_roster, entries=(quiz,)),
            context.state,
            blocked=set(),
            blacklisted={},
            settled={'coins_quiz'},
        )
        == frozenset()
    )
    assert (
        engine._dispatchable_identities(
            replace(full_roster, entries=(replace(quiz, action=None),)),
            context.state,
            blocked=set(),
            blacklisted={},
            settled=set(),
        )
        == frozenset()
    )
    assert (
        engine._dispatchable_identities(
            replace(full_roster, entries=(_entry(full_roster, 'coins_shortcut'),)),
            context.state,
            blocked=set(),
            blacklisted={},
            settled=set(),
        )
        == frozenset()
    )

    context.state.record('coins_quiz')
    assert (
        engine._dispatchable_identities(
            replace(full_roster, entries=(quiz,)),
            context.state,
            blocked=set(),
            blacklisted={},
            settled=set(),
        )
        == frozenset()
    )


@pytest.mark.slow
def test_six_sponsored_rounds_finish_in_one_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'sponsored_items'),))
    frame = _boost_frame('sponsored_items')
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    rounds: list[int] = []

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        rounds.append(1)
        _context.state.record(entry.key or '')
        return EngineResult('completed', 'progress_badge', 'a round completed', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    _seed_other_families(context, catalogue, roster)
    report = engine.run_once(context)

    assert len(rounds) == 6
    assert report.passes == 6
    assert report.status is RunStatus.COMPLETED
    assert report.detail == 'no dispatchable row remains'
    assert context.state.count('sponsored_items') == 6
    assert context.state.snapshot().completed_night is True
    assert not [skip for skip in report.skipped if skip.key == 'sponsored_items']


@pytest.mark.slow
def test_a_no_progress_loop_stops_after_two_idle_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    no_action = replace(full_roster, entries=(replace(_entry(full_roster, 'coins_quiz'), action=None),))
    frame = _boost_frame('coins_quiz')
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: no_action)
    observation = RosterObservation(
        sequence=1,
        timestamp=CAPTURED_AT,
        rows=(),
        surface_variant=Surface.BOOST_PAGE,
        roster=no_action,
    )
    monkeypatch.setattr(engine, '_observe', lambda *args, **kwargs: observation)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )

    report = engine.run_once(context)

    assert report.status is RunStatus.BUDGET
    assert report.passes == engine.MAX_NO_PROGRESS_PASSES
    assert report.detail is not None
    assert 'no-progress pass(es)' in report.detail
    assert "'Take the coins quiz' was allowed but had no dispatchable action" in report.detail
    assert any(event.get('event') == 'surface_restart' for event in _events(report))


def test_a_roster_with_nothing_left_completes_in_one_pass(
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
    context.state.record('coins_quiz')

    _seed_other_families(context, catalogue, roster)
    report = engine.run_once(context)

    assert report.status is RunStatus.COMPLETED
    assert report.passes == 1
    assert report.detail == 'no dispatchable row remains'
    assert report.tasks == []
    assert context.state.snapshot().completed_night is True


def test_a_pass_ceiling_below_the_largest_daily_limit_is_warned(
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
    context.config = replace(context.config, max_passes=5)

    report = engine.run_once(context)

    warning = next(event for event in _events(report) if event.get('event') == 'pass_budget_below_daily_capacity')
    assert warning['level'] == 'warning'
    assert warning['max_passes'] == 5
    assert warning['largest_max_per_run'] == 6
    assert warning['key'] == 'sponsored_items'
    assert 'AECOINS_MAX_PASSES=5' in str(warning['detail'])


def test_a_blacklist_state_write_failure_is_logged_without_hiding_the_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = _context(tmp_path, capture=_Capture(_boost_frame()), catalogue=catalogue, corpus=corpus)
    report = RunReport(tmp_path, 'blacklist-write', budget_seconds=60.0, started_at=CAPTURED_AT)
    entry = _entry(full_roster, 'coins_quiz')

    def broken(_key: str, _attempts: int) -> None:
        raise OSError('state volume read-only')

    monkeypatch.setattr(context.state, 'blacklist_task', broken)
    caplog.set_level(logging.WARNING)

    updated = engine._blacklist_task(
        report,
        context.state,
        entry,
        attempts=2,
        blacklist={},
        logger=_logger(),
    )

    assert updated == {'coins_quiz': 2}
    assert any(skip.key == 'coins_quiz' and skip.reason == 'blacklisted after 2 attempts' for skip in report.skipped)
    assert 'state_blacklist_failed' in caplog.text


@pytest.mark.slow
def test_a_one_attempt_bound_skips_the_taxonomy_retry_and_blacklists_immediately(
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
    context.config = replace(context.config, max_task_attempts=1)
    attempts: list[int] = []

    def dispatch(*args: Any, **kwargs: Any) -> EngineResult:
        attempts.append(1)
        return EngineResult(
            'failed',
            'no_change',
            'the quiz row did not move',
            False,
            {},
            failure=failure(FailureCode.TASK_UNVERIFIED, detail='the quiz row did not move'),
        )

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert len(attempts) == 1
    assert report.blacklist == {'coins_quiz': 1}
    assert any(skip.reason == 'blacklisted after 1 attempts' for skip in report.skipped)
    summary = report.summary_path.read_text(encoding='utf-8')
    assert '| Take the coins quiz | blacklisted after 1 attempts |' in summary
    assert report.attempts == {'coins_quiz': 1}


def test_stuck_row_reason_without_a_roster_is_none(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(tmp_path, capture=_Capture(_boost_frame()), catalogue=catalogue, corpus=corpus)

    assert engine._stuck_row_reason(None, context.state, settled=set(), blocked=set(), blacklisted={}) is None


def test_restart_surface_re_raises_a_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    context = _context(tmp_path, capture=_Capture(_boost_frame()), catalogue=catalogue, corpus=corpus)
    assert context.controller is not None
    report = RunReport(tmp_path, 'restart-stop', budget_seconds=60.0, started_at=CAPTURED_AT)
    observer = engine._observer(context, report, full_roster)
    monkeypatch.setattr(
        context,
        'navigate',
        cast(Any, lambda _route: (_ for _ in ()).throw(RunInterrupted('stop requested'))),
    )

    with pytest.raises(RunInterrupted):
        engine._restart_surface(
            context,
            context.controller,
            observer,
            on_dismiss=None,
            reason='test restart',
        )


def test_blacklisting_a_refused_row_skips_the_state_write(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    full_roster: Roster,
) -> None:
    context = _context(tmp_path, capture=_Capture(_boost_frame()), catalogue=catalogue, corpus=corpus)
    report = RunReport(tmp_path, 'blacklist-refused', budget_seconds=60.0, started_at=CAPTURED_AT)
    denied = next(entry for entry in full_roster.entries if entry.denied)

    updated = engine._blacklist_task(
        report,
        context.state,
        denied,
        attempts=2,
        blacklist={},
        logger=_logger(),
    )

    assert updated == {denied.title.casefold(): 2}
    assert context.state.snapshot().blacklist == {}


@pytest.mark.slow
def test_a_repeatable_row_is_offered_again_until_its_daily_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'surprise_items'),))
    frame = _boost_frame('surprise_items')
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    attempts: list[str] = []

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        attempts.append(entry.key or '')
        _context.state.record(entry.key or '')
        return EngineResult('completed', 'progress_badge', 'the counter moved', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    _seed_other_families(context, catalogue, roster)
    report = engine.run_once(context)

    assert attempts == ['surprise_items', 'surprise_items']
    assert report.passes == 2
    assert len(report.tasks) == 2
    assert all(task.completed for task in report.tasks)
    assert context.state.count('surprise_items') == 2
    assert context.state.snapshot().completed_night is True


@pytest.mark.slow
def test_a_failed_shortcut_restart_never_dispatches_from_the_stale_roster(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    wanted = ('coins_quiz', 'notifications')
    roster = replace(full_roster, entries=tuple(_entry(full_roster, key) for key in wanted))
    good = _boost_frame(*wanted)
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: roster)
    opens: list[int] = []

    def open_boost(*args: Any, **kwargs: Any) -> BoostList:
        index = len(opens)
        opens.append(1)
        snapshot = good if index == 0 else _home_frame()
        return BoostList(snapshot=snapshot, route=DiscoveryRoute.BOOST, detail='test')

    monkeypatch.setattr(engine, 'open_boost_list', open_boost)
    context = _context(
        tmp_path,
        capture=_Capture(good, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, max_passes=2)
    monkeypatch.setattr(
        context,
        'navigate',
        cast(Any, lambda _route: (_ for _ in ()).throw(RuntimeError('shortcut did not start'))),
    )
    order: list[str] = []

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        order.append(entry.key or '')
        _context.state.record(entry.key or '')
        return EngineResult('completed', 'fake', 'done', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert order == ['coins_quiz']
    assert report.status is RunStatus.BUDGET
    assert report.detail is not None
    assert 'pass budget of 2 exhausted' in report.detail
    assert any(event.get('event') == 'surface_restart_failed' for event in _events(report))


@pytest.mark.slow
def test_a_second_unreadable_observation_stops_dispatch_and_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    wanted = ('coins_quiz', 'notifications')
    roster = replace(full_roster, entries=tuple(_entry(full_roster, key) for key in wanted))
    frame = _boost_frame(*wanted)
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, max_passes=2)
    real_observe = engine._observe
    observations: list[int] = []

    def observe_once_then_none(*args: Any, **kwargs: Any) -> Any:
        observations.append(1)
        if len(observations) == 1:
            return real_observe(*args, **kwargs)
        return None

    monkeypatch.setattr(engine, '_observe', observe_once_then_none)
    order: list[str] = []

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        order.append(entry.key or '')
        _context.state.record(entry.key or '')
        return EngineResult('completed', 'fake', 'done', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert order == ['coins_quiz']
    assert report.status is RunStatus.BUDGET
    assert report.detail is not None
    assert 'pass budget of 2 exhausted' in report.detail
    assert 'has not been read' in report.detail


@pytest.mark.slow
def test_a_capped_observation_never_completes_the_night_on_stale_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    wanted = ('coins_quiz', 'notifications')
    roster = replace(full_roster, entries=tuple(_entry(full_roster, key) for key in wanted))
    frame = _boost_frame(*wanted)
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, max_passes=2)
    context.observer = RosterObserver(
        catalogue,
        previous=roster,
        max_observations=1,
        clock=lambda: CAPTURED_AT,
        logger=_logger(),
    )
    order: list[str] = []

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        order.append(entry.key or '')
        _context.state.record(entry.key or '')
        return EngineResult('completed', 'fake', 'done', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert order == ['coins_quiz']
    assert context.observer.cap_reached is True
    assert report.status is RunStatus.BUDGET
    assert report.detail is not None
    assert 'pass budget of 2 exhausted' in report.detail
    assert 'observation cap' in report.detail


@pytest.mark.slow
def test_an_abort_after_a_blacklist_still_reports_attempts_and_the_blacklist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    wanted = ('coins_quiz', 'notifications')
    roster = replace(full_roster, entries=tuple(_entry(full_roster, key) for key in wanted))
    frame = _boost_frame(*wanted)
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    notification_calls: list[int] = []

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        if entry.key == 'coins_quiz':
            return EngineResult(
                'failed',
                'no_change',
                'the quiz row did not move',
                False,
                {'key': entry.key},
                failure=failure(FailureCode.TASK_UNVERIFIED, detail='the quiz row did not move'),
            )
        notification_calls.append(1)
        if len(notification_calls) == 1:
            return EngineResult('skipped', 'row_vanished', 'stale notification row', False, {'key': entry.key})
        raise _safety_abort()

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert report.status is RunStatus.ABORTED
    assert report.attempts == {'coins_quiz': 2, 'notifications': 2}
    assert report.blacklist == {'coins_quiz': 2}
    summary = report.summary_path.read_text(encoding='utf-8')
    assert (
        '**Attempts per task:** coins_quiz: 2, notifications: 2 (blacklist after 2 failed attempt(s) per task)'
        in summary
    )
    assert '**Blacklist:** coins_quiz: blacklisted after 2 attempt(s)' in summary


@pytest.mark.slow
def test_successful_repeatable_attempts_do_not_count_towards_the_failure_blacklist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'sponsored_items'),))
    frame = _boost_frame('sponsored_items')
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.config = replace(context.config, max_passes=5)
    calls: list[int] = []

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        calls.append(1)
        if len(calls) <= 3:
            _context.state.record(entry.key or '')
            return EngineResult('completed', 'progress_badge', 'a round completed', True, {'key': entry.key})
        return EngineResult(
            'failed',
            'no_change',
            'the round did not move',
            False,
            {'key': entry.key},
            failure=failure(FailureCode.TASK_UNVERIFIED, detail='the round did not move'),
        )

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert len(calls) == 5
    assert report.passes == 5
    assert [task.completed for task in report.tasks] == [True, True, True, False, False]
    assert report.attempts == {'sponsored_items': 5}
    assert report.blacklist == {'sponsored_items': 2}
    assert any(
        skip.key == 'sponsored_items' and skip.reason == 'blacklisted after 2 attempts' for skip in report.skipped
    )
    assert context.state.count('sponsored_items') == 3
    assert context.state.snapshot().completed_night is False


def test_a_failed_task_records_the_tap_sequence_and_both_reports_reference_it(
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

    def dispatch(current: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        for _ in range(3):
            engine._capture(current.capture)
            clock.sleep(0.4)
        return EngineResult(
            'failed',
            'no_change',
            'the quiz question never replaced the row',
            False,
            {'taps': [{'label': 'GO', 'point': [982, 1273], 'reason': 'start the quiz'}]},
            failure=failure(FailureCode.TASK_UNVERIFIED, detail='the row did not move'),
            acted=True,
        )

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert report.status is RunStatus.FAILED
    assert report.tasks[0].code == FailureCode.TASK_UNVERIFIED.value
    assert len(report.failure_recordings) == 1
    recording = report.failure_recordings[0]
    assert recording['key'] == 'coins_quiz'
    assert recording['attempt'] == 1
    assert '-attempt-1-' in recording['directory']
    assert recording['frames']
    for frame_path in recording['frames']:
        assert (report.run_dir / frame_path).is_file()
    sidecar = json.loads((report.run_dir / recording['sidecar']).read_text(encoding='utf-8'))
    assert sidecar['failure']['code'] == FailureCode.TASK_UNVERIFIED.value
    assert sidecar['failure']['verification'] == 'no_change'
    assert sidecar['tap'] == [982, 1273]
    assert sidecar['action']['taps'][0]['reason'] == 'start the quiz'

    payload = json.loads(report.json_path.read_text(encoding='utf-8'))
    summary = report.summary_path.read_text(encoding='utf-8')
    assert payload['failure_recordings'] == report.failure_recordings
    assert recording['directory'] in summary
    assert '| Take the coins quiz | 1 | task_unverified |' in summary


def test_two_failures_of_one_task_in_one_run_produce_two_distinct_artifacts(
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

    def dispatch(current: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        attempts.append(len(attempts) + 1)
        engine._capture(current.capture)
        return EngineResult(
            'failed',
            'no_change',
            'the row did not move',
            False,
            {'key': entry.key},
            failure=failure(FailureCode.TASK_UNVERIFIED, detail='the row did not move'),
        )

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert attempts == [1, 2]
    assert len(report.failure_recordings) == 2
    assert [recording['attempt'] for recording in report.failure_recordings] == [1, 2]
    directories = [recording['directory'] for recording in report.failure_recordings]
    assert directories[0] != directories[1]
    assert '-attempt-1-' in directories[0]
    assert '-attempt-2-' in directories[1]
    for recording in report.failure_recordings:
        assert recording['frames']
        assert (report.run_dir / recording['sidecar']).is_file()
    summary = report.summary_path.read_text(encoding='utf-8')
    assert directories[0] in summary
    assert directories[1] in summary


def test_a_failed_failure_capture_degrades_to_a_warning_and_the_task_still_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _ToggleCapture:
        def __init__(self, frame: Snapshot) -> None:
            self.frames = (frame,)
            self.frame = frame
            self.fail_calls = 0

        def capture(self) -> Snapshot:
            if self.fail_calls > 0:
                self.fail_calls -= 1
                raise SnapshotError('screencap died during the aftermath')
            return self.frame

    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    frame = _boost_frame('coins_quiz')
    _surface(monkeypatch, roster, frame)
    source = _ToggleCapture(frame)
    context = _context(
        tmp_path,
        capture=cast(Any, source),
        catalogue=catalogue,
        corpus=corpus,
    )
    caplog.set_level(logging.WARNING)

    def dispatch(current: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        engine._capture(current.capture)
        source.fail_calls = 3
        return EngineResult(
            'failed',
            'no_change',
            'the row did not move',
            False,
            {'key': entry.key},
            failure=failure(FailureCode.TASK_UNVERIFIED, detail='the row did not move'),
            acted=True,
        )

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert report.status is RunStatus.FAILED
    assert report.ok is False
    assert report.tasks[0].code == FailureCode.TASK_UNVERIFIED.value
    assert len(report.failure_recordings) == 1
    assert report.failure_recordings[0]['frames']
    assert 'failure_recording_capture_failed' in caplog.text
    assert report.artifact_errors == []


def test_a_failed_task_whose_aftermath_is_empty_boost_does_not_abort_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    class _SwapCapture:
        def __init__(self, frame: Snapshot, aftermath: Snapshot) -> None:
            self.frames = (frame,)
            self.frame = frame
            self.aftermath = aftermath
            self.empty_remaining = 0

        def capture(self) -> Snapshot:
            if self.empty_remaining > 0:
                self.empty_remaining -= 1
                return self.aftermath
            return self.frame

    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    frame = _boost_frame('coins_quiz')
    _surface(monkeypatch, roster, frame)
    source = _SwapCapture(frame, _empty_boost_frame())
    context = _context(
        tmp_path,
        capture=cast(Any, source),
        catalogue=catalogue,
        corpus=corpus,
    )

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        source.empty_remaining = 3
        return EngineResult(
            'failed',
            'no_change',
            'the row did not move',
            False,
            {'key': entry.key},
            failure=failure(FailureCode.TASK_UNVERIFIED, detail='the row did not move'),
            acted=True,
        )

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert report.status is RunStatus.FAILED
    assert report.abort is None
    assert report.tasks[0].outcome == 'failed'
    assert len(report.failure_recordings) == 1
    assert report.failure_recordings[0]['frames']
    assert (report.run_dir / report.failure_recordings[0]['sidecar']).is_file()


@pytest.mark.parametrize('injected_safety', [False, True])
def test_a_dangerous_control_abort_flushes_the_buffered_frames(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    injected_safety: bool,
) -> None:
    class _TriggerCapture:
        def __init__(self, frame: Snapshot) -> None:
            self.frames = (frame,)
            self.frame = frame

        def capture(self) -> Snapshot:
            return self.frame

    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    frame = _boost_frame('coins_quiz')
    _surface(monkeypatch, roster, frame)
    source = _TriggerCapture(frame)
    context = _context(
        tmp_path,
        capture=cast(Any, source),
        catalogue=catalogue,
        corpus=corpus,
    )
    if injected_safety:
        context.safety = SafetyGuard(catalogue=catalogue)

    def dispatch(current: RunContext, _controller: Controller, _entry: RosterEntry) -> EngineResult:
        source.frame = _snapshot(_box('Wireless Earbuds', 60, 300, 600, 350), _box('Buy now', 400, 1800, 700, 1860))
        engine._capture(current.capture)
        raise AssertionError('the safety guard must abort before this returns')

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert report.status is RunStatus.ABORTED
    assert report.abort is not None
    assert report.abort['code'] == AbortCode.DANGEROUS_CONTROL.value
    assert len(report.failure_recordings) == 1
    recording = report.failure_recordings[0]
    assert recording['failure_code'] == AbortCode.DANGEROUS_CONTROL.value
    assert recording['frames']
    sidecar = json.loads((report.run_dir / recording['sidecar']).read_text(encoding='utf-8'))
    assert sidecar['failure']['code'] == AbortCode.DANGEROUS_CONTROL.value
    assert sidecar['frames']
    assert sidecar['frames'][-1]['file'].startswith('frame-')


def test_a_stop_during_a_task_flushes_the_buffered_frames_without_an_aftermath(
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

    def dispatch(current: RunContext, _controller: Controller, _entry: RosterEntry) -> EngineResult:
        engine._capture(current.capture)
        raise RunInterrupted('stop requested')

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert report.status is RunStatus.STOPPED
    assert len(report.failure_recordings) == 1
    recording = report.failure_recordings[0]
    assert recording['failure_code'] == 'stopped'
    sidecar = json.loads((report.run_dir / recording['sidecar']).read_text(encoding='utf-8'))
    assert sidecar['failure']['code'] == 'stopped'
    assert sidecar['frames']


def test_run_once_attaches_the_action_hook_to_factory_created_controllers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    frame = _boost_frame('coins_quiz')
    _surface(monkeypatch, roster, frame)
    frames = [replace(_coin_frame(), captured_at=CAPTURED_AT + timedelta(seconds=index)) for index in range(12)]
    clock = _Clock()
    context = _context(
        tmp_path,
        capture=_Capture(*frames),
        catalogue=catalogue,
        corpus=corpus,
        clock=clock,
    )
    context.controller = None
    created: list[Controller] = []

    def factory(snapshot: Snapshot) -> Controller:
        controller = Controller.from_frame(_RecordingDevice(), snapshot, rng=random.Random(0))
        created.append(controller)
        return controller

    context.controller_factory = factory
    tapped: list[Snapshot] = []

    def dispatch(current: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        tapped.append(engine._capture(current.capture))
        assert current.controller is not None
        current.controller.tap((600, 1000), why='tap from the factory controller')
        clock.value += 100.0
        engine._capture(current.capture)
        clock.value += 1.0
        engine._capture(current.capture)
        return EngineResult(
            'failed',
            'no_change',
            'the row did not move',
            False,
            {'key': entry.key, 'taps': [{'point': [600, 1000]}]},
            failure=failure(FailureCode.TASK_UNVERIFIED, detail='the row did not move'),
            acted=True,
        )

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert len(created) == 1
    assert len(report.failure_recordings) == 1
    sidecar = json.loads((report.run_dir / report.failure_recordings[0]['sidecar']).read_text(encoding='utf-8'))
    captured = [entry['captured_at'] for entry in sidecar['frames']]
    assert tapped[0].captured_at.isoformat() in captured


@pytest.mark.parametrize(
    ('error', 'expected_status', 'expected_code'),
    [
        (RunInterrupted('stop requested'), RunStatus.STOPPED, 'stopped'),
        (_safety_abort(AbortCode.DANGEROUS_CONTROL), RunStatus.ABORTED, AbortCode.DANGEROUS_CONTROL.value),
    ],
)
def test_an_abort_or_stop_during_the_acted_recheck_flushes_the_buffer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    error: Exception,
    expected_status: RunStatus,
    expected_code: str,
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
    context.state.mark_acted_unverified('coins_quiz')

    def recheck(current: RunContext, _controller: Controller, _entry: RosterEntry) -> tuple[bool, RosterEntry | None]:
        engine._capture(current.capture)
        raise error

    monkeypatch.setattr(engine, '_recheck_acted_row', recheck)

    report = engine.run_once(context)

    assert report.status is expected_status
    assert len(report.failure_recordings) == 1
    recording = report.failure_recordings[0]
    assert recording['failure_code'] == expected_code
    sidecar = json.loads((report.run_dir / recording['sidecar']).read_text(encoding='utf-8'))
    assert sidecar['failure']['code'] == expected_code
    assert sidecar['frames']


def test_a_prewrapped_capture_with_an_injected_guard_is_not_inspected_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    full_roster: Roster,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    class _TriggerCapture:
        def __init__(self, frame: Snapshot) -> None:
            self.frames = (frame,)
            self.frame = frame

        def capture(self) -> Snapshot:
            return self.frame

    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    frame = _boost_frame('coins_quiz')
    _surface(monkeypatch, roster, frame)
    source = _TriggerCapture(frame)
    guard = SafetyGuard(catalogue=catalogue)
    context = _context(
        tmp_path,
        capture=cast(Any, source),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.capture = cast(Any, guard.wrap(source))
    context.safety = guard
    danger = replace(
        _snapshot(_box('Wireless Earbuds', 60, 300, 600, 350), _box('Buy now', 400, 1800, 700, 1860)),
        captured_at=CAPTURED_AT + timedelta(minutes=55),
    )

    def dispatch(current: RunContext, _controller: Controller, _entry: RosterEntry) -> EngineResult:
        source.frame = danger
        engine._capture(current.capture)
        raise AssertionError('the safety guard must abort before this returns')

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert report.status is RunStatus.ABORTED
    assert report.abort is not None
    assert report.abort['code'] == AbortCode.DANGEROUS_CONTROL.value
    assert len(report.failure_recordings) == 1
    sidecar = json.loads((report.run_dir / report.failure_recordings[0]['sidecar']).read_text(encoding='utf-8'))
    captured = [entry['captured_at'] for entry in sidecar['frames']]
    assert danger.captured_at.isoformat() in captured
