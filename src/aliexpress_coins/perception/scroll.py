"""Scroll as a bounded search, never a remembered swipe distance (issue #42).

A task below the fold is not a fixed number of pixels away. The search captures
the current frame, looks for the target, then swipes a fraction of the viewport
height, re-captures and looks again. It stops on a found target or on a frame
that did not change (the list has reached its end), and the attempt count is
bounded. Every gesture endpoint is derived from the live frame's dimensions, so
the same code runs at the tablet and phone geometries.

The target predicate is caller-supplied: a task title lookup, a collect control,
or any other structural question. This module only owns the movement policy.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import numpy as np

from aliexpress_coins.perception.snapshot import Snapshot

if TYPE_CHECKING:
    from aliexpress_coins.control.controller import Controller, SwipeRecord

SCROLL_VIEWPORT_FRACTION = 0.55
"""How far one search swipe moves the content, as a fraction of frame height.

The issue's measured range is 0.5 to 0.6; 0.55 sits in the middle. The distance
is derived from the frame every time, not stored as pixels.
"""

MAX_SCROLL_ATTEMPTS = 4
"""The number of search swipes before the target is reported absent."""

SCROLL_START_FRACTION = 0.75
"""The finger's start height, as a fraction of the frame.

Scrolling down (revealing lower content) drags from the lower content area up.
The end is ``start - SCROLL_VIEWPORT_FRACTION``, so both endpoints move with the
screen and stay inside the controller's safe inset.
"""

STABLE_FRAME_MAX_DIFFERENCE = 0.01
"""Mean per-pixel luminance difference (0 to 1) that counts as a stable frame.

Two frames of the same list are usually identical after a swipe that reached
the end; a small threshold absorbs a blinking cursor or a re-rendered label
without mistaking a real scroll for stability.
"""


class ScrollOutcome(StrEnum):
    """Why the scroll search stopped."""

    FOUND = 'found'
    STABLE = 'stable'
    ATTEMPTS = 'attempts'


@dataclass(frozen=True, slots=True)
class ScrollAttempt:
    """One swipe and the frame captured after it."""

    index: int
    swipe: SwipeRecord
    frame: Snapshot
    difference: float


@dataclass(frozen=True, slots=True)
class ScrollResult[T]:
    """The target a search found, or the evidence that it stopped looking."""

    outcome: ScrollOutcome
    target: T | None
    frames: tuple[Snapshot, ...]
    attempts: tuple[ScrollAttempt, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, 'frames', tuple(self.frames))
        object.__setattr__(self, 'attempts', tuple(self.attempts))

    @property
    def found(self) -> bool:
        """Whether the predicate returned a target."""
        return self.outcome is ScrollOutcome.FOUND

    @property
    def stable(self) -> bool:
        """Whether the list stopped changing before the attempt bound."""
        return self.outcome is ScrollOutcome.STABLE

    @property
    def swipes(self) -> tuple[SwipeRecord, ...]:
        """Every gesture issued by the search, in order."""
        return tuple(attempt.swipe for attempt in self.attempts)

    @property
    def last(self) -> Snapshot:
        """The most recent frame, which is where a found target was read."""
        return self.frames[-1]


def frame_difference(first: Snapshot, second: Snapshot) -> float:
    """Mean absolute luminance difference between two frames, scaled to 0 to 1.

    A size change is treated as completely different, because a re-captured
    frame at another geometry is not evidence that the list did not move.
    """
    if (first.width, first.height) != (second.width, second.height):
        return 1.0
    left = np.asarray(first.image.convert('L'), dtype=np.int16)
    right = np.asarray(second.image.convert('L'), dtype=np.int16)
    return float(np.abs(left - right).mean()) / 255.0


def scroll_until_found[T](
    controller: Controller,
    capture: Callable[[], Snapshot],
    find: Callable[[Snapshot], T | None],
    *,
    initial: Snapshot | None = None,
    max_attempts: int = MAX_SCROLL_ATTEMPTS,
    viewport_fraction: float = SCROLL_VIEWPORT_FRACTION,
    stable_threshold: float = STABLE_FRAME_MAX_DIFFERENCE,
    after_swipe: Callable[[], None] | None = None,
    duration_ms: int | None = None,
    why: str = 'scroll to find the target',
) -> ScrollResult[T]:
    """Search down the current surface for ``find``'s target.

    The initial frame is checked first. Each attempt swipes a fraction of the
    frame height, invokes the optional ``after_swipe`` settle hook, captures,
    checks the target, then checks whether the screen changed at all. A stable
    frame or the attempt bound ends the search with no target, which the caller
    reports honestly instead of tapping a guess.
    """
    if max_attempts < 1:
        raise ValueError(f'max_attempts must be positive, got {max_attempts!r}')
    if not 0.0 < viewport_fraction < 1.0:
        raise ValueError(f'viewport_fraction must be between 0 and 1, got {viewport_fraction!r}')
    if not 0.0 <= stable_threshold <= 1.0:
        raise ValueError(f'stable_threshold must be between 0 and 1, got {stable_threshold!r}')

    frame = initial if initial is not None else capture()
    frames = [frame]
    target = find(frame)
    if target is not None:
        return ScrollResult(outcome=ScrollOutcome.FOUND, target=target, frames=(frame,))

    previous = frame
    attempts: list[ScrollAttempt] = []
    for index in range(1, max_attempts + 1):
        start = (frame.width / 2.0, frame.height * SCROLL_START_FRACTION)
        end = (frame.width / 2.0, frame.height * (SCROLL_START_FRACTION - viewport_fraction))
        swipe = (
            controller.swipe(start, end, why=why, label='scroll search')
            if duration_ms is None
            else controller.swipe(start, end, why=why, label='scroll search', duration_ms=duration_ms)
        )
        if after_swipe is not None:
            after_swipe()
        frame = capture()
        frames.append(frame)
        difference = frame_difference(previous, frame)
        attempts.append(ScrollAttempt(index=index, swipe=swipe, frame=frame, difference=difference))

        target = find(frame)
        if target is not None:
            return ScrollResult(
                outcome=ScrollOutcome.FOUND,
                target=target,
                frames=tuple(frames),
                attempts=tuple(attempts),
            )
        if difference <= stable_threshold:
            return ScrollResult(
                outcome=ScrollOutcome.STABLE,
                target=None,
                frames=tuple(frames),
                attempts=tuple(attempts),
            )
        previous = frame

    return ScrollResult(
        outcome=ScrollOutcome.ATTEMPTS,
        target=None,
        frames=tuple(frames),
        attempts=tuple(attempts),
    )
