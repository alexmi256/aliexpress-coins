"""Issue #155: a card failure must not survive a sample that read the row.

A rotation sample can catch a card before its companion copy renders. If another
sample reads the same card completely, the title is a row and the partial render
is stale evidence. If no sample ever reads the card, the title stays a failure.
These tests use hand-built frames and no device.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PIL import Image

from aliexpress_coins.perception.snapshot import Snapshot
from aliexpress_coins.runner import engine
from aliexpress_coins.runner import report as report_module
from aliexpress_coins.tasks.catalogue import TaskCatalogue, load_task_catalogue
from aliexpress_coins.tasks.discovery import (
    DiscoveryRoute,
    Roster,
    RosterObservation,
    _merge_frame_rosters,
    collect_roster,
    enumerate_roster,
    merge_rosters,
)
from tests.test_runner_engine import _box, _Notifier, _snapshot
from tests.test_tasks_discovery import _Capture, _controller, _RecordingDevice

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE: TaskCatalogue = load_task_catalogue(ROOT / 'specs' / 'tasks.yaml')
CAPTURED_AT = datetime(2026, 11, 26, 15, 27, tzinfo=UTC)
WIDGET = 'Add & start using our widget'
NOTIFICATIONS = 'Turn on notifications'


def _full_widget_frame() -> Snapshot:
    return _snapshot(
        _box('Coins boost', 100, 100, 500, 150),
        _box(WIDGET, 247, 1082, 805, 1135),
        _box('Add our widget to your', 246, 1138, 670, 1190),
        _box('homescreen to use', 249, 1195, 600, 1235),
        _box('+1', 327, 1283, 372, 1322),
    )


def _bare_frame(title: str) -> Snapshot:
    return _snapshot(
        _box('Coins boost', 100, 100, 500, 150),
        _box(title, 247, 1082, 805, 1135),
    )


def _roster(frame: Snapshot) -> Roster:
    return enumerate_roster(frame, CATALOGUE, route=DiscoveryRoute.BOOST)


def _observation(roster: Roster) -> RosterObservation:
    return RosterObservation(
        sequence=1,
        timestamp=CAPTURED_AT,
        rows=(),
        roster=roster,
    )


def test_a_title_one_sample_read_as_a_row_is_not_a_card_failure_in_either_order() -> None:
    full = _roster(_full_widget_frame())
    bare = _roster(_bare_frame(WIDGET))

    assert [entry.title for entry in full.entries] == [WIDGET]
    assert full.card_structure_failures == 0
    assert bare.entries == ()
    assert bare.card_structure_titles == (WIDGET,)

    for merged in (merge_rosters(full, bare), merge_rosters(bare, full)):
        assert merged.titles == (WIDGET,)
        assert merged.card_structure_failures == 0
        assert merged.card_structure_titles == ()
        assert merged.warning is None
        assert merged.incomplete is False
        assert merged.as_dict()['card_structure_titles'] == []


def test_a_title_read_only_as_a_bare_title_in_every_sample_stays_incomplete() -> None:
    bare = _roster(_bare_frame(WIDGET))

    merged = merge_rosters(bare, bare)

    assert merged.entries == ()
    assert merged.card_structure_failures == 1
    assert merged.card_structure_titles == (WIDGET,)
    assert merged.warning == f"'{WIDGET}' had no readable card structure and were not counted"
    assert merged.incomplete is True
    assert engine._pass_restart_reason(merged, _observation(merged)) == (
        'surface_incomplete: the roster read was incomplete because 1 task title(s) had no readable card structure'
    )


def test_a_card_failure_for_a_different_title_is_not_removed() -> None:
    full = _roster(_full_widget_frame())
    other = _roster(_bare_frame(NOTIFICATIONS))

    merged = merge_rosters(full, other)

    assert merged.titles == (WIDGET,)
    assert merged.card_structure_titles == (NOTIFICATIONS,)
    assert merged.card_structure_failures == 1
    assert merged.incomplete is True


@pytest.mark.parametrize('full_first', [True, False])
def test_a_different_label_for_the_same_catalogue_key_is_not_a_failure(full_first: bool) -> None:
    other_label = 'Add our widget to your homescreen'
    full = _roster(_full_widget_frame())
    bare = _roster(_bare_frame(other_label))

    assert full.entries[0].key == 'coins_widget'
    assert bare.card_structure_titles == (other_label,)
    assert bare.card_structure_keys == ('coins_widget',)

    merged = merge_rosters(full, bare) if full_first else merge_rosters(bare, full)

    assert merged.titles == (WIDGET,)
    assert merged.card_structure_failures == 0
    assert merged.card_structure_titles == ()
    assert merged.incomplete is False


@pytest.mark.parametrize('full_first', [True, False])
def test_a_different_label_for_the_same_denied_rule_is_not_a_failure(full_first: bool) -> None:
    row_frame = _snapshot(
        _box('Coins boost', 100, 100, 500, 150),
        _box('Play & Earn', 100, 600, 700, 640),
        _box('GO', 980, 620, 1040, 660),
    )
    row = _roster(row_frame)
    bare = _roster(_bare_frame('Play and Earn'))

    assert row.entries[0].denied is True
    assert row.entries[0].rule == 'game_play_and_earn'
    assert bare.card_structure_titles == ('Play and Earn',)
    assert bare.card_structure_keys == ('game_play_and_earn',)

    merged = merge_rosters(row, bare) if full_first else merge_rosters(bare, row)

    assert merged.titles == ('Play & Earn',)
    assert merged.card_structure_failures == 0
    assert merged.card_structure_titles == ()
    assert merged.incomplete is False


def test_a_directly_built_roster_cannot_report_a_row_as_an_unreadable_card() -> None:
    full = _roster(_full_widget_frame())
    entry = full.entries[0]

    roster = Roster(entries=(entry,), card_structure_failures=2, card_structure_titles=(WIDGET,))

    assert roster.card_structure_titles == ()
    # The one failure without a title cannot be reconciled, so it is preserved.
    assert roster.card_structure_failures == 1
    assert roster.incomplete is True
    assert roster.warning == '1 task title(s) had no readable card structure and were not counted'


def test_a_directly_built_roster_counts_a_repeated_key_once() -> None:
    roster = Roster(
        entries=(),
        card_structure_failures=2,
        card_structure_titles=(WIDGET, WIDGET),
        card_structure_keys=('coins_widget', 'coins_widget'),
    )

    assert roster.card_structure_failures == 1
    assert roster.card_structure_titles == (WIDGET,)
    assert roster.card_structure_keys == ('coins_widget',)


def test_collect_roster_drops_a_failure_when_a_scrolled_frame_reads_the_card() -> None:
    first = _snapshot(
        _box('Coins boost', 100, 100, 500, 150),
        _box(NOTIFICATIONS, 247, 1082, 805, 1135),
        _box('Take the coins quiz', 100, 600, 700, 640),
        _box('Answer one question', 100, 645, 700, 675),
        _box('GO', 980, 620, 1040, 660),
    )
    second = _snapshot(
        _box('Coins boost', 100, 100, 500, 150),
        _box(NOTIFICATIONS, 249, 1467, 657, 1513),
        _box('Stay informed when rewards drop', 248, 1522, 773, 1571),
        _box('GO', 980, 1500, 1040, 1540),
        _box('Take the coins quiz', 100, 600, 700, 640),
        _box('Answer one question', 100, 645, 700, 675),
        _box('GO', 980, 620, 1040, 660),
    )
    first = replace(first, image=Image.new('RGB', (1200, 2000), 'white'))
    second = replace(second, image=Image.new('RGB', (1200, 2000), '#e0e0e0'))

    roster = collect_roster(
        _controller(_RecordingDevice(), first),
        _Capture((second, second, second, second)),
        CATALOGUE,
        first,
    )

    assert set(roster.titles) == {'Take the coins quiz', NOTIFICATIONS}
    assert roster.card_structure_failures == 0
    assert roster.card_structure_titles == ()
    assert roster.warning is None


def test_merge_frame_rosters_reconciles_a_failure_against_another_frame_row() -> None:
    full = _roster(_full_widget_frame())
    bare = _roster(_bare_frame(WIDGET))

    merged = _merge_frame_rosters((bare, full))

    assert merged.titles == (WIDGET,)
    assert merged.card_structure_failures == 0
    assert merged.card_structure_titles == ()


def test_an_untitled_failure_does_not_inflate_when_merged_repeatedly() -> None:
    sample = Roster(entries=(), card_structure_failures=1)

    once = merge_rosters(sample, sample)
    twice = merge_rosters(once, sample)

    assert once.card_structure_failures == 1
    assert twice.card_structure_failures == 1


def test_two_labels_for_one_failure_key_count_as_one_diagnostic() -> None:
    first = _roster(_bare_frame(WIDGET))
    second = _roster(_bare_frame('Add our widget to your homescreen'))

    merged = merge_rosters(first, second)

    assert merged.card_structure_failures == 1
    assert merged.card_structure_titles == (WIDGET,)
    assert merged.card_structure_keys == ('coins_widget',)


def test_a_keyless_failure_is_upgraded_by_the_same_title_with_a_key() -> None:
    keyless = Roster(entries=(), card_structure_failures=1, card_structure_titles=(WIDGET,))
    keyed = _roster(_bare_frame(WIDGET))

    for merged in (merge_rosters(keyless, keyed), merge_rosters(keyed, keyless)):
        assert merged.card_structure_failures == 1
        assert merged.card_structure_titles == (WIDGET,)
        assert merged.card_structure_keys == ('coins_widget',)


def test_a_merged_summary_counts_only_the_rows_it_records() -> None:
    """The roster line is derived from the merged entries, not one sample."""
    full = _roster(_full_widget_frame())
    bare = _roster(_bare_frame(NOTIFICATIONS))

    merged = merge_rosters(full, bare)
    payload = merged.as_dict()

    entries = cast(list[dict[str, object]], payload['entries'])
    assert payload['count'] == len(entries) == 1
    assert payload['allowed_count'] == sum(1 for entry in entries if entry['allowed']) == 1
    assert payload['denied_count'] == sum(1 for entry in entries if not entry['allowed']) == 0
    assert payload['claimed_count'] == sum(1 for entry in entries if entry['claimed']) == 1
    assert payload['dispatchable_count'] == sum(1 for entry in entries if entry['dispatchable']) == 0
    assert payload['card_structure_titles'] == [NOTIFICATIONS]

    roster_line = report_module._roster_summary(payload)
    assert '1 row(s) read, 1 allowed, 0 refused, 0 dispatchable, 1 already claimed' in roster_line
    assert WIDGET not in roster_line
    assert NOTIFICATIONS in roster_line
    assert 'no readable card structure' in roster_line


def test_the_report_rejects_counts_that_contradict_its_entries() -> None:
    merged = merge_rosters(_roster(_full_widget_frame()), _roster(_bare_frame(WIDGET)))
    payload = merged.as_dict()
    payload['count'] = 2

    with pytest.raises(AssertionError, match='count=2'):
        report_module._roster_summary(payload)


def test_the_report_rejects_claimed_and_dispatchable_counts_that_contradict_entries() -> None:
    merged = merge_rosters(_roster(_full_widget_frame()), _roster(_bare_frame(WIDGET)))
    payload = merged.as_dict()
    payload['claimed_count'] = 0
    with pytest.raises(AssertionError, match='claimed_count=0'):
        report_module._roster_summary(payload)

    payload = merged.as_dict()
    payload['dispatchable_count'] = 1
    with pytest.raises(AssertionError, match='dispatchable_count=1'):
        report_module._roster_summary(payload)


def test_the_report_rejects_more_named_failures_than_its_count() -> None:
    merged = merge_rosters(_roster(_full_widget_frame()), _roster(_bare_frame(WIDGET)))
    payload = merged.as_dict()
    payload['card_structure_failures'] = 0
    payload['card_structure_titles'] = [NOTIFICATIONS]

    with pytest.raises(AssertionError, match='0 card failure'):
        report_module._roster_summary(payload)


def test_the_report_rejects_a_warning_that_names_a_recorded_row() -> None:
    merged = merge_rosters(_roster(_full_widget_frame()), _roster(_bare_frame(WIDGET)))
    payload = merged.as_dict()
    payload['card_structure_failures'] = 1
    payload['card_structure_titles'] = [WIDGET]
    payload['warning'] = f"'{WIDGET}' had no readable card structure and were not counted"

    with pytest.raises(AssertionError, match='also listed as rows'):
        report_module._roster_summary(payload)


def test_a_denied_row_named_like_an_unread_family_key_is_not_a_contradiction(tmp_path: Path) -> None:
    frame = _snapshot(
        _box('Coins boost', 100, 100, 500, 150),
        _box('notifications', 100, 600, 700, 640),
        _box('GO', 980, 620, 1040, 660),
    )
    roster = replace(_roster(frame), unread_families=('notifications',))

    assert roster.entries[0].denied is True
    assert roster.warning == "catalogue families still claimable today were not seen in any read: 'notifications'"

    summary = report_module._roster_summary(roster.as_dict())
    assert "warning: catalogue families still claimable today were not seen in any read: 'notifications'" in summary

    notifier = _Notifier()
    context = SimpleNamespace(
        notifier=notifier,
        config=SimpleNamespace(notify_on_empty=True, apprise_url=None, notify_balance_delta=False),
    )
    report = report_module.RunReport.for_run(tmp_path, budget_seconds=1.0, clock=lambda: CAPTURED_AT)
    report.roster = roster.as_dict()
    report.finish(report_module.RunStatus.COMPLETED, budget_used_seconds=0.0, finished_at=CAPTURED_AT)

    engine._notify(cast(Any, context), report, logging.getLogger('test.issue155'))

    assert len(notifier.messages) == 1
    assert report.notification == {'attempted': True, 'ok': True, 'sink': 'webhook', 'error': None}
    assert report.json_path.exists()
    assert "catalogue families still claimable today were not seen in any read: 'notifications'" in (
        report.summary_path.read_text(encoding='utf-8')
    )


def test_a_contradictory_roster_is_rejected_before_run_json_is_written(tmp_path: Path) -> None:
    merged = merge_rosters(_roster(_full_widget_frame()), _roster(_bare_frame(WIDGET)))
    payload = merged.as_dict()
    payload['count'] = 2
    report = report_module.RunReport.for_run(tmp_path, budget_seconds=1.0, clock=lambda: CAPTURED_AT)
    report.roster = payload
    report.finish(report_module.RunStatus.COMPLETED, budget_used_seconds=0.0, finished_at=CAPTURED_AT)

    with pytest.raises(AssertionError, match='count=2'):
        report.write()

    assert not report.json_path.exists()
    assert not report.summary_path.exists()


def test_a_roster_cannot_name_more_failures_than_it_counts() -> None:
    with pytest.raises(AssertionError, match='do not cover'):
        Roster(entries=(), card_structure_failures=0, card_structure_titles=(WIDGET,))
