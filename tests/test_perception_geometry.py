"""Geometry parsing and profile ids (issue #12)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest

from aliexpress_coins.perception.errors import SnapshotError
from aliexpress_coins.perception.geometry import (
    DeviceGeometry,
    orientation_for,
    parse_geometry,
    parse_sdk,
    parse_wm_density,
    parse_wm_size,
)


def test_orientation_follows_the_frame() -> None:
    assert orientation_for(1200, 2000) == 'portrait'
    assert orientation_for(2000, 1200) == 'landscape'
    assert orientation_for(100, 100) == 'portrait'


def test_geometry_from_frame_derives_orientation_and_profile() -> None:
    geometry = DeviceGeometry.from_frame(1200, 2000, density=240, sdk=35)
    assert geometry.orientation == 'portrait'
    assert len(geometry.profile_id) == 16
    assert geometry.profile_id == DeviceGeometry.from_frame(1200, 2000, density=240, sdk=35).profile_id
    assert '1200x2000' in geometry.describe()


def test_profile_changes_with_geometry() -> None:
    base = DeviceGeometry.from_frame(1200, 2000, density=240, sdk=35)
    changed_size = DeviceGeometry.from_frame(1080, 1920, density=240, sdk=35)
    changed_density = DeviceGeometry.from_frame(1200, 2000, density=320, sdk=35)
    changed_sdk = DeviceGeometry.from_frame(1200, 2000, density=240, sdk=36)
    assert len({base.profile_id, changed_size.profile_id, changed_density.profile_id, changed_sdk.profile_id}) == 4


def test_with_frame_size_fixes_the_tablet_orientation_quirk() -> None:
    probed = DeviceGeometry.from_frame(2000, 1200, density=240, sdk=35)
    portrait = probed.with_frame_size(1200, 2000)
    assert (portrait.width, portrait.height, portrait.orientation) == (1200, 2000, 'portrait')
    assert (portrait.density, portrait.sdk) == (240, 35)
    assert portrait.profile_id != probed.profile_id


@pytest.mark.parametrize(
    ('size', 'density', 'sdk'),
    [
        ('Physical size: 2000x1200\n', 'Physical density: 240\n', '35\n'),
        (
            'Physical size: 2000x1200\nOverride size: 1080x1920\n',
            'Physical density: 240\nOverride density: 320\n',
            '35\n',
        ),
    ],
)
def test_parse_geometry_prefers_an_override(size: str, density: str, sdk: str) -> None:
    geometry = parse_geometry(size, density, sdk)
    if 'Override size' in size:
        assert (geometry.width, geometry.height) == (1080, 1920)
        assert geometry.density == 320
    else:
        assert (geometry.width, geometry.height) == (2000, 1200)
        assert geometry.density == 240
    assert geometry.sdk == 35


def test_parse_wm_size_uses_the_last_physical_line() -> None:
    assert parse_wm_size('Physical size: 1000x700\nPhysical size: 1200x2000\n') == (1200, 2000)


def test_parse_density_and_sdk_accept_plain_output() -> None:
    assert parse_wm_density('Physical density: 240\n') == 240
    assert parse_sdk('ro.build.version.sdk: 35\n') == 35


@pytest.mark.parametrize(
    ('function', 'output'),
    [
        (parse_wm_size, 'Physical size: unknown\n'),
        (parse_wm_density, 'Unknown density\n'),
        (parse_sdk, 'not-a-number\n'),
        (parse_wm_size, 'Physical size: 0x2000\n'),
        (parse_wm_density, 'Physical density: 0\n'),
        (parse_sdk, '0\n'),
    ],
)
def test_parsers_raise_a_typed_error_on_bad_output(
    function: Callable[[str], object],
    output: str,
) -> None:
    with pytest.raises(SnapshotError):
        function(output)


@pytest.mark.parametrize(
    'kwargs',
    [
        {'width': 0, 'height': 2000, 'density': 240, 'sdk': 35, 'orientation': 'portrait'},
        {'width': 1200, 'height': 2000, 'density': 0, 'sdk': 35, 'orientation': 'portrait'},
        {'width': 1200, 'height': 2000, 'density': 240, 'sdk': 0, 'orientation': 'portrait'},
        {'width': 2000, 'height': 1200, 'density': 240, 'sdk': 35, 'orientation': 'portrait'},
    ],
)
def test_device_geometry_rejects_invalid_values(kwargs: dict[str, int | str]) -> None:
    with pytest.raises(ValueError, match=r'positive|contradicts'):
        cast(Any, DeviceGeometry)(**kwargs)
