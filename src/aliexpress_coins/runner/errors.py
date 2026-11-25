"""Errors raised by the runner layer."""

from __future__ import annotations


class RunnerError(Exception):
    """Base class for a runner-layer failure."""


class StateError(RunnerError):
    """The daily state could not be used as asked.

    This covers a caller mistake (an empty task key or a non-numeric balance),
    a state directory that cannot be created, and a state file whose contents
    cannot be read. A *corrupt* file is deliberately not an error: it is moved
    aside and the day starts fresh, because losing a night to a half-written
    file is the failure this module exists to prevent.
    """


class StateReadError(StateError):
    """A state file exists but could not be read from disk.

    Permission and device failures are environmental; unlike invalid JSON they
    are not evidence that the file is expendable, so they are reported.
    """


class StateWriteError(StateError):
    """A state update could not be committed atomically.

    The in-memory state is left unchanged: a caller that sees this error knows
    the previous state is still the one on disk.
    """


class RunLockHeld(StateError):
    """Another process is already running against this state directory.

    Two runs that share a state directory would both drive the same device and
    overwrite each other's completion counters, so the second one refuses
    instead of racing. The exception text names the holding process when its
    owner record can be read.
    """


class ConfigError(RunnerError):
    """The runner configuration is missing or invalid."""


class ReportError(RunnerError):
    """A run report could not be written."""


class NotificationError(RunnerError):
    """A run notification could not be delivered.

    Delivery is best-effort by contract: the caller records this error and
    exits on the run's own status. It never turns a good night into a failure.
    ``status`` keeps an HTTP status when the transport supplied one, so the
    report can distinguish a 503 from a DNS failure without recording the
    exception's message.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class DeviceError(RunnerError):
    """The device could not be reached or prepared for a run."""


class DeviceUnavailable(DeviceError):
    """adb cannot see the configured serial (the network went away)."""


class DeviceSleeping(DeviceError):
    """The serial answers but the screen did not wake when asked."""


class DeviceLocked(DeviceError):
    """The screen woke but a PIN or biometric keyguard still holds it.

    The brief assumes the tablet is left unlocked (Q2). When that assumption is
    false the collector must stop: it cannot enter a PIN and must never guess at
    one, so this is a run-level failure reported for a human to fix.
    """


class AppNotInstalled(DeviceError):
    """The configured package is absent from the device."""


class AppLaunchFailed(DeviceError):
    """The package exists but its launch activity did not start.

    A transient activity-manager failure after a reboot is retryable, unlike an
    absent package. Keeping the two apart stops the report telling an operator to
    reinstall an app that is already installed.
    """


class UnsupportedGeometry(DeviceError):
    """The frame does not match the measured coordinate/template profile.

    This is a deliberate refusal, not a transport failure: the device may be
    answering perfectly while running a screen the build does not support yet.
    """


class AppLoggedOut(DeviceError):
    """The app shows a sign-in prompt instead of the signed-in account.

    No task can be collected until a human signs in. The engine stops at the
    first detection rather than recording every task as failed, which would
    waste the night and misreport the cause.
    """


class RunInterrupted(RunnerError):
    """The run was asked to stop before it finished its walk.

    This is raised from the injected sleep/capture seams when the scheduler has
    received SIGTERM, so an executor's own broad ``except Exception`` cannot
    swallow the stop and keep tapping.
    """


class RunBudgetExceeded(RunInterrupted):
    """The wall-clock budget elapsed while a task was still running.

    The engine checks the budget between dispatches; this exception makes the
    same deadline active at a task's injected clock, sleep and capture seams.
    It is a :class:`RunInterrupted` so the executors' existing "never swallow a
    stop" handling also lets a budget stop unwind cleanly.
    """
