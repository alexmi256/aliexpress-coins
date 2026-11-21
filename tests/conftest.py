"""Autouse guards that keep the suite honest (docs/03-conventions.md).

"Tests never spawn a process or open a socket — a test that reaches the real
phone 'passes' while testing nothing." An audit hook turns an accidental real
operation into a loud failure for every test, whichever API it uses: a `Popen`
imported by name is caught as well as `subprocess.Popen`.
"""

import sys
from collections.abc import Iterator

import pytest

_SOCKET_EVENTS = frozenset(
    {
        'socket.bind',
        'socket.connect',
        'socket.listen',
        'socket.sendmsg',
        'socket.sendto',
    }
)
_PROCESS_EVENTS = frozenset(
    {
        'os.exec',
        'os.fork',
        'os.forkpty',
        'os.posix_spawn',
        'os.spawn',
        'os.system',
        'subprocess.Popen',
    }
)

_sockets_armed = False
_processes_armed = False


def _audit(event: str, _args: tuple[object, ...]) -> None:
    """Fail the test that performs a blocked operation."""
    if _sockets_armed and event in _SOCKET_EVENTS:
        raise AssertionError('tests must not open sockets (docs/03-conventions.md)')
    if _processes_armed and event in _PROCESS_EVENTS:
        raise AssertionError('tests must not spawn processes (docs/03-conventions.md)')


sys.addaudithook(_audit)


@pytest.fixture(autouse=True)
def _no_sockets() -> Iterator[None]:
    """Refuse socket use from a test."""
    global _sockets_armed
    _sockets_armed = True
    try:
        yield
    finally:
        _sockets_armed = False


@pytest.fixture(autouse=True)
def _no_processes() -> Iterator[None]:
    """Refuse process spawns from a test."""
    global _processes_armed
    _processes_armed = True
    try:
        yield
    finally:
        _processes_armed = False
