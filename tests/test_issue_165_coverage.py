"""Issue #165: prove a rotating sheet by union coverage, not whole-read identity.

The sheet rotates between opens, so two photographs rarely match. These tests
drive the real engine with scripted observations: a rotating union that covers
the claimable set must close the night; a stable union that omits a claimable
family must keep sampling to its bound and stay open; a lost fresh open must not
become a completion proof. The report must carry the sample numbers.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from aliexpress_coins.control.controller import Controller
from aliexpress_coins.control.popups import PopupCorpus, load_popup_corpus
from aliexpress_coins.runner import engine
from aliexpress_coins.runner.engine import EngineResult, RunContext
from aliexpress_coins.runner.report import RunStatus
from aliexpress_coins.tasks.catalogue import TaskCatalogue, load_task_catalogue
from aliexpress_coins.tasks.discovery import RosterEntry
from aliexpress_coins.tasks.surface import Surface
from tests.test_issue_149_rotation import (
    _empty_roster,
    _frame,
    _observation,
    _roster,
    _ScriptedObserver,
    _seed_other_families,
)
from tests.test_runner_engine import _Capture, _coin_frame, _context

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE: TaskCatalogue = load_task_catalogue(ROOT / 'specs' / 'tasks.yaml')
CORPUS: PopupCorpus = load_popup_corpus(ROOT / 'specs' / 'popups.yaml')


def _rotation(report: Any) -> dict[str, Any]:
    return cast(dict[str, Any], cast(dict[str, Any], report.roster)['rotation'])


@pytest.mark.slow
def test_a_rotating_union_that_covers_the_claimable_set_completes_the_night(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _roster('coins_quiz')
    observations = [
        _observation(1, _roster('super_discounts')),
        _observation(2, _roster('coins_quiz')),
        _observation(3, _roster('super_discounts')),
        _observation(4, _roster('coins_quiz')),
    ]
    scripted = _ScriptedObserver(initial, observations)
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: initial)
    monkeypatch.setattr(engine, '_observe', lambda *args, **kwargs: scripted.observe())
    context = _context(
        tmp_path,
        capture=_Capture(_frame('coins_quiz'), _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    context.observer = cast(Any, scripted)
    _seed_other_families(context, seen={'coins_quiz', 'super_discounts'})
    dispatched: list[str] = []

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        dispatched.append(entry.key or '')
        _context.state.record(entry.key or '')
        return EngineResult('completed', 'progress_badge', 'the row moved', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert report.status is RunStatus.COMPLETED
    assert set(dispatched) == {'coins_quiz', 'super_discounts'}
    assert context.state.snapshot().completed_night is True
    rotation = _rotation(report)
    assert rotation['confirmed'] is True
    assert rotation['stopping_reason'] == 'coverage_reached'
    assert rotation['samples'] >= 3
    assert rotation['first_seen']['coins_quiz'] == 1
    assert rotation['first_seen']['super_discounts'] == 2
    assert rotation['union_sizes'][0] == 1
    assert rotation['union_sizes'][1] == 2
    line = next(
        line
        for line in report.summary_path.read_text(encoding='utf-8').splitlines()
        if line.startswith('- **Rotation:**')
    )
    assert 'union size(s) 1 -> 2' in line
    assert 'first seen: coins_quiz@1, super_discounts@2' in line
    assert 'stopped on coverage reached' in line


@pytest.mark.slow
def test_a_stable_union_that_omits_a_claimable_family_keeps_sampling_to_the_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Today's shape: three agreeing reads can still miss ``surprise_items``."""
    initial = _roster('coins_quiz')
    observations = [_observation(index, initial) for index in range(1, 7)]
    scripted = _ScriptedObserver(initial, observations)
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: initial)
    monkeypatch.setattr(engine, '_observe', lambda *args, **kwargs: scripted.observe())
    context = _context(
        tmp_path,
        capture=_Capture(_frame('coins_quiz'), _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    context.observer = cast(Any, scripted)
    context.state.record('coins_quiz')
    _seed_other_families(context, seen={'coins_quiz', 'surprise_items'})

    report = engine.run_once(context)

    assert context.state.snapshot().completed_night is False
    rotation = _rotation(report)
    assert rotation['confirmed'] is False
    assert rotation['stopping_reason'] == 'stable_sample_bound'
    assert rotation['samples'] == 4
    assert rotation['union_sizes'] == [1, 1, 1, 1]
    assert rotation['first_seen'] == {'coins_quiz': 1}
    assert rotation['unread_families'] == ['surprise_items']
    line = next(
        line
        for line in report.summary_path.read_text(encoding='utf-8').splitlines()
        if line.startswith('- **Rotation:**')
    )
    assert 'union size(s) 1 -> 1 -> 1 -> 1' in line
    assert '3 sample(s) added no new family' in line
    assert 'claimable but unseen' in line
    assert 'surprise_items' in line


@pytest.mark.slow
def test_a_lost_fresh_open_keeps_the_night_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _empty_roster()
    scripted = _ScriptedObserver(initial, [_observation(1, initial), None])
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: initial)
    monkeypatch.setattr(engine, '_observe', lambda *args, **kwargs: scripted.observe())
    context = _context(
        tmp_path,
        capture=_Capture(_frame(), _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    context.observer = cast(Any, scripted)

    report = engine.run_once(context)

    assert context.state.snapshot().completed_night is False
    rotation = _rotation(report)
    assert rotation['confirmed'] is False
    assert rotation['stopping_reason'] == 'unreadable_surface'
    line = next(
        line
        for line in report.summary_path.read_text(encoding='utf-8').splitlines()
        if line.startswith('- **Rotation:**')
    )
    assert 'the last fresh open was unreadable' in line


@pytest.mark.slow
def test_an_unreadable_roster_keeps_the_night_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A title nothing could parse is not an empty day (#137, kept by #165)."""
    readable = _roster('coins_quiz')
    titleless = replace(_empty_roster(), unreadable_count=1)
    observations = [
        _observation(1, readable),
        _observation(2, titleless),
        _observation(3, titleless),
        _observation(4, titleless),
    ]
    scripted = _ScriptedObserver(readable, observations)
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: readable)
    monkeypatch.setattr(engine, '_observe', lambda *args, **kwargs: scripted.observe())
    context = _context(
        tmp_path,
        capture=_Capture(_frame('coins_quiz'), _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    context.observer = cast(Any, scripted)
    context.state.record('coins_quiz')
    _seed_other_families(context, seen={'coins_quiz', 'surprise_items'})

    report = engine.run_once(context)

    assert context.state.snapshot().completed_night is False
    assert report.status is RunStatus.BUDGET
    assert report.detail is not None
    assert 'had no readable title' in report.detail


def test_the_coverage_tracker_names_every_stopping_reason() -> None:
    tracker = engine._CoverageTracker(sample_limit=4)
    tracker.samples = 2
    tracker.attempts = 2
    tracker.last_readable = True
    tracker.seen_keys = {'coins_quiz'}
    tracker.stable_samples = 3

    tracker.stop_reason = 'coverage_reached'
    assert tracker.stop_detail(('coins_quiz',)) == 'coverage reached after 2 readable sample(s)'

    tracker.stop_reason = 'stable_sample_bound'
    stable = tracker.stop_detail(('coins_quiz', 'surprise_items'))
    assert 'sample bound after 3 sample(s) added no new family' in stable
    assert 'surprise_items' in stable

    tracker.stop_reason = 'total_sample_bound'
    total = tracker.stop_detail(('coins_quiz', 'surprise_items'))
    assert 'sample bound of 4 reached with the union still growing' in total
    assert 'surprise_items' in total

    tracker.stop_reason = 'incomplete_roster'
    assert tracker.stop_detail(('coins_quiz',)) == 'the read was incomplete after 2 readable sample(s)'

    tracker.last_readable = False
    tracker.stop_reason = 'unreadable_surface'
    unreadable = tracker.stop_detail(('coins_quiz',))
    assert 'the last fresh open was unreadable after 2 attempt(s)' in unreadable

    tracker.stop_reason = 'unreadable_history'
    history = tracker.stop_detail(('coins_quiz', 'surprise_items'))
    assert 'an earlier sample was unreadable' in history
    assert 'surprise_items' in history

    tracker.stop_reason = 'run_ended'
    assert tracker.stop_detail(('coins_quiz',)) == 'sampling stopped after 2 readable sample(s)'


def test_the_coverage_tracker_names_each_finish_condition() -> None:
    tracker = engine._CoverageTracker(sample_limit=4)

    tracker.samples = 1
    tracker.attempts = 1
    tracker.last_readable = False
    tracker.finish(('coins_quiz',))
    assert tracker.stop_reason == 'unreadable_surface'

    tracker = engine._CoverageTracker(sample_limit=4)
    tracker.samples = 2
    tracker.attempts = 2
    tracker.last_readable = True
    tracker.seen_keys = {'coins_quiz'}
    tracker.finish(('coins_quiz',))
    assert tracker.stop_reason == 'coverage_reached'

    tracker = engine._CoverageTracker(sample_limit=4)
    tracker.samples = 2
    tracker.attempts = 2
    tracker.last_readable = True
    tracker.seen_keys = {'coins_quiz'}
    tracker.any_unreadable = True
    tracker.finish(('coins_quiz',))
    assert tracker.stop_reason == 'unreadable_history'

    tracker = engine._CoverageTracker(sample_limit=4)
    tracker.samples = 2
    tracker.attempts = 4
    tracker.last_readable = True
    tracker.finish(('coins_quiz',))
    assert tracker.stop_reason == 'total_sample_bound'

    tracker = engine._CoverageTracker(sample_limit=4)
    tracker.samples = 2
    tracker.attempts = 2
    tracker.last_readable = True
    tracker.stable_samples = 3
    tracker.finish(('coins_quiz',))
    assert tracker.stop_reason == 'stable_sample_bound'

    tracker = engine._CoverageTracker(sample_limit=4)
    tracker.samples = 1
    tracker.attempts = 1
    tracker.last_readable = True
    tracker.finish(('coins_quiz',))
    assert tracker.stop_reason == 'run_ended'


def test_a_broken_family_evidence_store_leaves_every_claimable_family_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aliexpress_coins.runner.state import DailyStateStore, FamilyObservationStore

    def broken(_self: object, _keys: object) -> object:
        raise OSError('the evidence volume went away')

    monkeypatch.setattr(FamilyObservationStore, 'peek', broken)
    store = FamilyObservationStore(tmp_path / 'state')
    keys = engine._claimable_coverage_keys(
        CATALOGUE,
        DailyStateStore(tmp_path / 'state'),
        {},
        store,
        engine.LOG,
    )

    assert 'coins_quiz' in keys
    assert 'surprise_items' in keys


@pytest.mark.slow
def test_a_lost_fresh_open_does_not_let_a_later_clean_sample_complete_the_night(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient unconfirmed read stays in the pass history (#149/#165)."""
    initial = _roster('coins_quiz')
    observations = [
        _observation(1, initial),
        None,
        _observation(3, _roster('super_discounts')),
        _observation(4, _roster('coins_quiz')),
        _observation(5, _roster('super_discounts')),
        _observation(6, _roster('coins_quiz')),
    ]
    scripted = _ScriptedObserver(initial, observations)
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: initial)
    monkeypatch.setattr(engine, '_observe', lambda *args, **kwargs: scripted.observe())
    context = _context(
        tmp_path,
        capture=_Capture(_frame('coins_quiz'), _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    context.observer = cast(Any, scripted)
    _seed_other_families(context, seen={'coins_quiz', 'super_discounts'})

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        _context.state.record(entry.key or '')
        return EngineResult('completed', 'progress_badge', 'the row moved', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert context.state.snapshot().completed_night is False
    rotation = _rotation(report)
    assert rotation['confirmed'] is False
    assert rotation['stopping_reason'] == 'unreadable_history'
    assert report.night is not None
    assert report.night['outcome'] == 'incomplete_roster'


@pytest.mark.slow
def test_a_transient_unknown_surface_does_not_let_a_later_clean_sample_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An UNKNOWN sample is an unreadable surface even when a later read recovers."""
    initial = _roster('coins_quiz')
    unknown = replace(_empty_roster(), surface_variant=Surface.UNKNOWN)
    observations = [
        _observation(1, initial),
        _observation(2, unknown),
        _observation(3, _roster('super_discounts')),
        _observation(4, _roster('coins_quiz')),
        _observation(5, _roster('super_discounts')),
        _observation(6, _roster('coins_quiz')),
    ]
    scripted = _ScriptedObserver(initial, observations)
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: initial)
    monkeypatch.setattr(engine, '_observe', lambda *args, **kwargs: scripted.observe())
    context = _context(
        tmp_path,
        capture=_Capture(_frame('coins_quiz'), _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    context.observer = cast(Any, scripted)
    _seed_other_families(context, seen={'coins_quiz', 'super_discounts'})

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        _context.state.record(entry.key or '')
        return EngineResult('completed', 'progress_badge', 'the row moved', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert context.state.snapshot().completed_night is False
    rotation = _rotation(report)
    assert rotation['confirmed'] is False
    assert rotation['stopping_reason'] == 'unreadable_history'
    line = next(
        line
        for line in report.summary_path.read_text(encoding='utf-8').splitlines()
        if line.startswith('- **Rotation:**')
    )
    assert 'an earlier sample was unreadable' in line
