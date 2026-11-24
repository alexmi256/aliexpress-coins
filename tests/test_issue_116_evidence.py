"""Sanitized evidence bounds for issue #116's missing post-tap frame."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'tests' / 'data' / 'flows' / 'recently_viewed_issue_116.json'


def test_issue_116_preserves_what_the_run_proves_and_what_it_does_not() -> None:
    payload: Any = json.loads(EVIDENCE.read_text(encoding='utf-8'))

    assert payload['run_id'] == '20261124T083006Z'
    assert payload['post_tap_frame_retained'] is False
    assert payload['retained_shots'] == ['balance-before.png', 'balance-after.png']
    assert payload['post_tap_labels'] == ['3:36', '0', '日', '24']
    chronology = payload['chronology']
    assert chronology['row_added_utc'] < chronology['roster_reflow_utc'] < chronology['tap_utc']
    assert chronology['sponsored_rounds_completed_after_row_addition'] == 6
    assert chronology['sponsored_rounds_completed_before_reflow'] == 6
    assert payload['row']['badge'] is None
    assert payload['row']['destination'] is None
    assert payload['row']['completion_contract'] is None
    assert payload['return']['verified'] is False
    assert payload['return']['history'][-1] == 'page'
    assert payload['conclusions']['foreground_loss_proven_by_retained_frame'] is False
    assert payload['conclusions']['cause'].startswith('unknown:')


def test_the_requested_point_matches_the_fresh_inline_go_not_the_stale_roster_boxes() -> None:
    payload: Any = json.loads(EVIDENCE.read_text(encoding='utf-8'))
    point = payload['tap']['point']
    center = payload['returned_inline_row']['action_center']
    left, top, right, bottom = payload['returned_inline_row']['action_box']

    assert point == [center[0], center[1] + payload['tap']['jitter'][1]]
    assert left <= point[0] <= right
    assert top <= point[1] <= bottom
    assert payload['conclusions']['requested_point_matches_returned_inline_action'] is True
    assert payload['conclusions']['actual_tap_inside_returned_inline_action'] is True
    assert payload['conclusions']['roster_boxes_were_stale_after_reflow'] is True
    assert [entry['target'] for entry in payload['roster_targets']] == [[1007, 1829.5], [1007, 1497.5]]
    assert all(entry['target'] != center for entry in payload['roster_targets'])
