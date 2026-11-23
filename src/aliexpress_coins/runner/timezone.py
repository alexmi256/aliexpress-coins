"""The configured zone must be the device's own zone (issue #79).

``AECOINS_TIMEZONE`` drives both the nightly trigger and the daily store's day
boundary. When it runs ahead of the device's ``persist.sys.timezone``, a night
fires before the device's day has rolled: every task is already claimed, the
fresh-day path is never exercised, and the run reports success while earning
nothing. That is silent and repeats every night, so the comparison is one
shared value instead of prose in three places.

The device read lives behind the same adb transport seam as the rest of the
collector, which keeps it testable without a phone.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import tzinfo
from typing import Literal
from zoneinfo import ZoneInfo

from aliexpress_coins.runner.config import timezone_from_environment
from aliexpress_coins.transport import AdbRunner, SubprocessAdbRunner

__all__ = [
    'DEVICE_TIMEZONE_PROPERTY',
    'DeviceTimezoneReader',
    'TimezoneCheck',
    'TimezoneStatus',
    'compare_timezone',
    'configured_timezone_name',
    'read_device_timezone',
    'timezone_check_from_environment',
]

DEVICE_TIMEZONE_PROPERTY = 'persist.sys.timezone'
"""The Android property that holds the device's IANA zone name."""

TimezoneStatus = Literal['match', 'mismatch', 'unreadable']

DeviceTimezoneReader = Callable[[str], str | None]
"""Read a serial's device timezone, or ``None`` when it cannot be read."""


@dataclass(frozen=True, slots=True)
class TimezoneCheck:
    """The configured zone and the device's own zone, named side by side."""

    configured: str
    device: str | None

    @property
    def status(self) -> TimezoneStatus:
        """Whether the two names agree or the device side is unknown."""
        if self.device is None:
            return 'unreadable'
        return 'match' if self.configured == self.device else 'mismatch'

    @property
    def matches(self) -> bool:
        """True only when both names are known and equal."""
        return self.status == 'match'

    def message(self) -> str:
        """One sentence naming both values, safe for a doctor line or a log."""
        if self.device is None:
            return f'configured timezone {self.configured}; the device timezone could not be read'
        if self.matches:
            return f'configured timezone {self.configured}; device timezone {self.device}'
        return (
            f'configured timezone {self.configured}; device timezone {self.device}; '
            'the schedule would fire before the device day rolls'
        )


def configured_timezone_name(zone: tzinfo) -> str:
    """Return the IANA key of an injected ``tzinfo``, or its display name."""
    if isinstance(zone, ZoneInfo):
        return zone.key
    name = zone.tzname(None)
    return name or str(zone)


def read_device_timezone(
    serial: str,
    *,
    runner: AdbRunner | None = None,
    timeout: float = 10.0,
) -> str | None:
    """Read ``persist.sys.timezone`` from one serial, without raising.

    The caller is a startup guard, not the run: an offline device must produce
    a loud refusal or warning, not an exception from a probe. A non-zero exit,
    an empty property or a spawn failure therefore reads as ``None``.
    """
    command = ('adb', '-s', serial, 'shell', 'getprop', DEVICE_TIMEZONE_PROPERTY)
    run = runner if runner is not None else SubprocessAdbRunner()
    try:
        result = run(command, timeout)
    except OSError, subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.decode('utf-8', errors='replace').strip()
    return value or None


def compare_timezone(configured: str, device: str | None) -> TimezoneCheck:
    """Compare two zone names; the caller decides whether that refuses a run."""
    return TimezoneCheck(configured=configured, device=device)


def timezone_check_from_environment(
    environment: Mapping[str, str],
    device: str | None,
    *,
    reader: DeviceTimezoneReader | None = None,
) -> TimezoneCheck | None:
    """Build the check for a configured device, or ``None`` when there is none."""
    if not device:
        return None
    configured = configured_timezone_name(timezone_from_environment(environment))
    read = reader if reader is not None else read_device_timezone
    return compare_timezone(configured, read(device))
