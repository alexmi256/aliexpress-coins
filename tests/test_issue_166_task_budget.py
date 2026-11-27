"""Issue #166: the task cap is derived from the catalogue, not a guessed twelve.

The acceptance case is a roster that offers seventeen dispatches: the old
constant stopped at twelve and skipped an allowed row as ``run budget``. These
tests use synthetic catalogues so the derivation itself, the startup warning and
the absolute backstop are measured without pretending to be live evidence.
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
from aliexpress_coins.perception.snapshot import Snapshot
from aliexpress_coins.runner import engine
from aliexpress_coins.runner.config import MAX_TASK_DISPATCHES, RunConfig
from aliexpress_coins.runner.engine import EngineResult, RunContext, TaskBudget
from aliexpress_coins.runner.report import RunReport, RunStatus
from aliexpress_coins.tasks.catalogue import AllowedTaskSpec, TaskCatalogue, TaskCategory, load_task_catalogue
from aliexpress_coins.tasks.discovery import (
    DiscoveryRoute,
    Roster,
    RosterEntry,
    RosterObservation,
    enumerate_roster,
)
from aliexpress_coins.tasks.surface import Surface, SurfaceRoute
from tests.test_runner_engine import CAPTURED_AT, _box, _Capture, _coin_frame, _context, _snapshot, _surface

ROOT = Path(__file__).resolve().parents[1]
SHIPPED: TaskCatalogue = load_task_catalogue(ROOT / 'specs' / 'tasks.yaml')
CORPUS: PopupCorpus = load_popup_corpus(ROOT / 'specs' / 'popups.yaml')


def _catalogue(*specs: AllowedTaskSpec) -> TaskCatalogue:
    return TaskCatalogue(
        version=SHIPPED.version,
        default_deny_reason=SHIPPED.default_deny_reason,
        allowed=specs,
        denied=SHIPPED.denied,
    )


def _spec(key: str, title: str, *, max_per_run: int = 1, repeatable: bool = False) -> AllowedTaskSpec:
    return AllowedTaskSpec(
        key=key,
        label_patterns=(title,),
        category=TaskCategory.BROWSE_WAIT,
        wait_seconds=0,
        repeatable=repeatable,
        max_per_run=max_per_run,
    )


def _frame(titles: tuple[str, ...]) -> Snapshot:
    boxes = [_box('Coins boost', 100, 100, 500, 150)]
    for index, title in enumerate(titles):
        top = 220 + index * 90
        boxes.extend((_box(title, 100, top, 700, top + 35), _box('GO', 980, top, 1040, top + 40)))
    return _snapshot(*boxes)


def _roster(catalogue: TaskCatalogue, titles: tuple[str, ...]) -> tuple[Roster, Snapshot]:
    frame = _frame(titles)
    return (
        replace(
            enumerate_roster(frame, catalogue, route=DiscoveryRoute.BOOST),
            surface_variant=Surface.BOOST_PAGE,
            routes_taken=(SurfaceRoute.SHORTCUT,),
        ),
        frame,
    )


class _ReplayObserver:
    """Replay the prepared roster instead of re-classifying a synthetic frame."""

    def __init__(self, roster: Roster) -> None:
        self._roster = roster
        self.sequence = 0

    @property
    def roster(self) -> Roster | None:
        return self._roster

    def observe(self, *_args: object, **_kwargs: object) -> RosterObservation:
        self.sequence += 1
        return RosterObservation(
            sequence=self.sequence,
            timestamp=CAPTURED_AT,
            rows=(),
            surface_variant=Surface.BOOST_PAGE,
            roster=self._roster,
        )


def _complete(run_context: RunContext, entry: RosterEntry) -> EngineResult:
    run_context.state.record(entry.key or '')
    return EngineResult('completed', 'progress_badge', 'the round moved', True, {'key': entry.key})


def _default_config(context: RunContext) -> None:
    """Use the shipped default: no explicit task cap, so the catalogue derives it."""
    context.config = replace(context.config, max_tasks=None)


def _replay(context: RunContext, roster: Roster) -> None:
    """Attach a roster replay so the synthetic frame never needs OCR classification."""
    context.observer = cast(Any, _ReplayObserver(roster))


def _warnings(caplog: pytest.LogCaptureFixture, event: str) -> list[dict[str, object]]:
    return [json.loads(record.message) for record in caplog.records if event in record.message]


@pytest.mark.slow
def test_seventeen_dispatch_roster_completes_with_the_default_derived_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    titles = tuple(f'Daily task {index}' for index in range(1, 18))
    catalogue = _catalogue(*(_spec(f'family_{index}', title) for index, title in enumerate(titles, start=1)))
    roster, frame = _roster(catalogue, titles)
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=CORPUS,
    )
    _default_config(context)
    _replay(context, roster)
    caplog.set_level(logging.WARNING)

    calls: list[str] = []

    def dispatch(run_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        calls.append(entry.key or '')
        return _complete(run_context, entry)

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    budget = engine.resolve_task_budget(context.config, catalogue)
    assert budget.derived is True
    assert budget.need == 34
    assert budget.limit == budget.need
    assert calls == []

    report = engine.run_once(context)

    assert len(calls) == 17
    assert len(report.tasks) == 17
    assert all(task.completed for task in report.tasks)
    assert report.status is RunStatus.COMPLETED
    assert report.detail == 'no dispatchable row remains'
    assert not [skip for skip in report.skipped if skip.reason == 'run budget']
    assert not _warnings(caplog, 'task_budget_below_daily_capacity')


@pytest.mark.slow
def test_a_configured_cap_below_the_derived_need_warns_and_names_the_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    titles = tuple(f'Daily task {index}' for index in range(1, 18))
    catalogue = _catalogue(*(_spec(f'family_{index}', title) for index, title in enumerate(titles, start=1)))
    roster, frame = _roster(catalogue, titles)
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=CORPUS,
    )
    context.config = replace(context.config, max_tasks=5)
    _replay(context, roster)
    caplog.set_level(logging.WARNING)

    def dispatch(run_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        return _complete(run_context, entry)

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    warnings = _warnings(caplog, 'task_budget_below_daily_capacity')
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning['event'] == 'task_budget_below_daily_capacity'
    assert warning['max_tasks'] == 5
    assert warning['recommended_tasks'] == 34
    assert warning['derived_task_need'] == 34
    assert warning['max_task_attempts'] == 2
    assert warning['absolute_ceiling'] == MAX_TASK_DISPATCHES
    assert 'AECOINS_MAX_TASKS_PER_RUN=5' in str(warning['detail'])
    assert '34 dispatches' in str(warning['detail'])

    assert report.status is RunStatus.BUDGET
    assert len(report.tasks) == 5
    assert report.detail is not None
    assert 'task budget of 5 dispatch(es) exhausted' in report.detail
    assert 'the configured cap is the reason' in report.detail
    assert 'derived need was 34 dispatch(es)' in report.detail
    skip = next(row for row in report.skipped if row.reason == 'run budget')
    assert "the catalogue's derived need was 34 dispatch(es)" in skip.detail


@pytest.mark.slow
def test_a_pathological_roster_still_stops_at_the_absolute_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    title = 'Explore sponsored items'
    catalogue = _catalogue(_spec('runaway_family', title, max_per_run=100, repeatable=True))
    roster, frame = _roster(catalogue, (title,))
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=CORPUS,
    )
    _default_config(context)
    _replay(context, roster)
    # The pass ceiling is a separate bound (#141); lift it so this test measures
    # only the task backstop that #166 adds.
    context.config = replace(context.config, max_passes=100)
    caplog.set_level(logging.WARNING)

    def dispatch(run_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        return _complete(run_context, entry)

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    budget = engine.resolve_task_budget(context.config, catalogue)
    assert budget.need == 200
    assert budget.limit == MAX_TASK_DISPATCHES == 64

    report = engine.run_once(context)

    assert len(report.tasks) == MAX_TASK_DISPATCHES
    assert report.status is RunStatus.BUDGET
    assert report.detail is not None
    assert f'task budget of {MAX_TASK_DISPATCHES} dispatch(es) exhausted' in report.detail
    assert 'the absolute ceiling is the reason' in report.detail
    assert 'derived need was 200 dispatch(es)' in report.detail
    ceiling_warnings = _warnings(caplog, 'task_budget_ceiling_below_daily_capacity')
    assert len(ceiling_warnings) == 1
    assert ceiling_warnings[0]['derived_task_need'] == 200
    assert ceiling_warnings[0]['absolute_ceiling'] == MAX_TASK_DISPATCHES


def test_the_derived_limit_names_the_full_worst_case_when_it_stops() -> None:
    budget = TaskBudget(limit=42, need=42, ceiling=MAX_TASK_DISPATCHES, configured=None, max_task_attempts=2)

    detail = budget.describe_stop()

    assert 'task budget of 42 dispatch(es) exhausted' in detail
    assert "the catalogue's derived need was 42 dispatch(es)" in detail
    assert 'up to 2 attempt(s) per round' in detail
    assert 'full worst-case allowance' in detail


def test_an_explicit_cap_above_the_absolute_ceiling_warns_and_is_clamped(caplog: pytest.LogCaptureFixture) -> None:
    budget = TaskBudget(
        limit=MAX_TASK_DISPATCHES, need=42, ceiling=MAX_TASK_DISPATCHES, configured=100, max_task_attempts=2
    )
    caplog.set_level(logging.WARNING)

    engine._warn_task_ceiling(budget, logging.getLogger('test.task-ceiling'))

    detail = budget.describe_stop()
    assert 'the absolute ceiling is the reason' in detail
    assert 'AECOINS_MAX_TASKS_PER_RUN=100' in detail
    warnings = _warnings(caplog, 'task_budget_above_absolute_ceiling')
    assert len(warnings) == 1
    assert warnings[0]['max_tasks'] == 100
    assert warnings[0]['absolute_ceiling'] == MAX_TASK_DISPATCHES
    assert 'stop at 64 dispatches' in str(warnings[0]['detail'])


def test_the_observer_derives_its_cap_when_the_config_has_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalogue = _catalogue(_spec('family_1', 'Daily task 1'))
    roster, frame = _roster(catalogue, ('Daily task 1',))
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=catalogue,
        corpus=CORPUS,
    )
    _default_config(context)
    report = RunReport.for_run(tmp_path / 'runs', budget_seconds=60, clock=lambda: CAPTURED_AT)

    observer = engine._observer(context, report, roster)

    assert observer.max_observations == 2 + context.config.max_passes + 2


def test_the_shipped_catalogue_derives_more_than_the_old_twelve() -> None:
    config = RunConfig.from_environment({})
    budget = engine.resolve_task_budget(config, SHIPPED)

    assert config.max_tasks is None
    assert budget.derived is True
    assert budget.need == sum(spec.max_per_run * config.max_task_attempts for spec in SHIPPED.allowed)
    assert budget.need == 42
    assert budget.limit == 42
    assert budget.ceiling == MAX_TASK_DISPATCHES
    assert budget.limit < MAX_TASK_DISPATCHES


def test_a_catalogue_larger_than_an_explicit_cap_is_clamped_only_by_the_ceiling() -> None:
    config = replace(RunConfig.from_environment({}), max_tasks=12)
    catalogue = _catalogue(_spec('runaway_family', 'Explore sponsored items', max_per_run=100, repeatable=True))

    configured = engine.resolve_task_budget(config, catalogue)
    assert isinstance(configured, TaskBudget)
    assert configured.need == 200
    assert configured.limit == 12
    assert configured.below_need is True

    derived = engine.resolve_task_budget(replace(config, max_tasks=None), catalogue)
    assert derived.need == 200
    assert derived.limit == MAX_TASK_DISPATCHES
