"""adb capture: one binary frame, validated before it becomes a `Snapshot`.

`screencap` can fail while still writing an error string to stdout. Taking that
as image data produces a corrupt frame that later looks like a rendering bug,
so the bytes are checked for the PNG magic and decoded before anything else sees
them. No device handle leaks into the resulting snapshot.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from io import BytesIO
from typing import Protocol

from PIL import Image, UnidentifiedImageError

from aliexpress_coins.perception.errors import FrameDecodeError, SnapshotError
from aliexpress_coins.perception.geometry import (
    DeviceGeometry,
    parse_geometry,
)
from aliexpress_coins.perception.ocr import OcrEngine
from aliexpress_coins.perception.snapshot import Snapshot
from aliexpress_coins.transport import AdbResult, AdbRunner, SubprocessAdbRunner

__all__ = [
    'AdbResult',
    'AdbRunner',
    'SubprocessAdbRunner',
]

PNG_MAGIC = b'\x89PNG\r\n\x1a\n'
_PREVIEW_BYTES = 200


class Snapshotter(Protocol):
    """The snapshot seam a controller or test can script."""

    def capture(self) -> Snapshot:
        """Capture one frame."""
        ...


Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """The default capture clock; injected in tests."""
    return datetime.now(UTC)


def _preview(data: bytes) -> str:
    if len(data) <= _PREVIEW_BYTES:
        return repr(data)
    return f'{data[:_PREVIEW_BYTES]!r}... ({len(data)} bytes)'


def _decode_png(data: bytes) -> Image.Image:
    if not data.startswith(PNG_MAGIC):
        raise FrameDecodeError(f'screencap did not return a PNG: {_preview(data)}')
    try:
        with Image.open(BytesIO(data)) as opened:
            opened.load()
            return opened.convert('RGB')
    except (OSError, SyntaxError, ValueError, UnidentifiedImageError) as exc:
        raise FrameDecodeError(
            f'screencap returned an undecodable PNG ({type(exc).__name__}: {exc}): {_preview(data)}',
        ) from exc


class AdbSnapshotter:
    """Capture PNG frames from one configured adb serial."""

    def __init__(
        self,
        serial: str,
        *,
        geometry: DeviceGeometry | None = None,
        ocr: OcrEngine | None = None,
        runner: AdbRunner | None = None,
        clock: Clock = utc_now,
        timeout: float = 30.0,
    ) -> None:
        if not serial.strip():
            raise ValueError('an adb serial is required')
        if timeout <= 0:
            raise ValueError('the adb timeout must be positive')
        self._serial = serial
        self._geometry = geometry
        self._ocr = ocr
        self._runner = runner if runner is not None else SubprocessAdbRunner()
        self._clock = clock
        self._timeout = timeout
        self._probed: DeviceGeometry | None = None

    def _run(self, args: Sequence[str]) -> AdbResult:
        try:
            return self._runner(args, self._timeout)
        except subprocess.TimeoutExpired as exc:
            raise SnapshotError(f'adb timed out after {self._timeout:g}s: {" ".join(args)}') from exc
        except OSError as exc:
            raise SnapshotError(f'could not run adb: {exc}') from exc

    def _shell_text(self, *command: str) -> str:
        args = ['adb', '-s', self._serial, 'shell', *command]
        result = self._run(args)
        if result.returncode != 0:
            raise SnapshotError(
                f'adb {" ".join(command)} exited {result.returncode}: '
                f'{_preview(result.stdout)} {_preview(result.stderr)}',
            )
        return result.stdout.decode('utf-8', errors='replace')

    def probe_geometry(self) -> DeviceGeometry:
        """Read width, density and SDK with the three read-only probes."""
        geometry = parse_geometry(
            self._shell_text('wm', 'size'),
            self._shell_text('wm', 'density'),
            self._shell_text('getprop', 'ro.build.version.sdk'),
        )
        self._probed = geometry
        return geometry

    def _base_geometry(self) -> DeviceGeometry:
        if self._probed is None:
            return self.probe_geometry()
        return self._probed

    def capture_png(self) -> bytes:
        """Run `exec-out screencap -p` and return bytes that start with PNG magic."""
        args = ['adb', '-s', self._serial, 'exec-out', 'screencap', '-p']
        result = self._run(args)
        if result.returncode != 0:
            raise SnapshotError(
                f'screencap exited {result.returncode}: {_preview(result.stdout)} {_preview(result.stderr)}',
            )
        if not result.stdout.startswith(PNG_MAGIC):
            raise SnapshotError(f'screencap did not return a PNG: {_preview(result.stdout)}')
        return result.stdout

    def capture(self) -> Snapshot:
        """Capture, validate, decode and OCR one frame."""
        png = self.capture_png()
        image = _decode_png(png)
        captured_at = self._clock()

        base = self._geometry if self._geometry is not None else self._base_geometry()
        geometry = base.with_frame_size(image.width, image.height)

        boxes = self._ocr.read(image) if self._ocr is not None else ()
        return Snapshot(image=image, boxes=boxes, captured_at=captured_at, geometry=geometry)
