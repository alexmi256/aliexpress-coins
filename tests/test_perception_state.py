"""Screen-state classification over recorded frames (issue #16)."""

from __future__ import annotations

import statistics
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from PIL import Image, ImageDraw

from aliexpress_coins.perception import state as state_module
from aliexpress_coins.perception.errors import StateTimeout
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.matching import find_text
from aliexpress_coins.perception.ocr import RapidOcrEngine
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.state import (
    Classification,
    Evidence,
    ScreenState,
    classify,
    classify_state,
    modal_bounds,
    wait_for_state,
)

CAPTURED_AT = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
DATA = Path(__file__).parent / 'data'
STATES = DATA / 'states'

_EXPECTED: dict[str, ScreenState] = {
    'coins.png': ScreenState.PAGE,
    'account.png': ScreenState.PAGE,
    'launch_modal.png': ScreenState.POPUP,
    'skeleton.png': ScreenState.LOADING,
    'blank_webview.png': ScreenState.ERROR,
    'boost.png': ScreenState.PAGE,
    'quiz.png': ScreenState.PAGE,
    'surprise_grid.png': ScreenState.PAGE,
    'popup_over_page.png': ScreenState.POPUP,
    'ambiguous_feed.png': ScreenState.UNKNOWN,
}


def _geometry(width: int, height: int) -> DeviceGeometry:
    return DeviceGeometry.from_frame(width, height, density=240, sdk=35)


def _snapshot(
    image: Image.Image,
    boxes: tuple[TextBox, ...] = (),
) -> Snapshot:
    return Snapshot(
        image=image,
        boxes=boxes,
        captured_at=CAPTURED_AT,
        geometry=_geometry(image.width, image.height),
    )


def _box(text: str, left: float, top: float, right: float, bottom: float) -> TextBox:
    return TextBox(
        text=text,
        box=((left, top), (right, top), (right, bottom), (left, bottom)),
        score=0.95,
    )


def _load_snapshot(path: Path, engine: RapidOcrEngine) -> Snapshot:
    with Image.open(path) as opened:
        image = opened.convert('RGB')
    return _snapshot(image, engine.read(image))


@pytest.fixture(scope='module')
def frames() -> dict[str, Snapshot]:
    engine = RapidOcrEngine()
    loaded: dict[str, Snapshot] = {}
    for name in _EXPECTED:
        loaded[name] = _load_snapshot(STATES / name, engine)
    return loaded


@pytest.mark.parametrize(('name', 'expected'), tuple(_EXPECTED.items()))
def test_the_recorded_frames_classify_as_expected(
    frames: dict[str, Snapshot],
    name: str,
    expected: ScreenState,
) -> None:
    classification = classify(frames[name])

    assert classification.state is expected, classification.describe()


def test_page_evidence_names_the_marker_that_fired(frames: dict[str, Snapshot]) -> None:
    classification = classify(frames['coins.png'])

    assert classification.state is ScreenState.PAGE
    assert classification.score > 0.9
    assert classification.evidence[0].signal == 'page_marker'
    assert 'Daily coins' in classification.evidence[0].detail
    assert 'page_marker' in classification.describe()
    assert classify_state(frames['coins.png']) is ScreenState.PAGE


def test_popup_evidence_distinguishes_a_headline_from_a_panel(frames: dict[str, Snapshot]) -> None:
    launch = classify(frames['launch_modal.png'])
    overlay = classify(frames['popup_over_page.png'])

    assert launch.state is ScreenState.POPUP
    assert launch.evidence[0].signal == 'popup_marker'
    assert overlay.state is ScreenState.POPUP
    assert overlay.evidence[0].signal == 'popup_panel'
    assert 'area=' in overlay.evidence[0].detail


def test_modal_bounds_are_measured_from_the_panel_not_the_screen(frames: dict[str, Snapshot]) -> None:
    bounds = modal_bounds(frames['popup_over_page.png'])

    assert bounds is not None
    left, top, right, bottom = bounds
    assert left == pytest.approx(121.7, abs=0.1)
    assert top == pytest.approx(640.0, abs=0.1)
    assert right == pytest.approx(1078.3, abs=0.1)
    assert bottom == pytest.approx(1640.0, abs=0.1)
    assert modal_bounds(frames['launch_modal.png']) is None
    assert modal_bounds(frames['coins.png']) is None


def test_loading_and_blank_evidence_carry_frame_statistics(frames: dict[str, Snapshot]) -> None:
    skeleton = classify(frames['skeleton.png'])
    blank = classify(frames['blank_webview.png'])

    assert skeleton.state is ScreenState.LOADING
    assert skeleton.evidence[0].signal == 'skeleton'
    assert 'edge fraction=' in skeleton.evidence[0].detail
    assert blank.state is ScreenState.ERROR
    assert blank.evidence[0].signal == 'blank_frame'
    assert 'luminance range=' in blank.evidence[0].detail


def test_full_captures_keep_the_clock_and_taskbar_but_ignore_them(frames: dict[str, Snapshot]) -> None:
    for name, expected in (
        ('skeleton.png', ScreenState.LOADING),
        ('blank_webview.png', ScreenState.ERROR),
    ):
        snapshot = frames[name]
        assert (snapshot.width, snapshot.height) == (1200, 2000)
        assert any(box.center[1] < snapshot.height * 0.03 for box in snapshot.boxes)
        assert any(box.center[1] > snapshot.height * 0.90 for box in snapshot.boxes)
        assert classify(snapshot).state is expected


def test_launch_modal_without_its_cta_still_has_a_scrim_signal(frames: dict[str, Snapshot]) -> None:
    snapshot = frames['launch_modal.png']
    boxes = tuple(box for box in snapshot.boxes if 'tap to claim' not in box.text.lower())

    classification = classify(_snapshot(snapshot.image, boxes))

    assert classification.state is ScreenState.POPUP
    assert classification.evidence[0].signal == 'dimmed_scrim'


def test_an_unmodelled_page_is_unknown_not_a_guess(frames: dict[str, Snapshot]) -> None:
    classification = classify(frames['ambiguous_feed.png'])

    assert classification.state is ScreenState.UNKNOWN
    assert classification.score == 0.0
    assert classification.evidence[0].signal == 'no_signal'
    assert 'no known marker' in classification.evidence[0].detail


@pytest.mark.parametrize('colour', ['white', 'black'])
def test_a_uniform_frame_is_never_a_page(colour: str) -> None:
    classification = classify(_snapshot(Image.new('RGB', (400, 800), colour)))

    assert classification.state is ScreenState.ERROR
    assert classification.evidence[0].signal == 'blank_frame'


def test_a_single_fragment_on_a_blank_frame_is_loading_not_a_page() -> None:
    image = Image.new('RGB', (400, 800), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((180, 390, 220, 410), fill='black')
    box = _box('Loading', 180, 390, 220, 410)

    classification = classify(_snapshot(image, (box,)))

    assert classification.state is ScreenState.LOADING
    assert classification.evidence[0].signal == 'few_text_fragments'


def test_classification_is_well_under_an_ocr_pass(frames: dict[str, Snapshot]) -> None:
    snapshot = frames['boost.png']
    timings: list[float] = []
    for _ in range(5):
        started = time.perf_counter()
        classify(snapshot)
        timings.append(time.perf_counter() - started)

    # The measured OCR pass on this fixture class is ~1.3 s (docs/01 §3); the
    # classifier reuses the boxes and only does grayscale statistics.
    assert statistics.median(timings) < 0.25


class _ScriptedClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def _scripted_classification(state: ScreenState, detail: str = 'scripted') -> Classification:
    return Classification(
        state=state,
        score=1.0,
        evidence=(Evidence('scripted', 1.0, detail),),
    )


def _scripted_capture(monkeypatch: pytest.MonkeyPatch, states: list[ScreenState]) -> list[ScreenState]:
    seen: list[ScreenState] = []
    queue = list(states)

    def fake_classify(_snapshot: Snapshot) -> Classification:
        state = queue.pop(0)
        seen.append(state)
        return _scripted_classification(state)

    monkeypatch.setattr(state_module, 'classify', fake_classify)
    return seen


def _dummy_snapshot() -> Snapshot:
    return _snapshot(Image.new('RGB', (10, 20), 'white'))


def test_wait_for_state_returns_the_observation_history(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _ScriptedClock()
    seen = _scripted_capture(
        monkeypatch,
        [ScreenState.LOADING, ScreenState.LOADING, ScreenState.PAGE],
    )

    result = wait_for_state(
        _dummy_snapshot,
        ScreenState.PAGE,
        timeout=5,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert result.state is ScreenState.PAGE
    assert result.history == (ScreenState.LOADING, ScreenState.LOADING, ScreenState.PAGE)
    assert result.elapsed == pytest.approx(2.0)
    assert seen == list(result.history)


def test_wait_for_popup_after_page_ignores_a_popup_seen_during_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _ScriptedClock()
    _scripted_capture(
        monkeypatch,
        [ScreenState.LOADING, ScreenState.POPUP, ScreenState.PAGE, ScreenState.POPUP],
    )

    result = wait_for_state(
        _dummy_snapshot,
        ScreenState.POPUP,
        after=ScreenState.PAGE,
        timeout=10,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert result.history == (
        ScreenState.LOADING,
        ScreenState.POPUP,
        ScreenState.PAGE,
        ScreenState.POPUP,
    )
    assert result.elapsed == pytest.approx(3.0)


def test_wait_for_state_accepts_a_set_and_reports_each_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _ScriptedClock()
    _scripted_capture(monkeypatch, [ScreenState.LOADING, ScreenState.ERROR])
    seen: list[Classification] = []

    result = wait_for_state(
        _dummy_snapshot,
        (ScreenState.PAGE, ScreenState.ERROR),
        timeout=5,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
        on_frame=seen.append,
    )

    assert result.state is ScreenState.ERROR
    assert len(seen) == 2
    assert seen[-1] is result.classification


def test_wait_for_state_times_out_with_the_last_state_and_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _ScriptedClock()
    _scripted_capture(monkeypatch, [ScreenState.LOADING, ScreenState.LOADING, ScreenState.LOADING])

    with pytest.raises(StateTimeout) as raised:
        wait_for_state(
            _dummy_snapshot,
            ScreenState.PAGE,
            timeout=2,
            interval=1,
            clock=clock,
            sleep=clock.sleep,
        )

    message = str(raised.value)
    assert 'not observed within 2s' in message
    assert 'loading -> loading -> loading' in message


def test_wait_for_state_requires_positive_times_and_a_target() -> None:
    with pytest.raises(ValueError, match='timeout'):
        wait_for_state(_dummy_snapshot, ScreenState.PAGE, timeout=0, interval=1)
    with pytest.raises(ValueError, match='interval'):
        wait_for_state(_dummy_snapshot, ScreenState.PAGE, timeout=1, interval=0)
    with pytest.raises(ValueError, match='target'):
        wait_for_state(_dummy_snapshot, (), timeout=1, interval=1)
    with pytest.raises(ValueError, match='after'):
        wait_for_state(_dummy_snapshot, ScreenState.PAGE, after=(), timeout=1, interval=1)


def test_wait_for_state_accepts_a_snapshotter_object(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _ScriptedClock()
    _scripted_capture(monkeypatch, [ScreenState.PAGE])

    class _Source:
        def capture(self) -> Snapshot:
            return _dummy_snapshot()

    result = wait_for_state(
        _Source(),
        ScreenState.PAGE,
        timeout=1,
        interval=1,
        clock=clock,
        sleep=clock.sleep,
    )

    assert result.state is ScreenState.PAGE


def test_the_boost_fixture_uses_the_real_page_markers(frames: dict[str, Snapshot]) -> None:
    snapshot = frames['boost.png']

    assert find_text(snapshot, 'Coins boost') is not None
    assert find_text(snapshot, 'Explore sponsored items') is not None
    assert classify(snapshot).state is ScreenState.PAGE


def test_evidence_rejects_a_score_outside_zero_to_one() -> None:
    with pytest.raises(ValueError, match='between 0 and 1'):
        Classification(state=ScreenState.PAGE, score=1.5, evidence=())


def test_the_states_are_the_five_contract_values() -> None:
    assert {state.value for state in ScreenState} == {'loading', 'page', 'popup', 'error', 'unknown'}


def _page_snapshot(*boxes: TextBox) -> Snapshot:
    return _snapshot(Image.new('RGB', (400, 800), (128, 128, 128)), boxes)


def test_a_product_collection_header_and_price_is_a_page() -> None:
    classification = classify(
        _page_snapshot(
            _box('Bundle Deals', 20, 40, 300, 100),
            _box('C$34.18', 20, 200, 300, 260),
        ),
    )

    assert classification.state is ScreenState.PAGE
    assert 'product collection' in classification.evidence[0].detail


def test_a_collection_header_without_a_price_is_unknown() -> None:
    classification = classify(_page_snapshot(_box('Bundle Deals', 20, 40, 300, 100)))

    assert classification.state is ScreenState.UNKNOWN


def test_a_feed_section_called_bundle_deals_is_not_a_collection_header() -> None:
    classification = classify(
        _page_snapshot(
            _box('Bundle Deals', 20, 600, 300, 660),
            _box('C$34.18', 20, 500, 300, 560),
        ),
    )

    assert classification.state is ScreenState.UNKNOWN


def test_an_error_marker_is_error() -> None:
    classification = classify(_page_snapshot(_box('404 Page', 20, 20, 380, 120)))

    assert classification.state is ScreenState.ERROR
    assert classification.evidence[0].signal == 'error_marker'


def test_a_quiz_result_marker_is_a_page() -> None:
    classification = classify(
        _page_snapshot(_box('Correct answer: All of the above', 20, 20, 380, 120)),
    )

    assert classification.state is ScreenState.PAGE
    assert 'quiz result' in classification.evidence[0].detail


def test_a_boost_header_without_rows_is_loading() -> None:
    classification = classify(_page_snapshot(_box('Coins boost', 20, 20, 380, 120)))

    assert classification.state is ScreenState.LOADING
    assert classification.evidence[0].signal == 'boost_without_rows'


def test_account_marker_needs_a_second_account_label() -> None:
    classification = classify(_page_snapshot(_box('My orders', 20, 20, 380, 120)))

    assert classification.state is ScreenState.UNKNOWN
    assert classification.evidence[0].signal == 'no_signal'


def test_low_confidence_and_empty_boxes_do_not_count_as_text() -> None:
    low = _box('Coins quiz', 20, 20, 380, 120)
    low = TextBox(text=low.text, box=low.box, score=0.1)
    empty = _box('', 20, 140, 380, 240)
    classification = classify(_page_snapshot(low, empty))

    assert classification.state is ScreenState.ERROR
    assert classification.evidence[0].signal == 'blank_frame'


def test_a_one_pixel_frame_is_safe_and_blank() -> None:
    classification = classify(_snapshot(Image.new('RGB', (1, 1), 'white')))

    assert classification.state is ScreenState.ERROR
    assert classification.evidence[0].signal == 'blank_frame'


def test_a_small_bright_region_is_not_a_popup_panel() -> None:
    image = Image.new('RGB', (400, 800), (128, 128, 128))
    draw = ImageDraw.Draw(image)
    draw.rectangle((150, 350, 250, 450), fill='white')

    classification = classify(_snapshot(image))

    assert classification.state is ScreenState.UNKNOWN
    assert classification.evidence[0].signal == 'no_signal'


def test_wait_for_state_rejects_a_non_state_target() -> None:
    bogus = cast('Iterable[ScreenState]', ('page',))

    with pytest.raises(TypeError, match='ScreenState'):
        wait_for_state(_dummy_snapshot, bogus, timeout=1, interval=1)
