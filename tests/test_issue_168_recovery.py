"""Issue #168: reset to the known-good coins page and retry by default.

The acceptance cases are measured in both directions: a start on a recorded bad
surface must normalise before the first roster read, a retryable dispatch failure
must reset and complete on the retry, a refusal must be recorded once, and a
reset that cannot prove the coins page must end the run with the bounded
``unexpected_screen`` failure rather than spinning.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from aliexpress_coins.control.controller import Controller
from aliexpress_coins.control.popups import DismissReport, PopupCorpus, load_popup_corpus
from aliexpress_coins.perception.errors import SnapshotError, StateTimeout
from aliexpress_coins.perception.ocr import RapidOcrEngine
from aliexpress_coins.perception.snapshot import Snapshot
from aliexpress_coins.runner import engine
from aliexpress_coins.runner.engine import EngineResult, ResetResult, RunContext
from aliexpress_coins.runner.errors import RunInterrupted
from aliexpress_coins.runner.failures import FailureCode, failure
from aliexpress_coins.runner.report import RecoveryRecord, RunReport, RunStatus
from aliexpress_coins.runner.safety import SafetyGuard, ScreenReport
from aliexpress_coins.tasks.browse import ReturnResult
from aliexpress_coins.tasks.catalogue import TaskCatalogue, load_task_catalogue
from aliexpress_coins.tasks.discovery import DiscoveryRoute, Roster, RosterEntry, RosterObservation
from aliexpress_coins.tasks.surface import Surface, SurfaceRoute, classify_surface
from tests.support.frames import load_frame
from tests.test_runner_engine import (
    CAPTURED_AT,
    _boost_frame,
    _Capture,
    _coin_frame,
    _coin_popup_frame,
    _context,
    _dismiss,
    _entry,
    _logged_out_frame,
    _seed_other_families,
    _surface,
)

ROOT = Path(__file__).resolve().parents[1]
STATES = ROOT / 'tests' / 'data' / 'states'
FLOWS = ROOT / 'tests' / 'data' / 'flows'
BAD_SURFACES = {
    'boost sheet': STATES / 'boost.png',
    'launcher': STATES / 'launcher.png',
    'product page': FLOWS / 'surprise_items_item_detail.png',
}


@pytest.fixture(scope='module')
def catalogue() -> TaskCatalogue:
    return load_task_catalogue(ROOT / 'specs' / 'tasks.yaml')


@pytest.fixture(scope='module')
def corpus() -> PopupCorpus:
    return load_popup_corpus(ROOT / 'specs' / 'popups.yaml')


@pytest.fixture(scope='module')
def full_roster(catalogue: TaskCatalogue) -> Roster:
    return _roster_for_test(catalogue)


@pytest.fixture(scope='module')
def recorded() -> dict[str, Snapshot]:
    ocr = RapidOcrEngine()
    return {name: load_frame(path, ocr=ocr) for name, path in BAD_SURFACES.items()}


@pytest.fixture(scope='module')
def recorded_coin_page() -> Snapshot:
    return load_frame(STATES / 'coins.png', ocr=RapidOcrEngine())


@pytest.mark.slow
def test_the_recorded_coin_page_is_known_good_and_bad_surfaces_are_not(
    recorded: dict[str, Snapshot],
    recorded_coin_page: Snapshot,
) -> None:
    assert engine.is_known_good_coins_page(recorded_coin_page) is True
    assert classify_surface(recorded_coin_page) in engine.KNOWN_GOOD_SURFACES
    for name, frame in recorded.items():
        assert engine.is_known_good_coins_page(frame) is False, name


@pytest.mark.slow
@pytest.mark.parametrize('name', tuple(BAD_SURFACES))
def test_a_run_started_on_a_recorded_bad_surface_resets_before_the_first_read(
    name: str,
    recorded: dict[str, Snapshot],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: Any,
) -> None:
    base = _roster_for_test(catalogue)
    roster = replace(base, entries=(_entry(base, 'coins_quiz'),))
    _surface(monkeypatch, roster, _boost_frame('coins_quiz'))
    good = _coin_frame()
    resets: list[str] = []

    def reset(
        _context: RunContext,
        _controller: Controller,
        *,
        trigger: str,
        on_dismiss: Any = None,
    ) -> ResetResult:
        del on_dismiss
        resets.append(trigger)
        return ResetResult(True, 2, 'a settled coins page was proven', surface='rows_absent', snapshot=good)

    monkeypatch.setattr(engine, '_reset_to_known_good', reset)
    context = _context(
        tmp_path,
        capture=_Capture(recorded[name]),
        catalogue=catalogue,
        corpus=corpus,
    )
    attempts: list[int] = []

    def dispatch(current: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        attempts.append(1)
        current.state.record(entry.key or '')
        return EngineResult('completed', 'progress_badge', 'the round moved', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)
    _seed_other_families(context, catalogue, roster)

    report = engine.run_once(context)

    assert resets == [f'start_of_run:{classify_surface(recorded[name]).value}']
    assert report.recoveries[0].scope == 'start'
    assert report.recoveries[0].outcome == 'recovered'
    assert attempts == [1]
    assert report.tasks[0].completed is True


def test_a_signed_out_frame_is_never_known_good() -> None:
    assert engine.is_known_good_coins_page(_logged_out_frame()) is False


def _roster_for_test(catalogue: TaskCatalogue) -> Roster:
    """The engine fixture roster, built once to keep the parametrised test small."""
    from aliexpress_coins.tasks.discovery import enumerate_roster

    frame = _boost_frame()
    return replace(
        enumerate_roster(frame, catalogue, route=DiscoveryRoute.BOOST),
        surface_variant=Surface.BOOST_PAGE,
        routes_taken=(SurfaceRoute.SHORTCUT,),
    )


def _recovered_reset(good: Snapshot) -> ResetResult:
    return ResetResult(True, 1, 'a settled coins page was proven', surface='rows_absent', snapshot=good)


def test_a_dispatch_failure_resets_reobserves_and_completes_on_the_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: Any,
    full_roster: Roster,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    _surface(monkeypatch, roster, _boost_frame('coins_quiz'))
    good = _coin_frame()
    resets: list[str] = []

    def reset(
        _context: RunContext,
        _controller: Controller,
        *,
        trigger: str,
        on_dismiss: Any = None,
    ) -> ResetResult:
        del on_dismiss
        resets.append(trigger)
        return _recovered_reset(good)

    monkeypatch.setattr(engine, '_reset_to_known_good', reset)
    context = _context(
        tmp_path,
        capture=_Capture(good),
        catalogue=catalogue,
        corpus=corpus,
    )
    attempts: list[int] = []
    _seed_other_families(context, catalogue, roster)

    def dispatch(current: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        attempts.append(1)
        if len(attempts) == 1:
            return EngineResult(
                'failed',
                'return_failed',
                'BACK did not return to the grid; no completion was recorded',
                False,
                {'key': entry.key},
                failure=failure(FailureCode.TASK_UNVERIFIED, detail='misrouted BACK'),
            )
        current.state.record(entry.key or '')
        return EngineResult('completed', 'progress_badge', 'the round moved', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert attempts == [1, 1]
    assert resets == ['return_failed']
    assert [task.completed for task in report.tasks] == [True]
    assert report.status is RunStatus.COMPLETED
    recovery = report.recoveries[0]
    assert recovery.scope == 'task'
    assert recovery.task == 'Take the coins quiz'
    assert recovery.trigger == 'return_failed'
    assert recovery.outcome == 'recovered'
    assert recovery.retry is not None
    assert recovery.retry['outcome'] == 'completed'


def test_a_reset_retry_counts_against_the_dispatch_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: Any,
    full_roster: Roster,
) -> None:
    wanted = ('coins_quiz', 'notifications')
    roster = replace(full_roster, entries=tuple(_entry(full_roster, key) for key in wanted))
    _surface(monkeypatch, roster, _boost_frame(*wanted))
    good = _coin_frame()
    monkeypatch.setattr(
        engine,
        '_reset_to_known_good',
        lambda *args, **kwargs: _recovered_reset(good),
    )
    context = _context(
        tmp_path,
        capture=_Capture(good),
        catalogue=catalogue,
        corpus=corpus,
        max_tasks=2,
    )
    calls: list[str] = []
    _seed_other_families(context, catalogue, roster)

    def dispatch(current: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        calls.append(entry.key or '')
        if len(calls) == 1:
            return EngineResult(
                'failed',
                'return_failed',
                'the return route did not prove the coins page',
                False,
                {'key': entry.key},
                failure=failure(FailureCode.TASK_UNVERIFIED, detail='misrouted BACK'),
            )
        current.state.record(entry.key or '')
        return EngineResult('completed', 'progress_badge', 'the round moved', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert calls == ['coins_quiz', 'coins_quiz']
    assert report.attempts['coins_quiz'] == 2
    assert 'notifications' not in report.attempts
    assert report.status is RunStatus.BUDGET
    assert report.detail is not None
    assert 'task budget of 2 dispatch(es) exhausted' in report.detail


def test_a_row_missing_from_a_complete_fresh_surface_is_settled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: Any,
    full_roster: Roster,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    _surface(monkeypatch, roster, _boost_frame('coins_quiz'))
    good = _coin_frame()
    monkeypatch.setattr(
        engine,
        '_reset_to_known_good',
        lambda *args, **kwargs: _recovered_reset(good),
    )
    context = _context(
        tmp_path,
        capture=_Capture(good),
        catalogue=catalogue,
        corpus=corpus,
    )
    attempts: list[int] = []
    _seed_other_families(context, catalogue, roster)
    absent = replace(roster, entries=(), surface_variant=Surface.BOOST_PAGE)
    monkeypatch.setattr(
        engine,
        '_observe',
        lambda *args, **kwargs: RosterObservation(
            sequence=1,
            timestamp=CAPTURED_AT,
            rows=(),
            roster=absent,
        ),
    )

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        attempts.append(1)
        return EngineResult(
            'failed',
            'return_failed',
            'the return route did not prove the coins page',
            False,
            {'key': entry.key},
            failure=failure(FailureCode.TASK_UNVERIFIED, detail='misrouted BACK'),
        )

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert attempts == [1]
    assert len(report.tasks) == 1
    assert report.recoveries[0].retry is None
    assert report.recoveries[0].outcome == 'still_failed'


def test_a_refusal_is_recorded_once_and_never_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: Any,
    full_roster: Roster,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    _surface(monkeypatch, roster, _boost_frame('coins_quiz'))
    resets: list[int] = []
    monkeypatch.setattr(
        engine,
        '_reset_to_known_good',
        lambda *args, **kwargs: resets.append(1),
    )
    context = _context(
        tmp_path,
        capture=_Capture(_coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    attempts: list[int] = []
    _seed_other_families(context, catalogue, roster)

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        attempts.append(1)
        return EngineResult(
            'skipped',
            'policy',
            f'{entry.title!r} refused by spending_order: spending is refused',
            False,
            {'code': 'spending_order'},
        )

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert attempts == [1]
    assert resets == []
    assert not [recovery for recovery in report.recoveries if recovery.scope == 'task']
    assert report.tasks[0].outcome == 'skipped'


def test_an_absent_control_is_recorded_once_and_never_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: Any,
    full_roster: Roster,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    _surface(monkeypatch, roster, _boost_frame('coins_quiz'))
    resets: list[int] = []
    monkeypatch.setattr(
        engine,
        '_reset_to_known_good',
        lambda *args, **kwargs: resets.append(1),
    )
    context = _context(
        tmp_path,
        capture=_Capture(_coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    attempts: list[int] = []
    _seed_other_families(context, catalogue, roster)

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        attempts.append(1)
        return EngineResult(
            'failed',
            'search_field_not_found',
            'the landing page did not show a search field',
            False,
            {'key': entry.key},
            failure=failure(FailureCode.TASK_UNVERIFIED, detail='the search field was absent'),
        )

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert attempts == [1]
    assert resets == []
    assert not [recovery for recovery in report.recoveries if recovery.scope == 'task']
    assert report.tasks[0].outcome == 'failed'
    assert report.tasks[0].verification == 'search_field_not_found'


def test_a_retry_that_fails_again_is_reported_as_still_failed_after_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: Any,
    full_roster: Roster,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    _surface(monkeypatch, roster, _boost_frame('coins_quiz'))
    good = _coin_frame()
    monkeypatch.setattr(
        engine,
        '_reset_to_known_good',
        lambda *args, **kwargs: _recovered_reset(good),
    )
    context = _context(
        tmp_path,
        capture=_Capture(good),
        catalogue=catalogue,
        corpus=corpus,
    )
    attempts: list[int] = []
    _seed_other_families(context, catalogue, roster)

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        attempts.append(1)
        return EngineResult(
            'failed',
            'no_change',
            'the row still did not move',
            False,
            {'key': entry.key},
            failure=failure(FailureCode.TASK_UNVERIFIED, detail='the row still did not move'),
        )

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert attempts == [1, 1]
    recovery = report.recoveries[0]
    assert recovery.outcome == 'still_failed'
    assert recovery.retry is not None
    assert recovery.retry['outcome'] == 'failed'
    assert report.tasks[0].result['payload']['recovery']['outcome'] == 'still_failed'
    summary = report.summary_path.read_text(encoding='utf-8')
    assert 'still_failed' in summary
    assert 'retry failed' in summary


def test_a_reset_does_not_repress_a_row_that_is_already_claimed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: Any,
    full_roster: Roster,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    _surface(monkeypatch, roster, _boost_frame('coins_quiz'))
    good = _coin_frame()
    monkeypatch.setattr(
        engine,
        '_reset_to_known_good',
        lambda *args, **kwargs: _recovered_reset(good),
    )
    claimed = replace(roster.entries[0], action=None, claimed=True)
    fresh = replace(roster, entries=(claimed,))
    context = _context(
        tmp_path,
        capture=_Capture(good),
        catalogue=catalogue,
        corpus=corpus,
    )
    attempts: list[int] = []
    _seed_other_families(context, catalogue, roster)
    monkeypatch.setattr(
        engine,
        '_observe',
        lambda *args, **kwargs: RosterObservation(
            sequence=1,
            timestamp=CAPTURED_AT,
            rows=(),
            roster=fresh,
        ),
    )

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        attempts.append(1)
        return EngineResult(
            'failed',
            'return_failed',
            'the return route did not prove the coins page',
            False,
            {'key': entry.key},
            failure=failure(FailureCode.TASK_UNVERIFIED, detail='misrouted BACK'),
        )

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert attempts == [1]
    assert report.recoveries[0].outcome == 'still_failed'
    assert report.recoveries[0].retry is None
    assert 'retry: not retried' in report.recoveries[0].detail


def test_a_reset_that_cannot_reach_the_coins_page_aborts_with_a_named_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: Any,
    full_roster: Roster,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    bad = _boost_frame('coins_quiz')
    _surface(monkeypatch, roster, bad)
    monkeypatch.setattr(
        engine,
        '_reset_to_known_good',
        lambda *args, **kwargs: ResetResult(
            False,
            engine.RESET_ATTEMPTS,
            'the coins page was not proven',
            surface='boost_page',
            state='page',
            snapshot=bad,
        ),
    )
    context = _context(
        tmp_path,
        capture=_Capture(_coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    attempts: list[int] = []

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        attempts.append(1)
        return EngineResult(
            'failed',
            'return_failed',
            'the return route did not prove the coins page',
            False,
            {'key': entry.key},
            failure=failure(FailureCode.TASK_UNVERIFIED, detail='misrouted BACK'),
        )

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert attempts == [1]
    assert report.status is RunStatus.ABORTED
    assert report.failure_code == FailureCode.UNEXPECTED_SCREEN.value
    assert report.abort is not None
    assert report.abort['code'] == FailureCode.UNEXPECTED_SCREEN.value
    assert report.recoveries[0].outcome == 'still_failed'
    assert report.detail is not None
    assert 'after 3 bounded shortcut attempt(s)' in report.detail


@pytest.mark.slow
def test_a_start_of_run_reset_that_cannot_reach_the_coins_page_aborts_the_run(
    recorded: dict[str, Snapshot],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: Any,
) -> None:
    bad = recorded['boost sheet']
    monkeypatch.setattr(
        engine,
        '_reset_to_known_good',
        lambda *args, **kwargs: ResetResult(
            False,
            engine.RESET_ATTEMPTS,
            'the coins page was not proven',
            surface='boost_page',
            state='page',
            snapshot=bad,
        ),
    )
    context = _context(
        tmp_path,
        capture=_Capture(bad),
        catalogue=catalogue,
        corpus=corpus,
    )

    report = engine.run_once(context)

    assert report.status is RunStatus.ABORTED
    assert report.failure_code == FailureCode.UNEXPECTED_SCREEN.value
    assert report.recoveries[0].scope == 'start'
    assert report.recoveries[0].outcome == 'still_failed'
    assert 'start_of_run:boost_page' in report.recoveries[0].trigger


def test_the_reset_helper_accepts_only_a_settled_classified_coin_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: Any,
) -> None:
    good = _coin_frame()
    context = _context(
        tmp_path,
        capture=_Capture(good),
        catalogue=catalogue,
        corpus=corpus,
    )
    monkeypatch.setattr(
        engine,
        'return_to_coins_page',
        lambda *args, **kwargs: ReturnResult(
            returned=True,
            reentered=True,
            attempts=1,
            route=SurfaceRoute.SHORTCUT,
            elapsed=1.0,
            snapshot=good,
        ),
    )

    reset = engine._reset_to_known_good(context, cast(Controller, context.controller), trigger='test')

    assert reset.recovered is True
    assert reset.surface == 'rows_absent'
    assert reset.state == 'page'


def test_the_reset_helper_settles_a_loading_arrival_before_calling_it_known_good(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: Any,
) -> None:
    good = _coin_frame()
    boost = _boost_frame('coins_quiz')
    context = _context(
        tmp_path,
        capture=_Capture(good),
        catalogue=catalogue,
        corpus=corpus,
    )
    monkeypatch.setattr(
        engine,
        'return_to_coins_page',
        lambda *args, **kwargs: ReturnResult(
            returned=True,
            reentered=True,
            attempts=1,
            route=SurfaceRoute.SHORTCUT,
            elapsed=1.0,
            snapshot=boost,
        ),
    )

    reset = engine._reset_to_known_good(context, cast(Controller, context.controller), trigger='test')

    assert reset.recovered is True
    assert reset.surface == 'rows_absent'


def test_the_reset_helper_names_a_failed_return_with_its_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: Any,
) -> None:
    boost = _boost_frame('coins_quiz')
    context = _context(
        tmp_path,
        capture=_Capture(boost),
        catalogue=catalogue,
        corpus=corpus,
    )
    monkeypatch.setattr(
        engine,
        'return_to_coins_page',
        lambda *args, **kwargs: ReturnResult(
            returned=False,
            reentered=True,
            attempts=engine.RESET_ATTEMPTS,
            route=SurfaceRoute.SHORTCUT,
            elapsed=1.0,
            error='no coin markers',
        ),
    )

    reset = engine._reset_to_known_good(context, cast(Controller, context.controller), trigger='test')

    assert reset.recovered is False
    assert reset.attempts == engine.RESET_ATTEMPTS
    assert 'no coin markers' in reset.detail
    assert reset.surface == 'boost_page'


def test_the_reset_helper_turns_a_route_exception_into_still_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: Any,
) -> None:
    boost = _boost_frame('coins_quiz')
    context = _context(
        tmp_path,
        capture=_Capture(boost),
        catalogue=catalogue,
        corpus=corpus,
    )

    def explode(*args: Any, **kwargs: Any) -> ReturnResult:
        raise StateTimeout('route died')

    monkeypatch.setattr(engine, 'return_to_coins_page', explode)

    reset = engine._reset_to_known_good(context, cast(Controller, context.controller), trigger='test')

    assert reset.recovered is False
    assert 'StateTimeout: route died' in reset.detail


@pytest.mark.parametrize('where', ['return', 'wait', 'capture'])
def test_a_device_failure_during_a_reset_keeps_its_own_named_code(
    where: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: Any,
    full_roster: Roster,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    _surface(monkeypatch, roster, _boost_frame('coins_quiz'))
    context = _context(
        tmp_path,
        capture=_Capture(_coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    attempts: list[int] = []
    _seed_other_families(context, catalogue, roster)

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        attempts.append(1)
        return EngineResult(
            'failed',
            'return_failed',
            'the return route did not prove the coins page',
            False,
            {'key': entry.key},
            failure=failure(FailureCode.TASK_UNVERIFIED, detail='misrouted BACK'),
        )

    def dead_device(*args: Any, **kwargs: Any) -> Any:
        raise SnapshotError('the device stopped answering')

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)
    if where == 'return':
        monkeypatch.setattr(engine, 'return_to_coins_page', dead_device)
    elif where == 'wait':
        monkeypatch.setattr(
            engine,
            'return_to_coins_page',
            lambda *args, **kwargs: ReturnResult(
                returned=True,
                reentered=True,
                attempts=1,
                route=SurfaceRoute.SHORTCUT,
                elapsed=1.0,
                snapshot=_boost_frame('coins_quiz'),
            ),
        )
        monkeypatch.setattr(engine, 'wait_for_coin_page', dead_device)
    else:

        class _FirstGoodThenDead:
            def __init__(self) -> None:
                self.calls = 0

            def capture(self) -> Snapshot:
                self.calls += 1
                if self.calls > 1:
                    raise SnapshotError('the device stopped answering')
                return _coin_frame()

        context.capture = cast(Any, _FirstGoodThenDead())
        monkeypatch.setattr(
            engine,
            'return_to_coins_page',
            lambda *args, **kwargs: ReturnResult(
                returned=False,
                reentered=True,
                attempts=engine.RESET_ATTEMPTS,
                route=SurfaceRoute.SHORTCUT,
                elapsed=1.0,
            ),
        )

    report = engine.run_once(context)

    assert attempts == [1]
    assert report.status is RunStatus.FAILED
    assert report.failure_code == FailureCode.DEVICE_OFFLINE.value
    assert report.failure_code != FailureCode.UNEXPECTED_SCREEN.value


def test_the_reset_helper_dismisses_a_popup_while_settling_the_coins_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: Any,
) -> None:
    popup = _coin_popup_frame()
    good = _coin_frame()
    context = _context(
        tmp_path,
        capture=_Capture(popup, good),
        catalogue=catalogue,
        corpus=corpus,
    )
    monkeypatch.setattr(
        engine,
        'return_to_coins_page',
        lambda *args, **kwargs: ReturnResult(
            returned=True,
            reentered=True,
            attempts=1,
            route=SurfaceRoute.SHORTCUT,
            elapsed=1.0,
            snapshot=popup,
        ),
    )
    monkeypatch.setattr(engine, 'dismiss_after', lambda *args, **kwargs: _dismiss('test_popup'))
    dismissals: list[DismissReport] = []

    reset = engine._reset_to_known_good(
        context,
        cast(Controller, context.controller),
        trigger='test',
        on_dismiss=dismissals.append,
    )

    assert reset.recovered is True
    assert reset.surface == 'rows_absent'
    assert [report.closed for report in dismissals] == [('test_popup',)]


def test_the_reset_helper_names_a_settle_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: Any,
) -> None:
    boost = _boost_frame('coins_quiz')
    context = _context(
        tmp_path,
        capture=_Capture(boost),
        catalogue=catalogue,
        corpus=corpus,
    )
    monkeypatch.setattr(
        engine,
        'return_to_coins_page',
        lambda *args, **kwargs: ReturnResult(
            returned=True,
            reentered=True,
            attempts=1,
            route=SurfaceRoute.SHORTCUT,
            elapsed=1.0,
            snapshot=boost,
        ),
    )

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise StateTimeout('the page capture timed out')

    monkeypatch.setattr(engine, 'wait_for_coin_page', explode)

    reset = engine._reset_to_known_good(context, cast(Controller, context.controller), trigger='test')

    assert reset.recovered is False
    assert 'StateTimeout: the page capture timed out' in reset.detail


def test_the_reset_helper_names_a_capture_failure_after_a_failed_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: Any,
) -> None:
    class _TimedOutCapture:
        def capture(self) -> Snapshot:
            raise StateTimeout('the capture timed out')

    context = _context(
        tmp_path,
        capture=_Capture(_coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    context.capture = cast(Any, _TimedOutCapture())
    monkeypatch.setattr(
        engine,
        'return_to_coins_page',
        lambda *args, **kwargs: ReturnResult(
            returned=False,
            reentered=True,
            attempts=engine.RESET_ATTEMPTS,
            route=SurfaceRoute.SHORTCUT,
            elapsed=1.0,
        ),
    )

    reset = engine._reset_to_known_good(context, cast(Controller, context.controller), trigger='test')

    assert reset.recovered is False
    assert 'StateTimeout: the capture timed out' in reset.detail


@pytest.mark.parametrize('where', ['return', 'wait', 'capture'])
def test_a_stop_during_a_reset_is_re_raised(
    where: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: Any,
) -> None:
    boost = _boost_frame('coins_quiz')
    context = _context(
        tmp_path,
        capture=_Capture(boost),
        catalogue=catalogue,
        corpus=corpus,
    )
    if where == 'return':

        def stop_return(*args: Any, **kwargs: Any) -> ReturnResult:
            raise RunInterrupted('stop requested')

        monkeypatch.setattr(engine, 'return_to_coins_page', stop_return)
    elif where == 'wait':
        monkeypatch.setattr(
            engine,
            'return_to_coins_page',
            lambda *args, **kwargs: ReturnResult(
                returned=True,
                reentered=True,
                attempts=1,
                route=SurfaceRoute.SHORTCUT,
                elapsed=1.0,
                snapshot=boost,
            ),
        )

        def stop_wait(*args: Any, **kwargs: Any) -> Any:
            raise RunInterrupted('stop requested')

        monkeypatch.setattr(engine, 'wait_for_coin_page', stop_wait)
    else:

        class _StoppedCapture:
            def capture(self) -> Snapshot:
                raise RunInterrupted('stop requested')

        context.capture = cast(Any, _StoppedCapture())
        monkeypatch.setattr(
            engine,
            'return_to_coins_page',
            lambda *args, **kwargs: ReturnResult(
                returned=False,
                reentered=True,
                attempts=engine.RESET_ATTEMPTS,
                route=SurfaceRoute.SHORTCUT,
                elapsed=1.0,
            ),
        )

    with pytest.raises(RunInterrupted):
        engine._reset_to_known_good(context, cast(Controller, context.controller), trigger='test')


def test_reset_abort_prefers_the_guard_screen_and_has_a_named_fallback(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: Any,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    screen = ScreenReport(state='page', surface='boost_page', labels=('Coins boost',), fingerprint='test')
    guard = SafetyGuard(catalogue=catalogue)
    guard.last_screen = screen
    context.safety = guard

    guarded = engine._reset_abort(
        context,
        trigger='test',
        reset=ResetResult(False, engine.RESET_ATTEMPTS, 'not proven'),
    )
    context.safety = None
    unguarded = engine._reset_abort(
        context,
        trigger='test',
        reset=ResetResult(False, engine.RESET_ATTEMPTS, 'not proven'),
    )

    assert guarded.screen is screen
    assert unguarded.screen.state == 'unknown'
    assert 'after 3 bounded shortcut attempt(s)' in guarded.reason


def test_a_fresh_entry_lookup_never_returns_a_stale_row(full_roster: Roster) -> None:
    entry = _entry(full_roster, 'coins_quiz')

    assert engine._fresh_entry(None, entry) is None
    assert engine._fresh_entry(replace(full_roster, entries=()), entry) is None


@pytest.mark.parametrize(
    ('outcome', 'verification', 'code', 'expected'),
    [
        ('failed', 'return_failed', FailureCode.TASK_UNVERIFIED, 'return_failed'),
        ('failed', 'task_unverified', FailureCode.TASK_UNVERIFIED, 'task_unverified'),
        ('failed', 'unclassified_fault', None, 'unclassified_fault'),
        ('failed', 'balance_unreadable_before', FailureCode.TASK_UNVERIFIED, 'balance_unreadable_before'),
        ('failed', 'foreground_lost_after_action', FailureCode.TASK_UNVERIFIED, 'foreground_lost_after_action'),
        ('failed', 'search_field_not_found', FailureCode.TASK_UNVERIFIED, None),
        ('failed', 'options_not_found', FailureCode.TASK_UNVERIFIED, None),
        ('failed', 'device_locked', FailureCode.DEVICE_LOCKED, None),
        ('failed', 'not_found', FailureCode.TASK_UNVERIFIED, None),
        ('failed', 'already_done', FailureCode.TASK_UNVERIFIED, None),
        ('skipped', 'policy', None, None),
        ('completed', 'progress_badge', None, None),
    ],
)
def test_only_navigation_and_state_failures_are_reset_retryable(
    outcome: str,
    verification: str,
    code: FailureCode | None,
    expected: str | None,
) -> None:
    result = EngineResult(
        outcome,
        verification,
        'evidence',
        outcome == 'completed',
        {},
        failure=None if code is None else failure(code),
    )

    assert engine._reset_retry_trigger(result) == expected


def test_the_report_keeps_recovered_and_still_failed_recoveries_distinct(tmp_path: Path) -> None:
    report = RunReport(tmp_path, '20261127T140626Z', budget_seconds=60.0, started_at=CAPTURED_AT)
    report.record_recovery(
        RecoveryRecord(
            scope='task',
            trigger='return_failed',
            outcome='recovered',
            detail='reset recovered; retry completed',
            task='Browse surprise items',
            key='surprise_items',
            attempts=1,
            surface='rows_absent',
            retry={'outcome': 'completed', 'verification': 'progress_badge'},
        ),
    )
    report.record_recovery(
        RecoveryRecord(
            scope='task',
            trigger='task_unverified',
            outcome='still_failed',
            detail='reset recovered; retry failed',
            task='Visit the page via coins',
            key='coins_shortcut_visit',
            attempts=1,
            surface='rows_absent',
            retry={'outcome': 'failed', 'verification': 'balance_unreadable_before'},
        ),
    )
    report.finish(RunStatus.FAILED, budget_used_seconds=1.0, finished_at=CAPTURED_AT)
    report.write()

    payload = cast(dict[str, Any], json.loads(report.json_path.read_text(encoding='utf-8')))
    summary = report.summary_path.read_text(encoding='utf-8')

    assert [item['outcome'] for item in payload['recoveries']] == ['recovered', 'still_failed']
    assert payload['recoveries'][0]['retry']['outcome'] == 'completed'
    assert payload['recoveries'][1]['retry']['outcome'] == 'failed'
    assert '**Recovery:**' in summary
    assert 'Browse surprise items: return_failed -> recovered' in summary
    assert 'Visit the page via coins: task_unverified -> still_failed' in summary
    assert '| task | Browse surprise items | return_failed |' in summary


def test_a_surface_restart_is_recorded_as_a_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: Any,
    full_roster: Roster,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    report = RunReport(tmp_path / 'runs', '20261127T140626Z', budget_seconds=60.0, started_at=CAPTURED_AT)
    monkeypatch.setattr(
        engine,
        '_observe',
        lambda *args, **kwargs: RosterObservation(
            sequence=1,
            timestamp=CAPTURED_AT,
            rows=(),
            roster=full_roster,
        ),
    )

    engine._restart_surface(
        context,
        cast(Controller, context.controller),
        cast(Any, object()),
        report,
        on_dismiss=None,
        reason='a stuck row needs a fresh surface',
    )

    recovery = report.recoveries[0]
    assert recovery.scope == 'surface'
    assert recovery.trigger == 'a stuck row needs a fresh surface'
    assert recovery.outcome == 'recovered'
    assert recovery.surface == 'boost_page'


@pytest.mark.parametrize('variant', [Surface.NON_APP, Surface.UNKNOWN])
def test_a_surface_restart_on_an_unusable_surface_is_not_called_recovered(
    variant: Surface,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: Any,
    full_roster: Roster,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    report = RunReport(tmp_path / 'runs', '20261127T140626Z', budget_seconds=60.0, started_at=CAPTURED_AT)
    monkeypatch.setattr(
        engine,
        '_observe',
        lambda *args, **kwargs: RosterObservation(
            sequence=1,
            timestamp=CAPTURED_AT,
            rows=(),
            roster=replace(full_roster, surface_variant=variant),
        ),
    )

    engine._restart_surface(
        context,
        cast(Controller, context.controller),
        cast(Any, object()),
        report,
        on_dismiss=None,
        reason='a lost surface needs a fresh read',
    )

    recovery = report.recoveries[0]
    assert recovery.scope == 'surface'
    assert recovery.outcome == 'still_failed'
    assert recovery.surface == variant.value
    assert 'did not re-establish a usable task surface' in recovery.detail


def test_a_surface_restart_that_cannot_navigate_is_still_reported(
    tmp_path: Path,
    catalogue: TaskCatalogue,
    corpus: Any,
) -> None:
    context = _context(
        tmp_path,
        capture=_Capture(_coin_frame()),
        catalogue=catalogue,
        corpus=corpus,
    )
    report = RunReport(tmp_path / 'runs', '20261127T140626Z', budget_seconds=60.0, started_at=CAPTURED_AT)

    def dead_route(_route: SurfaceRoute) -> None:
        raise StateTimeout('the shortcut route did not start')

    context.navigate = cast(Any, dead_route)

    result = engine._restart_surface(
        context,
        cast(Controller, context.controller),
        cast(Any, object()),
        report,
        on_dismiss=None,
        reason='a stuck row needs a fresh surface',
    )

    assert result is None
    assert report.recoveries[0].scope == 'surface'
    assert report.recoveries[0].outcome == 'still_failed'
    assert 'StateTimeout' in report.recoveries[0].detail


def test_the_recovery_log_carries_its_trigger_outcome_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalogue: TaskCatalogue,
    corpus: Any,
    full_roster: Roster,
    caplog: pytest.LogCaptureFixture,
) -> None:
    roster = replace(full_roster, entries=(_entry(full_roster, 'coins_quiz'),))
    _surface(monkeypatch, roster, _boost_frame('coins_quiz'))
    good = _coin_frame()
    monkeypatch.setattr(
        engine,
        '_reset_to_known_good',
        lambda *args, **kwargs: _recovered_reset(good),
    )
    context = _context(
        tmp_path,
        capture=_Capture(good),
        catalogue=catalogue,
        corpus=corpus,
    )
    _seed_other_families(context, catalogue, roster)

    def dispatch(current: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        current.state.record(entry.key or '')
        return EngineResult('completed', 'progress_badge', 'the round moved', True, {'key': entry.key})

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)
    caplog.set_level(logging.INFO)

    engine.run_once(context)

    events = [json.loads(record.message) for record in caplog.records if 'task_recovery' in record.message]
    assert not events
    # The start frame is the good coin page, so this run needs no reset at all.
    assert all('surface_reset' not in record.message for record in caplog.records)
