"""Issue #171: a settled family must not look like an unreadable card.

The claimed ``Browse recently viewed items`` card carries a two-line subtitle
whose last words are ``in-cart items``. Read as a title, that lower line matches
the generic ``cart`` deny rule; read as the card's subtitle it is ordinary copy.
The first test replays the recorded frame, so the reader is measured against the
real layout. The remaining tests keep the day-state rule honest in both
directions: a claimed/refused/excluded/blacklisted family cannot keep the night
open, but an unsettled title that nothing could parse still does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from aliexpress_coins.perception.ocr import RapidOcrEngine
from aliexpress_coins.perception.snapshot import Snapshot
from aliexpress_coins.runner import engine
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks.catalogue import TaskCatalogue, load_task_catalogue
from aliexpress_coins.tasks.discovery import (
    DiscoveryRoute,
    Roster,
    RosterObservation,
    _is_other_family_title,
    enumerate_roster,
)
from tests.test_tasks_discovery import CAPTURED_AT, _box, _load, _snapshot

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE_PATH = ROOT / 'specs' / 'tasks.yaml'
FRAME = ROOT / 'tests' / 'data' / 'flows' / 'recently_viewed_issue_171_claimed.png'
REVIEWED = 'Browse recently viewed items'
SUBTITLE = 'Coins deal on viewed, in-cart'
OTHER_TITLE = 'Explore sponsored items'


@pytest.fixture(scope='module')
def engine_ocr() -> RapidOcrEngine:
    return RapidOcrEngine()


@pytest.fixture(scope='module')
def catalogue() -> TaskCatalogue:
    return load_task_catalogue(CATALOGUE_PATH)


def _observation(roster: Roster) -> RosterObservation:
    return RosterObservation(sequence=1, timestamp=CAPTURED_AT, rows=(), roster=roster)


def _bare_title_frame(frame: Snapshot, title: str) -> Snapshot:
    """The recorded frame with its card copy removed, leaving only the title."""
    title_box = next(box for box in frame.boxes if title in box.text)
    return Snapshot(
        image=frame.image,
        boxes=(title_box,),
        captured_at=frame.captured_at,
        geometry=frame.geometry,
    )


def test_the_recorded_claimed_card_parses_into_a_row(
    engine_ocr: RapidOcrEngine,
    catalogue: TaskCatalogue,
) -> None:
    frame = _load(FRAME, engine_ocr)
    frame_text = ' '.join(box.text for box in frame.boxes)
    assert REVIEWED in frame_text
    assert SUBTITLE in frame_text
    assert 'items' in frame_text

    roster = enumerate_roster(frame, catalogue, route=DiscoveryRoute.BOOST)

    entry = next(candidate for candidate in roster.entries if candidate.key == 'recently_viewed')
    assert entry.title == REVIEWED
    assert entry.claimed is True
    assert entry.action is None
    assert entry.dispatch_target is None
    assert roster.card_structure_failures == 0
    assert roster.card_structure_titles == ()
    assert roster.incomplete is False
    assert roster.warning is None


def test_an_unparseable_claimed_card_does_not_keep_the_night_open(
    engine_ocr: RapidOcrEngine,
    catalogue: TaskCatalogue,
    tmp_path: Path,
) -> None:
    frame = _load(FRAME, engine_ocr)
    bare = _bare_title_frame(frame, REVIEWED)
    unsettled = enumerate_roster(bare, catalogue, route=DiscoveryRoute.BOOST)

    assert unsettled.card_structure_titles == (REVIEWED,)
    assert unsettled.card_structure_keys == ('recently_viewed',)
    assert unsettled.incomplete is True

    state = DailyStateStore(tmp_path, clock=lambda: CAPTURED_AT)
    state.record('recently_viewed')

    reconciled = engine._with_settled_families(unsettled, state, catalogue)

    assert 'recently_viewed' in reconciled.settled_families
    assert reconciled.card_structure_failures == 0
    assert reconciled.card_structure_titles == ()
    assert reconciled.incomplete is False
    assert reconciled.warning is None
    assert engine._pass_restart_reason(reconciled, _observation(reconciled)) is None


def test_a_genuinely_unreadable_unsettled_row_still_keeps_the_night_open(
    engine_ocr: RapidOcrEngine,
    catalogue: TaskCatalogue,
    tmp_path: Path,
) -> None:
    frame = _load(FRAME, engine_ocr)
    bare = _bare_title_frame(frame, OTHER_TITLE)
    roster = enumerate_roster(bare, catalogue, route=DiscoveryRoute.BOOST)

    state = DailyStateStore(tmp_path, clock=lambda: CAPTURED_AT)
    reconciled = engine._with_settled_families(roster, state, catalogue)

    assert reconciled.card_structure_failures == 1
    assert reconciled.card_structure_titles == (OTHER_TITLE,)
    assert reconciled.incomplete is True
    assert reconciled.warning == f"'{OTHER_TITLE}' had no readable card structure and were not counted"
    assert engine._pass_restart_reason(reconciled, _observation(reconciled)) == (
        'surface_incomplete: the roster read was incomplete because 1 task title(s) had no readable card structure'
    )


def test_a_refused_family_does_not_keep_the_night_open(
    catalogue: TaskCatalogue,
    tmp_path: Path,
) -> None:
    bare = _snapshot(_box('Place an order on this page', 247, 1082, 805, 1135))
    roster = enumerate_roster(bare, catalogue, route=DiscoveryRoute.BOOST)
    assert roster.card_structure_keys == ('spending_order',)
    assert roster.incomplete is True

    state = DailyStateStore(tmp_path, clock=lambda: CAPTURED_AT)
    reconciled = engine._with_settled_families(roster, state, catalogue)

    assert reconciled.card_structure_failures == 0
    assert reconciled.card_structure_titles == ()
    assert reconciled.incomplete is False
    assert reconciled.warning is None


def test_an_excluded_family_does_not_keep_the_night_open(
    catalogue: TaskCatalogue,
    tmp_path: Path,
) -> None:
    bare = _snapshot(_box('Add & start using our widget', 247, 1082, 805, 1135))
    roster = enumerate_roster(bare, catalogue, route=DiscoveryRoute.BOOST)
    assert roster.card_structure_keys == ('coins_widget',)
    assert roster.incomplete is True

    state = DailyStateStore(tmp_path, clock=lambda: CAPTURED_AT)
    reconciled = engine._with_settled_families(roster, state, catalogue)

    assert 'coins_widget' in reconciled.settled_families
    assert reconciled.card_structure_failures == 0
    assert reconciled.card_structure_titles == ()
    assert reconciled.incomplete is False
    assert reconciled.warning is None


def test_a_blacklisted_family_does_not_keep_the_night_open(
    catalogue: TaskCatalogue,
    tmp_path: Path,
) -> None:
    bare = _snapshot(_box('Take the coins quiz', 247, 1082, 805, 1135))
    roster = enumerate_roster(bare, catalogue, route=DiscoveryRoute.BOOST)
    assert roster.card_structure_keys == ('coins_quiz',)
    assert roster.incomplete is True

    state = DailyStateStore(tmp_path, clock=lambda: CAPTURED_AT)
    state.blacklist_task('coins_quiz', 2)
    reconciled = engine._with_settled_families(roster, state, catalogue)

    assert 'coins_quiz' in reconciled.settled_families
    assert reconciled.card_structure_failures == 0
    assert reconciled.card_structure_titles == ()
    assert reconciled.incomplete is False
    assert reconciled.warning is None


def test_an_allowed_title_below_a_card_is_another_family(catalogue: TaskCatalogue) -> None:
    """A catalogue title is never consumed as the card's subtitle."""
    decision = catalogue.decide('Take the coins quiz')

    assert _is_other_family_title(OTHER_TITLE, decision=decision, catalogue=catalogue) is True


def test_a_state_read_failure_leaves_the_unknown_card_unsettled(
    catalogue: TaskCatalogue,
    tmp_path: Path,
) -> None:
    class _BrokenSnapshotState:
        def count(self, _key: str) -> int:
            return 0

        def snapshot(self) -> object:
            raise RuntimeError('state unreadable')

    bare = _snapshot(_box(OTHER_TITLE, 247, 1082, 805, 1135))
    roster = enumerate_roster(bare, catalogue, route=DiscoveryRoute.BOOST)
    broken = cast(Any, _BrokenSnapshotState())

    settled = engine._settled_family_keys(broken, catalogue)

    assert 'spending_order' in settled
    assert 'sponsored_items' not in settled
    reconciled = engine._with_settled_families(roster, broken, catalogue)
    assert reconciled.card_structure_titles == (OTHER_TITLE,)
    assert reconciled.incomplete is True

    state = DailyStateStore(tmp_path, clock=lambda: CAPTURED_AT)
    stable = engine._with_settled_families(roster, state, catalogue)
    assert engine._with_settled_families(stable, state, catalogue) is stable
