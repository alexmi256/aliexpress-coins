"""Sanitized evidence for issue #137's completed-night-empty-roster run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'tests' / 'data' / 'flows' / 'roster_issue_137.json'


def _payload() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(EVIDENCE.read_text(encoding='utf-8')))


def test_issue_137_recorded_a_completed_night_from_one_non_claimable_row() -> None:
    payload = _payload()
    events = payload['log_events']
    night = next(event for event in events if event['event'] == 'night_decided')
    observation = next(event for event in events if event['event'] == 'roster_observation')

    assert payload['run_id'] == '20261125T143832Z'
    assert payload['balance_before'] == payload['balance_after'] == 88
    assert night['complete'] is True
    assert night['outcome'] == 'exhausted'
    assert night['dispatched'] == 0
    assert observation['removed'] == [
        'Daily check-in',
        'Explore sponsored items',
        'Browse surprise items',
        'Place an order on this page',
        'Water to win a US $0.10 prize',
        'Visit the page via coins',
    ]
    assert payload['captured_roster']['rows'][0]['label'] == 'Share coins with friends'
    assert payload['captured_roster']['rows'][0]['badge'] == '0/3'
    assert payload['summary']['roster'].endswith('read across 0 revealing scroll(s)')


def test_issue_137_names_the_second_read_and_the_titleless_row_as_the_fault() -> None:
    payload = _payload()
    conclusions = payload['conclusions']

    assert conclusions['boost_sheet_opens_by_hand'] is True
    assert conclusions['initial_read_saw_the_claimable_rows'] is True
    assert conclusions['second_read_started_at_the_bottom'] is True
    assert conclusions['merge_does_not_discard_earlier_frames'] is True
    assert conclusions['root_cause'].startswith('the post-discover observation re-read')
    assert payload['issue_reported_row']['label'] is None
    assert payload['issue_reported_row']['badge'] == '0/3'
