#!/usr/bin/env python3
"""Cut a reviewed glyph-only close control out of a full frame (issue #15).

A template is only portable if it records the width of the frame it came from.
This tool takes a reviewed full frame, an inclusive-left/exclusive-right box and
a family id, writes the crop to ``assets/templates/<family>.png`` and prints the
corpus row (and the frame width to record as ``source_width``). It refuses a
uniform crop, because NCC cannot match one and the matcher returns ``None``.

Run it from the repository root after ``just setup``:

    .venv/bin/python tools/cut_template.py FRAME FAMILY --box LEFT TOP RIGHT BOTTOM

Example, recutting the launch modal's close control from the reviewed fixture:

    .venv/bin/python tools/cut_template.py \\
        tests/data/popups/launch_welcome_gifts.png launch_welcome_gifts \\
        --box 532 1600 666 1733 --force
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageStat, UnidentifiedImageError

ROOT = Path(__file__).resolve().parents[1]


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError(f'must be positive, got {number}')
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Cut one popup close-control template from a full frame.',
        epilog='The box is (left, top, right, bottom), with right/bottom exclusive, exactly like PIL crop().',
    )
    parser.add_argument('frame', type=Path, help='reviewed full-frame PNG')
    parser.add_argument('family', help='popup family id, used as the default filename')
    parser.add_argument(
        '--box',
        nargs=4,
        type=int,
        required=True,
        metavar=('LEFT', 'TOP', 'RIGHT', 'BOTTOM'),
        help='crop box in frame pixels',
    )
    parser.add_argument(
        '--output',
        type=Path,
        help='destination crop (default: assets/templates/<family>.png)',
    )
    parser.add_argument(
        '--source-width',
        type=_positive_int,
        help='width of the frame the crop came from (default: the frame width)',
    )
    parser.add_argument('--force', action='store_true', help='overwrite an existing crop')
    return parser


def main() -> int:
    args = _parser().parse_args()
    frame_path = args.frame
    if not frame_path.is_file():
        raise SystemExit(f'frame not found: {frame_path}')

    try:
        with Image.open(frame_path) as opened:
            opened.load()
            frame = opened.copy()
    except (OSError, SyntaxError, ValueError, UnidentifiedImageError) as exc:
        raise SystemExit(f'could not read frame {frame_path}: {exc}') from exc

    left, top, right, bottom = (int(value) for value in args.box)
    if not (0 <= left < right <= frame.width and 0 <= top < bottom <= frame.height):
        raise SystemExit(
            f'box {(left, top, right, bottom)} is outside the {frame.width}x{frame.height} frame '
            'or has no area',
        )

    output = args.output if args.output is not None else ROOT / 'assets' / 'templates' / f'{args.family}.png'
    if output.exists() and not args.force:
        raise SystemExit(f'{output} already exists; pass --force to replace it')

    crop = frame.crop((left, top, right, bottom))
    deviation = ImageStat.Stat(crop.convert('L')).stddev[0]
    if deviation == 0.0:
        raise SystemExit(
            'the crop is uniform, so TM_CCOEFF_NORMED is undefined and it would never match; '
            'include part of the glyph and its surrounding control',
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    crop.save(output)
    source_width = args.source_width if args.source_width is not None else frame.width

    try:
        relative = output.resolve().relative_to(ROOT)
    except ValueError:
        relative = output
    print(f'wrote {relative} ({crop.width}x{crop.height}, source frame {frame.width} px)')
    print('corpus rule:')
    print('  close:')
    print(f'    template: {relative.as_posix()}')
    print(f'    source_width: {source_width}')
    print()
    print('Review the crop before committing: artwork only, no account data.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
