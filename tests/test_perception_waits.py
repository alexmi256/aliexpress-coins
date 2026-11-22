"""Observable wait primitives over recorded and synthetic frames (issue #19).

Every wait is driven by an injected clock and a scripted capture sequence, so
these tests never sleep through wall-clock time and never reach the device. The
skeleton-to-content transition replays the reviewed state fixtures; stability
and countdown timings use tiny synthetic frames whose differences are exact.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from PIL import Image

from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.matching import DEFAULT_MIN_SCORE, find_text
from aliexpress_coins.perception.ocr import RapidOcrEngine
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.state import CaptureSource
from aliexpress_coins.perception.waits import (
    DEFAULT_POLL_INTERVAL,
    CountdownEnding,
    CountdownReading,
    CountdownStatus,
    CountdownWait,
    StableWait,
    TextWait,
    WaitStatus,
    frame_signature,
    read_countdown,
    signature_difference,
    wait_for_countdown,
    wait_for_text,
    wait_for_text_gone,
    wait_until_stable,
)

CAPTURED_AT = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
DATA = Path(__file__).parent / 'data'
STATES = DATA / 'states'


class _Clock:
    """A manual clock: ``sleep`` advances it, reads do not."""

    def __init__(self, start: float = 0.0) -> None:
        self.value = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class _QueueCapture:
    """A frame sequence that keeps returning its last frame once exhausted."""

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


class _AlternatingCapture:
    """A screen that never stops changing, for the "keep rendering" case."""

    def __init__(self) -> None:
        self.calls = 0

    def capture(self) -> Snapshot:
        self.calls += 1
        return _frame(0 if self.calls % 2 else 255)


class _ForbiddenCapture:
    """A capture that proves an initial frame was used instead."""

    def __init__(self) -> None:
        self.calls = 0

    def capture(self) -> Snapshot:
        self.calls += 1
        raise AssertionError('the wait must not capture when an initial frame is supplied')


def _geometry(width: int, height: int) -> DeviceGeometry:
    return DeviceGeometry.from_frame(width, height, density=240, sdk=35)


def _box(text: str, left: float, top: float, right: float, bottom: float, score: float = 0.99) -> TextBox:
    return TextBox(
        text=text,
        box=((left, top), (right, top), (right, bottom), (left, bottom)),
        score=score,
    )


def _snapshot(
    *boxes: TextBox,
    size: tuple[int, int] = (1200, 2000),
    image: Image.Image | None = None,
) -> Snapshot:
    return Snapshot(
        image=image if image is not None else Image.new('RGB', size, 'white'),
        boxes=boxes,
        captured_at=CAPTURED_AT,
        geometry=_geometry(size[0], size[1]),
    )


def _frame(value: int, text: str | None = None) -> Snapshot:
    image = Image.new('L', (64, 64), value)
    boxes = () if text is None else (_box(text, 10, 10, 60, 30),)
    return _snapshot(*boxes, size=(64, 64), image=image)


def _countdown_frame(text: str | None) -> Snapshot:
    boxes = [] if text is None else [_box(text, 1100, 1000, 1160, 1040)]
    return _snapshot(*boxes)


def _load(path: Path, engine: RapidOcrEngine) -> Snapshot:
    with Image.open(path) as opened:
        image = opened.convert('RGB')
    return Snapshot(
        image=image,
        boxes=engine.read(image),
        captured_at=CAPTURED_AT,
        geometry=_geometry(image.width, image.height),
    )


@pytest.fixture(scope='module')
def frames() -> dict[str, Snapshot]:
    engine = RapidOcrEngine()
    return {
        'skeleton': _load(STATES / 'skeleton.png', engine),
        'coins': _load(STATES / 'coins.png', engine),
    }


def _has_daily_coins(frame: Snapshot) -> bool:
    return find_text(frame, 'Daily coins', min_score=DEFAULT_MIN_SCORE) is not None


def test_wait_for_text_waits_out_the_skeleton_transition(frames: dict[str, Snapshot]) -> None:
    clock = _Clock()
    capture = _QueueCapture([frames['skeleton'], frames['coins']])

    wait = wait_for_text(capture, _has_daily_coins, timeout=5.0, interval=0.25, clock=clock, sleep=clock.sleep)

    assert wait.status is WaitStatus.SATISFIED
    assert wait.satisfied is True
    assert wait.timed_out is False
    assert wait.found is True
    assert wait.gone is False
    assert wait.frame is frames['coins']
    assert wait.first_frame is frames['skeleton']
    assert wait.history == (False, True)
    assert wait.attempts == 2
    assert wait.elapsed == pytest.approx(0.25)
    assert wait.bound == 5.0
    assert wait.as_dict()['history'] == [False, True]


def test_wait_for_text_uses_the_initial_frame_without_capturing() -> None:
    frame = _frame(200, 'Daily coins')

    wait = wait_for_text(
        _ForbiddenCapture(),
        lambda candidate: candidate.has('Daily coins'),
        timeout=1.0,
        initial=frame,
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert wait.satisfied is True
    assert wait.attempts == 1
    assert wait.frame is frame


def test_wait_for_text_times_out_with_the_last_frame_as_evidence() -> None:
    clock = _Clock()
    first = _frame(0, 'Loading')
    last = _frame(255, 'Still loading')
    capture = _QueueCapture([first, last])

    wait = wait_for_text(
        capture,
        lambda candidate: candidate.has('Daily coins'),
        timeout=0.5,
        interval=0.25,
        clock=clock,
        sleep=clock.sleep,
    )

    assert wait.status is WaitStatus.TIMED_OUT
    assert wait.timed_out is True
    assert wait.satisfied is False
    assert wait.matched is False
    assert wait.frame is last
    assert wait.elapsed >= 0.5
    assert wait.history == (False, False, False)


def test_wait_for_text_gone_returns_when_the_label_leaves() -> None:
    clock = _Clock()
    capture = _QueueCapture([_frame(0, '12s'), _frame(0, '6s'), _frame(0)])

    wait = wait_for_text_gone(
        capture,
        lambda candidate: candidate.has('12s') or candidate.has('6s'),
        timeout=2.0,
        interval=0.5,
        clock=clock,
        sleep=clock.sleep,
    )

    assert wait.satisfied is True
    assert wait.matched is False
    assert wait.gone is True
    assert wait.history == (True, True, False)
    assert wait.attempts == 3


def test_wait_for_text_gone_times_out_while_the_label_remains() -> None:
    clock = _Clock()
    capture = _QueueCapture([_frame(0, 'BOOST'), _frame(0, 'BOOST')])

    wait = wait_for_text_gone(
        capture,
        lambda candidate: candidate.has('BOOST'),
        timeout=0.5,
        interval=0.25,
        clock=clock,
        sleep=clock.sleep,
    )

    assert wait.timed_out is True
    assert wait.matched is True
    assert wait.found is True
    assert wait.history == (True, True, True)


@pytest.mark.parametrize(('timeout', 'interval'), [(0.0, 1.0), (-1.0, 1.0), (1.0, 0.0)])
def test_wait_for_text_rejects_invalid_parameters(timeout: float, interval: float) -> None:
    with pytest.raises(ValueError, match=r'must be positive'):
        wait_for_text(_QueueCapture([_frame(0)]), lambda _frame: True, timeout=timeout, interval=interval)


def test_waits_accept_the_object_capture_seam() -> None:
    class _Capture:
        def __init__(self) -> None:
            self.calls = 0

        def capture(self) -> Snapshot:
            self.calls += 1
            return _frame(0, 'Daily coins')

    capture = _Capture()

    wait = wait_for_text(
        cast(CaptureSource, capture),
        lambda candidate: candidate.has('Daily coins'),
        timeout=1.0,
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert wait.satisfied is True
    assert capture.calls == 1


def test_frame_signature_is_a_small_greyscale_grid() -> None:
    signature = frame_signature(_snapshot(image=Image.new('RGB', (1200, 2000), 'white')))

    assert signature.shape == (32, 32)
    assert float(signature.mean()) == pytest.approx(255.0)
    assert signature_difference(signature, signature) == 0.0
    assert signature_difference(signature, frame_signature(_frame(0))) == 1.0


def test_frame_signature_rejects_a_zero_size() -> None:
    with pytest.raises(ValueError, match='must be positive'):
        frame_signature(_frame(0), size=0)


def test_signature_difference_treats_another_grid_as_completely_different() -> None:
    first = frame_signature(_frame(0))
    second = frame_signature(_frame(0), size=16)

    assert signature_difference(first, second) == 1.0


def test_wait_until_stable_returns_after_the_hold_under_a_fake_clock() -> None:
    clock = _Clock()
    capture = _QueueCapture([_frame(0), _frame(40), _frame(80), _frame(80), _frame(80), _frame(80)])

    wait = wait_until_stable(
        capture,
        timeout=1.0,
        interval=0.125,
        hold=0.25,
        clock=clock,
        sleep=clock.sleep,
    )

    assert wait.status is WaitStatus.SATISFIED
    assert wait.stable is True
    assert wait.timed_out is False
    assert wait.elapsed == pytest.approx(0.625)
    assert wait.elapsed < wait.bound
    assert wait.attempts == 6
    assert wait.differences[0] > wait.threshold
    assert wait.difference == 0.0
    assert wait.as_dict()['difference'] == 0.0


def test_wait_until_stable_does_not_return_while_frames_keep_changing() -> None:
    clock = _Clock()

    wait = wait_until_stable(
        _AlternatingCapture(),
        timeout=0.5,
        interval=0.1,
        hold=0.2,
        clock=clock,
        sleep=clock.sleep,
    )

    assert wait.status is WaitStatus.TIMED_OUT
    assert wait.stable is False
    assert wait.timed_out is True
    assert all(difference > wait.threshold for difference in wait.differences)
    assert wait.elapsed >= 0.5


def test_wait_until_stable_uses_the_initial_frame_for_its_first_comparison() -> None:
    clock = _Clock()
    frame = _frame(0)

    wait = wait_until_stable(
        _QueueCapture([frame]),
        timeout=1.0,
        interval=0.1,
        hold=0.0,
        initial=frame,
        clock=clock,
        sleep=clock.sleep,
    )

    assert wait.satisfied is True
    assert wait.first_frame is frame
    assert wait.differences == (0.0,)


@pytest.mark.parametrize(
    ('keyword', 'value'),
    [
        ('timeout', 0.0),
        ('interval', 0.0),
        ('hold', -0.1),
        ('threshold', -0.1),
        ('threshold', 1.1),
    ],
)
def test_wait_until_stable_rejects_invalid_parameters(keyword: str, value: float) -> None:
    with pytest.raises(ValueError, match=r'must be positive|must not be negative|must be between'):
        _invalid_stable(keyword, value)


def _invalid_stable(keyword: str, value: float) -> StableWait:
    capture = _QueueCapture([_frame(0)])
    if keyword == 'timeout':
        return wait_until_stable(capture, timeout=value)
    if keyword == 'interval':
        return wait_until_stable(capture, interval=value)
    if keyword == 'hold':
        return wait_until_stable(capture, hold=value)
    return wait_until_stable(capture, threshold=value)


def test_stable_wait_rejects_an_invalid_hold_or_threshold() -> None:
    with pytest.raises(ValueError, match='must not be negative'):
        StableWait(
            status=WaitStatus.SATISFIED,
            frame=_frame(0),
            first_frame=_frame(0),
            elapsed=0.0,
            attempts=1,
            bound=1.0,
            hold=-1.0,
            threshold=0.01,
        )
    with pytest.raises(ValueError, match='must be between'):
        StableWait(
            status=WaitStatus.SATISFIED,
            frame=_frame(0),
            first_frame=_frame(0),
            elapsed=0.0,
            attempts=1,
            bound=1.0,
            hold=0.0,
            threshold=2.0,
        )


def test_read_countdown_prefers_a_running_rightmost_pill() -> None:
    frame = _snapshot(
        _box('9s', 800, 1000, 860, 1040),
        _box('12s', 1100, 1000, 1160, 1040),
        _box('check', 1050, 1000, 1090, 1040),
        _box('3s', 300, 1000, 360, 1040),
    )

    reading = read_countdown(frame)

    assert reading is not None
    assert reading.running is True
    assert reading.seconds == 12
    assert reading.text == '12s'
    assert reading.box is not None
    assert reading.as_dict()['seconds'] == 12


def test_read_countdown_reads_the_cleared_check_and_ignores_low_confidence() -> None:
    cleared = read_countdown(_snapshot(_box('\u2713', 1100, 1000, 1160, 1040)))
    assert cleared is not None
    assert cleared.clear is True
    assert cleared.running is False
    assert cleared.seconds is None

    assert read_countdown(_snapshot(_box('12s', 1100, 1000, 1160, 1040, score=0.2))) is None
    assert read_countdown(_snapshot(_box('Wireless Earbuds', 60, 400, 500, 450))) is None


def test_wait_for_countdown_is_ended_by_the_chip_disappearing() -> None:
    clock = _Clock()
    capture = _QueueCapture([_countdown_frame('12s'), _countdown_frame('6s'), _countdown_frame(None)])

    wait = wait_for_countdown(capture, timeout=15.0, interval=0.5, clock=clock, sleep=clock.sleep)

    assert wait.status is CountdownStatus.CLEARED
    assert wait.satisfied is True
    assert wait.seen is True
    assert wait.verified is False
    assert wait.ended_by is CountdownEnding.FRAME
    assert wait.seconds == 6
    assert wait.history == (12, 6, None)
    assert wait.elapsed < wait.bound
    assert wait.as_dict()['ended_by'] == 'frame'


def test_wait_for_countdown_accepts_an_already_cleared_chip() -> None:
    clock = _Clock()

    wait = wait_for_countdown(
        _QueueCapture([_countdown_frame('\u2713')]),
        timeout=15.0,
        clock=clock,
        sleep=clock.sleep,
    )

    assert wait.status is CountdownStatus.CLEARED
    assert wait.seen is False
    assert wait.ended_by is CountdownEnding.FRAME
    assert wait.attempts == 1


def test_wait_for_countdown_verifies_an_already_cleared_chip() -> None:
    wait = wait_for_countdown(
        _QueueCapture([_countdown_frame('\u2713')]),
        timeout=5.0,
        verify=lambda _frame: True,
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert wait.status is CountdownStatus.VERIFIED
    assert wait.verified is True
    assert wait.ended_by is CountdownEnding.FRAME


def test_wait_for_countdown_cleared_chip_without_verification_times_out() -> None:
    wait = wait_for_countdown(
        _QueueCapture([_countdown_frame('\u2713')]),
        timeout=1.0,
        interval=0.5,
        verify=lambda _frame: False,
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    assert wait.status is CountdownStatus.TIMED_OUT
    assert 'cleared but verification did not pass' in wait.detail


def test_wait_for_countdown_absent_is_a_finding_not_a_long_sleep() -> None:
    clock = _Clock()
    capture = _QueueCapture([_countdown_frame(None)])

    wait = wait_for_countdown(capture, timeout=15.0, clock=clock, sleep=clock.sleep)

    assert wait.status is CountdownStatus.ABSENT
    assert wait.satisfied is False
    assert wait.seen is False
    assert wait.attempts == 1
    assert capture.calls == 1
    assert clock.value == 0.0


def test_wait_for_countdown_times_out_while_the_digits_remain() -> None:
    clock = _Clock()

    wait = wait_for_countdown(
        _QueueCapture([_countdown_frame('9s')]),
        timeout=2.0,
        interval=1.0,
        clock=clock,
        sleep=clock.sleep,
    )

    assert wait.status is CountdownStatus.TIMED_OUT
    assert wait.timed_out is True
    assert wait.seen is True
    assert wait.elapsed >= 2.0
    assert wait.history == (9, 9, 9)


def test_wait_for_countdown_arithmetic_fallback_requires_verification() -> None:
    with pytest.raises(ValueError, match='elapsed time is not evidence'):
        wait_for_countdown(_QueueCapture([_countdown_frame(None)]), expected_seconds=15.0)


def test_wait_for_countdown_arithmetic_fallback_returns_on_verified_completion() -> None:
    clock = _Clock()
    unverified = _countdown_frame(None)
    verified = _countdown_frame('Mission Completed')
    capture = _QueueCapture([unverified, verified])

    wait = wait_for_countdown(
        capture,
        timeout=20.0,
        interval=0.5,
        expected_seconds=15.0,
        verify=lambda frame: frame.has('Mission Completed'),
        clock=clock,
        sleep=clock.sleep,
    )

    assert wait.status is CountdownStatus.VERIFIED
    assert wait.verified is True
    assert wait.ended_by is CountdownEnding.ARITHMETIC
    assert wait.expected_seconds == 15.0
    assert wait.frame is verified
    assert wait.elapsed == pytest.approx(15.0)


def test_wait_for_countdown_frame_evidence_waits_for_verification() -> None:
    clock = _Clock()
    cleared = _countdown_frame(None)
    completed = _countdown_frame('Row completed')
    capture = _QueueCapture([_countdown_frame('3s'), cleared, completed])

    wait = wait_for_countdown(
        capture,
        timeout=5.0,
        interval=0.5,
        verify=lambda frame: frame.has('Row completed'),
        clock=clock,
        sleep=clock.sleep,
    )

    assert wait.status is CountdownStatus.VERIFIED
    assert wait.verified is True
    assert wait.ended_by is CountdownEnding.FRAME
    assert wait.seen is True
    assert wait.frame is completed
    assert wait.attempts == 3


def test_wait_for_countdown_verification_failure_times_out() -> None:
    clock = _Clock()
    capture = _QueueCapture([_countdown_frame('3s'), _countdown_frame(None)])

    wait = wait_for_countdown(
        capture,
        timeout=1.5,
        interval=0.5,
        verify=lambda _frame: False,
        clock=clock,
        sleep=clock.sleep,
    )

    assert wait.status is CountdownStatus.TIMED_OUT
    assert wait.verified is False
    assert wait.ended_by is CountdownEnding.FRAME
    assert 'verification did not pass' in wait.detail
    assert wait.elapsed >= 1.5


def test_wait_for_countdown_arithmetic_verification_failure_times_out() -> None:
    clock = _Clock()

    wait = wait_for_countdown(
        _QueueCapture([_countdown_frame(None)]),
        timeout=2.0,
        interval=0.5,
        expected_seconds=1.0,
        verify=lambda _frame: False,
        clock=clock,
        sleep=clock.sleep,
    )

    assert wait.status is CountdownStatus.TIMED_OUT
    assert 'verification did not pass' in wait.detail
    assert wait.elapsed >= 2.0


def test_wait_for_countdown_arithmetic_verifies_a_late_render() -> None:
    clock = _Clock()
    completed = _countdown_frame('Row completed')
    capture = _QueueCapture([_countdown_frame(None), _countdown_frame(None), completed])

    wait = wait_for_countdown(
        capture,
        timeout=3.0,
        interval=0.5,
        expected_seconds=1.0,
        verify=lambda frame: frame.has('Row completed'),
        clock=clock,
        sleep=clock.sleep,
    )

    assert wait.status is CountdownStatus.VERIFIED
    assert wait.ended_by is CountdownEnding.ARITHMETIC
    assert wait.frame is completed
    assert wait.attempts == 2


@pytest.mark.parametrize(
    ('keyword', 'value'),
    [('timeout', 0.0), ('interval', 0.0), ('expected_seconds', 0.0)],
)
def test_wait_for_countdown_rejects_invalid_parameters(keyword: str, value: float) -> None:
    with pytest.raises(ValueError, match='must be positive'):
        _invalid_countdown(keyword, value)


def _invalid_countdown(keyword: str, value: float) -> CountdownWait:
    capture = _QueueCapture([_countdown_frame(None)])
    if keyword == 'timeout':
        return wait_for_countdown(capture, timeout=value)
    if keyword == 'interval':
        return wait_for_countdown(capture, interval=value)
    return wait_for_countdown(capture, expected_seconds=value, verify=lambda _frame: True)


def test_countdown_observation_and_reading_render_for_the_report() -> None:
    wait = wait_for_countdown(
        _QueueCapture([_countdown_frame('4s'), _countdown_frame(None)]),
        timeout=5.0,
        interval=0.5,
        clock=_Clock(),
        sleep=_Clock().sleep,
    )

    rendered = wait.as_dict()
    observations = cast(list[dict[str, object]], rendered['observations'])
    reading = cast(dict[str, object], observations[0]['reading'])

    assert rendered['history'] == [4, None]
    assert reading['text'] == '4s'
    assert reading['seconds'] == 4
    assert reading['checked'] is False

    bare = CountdownReading(text=None, seconds=None).as_dict()
    assert bare == {'text': None, 'seconds': None, 'checked': False}


@pytest.mark.parametrize('name', ['text', 'gone', 'stable', 'countdown'])
def test_no_wait_can_loop_unbounded_even_with_a_frozen_clock_and_sleep(name: str) -> None:
    clock = _Clock()
    timeout = 0.5
    interval = 0.1

    def frozen(_seconds: float) -> None:
        return None

    result: TextWait | StableWait | CountdownWait
    if name == 'text':
        result = wait_for_text(
            _QueueCapture([_frame(0)]),
            lambda _frame: False,
            timeout=timeout,
            interval=interval,
            clock=clock,
            sleep=frozen,
        )
    elif name == 'gone':
        result = wait_for_text_gone(
            _QueueCapture([_frame(0, 'label')]),
            lambda candidate: candidate.has('label'),
            timeout=timeout,
            interval=interval,
            clock=clock,
            sleep=frozen,
        )
    elif name == 'stable':
        result = wait_until_stable(
            _AlternatingCapture(),
            timeout=timeout,
            interval=interval,
            hold=0.1,
            clock=clock,
            sleep=frozen,
        )
    else:
        result = wait_for_countdown(
            _QueueCapture([_countdown_frame('9s')]),
            timeout=timeout,
            interval=interval,
            clock=clock,
            sleep=frozen,
        )

    assert result.timed_out is True
    assert 1 <= result.attempts <= math.ceil(timeout / interval) + 1
    assert result.elapsed <= timeout


def test_default_poll_interval_is_a_positive_duration() -> None:
    assert DEFAULT_POLL_INTERVAL > 0
