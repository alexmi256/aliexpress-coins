"""Snapshot query helpers and the region crop (issue #12)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from PIL import Image

from aliexpress_coins.perception.errors import SnapshotError
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.snapshot import Snapshot, TextBox, crop_snapshot

CAPTURED_AT = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def _box(text: str, left: float, top: float, right: float, bottom: float, score: float = 0.95) -> TextBox:
    return TextBox(
        text=text,
        box=((left, top), (right, top), (right, bottom), (left, bottom)),
        score=score,
    )


def _snapshot(*boxes: TextBox, size: tuple[int, int] = (400, 800)) -> Snapshot:
    return Snapshot(
        image=Image.new('RGB', size, 'white'),
        boxes=boxes,
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(size[0], size[1], density=240, sdk=35),
    )


def test_text_box_geometry_helpers() -> None:
    box = _box('Coins', 10, 20, 30, 50, score=0.8)
    assert box.bbox == (10.0, 20.0, 30.0, 50.0)
    assert box.center == (20.0, 35.0)
    assert box.width == 20.0
    assert box.height == 30.0
    assert box.score == 0.8

    shifted = box.shifted(5, -10)
    assert shifted.text == 'Coins'
    assert shifted.box[0] == (15.0, 10.0)
    assert shifted.box[2] == (35.0, 40.0)
    assert shifted.score == box.score


def test_text_box_requires_four_points() -> None:
    with pytest.raises(ValueError, match='four points'):
        TextBox(text='bad', box=cast(Any, ((0, 0), (1, 1), (2, 2))), score=1.0)


def test_snapshot_find_and_has_are_exact_but_case_insensitive() -> None:
    snapshot = _snapshot(_box('Earn more', 0, 0, 10, 10), _box('Coins', 20, 20, 40, 40))
    assert snapshot.find(' coins ') == snapshot.boxes[1]
    assert snapshot.has('EARN MORE')
    assert snapshot.find('Earn') is None
    assert not snapshot.has('Vouchers')


def test_snapshot_description_names_geometry_and_every_box() -> None:
    snapshot = _snapshot(_box('Coins', 20, 30, 40, 50))
    description = snapshot.describe()
    assert 'Snapshot 400x800' in description
    assert f'profile={snapshot.profile_id}' in description
    assert 'boxes=1' in description
    assert "'Coins' @ (30.0, 40.0)" in description


def test_snapshot_holds_boxes_as_an_immutable_tuple() -> None:
    snapshot = _snapshot(_box('Coins', 0, 0, 10, 10))
    assert isinstance(snapshot.boxes, tuple)
    assert (snapshot.width, snapshot.height) == (400, 800)


def test_crop_shifts_boxes_and_keeps_only_centres_inside() -> None:
    inside = _box('inside', 80, 80, 120, 120)
    also_inside = _box('second', 240, 240, 260, 260)
    outside = _box('outside', 340, 340, 380, 380)
    on_the_edge = _box('edge', 290, 150, 310, 170)
    snapshot = _snapshot(inside, outside, also_inside, on_the_edge)
    original_profile = snapshot.profile_id

    cropped = snapshot.crop((50, 50, 300, 300))

    assert cropped.image.size == (250, 250)
    assert cropped.profile_id == original_profile
    assert cropped.geometry is snapshot.geometry
    assert cropped.captured_at == snapshot.captured_at
    assert [box.text for box in cropped.boxes] == ['inside', 'second']
    assert cropped.boxes[0].box[0] == (30.0, 30.0)
    assert cropped.boxes[1].box[0] == (190.0, 190.0)


def test_crop_module_function_matches_the_method() -> None:
    snapshot = _snapshot(_box('Coins', 20, 20, 40, 40))
    assert crop_snapshot(snapshot, (0, 0, 100, 100)).boxes == snapshot.crop((0, 0, 100, 100)).boxes


@pytest.mark.parametrize(
    'rect',
    [
        (-1, 0, 100, 100),
        (0, -1, 100, 100),
        (0, 0, 401, 100),
        (0, 0, 100, 801),
        (100, 100, 100, 200),
        (100, 100, 200, 100),
    ],
)
def test_crop_rejects_a_rectangle_outside_the_frame(rect: tuple[int, int, int, int]) -> None:
    with pytest.raises(SnapshotError):
        _snapshot().crop(rect)
