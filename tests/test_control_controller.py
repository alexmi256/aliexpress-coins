"""Tap planning, clamping, jitter, logging and dry-run (issue #17)."""

from __future__ import annotations

import json
import logging
import random
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from PIL import Image

from aliexpress_coins.control.controller import (
    LOGGER_NAME,
    MAX_JITTER_FRACTION,
    SAFE_INSET_FRACTION,
    Controller,
    TapRecord,
    clamp_point,
    jitter_radius,
    safe_inset,
)
from aliexpress_coins.control.device import InputDevice
from aliexpress_coins.control.errors import InputError, TapTargetNotFound
from aliexpress_coins.perception.geometry import DeviceGeometry, parse_geometry
from aliexpress_coins.perception.snapshot import Snapshot, TextBox

CAPTURED_AT = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
GEOMETRY = DeviceGeometry.from_frame(1200, 2000, density=240, sdk=35)


class _FakeDevice:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def tap(self, x: int, y: int) -> None:
        self.calls.append((x, y))


class _FailingDevice:
    def tap(self, x: int, y: int) -> None:
        raise InputError(f'could not tap ({x}, {y})')


class _ZeroRng:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def randint(self, a: int, b: int) -> int:
        self.calls.append((a, b))
        return 0


class _SequenceRng:
    def __init__(self, values: list[int]) -> None:
        self.calls: list[tuple[int, int]] = []
        self._values = iter(values)

    def randint(self, a: int, b: int) -> int:
        self.calls.append((a, b))
        value = next(self._values)
        assert a <= value <= b
        return value


def _geometry(width: int = 1200, height: int = 2000) -> DeviceGeometry:
    return DeviceGeometry.from_frame(width, height, density=240, sdk=35)


def _box(
    text: str,
    left: float,
    top: float,
    right: float,
    bottom: float,
    score: float = 0.95,
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
        geometry=_geometry(*size),
    )


def _controller(
    device: InputDevice | None = None,
    *,
    geometry: DeviceGeometry = GEOMETRY,
    dry_run: bool = False,
    rng: Any = None,
    logger: logging.Logger | None = None,
) -> Controller:
    return Controller(
        device if device is not None else _FakeDevice(),
        geometry,
        dry_run=dry_run,
        rng=rng if rng is not None else _ZeroRng(),
        logger=logger,
    )


def test_clamping_lands_inside_the_derived_inset_at_every_edge() -> None:
    device = _FakeDevice()
    controller = _controller(device)

    cases = {
        (0.0, 0.0): (48, 48),
        (-100.0, -200.0): (48, 48),
        (1200.0, 2000.0): (1152, 1952),
        (5000.0, 9000.0): (1152, 1952),
        (600.0, 1000.0): (600, 1000),
        (1200.0, 1000.0): (1152, 1000),
        (600.0, 2000.0): (600, 1952),
    }

    for requested, expected in cases.items():
        record = controller.tap(requested, why='edge probe')

        assert record.clamped == expected
        assert record.point == expected
        assert record.jitter == (0, 0)
        assert device.calls[-1] == expected


def test_safe_inset_and_jitter_are_fractions_of_the_shorter_dimension() -> None:
    assert SAFE_INSET_FRACTION == 0.04
    assert MAX_JITTER_FRACTION == 0.005
    assert safe_inset(GEOMETRY) == 48
    assert jitter_radius(GEOMETRY) == 6

    phone = _geometry(1080, 1920)
    assert safe_inset(phone) == 43
    assert jitter_radius(phone) == 5


def test_tiny_geometry_degrades_to_a_valid_clamp_without_negative_coordinates() -> None:
    tiny = _geometry(10, 10)
    assert safe_inset(tiny) == 1
    assert jitter_radius(tiny) == 0
    assert clamp_point((0.0, 0.0), tiny) == (1, 1)
    assert clamp_point((10.0, 10.0), tiny) == (9, 9)

    single_pixel = _geometry(1, 5)
    assert safe_inset(single_pixel) == 0
    assert clamp_point((9.0, 9.0), single_pixel) == (1, 5)


def test_clamp_point_rounds_to_the_nearest_pixel() -> None:
    assert clamp_point((600.6, 999.4), GEOMETRY) == (601, 999)


def test_zero_radius_skips_the_rng() -> None:
    rng = _ZeroRng()
    controller = _controller(_FakeDevice(), geometry=_geometry(10, 10), rng=rng)

    record = controller.tap((5.0, 5.0), why='tiny screen')

    assert record.jitter == (0, 0)
    assert rng.calls == []


def test_jitter_uses_the_injected_rng_and_logs_the_applied_offset() -> None:
    rng = _SequenceRng([-3, 4])
    device = _FakeDevice()
    controller = _controller(device, rng=rng)

    record = controller.tap((600.0, 1000.0), why='jitter probe')

    assert rng.calls == [(-6, 6), (-6, 6)]
    assert record.clamped == (600, 1000)
    assert record.jitter == (-3, 4)
    assert record.point == (597, 1004)
    assert device.calls == [(597, 1004)]


def test_jitter_is_reproducible_and_always_stays_inside_the_safe_region() -> None:
    points = (
        (0.0, 0.0),
        (1.0, 1.0),
        (-100.0, 3000.0),
        (600.0, 1000.0),
        (1200.0, 2000.0),
        (1199.0, 1.0),
        (100.0, 1900.0),
        (600.0, 0.0),
    )
    inset = safe_inset(GEOMETRY)
    radius = jitter_radius(GEOMETRY)
    lower_x, upper_x = inset, GEOMETRY.width - inset
    lower_y, upper_y = inset, GEOMETRY.height - inset

    for point in points:
        first_device = _FakeDevice()
        second_device = _FakeDevice()
        first = _controller(first_device, rng=random.Random(20261121)).tap(point, why='grid probe')
        second = _controller(second_device, rng=random.Random(20261121)).tap(point, why='grid probe')

        assert first.point == second.point
        assert first.jitter == second.jitter
        assert first_device.calls == second_device.calls
        assert lower_x <= first.point[0] <= upper_x
        assert lower_y <= first.point[1] <= upper_y
        assert abs(first.jitter[0]) <= radius
        assert abs(first.jitter[1]) <= radius


def test_default_rng_produces_a_bounded_tap() -> None:
    device = _FakeDevice()
    controller = Controller(device, GEOMETRY)

    record = controller.tap((600.0, 1000.0), why='default rng')

    assert controller.geometry is GEOMETRY
    assert safe_inset(GEOMETRY) <= record.point[0] <= GEOMETRY.width - safe_inset(GEOMETRY)
    assert safe_inset(GEOMETRY) <= record.point[1] <= GEOMETRY.height - safe_inset(GEOMETRY)
    assert device.calls == [record.point]


def test_a_tap_cannot_be_issued_without_a_reason_keyword() -> None:
    device = _FakeDevice()
    controller = _controller(device)

    with pytest.raises(TypeError):
        cast(Any, controller.tap)((10.0, 10.0))

    assert device.calls == []
    assert controller.tap_count == 0


@pytest.mark.parametrize('reason', ['', ' ', '\n\t'])
def test_tap_rejects_an_empty_or_whitespace_reason(reason: str) -> None:
    device = _FakeDevice()
    controller = _controller(device)

    with pytest.raises(ValueError, match='reason is required'):
        controller.tap((10.0, 10.0), why=reason)

    assert device.calls == []
    assert controller.tap_count == 0


def test_box_and_text_taps_also_require_a_reason() -> None:
    controller = _controller()
    box = _box('Claim', 580, 980, 620, 1020)
    snapshot = _snapshot(box)

    with pytest.raises(TypeError):
        cast(Any, controller.tap_box)(box)
    with pytest.raises(TypeError):
        cast(Any, controller.tap_text)(snapshot, 'Claim')

    with pytest.raises(ValueError, match='reason is required'):
        controller.tap_box(box, why='')
    with pytest.raises(ValueError, match='reason is required'):
        controller.tap_text(snapshot, 'Claim', why='   ')


def test_tap_rejects_non_finite_coordinates() -> None:
    controller = _controller()

    with pytest.raises(ValueError, match='finite'):
        controller.tap((float('nan'), 10.0), why='bad coordinate')
    with pytest.raises(ValueError, match='finite'):
        controller.tap((10.0, float('inf')), why='bad coordinate')


def test_tap_box_uses_the_box_centre_and_its_text_as_the_default_label() -> None:
    device = _FakeDevice()
    controller = _controller(device)
    box = _box('Claim', 550, 980, 650, 1020)

    record = controller.tap_box(box, why='claim button')

    assert record.requested == (600, 1000)
    assert record.point == (600, 1000)
    assert record.label == 'Claim'
    assert device.calls == [(600, 1000)]

    labelled = controller.tap_box(box, why='claim button', label='daily reward')
    assert labelled.label == 'daily reward'


def test_tap_box_clamps_a_box_whose_centre_is_at_the_screen_edge() -> None:
    device = _FakeDevice()
    controller = _controller(device)
    box = _box('Edge', 1190, 1990, 1200, 2000)

    record = controller.tap_box(box, why='edge box')

    assert record.point == (1152, 1952)
    assert device.calls == [(1152, 1952)]


def test_tap_text_finds_the_label_and_uses_the_frames_geometry() -> None:
    device = _FakeDevice()
    controller = _controller(device, geometry=_geometry(400, 800))
    snapshot = _snapshot(_box('Claim', 580, 980, 620, 1020), _box('Other', 10, 10, 20, 20))

    record = controller.tap_text(snapshot, 'Claim', why='claim button')

    assert record.label == 'Claim'
    assert record.requested == (600, 1000)
    assert record.point == (600, 1000)
    assert device.calls == [(600, 1000)]


def test_controller_from_frame_reconciles_the_landscape_probe_with_a_portrait_frame() -> None:
    probe = parse_geometry('Physical size: 2000x1200\n', 'Physical density: 240\n', '35\n')
    assert probe.orientation == 'landscape'
    frame = _snapshot(_box('Close', 580, 1632, 620, 1672), size=(1200, 2000))
    frame = Snapshot(
        image=frame.image,
        boxes=frame.boxes,
        captured_at=frame.captured_at,
        geometry=probe.with_frame_size(1200, 2000),
    )
    device = _FakeDevice()

    controller = Controller.from_frame(device, frame, rng=_ZeroRng())
    record = controller.tap_box(frame.boxes[0], why='close the popup')

    assert controller.geometry == frame.geometry
    assert record.point == (600, 1652)
    assert device.calls == [(600, 1652)]


def test_tap_text_reports_a_missing_label_without_tapping(
    caplog: pytest.LogCaptureFixture,
) -> None:
    device = _FakeDevice()
    controller = _controller(device)
    snapshot = _snapshot(_box('Other', 10, 10, 20, 20))

    with (
        caplog.at_level(logging.INFO, logger=LOGGER_NAME),
        pytest.raises(TapTargetNotFound, match=r"'Claim'.*1200x2000"),
    ):
        controller.tap_text(snapshot, 'Claim', why='claim button')

    assert device.calls == []
    assert controller.tap_count == 0
    messages = [json.loads(record.getMessage()) for record in caplog.records if record.name == LOGGER_NAME]
    assert messages == [
        {
            'event': 'tap_miss',
            'label': 'Claim',
            'reason': 'claim button',
            'frame': [1200, 2000],
        },
    ]


def test_dry_run_suppresses_the_device_but_keeps_the_log_and_counter(
    caplog: pytest.LogCaptureFixture,
) -> None:
    device = _FakeDevice()
    controller = _controller(device, dry_run=True)

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        records = [
            controller.tap((100.0, 200.0), why='first plan', label='one'),
            controller.tap((300.0, 400.0), why='second plan', label='two'),
            controller.tap((500.0, 600.0), why='third plan', label='three'),
        ]

    assert device.calls == []
    assert controller.dry_run is True
    assert controller.tap_count == 3
    assert [record.suppressed for record in records] == [True, True, True]

    messages = [json.loads(record.getMessage()) for record in caplog.records if record.name == LOGGER_NAME]
    assert len(messages) == 3
    assert [message['tap'] for message in messages] == [1, 2, 3]
    assert [message['point'] for message in messages] == [[100, 200], [300, 400], [500, 600]]
    assert all(message['suppressed'] is True for message in messages)


def test_a_live_tap_logs_one_stable_machine_readable_json_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    device = _FakeDevice()
    logger = logging.getLogger(LOGGER_NAME)
    controller = _controller(device, rng=_SequenceRng([1, -2]), logger=logger)

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        record = controller.tap((600.4, 1000.6), why='  claim the daily reward  ', label='Claim')

    messages = [log.getMessage() for log in caplog.records if log.name == LOGGER_NAME]
    assert len(messages) == 1
    assert '\n' not in messages[0]
    assert json.loads(messages[0]) == {
        'event': 'tap',
        'tap': 1,
        'label': 'Claim',
        'reason': 'claim the daily reward',
        'requested': [600, 1001],
        'clamped': [600, 1001],
        'jitter': [1, -2],
        'point': [601, 999],
        'suppressed': False,
    }
    assert record.reason == 'claim the daily reward'
    assert device.calls == [(601, 999)]


def test_tap_count_includes_an_attempt_that_the_device_rejects(
    caplog: pytest.LogCaptureFixture,
) -> None:
    controller = Controller(_FailingDevice(), GEOMETRY, rng=_ZeroRng())

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME), pytest.raises(InputError, match='could not tap'):
        controller.tap((600.0, 1000.0), why='failing device')

    assert controller.tap_count == 1
    messages = [json.loads(record.getMessage()) for record in caplog.records if record.name == LOGGER_NAME]
    assert [message['suppressed'] for message in messages] == [False]


def test_tap_record_renders_the_same_fields_as_json() -> None:
    record = TapRecord(
        index=7,
        label='Claim',
        reason='claim button',
        requested=(600, 1000),
        clamped=(600, 1000),
        jitter=(1, -1),
        point=(601, 999),
        suppressed=True,
    )

    assert record.as_dict() == json.loads(record.to_json())
    assert record.as_dict()['event'] == 'tap'
