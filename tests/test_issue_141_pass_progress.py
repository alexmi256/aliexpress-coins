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
    assert warning['largest_max_per_run'] == 6
    assert warning['key'] == 'sponsored_items'
    detail = str(warning['detail'])
    assert 'AECOINS_MAX_PASSES=4' in detail
    assert 'max_per_run (6 for' in detail


def test_the_shipped_ceiling_covers_every_family_daily_limit() -> None:
    largest = max(spec.max_per_run for spec in CATALOGUE.allowed)

    assert largest <= DEFAULT_MAX_PASSES
    assert RunConfig.from_environment({}).max_passes == DEFAULT_MAX_PASSES
