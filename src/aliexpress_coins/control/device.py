"""The only door to a synthetic tap.

`InputDevice` is deliberately one method wide: a controller decides where and
why, and a device turns that decision into ``adb shell input tap <x> <y>``.
Tests inject a recording fake, so no test spawns adb (docs/03-conventions.md).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from aliexpress_coins.control.errors import InputError

_PREVIEW_BYTES = 200


@dataclass(frozen=True, slots=True)
class InputResult:
    """The raw bytes and exit status of one input invocation."""

    returncode: int
    stdout: bytes = b''
    stderr: bytes = b''


InputRunner = Callable[[Sequence[str], float], InputResult]
"""The transport seam: a callable that runs one adb argv and returns bytes."""


class SubprocessInputRunner:
    """The real transport, used outside tests."""

    def __call__(self, args: Sequence[str], timeout: float) -> InputResult:
        completed = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
        return InputResult(completed.returncode, completed.stdout, completed.stderr)


class InputDevice(Protocol):
    """One method that synthesises a tap at a screen pixel."""

    def tap(self, x: int, y: int) -> None:
        """Tap the screen at ``(x, y)``."""
        ...


class KeyDevice(Protocol):
    """An input device that can press a named Android key.

    Kept separate from :class:`InputDevice` so a tap-only fake stays a valid
    device. The controller checks for this capability only on the paths that
    need a key (the surprise-item detail page's BACK, for example), and raises
    a clear input error rather than silently doing nothing.
    """

    def keyevent(self, key: str) -> None:
        """Press ``key`` (a ``KEYCODE_*`` name)."""
        ...


class SwipeDevice(Protocol):
    """An input device that can drag between two screen points."""

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        """Swipe from ``(x1, y1)`` to ``(x2, y2)`` over ``duration_ms``."""
        ...


def _preview(data: bytes) -> str:
    if len(data) <= _PREVIEW_BYTES:
        return repr(data)
    return f'{data[:_PREVIEW_BYTES]!r}... ({len(data)} bytes)'


class AdbInputDevice:
    """Synthesise taps on one configured adb serial."""

    def __init__(
        self,
        serial: str,
        *,
        runner: InputRunner | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not serial.strip():
            raise ValueError('an adb serial is required')
        if timeout <= 0:
            raise ValueError('the adb timeout must be positive')
        self._serial = serial
        self._runner = runner if runner is not None else SubprocessInputRunner()
        self._timeout = timeout

    def tap(self, x: int, y: int) -> None:
        """Run ``adb -s <serial> shell input tap <x> <y>``."""
        self._run_input('tap', str(x), str(y))

    def keyevent(self, key: str) -> None:
        """Run ``adb -s <serial> shell input keyevent <KEYCODE_* >``."""
        if not key.strip():
            raise ValueError('a key name is required')
        self._run_input('keyevent', key)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        """Run ``adb -s <serial> shell input swipe <x1> <y1> <x2> <y2> <ms>``."""
        if duration_ms <= 0:
            raise ValueError(f'the swipe duration must be positive, got {duration_ms!r}')
        self._run_input('swipe', str(x1), str(y1), str(x2), str(y2), str(duration_ms))

    def _run_input(self, *command: str) -> None:
        args = ('adb', '-s', self._serial, 'shell', 'input', *command)
        try:
            result = self._runner(args, self._timeout)
        except subprocess.TimeoutExpired as exc:
            raise InputError(f'adb input timed out after {self._timeout:g}s: {" ".join(args)}') from exc
        except OSError as exc:
            raise InputError(f'could not run adb input: {exc}') from exc

        if result.returncode != 0:
            raise InputError(
                f'adb input {command[0]} exited {result.returncode}: '
                f'{_preview(result.stdout)} {_preview(result.stderr)}',
            )
