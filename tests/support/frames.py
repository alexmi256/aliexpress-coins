"""Replay recorded frames through the production ``Snapshotter`` seam (#36).

The controller and every task executor already take a `CaptureSource`; this
object implements the same one-method protocol, so a recorded fixture directory
drives the real wait, tap and popup code without a device. Frames are decoded
once and OCR'd once per call site (the engine may ask whether a frame changed),
so a replay is cheap and deterministic.

Two helpers exist because real flows need them:

* ``repeat=2`` replays each frame twice, which is what a stability wait needs;
* :meth:`ScriptedSnapshotter.override_call` substitutes one call, e.g. a frame
  that shows the row gone after the tap, without rewriting the whole sequence.

Exhaustion repeats the last frame, matching a live screen that does not change
between two captures. That is replay, not a hidden fallback: no frame in this
class ever comes from the device.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from PIL import Image

from aliexpress_coins.perception.capture import Clock, utc_now
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.ocr import OcrEngine
from aliexpress_coins.perception.snapshot import Snapshot

__all__ = [
    'ScriptedSnapshotter',
    'load_frame',
]

FrameSource = Snapshot | Path | str
"""A frame already in memory, or a path to a recorded PNG."""


def load_frame(
    path: Path | str,
    *,
    ocr: OcrEngine | None = None,
    clock: Clock = utc_now,
    density: int = 240,
    sdk: int = 35,
    geometry: DeviceGeometry | None = None,
) -> Snapshot:
    """Decode one recorded PNG and read its labels, exactly like a capture."""
    with Image.open(path) as opened:
        image = opened.convert('RGB')
    base = (
        geometry
        if geometry is not None
        else DeviceGeometry.from_frame(image.width, image.height, density=density, sdk=sdk)
    )
    boxes = ocr.read(image) if ocr is not None else ()
    return Snapshot(
        image=image,
        boxes=boxes,
        captured_at=clock(),
        geometry=base.with_frame_size(image.width, image.height),
    )


class ScriptedSnapshotter:
    """A `Snapshotter` that replays recorded frames in order."""

    def __init__(
        self,
        frames: Sequence[FrameSource],
        *,
        ocr: OcrEngine | None = None,
        clock: Clock = utc_now,
        density: int = 240,
        sdk: int = 35,
        geometry: DeviceGeometry | None = None,
    ) -> None:
        if not frames:
            raise ValueError('a scripted snapshotter needs at least one frame')
        self._frames = tuple(frames)
        self._ocr = ocr
        self._clock = clock
        self._density = density
        self._sdk = sdk
        self._geometry = geometry
        self._overrides: dict[int, FrameSource] = {}
        self.calls = 0
        """How many times ``capture()`` has been called."""

    @classmethod
    def from_directory(
        cls,
        directory: Path | str,
        *,
        pattern: str = '*.png',
        repeat: int = 1,
        ocr: OcrEngine | None = None,
        clock: Clock = utc_now,
        density: int = 240,
        sdk: int = 35,
        geometry: DeviceGeometry | None = None,
    ) -> ScriptedSnapshotter:
        """Replay every matching frame, sorted by name.

        ``repeat=2`` plays each frame twice for a stability wait. When the
        directory has a ``manifest.json`` from ``tools/capture_frames.py``, its
        density and SDK win over the defaults, so a replay is keyed by the
        geometry the frames were captured at rather than this machine's guess.
        """
        root = Path(directory)
        paths = sorted(root.glob(pattern))
        if not paths:
            raise ValueError(f'no frames match {pattern!r} in {root}')
        manifest_path = root / 'manifest.json'
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            density = int(manifest.get('density', density))
            sdk = int(manifest.get('sdk', sdk))
            width = manifest.get('width')
            height = manifest.get('height')
            if width is not None and height is not None:
                geometry = DeviceGeometry.from_frame(int(width), int(height), density=density, sdk=sdk)
        frames: list[FrameSource] = []
        for path in paths:
            frames.extend([path] * repeat)
        return cls(
            frames,
            ocr=ocr,
            clock=clock,
            density=density,
            sdk=sdk,
            geometry=geometry,
        )

    def override_call(self, index: int, frame: FrameSource) -> None:
        """Use ``frame`` for the zero-based ``index``-th capture instead."""
        if index < 0:
            raise ValueError('a capture index cannot be negative')
        self._overrides[index] = frame

    def capture(self) -> Snapshot:
        """Return the next recorded frame, or the last one when exhausted."""
        index = self.calls
        self.calls += 1
        source = self._overrides.get(index, self._frames[min(index, len(self._frames) - 1)])
        if isinstance(source, Snapshot):
            return source
        return load_frame(
            source,
            ocr=self._ocr,
            clock=self._clock,
            density=self._density,
            sdk=self._sdk,
            geometry=self._geometry,
        )
