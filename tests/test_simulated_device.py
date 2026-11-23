"""The simulated-device harness: shared adb runner and recorded frames (#36).

Nothing here reaches the tablet. The transport tests construct the real clients
around `ScriptedAdbRunner`, and the frame tests replay the reviewed PNGs under
`tests/data/` through `ScriptedSnapshotter`. The autouse guards in
`tests/conftest.py` are the backstop; the test below proves the new runner does
not weaken them.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image

from aliexpress_coins.control.device import AdbInputDevice
from aliexpress_coins.perception.capture import AdbSnapshotter
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.layout import group_rows
from aliexpress_coins.perception.ocr import RapidOcrEngine
from aliexpress_coins.perception.snapshot import Snapshot
from aliexpress_coins.perception.waits import WaitStatus, wait_until_stable
from aliexpress_coins.transport import AdbResult
from tests.support.adb import RecordingAdbRunner, ScriptedAdbRunner, ok
from tests.support.frames import ScriptedSnapshotter

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'tests' / 'data'
COINS_PAGE = DATA / 'boost.png'
CAPTURED_AT = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)

# The measured task rows in tests/data/boost.png, the same values
# tests/test_perception_layout.py asserts. Reproducing them through the replayed
# directory is the acceptance criterion.
EXPECTED_ACTIONS = {
    'Daily check-in': 357.0,
    'Explore sponsored items': 688.5,
    'Browse surprise items': 1046.0,
    'Place an order on this page': 1404.0,
}
EXPECTED_PROGRESS = {
    'Daily check-in': None,
    'Explore sponsored items': (0, 6),
    'Browse surprise items': (0, 2),
    'Place an order on this page': (0, 3),
}


@pytest.fixture(scope='module')
def coins_page() -> Snapshot:
    """The recorded coins page, OCR'd once through the scripted snapshotter."""
    snapshotter = ScriptedSnapshotter.from_directory(
        DATA,
        pattern='boost.png',
        ocr=RapidOcrEngine(),
        clock=lambda: CAPTURED_AT,
    )
    return snapshotter.capture()


@pytest.mark.slow
def test_a_recorded_coins_page_reproduces_the_same_task_rows(coins_page: Snapshot) -> None:
    rows = group_rows(coins_page.boxes, coins_page.geometry)
    task_rows = [row for row in rows if row.action() is not None]

    titles = {row.boxes[0].text: row for row in task_rows}
    assert set(titles) == set(EXPECTED_ACTIONS)
    for title, action_y in EXPECTED_ACTIONS.items():
        row = titles[title]
        action = row.action()
        assert action is not None
        assert action.text == 'GO'
        assert action.center[1] == pytest.approx(action_y)

    assert {title: row.progress and (row.progress.completed, row.progress.total) for title, row in titles.items()} == {
        title: EXPECTED_PROGRESS[title] for title in EXPECTED_ACTIONS
    }


def test_the_scripted_runner_drives_the_real_capture_client() -> None:
    frame = COINS_PAGE.read_bytes()
    runner = ScriptedAdbRunner(AdbResult(0, frame, b''))
    snapshotter = AdbSnapshotter(
        'tablet',
        geometry=DeviceGeometry.from_frame(1200, 1520, density=240, sdk=35),
        runner=runner,
        clock=lambda: CAPTURED_AT,
    )

    snapshot = snapshotter.capture()

    assert runner.calls == [('adb', '-s', 'tablet', 'exec-out', 'screencap', '-p')]
    assert snapshot.image.size == (1200, 1520)
    assert snapshot.captured_at == CAPTURED_AT


def test_the_scripted_runner_drives_the_real_input_client() -> None:
    runner = ScriptedAdbRunner(ok())
    device = AdbInputDevice('tablet', runner=runner, timeout=4.0)

    device.tap(640, 1200)

    assert runner.calls == [('adb', '-s', 'tablet', 'shell', 'input', 'tap', '640', '1200')]
    assert runner.timeouts == [4.0]


def test_an_unscripted_call_fails_loudly_and_never_spawns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner has no fallback; exhaustion is an assertion, not a real adb."""

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError('the scripted runner must never fall through to subprocess.run')

    monkeypatch.setattr(subprocess, 'run', forbidden)
    snapshotter = AdbSnapshotter('tablet', runner=ScriptedAdbRunner())

    with pytest.raises(AssertionError, match=r'no response for.*never fall through to a real device'):
        snapshotter.capture_png()


def test_the_existing_process_guard_still_bites() -> None:
    """The cited guard from tests/test_package.py, not replaced by the new seam."""
    with pytest.raises(AssertionError, match='must not spawn processes'):
        subprocess.run(['adb', 'devices'], check=False)


def test_the_scripted_runner_can_respond_by_argv() -> None:
    def responder(argv: tuple[str, ...]) -> AdbResult | None:
        if argv[-2:] == ('wm', 'size'):
            return ok('Physical size: 1200x2000\n')
        if argv[-2:] == ('wm', 'density'):
            return ok('Physical density: 240\n')
        if argv[-2:] == ('getprop', 'ro.build.version.sdk'):
            return ok('35\n')
        return None

    runner = ScriptedAdbRunner(responder=responder)
    snapshotter = AdbSnapshotter('tablet', runner=runner)

    geometry = snapshotter.probe_geometry()

    assert (geometry.width, geometry.height, geometry.density, geometry.sdk) == (1200, 2000, 240, 35)
    assert len(runner.calls) == 3


def test_recording_writes_a_transcript_that_replays(tmp_path: Path) -> None:
    transcript = tmp_path / 'session.json'
    inner = ScriptedAdbRunner(
        ok('device'),
        AdbResult(1, b'\x89PNG\r\n\x1a\n', b'warn'),
    )
    recorder = RecordingAdbRunner(inner, transcript)

    first = recorder(('adb', '-s', 'tablet', 'get-state'), 10.0)
    second = recorder(('adb', '-s', 'tablet', 'exec-out', 'screencap', '-p'), 30.0)

    assert first == ok('device')
    assert second == AdbResult(1, b'\x89PNG\r\n\x1a\n', b'warn')
    assert transcript.is_file()

    replay = ScriptedAdbRunner.from_transcript(transcript)
    assert replay(('adb', '-s', 'tablet', 'get-state'), 10.0) == first
    assert replay(('adb', '-s', 'tablet', 'exec-out', 'screencap', '-p'), 30.0) == second


def test_a_transcript_replay_rejects_a_different_command(tmp_path: Path) -> None:
    transcript = tmp_path / 'session.json'
    RecordingAdbRunner(ScriptedAdbRunner(ok('device')), transcript)(('adb', 'get-state'), 5.0)
    replay = ScriptedAdbRunner.from_transcript(transcript)

    with pytest.raises(AssertionError, match=r'expected.*get-state.*sent'):
        replay(('adb', 'devices'), 5.0)


def test_a_responder_can_fall_through_to_the_queue_and_failures_raise() -> None:
    runner = ScriptedAdbRunner(OSError('device offline'), responder=lambda _argv: None)

    with pytest.raises(OSError, match='device offline'):
        runner(('adb', 'get-state'), 5.0)

    assert runner.remaining == 0


def test_the_snapshotter_replays_a_directory_and_can_repeat_a_frame() -> None:
    snapshotter = ScriptedSnapshotter.from_directory(DATA / 'states', pattern='coins.png', repeat=2)

    first = snapshotter.capture()
    second = snapshotter.capture()
    third = snapshotter.capture()

    assert first.geometry.width == 1200
    assert first.geometry.height == 2000
    assert first.captured_at.tzinfo is not None
    # `repeat=2` is the stability-wait helper: the same recorded frame twice.
    assert first.image.tobytes() == second.image.tobytes()
    assert third.image.tobytes() == second.image.tobytes()


def test_the_snapshotter_honours_a_capture_manifest(tmp_path: Path) -> None:
    path = tmp_path / 'frame.png'
    Image.new('RGB', (80, 120), 'white').save(path)
    (tmp_path / 'manifest.json').write_text(
        '{"width": 80, "height": 120, "density": 320, "sdk": 34}',
        encoding='utf-8',
    )

    snapshot = ScriptedSnapshotter.from_directory(tmp_path).capture()

    assert (snapshot.geometry.width, snapshot.geometry.height) == (80, 120)
    assert snapshot.geometry.density == 320
    assert snapshot.geometry.sdk == 34


def test_a_per_call_override_replaces_one_frame() -> None:
    base = _frame('white')
    after_tap = _frame('black')
    overridden = _frame('red')
    snapshotter = ScriptedSnapshotter([base, after_tap])
    snapshotter.override_call(0, overridden)

    assert snapshotter.capture().image.getpixel((0, 0)) == (255, 0, 0)
    assert snapshotter.capture().image.getpixel((0, 0)) == (0, 0, 0)
    assert snapshotter.capture().image.getpixel((0, 0)) == (0, 0, 0)
    assert snapshotter.calls == 3


def test_the_snapshotter_replays_the_same_frame_for_a_stability_wait() -> None:
    frame = _frame('white')
    snapshotter = ScriptedSnapshotter([frame, frame])
    clock = _Clock()

    result = wait_until_stable(
        snapshotter,
        timeout=5.0,
        interval=0.5,
        hold=0.5,
        clock=clock,
        sleep=clock.sleep,
    )

    assert result.status is WaitStatus.SATISFIED
    assert snapshotter.calls >= 2


def test_the_snapshotter_rejects_an_empty_script_and_a_negative_override(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match='at least one frame'):
        ScriptedSnapshotter([])
    with pytest.raises(ValueError, match='no frames match'):
        ScriptedSnapshotter.from_directory(tmp_path, pattern='*.png')
    snapshotter = ScriptedSnapshotter([_frame('white')])
    with pytest.raises(ValueError, match='cannot be negative'):
        snapshotter.override_call(-1, _frame('black'))


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def _frame(colour: str) -> Snapshot:
    image = Image.new('RGB', (40, 60), colour)
    return Snapshot(
        image=image,
        boxes=(),
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(40, 60, density=240, sdk=35),
    )
