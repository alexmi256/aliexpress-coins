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
        lambda s: s.bind(('127.0.0.1', 0)),
        lambda s: s.connect(('127.0.0.1', 1)),
        lambda s: s.connect_ex(('127.0.0.1', 1)),
        lambda s: s.sendmsg([b'probe'], [], 0, ('127.0.0.1', 1)),
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


def test_socket_listen_guard_fires() -> None:
    """listen() has no audit event, so the fixture patches it directly."""
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s,
        pytest.raises(AssertionError, match='must not open sockets'),
    ):
        s.listen(1)


@pytest.mark.parametrize(
    'operation',
    [
        lambda: subprocess.run(['true']),
        lambda: os.system('true'),
        lambda: os.spawnv(os.P_WAIT, '/bin/true', ['true']),
        lambda: os.execv('/nonexistent', ['nonexistent']),
        lambda: os.posix_spawn('/nonexistent', ['nonexistent'], os.environ),
        lambda: Popen(['true']),
    ],
)
def test_process_guard_fires(operation: Callable[[], object]) -> None:
    """The guard must refuse common process-spawn bypasses, not just subprocess.run."""
    with pytest.raises(AssertionError, match='must not spawn processes'):
        operation()


def _fork_and_reap() -> int:
    """Fork once and reap the child, so a failed guard cannot leave a process behind."""
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    return pid


def _forkpty_and_reap() -> int:
    """Open a pty and reap the child, so a failed guard cannot leave one behind."""
    pid, fd = os.forkpty()
    if pid == 0:
        os._exit(0)
    os.close(fd)
    os.waitpid(pid, 0)
    return pid


def test_os_fork_guard_fires() -> None:
    """os.fork is the path os.spawn* takes on POSIX; the child must never start."""
    with pytest.raises(AssertionError, match='must not spawn processes'):
        _fork_and_reap()


def test_os_forkpty_guard_fires() -> None:
    """forkpty() is a separate spawn path and must be refused too."""
    with pytest.raises(AssertionError, match='must not spawn processes'):
        _forkpty_and_reap()
