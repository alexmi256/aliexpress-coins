"""Replay one recorded task frame at a second geometry (issue #42)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.ocr import RapidOcrEngine
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.tasks.catalogue import load_task_catalogue
from aliexpress_coins.tasks.discovery import enumerate_roster

ROOT = Path(__file__).resolve().parents[1]
BOOST = ROOT / 'tests' / 'data' / 'states' / 'boost.png'
COINS = ROOT / 'tests' / 'data' / 'states' / 'coins.png'
CATALOGUE = ROOT / 'specs' / 'tasks.yaml'
CAPTURED_AT = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def _load(path: Path, size: tuple[int, int] | None = None) -> Snapshot:
    with Image.open(path) as opened:
        image = opened.convert('RGB')
    boxes = RapidOcrEngine().read(image)
    if size is not None:
        width, height = size
        scale_x, scale_y = width / image.width, height / image.height

        def _scaled(point: tuple[float, float]) -> tuple[float, float]:
            return point[0] * scale_x, point[1] * scale_y

        boxes = tuple(
            TextBox(
                text=box.text,
                box=(_scaled(box.box[0]), _scaled(box.box[1]), _scaled(box.box[2]), _scaled(box.box[3])),
                score=box.score,
            )
            for box in boxes
        )
        image = image.resize(size, Image.Resampling.LANCZOS)
    return Snapshot(
        image=image,
        boxes=boxes,
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(image.width, image.height, density=240 if size is None else 320, sdk=35),
    )


@pytest.mark.parametrize('path', [BOOST, COINS], ids=['boost-page', 'coin-page'])
@pytest.mark.slow
def test_a_scaled_task_frame_yields_the_same_roster_keys(path: Path) -> None:
    catalogue = load_task_catalogue(CATALOGUE)
    tablet = _load(path)
    phone = _load(path, (1080, 1920))

    tablet_roster = enumerate_roster(tablet, catalogue)
    phone_roster = enumerate_roster(phone, catalogue)

    assert (tablet.width, tablet.height) == (1200, 2000)
    assert (phone.width, phone.height) == (1080, 1920)
    assert phone.profile_id != tablet.profile_id
    assert tablet_roster.titles == phone_roster.titles
    assert tuple(entry.key or entry.title for entry in tablet_roster.entries) == tuple(
        entry.key or entry.title for entry in phone_roster.entries
    )
    assert all(entry.action is not None for entry in phone_roster.entries)


@pytest.mark.parametrize(('path', 'count'), [(BOOST, 4), (COINS, 3)], ids=['boost-page', 'coin-page'])
@pytest.mark.slow
def test_every_scaled_action_stays_right_of_its_title(path: Path, count: int) -> None:
    catalogue = load_task_catalogue(CATALOGUE)
    phone = _load(path, (1080, 1920))

    roster = enumerate_roster(phone, catalogue)

    assert roster.count == count
    for entry in roster.entries:
        assert entry.action is not None
        title = next(box for box in entry.row.boxes if box.text == entry.title)
        assert entry.action.center[0] > title.center[0]
