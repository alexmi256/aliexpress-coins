"""Autouse guards that keep the suite honest (docs/03-conventions.md).

"Tests never spawn a process or open a socket — a test that reaches the real
phone 'passes' while testing nothing." These fixtures turn an accidental real
connection or spawn into a loud failure instead of a false pass, for every
test in the suite, without any test having to opt in.
"""

import socket
import subprocess

import pytest


@pytest.fixture(autouse=True)
def _no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse any socket connection from a test."""

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError('tests must not open sockets (docs/03-conventions.md)')

    monkeypatch.setattr(socket.socket, 'connect', _refuse)


@pytest.fixture(autouse=True)
def _no_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse any process spawn from a test (covers Popen, run, check_output, ...)."""

    def _refuse(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
        raise AssertionError('tests must not spawn processes (docs/03-conventions.md)')

    # The tripwire deliberately stands a function in for the Popen class.
    monkeypatch.setattr(subprocess, 'Popen', _refuse)
