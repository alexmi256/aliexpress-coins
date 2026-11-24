"""The capture recipe validates before it replaces a reviewed fixture (#36)."""

from __future__ import annotations

import json
import os
import runpy
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from aliexpress_coins.perception.capture import PNG_MAGIC, AdbSnapshotter
from aliexpress_coins.perception.errors import SnapshotError
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.tools import capture_frames
from aliexpress_coins.tools.capture_frames import capture_one, main
from aliexpress_coins.transport import AdbResult
from tests.support.adb import ScriptedAdbRunner


def _png(size: tuple[int, int] = (4, 6)) -> bytes:
    buffer = BytesIO()
    Image.new('RGB', size, 'white').save(buffer, 'PNG')
    return buffer.getvalue()


def _snapshotter(data: bytes) -> AdbSnapshotter:
    return AdbSnapshotter('tablet', runner=ScriptedAdbRunner(AdbResult(0, data, b'')))


def test_a_valid_capture_replaces_the_fixture_atomically(tmp_path: Path) -> None:
    destination = tmp_path / 'coins.png'
    destination.write_bytes(b'reviewed fixture')
    data = _png((4, 6))

    frame = capture_one(_snapshotter(data), 'coins', destination, (4, 6))

    assert destination.read_bytes() == data
    assert (frame.width, frame.height) == (4, 6)
    assert frame.sha256 != ''
    assert not list(tmp_path.glob('.*.tmp'))


def test_a_truncated_capture_leaves_the_fixture_untouched(tmp_path: Path) -> None:
    destination = tmp_path / 'coins.png'
    original = b'reviewed fixture'
    destination.write_bytes(original)

    with pytest.raises(SystemExit, match='fully decodable PNG'):
        capture_one(_snapshotter(PNG_MAGIC + b'broken'), 'coins', destination, (1200, 2000))

    assert destination.read_bytes() == original
    assert not list(tmp_path.glob('.*.tmp'))


def test_a_wrong_size_capture_leaves_the_fixture_untouched(tmp_path: Path) -> None:
    destination = tmp_path / 'coins.png'
    original = b'reviewed fixture'
    destination.write_bytes(original)

    with pytest.raises(SystemExit, match='expected 1200x2000'):
        capture_one(_snapshotter(_png((10, 20))), 'coins', destination, (1200, 2000))

    assert destination.read_bytes() == original
    assert not list(tmp_path.glob('.*.tmp'))


def test_a_non_png_capture_leaves_the_fixture_untouched(tmp_path: Path) -> None:
    destination = tmp_path / 'coins.png'
    original = b'reviewed fixture'
    destination.write_bytes(original)

    with pytest.raises(SystemExit, match='did not return a PNG'):
        capture_one(_snapshotter(b'error: closed'), 'coins', destination, (1200, 2000))

    assert destination.read_bytes() == original


def test_capture_requires_an_address_from_the_argument_or_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv('AECOINS_DEVICE', raising=False)

    with pytest.raises(SystemExit) as raised:
        main(['--set', 'states', '--no-prompt'])

    assert raised.value.code == 2


def test_list_does_not_need_a_device() -> None:
    assert main(['--list']) == 0


def test_set_is_required_without_list() -> None:
    with pytest.raises(SystemExit) as raised:
        main([])

    assert raised.value.code == 2


def test_a_failed_replace_keeps_the_fixture_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / 'coins.png'
    original = b'reviewed fixture'
    destination.write_bytes(original)

    def broken_replace(*_args: object) -> None:
        raise OSError('disk full')

    monkeypatch.setattr(os, 'replace', broken_replace)
    with pytest.raises(OSError, match='disk full'):
        capture_one(_snapshotter(_png((4, 6))), 'coins', destination, (4, 6))

    assert destination.read_bytes() == original
    assert not list(tmp_path.glob('.*.tmp'))


def test_manifest_only_reads_existing_frames_without_a_device(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / 'coins.png').write_bytes(_png((4, 6)))
    (tmp_path / 'account.png').write_bytes(_png((4, 6)))
    manifest_path = tmp_path / 'manifest.json'

    assert (
        main(
            [
                '--set',
                'states',
                '--out',
                str(tmp_path),
                '--manifest-only',
                '--manifest-out',
                str(manifest_path),
            ],
        )
        == 0
    )

    printed = json.loads(capsys.readouterr().out)
    assert printed['source'] == 'existing'
    assert printed['width'] == 4
    assert [frame['name'] for frame in printed['frames']] == ['coins', 'account']
    assert json.loads(manifest_path.read_text(encoding='utf-8')) == printed

    assert main(['--set', 'states', '--out', str(tmp_path), '--manifest-only']) == 0
    assert json.loads(capsys.readouterr().out)['source'] == 'existing'


def test_manifest_only_with_no_frames_fails(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match='no frames'):
        main(['--set', 'states', '--out', str(tmp_path), '--manifest-only'])


class _FakeSnapshotter:
    """A capture client that answers from memory, so main() never touches adb."""

    def __init__(self, serial: str, *, runner: Any) -> None:
        self.serial = serial
        self.runner = runner

    def probe_geometry(self) -> DeviceGeometry:
        return DeviceGeometry.from_frame(4, 6, density=240, sdk=35)

    def capture_png(self) -> bytes:
        return _png((4, 6))


class _BrokenProbe(_FakeSnapshotter):
    def probe_geometry(self) -> DeviceGeometry:
        raise SnapshotError('no device')


def test_capture_loop_uses_the_probed_geometry_and_writes_the_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(capture_frames, 'AdbSnapshotter', _FakeSnapshotter)
    monkeypatch.setattr(capture_frames, 'SubprocessAdbRunner', lambda: object())
    monkeypatch.setattr(capture_frames, '_prompt', lambda *_args: None)
    manifest_path = tmp_path / 'manifest.json'

    assert (
        main(
            [
                '--set',
                'states',
                '--out',
                str(tmp_path),
                '--serial',
                'tablet',
                '--manifest-out',
                str(manifest_path),
            ],
        )
        == 0
    )

    printed = json.loads(capsys.readouterr().out)
    assert printed['source'] == 'capture'
    assert printed['serial'] == 'tablet'
    assert len(printed['frames']) == 12
    assert len(list(tmp_path.glob('*.png'))) == 12
    assert json.loads(manifest_path.read_text(encoding='utf-8')) == printed

    # The non-prompting path and the manifest-less branch are separate arms.
    assert main(['--set', 'states', '--out', str(tmp_path / 'second'), '--serial', 'tablet', '--no-prompt']) == 0
    assert len(list((tmp_path / 'second').glob('*.png'))) == 12
    capsys.readouterr()


def test_a_probe_failure_is_a_clear_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(capture_frames, 'AdbSnapshotter', _BrokenProbe)
    monkeypatch.setattr(capture_frames, 'SubprocessAdbRunner', lambda: object())

    with pytest.raises(SystemExit, match='could not probe tablet'):
        main(['--set', 'states', '--out', str(tmp_path), '--serial', 'tablet', '--no-prompt'])


def test_prompt_refuses_a_non_terminal() -> None:
    with pytest.raises(SystemExit, match='needs a terminal'):
        capture_frames._prompt(1, 2, 'coins')


def test_prompt_reads_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr('builtins.input', lambda _prompt: '')

    capture_frames._prompt(1, 2, 'coins')


def test_the_module_entry_point_lists_sets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, 'argv', ['capture_frames', '--list'])

    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(capture_frames.__file__), run_name='__main__')

    assert raised.value.code == 0
