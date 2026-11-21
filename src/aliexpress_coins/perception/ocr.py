"""OCR seam: a protocol and one thin RapidOCR adapter.

The index, fuzzy matching and row inference are #13's job. This adapter only
turns a frame into raw text boxes; it never downloads a model, because the
RapidOCR wheel bundles the PP-OCRv6 small models used by the image.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from PIL import Image

from aliexpress_coins.perception.errors import OcrError
from aliexpress_coins.perception.snapshot import TextBox


class OcrEngine(Protocol):
    """Anything that can return raw OCR boxes for a frame."""

    def read(self, image: Image.Image) -> tuple[TextBox, ...]:
        """Read one frame, in its original coordinates."""
        ...


class RapidOcrEngine:
    """RapidOCR on CPU, with model loading deferred until the first frame."""

    def __init__(
        self,
        *,
        engine: Any | None = None,
        params: Mapping[str, object] | None = None,
    ) -> None:
        self._engine = engine
        self._params = dict(params) if params is not None else {'Global.log_level': 'error'}

    def _instance(self) -> Any:
        if self._engine is None:
            try:
                from rapidocr import RapidOCR
            except ImportError as exc:  # pragma: no cover - the pinned runtime has it
                raise OcrError(f'RapidOCR is not installed: {exc}') from exc
            self._engine = RapidOCR(params=self._params)
        return self._engine

    def read(self, image: Image.Image) -> tuple[TextBox, ...]:
        """Run detection, orientation and recognition on one frame."""
        try:
            result = self._instance()(image)
        except OcrError:
            raise
        except Exception as exc:
            raise OcrError(f'OCR failed: {type(exc).__name__}: {exc}') from exc
        return _boxes_from_result(result)


def _as_sequence(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    try:
        return tuple(value)
    except TypeError:
        return (value,)


def _boxes_from_result(result: Any) -> tuple[TextBox, ...]:
    """Normalise RapidOCR's arrays into immutable, JSON-friendly boxes."""
    try:
        raw_boxes = result.boxes
        raw_texts = result.txts
        raw_scores = result.scores
    except AttributeError as exc:
        raise OcrError(f'OCR returned an unexpected result: {type(result).__name__}') from exc

    if raw_boxes is None:
        return ()

    boxes = tuple(raw_boxes)
    texts = _as_sequence(raw_texts)
    scores = _as_sequence(raw_scores)
    if len(texts) != len(boxes) or len(scores) != len(boxes):
        raise OcrError(
            f'OCR returned mismatched boxes, texts and scores ({len(boxes)}, {len(texts)}, {len(scores)})',
        )

    parsed: list[TextBox] = []
    for raw_box, text, score in zip(boxes, texts, scores, strict=True):
        points = tuple((float(x), float(y)) for x, y in raw_box)
        if len(points) != 4:
            raise OcrError(f'OCR returned a box with {len(points)} points instead of four')
        parsed.append(TextBox(text=str(text), box=points, score=float(score)))
    return tuple(parsed)
