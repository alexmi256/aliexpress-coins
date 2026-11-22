"""Live device hygiene and routes, with the transport seam scripted (issue #29)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

import pytest

from aliexpress_coins.runner.device import (
    APP_ACTIVITY,
    AdbDevice,
    DeviceCommandResult,
    HygieneReport,
    _portrait_rotation,
)
from aliexpress_coins.runner.errors import DeviceError
from aliexpress_coins.tasks.surface import SHORTCUT_DEEP_LINK, SurfaceRoute


class _Runner:
    def __init__(self, *results: DeviceCommandResult | Exception) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, ...]] = []
        self.timeouts: list[float] = []

    def __call__(self, args: Sequence[str], timeout: float) -> DeviceCommandResult:
        assert timeout > 0
        self.calls.append(tuple(args))
        self.timeouts.append(timeout)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _ok(text: str = '') -> DeviceCommandResult:
    return DeviceCommandResult(0, text.encode(), b'')


def _fail(text: str = 'failed') -> DeviceCommandResult:
    return DeviceCommandResult(1, b'', text.encode())


def test_prepare_checks_the_device_wakes_it_and_warms_the_app() -> None:
    runner = _Runner(
        _ok('device'),
        _ok(),
        _ok(),
        _ok('Physical size: 2000x1200'),
        _ok('1'),
        _ok('0'),
        _ok(),
        _ok(),
        _ok('Starting: Intent'),
    )
    sleeps: list[float] = []
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=10.0, settle=sleeps.append)

    report = device.prepare()

    assert report == HygieneReport(
        serial='usb-serial',
        state='device',
        awake=True,
        app_started=True,
        detail='woke usb-serial and started com.alibaba.aliexpresshd/com.aliexpress.app.FirstActivity; '
        'launcher said Starting: Intent',
    )
    assert sleeps == [10.0]
    assert runner.calls == [
        ('adb', '-s', 'usb-serial', 'get-state'),
        ('adb', '-s', 'usb-serial', 'shell', 'input', 'keyevent', 'KEYCODE_WAKEUP'),
        ('adb', '-s', 'usb-serial', 'shell', 'wm', 'dismiss-keyguard'),
        ('adb', '-s', 'usb-serial', 'shell', 'wm', 'size'),
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'get', 'system', 'accelerometer_rotation'),
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'get', 'system', 'user_rotation'),
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'put', 'system', 'accelerometer_rotation', '0'),
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'put', 'system', 'user_rotation', '1'),
        ('adb', '-s', 'usb-serial', 'shell', 'am', 'start', '-n', APP_ACTIVITY),
    ]


def test_restore_puts_the_previous_rotation_settings_back() -> None:
    runner = _Runner(
        _ok('device'),
        _ok(),
        _ok(),
        _ok('Physical size: 2000x1200'),
        _ok('1'),
        _ok('0'),
        _ok(),
        _ok(),
        _ok('Starting: Intent'),
        _ok(),
        _ok(),
    )
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0)
    device.prepare()

    device.restore()

    assert runner.calls[-2:] == [
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'put', 'system', 'accelerometer_rotation', '1'),
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'put', 'system', 'user_rotation', '0'),
    ]
    assert runner.timeouts[-2:] == [1.5, 1.5]
    calls = len(runner.calls)
    device.restore()
    assert len(runner.calls) == calls


def test_a_network_serial_is_connected_when_get_state_fails_first() -> None:
    runner = _Runner(
        _fail('not found'),
        _ok('connected to 192.168.8.65:5555'),
        _ok('device'),
        _ok(),
        _ok(),
        _ok('Physical size: 1200x2000'),
        _ok('1'),
        _ok('0'),
        _ok(),
        _ok(),
        _ok(),
    )
    device = AdbDevice('192.168.8.65:5555', runner=runner, app_settle_seconds=0)

    report = device.prepare()

    assert report.state == 'device'
    assert runner.calls[1] == ('adb', 'connect', '192.168.8.65:5555')
    assert runner.calls[2] == ('adb', '-s', '192.168.8.65:5555', 'get-state')


def test_a_network_connect_that_does_not_produce_a_device_is_an_error() -> None:
    runner = _Runner(_fail('not found'), _ok('connected'), _fail('offline'))
    device = AdbDevice('192.168.8.65:5555', runner=runner)

    with pytest.raises(DeviceError, match='adb connect: connected'):
        device.prepare()


def test_a_missing_usb_device_is_an_error() -> None:
    runner = _Runner(_fail('device offline'))
    device = AdbDevice('usb-serial', runner=runner)
    with pytest.raises(DeviceError, match='device offline'):
        device.prepare()


def test_a_failed_wake_or_start_is_an_error() -> None:
    runner = _Runner(_ok('device'), _fail('input failed'))
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0)
    with pytest.raises(DeviceError, match='wake the screen'):
        device.prepare()


def test_navigate_fires_the_measured_shortcut_intent() -> None:
    runner = _Runner(_ok())
    device = AdbDevice('usb-serial', runner=runner)

    device.navigate(SurfaceRoute.SHORTCUT)

    assert runner.calls == [
        (
            'adb',
            '-s',
            'usb-serial',
            'shell',
            'am',
            'start',
            '-a',
            'android.intent.action.VIEW',
            '-d',
            SHORTCUT_DEEP_LINK,
        ),
    ]


def test_the_widget_route_is_refused_rather_than_guessed() -> None:
    runner = _Runner()
    device = AdbDevice('usb-serial', runner=runner)
    with pytest.raises(DeviceError, match='not implemented'):
        device.navigate(SurfaceRoute.WIDGET)
    assert runner.calls == []


def test_a_failed_navigation_is_a_device_error() -> None:
    runner = _Runner(_fail('no activity found'))
    device = AdbDevice('usb-serial', runner=runner)
    with pytest.raises(DeviceError, match='take the measured shortcut route'):
        device.navigate(SurfaceRoute.SHORTCUT)


def test_a_timeout_is_a_device_error() -> None:
    runner = _Runner(subprocess.TimeoutExpired('adb', 1.0))
    device = AdbDevice('usb-serial', runner=runner)
    with pytest.raises(DeviceError, match='timed out'):
        device.prepare()


def test_an_oserror_is_a_device_error() -> None:
    runner = _Runner(OSError('no adb binary'))
    device = AdbDevice('usb-serial', runner=runner)
    with pytest.raises(DeviceError, match='could not run adb'):
        device.prepare()


def test_a_long_failure_output_is_previewed_and_bounded() -> None:
    runner = _Runner(_fail('x' * 400))
    device = AdbDevice('usb-serial', runner=runner)
    with pytest.raises(DeviceError) as raised:
        device.prepare()
    assert '(400 bytes)' in str(raised.value)


@pytest.mark.parametrize(('serial', 'timeout', 'settle'), [('', 1.0, 0.0), ('x', 0.0, 0.0), ('x', 1.0, -1.0)])
def test_constructor_rejects_impossible_settings(serial: str, timeout: float, settle: float) -> None:
    with pytest.raises(ValueError, match=r'positive|required|negative'):
        AdbDevice(serial, timeout=timeout, app_settle_seconds=settle)


@pytest.mark.parametrize(
    ('output', 'rotation'),
    [
        ('Physical size: 2000x1200', 1),
        ('Physical size: 1200x2000', 0),
        ('Physical size: 2000x1200\nOverride size: 1080x1920', 0),
        ('no dimensions here', None),
    ],
)
def test_portrait_rotation_is_derived_from_the_panel_size(output: str, rotation: int | None) -> None:
    assert _portrait_rotation(output) == rotation


def test_the_real_subprocess_runner_returns_the_completed_process(monkeypatch: pytest.MonkeyPatch) -> None:
    class Completed:
        returncode = 0
        stdout = b'out'
        stderr = b'err'

    seen: list[tuple[Sequence[str], int]] = []

    def fake_run(args: Sequence[str], *, capture_output: bool, timeout: int, check: bool) -> Completed:
        seen.append((args, timeout))
        assert capture_output is True
        assert check is False
        return Completed()

    monkeypatch.setattr(subprocess, 'run', fake_run)
    from aliexpress_coins.runner.device import SubprocessCommandRunner

    result = SubprocessCommandRunner()(['adb', 'version'], 5.0)
    assert result.returncode == 0
    assert result.stdout == b'out'
    assert result.stderr == b'err'
    assert seen == [(['adb', 'version'], 5.0)]


def test_an_unparseable_panel_size_skips_the_rotation_lock() -> None:
    runner = _Runner(_ok('device'), _ok(), _ok(), _ok('no dimensions here'), _ok('Starting: Intent'))
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0)

    device.prepare()

    assert not any('settings' in call for call in runner.calls)


def test_a_missing_previous_setting_leaves_rotation_alone() -> None:
    runner = _Runner(
        _ok('device'),
        _ok(),
        _ok(),
        _ok('Physical size: 2000x1200'),
        _ok('null'),
        _ok('0'),
        _ok('Starting: Intent'),
    )
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0)

    device.prepare()
    calls = len(runner.calls)
    device.restore()

    assert not any('settings' in call and 'put' in call for call in runner.calls)
    assert len(runner.calls) == calls


def test_a_failed_restore_raises_and_keeps_the_pending_values() -> None:
    runner = _Runner(
        _ok('device'),
        _ok(),
        _ok(),
        _ok('Physical size: 2000x1200'),
        _ok('1'),
        _ok('0'),
        _ok(),
        _ok(),
        _ok('Starting: Intent'),
        _fail('settings write failed'),
        _ok(),
        _ok(),
    )
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0)
    device.prepare()

    with pytest.raises(DeviceError, match='restore the previous auto-rotation'):
        device.restore()

    device.restore()

    assert runner.calls[-2:] == [
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'put', 'system', 'accelerometer_rotation', '1'),
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'put', 'system', 'user_rotation', '0'),
    ]
