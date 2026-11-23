"""Capture validation against a fake adb runner; no device required (issue #12)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from aliexpress_coins.perception.capture import (
    PNG_MAGIC,
    AdbResult,
    AdbSnapshotter,
    SubprocessAdbRunner,
    _decode_png,
)
from aliexpress_coins.perception.errors import OcrError, SnapshotError
from aliexpress_coins.perception.geometry import DeviceGeometry, parse_geometry
from aliexpress_coins.perception.ocr import OcrEngine
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from tests.support.adb import ScriptedAdbRunner

FRAME_PNG = (Path(__file__).parent / 'data' / 'frame.png').read_bytes()
CAPTURED_AT = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
GEOMETRY = DeviceGeometry.from_frame(1200, 2000, density=240, sdk=35)
SCREENCAP_ARGS = ['adb', '-s', '192.0.2.10:5555', 'exec-out', 'screencap', '-p']


class FakeOcr:
    def __init__(self, boxes: tuple[TextBox, ...] = ()) -> None:
        self.boxes = boxes
        self.seen: list[tuple[int, int]] = []

    def read(self, image: Image.Image) -> tuple[TextBox, ...]:
        self.seen.append((image.width, image.height))
        return self.boxes


def _box(text: str) -> TextBox:
    return TextBox(text=text, box=((10, 10), (100, 10), (100, 50), (10, 50)), score=0.9)


def test_capture_returns_a_decoded_snapshot() -> None:
    ocr: OcrEngine = FakeOcr((_box('Coins'), _box('Daily coins')))
    runner = ScriptedAdbRunner(AdbResult(0, FRAME_PNG, b''))
    snapshotter = AdbSnapshotter(
        '192.0.2.10:5555',
        geometry=GEOMETRY,
        ocr=ocr,
        runner=runner,
        clock=lambda: CAPTURED_AT,
    )

    snapshot = snapshotter.capture()

    assert list(runner.calls[0]) == SCREENCAP_ARGS
    assert snapshot.image.size == (1200, 2000)
    assert snapshot.image.mode == 'RGB'
    assert snapshot.boxes == (_box('Coins'), _box('Daily coins'))
    assert snapshot.captured_at == CAPTURED_AT
    assert snapshot.geometry == GEOMETRY
    assert cast_fake_ocr(ocr).seen == [(1200, 2000)]


def cast_fake_ocr(engine: OcrEngine) -> FakeOcr:
    assert isinstance(engine, FakeOcr)
    return engine


def test_capture_without_an_ocr_engine_has_no_boxes() -> None:
    snapshot = AdbSnapshotter(
        '192.0.2.10:5555',
        geometry=GEOMETRY,
        runner=ScriptedAdbRunner(AdbResult(0, FRAME_PNG, b'')),
    ).capture()
    assert snapshot.boxes == ()


def test_capture_png_checks_the_magic_before_decoding() -> None:
    snapshotter = AdbSnapshotter('serial', runner=ScriptedAdbRunner(AdbResult(0, b'not a png', b'')))
    with pytest.raises(SnapshotError, match='did not return a PNG'):
        snapshotter.capture_png()


def test_decoding_also_checks_the_magic_on_its_own() -> None:
    with pytest.raises(SnapshotError, match='did not return a PNG'):
        _decode_png(b'error: closed')


def test_the_real_runner_returns_raw_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(args: Sequence[str], **kwargs: object) -> SimpleNamespace:
        seen.append(list(args))
        assert kwargs['capture_output'] is True
        assert kwargs['timeout'] == 2.5
        return SimpleNamespace(returncode=0, stdout=b'\x89PNG', stderr=b'warn')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    result = SubprocessAdbRunner()(['adb', 'exec-out', 'screencap', '-p'], 2.5)

    assert result == AdbResult(0, b'\x89PNG', b'warn')
    assert seen == [['adb', 'exec-out', 'screencap', '-p']]


def test_a_second_capture_reuses_the_probed_geometry() -> None:
    runner = ScriptedAdbRunner(
        AdbResult(0, FRAME_PNG, b''),
        AdbResult(0, b'Physical size: 2000x1200\n', b''),
        AdbResult(0, b'Physical density: 240\n', b''),
        AdbResult(0, b'35\n', b''),
        AdbResult(0, FRAME_PNG, b''),
    )
    snapshotter = AdbSnapshotter('serial', runner=runner)

    first = snapshotter.capture()
    second = snapshotter.capture()

    assert first.profile_id == second.profile_id
    assert len(runner.calls) == 5


def test_non_png_response_raises_and_names_the_output() -> None:
    snapshotter = AdbSnapshotter(
        'serial',
        geometry=GEOMETRY,
        runner=ScriptedAdbRunner(AdbResult(0, b'error: closed\n', b'')),
    )
    with pytest.raises(SnapshotError, match='error: closed'):
        snapshotter.capture()


def test_the_error_preview_is_bounded_to_the_first_bytes() -> None:
    payload = b'error: ' + b'x' * 500
    snapshotter = AdbSnapshotter('serial', runner=ScriptedAdbRunner(AdbResult(0, payload, b'')))
    with pytest.raises(SnapshotError) as raised:
        snapshotter.capture()
    message = str(raised.value)
    assert message.startswith("screencap did not return a PNG: b'error: ")
    assert f'({len(payload)} bytes)' in message


def test_a_non_zero_screencap_exit_raises() -> None:
    snapshotter = AdbSnapshotter(
        'serial',
        geometry=GEOMETRY,
        runner=ScriptedAdbRunner(AdbResult(1, b'', b'device offline')),
    )
    with pytest.raises(SnapshotError, match='device offline'):
        snapshotter.capture()


def test_a_truncated_png_raises_instead_of_becoming_a_corrupt_snapshot() -> None:
    snapshotter = AdbSnapshotter(
        'serial',
        geometry=GEOMETRY,
        runner=ScriptedAdbRunner(AdbResult(0, PNG_MAGIC + b'broken', b'')),
    )
    with pytest.raises(SnapshotError, match='undecodable PNG'):
        snapshotter.capture()


def test_an_os_error_from_the_runner_is_typed() -> None:
    runner = ScriptedAdbRunner(OSError('adb disappeared'))

    with pytest.raises(SnapshotError, match='adb disappeared'):
        AdbSnapshotter('serial', runner=runner).capture_png()


def test_a_timeout_from_the_runner_is_typed() -> None:
    runner = ScriptedAdbRunner(subprocess.TimeoutExpired(cmd='adb', timeout=1.5))

    with pytest.raises(SnapshotError, match='timed out'):
        AdbSnapshotter('serial', runner=runner, timeout=1.5).capture_png()


def test_probe_geometry_uses_read_only_commands_and_the_frame_orientation() -> None:
    runner = ScriptedAdbRunner(
        AdbResult(0, FRAME_PNG, b''),
        AdbResult(0, b'Physical size: 2000x1200\n', b''),
        AdbResult(0, b'Physical density: 240\n', b''),
        AdbResult(0, b'35\n', b''),
    )
    snapshotter = AdbSnapshotter(
        '192.0.2.10:5555',
        runner=runner,
        clock=lambda: CAPTURED_AT,
    )

    snapshot = snapshotter.capture()

    assert [list(call) for call in runner.calls] == [
        SCREENCAP_ARGS,
        ['adb', '-s', '192.0.2.10:5555', 'shell', 'wm', 'size'],
        ['adb', '-s', '192.0.2.10:5555', 'shell', 'wm', 'density'],
        ['adb', '-s', '192.0.2.10:5555', 'shell', 'getprop', 'ro.build.version.sdk'],
    ]
    assert (snapshot.geometry.width, snapshot.geometry.height) == (1200, 2000)
    assert snapshot.geometry.orientation == 'portrait'
    assert snapshot.geometry.density == 240
    assert snapshot.geometry.sdk == 35


def test_probe_geometry_raises_when_a_shell_command_fails() -> None:
    snapshotter = AdbSnapshotter(
        'serial',
        runner=ScriptedAdbRunner(AdbResult(1, b'', b'no device')),
    )
    with pytest.raises(SnapshotError, match='no device'):
        snapshotter.probe_geometry()


def test_explicit_probed_geometry_is_reconciled_with_the_frame() -> None:
    probed = parse_geometry(
        'Physical size: 2000x1200\n',
        'Physical density: 240\n',
        '35\n',
    )
    assert (probed.width, probed.height, probed.orientation) == (2000, 1200, 'landscape')

    snapshot = AdbSnapshotter(
        'serial',
        geometry=probed,
        runner=ScriptedAdbRunner(AdbResult(0, FRAME_PNG, b'')),
    ).capture()

    assert snapshot.geometry == DeviceGeometry.from_frame(1200, 2000, density=240, sdk=35)
    assert snapshot.geometry.orientation == 'portrait'


def test_probe_geometry_raises_on_unparseable_output() -> None:
    runner = ScriptedAdbRunner(
        AdbResult(0, b'Physical size: unknown\n', b''),
        AdbResult(0, b'Physical density: 240\n', b''),
        AdbResult(0, b'35\n', b''),
    )
    snapshotter = AdbSnapshotter('serial', runner=runner)
    with pytest.raises(SnapshotError, match='could not parse wm size'):
        snapshotter.probe_geometry()


def test_ocr_failure_propagates_as_an_ocr_error() -> None:
    class BrokenOcr:
        def read(self, _image: object) -> tuple[TextBox, ...]:
            raise OcrError('model missing')

    snapshotter = AdbSnapshotter(
        'serial',
        geometry=GEOMETRY,
        ocr=BrokenOcr(),
        runner=ScriptedAdbRunner(AdbResult(0, FRAME_PNG, b'')),
    )
    with pytest.raises(OcrError, match='model missing'):
        snapshotter.capture()


def test_empty_serial_and_non_positive_timeout_are_rejected() -> None:
    with pytest.raises(ValueError, match='serial'):
        AdbSnapshotter('  ')
    with pytest.raises(ValueError, match='timeout'):
        AdbSnapshotter('serial', timeout=0)


def test_snapshot_type_is_returned() -> None:
    snapshot = AdbSnapshotter(
        'serial',
        geometry=GEOMETRY,
        runner=ScriptedAdbRunner(AdbResult(0, FRAME_PNG, b'')),
    ).capture()
    assert isinstance(snapshot, Snapshot)
