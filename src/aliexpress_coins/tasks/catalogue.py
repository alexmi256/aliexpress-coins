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

When discovery supplies a :class:`TaskPolicyContext`, only the row's own OCR
elements are evidence. A span that also appears in a neighbouring element is
ambiguous and matches neither an allow nor a deny rule: it may be copy the row
splitter absorbed from somewhere else. Refusal remains the default when no
unambiguous rule matches.

Patterns are case-insensitive word sequences. Punctuation is ignored
(``Play & Earn`` normalises to ``play earn``), but a pattern must match whole
words: the pattern ``cart`` does not match the label ``cartoon``. That keeps a
short deny rule from accidentally refusing an unrelated task and, more
importantly, keeps a short allow rule from reaching a row it does not name. A
one-word allow pattern is trusted only in the title; a generic word in a
subtitle must not pull an unrelated row into an executor. The same rule applies
to deny patterns: a small generic-word set (``cart``, ``order``, ``game``) is
trusted only in the title, while action phrases and precise action verbs
(``Add to cart``, ``Checkout``) may match either field. A subtitle describes a
row as often as it names its action, so it must not turn an allowed task into a
refusal.

The loader also runs the policy against its own data. If an allowed row's
pattern would be refused by the deny list, or would resolve to a different
allowed row, loading fails. A contradictory spec is a broken gate, not a task
the nightly run discovers at 03:30.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import StrEnum
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from aliexpress_coins.tasks.errors import TaskCatalogueError

DEFAULT_DENY_REASON = 'unrecognised'
"""The reason recorded for a label that matches no row in the catalogue."""

_AUTHORIZATION_SEAL = object()
"""The private marker that proves a token came from the dispatch gate.

A token carries this object only when :class:`DispatchAuthorization._mint`
builds it. The check cannot stop a caller from importing a private name, but it
does stop the ordinary bypass: constructing a token by hand raises before an
executor can see it.
"""

_KEY_PATTERN = re.compile(r'[a-z][a-z0-9_]*')
"""Keys are snake_case so they read the same in YAML, logs and report fields."""

_GENERIC_DENY_WORDS = frozenset({'buy', 'cart', 'game', 'games', 'invite', 'order', 'review', 'reviews', 'share'})
"""One-word deny patterns that are too generic to trust in a subtitle.

``cart`` and ``order`` describe the action in a title, but the same word can
appear in an allowed row's subtitle. The list is deliberately short: precise
action verbs such as ``Checkout`` and ``Purchase`` still deny either field.
"""


class TaskCategory(StrEnum):
    """The kind of work an allowed task family performs.

    The set is intentionally small. A new category is a new executor contract,
    not a label: the loader rejects a category it does not know rather than
    letting the row reach a run with no code prepared to execute it.
    """

    SIGN_IN = 'sign_in'
    QUIZ = 'quiz'
    BROWSE = 'browse'
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
class TaskPolicyContext:
    """The OCR elements that belong to one row, and those beside it.

    ``title_elements`` and ``subtitle_elements`` are the row's own copy, kept as
    separate OCR elements rather than one flattened string. Discovery supplies
    the frame's other elements in ``neighbouring_elements``. If the same policy
    span appears there too, ownership is ambiguous and the span is not evidence
    about this row (#109).
    """

    title_elements: tuple[str, ...]
    subtitle_elements: tuple[str, ...] = ()
    neighbouring_elements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, 'title_elements', tuple(self.title_elements))
        object.__setattr__(self, 'subtitle_elements', tuple(self.subtitle_elements))
        object.__setattr__(self, 'neighbouring_elements', tuple(self.neighbouring_elements))


@dataclass(frozen=True, slots=True)
class ContaminatedMatch:
    """A policy pattern found in copy that also belongs to a neighbouring element.

    A splitter failure can attach a card or the mini-app strip to the row above
    it. If the matched span is also present in a separate element on the frame,
    it is not evidence about this row and must not decide an allow or a refusal.
    The match is kept only so the run report can name what was ignored.
    """

    pattern: str
    field: str
    source: str

    def as_dict(self) -> dict[str, object]:
        """Render the ignored match for a run log or report."""
        return {'pattern': self.pattern, 'field': self.field, 'source': self.source}


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
    contaminated: tuple[ContaminatedMatch, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, 'contaminated', tuple(self.contaminated))

    @property
    def contaminated_pattern(self) -> str | None:
        """The first matched span ignored as belonging to another element."""
        return self.contaminated[0].pattern if self.contaminated else None

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
        record: dict[str, object] = {
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
        if self.contaminated:
            record['contaminated'] = [match.as_dict() for match in self.contaminated]
        return record


@dataclass(frozen=True, slots=True)
class DispatchAuthorization:
    """A sealed proof that one row passed the dispatch gate.

    Executors require this token on the roster entry they are handed. The token
    is minted only by :class:`~aliexpress_coins.tasks.dispatch.DispatchGate`, so
    a caller cannot reach an executor with a raw roster row: the row must have
    been resolved against the catalogue and recorded first.
    """

    key: str
    title: str
    category: TaskCategory
    catalogue_version: int
    rule: str
    surface: str | None = None
    sequence: int = 0
    _seal: object = dataclass_field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _AUTHORIZATION_SEAL:
            raise ValueError('a dispatch authorization must be minted by the policy gate')
        if not self.key or not self.title:
            raise ValueError('a dispatch authorization needs a key and a title')
        if self.sequence < 1:
            raise ValueError('a dispatch authorization needs a positive sequence')

    @classmethod
    def _mint(
        cls,
        *,
        key: str,
        title: str,
        category: TaskCategory,
        catalogue_version: int,
        rule: str,
        surface: str | None,
        sequence: int,
    ) -> DispatchAuthorization:
        """Create a token; private-by-convention to the dispatch gate."""
        return cls(
            key=key,
            title=title,
            category=category,
            catalogue_version=catalogue_version,
            rule=rule,
            surface=surface,
            sequence=sequence,
            _seal=_AUTHORIZATION_SEAL,
        )

    def covers(self, *, key: str | None, title: str, category: TaskCategory | None) -> bool:
        """Whether this token authorises exactly the row it is attached to."""
        return self.key == key and self.title == title and self.category is category


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

    def decide(
        self,
        title: str,
        *,
        subtitle: str | None = None,
        context: TaskPolicyContext | None = None,
    ) -> TaskDecision:
        """Decide whether a task row may be dispatched.

        Deny rules are checked against both the title and the subtitle before
        any allow rule, so deny always beats allow. A generic word such as
        ``cart`` is trusted only in the title, while action phrases and precise
        action verbs (``Add to cart``, ``Checkout``) are trusted in either
        field: the title names what the row does, while a subtitle may merely
        describe it (for example a browse title with ``... in-cart items``). The
        title is preferred over the subtitle when an allowed row matches; the
        latter is the fallback for a title that OCR mangled.

        ``context`` carries the row's own OCR elements and the frame's other
        elements. When supplied, a field is *allowable* evidence only if its
        owned elements attest to the same copy. Denial is stricter: an
        unattested field is still displayed text, so the deny list is checked
        against it anyway. Only an attested subtitle whose duplicated span is
        explicitly contaminated may be skipped (#109).
        """
        fields = _policy_fields(title, subtitle, context)
        attested = {field for field, text in fields if text is not None}
        contaminated: list[ContaminatedMatch] = []
        for field, text in (('title', title), ('subtitle', subtitle)):
            if text is None:
                continue
            neighbouring = context.neighbouring_elements if context is not None and field in attested else ()
            for rule in self.denied:
                pattern, ignored = _first_match(
                    _denied_patterns(rule, field),
                    text,
                    field=field,
                    neighbouring_elements=neighbouring,
                )
                contaminated = _merge_contamination(contaminated, ignored)
                if pattern is not None:
                    return TaskDecision(
                        title=title,
                        subtitle=subtitle,
                        decision=DecisionKind.DENY,
                        rule=rule.key,
                        reason=rule.deny_reason,
                        matched_pattern=pattern,
                        matched_field=field,
                        contaminated=tuple(contaminated),
                    )

        for field, text in fields:
            if text is None:
                continue
            for spec in self.allowed:
                pattern, ignored = _first_match(
                    _allowed_patterns(spec, field),
                    text,
                    field=field,
                    neighbouring_elements=context.neighbouring_elements if context is not None else (),
                )
                contaminated = _merge_contamination(contaminated, ignored)
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
                        contaminated=tuple(contaminated),
                    )

        return TaskDecision(
            title=title,
            subtitle=subtitle,
            decision=DecisionKind.DENY,
            rule=None,
            reason=self.default_deny_reason,
            contaminated=tuple(contaminated),
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
    """Trust a generic deny word only on the title.

    A generic word (``cart``, ``order``, ``game``) says what the row does when
    it is the title. In a subtitle it can describe the merchandise instead, as
    ``in-cart items`` does. Action phrases (``Add to cart``, ``Place an order``)
    and precise one-word action verbs
    (``Checkout``, ``Purchase``) are trusted in either field.
    """
    if field == 'title':
        return rule.label_patterns
    return tuple(pattern for pattern in rule.label_patterns if _normalise(pattern) not in _GENERIC_DENY_WORDS)


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


def _policy_fields(
    title: str,
    subtitle: str | None,
    context: TaskPolicyContext | None,
) -> tuple[tuple[str, str | None], tuple[str, str | None]]:
    """Return the text the policy may inspect, in title-then-subtitle order."""
    if context is None:
        return (('title', title), ('subtitle', subtitle))
    return (
        ('title', _owned_field_text(title, context.title_elements)),
        ('subtitle', _owned_field_text(subtitle, context.subtitle_elements)),
    )


def _owned_field_text(display: str | None, elements: tuple[str, ...]) -> str | None:
    """Trust provenance only when it attests to the same copy being reported."""
    if display is None or not elements:
        return None
    owned = ' '.join(elements)
    return display if _normalise(owned) == _normalise(display) else None


def _first_match(
    patterns: tuple[str, ...],
    text: str,
    *,
    field: str,
    neighbouring_elements: tuple[str, ...] = (),
) -> tuple[str | None, tuple[ContaminatedMatch, ...]]:
    """Return the first authored pattern unambiguously owned by this row.

    A subtitle span that also appears in a neighbouring element is not evidence
    about this row, so it is skipped and returned as contamination for the run
    report. The title is the row's own name; a duplicate tile elsewhere cannot
    make a true game row's title ambiguous.
    """
    contaminated: list[ContaminatedMatch] = []
    for pattern in patterns:
        if not _contains(text, pattern):
            continue
        source = None
        if field == 'subtitle':
            source = next(
                (element for element in neighbouring_elements if _contains(element, pattern)),
                None,
            )
        if source is not None:
            contaminated.append(ContaminatedMatch(pattern=pattern, field=field, source=source))
            continue
        return pattern, tuple(contaminated)
    return None, tuple(contaminated)


def _merge_contamination(
    current: list[ContaminatedMatch],
    ignored: tuple[ContaminatedMatch, ...],
) -> list[ContaminatedMatch]:
    """Add newly ignored spans without repeating one in the report."""
    seen = {(match.pattern, match.field, match.source) for match in current}
    for match in ignored:
        identity = (match.pattern, match.field, match.source)
        if identity not in seen:
            current.append(match)
            seen.add(identity)
    return current


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
