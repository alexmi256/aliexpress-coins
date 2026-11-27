"""Sanitized evidence for issue #158's launcher-shortcut validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aliexpress_coins.tasks.surface import SHORTCUT_LAUNCH_DEEP_LINK

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'tests' / 'data' / 'flows' / 'shortcut_visit_issue_158.json'
ROUND2 = ROOT / 'tests' / 'data' / 'flows' / 'shortcut_visit_issue_158_round2.json'


def test_issue_158_records_the_exact_launcher_intent_and_the_plus_one() -> None:
    payload: Any = json.loads(EVIDENCE.read_text(encoding='utf-8'))

    assert payload['issue'] == 158
    intent = payload['launcher_intent']
    assert intent['deep_link'] == SHORTCUT_LAUNCH_DEEP_LINK
    assert intent['credited'] is True
    assert intent['balance_after'] - intent['balance_before'] == 1
    assert intent['row_gone_in_later_boost_read'] is True


def test_issue_158_records_both_explicit_dialog_controls_and_the_cleanup() -> None:
    payload: Any = json.loads(EVIDENCE.read_text(encoding='utf-8'))

    sheet = payload['shortcut_sheet']['labels']
    launcher = payload['launcher_add']['labels']
    assert 'Create shortcut' in sheet
    assert 'Add to home screen' in launcher
    assert 'Got it' in sheet
    assert 'AliExpress' in launcher
    assert payload['home_shortcut']['credited_on_home_tap'] is False
    cleanup = payload['cleanup']
    assert 'cmd shortcut clear-shortcuts com.alibaba.aliexpresshd' in cleanup['command']
    assert cleanup['scope'].startswith('package-wide')
    assert cleanup['verified_no_pinned_shortcut'] is True
    assert cleanup['verified_no_launcher_item'] is True


def test_issue_158_round2_preserves_the_failed_step_and_the_live_credit() -> None:
    payload: Any = json.loads(ROUND2.read_text(encoding='utf-8'))

    failed = payload['failed_run']
    assert failed['run_id'] == '20261127T140626Z'
    assert failed['observed_steps']['create_sheet_opened'] is False
    assert failed['observed_steps']['pinned_intent_fired'] is False
    assert failed['observed_steps']['balance_before'] is None
    assert failed['observed_steps']['balance_after'] is None
    assert failed['frame_count'] == 22

    live = payload['live_run']
    task = live['shortcut_task']
    assert live['run_id'] == '20261127T145922Z'
    assert task['verification'] == 'balance_delta'
    assert task['balance_after'] - task['balance_before'] == 1
    assert task['shortcut_created'] is True
    assert task['shortcut_removed'] is True
    assert [tap['label'] for tap in task['taps']] == [
        'Visit the page via coins',
        'Create shortcut',
        'Add to home screen',
        'Got it',
    ]
    assert live['row_gone']['removed'] == ['Visit the page via coins']
    assert live['cleanup']['pinned_after'] is False
    assert 'AdcActivity' in live['foreground_after']['resumed_activity']
