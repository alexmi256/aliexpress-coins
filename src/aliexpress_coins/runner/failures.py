"""The named failure taxonomy, its retry policy and its stop conditions (#32).

A failure is named once here and consumed by the engine and the report. This
module deliberately holds no error hierarchy of its own: the engine classifies
the exceptions the existing layers already raise (``DeviceError``,
``StateTimeout``, ``PopupUnclosable``, ``SnapshotError``) into one of the codes
below, and the report records that code in ``run.json`` and ``summary.md``.

The table is the contract. #40 notifies on it; a new class is a change here, not
a new string somewhere in a log line.

====================  ==========  ==========  ==============================================
code                  stage       retry       stop condition
====================  ==========  ==========  ==============================================
device_offline        device      stage x1    abort the run after the retry
device_sleeping       device      stage x1    abort the run after the retry
device_locked         device      none        abort the run immediately (a human must unlock)
app_not_installed     device      none        abort the run immediately (pm path is absent)
app_launch_failed     device      stage x1    abort the run after the retry
unsupported_geometry  perception  none        abort the run immediately (refusal, not a fault)
frame_undecodable     perception  none        abort the run immediately (the device answered)
app_logged_out        account     none        abort the run immediately, at first detection
navigation_failed     route       none        the route's own warm re-entry; fall back and report
popup_unclosable      control     none        abort the run immediately (never tap a guess)
task_unverified       task        task x1     retry only when no action was taken; else report
unexpected_screen     safety      none        fail a task on the third sighting; abort only if recovery fails
dangerous_control     safety      none        abort on a short buy/checkout/cart/redeem caption while a task is running
balance_decreased     safety      none        abort: something spent coins without policy approval
budget_exhausted      run         none        stop the walk cleanly and report it
internal_error        run         none        abort the run and report the class
====================  ==========  ==========  ==============================================

"Stage" means the operation that was in flight: a hygiene pass, a route, or a
single capture. Retrying a *stage* is safe because it repeats a read or a
navigation, never a task that may already have tapped. "Task" means one more
dispatch of the same row, and it is spent only when the result carries no tap:
an executor that already acted may have changed the account, so an unproven
outcome is reported, never repeated.

The pause is exponential and injected: ``retry_delay(0) == 1.0`` and
``retry_delay(1) == 2.0`` seconds. Tests pass a clock/sleep pair, so the suite
never waits.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from aliexpress_coins.control.errors import PopupUnclosable
from aliexpress_coins.perception.errors import FrameDecodeError, SnapshotError, StateTimeout
from aliexpress_coins.runner.errors import (
    AppLaunchFailed,
    AppLoggedOut,
    AppNotInstalled,
    DeviceError,
    DeviceLocked,
    DeviceSleeping,
    RunInterrupted,
    UnsupportedGeometry,
)

__all__ = [
    'BACKOFF_BASE_SECONDS',
    'FAILURE_TABLE',
    'Failure',
    'FailureCode',
    'FailureSpec',
    'FailureStage',
    'RetryScope',
    'classify_exception',
    'failure',
    'retry_delay',
    'task_failure',
]

BACKOFF_BASE_SECONDS = 1.0
"""The first retry pause. Each further attempt doubles it."""


class FailureStage(StrEnum):
    """Where in one night the failure belongs."""

    DEVICE = 'device'
    PERCEPTION = 'perception'
    ACCOUNT = 'account'
    ROUTE = 'route'
    CONTROL = 'control'
    TASK = 'task'
    SAFETY = 'safety'
    RUN = 'run'


class RetryScope(StrEnum):
    """What one retry repeats, or that nothing is retried."""

    STAGE = 'stage'
    TASK = 'task'
    NONE = 'none'


class FailureCode(StrEnum):
    """The stable code written to ``run.json`` and the summary."""

    DEVICE_OFFLINE = 'device_offline'
    DEVICE_SLEEPING = 'device_sleeping'
    DEVICE_LOCKED = 'device_locked'
    APP_NOT_INSTALLED = 'app_not_installed'
    APP_LAUNCH_FAILED = 'app_launch_failed'
    UNSUPPORTED_GEOMETRY = 'unsupported_geometry'
    FRAME_UNDECODABLE = 'frame_undecodable'
    APP_LOGGED_OUT = 'app_logged_out'
    NAVIGATION_FAILED = 'navigation_failed'
    POPUP_UNCLOSABLE = 'popup_unclosable'
    TASK_UNVERIFIED = 'task_unverified'
    UNEXPECTED_SCREEN = 'unexpected_screen'
    DANGEROUS_CONTROL = 'dangerous_control'
    BALANCE_DECREASED = 'balance_decreased'
    BUDGET_EXHAUSTED = 'budget_exhausted'
    INTERNAL_ERROR = 'internal_error'


@dataclass(frozen=True, slots=True)
class FailureSpec:
    """One row of the table: identity, retry rule and the sentence a human reads."""

    code: FailureCode
    stage: FailureStage
    retry: RetryScope
    retries: int
    abort_run: bool
    sentence: str


FAILURE_TABLE: dict[FailureCode, FailureSpec] = {
    FailureCode.DEVICE_OFFLINE: FailureSpec(
        FailureCode.DEVICE_OFFLINE,
        FailureStage.DEVICE,
        RetryScope.STAGE,
        1,
        True,
        'The device stopped answering adb; the run stopped instead of waiting.',
    ),
    FailureCode.DEVICE_SLEEPING: FailureSpec(
        FailureCode.DEVICE_SLEEPING,
        FailureStage.DEVICE,
        RetryScope.STAGE,
        1,
        True,
        'The device was asleep and did not wake when asked.',
    ),
    FailureCode.DEVICE_LOCKED: FailureSpec(
        FailureCode.DEVICE_LOCKED,
        FailureStage.DEVICE,
        RetryScope.NONE,
        0,
        True,
        'The device is locked with a PIN or biometric; the collector cannot drive it.',
    ),
    FailureCode.APP_NOT_INSTALLED: FailureSpec(
        FailureCode.APP_NOT_INSTALLED,
        FailureStage.DEVICE,
        RetryScope.NONE,
        0,
        True,
        'The AliExpress app is not installed on the device.',
    ),
    FailureCode.APP_LAUNCH_FAILED: FailureSpec(
        FailureCode.APP_LAUNCH_FAILED,
        FailureStage.DEVICE,
        RetryScope.STAGE,
        1,
        True,
        'The app is installed but its launch activity did not start.',
    ),
    FailureCode.UNSUPPORTED_GEOMETRY: FailureSpec(
        FailureCode.UNSUPPORTED_GEOMETRY,
        FailureStage.PERCEPTION,
        RetryScope.NONE,
        0,
        True,
        'This screen geometry is not supported by the measured profile yet.',
    ),
    FailureCode.FRAME_UNDECODABLE: FailureSpec(
        FailureCode.FRAME_UNDECODABLE,
        FailureStage.PERCEPTION,
        RetryScope.NONE,
        0,
        True,
        'A screenshot was captured but could not be decoded.',
    ),
    FailureCode.APP_LOGGED_OUT: FailureSpec(
        FailureCode.APP_LOGGED_OUT,
        FailureStage.ACCOUNT,
        RetryScope.NONE,
        0,
        True,
        'The AliExpress account is signed out; a human must sign in before the next run.',
    ),
    FailureCode.NAVIGATION_FAILED: FailureSpec(
        FailureCode.NAVIGATION_FAILED,
        FailureStage.ROUTE,
        RetryScope.NONE,
        0,
        False,
        'The app did not reach the screen the route promised.',
    ),
    FailureCode.POPUP_UNCLOSABLE: FailureSpec(
        FailureCode.POPUP_UNCLOSABLE,
        FailureStage.CONTROL,
        RetryScope.NONE,
        0,
        True,
        'A popup could not be dismissed without guessing at a tap.',
    ),
    FailureCode.TASK_UNVERIFIED: FailureSpec(
        FailureCode.TASK_UNVERIFIED,
        FailureStage.TASK,
        RetryScope.TASK,
        1,
        False,
        'The task did not prove its completion on screen; it is not recorded as done.',
    ),
    FailureCode.UNEXPECTED_SCREEN: FailureSpec(
        FailureCode.UNEXPECTED_SCREEN,
        FailureStage.SAFETY,
        RetryScope.NONE,
        0,
        True,
        'The same screen was not understood three times in a row and could not be recovered; the run stopped rather than guessing.',
    ),
    FailureCode.DANGEROUS_CONTROL: FailureSpec(
        FailureCode.DANGEROUS_CONTROL,
        FailureStage.SAFETY,
        RetryScope.NONE,
        0,
        True,
        'A short buy/checkout/cart/redeem action caption appeared while a task was running; '
        'the run stopped before it could be tapped.',
    ),
    FailureCode.BALANCE_DECREASED: FailureSpec(
        FailureCode.BALANCE_DECREASED,
        FailureStage.SAFETY,
        RetryScope.NONE,
        0,
        True,
        'The coin balance decreased, so something acted without the policy authorising it.',
    ),
    FailureCode.BUDGET_EXHAUSTED: FailureSpec(
        FailureCode.BUDGET_EXHAUSTED,
        FailureStage.RUN,
        RetryScope.NONE,
        0,
        False,
        'The run used its whole time or task budget; the remaining rows wait for tomorrow.',
    ),
    FailureCode.INTERNAL_ERROR: FailureSpec(
        FailureCode.INTERNAL_ERROR,
        FailureStage.RUN,
        RetryScope.NONE,
        0,
        True,
        'The run stopped on an unexpected internal error.',
    ),
}
"""The taxonomy contract: code -> stage, retry behaviour, stop condition, sentence."""


def retry_delay(attempt: int) -> float:
    """The exponential pause before retry number ``attempt`` (zero-based)."""
    if attempt < 0:
        raise ValueError(f'attempt must not be negative, got {attempt!r}')
    return float(BACKOFF_BASE_SECONDS * (2**attempt))


@dataclass(frozen=True, slots=True)
class Failure:
    """One occurrence of a named class, with the detail this run saw."""

    code: FailureCode
    stage: FailureStage
    retry: RetryScope
    retries: int
    abort_run: bool
    sentence: str
    detail: str = ''

    @property
    def retryable(self) -> bool:
        """Whether the policy spends a retry on this class."""
        return self.retries > 0

    def describe(self) -> str:
        """The one-line form used by the summary, the notification and the log."""
        text = f'{self.code.value}: {self.sentence}'
        return text if not self.detail else f'{text} ({self.detail})'

    def as_dict(self) -> dict[str, object]:
        """Render the failure for ``run.json`` without losing the contract."""
        return {
            'code': self.code.value,
            'stage': self.stage.value,
            'retry': self.retry.value,
            'retries': self.retries,
            'retryable': self.retryable,
            'abort_run': self.abort_run,
            'sentence': self.sentence,
            'detail': self.detail,
        }


def failure(code: FailureCode, *, detail: str = '') -> Failure:
    """Build one failure from the table, keeping the table authoritative."""
    spec = FAILURE_TABLE[code]
    return Failure(
        code=spec.code,
        stage=spec.stage,
        retry=spec.retry,
        retries=spec.retries,
        abort_run=spec.abort_run,
        sentence=spec.sentence,
        detail=detail,
    )


def classify_exception(exc: BaseException, *, default: FailureCode = FailureCode.INTERNAL_ERROR) -> Failure | None:
    """Name an existing exception, or ``None`` for a stop that must pass through.

    The layers already raise typed errors; this function is the one translation
    into the taxonomy. ``default`` lets a task dispatch call a generic exception
    ``task_unverified`` while the run-level catch calls it ``internal_error``.
    """
    if isinstance(exc, RunInterrupted):
        return None
    detail = _detail(exc)
    if isinstance(exc, AppLoggedOut):
        return failure(FailureCode.APP_LOGGED_OUT, detail=detail)
    if isinstance(exc, DeviceSleeping):
        return failure(FailureCode.DEVICE_SLEEPING, detail=detail)
    if isinstance(exc, DeviceLocked):
        return failure(FailureCode.DEVICE_LOCKED, detail=detail)
    if isinstance(exc, AppNotInstalled):
        return failure(FailureCode.APP_NOT_INSTALLED, detail=detail)
    if isinstance(exc, AppLaunchFailed):
        return failure(FailureCode.APP_LAUNCH_FAILED, detail=detail)
    if isinstance(exc, UnsupportedGeometry):
        return failure(FailureCode.UNSUPPORTED_GEOMETRY, detail=detail)
    if isinstance(exc, FrameDecodeError):
        return failure(FailureCode.FRAME_UNDECODABLE, detail=detail)
    if isinstance(exc, PopupUnclosable):
        return failure(FailureCode.POPUP_UNCLOSABLE, detail=detail)
    if isinstance(exc, StateTimeout):
        return failure(FailureCode.NAVIGATION_FAILED, detail=detail)
    if isinstance(exc, (DeviceError, SnapshotError)):
        return failure(FailureCode.DEVICE_OFFLINE, detail=detail)
    return failure(default, detail=detail)


def task_failure(exc: BaseException | str) -> Failure:
    """Name an executor failure: transport errors keep their device class."""
    if isinstance(exc, BaseException):
        classified = classify_exception(exc, default=FailureCode.TASK_UNVERIFIED)
        if classified is None:
            # A stop is re-raised by the caller before an executor failure is
            # named; keeping the exception type honest beats inventing a task.
            raise exc
        if classified.code is FailureCode.NAVIGATION_FAILED:
            # A page that did not open inside a task is that task's failure; the
            # run-level route has its own retry and fallback in the engine.
            return failure(FailureCode.TASK_UNVERIFIED, detail=classified.detail)
        return classified
    return failure(FailureCode.TASK_UNVERIFIED, detail=str(exc))


def _detail(exc: BaseException) -> str:
    text = str(exc)
    return f'{type(exc).__name__}: {text}' if text else type(exc).__name__
