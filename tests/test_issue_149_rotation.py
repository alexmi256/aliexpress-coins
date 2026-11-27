"""Issue #149: read the rotating Coins boost sheet across passes.

The sheet is server-driven and rotates rows between loads, so one read is one
sample. These tests use fake captures and a scripted observer; no device is
touched. They prove the union, the two-read completion rule and the report.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from aliexpress_coins.control.controller import Controller
from aliexpress_coins.control.popups import PopupCorpus, load_popup_corpus
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.runner import engine
from aliexpress_coins.runner import report as report_module
from aliexpress_coins.runner.engine import EngineResult, RunContext
from aliexpress_coins.runner.report import RunStatus
from aliexpress_coins.tasks.catalogue import TaskCatalogue, load_task_catalogue
from aliexpress_coins.tasks.discovery import (
    DiscoveryRoute,
    Roster,
    RosterEntry,
    RosterObservation,
    enumerate_roster,
    merge_rosters,
)
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

_ROWS: dict[str, tuple[str, str]] = {
    'coins_quiz': ('Take the coins quiz', 'Answer one question'),
    'super_discounts': ('View Super discounts', 'Check out this page for 15 seconds'),
    'notifications': ('Turn on notifications', 'Stay informed when rewards drop'),
}

_DENIED: dict[str, tuple[str, str]] = {
    'game': ('Play & Earn', 'A mini-game'),
    'social': ('Share coins with friends', 'Win rewards together'),
}


def _frame(*keys: str) -> Snapshot:
    boxes: list[TextBox] = [_box('Coins boost', 100, 100, 500, 150)]
    for index, key in enumerate(keys):
        title, subtitle = _ROWS[key]
        top = 260 + index * 150
        boxes.append(_box(title, 100, top, 800, top + 40))
        boxes.append(_box(subtitle, 100, top + 45, 900, top + 75))
        boxes.append(_box('GO', 980, top + 20, 1040, top + 60))
    return _snapshot(*boxes)


def _denied_frame(kind: str) -> Snapshot:
    title, subtitle = _DENIED[kind]
    return _snapshot(
        _box('Coins boost', 100, 100, 500, 150),
        _box(title, 100, 260, 800, 300),
        _box(subtitle, 100, 305, 900, 335),
        _box('GO', 980, 280, 1040, 320),
    )


def _roster(*keys: str) -> Roster:
    return replace(
        enumerate_roster(_frame(*keys), CATALOGUE, route=DiscoveryRoute.BOOST),
        surface_variant=Surface.BOOST_PAGE,
        routes_taken=(SurfaceRoute.SHORTCUT,),
    )


def _denied_roster(kind: str) -> Roster:
    return replace(
        enumerate_roster(_denied_frame(kind), CATALOGUE, route=DiscoveryRoute.BOOST),
        surface_variant=Surface.BOOST_PAGE,
        routes_taken=(SurfaceRoute.SHORTCUT,),
    )


def _empty_roster() -> Roster:
    return Roster(
        entries=(),
        route=DiscoveryRoute.BOOST,
        catalogue_version=CATALOGUE.version,
        surface_variant=Surface.BOOST_PAGE,
        routes_taken=(SurfaceRoute.SHORTCUT,),
    )


def _observation(
    sequence: int,
    roster: Roster | None,
    *,
    error: str | None = None,
) -> RosterObservation:
    return RosterObservation(
        sequence=sequence,
        timestamp=CAPTURED_AT,
        rows=(),
        surface_variant=Surface.BOOST_PAGE,
        error=error,
        roster=roster,
    )


class _ScriptedObserver:
    """Replay prepared observations and advance the roster the loop sees."""

    def __init__(
        self,
        initial: Roster,
        observations: list[RosterObservation | None],
    ) -> None:
        self._roster = initial
        self._observations = observations
        self.sequence = 0

    @property
    def roster(self) -> Roster | None:
        return self._roster

    def observe(self, *_args: object, **_kwargs: object) -> RosterObservation | None:
        observation = self._observations[min(self.sequence, len(self._observations) - 1)]
        self.sequence += 1
        if observation is not None and observation.roster is not None:
            self._roster = observation.roster
        return observation


def _complete(entry: RosterEntry) -> EngineResult:
    return EngineResult('completed', 'progress_badge', 'the row moved', True, {'key': entry.key})


def _seed_other_families(context: RunContext, *, seen: set[str]) -> None:
    """Record daily limits for catalogue families a fixture deliberately omits.

    A clean completion now also requires that no unseen family is still
    claimable. These tests are about rotation agreement, not about the rest of
    the catalogue, so mark the omitted families as already earned today.
    """
    for spec in CATALOGUE.allowed:
        if spec.key in seen:
            continue
        for _ in range(spec.max_per_run):
            context.state.record(spec.key)


def test_merge_rosters_keeps_a_row_that_rotated_out() -> None:
    both = _roster('coins_quiz', 'super_discounts')
    later = _roster('coins_quiz')

    union = merge_rosters(both, later)

    assert {entry.key for entry in union.entries} == {'coins_quiz', 'super_discounts'}
    assert union.surface_variant is later.surface_variant
    assert merge_rosters(None, later) is later
    assert merge_rosters(_empty_roster(), later) is later


def test_merge_rosters_does_not_duplicate_an_identity_already_seen() -> None:
    quiz = next(entry for entry in _roster('coins_quiz').entries if entry.key == 'coins_quiz')
    duplicate = replace(
        _roster('coins_quiz'),
        entries=(quiz, replace(quiz, subtitle='a second copy')),
    )

    union = merge_rosters(duplicate, _empty_roster())

    assert [entry.subtitle for entry in union.entries] == ['a second copy']


def _card_failure_roster(*titles: str) -> Roster:
    """A sample that read bare titles and no card for them."""
    return replace(
        _empty_roster(),
        card_structure_failures=len(titles),
        card_structure_titles=titles,
    )


@pytest.mark.parametrize('full_first', [True, False])
def test_merge_rosters_drops_a_card_failure_another_sample_read_as_a_row(full_first: bool) -> None:
    title = 'Turn on notifications'
    full = _roster('notifications')
    bare = _card_failure_roster(title)

    union = merge_rosters(full, bare) if full_first else merge_rosters(bare, full)

    assert [entry.title for entry in union.entries] == [title]
    assert union.card_structure_failures == 0
    assert union.card_structure_titles == ()
    assert union.incomplete is False
    assert union.warning is None


def test_merge_rosters_keeps_a_card_failure_no_sample_read_as_a_row() -> None:
    title = 'Take the coins quiz'

    union = merge_rosters(_card_failure_roster(title), _card_failure_roster(title))

    assert union.entries == ()
    assert union.card_structure_failures == 1
    assert union.card_structure_titles == (title,)
    assert union.incomplete is True
    assert union.warning == f"'{title}' had no readable card structure and were not counted"


def test_merge_rosters_keeps_every_distinct_bare_title() -> None:
    quiz = 'Take the coins quiz'
    notifications = 'Turn on notifications'

    union = merge_rosters(_card_failure_roster(quiz), _card_failure_roster(notifications))

    assert union.card_structure_failures == 2
    assert union.card_structure_titles == (quiz, notifications)
    assert union.incomplete is True


def test_a_merged_roster_warning_never_names_a_title_it_lists_as_a_row() -> None:
    read = 'Turn on notifications'
    unread = 'Take the coins quiz'
    union = merge_rosters(_roster('notifications'), _card_failure_roster(read, unread))

    assert [entry.title for entry in union.entries] == [read]
    assert union.card_structure_failures == 1
    assert union.card_structure_titles == (unread,)
    assert union.warning is not None
    assert repr(read) not in union.warning
    assert repr(unread) in union.warning


def _idle_rotation_observations() -> tuple[Roster, list[RosterObservation | None]]:
    """Initial A, then B, then B twice: one disagreement earns a second pass."""
    first = _denied_roster('game')
    second = _denied_roster('social')
    return first, [
        _observation(1, first),
        _observation(2, second),
        _observation(3, second),
        _observation(4, second),
    ]


@pytest.mark.slow
def test_a_disagreeing_read_spends_a_pass_then_agreement_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial, observations = _idle_rotation_observations()
    scripted = _ScriptedObserver(initial, observations)
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: initial)
    monkeypatch.setattr(engine, '_observe', lambda *args, **kwargs: scripted.observe())
    context = _context(
        tmp_path,
        capture=_Capture(_denied_frame('game'), _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    context.observer = cast(Any, scripted)
    _seed_other_families(context, seen=set())

    report = engine.run_once(context)

    assert report.status is RunStatus.COMPLETED
    assert report.passes == 2
    rotation = cast(dict[str, Any], cast(dict[str, Any], report.roster)['rotation'])
    assert rotation['confirmed'] is True
    assert rotation['samples'] == 4
    assert rotation['agreeing_samples'] == 2


@pytest.mark.slow
def test_a_sheet_that_keeps_disagreeing_stops_at_the_pass_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial, observations = _idle_rotation_observations()
    scripted = _ScriptedObserver(initial, observations)
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: initial)
    monkeypatch.setattr(engine, '_observe', lambda *args, **kwargs: scripted.observe())
    context = _context(
        tmp_path,
        capture=_Capture(_denied_frame('game'), _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    context.observer = cast(Any, scripted)
    context.config = replace(context.config, max_passes=1)
    _seed_other_families(context, seen=set())

    report = engine.run_once(context)

    assert report.status is RunStatus.BUDGET
    assert context.state.snapshot().completed_night is False
    rotation = cast(dict[str, Any], cast(dict[str, Any], report.roster)['rotation'])
    assert rotation['confirmed'] is False
    assert 'kept rotating without two agreeing reads' in str(report.detail)


@pytest.mark.slow
def test_a_pass_plans_the_union_of_two_rotations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _roster('coins_quiz', 'super_discounts')
    observations: list[RosterObservation | None] = [
        _observation(1, _roster('coins_quiz')),
        _observation(2, _empty_roster()),
        _observation(3, _empty_roster()),
        _observation(4, _empty_roster()),
    ]
    scripted = _ScriptedObserver(initial, observations)
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: initial)
    monkeypatch.setattr(engine, '_observe', lambda *args, **kwargs: scripted.observe())
    context = _context(
        tmp_path,
        capture=_Capture(_frame('coins_quiz', 'super_discounts'), _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    context.observer = cast(Any, scripted)
    _seed_other_families(context, seen={'coins_quiz', 'super_discounts'})
    dispatched: list[str] = []

    def dispatch(_context: RunContext, _controller: Controller, entry: RosterEntry) -> EngineResult:
        dispatched.append(entry.key or '')
        _context.state.record(entry.key or '')
        return _complete(entry)

    monkeypatch.setattr(engine, '_dispatch_entry', dispatch)

    report = engine.run_once(context)

    # The quiz is cheapest; the super-discount row rotated out before its turn
    # but was still in the union, so it is dispatched instead of vanishing.
    assert dispatched == ['coins_quiz', 'super_discounts']
    assert report.status is RunStatus.COMPLETED
    assert context.state.count('super_discounts') == 1
    rotation = cast(dict[str, Any], cast(dict[str, Any], report.roster)['rotation'])
    assert rotation['confirmed'] is True
    assert rotation['samples'] >= 2
    assert rotation['agreeing_samples'] >= 2


@pytest.mark.slow
def test_a_single_rotation_sample_cannot_complete_the_night(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _empty_roster()
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: initial)
    monkeypatch.setattr(engine, '_observe', lambda *args, **kwargs: None)
    context = _context(
        tmp_path,
        capture=_Capture(_frame(), _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    context.config = replace(context.config, max_passes=1)

    report = engine.run_once(context)

    assert report.status is RunStatus.BUDGET
    assert context.state.snapshot().completed_night is False
    rotation = cast(dict[str, Any], cast(dict[str, Any], report.roster)['rotation'])
    assert rotation['samples'] == 1
    assert rotation['confirmed'] is False
    assert 'one rotation sample' in str(report.detail) or 'not been read' in str(report.detail)


@pytest.mark.slow
def test_a_lost_confirmation_read_does_not_complete_the_night(
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
    context.config = replace(context.config, max_passes=1)

    report = engine.run_once(context)

    assert report.status is RunStatus.BUDGET
    assert context.state.snapshot().completed_night is False
    rotation = cast(dict[str, Any], cast(dict[str, Any], report.roster)['rotation'])
    assert rotation['confirmed'] is False
    assert 'kept rotating' in str(report.detail) or 'not been read' in str(report.detail)


@pytest.mark.slow
def test_two_agreeing_fresh_reads_still_complete_the_night(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster = _roster('coins_quiz')
    frame = _frame('coins_quiz')
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    context.state.record('coins_quiz')
    _seed_other_families(context, seen={'coins_quiz'})

    report = engine.run_once(context)

    assert report.status is RunStatus.COMPLETED
    assert report.ok is True
    assert context.state.snapshot().completed_night is True
    rotation = cast(dict[str, Any], cast(dict[str, Any], report.roster)['rotation'])
    assert rotation['confirmed'] is True
    assert rotation['samples'] >= 2
    assert rotation['agreeing_samples'] >= 2


@pytest.mark.slow
def test_a_stale_bare_title_sample_does_not_reopen_a_reconciled_night(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later bare sample cannot undo a card another sample already read."""
    full = _roster('notifications')
    bare = _card_failure_roster('Turn on notifications')
    observations: list[RosterObservation | None] = [
        _observation(1, full),
        _observation(2, bare),
        _observation(3, bare),
        _observation(4, bare),
    ]
    scripted = _ScriptedObserver(full, observations)
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: full)
    monkeypatch.setattr(engine, '_observe', lambda *args, **kwargs: scripted.observe())
    context = _context(
        tmp_path,
        capture=_Capture(_frame('notifications'), _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    context.observer = cast(Any, scripted)
    _seed_other_families(context, seen=set())

    report = engine.run_once(context)

    assert report.status is RunStatus.COMPLETED
    assert report.roster is not None
    assert report.roster['incomplete'] is False
    assert report.roster['card_structure_failures'] == 0
    assert report.roster['card_structure_titles'] == []
    assert report.night is not None
    assert context.state.snapshot().completed_night is True


@pytest.mark.slow
def test_a_restart_roster_keeps_card_keys_in_step_with_the_union_titles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    widget = replace(_card_failure_roster('Add & start using our widget'), card_structure_keys=('coins_widget',))
    quiz = replace(_card_failure_roster('Take the coins quiz'), card_structure_keys=('coins_quiz',))
    union = merge_rosters(widget, quiz)
    observations: list[RosterObservation | None] = [
        _observation(1, quiz),
        _observation(2, quiz),
        _observation(3, quiz),
    ]
    scripted = _ScriptedObserver(quiz, observations)
    captured: list[Roster] = []
    original = engine._pass_restart_reason

    def capture(roster: Roster | None, observation: RosterObservation | None) -> str | None:
        if roster is not None:
            captured.append(roster)
        return original(roster, observation)

    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: union)
    monkeypatch.setattr(engine, '_observe', lambda *args, **kwargs: scripted.observe())
    monkeypatch.setattr(engine, '_pass_restart_reason', capture)
    context = _context(
        tmp_path,
        capture=_Capture(_frame('notifications'), _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    context.observer = cast(Any, scripted)
    # Keep both families claimable: a claimed family's card failure is now
    # settled by the day state (#171), so seeding them would remove the case
    # this union test is about (two failure keys kept in step with their titles).
    _seed_other_families(context, seen={'coins_widget', 'coins_quiz'})

    engine.run_once(context)

    first = captured[0]
    assert first.card_structure_titles == ('Add & start using our widget', 'Take the coins quiz')
    assert first.card_structure_keys == ('coins_widget', 'coins_quiz')


@pytest.mark.slow
def test_a_late_titleless_sample_still_restarts_the_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient titleless frame keeps #137's recovery read."""
    readable = _roster('notifications')
    titleless = replace(_empty_roster(), unreadable_count=1)
    observations: list[RosterObservation | None] = [
        _observation(1, readable),
        _observation(2, titleless),
        _observation(3, titleless),
    ]
    scripted = _ScriptedObserver(readable, observations)
    monkeypatch.setattr(engine, 'discover_roster', lambda *args, **kwargs: readable)
    monkeypatch.setattr(engine, '_observe', lambda *args, **kwargs: scripted.observe())
    context = _context(
        tmp_path,
        capture=_Capture(_frame('notifications'), _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    context.observer = cast(Any, scripted)
    _seed_other_families(context, seen=set())

    report = engine.run_once(context)

    assert report.status is RunStatus.BUDGET
    assert report.passes == 2
    assert report.detail is not None
    assert 'had no readable title' in report.detail


@pytest.mark.slow
def test_an_unseen_claimable_family_keeps_the_night_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster = _roster('coins_quiz')
    frame = _frame('coins_quiz')
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    context.state.record('coins_quiz')

    report = engine.run_once(context)

    rotation = cast(dict[str, Any], cast(dict[str, Any], report.roster)['rotation'])
    unread = cast(list[str], rotation['unread_families'])
    assert 'super_discounts' in unread
    assert report.night is not None
    assert report.night['outcome'] == 'incomplete_roster'
    assert context.state.snapshot().completed_night is False
    summary = report.summary_path.read_text(encoding='utf-8')
    line = next(line for line in summary.splitlines() if line.startswith('- **Rotation:**'))
    assert 'still claimable today' in line
    assert 'super_discounts' in line


@pytest.mark.slow
def test_an_unseen_family_already_claimed_today_is_not_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roster = _roster('coins_quiz')
    frame = _frame('coins_quiz')
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    context.state.record('coins_quiz')
    # super_discounts was claimed earlier today, so its absence is expected.
    for _ in range(CATALOGUE.allowed_by_key('super_discounts').max_per_run):
        context.state.record('super_discounts')
    _seed_other_families(context, seen={'coins_quiz', 'super_discounts'})

    report = engine.run_once(context)

    rotation = cast(dict[str, Any], cast(dict[str, Any], report.roster)['rotation'])
    assert cast(list[str], rotation['unread_families']) == []
    assert 'super_discounts' in cast(list[str], rotation['already_claimed_families'])
    assert context.state.snapshot().completed_night is True
    line = next(
        line
        for line in report.summary_path.read_text(encoding='utf-8').splitlines()
        if line.startswith('- **Rotation:**')
    )
    assert 'already claimed today, so absent as expected' in line
    assert 'super_discounts' in line


@pytest.mark.slow
def test_learn_to_save_stays_claimable_and_unseen_keeps_the_night_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#159: absence must not be turned into a completed night by catalogue edits."""
    roster = _roster('coins_quiz')
    frame = _frame('coins_quiz')
    _surface(monkeypatch, roster, frame)
    context = _context(
        tmp_path,
        capture=_Capture(frame, _coin_frame()),
        catalogue=CATALOGUE,
        corpus=CORPUS,
    )
    context.state.record('coins_quiz')
    for spec in CATALOGUE.allowed:
        if spec.key in {'coins_quiz', 'learn_to_save'}:
            continue
        for _ in range(spec.max_per_run):
            context.state.record(spec.key)

    report = engine.run_once(context)

    assert 'learn_to_save' in CATALOGUE.allowed_keys
    rotation = cast(dict[str, Any], cast(dict[str, Any], report.roster)['rotation'])
    assert 'learn_to_save' in cast(list[str], rotation['unread_families'])
    assert report.night is not None
    assert report.night['outcome'] == 'incomplete_roster'
    assert context.state.snapshot().completed_night is False
    line = next(
        line
        for line in report.summary_path.read_text(encoding='utf-8').splitlines()
        if line.startswith('- **Rotation:**')
    )
    assert 'learn_to_save' in line
    assert 'still claimable today' in line


def test_the_rotation_summary_states_the_samples_and_unread_families() -> None:
    assert report_module._rotation_summary(None) == 'not sampled'
    assert report_module._rotation_summary({'rotation': {'samples': 0}}) == 'not sampled'
    assert (
        report_module._rotation_summary(
            {
                'rotation': {
                    'samples': 2,
                    'agreeing_samples': 2,
                    'confirmed': True,
                    'unread_families': [],
                },
            },
        )
        == 'confirmed after 2 agreeing sample(s) of 2'
    )
    text = report_module._rotation_summary(
        {
            'rotation': {
                'samples': 3,
                'agreeing_samples': 1,
                'confirmed': False,
                'unread_families': ['super_discounts'],
            },
        },
    )
    assert text.startswith('not confirmed after 3 sample(s)')
    assert 'super_discounts' in text


def test_roster_incomplete_without_rotation_handles_a_missing_payload() -> None:
    assert engine._roster_incomplete_without_rotation(None) is False
    assert engine._roster_incomplete_without_rotation({'rotation': {'confirmed': False}}) is False
    assert engine._roster_incomplete_without_rotation({'surface_variant': 'rows_absent'}) is True
