"""Per-attempt failure recordings for a task that did not verify (#124).

The recorder wraps the run's capture seam, so every frame the perception path
already takes during an attempt is kept in a bounded rolling buffer. It never
opens a second adb capture channel and it does not capture on a successful task:
a good night writes nothing here.

On a failed attempt the last :data:`DEFAULT_FAILURE_WINDOW_SECONDS` of frames
are written with a small aftermath burst, one directory per attempt::

    runs/<run-id>/failures/<key>-attempt-<n>-<UTC timestamp>/
        attempt.json
        frame-000.png
        frame-001.png
        ...

The sidecar names the task, attempt, failure code, verification and every tap,
key or swipe the executor reported. The engine adds the directory and frame
paths to ``run.json`` and ``summary.md``; run retention removes the whole run
directory with the rest of the night's evidence.

Memory and disk stay bounded: the buffer keeps at most
:data:`DEFAULT_FAILURE_MAX_FRAMES` compressed PNG frames and drops anything
older than the window. A successful attempt discards the buffer, and writing a
recording is best-effort: a capture or write failure is logged and recorded in
``artifact_errors`` rather than turning a task failure into a run failure.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from aliexpress_coins.perception.artifacts import atomic_write_bytes
from aliexpress_coins.perception.errors import ArtifactError
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.state import CaptureSource
from aliexpress_coins.runner.errors import RunInterrupted
from aliexpress_coins.runner.report import RunReport
from aliexpress_coins.runner.safety import RunAborted

__all__ = [
    'DEFAULT_FAILURE_AFTERMATH_FRAMES',
    'DEFAULT_FAILURE_AFTERMATH_INTERVAL_SECONDS',
    'DEFAULT_FAILURE_MAX_FRAMES',
    'DEFAULT_FAILURE_WINDOW_SECONDS',
    'FAILURE_DIRECTORY',
    'FailureRecorder',
]

LOG = logging.getLogger('aliexpress_coins.runner.recording')

FAILURE_DIRECTORY = 'failures'
"""Where failure recordings live inside the run directory."""

DEFAULT_FAILURE_WINDOW_SECONDS = 30.0
"""How much of an attempt's own frame stream a failure retains.

Thirty seconds covers a tap, its transition and the verification wait around a
normal task while still being a small rolling window. Frames older than this
are dropped before they are ever written.
"""

DEFAULT_FAILURE_MAX_FRAMES = 120
"""The hard frame cap, roughly four frames per second for the window.

The cap bounds the buffer even when a task captures much faster than the
screenshots it needs; the oldest frame is dropped first.
"""

DEFAULT_FAILURE_AFTERMATH_FRAMES = 3
"""How many extra frames are taken after a failure.

The task's own captures prove the tap and transition; these three give the
post-tap screen the #116 review lacked, even when the executor failed before
its next observation.
"""

DEFAULT_FAILURE_AFTERMATH_INTERVAL_SECONDS = 0.4
"""The pause between aftermath frames, about 2.5 frames per second."""

_WallClock = Callable[[], datetime]
_MonotonicClock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class _RecordedFrame:
    """One compressed frame and the OCR metadata that explains it."""

    png: bytes
    boxes: tuple[TextBox, ...]
    captured_at: datetime
    geometry: DeviceGeometry
    observed_at: float
    width: int
    height: int


@dataclass(slots=True)
class _Attempt:
    """The rolling buffer for one dispatch attempt."""

    key: str
    title: str
    category: str | None
    number: int
    started_at: datetime
    started_monotonic: float
    frames: deque[_RecordedFrame] = field(default_factory=deque)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _event(name: str, **fields: object) -> str:
    return json.dumps({'event': name, **fields}, separators=(',', ':'), sort_keys=True, default=str)


def _safe_component(value: str) -> str:
    cleaned = re.sub(r'[^A-Za-z0-9_.-]+', '-', value).strip('.-')
    return cleaned or 'task'


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


class FailureRecorder:
    """Keep the last frames of each attempt and write them only on failure."""

    def __init__(
        self,
        report: RunReport,
        *,
        clock: _MonotonicClock = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: _WallClock = _utc_now,
        window_seconds: float = DEFAULT_FAILURE_WINDOW_SECONDS,
        max_frames: int = DEFAULT_FAILURE_MAX_FRAMES,
        aftermath_frames: int = DEFAULT_FAILURE_AFTERMATH_FRAMES,
        aftermath_interval: float = DEFAULT_FAILURE_AFTERMATH_INTERVAL_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError(f'window_seconds must be positive, got {window_seconds!r}')
        if max_frames < 1:
            raise ValueError(f'max_frames must be positive, got {max_frames!r}')
        if aftermath_frames < 0:
            raise ValueError(f'aftermath_frames must not be negative, got {aftermath_frames!r}')
        if aftermath_interval < 0:
            raise ValueError(f'aftermath_interval must not be negative, got {aftermath_interval!r}')
        self.report = report
        self.window_seconds = window_seconds
        self.max_frames = max_frames
        self.aftermath_frames = aftermath_frames
        self.aftermath_interval = aftermath_interval
        self._clock = clock
        self._sleep = sleep
        self._wall_clock = wall_clock
        self._logger = logger if logger is not None else LOG
        self._source: CaptureSource | None = None
        self._active: _Attempt | None = None
        self._last_frame: Snapshot | None = None
        self._attempts: dict[str, int] = {}

    def wrap(self, source: CaptureSource) -> Callable[[], Snapshot]:
        """Return the run's capture seam with this recorder in front of it."""
        self._source = source
        return self.capture

    def capture(self) -> Snapshot:
        """Capture one frame through the real seam and buffer it when recording."""
        if self._source is None:  # pragma: no cover - wrap() is called before use
            raise RuntimeError('the failure recorder has no capture source')
        frame = self._source() if callable(self._source) else self._source.capture()
        self._last_frame = frame
        attempt = self._active
        if attempt is not None:
            recorded = self._encode(frame)
            if recorded is not None:
                attempt.frames.append(recorded)
                self._trim(attempt, self._clock())
        return frame

    def begin(self, *, key: str, title: str, category: str | None = None) -> int:
        """Start buffering one dispatch attempt and return its 1-based number."""
        self._active = None
        number = self._attempts.get(key, 0) + 1
        self._attempts[key] = number
        started_at = self._wall_clock().astimezone(UTC)
        attempt = _Attempt(
            key=key,
            title=title,
            category=category,
            number=number,
            started_at=started_at,
            started_monotonic=self._clock(),
            frames=deque(maxlen=self.max_frames),
        )
        if self._last_frame is not None:
            recorded = self._encode(self._last_frame)
            if recorded is not None:
                attempt.frames.append(recorded)
        self._active = attempt
        self._logger.debug('%s', _event('failure_recording_started', key=key, attempt=number))
        return number

    def finish(
        self,
        *,
        outcome: str,
        verification: str,
        detail: str,
        completed: bool,
        failure_code: str | None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Discard a success; write one per-attempt artifact for a failure."""
        attempt = self._active
        if attempt is None:
            return None
        try:
            if completed or outcome != 'failed':
                self._logger.debug(
                    '%s',
                    _event(
                        'failure_recording_discarded',
                        key=attempt.key,
                        attempt=attempt.number,
                        outcome=outcome,
                    ),
                )
                return None
            try:
                self._aftermath()
            except RunAborted, RunInterrupted:
                raise
            self._trim(attempt, self._clock())
            return self._write(
                attempt,
                outcome=outcome,
                verification=verification,
                detail=detail,
                failure_code=failure_code,
                payload=payload or {},
            )
        except RunAborted, RunInterrupted:
            raise
        except Exception as exc:
            detail_text = f'{type(exc).__name__}: {exc}'
            self._logger.warning(
                '%s',
                _event(
                    'failure_recording_failed',
                    key=attempt.key,
                    attempt=attempt.number,
                    detail=detail_text,
                ),
            )
            self.report.record_artifact_error(
                f'failure recording {attempt.key} attempt {attempt.number}: {detail_text}'
            )
            return None
        finally:
            self._active = None

    def abandon(self) -> None:
        """Drop the current buffer without writing anything (run interrupted)."""
        self._active = None

    def _aftermath(self) -> None:
        for index in range(self.aftermath_frames):
            try:
                self._sleep(self.aftermath_interval)
                self.capture()
            except RunAborted, RunInterrupted:
                raise
            except Exception as exc:
                self._logger.warning(
                    '%s',
                    _event(
                        'failure_recording_capture_failed',
                        frame=index + 1,
                        detail=f'{type(exc).__name__}: {exc}',
                    ),
                )

    def _trim(self, attempt: _Attempt, now: float) -> None:
        cutoff = now - self.window_seconds
        while attempt.frames and attempt.frames[0].observed_at < cutoff:
            attempt.frames.popleft()

    def _encode(self, frame: Snapshot) -> _RecordedFrame | None:
        try:
            buffer = BytesIO()
            frame.image.save(buffer, format='PNG')
        except OSError as exc:
            self._logger.warning(
                '%s',
                _event('failure_recording_encode_failed', detail=f'{type(exc).__name__}: {exc}'),
            )
            return None
        return _RecordedFrame(
            png=buffer.getvalue(),
            boxes=frame.boxes,
            captured_at=frame.captured_at,
            geometry=frame.geometry,
            observed_at=self._clock(),
            width=frame.width,
            height=frame.height,
        )

    def _write(
        self,
        attempt: _Attempt,
        *,
        outcome: str,
        verification: str,
        detail: str,
        failure_code: str | None,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        run_dir = self.report.run_dir
        failures_dir = run_dir / FAILURE_DIRECTORY
        base_name = (
            f'{_safe_component(attempt.key)}-attempt-{attempt.number}-{attempt.started_at.strftime("%Y%m%dT%H%M%SZ")}'
        )
        directory = _unique_directory(failures_dir, base_name)
        frame_payloads: list[dict[str, Any]] = []
        try:
            for index, frame in enumerate(attempt.frames):
                file_name = f'frame-{index:03d}.png'
                atomic_write_bytes(directory / file_name, frame.png)
                frame_payloads.append(
                    {
                        'file': file_name,
                        'captured_at': frame.captured_at.isoformat(),
                        'offset_seconds': round(max(0.0, frame.observed_at - attempt.started_monotonic), 3),
                        'width': frame.width,
                        'height': frame.height,
                    },
                )
            ended_at = self._wall_clock().astimezone(UTC)
            action = _action_evidence(payload)
            sidecar = {
                'schema': 1,
                'name': directory.name,
                'task': attempt.title,
                'key': attempt.key,
                'category': attempt.category,
                'attempt': attempt.number,
                'started_at': attempt.started_at.isoformat(),
                'ended_at': ended_at.isoformat(),
                'window_seconds': self.window_seconds,
                'max_frames': self.max_frames,
                'aftermath_frames': self.aftermath_frames,
                'outcome': outcome,
                'completed': False,
                'failure': {
                    'code': failure_code,
                    'verification': verification,
                    'detail': detail,
                },
                'action': action,
                'tap': action['last_point'],
                'point': action['last_point'],
                'frames': frame_payloads,
            }
            atomic_write_bytes(
                directory / 'attempt.json',
                (json.dumps(sidecar, ensure_ascii=False, indent=2, sort_keys=True, default=str) + '\n').encode('utf-8'),
            )
        except (OSError, ArtifactError) as exc:
            shutil.rmtree(directory, ignore_errors=True)
            raise ArtifactError(f'could not write the failure recording in {directory}: {exc}') from exc

        relative_dir = directory.relative_to(run_dir).as_posix()
        recording: dict[str, Any] = {
            'task': attempt.title,
            'key': attempt.key,
            'category': attempt.category,
            'attempt': attempt.number,
            'started_at': attempt.started_at.isoformat(),
            'outcome': outcome,
            'failure_code': failure_code,
            'verification': verification,
            'detail': detail,
            'directory': relative_dir,
            'frames': [f'{relative_dir}/{frame["file"]}' for frame in frame_payloads],
            'sidecar': f'{relative_dir}/attempt.json',
        }
        self.report.record_failure_recording(recording)
        self._logger.info(
            '%s',
            _event(
                'failure_recorded',
                key=attempt.key,
                attempt=attempt.number,
                directory=relative_dir,
                frames=len(frame_payloads),
            ),
        )
        return recording


def _unique_directory(parent: Path, base_name: str) -> Path:
    """Resolve a fresh attempt directory, never overwriting an earlier attempt."""
    candidate = parent / base_name
    suffix = 2
    while candidate.exists():
        candidate = parent / f'{base_name}-{suffix}'
        suffix += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def _action_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The taps/keys/swipes a report already contains, plus the last tap point."""
    taps = _sequence(payload.get('taps'))
    keys = _sequence(payload.get('keys'))
    swipes = _sequence(payload.get('swipes'))
    last_point: list[Any] | None = None
    for tap in reversed(taps):
        if isinstance(tap, Mapping):
            point = tap.get('point')
            if isinstance(point, Sequence) and not isinstance(point, (str, bytes, bytearray)):
                last_point = list(point)
                break
    return {
        'taps': taps,
        'keys': keys,
        'swipes': swipes,
        'presses': payload.get('presses'),
        'last_point': last_point,
    }
