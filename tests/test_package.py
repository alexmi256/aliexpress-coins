"""Scaffold smoke tests: the package imports, and the suite guards actually bite."""

import os
import socket
import subprocess
from collections.abc import Callable
from subprocess import Popen

import pytest

import aliexpress_coins


def test_version_is_semver() -> None:
    parts = aliexpress_coins.__version__.split('.')
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)


@pytest.mark.parametrize(
    'operation',
    [
        lambda s: s.connect(('127.0.0.1', 1)),
        lambda s: s.connect_ex(('127.0.0.1', 1)),
        lambda s: s.sendto(b'probe', ('127.0.0.1', 1)),
    ],
)
def test_socket_guard_fires(operation: Callable[[socket.socket], object]) -> None:
    """The guard must refuse common socket-operation bypasses, not just connect."""
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s,
        pytest.raises(AssertionError, match='must not open sockets'),
    ):
        operation(s)


@pytest.mark.parametrize(
    'operation',
    [
        lambda: subprocess.run(['true']),
        lambda: os.system('true'),
        lambda: os.spawnv(os.P_WAIT, '/bin/true', ['true']),
        lambda: os.execv('/nonexistent', ['nonexistent']),
        lambda: Popen(['true']),
    ],
)
def test_process_guard_fires(operation: Callable[[], object]) -> None:
    """The guard must refuse common process-spawn bypasses, not just subprocess.run."""
    with pytest.raises(AssertionError, match='must not spawn processes'):
        operation()
