"""Cross-night family evidence and the dormant-family exit (#161).

The tests advance a date rather than sleeping and use temporary state
directories. They prove that absence is counted per observed night, that a
corrupt evidence file cannot fail a run, and that a sighting clears dormancy.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from aliexpress_coins.runner import report as report_module
from aliexpress_coins.runner import state as state_module
from aliexpress_coins.runner.errors import StateError, StateReadError, StateWriteError
from aliexpress_coins.runner.state import (
    FAMILY_OBSERVATION_FILENAME,
    FAMILY_OBSERVATION_SCHEMA,
    FamilyObservationStore,
    NightOutcome,
    NightRecord,
    _CorruptState,
    _parse_dormant_families,
    _parse_family_observations,
)
from aliexpress_coins.tasks.families import FamilyEvidence

DAY1 = date(2026, 11, 20)
DAY2 = date(2026, 11, 21)
DAY3 = date(2026, 11, 22)


def _clock(value: date = DAY3) -> Callable[[], datetime]:
    return lambda: datetime(value.year, value.month, value.day, 3, 30)


def _store(tmp_path: Path) -> FamilyObservationStore:
    return FamilyObservationStore(tmp_path, clock=_clock())


def _observe(
    store: FamilyObservationStore,
    day: date,
    *,
    seen: tuple[str, ...] = (),
    samples: int = 1,
    keys: tuple[str, ...] = ('learn_to_save',),
    probed: tuple[str, ...] = (),
) -> FamilyEvidence:
    return store.observe(
        day=day,
        keys=keys,
        seen=seen,
        samples=samples,
        dormancy_days=2,
        retry_days=7,
        probed=probed,
    )[0]


def test_a_missing_file_peeks_at_a_neutral_evidence_record(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.load() == {}
    evidence = store.peek(('learn_to_save',))[0]
    assert evidence == FamilyEvidence(key='learn_to_save')
    assert evidence.dormant is False


def test_absence_is_counted_once_per_night_and_becomes_dormant_at_the_threshold(tmp_path: Path) -> None:
    store = _store(tmp_path)

    first = _observe(store, DAY1, samples=3)
    second = _observe(store, DAY2, samples=2)

    assert first.missed_days == 1
    assert first.dormant is False
    assert first.unseen_since == DAY1
    assert second.missed_days == 2
    assert second.samples == 5
    assert second.dormant is True
    assert second.dormant_since == DAY2
    assert second.next_probe == date(2026, 11, 28)
    assert _store(tmp_path).load()['learn_to_save'] == second


def test_a_second_run_on_the_same_night_does_not_advance_the_streak(tmp_path: Path) -> None:
    store = _store(tmp_path)

    _observe(store, DAY1, samples=2)
    again = _observe(store, DAY1, samples=3)

    assert again.missed_days == 1
    assert again.samples == 3
    assert again.dormant is False


def test_a_sighting_clears_the_absence_and_dormancy(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _observe(store, DAY1)
    dormant = _observe(store, DAY2)
    assert dormant.dormant is True

    seen = _observe(store, DAY3, seen=('learn_to_save',), samples=1)

    assert seen == FamilyEvidence(key='learn_to_save', last_seen=DAY3, last_checked=DAY3)
    assert seen.dormant is False
    assert _store(tmp_path).load()['learn_to_save'] == seen


def test_a_dormant_family_is_due_for_an_extra_probe_on_the_slower_cadence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _observe(store, DAY1)
    dormant = _observe(store, DAY2)
    assert dormant.next_probe == date(2026, 11, 28)

    assert store.due_for_probe(('learn_to_save',), date(2026, 11, 27)) == ()
    assert store.due_for_probe(('learn_to_save',), date(2026, 11, 28)) == ('learn_to_save',)

    retried = _observe(store, date(2026, 11, 28), probed=('learn_to_save',))

    assert retried.dormant is True
    assert retried.retried_on == date(2026, 11, 28)
    assert retried.probe_due is True
    assert retried.next_probe == date(2026, 12, 5)
    assert _observe(store, date(2026, 11, 29)).retried_on is None


def test_a_sighting_during_the_probe_clears_dormancy(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _observe(store, DAY1)
    _observe(store, DAY2)

    seen = _observe(store, date(2026, 11, 28), seen=('learn_to_save',))

    assert seen.dormant is False
    assert seen.retried_on is None
    assert seen.next_probe is None


def test_untouched_families_keep_their_previous_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _observe(store, DAY1, keys=('learn_to_save', 'recently_viewed'))

    _observe(store, DAY2, keys=('learn_to_save',))

    loaded = store.load()
    assert loaded['recently_viewed'].missed_days == 1
    assert loaded['recently_viewed'].last_checked == DAY1


def test_invalid_thresholds_and_samples_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for field in ('dormancy_days', 'retry_days', 'samples'):
        with pytest.raises(ValueError, match=field):
            store.observe(
                day=DAY1,
                keys=('learn_to_save',),
                seen=(),
                samples=-1 if field == 'samples' else 1,
                dormancy_days=0 if field == 'dormancy_days' else 2,
                retry_days=0 if field == 'retry_days' else 7,
            )


def test_a_corrupt_evidence_file_is_quarantined_and_starts_empty(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / FAMILY_OBSERVATION_FILENAME
    path.write_text('{not json', encoding='utf-8')

    store = _store(tmp_path)

    assert store.load() == {}
    quarantined = list(tmp_path.glob(f'{FAMILY_OBSERVATION_FILENAME}.corrupt-*'))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding='utf-8') == '{not json'
    assert 'state_corrupt' in caplog.text


def test_the_evidence_file_is_plain_json_with_its_schema(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _observe(store, DAY1, samples=4)

    payload = json.loads(store.path.read_text(encoding='utf-8'))

    assert payload['schema'] == FAMILY_OBSERVATION_SCHEMA
    assert payload['families']['learn_to_save']['missed_days'] == 1
    assert payload['families']['learn_to_save']['samples'] == 4
    assert payload['families']['learn_to_save']['dormant'] is False


@pytest.mark.parametrize(
    ('mutate', 'message'),
    [
        (lambda payload: payload.__setitem__('schema', 99), 'unsupported schema'),
        (lambda payload: payload.__setitem__('families', []), "'families' must be a JSON object"),
        (
            lambda payload: payload['families'].__setitem__('learn_to_save', {'key': 'other'}),
            'records a different key',
        ),
        (
            lambda payload: payload['families']['learn_to_save'].__setitem__('missed_days', -1),
            'non-negative integer',
        ),
        (
            lambda payload: payload['families']['learn_to_save'].__setitem__('unseen_since', 'yesterday'),
            'not an ISO date',
        ),
    ],
)
def test_invalid_evidence_payloads_are_quarantined(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    mutate: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    payload: dict[str, Any] = {
        'schema': FAMILY_OBSERVATION_SCHEMA,
        'families': {
            'learn_to_save': {
                'key': 'learn_to_save',
                'missed_days': 1,
                'samples': 2,
            },
        },
    }
    mutate(payload)
    path = tmp_path / FAMILY_OBSERVATION_FILENAME
    path.write_text(json.dumps(payload), encoding='utf-8')

    assert _store(tmp_path).load() == {}

    assert message in caplog.text
    assert list(tmp_path.glob(f'{FAMILY_OBSERVATION_FILENAME}.corrupt-*'))


def test_the_night_record_serialises_dormant_families_only_when_present() -> None:
    quiet = NightRecord(NightOutcome.EXHAUSTED, dispatched=1, completed=1)
    assert 'dormant_families' not in quiet.as_dict()

    family = FamilyEvidence(
        key='learn_to_save',
        unseen_since=DAY1,
        missed_days=3,
        samples=15,
        dormant_since=DAY2,
        next_probe=date(2026, 11, 28),
        last_checked=DAY3,
    )
    night = NightRecord(NightOutcome.EXHAUSTED, dispatched=1, completed=1, dormant_families=(family,))

    payload = night.as_dict()
    assert payload['dormant_families'] == [family.as_dict()]
    assert payload['dormant_families'][0]['samples'] == 15


def test_family_store_names_creation_read_and_write_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocker = tmp_path / 'not-a-directory'
    blocker.write_text('x', encoding='utf-8')
    with pytest.raises(StateError, match='could not create state directory'):
        FamilyObservationStore(blocker)

    store = FamilyObservationStore(tmp_path / 'read-failure', clock=_clock())
    store.path.mkdir(parents=True)
    with pytest.raises(StateReadError, match='could not read'):
        store.load()

    write_store = FamilyObservationStore(tmp_path / 'write-failure', clock=_clock())

    def broken_write(*_args: object, **_kwargs: object) -> None:
        raise OSError('the evidence volume is full')

    monkeypatch.setattr(state_module, 'atomic_write_bytes', broken_write)
    with pytest.raises(StateWriteError, match='could not write'):
        write_store.observe(
            day=DAY1,
            keys=('learn_to_save',),
            seen=(),
            samples=1,
            dormancy_days=2,
            retry_days=7,
        )


def test_malformed_dormant_family_payloads_are_rejected() -> None:
    assert _parse_dormant_families(None) == ()
    dormant = FamilyEvidence(
        key='learn_to_save',
        unseen_since=DAY1,
        missed_days=3,
        samples=15,
        dormant_since=DAY2,
        next_probe=date(2026, 11, 28),
        last_checked=DAY3,
    )
    assert _parse_dormant_families([dormant.as_dict()]) == (dormant,)

    for raw in ('not a list', [1], [FamilyEvidence(key='learn_to_save').as_dict()]):
        with pytest.raises(_CorruptState):
            _parse_dormant_families(raw)


def test_malformed_family_observation_payloads_are_rejected() -> None:
    payloads: list[object] = [
        [],
        {'schema': FAMILY_OBSERVATION_SCHEMA, 'families': {'': {'key': ''}}},
        {'schema': FAMILY_OBSERVATION_SCHEMA, 'families': {'learn_to_save': 'not an object'}},
        {'schema': FAMILY_OBSERVATION_SCHEMA, 'families': {'learn_to_save': {'missed_days': 0}}},
        {
            'schema': FAMILY_OBSERVATION_SCHEMA,
            'families': {'learn_to_save': {'key': 'learn_to_save', 'last_checked': 3}},
        },
    ]

    for payload in payloads:
        with pytest.raises(_CorruptState):
            _parse_family_observations(json.dumps(payload).encode('utf-8'))


def test_report_dormant_summaries_handle_bad_or_partial_payloads() -> None:
    assert report_module._night_dormant_summary(None) == ''
    assert report_module._night_dormant_summary(['not a family']) == ''

    assert report_module._not_offered_summary(None) == 'not offered on this account'
    assert report_module._not_offered_summary(['not a family']) == 'not offered on this account'
    plain = report_module._not_offered_summary(
        [{'key': 'learn_to_save', 'missed_days': 14, 'samples': 63, 'dormant_since': DAY1.isoformat()}],
    )
    assert 'learn_to_save not offered for 14 day(s)' in plain
    assert '0 sightings in 63 sample(s)' in plain

    probed = report_module._not_offered_summary(
        [
            {
                'key': 'learn_to_save',
                'missed_days': 14,
                'samples': 63,
                'dormant_since': DAY1.isoformat(),
                'retried_on': DAY2.isoformat(),
                'next_probe': DAY3.isoformat(),
            },
        ],
    )
    assert f'extra probe on {DAY2.isoformat()}' in probed
    assert f'next extra probe {DAY3.isoformat()}' in probed
