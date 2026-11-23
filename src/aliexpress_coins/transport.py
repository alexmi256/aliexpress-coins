"""The one adb transport seam (issue #36).

Everything in this project that runs ``adb`` takes an :data:`AdbRunner`: a
callable that receives one argv and a timeout and returns an :class:`AdbResult`.
The production implementation, :class:`SubprocessAdbRunner`, is the only object
that calls :func:`subprocess.run` for a device command. Tests inject the
scripted runner from ``tests/support/adb.py``, so the argv construction, output
parsing and error paths are still exercised by the real client while no process
is ever spawned.

Before this module there were five look-alike seams (``InputRunner``,
``AdbRunner``, ``CommandRunner`` twice and ``ProbeRunner``) with three result
types. A test fake written for one could not be passed to another, which is how
"every test rolls its own adb fake" happened. The names below are the seam; the
old names remain as aliases in the modules that used to own them so callers do
not silently acquire a different contract.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

__all__ = [
    'AdbResult',
    'AdbRunner',
    'SubprocessAdbRunner',
]


@dataclass(frozen=True, slots=True)
class AdbResult:
    """The raw bytes and exit status of one adb invocation."""

    returncode: int
    stdout: bytes = b''
    stderr: bytes = b''

    @property
    def stdout_text(self) -> str:
        """``stdout`` decoded for the commands whose output is text."""
        return self.stdout.decode('utf-8', errors='replace')

    @property
    def stderr_text(self) -> str:
        """``stderr`` decoded for the commands whose output is text."""
        return self.stderr.decode('utf-8', errors='replace')


AdbRunner = Callable[[Sequence[str], float], AdbResult]
"""The transport seam: a callable that runs one adb argv and returns bytes."""


class SubprocessAdbRunner:
    """The real transport, used outside tests."""

    def __call__(self, args: Sequence[str], timeout: float) -> AdbResult:
        completed = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
        return AdbResult(completed.returncode, completed.stdout, completed.stderr)
