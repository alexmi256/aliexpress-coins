#!/usr/bin/env python3
"""Capture the recorded frames a fixture set replays (issue #36).

The suite never touches the tablet. When a flow needs a new fixture, this tool
captures it once on the real device, prints a manifest describing the geometry
it was taken at, and writes the PNGs under ``tests/data/<set>/`` unchanged. The
suite then replays those files through ``tests/support/frames.py``.

No dependency is added for capture: ``AdbSnapshotter`` runs
``adb exec-out screencap -p`` through the shared ``SubprocessAdbRunner``, so the
tool uses the production geometry probe and PNG validation instead of a second
capture path.

The device address is configuration, not a constant: pass ``--serial`` or set
``AECOINS_DEVICE``. Both the IP and the adb port move when the tablet
reconnects, so a baked-in default can capture from the wrong host.

Usage::

    # Capture the ``states`` set; prompts before each frame so the operator can
    # put the device on the right screen. Never taps, orders, redeems or shares.
    .venv/bin/python -m aliexpress_coins.tools.capture_frames --serial "$AECOINS_DEVICE" --set states

    # Re-print the manifest for an existing set without touching the device.
    .venv/bin/python -m aliexpress_coins.tools.capture_frames --set states --manifest-only

    # Discover the set names and the file each one needs.
    .venv/bin/python -m aliexpress_coins.tools.capture_frames --list
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from aliexpress_coins.perception.capture import AdbSnapshotter
from aliexpress_coins.perception.errors import SnapshotError
from aliexpress_coins.transport import SubprocessAdbRunner

ROOT = Path(__file__).resolve().parents[3]

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
    """Fully decode the PNG, not just its header.

    A truncated screencap can carry a valid IHDR, so reading ``image.size``
    alone would accept bytes that later fail to open. ``load()`` forces the
    whole image through the decoder before anything touches the fixture.
    """
    try:
        with Image.open(BytesIO(data)) as image:
            image.load()
            return image.width, image.height
    except (OSError, SyntaxError, ValueError, UnidentifiedImageError) as exc:
        raise SnapshotError(
            f'the captured frame is not a fully decodable PNG ({type(exc).__name__}: {exc})',
        ) from exc


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


def _atomic_write(destination: Path, data: bytes) -> None:
    """Write via a temporary file and rename, so a failed write keeps the old fixture."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f'.{destination.name}.', suffix='.tmp', dir=destination.parent)
    try:
        with os.fdopen(handle, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def capture_one(
    snapshotter: AdbSnapshotter,
    name: str,
    destination: Path,
    expected: tuple[int, int],
) -> Frame:
    """Capture, fully validate and only then replace ``destination``."""
    try:
        data = snapshotter.capture_png()
    except SnapshotError as exc:
        raise SystemExit(f'{name}: {exc}') from exc
    try:
        width, height = _png_size(data)
    except SnapshotError as exc:
        raise SystemExit(f'{name}: {exc}') from exc
    if (width, height) != expected:
        raise SystemExit(
            f'{name}: captured {width}x{height}, expected {expected[0]}x{expected[1]}; '
            'the frame must be captured at the device geometry, not downscaled',
        )
    _atomic_write(destination, data)
    return _frame_from_bytes(name, destination, data)


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
    parser.add_argument(
        '--serial',
        help='adb serial; defaults to AECOINS_DEVICE because the address is configuration, not a constant',
    )
    parser.add_argument('--set', dest='set_name', choices=sorted(SETS), help='the fixture set to capture')
    parser.add_argument('--out', type=Path, help='destination directory (default: tests/data/<set>)')
    parser.add_argument('--list', action='store_true', help='list set names and their frames')
    parser.add_argument('--manifest-only', action='store_true', help='print the manifest without touching the device')
    parser.add_argument('--manifest-out', type=Path, help='also write the manifest as JSON')
    parser.add_argument(
        '--no-prompt', action='store_true', help='capture immediately; the screen must already be staged'
    )
    parser.add_argument('--density', type=int, default=240, help='density for --manifest-only (default: 240)')
    parser.add_argument('--sdk', type=int, default=35, help='SDK for --manifest-only (default: 35)')
    args = parser.parse_args(argv)

    if args.list:
        for set_name, frame_names in SETS.items():
            print(f'{set_name}: {len(frame_names)} frames')
            for frame_name in frame_names:
                print(f'  {frame_name}.png')
        return 0
    if not args.set_name:
        parser.error('--set is required unless --list is given')

    names = SETS[args.set_name]
    destination = args.out if args.out is not None else ROOT / 'tests' / 'data' / args.set_name

    if args.manifest_only:
        existing = [
            _frame_from_path(name, destination / f'{name}.png')
            for name in names
            if (destination / f'{name}.png').is_file()
        ]
        if not existing:
            raise SystemExit(f'no frames for {args.set_name!r} in {destination}')
        width, height = existing[0].width, existing[0].height
        payload = manifest(
            name=args.set_name,
            serial=None,
            width=width,
            height=height,
            density=args.density,
            sdk=args.sdk,
            frames=existing,
            source='existing',
        )
        _print_manifest(payload)
        if args.manifest_out is not None:
            args.manifest_out.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        return 0

    serial = args.serial or os.environ.get('AECOINS_DEVICE')
    if not serial:
        parser.error('--serial or AECOINS_DEVICE is required; the device address is configuration')

    snapshotter = AdbSnapshotter(serial, runner=SubprocessAdbRunner())
    try:
        geometry = snapshotter.probe_geometry()
    except SnapshotError as exc:
        raise SystemExit(f'could not probe {serial}: {exc}') from exc
    width, height = sorted((geometry.width, geometry.height))

    destination.mkdir(parents=True, exist_ok=True)
    captured: list[Frame] = []
    for index, name in enumerate(names, start=1):
        if not args.no_prompt:
            _prompt(index, len(names), name)
        captured.append(capture_one(snapshotter, name, destination / f'{name}.png', (width, height)))

    payload = manifest(
        name=args.set_name,
        serial=serial,
        width=width,
        height=height,
        density=geometry.density,
        sdk=geometry.sdk,
        frames=captured,
        source='capture',
    )
    _print_manifest(payload)
    if args.manifest_out is not None:
        args.manifest_out.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
