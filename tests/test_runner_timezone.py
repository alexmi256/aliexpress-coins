"""The configured zone against the device's own zone (issue #79)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import UTC, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

import pytest
import tzlocal

from aliexpress_coins.runner.timezone import (
    compare_timezone,
    configured_timezone_name,
    read_device_timezone,
    timezone_check_from_environment,
)
from aliexpress_coins.transport import AdbResult
from tests.support.adb import ScriptedAdbRunner, fail, ok


def test_read_device_timezone_uses_the_device_property() -> None:
    runner = ScriptedAdbRunner(ok('America/Toronto\n'))

    assert read_device_timezone('192.0.2.10:5555', runner=runner) == 'America/Toronto'
    assert runner.calls == [
        ('adb', '-s', '192.0.2.10:5555', 'shell', 'getprop', 'persist.sys.timezone'),
    ]
    assert runner.timeouts == [10.0]


def test_read_device_timezone_treats_an_empty_property_as_unknown() -> None:
    runner = ScriptedAdbRunner(ok('  \n'))

    assert read_device_timezone('serial', runner=runner) is None


def test_read_device_timezone_connects_a_network_serial_once_then_retries() -> None:
    runner = ScriptedAdbRunner(
        fail('not found'),
        ok('connected to 192.0.2.10:5555\n'),
        ok('America/Toronto\n'),
    )

    assert read_device_timezone('192.0.2.10:5555', runner=runner) == 'America/Toronto'
    assert runner.calls == [
        ('adb', '-s', '192.0.2.10:5555', 'shell', 'getprop', 'persist.sys.timezone'),
        ('adb', 'connect', '192.0.2.10:5555'),
        ('adb', '-s', '192.0.2.10:5555', 'shell', 'getprop', 'persist.sys.timezone'),
    ]


def test_read_device_timezone_does_not_retry_when_the_connect_fails() -> None:
    runner = ScriptedAdbRunner(fail('not found'), fail('failed to connect'))

    assert read_device_timezone('192.0.2.10:5555', runner=runner) is None
    assert len(runner.calls) == 2


def test_read_device_timezone_treats_a_failed_probe_as_unknown() -> None:
    runner = ScriptedAdbRunner(fail('offline'))

    assert read_device_timezone('serial', runner=runner) is None


def test_read_device_timezone_survives_a_timeout() -> None:
    def timeout(_args: Sequence[str], _timeout: float) -> AdbResult:
        raise subprocess.TimeoutExpired('adb', 10.0)

    assert read_device_timezone('serial', runner=timeout) is None


def test_read_device_timezone_survives_a_missing_executable() -> None:
    def unexecutable(_args: Sequence[str], _timeout: float) -> AdbResult:
        raise OSError('adb not found')

    assert read_device_timezone('serial', runner=unexecutable) is None


def test_compare_timezone_names_both_values() -> None:
    match = compare_timezone('America/Toronto', 'America/Toronto')
    mismatch = compare_timezone('America/Toronto', 'UTC')
    unreadable = compare_timezone('America/Toronto', None)

    assert match.status == 'match'
    assert match.matches is True
    assert match.message() == 'configured timezone America/Toronto; device timezone America/Toronto'

    assert mismatch.status == 'mismatch'
    assert mismatch.matches is False
    assert 'America/Toronto' in mismatch.message()
    assert 'UTC' in mismatch.message()
    assert 'before the device day rolls' in mismatch.message()

    assert unreadable.status == 'unreadable'
    assert unreadable.matches is False
    assert 'could not be read' in unreadable.message()


@pytest.mark.parametrize(
    ('zone', 'expected'),
    [
        (ZoneInfo('America/Toronto'), 'America/Toronto'),
        (UTC, 'UTC'),
        (timezone(timedelta(hours=-5), 'EST'), 'EST'),
    ],
)
def test_configured_timezone_name_keeps_the_iana_key(zone: tzinfo, expected: str) -> None:
    assert configured_timezone_name(zone) == expected


def test_timezone_check_from_environment_skips_without_a_device() -> None:
    def unexpected(_serial: str) -> str | None:
        raise AssertionError('no device read should happen without a serial')

    assert timezone_check_from_environment({'AECOINS_TIMEZONE': 'UTC'}, None, reader=unexpected) is None


def test_an_unset_timezone_uses_the_system_iana_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tzlocal, 'get_localzone', lambda: ZoneInfo('America/Toronto'))

    check = timezone_check_from_environment(
        {},
        '192.0.2.10:5555',
        reader=lambda _serial: 'America/Toronto',
    )

    assert check is not None
    assert check.configured == 'America/Toronto'
    assert check.matches is True


def test_a_mismatched_timezone_environment_is_reported() -> None:
    check = timezone_check_from_environment(
        {'TZ': 'UTC'},
        '192.0.2.10:5555',
        reader=lambda _serial: 'America/Toronto',
    )

    assert check is not None
    assert check.status == 'mismatch'
    assert 'configured timezone UTC' in check.message()
    assert 'device timezone America/Toronto' in check.message()


def test_timezone_check_from_environment_injects_the_device_read() -> None:
    seen: list[str] = []

    def reader(serial: str) -> str | None:
        seen.append(serial)
        return 'America/Toronto'

    check = timezone_check_from_environment(
        {'AECOINS_TIMEZONE': 'America/Toronto'},
        '192.0.2.10:5555',
        reader=reader,
    )

    assert check is not None
    assert check.matches is True
    assert seen == ['192.0.2.10:5555']
