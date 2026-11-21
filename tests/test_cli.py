"""Tests for the command-line entry point (issue #34)."""

from __future__ import annotations

import importlib
import runpy
import signal
import sys
from collections.abc import Callable, Mapping
from types import FrameType

import pytest

import aliexpress_coins
from aliexpress_coins import cli

Handler = Callable[[int, FrameType | None], None]


def test_main_dispatches_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(['version']) == 0
    assert capsys.readouterr().out.strip() == aliexpress_coins.__version__


def test_main_dispatches_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_doctor(_environment: Mapping[str, str]) -> int:
        return 7

    monkeypatch.setattr(cli, 'run_doctor', fake_doctor)
    assert cli.main(['doctor']) == 7


def test_main_dispatches_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, 'run_idle', lambda: 0)
    assert cli.main(['idle']) == 0


def test_main_without_a_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert 'usage:' in capsys.readouterr().out


def test_module_entrypoint_runs_main(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, 'argv', ['aliexpress-coins'])
    with pytest.raises(SystemExit) as raised:
        runpy.run_module('aliexpress_coins', run_name='__main__')
    assert raised.value.code == 2
    assert 'usage:' in capsys.readouterr().out


def test_importing_the_entrypoint_does_not_run_the_cli() -> None:
    module = importlib.import_module('aliexpress_coins.__main__')
    assert module.__name__ == 'aliexpress_coins.__main__'


def test_run_idle_stops_on_sigterm(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    handlers: dict[int, Handler] = {}

    def fake_signal(signum: int, handler: Handler) -> None:
        handlers[signum] = handler
        if signum == signal.SIGTERM:
            handler(signum, None)

    monkeypatch.setattr(signal, 'signal', fake_signal)
    assert cli.run_idle() == 0
    assert set(handlers) == {signal.SIGTERM, signal.SIGINT}
    output = capsys.readouterr().out
    assert 'collector idle' in output
    assert 'collector stopped' in output
