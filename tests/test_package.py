"""Scaffold smoke tests: the package imports, and the suite guards actually bite."""

import socket
import subprocess

import pytest

import aliexpress_coins


def test_version_is_semver() -> None:
    parts = aliexpress_coins.__version__.split('.')
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)


def test_socket_guard_fires() -> None:
    """The autouse fixture must refuse a real connect, so a forgotten mock cannot pass."""
    with socket.socket() as s, pytest.raises(AssertionError, match='must not open sockets'):
        s.connect(('127.0.0.1', 1))


def test_process_guard_fires() -> None:
    """The autouse fixture must refuse a real spawn, so a forgotten mock cannot pass."""
    with pytest.raises(AssertionError, match='must not spawn processes'):
        subprocess.run(['true'])
