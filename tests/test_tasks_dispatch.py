"""The single dispatch gate: allow, refuse, audit and the sealed token (issue #20).

Every row is built by the real roster reader from measured-style geometry, so
these tests exercise the labels the app actually renders. No device is reached:
the controller records nothing because the gate has no controller, and the
executor bypass test uses a capture that fails if it is ever called.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from aliexpress_coins.control.controller import Controller
from aliexpress_coins.control.popups import PopupCorpus, load_popup_corpus
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks.browse import run_browse_task
from aliexpress_coins.tasks.catalogue import (
    DispatchAuthorization,
    TaskCatalogue,
    TaskPolicyContext,
    load_task_catalogue,
)
from aliexpress_coins.tasks.discovery import RosterEntry, enumerate_roster
from aliexpress_coins.tasks.dispatch import DispatchGate, PolicyRefusal, PolicyRefused, authorization_of
from aliexpress_coins.tasks.errors import TaskError
from aliexpress_coins.tasks.notifications import run_notifications_task
from aliexpress_coins.tasks.quiz import run_quiz_task
from aliexpress_coins.tasks.search import run_search_task
from aliexpress_coins.tasks.signin import claim_boost_checkin, claim_daily_signins, claim_streak
from aliexpress_coins.tasks.surface import SurfaceRoute
from aliexpress_coins.tasks.surprise import run_surprise_task

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE_PATH = ROOT / 'specs' / 'tasks.yaml'
CORPUS_PATH = ROOT / 'specs' / 'popups.yaml'
CAPTURED_AT = datetime(2026, 11, 22, 3, 30, tzinfo=UTC)


class _RecordingDevice:
    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))


class _ForbiddenCapture:
    def __init__(self) -> None:
        self.calls = 0

    def capture(self) -> Snapshot:
        self.calls += 1
        raise AssertionError('an executor must not capture without a dispatch decision')


def _box(text: str, left: float, top: float, right: float, bottom: float) -> TextBox:
    return TextBox(
        text=text,
        box=((left, top), (right, top), (right, bottom), (left, bottom)),
        score=0.99,
    )


def _snapshot(*boxes: TextBox, size: tuple[int, int] = (1200, 2000)) -> Snapshot:
    return Snapshot(
        image=Image.new('RGB', size, 'white'),
        boxes=boxes,
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(*size, density=240, sdk=35),
    )


def _row_frame(title: str, *, subtitle: str | None = None) -> Snapshot:
    boxes = [
        _box('Coins boost', 100, 100, 500, 150),
        _box(title, 100, 260, 900, 300),
        _box('GO', 980, 280, 1040, 320),
    ]
    if subtitle is not None:
        boxes.insert(2, _box(subtitle, 100, 305, 1000, 335))
    return _snapshot(*boxes)


def _entry(catalogue: TaskCatalogue, title: str, *, subtitle: str | None = None) -> RosterEntry:
    roster = enumerate_roster(_row_frame(title, subtitle=subtitle), catalogue)
    assert roster.entries
    return roster.entries[0]


@pytest.fixture(scope='module')
def catalogue() -> TaskCatalogue:
    return load_task_catalogue(CATALOGUE_PATH)


@pytest.fixture(scope='module')
def corpus() -> PopupCorpus:
    return load_popup_corpus(CORPUS_PATH)


def test_the_gate_mints_a_sealed_token_and_logs_the_allow(
    catalogue: TaskCatalogue,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    raw = _entry(catalogue, 'Take the coins quiz')
    gate = DispatchGate(catalogue, surface='boost_page')
    assert raw.authorized is False

    authorized = gate.authorize(raw)

    assert authorized.authorized is True
    assert authorized.authorization is not None
    assert authorized.authorization.key == 'coins_quiz'
    assert authorized.authorization.category is authorized.category
    assert authorized.authorization.surface == 'boost_page'
    assert authorization_of(authorized) is authorized.authorization
    assert 'policy_decision' in caplog.text
    assert 'Take the coins quiz' in caplog.text


def test_the_recently_viewed_header_and_prefix_are_not_authorized(
    catalogue: TaskCatalogue,
) -> None:
    gate = DispatchGate(catalogue)
    cases = (
        ('Recently viewed', None),
        ('Browse recently viewed', None),
        ('Deals for you', 'Recently viewed items'),
    )

    for title, subtitle in cases:
        with pytest.raises(PolicyRefused) as raised:
            gate.authorize(_entry(catalogue, title, subtitle=subtitle))
        assert raised.value.refusal.code == 'unrecognised'


def test_a_token_cannot_be_constructed_without_the_gate() -> None:
    with pytest.raises(ValueError, match='minted by the policy gate'):
        DispatchAuthorization(
            key='coins_quiz',
            title='Take the coins quiz',
            category=cast(Any, 'quiz'),
            catalogue_version=1,
            rule='coins_quiz',
            sequence=1,
        )


@pytest.mark.parametrize(
    ('key', 'title', 'sequence', 'message'),
    [
        ('', 'Take the coins quiz', 1, 'needs a key and a title'),
        ('coins_quiz', '', 1, 'needs a key and a title'),
        ('coins_quiz', 'Take the coins quiz', 0, 'positive sequence'),
    ],
)
def test_a_minted_token_still_rejects_empty_identity(
    key: str,
    title: str,
    sequence: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        DispatchAuthorization._mint(
            key=key,
            title=title,
            category=cast(Any, 'quiz'),
            catalogue_version=1,
            rule='coins_quiz',
            surface=None,
            sequence=sequence,
        )


def test_a_token_covers_only_the_row_it_was_minted_for(catalogue: TaskCatalogue) -> None:
    gate = DispatchGate(catalogue)
    quiz = gate.authorize(_entry(catalogue, 'Take the coins quiz'))
    other = _entry(catalogue, 'Daily check-in')

    mismatched = replace(other, authorization=quiz.authorization)

    assert mismatched.authorized is False
    assert quiz.as_dict()['authorized'] is True
    assert quiz.as_dict()['authorization'] is not None
    with pytest.raises(TaskError, match='policy gate was bypassed'):
        authorization_of(mismatched)


@pytest.mark.parametrize('key', sorted(spec.key for spec in load_task_catalogue(CATALOGUE_PATH).denied))
def test_every_denied_family_is_refused_with_its_code_and_audit_line(
    catalogue: TaskCatalogue,
    key: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    spec = catalogue.denied_by_key(key)
    raw = _entry(catalogue, spec.label_patterns[0])
    gate = DispatchGate(catalogue, surface='boost_page')
    device = _RecordingDevice()
    controller = Controller.from_frame(device, _row_frame(spec.label_patterns[0]), rng=random.Random(0))

    with pytest.raises(PolicyRefused) as raised:
        gate.authorize(raw)

    refusal = raised.value.refusal
    assert refusal.code == key
    assert refusal.rule == key
    assert refusal.reason == spec.deny_reason
    assert refusal.label == spec.label_patterns[0]
    assert refusal.surface == 'boost_page'
    assert controller.tap_count == 0
    assert device.taps == []
    assert 'policy_refusal' in caplog.text
    assert f'"code":"{key}"' in caplog.text
    assert f'"rule":"{key}"' in caplog.text


def test_the_refusal_audit_line_is_emitted_once_per_label_and_code(
    catalogue: TaskCatalogue,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    raw = _entry(catalogue, 'Play & Earn')
    gate = DispatchGate(catalogue)

    for _ in range(2):
        with pytest.raises(PolicyRefused):
            gate.authorize(raw)

    assert caplog.text.count('policy_refusal') == 1


def test_an_unrecognised_label_is_refused_by_default(catalogue: TaskCatalogue) -> None:
    raw = _entry(catalogue, 'Win a free cruise')
    gate = DispatchGate(catalogue)

    with pytest.raises(PolicyRefused) as raised:
        gate.authorize(raw)

    assert raised.value.refusal.code == 'unrecognised'
    assert raised.value.refusal.rule is None
    assert raised.value.refusal.reason == catalogue.default_deny_reason


def test_a_refusal_audit_names_a_contaminated_match_instead_of_hiding_it(
    catalogue: TaskCatalogue,
) -> None:
    context = TaskPolicyContext(
        title_elements=('Mystery task',),
        subtitle_elements=('Play & Earn',),
        neighbouring_elements=('Play & Earn',),
    )
    decision = catalogue.decide('Mystery task', subtitle='Play & Earn', context=context)

    refusal = PolicyRefusal.from_decision(decision, surface='boost_page')
    payload = refusal.as_dict()

    assert payload['code'] == 'unrecognised'
    assert payload['contaminated'] == [
        {'pattern': 'Play & Earn', 'field': 'subtitle', 'source': 'Play & Earn'},
    ]


def test_refusal_for_returns_the_audit_refusal_and_none_for_an_allowed_row(
    catalogue: TaskCatalogue,
) -> None:
    gate = DispatchGate(catalogue)

    refusal = gate.refusal_for(_entry(catalogue, 'Play & Earn'))
    allowed = gate.refusal_for(_entry(catalogue, 'Take the coins quiz'))

    assert refusal is not None
    assert refusal.code == 'game_play_and_earn'
    assert allowed is None


def test_a_raw_row_cannot_reach_any_executor(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    state = DailyStateStore(tmp_path / 'state')
    capture = _ForbiddenCapture()
    device = _RecordingDevice()
    frame = _row_frame('Take the coins quiz')
    controller = Controller.from_frame(device, frame, rng=random.Random(0))

    def navigate(_route: SurfaceRoute) -> None:
        return None

    executors: tuple[tuple[str, Callable[..., Any]], ...] = (
        ('coins_quiz', run_quiz_task),
        ('sponsored_items', run_browse_task),
        ('search_for_what_you_love', run_search_task),
        ('surprise_items', run_surprise_task),
        ('notifications', run_notifications_task),
    )

    for key, executor in executors:
        title = catalogue.allowed_by_key(key).label_patterns[0]
        raw = _entry(catalogue, title)
        with pytest.raises(TaskError, match='policy gate was bypassed'):
            executor(controller, capture, corpus, state, raw, navigate=navigate)

    signin_tappers: tuple[Callable[..., Any], ...] = (claim_streak, claim_boost_checkin, claim_daily_signins)
    for tapper in signin_tappers:
        with pytest.raises(TaskError, match='policy gate was bypassed'):
            tapper(controller, capture, corpus, state, authorization=cast(Any, None))

    quiz = DispatchGate(catalogue).authorize(_entry(catalogue, 'Take the coins quiz'))
    assert quiz.authorization is not None
    with pytest.raises(TaskError, match='not a dispatchable daily sign-in'):
        claim_daily_signins(
            controller,
            capture,
            corpus,
            state,
            authorization=quiz.authorization,
        )

    assert capture.calls == 0
    assert device.taps == []
    assert controller.tap_count == 0
