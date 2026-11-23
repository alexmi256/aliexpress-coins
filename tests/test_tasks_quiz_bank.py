"""The quiz question bank: normalisation, lookup, recording and round trips (#39)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aliexpress_coins.tasks.quiz_bank import (
    BANK_SCHEMA,
    DEFAULT_QUIZ_BANK_PATH,
    BankEntry,
    QuestionSource,
    QuizBank,
    QuizBankError,
    match_option,
    normalise_question,
    normalize_question,
    question_key,
)

STAMP = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
QUESTION = 'Q : Which of the following statements is correct?'
OPTIONS = ('True', 'False')


def _bank(path: Path) -> QuizBank:
    return QuizBank(path, clock=lambda: STAMP)


def _record(bank: QuizBank, *, correct: str = 'True', source: QuestionSource = QuestionSource.RESULT_PAGE) -> BankEntry:
    return bank.record(QUESTION, OPTIONS, 'False', correct, source=source)


def test_normalisation_collapses_case_spacing_and_punctuation() -> None:
    assert normalize_question('  Which   OF the following? ') == normalize_question('Which of the following')
    assert '?' not in normalize_question('Is this true?')
    assert '  ' not in normalize_question('Is   this   true?')
    assert normalise_question is normalize_question


def test_normalisation_rejects_a_non_string() -> None:
    with pytest.raises(QuizBankError, match='question must be a string'):
        normalize_question(7)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ('left', 'right'),
    [
        ('Q : Is this true?', 'q : is this true'),
        ('Q : Is  this  true?', 'Q : Is this true.'),
        ('Q : This is l1?', 'Q : This is 11?'),
        ('Q : Choose O0 now.', 'Q : Choose 00 now'),
    ],
)
def test_near_duplicate_questions_share_one_key(left: str, right: str) -> None:
    assert question_key(left) == question_key(right)


@pytest.mark.parametrize(
    ('left', 'right'),
    [
        ('Q : Is this true?', 'Q : Is this false?'),
        ('Q : Choose the first option.', 'Q : Choose the second option.'),
        ('Q : What is 10 + 1?', 'Q : What is 100 + 1?'),
    ],
)
def test_genuinely_different_questions_keep_different_keys(left: str, right: str) -> None:
    assert question_key(left) != question_key(right)


def test_key_is_a_stable_sha256_of_the_normalised_text() -> None:
    key = question_key(QUESTION)

    assert len(key) == 64
    assert key == question_key('Q : Which of the following statements is correct?')
    assert key != question_key('Q : Which of the following statements are correct?')


def test_match_option_uses_text_and_the_same_normalisation() -> None:
    assert match_option('True', ['False', 'True']) == 1
    assert match_option('TRUE.', ['true', 'false']) == 0
    assert match_option('A11 of the above', ['All of the above', 'None']) == 0
    assert match_option('Maybe', ['True', 'False']) is None


def test_a_new_question_is_written_with_every_recorded_field(tmp_path: Path) -> None:
    bank = _bank(tmp_path / 'quiz_bank.json')

    entry = _record(bank)

    assert entry.question == QUESTION
    assert entry.normalized == normalize_question(QUESTION)
    assert entry.key == question_key(QUESTION)
    assert entry.options == OPTIONS
    assert entry.chosen == 'False'
    assert entry.correct == 'True'
    assert entry.source is QuestionSource.RESULT_PAGE
    assert entry.seen_at == STAMP

    payload = json.loads((tmp_path / 'quiz_bank.json').read_text())
    assert payload == {
        'schema': BANK_SCHEMA,
        'questions': [
            {
                'key': question_key(QUESTION),
                'question': QUESTION,
                'normalized': normalize_question(QUESTION),
                'options': ['True', 'False'],
                'chosen': 'False',
                'correct': 'True',
                'source': 'result_page',
                'seen_at': STAMP.isoformat(),
            },
        ],
    }


def test_lookup_resolves_the_recorded_answer_by_text(tmp_path: Path) -> None:
    bank = _bank(tmp_path / 'quiz_bank.json')
    _record(bank, correct='False')

    assert bank.lookup(QUESTION) is not None
    assert bank.resolve(QUESTION, ['false.', 'true']) == 0
    assert bank.resolve('Q : A different question?', ['True', 'False']) is None


def test_a_repeat_is_a_no_op_and_the_file_stays_byte_stable(tmp_path: Path) -> None:
    path = tmp_path / 'quiz_bank.json'
    bank = _bank(path)
    _record(bank)
    first = path.read_bytes()

    again = _record(bank)

    assert path.read_bytes() == first
    assert again.seen_at == STAMP


@pytest.mark.parametrize(
    'variant',
    [
        'q : which of the following statements is correct',
        'Q : Which  of the following statements is correct.',
        'Q : Which of the following statements is correct?',
    ],
)
def test_near_duplicate_observations_do_not_create_a_second_entry(tmp_path: Path, variant: str) -> None:
    path = tmp_path / 'quiz_bank.json'
    bank = _bank(path)
    _record(bank)
    first = path.read_bytes()

    bank.record(variant, OPTIONS, 'False', 'True')

    payload = json.loads(path.read_text())
    assert len(payload['questions']) == 1
    assert path.read_bytes() == first
    assert bank.entries[0].question == QUESTION


def test_different_questions_create_separate_sorted_entries(tmp_path: Path) -> None:
    path = tmp_path / 'quiz_bank.json'
    bank = _bank(path)

    bank.record('Q : Second question?', ('Yes', 'No'), 'No', 'Yes')
    bank.record('Q : First question?', ('True', 'False'), 'True', 'False')

    payload = json.loads(path.read_text())
    keys = [entry['key'] for entry in payload['questions']]
    assert keys == sorted(keys)
    assert len(keys) == 2


def test_a_reordered_option_set_is_still_the_same_question(tmp_path: Path) -> None:
    path = tmp_path / 'quiz_bank.json'
    bank = _bank(path)
    _record(bank, correct='True')
    first = path.read_bytes()

    again = bank.record(QUESTION, ('False', 'True'), 'True', 'True')

    assert again.options == OPTIONS
    assert path.read_bytes() == first


def test_a_changed_option_set_is_not_answered_by_position(tmp_path: Path) -> None:
    bank = _bank(tmp_path / 'quiz_bank.json')
    _record(bank, correct='True')

    assert bank.resolve(QUESTION, ['Yes', 'No']) is None

    updated = bank.record(QUESTION, ('Yes', 'No'), 'Yes', 'Yes')

    assert updated.correct == 'Yes'
    assert updated.options == ('Yes', 'No')
    assert bank.resolve(QUESTION, ['No', 'Yes']) == 1


def test_a_manual_entry_survives_an_equivalent_result_page(tmp_path: Path) -> None:
    bank = _bank(tmp_path / 'quiz_bank.json')
    manual = _record(bank, correct='False', source=QuestionSource.MANUAL)

    same = _record(bank, correct='True', source=QuestionSource.RESULT_PAGE)

    assert same.source is QuestionSource.MANUAL
    assert same.correct == 'False'
    assert bank.resolve(QUESTION, ['True', 'False']) == 1
    assert manual.seen_at == STAMP


def test_a_manual_entry_is_replaced_when_its_answer_is_gone(tmp_path: Path) -> None:
    bank = _bank(tmp_path / 'quiz_bank.json')
    _record(bank, correct='False', source=QuestionSource.MANUAL)

    updated = bank.record(QUESTION, ('Yes', 'No'), 'No', 'Yes')

    assert updated.source is QuestionSource.RESULT_PAGE
    assert updated.correct == 'Yes'


def test_a_manual_source_may_be_rewritten_manually(tmp_path: Path) -> None:
    bank = _bank(tmp_path / 'quiz_bank.json')
    _record(bank, correct='False', source=QuestionSource.RESULT_PAGE)

    updated = _record(bank, correct='True', source=QuestionSource.MANUAL)

    assert updated.source is QuestionSource.MANUAL
    assert updated.correct == 'True'


def test_round_trip_write_reload_and_resolve(tmp_path: Path) -> None:
    path = tmp_path / 'quiz_bank.json'
    _record(_bank(path), correct='False')

    reloaded = _bank(path)

    assert reloaded.resolve(QUESTION, ['True', 'False']) == 1
    entry = reloaded.lookup(QUESTION)
    assert entry is not None
    assert entry.source is QuestionSource.RESULT_PAGE


def test_a_human_edited_entry_may_omit_optional_fields(tmp_path: Path) -> None:
    path = tmp_path / 'quiz_bank.json'
    path.write_text(
        json.dumps(
            {
                'schema': 1,
                'questions': [
                    {
                        'question': QUESTION,
                        'options': ['True', 'False'],
                        'correct': 'False',
                    },
                ],
            },
        ),
    )

    entry = _bank(path).lookup(QUESTION)

    assert entry is not None
    assert entry.key == question_key(QUESTION)
    assert entry.chosen == 'False'
    assert entry.source is QuestionSource.MANUAL
    assert entry.seen_at is None


def test_a_missing_file_is_an_empty_bank(tmp_path: Path) -> None:
    bank = _bank(tmp_path / 'missing.json')

    assert bank.entries == ()
    assert bank.lookup(QUESTION) is None
    assert not (tmp_path / 'missing.json').exists()


def test_corrupt_json_is_quarantined_and_the_bank_starts_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / 'quiz_bank.json'
    path.write_text('{ not json')

    bank = _bank(path)

    assert bank.entries == ()
    quarantined = list(tmp_path.glob('quiz_bank.json.corrupt-*'))
    assert len(quarantined) == 1
    assert 'quiz_bank_corrupt' in caplog.text


@pytest.mark.parametrize(
    'payload',
    [
        [],
        {'schema': 2, 'questions': []},
        {'schema': True, 'questions': []},
        {'schema': 1, 'questions': {}},
        {'schema': 1, 'questions': ['not an object']},
        {'schema': 1, 'questions': [{'question': '', 'options': ['a'], 'correct': 'a'}]},
        {'schema': 1, 'questions': [{'question': 'Q?', 'options': [], 'correct': 'a'}]},
        {'schema': 1, 'questions': [{'question': 'Q?', 'options': ['a'], 'correct': ''}]},
        {'schema': 1, 'questions': [{'question': 'Q?', 'options': ['a'], 'correct': 'a', 'source': 'guess'}]},
        {'schema': 1, 'questions': [{'question': 'Q?', 'options': ['a'], 'correct': 'a', 'source': 5}]},
        {'schema': 1, 'questions': [{'question': 5, 'options': ['a'], 'correct': 'a'}]},
        {'schema': 1, 'questions': [{'question': 'Q?', 'options': 'a', 'correct': 'a'}]},
        {'schema': 1, 'questions': [{'question': 'Q?', 'options': ['a'], 'correct': 'a', 'seen_at': 'yesterday'}]},
        {'schema': 1, 'questions': [{'question': 'Q?', 'options': ['a'], 'correct': 'a', 'seen_at': 4}]},
    ],
)
def test_invalid_bank_payloads_are_quarantined(tmp_path: Path, payload: object) -> None:
    path = tmp_path / 'quiz_bank.json'
    path.write_text(json.dumps(payload))

    bank = _bank(path)

    assert bank.entries == ()
    assert list(tmp_path.glob('quiz_bank.json.corrupt-*'))


def test_a_naive_timestamp_is_read_as_utc(tmp_path: Path) -> None:
    path = tmp_path / 'quiz_bank.json'
    path.write_text(
        json.dumps(
            {
                'schema': 1,
                'questions': [
                    {
                        'question': QUESTION,
                        'options': list(OPTIONS),
                        'correct': 'True',
                        'seen_at': '2026-11-22T12:00:00',
                    },
                ],
            },
        ),
    )

    entry = _bank(path).lookup(QUESTION)

    assert entry is not None
    assert entry.seen_at == STAMP


def test_a_second_corrupt_file_does_not_overwrite_the_first(tmp_path: Path) -> None:
    path = tmp_path / 'quiz_bank.json'
    first = path.with_name('quiz_bank.json.corrupt-20261122T120000000000Z')
    first.write_text('earlier')
    path.write_text('{ not json')

    assert _bank(path).entries == ()
    assert first.read_text() == 'earlier'
    assert list(tmp_path.glob('quiz_bank.json.corrupt-*.1'))


def test_a_failed_quarantine_still_leaves_an_empty_bank(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / 'quiz_bank.json'
    path.write_text('{ not json')

    def refuse(_source: object, _target: object) -> None:
        raise OSError('read-only directory')

    monkeypatch.setattr('aliexpress_coins.tasks.quiz_bank.os.replace', refuse)

    assert _bank(path).entries == ()
    assert path.exists()
    assert 'quarantine_error' in caplog.text


def test_a_read_failure_is_a_bank_error(tmp_path: Path) -> None:
    bank = _bank(tmp_path)

    with pytest.raises(QuizBankError, match='could not read'):
        _ = bank.entries


def test_a_write_failure_is_a_bank_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bank = _bank(tmp_path / 'quiz_bank.json')

    def refuse(_path: Path, _data: bytes) -> None:
        raise OSError('disk full')

    monkeypatch.setattr('aliexpress_coins.tasks.quiz_bank.atomic_write_bytes', refuse)

    with pytest.raises(QuizBankError, match='could not write'):
        _record(bank)


@pytest.mark.parametrize(
    ('question', 'options', 'chosen', 'correct', 'source'),
    [
        ('', OPTIONS, 'True', 'True', QuestionSource.RESULT_PAGE),
        (QUESTION, (), 'True', 'True', QuestionSource.RESULT_PAGE),
        (QUESTION, OPTIONS, '', 'True', QuestionSource.RESULT_PAGE),
        (QUESTION, OPTIONS, 'True', '', QuestionSource.RESULT_PAGE),
        (QUESTION, OPTIONS, 'True', 'True', 'somewhere'),
    ],
)
def test_record_rejects_invalid_arguments(
    tmp_path: Path,
    question: str,
    options: tuple[str, ...],
    chosen: str,
    correct: str,
    source: QuestionSource | str,
) -> None:
    bank = _bank(tmp_path / 'quiz_bank.json')

    with pytest.raises(QuizBankError):
        bank.record(question, options, chosen, correct, source=source)


def test_from_environment_reads_the_path_and_defaults(tmp_path: Path) -> None:
    explicit = QuizBank.from_environment({'AECOINS_QUIZ_BANK_PATH': str(tmp_path / 'custom.json')})

    assert explicit.path == tmp_path / 'custom.json'
    assert QuizBank.from_environment({}).path == Path(DEFAULT_QUIZ_BANK_PATH)


def test_entry_as_dict_round_trips_through_the_parser(tmp_path: Path) -> None:
    path = tmp_path / 'quiz_bank.json'
    entry = _record(_bank(path))
    payload = {'schema': 1, 'questions': [entry.as_dict()]}
    path.write_text(json.dumps(payload))

    reloaded = _bank(path).lookup(QUESTION)

    assert reloaded == entry


def test_the_committed_bank_is_an_empty_valid_schema() -> None:
    path = Path(__file__).resolve().parents[1] / 'data' / 'quiz_bank.json'
    payload = json.loads(path.read_text())

    assert payload == {'schema': BANK_SCHEMA, 'questions': []}
    assert _bank(path).entries == ()
