"""The adb-backed input device (issue #17)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

import pytest

from aliexpress_coins.control.device import (
    AdbInputDevice,
    InputResult,
    SubprocessInputRunner,
)
from aliexpress_coins.control.errors import InputError


class _RecordingRunner:
    def __init__(self, result: InputResult | None = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[Sequence[str], float]] = []
        self._result = result if result is not None else InputResult(0)
        self._error = error

    def __call__(self, args: Sequence[str], timeout: float) -> InputResult:
        self.calls.append((args, timeout))
        if self._error is not None:
            raise self._error
        return self._result


def test_adb_input_device_runs_the_documented_shell_command() -> None:
    runner = _RecordingRunner()
    device = AdbInputDevice('192.168.8.220:38045', runner=runner, timeout=4.0)

    device.tap(640, 1200)

    assert len(runner.calls) == 1
    args, timeout = runner.calls[0]
    assert list(args) == ['adb', '-s', '192.168.8.220:38045', 'shell', 'input', 'tap', '640', '1200']
    assert timeout == 4.0


def test_adb_input_device_defaults_to_the_subprocess_runner() -> None:
    assert isinstance(AdbInputDevice('serial'), AdbInputDevice)


def test_adb_input_device_reports_a_non_zero_exit_with_a_short_preview() -> None:
    runner = _RecordingRunner(InputResult(1, b'', b'no input service'))
    device = AdbInputDevice('serial', runner=runner)

    with pytest.raises(InputError, match=r'input tap exited 1.*no input service'):
        device.tap(1, 2)


def test_adb_input_device_truncates_a_large_error_preview() -> None:
    runner = _RecordingRunner(InputResult(1, b'x' * 300, b''))
    device = AdbInputDevice('serial', runner=runner)

    with pytest.raises(InputError, match=r'300 bytes'):
        device.tap(1, 2)


def test_adb_input_device_wraps_a_timeout() -> None:
    runner = _RecordingRunner(error=subprocess.TimeoutExpired(cmd='adb', timeout=10.0))
    device = AdbInputDevice('serial', runner=runner)

    with pytest.raises(InputError, match='timed out'):
        device.tap(1, 2)


def test_adb_input_device_wraps_an_os_error() -> None:
    runner = _RecordingRunner(error=OSError('adb vanished'))
    device = AdbInputDevice('serial', runner=runner)

    with pytest.raises(InputError, match='adb vanished'):
        device.tap(1, 2)


@pytest.mark.parametrize(('serial', 'timeout'), [('', 10.0), ('   ', 10.0), ('serial', 0.0), ('serial', -1.0)])
def test_adb_input_device_rejects_invalid_configuration(serial: str, timeout: float) -> None:
    with pytest.raises(ValueError, match=r'serial is required|timeout must be positive'):
        AdbInputDevice(serial, timeout=timeout)


def test_subprocess_input_runner_returns_stdout_stderr_and_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Sequence[str], bool, float, bool]] = []

    def fake_run(
        args: Sequence[str],
        *,
        capture_output: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((args, capture_output, timeout, check))
        return subprocess.CompletedProcess(args, 3, b'out', b'err')

    monkeypatch.setattr(subprocess, 'run', fake_run)

    result = SubprocessInputRunner()(['adb', 'shell', 'input', 'tap', '1', '2'], 7.0)

    assert result == InputResult(3, b'out', b'err')
    assert calls == [(['adb', 'shell', 'input', 'tap', '1', '2'], True, 7.0, False)]


def test_adb_input_device_presses_a_key_without_inventing_a_coordinate() -> None:
    runner = _RecordingRunner()
    device = AdbInputDevice('serial', runner=runner)

    device.keyevent('KEYCODE_BACK')

    args, _ = runner.calls[0]
    assert list(args) == ['adb', '-s', 'serial', 'shell', 'input', 'keyevent', 'KEYCODE_BACK']


def test_adb_input_device_swipes_with_the_duration_last() -> None:
    runner = _RecordingRunner()
    device = AdbInputDevice('serial', runner=runner)

    device.swipe(600, 1500, 600, 800, 300)

    args, _ = runner.calls[0]
    assert list(args) == ['adb', '-s', 'serial', 'shell', 'input', 'swipe', '600', '1500', '600', '800', '300']


def test_adb_input_device_rejects_an_empty_key_and_a_non_positive_swipe() -> None:
    device = AdbInputDevice('serial', runner=_RecordingRunner())

    with pytest.raises(ValueError, match='key name'):
        device.keyevent('   ')
    with pytest.raises(ValueError, match='duration must be positive'):
        device.swipe(1, 2, 3, 4, 0)


def test_adb_input_device_reports_a_non_zero_key_exit() -> None:
    runner = _RecordingRunner(InputResult(2, b'', b'key rejected'))
    device = AdbInputDevice('serial', runner=runner)

    with pytest.raises(InputError, match=r'input keyevent exited 2.*key rejected'):
        device.keyevent('KEYCODE_BACK')
