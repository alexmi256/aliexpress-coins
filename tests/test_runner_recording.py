"""Per-attempt failure recordings: bounded buffer, per-attempt artifacts (#124)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.runner.errors import RunInterrupted
from aliexpress_coins.runner.recording import (
    FAILURE_DIRECTORY,
    FailureRecorder,
)
from aliexpress_coins.runner.report import RunReport

STARTED = datetime(2026, 11, 22, 3, 30, tzinfo=UTC)
CAPTURED_AT = STARTED


class _Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class _Source:
    def __init__(self, *frames: Snapshot) -> None:
        self.frames = tuple(frames)
        self.calls = 0

    def capture(self) -> Snapshot:
        frame = self.frames[min(self.calls, len(self.frames) - 1)]
        self.calls += 1
        return frame


class _CallableSource:
    def __init__(self, *frames: Snapshot) -> None:
        self.frames = tuple(frames)
        self.calls = 0

    def __call__(self) -> Snapshot:
        frame = self.frames[min(self.calls, len(self.frames) - 1)]
        self.calls += 1
        return frame


class _FlakySource(_Source):
    def __init__(self, *frames: Snapshot, fail_after: int) -> None:
        super().__init__(*frames)
        self.fail_after = fail_after

    def capture(self) -> Snapshot:
        self.calls += 1
        if self.calls > self.fail_after:
            raise OSError('screencap failed during the aftermath')
        return self.frames[min(self.calls - 1, len(self.frames) - 1)]


def _box(text: str, left: float, top: float) -> TextBox:
    return TextBox(
        text=text,
        box=((left, top), (left + 30, top), (left + 30, top + 20), (left, top + 20)),
        score=0.99,
    )


def _snapshot(text: str = 'Coins', *, minute: int = 0) -> Snapshot:
    return Snapshot(
        image=Image.new('RGB', (120, 200), 'white'),
        boxes=(_box(text, 10, 20),),
        captured_at=CAPTURED_AT + timedelta(minutes=minute),
        geometry=DeviceGeometry.from_frame(120, 200, density=240, sdk=35),
    )


def _report(tmp_path: Path) -> RunReport:
    return RunReport(tmp_path / 'runs', 'run-1', budget_seconds=60.0, started_at=STARTED)


def _recorder(
    report: RunReport,
    clock: _Clock,
    *,
    aftermath_frames: int = 0,
    max_frames: int = 120,
    max_bytes: int = 4 * 1024 * 1024,
    window_seconds: float = 30.0,
    logger: logging.Logger | None = None,
) -> FailureRecorder:
    return FailureRecorder(
        report,
        clock=clock,
        sleep=clock.sleep,
        wall_clock=lambda: STARTED,
        aftermath_frames=aftermath_frames,
        aftermath_interval=0.0,
        max_frames=max_frames,
        max_bytes=max_bytes,
        window_seconds=window_seconds,
        logger=logger,
    )


def _finish_failure(recorder: FailureRecorder, *, payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    return recorder.finish(
        outcome='failed',
        verification='no_change',
        detail='the row did not move',
        completed=False,
        failure_code='task_unverified',
        payload=payload or {},
    )


def test_a_success_discards_the_buffer_and_writes_nothing(tmp_path: Path) -> None:
    report = _report(tmp_path)
    clock = _Clock()
    source = _Source(_snapshot())
    recorder = _recorder(report, clock)
    recorder.wrap(source)

    recorder.begin(key='coins_quiz', title='Take the coins quiz', category='quiz')
    recorder.capture()
    clock.sleep(1.0)
    recorder.capture()
    assert (
        recorder.finish(
            outcome='completed',
            verification='progress_badge',
            detail='the badge moved',
            completed=True,
            failure_code=None,
            payload={},
        )
        is None
    )

    assert report.failure_recordings == []
    assert not (report.run_dir / FAILURE_DIRECTORY).exists()


def test_a_skipped_outcome_discards_the_buffer_and_writes_nothing(tmp_path: Path) -> None:
    report = _report(tmp_path)
    clock = _Clock()
    recorder = _recorder(report, clock)
    recorder.wrap(_Source(_snapshot()))

    recorder.begin(key='coins_quiz', title='Take the coins quiz', category='quiz')
    recorder.capture()
    assert (
        recorder.finish(
            outcome='skipped',
            verification='already_done',
            detail='the row was already claimed',
            completed=False,
            failure_code=None,
            payload={},
        )
        is None
    )

    assert report.failure_recordings == []
    assert not (report.run_dir / FAILURE_DIRECTORY).exists()


def test_a_failure_writes_the_bounded_sequence_and_a_sidecar(tmp_path: Path) -> None:
    report = _report(tmp_path)
    clock = _Clock()
    source = _CallableSource(_snapshot(minute=0), _snapshot('Transition', minute=1), _snapshot('Home', minute=2))
    recorder = _recorder(report, clock)
    recorder.wrap(source.__call__)

    recorder.begin(key='coins_quiz', title='Take the coins quiz', category='quiz')
    for _ in range(3):
        recorder.capture()
        clock.sleep(1.0)
    payload = {
        'taps': [
            {'label': 'GO', 'point': [982, 1273], 'reason': 'start the quiz'},
            {'label': 'Coins', 'point': [100, 220], 'reason': 'answer'},
        ],
        'keys': [{'name': 'KEYCODE_BACK'}],
        'swipes': [{'label': 'roster scroll'}],
        'presses': 1,
    }
    recording = _finish_failure(recorder, payload=payload)

    assert recording is not None
    assert recording['key'] == 'coins_quiz'
    assert recording['attempt'] == 1
    assert 'coins_quiz-attempt-1-' in recording['directory']
    assert len(recording['frames']) == 3
    directory = report.run_dir / recording['directory']
    frames = sorted(directory.glob('frame-*.jpg'))
    assert len(frames) == 3
    assert frames[0].read_bytes().startswith(b'\xff\xd8\xff')
    sidecar = json.loads((directory / 'attempt.json').read_text(encoding='utf-8'))
    assert sidecar['task'] == 'Take the coins quiz'
    assert sidecar['attempt'] == 1
    assert sidecar['failure']['code'] == 'task_unverified'
    assert sidecar['failure']['verification'] == 'no_change'
    assert sidecar['tap'] == [100, 220]
    assert sidecar['point'] == [100, 220]
    assert sidecar['action']['presses'] == 1
    assert sidecar['action']['keys'] == [{'name': 'KEYCODE_BACK'}]
    assert sidecar['frames'][0]['file'] == 'frame-000.jpg'
    assert sidecar['window_seconds'] == 30.0
    assert sidecar['max_bytes'] == 4 * 1024 * 1024

    report.write()
    report_payload = json.loads(report.json_path.read_text(encoding='utf-8'))
    summary = report.summary_path.read_text(encoding='utf-8')
    assert report_payload['failure_recordings'][0]['directory'] == recording['directory']
    assert recording['directory'] in summary
    assert '## Failure recordings' in summary
    assert '| Take the coins quiz | 1 | task_unverified | 3 |' in summary


def test_two_failures_of_one_task_get_two_distinct_attempt_directories(tmp_path: Path) -> None:
    report = _report(tmp_path)
    clock = _Clock()
    recorder = _recorder(report, clock, aftermath_frames=1)
    recorder.wrap(_Source(_snapshot()))

    recordings: list[dict[str, Any]] = []
    for _ in range(2):
        recorder.begin(key='coins_quiz', title='Take the coins quiz', category='quiz')
        recorder.capture()
        recording = _finish_failure(recorder)
        assert recording is not None
        recordings.append(recording)

    assert [recording['attempt'] for recording in recordings] == [1, 2]
    directories = [recording['directory'] for recording in recordings]
    assert directories[0] != directories[1]
    assert '-attempt-1-' in directories[0]
    assert '-attempt-2-' in directories[1]
    for directory in directories:
        assert (report.run_dir / directory / 'attempt.json').is_file()


def test_the_rolling_buffer_keeps_only_the_newest_frames(tmp_path: Path) -> None:
    report = _report(tmp_path)
    clock = _Clock()
    recorder = _recorder(report, clock, max_frames=3)
    frames = [_snapshot(minute=index) for index in range(6)]
    recorder.wrap(_Source(*frames))

    recorder.begin(key='coins_quiz', title='Take the coins quiz', category='quiz')
    for _ in frames:
        recorder.capture()
        clock.sleep(1.0)
    recording = _finish_failure(recorder)

    assert recording is not None
    assert len(recording['frames']) == 3
    sidecar = json.loads((report.run_dir / recording['sidecar']).read_text(encoding='utf-8'))
    assert [entry['captured_at'] for entry in sidecar['frames']] == [
        (CAPTURED_AT + timedelta(minutes=index)).isoformat() for index in (3, 4, 5)
    ]


def test_the_rolling_buffer_drops_frames_older_than_the_window(tmp_path: Path) -> None:
    report = _report(tmp_path)
    clock = _Clock()
    recorder = _recorder(report, clock, window_seconds=2.0)
    recorder.wrap(_Source(_snapshot(minute=0), _snapshot(minute=1), _snapshot(minute=2), _snapshot(minute=3)))

    recorder.begin(key='coins_quiz', title='Take the coins quiz', category='quiz')
    for second in (0.0, 1.0, 3.0, 5.0):
        clock.value = second
        recorder.capture()
    recording = _finish_failure(recorder)

    assert recording is not None
    assert len(recording['frames']) == 2
    sidecar = json.loads((report.run_dir / recording['sidecar']).read_text(encoding='utf-8'))
    assert [entry['captured_at'] for entry in sidecar['frames']] == [
        (CAPTURED_AT + timedelta(minutes=index)).isoformat() for index in (2, 3)
    ]


def test_the_pre_action_frames_survive_the_window_and_frame_cap(tmp_path: Path) -> None:
    report = _report(tmp_path)
    clock = _Clock()
    recorder = _recorder(report, clock, window_seconds=2.0, max_frames=3)
    recorder.wrap(_Source(_snapshot(minute=0), _snapshot(minute=1), _snapshot(minute=2), _snapshot(minute=3)))

    clock.value = 0.0
    recorder.capture()
    recorder.begin(key='coins_quiz', title='Take the coins quiz', category='quiz')
    clock.value = 10.0
    recorder.capture()
    recorder.pin_action()
    clock.value = 11.0
    recorder.capture()
    clock.value = 12.0
    recorder.capture()
    recording = _finish_failure(recorder)

    assert recording is not None
    assert len(recording['frames']) == 3
    sidecar = json.loads((report.run_dir / recording['sidecar']).read_text(encoding='utf-8'))
    assert [entry['captured_at'] for entry in sidecar['frames']] == [
        (CAPTURED_AT + timedelta(minutes=index)).isoformat() for index in (0, 1, 3)
    ]


def test_the_rolling_buffer_also_honors_the_byte_cap(tmp_path: Path) -> None:
    report = _report(tmp_path)
    clock = _Clock()
    first = _snapshot(minute=0)
    second = _snapshot(minute=1)
    buffer = BytesIO()
    first.image.save(buffer, format='JPEG')
    recorder = _recorder(report, clock, max_bytes=len(buffer.getvalue()))
    recorder.wrap(_Source(first, second))

    recorder.begin(key='coins_quiz', title='Take the coins quiz', category='quiz')
    recorder.capture()
    clock.sleep(1.0)
    recorder.capture()
    recording = _finish_failure(recorder)

    assert recording is not None
    assert len(recording['frames']) == 1
    sidecar = json.loads((report.run_dir / recording['sidecar']).read_text(encoding='utf-8'))
    assert sidecar['frames'][0]['captured_at'] == (CAPTURED_AT + timedelta(minutes=1)).isoformat()


def test_pin_action_without_an_attempt_or_before_a_capture_uses_the_seed(tmp_path: Path) -> None:
    report = _report(tmp_path)
    clock = _Clock()
    recorder = _recorder(report, clock)
    recorder.wrap(_Source(_snapshot()))

    recorder.pin_action()
    recorder.capture()
    recorder.begin(key='coins_quiz', title='Take the coins quiz', category='quiz')
    recorder.pin_action()
    recording = _finish_failure(recorder)

    assert recording is not None
    assert len(recording['frames']) == 1


def test_pinned_frames_cannot_be_dropped_by_a_tiny_frame_cap(tmp_path: Path) -> None:
    report = _report(tmp_path)
    clock = _Clock()
    recorder = _recorder(report, clock, max_frames=2)
    recorder.wrap(_Source(_snapshot(minute=0), _snapshot(minute=1), _snapshot(minute=2)))

    clock.value = 0.0
    recorder.capture()
    recorder.begin(key='coins_quiz', title='Take the coins quiz', category='quiz')
    clock.value = 1.0
    recorder.capture()
    recorder.pin_action()
    recorder.max_frames = 1
    clock.value = 2.0
    recorder.capture()
    recording = _finish_failure(recorder)

    assert recording is not None
    assert len(recording['frames']) == 2


def test_the_default_wall_clock_stamps_an_aware_utc_attempt(tmp_path: Path) -> None:
    report = _report(tmp_path)
    clock = _Clock()
    recorder = FailureRecorder(report, clock=clock, sleep=clock.sleep, aftermath_frames=0)
    recorder.wrap(_Source(_snapshot()))

    recorder.begin(key='coins_quiz', title='Take the coins quiz', category='quiz')
    recorder.capture()
    recording = _finish_failure(recorder)

    assert recording is not None
    sidecar = json.loads((report.run_dir / recording['sidecar']).read_text(encoding='utf-8'))
    started_at = datetime.fromisoformat(sidecar['started_at'])
    assert started_at.tzinfo is UTC


def test_a_failed_aftermath_capture_is_a_warning_and_the_recording_still_lands(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    report = _report(tmp_path)
    clock = _Clock()
    source = _FlakySource(_snapshot(), fail_after=1)
    recorder = _recorder(report, clock, aftermath_frames=3)
    recorder.wrap(source)

    recorder.begin(key='coins_quiz', title='Take the coins quiz', category='quiz')
    recorder.capture()
    caplog.set_level(logging.WARNING, logger='aliexpress_coins.runner.recording')
    recording = _finish_failure(recorder)

    assert recording is not None
    assert len(recording['frames']) == 1
    assert caplog.text.count('failure_recording_capture_failed') == 3
    assert report.artifact_errors == []


def test_a_stop_during_the_aftermath_is_not_swallowed(tmp_path: Path) -> None:
    report = _report(tmp_path)
    clock = _Clock()

    def stop(_seconds: float) -> None:
        raise RunInterrupted('stop requested')

    recorder = FailureRecorder(
        report,
        clock=clock,
        sleep=stop,
        wall_clock=lambda: STARTED,
        aftermath_frames=1,
        aftermath_interval=0.0,
    )
    recorder.wrap(_Source(_snapshot()))

    recorder.begin(key='coins_quiz', title='Take the coins quiz', category='quiz')
    recorder.capture()
    with pytest.raises(RunInterrupted):
        _finish_failure(recorder)

    assert report.failure_recordings == []
    assert _finish_failure(recorder) is None


def test_a_write_failure_is_an_artifact_error_not_a_task_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    report = _report(tmp_path)
    clock = _Clock()
    recorder = _recorder(report, clock)
    recorder.wrap(_Source(_snapshot()))

    def broken_write(*_args: Any, **_kwargs: Any) -> None:
        raise OSError('disk full')

    monkeypatch.setattr('aliexpress_coins.runner.recording.atomic_write_bytes', broken_write)
    recorder.begin(key='coins_quiz', title='Take the coins quiz', category='quiz')
    recorder.capture()
    caplog.set_level(logging.WARNING, logger='aliexpress_coins.runner.recording')
    recording = _finish_failure(recorder)

    assert recording is None
    assert report.failure_recordings == []
    assert len(report.artifact_errors) == 1
    assert report.artifact_errors[0].startswith(
        'failure recording coins_quiz attempt 1: ArtifactError: could not write the failure recording',
    )
    assert 'failure_recording_failed' in caplog.text
    assert not (report.run_dir / FAILURE_DIRECTORY / 'coins_quiz-attempt-1-20261122T033000Z').exists()


def test_an_encode_failure_is_a_warning_and_still_leaves_a_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    report = _report(tmp_path)
    clock = _Clock()
    recorder = _recorder(report, clock)
    recorder.wrap(_Source(_snapshot()))

    def broken_save(*_args: Any, **_kwargs: Any) -> None:
        raise OSError('cannot encode')

    monkeypatch.setattr(Image.Image, 'save', broken_save)
    recorder.capture()
    recorder.begin(key='coins_quiz', title='Take the coins quiz', category='quiz')
    caplog.set_level(logging.WARNING, logger='aliexpress_coins.runner.recording')
    recorder.capture()
    recording = _finish_failure(recorder)

    assert recording is not None
    assert recording['frames'] == []
    assert caplog.text.count('failure_recording_encode_failed') == 2
    assert (report.run_dir / recording['sidecar']).is_file()


def test_action_evidence_tolerates_partial_tap_shapes(tmp_path: Path) -> None:
    report = _report(tmp_path)
    clock = _Clock()
    recorder = _recorder(report, clock)
    recorder.wrap(_Source(_snapshot()))

    recorder.begin(key='coins_quiz', title='Take the coins quiz', category='quiz')
    recorder.capture()
    recording = _finish_failure(
        recorder,
        payload={
            'taps': [{'point': [10, 20]}, 'not a tap', {'point': 'not a point'}],
            'keys': 'not a list',
        },
    )

    assert recording is not None
    sidecar = json.loads((report.run_dir / recording['sidecar']).read_text(encoding='utf-8'))
    assert sidecar['tap'] == [10, 20]
    assert sidecar['action']['keys'] == []
    assert len(sidecar['action']['taps']) == 3


def test_a_colliding_directory_name_gets_a_suffix_and_a_path_safe_key(tmp_path: Path) -> None:
    report = _report(tmp_path)
    clock = _Clock()
    recorder = _recorder(report, clock)
    recorder.wrap(_Source(_snapshot()))
    base = report.run_dir / FAILURE_DIRECTORY / 'coins-quiz-attempt-1-20261122T033000Z'
    base.mkdir(parents=True)

    recorder.begin(key='../coins quiz', title='Take the coins quiz', category='quiz')
    recorder.capture()
    recording = _finish_failure(recorder)

    assert recording is not None
    assert recording['directory'].startswith(f'{FAILURE_DIRECTORY}/coins-quiz-attempt-1-')
    assert recording['directory'].endswith('-2')
    assert '..' not in recording['directory']


def test_finish_without_an_active_attempt_is_a_noop(tmp_path: Path) -> None:
    report = _report(tmp_path)
    recorder = _recorder(report, _Clock())

    assert _finish_failure(recorder) is None
    recorder.abandon()
    assert report.failure_recordings == []


@pytest.mark.parametrize(
    ('kwargs', 'message'),
    [
        ({'window_seconds': 0.0}, 'window_seconds must be positive'),
        ({'max_frames': 0}, 'max_frames must be positive'),
        ({'max_bytes': 0}, 'max_bytes must be positive'),
        ({'aftermath_frames': -1}, 'aftermath_frames must not be negative'),
        ({'aftermath_interval': -1.0}, 'aftermath_interval must not be negative'),
    ],
)
def test_the_recorder_validates_its_bounds(tmp_path: Path, kwargs: dict[str, Any], message: str) -> None:
    report = _report(tmp_path)
    with pytest.raises(ValueError, match=message):
        FailureRecorder(report, **kwargs)


def test_the_source_must_be_wrapped_before_capture(tmp_path: Path) -> None:
    recorder = _recorder(_report(tmp_path), _Clock())
    with pytest.raises(RuntimeError, match='no capture source'):
        recorder.capture()
