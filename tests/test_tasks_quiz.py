"""The quiz executor over measured and synthetic frames (issue #25).

The device is simulated at the capture and input seams only: taps are recorded
by a fake device that drives a small screen graph, captures come from that graph,
and time is injected. No test reaches adb and no test sleeps.

The synthetic frames are built from the measured layout: the ``Coins quiz`` page
carries a header, a subtitle, one question prefixed ``Q :`` and one box per
option; the result page prints the correct answer, the chosen answer and the
payout; the coin page is what the flow returns to.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast

import pytest
from PIL import Image

from aliexpress_coins.control.controller import Controller
from aliexpress_coins.control.errors import PopupUnclosable
from aliexpress_coins.control.popups import PopupCorpus, load_popup_corpus
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.ocr import RapidOcrEngine
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks.catalogue import TaskCatalogue, load_task_catalogue
from aliexpress_coins.tasks.discovery import RosterEntry, enumerate_roster
from aliexpress_coins.tasks.dispatch import DispatchGate
from aliexpress_coins.tasks.errors import TaskError
from aliexpress_coins.tasks.quiz import (
    ANSWER_FROM_BANK,
    ANSWER_GUESS,
    REDEEM_INTERSTITIAL,
    AnswerOption,
    QuizOutcome,
    QuizResult,
    find_answer_options,
    find_go_back,
    read_payout,
    run_quiz_task,
)
from aliexpress_coins.tasks.quiz_bank import QuestionSource, QuizBank, normalize_question
from aliexpress_coins.tasks.surface import RouteFirer, SurfaceRoute

ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = ROOT / 'specs' / 'popups.yaml'
CATALOGUE_PATH = ROOT / 'specs' / 'tasks.yaml'
DATA = ROOT / 'tests' / 'data'

TITLE = 'Take the coins quiz'


class _RecordingDevice:
    """The input seam: record taps and let the world react to them."""

    def __init__(self, world: _World) -> None:
        self._world = world

    def tap(self, x: int, y: int) -> None:
        self._world.tap(x, y)


class _ForbiddenCapture:
    """A capture that proves state or policy stopped the executor first."""

    def __init__(self) -> None:
        self.calls = 0

    def capture(self) -> Snapshot:
        self.calls += 1
        raise AssertionError('the executor must not capture on this path')


class _SteppingClock:
    """An injected clock that advances on every read and sleep."""

    def __init__(self, step: float = 0.5) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        current = self.value
        self.value += self.step
        return current

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class _World:
    """A tiny screen graph driven by taps and navigation, not a frame queue."""

    def __init__(
        self,
        series: dict[str, Sequence[Snapshot]],
        transitions: dict[tuple[str, str | None], str],
        *,
        start: str | None = None,
        on_tap: Callable[[str, str | None, int], str | None] | None = None,
    ) -> None:
        if start is None:
            start = next(iter(series), '')
        if start not in series:
            raise ValueError(f'start screen {start!r} has no frames')
        self._series = {name: tuple(frames) for name, frames in series.items()}
        self._index = dict.fromkeys(series, 0)
        self._transitions = transitions
        self._on_tap = on_tap
        self.current = start
        self.taps: list[tuple[int, int]] = []
        self.navigations: list[SurfaceRoute] = []
        self.captures = 0

    @property
    def frame(self) -> Snapshot:
        return self._series[self.current][0]

    @property
    def current_frame(self) -> Snapshot:
        frames = self._series[self.current]
        index = self._index[self.current]
        return frames[max(0, min(index - 1, len(frames) - 1))]

    def capture(self) -> Snapshot:
        self.captures += 1
        frames = self._series[self.current]
        index = min(self._index[self.current], len(frames) - 1)
        self._index[self.current] = index + 1
        return frames[index]

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))
        label = _label_at(self.current_frame, x, y)
        target = None
        if self._on_tap is not None:
            target = self._on_tap(self.current, label, len(self.taps))
        if target is None:
            target = self._transitions.get((self.current, label))
        if target is not None:
            self.go(target)

    def navigate(self, route: SurfaceRoute) -> None:
        self.navigations.append(route)
        self.go('coins')

    def go(self, screen: str) -> None:
        if screen not in self._series:
            raise AssertionError(f'unknown screen {screen!r}')
        self.current = screen
        self._index[screen] = 0


def _box(text: str, left: float, top: float, right: float, bottom: float, score: float = 0.99) -> TextBox:
    return TextBox(
        text=text,
        box=((left, top), (right, top), (right, bottom), (left, bottom)),
        score=score,
    )


def _snapshot(*boxes: TextBox, size: tuple[int, int] = (1200, 2000)) -> Snapshot:
    from datetime import UTC, datetime

    from aliexpress_coins.perception.snapshot import Snapshot

    return Snapshot(
        image=Image.new('RGB', size, 'white'),
        boxes=boxes,
        captured_at=datetime(2026, 11, 22, 12, 0, tzinfo=UTC),
        geometry=DeviceGeometry.from_frame(*size, density=240, sdk=35),
    )


def _label_at(snapshot: Snapshot, x: float, y: float) -> str | None:
    best: TextBox | None = None
    for box in snapshot.boxes:
        left, top, right, bottom = box.bbox
        if not (left <= x <= right and top <= y <= bottom):
            continue
        if best is None or box.width * box.height < best.width * best.height:
            best = box
    return None if best is None else best.text


def _quiz_boost_frame(*, with_action: bool = True, badge_done: bool = False) -> Snapshot:
    """The boost sheet carrying the quiz row, plus a refused row and a GO row."""
    boxes = [
        _box('Coins boost', 80, 90, 400, 150),
        _box(TITLE, 60, 420, 700, 470),
        _box('Each correct answer grants you more rewards', 60, 480, 700, 520),
        *([] if not with_action else [_box('GO', 1000, 480, 1080, 530)]),
        _box('Turn on notifications', 60, 800, 500, 850),
        _box('Stay informed when rewards drop', 60, 860, 500, 900),
        _box('GO', 1000, 860, 1080, 910),
        _box('Place an order on this page', 60, 1200, 500, 1250),
        _box('GO', 1000, 1200, 1080, 1250),
    ]
    if badge_done:
        boxes.append(_box('Claimed', 820, 480, 980, 520))
    return _snapshot(*boxes)


def _question_frame(
    *,
    options: tuple[str, ...] = ('True', 'False'),
    question: str = 'Q : Which of the following statements is correct?',
) -> Snapshot:
    """The ``Coins quiz`` page: header, subtitle, question and option boxes."""
    boxes = [
        _box('Coins quiz', 60, 90, 400, 150),
        _box('Answer quiz questions for more coins!', 60, 170, 600, 210),
        _box(question, 80, 400, 1100, 450),
    ]
    for index, option in enumerate(options):
        top = 600 + 150 * index
        boxes.append(_box(option, 80, top, 800, top + 50))
    boxes.append(_box('1', 700, 1900, 760, 1940))  # page counter
    return _snapshot(*boxes)


def _result_frame(
    *,
    correct: str = 'True',
    payout: str = 'You got +6',
    go_back: bool = True,
) -> Snapshot:
    """The quiz result page: learned answer, chosen answer, payout, Go back."""
    boxes = [
        _box(f'Correct answer: {correct}', 60, 400, 700, 450),
        _box('Your answer: False', 60, 520, 700, 570),
        _box(payout, 60, 700, 500, 760),
    ]
    if go_back:
        boxes.append(_box('Go back', 400, 1400, 800, 1500))
    return _snapshot(*boxes)


def _coins_frame(*, inline: bool = False, action: str | None = 'GO') -> Snapshot:
    """The coins page — the surface the run returns to."""
    boxes = [
        _box('Coins', 101, 194, 254, 242),
        _box('Coins credits to redeem', 59, 567, 552, 617),
        _box('Daily coins', 66, 919, 320, 978),
        _box('Today', 109, 1013, 210, 1063),
        _box('Earn more >', 917, 926, 1133, 967),
    ]
    if inline:
        boxes.append(_box(TITLE, 60, 1200, 700, 1250))
        boxes.append(_box('Each correct answer grants you more rewards', 60, 1260, 700, 1300))
        if action is not None:
            boxes.append(_box(action, 1000, 1260, 1080, 1310))
    return _snapshot(*boxes)


def _wrong_page() -> Snapshot:
    return _snapshot(
        _box('AliExpress', 60, 100, 400, 160),
        _box('Wireless Earbuds', 60, 400, 500, 450),
        _box('C$12.99', 60, 460, 300, 510),
    )


def _boost_after_frame(
    *,
    with_row: bool = False,
    action: str | None = 'GO',
    done_label: str | None = None,
) -> Snapshot:
    """The boost sheet after the quiz: the quiz row is gone, claimed or unchanged."""
    boxes = [
        _box('Coins boost', 80, 90, 400, 150),
        _box('Turn on notifications', 60, 800, 500, 850),
        _box('Stay informed when rewards drop', 60, 860, 500, 900),
        _box('GO', 1000, 860, 1080, 910),
    ]
    if with_row:
        boxes.insert(1, _box(TITLE, 60, 420, 700, 470))
        boxes.insert(2, _box('Each correct answer grants you more rewards', 60, 480, 700, 520))
        if action is not None:
            boxes.insert(3, _box(action, 1000, 480, 1080, 530))
        if done_label is not None:
            boxes.insert(3, _box(done_label, 820, 480, 980, 520))
    return _snapshot(*boxes)


def _is_redeem_frame() -> Snapshot:
    """The redeem interstitial drawn over the coins page."""
    return _snapshot(
        _box('YOU CAN NOW REDEEM!', 80, 700, 1120, 780),
        _box('Coins', 101, 194, 254, 242),
        _box('Daily coins', 66, 919, 320, 978),
    )


def _controller(world: _World, frame: Snapshot) -> Controller:
    return Controller.from_frame(_RecordingDevice(world), frame, rng=random.Random(0))


def _state(tmp_path: Path) -> DailyStateStore:
    from datetime import UTC, datetime

    return DailyStateStore(tmp_path / 'state', clock=lambda: datetime(2026, 11, 22, tzinfo=UTC))


def _entry(catalogue: TaskCatalogue) -> RosterEntry:
    roster = enumerate_roster(_quiz_boost_frame(), catalogue)
    for entry in roster.entries:
        if entry.key == 'coins_quiz':
            return DispatchGate(catalogue).authorize(entry)
    raise AssertionError('coins_quiz is not in the synthetic roster')


def _success_world(
    *,
    after_with_row: bool = False,
    after_action: str | None = 'GO',
    after_done: str | None = None,
    result_frame: Snapshot | None = None,
) -> _World:
    """A normal visit: the row is gone, claimed or unchanged on the boost list."""
    return _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'question': (_question_frame(),),
            'result': (result_frame if result_frame is not None else _result_frame(),),
            'coins': (_coins_frame(),),
            'boost_after': (_boost_after_frame(with_row=after_with_row, action=after_action, done_label=after_done),),
        },
        transitions={
            ('boost', 'GO'): 'question',
            ('question', 'True'): 'result',
            ('question', 'False'): 'result',
            ('result', 'Go back'): 'coins',
            ('coins', 'Earn more >'): 'boost_after',
        },
    )


@pytest.fixture(scope='module')
def catalogue() -> TaskCatalogue:
    return load_task_catalogue(CATALOGUE_PATH)


@pytest.fixture(scope='module')
def corpus() -> PopupCorpus:
    return load_popup_corpus(CORPUS_PATH)


@pytest.fixture(scope='module')
def engine() -> RapidOcrEngine:
    return RapidOcrEngine()


def _load(path: Path, engine: RapidOcrEngine) -> Snapshot:
    from datetime import UTC, datetime

    with Image.open(path) as opened:
        image = opened.convert('RGB')
    return Snapshot(
        image=image,
        boxes=tuple(engine.read(image)),
        captured_at=datetime(2026, 11, 22, tzinfo=UTC),
        geometry=DeviceGeometry.from_frame(image.width, image.height, density=240, sdk=35),
    )


@pytest.fixture(scope='module')
def launch(engine: RapidOcrEngine) -> Snapshot:
    return _load(DATA / 'popups' / 'launch_welcome_gifts.png', engine)


def test_find_answer_options_on_a_two_option_frame() -> None:
    frame = _question_frame()

    options = find_answer_options(frame)

    assert [option.text for option in options] == ['True', 'False']
    assert options[0].box.text == 'True'


@pytest.mark.slow
def test_find_answer_options_on_the_measured_four_option_frame() -> None:
    """The reviewed four-option question fixture gives every OCR line back."""
    pytest.importorskip('aliexpress_coins.perception.ocr')
    from datetime import UTC, datetime

    from PIL import Image

    from aliexpress_coins.perception.geometry import DeviceGeometry
    from aliexpress_coins.perception.ocr import RapidOcrEngine
    from aliexpress_coins.perception.snapshot import Snapshot

    with Image.open(DATA / 'flows' / 'coins_quiz_question.png') as opened:
        image = opened.convert('RGB')
        boxes = RapidOcrEngine().read(image)
        snapshot = Snapshot(
            image=image,
            boxes=tuple(boxes),
            captured_at=datetime(2026, 11, 22, tzinfo=UTC),
            geometry=DeviceGeometry.from_frame(image.width, image.height, density=240, sdk=35),
        )

    options = find_answer_options(snapshot)

    assert len(options) == 4
    texts = [option.text for option in options]
    assert any('All of the above' in text for text in texts)
    assert any('expiration date' in text for text in texts)
    assert all(len(text) >= 2 for text in texts)
    assert all('Q :' not in text for text in texts)


def test_find_answer_options_excludes_the_question_and_the_counter() -> None:
    frame = _question_frame(options=('True', 'False', 'Maybe', 'All of the above'))

    options = find_answer_options(frame)

    assert [option.text for option in options] == ['True', 'False', 'Maybe', 'All of the above']
    assert not any(option.text == '1' for option in options)


def test_find_answer_options_returns_empty_without_a_question() -> None:
    frame = _snapshot(_box('Coins quiz', 60, 90, 400, 150), _box('True', 80, 600, 800, 650))

    assert find_answer_options(frame) == ()


def test_find_go_back_finds_the_control() -> None:
    frame = _result_frame()

    box = find_go_back(frame)

    assert box is not None
    assert box.text == 'Go back'


def test_find_go_back_accepts_a_chevron_or_destination_wording() -> None:
    chevron = _snapshot(_box('Go back >', 400, 1400, 800, 1500))
    destination = _snapshot(_box('Go back to coins', 400, 1400, 800, 1500))

    assert find_go_back(chevron) is not None
    assert find_go_back(destination) is not None


def test_find_go_back_returns_none_when_absent() -> None:
    frame = _snapshot(_box('Correct answer: True', 60, 400, 700, 450))

    assert find_go_back(frame) is None


def test_read_payout_reads_the_payout_line() -> None:
    frame = _result_frame(payout='You got +6')

    assert read_payout(frame) == '+6'


def test_read_payout_returns_none_without_a_payout() -> None:
    frame = _snapshot(_box('Correct answer: True', 60, 400, 700, 450))

    assert read_payout(frame) is None


def test_run_quiz_task_rejects_a_denied_entry(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    roster = enumerate_roster(_quiz_boost_frame(), catalogue)
    denied = next(entry for entry in roster.entries if entry.denied)
    world = _World({'boost': (_quiz_boost_frame(),)}, {})

    with pytest.raises(TaskError, match='not dispatchable'):
        run_quiz_task(
            _controller(world, world.frame),
            _ForbiddenCapture(),
            corpus,
            _state(tmp_path),
            denied,
            navigate=world.navigate,
        )


def test_run_quiz_task_rejects_a_non_quiz_category(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    roster = enumerate_roster(_quiz_boost_frame(), catalogue)
    entry = next(entry for entry in roster.entries if entry.key == 'notifications')
    world = _World({'boost': (_quiz_boost_frame(),)}, {})

    with pytest.raises(TaskError, match='not a quiz task'):
        run_quiz_task(
            _controller(world, world.frame),
            _ForbiddenCapture(),
            corpus,
            _state(tmp_path),
            entry,
            navigate=world.navigate,
        )


@pytest.mark.parametrize(
    ('name', 'value'),
    [
        ('load_timeout', 0.0),
        ('arrival_timeout', 0.0),
        ('exit_timeout', 0.0),
        ('interval', 0.0),
        ('return_timeout', 0.0),
        ('return_interval', 0.0),
        ('return_attempts', 0),
        ('settle', -1.0),
    ],
)
def test_run_quiz_task_rejects_invalid_parameters(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    name: str,
    value: float,
) -> None:
    world = _World({'boost': (_quiz_boost_frame(),)}, {})

    with pytest.raises(ValueError, match=r'must be positive|must not be negative'):
        run_quiz_task(
            _controller(world, world.frame),
            world.capture,
            corpus,
            _state(tmp_path),
            _entry(catalogue),
            navigate=world.navigate,
            **{name: value},  # type: ignore[arg-type]
        )


def test_run_quiz_task_skips_at_the_daily_limit(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    state.record('coins_quiz')
    world = _World({'boost': (_quiz_boost_frame(),)}, {})

    result = run_quiz_task(
        _controller(world, world.frame),
        _ForbiddenCapture(),
        corpus,
        state,
        _entry(catalogue),
        navigate=world.navigate,
    )

    assert result.outcome is QuizOutcome.SKIPPED
    assert result.verification == 'daily_limit'
    assert result.recorded is False


def test_run_quiz_task_skips_a_vanished_row(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={'boost': (_boost_after_frame(),)},
        transitions={},
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
    )

    assert result.outcome is QuizOutcome.SKIPPED
    assert result.verification == 'row_vanished'


def test_run_quiz_task_skips_a_row_with_no_action(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={'boost': (_quiz_boost_frame(with_action=False),)},
        transitions={},
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
    )

    assert result.outcome is QuizOutcome.SKIPPED
    assert result.verification == 'row_complete'


@pytest.mark.slow
def test_run_quiz_task_completes_a_normal_visit(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _success_world()
    state = _state(tmp_path)
    entry = _entry(catalogue)
    clock = _SteppingClock()

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        state,
        entry,
        navigate=world.navigate,
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=clock,
        sleep=clock.sleep,
        rng=random.Random(7),
    )

    assert result.outcome is QuizOutcome.COMPLETED
    assert result.verification == 'row_gone'
    assert result.recorded is True
    assert result.returned is True
    assert result.question is not None
    assert result.options
    assert result.chosen is not None
    assert result.correct == 'True'
    assert result.payout == '+6'
    assert state.count('coins_quiz') == 1
    assert world.current == 'coins'
    assert world.taps  # at least GO, the option and Go back and Earn more
    payload = result.as_dict()
    assert payload['outcome'] == 'completed'
    assert payload['verification'] == 'row_gone'


def test_run_quiz_task_records_a_new_question_in_the_bank(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    """The first encounter records the question, its options and the learned answer."""
    from datetime import UTC, datetime

    stamp = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    bank = QuizBank(tmp_path / 'quiz_bank.json', clock=lambda: stamp)
    world = _success_world()

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
        bank=bank,
    )

    assert result.outcome is QuizOutcome.COMPLETED
    assert result.answer_source == ANSWER_GUESS
    entry = bank.lookup(result.question or '')
    assert entry is not None
    assert entry.question == result.question
    assert entry.normalized == normalize_question(result.question or '')
    assert entry.options == tuple(option.text for option in result.options)
    assert entry.chosen == result.chosen
    assert entry.correct == result.correct == 'True'
    assert entry.source is QuestionSource.RESULT_PAGE
    assert entry.seen_at == stamp


def test_run_quiz_task_answers_a_repeated_question_from_the_bank(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    """The second encounter taps the learned option, not another random guess."""
    from datetime import UTC, datetime

    stamp = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    bank = QuizBank(tmp_path / 'quiz_bank.json', clock=lambda: stamp)

    first_world = _success_world()
    first = run_quiz_task(
        _controller(first_world, first_world.frame),
        first_world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=first_world.navigate,
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
        bank=bank,
    )
    learned = bank.lookup(first.question or '')
    assert learned is not None
    first_bytes = (tmp_path / 'quiz_bank.json').read_bytes()

    second_world = _success_world()
    second = run_quiz_task(
        _controller(second_world, second_world.frame),
        second_world.capture,
        corpus,
        DailyStateStore(tmp_path / 'state-two', clock=lambda: datetime(2026, 11, 22, tzinfo=UTC)),
        _entry(catalogue),
        navigate=second_world.navigate,
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(999),
        bank=bank,
    )

    assert second.outcome is QuizOutcome.COMPLETED
    assert second.bank_hit is True
    assert second.answer_source == ANSWER_FROM_BANK
    assert second.chosen == learned.correct
    assert (tmp_path / 'quiz_bank.json').read_bytes() == first_bytes
    answer_tap = second.taps[1]
    assert answer_tap.label == learned.correct


def test_run_quiz_task_guesses_and_records_when_the_recorded_option_is_gone(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    """A changed option set is a new question: no remembered position is tapped."""
    bank = QuizBank(tmp_path / 'quiz_bank.json')
    bank.record(
        'Q : Which of the following statements is correct?',
        ('True', 'False'),
        'False',
        'True',
    )
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'question': (_question_frame(options=('Yes', 'No')),),
            'result': (_result_frame(correct='Yes', payout='You got +6'),),
            'coins': (_coins_frame(),),
            'boost_after': (_boost_after_frame(action=None),),
        },
        transitions={
            ('boost', 'GO'): 'question',
            ('question', 'Yes'): 'result',
            ('question', 'No'): 'result',
            ('result', 'Go back'): 'coins',
            ('coins', 'Earn more >'): 'boost_after',
        },
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(7),
        bank=bank,
    )

    assert result.outcome is QuizOutcome.COMPLETED
    assert result.answer_source == ANSWER_GUESS
    assert result.chosen in ('Yes', 'No')
    entry = bank.lookup(result.question or '')
    assert entry is not None
    assert entry.options == ('Yes', 'No')
    assert entry.correct == 'Yes'
    assert result.taps[1].label in ('Yes', 'No')


def test_run_quiz_task_guesses_when_the_bank_cannot_be_read_or_written(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bank path that is a directory warns twice and never fails the quiz."""
    world = _success_world()

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
        bank=tmp_path,
    )

    assert result.outcome is QuizOutcome.COMPLETED
    assert result.answer_source == ANSWER_GUESS
    assert 'quiz_bank_unreadable' in caplog.text
    assert 'quiz_bank_write_failed' in caplog.text


def _done_label_coins_frame() -> Snapshot:
    """The coin page with the quiz row flipped to a done label."""
    return _snapshot(
        _box('Coins', 101, 194, 254, 242),
        _box('Coins credits to redeem', 59, 567, 552, 617),
        _box('Daily coins', 66, 919, 320, 978),
        _box('Today', 109, 1013, 210, 1063),
        _box('Earn more >', 917, 926, 1133, 967),
        _box(TITLE, 60, 1200, 700, 1250),
        _box('Each correct answer grants you more rewards', 60, 1260, 700, 1300),
        _box('Claimed', 1000, 1260, 1080, 1310),
    )


def test_run_quiz_task_completes_a_completed_variant_via_the_done_label(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    """The inline coin page flipped to a done label: no boost-list re-open."""
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'question': (_question_frame(),),
            'result': (_result_frame(),),
            'coins': (_done_label_coins_frame(),),
        },
        transitions={
            ('boost', 'GO'): 'question',
            ('question', 'True'): 'result',
            ('question', 'False'): 'result',
            ('result', 'Go back'): 'coins',
        },
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        return_timeout=2.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.COMPLETED
    assert result.verification == 'action_completed'
    assert result.recorded is True
    # No boost-sheet re-open: exactly GO, the option and Go back.
    assert len(world.taps) == 3


@pytest.mark.slow
def test_run_quiz_task_completes_when_the_action_is_gone(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _success_world(after_with_row=True, after_action=None)
    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.COMPLETED
    assert result.verification == 'action_gone'
    assert result.recorded is True


@pytest.mark.slow
def test_run_quiz_task_does_not_complete_on_a_visible_payout_when_the_row_still_offers_go(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    """A trusted row that still offers GO contradicts the result payout."""
    world = _success_world(after_with_row=True, after_action='GO')
    state = _state(tmp_path)

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        state,
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.FAILED
    assert result.verification == 'no_change'
    assert result.recorded is False
    assert state.count('coins_quiz') == 0


@pytest.mark.slow
def test_run_quiz_task_fails_when_nothing_changes(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _success_world(
        after_with_row=True,
        after_action='GO',
        result_frame=_result_frame(payout='No payout shown'),
    )
    state = _state(tmp_path)
    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        state,
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.FAILED
    assert result.verification == 'no_change'
    assert result.recorded is False
    assert state.count('coins_quiz') == 0


def test_run_quiz_task_completes_the_interstitial_visit(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'question': (_question_frame(),),
            'result': (_result_frame(),),
            'redeem': (_is_redeem_frame(), _coins_frame()),
            'coins': (_coins_frame(),),
        },
        transitions={
            ('boost', 'GO'): 'question',
            ('question', 'True'): 'result',
            ('question', 'False'): 'result',
            ('result', 'Go back'): 'redeem',
        },
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.COMPLETED
    assert result.verification == 'interstitial'
    assert result.stop == REDEEM_INTERSTITIAL
    assert result.recorded is True
    assert result.returned is True
    assert result.interstitial == REDEEM_INTERSTITIAL


@pytest.mark.slow
def test_run_quiz_task_completes_a_wrong_answer_without_aborting(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    """A wrong pick still pays: the flow never branches on correctness."""
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'question': (_question_frame(),),
            # The result page names a different answer than the guess was for.
            'result': (_result_frame(correct='False', payout='You got +3'),),
            'coins': (_coins_frame(),),
            'boost_after': (_boost_after_frame(action=None),),
        },
        transitions={
            ('boost', 'GO'): 'question',
            ('question', 'True'): 'result',
            ('question', 'False'): 'result',
            ('result', 'Go back'): 'coins',
            ('coins', 'Earn more >'): 'boost_after',
        },
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(7),
    )

    assert result.outcome is QuizOutcome.COMPLETED
    assert result.payout == '+3'
    assert result.correct == 'False'
    assert result.chosen in ('True', 'False')
    assert result.recorded is True
    assert world.current == 'coins'


@pytest.mark.slow
def test_run_quiz_task_handles_a_multi_round_visit(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    """A second question appearing after ``Go back`` means a second round."""
    rounds: list[int] = []

    def on_tap(current: str, label: str | None, index: int) -> str | None:
        if current == 'result' and label == 'Go back':
            rounds.append(len(rounds) + 1)
            if rounds[-1] == 1:
                return 'question_2'
            return 'coins'
        return None

    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'question': (_question_frame(),),
            'question_2': (_question_frame(options=('Yes', 'No'), question='Q : Second question?'),),
            'result': (_result_frame(),),
            'result_2': (_result_frame(correct='Yes', payout='You got +4'),),
            'coins': (_coins_frame(),),
            'boost_after': (_boost_after_frame(action=None),),
        },
        transitions={
            ('boost', 'GO'): 'question',
            ('question', 'True'): 'result',
            ('question', 'False'): 'result',
            ('question_2', 'Yes'): 'result_2',
            ('question_2', 'No'): 'result_2',
            ('result_2', 'Go back'): 'coins',
            ('coins', 'Earn more >'): 'boost_after',
        },
        on_tap=on_tap,
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.COMPLETED
    assert result.rounds == 2
    assert result.recorded is True


def test_run_quiz_task_fails_when_the_question_never_arrives(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'wrong': (_wrong_page(),),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'wrong'},
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        return_timeout=2.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.FAILED
    assert result.verification == 'question_not_arrived'
    assert result.returned is True


def test_run_quiz_task_fails_when_the_result_never_arrives(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'question': (_question_frame(),),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'question'},
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        return_timeout=2.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.FAILED
    assert result.verification == 'result_not_arrived'
    assert result.recorded is False


def test_run_quiz_task_fails_when_go_back_is_missing(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'question': (_question_frame(),),
            'result': (_result_frame(go_back=False),),
            'coins': (_coins_frame(),),
        },
        transitions={
            ('boost', 'GO'): 'question',
            ('question', 'True'): 'result',
            ('question', 'False'): 'result',
        },
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=2.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.FAILED
    assert result.verification == 'go_back_not_found'


@pytest.mark.slow
def test_run_quiz_task_fails_when_the_exit_never_arrives(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'question': (_question_frame(),),
            'result': (_result_frame(),),
            'wrong': (_wrong_page(),),
            'coins': (_coins_frame(),),
        },
        transitions={
            ('boost', 'GO'): 'question',
            ('question', 'True'): 'result',
            ('question', 'False'): 'result',
            ('result', 'Go back'): 'wrong',
        },
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=2.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.FAILED
    assert result.verification == 'exit_not_reached'


def test_run_quiz_task_fails_when_fewer_than_two_options_are_detected(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'question': (_question_frame(options=('True',)),),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'question'},
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        return_timeout=2.0,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.FAILED
    assert result.verification == 'options_not_found'


@pytest.mark.slow
def test_run_quiz_task_fails_without_a_finishing_return(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'question': (_question_frame(),),
            'result': (_result_frame(),),
            'coins': (_coins_frame(),),
            'wrong': (_wrong_page(),),
        },
        transitions={
            ('boost', 'GO'): 'question',
            ('question', 'True'): 'result',
            ('question', 'False'): 'result',
            ('result', 'Go back'): 'coins',
            ('coins', 'Earn more >'): 'wrong',
        },
    )

    def navigate(route: SurfaceRoute) -> None:
        world.navigations.append(route)
        world.go('wrong')

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=navigate,
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=0.5,
        return_interval=0.5,
        clock=_SteppingClock(step=1.0),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.FAILED
    assert result.verification == 'return_failed'
    assert result.recorded is False


def _popup_frame() -> Snapshot:
    """A popup whose anchor is not in the corpus: it cannot be closed."""
    return _snapshot(
        _box('Tap to claim', 80, 700, 600, 780),
        _box('Wireless Earbuds', 60, 400, 500, 450),
        _box('C$12.99', 60, 460, 300, 510),
    )


def test_run_quiz_task_returns_before_re_raising_an_unclosable_popup(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    """A popup that is not the redeem interstitial must never be tapped blindly."""
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'question': (_question_frame(),),
            'popup': (_popup_frame(),),
            'coins': (_coins_frame(),),
        },
        transitions={
            ('boost', 'GO'): 'question',
            ('question', 'True'): 'popup',
            ('question', 'False'): 'popup',
        },
    )

    with pytest.raises(PopupUnclosable):
        run_quiz_task(
            _controller(world, world.frame),
            world.capture,
            corpus,
            _state(tmp_path),
            _entry(catalogue),
            navigate=world.navigate,
            arrival_timeout=1.0,
            return_timeout=2.0,
            clock=_SteppingClock(),
            sleep=_SteppingClock().sleep,
            rng=random.Random(3),
        )

    assert world.current == 'coins'
    assert len(world.navigations) >= 1


@pytest.mark.slow
def test_run_quiz_task_reports_no_observation_when_the_list_cannot_be_read(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'question': (_question_frame(),),
            'result': (_result_frame(),),
            'coins': (_coins_frame(),),
            'broken': (_wrong_page(),),
        },
        transitions={
            ('boost', 'GO'): 'question',
            ('question', 'True'): 'result',
            ('question', 'False'): 'result',
            ('result', 'Go back'): 'coins',
            ('coins', 'Earn more >'): 'broken',
        },
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=1.0,
        clock=_SteppingClock(step=1.0),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.COMPLETED
    assert result.verification == 'result_reward'
    assert result.recorded is True
    assert result.returned is True


def _loading_frame() -> Snapshot:
    """A mostly-blank frame with one text fragment: the loading state."""
    return _snapshot(_box('Loading', 100, 100, 300, 150))


def test_run_quiz_task_fails_and_returns_when_the_entry_never_settles(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'loading': (_loading_frame(),),
            'coins': (_coins_frame(),),
        },
        transitions={},
        start='loading',
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        load_timeout=2.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(step=1.0),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is QuizOutcome.FAILED
    assert result.verification == 'not_settled'
    assert result.returned is True
    assert world.current == 'coins'


def test_run_quiz_task_fails_and_returns_on_a_non_task_surface(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'wrong': (_wrong_page(),),
            'coins': (_coins_frame(),),
        },
        transitions={},
        start='wrong',
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        load_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is QuizOutcome.FAILED
    assert result.verification == 'task_surface_unavailable'
    assert result.returned is True
    assert world.current == 'coins'


def test_choose_option_guesses_when_there_is_no_question(tmp_path: Path) -> None:
    import logging

    from aliexpress_coins.tasks.quiz import _choose_option

    option = AnswerOption(text='True', box=_box('True', 80, 600, 800, 650))

    chosen, source = _choose_option(
        QuizBank(tmp_path / 'quiz_bank.json'),
        None,
        (option,),
        random.Random(0),
        logger=logging.getLogger('test'),
    )

    assert chosen is option
    assert source == ANSWER_GUESS


def test_quiz_result_as_dict_renders_the_evidence() -> None:
    result = QuizResult(
        key='coins_quiz',
        title=TITLE,
        outcome=QuizOutcome.COMPLETED,
        verification='interstitial',
        detail='detail',
        limit=1,
        done_today_before=0,
        done_today_after=1,
        rounds=1,
        question='Q : how?',
        options=(AnswerOption(text='True', box=_box('True', 80, 600, 800, 650)),),
        chosen='True',
        correct='True',
        payout='+6',
        stop=REDEEM_INTERSTITIAL,
        recorded=True,
    )

    assert result.completed is True
    assert result.failed is False
    assert result.skipped is False
    assert result.interstitial == REDEEM_INTERSTITIAL
    assert result.returned is False

    payload = result.as_dict()
    assert payload['key'] == 'coins_quiz'
    assert payload['outcome'] == 'completed'
    assert payload['rounds'] == 1
    assert payload['options'] == ['True']
    assert payload['chosen'] == 'True'
    assert payload['correct'] == 'True'
    assert payload['payout'] == '+6'
    assert payload['recorded'] is True
    assert payload['stop'] == REDEEM_INTERSTITIAL


# --- entry and arrival sweeps, and the verification return paths --------------


@pytest.mark.slow
def test_run_quiz_task_sweeps_a_popup_on_entry(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    launch: Snapshot,
    tmp_path: Path,
) -> None:
    def on_tap(screen: str, _label: str | None, _count: int) -> str | None:
        return 'boost' if screen == 'popup' else None

    world = _World(
        series={
            'popup': (launch,),
            'boost': (_quiz_boost_frame(),),
            'question': (_question_frame(),),
            'result': (_result_frame(),),
            'coins': (_coins_frame(),),
            'boost_after': (_boost_after_frame(action=None),),
        },
        transitions={
            ('boost', 'GO'): 'question',
            ('question', 'True'): 'result',
            ('question', 'False'): 'result',
            ('result', 'Go back'): 'coins',
            ('coins', 'Earn more >'): 'boost_after',
        },
        on_tap=on_tap,
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        load_timeout=2.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.COMPLETED
    assert result.dismissals
    assert any('launch_welcome_gifts' in report.closed for report in result.dismissals)


@pytest.mark.slow
def test_run_quiz_task_sweeps_a_popup_on_arrival(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    launch: Snapshot,
    tmp_path: Path,
) -> None:
    def on_tap(screen: str, _label: str | None, _count: int) -> str | None:
        return 'question' if screen == 'popup' else None

    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'popup': (launch,),
            'question': (_question_frame(),),
            'result': (_result_frame(),),
            'coins': (_coins_frame(),),
            'boost_after': (_boost_after_frame(action=None),),
        },
        transitions={
            ('boost', 'GO'): 'popup',
            ('question', 'True'): 'result',
            ('question', 'False'): 'result',
            ('result', 'Go back'): 'coins',
            ('coins', 'Earn more >'): 'boost_after',
        },
        on_tap=on_tap,
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=2.0,
        load_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.COMPLETED
    assert result.dismissals


@pytest.mark.slow
def test_run_quiz_task_waits_out_a_loading_frame_on_arrival(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'question': (_loading_frame(), _question_frame(), _question_frame()),
            'result': (_result_frame(),),
            'coins': (_coins_frame(),),
            'boost_after': (_boost_after_frame(action=None),),
        },
        transitions={
            ('boost', 'GO'): 'question',
            ('question', 'True'): 'result',
            ('question', 'False'): 'result',
            ('result', 'Go back'): 'coins',
            ('coins', 'Earn more >'): 'boost_after',
        },
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=2.0,
        load_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.COMPLETED


def test_run_quiz_task_fails_when_the_entry_popup_outlives_the_budget(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    launch: Snapshot,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'popup': (launch,),
            'coins': (_coins_frame(),),
        },
        transitions={},
        start='popup',
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        load_timeout=0.5,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(step=1.0),
        sleep=_SteppingClock().sleep,
    )

    assert result.outcome is QuizOutcome.FAILED
    assert result.verification == 'not_settled'
    assert world.current == 'coins'


def test_run_quiz_task_returns_before_re_raising_when_the_return_also_fails(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'popup': (_popup_frame(),),
        },
        transitions={('boost', 'GO'): 'popup'},
    )

    class _RaisingNavigate:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, _route: SurfaceRoute) -> None:
            self.calls += 1
            raise OSError('adb route unavailable')

    navigate = _RaisingNavigate()

    with pytest.raises(PopupUnclosable):
        run_quiz_task(
            _controller(world, world.frame),
            world.capture,
            corpus,
            _state(tmp_path),
            _entry(catalogue),
            navigate=cast(RouteFirer, navigate),
            arrival_timeout=1.0,
            load_timeout=1.0,
            return_timeout=0.5,
            return_interval=0.5,
            return_attempts=1,
            clock=_SteppingClock(),
            sleep=_SteppingClock().sleep,
            rng=random.Random(3),
        )

    assert navigate.calls >= 1


@pytest.mark.slow
def test_run_quiz_task_stops_the_round_loop_at_the_bound(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        if screen == 'result' and label == 'Go back':
            return 'question'
        return None

    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'question': (_question_frame(),),
            'result': (_result_frame(payout='No payout shown'),),
            'coins': (_coins_frame(),),
            'boost_after': (_boost_after_frame(with_row=True, action='GO'),),
        },
        transitions={
            ('boost', 'GO'): 'question',
            ('question', 'True'): 'result',
            ('question', 'False'): 'result',
            ('coins', 'Earn more >'): 'boost_after',
        },
        on_tap=on_tap,
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.rounds == 3
    # The bound stops the round loop; the verification verdict decides.
    assert result.outcome is QuizOutcome.FAILED
    assert result.verification == 'no_change'
    assert result.recorded is False


def test_find_answer_options_ignores_a_low_score_question() -> None:
    frame = _snapshot(
        _box('Coins quiz', 60, 90, 400, 150),
        _box('Q : Which of the following statements is correct?', 80, 400, 1100, 450, score=0.1),
        _box('True', 80, 600, 800, 650),
        _box('False', 80, 750, 800, 800),
    )

    assert find_answer_options(frame) == ()


def test_find_go_back_ignores_a_low_score_box() -> None:
    frame = _result_frame()
    frame = _snapshot(*frame.boxes[:-1], _box('Go back', 400, 1400, 800, 1500, score=0.1))

    assert find_go_back(frame) is None


def test_read_payout_ignores_a_low_score_box() -> None:
    frame = _snapshot(_box('You got +6', 60, 700, 500, 760, score=0.1))

    assert read_payout(frame) is None


@pytest.mark.slow
def test_run_quiz_task_records_nothing_when_the_result_prints_no_answer(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    """A result page with a valueless learned answer still completes the visit."""
    frames = (
        _question_frame(),
        _snapshot(
            _box('Correct answer:', 60, 400, 700, 450),
            _box('Your answer: False', 60, 520, 700, 570),
            _box('You got +6', 60, 700, 500, 760),
            _box('Go back', 400, 1400, 800, 1500),
        ),
    )
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'question': (frames[0],),
            'result': (frames[1],),
            'coins': (_coins_frame(),),
            'boost_after': (_boost_after_frame(action=None),),
        },
        transitions={
            ('boost', 'GO'): 'question',
            ('question', 'True'): 'result',
            ('question', 'False'): 'result',
            ('result', 'Go back'): 'coins',
            ('coins', 'Earn more >'): 'boost_after',
        },
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.COMPLETED
    assert result.correct is None
    assert result.payout == '+6'


# --- remaining branches: popup/loading waits and the small helpers ------------


def test_answer_option_as_dict_renders_the_measured_box() -> None:
    option = AnswerOption(text='True', box=_box('True', 80, 600, 800, 650))

    assert option.as_dict() == {'text': 'True', 'box': [80.0, 600.0, 800.0, 650.0]}


def test_find_answer_options_without_any_positive_text_height() -> None:
    zero = TextBox(text='Q : A question?', box=((0, 0), (0, 0), (0, 0), (0, 0)), score=0.99)

    assert find_answer_options(_snapshot(zero)) == ()


def test_find_answer_options_skips_a_bare_counter_cluster() -> None:
    frame = _snapshot(
        _box('Coins quiz', 60, 90, 400, 150),
        _box('Answer quiz questions for more coins!', 60, 170, 600, 210),
        _box('Q : A question?', 80, 400, 1100, 450),
        _box('22', 900, 600, 1000, 650),
        _box('True', 80, 800, 800, 850),
    )

    assert [option.text for option in find_answer_options(frame)] == ['True']


def test_question_of_returns_none_without_a_question_prefix() -> None:
    from aliexpress_coins.tasks.quiz import question_of

    assert question_of(_coins_frame()) is None


def test_question_text_with_no_continuation_and_no_options() -> None:
    from aliexpress_coins.tasks.quiz import question_of

    frame = _snapshot(
        _box('Coins quiz', 60, 90, 400, 150),
        _box('Answer quiz questions for more coins!', 60, 170, 600, 210),
        _box('Q : A question?', 80, 400, 1100, 450),
    )

    assert question_of(frame) == 'Q : A question?'
    assert find_answer_options(frame) == ()


def test_correct_answer_ignores_a_low_confidence_box() -> None:
    from aliexpress_coins.tasks.quiz import _correct_answer

    frame = _snapshot(
        _box('Correct answer: True', 60, 400, 700, 450, score=0.2),
        _box('Your answer: False', 60, 520, 700, 570),
    )

    assert _correct_answer(frame) is None


@pytest.mark.slow
def test_run_quiz_task_sweeps_a_popup_during_the_result_wait(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    launch: Snapshot,
    tmp_path: Path,
) -> None:
    def on_tap(screen: str, _label: str | None, _count: int) -> str | None:
        return 'result' if screen == 'popup' else None

    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'question': (_question_frame(),),
            'popup': (launch,),
            'result': (_result_frame(),),
            'coins': (_coins_frame(),),
            'boost_after': (_boost_after_frame(action=None),),
        },
        transitions={
            ('boost', 'GO'): 'question',
            ('question', 'True'): 'popup',
            ('question', 'False'): 'popup',
            ('result', 'Go back'): 'coins',
            ('coins', 'Earn more >'): 'boost_after',
        },
        on_tap=on_tap,
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=2.0,
        exit_timeout=2.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.COMPLETED
    assert result.dismissals


@pytest.mark.slow
def test_run_quiz_task_sweeps_a_popup_during_the_exit_wait(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    launch: Snapshot,
    tmp_path: Path,
) -> None:
    def on_tap(screen: str, _label: str | None, _count: int) -> str | None:
        return 'coins' if screen == 'popup' else None

    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'question': (_question_frame(),),
            'result': (_result_frame(),),
            'popup': (launch,),
            'coins': (_coins_frame(),),
            'boost_after': (_boost_after_frame(action=None),),
        },
        transitions={
            ('boost', 'GO'): 'question',
            ('question', 'True'): 'result',
            ('question', 'False'): 'result',
            ('result', 'Go back'): 'popup',
            ('coins', 'Earn more >'): 'boost_after',
        },
        on_tap=on_tap,
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=2.0,
        exit_timeout=2.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.COMPLETED
    assert result.dismissals


def test_run_quiz_task_stops_on_a_redeem_seen_while_waiting_for_the_question(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'redeem': (_is_redeem_frame(),),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'redeem'},
    )

    def navigate(_route: SurfaceRoute) -> None:
        world.navigations.append(_route)
        world.go('coins')

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=cast(RouteFirer, navigate),
        arrival_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.FAILED
    assert result.verification == 'redeem_interstitial'
    assert result.stop == REDEEM_INTERSTITIAL
    assert result.recorded is False


def test_run_quiz_task_times_out_on_a_loading_question_wait(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'loading': (_loading_frame(),),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'loading'},
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(step=1.0),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.FAILED
    assert result.verification == 'question_not_arrived'
    assert result.returned is True


@pytest.mark.slow
def test_run_quiz_task_reads_an_inline_route_with_no_earn_more(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'question': (_question_frame(),),
            'result': (_result_frame(),),
            'inline': (
                _snapshot(
                    _box('Coins', 101, 194, 254, 242),
                    _box('Coins credits to redeem', 59, 567, 552, 617),
                    _box('Daily coins', 66, 919, 320, 978),
                    _box(TITLE, 60, 1200, 700, 1250),
                    _box('Each correct answer grants you more rewards', 60, 1260, 700, 1300),
                    _box('GO', 1000, 1260, 1080, 1310),
                ),
            ),
        },
        transitions={
            ('boost', 'GO'): 'question',
            ('question', 'True'): 'result',
            ('question', 'False'): 'result',
            ('result', 'Go back'): 'inline',
        },
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=2.0,
        exit_timeout=2.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.FAILED
    assert result.verification == 'no_change'
    assert result.recorded is False
    assert result.returned is True


@pytest.mark.slow
def test_run_quiz_task_works_without_the_boost_family(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    trimmed = replace(
        corpus,
        families=tuple(family for family in corpus.families if family.family != 'coins_boost_sheet'),
    )
    world = _success_world()

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        trimmed,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.COMPLETED


class _CaptureObject:
    """A capture object rather than a callable, exercising the other seam branch."""

    def __init__(self, world: _World) -> None:
        self._world = world

    def capture(self) -> Snapshot:
        return self._world.capture()


@pytest.mark.slow
def test_run_quiz_task_accepts_a_capture_object(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _success_world()

    result = run_quiz_task(
        _controller(world, world.frame),
        _CaptureObject(world),
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.COMPLETED


def test_without_boost_family_returns_a_trimmed_corpus_unchanged() -> None:
    from dataclasses import replace

    from aliexpress_coins.tasks.quiz import _without_boost_family

    trimmed = replace(corpus_for_helper(), families=())
    assert _without_boost_family(trimmed) is trimmed


def corpus_for_helper() -> PopupCorpus:
    return load_popup_corpus(CORPUS_PATH)


def test_trusted_surface_rejects_an_empty_boost_list() -> None:
    from aliexpress_coins.tasks.discovery import BoostList, DiscoveryRoute
    from aliexpress_coins.tasks.quiz import _trusted_surface

    empty = _snapshot(
        _box('Coins boost', 80, 90, 400, 150),
        _box("There's nothing here yet", 60, 700, 800, 760),
    )
    opened = BoostList(snapshot=empty, route=DiscoveryRoute.BOOST, detail='empty')

    assert _trusted_surface(opened) is False


def test_run_quiz_task_stops_on_a_redeem_seen_while_waiting_for_the_result(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'question': (_question_frame(),),
            'redeem': (_is_redeem_frame(),),
            'coins': (_coins_frame(),),
        },
        transitions={
            ('boost', 'GO'): 'question',
            ('question', 'True'): 'redeem',
            ('question', 'False'): 'redeem',
        },
    )

    def navigate(_route: SurfaceRoute) -> None:
        world.navigations.append(_route)
        world.go('coins')

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=cast(RouteFirer, navigate),
        arrival_timeout=2.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.COMPLETED
    assert result.stop == REDEEM_INTERSTITIAL


def test_run_quiz_task_marks_return_failed_when_the_exit_return_cannot_land(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'question': (_question_frame(),),
            'redeem': (_is_redeem_frame(),),
            'wrong': (_wrong_page(),),
        },
        transitions={
            ('boost', 'GO'): 'question',
            ('question', 'True'): 'redeem',
            ('question', 'False'): 'redeem',
        },
    )

    def navigate(_route: SurfaceRoute) -> None:
        world.navigations.append(_route)
        world.go('wrong')

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=cast(RouteFirer, navigate),
        arrival_timeout=2.0,
        return_timeout=0.5,
        return_interval=0.5,
        return_attempts=1,
        clock=_SteppingClock(step=1.0),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.FAILED
    assert result.verification == 'return_failed'


def test_run_quiz_task_finishes_a_failure_on_a_redeem_at_entry(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'redeem': (_is_redeem_frame(),),
            'coins': (_coins_frame(),),
        },
        transitions={},
        start='redeem',
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        load_timeout=2.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(step=1.0),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.FAILED
    assert result.verification == 'not_settled'
    assert result.returned is True


def test_run_quiz_task_marks_return_failed_for_a_failure_that_cannot_return(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'wrong': (_wrong_page(),),
        },
        transitions={('boost', 'GO'): 'wrong'},
    )

    def navigate(_route: SurfaceRoute) -> None:
        world.navigations.append(_route)
        world.go('wrong')

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=cast(RouteFirer, navigate),
        arrival_timeout=1.0,
        return_timeout=0.5,
        return_interval=0.5,
        return_attempts=1,
        clock=_SteppingClock(step=1.0),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.FAILED
    assert result.verification == 'return_failed'


def test_run_quiz_task_quietly_returns_before_re_raising_when_the_popup_blocks_the_return(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'popup': (_popup_frame(),),
        },
        transitions={('boost', 'GO'): 'popup'},
    )

    def navigate(_route: SurfaceRoute) -> None:
        world.navigations.append(_route)
        world.go('popup')

    with pytest.raises(PopupUnclosable):
        run_quiz_task(
            _controller(world, world.frame),
            world.capture,
            corpus,
            _state(tmp_path),
            _entry(catalogue),
            navigate=cast(RouteFirer, navigate),
            arrival_timeout=1.0,
            return_timeout=0.5,
            return_interval=0.5,
            return_attempts=1,
            clock=_SteppingClock(),
            sleep=_SteppingClock().sleep,
            rng=random.Random(3),
        )

    assert world.navigations


@pytest.mark.slow
def test_run_quiz_task_taps_the_rng_chosen_label_in_a_reordered_answer_set(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    """The answer is chosen from the frame, not from a remembered position."""
    reordered = ('False', 'True')
    options = find_answer_options(_question_frame(options=reordered))
    expected = random.Random(7).choice(options)
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'question': (_question_frame(options=reordered),),
            'result': (_result_frame(correct='True'),),
            'coins': (_coins_frame(),),
            'boost_after': (_boost_after_frame(action=None),),
        },
        transitions={
            ('boost', 'GO'): 'question',
            ('question', 'False'): 'result',
            ('question', 'True'): 'result',
            ('result', 'Go back'): 'coins',
            ('coins', 'Earn more >'): 'boost_after',
        },
    )

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=world.navigate,
        arrival_timeout=1.0,
        exit_timeout=1.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(7),
    )

    assert result.outcome is QuizOutcome.COMPLETED
    assert result.chosen == expected.text
    answer_tap = result.taps[1]
    assert answer_tap.label == expected.box.text
    left, top, right, bottom = expected.box.bbox
    assert left <= answer_tap.point[0] <= right
    assert top <= answer_tap.point[1] <= bottom


@pytest.mark.slow
def test_run_quiz_task_keeps_a_redeem_stop_seen_by_the_exit_sweep(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    """A stale frame turning into the interstitial during the sweep still completes."""
    world = _World(
        series={
            'boost': (_quiz_boost_frame(),),
            'question': (_question_frame(),),
            'result': (_result_frame(),),
            # The first capture is a non-redeem POPUP; the sweep's two fresh
            # captures are the redeem interstitial that replaced it.
            'popup': (_popup_frame(), _is_redeem_frame(), _is_redeem_frame()),
            'coins': (_coins_frame(),),
        },
        transitions={
            ('boost', 'GO'): 'question',
            ('question', 'True'): 'result',
            ('question', 'False'): 'result',
            ('result', 'Go back'): 'popup',
        },
    )

    def navigate(_route: SurfaceRoute) -> None:
        world.navigations.append(_route)
        world.go('coins')

    result = run_quiz_task(
        _controller(world, world.frame),
        world.capture,
        corpus,
        _state(tmp_path),
        _entry(catalogue),
        navigate=cast(RouteFirer, navigate),
        arrival_timeout=2.0,
        exit_timeout=2.0,
        return_timeout=2.0,
        return_interval=0.5,
        clock=_SteppingClock(),
        sleep=_SteppingClock().sleep,
        rng=random.Random(3),
    )

    assert result.outcome is QuizOutcome.COMPLETED
    assert result.verification == 'interstitial'
    assert result.stop == REDEEM_INTERSTITIAL
    assert result.recorded is True
