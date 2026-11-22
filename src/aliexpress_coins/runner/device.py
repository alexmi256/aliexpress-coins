"""The live device's pre-run hygiene and the two Android routes (issue #29).

This is the only module that knows how to wake the tablet, prove it answers,
start the app normally and fire the measured shortcut intent. It is deliberately
thin: taps still go through :class:`~aliexpress_coins.control.controller.Controller`,
captures still go through ``AdbSnapshotter``, and tests inject a runner seam so
nothing here spawns a process.

The order is measured, not cosmetic (docs/06 §1): the shortcut intent is a
*navigation* command, not a launch command. Firing it at a cold app produces the
empty boost page, so ``prepare`` starts ``FirstActivity`` first, lets the caller's
clock settle, and only then does :meth:`AdbDevice.navigate` fire the route.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from aliexpress_coins.runner.errors import DeviceError
from aliexpress_coins.tasks.surface import SHORTCUT_DEEP_LINK, SurfaceRoute

__all__ = [
    'APP_ACTIVITY',
    'APP_PACKAGE',
    'AdbDevice',
    'CommandRunner',
    'DeviceCommandResult',
    'HygieneReport',
    'SubprocessCommandRunner',
]

APP_PACKAGE = 'com.alibaba.aliexpresshd'
"""The measured AliExpress package."""

APP_ACTIVITY = f'{APP_PACKAGE}/com.aliexpress.app.FirstActivity'
"""The normal launcher activity; starting it warms the process the route needs."""

_PREVIEW_BYTES = 200


@dataclass(frozen=True, slots=True)
class DeviceCommandResult:
    """The bytes and exit status of one adb command."""

    returncode: int
    stdout: bytes = b''
    stderr: bytes = b''


CommandRunner = Callable[[Sequence[str], float], DeviceCommandResult]
"""The transport seam: a callable that runs one adb argv."""


class SubprocessCommandRunner:
    """Run the real adb command outside tests."""

    def __call__(self, args: Sequence[str], timeout: float) -> DeviceCommandResult:
        completed = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
        return DeviceCommandResult(completed.returncode, completed.stdout, completed.stderr)


class HygieneDevice(Protocol):
    """What the engine needs from the physical side before the first capture."""

    def prepare(self) -> HygieneReport:
        """Wake the device, prove it answers and warm the app."""
        ...

    def navigate(self, route: SurfaceRoute) -> None:
        """Fire one navigation route (the app is already warm)."""
        ...


@dataclass(frozen=True, slots=True)
class HygieneReport:
    """The evidence that the run may touch the device."""

    serial: str
    state: str
    awake: bool
    app_started: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        """Render the hygiene step for ``run.json``."""
        return {
            'serial': self.serial,
            'state': self.state,
            'awake': self.awake,
            'app_started': self.app_started,
            'detail': self.detail,
        }


class AdbDevice:
    """Prepare and navigate one configured adb serial."""

    def __init__(
        self,
        serial: str,
        *,
        runner: CommandRunner | None = None,
        timeout: float = 20.0,
        app_settle_seconds: float = 10.0,
        settle: Callable[[float], None] = time.sleep,
    ) -> None:
        if not serial.strip():
            raise ValueError('an adb serial is required')
        if timeout <= 0:
            raise ValueError(f'the adb timeout must be positive, got {timeout!r}')
        if app_settle_seconds < 0:
            raise ValueError(f'app_settle_seconds must not be negative, got {app_settle_seconds!r}')
        self.serial = serial
        self._runner = runner if runner is not None else SubprocessCommandRunner()
        self._timeout = timeout
        self._app_settle_seconds = app_settle_seconds
        self._settle = settle

    def prepare(self) -> HygieneReport:
        """Connect if needed, wake the screen and warm the app.

        A network serial is connected once when ``get-state`` does not already
        report ``device``; this is the same repair ``doctor`` performs, and it
        is read-only with respect to the app.
        """
        state, detail = self._state()
        if state is None and _is_network_serial(self.serial):
            connected = self._run(('adb', 'connect', self.serial))
            connect_detail = _text(connected)
            state, detail = self._state()
            if state is None:
                raise DeviceError(f'{self.serial}: {detail}; adb connect: {connect_detail}')
        if state is None:
            raise DeviceError(f'{self.serial}: {detail}')

        self._shell('input', 'keyevent', 'KEYCODE_WAKEUP', why='wake the screen before a run')
        # Dismissing a keyguard is best-effort: the reference device has no PIN
        # (Q2), but the command can fail on a build that does not implement it.
        self._shell_ok('wm', 'dismiss-keyguard')
        # The app renders a portrait column but the panel's natural orientation
        # is landscape. Auto-rotation turned the screenshot into a letterboxed
        # 2000x1200 frame with black side bars, which the popup classifier
        # correctly called a dimmed scrim. Pin the measured portrait geometry
        # before the first capture; the rotation is derived from `wm size`.
        size = self._shell('wm', 'size', why='read the panel orientation before capture')
        rotation = _portrait_rotation(size)
        if rotation is not None:
            self._shell_ok('settings', 'put', 'system', 'accelerometer_rotation', '0')
            self._shell_ok('settings', 'put', 'system', 'user_rotation', str(rotation))

        started = self._shell('am', 'start', '-n', APP_ACTIVITY, why='warm the app before the route')
        if self._app_settle_seconds:
            self._settle(self._app_settle_seconds)
        return HygieneReport(
            serial=self.serial,
            state=state,
            awake=True,
            app_started=True,
            detail=f'woke {self.serial} and started {APP_ACTIVITY}; launcher said {started.strip() or "ok"}',
        )

    def navigate(self, route: SurfaceRoute) -> None:
        """Fire the shortcut intent, or refuse an unimplemented route."""
        if route is not SurfaceRoute.SHORTCUT:
            raise DeviceError(f'the {route.value!r} route is not implemented (#27); use the shortcut route')
        self._shell(
            'am',
            'start',
            '-a',
            'android.intent.action.VIEW',
            '-d',
            SHORTCUT_DEEP_LINK,
            why='take the measured shortcut route to the coin page',
        )

    def _state(self) -> tuple[str | None, str]:
        result = self._run(('adb', '-s', self.serial, 'get-state'))
        output = _text(result).strip()
        if result.returncode != 0 or output != 'device':
            return None, output or f'adb exited {result.returncode}'
        return output, output

    def _shell(self, *command: str, why: str) -> str:
        args = ('adb', '-s', self.serial, 'shell', *command)
        result = self._run(args)
        if result.returncode != 0:
            raise DeviceError(f'{why}: adb {" ".join(command)} exited {result.returncode}: {_text(result)}')
        return _text(result)

    def _shell_ok(self, *command: str) -> str:
        args = ('adb', '-s', self.serial, 'shell', *command)
        result = self._run(args)
        return _text(result)

    def _run(self, args: Sequence[str]) -> DeviceCommandResult:
        try:
            return self._runner(args, self._timeout)
        except subprocess.TimeoutExpired as exc:
            raise DeviceError(f'adb timed out after {self._timeout:g}s: {" ".join(args)}') from exc
        except OSError as exc:
            raise DeviceError(f'could not run adb: {exc}') from exc


_SIZE = re.compile(r'(?P<width>\d+)\s*x\s*(?P<height>\d+)')


def _portrait_rotation(size_output: str) -> int | None:
    """Choose the rotation that makes the screencap taller than it is wide.

    ``wm size`` reports the panel's physical (or overridden) dimensions, not the
    current rotation. A landscape panel needs ``user_rotation=1`` to produce the
    measured portrait 1200x2000 frame; an already-portrait panel needs 0. An
    unparseable output is left alone rather than guessed at.
    """
    matches = list(_SIZE.finditer(size_output))
    if not matches:
        return None
    last = matches[-1]
    width = int(last.group('width'))
    height = int(last.group('height'))
    return 1 if width > height else 0


def _is_network_serial(serial: str) -> bool:
    host, separator, port = serial.rpartition(':')
    return bool(host) and separator == ':' and port.isdigit()


def _text(result: DeviceCommandResult) -> str:
    raw = result.stdout or result.stderr
    if len(raw) <= _PREVIEW_BYTES:
        return raw.decode('utf-8', errors='replace')
    return f'{raw[:_PREVIEW_BYTES].decode("utf-8", errors="replace")}... ({len(raw)} bytes)'
