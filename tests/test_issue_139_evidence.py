"""Sanitized evidence for issue #139: a purchase caption inside a product photo.

The fixture is the OCR half of the failure recorder's ``frame-004.jpg``. It keeps
the measured grid, the Hyundai card's title and the ``Add To Cart`` line painted
inside that card's photograph. The image is deliberately not committed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.runner.safety import AbortCode, RunAborted, SafetyGuard, find_dangerous_controls
from aliexpress_coins.tasks.catalogue import TaskCatalogue, load_task_catalogue
from aliexpress_coins.tasks.surprise import SURPRISE_TITLE, find_imagery_captions, is_surprise_grid

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'tests' / 'data' / 'flows' / 'surprise_items_issue_139.json'
CATALOGUE: TaskCatalogue = load_task_catalogue(ROOT / 'specs' / 'tasks.yaml')
CAPTURED_AT = datetime(2026, 11, 25, 15, 12, tzinfo=UTC)


def _payload() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(EVIDENCE.read_text(encoding='utf-8'))
    return payload


def _box(text: str, left: float, top: float, right: float, bottom: float, score: float) -> TextBox:
    return TextBox(
        text=text,
        box=((left, top), (right, top), (right, bottom), (left, bottom)),
        score=score,
    )


def _frame(*, moved_caption: tuple[float, float, float, float] | None = None) -> Snapshot:
    payload = _payload()
    frame = payload['frame']
    boxes: list[TextBox] = []
    for item in frame['boxes']:
        left, top, right, bottom = item['box']
        if moved_caption is not None and item['text'] == payload['observed']['caption']:
            left, top, right, bottom = moved_caption
        boxes.append(_box(item['text'], left, top, right, bottom, item['score']))
    width, height = frame['width'], frame['height']
    return Snapshot(
        image=Image.new('RGB', (width, height), 'white'),
        boxes=tuple(boxes),
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(width, height, density=240, sdk=35),
    )


def test_issue_139_preserves_the_caption_the_failure_recorder_captured() -> None:
    payload = _payload()

    assert payload['issue'] == 139
    assert payload['run_id'] == '20261125T151001Z'
    assert payload['failure']['code'] == 'dangerous_control'
    assert "'Add To Cart'" in payload['failure']['detail']
    assert payload['observed']['caption'] == 'Add To Cart'
    assert payload['observed']['inside_tile'] == 'For Hyundai Tucson 2 LM IX35 2009 -2015'


def test_issue_139_product_photo_caption_is_not_a_live_control() -> None:
    frame = _frame()

    assert is_surprise_grid(frame) is True
    # Without the surprise-task scope the same line is still a live control:
    # the exemption is the grid's product imagery, not the caption's spelling.
    assert [box.text for box in find_dangerous_controls(frame)] == ['Add To Cart']
    assert find_dangerous_controls(frame, task_key='surprise_items') == ()

    pairs = find_imagery_captions(frame)
    assert [(caption.text, tile.label) for caption, tile in pairs] == [
        ('Add To Cart', 'For Hyundai Tucson 2 LM IX35 2009 -2015'),
    ]


def test_issue_139_the_guard_does_not_abort_on_the_captured_photo() -> None:
    guard = SafetyGuard(catalogue=CATALOGUE)
    frame = _frame()

    with guard.task(SURPRISE_TITLE, key='surprise_items'):
        guard.inspect(frame)
        guard.inspect(frame)
        guard.inspect(frame)

    assert guard.streak == 0


def test_issue_139_a_caption_in_page_chrome_still_aborts() -> None:
    # Move the same caption out of every card's image band, below the last
    # card's title and above the content bottom. That is page-level text, not
    # a photograph, so the guard must keep its teeth.
    frame = _frame(moved_caption=(760, 1810, 980, 1830))
    guard = SafetyGuard(catalogue=CATALOGUE)

    assert [box.text for box in find_dangerous_controls(frame, task_key='surprise_items')] == ['Add To Cart']
    with pytest.raises(RunAborted) as raised, guard.task(SURPRISE_TITLE, key='surprise_items'):
        guard.inspect(frame)

    assert raised.value.code is AbortCode.DANGEROUS_CONTROL
    assert 'Add To Cart' in raised.value.reason
