"""Sanitized evidence for issue #158's launcher-shortcut validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aliexpress_coins.tasks.surface import SHORTCUT_LAUNCH_DEEP_LINK

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'tests' / 'data' / 'flows' / 'shortcut_visit_issue_158.json'


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
