"""Load the task catalogue and apply its default-deny policy (issue #21).

`specs/tasks.yaml` is the only place a task family's rules live. This module
validates the whole document with Pydantic before a run can start, then answers
one question for the dispatch point: may this OCR label be touched, and if not,
why?

The policy is deliberately lopsided:

* a label is allowed only when it matches an ``allowed`` row's pattern;
* every ``denied`` row is checked first, so a label that matches both an allow
  pattern and a deny rule is refused with the deny reason;
* a label that matches nothing is refused as ``unrecognised``.

Patterns are case-insensitive word sequences. Punctuation is ignored
(``Play & Earn`` normalises to ``play earn``), but a pattern must match whole
words: the pattern ``cart`` does not match the label ``cartoon``. That keeps a
short deny rule from accidentally refusing an unrelated task and, more
importantly, keeps a short allow rule from reaching a row it does not name. A
one-word allow pattern is trusted only in the title; a generic word in a
subtitle must not pull an unrelated row into an executor. The same rule applies
to deny patterns: a one-word deny pattern (``cart``, ``order``, ``game``) is
trusted only in the title, while an action phrase (``Add to cart``) may match
either field. A subtitle describes a row as often as it names its action, so it
must not turn an allowed task into a refusal.

The loader also runs the policy against its own data. If an allowed row's
pattern would be refused by the deny list, or would resolve to a different
allowed row, loading fails. A contradictory spec is a broken gate, not a task
the nightly run discovers at 03:30.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from aliexpress_coins.tasks.errors import TaskCatalogueError

DEFAULT_DENY_REASON = 'unrecognised'
"""The reason recorded for a label that matches no row in the catalogue."""

_KEY_PATTERN = re.compile(r'[a-z][a-z0-9_]*')
"""Keys are snake_case so they read the same in YAML, logs and report fields."""


class TaskCategory(StrEnum):
    """The kind of work an allowed task family performs.

    The set is intentionally small. A new category is a new executor contract,
    not a label: the loader rejects a category it does not know rather than
    letting the row reach a run with no code prepared to execute it.
    """

    SIGN_IN = 'sign_in'
    QUIZ = 'quiz'
    BROWSE_WAIT = 'browse_wait'
    ITEM_TAP = 'item_tap'
    SHORTCUT = 'shortcut'
    WIDGET = 'widget'
    NOTIFICATION = 'notification'


class DecisionKind(StrEnum):
    """Whether a label may be dispatched."""

    ALLOW = 'allow'
    DENY = 'deny'


class AllowedTaskSpec(BaseModel):
    """One task family the collector may perform."""

    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)

    key: str
    label_patterns: tuple[str, ...] = Field(min_length=1)
    category: TaskCategory
    wait_seconds: float = Field(default=0.0, ge=0)
    repeatable: bool = False
    max_per_run: int = Field(default=1, ge=1)
    notes: str | None = None

    @field_validator('key')
    @classmethod
    def _check_key(cls, value: str) -> str:
        return _clean_key(value)

    @field_validator('label_patterns')
    @classmethod
    def _check_patterns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_patterns(value)

    @field_validator('category', mode='before')
    @classmethod
    def _check_category(cls, value: object) -> object:
        return _coerce_category(value)

    @field_validator('notes')
    @classmethod
    def _check_notes(cls, value: str | None) -> str | None:
        return _clean_optional_text(value)

    @model_validator(mode='after')
    def _check_run_limit(self) -> Self:
        if not self.repeatable and self.max_per_run != 1:
            raise ValueError('max_per_run must be 1 when repeatable is false')
        return self


class DeniedTaskSpec(BaseModel):
    """One rule that refuses a task family by name."""

    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)

    key: str
    label_patterns: tuple[str, ...] = Field(min_length=1)
    deny_reason: str
    notes: str | None = None

    @field_validator('key')
    @classmethod
    def _check_key(cls, value: str) -> str:
        return _clean_key(value)

    @field_validator('label_patterns')
    @classmethod
    def _check_patterns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _clean_patterns(value)

    @field_validator('deny_reason')
    @classmethod
    def _check_reason(cls, value: str) -> str:
        return _clean_text(value, 'deny_reason')

    @field_validator('notes')
    @classmethod
    def _check_notes(cls, value: str | None) -> str | None:
        return _clean_optional_text(value)


class TaskCatalogueSpec(BaseModel):
    """The YAML document shape, validated in one place."""

    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)

    version: int = Field(ge=1)
    default_deny_reason: str = DEFAULT_DENY_REASON
    allowed: tuple[AllowedTaskSpec, ...] = Field(min_length=1)
    denied: tuple[DeniedTaskSpec, ...] = Field(min_length=1)
    notes: str | None = None

    @field_validator('default_deny_reason')
    @classmethod
    def _check_default_reason(cls, value: str) -> str:
        return _clean_text(value, 'default_deny_reason')

    @field_validator('notes')
    @classmethod
    def _check_notes(cls, value: str | None) -> str | None:
        return _clean_optional_text(value)

    @model_validator(mode='after')
    def _check_unique_keys(self) -> Self:
        keys = [spec.key for spec in self.allowed]
        keys.extend(spec.key for spec in self.denied)
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(f'duplicate task keys: {duplicates!r}')
        return self


@dataclass(frozen=True, slots=True)
class TaskDecision:
    """The policy answer for one OCR label, ready for the run report."""

    title: str
    subtitle: str | None
    decision: DecisionKind
    rule: str | None
    reason: str | None
    spec: AllowedTaskSpec | None = None
    matched_pattern: str | None = None
    matched_field: str | None = None

    @property
    def allowed(self) -> bool:
        """True only when an allowed row matched."""
        return self.decision is DecisionKind.ALLOW and self.spec is not None

    @property
    def denied(self) -> bool:
        """True for a deny rule and for the default deny."""
        return not self.allowed

    @property
    def deny_reason(self) -> str | None:
        """The human-readable reason for a refusal; ``None`` when allowed."""
        return None if self.allowed else self.reason

    def as_dict(self) -> dict[str, object]:
        """Render the decision as the structured event a run log carries."""
        return {
            'title': self.title,
            'subtitle': self.subtitle,
            'decision': self.decision.value,
            'allowed': self.allowed,
            'key': self.spec.key if self.spec is not None else None,
            'category': self.spec.category.value if self.spec is not None else None,
            'rule': self.rule,
            'reason': self.reason,
            'matched_pattern': self.matched_pattern,
            'matched_field': self.matched_field,
        }


@dataclass(frozen=True, slots=True)
class TaskCatalogue:
    """A validated catalogue and the policy it defines."""

    version: int
    default_deny_reason: str
    allowed: tuple[AllowedTaskSpec, ...]
    denied: tuple[DeniedTaskSpec, ...]
    notes: str | None = None

    @property
    def allowed_keys(self) -> tuple[str, ...]:
        """Keys of the families that may run, in catalogue order."""
        return tuple(spec.key for spec in self.allowed)

    @property
    def denied_keys(self) -> tuple[str, ...]:
        """Keys of the rules that refuse a family, in catalogue order."""
        return tuple(spec.key for spec in self.denied)

    def allowed_by_key(self, key: str) -> AllowedTaskSpec:
        """Return an allowed family by key, or raise ``KeyError``."""
        for spec in self.allowed:
            if spec.key == key:
                return spec
        raise KeyError(f'unknown allowed task: {key!r}')

    def denied_by_key(self, key: str) -> DeniedTaskSpec:
        """Return a deny rule by key, or raise ``KeyError``."""
        for spec in self.denied:
            if spec.key == key:
                return spec
        raise KeyError(f'unknown denied task: {key!r}')

    def decide(self, title: str, *, subtitle: str | None = None) -> TaskDecision:
        """Decide whether a task row may be dispatched.

        Deny rules are checked against both the title and the subtitle before
        any allow rule, so deny always beats allow. An action phrase such as
        ``Add to cart`` is trusted in either field, but a generic single word
        such as ``cart`` is trusted only in the title: the title names what the
        row does, while a subtitle may merely describe it (for example
        ``Browse recently viewed items`` with ``... in-cart items``). The title
        is preferred over the subtitle when an allowed row matches; the latter
        is the fallback for a title that OCR mangled.
        """
        for field, text in (('title', title), ('subtitle', subtitle)):
            if text is None:
                continue
            for rule in self.denied:
                pattern = _first_match(_denied_patterns(rule, field), text)
                if pattern is not None:
                    return TaskDecision(
                        title=title,
                        subtitle=subtitle,
                        decision=DecisionKind.DENY,
                        rule=rule.key,
                        reason=rule.deny_reason,
                        matched_pattern=pattern,
                        matched_field=field,
                    )

        for field, text in (('title', title), ('subtitle', subtitle)):
            if text is None:
                continue
            for spec in self.allowed:
                pattern = _first_match(_allowed_patterns(spec, field), text)
                if pattern is not None:
                    return TaskDecision(
                        title=title,
                        subtitle=subtitle,
                        decision=DecisionKind.ALLOW,
                        rule=spec.key,
                        reason=None,
                        spec=spec,
                        matched_pattern=pattern,
                        matched_field=field,
                    )

        return TaskDecision(
            title=title,
            subtitle=subtitle,
            decision=DecisionKind.DENY,
            rule=None,
            reason=self.default_deny_reason,
        )


def load_task_catalogue(path: str | Path) -> TaskCatalogue:
    """Read, validate and self-check one catalogue file.

    Every failure is a :class:`TaskCatalogueError`: a missing or unreadable
    file, invalid YAML, a Pydantic validation error, a deny pattern claimed by
    two rules, or a catalogue that refuses its own allowed rows.
    """
    catalogue_path = Path(path)
    try:
        raw = yaml.safe_load(catalogue_path.read_text(encoding='utf-8'))
    except FileNotFoundError as exc:
        raise TaskCatalogueError(f'task catalogue not found: {catalogue_path}') from exc
    except OSError as exc:
        raise TaskCatalogueError(f'could not read task catalogue {catalogue_path}: {exc}') from exc
    except UnicodeError as exc:
        raise TaskCatalogueError(f'task catalogue is not valid UTF-8: {catalogue_path}') from exc
    except yaml.YAMLError as exc:
        raise TaskCatalogueError(f'invalid YAML in {catalogue_path}: {exc}') from exc

    try:
        document = TaskCatalogueSpec.model_validate(_tupleise(raw))
    except ValidationError as exc:
        raise TaskCatalogueError(f'invalid task catalogue {catalogue_path}: {_format_errors(exc)}') from exc

    catalogue = TaskCatalogue(
        version=document.version,
        default_deny_reason=document.default_deny_reason,
        allowed=document.allowed,
        denied=document.denied,
        notes=document.notes,
    )
    _check_deny_patterns_are_unique(catalogue)
    _check_catalogue_accepts_its_own_rows(catalogue)
    return catalogue


def _tupleise(value: object) -> object:
    """Turn YAML lists into tuples before strict Pydantic validation.

    PyYAML has no tuple type, so every sequence arrives as a list while the
    schema uses immutable tuples. Converting here keeps ``strict=True`` for the
    scalar fields, which is what rejects a boolean where an integer or a float
    belongs.
    """
    if isinstance(value, list):
        return tuple(_tupleise(item) for item in value)
    if isinstance(value, dict):
        return {key: _tupleise(item) for key, item in value.items()}
    return value


def _check_deny_patterns_are_unique(catalogue: TaskCatalogue) -> None:
    """A deny pattern claimed by two rules would make one rule unreachable."""
    owners: dict[str, str] = {}
    for rule in catalogue.denied:
        for pattern in rule.label_patterns:
            normalised = _normalise(pattern)
            owner = owners.get(normalised)
            if owner is not None:
                raise TaskCatalogueError(
                    f'deny pattern {pattern!r} is claimed by both {owner!r} and {rule.key!r}',
                )
            owners[normalised] = rule.key


def _check_catalogue_accepts_its_own_rows(catalogue: TaskCatalogue) -> None:
    """Prove at load time that no allowed row is refused or shadowed.

    Each authored pattern is fed back through the real decision path. That is
    stronger than comparing strings: it catches a deny rule that contains an
    allow pattern, and two allow rows that would compete for the same label.
    """
    for spec in catalogue.allowed:
        for pattern in spec.label_patterns:
            decision = catalogue.decide(pattern)
            if decision.denied:
                raise TaskCatalogueError(
                    f'allowed task {spec.key!r} is refused by its own policy: '
                    f'pattern {pattern!r} -> {decision.rule or "default"!s} ({decision.reason!r})',
                )
            if decision.spec is None or decision.spec.key != spec.key:
                raise TaskCatalogueError(
                    f'allowed task {spec.key!r} pattern {pattern!r} resolves to '
                    f'{decision.rule!r}; every allowed row must be unambiguous',
                )


def _denied_patterns(rule: DeniedTaskSpec, field: str) -> tuple[str, ...]:
    """Trust a one-word deny pattern only on the title.

    A generic word (``cart``, ``order``, ``game``) says what the row does when
    it is the title. In a subtitle it can describe an allowed row instead, as
    ``in-cart items`` does for ``Browse recently viewed items``. Action phrases
    (``Add to cart``, ``Place an order``) are precise enough for either field.
    """
    if field == 'title':
        return rule.label_patterns
    return tuple(pattern for pattern in rule.label_patterns if ' ' in _normalise(pattern))


def _allowed_patterns(spec: AllowedTaskSpec, field: str) -> tuple[str, ...]:
    """Trust a one-word allow pattern only on the title.

    A generic word in a subtitle is how an unrelated row would be mistaken for
    an allowed family. The title is the row's own name, so a one-word control
    such as ``Collect`` may still be named there; the subtitle must carry a
    phrase. In practice the catalogue keeps the streak ``Collect`` out of row
    dispatch entirely (it is a control, not a boost row), but the rule keeps a
    future one-word pattern from becoming a default-deny leak.
    """
    if field == 'title':
        return spec.label_patterns
    return tuple(pattern for pattern in spec.label_patterns if ' ' in _normalise(pattern))


def _first_match(patterns: tuple[str, ...], text: str) -> str | None:
    """Return the first authored pattern that matches the OCR label."""
    for pattern in patterns:
        if _contains(text, pattern):
            return pattern
    return None


def _contains(text: str, pattern: str) -> bool:
    """Match a normalised pattern only on whole-word boundaries."""
    needle = _normalise(pattern)
    if not needle:
        return False
    return f' {needle} ' in f' {_normalise(text)} '


def _normalise(text: str) -> str:
    """Case-fold, drop punctuation and collapse whitespace.

    ``US $0.10`` becomes ``us 0 10`` and ``Play & Earn`` becomes
    ``play earn``, so a pattern does not have to reproduce OCR punctuation.
    """
    folded = unicodedata.normalize('NFKC', text).casefold()
    return ' '.join(''.join(character if character.isalnum() else ' ' for character in folded).split())


def _clean_key(value: str) -> str:
    if _KEY_PATTERN.fullmatch(value) is None:
        raise ValueError(f'key must be snake_case, got {value!r}')
    return value


def _coerce_category(value: object) -> object:
    """Turn the YAML string into the enum before strict validation.

    ``strict=True`` deliberately refuses to turn an arbitrary string into an
    enum behind our back. Doing it here keeps the conversion explicit and gives
    an unknown category a message that names the accepted set.
    """
    if isinstance(value, TaskCategory) or not isinstance(value, str):
        return value
    try:
        return TaskCategory(value)
    except ValueError as exc:
        accepted = ', '.join(category.value for category in TaskCategory)
        raise ValueError(f'unknown task category {value!r}; expected one of: {accepted}') from exc


def _clean_patterns(value: tuple[str, ...]) -> tuple[str, ...]:
    cleaned = tuple(pattern.strip() for pattern in value)
    for pattern in cleaned:
        if not pattern or not _normalise(pattern):
            raise ValueError('label_patterns must not contain an empty pattern')
    normalised = [_normalise(pattern) for pattern in cleaned]
    if len(set(normalised)) != len(normalised):
        raise ValueError('label_patterns must not repeat a pattern')
    return cleaned


def _clean_text(value: str, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f'{field} must not be empty')
    return cleaned


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    return _clean_text(value, 'notes')


def _format_errors(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        location = '.'.join(str(part) for part in error['loc']) or '<document>'
        parts.append(f'{location}: {error["msg"]}')
    return '; '.join(parts)
