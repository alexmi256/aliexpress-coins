"""The thin RapidOCR adapter against a fake engine (issue #12)."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from PIL import Image

from aliexpress_coins.perception.errors import OcrError
from aliexpress_coins.perception.ocr import RapidOcrEngine


class FakeEngine:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    def __call__(self, _image: Image.Image) -> Any:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def _result(boxes: Any, texts: Any, scores: Any) -> Any:
    return types.SimpleNamespace(boxes=boxes, txts=texts, scores=scores)


def _frame() -> Image.Image:
    return Image.new('RGB', (100, 50), 'white')


def test_adapter_returns_raw_boxes_in_order() -> None:
    engine = FakeEngine(
        _result(
            [
                [[10, 20], [80, 20], [80, 40], [10, 40]],
                [[20, 5], [90, 5], [90, 15], [20, 15]],
            ],
            ('Coins', 'Daily coins'),
            (0.98, 0.91),
        ),
    )
    boxes = RapidOcrEngine(engine=engine).read(_frame())

    assert [box.text for box in boxes] == ['Coins', 'Daily coins']
    assert boxes[0].box == ((10.0, 20.0), (80.0, 20.0), (80.0, 40.0), (10.0, 40.0))
    assert boxes[0].score == 0.98
    assert engine.calls == 1


def test_no_boxes_is_an_empty_result() -> None:
    assert RapidOcrEngine(engine=FakeEngine(_result(None, None, None))).read(_frame()) == ()
    assert RapidOcrEngine(engine=FakeEngine(_result([], None, None))).read(_frame()) == ()


def test_mismatched_box_and_text_counts_raise() -> None:
    engine = FakeEngine(_result([[[0, 0], [1, 0], [1, 1], [0, 1]]], ('one', 'two'), (0.9, 0.8)))
    with pytest.raises(OcrError, match='mismatched'):
        RapidOcrEngine(engine=engine).read(_frame())


def test_a_malformed_box_raises() -> None:
    engine = FakeEngine(_result([[[0, 0], [1, 0], [1, 1]]], ('one',), (0.9,)))
    with pytest.raises(OcrError, match='instead of four'):
        RapidOcrEngine(engine=engine).read(_frame())


def test_engine_exceptions_become_typed_errors() -> None:
    engine = FakeEngine(error=RuntimeError('onnx exploded'))
    with pytest.raises(OcrError, match='RuntimeError: onnx exploded'):
        RapidOcrEngine(engine=engine).read(_frame())


def test_an_unexpected_result_object_becomes_a_typed_error() -> None:
    engine = FakeEngine(result=object())
    with pytest.raises(OcrError, match='unexpected result'):
        RapidOcrEngine(engine=engine).read(_frame())


def test_a_single_string_text_is_not_split_into_characters() -> None:
    engine = FakeEngine(_result([[[0, 0], [1, 0], [1, 1], [0, 1]]], 'single', 0.5))
    boxes = RapidOcrEngine(engine=engine).read(_frame())
    assert [box.text for box in boxes] == ['single']


def test_rapidocr_is_imported_lazily_and_once(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict[str, object]] = []
    result = _result([[[0, 0], [1, 0], [1, 1], [0, 1]]], ('hi',), (0.7,))

    class FakeRapidOCR:
        def __init__(self, *, params: dict[str, object]) -> None:
            created.append(params)

        def __call__(self, _image: Image.Image) -> Any:
            return result

    fake_module = types.ModuleType('rapidocr')
    monkeypatch.setattr(fake_module, 'RapidOCR', FakeRapidOCR, raising=False)
    monkeypatch.setitem(sys.modules, 'rapidocr', fake_module)

    engine = RapidOcrEngine(params={'Global.log_level': 'error'})
    assert created == []
    assert engine.read(_frame())[0].text == 'hi'
    assert engine.read(_frame())[0].text == 'hi'
    assert created == [{'Global.log_level': 'error'}]
