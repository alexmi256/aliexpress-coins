"""The quiz question bank: learn an answer once, answer it next time (#39).

The quiz result page prints ``Correct answer: X`` before the reward, so the
first encounter with a question is enough to learn it. This module persists
that observation in a JSON file outside the package:

.. code-block:: json

    {
      "schema": 1,
      "questions": [
        {
          "key": "<sha256 of the normalised question>",
          "question": "Q : Which of the following statements is correct?",
          "normalized": "q which of the following statements is correct",
          "options": ["True", "False"],
          "chosen": "False",
          "correct": "True",
          "source": "result_page",
          "seen_at": "2026-11-22T12:00:00+00:00"
        }
      ]
    }

The file is sorted by key, written atomically and left untouched when an
observation adds nothing: replaying the same question must not dirty the work
tree. ``source`` is ``result_page`` for something a result page taught the bank
and ``manual`` for a human correction. A manual entry stays authoritative while
its recorded answer is still one of the question's options; when the options
change, the result page replaces it.

The lookup rule is deliberately conservative. An entry is used only when its
recorded correct option is present in the *current* options by text. A recorded
option that is missing means the quiz changed: the executor falls back to its
#25 guess rather than tapping a remembered position, then records the new
observation.

No account data belongs here: questions and answer options only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from aliexpress_coins.perception.artifacts import atomic_write_bytes

__all__ = [
    'ANSWER_FROM_BANK',
    'ANSWER_GUESS',
    'BANK_SCHEMA',
    'DEFAULT_QUIZ_BANK_PATH',
    'BankEntry',
    'QuestionSource',
    'QuizBank',
    'QuizBankError',
    'match_option',
    'normalise_question',
    'normalize_question',
    'question_key',
]

BANK_SCHEMA = 1
"""The only on-disk schema this module reads or writes."""

DEFAULT_QUIZ_BANK_PATH = 'data/quiz_bank.json'
"""The bank's repository path, relative to the run's working directory."""

LOGGER_NAME = 'aliexpress_coins.tasks.quiz_bank'
"""The logger used when the caller does not supply one."""

ANSWER_FROM_BANK = 'bank'
"""``QuizResult.answer_source`` when the bank supplied the tapped option."""

ANSWER_GUESS = 'guess'
"""``QuizResult.answer_source`` when the executor fell back to its #25 guess."""

_CONFUSABLES = str.maketrans({'l': '1', 'o': '0'})
"""OCR confusables that must not create a duplicate question (``l``/``1``, ``O``/``0``)."""

_PUNCTUATION = re.compile(r'[\W_]+', re.UNICODE)
"""Everything that is not a letter, digit or whitespace, including underscores."""

_WHITESPACE = re.compile(r'\s+')
"""Runs of whitespace collapse to one space."""

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class QuestionSource(StrEnum):
    """Where a bank entry's answer came from."""

    RESULT_PAGE = 'result_page'
    MANUAL = 'manual'


class QuizBankError(Exception):
    """The bank could not be read, validated or written."""


def normalize_question(text: str) -> str:
    """Return the stable key text for a question.

    Case folding, OCR-confusable folding (``l``/``1`` and ``o``/``0``),
    punctuation stripping and whitespace collapse are applied in that order.
    The raw question is kept separately in the entry for a human to read.
    """
    if not isinstance(text, str):
        raise QuizBankError(f'question must be a string, got {type(text).__name__}')
    folded = unicodedata.normalize('NFKC', text).casefold().translate(_CONFUSABLES)
    spaced = _PUNCTUATION.sub(' ', folded)
    return _WHITESPACE.sub(' ', spaced).strip()


# The repository's prose uses British spelling; both names are public so a
# caller does not have to guess which one this module chose.
normalise_question = normalize_question


def question_key(text: str) -> str:
    """A stable SHA-256 identity for the normalised question text."""
    normalized = normalize_question(text)
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def match_option(recorded: str, options: Sequence[str]) -> int | None:
    """Find a recorded answer text among the options now on screen.

    Comparison uses the same normalisation as the question key, so a trailing
    period or an OCR ``1`` for ``l`` still matches. Position is never used.
    """
    target = normalize_question(recorded)
    for index, option in enumerate(options):
        if normalize_question(option) == target:
            return index
    return None


@dataclass(frozen=True, slots=True)
class BankEntry:
    """One learned question, with both its readable and stable forms."""

    key: str
    question: str
    normalized: str
    options: tuple[str, ...]
    chosen: str
    correct: str
    source: QuestionSource
    seen_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'options', tuple(self.options))

    def as_dict(self) -> dict[str, object]:
        """Render the entry in the bank's documented, diff-friendly order."""
        return {
            'key': self.key,
            'question': self.question,
            'normalized': self.normalized,
            'options': list(self.options),
            'chosen': self.chosen,
            'correct': self.correct,
            'source': self.source.value,
            'seen_at': None if self.seen_at is None else self.seen_at.isoformat(),
        }


class QuizBank:
    """Read and atomically grow the human-editable quiz bank.

    Construction does not touch the file. The first lookup or record loads it;
    an absent file is an empty bank. A file that is not valid UTF-8 JSON, or
    whose entries fail validation, is moved aside as ``.corrupt-<timestamp>``
    and the night continues with an empty bank rather than failing a quiz.
    """

    def __init__(
        self,
        path: str | Path = DEFAULT_QUIZ_BANK_PATH,
        *,
        clock: Clock = _utc_now,
        logger: logging.Logger | None = None,
    ) -> None:
        self.path = Path(path)
        self._clock = clock
        self._logger = logger if logger is not None else logging.getLogger(LOGGER_NAME)
        self._entries: dict[str, BankEntry] | None = None

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        clock: Clock = _utc_now,
        logger: logging.Logger | None = None,
    ) -> QuizBank:
        """Use ``AECOINS_QUIZ_BANK_PATH``, falling back to ``data/quiz_bank.json``."""
        env = os.environ if environment is None else environment
        path = env.get('AECOINS_QUIZ_BANK_PATH', '').strip() or DEFAULT_QUIZ_BANK_PATH
        return cls(path, clock=clock, logger=logger)

    @property
    def entries(self) -> tuple[BankEntry, ...]:
        """Every entry, sorted by key so a rewrite has a stable order."""
        return tuple(self._loaded()[key] for key in sorted(self._loaded()))

    def lookup(self, question: str) -> BankEntry | None:
        """Return the learned entry for a question, or ``None``."""
        return self._loaded().get(question_key(question))

    def resolve(self, question: str, options: Sequence[str]) -> int | None:
        """Return the index of the recorded correct option, if still present.

        ``None`` means either the question is unknown or its recorded answer is
        not among the options in this frame; the caller must then guess and
        record the new observation, never tap a remembered position.
        """
        entry = self.lookup(question)
        if entry is None:
            return None
        return match_option(entry.correct, options)

    def record(
        self,
        question: str,
        options: Sequence[str],
        chosen: str,
        correct: str,
        *,
        source: QuestionSource | str = QuestionSource.RESULT_PAGE,
        seen_at: datetime | None = None,
    ) -> BankEntry:
        """Learn one observation, rewriting the file only when it changed.

        A repeat with the same normalised options and correct answer is a no-op:
        it keeps the original raw question, chosen answer and ``seen_at`` so a
        nightly run does not dirty the bank. A manual entry survives an equally
        applicable result-page observation; when the option set changes the
        screen wins and the entry is replaced.
        """
        try:
            raw_question = _text(question, 'question')
            raw_options = _options(options)
            raw_chosen = _text(chosen, 'chosen')
            raw_correct = _text(correct, 'correct')
            source_value = _source(source)
        except _CorruptBank as exc:
            raise QuizBankError(str(exc)) from exc
        key = question_key(raw_question)
        normalized = normalize_question(raw_question)
        incoming_options = tuple(normalize_question(option) for option in raw_options)

        existing = self._loaded().get(key)
        if existing is not None:
            existing_options = tuple(sorted(normalize_question(option) for option in existing.options))
            same_options = existing_options == tuple(sorted(incoming_options))
            same_correct = normalize_question(existing.correct) == normalize_question(raw_correct)
            if same_options and same_correct:
                return existing
            if (
                existing.source is QuestionSource.MANUAL
                and source_value is QuestionSource.RESULT_PAGE
                and same_options
                and match_option(existing.correct, raw_options) is not None
            ):
                return existing

        entry = BankEntry(
            key=key,
            question=raw_question,
            normalized=normalized,
            options=raw_options,
            chosen=raw_chosen,
            correct=raw_correct,
            source=source_value,
            seen_at=self._clock() if seen_at is None else seen_at,
        )
        entries = dict(self._loaded())
        entries[key] = entry
        self._commit(entries)
        return entry

    def _loaded(self) -> dict[str, BankEntry]:
        if self._entries is None:
            self._entries = self._load()
        return self._entries

    def _load(self) -> dict[str, BankEntry]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise QuizBankError(f'could not read {self.path}: {exc}') from exc

        try:
            payload: Any = json.loads(raw.decode('utf-8'))
            return _parse(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, _CorruptBank) as exc:
            self._quarantine(exc)
            return {}

    def _commit(self, entries: Mapping[str, BankEntry]) -> None:
        payload = {
            'schema': BANK_SCHEMA,
            'questions': [entries[key].as_dict() for key in sorted(entries)],
        }
        data = (json.dumps(payload, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        try:
            atomic_write_bytes(self.path, data)
        except OSError as exc:
            raise QuizBankError(f'could not write {self.path}: {exc}') from exc
        self._entries = dict(entries)

    def _quarantine(self, reason: Exception) -> None:
        stamp = self._clock().strftime('%Y%m%dT%H%M%S%fZ')
        target = self.path.with_name(f'{self.path.name}.corrupt-{stamp}')
        suffix = 1
        while target.exists():
            target = self.path.with_name(f'{self.path.name}.corrupt-{stamp}.{suffix}')
            suffix += 1
        fields: dict[str, object] = {'path': str(self.path), 'reason': str(reason)}
        try:
            os.replace(self.path, target)
        except OSError as exc:
            fields['quarantine_error'] = str(exc)
        else:
            fields['quarantined'] = str(target)
        self._warn('quiz_bank_corrupt', **fields)

    def _warn(self, event: str, **fields: object) -> None:
        self._logger.warning(
            '%s',
            json.dumps({'event': event, **fields}, separators=(',', ':'), sort_keys=True),
        )


class _CorruptBank(Exception):
    """Internal marker: the file is unparseable or fails validation."""


def _parse(payload: Any) -> dict[str, BankEntry]:
    if not isinstance(payload, dict):
        raise _CorruptBank('bank must be a JSON object')
    data = cast(dict[str, Any], payload)

    schema = data.get('schema')
    if isinstance(schema, bool) or not isinstance(schema, int) or schema != BANK_SCHEMA:
        raise _CorruptBank(f'unsupported schema: {schema!r}')

    questions = data.get('questions')
    if not isinstance(questions, list):
        raise _CorruptBank("'questions' must be a JSON array")

    entries: dict[str, BankEntry] = {}
    for index, item in enumerate(questions):
        entry = _parse_entry(item, index)
        entries[entry.key] = entry
    return entries


def _parse_entry(item: Any, index: int) -> BankEntry:
    if not isinstance(item, dict):
        raise _CorruptBank(f'question {index} must be a JSON object')
    data = cast(dict[str, Any], item)

    question = _text(data.get('question'), f'question {index} question')
    options = _options(data.get('options'), field=f'question {index} options')
    correct = _text(data.get('correct'), f'question {index} correct')
    chosen_raw = data.get('chosen')
    chosen = correct if chosen_raw is None else _text(chosen_raw, f'question {index} chosen')
    source_raw = data.get('source')
    source = QuestionSource.MANUAL if source_raw is None else _source(source_raw)
    seen_raw = data.get('seen_at')
    seen_at = None if seen_raw is None else _timestamp(seen_raw, index)

    return BankEntry(
        key=question_key(question),
        question=question,
        normalized=normalize_question(question),
        options=options,
        chosen=chosen,
        correct=correct,
        source=source,
        seen_at=seen_at,
    )


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise _CorruptBank(f'{field} must be a string')
    text = value.strip()
    if not text:
        raise _CorruptBank(f'{field} must not be empty')
    return text


def _options(value: Any, *, field: str = 'options') -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise _CorruptBank(f'{field} must be an array')
    options = tuple(_text(option, f'{field} item') for option in value)
    if not options:
        raise _CorruptBank(f'{field} must not be empty')
    return options


def _source(value: Any) -> QuestionSource:
    if isinstance(value, QuestionSource):
        return value
    if not isinstance(value, str):
        raise _CorruptBank(f'source must be a string, got {type(value).__name__}')
    try:
        return QuestionSource(value.strip().casefold())
    except ValueError as exc:
        raise _CorruptBank(f'unknown source: {value!r}') from exc


def _timestamp(value: Any, index: int) -> datetime:
    if not isinstance(value, str):
        raise _CorruptBank(f'question {index} seen_at must be an ISO timestamp string')
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _CorruptBank(f'question {index} seen_at is not an ISO timestamp: {value!r}') from exc
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp
