"""The notifications executor: turn the permission on without trusting a tap (#28).

``Turn on notifications`` is the smallest row in the roster and the only one that
can leave the app for an **Android runtime permission dialog**. That dialog is a
different window: it is not dismissible by the app's back gesture, and on the
reference device the row has credited its +1 without ever showing it. The
executor therefore treats the dialog as its own state instead of asking the
in-app popup sweep to close it.

The measured contract (``specs/flows.yaml``) has four destinations:

* **no dialog** — the row flips to the drawn check and the balance moves by +1
  (observed 2026-11-20); the permission may still be ``ignore``;
* **the app pre-prompt** — ``Receive timely notifications``. Its
  ``Enable all notifications`` control is a transition to the system dialog,
  not a dismissal;
* **the system dialog** — ``Allow AliExpress to send you notifications?`` with
  ``Allow`` and ``Don't allow``. This executor is the one task that wants the
  grant, so ``Allow`` is the intended control; a dialog that offers only a
  denial path is recorded as ``permission_denied``;
* **the OS settings page** — ``All AliExpress notifications``. A redirect there
  is ``routed_to_settings`` and the task stops. The executor never flips that
  switch, because changing system settings outside the permission is out of
  scope.

A denial is an outcome, not a crash. The task records it, proves the app is
still reachable and returns control to the run, so a user who says no does not
break the rest of the night's collection. The daily store is written for every
terminal outcome so a second run does not open the dialog again.

The module deliberately keeps two seams explicit: captures come from the
``capture`` argument and every tap goes through :class:`Controller`. The
permission itself is read through an optional injected probe
(:class:`AppOpsPermissionProbe` is the measured ``cmd appops`` implementation);
a fixture run supplies its own, so no test spawns adb.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from aliexpress_coins.control.controller import LOGGER_NAME, Controller, TapRecord
from aliexpress_coins.control.errors import PopupUnclosable
from aliexpress_coins.control.popups import DismissReport, PopupCorpus, _matching_family
from aliexpress_coins.perception.errors import StateTimeout
from aliexpress_coins.perception.matching import DEFAULT_MIN_OCR_SCORE, normalize
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.state import CaptureSource, Classification, ScreenState, classify
from aliexpress_coins.perception.templates import TemplateStore
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks.browse import ReturnResult, find_task_row, return_to_coins_page
from aliexpress_coins.tasks.catalogue import AllowedTaskSpec, TaskCategory
from aliexpress_coins.tasks.discovery import BoostList, RosterEntry, open_boost_list
from aliexpress_coins.tasks.errors import TaskError
from aliexpress_coins.tasks.surface import (
    COIN_PAGE_MARKERS,
    RouteFirer,
    SurfaceRoute,
    contains_text,
    has_task_rows,
    is_boost_page,
    is_empty_boost,
)

__all__ = [
    'ALLOW_LABELS',
    'APP_PROMPT_MARKER',
    'DEFAULT_ARRIVAL_TIMEOUT',
    'DEFAULT_EXIT_TIMEOUT',
    'DEFAULT_INTERVAL',
    'DEFAULT_LOAD_TIMEOUT',
    'DEFAULT_RETURN_ATTEMPTS',
    'DEFAULT_RETURN_INTERVAL',
    'DEFAULT_RETURN_TIMEOUT',
    'DENY_LABELS',
    'ENABLE_ALL_LABEL',
    'NOTIFICATIONS_SUBTITLE',
    'NOTIFICATIONS_TITLE',
    'NOTIFICATION_PACKAGE',
    'OS_SETTINGS_BLOCKED_MARKER',
    'OS_SETTINGS_MARKER',
    'SYSTEM_DIALOG_MARKER',
    'AppOpsPermissionProbe',
    'NotificationOutcome',
    'NotificationPermission',
    'NotificationResult',
    'NotificationScreen',
    'PermissionReading',
    'classify_notification_screen',
    'find_allow_control',
    'find_deny_control',
    'find_enable_all_control',
    'find_grant_control',
    'is_app_prompt',
    'is_os_settings',
    'is_system_dialog',
    'parse_permission_output',
    'run_notifications',
    'run_notifications_task',
]

NOTIFICATIONS_TITLE = 'Turn on notifications'
NOTIFICATIONS_SUBTITLE = 'Stay informed when rewards drop'

SYSTEM_DIALOG_MARKER = 'Allow AliExpress to send you notifications?'
"""The Android runtime POST_NOTIFICATIONS dialog's identifying sentence."""

APP_PROMPT_MARKER = 'Receive timely notifications'
"""The app's own pre-prompt, which can precede the system dialog."""

ENABLE_ALL_LABEL = 'Enable all notifications'
"""The app-prompt control that transitions to the system dialog."""

ALLOW_LABELS: tuple[str, ...] = ('Allow', 'Allow notifications')
"""Labels that grant the permission from the runtime dialog.

Matching is exact after normalisation: ``Allow`` must not match the word inside
the dialog's question (``Allow AliExpress to send you notifications?``).
"""

DENY_LABELS: tuple[str, ...] = ("Don't allow", 'No thanks', 'Not now', 'Deny', 'Cancel')
"""The controls that close a notification prompt without granting."""

OS_SETTINGS_MARKER = 'All AliExpress notifications'
"""The OS notification page reached by the app's own settings toggle."""

OS_SETTINGS_BLOCKED_MARKER = "You haven't allowed notifications from this app"
"""The OS page's own proof that the permission is still denied."""

AFTER_GRANT_MARKERS: tuple[str, ...] = ('Categories', 'Allow notification dot')
"""The OS page's category list appears after a grant."""

NOTIFICATION_PACKAGE = 'com.alibaba.aliexpresshd'
"""The measured AliExpress package for the ``appops`` permission probe."""

NOTIFICATION_PERMISSION = 'POST_NOTIFICATION'
"""The ``appops``/runtime name of the notification permission."""

NOTIFICATION_SWEEP_FAMILIES: frozenset[str] = frozenset(
    {
        'coins_boost_sheet',
        'system_notification_permission',
        'push_permission_prompt',
    },
)
"""Families the notification task handles itself.

The boost sheet is excluded because clearing it would close the list the task is
working from (the regression caught by #26). The two notification families are
excluded because the executor must classify the system dialog as its own state,
not as an in-app popup, and because ``Enable all notifications`` is a transition
rather than a dismissal.
"""

DEFAULT_LOAD_TIMEOUT = 15.0
"""How long the task surface may take to stop loading before the executor gives up."""

DEFAULT_ARRIVAL_TIMEOUT = 10.0
"""How long the dialog or completed row may take to appear after ``GO``."""

DEFAULT_EXIT_TIMEOUT = 10.0
"""How long a dialog may take to close after a control is tapped."""

DEFAULT_INTERVAL = 0.5
"""How often capture is polled while a prompt or page is settling."""

DEFAULT_RETURN_TIMEOUT = 20.0
"""How long one shortcut re-entry may take to show the coin-page markers."""

DEFAULT_RETURN_INTERVAL = 0.5
"""How often the return route is polled."""

DEFAULT_RETURN_ATTEMPTS = 2
"""The bounded number of shortcut re-entries spent trying to reach the coin page."""

_PERMISSION_ALLOW = re.compile(r'POST_NOTIFICATION:\s*(?:allow|allowed|granted)\b', re.IGNORECASE)
_PERMISSION_DENY = re.compile(r'POST_NOTIFICATION:\s*(?:ignore|ignored|deny|denied|denied)\b', re.IGNORECASE)
_PERMISSION_WORD = re.compile(r'\b(?P<word>allow(?:ed)?|grant(?:ed)?|ignore(?:d)?|deny|denied)\b', re.IGNORECASE)


class NotificationScreen(StrEnum):
    """The notification-specific screen a frame shows.

    This is deliberately separate from :class:`ScreenState`: the Android dialog
    classifies as ``POPUP`` and the OS page as ``PAGE``, but the executor needs
    to know which prompt it is looking at before it taps anything.
    """

    ENTRY_ROW = 'entry_row'
    APP_PROMPT = 'app_prompt'
    SYSTEM_DIALOG = 'system_dialog'
    OS_SETTINGS = 'os_settings'
    OS_SETTINGS_GRANTED = 'os_settings_granted'
    BOOST_LIST = 'boost_list'
    COIN_PAGE = 'coin_page'
    OTHER = 'other'


class NotificationPermission(StrEnum):
    """The OS permission as a probe reports it."""

    GRANTED = 'granted'
    DENIED = 'denied'
    UNKNOWN = 'unknown'


class NotificationOutcome(StrEnum):
    """What one notifications visit actually did."""

    COMPLETED = 'completed'
    ALREADY_DONE = 'already_done'
    PERMISSION_DENIED = 'permission_denied'
    ROUTED_TO_SETTINGS = 'routed_to_settings'
    UNAVAILABLE = 'unavailable'
    FAILED = 'failed'
    SKIPPED = 'skipped'


@dataclass(frozen=True, slots=True)
class PermissionReading:
    """One reading of the app's notification permission."""

    state: NotificationPermission
    raw: str | None = None
    source: str = 'appops'

    @property
    def granted(self) -> bool:
        """Whether the permission is positively held."""
        return self.state is NotificationPermission.GRANTED

    @property
    def denied(self) -> bool:
        """Whether the permission is positively not held."""
        return self.state is NotificationPermission.DENIED

    def as_dict(self) -> dict[str, object]:
        """Render the reading for ``run.json``."""
        return {'state': self.state.value, 'granted': self.granted, 'raw': self.raw, 'source': self.source}


@dataclass(frozen=True, slots=True)
class _ProbeResult:
    """The minimal shape a probe runner must return."""

    returncode: int = 0
    stdout: str = ''
    stderr: str = ''


ProbeRunner = Callable[[Sequence[str], float], Any]
"""The transport seam for the optional permission probe."""


class _SubprocessProbeRunner:
    """Run the real adb command outside tests."""

    def __call__(self, args: Sequence[str], timeout: float) -> _ProbeResult:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return _ProbeResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True, slots=True)
class NotificationResult:
    """One notifications visit, the evidence behind it and whether state changed.

    ``verification`` names the proof: ``action_completed`` or ``row_gone`` on
    the row path; ``permission_granted`` when the probe proved the grant;
    ``already_done`` when the row was already claimed; ``permission_denied``
    when the dialog or a failed grant left the permission off;
    ``routed_to_settings`` when the app sent the flow to OS settings;
    ``unavailable`` when the row was absent; ``daily_limit``, ``not_settled``,
    ``task_surface_unavailable``, ``no_change``, ``grant_not_observed`` or
    ``error`` on the others.
    """

    key: str
    title: str
    outcome: NotificationOutcome
    verification: str
    detail: str
    limit: int
    done_today_before: int
    done_today_after: int
    taps: tuple[TapRecord, ...] = ()
    dismissals: tuple[DismissReport, ...] = ()
    return_state: ReturnResult | None = None
    recorded: bool = False
    dialog_appeared: bool = False
    classification: str | None = None
    screen: str | None = None
    permission_before: PermissionReading | None = None
    permission_after: PermissionReading | None = None
    stop: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'taps', tuple(self.taps))
        object.__setattr__(self, 'dismissals', tuple(self.dismissals))

    @property
    def completed(self) -> bool:
        """Whether the row or the permission proved the task."""
        return self.outcome is NotificationOutcome.COMPLETED

    @property
    def already_done(self) -> bool:
        """Whether the row was already claimed today."""
        return self.outcome is NotificationOutcome.ALREADY_DONE

    @property
    def permission_denied(self) -> bool:
        """Whether a denial (or a failed grant) was recorded."""
        return self.outcome is NotificationOutcome.PERMISSION_DENIED

    @property
    def routed_to_settings(self) -> bool:
        """Whether the app redirected the flow to OS settings."""
        return self.outcome is NotificationOutcome.ROUTED_TO_SETTINGS

    @property
    def unavailable(self) -> bool:
        """Whether the row was absent or not on a task surface."""
        return self.outcome is NotificationOutcome.UNAVAILABLE

    @property
    def failed(self) -> bool:
        """Whether the screen did not prove a completion or a recorded denial."""
        return self.outcome is NotificationOutcome.FAILED

    @property
    def skipped(self) -> bool:
        """Whether state or the entry policy stopped the task before any tap."""
        return self.outcome is NotificationOutcome.SKIPPED

    @property
    def returned(self) -> bool:
        """Whether the run finished on the coins page."""
        return self.return_state is not None and self.return_state.returned

    @property
    def granted(self) -> bool:
        """Whether the post-run probe positively says the permission is held."""
        return self.permission_after is not None and self.permission_after.granted

    def as_dict(self) -> dict[str, object]:
        """Render the result for ``run.json`` without losing the evidence."""
        return {
            'key': self.key,
            'title': self.title,
            'outcome': self.outcome.value,
            'verification': self.verification,
            'detail': self.detail,
            'limit': self.limit,
            'done_today_before': self.done_today_before,
            'done_today_after': self.done_today_after,
            'taps': [tap.as_dict() for tap in self.taps],
            'dismissals': [report.as_dict() for report in self.dismissals],
            'return': None if self.return_state is None else self.return_state.as_dict(),
            'recorded': self.recorded,
            'dialog_appeared': self.dialog_appeared,
            'classification': self.classification,
            'screen': self.screen,
            'permission_before': None if self.permission_before is None else self.permission_before.as_dict(),
            'permission_after': None if self.permission_after is None else self.permission_after.as_dict(),
            'granted': self.granted,
            'stop': self.stop,
        }


def classify_notification_screen(
    snapshot: Snapshot,
    *,
    classification: Classification | None = None,
) -> NotificationScreen:
    """Name the notification prompt/page in ``snapshot``, if any.

    The base :func:`~aliexpress_coins.perception.state.classify` answer is
    accepted as an argument so the caller can reuse it in the report, but the
    notification-specific decision is made from the labels the flow spec names.
    The dialog is checked before the app prompt because the dialog is a separate
    window *above* the prompt; both can be visible in the same frame.
    """
    state = classification if classification is not None else classify(snapshot)
    if contains_text(snapshot, SYSTEM_DIALOG_MARKER):
        return NotificationScreen.SYSTEM_DIALOG
    if contains_text(snapshot, APP_PROMPT_MARKER):
        return NotificationScreen.APP_PROMPT
    if contains_text(snapshot, OS_SETTINGS_MARKER):
        blocked = contains_text(snapshot, OS_SETTINGS_BLOCKED_MARKER)
        granted = all(contains_text(snapshot, marker) for marker in AFTER_GRANT_MARKERS)
        if granted and not blocked:
            return NotificationScreen.OS_SETTINGS_GRANTED
        return NotificationScreen.OS_SETTINGS
    if is_boost_page(snapshot):
        return NotificationScreen.BOOST_LIST
    if all(contains_text(snapshot, marker) for marker in COIN_PAGE_MARKERS):
        return NotificationScreen.COIN_PAGE
    if state.state is ScreenState.PAGE:
        return NotificationScreen.ENTRY_ROW
    return NotificationScreen.OTHER


def is_system_dialog(snapshot: Snapshot) -> bool:
    """Whether the Android runtime permission dialog is present."""
    return contains_text(snapshot, SYSTEM_DIALOG_MARKER)


def is_app_prompt(snapshot: Snapshot) -> bool:
    """Whether the app's ``Receive timely notifications`` prompt is present."""
    return contains_text(snapshot, APP_PROMPT_MARKER)


def is_os_settings(snapshot: Snapshot) -> bool:
    """Whether the frame is the OS app-notification page."""
    return contains_text(snapshot, OS_SETTINGS_MARKER)


def find_grant_control(snapshot: Snapshot) -> TextBox | None:
    """Return the dialog's ``Allow`` control, or ``None``."""
    return _find_exact_control(snapshot, ALLOW_LABELS)


def find_allow_control(snapshot: Snapshot) -> TextBox | None:
    """Alias for :func:`find_grant_control`."""
    return find_grant_control(snapshot)


def find_deny_control(snapshot: Snapshot) -> TextBox | None:
    """Return the prompt's safe refusal/close control, or ``None``."""
    return _find_exact_control(snapshot, DENY_LABELS)


def find_enable_all_control(snapshot: Snapshot) -> TextBox | None:
    """Return ``Enable all notifications`` on the app pre-prompt, or ``None``."""
    return _find_exact_control(snapshot, (ENABLE_ALL_LABEL,))


def parse_permission_output(output: str, *, source: str = 'appops') -> PermissionReading:
    """Parse ``cmd appops`` (or a permission-shaped) output into a reading.

    The measured reference form is ``Uid mode: POST_NOTIFICATION: ignore``.
    A bare ``allow``/``granted`` or ``ignore``/``denied`` is also accepted so a
    different Android build or a test fake can use the same probe.
    """
    text = output.strip()
    # `cmd appops get <pkg> POST_NOTIFICATION` on the reference device prints
    # both the authoritative uid mode (`ignore`) and a second op line (`allow`)
    # whose meaning is not the runtime grant. The uid line wins when present.
    lines = text.splitlines()
    uid_line = next((line for line in lines if 'uid mode:' in line.casefold()), '')
    graded = uid_line or text
    if _PERMISSION_ALLOW.search(graded) is not None:
        return PermissionReading(NotificationPermission.GRANTED, raw=text or None, source=source)
    if _PERMISSION_DENY.search(graded) is not None:
        return PermissionReading(NotificationPermission.DENIED, raw=text or None, source=source)
    match = _PERMISSION_WORD.search(graded)
    if match is not None:
        word = match.group('word').casefold()
        state = NotificationPermission.GRANTED if word.startswith(('allow', 'grant')) else NotificationPermission.DENIED
        return PermissionReading(state, raw=text or None, source=source)
    return PermissionReading(NotificationPermission.UNKNOWN, raw=text or None, source=source)


class AppOpsPermissionProbe:
    """Read ``POST_NOTIFICATION`` through ``adb shell cmd appops``.

    The probe is optional on :func:`run_notifications_task`: a fixture run
    injects a callable instead, and no test spawns adb. The command is the one
    measured on the reference device; ``cmd notification`` does not expose the
    per-app runtime grant on this build.
    """

    def __init__(
        self,
        serial: str,
        *,
        runner: ProbeRunner | None = None,
        timeout: float = 10.0,
        package: str = NOTIFICATION_PACKAGE,
    ) -> None:
        if not serial.strip():
            raise ValueError('an adb serial is required')
        if timeout <= 0:
            raise ValueError('the probe timeout must be positive')
        self.serial = serial
        self.package = package
        self._runner = runner if runner is not None else _SubprocessProbeRunner()
        self._timeout = timeout

    def __call__(self) -> PermissionReading:
        """Run the probe once and parse its output."""
        args = ('adb', '-s', self.serial, 'shell', 'cmd', 'appops', 'get', self.package, NOTIFICATION_PERMISSION)
        try:
            result = self._runner(args, self._timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return PermissionReading(
                NotificationPermission.UNKNOWN, raw=f'{type(exc).__name__}: {exc}', source='appops'
            )
        returncode = int(getattr(result, 'returncode', 0))
        stdout = _text_output(getattr(result, 'stdout', ''))
        stderr = _text_output(getattr(result, 'stderr', ''))
        if returncode != 0:
            return PermissionReading(
                NotificationPermission.UNKNOWN,
                raw=(stdout or stderr or f'appops exited {returncode}').strip() or None,
                source='appops',
            )
        return parse_permission_output(stdout or stderr)


# A descriptive alias for callers that name the transport rather than the command.
AdbNotificationPermissionProbe = AppOpsPermissionProbe


def run_notifications_task(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    state: DailyStateStore,
    entry: RosterEntry,
    *,
    navigate: RouteFirer,
    snapshot: Snapshot | None = None,
    templates: TemplateStore | None = None,
    route: SurfaceRoute = SurfaceRoute.SHORTCUT,
    load_timeout: float = DEFAULT_LOAD_TIMEOUT,
    arrival_timeout: float = DEFAULT_ARRIVAL_TIMEOUT,
    exit_timeout: float = DEFAULT_EXIT_TIMEOUT,
    interval: float = DEFAULT_INTERVAL,
    return_timeout: float = DEFAULT_RETURN_TIMEOUT,
    return_interval: float = DEFAULT_RETURN_INTERVAL,
    return_attempts: int = DEFAULT_RETURN_ATTEMPTS,
    settle: float = 0.0,
    permission_probe: Callable[[], PermissionReading | str | bool | None] | None = None,
    enable_notifications: bool = True,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    logger: logging.Logger | None = None,
) -> NotificationResult:
    """Run one notifications task from an allowed roster row.

    The row is re-resolved from the current frame, its action is pressed once,
    and the result is read from the next screens. The system permission dialog
    is handled as :class:`NotificationScreen.SYSTEM_DIALOG`; the app pre-prompt
    is followed through ``Enable all notifications``; an OS settings page stops
    the task without touching its switch. A denial (or a grant that does not
    take) is returned as ``permission_denied`` after the app has been proven
    reachable again.

    State is checked before the first capture. ``enable_notifications=False`` is
    for an operator who does not want the permission: the dialog is closed with
    its denial control and the outcome is still recorded, rather than granting
    and trying to undo it through system settings.
    """
    log = logger if logger is not None else logging.getLogger(LOGGER_NAME)
    spec = _allowed_spec(entry)
    limit = spec.max_per_run
    done_before = state.count(spec.key)
    if done_before >= limit:
        result = _terminal(
            spec,
            entry.title,
            NotificationOutcome.SKIPPED,
            verification='daily_limit',
            detail=f'{spec.key!r} already completed {done_before} time(s) today; the limit is {limit}',
            limit=limit,
            done_before=done_before,
            done_after=done_before,
        )
        _log_result(log, result)
        return result

    if load_timeout <= 0:
        raise ValueError(f'load_timeout must be positive, got {load_timeout!r}')
    if arrival_timeout <= 0:
        raise ValueError(f'arrival_timeout must be positive, got {arrival_timeout!r}')
    if exit_timeout <= 0:
        raise ValueError(f'exit_timeout must be positive, got {exit_timeout!r}')
    if interval <= 0:
        raise ValueError(f'interval must be positive, got {interval!r}')
    if return_timeout <= 0:
        raise ValueError(f'return_timeout must be positive, got {return_timeout!r}')
    if return_interval <= 0:
        raise ValueError(f'return_interval must be positive, got {return_interval!r}')
    if return_attempts < 1:
        raise ValueError(f'return_attempts must be positive, got {return_attempts!r}')
    if settle < 0:
        raise ValueError(f'settle must not be negative, got {settle!r}')

    sweep_corpus = _sweep_corpus(corpus)
    permission_before = _probe(permission_probe, log)
    frame = snapshot if snapshot is not None else _capture(capture)
    frame, settled, entry_sweep = _settle_surface(
        controller,
        capture,
        sweep_corpus,
        frame,
        templates=templates,
        timeout=load_timeout,
        interval=interval,
        settle=settle,
        clock=clock,
        sleep=sleep,
        logger=log,
    )
    dismissals: list[DismissReport] = []
    if entry_sweep is not None:
        dismissals.append(entry_sweep)
    taps: list[TapRecord] = []
    dialog_appeared = False
    grant_attempted = False
    denial_attempted = False
    final_screen = classify_notification_screen(frame)
    final_classification = classify(frame)
    current = frame

    if not settled:
        return _finish_failed(
            controller,
            capture,
            sweep_corpus,
            navigate,
            spec,
            entry,
            done_before,
            limit,
            verification='not_settled',
            detail='the task surface never stopped loading; no action was tapped',
            taps=taps,
            dismissals=dismissals,
            route=route,
            templates=templates,
            return_timeout=return_timeout,
            return_interval=return_interval,
            return_attempts=return_attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=log,
            permission_before=permission_before,
        )

    if final_screen is NotificationScreen.OS_SETTINGS or final_screen is NotificationScreen.OS_SETTINGS_GRANTED:
        _record_once(state, spec)
        return _finish_denied_or_routed(
            controller,
            capture,
            sweep_corpus,
            navigate,
            spec,
            entry,
            done_before,
            limit,
            stop='routed_to_settings',
            current=current,
            taps=taps,
            dismissals=dismissals,
            route=route,
            templates=templates,
            return_timeout=return_timeout,
            return_interval=return_interval,
            return_attempts=return_attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=log,
            permission_before=permission_before,
            notification_screen=final_screen,
            classification=final_classification,
            dialog_appeared=dialog_appeared,
        )

    if not _is_task_surface(current):
        return _finish_unavailable(
            controller,
            capture,
            sweep_corpus,
            navigate,
            spec,
            entry,
            done_before,
            limit,
            detail=f'{entry.title!r} is not on a task surface in this frame',
            taps=taps,
            dismissals=dismissals,
            route=route,
            templates=templates,
            return_timeout=return_timeout,
            return_interval=return_interval,
            return_attempts=return_attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=log,
            permission_before=permission_before,
            notification_screen=final_screen,
            classification=final_classification,
        )

    row, action, _badge = find_task_row(current, entry.title)
    if row is None:
        return _finish_unavailable(
            controller,
            capture,
            sweep_corpus,
            navigate,
            spec,
            entry,
            done_before,
            limit,
            detail=f'{entry.title!r} is absent from the current roster; no dialog was opened',
            taps=taps,
            dismissals=dismissals,
            route=route,
            templates=templates,
            return_timeout=return_timeout,
            return_interval=return_interval,
            return_attempts=return_attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=log,
            permission_before=permission_before,
            notification_screen=final_screen,
            classification=final_classification,
        )
    if action is None:
        return _finish_already_done(
            controller,
            capture,
            sweep_corpus,
            navigate,
            state,
            spec,
            entry,
            done_before,
            permission_before=permission_before,
            detail=f'{entry.title!r} already carries its completed control; no dialog was opened',
            taps=taps,
            dismissals=dismissals,
            route=route,
            templates=templates,
            return_timeout=return_timeout,
            return_interval=return_interval,
            return_attempts=return_attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=log,
            notification_screen=final_screen,
            classification=final_classification,
        )

    try:
        taps.append(
            controller.tap_box(
                action,
                why=f'notifications {spec.key}: open the notification task',
                label=action.text,
            ),
        )
        stop, dialog_appeared, grant_attempted, denial_attempted = _wait_after_action(
            controller,
            capture,
            sweep_corpus,
            current,
            title=entry.title,
            spec=spec,
            enable_notifications=enable_notifications,
            arrival_timeout=arrival_timeout,
            exit_timeout=exit_timeout,
            interval=interval,
            settle=settle,
            templates=templates,
            clock=clock,
            sleep=sleep,
            logger=log,
            taps=taps,
            dismissals=dismissals,
            dialog_appeared=dialog_appeared,
            grant_attempted=grant_attempted,
            denial_attempted=denial_attempted,
        )
        current = _capture(capture)
        final_screen = classify_notification_screen(current)
        final_classification = classify(current)
        permission_after = _probe(permission_probe, log)

        if stop is not None:
            _record_once(state, spec)
            return _finish_denied_or_routed(
                controller,
                capture,
                sweep_corpus,
                navigate,
                spec,
                entry,
                done_before,
                limit,
                stop=stop,
                current=current,
                taps=taps,
                dismissals=dismissals,
                route=route,
                templates=templates,
                return_timeout=return_timeout,
                return_interval=return_interval,
                return_attempts=return_attempts,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=log,
                permission_before=permission_before,
                permission_after=permission_after,
                notification_screen=final_screen,
                classification=final_classification,
                dialog_appeared=dialog_appeared,
                grant_attempted=grant_attempted,
            )

        verdict, verify_dismiss, verify_return = _verify_row(
            controller,
            capture,
            sweep_corpus,
            navigate,
            entry.title,
            current,
            templates=templates,
            route=route,
            load_timeout=load_timeout,
            interval=interval,
            settle=settle,
            timeout=return_timeout,
            return_interval=return_interval,
            return_attempts=return_attempts,
            clock=clock,
            sleep=sleep,
            logger=log,
        )
        if verify_dismiss is not None:
            dismissals.append(verify_dismiss)
        return_state = verify_return
        if return_state is None:
            return_state = return_to_coins_page(
                controller,
                capture,
                sweep_corpus,
                navigate,
                snapshot=current,
                templates=templates,
                route=route,
                timeout=return_timeout,
                interval=return_interval,
                attempts=return_attempts,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=log,
            )
        if not return_state.returned:
            return _finish_failed(
                controller,
                capture,
                sweep_corpus,
                navigate,
                spec,
                entry,
                done_before,
                limit,
                verification='return_failed',
                detail='the verification surface was read but the run could not return to the coins page',
                taps=taps,
                dismissals=dismissals,
                return_state=return_state,
                route=route,
                templates=templates,
                return_timeout=return_timeout,
                return_interval=return_interval,
                return_attempts=return_attempts,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=log,
                permission_before=permission_before,
                permission_after=permission_after,
                notification_screen=final_screen,
                classification=final_classification,
            )

        # A probe that positively says the grant did not take overrides a row
        # that merely flipped: the row pays on the tap, so its check is not proof
        # the permission is held.
        if grant_attempted and permission_after is not None and permission_after.denied:
            _record_once(state, spec)
            return _finish_denied_or_routed(
                controller,
                capture,
                sweep_corpus,
                navigate,
                spec,
                entry,
                done_before,
                limit,
                stop='permission_denied',
                current=current,
                taps=taps,
                dismissals=dismissals,
                route=route,
                templates=templates,
                return_timeout=return_timeout,
                return_interval=return_interval,
                return_attempts=return_attempts,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=log,
                permission_before=permission_before,
                permission_after=permission_after,
                notification_screen=final_screen,
                classification=final_classification,
                dialog_appeared=dialog_appeared,
                grant_attempted=grant_attempted,
                return_state=return_state,
            )

        permission_completion = (
            permission_after is not None
            and permission_after.granted
            and (permission_before is None or not permission_before.granted)
        )
        if verdict.decisive or permission_completion:
            verification = verdict.status if verdict.decisive else 'permission_granted'
            detail = (
                verdict.detail
                if verdict.decisive
                else 'the notification permission probe now reports the permission as granted'
            )
            state.record(spec.key)
            return _terminal(
                spec,
                entry.title,
                NotificationOutcome.COMPLETED,
                verification=verification,
                detail=detail,
                limit=limit,
                done_before=done_before,
                done_after=state.count(spec.key),
                taps=taps,
                dismissals=dismissals,
                return_state=return_state,
                recorded=True,
                dialog_appeared=dialog_appeared,
                classification=final_classification.state.value,
                screen=final_screen.value,
                permission_before=permission_before,
                permission_after=permission_after,
                stop=stop,
                logger=log,
            )

        if grant_attempted and permission_after is None and verdict.status == 'no_change':
            return _finish_failed(
                controller,
                capture,
                sweep_corpus,
                navigate,
                spec,
                entry,
                done_before,
                limit,
                verification='grant_not_observed',
                detail='the grant control was pressed but neither the row nor a permission probe proved the change',
                taps=taps,
                dismissals=dismissals,
                return_state=return_state,
                route=route,
                templates=templates,
                return_timeout=return_timeout,
                return_interval=return_interval,
                return_attempts=return_attempts,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=log,
                permission_before=permission_before,
                permission_after=permission_after,
                notification_screen=final_screen,
                classification=final_classification,
                dialog_appeared=dialog_appeared,
            )

        return _finish_failed(
            controller,
            capture,
            sweep_corpus,
            navigate,
            spec,
            entry,
            done_before,
            limit,
            verification=verdict.status,
            detail=verdict.detail,
            taps=taps,
            dismissals=dismissals,
            return_state=return_state,
            route=route,
            templates=templates,
            return_timeout=return_timeout,
            return_interval=return_interval,
            return_attempts=return_attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=log,
            permission_before=permission_before,
            permission_after=permission_after,
            notification_screen=final_screen,
            classification=final_classification,
            dialog_appeared=dialog_appeared,
        )
    except Exception as exc:
        # A prompt the corpus cannot close, a transport fault or an unexpected
        # state must not abort the night. Return to the coins page and record it.
        return_state = _return_quietly(
            controller,
            capture,
            sweep_corpus,
            navigate,
            templates=templates,
            route=route,
            timeout=return_timeout,
            interval=return_interval,
            attempts=return_attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=log,
        )
        result = _terminal(
            spec,
            entry.title,
            NotificationOutcome.FAILED,
            verification='error',
            detail=f'{type(exc).__name__}: {exc}',
            limit=limit,
            done_before=done_before,
            done_after=state.count(spec.key),
            taps=taps,
            dismissals=dismissals,
            return_state=return_state,
            dialog_appeared=dialog_appeared,
            permission_before=permission_before,
            stop='error',
        )
        _log_result(log, result)
        return result


# The issue's public name; ``run_notifications_task`` says what it does at the dispatch point.
run_notifications = run_notifications_task


def _wait_after_action(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    snapshot: Snapshot,
    *,
    title: str,
    spec: AllowedTaskSpec,
    enable_notifications: bool,
    arrival_timeout: float,
    exit_timeout: float,
    interval: float,
    settle: float,
    templates: TemplateStore | None,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
    taps: list[TapRecord],
    dismissals: list[DismissReport],
    dialog_appeared: bool,
    grant_attempted: bool,
    denial_attempted: bool,
) -> tuple[str | None, bool, bool, bool]:
    """Follow the row's action until a prompt, settings redirect or completion."""
    started = clock()
    deadline = started + arrival_timeout
    grant_deadline: float | None = None
    current = snapshot
    while True:
        now = clock()
        classification = classify(current)
        screen = classify_notification_screen(current, classification=classification)

        if screen is NotificationScreen.SYSTEM_DIALOG:
            dialog_appeared = True
            allow = find_grant_control(current)
            deny = find_deny_control(current)
            if grant_attempted:
                assert grant_deadline is not None
                if now < grant_deadline:
                    sleep(min(interval, max(0.0, grant_deadline - now)))
                    current = _capture(capture)
                    continue
                if deny is not None and not denial_attempted:
                    taps.append(
                        controller.tap_box(
                            deny,
                            why=f'notifications {spec.key}: close a dialog whose grant did not take',
                            label=deny.text,
                        ),
                    )
                    denial_attempted = True
                return 'permission_denied', dialog_appeared, grant_attempted, denial_attempted
            if allow is not None and enable_notifications:
                taps.append(
                    controller.tap_box(
                        allow,
                        why=f'notifications {spec.key}: grant the runtime notification permission',
                        label=allow.text,
                    ),
                )
                grant_attempted = True
                grant_deadline = clock() + exit_timeout
                current = _capture(capture)
                continue
            if deny is not None and not denial_attempted:
                taps.append(
                    controller.tap_box(
                        deny,
                        why=f'notifications {spec.key}: deny the permission the dialog offered',
                        label=deny.text,
                    ),
                )
                denial_attempted = True
            return 'permission_denied', dialog_appeared, grant_attempted, denial_attempted

        if screen is NotificationScreen.APP_PROMPT:
            enable = find_enable_all_control(current)
            if enable is not None and enable_notifications:
                taps.append(
                    controller.tap_box(
                        enable,
                        why=f'notifications {spec.key}: ask the app for the system dialog',
                        label=enable.text,
                    ),
                )
                current = _capture(capture)
                continue
            deny = find_deny_control(current)
            if deny is not None and not denial_attempted:
                taps.append(
                    controller.tap_box(
                        deny,
                        why=f'notifications {spec.key}: decline the app pre-prompt',
                        label=deny.text,
                    ),
                )
                denial_attempted = True
            else:
                report, stop = _sweep(
                    controller,
                    capture,
                    corpus,
                    templates=templates,
                    settle=settle,
                    clock=clock,
                    sleep=sleep,
                    logger=logger,
                )
                if report is not None:
                    dismissals.append(report)
                if stop is not None:
                    return stop, dialog_appeared, grant_attempted, denial_attempted
            return 'permission_denied', dialog_appeared, grant_attempted, denial_attempted

        if screen in (NotificationScreen.OS_SETTINGS, NotificationScreen.OS_SETTINGS_GRANTED):
            return 'routed_to_settings', dialog_appeared, grant_attempted, denial_attempted

        if _is_task_surface(current):
            _row, action, _badge = find_task_row(current, title)
            if action is None:
                return None, dialog_appeared, grant_attempted, denial_attempted

        if classification.state is ScreenState.POPUP:
            if _is_trusted_page(current, screen, corpus):
                if now >= deadline:
                    return None, dialog_appeared, grant_attempted, denial_attempted
                sleep(min(interval, max(0.0, deadline - now)))
                current = _capture(capture)
                continue
            report, stop = _sweep(
                controller, capture, corpus, templates=templates, settle=settle, clock=clock, sleep=sleep, logger=logger
            )
            if report is not None:
                dismissals.append(report)
            if stop is not None:
                return stop, dialog_appeared, grant_attempted, denial_attempted
            # A bounded or still-remaining sweep is not progress. Returning a
            # stop here is what bounds a popup that reopens after every close.
            if report is None or report.remaining or report.bounded:
                return 'popup_unclosable', dialog_appeared, grant_attempted, denial_attempted
            current = report.frames[-1].snapshot if report.frames else _capture(capture)
            continue

        if classification.state is ScreenState.LOADING:
            if now >= deadline:
                return None, dialog_appeared, grant_attempted, denial_attempted
            sleep(min(interval, max(0.0, deadline - now)))
            current = _capture(capture)
            continue

        if now >= deadline:
            return None, dialog_appeared, grant_attempted, denial_attempted
        sleep(min(interval, max(0.0, deadline - now)))
        current = _capture(capture)


def _verify_row(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    navigate: RouteFirer,
    title: str,
    frame: Snapshot,
    *,
    templates: TemplateStore | None,
    route: SurfaceRoute,
    load_timeout: float,
    interval: float,
    settle: float,
    timeout: float,
    return_interval: float,
    return_attempts: int,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
) -> tuple[_Verdict, DismissReport | None, ReturnResult | None]:
    """Read the row on the returned surface, then re-open the list if needed."""
    del load_timeout
    first = _row_verdict(frame, title, trusted=False)
    if first.decisive:
        return first, None, None

    try:
        opened = open_boost_list(
            controller,
            capture,
            corpus,
            templates=templates,
            timeout=timeout,
            interval=interval,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=logger,
        )
    except StateTimeout, PopupUnclosable:
        final = return_to_coins_page(
            controller,
            capture,
            corpus,
            navigate,
            snapshot=None,
            templates=templates,
            route=route,
            timeout=timeout,
            interval=return_interval,
            attempts=return_attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=logger,
        )
        return (
            _Verdict('no_observation', False, 'the boost list could not be read for verification'),
            None,
            final,
        )

    trusted = _trusted_surface(opened)
    verdict = _row_verdict(opened.snapshot, title, trusted=trusted)
    if _on_coin_page(opened.snapshot):
        return verdict, opened.dismiss, None

    final = return_to_coins_page(
        controller,
        capture,
        corpus,
        navigate,
        snapshot=opened.snapshot,
        templates=templates,
        route=route,
        timeout=timeout,
        interval=return_interval,
        attempts=return_attempts,
        settle=settle,
        clock=clock,
        sleep=sleep,
        logger=logger,
    )
    return verdict, opened.dismiss, final


@dataclass(frozen=True, slots=True)
class _Verdict:
    """The internal answer from one verification frame."""

    status: str
    decisive: bool
    detail: str


def _row_verdict(snapshot: Snapshot, title: str, *, trusted: bool) -> _Verdict:
    """Turn one frame into a completion verdict, strongest signal first."""
    row, action, _badge = find_task_row(snapshot, title)
    if row is None:
        if trusted and is_boost_page(snapshot) and has_task_rows(snapshot):
            return _Verdict('row_gone', True, f'{title!r} is gone from the populated boost list')
        return _Verdict('no_observation', False, f'{title!r} was not found and the surface cannot prove it is gone')
    if action is None:
        return _Verdict('action_completed', True, f'{title!r} now carries its completed control')
    return _Verdict('no_change', False, f'{title!r} is still present with the same action')


def _trusted_surface(opened: BoostList) -> bool:
    """Whether the opened surface is a task list whose missing row means something."""
    if opened.empty or is_empty_boost(opened.snapshot):
        return False
    return is_boost_page(opened.snapshot) and has_task_rows(opened.snapshot)


def _is_task_surface(snapshot: Snapshot) -> bool:
    """Whether a frame is the task list the roster was read from."""
    return is_boost_page(snapshot) or _on_coin_page(snapshot)


def _is_trusted_page(snapshot: Snapshot, screen: NotificationScreen, corpus: PopupCorpus) -> bool:
    """Whether a generic ``POPUP`` is the page itself, not an unswept modal.

    The classifier can read a dim or low-brightness task page as a panel/scrim.
    Once the notification-specific labels positively identify the boost list or
    coin page and no corpus family is anchored on the frame, that page is the
    surface the task was opened from — sweeping it would close the list (#26).
    A real modal still carries its family anchor and is swept normally.
    """
    if screen not in (NotificationScreen.BOOST_LIST, NotificationScreen.COIN_PAGE):
        return False
    return _matching_family(snapshot, corpus) is None


def _on_coin_page(snapshot: Snapshot) -> bool:
    return all(contains_text(snapshot, marker) for marker in COIN_PAGE_MARKERS)


def _probe(
    probe: Callable[[], PermissionReading | str | bool | None] | None,
    logger: logging.Logger,
) -> PermissionReading | None:
    """Read the optional permission probe, treating its failure as unknown."""
    if probe is None:
        return None
    try:
        value = probe()
    except Exception as exc:
        logger.warning(
            '%s',
            _json({'event': 'notification_probe_failed', 'detail': f'{type(exc).__name__}: {exc}'}),
        )
        return PermissionReading(NotificationPermission.UNKNOWN, raw=f'{type(exc).__name__}: {exc}', source='probe')
    return _coerce_reading(value)


def _coerce_reading(value: PermissionReading | str | bool | None) -> PermissionReading:
    if isinstance(value, PermissionReading):
        return value
    if isinstance(value, bool):
        return PermissionReading(
            NotificationPermission.GRANTED if value else NotificationPermission.DENIED, source='probe'
        )
    if isinstance(value, str):
        return parse_permission_output(value, source='probe')
    return PermissionReading(NotificationPermission.UNKNOWN, source='probe')


def _sweep_corpus(corpus: PopupCorpus) -> PopupCorpus:
    """Remove the families this executor handles itself.

    The boost sheet must survive: its ``Coins boost`` anchor is the list the
    task was opened from, and closing it is the #26 regression.
    """
    families = tuple(family for family in corpus.families if family.family not in NOTIFICATION_SWEEP_FAMILIES)
    if len(families) == len(corpus.families):
        return corpus
    return replace(corpus, families=families)


def _find_exact_control(snapshot: Snapshot, labels: Sequence[str]) -> TextBox | None:
    """Find a control by an exact normalised label, never a substring.

    A dialog's question contains the word ``Allow``; a substring match there
    would tap the sentence instead of the button.
    """
    wanted = tuple(normalize(label) for label in labels)
    for label in wanted:
        for box in sorted(snapshot.boxes, key=lambda item: (item.bbox[1], item.bbox[0])):
            if box.score < DEFAULT_MIN_OCR_SCORE:
                continue
            if normalize(box.text) == label:
                return box
    return None


def _settle_surface(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    frame: Snapshot,
    *,
    templates: TemplateStore | None,
    timeout: float,
    interval: float,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
) -> tuple[Snapshot, bool, DismissReport | None]:
    """Resolve loading and generic popups before a row is read or pressed."""
    deadline = clock() + timeout
    dismiss: DismissReport | None = None
    while True:
        classification = classify(frame)
        screen = classify_notification_screen(frame, classification=classification)
        if screen in (NotificationScreen.SYSTEM_DIALOG, NotificationScreen.APP_PROMPT):
            return frame, True, dismiss
        if screen in (NotificationScreen.OS_SETTINGS, NotificationScreen.OS_SETTINGS_GRANTED):
            return frame, True, dismiss
        if classification.state is ScreenState.LOADING:
            now = clock()
            if now >= deadline:
                return frame, False, dismiss
            sleep(min(interval, max(0.0, deadline - now)))
            frame = _capture(capture)
            continue
        if classification.state is ScreenState.POPUP:
            if _is_trusted_page(frame, screen, corpus):
                return frame, True, dismiss
            now = clock()
            if now >= deadline:
                return frame, False, dismiss
            report, stop = _sweep(
                controller,
                capture,
                corpus,
                templates=templates,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=logger,
            )
            dismiss = report
            if stop is not None:
                return frame, False, dismiss
            frame = report.frames[-1].snapshot if report is not None and report.frames else _capture(capture)
            continue
        return frame, True, dismiss


def _sweep(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    *,
    templates: TemplateStore | None,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
) -> tuple[DismissReport | None, str | None]:
    """Run the one in-app sweep and convert an unclosable popup into a stop."""
    # Import here so the only dismissal loop in the codebase stays the one in
    # control.popups, and this module never grows a second tap policy.
    from aliexpress_coins.control.popups import dismiss_after

    try:
        report = dismiss_after(
            controller,
            capture,
            corpus,
            templates=templates,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=logger,
        )
    except PopupUnclosable as exc:
        logger.warning('%s', _json({'event': 'notification_popup_unclosable', 'detail': str(exc)}))
        return None, 'popup_unclosable'
    return report, None


def _allowed_spec(entry: RosterEntry) -> AllowedTaskSpec:
    """Return the entry's allowed spec, refusing anything the policy refused.

    Unlike the page-transition executors, an allowed notification row with no
    action is still dispatchable: its drawn check is the ``already_done`` state
    the flow spec records, not an error. No tap is issued for that row.
    """
    spec = entry.spec
    if not entry.allowed or spec is None:
        reason = entry.reason or 'no allowed action is exposed'
        raise TaskError(f'{entry.title!r} is not dispatchable: {reason}')
    if spec.category is not TaskCategory.NOTIFICATION:
        raise TaskError(f'{entry.title!r} is {spec.category.value!r}, not a notification task')
    return spec


def _finish_unavailable(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    navigate: RouteFirer,
    spec: AllowedTaskSpec,
    entry: RosterEntry,
    done_before: int,
    limit: int,
    *,
    detail: str,
    taps: Sequence[TapRecord],
    dismissals: Sequence[DismissReport],
    route: SurfaceRoute,
    templates: TemplateStore | None,
    return_timeout: float,
    return_interval: float,
    return_attempts: int,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
    permission_before: PermissionReading | None,
    notification_screen: NotificationScreen | None = None,
    classification: Classification | None = None,
) -> NotificationResult:
    return_state = _return_quietly(
        controller,
        capture,
        corpus,
        navigate,
        templates=templates,
        route=route,
        timeout=return_timeout,
        interval=return_interval,
        attempts=return_attempts,
        settle=settle,
        clock=clock,
        sleep=sleep,
        logger=logger,
    )
    return _terminal(
        spec,
        entry.title,
        NotificationOutcome.UNAVAILABLE,
        verification='unavailable',
        detail=detail,
        limit=limit,
        done_before=done_before,
        done_after=done_before,
        taps=taps,
        dismissals=dismissals,
        return_state=return_state,
        classification=None if classification is None else classification.state.value,
        screen=None if notification_screen is None else notification_screen.value,
        permission_before=permission_before,
        logger=logger,
    )


def _finish_already_done(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    navigate: RouteFirer,
    state: DailyStateStore,
    spec: AllowedTaskSpec,
    entry: RosterEntry,
    done_before: int,
    *,
    permission_before: PermissionReading | None,
    detail: str,
    taps: Sequence[TapRecord],
    dismissals: Sequence[DismissReport],
    route: SurfaceRoute,
    templates: TemplateStore | None,
    return_timeout: float,
    return_interval: float,
    return_attempts: int,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
    notification_screen: NotificationScreen | None = None,
    classification: Classification | None = None,
) -> NotificationResult:
    return_state = _return_quietly(
        controller,
        capture,
        corpus,
        navigate,
        templates=templates,
        route=route,
        timeout=return_timeout,
        interval=return_interval,
        attempts=return_attempts,
        settle=settle,
        clock=clock,
        sleep=sleep,
        logger=logger,
    )
    state.record(spec.key)
    return _terminal(
        spec,
        entry.title,
        NotificationOutcome.ALREADY_DONE,
        verification='already_done',
        detail=detail,
        limit=spec.max_per_run,
        done_before=done_before,
        done_after=state.count(spec.key),
        taps=taps,
        dismissals=dismissals,
        return_state=return_state,
        recorded=True,
        classification=None if classification is None else classification.state.value,
        screen=None if notification_screen is None else notification_screen.value,
        permission_before=permission_before,
        logger=logger,
    )


def _finish_denied_or_routed(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    navigate: RouteFirer,
    spec: AllowedTaskSpec,
    entry: RosterEntry,
    done_before: int,
    limit: int,
    *,
    stop: str,
    current: Snapshot,
    taps: Sequence[TapRecord],
    dismissals: Sequence[DismissReport],
    route: SurfaceRoute,
    templates: TemplateStore | None,
    return_timeout: float,
    return_interval: float,
    return_attempts: int,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
    permission_before: PermissionReading | None,
    permission_after: PermissionReading | None = None,
    notification_screen: NotificationScreen | None = None,
    classification: Classification | None = None,
    dialog_appeared: bool = False,
    grant_attempted: bool = False,
    return_state: ReturnResult | None = None,
) -> NotificationResult:
    """Record a denial or settings redirect and leave the app reachable."""
    state = return_state
    if state is None:
        state = return_to_coins_page(
            controller,
            capture,
            corpus,
            navigate,
            snapshot=current if _on_coin_page(current) else None,
            templates=templates,
            route=route,
            timeout=return_timeout,
            interval=return_interval,
            attempts=return_attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=logger,
        )
    outcome = (
        NotificationOutcome.ROUTED_TO_SETTINGS
        if stop == 'routed_to_settings'
        else NotificationOutcome.PERMISSION_DENIED
    )
    detail = (
        'the app routed the flow to OS settings; the task stopped without flipping the system switch'
        if stop == 'routed_to_settings'
        else (
            'the permission grant was pressed but the dialog or the probe did not confirm it'
            if grant_attempted
            else 'the dialog offered no grant path; its denial control was used and the app is still reachable'
        )
    )
    if not state.returned:
        detail = f'{detail}; the shortcut route did not prove the coins page'
    return _terminal(
        spec,
        entry.title,
        outcome,
        verification=stop,
        detail=detail,
        limit=limit,
        done_before=done_before,
        done_after=done_before + 1,
        taps=taps,
        dismissals=dismissals,
        return_state=state,
        classification=None if classification is None else classification.state.value,
        screen=None if notification_screen is None else notification_screen.value,
        permission_before=permission_before,
        permission_after=permission_after,
        dialog_appeared=dialog_appeared,
        stop=stop,
        logger=logger,
    )


def _record_once(state: DailyStateStore, spec: AllowedTaskSpec) -> None:
    """Record a terminal notification outcome once for the day."""
    if not state.done_today(spec.key):
        state.record(spec.key)


def _return_quietly(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    navigate: RouteFirer,
    *,
    templates: TemplateStore | None,
    route: SurfaceRoute,
    timeout: float,
    interval: float,
    attempts: int,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
) -> ReturnResult:
    """Best-effort return that never raises over the original outcome."""
    try:
        return return_to_coins_page(
            controller,
            capture,
            corpus,
            navigate,
            snapshot=None,
            templates=templates,
            route=route,
            timeout=timeout,
            interval=interval,
            attempts=attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=logger,
        )
    except Exception as exc:
        logger.warning('%s', _json({'event': 'notification_return_failed', 'detail': f'{type(exc).__name__}: {exc}'}))
        return ReturnResult(
            returned=False,
            reentered=False,
            attempts=0,
            route=route,
            elapsed=0.0,
            error=f'{type(exc).__name__}: {exc}',
        )


def _finish_failed(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    navigate: RouteFirer,
    spec: AllowedTaskSpec,
    entry: RosterEntry,
    done_before: int,
    limit: int,
    *,
    verification: str,
    detail: str,
    taps: Sequence[TapRecord],
    dismissals: Sequence[DismissReport],
    route: SurfaceRoute,
    templates: TemplateStore | None,
    return_timeout: float,
    return_interval: float,
    return_attempts: int,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
    return_state: ReturnResult | None = None,
    permission_before: PermissionReading | None,
    permission_after: PermissionReading | None = None,
    notification_screen: NotificationScreen | None = None,
    classification: Classification | None = None,
    dialog_appeared: bool = False,
) -> NotificationResult:
    state = return_state
    if state is None:
        state = _return_quietly(
            controller,
            capture,
            corpus,
            navigate,
            templates=templates,
            route=route,
            timeout=return_timeout,
            interval=return_interval,
            attempts=return_attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=logger,
        )
    if not state.returned:
        verification = 'return_failed'
        detail = f'{detail}; the shortcut route did not prove the coins page'
    return _terminal(
        spec,
        entry.title,
        NotificationOutcome.FAILED,
        verification=verification,
        detail=detail,
        limit=limit,
        done_before=done_before,
        done_after=done_before,
        taps=taps,
        dismissals=dismissals,
        return_state=state,
        classification=None if classification is None else classification.state.value,
        screen=None if notification_screen is None else notification_screen.value,
        permission_before=permission_before,
        permission_after=permission_after,
        dialog_appeared=dialog_appeared,
        logger=logger,
    )


def _terminal(
    spec: AllowedTaskSpec,
    title: str,
    outcome: NotificationOutcome,
    *,
    verification: str,
    detail: str,
    limit: int,
    done_before: int,
    done_after: int,
    taps: Sequence[TapRecord] = (),
    dismissals: Sequence[DismissReport] = (),
    return_state: ReturnResult | None = None,
    recorded: bool = False,
    dialog_appeared: bool = False,
    classification: str | None = None,
    screen: str | None = None,
    permission_before: PermissionReading | None = None,
    permission_after: PermissionReading | None = None,
    stop: str | None = None,
    logger: logging.Logger | None = None,
) -> NotificationResult:
    result = NotificationResult(
        key=spec.key,
        title=title,
        outcome=outcome,
        verification=verification,
        detail=detail,
        limit=limit,
        done_today_before=done_before,
        done_today_after=done_after,
        taps=tuple(taps),
        dismissals=tuple(dismissals),
        return_state=return_state,
        recorded=recorded,
        dialog_appeared=dialog_appeared,
        classification=classification,
        screen=screen,
        permission_before=permission_before,
        permission_after=permission_after,
        stop=stop,
    )
    if logger is not None:
        _log_result(logger, result)
    return result


def _log_result(logger: logging.Logger, result: NotificationResult) -> None:
    logger.info('%s', _json(result.as_dict()))


def _json(fields: dict[str, object]) -> str:
    import json

    return json.dumps(fields, separators=(',', ':'), sort_keys=True, default=str)


def _capture(source: CaptureSource) -> Snapshot:
    if callable(source):
        return source()
    return source.capture()


def _text_output(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    if isinstance(value, str):
        return value
    return '' if value is None else str(value)
