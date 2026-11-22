"""Live device hygiene and routes, with the transport seam scripted (issues #29, #32)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

import pytest

from aliexpress_coins.runner.device import (
    APP_ACTIVITY,
    AdbDevice,
    DeviceCommandResult,
    HygieneReport,
    _keyguard_showing,
    _launch_failed,
    _package_installed,
    _portrait_rotation,
    _screen_asleep,
)
from aliexpress_coins.runner.errors import (
    AppLaunchFailed,
    AppNotInstalled,
    DeviceError,
    DeviceLocked,
    DeviceSleeping,
    DeviceUnavailable,
)
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


def _prepare_results(
    *,
    state: DeviceCommandResult | None = None,
    power: str = 'mWakefulness=Awake',
    size: str = 'Physical size: 2000x1200',
    auto: str = '1',
    rotation: str = '0',
    wake: DeviceCommandResult | None = None,
    power_after_wake: str | None = None,
    keyguard: str = 'isKeyguardShowing=false',
    stay_on: str = '15',
    package: DeviceCommandResult | None = None,
    start: DeviceCommandResult | None = None,
) -> list[DeviceCommandResult | Exception]:
    """The full prepare sequence for a readable landscape panel."""
    results: list[DeviceCommandResult | Exception] = [
        state if state is not None else _ok('device'),
        _ok(power),
        _ok(size),
        _ok(auto),
        _ok(rotation),
        _ok(),
        _ok(),
        wake if wake is not None else _ok(),
        _ok(),
        _ok(),
        _ok(keyguard),
        _ok(stay_on),
    ]
    if power_after_wake is not None:
        results.insert(8, _ok(power_after_wake))
    if stay_on.isdigit():
        results.append(_ok())  # svc power stayon true
    results.extend(
        (
            package if package is not None else _ok('package:/data/app/base.apk'),
            start if start is not None else _ok('Starting: Intent'),
        ),
    )
    return results


def _device(*results: DeviceCommandResult | Exception, settle: list[float] | None = None) -> AdbDevice:
    return AdbDevice(
        'usb-serial',
        runner=_Runner(*results),
        app_settle_seconds=10.0,
        settle=(settle if settle is not None else []).append,
    )


def test_prepare_wakes_dismisses_the_keyguard_and_warms_the_app() -> None:
    runner = _Runner(*_prepare_results())
    sleeps: list[float] = []
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=10.0, settle=sleeps.append)

    report = device.prepare()

    assert report == HygieneReport(
        serial='usb-serial',
        state='device',
        awake=True,
        app_started=True,
        was_asleep=False,
        kept_awake=True,
        app_installed=True,
        detail='woke usb-serial (device was awake; stay-awake on) and started '
        'com.alibaba.aliexpresshd/com.aliexpress.app.FirstActivity; launcher said Starting: Intent',
    )
    assert sleeps == [10.0]
    assert runner.calls == [
        ('adb', '-s', 'usb-serial', 'get-state'),
        ('adb', '-s', 'usb-serial', 'shell', 'dumpsys', 'power'),
        ('adb', '-s', 'usb-serial', 'shell', 'wm', 'size'),
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'get', 'system', 'accelerometer_rotation'),
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'get', 'system', 'user_rotation'),
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'put', 'system', 'accelerometer_rotation', '0'),
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'put', 'system', 'user_rotation', '1'),
        ('adb', '-s', 'usb-serial', 'shell', 'input', 'keyevent', 'KEYCODE_WAKEUP'),
        ('adb', '-s', 'usb-serial', 'shell', 'wm', 'dismiss-keyguard'),
        ('adb', '-s', 'usb-serial', 'shell', 'input', 'swipe', '600', '1700', '600', '500', '200'),
        ('adb', '-s', 'usb-serial', 'shell', 'dumpsys', 'window'),
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'get', 'global', 'stay_on_while_plugged_in'),
        ('adb', '-s', 'usb-serial', 'shell', 'svc', 'power', 'stayon', 'true'),
        ('adb', '-s', 'usb-serial', 'shell', 'pm', 'path', 'com.alibaba.aliexpresshd'),
        ('adb', '-s', 'usb-serial', 'shell', 'am', 'start', '-n', APP_ACTIVITY),
    ]


def test_a_sleeping_device_is_recorded_woken_and_then_prepared() -> None:
    runner = _Runner(*_prepare_results(power='mWakefulness=Asleep', power_after_wake='mWakefulness=Awake'))
    sleeps: list[float] = []
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0, settle=sleeps.append)

    report = device.prepare()

    assert report.was_asleep is True
    assert report.awake is True
    assert 'device was asleep' in report.detail
    assert ('adb', '-s', 'usb-serial', 'shell', 'input', 'keyevent', 'KEYCODE_WAKEUP') in runner.calls
    assert daily_commands(runner.calls) == [
        'input keyevent KEYCODE_WAKEUP',
        'wm dismiss-keyguard',
        'input swipe 600 1700 600 500 200',
    ]
    assert sleeps == [0.5]


def daily_commands(calls: list[tuple[str, ...]]) -> list[str]:
    """The wake/keyguard commands, in order, as one readable string each."""
    prefixes = {('input', 'keyevent'), ('input', 'swipe'), ('wm', 'dismiss-keyguard')}
    return [' '.join(call[4:]) for call in calls if len(call) > 5 and call[4:6] in prefixes]


def test_restore_puts_the_previous_stay_awake_and_rotation_settings_back() -> None:
    runner = _Runner(*_prepare_results(), _ok(), _ok(), _ok())
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0)
    device.prepare()

    device.restore()

    assert runner.calls[-3:] == [
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'put', 'global', 'stay_on_while_plugged_in', '15'),
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'put', 'system', 'accelerometer_rotation', '1'),
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'put', 'system', 'user_rotation', '0'),
    ]
    assert runner.timeouts[-3:] == [1.5, 1.5, 1.5]
    calls = len(runner.calls)
    device.restore()
    assert len(runner.calls) == calls


def test_an_unreadable_stay_awake_setting_is_left_alone() -> None:
    runner = _Runner(*_prepare_results(stay_on='null'), _ok(), _ok())
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0)

    report = device.prepare()
    calls = len(runner.calls)
    device.restore()

    assert report.kept_awake is False
    assert not any('svc' in call for call in runner.calls)
    assert not any('stay_on_while_plugged_in' in call and 'put' in call for call in runner.calls)
    assert len(runner.calls) == calls + 2  # only the rotation restore


def test_a_network_serial_is_connected_when_get_state_fails_first() -> None:
    runner = _Runner(
        _fail('not found'),
        _ok('connected to 192.168.8.65:5555'),
        *_prepare_results(),
    )
    device = AdbDevice('192.168.8.65:5555', runner=runner, app_settle_seconds=0)

    report = device.prepare()

    assert report.state == 'device'
    assert runner.calls[1] == ('adb', 'connect', '192.168.8.65:5555')
    assert runner.calls[2] == ('adb', '-s', '192.168.8.65:5555', 'get-state')


def test_a_network_connect_that_does_not_produce_a_device_is_an_error() -> None:
    runner = _Runner(_fail('not found'), _ok('connected'), _fail('offline'))
    device = AdbDevice('192.168.8.65:5555', runner=runner)

    with pytest.raises(DeviceUnavailable, match='adb connect: connected'):
        device.prepare()


def test_a_missing_usb_device_is_an_error() -> None:
    runner = _Runner(_fail('device offline'))
    device = AdbDevice('usb-serial', runner=runner)
    with pytest.raises(DeviceUnavailable, match='device offline'):
        device.prepare()


def test_a_failed_wake_is_named_as_sleeping() -> None:
    runner = _Runner(*_prepare_results(wake=_fail('input failed')))
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0)
    with pytest.raises(DeviceSleeping, match='did not wake'):
        device.prepare()


def test_a_keyguard_that_survives_wake_is_named_as_locked() -> None:
    runner = _Runner(*_prepare_results(keyguard='isKeyguardShowing=true'))
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0)
    with pytest.raises(DeviceLocked, match='PIN or biometric'):
        device.prepare()


def test_an_absent_package_is_named_as_not_installed() -> None:
    runner = _Runner(*_prepare_results(package=_ok('')))
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0)
    with pytest.raises(AppNotInstalled, match='not installed'):
        device.prepare()


def test_a_launch_that_reports_an_error_is_named_as_a_launch_failure() -> None:
    runner = _Runner(*_prepare_results(start=_ok('Error: Activity class does not exist')))
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0)
    with pytest.raises(AppLaunchFailed, match='did not start'):
        device.prepare()


def test_check_connected_names_a_lost_serial() -> None:
    device = AdbDevice('usb-serial', runner=_Runner(_fail('device offline')))
    with pytest.raises(DeviceUnavailable, match='device offline'):
        device.check_connected()


def test_check_connected_passes_when_the_serial_answers() -> None:
    device = AdbDevice('usb-serial', runner=_Runner(_ok('device')))
    device.check_connected()


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


@pytest.mark.parametrize(
    ('output', 'asleep'),
    [
        ('mWakefulness=Awake', False),
        ('mWakefulness=Asleep', True),
        ('mWakefulness=Dozing', True),
        ('no power state here', False),
    ],
)
def test_screen_sleep_is_read_not_guessed(output: str, asleep: bool) -> None:
    assert _screen_asleep(output) is asleep


@pytest.mark.parametrize(
    ('output', 'locked'),
    [
        ('isKeyguardShowing=false', False),
        ('isKeyguardShowing=true', True),
        ('mDreamingLockscreen=true', True),
        ('mDreamingLockscreen=false\nisKeyguardShowing=true', True),
        ('KeyguardServiceDelegate\n  showing=false', False),
    ],
)
def test_keyguard_detection_only_trusts_a_named_true(output: str, locked: bool) -> None:
    assert _keyguard_showing(output) is locked


@pytest.mark.parametrize(
    ('output', 'installed'),
    [
        ('package:/data/app/base.apk', True),
        ('', False),
        ('Error: package not found', False),
    ],
)
def test_package_detection_needs_a_package_line(output: str, installed: bool) -> None:
    assert _package_installed(output) is installed


@pytest.mark.parametrize(
    ('output', 'failed'),
    [
        ('Starting: Intent { cmp=... }', False),
        ('Warning: Activity not started, its current task has been brought to the front', False),
        ('Error: Activity class {x} does not exist.', True),
        ('Unable to find explicit activity class', True),
    ],
)
def test_launch_failure_is_named_without_calling_a_warning_a_failure(output: str, failed: bool) -> None:
    assert _launch_failed(output) is failed


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


def test_an_unparseable_panel_size_skips_the_rotation_lock_and_swipe() -> None:
    runner = _Runner(
        _ok('device'),
        _ok('mWakefulness=Awake'),
        _ok('no dimensions here'),
        _ok(),
        _ok(),
        _ok('isKeyguardShowing=false'),
        _ok('15'),
        _ok(),
        _ok('package:/data/app/base.apk'),
        _ok('Starting: Intent'),
    )
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0)

    report = device.prepare()

    assert not any('swipe' in call or ('put' in call and 'system' in call) for call in runner.calls)
    assert report.app_started is True


def test_a_missing_previous_setting_leaves_rotation_alone() -> None:
    runner = _Runner(
        _ok('device'),
        _ok('mWakefulness=Awake'),
        _ok('Physical size: 2000x1200'),
        _ok('null'),
        _ok('0'),
        _ok(),
        _ok(),
        _ok(),
        _ok('isKeyguardShowing=false'),
        _ok('15'),
        _ok(),
        _ok('package:/data/app/base.apk'),
        _ok('Starting: Intent'),
        _ok(),
    )
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0)

    device.prepare()
    calls = len(runner.calls)
    device.restore()

    assert not any('settings' in call and 'put' in call and 'system' in call for call in runner.calls)
    assert len(runner.calls) == calls + 1  # only the stay-awake restore


def test_a_failed_rotation_restore_still_attempts_the_stay_awake_restore() -> None:
    runner = _Runner(
        *_prepare_results(),
        _ok(),
        _fail('settings write failed'),
        _ok(),
        _ok(),
        _ok(),
    )
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0)
    device.prepare()

    with pytest.raises(DeviceError, match='restore the previous auto-rotation'):
        device.restore()

    # The stay-awake restore succeeded first and is not repeated.
    assert runner.calls[-3:] == [
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'put', 'global', 'stay_on_while_plugged_in', '15'),
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'put', 'system', 'accelerometer_rotation', '1'),
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'put', 'system', 'user_rotation', '0'),
    ]

    device.restore()

    assert runner.calls[-2:] == [
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'put', 'system', 'accelerometer_rotation', '1'),
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'put', 'system', 'user_rotation', '0'),
    ]


def test_restore_attempts_the_rotation_even_when_the_stay_awake_write_fails() -> None:
    runner = _Runner(
        *_prepare_results(),
        _fail('stay-awake write failed'),
        _ok(),
        _ok(),
    )
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0)
    device.prepare()

    with pytest.raises(DeviceError, match='restore the previous stay-awake'):
        device.restore()

    assert runner.calls[-2:] == [
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'put', 'system', 'accelerometer_rotation', '1'),
        ('adb', '-s', 'usb-serial', 'shell', 'settings', 'put', 'system', 'user_rotation', '0'),
    ]


def test_a_launch_command_that_fails_is_named_as_a_launch_failure() -> None:
    runner = _Runner(*_prepare_results(start=_fail('activity manager died')), _ok('device'))
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0)
    with pytest.raises(AppLaunchFailed, match='could not be started'):
        device.prepare()


def test_a_launch_probe_on_an_offline_serial_is_named_as_offline() -> None:
    runner = _Runner(*_prepare_results(start=_fail('activity manager died')), _fail('device offline'))
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0)
    with pytest.raises(DeviceUnavailable, match='device offline'):
        device.prepare()


def test_a_failed_user_rotation_restore_is_reported_after_the_stay_awake_write() -> None:
    runner = _Runner(
        *_prepare_results(),
        _ok(),
        _ok(),
        _fail('rotation write failed'),
    )
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0)
    device.prepare()

    with pytest.raises(DeviceError, match='restore the previous screen rotation'):
        device.restore()

    assert device._restore_rotation == ('1', '0')


def test_a_second_prepare_keeps_the_original_restore_values() -> None:
    runner = _Runner(*_prepare_results(), *_prepare_results())
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0)

    first = device.prepare()
    second = device.prepare()

    assert first.serial == second.serial
    assert device._restore_rotation == ('1', '0')
    assert device._restore_stay_on == '15'


def test_a_package_probe_on_an_offline_serial_is_named_as_offline() -> None:
    results = _prepare_results(package=_fail('pm path failed'))
    results.insert(14, _fail('device offline'))
    runner = _Runner(*results)
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0)

    with pytest.raises(DeviceUnavailable, match='device offline'):
        device.prepare()


def test_a_failed_stay_awake_write_still_checks_the_serial_and_continues() -> None:
    results = _prepare_results()
    results[12] = _fail('svc failed')
    results.insert(13, _ok('device'))
    runner = _Runner(*results)
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0)

    report = device.prepare()

    assert report.kept_awake is False
    assert any(call[-1] == 'get-state' for call in runner.calls)


def test_a_sleeping_device_that_stays_dark_is_named_as_sleeping_after_the_settle() -> None:
    runner = _Runner(
        *_prepare_results(power='mWakefulness=Dozing', power_after_wake='mWakefulness=Dozing'),
    )
    sleeps: list[float] = []
    device = AdbDevice('usb-serial', runner=runner, app_settle_seconds=0, settle=sleeps.append)

    with pytest.raises(DeviceSleeping, match='still asleep'):
        device.prepare()

    assert sleeps == [0.5]
    assert not any('dismiss-keyguard' in call for call in runner.calls)
