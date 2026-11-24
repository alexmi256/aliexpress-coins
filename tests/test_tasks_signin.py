"""The daily sign-in executor over the measured fixtures (issue #23).

The device is simulated at the capture and input seams only: every frame comes
from a reviewed fixture or a hand-built box list, taps are recorded by a fake
device, and time is injected. No test reaches adb.
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
from aliexpress_coins.control.popups import PopupCorpus, load_popup_corpus
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.ocr import RapidOcrEngine
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks.catalogue import DispatchAuthorization, load_task_catalogue
from aliexpress_coins.tasks.discovery import EMPTY_BOOST_MARKER, DiscoveryRoute, enumerate_roster
from aliexpress_coins.tasks.dispatch import DispatchGate
from aliexpress_coins.tasks.signin import (
    BOOST_KEY,
    REDEEM_INTERSTITIAL,
    STREAK_KEY,
    ClaimOutcome,
    SigninOutcome,
    _claim_balance,
    claim_boost_checkin,
    claim_daily_signins,
    claim_signins,
    claim_streak,
    find_boost_control,
    find_streak_control,
    read_coin_balance,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'tests' / 'data'
STATES = DATA / 'states'
CORPUS_PATH = ROOT / 'specs' / 'popups.yaml'
CAPTURED_AT = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


class _RecordingDevice:
    """The input seam: record taps instead of spawning adb."""

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
        self.calls = 0

    def capture(self) -> Snapshot:
        self.calls += 1
        frame = self._frames[min(self._index, len(self._frames) - 1)]
        self._index += 1
        return frame


class _ForbiddenCapture:
    """A capture that proves state skipped the claim before touching the screen."""

    def __init__(self) -> None:
        self.calls = 0

    def capture(self) -> Snapshot:
        self.calls += 1
        raise AssertionError('state was already done; the executor must not capture')


class _SteppingClock:
    """An injected clock that advances on every read and sleep."""

    def __init__(self, step: float = 1.0) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        current = self.value
        self.value += self.step
        return current

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def _box(
    text: str,
    left: float,
    top: float,
    right: float,
    bottom: float,
    score: float = 0.99,
) -> TextBox:
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


def _signin_authorization() -> DispatchAuthorization:
    """Mint a real sign-in token through the gate, never by hand."""
    catalogue = load_task_catalogue(ROOT / 'specs' / 'tasks.yaml')
    frame = _snapshot(
        _box('Daily coins', 100, 260, 700, 300),
        _box('Collect', 980, 280, 1040, 320),
    )
    entry = next(item for item in enumerate_roster(frame, catalogue).entries if item.key == 'daily_checkin')
    authorization = DispatchGate(catalogue).authorize(entry).authorization
    assert authorization is not None
    return authorization


def _load(path: Path, engine: RapidOcrEngine) -> Snapshot:
    with Image.open(path) as opened:
        image = opened.convert('RGB')
    return Snapshot(
        image=image,
        boxes=engine.read(image),
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(image.width, image.height, density=240, sdk=35),
    )


def _rebox(snapshot: Snapshot, boxes: Sequence[TextBox]) -> Snapshot:
    return Snapshot(
        image=snapshot.image,
        boxes=tuple(boxes),
        captured_at=snapshot.captured_at,
        geometry=snapshot.geometry,
    )


def _streak_frame(
    base: Snapshot,
    *,
    balance: str | None = '40',
    collect: bool = True,
    tomorrow: bool = False,
) -> Snapshot:
    boxes = [
        _box('Coins', 101, 194, 254, 242),
        _box('Coins credits to redeem', 59, 567, 552, 617),
        _box('Daily coins', 66, 919, 320, 978),
        _box('Earn more >', 917, 926, 1133, 967),
        _box('Today', 109, 1013, 210, 1063),
        _box('Day 5', 634, 1015, 731, 1063),
    ]
    if balance is not None:
        boxes.append(_box(balance, 149, 305, 285, 404))
    if collect:
        boxes.append(_box('Collect', 854, 1067, 1041, 1119))
    if tomorrow:
        boxes.append(_box('Tomorrow', 345, 1017, 499, 1051))
    return _rebox(base, boxes)


def _boost_without_row(base: Snapshot) -> Snapshot:
    boxes = tuple(
        box
        for box in base.boxes
        if box.text not in {'Daily check-in', "Finish today's check-in"}
        and not (box.text == 'GO' and box.bbox[1] < 450)
    )
    return _rebox(base, boxes)


def _empty_boost(base: Snapshot) -> Snapshot:
    return _rebox(
        base,
        (
            _box('Coins boost', 50, 470, 421, 541),
            _box(EMPTY_BOOST_MARKER, 100, 1400, 1100, 1460),
        ),
    )


def _boost_without_action(base: Snapshot) -> Snapshot:
    boxes = tuple(box for box in base.boxes if not (box.text == 'GO' and box.bbox[1] < 450))
    return _rebox(base, boxes)


def _with_box(snapshot: Snapshot, box: TextBox) -> Snapshot:
    return _rebox(snapshot, (*snapshot.boxes, box))


def _redeem(snapshot: Snapshot) -> Snapshot:
    return _with_box(snapshot, _box('YOU CAN NOW REDEEM!', 100, 700, 1100, 760))


def _unmatched_popup(snapshot: Snapshot) -> Snapshot:
    return _with_box(snapshot, _box('Tap to claim', 100, 700, 500, 760))


def _controller(device: _RecordingDevice, frame: Snapshot) -> Controller:
    return Controller.from_frame(device, frame, rng=random.Random(0))


def _state(tmp_path: Path) -> DailyStateStore:
    return DailyStateStore(tmp_path / 'state', clock=lambda: CAPTURED_AT)


@pytest.fixture(scope='module')
def engine() -> RapidOcrEngine:
    return RapidOcrEngine()


@pytest.fixture(scope='module')
def corpus() -> PopupCorpus:
    return load_popup_corpus(CORPUS_PATH)


@pytest.fixture(scope='module')
def frames(engine: RapidOcrEngine) -> dict[str, Snapshot]:
    return {
        'coins': _load(STATES / 'coins.png', engine),
        'state_boost': _load(STATES / 'boost.png', engine),
        'real_boost': _load(STATES / 'boost_daily_checkin.png', engine),
        'merged_card': _load(STATES / 'coins_merged_card.png', engine),
        'boost': _load(DATA / 'boost.png', engine),
        'launch': _load(DATA / 'popups' / 'launch_welcome_gifts.png', engine),
        'skeleton': _load(STATES / 'skeleton.png', engine),
    }


def test_read_coin_balance_reads_the_only_bare_number_in_the_top_quarter() -> None:
    snapshot = _snapshot(
        _box('Coins', 101, 194, 254, 242),
        _box('1,240', 149, 305, 285, 404),
        _box('+3~6', 332, 852, 512, 903),
    )

    assert read_coin_balance(snapshot) == 1240.0


def test_read_coin_balance_ignores_status_bar_currency_and_ocr_noise() -> None:
    snapshot = _snapshot(
        _box('40', 196, 33, 254, 77),
        _box('C$0.59', 37, 305, 364, 404),
        _box('42', 149, 350, 285, 410, score=0.2),
    )

    assert read_coin_balance(snapshot) is None
    assert read_coin_balance(_snapshot()) is None


@pytest.mark.slow
def test_find_streak_control_returns_the_measured_collect(frames: dict[str, Snapshot]) -> None:
    control = find_streak_control(frames['coins'])

    assert control is not None
    assert control.text == 'Collect'


@pytest.mark.slow
def test_find_streak_control_reads_a_collect_in_the_merged_card_band(
    frames: dict[str, Snapshot],
) -> None:
    """The 2026-11-23 coin page: the card and the first task row share one band.

    ``Daily coins``, the day chips, ``Collect``, ``Worth C$0.01`` and the
    ``Daily check-in`` row all chain into one OCR cluster on this frame. The
    control is in that header band, so a search that only looked below the
    header found nothing and left the daily check-in unclaimed (#84).
    """
    control = find_streak_control(frames['merged_card'])

    assert control is not None
    assert control.text == 'Collect'
    # The measured Collect button, not the Daily check-in GO below it.
    assert control.center == pytest.approx((946.0, 1092.5), abs=2.0)


def test_find_streak_control_is_none_without_the_section_marker(frames: dict[str, Snapshot]) -> None:
    assert find_streak_control(frames['boost']) is None


def test_find_streak_control_does_not_reach_a_task_row_below_the_card(frames: dict[str, Snapshot]) -> None:
    frame = _streak_frame(frames['coins'], collect=False)
    snapshot = _rebox(
        frame,
        (
            *frame.boxes,
            _box('Mystery task', 181, 1600, 432, 1643),
            _box('Collect', 854, 1640, 1041, 1692),
        ),
    )

    assert find_streak_control(snapshot) is None


def test_find_boost_control_reads_the_rows_own_action(frames: dict[str, Snapshot]) -> None:
    row, action = find_boost_control(frames['boost'])

    assert row is not None
    assert 'Daily check-in' in row.text
    assert action is not None
    assert action.text == 'GO'


def test_find_boost_control_is_none_when_the_row_is_missing(frames: dict[str, Snapshot]) -> None:
    row, action = find_boost_control(frames['state_boost'])

    assert row is None
    assert action is None


def test_find_boost_control_ignores_a_title_that_has_no_row(
    frames: dict[str, Snapshot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr('aliexpress_coins.tasks.signin.group_rows', lambda *_args, **_kwargs: ())

    row, action = find_boost_control(frames['boost'])

    assert row is None
    assert action is None


def test_claim_streak_completed_when_the_control_is_absent_on_the_fresh_frame(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = _streak_frame(frames['coins'])
    after = _streak_frame(frames['coins'], collect=False, tomorrow=True)
    device = _RecordingDevice()
    state = _state(tmp_path)

    result = claim_streak(
        _controller(device, before),
        _Capture([before, after]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=1.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.COMPLETED
    assert result.verification == 'absent_control'
    assert result.completed is True
    assert result.recorded is True
    assert result.tapped is True
    assert result.control is not None
    assert result.tap is not None
    assert result.dismiss is not None
    assert len(device.taps) == 1
    assert state.done_today(STREAK_KEY)
    assert result.as_dict()['outcome'] == 'completed'


def test_claim_streak_completed_by_the_claimed_card_marker(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = _streak_frame(frames['coins'])
    after = _streak_frame(frames['coins'], collect=True, tomorrow=True)

    result = claim_streak(
        _controller(_RecordingDevice(), before),
        _Capture([before, after]),
        corpus,
        _state(tmp_path),
        authorization=_signin_authorization(),
        timeout=1.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.COMPLETED
    assert result.verification == 'claimed_marker'


def test_claim_streak_accepts_a_balance_delta_as_secondary_evidence(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = _streak_frame(frames['coins'], balance='40')
    after = _streak_frame(frames['coins'], balance='42')
    state = _state(tmp_path)

    result = claim_streak(
        _controller(_RecordingDevice(), before),
        _Capture([before, after]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=1.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.COMPLETED
    assert result.verification == 'balance_delta'
    assert result.balance_before == 40.0
    assert result.balance_after == 42.0
    assert state.done_today(STREAK_KEY)


def test_claim_streak_without_a_control_is_unavailable_and_taps_nothing(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    frame = _streak_frame(frames['coins'], collect=False)
    device = _RecordingDevice()
    state = _state(tmp_path)

    result = claim_streak(
        _controller(device, frame),
        _Capture([frame]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=1.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.UNAVAILABLE
    assert result.unavailable is True
    assert result.verification == 'not_found'
    assert result.tapped is False
    assert device.taps == []
    assert state.done_today(STREAK_KEY) is False


@pytest.mark.slow
def test_claim_streak_claims_a_collect_in_the_merged_card_band(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    """The live coin page's Collect is claimable and verified by its absence."""
    before = frames['merged_card']
    after = _rebox(before, tuple(box for box in before.boxes if box.text != 'Collect'))
    device = _RecordingDevice()
    state = _state(tmp_path)

    result = claim_streak(
        _controller(device, before),
        _Capture([before, after, after, after]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=2.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.completed is True
    assert result.verification == 'absent_control'
    assert result.control is not None
    assert result.control.text == 'Collect'
    assert len(device.taps) == 1
    assert state.done_today(STREAK_KEY)


@pytest.mark.slow
def test_claim_streak_waits_for_the_streak_surface_after_the_boost_page(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    """A shortcut that has not landed yet is not an absent Collect control.

    The runner fires the shortcut and calls this executor immediately. When the
    capture still shows the boost page, the executor waits for the coin page
    instead of reporting ``unavailable`` from the wrong surface (#84).
    """
    before = frames['merged_card']
    after = _rebox(before, tuple(box for box in before.boxes if box.text != 'Collect'))
    device = _RecordingDevice()
    state = _state(tmp_path)

    result = claim_streak(
        _controller(device, before),
        _Capture([frames['real_boost'], before, after, after, after]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=3.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.completed is True
    assert len(device.taps) == 1
    assert state.done_today(STREAK_KEY)


def test_claim_streak_reports_a_streak_surface_that_never_appears(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    device = _RecordingDevice()

    result = claim_streak(
        _controller(device, frames['real_boost']),
        _Capture([frames['real_boost']]),
        corpus,
        _state(tmp_path),
        authorization=_signin_authorization(),
        timeout=1.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.UNAVAILABLE
    assert result.verification == 'not_found'
    assert 'did not appear' in result.detail
    assert device.taps == []


def test_claim_streak_accepts_a_callable_capture_seam(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    frame = _streak_frame(frames['coins'], collect=False)

    result = claim_streak(
        _controller(_RecordingDevice(), frame),
        lambda: frame,
        corpus,
        _state(tmp_path),
        authorization=_signin_authorization(),
        timeout=1.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.unavailable is True


def test_claim_streak_unverified_is_not_recorded(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    frame = _streak_frame(frames['coins'])
    state = _state(tmp_path)
    device = _RecordingDevice()

    result = claim_streak(
        _controller(device, frame),
        _Capture([frame, frame]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=2.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.UNVERIFIED
    assert result.unverified is True
    assert result.verification == 'no_evidence'
    assert result.recorded is False
    assert len(device.taps) == 1
    assert state.done_today(STREAK_KEY) is False


def test_claim_streak_unverified_when_the_streak_section_vanishes(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = _streak_frame(frames['coins'])
    elsewhere = _rebox(frames['coins'], [_box('Collect', 854, 1067, 1041, 1119)])
    state = _state(tmp_path)

    result = claim_streak(
        _controller(_RecordingDevice(), before),
        _Capture([before, elsewhere]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=2.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.UNVERIFIED
    assert result.recorded is False
    assert state.done_today(STREAK_KEY) is False


def test_claim_streak_does_not_verify_from_a_claimed_task_row_below_the_card(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = _streak_frame(frames['coins'])
    after = _rebox(
        before,
        (
            *before.boxes,
            _box('Mystery task', 181, 1600, 432, 1643),
            _box('Claimed', 800, 1600, 1000, 1643),
            _box('GO', 978, 1610, 1060, 1660),
        ),
    )
    state = _state(tmp_path)

    result = claim_streak(
        _controller(_RecordingDevice(), before),
        _Capture([before, after]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=2.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.UNVERIFIED
    assert result.recorded is False
    assert state.done_today(STREAK_KEY) is False


@pytest.mark.slow
def test_claim_streak_sweeps_a_popup_before_looking_for_the_control(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = _streak_frame(frames['coins'])
    after = _streak_frame(frames['coins'], collect=False, tomorrow=True)
    device = _RecordingDevice()

    result = claim_streak(
        _controller(device, frames['launch']),
        _Capture([frames['launch'], frames['launch'], before, after]),
        corpus,
        _state(tmp_path),
        authorization=_signin_authorization(),
        timeout=2.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.completed is True
    assert len(device.taps) == 2


def test_claim_streak_waits_for_a_loading_frame_to_settle(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = _streak_frame(frames['coins'])
    after = _streak_frame(frames['coins'], collect=False, tomorrow=True)

    result = claim_streak(
        _controller(_RecordingDevice(), frames['skeleton']),
        _Capture([frames['skeleton'], before, after]),
        corpus,
        _state(tmp_path),
        authorization=_signin_authorization(),
        timeout=2.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.completed is True


def test_claim_streak_is_unavailable_when_the_entry_never_settles(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    device = _RecordingDevice()
    state = _state(tmp_path)

    result = claim_streak(
        _controller(device, frames['skeleton']),
        _Capture([frames['skeleton']]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=1.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.UNAVAILABLE
    assert result.verification == 'not_settled'
    assert device.taps == []
    assert state.done_today(STREAK_KEY) is False


def test_claim_streak_skips_from_state_before_any_capture(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    state.record(STREAK_KEY)
    capture = _ForbiddenCapture()
    device = _RecordingDevice()

    result = claim_streak(
        _controller(device, frames['coins']),
        capture,
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=1.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.ALREADY_DONE
    assert result.already_done is True
    assert result.verification == 'state'
    assert capture.calls == 0
    assert device.taps == []


def test_claim_streak_uses_a_supplied_snapshot_for_the_decision(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = _streak_frame(frames['coins'])
    after = _streak_frame(frames['coins'], collect=False)
    capture = _Capture([after])

    result = claim_streak(
        _controller(_RecordingDevice(), before),
        capture,
        corpus,
        _state(tmp_path),
        authorization=_signin_authorization(),
        snapshot=before,
        timeout=1.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.completed is True
    # The sweep and verification both took frames from the capture seam.
    assert capture.calls == 2


def test_claim_streak_records_the_redeem_interstitial_as_a_clean_completion(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = _streak_frame(frames['coins'])
    redeem = _redeem(before)
    state = _state(tmp_path)

    result = claim_streak(
        _controller(_RecordingDevice(), before),
        _Capture([before, redeem, redeem]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=1.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.COMPLETED
    assert result.verification == 'interstitial'
    assert result.interstitial == REDEEM_INTERSTITIAL
    assert result.dismiss is None
    assert state.done_today(STREAK_KEY)


def test_claim_streak_records_redeem_that_arrives_during_verification(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = _streak_frame(frames['coins'])
    clear = _streak_frame(frames['coins'], collect=False, tomorrow=True)
    redeem = _redeem(clear)
    state = _state(tmp_path)

    result = claim_streak(
        _controller(_RecordingDevice(), before),
        _Capture([before, clear, redeem]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=2.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.COMPLETED
    assert result.verification == 'interstitial'
    assert result.interstitial == REDEEM_INTERSTITIAL
    assert result.recorded is True
    assert state.done_today(STREAK_KEY)


def test_claim_streak_re_raises_an_unidentified_unclosable_popup(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = _streak_frame(frames['coins'])
    popup = _unmatched_popup(before)
    state = _state(tmp_path)

    with pytest.raises(PopupUnclosable):
        claim_streak(
            _controller(_RecordingDevice(), before),
            _Capture([before, popup, popup]),
            corpus,
            state,
            authorization=_signin_authorization(),
            timeout=1.0,
            interval=1.0,
            clock=_SteppingClock(),
            sleep=_SteppingClock().sleep,
        )

    assert state.done_today(STREAK_KEY) is False


@pytest.mark.parametrize(('timeout', 'interval'), [(0.0, 1.0), (1.0, 0.0)])
def test_claim_streak_rejects_non_positive_wait_parameters(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
    timeout: float,
    interval: float,
) -> None:
    with pytest.raises(ValueError, match='must be positive'):
        claim_streak(
            _controller(_RecordingDevice(), frames['coins']),
            _Capture([frames['coins']]),
            corpus,
            _state(tmp_path),
            authorization=_signin_authorization(),
            timeout=timeout,
            interval=interval,
        )


def test_claim_boost_checkin_completed_when_the_row_disappears(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = frames['boost']
    after = _boost_without_row(before)
    device = _RecordingDevice()
    state = _state(tmp_path)

    result = claim_boost_checkin(
        _controller(device, before),
        _Capture([before, after]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=1.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.COMPLETED
    assert result.verification == 'absent_boost_row'
    assert result.control is not None
    assert result.control.text == 'GO'
    assert len(device.taps) == 1
    assert state.done_today(BOOST_KEY)


def test_claim_boost_checkin_keeps_the_recorded_frame_from_poisoning_the_balance(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    """The recorded boost frame carries no readable balance; recording keeps the prior one.

    The real 2026-11-23 post-claim frame's top quarter was a non-coin surface.
    Its balance is redacted here, so ``read_coin_balance`` returns ``None``: the
    claim must leave ``last_balance`` alone rather than write a number from a
    surface the coin-page rule does not describe.
    """
    before = frames['real_boost']
    after = _boost_without_row(before)
    state = _state(tmp_path)
    state.observe_balance(54.0)

    assert read_coin_balance(before) is None
    assert read_coin_balance(after) is None

    result = claim_boost_checkin(
        _controller(_RecordingDevice(), before),
        _Capture([before, after, after, after]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=2.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.completed is True
    assert result.verification == 'absent_boost_row'
    assert result.balance_before is None
    assert result.balance_after is None
    assert state.snapshot().last_balance == 54.0


def test_claim_boost_checkin_never_stores_zero_for_an_unreadable_balance(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A claim that could not read either balance leaves ``last_balance`` null."""
    before = frames['real_boost']
    after = _boost_without_row(before)
    state = _state(tmp_path)
    caplog.set_level(logging.WARNING)

    assert read_coin_balance(before) is None
    assert read_coin_balance(after) is None

    result = claim_boost_checkin(
        _controller(_RecordingDevice(), before),
        _Capture([before, after, after, after]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=2.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.completed is True
    assert result.balance_before is None
    assert result.balance_after is None
    assert state.snapshot().last_balance is None
    assert 'balance_unreadable' in caplog.text


def test_claim_boost_checkin_clears_a_stale_unverified_zero_on_an_unreadable_balance(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A read attempt clears a legacy zero even when the claim records no new one."""
    before = frames['real_boost']
    after = _boost_without_row(before)
    state = _state(tmp_path)
    state.observe_balance(0.0, verified=False)
    caplog.set_level(logging.WARNING)

    result = claim_boost_checkin(
        _controller(_RecordingDevice(), before),
        _Capture([before, after, after, after]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=2.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.completed is True
    assert state.snapshot().last_balance is None
    assert state.snapshot().last_balance_verified is None
    assert 'balance_zero_unverified_cleared' in caplog.text


def test_claim_boost_checkin_ignores_a_zero_from_a_boost_page_baseline(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A lone control zero on the boost page is not a genuine balance baseline."""
    before = _with_box(frames['real_boost'], _box('0', 200, 120, 300, 180))
    after = _boost_without_row(frames['real_boost'])
    state = _state(tmp_path)
    caplog.set_level(logging.WARNING)

    result = claim_boost_checkin(
        _controller(_RecordingDevice(), before),
        _Capture([before, after, after, after]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=2.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.completed is True
    assert result.balance_before is None
    assert state.snapshot().last_balance is None
    assert 'balance_zero_unverified' in caplog.text
    assert 'without a validated coin-page surface' in caplog.text


def test_claim_boost_checkin_does_not_store_a_bare_zero_after_an_unreadable_baseline(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The #94 pair: unreadable before, a bare ``0`` after, still no stored zero."""
    before = frames['real_boost']
    after = _with_box(_boost_without_row(before), _box('0', 200, 120, 300, 180))
    state = _state(tmp_path)
    caplog.set_level(logging.WARNING)

    result = claim_boost_checkin(
        _controller(_RecordingDevice(), before),
        _Capture([before, after, after, after]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=2.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.completed is True
    assert result.balance_before is None
    assert result.balance_after is None
    assert state.snapshot().last_balance is None
    assert 'balance_zero_unverified' in caplog.text


def test_claim_boost_checkin_records_a_readable_balance_when_none_was_stored(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = _with_box(frames['boost'], _box('55', 200, 120, 300, 180))
    after = _with_box(_boost_without_row(frames['boost']), _box('56', 200, 120, 300, 180))
    state = _state(tmp_path)
    assert state.snapshot().last_balance is None

    result = claim_boost_checkin(
        _controller(_RecordingDevice(), before),
        _Capture([before, after, after, after]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=2.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.balance_before == 55.0
    assert result.balance_after == 56.0
    assert state.snapshot().last_balance == 56.0


def test_claim_streak_keeps_a_genuine_zero_balance(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    """A brand-new account's readable zero is a value, not an unreadable frame."""
    before = _streak_frame(frames['coins'], balance='0')
    after = _streak_frame(frames['coins'], balance='0', collect=False, tomorrow=True)
    state = _state(tmp_path)

    result = claim_streak(
        _controller(_RecordingDevice(), before),
        _Capture([before, after, after, after]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=2.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.completed is True
    assert result.verification == 'absent_control'
    assert result.balance_before == 0.0
    assert result.balance_after == 0.0
    assert state.snapshot().last_balance == 0.0
    assert state.snapshot().last_balance_verified is True


def test_claim_streak_replaces_an_unverified_zero_with_a_readable_balance(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = _streak_frame(frames['coins'], balance='55')
    after = _streak_frame(frames['coins'], balance='56', collect=False, tomorrow=True)
    state = _state(tmp_path)
    state.observe_balance(0.0, verified=False)

    result = claim_streak(
        _controller(_RecordingDevice(), before),
        _Capture([before, after, after, after]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=2.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.completed is True
    assert result.balance_before == 55.0
    assert result.balance_after == 56.0
    assert state.snapshot().last_balance == 56.0
    assert state.snapshot().last_balance_verified is True


def test_claim_balance_never_records_a_post_claim_drop() -> None:
    assert _claim_balance(54.0, 0.0) == 54.0
    assert _claim_balance(54.0, 53.0) == 54.0
    assert _claim_balance(54.0, 55.0) == 55.0
    assert _claim_balance(54.0, None) == 54.0
    assert _claim_balance(0.0, None) == 0.0
    assert _claim_balance(None, 0.0) is None
    assert _claim_balance(0.0, 0.0) == 0.0


def test_claim_streak_clamps_a_post_claim_balance_drop(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    before = _streak_frame(frames['coins'], balance='40')
    after = _streak_frame(frames['coins'], balance='0', collect=False, tomorrow=True)
    state = _state(tmp_path)
    caplog.set_level(logging.WARNING)

    result = claim_streak(
        _controller(_RecordingDevice(), before),
        _Capture([before, after, after, after]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=2.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.completed is True
    assert result.verification == 'absent_control'
    assert result.balance_before == 40.0
    assert result.balance_after == 40.0
    assert state.snapshot().last_balance == 40.0
    assert 'balance_zero_unverified' in caplog.text
    assert 'validated coin page but the starting balance does not corroborate' in caplog.text


def test_claim_boost_checkin_completed_by_a_claimed_row_marker(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = frames['boost']
    after = _boost_without_action(before)

    result = claim_boost_checkin(
        _controller(_RecordingDevice(), before),
        _Capture([before, after]),
        corpus,
        _state(tmp_path),
        authorization=_signin_authorization(),
        timeout=1.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.COMPLETED
    assert result.verification == 'claimed_marker'


def test_claim_boost_checkin_accepts_a_balance_delta(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = _with_box(frames['boost'], _box('40', 200, 120, 300, 180))
    after = _with_box(frames['boost'], _box('42', 200, 120, 300, 180))

    result = claim_boost_checkin(
        _controller(_RecordingDevice(), before),
        _Capture([before, after]),
        corpus,
        _state(tmp_path),
        authorization=_signin_authorization(),
        timeout=1.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.COMPLETED
    assert result.verification == 'balance_delta'
    assert result.balance_before == 40.0
    assert result.balance_after == 42.0


def test_claim_boost_checkin_absent_row_is_unavailable(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    device = _RecordingDevice()
    state = _state(tmp_path)

    result = claim_boost_checkin(
        _controller(device, frames['state_boost']),
        _Capture([frames['state_boost']]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=1.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.UNAVAILABLE
    assert result.verification == 'not_found'
    assert device.taps == []
    assert state.done_today(BOOST_KEY) is False


def test_claim_boost_checkin_unverified_is_not_recorded(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = frames['boost']
    state = _state(tmp_path)

    result = claim_boost_checkin(
        _controller(_RecordingDevice(), before),
        _Capture([before, before]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=2.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.UNVERIFIED
    assert result.unverified is True
    assert result.recorded is False
    assert state.done_today(BOOST_KEY) is False


def test_claim_boost_checkin_unverified_on_a_non_boost_page_is_not_recorded(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = frames['boost']
    elsewhere = _snapshot()
    state = _state(tmp_path)

    result = claim_boost_checkin(
        _controller(_RecordingDevice(), before),
        _Capture([before, elsewhere]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=2.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.UNVERIFIED
    assert result.recorded is False
    assert state.done_today(BOOST_KEY) is False


def test_claim_boost_checkin_does_not_accept_another_task_page_as_proof(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = frames['boost']
    task_page = _snapshot(
        _box('Take the coins quiz', 250, 600, 650, 650),
        _box('GO', 965, 650, 1050, 700),
    )
    state = _state(tmp_path)

    result = claim_boost_checkin(
        _controller(_RecordingDevice(), before),
        _Capture([before, task_page]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=2.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.UNVERIFIED
    assert result.recorded is False
    assert state.done_today(BOOST_KEY) is False


def test_claim_boost_checkin_completes_when_the_inline_row_leaves_the_coin_page(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = frames['coins']
    after = _streak_frame(frames['coins'], collect=False, tomorrow=True)

    result = claim_boost_checkin(
        _controller(_RecordingDevice(), before),
        _Capture([before, after]),
        corpus,
        _state(tmp_path),
        authorization=_signin_authorization(),
        timeout=2.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.COMPLETED
    assert result.verification == 'absent_boost_row'


def test_claim_boost_checkin_is_unavailable_when_the_entry_never_settles(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    device = _RecordingDevice()
    state = _state(tmp_path)

    result = claim_boost_checkin(
        _controller(device, frames['skeleton']),
        _Capture([frames['skeleton']]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=1.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.UNAVAILABLE
    assert result.verification == 'not_settled'
    assert device.taps == []
    assert state.done_today(BOOST_KEY) is False


def test_claim_boost_checkin_records_redeem_that_arrives_during_verification(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = frames['boost']
    clear = _boost_without_row(before)
    redeem = _redeem(clear)
    state = _state(tmp_path)

    result = claim_boost_checkin(
        _controller(_RecordingDevice(), before),
        _Capture([before, clear, redeem]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=2.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.COMPLETED
    assert result.verification == 'interstitial'
    assert result.interstitial == REDEEM_INTERSTITIAL
    assert result.recorded is True
    assert state.done_today(BOOST_KEY)


def test_claim_boost_checkin_skips_from_state_before_any_capture(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    state.record(BOOST_KEY)
    capture = _ForbiddenCapture()

    result = claim_boost_checkin(
        _controller(_RecordingDevice(), frames['boost']),
        capture,
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=1.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.ALREADY_DONE
    assert capture.calls == 0


def test_claim_boost_checkin_records_redeem_as_a_clean_completion(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = frames['boost']
    redeem = _redeem(before)
    state = _state(tmp_path)

    result = claim_boost_checkin(
        _controller(_RecordingDevice(), before),
        _Capture([before, redeem, redeem]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=1.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is SigninOutcome.COMPLETED
    assert result.verification == 'interstitial'
    assert result.interstitial == REDEEM_INTERSTITIAL
    assert state.done_today(BOOST_KEY)


def test_claim_boost_checkin_works_when_the_corpus_has_no_boosheet_family(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = frames['boost']
    after = _boost_without_row(before)
    trimmed = replace(
        corpus,
        families=tuple(family for family in corpus.families if family.family != 'coins_boost_sheet'),
    )

    result = claim_boost_checkin(
        _controller(_RecordingDevice(), before),
        _Capture([before, after]),
        trimmed,
        _state(tmp_path),
        authorization=_signin_authorization(),
        timeout=1.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.completed is True


@pytest.mark.parametrize(('timeout', 'interval'), [(0.0, 1.0), (1.0, 0.0)])
def test_claim_boost_checkin_rejects_non_positive_wait_parameters(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
    timeout: float,
    interval: float,
) -> None:
    with pytest.raises(ValueError, match='must be positive'):
        claim_boost_checkin(
            _controller(_RecordingDevice(), frames['boost']),
            _Capture([frames['boost']]),
            corpus,
            _state(tmp_path),
            authorization=_signin_authorization(),
            timeout=timeout,
            interval=interval,
        )


@pytest.mark.slow
def test_claim_daily_signins_fresh_day_claims_both_and_reports_the_delta(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = _streak_frame(frames['coins'], balance='40')
    after = _streak_frame(frames['coins'], balance='42', collect=False, tomorrow=True)
    boost = frames['boost']
    boost_after = _boost_without_row(boost)
    capture = _Capture([before, after, after, after, boost, boost, boost_after, boost_after])
    device = _RecordingDevice()
    state = _state(tmp_path)

    report = claim_daily_signins(
        _controller(device, before),
        capture,
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=10.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert report.streak.outcome is SigninOutcome.COMPLETED
    assert report.boost is not None
    assert report.boost.outcome is SigninOutcome.COMPLETED
    assert report.route is DiscoveryRoute.BOOST
    assert report.balance_before == 40.0
    assert report.balance_after == 42.0
    assert report.completed == (report.streak, report.boost)
    assert report.already_done == ()
    assert report.unverified == ()
    assert report.stopped_by_redeem is False
    assert len(device.taps) == 3
    assert state.done_today(STREAK_KEY)
    assert state.done_today(BOOST_KEY)
    assert report.as_dict()['route'] == 'boost'


@pytest.mark.slow
def test_a_second_run_same_day_issues_zero_taps_and_skips_both(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = _streak_frame(frames['coins'], balance='40')
    after = _streak_frame(frames['coins'], balance='42', collect=False, tomorrow=True)
    boost = frames['boost']
    boost_after = _boost_without_row(boost)
    state = _state(tmp_path)
    first_taps = _RecordingDevice()

    claim_daily_signins(
        _controller(first_taps, before),
        _Capture([before, after, after, after, boost, boost, boost_after, boost_after]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=10.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )
    assert len(first_taps.taps) == 3

    second_device = _RecordingDevice()
    forbidden = _ForbiddenCapture()
    report = claim_daily_signins(
        _controller(second_device, before),
        forbidden,
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=1.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert forbidden.calls == 0
    assert second_device.taps == []
    assert report.streak.outcome is SigninOutcome.ALREADY_DONE
    assert report.boost is not None
    assert report.boost.outcome is SigninOutcome.ALREADY_DONE
    assert report.already_done == (report.streak, report.boost)
    assert report.route is None


def test_claim_daily_signins_stops_cleanly_on_redeem_after_the_streak(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = _streak_frame(frames['coins'])
    redeem = _redeem(before)
    device = _RecordingDevice()
    state = _state(tmp_path)

    report = claim_daily_signins(
        _controller(device, before),
        _Capture([before, redeem, redeem]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=1.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert report.streak.outcome is SigninOutcome.COMPLETED
    assert report.streak.interstitial == REDEEM_INTERSTITIAL
    assert report.boost is None
    assert report.results == (report.streak,)
    assert report.stop == REDEEM_INTERSTITIAL
    assert report.stopped_by_redeem is True
    assert len(device.taps) == 1
    assert state.done_today(STREAK_KEY)
    assert state.done_today(BOOST_KEY) is False
    assert report.as_dict()['stop'] == REDEEM_INTERSTITIAL


def test_claim_daily_signins_stops_when_redeem_blocks_opening_the_boost_list(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = _streak_frame(frames['coins'])
    clear = _streak_frame(frames['coins'], collect=False, tomorrow=True)
    redeem = _redeem(clear)
    device = _RecordingDevice()
    state = _state(tmp_path)

    report = claim_daily_signins(
        _controller(device, before),
        _Capture([before, clear, clear, redeem]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=5.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert report.streak.completed is True
    assert report.boost is None
    assert report.stop == REDEEM_INTERSTITIAL
    assert report.stopped_by_redeem is True
    assert len(device.taps) == 1
    assert state.done_today(STREAK_KEY)
    assert state.done_today(BOOST_KEY) is False


def test_claim_daily_signins_re_raises_a_non_redeem_popup_while_opening_the_boost_list(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = _streak_frame(frames['coins'])
    clear = _streak_frame(frames['coins'], collect=False, tomorrow=True)
    popup = _unmatched_popup(clear)
    state = _state(tmp_path)

    with pytest.raises(PopupUnclosable):
        claim_daily_signins(
            _controller(_RecordingDevice(), before),
            _Capture([before, clear, clear, popup]),
            corpus,
            state,
            authorization=_signin_authorization(),
            timeout=5.0,
            interval=1.0,
            clock=_SteppingClock(),
            sleep=_SteppingClock().sleep,
        )

    assert state.done_today(STREAK_KEY)


def test_claim_daily_signins_skips_a_done_boost_without_opening_the_list(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = _streak_frame(frames['coins'])
    after = _streak_frame(frames['coins'], collect=False, tomorrow=True)
    state = _state(tmp_path)
    state.record(BOOST_KEY)
    device = _RecordingDevice()

    report = claim_daily_signins(
        _controller(device, before),
        _Capture([before, after]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=1.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert report.streak.completed is True
    assert report.boost is not None
    assert report.boost.already_done is True
    assert report.route is None
    assert len(device.taps) == 1


def test_claim_daily_signins_claims_only_the_open_boost_when_the_streak_is_done(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    coin_page = _streak_frame(frames['coins'], collect=False, tomorrow=True)
    boost = _with_box(frames['boost'], _box('40', 200, 120, 300, 180))
    boost_after = _with_box(_boost_without_row(frames['boost']), _box('41', 200, 120, 300, 180))
    state = _state(tmp_path)
    state.record(STREAK_KEY)
    device = _RecordingDevice()

    report = claim_daily_signins(
        _controller(device, coin_page),
        _Capture([coin_page, boost, boost, boost_after, boost_after]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=10.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert report.streak.already_done is True
    assert report.boost is not None
    assert report.boost.completed is True
    assert report.route is DiscoveryRoute.BOOST
    assert report.balance_before == 40.0
    assert report.balance_after == 41.0
    assert len(device.taps) == 2
    assert state.done_today(BOOST_KEY)


def test_claim_daily_signins_fails_named_on_the_empty_boost_page(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    """The measured cold-start empty page is a load fault, not an absent row."""
    coin_page = _streak_frame(frames['coins'], collect=False, tomorrow=True)
    state = _state(tmp_path)
    state.record(STREAK_KEY)
    device = _RecordingDevice()

    report = claim_daily_signins(
        _controller(device, coin_page),
        _Capture([coin_page, _empty_boost(frames['real_boost'])]),
        corpus,
        state,
        authorization=_signin_authorization(),
        timeout=2.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert report.streak.already_done is True
    assert report.boost is not None
    assert report.boost.unverified is True
    assert report.boost.verification == 'empty_boost'
    assert 'empty' in report.boost.detail
    assert report.boost.tapped is False
    assert report.route is DiscoveryRoute.BOOST
    assert state.done_today(BOOST_KEY) is False
    assert len(device.taps) == 1


@pytest.mark.slow
def test_claim_daily_signins_falls_back_to_the_before_balance(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    before = _streak_frame(frames['coins'], balance='40')
    after = _streak_frame(frames['coins'], balance=None, collect=False, tomorrow=True)
    boost = _boost_without_row(frames['boost'])

    report = claim_daily_signins(
        _controller(_RecordingDevice(), before),
        _Capture([before, after, after, after, boost, boost, boost, boost]),
        corpus,
        _state(tmp_path),
        authorization=_signin_authorization(),
        timeout=10.0,
        interval=1.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert report.balance_before == 40.0
    assert report.balance_after == 40.0


def test_claim_signins_alias_and_outcome_alias_are_the_same_objects() -> None:
    assert claim_signins is claim_daily_signins
    assert ClaimOutcome is SigninOutcome


def test_claim_daily_signins_opens_the_boost_list_with_its_own_budget(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Navigation and claim verification must not share one 8 s deadline (#84)."""
    observed: dict[str, object] = {}

    def _open(*_args: object, **kwargs: object) -> object:
        observed.update(kwargs)
        raise RuntimeError('navigation sentinel')

    monkeypatch.setattr('aliexpress_coins.tasks.signin.open_boost_list', _open)
    state = _state(tmp_path)
    state.record(STREAK_KEY)

    with pytest.raises(RuntimeError, match='navigation sentinel'):
        claim_daily_signins(
            _controller(_RecordingDevice(), frames['merged_card']),
            _Capture([frames['merged_card']]),
            corpus,
            state,
            authorization=_signin_authorization(),
            timeout=2.0,
            open_timeout=7.5,
            interval=1.0,
            clock=_SteppingClock(),
            sleep=_SteppingClock().sleep,
        )

    assert observed['timeout'] == 7.5


def test_claim_daily_signins_rejects_a_non_positive_open_timeout(
    frames: dict[str, Snapshot],
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match='open_timeout'):
        claim_daily_signins(
            _controller(_RecordingDevice(), frames['coins']),
            _Capture([frames['coins']]),
            corpus,
            _state(tmp_path),
            authorization=_signin_authorization(),
            open_timeout=0,
        )
