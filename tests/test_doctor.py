"""Tests for the read-only doctor checks (issue #34)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from types import SimpleNamespace

import pytest

from aliexpress_coins import doctor


def _runner(
    responses: list[doctor.CommandResult],
    calls: list[list[str]] | None = None,
) -> doctor.CommandRunner:
    queue = list(responses)

    def run(args: Sequence[str], _timeout: float) -> doctor.CommandResult:
        if calls is not None:
            calls.append(list(args))
        return queue.pop(0)

    return run


def _raise_timeout(args: Sequence[str], timeout: float) -> doctor.CommandResult:
    raise subprocess.TimeoutExpired(list(args), timeout)


def test_check_adb_missing() -> None:
    result = doctor.check_adb(which=lambda _name: None)
    assert result.status == 'fail'
    assert 'not on PATH' in result.detail


def test_check_adb_reports_version() -> None:
    calls: list[list[str]] = []
    result = doctor.check_adb(
        which=lambda _name: '/usr/bin/adb',
        run=_runner([doctor.CommandResult(0, 'Android Debug Bridge version 1.0.41\n', '')], calls),
    )
    assert result.status == 'ok'
    assert 'Android Debug Bridge version 1.0.41' in result.detail
    assert calls == [['/usr/bin/adb', 'version']]


def test_check_adb_fails_on_non_zero_exit() -> None:
    result = doctor.check_adb(
        which=lambda _name: '/usr/bin/adb',
        run=_runner([doctor.CommandResult(1, '', 'cannot execute')]),
    )
    assert result.status == 'fail'
    assert 'cannot execute' in result.detail


def test_check_adb_fails_when_it_times_out() -> None:
    result = doctor.check_adb(which=lambda _name: '/usr/bin/adb', run=_raise_timeout)
    assert result.status == 'fail'
    assert 'timed out' in result.detail


def test_check_adb_fails_when_it_cannot_spawn() -> None:
    def unexecutable(_args: Sequence[str], _timeout: float) -> doctor.CommandResult:
        raise PermissionError('permission denied')

    result = doctor.check_adb(which=lambda _name: '/usr/bin/adb', run=unexecutable)
    assert result.status == 'fail'
    assert 'permission denied' in result.detail


def test_check_device_is_skipped_when_unset() -> None:
    def unexpected(_args: Sequence[str], _timeout: float) -> doctor.CommandResult:
        raise AssertionError('no command should run without a serial')

    result = doctor.check_device(None, run=unexpected)
    assert result.status == 'skipped'
    assert 'AECOINS_DEVICE' in result.detail


def test_check_device_accepts_an_already_connected_serial() -> None:
    calls: list[list[str]] = []
    result = doctor.check_device(
        '192.0.2.10:5555',
        run=_runner([doctor.CommandResult(0, 'device\n', '')], calls),
    )
    assert result.status == 'ok'
    assert calls == [['adb', '-s', '192.0.2.10:5555', 'get-state']]


def test_check_device_connects_a_network_serial_first() -> None:
    calls: list[list[str]] = []
    result = doctor.check_device(
        '192.0.2.10:5555',
        run=_runner(
            [
                doctor.CommandResult(1, '', 'unknown'),
                doctor.CommandResult(0, 'connected to 192.0.2.10:5555\n', ''),
                doctor.CommandResult(0, 'device\n', ''),
            ],
            calls,
        ),
    )
    assert result.status == 'ok'
    assert ['adb', 'connect', '192.0.2.10:5555'] in calls
    assert calls[-1] == ['adb', '-s', '192.0.2.10:5555', 'get-state']


def test_check_device_reports_a_failed_connect() -> None:
    result = doctor.check_device(
        '192.0.2.10:5555',
        run=_runner(
            [
                doctor.CommandResult(1, '', 'unknown'),
                doctor.CommandResult(0, 'failed to connect\n', ''),
                doctor.CommandResult(1, '', 'unknown'),
            ]
        ),
    )
    assert result.status == 'fail'
    assert 'failed to connect' in result.detail


def test_check_device_does_not_connect_a_usb_serial() -> None:
    calls: list[list[str]] = []
    result = doctor.check_device(
        'USB123456',
        run=_runner([doctor.CommandResult(1, '', 'offline')], calls),
    )
    assert result.status == 'fail'
    assert 'offline' in result.detail
    assert len(calls) == 1


def test_check_device_fails_when_connect_times_out() -> None:
    calls: list[list[str]] = []

    def run(args: Sequence[str], timeout: float) -> doctor.CommandResult:
        calls.append(list(args))
        if len(calls) == 1:
            return doctor.CommandResult(1, '', 'offline')
        raise subprocess.TimeoutExpired(list(args), timeout)

    result = doctor.check_device('192.0.2.10:5555', run=run)
    assert result.status == 'fail'
    assert 'timed out' in result.detail


def test_load_ocr_reads_its_probe_image() -> None:
    assert 'COINS' in doctor._load_ocr()


def test_check_ocr_reports_success() -> None:
    result = doctor.check_ocr(load=lambda: 'models loaded')
    assert result.status == 'ok'
    assert result.detail == 'models loaded'


def test_check_ocr_reports_engine_failure() -> None:
    def broken() -> str:
        raise RuntimeError('model missing')

    result = doctor.check_ocr(load=broken)
    assert result.status == 'fail'
    assert 'RuntimeError: model missing' in result.detail


def test_run_doctor_passes_with_device_skipped() -> None:
    result = doctor.run_doctor(
        {},
        which=lambda _name: '/usr/bin/adb',
        run=_runner([doctor.CommandResult(0, 'Android Debug Bridge\n', '')]),
        load_ocr=lambda: 'RapidOCR ready',
    )
    assert result == 0


def test_run_doctor_checks_the_configured_device() -> None:
    result = doctor.run_doctor(
        {'AECOINS_DEVICE': '192.0.2.10:5555'},
        which=lambda _name: '/usr/bin/adb',
        run=_runner(
            [
                doctor.CommandResult(0, 'Android Debug Bridge\n', ''),
                doctor.CommandResult(0, 'device\n', ''),
            ]
        ),
        load_ocr=lambda: 'RapidOCR ready',
    )
    assert result == 0


def test_run_doctor_exits_non_zero_when_a_check_fails() -> None:
    def broken() -> str:
        raise RuntimeError('model missing')

    result = doctor.run_doctor(
        {},
        which=lambda _name: '/usr/bin/adb',
        run=_runner([doctor.CommandResult(0, 'Android Debug Bridge\n', '')]),
        load_ocr=broken,
    )
    assert result == 1


def test_run_doctor_fails_when_adb_is_missing() -> None:
    result = doctor.run_doctor({}, which=lambda _name: None, load_ocr=lambda: 'RapidOCR ready')
    assert result == 1


def test_run_doctor_prints_one_line_per_check(capsys: pytest.CaptureFixture[str]) -> None:
    doctor.run_doctor(
        {},
        which=lambda _name: '/usr/bin/adb',
        run=_runner([doctor.CommandResult(0, 'Android Debug Bridge\n', '')]),
        load_ocr=lambda: 'RapidOCR ready',
    )
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert lines[0].startswith('[ok] adb:')
    assert lines[1].startswith('[skipped] device:')
    assert lines[2].startswith('[ok] ocr:')


def test_run_command_collects_the_process_output(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: Sequence[str], **kwargs: object) -> SimpleNamespace:
        assert list(args) == ['adb', 'version']
        assert kwargs['timeout'] == 5.0
        return SimpleNamespace(returncode=0, stdout='out\n', stderr='err\n')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    assert doctor._run_command(['adb', 'version'], 5.0) == doctor.CommandResult(0, 'out\n', 'err\n')
