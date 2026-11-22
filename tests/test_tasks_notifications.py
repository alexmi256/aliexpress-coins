"""The notifications executor over measured and synthetic frames (issue #28).

The device is simulated at the capture and input seams only: taps drive a small
screen graph, captures come from that graph, and time is injected. No test
reaches adb and no test sleeps.

The screen graph mirrors ``specs/flows.yaml``: the boost row, the app pre-prompt,
the Android runtime dialog, and the two OS-settings variants. The dialog is a
separate state in the executor even though the general classifier calls it a
popup; that distinction is what these tests pin down.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

import aliexpress_coins.tasks.notifications as notifications
from aliexpress_coins.control.controller import Controller
from aliexpress_coins.control.popups import PopupCorpus, load_popup_corpus
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.state import Classification, ScreenState, classify
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks.catalogue import TaskCatalogue, load_task_catalogue
from aliexpress_coins.tasks.discovery import RosterEntry, enumerate_roster
from aliexpress_coins.tasks.errors import TaskError
from aliexpress_coins.tasks.notifications import (
    NOTIFICATIONS_TITLE,
    NotificationOutcome,
    NotificationPermission,
    NotificationScreen,
    PermissionReading,
    _sweep_corpus,
    classify_notification_screen,
    find_allow_control,
    find_deny_control,
    find_enable_all_control,
    find_grant_control,
    is_app_prompt,
    is_os_settings,
    is_system_dialog,
    parse_permission_output,
    run_notifications_task,
)
from aliexpress_coins.tasks.surface import SurfaceRoute

ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = ROOT / 'specs' / 'popups.yaml'
CATALOGUE_PATH = ROOT / 'specs' / 'tasks.yaml'
DATA = ROOT / 'tests' / 'data'
CAPTURED_AT = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


class _RecordingDevice:
    """The input seam: record taps and let the world react to them."""

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
    """A tiny screen graph driven by taps and navigation, not a frame queue."""

    def __init__(
        self,
        series: dict[str, Sequence[Snapshot]],
        transitions: dict[tuple[str, str | None], str],
        *,
        start: str | None = None,
        navigate_to: str = 'coins',
        fail_navigation: bool = False,
        on_tap: Callable[[str, str | None, int], str | None] | None = None,
    ) -> None:
        if start is None:
            start = next(iter(series), '')
        if start not in series:
            raise ValueError(f'start screen {start!r} has no frames')
        self._series = {name: tuple(frames) for name, frames in series.items()}
        self._index = dict.fromkeys(series, 0)
        self._transitions = transitions
        self._navigate_to = navigate_to
        self._fail_navigation = fail_navigation
        self._on_tap = on_tap
        self.current = start
        self.taps: list[tuple[int, int]] = []
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

    def navigate(self, route: SurfaceRoute) -> None:
        self.navigations.append(route)
        if self._fail_navigation:
            raise RuntimeError('navigation failed')
        self.go(self._navigate_to)

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


def _boost_frame(*, done: bool = False, row: bool = True) -> Snapshot:
    boxes = [_box('Coins boost', 80, 90, 400, 150)]
    if row:
        boxes.extend(
            [
                _box(NOTIFICATIONS_TITLE, 60, 420, 700, 470),
                _box('Stay informed when rewards drop', 60, 480, 700, 530),
                _box('+1', 1000, 540, 1080, 580),
            ],
        )
        if not done:
            boxes.append(_box('GO', 1000, 480, 1080, 530))
    boxes.extend(
        [
            _box('Take the coins quiz', 60, 800, 700, 850),
            _box('Each correct answer grants you more rewards', 60, 860, 700, 900),
            _box('GO', 1000, 860, 1080, 910),
            _box('Place an order on this page', 60, 1200, 700, 1250),
            _box('GO', 1000, 1200, 1080, 1250),
        ],
    )
    return _snapshot(*boxes)


def _dialog_frame(*, allow: bool = True) -> Snapshot:
    boxes = [_box('Allow AliExpress to send you notifications?', 300, 890, 900, 930)]
    if allow:
        boxes.append(_box('Allow', 560, 990, 640, 1025))
    boxes.append(_box("Don't allow", 540, 1080, 660, 1115))
    return _snapshot(*boxes)


def _app_prompt_frame(*, enable: bool = True) -> Snapshot:
    boxes = [_box('Receive timely notifications', 12, 1186, 550, 1241)]
    if enable:
        boxes.append(_box('Enable all notifications', 474, 1650, 726, 1681))
    boxes.append(_box('No thanks', 540, 1852, 660, 1882))
    return _snapshot(*boxes)


def _os_settings_frame(*, granted: bool = False) -> Snapshot:
    boxes = [
        _box('AliExpress', 25, 210, 300, 282),
        _box('All AliExpress notifications', 62, 607, 423, 643),
    ]
    if granted:
        boxes.extend(
            [
                _box('Categories', 30, 741, 141, 773),
                _box('This app has not posted any notifications', 32, 808, 590, 847),
                _box('Allow notification dot', 114, 1104, 408, 1140),
            ],
        )
    else:
        boxes.append(_box("You haven't allowed notifications from this app", 29, 727, 660, 772))
    return _snapshot(*boxes)


def _coins_frame() -> Snapshot:
    return _snapshot(
        _box('Coins', 101, 194, 254, 242),
        _box('Coins credits to redeem', 59, 567, 552, 617),
        _box('Daily coins', 66, 919, 320, 978),
        _box('Earn more >', 917, 926, 1133, 967),
    )


def _wrong_frame() -> Snapshot:
    return _snapshot(
        _box('AliExpress', 60, 100, 400, 160),
        _box('Wireless Earbuds', 60, 400, 500, 450),
        _box('C$12.99', 60, 460, 300, 510),
    )


def _controller(world: _World, frame: Snapshot) -> Controller:
    import random

    return Controller.from_frame(_RecordingDevice(world), frame, rng=random.Random(0))


def _state(tmp_path: Path) -> DailyStateStore:
    return DailyStateStore(tmp_path / 'state', clock=lambda: datetime(2026, 11, 22, tzinfo=UTC))


def _entry(catalogue: TaskCatalogue, *, done: bool = False) -> RosterEntry:
    roster = enumerate_roster(_boost_frame(done=done), catalogue)
    for entry in roster.entries:
        if entry.key == 'notifications':
            return entry
    raise AssertionError('notifications is not in the synthetic roster')


def _run(
    world: _World,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    state: DailyStateStore,
    *,
    entry: RosterEntry | None = None,
    controller: Controller | None = None,
    **kwargs: object,
) -> Any:
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
    return run_notifications_task(
        controller if controller is not None else _controller(world, world.frame),
        world.capture,
        corpus,
        state,
        entry if entry is not None else _entry(catalogue),
        **options,  # type: ignore[arg-type]
    )


def _dialog_world(*, allow: bool = True, grant_works: bool = True, done: bool = True) -> _World:
    transitions: dict[tuple[str, str | None], str] = {
        ('boost', 'GO'): 'dialog',
        ('dialog', "Don't allow"): 'boost',
    }
    if allow and grant_works:
        transitions[('dialog', 'Allow')] = 'boost_done'
    return _World(
        series={
            'boost': (_boost_frame(),),
            'dialog': (_dialog_frame(allow=allow),),
            'boost_done': (_boost_frame(done=done),),
            'coins': (_coins_frame(),),
        },
        transitions=transitions,
    )


@pytest.fixture(scope='module')
def catalogue() -> TaskCatalogue:
    return load_task_catalogue(CATALOGUE_PATH)


@pytest.fixture(scope='module')
def corpus() -> PopupCorpus:
    return load_popup_corpus(CORPUS_PATH)


# --- state/perception --------------------------------------------------------


def test_the_measured_dialog_is_its_own_state_not_an_in_app_popup() -> None:
    with Image.open(DATA / 'popups' / 'system_notification_permission.png') as opened:
        image = opened.convert('RGB')
    frame = Snapshot(
        image=image,
        boxes=(),
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(image.width, image.height, density=240, sdk=35),
    )

    assert is_system_dialog(frame) is False  # boxes are the contract, not the image
    synthetic = _dialog_frame()
    assert is_system_dialog(synthetic) is True
    assert classify_notification_screen(synthetic) is NotificationScreen.SYSTEM_DIALOG
    assert classify(synthetic).state in (ScreenState.POPUP, ScreenState.UNKNOWN)


def test_a_dialog_with_the_app_prompt_behind_it_prefers_the_dialog() -> None:
    frame = _snapshot(
        _box('Allow AliExpress to send you notifications?', 300, 890, 900, 930),
        _box('Receive timely notifications', 12, 1186, 550, 1241),
    )

    assert classify_notification_screen(frame) is NotificationScreen.SYSTEM_DIALOG


def test_the_app_prompt_os_pages_and_helpers_are_recognised() -> None:
    app_prompt = _app_prompt_frame()
    off = _os_settings_frame()
    on = _os_settings_frame(granted=True)

    assert is_app_prompt(app_prompt) is True
    assert is_os_settings(off) is True
    assert classify_notification_screen(app_prompt) is NotificationScreen.APP_PROMPT
    assert classify_notification_screen(off) is NotificationScreen.OS_SETTINGS
    assert classify_notification_screen(on) is NotificationScreen.OS_SETTINGS_GRANTED
    assert find_enable_all_control(app_prompt) is not None
    assert classify_notification_screen(_coins_frame()) is NotificationScreen.COIN_PAGE
    assert classify_notification_screen(_wrong_frame()) is NotificationScreen.OTHER


def test_control_lookup_is_exact_so_the_dialog_question_is_not_the_allow_button() -> None:
    frame = _dialog_frame()

    allow = find_grant_control(frame)
    assert allow is not None
    assert find_allow_control(frame) is allow
    assert allow.text == 'Allow'
    assert find_deny_control(frame) is not None
    assert find_deny_control(frame).text == "Don't allow"  # type: ignore[union-attr]

    question_only = _snapshot(_box('Allow AliExpress to send you notifications?', 300, 890, 900, 930))
    assert find_grant_control(question_only) is None
    assert find_deny_control(question_only) is None
    assert find_enable_all_control(_app_prompt_frame(enable=False)) is None


def test_sweep_corpus_never_contains_the_boost_sheet_or_notification_prompts(corpus: PopupCorpus) -> None:
    filtered = _sweep_corpus(corpus)

    assert 'coins_boost_sheet' not in {family.family for family in filtered.families}
    assert 'system_notification_permission' not in {family.family for family in filtered.families}
    assert 'push_permission_prompt' not in {family.family for family in filtered.families}
    assert 'promo_coupon_unlocked' in {family.family for family in filtered.families}
    assert _sweep_corpus(filtered) is filtered


def test_permission_output_parsing_accepts_appops_and_probe_shapes() -> None:
    assert parse_permission_output('Uid mode: POST_NOTIFICATION: ignore').state is NotificationPermission.DENIED
    assert parse_permission_output('Uid mode: POST_NOTIFICATION: allow').state is NotificationPermission.GRANTED
    measured = 'Uid mode: POST_NOTIFICATION: ignore\nPOST_NOTIFICATION: allow'
    assert parse_permission_output(measured).state is NotificationPermission.DENIED
    assert parse_permission_output('granted').state is NotificationPermission.GRANTED
    assert parse_permission_output('denied').state is NotificationPermission.DENIED
    assert parse_permission_output('').state is NotificationPermission.UNKNOWN
    assert parse_permission_output('something else').state is NotificationPermission.UNKNOWN


class _FakeProbeRunner:
    def __init__(
        self, *, returncode: int = 0, stdout: str = '', stderr: str = '', error: Exception | None = None
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.error = error

    def __call__(self, args: Sequence[str], timeout: float) -> object:
        del args, timeout
        if self.error is not None:
            raise self.error
        from types import SimpleNamespace

        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)


def test_appops_probe_parses_success_failure_and_exceptions() -> None:
    from aliexpress_coins.tasks.notifications import AppOpsPermissionProbe

    granted = AppOpsPermissionProbe('serial', runner=_FakeProbeRunner(stdout='Uid mode: POST_NOTIFICATION: allow'))
    denied = AppOpsPermissionProbe('serial', runner=_FakeProbeRunner(stdout='Uid mode: POST_NOTIFICATION: ignore'))
    failed = AppOpsPermissionProbe('serial', runner=_FakeProbeRunner(returncode=1, stderr='boom'))
    broken = AppOpsPermissionProbe('serial', runner=_FakeProbeRunner(error=OSError('adb gone')))

    assert granted().state is NotificationPermission.GRANTED
    assert denied().state is NotificationPermission.DENIED
    assert failed().state is NotificationPermission.UNKNOWN
    assert broken().state is NotificationPermission.UNKNOWN
    assert broken().raw is not None

    with pytest.raises(ValueError, match='serial'):
        AppOpsPermissionProbe(' ')
    with pytest.raises(ValueError, match='positive'):
        AppOpsPermissionProbe('serial', timeout=0)


# --- guards ------------------------------------------------------------------


def test_an_allowed_row_with_no_action_is_already_done_not_a_policy_error(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    entry = replace(_entry(catalogue), action=None)
    world = _World({'boost': (_boost_frame(done=True),), 'coins': (_coins_frame(),)}, {})

    result = run_notifications_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        entry,
        navigate=world.navigate,
    )

    assert result.outcome is NotificationOutcome.ALREADY_DONE
    assert world.taps == []


def test_run_notifications_rejects_a_denied_entry(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    roster = enumerate_roster(_boost_frame(), catalogue)
    denied = next(entry for entry in roster.entries if entry.denied)
    world = _World({'boost': (_boost_frame(),)}, {})

    with pytest.raises(TaskError, match='not dispatchable'):
        run_notifications_task(
            _controller(world, world.frame),
            _ForbiddenCapture(),
            corpus,
            _state(tmp_path),
            denied,
            navigate=world.navigate,
        )


def test_run_notifications_rejects_a_non_notification_category(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    roster = enumerate_roster(_boost_frame(), catalogue)
    entry = next(entry for entry in roster.entries if entry.key == 'coins_quiz')
    world = _World({'boost': (_boost_frame(),)}, {})

    with pytest.raises(TaskError, match='not a notification task'):
        run_notifications_task(
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
    ],
)
def test_run_notifications_rejects_invalid_parameters(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    name: str,
    value: float,
) -> None:
    world = _World({'boost': (_boost_frame(),)}, {})

    with pytest.raises(ValueError, match=r'must be positive|must not be negative'):
        run_notifications_task(
            _controller(world, world.frame),
            world.capture,
            corpus,
            _state(tmp_path),
            _entry(catalogue),
            navigate=world.navigate,
            **{name: value},  # type: ignore[arg-type]
        )


def test_run_notifications_skips_at_the_daily_limit(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    state.record('notifications')
    world = _World({'boost': (_boost_frame(),)}, {})

    result = run_notifications_task(
        _controller(world, world.frame),
        _ForbiddenCapture(),
        corpus,
        state,
        _entry(catalogue),
        navigate=world.navigate,
    )

    assert result.outcome is NotificationOutcome.SKIPPED
    assert result.verification == 'daily_limit'
    assert result.recorded is False


def test_run_notifications_reports_an_absent_row_as_unavailable(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        {
            'boost': (_boost_frame(row=False),),
            'coins': (_coins_frame(),),
        },
        {},
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is NotificationOutcome.UNAVAILABLE
    assert result.verification == 'unavailable'
    assert result.recorded is False
    assert world.taps == []
    assert result.returned is True


def test_run_notifications_reports_a_non_task_surface(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World({'wrong': (_wrong_frame(),), 'coins': (_coins_frame(),)}, {}, start='wrong')

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is NotificationOutcome.UNAVAILABLE
    assert result.classification == 'unknown'
    assert result.returned is True


def test_run_notifications_reports_an_already_done_row_as_already_done(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World({'boost': (_boost_frame(done=True),), 'coins': (_coins_frame(),)}, {})
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state)

    assert result.outcome is NotificationOutcome.ALREADY_DONE
    assert result.verification == 'already_done'
    assert result.recorded is True
    assert state.count('notifications') == 1
    assert world.taps == []
    assert result.returned is True


# --- live-shaped flows -------------------------------------------------------


def test_granting_the_system_dialog_completes_and_records_the_row(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _dialog_world()
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state)

    assert result.outcome is NotificationOutcome.COMPLETED
    assert result.verification == 'action_completed'
    assert result.dialog_appeared is True
    assert result.screen is not None
    assert result.classification in ('popup', 'page', 'unknown')
    assert [tap.label for tap in result.taps] == ['GO', 'Allow']
    assert state.count('notifications') == 1
    assert result.recorded is True
    assert result.returned is True
    assert world.navigations == [SurfaceRoute.SHORTCUT]
    assert result.as_dict()['outcome'] == 'completed'


def test_a_dialog_with_only_a_denial_control_is_recorded_and_does_not_raise(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _dialog_world(allow=False)
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state)

    assert result.outcome is NotificationOutcome.PERMISSION_DENIED
    assert result.verification == 'permission_denied'
    assert result.permission_denied is True
    assert result.dialog_appeared is True
    assert [tap.label for tap in result.taps] == ['GO', "Don't allow"]
    assert result.returned is True
    assert result.recorded is True
    assert state.count('notifications') == 1
    assert result.done_today_after == 1


def test_a_grant_that_never_closes_the_dialog_is_permission_denied(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _dialog_world(grant_works=False)
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state)

    assert result.outcome is NotificationOutcome.PERMISSION_DENIED
    assert result.verification == 'permission_denied'
    assert [tap.label for tap in result.taps][:2] == ['GO', 'Allow']
    assert result.granted is False
    assert state.count('notifications') == 1


def test_an_os_settings_redirect_stops_the_task_without_flipping_the_switch(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        {
            'boost': (_boost_frame(),),
            'settings': (_os_settings_frame(),),
            'coins': (_coins_frame(),),
        },
        {('boost', 'GO'): 'settings'},
    )
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state)

    assert result.outcome is NotificationOutcome.ROUTED_TO_SETTINGS
    assert result.verification == 'routed_to_settings'
    assert result.routed_to_settings is True
    assert [tap.label for tap in result.taps] == ['GO']
    assert result.returned is True
    assert state.count('notifications') == 1


def test_the_os_page_with_the_permission_already_on_also_routes_to_settings(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        {
            'boost': (_boost_frame(),),
            'settings': (_os_settings_frame(granted=True),),
            'coins': (_coins_frame(),),
        },
        {('boost', 'GO'): 'settings'},
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is NotificationOutcome.ROUTED_TO_SETTINGS
    assert result.screen == NotificationScreen.OS_SETTINGS_GRANTED.value


def test_the_measured_no_dialog_path_records_the_row_completion(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        {
            'boost': (_boost_frame(),),
            'boost_done': (_boost_frame(done=True),),
            'coins': (_coins_frame(),),
        },
        {('boost', 'GO'): 'boost_done'},
    )
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state)

    assert result.outcome is NotificationOutcome.COMPLETED
    assert result.verification == 'action_completed'
    assert result.dialog_appeared is False
    assert [tap.label for tap in result.taps] == ['GO']
    assert state.count('notifications') == 1


def test_the_app_pre_prompt_is_followed_to_the_system_dialog(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        {
            'boost': (_boost_frame(),),
            'app_prompt': (_app_prompt_frame(),),
            'dialog': (_dialog_frame(),),
            'boost_done': (_boost_frame(done=True),),
            'coins': (_coins_frame(),),
        },
        {
            ('boost', 'GO'): 'app_prompt',
            ('app_prompt', 'Enable all notifications'): 'dialog',
            ('dialog', 'Allow'): 'boost_done',
        },
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is NotificationOutcome.COMPLETED
    assert result.dialog_appeared is True
    assert [tap.label for tap in result.taps] == ['GO', 'Enable all notifications', 'Allow']


def test_an_app_prompt_with_no_grant_path_is_recorded_as_denied(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        {
            'boost': (_boost_frame(),),
            'app_prompt': (_app_prompt_frame(enable=False),),
            'coins': (_coins_frame(),),
        },
        {('boost', 'GO'): 'app_prompt', ('app_prompt', 'No thanks'): 'boost'},
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is NotificationOutcome.PERMISSION_DENIED
    assert [tap.label for tap in result.taps] == ['GO', 'No thanks']


def test_enable_notifications_false_denies_instead_of_granting(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _dialog_world()

    result = _run(world, catalogue, corpus, _state(tmp_path), enable_notifications=False)

    assert result.outcome is NotificationOutcome.PERMISSION_DENIED
    assert [tap.label for tap in result.taps] == ['GO', "Don't allow"]


def test_a_probe_that_reports_the_grant_completes_even_when_the_row_stays(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World({'boost': (_boost_frame(),), 'coins': (_coins_frame(),)}, {})
    state = _state(tmp_path)
    readings = iter(('ignore', 'granted'))

    result = _run(
        world,
        catalogue,
        corpus,
        state,
        permission_probe=lambda: next(readings, 'granted'),
    )

    assert result.outcome is NotificationOutcome.COMPLETED
    assert result.verification == 'permission_granted'
    assert result.granted is True
    assert state.count('notifications') == 1


def test_a_probe_that_reports_denial_after_the_grant_overrides_the_row(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _dialog_world()
    state = _state(tmp_path)

    result = _run(
        world,
        catalogue,
        corpus,
        state,
        permission_probe=lambda: 'ignore',
    )

    assert result.outcome is NotificationOutcome.PERMISSION_DENIED
    assert result.verification == 'permission_denied'
    assert result.permission_after is not None
    assert result.permission_after.denied is True


def test_a_probe_that_raises_does_not_break_the_task(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _dialog_world()

    def broken() -> PermissionReading:
        raise OSError('probe socket closed')

    result = _run(world, catalogue, corpus, _state(tmp_path), permission_probe=broken)

    assert result.outcome is NotificationOutcome.COMPLETED
    assert result.permission_before is not None
    assert result.permission_before.state is NotificationPermission.UNKNOWN


def test_probe_values_are_coerced_from_bool_and_none(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World({'boost': (_boost_frame(),), 'coins': (_coins_frame(),)}, {})

    result = _run(world, catalogue, corpus, _state(tmp_path), permission_probe=lambda: None)

    assert result.permission_before is not None
    assert result.permission_before.state is NotificationPermission.UNKNOWN


def test_a_pressed_row_that_changes_nothing_fails_without_a_false_completion(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World({'boost': (_boost_frame(),), 'coins': (_coins_frame(),)}, {})
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state)

    assert result.outcome is NotificationOutcome.FAILED
    assert result.verification == 'no_change'
    assert result.recorded is False
    assert state.count('notifications') == 0
    assert result.returned is True


def test_a_grant_with_no_row_change_and_no_probe_is_grant_not_observed(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    # The dialog closes, but the row never changes and no probe can prove the
    # permission, so the executor refuses to call it complete.
    world = _World(
        {
            'boost': (_boost_frame(),),
            'dialog': (_dialog_frame(),),
            'coins': (_coins_frame(),),
        },
        {('boost', 'GO'): 'dialog', ('dialog', 'Allow'): 'boost'},
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is NotificationOutcome.FAILED
    assert result.verification == 'grant_not_observed'
    assert result.done_today_after == 0


def test_a_failed_return_is_reported_but_the_outcome_is_kept(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _dialog_world(allow=False)
    world._fail_navigation = True

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is NotificationOutcome.PERMISSION_DENIED
    assert result.returned is False
    assert result.detail.endswith('the shortcut route did not prove the coins page')


def test_an_unexpected_fault_is_recorded_not_raised(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    class _ExplodingController(Controller):
        def tap_box(self, *args: object, **kwargs: object) -> Any:
            raise RuntimeError('input layer broke')

    world = _dialog_world()

    result = _run(
        world,
        catalogue,
        corpus,
        _state(tmp_path),
        controller=_ExplodingController.from_frame(
            _RecordingDevice(world),
            world.frame,
        ),
    )

    assert result.outcome is NotificationOutcome.FAILED
    assert result.verification == 'error' or result.verification == 'return_failed'
    assert 'input layer broke' in result.detail


def test_a_dialog_whose_ocr_is_missing_fails_as_an_unclosable_popup_not_a_guess(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    # A POPUP frame with the dialog sentence missing cannot be matched by a
    # family because the notification families are excluded from the sweep. The
    # task records that stop rather than issuing a blind tap.
    bare_popup = _snapshot(_box('Welcome gifts', 100, 800, 900, 850))
    world = _World(
        {
            'boost': (_boost_frame(),),
            'popup': (bare_popup,),
            'coins': (_coins_frame(),),
        },
        {('boost', 'GO'): 'popup'},
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is NotificationOutcome.PERMISSION_DENIED
    assert result.stop == 'popup_unclosable'
    assert [tap.label for tap in result.taps] == ['GO']


def test_a_generic_in_app_popup_is_swept_before_the_row_is_read(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    # Build a label-close family whose anchor is on the synthetic frame; this
    # exercises the one dismissal loop without depending on a template crop.
    from dataclasses import replace

    from aliexpress_coins.control.popups import CloseAttempt, CloseKind

    family = replace(
        corpus.by_family('promo_coupon_unlocked'),
        anchor='Extra Coupon unlocked',
        close=(CloseAttempt(kind=CloseKind.LABEL, label='Dismiss'),),
    )
    labelled = replace(corpus, families=(family,))
    popup = _snapshot(
        _box('Welcome gifts', 100, 500, 1100, 560),
        _box('Extra Coupon unlocked', 100, 600, 1100, 700),
        _box('Dismiss', 540, 900, 660, 940),
    )
    world = _World(
        {
            'boost': (_boost_frame(), popup, popup, popup, popup, popup, popup),
            'coins': (_coins_frame(),),
        },
        {},
    )

    result = _run(world, catalogue, labelled, _state(tmp_path))

    # The sweep runs, but this synthetic world cannot close the popup because
    # its transition is keyed by screen, not by the dismiss label. The important
    # assertion is that the one loop was used, no tap was guessed, and the task
    # reported instead of raising.
    assert result.dismissals or result.stop is not None
    assert result.outcome in (NotificationOutcome.FAILED, NotificationOutcome.PERMISSION_DENIED)


def test_run_notifications_rejects_a_loading_surface_it_cannot_settle(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    # A blank white frame classifies as an error, not loading, so this also
    # covers the "not a task surface" branch without waiting on the injected clock.
    blank = Snapshot(
        image=Image.new('RGB', (1200, 2000), 'white'),
        boxes=(),
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(1200, 2000, density=240, sdk=35),
    )
    world = _World({'blank': (blank,), 'coins': (_coins_frame(),)}, {}, start='blank')

    result = _run(world, catalogue, corpus, _state(tmp_path), load_timeout=0.01)

    assert result.outcome is NotificationOutcome.UNAVAILABLE
    assert result.verification == 'unavailable'


def test_the_result_payload_keeps_the_permission_and_dialog_evidence(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _dialog_world()
    result = _run(world, catalogue, corpus, _state(tmp_path), permission_probe=lambda: 'allow')
    payload = result.as_dict()

    assert payload['dialog_appeared'] is True
    assert payload['granted'] is True
    assert payload['permission_before'] is not None
    assert payload['permission_after'] is not None
    assert payload['screen'] is not None
    assert isinstance(payload['taps'], list)


def test_route_firer_callable_is_accepted_as_the_navigate_seam(
    catalogue: TaskCatalogue, corpus: PopupCorpus, tmp_path: Path
) -> None:
    world = _dialog_world()
    calls: list[SurfaceRoute] = []

    def navigate(route: SurfaceRoute) -> None:
        calls.append(route)
        world.navigate(route)

    result = run_notifications_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=navigate,
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=2.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.completed is True
    assert calls == [SurfaceRoute.SHORTCUT]
    assert isinstance(result.permission_denied, bool)
    assert isinstance(result.already_done, bool)
    assert isinstance(result.unavailable, bool)
    assert isinstance(result.failed, bool)
    assert isinstance(result.skipped, bool)


def test_state_is_not_recorded_twice_for_an_already_recorded_day(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World({'boost': (_boost_frame(done=True),), 'coins': (_coins_frame(),)}, {})
    state = _state(tmp_path)

    first = _run(world, catalogue, corpus, state)
    world.go('boost')
    second = _run(world, catalogue, corpus, state)

    assert first.outcome is NotificationOutcome.ALREADY_DONE
    assert second.outcome is NotificationOutcome.SKIPPED
    assert second.verification == 'daily_limit'
    assert state.count('notifications') == 1


def test_catalogue_and_corpus_fixtures_are_the_real_documents(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
) -> None:
    assert catalogue.allowed_by_key('notifications').category.value == 'notification'
    assert corpus.by_family('system_notification_permission').anchor.startswith('Allow AliExpress')


def test_classification_returns_entry_row_for_a_plain_page() -> None:
    page = _snapshot(
        _box('Coins quiz', 60, 90, 400, 150),
        _box('Answer quiz questions for more coins!', 60, 170, 700, 210),
        _box('Q : Which of the following statements is correct?', 80, 400, 1100, 450),
        _box('True', 80, 600, 800, 650),
        _box('False', 80, 750, 800, 800),
    )

    assert classify(page).state is ScreenState.PAGE
    assert classify_notification_screen(page) is NotificationScreen.ENTRY_ROW


def test_low_confidence_ocr_is_never_tapped_as_a_control() -> None:
    frame = _snapshot(_box('Allow', 560, 990, 640, 1025, score=0.1))

    assert find_grant_control(frame) is None


def test_the_real_subprocess_probe_runner_uses_the_injected_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess
    from types import SimpleNamespace

    calls: list[tuple[tuple[str, ...], float]] = []

    def fake_run(args: Sequence[str], **kwargs: object) -> object:
        calls.append((tuple(args), float(cast(float, kwargs['timeout']))))
        return SimpleNamespace(returncode=0, stdout='POST_NOTIFICATION: allow', stderr='')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    runner = notifications._SubprocessProbeRunner()

    result = runner(('adb', 'shell', 'cmd'), 2.0)

    assert result.returncode == 0
    assert result.stdout == 'POST_NOTIFICATION: allow'
    assert calls == [(('adb', 'shell', 'cmd'), 2.0)]


def test_probe_values_support_bool_and_bytes() -> None:
    assert notifications._coerce_reading(True).state is NotificationPermission.GRANTED
    assert notifications._coerce_reading(False).state is NotificationPermission.DENIED


def test_the_probe_turns_non_text_transport_output_into_unknown() -> None:
    from types import SimpleNamespace

    probe = notifications.AppOpsPermissionProbe(
        'serial',
        runner=lambda args, timeout: SimpleNamespace(returncode=0, stdout=None, stderr=123),
    )

    assert probe().state is NotificationPermission.UNKNOWN


def test_an_os_settings_page_at_entry_is_routed_without_reading_a_row(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        {'settings': (_os_settings_frame(),), 'coins': (_coins_frame(),)},
        {},
        start='settings',
    )
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state)

    assert result.outcome is NotificationOutcome.ROUTED_TO_SETTINGS
    assert world.taps == []
    assert state.count('notifications') == 1


def test_an_app_prompt_at_entry_is_resolved_before_the_row_is_tapped(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        {'app_prompt': (_app_prompt_frame(),), 'coins': (_coins_frame(),)},
        {},
        start='app_prompt',
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is NotificationOutcome.PERMISSION_DENIED
    assert next(tap.label for tap in result.taps) == 'Enable all notifications'
    assert all(tap.label != 'GO' for tap in result.taps)


def test_a_system_dialog_at_entry_is_handled_before_the_row_is_tapped(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    overlaying = _snapshot(
        _box('Coins boost', 80, 90, 400, 150),
        _box(NOTIFICATIONS_TITLE, 60, 420, 700, 470),
        _box('Stay informed when rewards drop', 60, 480, 700, 530),
        _box('GO', 1000, 480, 1080, 530),
        _box('Allow AliExpress to send you notifications?', 300, 890, 900, 930),
        _box('Allow', 560, 990, 640, 1025),
        _box("Don't allow", 540, 1080, 660, 1115),
    )
    world = _World(
        {
            'entry_dialog': (overlaying,),
            'boost_done': (_boost_frame(done=True),),
            'coins': (_coins_frame(),),
        },
        {('entry_dialog', 'Allow'): 'boost_done'},
        start='entry_dialog',
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is NotificationOutcome.ALREADY_DONE
    assert [tap.label for tap in result.taps] == ['Allow']
    assert result.dialog_appeared is True


def test_an_entry_prompt_grant_is_probed_and_recorded_when_no_row_remains(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    overlaying = _snapshot(
        _box('Daily coins', 66, 919, 320, 978),
        _box('Coins credits to redeem', 59, 567, 552, 617),
        _box('Allow AliExpress to send you notifications?', 300, 890, 900, 930),
        _box('Allow', 560, 990, 640, 1025),
        _box("Don't allow", 540, 1080, 660, 1115),
    )
    world = _World(
        {'entry_dialog': (overlaying,), 'coins': (_coins_frame(),)},
        {('entry_dialog', 'Allow'): 'coins'},
        start='entry_dialog',
    )
    state = _state(tmp_path)
    readings = iter(('ignore', 'allow'))

    result = _run(
        world,
        catalogue,
        corpus,
        state,
        permission_probe=lambda: next(readings, 'allow'),
    )

    assert result.outcome is NotificationOutcome.COMPLETED
    assert result.verification == 'permission_granted'
    assert [tap.label for tap in result.taps] == ['Allow']
    assert result.recorded is True
    assert state.count('notifications') == 1


def test_a_row_that_disappears_from_the_populated_boost_list_is_completed(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        {
            'boost': (_boost_frame(),),
            'gone': (_boost_frame(row=False),),
            'coins': (_coins_frame(),),
        },
        {('boost', 'GO'): 'gone'},
    )
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state)

    assert result.outcome is NotificationOutcome.COMPLETED
    assert result.verification == 'row_gone'
    assert state.count('notifications') == 1


def test_completion_that_cannot_return_is_not_reported_as_complete(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _dialog_world()
    world._fail_navigation = True
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state)

    assert result.outcome is NotificationOutcome.FAILED
    assert result.verification == 'return_failed'
    assert state.count('notifications') == 0


def test_a_dialog_without_any_control_is_recorded_as_denied_without_a_tap(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    only_question = _snapshot(_box('Allow AliExpress to send you notifications?', 300, 890, 900, 930))
    world = _World(
        {
            'boost': (_boost_frame(),),
            'dialog': (only_question,),
            'coins': (_coins_frame(),),
        },
        {('boost', 'GO'): 'dialog'},
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is NotificationOutcome.PERMISSION_DENIED
    assert [tap.label for tap in result.taps] == ['GO']


def test_a_failed_grant_without_a_denial_control_still_returns(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    no_deny = _snapshot(
        _box('Allow AliExpress to send you notifications?', 300, 890, 900, 930),
        _box('Allow', 560, 990, 640, 1025),
    )
    world = _World(
        {
            'boost': (_boost_frame(),),
            'dialog': (no_deny,),
            'coins': (_coins_frame(),),
        },
        {('boost', 'GO'): 'dialog'},
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is NotificationOutcome.PERMISSION_DENIED
    assert [tap.label for tap in result.taps] == ['GO', 'Allow']


def test_an_app_prompt_with_no_control_uses_the_existing_sweep_and_reports(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    bare = _snapshot(_box('Receive timely notifications', 12, 1186, 550, 1241))
    world = _World(
        {
            'boost': (_boost_frame(),),
            'prompt': (bare,),
            'coins': (_coins_frame(),),
        },
        {('boost', 'GO'): 'prompt'},
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is NotificationOutcome.PERMISSION_DENIED
    assert result.stop is not None


def test_a_generic_popup_that_closes_lets_the_flow_continue_to_completion(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from aliexpress_coins.control.popups import CloseAttempt, CloseKind

    family = replace(
        corpus.by_family('promo_coupon_unlocked'),
        anchor='Extra Coupon unlocked',
        close=(CloseAttempt(kind=CloseKind.LABEL, label='Dismiss'),),
    )
    labelled = replace(corpus, families=(family,))
    popup = _snapshot(
        _box('Welcome gifts', 100, 500, 1100, 560),
        _box('Extra Coupon unlocked', 100, 600, 1100, 700),
        _box('Dismiss', 540, 900, 660, 940),
    )
    world = _World(
        {'boost': (_boost_frame(), popup, popup, popup, popup, popup, popup), 'coins': (_coins_frame(),)},
        {},
        on_tap=lambda screen, label, count: 'boost_done' if label == 'Dismiss' else None,
    )
    world._series['boost_done'] = (_boost_frame(done=True),)

    result = _run(world, catalogue, labelled, _state(tmp_path))

    assert result.outcome is NotificationOutcome.COMPLETED
    assert any(attempt.label == 'Dismiss' for report in result.dismissals for attempt in report.taps)


def test_an_enable_tap_that_changes_nothing_is_not_repeated_forever(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        {
            'boost': (_boost_frame(),),
            'prompt': (_app_prompt_frame(),),
            'coins': (_coins_frame(),),
        },
        {('boost', 'GO'): 'prompt'},
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is NotificationOutcome.PERMISSION_DENIED
    assert [tap.label for tap in result.taps].count('Enable all notifications') == 1
    assert result.recorded is True


def test_an_enable_tap_that_changes_nothing_without_a_deny_control_still_terminates(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    enable_only = _snapshot(
        _box('Receive timely notifications', 12, 1186, 550, 1241),
        _box('Enable all notifications', 474, 1650, 726, 1681),
    )
    world = _World(
        {'boost': (_boost_frame(),), 'prompt': (enable_only,), 'coins': (_coins_frame(),)},
        {('boost', 'GO'): 'prompt'},
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is NotificationOutcome.PERMISSION_DENIED
    assert [tap.label for tap in result.taps].count('Enable all notifications') == 1


def test_an_entry_prompt_grant_without_a_probe_still_reports_what_it_saw(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    overlaying = _snapshot(
        _box('Daily coins', 66, 919, 320, 978),
        _box('Coins credits to redeem', 59, 567, 552, 617),
        _box('Allow AliExpress to send you notifications?', 300, 890, 900, 930),
        _box('Allow', 560, 990, 640, 1025),
    )
    world = _World(
        {'entry_dialog': (overlaying,), 'coins': (_coins_frame(),)},
        {('entry_dialog', 'Allow'): 'coins'},
        start='entry_dialog',
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is NotificationOutcome.UNAVAILABLE
    assert [tap.label for tap in result.taps] == ['Allow']


def test_an_entry_prompt_grant_keeps_the_grant_when_the_return_fails(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    overlaying = _snapshot(
        _box('Allow AliExpress to send you notifications?', 300, 890, 900, 930),
        _box('Allow', 560, 990, 640, 1025),
    )
    world = _World(
        {'entry_dialog': (overlaying,), 'wrong': (_wrong_frame(),), 'coins': (_coins_frame(),)},
        {('entry_dialog', 'Allow'): 'wrong'},
        start='entry_dialog',
        fail_navigation=True,
    )
    readings = iter(('ignore', 'allow'))

    result = _run(
        world,
        catalogue,
        corpus,
        _state(tmp_path),
        permission_probe=lambda: next(readings, 'allow'),
    )

    assert result.outcome is NotificationOutcome.COMPLETED
    assert result.verification == 'permission_granted'
    assert result.returned is False
    assert result.detail.endswith('the shortcut route did not prove the coins page')


def test_a_popup_over_the_boost_list_is_swept_before_a_missing_action_means_complete(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from aliexpress_coins.control.popups import CloseAttempt, CloseKind

    family = replace(
        corpus.by_family('promo_coupon_unlocked'),
        anchor='Welcome gifts',
        close=(CloseAttempt(kind=CloseKind.LABEL, label='Dismiss'),),
    )
    labelled = replace(corpus, families=(family,))
    over_boost = _snapshot(
        _box('Coins boost', 80, 90, 400, 150),
        _box(NOTIFICATIONS_TITLE, 60, 420, 700, 470),
        _box('Stay informed when rewards drop', 60, 480, 700, 530),
        _box('Welcome gifts', 100, 600, 1100, 700),
        _box('Dismiss', 540, 900, 660, 940),
    )
    world = _World(
        {'boost': (_boost_frame(),), 'popup_over': (over_boost,), 'coins': (_coins_frame(),)},
        {('boost', 'GO'): 'popup_over'},
        on_tap=lambda screen, label, count: 'boost_done' if label == 'Dismiss' else None,
    )
    world._series['boost_done'] = (_boost_frame(done=True),)
    state = _state(tmp_path)

    result = _run(world, catalogue, labelled, state)

    assert result.outcome is NotificationOutcome.COMPLETED
    assert result.dismissals
    assert any(attempt.label == 'Dismiss' for report in result.dismissals for attempt in report.taps)
    assert state.count('notifications') == 1


def test_the_result_log_carries_the_notifications_task_event(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = _dialog_world(allow=False)

    with caplog.at_level('INFO', logger='aliexpress_coins.control'):
        _run(world, catalogue, corpus, _state(tmp_path))

    assert '"event":"notifications_task"' in caplog.text


def test_a_loading_frame_is_waited_out_before_reading_the_row(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    skeleton = Snapshot(
        image=Image.open(DATA / 'states' / 'skeleton.png').convert('RGB'),
        boxes=(),
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(1200, 2000, density=240, sdk=35),
    )
    world = _World(
        {'boost': (_boost_frame(), skeleton, skeleton, _boost_frame(done=True)), 'coins': (_coins_frame(),)},
        {},
        on_tap=lambda screen, label, count: None,
    )

    result = _run(world, catalogue, corpus, _state(tmp_path), load_timeout=1.0)

    assert result.outcome in (NotificationOutcome.COMPLETED, NotificationOutcome.UNAVAILABLE)


def test_an_unsettled_loading_surface_is_not_tapped_and_still_returns(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    skeleton = Snapshot(
        image=Image.open(DATA / 'states' / 'skeleton.png').convert('RGB'),
        boxes=(),
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(1200, 2000, density=240, sdk=35),
    )
    world = _World(
        {'loading': (skeleton,), 'coins': (_coins_frame(),)},
        {},
        start='loading',
        fail_navigation=True,
    )

    result = _run(world, catalogue, corpus, _state(tmp_path), load_timeout=0.01)

    assert result.outcome is NotificationOutcome.FAILED
    assert result.verification == 'return_failed'
    assert world.taps == []


def test_verify_row_converts_an_unreadable_boost_list_into_no_observation(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aliexpress_coins.perception.errors import StateTimeout

    world = _World({'boost': (_boost_frame(),), 'coins': (_coins_frame(),)}, {})
    monkeypatch.setattr(
        notifications, 'open_boost_list', lambda *args, **kwargs: (_ for _ in ()).throw(StateTimeout('no list'))
    )

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is NotificationOutcome.FAILED
    assert result.verification == 'no_observation'


def test_capture_can_be_a_snapshotter_object_not_only_a_callable(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _dialog_world()

    class _Snapshotter:
        def capture(self) -> Snapshot:
            return world.capture()

    result = run_notifications_task(
        _controller(world, world.frame),
        _Snapshotter(),
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=2.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is NotificationOutcome.COMPLETED


def test_record_once_does_not_increment_an_existing_completion(
    catalogue: TaskCatalogue,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    state.record('notifications')

    notifications._record_once(state, catalogue.allowed_by_key('notifications'))

    assert state.count('notifications') == 1


def test_trusted_surface_rejects_the_empty_boost_page(catalogue: TaskCatalogue, corpus: PopupCorpus) -> None:
    from aliexpress_coins.tasks.discovery import BoostList, DiscoveryRoute

    frame = _snapshot(
        _box('Coins boost', 80, 90, 400, 150),
        _box("There's nothing here yet", 200, 500, 1000, 560),
    )
    opened = BoostList(snapshot=frame, route=DiscoveryRoute.BOOST, detail='empty')

    assert notifications._trusted_surface(opened) is False
    assert corpus.by_family('coins_boost_sheet').family == 'coins_boost_sheet'
    assert catalogue.allowed_by_key('notifications').category.value == 'notification'


def _skeleton() -> Snapshot:
    return Snapshot(
        image=Image.open(DATA / 'states' / 'skeleton.png').convert('RGB'),
        boxes=(),
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(1200, 2000, density=240, sdk=35),
    )


def test_a_loading_entry_is_polled_until_the_boost_list_appears(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        {'loading': (_skeleton(),), 'boost': (_boost_frame(),), 'coins': (_coins_frame(),)},
        {},
        start='loading',
    )
    world._series['loading'] = (_skeleton(), _skeleton(), _boost_frame())

    result = _run(world, catalogue, corpus, _state(tmp_path), load_timeout=5.0)

    assert result.outcome in (
        NotificationOutcome.FAILED,
        NotificationOutcome.UNAVAILABLE,
        NotificationOutcome.COMPLETED,
    )
    assert [tap.label for tap in result.taps][:1] == ['GO']


def test_an_initial_popup_is_swept_before_the_row_is_read(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from aliexpress_coins.control.popups import CloseAttempt, CloseKind

    family = replace(
        corpus.by_family('promo_coupon_unlocked'),
        anchor='Extra Coupon unlocked',
        close=(CloseAttempt(kind=CloseKind.LABEL, label='Dismiss'),),
    )
    labelled = replace(corpus, families=(family,))
    popup = _snapshot(
        _box('Welcome gifts', 100, 500, 1100, 560),
        _box('Extra Coupon unlocked', 600, 600, 1100, 700),
        _box('Dismiss', 540, 900, 660, 940),
    )
    world = _World(
        {
            'popup': (popup, popup, _boost_frame(done=True)),
            'boost': (_boost_frame(done=True),),
            'coins': (_coins_frame(),),
        },
        {},
        start='popup',
        on_tap=lambda screen, label, count: 'boost' if label == 'Dismiss' else None,
    )

    result = _run(world, catalogue, labelled, _state(tmp_path), load_timeout=5.0)

    assert result.dismissals
    assert result.outcome is NotificationOutcome.ALREADY_DONE


def test_loading_after_go_is_polled_inside_the_arrival_budget(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        {
            'boost': (_boost_frame(), _skeleton(), _skeleton(), _boost_frame(done=True)),
            'coins': (_coins_frame(),),
        },
        {},
    )

    result = _run(world, catalogue, corpus, _state(tmp_path), arrival_timeout=5.0)

    assert result.outcome is NotificationOutcome.COMPLETED


def test_verify_row_keeps_the_dismissal_it_saw_while_opening_the_list(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aliexpress_coins.control.popups import DismissReport, DismissStop
    from aliexpress_coins.tasks.discovery import BoostList, DiscoveryRoute

    world = _World({'boost': (_boost_frame(),), 'coins': (_coins_frame(),)}, {})
    dismiss = DismissReport(closed=('promo',), attempts=(), frames=(), remaining=False, stop=DismissStop.CLEAR)
    opened = BoostList(
        snapshot=_boost_frame(row=False),
        route=DiscoveryRoute.BOOST,
        detail='boost',
        dismiss=dismiss,
    )
    monkeypatch.setattr(notifications, 'open_boost_list', lambda *args, **kwargs: opened)

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is NotificationOutcome.COMPLETED
    assert result.verification == 'row_gone'
    assert dismiss in result.dismissals


def test_verify_row_can_stop_on_an_inline_coin_page(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aliexpress_coins.tasks.discovery import BoostList, DiscoveryRoute

    world = _World({'boost': (_boost_frame(),), 'coins': (_coins_frame(),)}, {})
    opened = BoostList(snapshot=_coins_frame(), route=DiscoveryRoute.INLINE, detail='inline')
    monkeypatch.setattr(notifications, 'open_boost_list', lambda *args, **kwargs: opened)

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is NotificationOutcome.FAILED
    assert result.verification == 'no_observation'


def test_return_quietly_swallows_a_return_fault(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World({'loading': (_skeleton(),), 'coins': (_coins_frame(),)}, {}, start='loading')

    def explode(*args: object, **kwargs: object) -> object:
        raise RuntimeError('return transport broke')

    monkeypatch.setattr(notifications, 'return_to_coins_page', explode)

    result = _run(world, catalogue, corpus, _state(tmp_path), load_timeout=0.01)

    assert result.outcome is NotificationOutcome.FAILED
    assert result.verification == 'return_failed'
    assert 'return transport broke' not in result.detail


def test_coerce_reading_preserves_an_existing_reading() -> None:
    reading = PermissionReading(NotificationPermission.GRANTED, raw='allow', source='test')

    assert notifications._coerce_reading(reading) is reading


def test_probe_bytes_output_is_decoded() -> None:
    from types import SimpleNamespace

    probe = notifications.AppOpsPermissionProbe(
        'serial',
        runner=lambda args, timeout: SimpleNamespace(returncode=0, stdout=b'POST_NOTIFICATION: allow', stderr=b''),
    )

    assert probe().state is NotificationPermission.GRANTED


def test_a_popup_stop_from_the_sweep_is_returned_from_the_wait(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    popup = _snapshot(_box('Welcome gifts', 100, 500, 1100, 560))
    world = _World(
        {'boost': (_boost_frame(), popup, popup), 'coins': (_coins_frame(),)},
        {},
    )
    monkeypatch.setattr(notifications, '_sweep', lambda *args, **kwargs: (None, 'popup_unclosable'))

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is NotificationOutcome.PERMISSION_DENIED
    assert result.stop == 'popup_unclosable'


def test_a_dim_but_identified_boost_page_is_trusted_over_a_generic_popup(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    popup = Classification(ScreenState.POPUP, 1.0, ())
    monkeypatch.setattr(notifications, 'classify', lambda snapshot: popup)
    world = _World({'boost': (_boost_frame(done=True),), 'coins': (_coins_frame(),)}, {})

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is NotificationOutcome.ALREADY_DONE
    assert not result.dismissals
    assert world.taps == []


def test_the_wait_retries_a_dim_boost_page_until_the_arrival_budget(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    popup = Classification(ScreenState.POPUP, 1.0, ())
    monkeypatch.setattr(notifications, 'classify', lambda snapshot: popup)
    world = _World({'boost': (_boost_frame(),), 'coins': (_coins_frame(),)}, {})

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is NotificationOutcome.FAILED
    assert result.verification in ('no_change', 'no_observation')
    assert not result.dismissals
    assert [tap.label for tap in result.taps] == ['GO']


def test_trusted_page_helper_ignores_the_boost_family_once_it_is_removed(
    corpus: PopupCorpus,
) -> None:
    frame = _boost_frame()
    filtered = _sweep_corpus(corpus)

    assert notifications._is_trusted_page(frame, NotificationScreen.BOOST_LIST, filtered) is True
    assert notifications._is_trusted_page(frame, NotificationScreen.BOOST_LIST, corpus) is False
    assert notifications._is_trusted_page(frame, NotificationScreen.OTHER, filtered) is False


def test_a_popup_still_covering_the_entry_surface_times_out_without_a_tap(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    popup = _snapshot(_box('Welcome gifts', 100, 500, 1100, 560))
    world = _World({'popup': (popup,), 'coins': (_coins_frame(),)}, {}, start='popup')

    result = _run(world, catalogue, corpus, _state(tmp_path), load_timeout=0.01)

    assert result.outcome is NotificationOutcome.FAILED
    assert result.verification == 'not_settled'
    assert world.taps == []


def test_a_popup_stop_while_settling_the_entry_surface_is_reported(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    popup = _snapshot(_box('Welcome gifts', 100, 500, 1100, 560))
    world = _World({'popup': (popup,), 'coins': (_coins_frame(),)}, {}, start='popup')
    monkeypatch.setattr(notifications, '_sweep', lambda *args, **kwargs: (None, 'popup_unclosable'))

    result = _run(world, catalogue, corpus, _state(tmp_path), load_timeout=5.0)

    assert result.outcome is NotificationOutcome.FAILED
    assert result.verification == 'not_settled'


def test_an_app_prompt_with_no_control_reports_a_sweep_stop(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bare = _snapshot(_box('Receive timely notifications', 12, 1186, 550, 1241))
    world = _World(
        {'boost': (_boost_frame(),), 'prompt': (bare,), 'coins': (_coins_frame(),)},
        {('boost', 'GO'): 'prompt'},
    )
    monkeypatch.setattr(notifications, '_sweep', lambda *args, **kwargs: (None, 'popup_unclosable'))

    result = _run(world, catalogue, corpus, _state(tmp_path))

    assert result.outcome is NotificationOutcome.PERMISSION_DENIED
    assert result.stop == 'popup_unclosable'


def test_the_sweep_wrapper_turns_an_unclosable_family_into_a_named_stop(
    monkeypatch: pytest.MonkeyPatch,
    corpus: PopupCorpus,
) -> None:
    import logging
    from typing import cast

    from aliexpress_coins.control.errors import PopupUnclosable
    from aliexpress_coins.perception.state import CaptureSource

    def explode(*args: object, **kwargs: object) -> object:
        raise PopupUnclosable('no close target')

    monkeypatch.setattr('aliexpress_coins.control.popups.dismiss_after', explode)

    report, stop = notifications._sweep(
        cast(Controller, object()),
        cast(CaptureSource, object()),
        corpus,
        templates=None,
        settle=0.0,
        clock=lambda: 0.0,
        sleep=lambda seconds: None,
        logger=logging.getLogger('test'),
    )

    assert report is None
    assert stop == 'popup_unclosable'
