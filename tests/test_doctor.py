"""Tests for the read-only doctor checks (issue #34)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from types import SimpleNamespace

import pytest

from aliexpress_coins import doctor
from tests.support.adb import ScriptedAdbRunner


def _runner(
    responses: list[doctor.CommandResult],
    calls: list[list[str]] | None = None,
) -> doctor.CommandRunner:
    queue = list(responses)

    def respond(argv: tuple[str, ...]) -> doctor.CommandResult:
        if calls is not None:
            calls.append(list(argv))
        return queue.pop(0)

    return ScriptedAdbRunner.responding(respond)


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
        run=_runner([doctor.CommandResult(0, b'Android Debug Bridge version 1.0.41\n', b'')], calls),
    )
    assert result.status == 'ok'
    assert 'Android Debug Bridge version 1.0.41' in result.detail
    assert calls == [['/usr/bin/adb', 'version']]


def test_check_adb_fails_on_non_zero_exit() -> None:
    result = doctor.check_adb(
        which=lambda _name: '/usr/bin/adb',
        run=_runner([doctor.CommandResult(1, b'', b'cannot execute')]),
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
        run=_runner([doctor.CommandResult(0, b'device\n', b'')], calls),
    )
    assert result.status == 'ok'
    assert calls == [['adb', '-s', '192.0.2.10:5555', 'get-state']]


def test_check_device_connects_a_network_serial_first() -> None:
    calls: list[list[str]] = []
    result = doctor.check_device(
        '192.0.2.10:5555',
        run=_runner(
            [
                doctor.CommandResult(1, b'', b'unknown'),
                doctor.CommandResult(0, b'connected to 192.0.2.10:5555\n', b''),
                doctor.CommandResult(0, b'device\n', b''),
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
                doctor.CommandResult(1, b'', b'unknown'),
                doctor.CommandResult(0, b'failed to connect\n', b''),
                doctor.CommandResult(1, b'', b'unknown'),
            ]
        ),
    )
    assert result.status == 'fail'
    assert 'failed to connect' in result.detail


def test_check_device_does_not_connect_a_usb_serial() -> None:
    calls: list[list[str]] = []
    result = doctor.check_device(
        'USB123456',
        run=_runner([doctor.CommandResult(1, b'', b'offline')], calls),
    )
    assert result.status == 'fail'
    assert 'offline' in result.detail
    assert len(calls) == 1


def test_check_device_reports_a_probe_that_cannot_spawn() -> None:
    def unexecutable(_args: Sequence[str], _timeout: float) -> doctor.CommandResult:
        raise OSError('adb is not executable')

    result = doctor.check_device('192.0.2.10:5555', run=unexecutable)

    assert result.status == 'fail'
    assert 'adb is not executable' in result.detail


def test_check_device_fails_when_connect_times_out() -> None:
    calls: list[list[str]] = []

    def run(args: Sequence[str], timeout: float) -> doctor.CommandResult:
        calls.append(list(args))
        if len(calls) == 1:
            return doctor.CommandResult(1, b'', b'offline')
        raise subprocess.TimeoutExpired(list(args), timeout)

    result = doctor.check_device('192.0.2.10:5555', run=run)
    assert result.status == 'fail'
    assert 'timed out' in result.detail


def test_check_geometry_is_skipped_when_unset() -> None:
    def unexpected(_args: Sequence[str], _timeout: float) -> doctor.CommandResult:
        raise AssertionError('no command should run without a serial')

    result = doctor.check_geometry(None, run=unexpected)
    assert result.status == 'skipped'
    assert 'AECOINS_DEVICE' in result.detail


def test_check_geometry_prints_the_profile_and_every_derived_dimension() -> None:
    calls: list[list[str]] = []
    result = doctor.check_geometry(
        '192.0.2.10:5555',
        run=_runner(
            [
                doctor.CommandResult(0, b'Physical size: 2000x1200\n', b''),
                doctor.CommandResult(0, b'Physical density: 240\n', b''),
                doctor.CommandResult(0, b'35\n', b''),
            ],
            calls,
        ),
    )

    assert result.status == 'ok'
    assert '1200x2000' in result.detail
    assert '240 dpi' in result.detail
    assert 'sdk 35' in result.detail
    assert 'portrait' in result.detail
    assert 'profile c9b8f88670e295b5' in result.detail
    assert calls == [
        ['adb', '-s', '192.0.2.10:5555', 'shell', 'wm', 'size'],
        ['adb', '-s', '192.0.2.10:5555', 'shell', 'wm', 'density'],
        ['adb', '-s', '192.0.2.10:5555', 'shell', 'getprop', 'ro.build.version.sdk'],
    ]


def test_check_geometry_keeps_a_panel_that_is_already_portrait() -> None:
    result = doctor.check_geometry(
        '192.0.2.10:5555',
        run=_runner(
            [
                doctor.CommandResult(0, b'Physical size: 1200x2000\n', b''),
                doctor.CommandResult(0, b'Physical density: 240\n', b''),
                doctor.CommandResult(0, b'35\n', b''),
            ],
        ),
    )

    assert result.status == 'ok'
    assert '1200x2000' in result.detail
    assert 'portrait' in result.detail


def test_check_geometry_reports_a_failed_probe() -> None:
    result = doctor.check_geometry(
        '192.0.2.10:5555',
        run=_runner([doctor.CommandResult(1, b'', b'no shell')]),
    )

    assert result.status == 'fail'
    assert 'wm size exited 1' in result.detail
    assert 'no shell' in result.detail


def test_check_geometry_reports_a_timeout() -> None:
    result = doctor.check_geometry('192.0.2.10:5555', run=_raise_timeout)

    assert result.status == 'fail'
    assert 'timed out' in result.detail


def test_check_geometry_reports_unparseable_probe_output() -> None:
    result = doctor.check_geometry(
        '192.0.2.10:5555',
        run=_runner(
            [
                doctor.CommandResult(0, b'size unknown\n', b''),
                doctor.CommandResult(0, b'Physical density: 240\n', b''),
                doctor.CommandResult(0, b'35\n', b''),
            ],
        ),
    )

    assert result.status == 'fail'
    assert 'could not parse wm size' in result.detail


def test_check_timezone_prints_both_values_when_they_match() -> None:
    calls: list[list[str]] = []
    result = doctor.check_timezone(
        {'AECOINS_TIMEZONE': 'America/Toronto'},
        '192.0.2.10:5555',
        run=_runner([doctor.CommandResult(0, b'America/Toronto\n', b'')], calls),
    )

    assert result.status == 'ok'
    assert result.detail.count('America/Toronto') == 2
    assert calls == [['adb', '-s', '192.0.2.10:5555', 'shell', 'getprop', 'persist.sys.timezone']]


def test_check_timezone_fails_naming_both_when_they_differ() -> None:
    result = doctor.check_timezone(
        {'AECOINS_TIMEZONE': 'America/Toronto'},
        '192.0.2.10:5555',
        run=_runner([doctor.CommandResult(0, b'UTC\n', b'')]),
    )

    assert result.status == 'fail'
    assert 'America/Toronto' in result.detail
    assert 'UTC' in result.detail
    assert 'before the device day rolls' in result.detail


def test_check_timezone_is_skipped_when_the_device_is_unset() -> None:
    def unexpected(_args: Sequence[str], _timeout: float) -> doctor.CommandResult:
        raise AssertionError('no command should run without a serial')

    result = doctor.check_timezone({'AECOINS_TIMEZONE': 'America/Toronto'}, None, run=unexpected)

    assert result.status == 'skipped'
    assert 'America/Toronto' in result.detail
    assert 'AECOINS_DEVICE' in result.detail


def test_check_timezone_fails_when_the_device_property_is_empty() -> None:
    result = doctor.check_timezone(
        {'AECOINS_TIMEZONE': 'America/Toronto'},
        '192.0.2.10:5555',
        run=_runner([doctor.CommandResult(0, b'\n', b'')]),
    )

    assert result.status == 'fail'
    assert 'America/Toronto' in result.detail
    assert 'could not be read' in result.detail


def test_check_timezone_reports_an_invalid_configured_zone() -> None:
    result = doctor.check_timezone({'AECOINS_TIMEZONE': 'Mars/Olympus'}, '192.0.2.10:5555')

    assert result.status == 'fail'
    assert 'Mars/Olympus' in result.detail
    assert 'unknown timezone' in result.detail


@pytest.mark.slow
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
        run=_runner([doctor.CommandResult(0, b'Android Debug Bridge\n', b'')]),
        load_ocr=lambda: 'RapidOCR ready',
    )
    assert result == 0


def test_run_doctor_checks_the_configured_device() -> None:
    result = doctor.run_doctor(
        {'AECOINS_DEVICE': '192.0.2.10:5555', 'AECOINS_TIMEZONE': 'America/Toronto'},
        which=lambda _name: '/usr/bin/adb',
        run=_runner(
            [
                doctor.CommandResult(0, b'Android Debug Bridge\n', b''),
                doctor.CommandResult(0, b'device\n', b''),
                doctor.CommandResult(0, b'Physical size: 2000x1200\n', b''),
                doctor.CommandResult(0, b'Physical density: 240\n', b''),
                doctor.CommandResult(0, b'35\n', b''),
                doctor.CommandResult(0, b'America/Toronto\n', b''),
            ]
        ),
        load_ocr=lambda: 'RapidOCR ready',
    )
    assert result == 0


def test_run_doctor_fails_on_a_timezone_mismatch(capsys: pytest.CaptureFixture[str]) -> None:
    result = doctor.run_doctor(
        {'AECOINS_DEVICE': '192.0.2.10:5555', 'AECOINS_TIMEZONE': 'UTC'},
        which=lambda _name: '/usr/bin/adb',
        run=_runner(
            [
                doctor.CommandResult(0, b'Android Debug Bridge\n', b''),
                doctor.CommandResult(0, b'device\n', b''),
                doctor.CommandResult(0, b'Physical size: 2000x1200\n', b''),
                doctor.CommandResult(0, b'Physical density: 240\n', b''),
                doctor.CommandResult(0, b'35\n', b''),
                doctor.CommandResult(0, b'America/Toronto\n', b''),
            ]
        ),
        load_ocr=lambda: 'RapidOCR ready',
    )

    assert result == 1
    output = capsys.readouterr().out
    assert '[fail] timezone:' in output
    assert 'configured timezone UTC' in output
    assert 'device timezone America/Toronto' in output


def test_run_doctor_exits_non_zero_when_a_check_fails() -> None:
    def broken() -> str:
        raise RuntimeError('model missing')

    result = doctor.run_doctor(
        {},
        which=lambda _name: '/usr/bin/adb',
        run=_runner([doctor.CommandResult(0, b'Android Debug Bridge\n', b'')]),
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
        run=_runner([doctor.CommandResult(0, b'Android Debug Bridge\n', b'')]),
        load_ocr=lambda: 'RapidOCR ready',
    )
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 5
    assert lines[0].startswith('[ok] adb:')
    assert lines[1].startswith('[skipped] device:')
    assert lines[2].startswith('[skipped] geometry:')
    assert lines[3].startswith('[skipped] timezone:')
    assert lines[4].startswith('[ok] ocr:')


def test_run_command_collects_the_process_output(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(args: Sequence[str], **kwargs: object) -> SimpleNamespace:
        assert list(args) == ['adb', 'version']
        assert kwargs['timeout'] == 5.0
        return SimpleNamespace(returncode=0, stdout=b'out\n', stderr=b'err\n')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    assert doctor._run_command(['adb', 'version'], 5.0) == doctor.CommandResult(0, b'out\n', b'err\n')
