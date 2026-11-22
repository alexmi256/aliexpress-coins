"""Observable waits: text appearance, frame stability and countdowns (#19).

Blind sleeps are why UI automation is flaky. Every wait here polls something the
frame actually shows and stops on a *named* outcome: the deadline is not an
exception a caller may forget to catch, it is :attr:`WaitStatus.TIMED_OUT` on
the result. Time is injected (``clock`` + ``sleep``), so a test advances a fake
clock instead of spending wall-clock seconds.

Three primitives share one contract:

* :func:`wait_for_text` / :func:`wait_for_text_gone` — a caller-supplied matcher,
  so the wait is "this label is on screen" or "this label has left";
* :func:`wait_until_stable` — a 32x32 greyscale signature compared by mean
  absolute difference, returning once consecutive captures stay below a
  threshold for ``hold`` seconds. This is how the hybrid container distinguishes
  *still rendering* from *waiting forever*;
* :func:`wait_for_countdown` — the measured browse-task ``Ns`` chip, read from
  the frame. Arithmetic is only a fallback, and it may only be used together
  with a verification predicate: a wait that returns because time passed is a
  guess, not evidence (docs/03 §"Verification: absence is not evidence").

Every primitive also bounds its poll count from ``timeout / interval``. A
deadline alone protects against a clock that jumps forward; the poll bound also
protects against an injected clock that never moves, so no wait can loop
unbounded even when its clock seam is broken.
"""

from __future__ import annotations

import math
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from aliexpress_coins.perception.matching import DEFAULT_MIN_OCR_SCORE
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.state import CaptureSource

__all__ = [
    'DEFAULT_COUNTDOWN_TIMEOUT',
    'DEFAULT_POLL_INTERVAL',
    'DEFAULT_STABLE_HOLD',
    'DEFAULT_STABLE_TIMEOUT',
    'DEFAULT_TEXT_TIMEOUT',
    'STABLE_MAX_DIFFERENCE',
    'STABLE_SIGNATURE_SIZE',
    'CountdownEnding',
    'CountdownObservation',
    'CountdownReading',
    'CountdownStatus',
    'CountdownWait',
    'StableWait',
    'TextWait',
    'WaitStatus',
    'frame_signature',
    'read_countdown',
    'signature_difference',
    'wait_for_countdown',
    'wait_for_text',
    'wait_for_text_gone',
    'wait_until_stable',
]

DEFAULT_POLL_INTERVAL = 0.5
"""How often a wait captures a new frame by default."""

DEFAULT_TEXT_TIMEOUT = 10.0
"""How long a text wait may take before it reports ``timed_out``."""

DEFAULT_STABLE_TIMEOUT = 10.0
"""How long a stability wait may take before it reports ``timed_out``."""

DEFAULT_STABLE_HOLD = 0.5
"""How many seconds consecutive captures must stay similar to count as stable.

Half a second is longer than a re-render and shorter than a page transition; it
is a duration, not a frame count, so a fast capture loop cannot pass on one pair
of identical frames.
"""

DEFAULT_COUNTDOWN_TIMEOUT = 15.0
"""The measured browse-task dwell bound (docs/05-app-knowledge.md §5)."""

STABLE_SIGNATURE_SIZE = 32
"""Cells on each side of the downscaled stability signature.

This is a sampling resolution, not a device pixel size: the source frame is
resized to it, so the comparison costs the same at every device geometry.
"""

STABLE_MAX_DIFFERENCE = 0.01
"""Mean absolute luminance difference (0 to 1) that counts as unchanged.

The same tolerance the scroll search uses: it absorbs a blinking cursor or a
repainted label without calling a real page transition stable.
"""

_COUNTDOWN_TEXT = re.compile(r'^\s*(?P<seconds>\d{1,3})\s*s\s*$', re.IGNORECASE)
"""OCR reads the running chip as ``12s``; case and inner spaces are ignored."""


class WaitStatus(StrEnum):
    """How a text or stability wait ended."""

    SATISFIED = 'satisfied'
    TIMED_OUT = 'timed_out'


class CountdownStatus(StrEnum):
    """How a countdown wait ended, and how strong the evidence was."""

    CLEARED = 'cleared'
    """The chip was running and then disappeared: frame evidence, no verifier."""
    VERIFIED = 'verified'
    """The chip disappeared (or the arithmetic elapsed) and the verifier agreed."""
    ABSENT = 'absent'
    """No chip appeared at all; there is nothing to wait on."""
    TIMED_OUT = 'timed_out'
    """The deadline was reached before the evidence arrived."""


class CountdownEnding(StrEnum):
    """What produced the countdown verdict."""

    FRAME = 'frame'
    ARITHMETIC = 'arithmetic'


@dataclass(frozen=True, slots=True)
class TextWait:
    """One text-appearance wait and the evidence behind its verdict.

    ``matched`` is the matcher's answer on :attr:`frame`, the last frame seen.
    ``history`` is the answer for every poll, so a caller can prove the skeleton
    frame was seen before the content frame satisfied the wait.
    """

    status: WaitStatus
    matched: bool
    frame: Snapshot
    first_frame: Snapshot
    elapsed: float
    attempts: int
    bound: float
    history: tuple[bool, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, 'history', tuple(self.history))

    @property
    def satisfied(self) -> bool:
        """Whether the awaited condition was reached before the deadline."""
        return self.status is WaitStatus.SATISFIED

    @property
    def timed_out(self) -> bool:
        """Whether the deadline ended the wait."""
        return self.status is WaitStatus.TIMED_OUT

    @property
    def found(self) -> bool:
        """Whether the matcher was true on the final frame."""
        return self.matched

    @property
    def gone(self) -> bool:
        """Whether the matcher was false on the final frame."""
        return not self.matched

    def as_dict(self) -> dict[str, object]:
        """Render the wait for a run report."""
        return {
            'status': self.status.value,
            'matched': self.matched,
            'elapsed': self.elapsed,
            'attempts': self.attempts,
            'bound': self.bound,
            'history': list(self.history),
        }


@dataclass(frozen=True, slots=True)
class StableWait:
    """One stability wait, including every signature difference it measured."""

    status: WaitStatus
    frame: Snapshot
    first_frame: Snapshot
    elapsed: float
    attempts: int
    bound: float
    hold: float
    threshold: float
    differences: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, 'differences', tuple(self.differences))
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError(f'threshold must be between 0 and 1, got {self.threshold!r}')
        if self.hold < 0:
            raise ValueError(f'hold must not be negative, got {self.hold!r}')

    @property
    def satisfied(self) -> bool:
        """Whether consecutive frames stayed similar for the hold time."""
        return self.status is WaitStatus.SATISFIED

    @property
    def stable(self) -> bool:
        """Whether the screen stopped changing before the deadline."""
        return self.satisfied

    @property
    def timed_out(self) -> bool:
        """Whether the deadline ended the wait."""
        return self.status is WaitStatus.TIMED_OUT

    @property
    def difference(self) -> float | None:
        """The most recent signature difference, or ``None`` before one was measured."""
        return self.differences[-1] if self.differences else None

    def as_dict(self) -> dict[str, object]:
        """Render the wait for a run report."""
        return {
            'status': self.status.value,
            'elapsed': self.elapsed,
            'attempts': self.attempts,
            'bound': self.bound,
            'hold': self.hold,
            'threshold': self.threshold,
            'difference': self.difference,
            'differences': list(self.differences),
        }


@dataclass(frozen=True, slots=True)
class CountdownReading:
    """One read of a countdown surface: running digits or the cleared check."""

    text: str | None
    seconds: int | None
    checked: bool = False
    box: TextBox | None = None

    @property
    def running(self) -> bool:
        """Whether the reading is a live ``Ns`` countdown."""
        return self.seconds is not None

    @property
    def clear(self) -> bool:
        """Whether the reading is the positive "dwell met" check state."""
        return self.seconds is None and self.checked

    def as_dict(self) -> dict[str, object]:
        """Render the reading for a run report."""
        record: dict[str, object] = {'text': self.text, 'seconds': self.seconds, 'checked': self.checked}
        if self.box is not None:
            record['box'] = list(self.box.bbox)
            record['target'] = list(self.box.center)
        return record


@dataclass(frozen=True, slots=True)
class CountdownObservation:
    """One poll of the countdown surface."""

    elapsed: float
    reading: CountdownReading | None

    def as_dict(self) -> dict[str, object]:
        """Render the observation for a run report."""
        return {
            'elapsed': self.elapsed,
            'reading': None if self.reading is None else self.reading.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class CountdownWait:
    """The whole countdown poll, the evidence that ended it and when.

    ``ended_by`` names the *evidence*, not the elapsed duration: ``frame`` means
    a running chip was seen and then gone (or the check was already drawn), and
    ``arithmetic`` means the expected dwell elapsed under a verification
    predicate. A bare elapsed time is never a successful ending.
    """

    status: CountdownStatus
    frame: Snapshot
    first_frame: Snapshot
    elapsed: float
    attempts: int
    bound: float
    ended_by: CountdownEnding | None = None
    seconds: int | None = None
    verified: bool = False
    expected_seconds: float | None = None
    observations: tuple[CountdownObservation, ...] = ()
    detail: str = ''

    def __post_init__(self) -> None:
        object.__setattr__(self, 'observations', tuple(self.observations))

    @property
    def satisfied(self) -> bool:
        """Whether the countdown ended with frame or verified evidence."""
        return self.status in (CountdownStatus.CLEARED, CountdownStatus.VERIFIED)

    @property
    def timed_out(self) -> bool:
        """Whether the deadline ended the wait."""
        return self.status is CountdownStatus.TIMED_OUT

    @property
    def seen(self) -> bool:
        """Whether a running countdown was observed at least once."""
        return any(item.reading is not None and item.reading.running for item in self.observations)

    @property
    def history(self) -> tuple[int | None, ...]:
        """The seconds read on each poll, with ``None`` for "no digits"."""
        return tuple(None if item.reading is None else item.reading.seconds for item in self.observations)

    def as_dict(self) -> dict[str, object]:
        """Render the poll for a run report."""
        return {
            'status': self.status.value,
            'ended_by': None if self.ended_by is None else self.ended_by.value,
            'verified': self.verified,
            'seen': self.seen,
            'seconds': self.seconds,
            'expected_seconds': self.expected_seconds,
            'elapsed': self.elapsed,
            'attempts': self.attempts,
            'bound': self.bound,
            'detail': self.detail,
            'history': list(self.history),
            'observations': [item.as_dict() for item in self.observations],
        }


def frame_signature(snapshot: Snapshot, *, size: int = STABLE_SIGNATURE_SIZE) -> NDArray[np.float32]:
    """Return a downscaled greyscale signature of one frame.

    The frame's aspect ratio is deliberately flattened: the signature answers
    "did this frame change", not "where is the content". A 32x32 grid keeps a
    comparison in the microsecond range, far below the ~1.3 s OCR pass.
    """
    if size < 1:
        raise ValueError(f'signature size must be positive, got {size!r}')
    image = snapshot.image.convert('L').resize((size, size), resample=Image.Resampling.BOX)
    return np.asarray(image, dtype=np.float32)


def signature_difference(first: NDArray[np.float32], second: NDArray[np.float32]) -> float:
    """Mean absolute luminance difference between two signatures, scaled to 0 to 1.

    A shape change is treated as completely different: a signature from another
    resolution is not evidence that the screen did not move.
    """
    if first.shape != second.shape:
        return 1.0
    return float(np.abs(first - second).mean()) / 255.0


def read_countdown(snapshot: Snapshot) -> CountdownReading | None:
    """Read running ``Ns`` digits from any box in the frame.

    The check state is deliberately not inferred here. A lone check glyph can
    appear anywhere on an ordinary shopping page, and the matching layer maps a
    product size ``S`` to the same token as ``✓``; treating either as the chip
    would end a dwell before it started. The browse executor supplies its own
    measured right-edge reader, which reads the check inside the chip band.
    """
    running: tuple[TextBox, int] | None = None
    for box in snapshot.boxes:
        if box.score < DEFAULT_MIN_OCR_SCORE:
            continue
        text = unicodedata.normalize('NFKC', box.text)
        match = _COUNTDOWN_TEXT.match(text)
        if match is None:
            continue
        candidate = int(match.group('seconds'))
        if running is None or box.bbox[2] > running[0].bbox[2]:
            running = (box, candidate)

    if running is None:
        return None
    box, seconds = running
    return CountdownReading(text=box.text, seconds=seconds, box=box)


def wait_for_text[T](
    capture: CaptureSource,
    matcher: Callable[[Snapshot], T | None],
    *,
    timeout: float = DEFAULT_TEXT_TIMEOUT,
    interval: float = DEFAULT_POLL_INTERVAL,
    initial: Snapshot | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> TextWait:
    """Poll until ``matcher`` returns a truthy value, bounded by ``timeout``.

    ``matcher`` is the live capture seam's only contract: a label lookup, a
    surface predicate or any other question about one frame. The result keeps
    the frame that satisfied it (or the last frame on timeout), the first frame,
    the per-poll history and the elapsed time, so a report can quote evidence
    rather than a duration.
    """
    return _wait_for_text(
        capture,
        matcher,
        wanted=True,
        timeout=timeout,
        interval=interval,
        initial=initial,
        clock=clock,
        sleep=sleep,
    )


def wait_for_text_gone[T](
    capture: CaptureSource,
    matcher: Callable[[Snapshot], T | None],
    *,
    timeout: float = DEFAULT_TEXT_TIMEOUT,
    interval: float = DEFAULT_POLL_INTERVAL,
    initial: Snapshot | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> TextWait:
    """Poll until ``matcher`` returns falsy, bounded by ``timeout``.

    The same result shape as :func:`wait_for_text`; ``matched`` is the final
    frame's answer, so a caller can still report what was (or was not) there.
    """
    return _wait_for_text(
        capture,
        matcher,
        wanted=False,
        timeout=timeout,
        interval=interval,
        initial=initial,
        clock=clock,
        sleep=sleep,
    )


def wait_until_stable(
    capture: CaptureSource,
    *,
    timeout: float = DEFAULT_STABLE_TIMEOUT,
    interval: float = DEFAULT_POLL_INTERVAL,
    hold: float = DEFAULT_STABLE_HOLD,
    threshold: float = STABLE_MAX_DIFFERENCE,
    initial: Snapshot | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> StableWait:
    """Poll until consecutive captures stay below ``threshold`` for ``hold`` seconds.

    A changing pair resets the hold window; a timeout returns ``timed_out``
    instead of pretending the screen settled. ``differences`` keeps every
    measured pair, which is what a failure report needs to show "still
    rendering" rather than "nothing happened".
    """
    if timeout <= 0:
        raise ValueError(f'timeout must be positive, got {timeout!r}')
    if interval <= 0:
        raise ValueError(f'interval must be positive, got {interval!r}')
    if hold < 0:
        raise ValueError(f'hold must not be negative, got {hold!r}')
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f'threshold must be between 0 and 1, got {threshold!r}')

    started = clock()
    deadline = started + timeout
    budget = _poll_budget(timeout, interval)
    frame = initial if initial is not None else _capture(capture)
    first = frame
    previous = frame_signature(frame)
    stable_since: float | None = None
    differences: list[float] = []
    attempts = 1

    while True:
        now = clock()
        if stable_since is not None and now - stable_since >= hold:
            return _stable_result(
                WaitStatus.SATISFIED,
                frame,
                first,
                max(0.0, now - started),
                attempts,
                timeout,
                hold,
                threshold,
                differences,
            )
        if attempts >= budget or now >= deadline:
            return _stable_result(
                WaitStatus.TIMED_OUT,
                frame,
                first,
                max(0.0, now - started),
                attempts,
                timeout,
                hold,
                threshold,
                differences,
            )

        sleep(min(interval, max(0.0, deadline - now)))
        frame = _capture(capture)
        attempts += 1
        current = frame_signature(frame)
        difference = signature_difference(previous, current)
        differences.append(difference)
        previous = current
        if difference <= threshold:
            if stable_since is None:
                stable_since = clock()
        else:
            stable_since = None


def wait_for_countdown(
    capture: CaptureSource,
    *,
    timeout: float = DEFAULT_COUNTDOWN_TIMEOUT,
    interval: float = DEFAULT_POLL_INTERVAL,
    read: Callable[[Snapshot], CountdownReading | None] = read_countdown,
    verify: Callable[[Snapshot], bool] | None = None,
    expected_seconds: float | None = None,
    initial: Snapshot | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> CountdownWait:
    """Wait out a countdown by reading the frame, with arithmetic only as a fallback.

    The frame is the primary evidence: while ``read`` returns running digits the
    wait polls at ``interval``; when the digits disappear (or the check is
    already drawn) that is a positive "dwell met" reading. If no chip is present
    at all, ``expected_seconds`` may stand in for it, but only with ``verify``:
    the arithmetic branch sleeps to the expected finish and *then* asks the
    verifier whether the task really completed. Without a verifier the branch is
    refused at call time, because an elapsed duration is not evidence.

    ``ABSENT`` is immediate and is a finding, not a reason to sleep through the
    whole timeout: a page that never showed a chip cannot satisfy a chip wait.
    """
    if timeout <= 0:
        raise ValueError(f'timeout must be positive, got {timeout!r}')
    if interval <= 0:
        raise ValueError(f'interval must be positive, got {interval!r}')
    if expected_seconds is not None and expected_seconds <= 0:
        raise ValueError(f'expected_seconds must be positive, got {expected_seconds!r}')
    if expected_seconds is not None and verify is None:
        raise ValueError('expected_seconds needs verify: elapsed time is not evidence')

    started = clock()
    deadline = started + timeout
    budget = _poll_budget(timeout, interval)
    frame = initial if initial is not None else _capture(capture)
    first = frame
    observations: list[CountdownObservation] = []
    attempts = 0
    seen_positive = False
    arithmetic_done = False
    ending: CountdownEnding | None = None
    last_seconds: int | None = None
    detail = ''

    while True:
        now = clock()
        attempts += 1
        elapsed = max(0.0, now - started)
        reading = read(frame)
        observations.append(CountdownObservation(elapsed=elapsed, reading=reading))

        if reading is not None and reading.running:
            seen_positive = True
            last_seconds = reading.seconds
        elif reading is not None and reading.clear:
            # The check is the chip's own positive "dwell met" state. Keep
            # checking it after a failed verification: the completion row can
            # render a moment later without any chip on the frame.
            seen_positive = True
            ending = CountdownEnding.FRAME
            if verify is None:
                return _countdown_result(
                    CountdownStatus.CLEARED,
                    frame,
                    first,
                    max(0.0, clock() - started),
                    attempts,
                    timeout,
                    observations,
                    ended_by=ending,
                    expected_seconds=expected_seconds,
                    detail='the countdown chip already showed its cleared state',
                )
            if verify(frame):
                return _countdown_result(
                    CountdownStatus.VERIFIED,
                    frame,
                    first,
                    max(0.0, clock() - started),
                    attempts,
                    timeout,
                    observations,
                    ended_by=ending,
                    expected_seconds=expected_seconds,
                    verified=True,
                    detail='the countdown chip was cleared and verification passed',
                )
            detail = 'the countdown chip was cleared but verification did not pass yet'
        elif seen_positive:
            # A positive chip was seen and is now gone. This covers both the
            # running digits with no verifier and a cleared check whose
            # verification has not passed yet; the latter must keep polling so
            # the completion row gets its turn.
            ending = CountdownEnding.FRAME
            if verify is None:
                return _countdown_result(
                    CountdownStatus.CLEARED,
                    frame,
                    first,
                    max(0.0, clock() - started),
                    attempts,
                    timeout,
                    observations,
                    ended_by=ending,
                    seconds=last_seconds,
                    expected_seconds=expected_seconds,
                    detail='the countdown chip disappeared',
                )
            if verify(frame):
                return _countdown_result(
                    CountdownStatus.VERIFIED,
                    frame,
                    first,
                    max(0.0, clock() - started),
                    attempts,
                    timeout,
                    observations,
                    ended_by=ending,
                    seconds=last_seconds,
                    expected_seconds=expected_seconds,
                    verified=True,
                    detail='the countdown chip disappeared and verification passed',
                )
            detail = 'the countdown chip disappeared but verification did not pass yet'
        elif expected_seconds is None or verify is None:
            return _countdown_result(
                CountdownStatus.ABSENT,
                frame,
                first,
                max(0.0, clock() - started),
                attempts,
                timeout,
                observations,
                expected_seconds=expected_seconds,
                detail='no countdown chip was visible; there is nothing to wait on',
            )
        elif not arithmetic_done:
            # Arithmetic fallback: no chip, but the measured dwell is known.
            arithmetic_done = True
            target = started + expected_seconds
            sleep(min(max(0.0, target - now), max(0.0, deadline - now)))
            frame = _capture(capture)
            if verify is not None and verify(frame):
                return _countdown_result(
                    CountdownStatus.VERIFIED,
                    frame,
                    first,
                    max(0.0, clock() - started),
                    attempts,
                    timeout,
                    observations,
                    ended_by=CountdownEnding.ARITHMETIC,
                    expected_seconds=expected_seconds,
                    verified=True,
                    detail='the expected dwell elapsed and verification passed',
                )
            detail = 'the expected dwell elapsed but verification did not pass yet'
        elif verify is not None and verify(frame):
            # Still no chip after the arithmetic target; keep checking the
            # frame until the deadline so a late render can still verify.
            return _countdown_result(
                CountdownStatus.VERIFIED,
                frame,
                first,
                max(0.0, clock() - started),
                attempts,
                timeout,
                observations,
                ended_by=CountdownEnding.ARITHMETIC,
                expected_seconds=expected_seconds,
                verified=True,
                detail='the expected dwell elapsed and verification passed',
            )

        if attempts >= budget or now >= deadline:
            status = CountdownStatus.TIMED_OUT
            if detail == '':
                detail = 'the deadline was reached before the countdown cleared'
            return _countdown_result(
                status,
                frame,
                first,
                max(0.0, clock() - started),
                attempts,
                timeout,
                observations,
                ended_by=ending,
                seconds=last_seconds,
                expected_seconds=expected_seconds,
                detail=detail,
            )

        sleep(min(interval, max(0.0, deadline - now)))
        frame = _capture(capture)


def _wait_for_text[T](
    capture: CaptureSource,
    matcher: Callable[[Snapshot], T | None],
    *,
    wanted: bool,
    timeout: float,
    interval: float,
    initial: Snapshot | None,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> TextWait:
    if timeout <= 0:
        raise ValueError(f'timeout must be positive, got {timeout!r}')
    if interval <= 0:
        raise ValueError(f'interval must be positive, got {interval!r}')

    started = clock()
    deadline = started + timeout
    budget = _poll_budget(timeout, interval)
    frame = initial if initial is not None else _capture(capture)
    first = frame
    history: list[bool] = []
    attempts = 0

    while True:
        now = clock()
        attempts += 1
        matched = bool(matcher(frame))
        history.append(matched)
        if matched is wanted:
            return TextWait(
                status=WaitStatus.SATISFIED,
                matched=matched,
                frame=frame,
                first_frame=first,
                elapsed=max(0.0, now - started),
                attempts=attempts,
                bound=timeout,
                history=tuple(history),
            )
        if attempts >= budget or now >= deadline:
            return TextWait(
                status=WaitStatus.TIMED_OUT,
                matched=matched,
                frame=frame,
                first_frame=first,
                elapsed=max(0.0, now - started),
                attempts=attempts,
                bound=timeout,
                history=tuple(history),
            )
        sleep(min(interval, max(0.0, deadline - now)))
        frame = _capture(capture)


def _stable_result(
    status: WaitStatus,
    frame: Snapshot,
    first: Snapshot,
    elapsed: float,
    attempts: int,
    bound: float,
    hold: float,
    threshold: float,
    differences: list[float],
) -> StableWait:
    return StableWait(
        status=status,
        frame=frame,
        first_frame=first,
        elapsed=elapsed,
        attempts=attempts,
        bound=bound,
        hold=hold,
        threshold=threshold,
        differences=tuple(differences),
    )


def _countdown_result(
    status: CountdownStatus,
    frame: Snapshot,
    first: Snapshot,
    elapsed: float,
    attempts: int,
    bound: float,
    observations: list[CountdownObservation],
    *,
    ended_by: CountdownEnding | None = None,
    seconds: int | None = None,
    expected_seconds: float | None = None,
    verified: bool = False,
    detail: str = '',
) -> CountdownWait:
    return CountdownWait(
        status=status,
        frame=frame,
        first_frame=first,
        elapsed=elapsed,
        attempts=attempts,
        bound=bound,
        ended_by=ended_by,
        seconds=seconds,
        verified=verified,
        expected_seconds=expected_seconds,
        observations=tuple(observations),
        detail=detail,
    )


def _poll_budget(timeout: float, interval: float) -> int:
    """The hard cap on polls, independent of the injected clock.

    ``ceil(timeout / interval) + 1`` is enough for a well-behaved clock (the
    deadline ends first) and finite for a frozen one (the cap ends it).
    """
    return max(1, math.ceil(timeout / interval) + 1)


def _capture(source: CaptureSource) -> Snapshot:
    if callable(source):
        return source()
    return source.capture()
