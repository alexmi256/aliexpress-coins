"""Command-line entry point for the collector (issue #34)."""

from __future__ import annotations

import argparse
import os
import signal
import threading
from collections.abc import Sequence
from types import FrameType

from aliexpress_coins import __version__
from aliexpress_coins.doctor import run_doctor


def run_idle() -> int:
    """Block until SIGTERM or SIGINT.

    This is the container smoke-test command: the scheduler that replaces it
    lands in #29, but the signal handling is already the real behaviour a
    `docker stop` relies on.
    """
    stop = threading.Event()

    def _request_stop(signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    print('collector idle: send SIGTERM to stop', flush=True)
    while not stop.wait(1.0):
        pass
    print('collector stopped', flush=True)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line and dispatch one subcommand."""
    parser = argparse.ArgumentParser(prog='aliexpress-coins')
    subparsers = parser.add_subparsers(dest='command')
    subparsers.add_parser('version', help='print the installed version')
    subparsers.add_parser('doctor', help='run read-only pre-flight checks')
    subparsers.add_parser('idle', help='block until SIGTERM (container smoke test)')
    args = parser.parse_args(argv)

    if args.command == 'version':
        print(__version__)
        return 0
    if args.command == 'doctor':
        return run_doctor(os.environ)
    if args.command == 'idle':
        return run_idle()

    parser.print_help()
    return 2
