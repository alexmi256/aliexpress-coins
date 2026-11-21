"""Popup dismissal loop over the committed corpus and fixtures (issue #18).

The loop is driven with real reviewed frames and a recording fake device. No
test reaches adb: the controller and capture seam are the only device-facing
pieces, exactly as docs/03-conventions.md requires.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

from aliexpress_coins.control.controller import Controller
from aliexpress_coins.control.errors import PopupUnclosable
from aliexpress_coins.control.popups import (
    DEFAULT_MAX_DISMISS_ROUNDS,
    CloseAttempt,
    CloseKind,
    DismissOutcome,
    DismissStop,
    PopupCorpus,
    dismiss,
    dismiss_after,
    load_popup_corpus,
)
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.ocr import RapidOcrEngine
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.state import ScreenState

ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = ROOT / 'specs' / 'popups.yaml'
POPUP_FIXTURES = ROOT / 'tests' / 'data' / 'popups'
PAGE_FIXTURE = ROOT / 'tests' / 'data' / 'states' / 'coins.png'
CAPTURED_AT = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)

_FIXTURES: tuple[tuple[str, str, CloseKind], ...] = (
    ('system_notification_permission.png', 'system_notification_permission', CloseKind.LABEL),
    ('launch_welcome_gifts.png', 'launch_welcome_gifts', CloseKind.TEMPLATE),
    ('promo_coupon_unlocked.png', 'promo_coupon_unlocked', CloseKind.TEMPLATE),
    ('push_permission_prompt.png', 'push_permission_prompt', CloseKind.TEMPLATE),
    ('push_permission_prompt_sheet.png', 'push_permission_prompt', CloseKind.LABEL),
    ('coins_boost_sheet.png', 'coins_boost_sheet', CloseKind.TEMPLATE),
    ('promo_bundle_deals_badge.png', 'promo_bundle_deals_badge', CloseKind.TEMPLATE),
)


class _RecordingDevice:
    """The only device seam: records taps instead of spawning adb."""

    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))


class _Capture:
    """A scripted frame sequence; it holds the last frame once exhausted."""

    def __init__(self, frames: Sequence[Snapshot]) -> None:
        if not frames:
            raise ValueError('a capture sequence needs at least one frame')
        self._frames = tuple(frames)
        self._index = 0

    def capture(self) -> Snapshot:
        frame = self._frames[min(self._index, len(self._frames) - 1)]
        self._index += 1
        return frame


class _SteppingClock:
    """An injected clock that advances on every read."""

    def __init__(self, step: float = 1.0) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        current = self.value
        self.value += self.step
        return current

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def _snapshot(path: Path, engine: RapidOcrEngine, boxes: tuple[TextBox, ...] = ()) -> Snapshot:
    with Image.open(path) as opened:
        image = opened.convert('RGB')
    return Snapshot(
        image=image,
        boxes=(*engine.read(image), *boxes),
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(image.width, image.height, density=240, sdk=35),
    )


def _box(text: str, left: float, top: float, right: float, bottom: float) -> TextBox:
    return TextBox(
        text=text,
        box=((left, top), (right, top), (right, bottom), (left, bottom)),
        score=0.99,
    )


def _with_boxes(base: Snapshot, *boxes: TextBox) -> Snapshot:
    return Snapshot(
        image=base.image,
        boxes=(*base.boxes, *boxes),
        captured_at=base.captured_at,
        geometry=base.geometry,
    )


def _controller(device: _RecordingDevice, frame: Snapshot, *, dry_run: bool = False) -> Controller:
    return Controller.from_frame(
        device,
        frame,
        dry_run=dry_run,
        rng=random.Random(0),
        logger=logging.getLogger('aliexpress_coins.control.test'),
    )


@pytest.fixture(scope='module')
def engine() -> RapidOcrEngine:
    return RapidOcrEngine()


@pytest.fixture(scope='module')
def corpus() -> PopupCorpus:
    return load_popup_corpus(CORPUS_PATH)


@pytest.fixture(scope='module')
def page(engine: RapidOcrEngine) -> Snapshot:
    return _snapshot(PAGE_FIXTURE, engine)


@pytest.fixture(scope='module')
def popup_frames(engine: RapidOcrEngine) -> dict[str, Snapshot]:
    return {name: _snapshot(POPUP_FIXTURES / name, engine) for name, _, _ in _FIXTURES}


@pytest.mark.parametrize(('name', 'family', 'kind'), _FIXTURES)
def test_each_committed_family_closes_in_one_tap(
    corpus: PopupCorpus,
    page: Snapshot,
    popup_frames: dict[str, Snapshot],
    name: str,
    family: str,
    kind: CloseKind,
) -> None:
    first = popup_frames[name]
    device = _RecordingDevice()
    controller = _controller(device, first)

    report = dismiss(controller, _Capture((first, page)), corpus)

    assert report.closed == (family,)
    assert report.remaining is False
    assert report.stop is DismissStop.CLEAR
    assert report.final_state is ScreenState.PAGE
    assert report.dismissed == report.closed
    assert report.bounded is False
    assert len(device.taps) == 1
    assert controller.tap_count == 1
    assert len(report.taps) == 1
    assert report.taps[0].family == family
    assert report.taps[0].kind is kind
    assert report.taps[0].tap is not None


def test_a_chain_is_followed_from_one_family_to_the_next(
    corpus: PopupCorpus,
    page: Snapshot,
    popup_frames: dict[str, Snapshot],
) -> None:
    launch = popup_frames['launch_welcome_gifts.png']
    badge = popup_frames['promo_bundle_deals_badge.png']
    device = _RecordingDevice()
    controller = _controller(device, launch)

    report = dismiss(controller, _Capture((launch, badge, page)), corpus)

    assert report.closed == ('launch_welcome_gifts', 'promo_bundle_deals_badge')
    assert report.stop is DismissStop.CLEAR
    assert report.remaining is False
    assert [frame.state for frame in report.frames] == [
        ScreenState.POPUP,
        ScreenState.UNKNOWN,
        ScreenState.PAGE,
    ]
    assert [frame.family for frame in report.frames] == [
        'launch_welcome_gifts',
        'promo_bundle_deals_badge',
        None,
    ]
    assert len(device.taps) == 2


def test_an_unknown_popup_fails_before_any_tap(
    corpus: PopupCorpus,
    popup_frames: dict[str, Snapshot],
) -> None:
    launch = popup_frames['launch_welcome_gifts.png']
    system_only = replace(corpus, families=(corpus.by_family('system_notification_permission'),))
    device = _RecordingDevice()
    controller = _controller(device, launch)

    with pytest.raises(PopupUnclosable, match='no corpus family anchor matched'):
        dismiss(controller, _Capture((launch,)), system_only)

    assert device.taps == []
    assert controller.tap_count == 0


def test_a_dry_run_reports_the_attempt_without_tapping(
    corpus: PopupCorpus,
    popup_frames: dict[str, Snapshot],
) -> None:
    launch = popup_frames['launch_welcome_gifts.png']
    device = _RecordingDevice()
    controller = _controller(device, launch, dry_run=True)

    report = dismiss_after(controller, _Capture((launch,)), corpus)

    assert device.taps == []
    assert controller.tap_count == 1
    assert report.dry_run is True
    assert report.closed == ()
    assert report.remaining is True
    assert report.stop is DismissStop.DRY_RUN
    assert report.unclosed == ('launch_welcome_gifts',)
    assert len(report.taps) == 1
    assert report.taps[0].tap is not None
    assert report.taps[0].tap.suppressed is True


def test_a_wide_cancel_style_button_never_matches_as_a_close(
    corpus: PopupCorpus,
    popup_frames: dict[str, Snapshot],
) -> None:
    launch = popup_frames['launch_welcome_gifts.png']
    wide = _box('Close', 350, 1000, 750, 1045)
    frame = _with_boxes(launch, wide)
    family = replace(
        corpus.by_family('launch_welcome_gifts'),
        close=(CloseAttempt(kind=CloseKind.LABEL, label='Close'),),
    )
    custom = replace(corpus, families=(family,))
    device = _RecordingDevice()
    controller = _controller(device, frame)

    with pytest.raises(PopupUnclosable, match='popup_unclosable'):
        dismiss(controller, _Capture((frame,)), custom)

    assert device.taps == []


def test_a_small_close_label_is_tapped_inside_its_own_box(
    corpus: PopupCorpus,
    page: Snapshot,
    popup_frames: dict[str, Snapshot],
) -> None:
    launch = popup_frames['launch_welcome_gifts.png']
    wide = _box('Close', 350, 1000, 750, 1045)
    small = _box('Close', 550, 1500, 650, 1540)
    frame = _with_boxes(launch, wide, small)
    family = replace(
        corpus.by_family('launch_welcome_gifts'),
        close=(CloseAttempt(kind=CloseKind.LABEL, label='Close'),),
    )
    custom = replace(corpus, families=(family,))
    device = _RecordingDevice()
    controller = _controller(device, frame)

    report = dismiss(controller, _Capture((frame, page)), custom)

    assert report.closed == ('launch_welcome_gifts',)
    assert len(device.taps) == 1
    assert 550 <= device.taps[0][0] <= 650
    assert 1500 <= device.taps[0][1] <= 1540


def test_a_fraction_is_a_last_resort_after_the_anchor_matched(
    corpus: PopupCorpus,
    page: Snapshot,
    popup_frames: dict[str, Snapshot],
) -> None:
    launch = popup_frames['launch_welcome_gifts.png']
    family = replace(
        corpus.by_family('launch_welcome_gifts'),
        close=(CloseAttempt(kind=CloseKind.FRACTION, fraction=(0.499, 0.833)),),
    )
    custom = replace(corpus, families=(family,))
    device = _RecordingDevice()
    controller = _controller(device, launch)

    report = dismiss(controller, _Capture((launch, page)), custom)

    assert report.closed == ('launch_welcome_gifts',)
    assert len(device.taps) == 1
    assert report.taps[0].kind is CloseKind.FRACTION
    assert report.taps[0].target == (599, 1666)


def test_a_fraction_is_not_used_on_a_frame_that_is_not_a_popup(
    corpus: PopupCorpus,
    popup_frames: dict[str, Snapshot],
) -> None:
    sheet = popup_frames['coins_boost_sheet.png']
    family = replace(
        corpus.by_family('coins_boost_sheet'),
        close=(CloseAttempt(kind=CloseKind.FRACTION, fraction=(0.941, 0.090)),),
    )
    custom = replace(corpus, families=(family,))
    device = _RecordingDevice()
    controller = _controller(device, sheet)

    report = dismiss(controller, _Capture((sheet,)), custom)

    assert device.taps == []
    assert report.closed == ()
    assert report.remaining is True
    assert report.stop is DismissStop.UNCLOSABLE
    assert report.attempts[0].outcome is DismissOutcome.NOT_APPLICABLE


def test_a_fraction_can_close_an_unmodelled_overlay_after_its_anchor_matches(
    corpus: PopupCorpus,
    page: Snapshot,
    popup_frames: dict[str, Snapshot],
) -> None:
    prompt = popup_frames['push_permission_prompt.png']
    family = replace(
        corpus.by_family('push_permission_prompt'),
        close=(CloseAttempt(kind=CloseKind.FRACTION, fraction=(0.95, 0.12)),),
    )
    custom = replace(corpus, families=(family,))
    device = _RecordingDevice()
    controller = _controller(device, prompt)

    report = dismiss(controller, _Capture((prompt, page)), custom)

    assert report.closed == ('push_permission_prompt',)
    assert report.taps[0].kind is CloseKind.FRACTION
    assert len(device.taps) == 1


def test_a_declared_fail_attempt_stops_the_run_without_a_tap(
    corpus: PopupCorpus,
    popup_frames: dict[str, Snapshot],
) -> None:
    launch = popup_frames['launch_welcome_gifts.png']
    family = replace(
        corpus.by_family('launch_welcome_gifts'),
        close=(CloseAttempt(kind=CloseKind.FAIL, reason='no close control exists'),),
    )
    custom = replace(corpus, families=(family,))
    device = _RecordingDevice()
    controller = _controller(device, launch)

    with pytest.raises(PopupUnclosable, match='popup_unclosable'):
        dismiss(controller, _Capture((launch,)), custom)

    assert device.taps == []


def test_a_non_blocking_family_that_cannot_close_is_reported_not_fatal(
    corpus: PopupCorpus,
    popup_frames: dict[str, Snapshot],
) -> None:
    badge = popup_frames['promo_bundle_deals_badge.png']
    family = replace(
        corpus.by_family('promo_bundle_deals_badge'),
        close=(CloseAttempt(kind=CloseKind.LABEL, label='Not on this frame'),),
    )
    custom = replace(corpus, families=(family,))
    device = _RecordingDevice()
    controller = _controller(device, badge)

    report = dismiss(controller, _Capture((badge,)), custom)

    assert device.taps == []
    assert report.closed == ()
    assert report.remaining is True
    assert report.stop is DismissStop.UNCLOSABLE
    assert report.unclosed == ('promo_bundle_deals_badge',)
    assert report.attempts[0].outcome is DismissOutcome.NOT_FOUND


def test_the_repeated_target_guard_stops_a_reopened_family(
    corpus: PopupCorpus,
    popup_frames: dict[str, Snapshot],
) -> None:
    launch = popup_frames['launch_welcome_gifts.png']
    badge = popup_frames['promo_bundle_deals_badge.png']
    device = _RecordingDevice()
    controller = _controller(device, launch)

    report = dismiss(controller, _Capture((launch, badge, launch, launch)), corpus, max_rounds=4)

    assert report.closed == ('launch_welcome_gifts', 'promo_bundle_deals_badge')
    assert report.remaining is True
    assert report.stop is DismissStop.UNCLOSABLE
    assert report.unclosed == ('launch_welcome_gifts',)
    assert len(device.taps) == 3
    assert report.attempts[-1].outcome is DismissOutcome.REPEATED


def test_a_popup_that_does_not_close_ends_with_a_warning(
    corpus: PopupCorpus,
    popup_frames: dict[str, Snapshot],
) -> None:
    launch = popup_frames['launch_welcome_gifts.png']
    device = _RecordingDevice()
    controller = _controller(device, launch)

    report = dismiss(controller, _Capture((launch, launch)), corpus, max_rounds=4)

    assert report.closed == ()
    assert report.remaining is True
    assert report.stop is DismissStop.UNCLOSABLE
    assert report.unclosed == ('launch_welcome_gifts',)
    assert len(device.taps) == 1
    assert report.attempts[-1].outcome is DismissOutcome.REPEATED


def test_a_missing_glyph_is_unclosable_without_a_tap(
    corpus: PopupCorpus,
    popup_frames: dict[str, Snapshot],
) -> None:
    launch = popup_frames['launch_welcome_gifts.png']
    family = replace(
        corpus.by_family('launch_welcome_gifts'),
        close=(
            CloseAttempt(
                kind=CloseKind.TEMPLATE,
                template=(ROOT / 'assets' / 'templates' / 'promo_coupon_unlocked.png').resolve(),
                source_width=1200,
            ),
        ),
    )
    custom = replace(corpus, families=(family,))
    device = _RecordingDevice()
    controller = _controller(device, launch)

    with pytest.raises(PopupUnclosable, match='popup_unclosable'):
        dismiss(controller, _Capture((launch,)), custom)

    assert device.taps == []


def test_settle_time_is_injected_not_slept_through(
    corpus: PopupCorpus,
    page: Snapshot,
    popup_frames: dict[str, Snapshot],
) -> None:
    system = popup_frames['system_notification_permission.png']
    device = _RecordingDevice()
    controller = _controller(device, system)
    sleeps: list[float] = []

    report = dismiss(
        controller,
        _Capture((system, page)),
        corpus,
        settle=0.25,
        sleep=sleeps.append,
    )

    assert report.closed == ('system_notification_permission',)
    assert sleeps == [0.25]


def test_a_chain_that_reopens_each_other_stops_at_the_round_bound(
    corpus: PopupCorpus,
    popup_frames: dict[str, Snapshot],
) -> None:
    launch = popup_frames['launch_welcome_gifts.png']
    badge = popup_frames['promo_bundle_deals_badge.png']
    frames = (launch, badge) * (DEFAULT_MAX_DISMISS_ROUNDS // 2)
    device = _RecordingDevice()
    controller = _controller(device, launch)

    report = dismiss(controller, _Capture(frames), corpus)

    assert report.stop is DismissStop.MAX_ROUNDS
    assert report.bounded is True
    assert report.remaining is True
    assert report.unclosed == ('promo_bundle_deals_badge',)
    assert len(report.frames) == DEFAULT_MAX_DISMISS_ROUNDS
    assert len(device.taps) == DEFAULT_MAX_DISMISS_ROUNDS


def test_the_wall_clock_budget_can_stop_the_sweep_first(
    corpus: PopupCorpus,
    popup_frames: dict[str, Snapshot],
) -> None:
    launch = popup_frames['launch_welcome_gifts.png']
    device = _RecordingDevice()
    controller = _controller(device, launch)
    clock = _SteppingClock(step=1.0)

    report = dismiss(
        controller,
        _Capture((launch,)),
        corpus,
        budget=0.5,
        clock=clock,
        sleep=clock.sleep,
    )

    assert report.stop is DismissStop.BUDGET
    assert report.bounded is True
    assert len(report.frames) == 1
    assert len(device.taps) == 1


def test_a_capture_callable_is_accepted(
    corpus: PopupCorpus,
    page: Snapshot,
    popup_frames: dict[str, Snapshot],
) -> None:
    system = popup_frames['system_notification_permission.png']
    frames = iter((system, page))
    device = _RecordingDevice()
    controller = _controller(device, system)

    report = dismiss(controller, lambda: next(frames), corpus)

    assert report.closed == ('system_notification_permission',)
    assert len(device.taps) == 1


def test_the_report_renders_structured_records(
    corpus: PopupCorpus,
    page: Snapshot,
    popup_frames: dict[str, Snapshot],
) -> None:
    launch = popup_frames['launch_welcome_gifts.png']
    device = _RecordingDevice()
    controller = _controller(device, launch)

    report = dismiss(controller, _Capture((launch, page)), corpus)
    data = report.as_dict()

    assert data['closed'] == ['launch_welcome_gifts']
    assert data['remaining'] is False
    assert data['stop'] == 'clear'
    assert data['final_state'] == 'page'
    frames = data['frames']
    attempts = data['attempts']
    assert isinstance(frames, list)
    assert isinstance(attempts, list)
    assert frames[0]['state'] == 'popup'
    assert frames[0]['family'] == 'launch_welcome_gifts'
    assert attempts[0]['kind'] == 'template'
    assert attempts[0]['outcome'] == 'executed'
    assert 'tap' in attempts[0]


@pytest.mark.parametrize(
    ('options', 'message'),
    [
        ({'max_rounds': 0}, 'max_rounds'),
        ({'budget': 0}, 'budget'),
        ({'settle': -1}, 'settle'),
    ],
)
def test_invalid_bounds_are_rejected(
    corpus: PopupCorpus,
    popup_frames: dict[str, Snapshot],
    options: dict[str, float],
    message: str,
) -> None:
    launch = popup_frames['launch_welcome_gifts.png']
    device = _RecordingDevice()
    controller = _controller(device, launch)

    with pytest.raises(ValueError, match=message):
        dismiss(controller, _Capture((launch,)), corpus, **options)  # type: ignore[arg-type]
