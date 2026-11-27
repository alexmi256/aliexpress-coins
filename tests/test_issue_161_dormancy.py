"""Issue #161: a family that is not being offered stops blocking the night.

The rotating sheet makes one absence ambiguous, so these tests use the real
cross-day evidence store with injected dates. A complete absence streak of the
configured length is reported as ``not_offered``; a bad read is not evidence of
absence and still keeps the night open.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest

from aliexpress_coins.control.popups import PopupCorpus, load_popup_corpus
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.runner import engine
from aliexpress_coins.runner.engine import RunContext
from aliexpress_coins.runner.errors import StateWriteError
from aliexpress_coins.runner.report import RunStatus
from aliexpress_coins.runner.state import DailyStateStore, FamilyObservationStore
from aliexpress_coins.tasks.catalogue import TaskCatalogue, load_task_catalogue
from aliexpress_coins.tasks.discovery import DiscoveryRoute, Roster, RosterObservation, enumerate_roster
from aliexpress_coins.tasks.families import FamilyEvidence
from aliexpress_coins.tasks.surface import Surface, SurfaceRoute
from tests.test_runner_engine import (
    CAPTURED_AT,
    _box,
    _Capture,
    _coin_frame,
    _context,
    _snapshot,
    _surface,
)

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE: TaskCatalogue = load_task_catalogue(ROOT / 'specs' / 'tasks.yaml')
CORPUS: PopupCorpus = load_popup_corpus(ROOT / 'specs' / 'popups.yaml')

DAY = date(2026, 11, 22)
PREVIOUS_DAY = date(2026, 11, 21)
SEED = date(2026, 11, 20)


def _frame(*keys: str) -> Snapshot:
    rows: dict[str, tuple[str, str]] = {
        'coins_quiz': ('Take the coins quiz', 'Answer one question'),
        'learn_to_save': (
            'Learn to save money with Coins Credits',
            'Stack with coins, coupons and other discounts',
        ),
    }
    boxes: list[TextBox] = [_box('Coins boost', 100, 100, 500, 150)]
    for index, key in enumerate(keys):
        title, subtitle = rows[key]
        top = 260 + index * 150
        boxes.append(_box(title, 100, top, 800, top + 40))
        boxes.append(_box(subtitle, 100, top + 45, 900, top + 75))
        boxes.append(_box('GO', 980, top + 20, 1040, top + 60))
    return _snapshot(*boxes)


def _roster(*keys: str) -> Roster:
    return replace(
        enumerate_roster(_frame(*keys), CATALOGUE, route=DiscoveryRoute.BOOST),
        surface_variant=Surface.BOOST_PAGE,
        routes_taken=(SurfaceRoute.SHORTCUT,),
    )


def _seed_other_families(context: RunContext, *, seen: set[str]) -> None:
    for spec in CATALOGUE.allowed:
        if spec.key in seen:
            continue
        for _ in range(spec.max_per_run):
            context.state.record(spec.key)


def _seed_absence(tmp_path: Path, days: Iterable[date], *, samples: int = 2) -> None:
    store = FamilyObservationStore(tmp_path / 'state', clock=lambda: CAPTURED_AT)
    for day in days:
        store.observe(
            day=day,
            keys=('learn_to_save',),
            seen=(),
            samples=samples,
            dormancy_days=2,
            retry_days=7,
        )


def _context_for(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    roster: Roster,
    frame: Snapshot,
    dormancy_days: int = 2,
) -> RunContext:
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    context.config = replace(context.config, family_dormancy_days=dormancy_days, family_retry_days=7)
    return context


def _rotation(report: Any) -> dict[str, Any]:
    return cast(dict[str, Any], cast(dict[str, Any], report.roster)['rotation'])


@pytest.mark.slow
def test_a_family_at_the_threshold_is_not_offered_and_the_night_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster = _roster('coins_quiz')
    frame = _frame('coins_quiz')
    _seed_absence(tmp_path, [PREVIOUS_DAY], samples=5)
    context = _context_for(tmp_path, monkeypatch, roster=roster, frame=frame)
    context.state.record('coins_quiz')
    _seed_other_families(context, seen={'coins_quiz', 'learn_to_save'})

    report = engine.run_once(context)

    rotation = _rotation(report)
    assert rotation['unread_families'] == []
    assert rotation['not_offered_families'] == ['learn_to_save']
    assert len(rotation['not_offered']) == 1
    evidence = cast(dict[str, Any], rotation['not_offered'][0])
    assert evidence['key'] == 'learn_to_save'
    assert evidence['missed_days'] == 2
    assert evidence['dormant'] is True
    assert evidence['samples'] > 5
    assert evidence['dormant_since'] == DAY.isoformat()
    assert cast(list[dict[str, Any]], rotation['dormant_families'])[0]['key'] == 'learn_to_save'
    assert report.status is RunStatus.COMPLETED
    assert report.night is not None
    assert report.night['outcome'] == 'exhausted'
    assert cast(list[dict[str, Any]], report.night['dormant_families'])[0]['key'] == 'learn_to_save'
    assert context.state.snapshot().completed_night is True
    night = context.state.snapshot().last_night
    assert night is not None
    assert [family.key for family in night.dormant_families] == ['learn_to_save']
    summary = report.summary_path.read_text(encoding='utf-8')
    rotation_line = next(line for line in summary.splitlines() if line.startswith('- **Rotation:**'))
    night_line = next(line for line in summary.splitlines() if line.startswith('- **Night:**'))
    assert 'not offered for 2 day(s)' in rotation_line
    assert 'dormant since 2026-11-22' in rotation_line
    assert 'sample(s)' in rotation_line
    assert 'dormant and not offered: learn_to_save' in night_line


@pytest.mark.slow
def test_below_the_threshold_a_family_is_still_unread_and_keeps_the_night_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster = _roster('coins_quiz')
    frame = _frame('coins_quiz')
    _seed_absence(tmp_path, [PREVIOUS_DAY])
    context = _context_for(tmp_path, monkeypatch, roster=roster, frame=frame, dormancy_days=3)
    context.state.record('coins_quiz')
    _seed_other_families(context, seen={'coins_quiz', 'learn_to_save'})

    report = engine.run_once(context)

    rotation = _rotation(report)
    assert rotation['unread_families'] == ['learn_to_save']
    assert rotation['not_offered_families'] == []
    assert report.night is not None
    assert report.night['outcome'] == 'incomplete_roster'
    assert context.state.snapshot().completed_night is False


@pytest.mark.slow
def test_a_failed_read_does_not_advance_dormancy_or_complete_the_night(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost surface is not evidence that the family was not offered."""
    roster = _roster('coins_quiz')
    unreadable = replace(roster, scroll_surface_lost=True)
    frame = _frame('coins_quiz')
    _seed_absence(tmp_path, [PREVIOUS_DAY], samples=4)
    context = _context_for(tmp_path, monkeypatch, roster=roster, frame=frame)
    context.state.record('coins_quiz')
    _seed_other_families(context, seen={'coins_quiz', 'learn_to_save'})
    monkeypatch.setattr(
        engine,
        '_observe',
        lambda *args, **kwargs: RosterObservation(
            sequence=1,
            timestamp=CAPTURED_AT,
            rows=(),
            surface_variant=Surface.BOOST_PAGE,
            roster=unreadable,
        ),
    )

    report = engine.run_once(context)

    stored = FamilyObservationStore(tmp_path / 'state', clock=lambda: CAPTURED_AT).load()['learn_to_save']
    assert stored.missed_days == 1
    assert stored.samples == 4
    assert stored.dormant is False
    rotation = _rotation(report)
    assert rotation['unread_families'] == ['learn_to_save']
    assert rotation['not_offered_families'] == []
    assert report.roster is not None
    assert report.roster['incomplete'] is True
    assert report.night is not None
    assert report.night['outcome'] == 'incomplete_roster'
    assert context.state.snapshot().completed_night is False


@pytest.mark.slow
def test_a_dormant_family_is_reprobed_on_the_slower_cadence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster = _roster('coins_quiz')
    frame = _frame('coins_quiz')
    _seed_absence(tmp_path, [date(2026, 11, 13), date(2026, 11, 14)])
    context = _context_for(tmp_path, monkeypatch, roster=roster, frame=frame)
    context.state.record('coins_quiz')
    _seed_other_families(context, seen={'coins_quiz', 'learn_to_save'})

    report = engine.run_once(context)

    evidence = cast(dict[str, Any], _rotation(report)['not_offered'][0])
    assert evidence['retried_on'] == DAY.isoformat()
    assert evidence['next_probe'] == '2026-11-29'
    assert 'family_probe_due' in report.event_log_path.read_text(encoding='utf-8')
    assert report.night is not None
    assert report.night['outcome'] == 'exhausted'


@pytest.mark.slow
def test_an_evidence_store_failure_falls_back_to_the_unread_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken artefact must not turn an unseen family into a missing one."""
    roster = _roster('coins_quiz')
    frame = _frame('coins_quiz')
    _seed_absence(tmp_path, [PREVIOUS_DAY], samples=4)
    context = _context_for(tmp_path, monkeypatch, roster=roster, frame=frame)
    context.state.record('coins_quiz')
    _seed_other_families(context, seen={'coins_quiz', 'learn_to_save'})

    def broken(*_args: object, **_kwargs: object) -> None:
        raise StateWriteError('the evidence volume is read-only')

    monkeypatch.setattr(FamilyObservationStore, 'observe', broken)

    report = engine.run_once(context)

    rotation = _rotation(report)
    assert rotation['unread_families'] == ['learn_to_save']
    assert rotation['not_offered_families'] == []
    assert report.roster is not None
    assert report.roster['incomplete'] is True
    assert context.state.snapshot().completed_night is False
    assert 'family_observation_failed' in report.event_log_path.read_text(encoding='utf-8')


@pytest.mark.slow
def test_a_sighting_during_an_incomplete_read_still_clears_dormancy(tmp_path: Path) -> None:
    state = FamilyObservationStore(tmp_path / 'state', clock=lambda: CAPTURED_AT)
    state.observe(day=SEED, keys=('learn_to_save',), seen=(), samples=2, dormancy_days=1, retry_days=7)
    assert state.load()['learn_to_save'].dormant is True

    unread, _claimed, not_offered, dormant = engine._family_rotation(
        _roster('learn_to_save'),
        CATALOGUE,
        DailyStateStore(tmp_path / 'state', clock=lambda: CAPTURED_AT),
        {},
        state,
        day=DAY,
        samples=2,
        dormancy_days=2,
        retry_days=7,
        trustworthy=False,
        logger=logging.getLogger('test.issue_161'),
    )

    assert 'learn_to_save' not in unread
    assert not_offered == ()
    assert dormant == ()
    stored = FamilyObservationStore(tmp_path / 'state', clock=lambda: CAPTURED_AT).load()['learn_to_save']
    assert stored == FamilyEvidence(key='learn_to_save', last_seen=DAY, last_checked=DAY)
    assert stored.dormant is False


@pytest.mark.slow
def test_a_dormant_family_that_returns_is_read_and_clears_its_dormancy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster = _roster('coins_quiz', 'learn_to_save')
    frame = _frame('coins_quiz', 'learn_to_save')
    _seed_absence(tmp_path, [SEED, PREVIOUS_DAY])
    context = _context_for(tmp_path, monkeypatch, roster=roster, frame=frame)
    context.state.record('coins_quiz')
    _seed_other_families(context, seen={'coins_quiz', 'learn_to_save'})
    dispatched: list[str] = []

    def dispatch(_context: RunContext, _controller: object, entry: Any) -> Any:
        dispatched.append(entry.key or '')
        _context.state.record(entry.key or '')
        return engine.EngineResult(
            'completed',
            'progress_badge',
            'the row moved',
            True,
            {'key': entry.key},
        )

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    assert 'learn_to_save' in dispatched
    stored = FamilyObservationStore(tmp_path / 'state', clock=lambda: CAPTURED_AT).load()['learn_to_save']
    assert stored.dormant is False
    assert stored.missed_days == 0
    assert stored.unseen_since is None
    rotation = _rotation(report)
    assert rotation['not_offered_families'] == []
    assert report.night is not None
    assert report.night['outcome'] == 'exhausted'
