"""Scroll-until-found over replayed frames at two geometries (issue #42)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from PIL import Image

from aliexpress_coins.control.controller import Controller
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.scroll import (
    MAX_SCROLL_ATTEMPTS,
    SCROLL_START_FRACTION,
    SCROLL_VIEWPORT_FRACTION,
    ScrollOutcome,
    frame_difference,
    scroll_until_found,
)
from aliexpress_coins.perception.snapshot import Snapshot, TextBox

CAPTURED_AT = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


class _SwipeDevice:
    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []
        self.swipes: list[tuple[int, int, int, int, int]] = []

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        self.swipes.append((x1, y1, x2, y2, duration_ms))


class _Capture:
    def __init__(self, frames: Sequence[Snapshot]) -> None:
        self._frames = tuple(frames)
        self._index = 0

    def __call__(self) -> Snapshot:
        frame = self._frames[min(self._index, len(self._frames) - 1)]
        self._index += 1
        return frame


def _box(text: str, left: float, top: float, right: float, bottom: float) -> TextBox:
    return TextBox(
        text=text,
        box=((left, top), (right, top), (right, bottom), (left, bottom)),
        score=0.99,
    )


def _snapshot(size: tuple[int, int], *, shade: int, target: bool = False) -> Snapshot:
    boxes = (_box('Target task', 100, 700, 600, 750),) if target else ()
    return Snapshot(
        image=Image.new('RGB', size, (shade, shade, shade)),
        boxes=boxes,
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(*size, density=240, sdk=35),
    )


def _find_target(snapshot: Snapshot) -> TextBox | None:
    return next((box for box in snapshot.boxes if box.text == 'Target task'), None)


def _controller(frame: Snapshot) -> tuple[Controller, _SwipeDevice]:
    device = _SwipeDevice()
    return Controller(device, frame.geometry), device


@pytest.mark.parametrize('size', [(1200, 2000), (1080, 1920)])
def test_scroll_finds_the_same_target_at_both_geometries(size: tuple[int, int]) -> None:
    first = _snapshot(size, shade=10)
    second = _snapshot(size, shade=80)
    third = _snapshot(size, shade=160, target=True)
    controller, device = _controller(first)

    result = scroll_until_found(controller, _Capture((second, third)), _find_target, initial=first)

    assert result.found
    assert result.target is not None
    assert result.target.text == 'Target task'
    assert len(result.attempts) == 2
    assert len(device.swipes) == 2
    assert result.swipes == tuple(attempt.swipe for attempt in result.attempts)
    assert result.last is third


def test_each_swipe_is_a_fraction_of_the_current_frame() -> None:
    sizes = [(1200, 2000), (1080, 1920)]
    requests: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for size in sizes:
        first = _snapshot(size, shade=10)
        second = _snapshot(size, shade=80, target=True)
        controller, _device = _controller(first)
        result = scroll_until_found(controller, _Capture((second,)), _find_target, initial=first)
        assert result.found
        requests.append(result.swipes[0].requested)

    for (width, height), (start, end) in zip(sizes, requests, strict=True):
        assert start == (round(width / 2), round(height * SCROLL_START_FRACTION))
        assert end == (round(width / 2), round(height * (SCROLL_START_FRACTION - SCROLL_VIEWPORT_FRACTION)))
        assert start[1] - end[1] == pytest.approx(height * SCROLL_VIEWPORT_FRACTION, abs=1)


def test_the_settle_hook_runs_between_each_swipe_and_capture() -> None:
    first = _snapshot((1200, 2000), shade=10)
    second = _snapshot((1200, 2000), shade=80, target=True)
    controller, device = _controller(first)
    settled: list[int] = []

    result = scroll_until_found(
        controller,
        _Capture((second,)),
        _find_target,
        initial=first,
        after_swipe=lambda: settled.append(len(device.swipes)),
    )

    assert result.found
    assert settled == [1]


def test_an_unchanged_frame_stops_the_search_as_stable() -> None:
    frame = _snapshot((1200, 2000), shade=30)
    controller, device = _controller(frame)

    result = scroll_until_found(controller, _Capture((frame, frame)), _find_target, initial=frame)

    assert result.stable
    assert result.target is None
    assert len(result.attempts) == 1
    assert len(device.swipes) == 1
    assert result.attempts[0].difference == pytest.approx(0.0)


def test_a_moving_frame_that_never_shows_the_target_hits_the_attempt_bound() -> None:
    frames = [_snapshot((1200, 2000), shade=shade) for shade in (10, 60, 120, 180, 240)]
    controller, device = _controller(frames[0])

    result = scroll_until_found(
        controller,
        _Capture(frames[1:]),
        _find_target,
        initial=frames[0],
        max_attempts=2,
    )

    assert result.outcome is ScrollOutcome.ATTEMPTS
    assert result.target is None
    assert len(result.attempts) == 2
    assert len(result.frames) == 3
    assert len(device.swipes) == 2


def test_a_target_in_the_initial_frame_costs_no_swipe() -> None:
    frame = _snapshot((1200, 2000), shade=30, target=True)
    controller, device = _controller(frame)

    result = scroll_until_found(controller, _Capture((frame,)), _find_target, initial=frame)

    assert result.found
    assert result.attempts == ()
    assert device.swipes == []


def test_frame_difference_is_normalised_and_rejects_a_size_change() -> None:
    first = _snapshot((1200, 2000), shade=0)
    same = _snapshot((1200, 2000), shade=0)
    brighter = _snapshot((1200, 2000), shade=255)
    resized = _snapshot((1080, 1920), shade=0)

    assert frame_difference(first, same) == pytest.approx(0.0)
    assert frame_difference(first, brighter) == pytest.approx(1.0)
    assert frame_difference(first, resized) == pytest.approx(1.0)


@pytest.mark.parametrize(
    ('kwargs', 'message'),
    [
        ({'max_attempts': 0}, 'max_attempts must be positive'),
        ({'viewport_fraction': 0.0}, 'viewport_fraction must be between 0 and 1'),
        ({'viewport_fraction': 1.0}, 'viewport_fraction must be between 0 and 1'),
        ({'stable_threshold': -0.1}, 'stable_threshold must be between 0 and 1'),
        ({'stable_threshold': 1.1}, 'stable_threshold must be between 0 and 1'),
    ],
)
def test_scroll_parameters_are_validated(kwargs: dict[str, object], message: str) -> None:
    frame = _snapshot((1200, 2000), shade=30)
    controller, _device = _controller(frame)

    with pytest.raises(ValueError, match=message):
        scroll_until_found(controller, _Capture((frame,)), _find_target, initial=frame, **cast(Any, kwargs))


def test_the_attempt_bound_is_a_small_reusable_constant() -> None:
    assert MAX_SCROLL_ATTEMPTS == 4
