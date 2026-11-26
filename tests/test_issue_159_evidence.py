"""Issue #159: frame-backed absence of the learn-to-save family.

The evidence is a sample log plus two reviewed full frames. The family was not
offered in any read, so the contract claims stay unknown and the catalogue row
is retained; this test keeps both facts reviewable without inventing a contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast

from PIL import Image

from aliexpress_coins.tasks.catalogue import TaskCategory, load_task_catalogue

ROOT = Path(__file__).resolve().parents[1]
FLOWS = ROOT / 'tests' / 'data' / 'flows'
EVIDENCE = FLOWS / 'learn_to_save_issue_159_observation.json'
CATALOGUE_PATH = ROOT / 'specs' / 'tasks.yaml'

EXPECTED_ROWS = [
    'Place an order on this page',
    'Add 5 items to your cart',
    'Water to win a US $0.10 prize',
    'Share coins with friends',
    'Explore sponsored items',
    'Browse surprise items',
    'Browse recently viewed items',
    'Add a coins shortcut',
    'Add & start using our widget',
    'Turn on notifications',
]
_SHA256 = re.compile(r'^[0-9a-f]{64}$')


def _payload() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(EVIDENCE.read_text(encoding='utf-8')))


def test_issue_159_sample_log_records_every_read_and_every_distinct_row() -> None:
    payload = _payload()
    result = cast(dict[str, Any], payload['result'])
    samples = cast(list[dict[str, Any]], cast(dict[str, Any], payload['sampling'])['samples'])

    assert payload['issue'] == 159
    assert result['seen'] is False
    assert result['first_seen_at_utc'] is None
    assert result['sample_count'] == len(samples) == 15
    assert result['span_seconds'] >= 600
    assert len({sample['batch'] for sample in samples}) >= 2
    for sample in samples:
        assert sample['reloaded'] is True
        assert sample['sheet_views'] >= 4
        assert sample['rows'] == EXPECTED_ROWS
        assert sample['coin_rows'] == [
            'Place an order on this page',
            'Add 5 items to your cart',
            'Water to win a US $0.10 prize',
        ]
        assert 'Learn to save money with Coins Credits' not in sample['rows']
        assert 'Learn to save money with Coins Credits' not in sample['coin_rows']
        assert sample['coin_frame_at_utc']
        assert sample['learn_to_save_seen'] is False


def test_issue_159_frames_are_committed_with_masks_and_hashes() -> None:
    payload = _payload()

    frames = cast(list[dict[str, Any]], payload['frames'])
    assert len(frames) == 2
    for frame in frames:
        fixture = FLOWS / cast(str, frame['fixture'])
        assert fixture.is_file()
        data = fixture.read_bytes()
        assert hashlib.sha256(data).hexdigest() == frame['sha256']
        assert _SHA256.fullmatch(cast(str, frame['source_sha256']))
        assert frame['masked']
        for left, top, right, bottom in cast(list[list[int]], frame['mask_rects']):
            assert 0 <= left < right <= 1200
            assert 0 <= top < bottom <= 2000
        with Image.open(fixture) as image:
            assert image.size == (1200, 2000)
        assert len(data) < 550 * 1024


def test_issue_159_keeps_the_family_claimable_and_the_contract_unknown() -> None:
    payload = _payload()
    claims = cast(dict[str, str], payload['claims'])

    for claim in ('destination', 'progress', 'badge_or_counter', 'return_route', 'timing_or_dwell', 'reward'):
        assert claims[claim] == 'unknown'
    assert claims['row_offer'].startswith('not observed')
    assert 'retained' in claims['catalogue_policy']

    catalogue = load_task_catalogue(CATALOGUE_PATH)
    spec = catalogue.allowed_by_key('learn_to_save')
    assert spec.category is TaskCategory.BROWSE_WAIT
    assert 'not offered' in (spec.notes or '')

    live_run = cast(dict[str, Any], payload['live_run'])
    assert re.fullmatch(r'\d{8}T\d{6}Z', cast(str, live_run['run_id']))
    assert live_run['status'] == 'budget'
    assert live_run['balance_before'] == live_run['balance_after'] == 130.0
    assert live_run['balance_delta'] == 0.0
    assert live_run['night'] is None
    rotation = cast(dict[str, Any], live_run['rotation'])
    assert rotation['confirmed'] is False
    assert 'learn_to_save' in cast(list[str], rotation['unread_families'])
