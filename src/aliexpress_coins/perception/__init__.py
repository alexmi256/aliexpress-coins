"""Perception: capture a frame, read its labels and keep the pair replayable."""

from __future__ import annotations

from aliexpress_coins.perception.artifacts import ArtifactPaths, ArtifactStore
from aliexpress_coins.perception.capture import (
    PNG_MAGIC,
    AdbResult,
    AdbRunner,
    AdbSnapshotter,
    Snapshotter,
    SubprocessAdbRunner,
    utc_now,
)
from aliexpress_coins.perception.errors import (
    ArtifactError,
    OcrError,
    PerceptionError,
    SnapshotError,
)
from aliexpress_coins.perception.geometry import (
    DeviceGeometry,
    Orientation,
    orientation_for,
    parse_geometry,
    parse_sdk,
    parse_wm_density,
    parse_wm_size,
)
from aliexpress_coins.perception.matching import (
    DEFAULT_MIN_OCR_SCORE,
    DEFAULT_MIN_SCORE,
    STRICT_MIN_SCORE,
    LabelSource,
    TextIndex,
    TextMatch,
    find_all,
    find_text,
    normalize,
    similarity,
)
from aliexpress_coins.perception.ocr import OcrEngine, RapidOcrEngine
from aliexpress_coins.perception.snapshot import (
    BoxPoints,
    Point,
    Rect,
    Snapshot,
    TextBox,
    crop_snapshot,
)

__all__ = [
    'DEFAULT_MIN_OCR_SCORE',
    'DEFAULT_MIN_SCORE',
    'PNG_MAGIC',
    'STRICT_MIN_SCORE',
    'AdbResult',
    'AdbRunner',
    'AdbSnapshotter',
    'ArtifactError',
    'ArtifactPaths',
    'ArtifactStore',
    'BoxPoints',
    'DeviceGeometry',
    'LabelSource',
    'OcrEngine',
    'OcrError',
    'Orientation',
    'PerceptionError',
    'Point',
    'RapidOcrEngine',
    'Rect',
    'Snapshot',
    'SnapshotError',
    'Snapshotter',
    'SubprocessAdbRunner',
    'TextBox',
    'TextIndex',
    'TextMatch',
    'crop_snapshot',
    'find_all',
    'find_text',
    'normalize',
    'orientation_for',
    'parse_geometry',
    'parse_sdk',
    'parse_wm_density',
    'parse_wm_size',
    'similarity',
    'utc_now',
]
