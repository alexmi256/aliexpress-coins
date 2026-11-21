"""Device geometry and the profile id used to replay a snapshot.

The reference tablet reports `Physical size: 2000x1200` from `wm size` while
`screencap` returns a 1200x2000 portrait frame. Geometry therefore follows the
frame: `with_frame_size()` fixes the dimensions and orientation after decoding,
and the profile id is derived only from that recorded geometry. No module here
stores a pixel constant: callers either probe the device or supply a measured
profile.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from aliexpress_coins.perception.errors import SnapshotError

Orientation = Literal['portrait', 'landscape']

_SIZE_PATTERN = re.compile(r'(?P<kind>Override|Physical) size:\s*(?P<width>\d+)x(?P<height>\d+)')
_DENSITY_PATTERN = re.compile(r'(?P<kind>Override|Physical) density:\s*(?P<density>\d+)')
_INT_PATTERN = re.compile(r'\d+')


def orientation_for(width: int, height: int) -> Orientation:
    """Return the orientation implied by a frame's dimensions."""
    return 'landscape' if width > height else 'portrait'


@dataclass(frozen=True, slots=True)
class DeviceGeometry:
    """The source geometry recorded with every snapshot."""

    width: int
    height: int
    density: int
    sdk: int
    orientation: Orientation

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError('geometry width and height must be positive')
        if self.density <= 0:
            raise ValueError('geometry density must be positive')
        if self.sdk <= 0:
            raise ValueError('geometry sdk must be positive')
        if self.orientation != orientation_for(self.width, self.height):
            raise ValueError(
                f'orientation {self.orientation!r} contradicts a {self.width}x{self.height} frame',
            )

    @classmethod
    def from_frame(cls, width: int, height: int, *, density: int, sdk: int) -> DeviceGeometry:
        """Build geometry for the frame that was actually captured."""
        return cls(width, height, density, sdk, orientation_for(width, height))

    def with_frame_size(self, width: int, height: int) -> DeviceGeometry:
        """Keep the measured density/SDK, but take dimensions from the frame."""
        return DeviceGeometry.from_frame(width, height, density=self.density, sdk=self.sdk)

    @property
    def profile_id(self) -> str:
        """A stable key for calibration data, derived from the recorded geometry."""
        canonical = f'{self.width}x{self.height}@{self.density}dpi-sdk{self.sdk}-{self.orientation}'
        return hashlib.sha256(canonical.encode('ascii')).hexdigest()[:16]

    def describe(self) -> str:
        """A compact human-readable form for logs and doctor output."""
        return (
            f'{self.width}x{self.height} @ {self.density} dpi, '
            f'sdk {self.sdk}, {self.orientation}, profile {self.profile_id}'
        )


def _preferred(output: str, pattern: re.Pattern[str]) -> re.Match[str] | None:
    matches = list(pattern.finditer(output))
    if not matches:
        return None
    for match in matches:
        if match.group('kind') == 'Override':
            return match
    return matches[-1]


def parse_wm_size(output: str) -> tuple[int, int]:
    """Parse `wm size`, preferring an override over the physical display."""
    match = _preferred(output, _SIZE_PATTERN)
    if match is None:
        raise SnapshotError(f'could not parse wm size output: {output.strip()!r}')
    width, height = int(match.group('width')), int(match.group('height'))
    if width <= 0 or height <= 0:
        raise SnapshotError(f'wm size reported a non-positive dimension: {output.strip()!r}')
    return width, height


def parse_wm_density(output: str) -> int:
    """Parse `wm density`, preferring an override over the physical density."""
    match = _preferred(output, _DENSITY_PATTERN)
    if match is None:
        raise SnapshotError(f'could not parse wm density output: {output.strip()!r}')
    density = int(match.group('density'))
    if density <= 0:
        raise SnapshotError(f'wm density reported a non-positive value: {output.strip()!r}')
    return density


def parse_sdk(output: str) -> int:
    """Read the SDK level from `getprop ro.build.version.sdk`."""
    match = _INT_PATTERN.search(output)
    if match is None:
        raise SnapshotError(f'could not parse the SDK level: {output.strip()!r}')
    sdk = int(match.group(0))
    if sdk <= 0:
        raise SnapshotError(f'the SDK level must be positive: {output.strip()!r}')
    return sdk


def parse_geometry(size_output: str, density_output: str, sdk_output: str) -> DeviceGeometry:
    """Combine the three read-only geometry probes into one profile."""
    width, height = parse_wm_size(size_output)
    return DeviceGeometry.from_frame(
        width,
        height,
        density=parse_wm_density(density_output),
        sdk=parse_sdk(sdk_output),
    )
