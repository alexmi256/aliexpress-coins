#!/usr/bin/env python3
"""Capture the recorded frames a fixture set replays (issue #36).

The suite never touches the tablet. When a flow needs a new fixture, this tool
captures it once on the real device, prints a manifest describing the geometry
it was taken at, and writes the PNGs under ``tests/data/<set>/`` unchanged. The
suite then replays those files through ``tests/support/frames.py``.

No dependency is added for capture: ``adb exec-out screencap -p`` returns the
PNG bytes, and the tool uses the same ``SubprocessAdbRunner`` transport the
package uses.

Usage::

    # Capture the ``states`` set; prompts before each frame so the operator can
    # put the device on the right screen. Never taps, orders, redeems or shares.
    .venv/bin/python tools/capture_frames.py --serial 192.168.8.65:5555 --set states

    # Re-print the manifest for an existing set without touching the device.
    .venv/bin/python tools/capture_frames.py --set states --manifest-only

    # Discover the set names and the file each one needs.
    .venv/bin/python tools/capture_frames.py --list
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from PIL import Image

from aliexpress_coins.perception.capture import PNG_MAGIC
from aliexpress_coins.perception.geometry import parse_geometry
from aliexpress_coins.transport import AdbResult, SubprocessAdbRunner

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEVICE = '192.168.8.65:5555'

SETS: dict[str, tuple[str, ...]] = {
    'states': (
        'coins',
        'account',
        'launch_modal',
        'skeleton',
        'blank_webview',
        'boost',
        'quiz',
        'surprise_grid',
        'popup_over_page',
        'ambiguous_feed',
    ),
    'flows': (
        'surprise_items_entry_row',
        'surprise_items_round_grid',
        'surprise_items_item_detail',
        'surprise_items_progress_1of3',
        'surprise_items_progress_2of3',
        'surprise_items_round_complete',
        'surprise_items_mission_completed',
        'surprise_items_row_gone',
        'notifications_row_completed',
        'notifications_os_settings',
        'notifications_after_grant',
        'coins_quiz_entry_row',
        'coins_quiz_question',
        'coins_quiz_exit_coin_page',
    ),
    'popups': (
        'launch_welcome_gifts',
        'promo_coupon_unlocked',
        'promo_bundle_deals_badge',
        'coins_boost_sheet',
        'push_permission_prompt',
        'push_permission_prompt_sheet',
        'system_notification_permission',
    ),
}


@dataclass(frozen=True, slots=True)
class Frame:
    """One captured or existing frame in a set."""

    name: str
    path: Path
    width: int
    height: int
    size: int
    sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            'name': self.name,
            'file': self.path.name,
            'width': self.width,
            'height': self.height,
            'bytes': self.size,
            'sha256': self.sha256,
        }


def _png_size(data: bytes) -> tuple[int, int]:
    with Image.open(BytesIO(data)) as image:
        return image.width, image.height


def _frame_from_bytes(name: str, path: Path, data: bytes) -> Frame:
    width, height = _png_size(data)
    return Frame(
        name=name,
        path=path,
        width=width,
        height=height,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def _frame_from_path(name: str, path: Path) -> Frame:
    return _frame_from_bytes(name, path, path.read_bytes())


def _shell_text(runner: SubprocessAdbRunner, serial: str, *command: str) -> str:
    args = ('adb', '-s', serial, 'shell', *command)
    result: AdbResult = runner(args, 30.0)
    if result.returncode != 0:
        raise SystemExit(f'adb {" ".join(command)} exited {result.returncode}: {result.stderr_text.strip()}')
    return result.stdout_text


def probe_geometry(serial: str, runner: SubprocessAdbRunner) -> tuple[int, int, int, int]:
    """Read the real panel geometry before the first capture.

    The dimensions are returned portrait-normalised, matching what a screencap
    writes. A frame that arrives smaller than these dimensions is a downscaled
    fixture and is refused: #42 keys calibration by this geometry.
    """
    geometry = parse_geometry(
        _shell_text(runner, serial, 'wm', 'size'),
        _shell_text(runner, serial, 'wm', 'density'),
        _shell_text(runner, serial, 'getprop', 'ro.build.version.sdk'),
    )
    width, height = sorted((geometry.width, geometry.height))
    return width, height, geometry.density, geometry.sdk


def capture_one(serial: str, runner: SubprocessAdbRunner, name: str, destination: Path) -> Frame:
    result = runner(('adb', '-s', serial, 'exec-out', 'screencap', '-p'), 60.0)
    if result.returncode != 0:
        raise SystemExit(f'screencap exited {result.returncode}: {result.stderr_text.strip()}')
    if not result.stdout.startswith(PNG_MAGIC):
        raise SystemExit(f'screencap for {name!r} did not return a PNG')
    destination.write_bytes(result.stdout)
    return _frame_from_bytes(name, destination, result.stdout)


def manifest(
    *,
    name: str,
    serial: str | None,
    width: int,
    height: int,
    density: int,
    sdk: int,
    frames: list[Frame],
    source: str,
) -> dict[str, object]:
    return {
        'version': 1,
        'set': name,
        'source': source,
        'serial': serial,
        'captured_at': datetime.now(UTC).isoformat() if source == 'capture' else None,
        'width': width,
        'height': height,
        'density': density,
        'sdk': sdk,
        'frames': [frame.as_dict() for frame in frames],
    }


def _print_manifest(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _prompt(index: int, total: int, name: str) -> None:
    if not sys.stdin.isatty():
        raise SystemExit('capture needs a terminal to prompt; pass --no-prompt for an already-staged screen')
    input(f'[{index}/{total}] Put the device on {name!r}, then press Enter: ')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Capture recorded frames for one fixture set.')
    parser.add_argument('--serial', default=DEFAULT_DEVICE, help=f'adb serial (default: {DEFAULT_DEVICE})')
    parser.add_argument('--set', dest='set_name', choices=sorted(SETS), help='the fixture set to capture')
    parser.add_argument('--out', type=Path, help='destination directory (default: tests/data/<set>)')
    parser.add_argument('--list', action='store_true', help='list set names and their frames')
    parser.add_argument('--manifest-only', action='store_true', help='print the manifest without touching the device')
    parser.add_argument('--manifest-out', type=Path, help='also write the manifest as JSON')
    parser.add_argument('--no-prompt', action='store_true', help='capture immediately; the screen must already be staged')
    parser.add_argument('--density', type=int, default=240, help='density for --manifest-only (default: 240)')
    parser.add_argument('--sdk', type=int, default=35, help='SDK for --manifest-only (default: 35)')
    args = parser.parse_args(argv)

    if args.list:
        for set_name, frames in SETS.items():
            print(f'{set_name}: {len(frames)} frames')
            for frame in frames:
                print(f'  {frame}.png')
        return 0
    if not args.set_name:
        parser.error('--set is required unless --list is given')

    names = SETS[args.set_name]
    destination = args.out if args.out is not None else ROOT / 'tests' / 'data' / args.set_name
    destination.mkdir(parents=True, exist_ok=True)

    if args.manifest_only:
        frames = [
            _frame_from_path(name, destination / f'{name}.png')
            for name in names
            if (destination / f'{name}.png').is_file()
        ]
        if not frames:
            raise SystemExit(f'no frames for {args.set_name!r} in {destination}')
        width, height = frames[0].width, frames[0].height
        payload = manifest(
            name=args.set_name,
            serial=None,
            width=width,
            height=height,
            density=args.density,
            sdk=args.sdk,
            frames=frames,
            source='existing',
        )
        _print_manifest(payload)
        if args.manifest_out is not None:
            args.manifest_out.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        return 0

    runner = SubprocessAdbRunner()
    width, height, density, sdk = probe_geometry(args.serial, runner)
    captured: list[Frame] = []
    for index, name in enumerate(names, start=1):
        if not args.no_prompt:
            _prompt(index, len(names), name)
        frame = capture_one(args.serial, runner, name, destination / f'{name}.png')
        if (frame.width, frame.height) != (width, height):
            raise SystemExit(
                f'{name}: captured {frame.width}x{frame.height}, expected {width}x{height}; '
                'the frame must be captured at the device geometry, not downscaled',
            )
        captured.append(frame)

    payload = manifest(
        name=args.set_name,
        serial=args.serial,
        width=width,
        height=height,
        density=density,
        sdk=sdk,
        frames=captured,
        source='capture',
    )
    _print_manifest(payload)
    if args.manifest_out is not None:
        args.manifest_out.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
