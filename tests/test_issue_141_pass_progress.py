"""Issue #141: the pass bound must fit the largest family's daily capacity.

The live six-round proof is deliberately deferred to the next night; these tests
prove the loop's bound without claiming a synthetic run as live evidence.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from aliexpress_coins.control.controller import Controller
from aliexpress_coins.control.popups import PopupCorpus, load_popup_corpus
from aliexpress_coins.runner import engine
from aliexpress_coins.runner.config import DEFAULT_MAX_PASSES, RunConfig
from aliexpress_coins.runner.engine import EngineResult, RunContext
from aliexpress_coins.runner.report import RunStatus
from aliexpress_coins.tasks.catalogue import TaskCatalogue, load_task_catalogue
from aliexpress_coins.tasks.discovery import (
    DiscoveryRoute,
    Roster,
    RosterEntry,
    RosterObservation,
    enumerate_roster,
)
from aliexpress_coins.tasks.surface import Surface, SurfaceRoute
from tests.test_runner_engine import (
    CAPTURED_AT,
    _boost_frame,
    _Capture,
    _coin_frame,
    _context,
    _entry,
    _surface,
)

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE: TaskCatalogue = load_task_catalogue(ROOT / 'specs' / 'tasks.yaml')
CORPUS: PopupCorpus = load_popup_corpus(ROOT / 'specs' / 'popups.yaml')


def _roster(*keys: str) -> Roster:
    frame = _boost_frame(*keys)
    return replace(
        enumerate_roster(frame, CATALOGUE, route=DiscoveryRoute.BOOST),
        surface_variant=Surface.BOOST_PAGE,
        routes_taken=(SurfaceRoute.SHORTCUT,),
    )


def _complete(entry: RosterEntry) -> EngineResult:
    return EngineResult('completed', 'progress_badge', 'the round moved', True, {'key': entry.key})


class _ScriptedObserver:
    """Replay prepared observations and advance the roster the loop sees."""

    def __init__(self, initial: Roster, observations: list[RosterObservation]) -> None:
        self._roster = initial
        self._observations = observations
        self.sequence = 0

    @property
    def roster(self) -> Roster | None:
        return self._roster

    def observe(self, *_args: object, **_kwargs: object) -> RosterObservation:
        observation = self._observations[min(self.sequence, len(self._observations) - 1)]
        self.sequence += 1
        if observation.roster is not None:
            self._roster = observation.roster
        return observation


@pytest.mark.slow
def test_six_sponsored_rounds_complete_in_one_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster = _roster('sponsored_items')
    frame = _boost_frame('sponsored_items')
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    assert context.config.max_passes == DEFAULT_MAX_PASSES == 12
    calls: list[str] = []

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        calls.append(entry.key or '')
        _context.state.record(entry.key or '')
        return _complete(entry)

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert calls == ['sponsored_items'] * 6
    assert report.passes == 6
    assert report.status is RunStatus.COMPLETED
    assert [task.completed for task in report.tasks] == [True] * 6
    assert context.state.count('sponsored_items') == 6
    assert context.state.snapshot().completed_night is True
    assert 'pass budget' not in str(report.detail)


@pytest.mark.slow
def test_a_roster_with_nothing_left_completes_in_one_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster = _roster('sponsored_items')
    frame = _boost_frame('sponsored_items')
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    for _ in range(6):
        context.state.record('sponsored_items')

    report = engine.run_once(context)

    assert report.passes == 1
    assert report.status is RunStatus.COMPLETED
    assert report.detail == 'no dispatchable row remains'
    assert report.tasks == []
    assert context.state.snapshot().completed_night is True


@pytest.mark.slow
def test_a_no_progress_roster_terminates_below_the_absolute_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stuck = replace(_entry(_roster('coins_quiz'), 'coins_quiz'), action=None)
    roster = _roster('coins_quiz')
    roster = replace(roster, entries=(stuck,))
    frame = _boost_frame('coins_quiz')
    observation = RosterObservation(
        sequence=1,
        timestamp=CAPTURED_AT,
        rows=(),
        surface_variant=Surface.BOOST_PAGE,
        roster=roster,
    )
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: roster)
    monkeypatch.setattr(engine, '_observe', lambda *args, **kwargs: observation)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    assert context.config.max_passes == DEFAULT_MAX_PASSES
    assert context.config.max_passes > engine.MAX_NO_PROGRESS_PASSES

    report = engine.run_once(context)

    assert report.status is RunStatus.BUDGET
    assert report.passes == engine.MAX_NO_PROGRESS_PASSES
    assert report.detail is not None
    assert 'consecutive no-progress pass' in report.detail
    assert "'Take the coins quiz' was allowed but had no dispatchable action" in report.detail
    assert 'pass budget of 12 exhausted' not in report.detail
    assert SurfaceRoute.SHORTCUT in cast(Any, context.navigate).routes


@pytest.mark.slow
def test_a_revealed_row_resets_the_idle_streak_without_a_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = replace(_roster('coins_quiz'), entries=(), surface_variant=Surface.UNKNOWN)
    quiz = _roster('coins_quiz')
    lost = replace(quiz, scroll_surface_lost=True)
    observations = [
        RosterObservation(
            sequence=1,
            timestamp=CAPTURED_AT,
            rows=(),
            surface_variant=Surface.UNKNOWN,
            roster=initial,
        ),
        # The restart reveals a row, but scrolling lost the boost surface, so the
        # loop restarts again instead of dispatching from it. The reveal alone
        # must reset the idle streak and earn the pass that eventually claims it.
        RosterObservation(
            sequence=2,
            timestamp=CAPTURED_AT,
            rows=(),
            surface_variant=Surface.BOOST_PAGE,
            roster=lost,
        ),
        RosterObservation(
            sequence=3,
            timestamp=CAPTURED_AT,
            rows=(),
            surface_variant=Surface.BOOST_PAGE,
            roster=quiz,
        ),
    ]
    scripted = _ScriptedObserver(initial, observations)
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: initial)
    monkeypatch.setattr(
        engine,
        '_observe',
        lambda *args, **kwargs: scripted.observe(),
    )
    context = _context(
        tmp_path,
        capture=_Capture(_boost_frame('coins_quiz'), _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    context.observer = cast(Any, scripted)

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        _context.state.record(entry.key or '')
        return _complete(entry)

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert report.passes == 3
    assert [task.key for task in report.tasks] == ['coins_quiz']
    assert report.tasks[0].completed is True
    assert context.state.count('coins_quiz') == 1
    assert report.status is RunStatus.COMPLETED


@pytest.mark.slow
def test_a_revealed_row_is_counted_once_when_the_surface_stays_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = replace(_roster('coins_quiz'), entries=(), surface_variant=Surface.UNKNOWN)
    quiz = _roster('coins_quiz')
    lost = replace(quiz, scroll_surface_lost=True)
    observations = [
        RosterObservation(
            sequence=1,
            timestamp=CAPTURED_AT,
            rows=(),
            surface_variant=Surface.UNKNOWN,
            roster=initial,
        ),
        RosterObservation(
            sequence=2,
            timestamp=CAPTURED_AT,
            rows=(),
            surface_variant=Surface.BOOST_PAGE,
            roster=lost,
        ),
    ]
    scripted = _ScriptedObserver(initial, observations)
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: initial)
    monkeypatch.setattr(engine, '_observe', lambda *args, **kwargs: scripted.observe())
    context = _context(
        tmp_path,
        capture=_Capture(_boost_frame('coins_quiz'), _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    context.observer = cast(Any, scripted)

    report = engine.run_once(context)

    # Pass 2 counts the reveal; pass 3 no longer sees a new identity and becomes
    # idle, pass 4 is the second idle pass. A missing pass-start baseline would
    # keep treating the same revealed row as new and run to the 12-sweep ceiling.
    assert report.passes == 4
    assert report.status is RunStatus.BUDGET
    assert report.detail is not None
    assert 'consecutive no-progress pass' in report.detail
    assert 'surface_lost' in report.detail


@pytest.mark.slow
def test_an_idle_pass_after_a_dispatch_still_counts_toward_the_progress_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster = _roster('coins_quiz', 'daily_checkin')
    quiz = _entry(roster, 'coins_quiz')
    stuck = replace(_entry(roster, 'boost_checkin'), action=None)
    roster = replace(roster, entries=(quiz, stuck))
    observation = RosterObservation(
        sequence=1,
        timestamp=CAPTURED_AT,
        rows=(),
        surface_variant=Surface.BOOST_PAGE,
        roster=roster,
    )
    scripted = _ScriptedObserver(roster, [observation])
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: roster)
    monkeypatch.setattr(engine, '_observe', lambda *args, **kwargs: scripted.observe())
    context = _context(
        tmp_path,
        capture=_Capture(_boost_frame('coins_quiz', 'daily_checkin'), _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    context.observer = cast(Any, scripted)

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        _context.state.record(entry.key or '')
        return _complete(entry)

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    # Pass 1 dispatches the quiz; the stuck row then gives passes 2 and 3 no
    # dispatchable work. Leaving dispatched_this_pass set would keep resetting
    # the idle streak and run to the ceiling instead of stopping here.
    assert report.passes == 3
    assert [task.key for task in report.tasks] == ['coins_quiz']
    assert report.status is RunStatus.BUDGET
    assert report.detail is not None
    assert "'Daily check-in' was allowed but had no dispatchable action" in report.detail


@pytest.mark.slow
def test_a_configured_ceiling_still_names_the_row_it_left(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster = _roster('sponsored_items')
    frame = _boost_frame('sponsored_items')
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    context.config = replace(context.config, max_passes=1)

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        _context.state.record(entry.key or '')
        return _complete(entry)

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert report.status is RunStatus.BUDGET
    assert report.passes == 1
    assert report.detail is not None
    assert 'pass budget of 1 exhausted' in report.detail
    assert "'sponsored_items' is still dispatchable" in report.detail
    skip = next(row for row in report.skipped if row.key == 'sponsored_items')
    assert skip.reason == 'pass budget'
    assert 'pass budget of 1 exhausted' in skip.detail


def test_a_ceiling_below_the_largest_family_limit_warns_at_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    roster = _roster('coins_quiz')
    frame = _boost_frame('coins_quiz')
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    context.config = replace(context.config, max_passes=4)
    caplog.set_level(logging.WARNING)

    engine.run_once(context)

    warning = next(
        json.loads(record.message) for record in caplog.records if 'pass_budget_below_daily_capacity' in record.message
    )
    assert warning['event'] == 'pass_budget_below_daily_capacity'
    assert warning['max_passes'] == 4
    assert warning['recommended_passes'] == 12
    assert warning['largest_max_per_run'] == 6
    assert warning['max_task_attempts'] == 2
    assert warning['key'] == 'sponsored_items'
    detail = str(warning['detail'])
    assert 'AECOINS_MAX_PASSES=4' in detail
    assert '12 sweeps needed' in detail
    assert '6 rounds for' in detail
    assert 'up to 2 attempt(s) per round' in detail
    assert 'success resets the failure streak' in detail


def test_the_ceiling_warning_boundary_is_retry_aware(caplog: pytest.LogCaptureFixture) -> None:
    config = RunConfig.from_environment({})
    logger = logging.getLogger('test.ceiling')
    caplog.set_level(logging.WARNING)

    engine._warn_pass_ceiling(replace(config, max_passes=11), CATALOGUE, logger)
    warnings = [
        json.loads(record.message) for record in caplog.records if 'pass_budget_below_daily_capacity' in record.message
    ]
    assert len(warnings) == 1
    assert warnings[0]['max_passes'] == 11
    assert warnings[0]['recommended_passes'] == 12

    caplog.clear()
    engine._warn_pass_ceiling(replace(config, max_passes=12), CATALOGUE, logger)
    assert not [record for record in caplog.records if 'pass_budget_below_daily_capacity' in record.message]


def test_the_shipped_ceiling_covers_every_family_daily_limit() -> None:
    largest = max(spec.max_per_run for spec in CATALOGUE.allowed)
    config = RunConfig.from_environment({})
    recommended = largest * config.max_task_attempts

    assert recommended <= DEFAULT_MAX_PASSES
    assert config.max_passes == DEFAULT_MAX_PASSES
