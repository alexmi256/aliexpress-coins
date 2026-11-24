"""Sanitized evidence for issue #130's shortcut-row observation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'tests' / 'data' / 'flows' / 'shortcut_visit_issue_130.json'


def test_issue_130_preserves_the_shortcut_row_and_its_share_neighbour() -> None:
    payload: Any = json.loads(EVIDENCE.read_text(encoding='utf-8'))

    assert payload['issue'] == 130
    row = payload['row']
    assert row['title'] == 'Visit the page via coins shortcut'
    assert row['subtitle'] == 'Unlock a daily surprise'
    assert row['reward'] == '+1'
    assert row['badge'] is None
    assert row['action'] == 'GO'
    share = payload['share_row']
    assert share['policy_rule'] == 'social'
    assert share['policy_reason'].startswith('social:')
    assert 'sharing' in share['policy_reason']


def test_issue_130_does_not_confuse_the_creation_sheet_with_a_credit() -> None:
    payload: Any = json.loads(EVIDENCE.read_text(encoding='utf-8'))
    after = payload['after_tap']
    conclusions = payload['conclusions']

    assert 'Create shortcut' in after['labels']
    assert after['balance'] == payload['pre_tap']['balance'] == 87
    assert after['row_present'] is True
    assert conclusions['go_opens_shortcut_creation_sheet'] is True
    assert conclusions['go_credits_immediately'] is False
    assert conclusions['got_it_credits'] is False
    assert conclusions['creating_shortcut_alone_credits'] is False
    assert conclusions['shortcut_uri_immediately_after_creation_credits'] is False
    assert conclusions['home_shortcut_launch_preceded_the_credit'] is True
    assert conclusions['balance_after'] - conclusions['balance_before'] == 1
    assert conclusions['credit_timing_bracketed'] is False
