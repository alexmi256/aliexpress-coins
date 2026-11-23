"""The live device's pre-run hygiene and the two Android routes (issues #29, #32).

This is the only module that knows how to wake the tablet, prove it answers,
start the app normally and fire the measured shortcut intent. It is deliberately
thin: taps still go through :class:`~aliexpress_coins.control.controller.Controller`,
captures still go through ``AdbSnapshotter``, and tests inject a runner seam so
nothing here spawns a process.

The order is measured, not cosmetic (docs/07 §1): the shortcut intent is a
*navigation* command, not a launch command. Firing it at a cold app produces the
empty boost page, so ``prepare`` starts ``FirstActivity`` first, lets the caller's
clock settle, and only then does :meth:`AdbDevice.navigate` fire the route.

Hygiene, in the order it runs, is:

1. prove the serial answers (and repair a network serial once);
2. read ``dumpsys power`` so the report can say whether the device was asleep;
3. pin the measured portrait rotation before any coordinate is used;
4. wake with ``KEYCODE_WAKEUP``, dismiss the keyguard and swipe up;
5. refuse to continue if a keyguard still holds the screen (a PIN/biometric
   lock cannot be driven; the brief assumes none, so this is reported);
6. keep the screen awake for the run with ``svc power stayon true``;
7. prove the app package exists and its launch activity starts.

``svc power stayon true`` is chosen over a periodic ``input keyevent`` because it
is one write instead of a timer that competes with executor sleeps, survives a
slow task without injecting taps, and is restored with the previous numeric
setting just like rotation. The previous value must be readable; an unreadable
setting is left alone rather than overwritten. :meth:`AdbDevice.restore` puts
back both settings and is called in the engine's ``finally``, before delivery.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from aliexpress_coins.perception.errors import SnapshotError
from aliexpress_coins.perception.geometry import parse_wm_size
from aliexpress_coins.runner.errors import (
    AppLaunchFailed,
    AppNotInstalled,
    DeviceError,
    DeviceLocked,
    DeviceSleeping,
    DeviceUnavailable,
)
from aliexpress_coins.tasks.surface import SHORTCUT_DEEP_LINK, SurfaceRoute
from aliexpress_coins.transport import AdbResult, AdbRunner, SubprocessAdbRunner

__all__ = [
    'APP_ACTIVITY',
    'APP_PACKAGE',
    'AdbDevice',
    'CommandRunner',
    'DeviceCommandResult',
    'HygieneReport',
    'SubprocessCommandRunner',
]

DeviceCommandResult = AdbResult
"""The shared transport result; the device vocabulary keeps its old name."""

CommandRunner = AdbRunner
"""The shared adb transport seam; the device vocabulary keeps its old name."""

SubprocessCommandRunner = SubprocessAdbRunner
"""The real transport; the device vocabulary keeps its old name."""

APP_PACKAGE = 'com.alibaba.aliexpresshd'
"""The measured AliExpress package."""

APP_ACTIVITY = f'{APP_PACKAGE}/com.aliexpress.app.FirstActivity'
"""The normal launcher activity; starting it warms the process the route needs."""

_PREVIEW_BYTES = 200
RESTORE_TIMEOUT_SECONDS = 1.5
"""A cleanup write gets a short bound so it cannot outlive the container grace."""

SWIPE_DURATION_MS = 200
"""The keyguard swipe is a gesture, not a tap; short enough to be a flick upward."""

WAKE_SETTLE_SECONDS = 0.5
"""A bounded pause before re-reading power state after a sleeping start."""


class HygieneDevice(Protocol):
    """What the engine needs from the physical side before the first capture."""

    def prepare(self) -> HygieneReport:
        """Wake the device, prove it answers and warm the app."""
        ...

    def check_connected(self) -> None:
        """Prove the serial still answers, or raise ``DeviceUnavailable``."""
        ...

    def navigate(self, route: SurfaceRoute) -> None:
        """Fire one navigation route (the app is already warm)."""
        ...

    def restore(self) -> None:
        """Restore any device setting this hygiene pass changed."""
        ...


@dataclass(frozen=True, slots=True)
class HygieneReport:
    """The evidence that the run may touch the device."""

    serial: str
    state: str
    awake: bool
    app_started: bool
    detail: str
    was_asleep: bool = False
    kept_awake: bool = False
    app_installed: bool = True

    def as_dict(self) -> dict[str, object]:
        """Render the hygiene step for ``run.json``."""
        return {
            'serial': self.serial,
            'state': self.state,
            'awake': self.awake,
            'app_started': self.app_started,
            'was_asleep': self.was_asleep,
            'kept_awake': self.kept_awake,
            'app_installed': self.app_installed,
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
        self._restore_rotation: tuple[str, str] | None = None
        self._restore_stay_on: str | None = None

    def prepare(self) -> HygieneReport:
        """Connect if needed, wake the screen, pin rotation and warm the app.

        A network serial is connected once when ``get-state`` does not already
        report ``device``; this is the same repair ``doctor`` performs, and it
        is read-only with respect to the app. A serial that still does not answer
        raises :class:`DeviceUnavailable`, which the taxonomy names
        ``device_offline``.
        """
        state, detail = self._state()
        if state is None and _is_network_serial(self.serial):
            connected = self._run(('adb', 'connect', self.serial))
            connect_detail = _text(connected)
            state, detail = self._state()
            if state is None:
                raise DeviceUnavailable(f'{self.serial}: {detail}; adb connect: {connect_detail}')
        if state is None:
            raise DeviceUnavailable(f'{self.serial}: {detail}')

        was_asleep = _screen_asleep(self._shell_ok('dumpsys', 'power'))
        size = self._shell('wm', 'size', why='read the panel orientation before capture')
        self._pin_portrait(size)

        # A PIN/biometric keyguard cannot be entered by this collector. Wake and
        # swipe once (the reference device has no lock, Q2), then prove it is
        # gone rather than tapping into a screen that is still restricted.
        try:
            self._shell('input', 'keyevent', 'KEYCODE_WAKEUP', why='wake the screen before a run')
        except DeviceError as exc:
            raise DeviceSleeping(f'{self.serial}: the screen did not wake: {exc}') from exc
        if was_asleep:
            # A successful keyevent only means adb accepted the command. Re-read
            # the power state after a bounded pause so a device that stayed dark
            # is named `device_sleeping`, not reported awake.
            self._settle(WAKE_SETTLE_SECONDS)
            if _screen_asleep(self._shell_ok('dumpsys', 'power')):
                raise DeviceSleeping(f'{self.serial}: the screen was still asleep after KEYCODE_WAKEUP')
        # Dismissing a keyguard is best-effort: the reference device has no PIN
        # (Q2), but the command can fail on a build that does not implement it.
        self._shell_ok('wm', 'dismiss-keyguard')
        self._swipe_up(size)
        if _keyguard_showing(self._shell_ok('dumpsys', 'window')):
            raise DeviceLocked(
                f'{self.serial}: the keyguard is still showing after wake and swipe; '
                'a PIN or biometric lock cannot be driven by this collector',
            )

        kept_awake = self._keep_awake()

        package = self._run(('adb', '-s', self.serial, 'shell', 'pm', 'path', APP_PACKAGE))
        if package.returncode != 0:
            # Distinguish an offline serial from a genuinely absent package:
            # the exit status alone cannot, but get-state can.
            self.check_connected()
        if not _package_installed(_text(package)):
            raise AppNotInstalled(
                f'{self.serial}: {APP_PACKAGE} is not installed (pm path said {_text(package).strip()!r})',
            )

        try:
            started = self._shell('am', 'start', '-n', APP_ACTIVITY, why='warm the app before the route')
        except DeviceError as exc:
            self.check_connected()
            raise AppLaunchFailed(f'{self.serial}: {APP_ACTIVITY} could not be started: {exc}') from exc
        if _launch_failed(started):
            raise AppLaunchFailed(f'{self.serial}: {APP_ACTIVITY} did not start: {started.strip()}')
        if self._app_settle_seconds:
            self._settle(self._app_settle_seconds)
        sleep_note = 'device was asleep' if was_asleep else 'device was awake'
        awake_note = 'stay-awake on' if kept_awake else 'stay-awake not changed'
        return HygieneReport(
            serial=self.serial,
            state=state,
            awake=True,
            app_started=True,
            was_asleep=was_asleep,
            kept_awake=kept_awake,
            app_installed=True,
            detail=f'woke {self.serial} ({sleep_note}; {awake_note}) and started {APP_ACTIVITY}; '
            f'launcher said {started.strip() or "ok"}',
        )

    def check_connected(self) -> None:
        """Prove the serial still answers; used when a mid-run capture fails."""
        state, detail = self._state()
        if state is None:
            raise DeviceUnavailable(f'{self.serial}: {detail}')

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

    def restore(self) -> None:
        """Put the stay-awake and rotation settings back exactly as they were.

        Every restore is attempted even when an earlier one fails, so a failed
        rotation write cannot leave the tablet permanently awake. The pending
        values are kept for a later attempt; only a successful write clears one.
        A failure is reported as :class:`DeviceError`.
        """
        failures: list[str] = []
        if self._restore_stay_on is not None:
            try:
                self._shell(
                    'settings',
                    'put',
                    'global',
                    'stay_on_while_plugged_in',
                    self._restore_stay_on,
                    why='restore the previous stay-awake setting',
                    timeout=RESTORE_TIMEOUT_SECONDS,
                )
            except DeviceError as exc:
                failures.append(str(exc))
            else:
                self._restore_stay_on = None
        if self._restore_rotation is not None:
            auto, rotation = self._restore_rotation
            rotation_ok = True
            try:
                self._shell(
                    'settings',
                    'put',
                    'system',
                    'accelerometer_rotation',
                    auto,
                    why='restore the previous auto-rotation setting',
                    timeout=RESTORE_TIMEOUT_SECONDS,
                )
            except DeviceError as exc:
                failures.append(str(exc))
                rotation_ok = False
            try:
                self._shell(
                    'settings',
                    'put',
                    'system',
                    'user_rotation',
                    rotation,
                    why='restore the previous screen rotation',
                    timeout=RESTORE_TIMEOUT_SECONDS,
                )
            except DeviceError as exc:
                failures.append(str(exc))
                rotation_ok = False
            if rotation_ok:
                self._restore_rotation = None
        if failures:
            raise DeviceError('; '.join(failures))

    def _pin_portrait(self, size_output: str) -> None:
        """Lock the measured portrait geometry before a swipe or capture.

        The app renders a portrait column but the panel's natural orientation is
        landscape. Auto-rotation turned the screenshot into a letterboxed
        2000x1200 frame with black side bars, which the popup classifier
        correctly called a dimmed scrim. The rotation is derived from `wm size`.
        """
        rotation = _portrait_rotation(size_output)
        if rotation is None:
            return
        previous_auto = _setting_value(self._shell_ok('settings', 'get', 'system', 'accelerometer_rotation'))
        previous_rotation = _setting_value(self._shell_ok('settings', 'get', 'system', 'user_rotation'))
        # Never change a shared device setting unless both previous values are
        # known and can be put back. An unreadable setting is not a reason to
        # leave the tablet locked in a different rotation. A retry of prepare
        # must not overwrite the original values with our own writes.
        if previous_auto is not None and previous_rotation is not None:
            if self._restore_rotation is None:
                self._restore_rotation = (previous_auto, previous_rotation)
            self._shell_ok('settings', 'put', 'system', 'accelerometer_rotation', '0')
            self._shell_ok('settings', 'put', 'system', 'user_rotation', str(rotation))

    def _swipe_up(self, size_output: str) -> None:
        """Swipe from the lower third to the upper third in portrait coordinates."""
        try:
            width, height = parse_wm_size(size_output)
        except SnapshotError:
            return
        frame_width, frame_height = min(width, height), max(width, height)
        x = frame_width // 2
        start_y = int(frame_height * 0.85)
        end_y = int(frame_height * 0.25)
        self._shell_ok(
            'input',
            'swipe',
            str(x),
            str(start_y),
            str(x),
            str(end_y),
            str(SWIPE_DURATION_MS),
        )

    def _keep_awake(self) -> bool:
        """Turn on stay-awake, remembering the previous numeric setting."""
        previous = _setting_value(self._shell_ok('settings', 'get', 'global', 'stay_on_while_plugged_in'))
        if previous is None:
            return False
        if self._restore_stay_on is None:
            self._restore_stay_on = previous
        result = self._run(('adb', '-s', self.serial, 'shell', 'svc', 'power', 'stayon', 'true'))
        if result.returncode != 0:
            # A failed write is tolerable, but a serial that has gone away is
            # not: name it here rather than let the next probe misread it.
            self.check_connected()
        return result.returncode == 0

    def _state(self) -> tuple[str | None, str]:
        result = self._run(('adb', '-s', self.serial, 'get-state'))
        output = _text(result).strip()
        if result.returncode != 0 or output != 'device':
            return None, output or f'adb exited {result.returncode}'
        return output, output

    def _shell(self, *command: str, why: str, timeout: float | None = None) -> str:
        args = ('adb', '-s', self.serial, 'shell', *command)
        result = self._run(args, timeout=timeout)
        if result.returncode != 0:
            raise DeviceError(f'{why}: adb {" ".join(command)} exited {result.returncode}: {_text(result)}')
        return _text(result)

    def _shell_ok(self, *command: str) -> str:
        args = ('adb', '-s', self.serial, 'shell', *command)
        result = self._run(args)
        return _text(result)

    def _run(self, args: Sequence[str], *, timeout: float | None = None) -> DeviceCommandResult:
        effective = self._timeout if timeout is None else timeout
        try:
            return self._runner(args, effective)
        except subprocess.TimeoutExpired as exc:
            raise DeviceError(f'adb timed out after {effective:g}s: {" ".join(args)}') from exc
        except OSError as exc:
            raise DeviceError(f'could not run adb: {exc}') from exc


def _portrait_rotation(size_output: str) -> int | None:
    """Choose the rotation that makes the screencap taller than it is wide.

    ``wm size`` reports the panel's physical (or overridden) dimensions, not the
    current rotation. A landscape panel needs ``user_rotation=1`` to produce the
    measured portrait 1200x2000 frame; an already-portrait panel needs 0. An
    unparseable output is left alone rather than guessed at. ``parse_wm_size``
    already prefers an override line over the physical one.
    """
    try:
        width, height = parse_wm_size(size_output)
    except SnapshotError:
        return None
    return 1 if width > height else 0


def _setting_value(output: str) -> str | None:
    value = output.strip()
    return value if value.isdigit() else None


def _screen_asleep(output: str) -> bool:
    """Read ``dumpsys power`` without pretending an absent line proves sleep."""
    for line in output.splitlines():
        stripped = line.strip()
        if 'mWakefulness=' in stripped:
            value = stripped.partition('mWakefulness=')[2].strip()
            return value in {'Asleep', 'Dozing', 'Sleeping'}
    return False


def _keyguard_showing(output: str) -> bool:
    """Whether a keyguard is still up; an absent key is not evidence of a lock."""
    keys = ('isKeyguardShowing=', 'mDreamingLockscreen=', 'mKeyguardShowing=')
    for line in output.splitlines():
        stripped = line.strip()
        for key in keys:
            if key in stripped and stripped.partition(key)[2].strip().casefold() == 'true':
                return True
    return False


def _package_installed(output: str) -> bool:
    return any(line.strip().startswith('package:') for line in output.splitlines())


def _launch_failed(output: str) -> bool:
    text = output.casefold()
    return 'error' in text or 'does not exist' in text or 'unable to find explicit activity class' in text


def _is_network_serial(serial: str) -> bool:
    host, separator, port = serial.rpartition(':')
    return bool(host) and separator == ':' and port.isdigit()


def _text(result: DeviceCommandResult) -> str:
    raw = result.stdout or result.stderr
    if len(raw) <= _PREVIEW_BYTES:
        return raw.decode('utf-8', errors='replace')
    return f'{raw[:_PREVIEW_BYTES].decode("utf-8", errors="replace")}... ({len(raw)} bytes)'
