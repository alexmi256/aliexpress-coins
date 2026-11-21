"""Load and validate the popup corpus (issue #11).

`specs/popups.yaml` is data: one row per popup family, each with an anchor and
an ordered set of close attempts. This module is the only code that knows the
shape of that file. It validates every row before a run can use it, because a
popup with an unexecutable close is exactly the case the issue says must fail
loudly instead of being tapped at a guessed coordinate.

Close attempts follow the policy order declared in the YAML — the committed
corpus is ``label``, ``template``, ``fraction`` — and a row either has at least
one of those or an explicit ``fail``. The loader resolves template and fixture
paths against the repository root and refuses a resolved path that escapes the
reviewed directories.

No device access, OCR or matching happens here. Loading is deterministic and
can be exercised wholly offline.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml
from PIL import Image, UnidentifiedImageError

from aliexpress_coins.control.errors import PopupSpecError

TEMPLATE_PREFIX = 'assets/templates/'
"""Templates are reviewed crops and live in exactly one directory."""

FIXTURE_PREFIX = 'tests/data/popups/'
"""Full-frame fixtures are reviewed captures and live in exactly one directory."""

_FAMILY_PATTERN = re.compile(r'[a-z][a-z0-9_]*')


class CloseKind(StrEnum):
    """How a popup family can be dismissed."""

    TEMPLATE = 'template'
    LABEL = 'label'
    FRACTION = 'fraction'
    FAIL = 'fail'


_REQUIRED_STRATEGIES = frozenset({CloseKind.LABEL, CloseKind.TEMPLATE, CloseKind.FRACTION})
"""Every corpus must declare all three non-fail strategies, in any order."""


class PopupStatus(StrEnum):
    """Whether the family was re-triggered during the corpus session."""

    REPRODUCED = 'reproduced'
    OBSERVED_NOT_REPRODUCED = 'observed_not_reproduced'


@dataclass(frozen=True, slots=True)
class ObservedGeometry:
    """The frame geometry a row's crop and fractions were measured at."""

    width: int
    height: int
    density: int
    sdk: int


@dataclass(frozen=True, slots=True)
class ReferenceGeometry:
    """The reference device recorded once at the top of the corpus."""

    device: str
    width: int
    height: int
    density: int
    sdk: int
    source: str


@dataclass(frozen=True, slots=True)
class CloseAttempt:
    """One executable dismissal attempt.

    Exactly the field for ``kind`` is populated. ``template`` is an absolute
    path resolved against the repository root; ``source_width`` records the
    width of the frame the crop came from so #15 can scale it.
    """

    kind: CloseKind
    template: Path | None = None
    source_width: int | None = None
    label: str | None = None
    fraction: tuple[float, float] | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PopupFamily:
    """A popup family and every way the runner may close it."""

    family: str
    when: str
    anchor: str
    close: tuple[CloseAttempt, ...]
    status: PopupStatus
    observed_at: ObservedGeometry
    blocking: bool = True
    chains_to: tuple[str, ...] = ()
    fixture: Path | None = None
    notes: str | None = None

    @property
    def is_unclosable(self) -> bool:
        """True when the only strategy is an explicit loud failure."""
        return len(self.close) == 1 and self.close[0].kind is CloseKind.FAIL


@dataclass(frozen=True, slots=True)
class PopupCorpus:
    """A validated popup corpus."""

    version: int
    reference: ReferenceGeometry
    escalation: tuple[CloseKind, ...]
    error: str
    families: tuple[PopupFamily, ...]
    note: str | None = None

    def by_family(self, family: str) -> PopupFamily:
        """Return one family by id, or raise ``KeyError``."""
        for candidate in self.families:
            if candidate.family == family:
                return candidate
        raise KeyError(f'unknown popup family: {family!r}')

    def chain_of(self, family: str) -> tuple[PopupFamily, ...]:
        """Return the families that closing ``family`` is expected to reveal."""
        return tuple(self.by_family(target) for target in self.by_family(family).chains_to)


def load_popup_corpus(path: str | Path, *, root: str | Path | None = None) -> PopupCorpus:
    """Read and validate one corpus file.

    ``path`` may be absolute or relative to the current directory. Template and
    fixture paths inside the YAML are repository-root relative; ``root`` names
    that root explicitly. When omitted, a file inside a directory named
    ``specs`` derives the root as its grandparent, and any other file derives
    it as its own directory.
    """
    corpus_path = Path(path)
    corpus_root = Path(root) if root is not None else _default_root(corpus_path)

    try:
        raw = yaml.safe_load(corpus_path.read_text(encoding='utf-8'))
    except FileNotFoundError as exc:
        raise PopupSpecError(f'popup corpus not found: {corpus_path}') from exc
    except OSError as exc:
        raise PopupSpecError(f'could not read popup corpus {corpus_path}: {exc}') from exc
    except UnicodeError as exc:
        raise PopupSpecError(f'popup corpus is not valid UTF-8: {corpus_path}') from exc
    except yaml.YAMLError as exc:
        raise PopupSpecError(f'invalid YAML in {corpus_path}: {exc}') from exc

    document = _mapping(raw, 'corpus')
    _reject_unknown(document, {'version', 'reference', 'policy', 'families'}, 'corpus')

    version = _positive_int(document.get('version'), 'version')
    reference = _reference(document.get('reference'))
    error, escalation, note = _policy(document.get('policy'))
    rows = _sequence(document.get('families'), 'families')
    if not rows:
        raise PopupSpecError('families must contain at least one row')

    families = tuple(_family(row, corpus_root, index, escalation) for index, row in enumerate(rows))
    _validate_chains(families)

    return PopupCorpus(
        version=version,
        reference=reference,
        escalation=escalation,
        error=error,
        families=families,
        note=note,
    )


def _default_root(path: Path) -> Path:
    parent = path.parent
    return parent.parent if parent.name == 'specs' else parent


def _reference(value: object) -> ReferenceGeometry:
    mapping = _mapping(value, 'reference')
    _reject_unknown(mapping, {'device', 'width', 'height', 'density', 'sdk', 'source'}, 'reference')
    return ReferenceGeometry(
        device=_text(mapping.get('device'), 'reference.device'),
        width=_positive_int(mapping.get('width'), 'reference.width'),
        height=_positive_int(mapping.get('height'), 'reference.height'),
        density=_positive_int(mapping.get('density'), 'reference.density'),
        sdk=_positive_int(mapping.get('sdk'), 'reference.sdk'),
        source=_text(mapping.get('source'), 'reference.source'),
    )


def _policy(value: object) -> tuple[str, tuple[CloseKind, ...], str | None]:
    mapping = _mapping(value, 'policy')
    _reject_unknown(mapping, {'escalation', 'on_exhausted', 'error', 'note'}, 'policy')

    raw_order = _sequence(mapping.get('escalation'), 'policy.escalation')
    order = tuple(_close_kind(item, 'policy.escalation') for item in raw_order)
    if len(order) != len(_REQUIRED_STRATEGIES) or set(order) != _REQUIRED_STRATEGIES:
        allowed = sorted(kind.value for kind in _REQUIRED_STRATEGIES)
        raise PopupSpecError(
            f'policy.escalation must be a permutation of {allowed!r}, got {[kind.value for kind in order]!r}',
        )
    if order[-1] is not CloseKind.FRACTION:
        raise PopupSpecError('policy.escalation must put fraction last, after label and template')

    exhausted = _text(mapping.get('on_exhausted'), 'policy.on_exhausted')
    if exhausted != 'fail':
        raise PopupSpecError("policy.on_exhausted must be 'fail'")
    error = _text(mapping.get('error'), 'policy.error')
    note = _optional_text(mapping.get('note'), 'policy.note')
    return error, order, note


def _family(value: object, root: Path, index: int, order: tuple[CloseKind, ...]) -> PopupFamily:
    where = f'families[{index}]'
    mapping = _mapping(value, where)
    _reject_unknown(
        mapping,
        {
            'family',
            'when',
            'anchor',
            'close',
            'status',
            'observed_at',
            'blocking',
            'chains_to',
            'fixture',
            'notes',
        },
        where,
    )

    family = _text(mapping.get('family'), f'{where}.family')
    if _FAMILY_PATTERN.fullmatch(family) is None:
        raise PopupSpecError(f'{where}.family must be snake_case, got {family!r}')

    status_raw = _text(mapping.get('status'), f'{where}.status')
    try:
        status = PopupStatus(status_raw)
    except ValueError as exc:
        raise PopupSpecError(f'{where}.status must be one of {[item.value for item in PopupStatus]!r}') from exc

    chains = _sequence(mapping.get('chains_to', []), f'{where}.chains_to')
    chains_to = tuple(_text(target, f'{where}.chains_to') for target in chains)
    if len(set(chains_to)) != len(chains_to):
        raise PopupSpecError(f'{where}.chains_to must not repeat a family')

    fixture = _optional_fixture(mapping.get('fixture'), f'{where}.fixture', root)

    return PopupFamily(
        family=family,
        when=_text(mapping.get('when'), f'{where}.when'),
        anchor=_text(mapping.get('anchor'), f'{where}.anchor'),
        close=_close(mapping.get('close'), f'{where}.close', root, order),
        status=status,
        observed_at=_observed_at(mapping.get('observed_at'), f'{where}.observed_at'),
        blocking=_boolean(mapping.get('blocking', True), f'{where}.blocking'),
        chains_to=chains_to,
        fixture=fixture,
        notes=_optional_text(mapping.get('notes'), f'{where}.notes'),
    )


def _close(value: object, where: str, root: Path, order: tuple[CloseKind, ...]) -> tuple[CloseAttempt, ...]:
    mapping = _mapping(value, where)
    _reject_unknown(mapping, {'template', 'source_width', 'label', 'fraction', 'fail'}, where)
    if not mapping:
        raise PopupSpecError(f'{where} must contain template, label, fraction or fail')

    if 'fail' in mapping:
        if len(mapping) != 1:
            raise PopupSpecError(f'{where}.fail must be the only key in close')
        return (CloseAttempt(kind=CloseKind.FAIL, reason=_text(mapping['fail'], f'{where}.fail')),)

    prepared: dict[CloseKind, CloseAttempt] = {}
    if 'template' in mapping:
        relative = _text(mapping['template'], f'{where}.template')
        if not relative.startswith(TEMPLATE_PREFIX):
            raise PopupSpecError(f'{where}.template must live under {TEMPLATE_PREFIX}')
        if not relative.endswith('.png'):
            raise PopupSpecError(f'{where}.template must be a .png')
        source_width = _positive_int(mapping.get('source_width'), f'{where}.source_width')
        path = _contained(root / relative, root, TEMPLATE_PREFIX, f'{where}.template')
        if not path.is_file():
            raise PopupSpecError(f'{where}.template does not exist: {path}')
        _check_png(path, f'{where}.template')
        prepared[CloseKind.TEMPLATE] = CloseAttempt(
            kind=CloseKind.TEMPLATE,
            template=path,
            source_width=source_width,
        )
    elif 'source_width' in mapping:
        raise PopupSpecError(f'{where}.source_width requires a template')

    if 'label' in mapping:
        prepared[CloseKind.LABEL] = CloseAttempt(
            kind=CloseKind.LABEL,
            label=_text(mapping['label'], f'{where}.label'),
        )

    if 'fraction' in mapping:
        prepared[CloseKind.FRACTION] = CloseAttempt(
            kind=CloseKind.FRACTION,
            fraction=_fraction(mapping['fraction'], where),
        )

    return tuple(prepared[kind] for kind in order if kind in prepared)


def _fraction(value: object, where: str) -> tuple[float, float]:
    pair = _sequence(value, f'{where}.fraction')
    if len(pair) != 2:
        raise PopupSpecError(f'{where}.fraction must contain exactly two numbers')
    numbers = tuple(_unit_number(item, f'{where}.fraction') for item in pair)
    return numbers[0], numbers[1]


def _unit_number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PopupSpecError(f'{where} must be a number, got {value!r}')
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise PopupSpecError(f'{where} must be between 0 and 1, got {number!r}')
    return number


def _observed_at(value: object, where: str) -> ObservedGeometry:
    mapping = _mapping(value, where)
    _reject_unknown(mapping, {'width', 'height', 'density', 'sdk'}, where)
    return ObservedGeometry(
        width=_positive_int(mapping.get('width'), f'{where}.width'),
        height=_positive_int(mapping.get('height'), f'{where}.height'),
        density=_positive_int(mapping.get('density'), f'{where}.density'),
        sdk=_positive_int(mapping.get('sdk'), f'{where}.sdk'),
    )


def _optional_fixture(value: object, where: str, root: Path) -> Path | None:
    if value is None:
        return None
    relative = _text(value, where)
    if not relative.startswith(FIXTURE_PREFIX) or not relative.endswith('.png'):
        raise PopupSpecError(f'{where} must be a .png under {FIXTURE_PREFIX}')
    path = _contained(root / relative, root, FIXTURE_PREFIX, where)
    if not path.is_file():
        raise PopupSpecError(f'{where} does not exist: {path}')
    _check_png(path, where)
    return path


def _contained(path: Path, root: Path, prefix: str, where: str) -> Path:
    """Resolve a reviewed directory path and refuse an escape via ``..``."""
    base = (root / prefix).resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise PopupSpecError(f'{where} must resolve under {prefix}') from exc
    return resolved


def _check_png(path: Path, where: str) -> None:
    try:
        with Image.open(path) as image:
            image.verify()
    except (OSError, SyntaxError, ValueError, UnidentifiedImageError) as exc:
        raise PopupSpecError(f'{where} is not a readable PNG: {path} ({exc})') from exc


def _validate_chains(families: tuple[PopupFamily, ...]) -> None:
    ids = [family.family for family in families]
    if len(set(ids)) != len(ids):
        duplicates = sorted({name for name in ids if ids.count(name) > 1})
        raise PopupSpecError(f'duplicate popup families: {duplicates!r}')
    known = set(ids)
    for family in families:
        for target in family.chains_to:
            if target == family.family:
                raise PopupSpecError(f'{family.family!r} cannot chain to itself')
            if target not in known:
                raise PopupSpecError(f'{family.family!r} chains to unknown family {target!r}')


def _close_kind(value: object, where: str) -> CloseKind:
    text = _text(value, where)
    try:
        return CloseKind(text)
    except ValueError as exc:
        raise PopupSpecError(f'{where} has an unknown close kind {text!r}') from exc


def _mapping(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PopupSpecError(f'{where} must be a mapping, got {type(value).__name__}')
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise PopupSpecError(f'{where} has a non-string key: {key!r}')
        result[key] = item
    return result


def _sequence(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        raise PopupSpecError(f'{where} must be a list, got {type(value).__name__}')
    return list(value)


def _text(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise PopupSpecError(f'{where} must be a string, got {type(value).__name__}')
    text = value.strip()
    if not text:
        raise PopupSpecError(f'{where} must not be empty')
    return text


def _optional_text(value: object, where: str) -> str | None:
    if value is None:
        return None
    return _text(value, where)


def _boolean(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise PopupSpecError(f'{where} must be a boolean, got {value!r}')
    return value


def _positive_int(value: object, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PopupSpecError(f'{where} must be an integer, got {value!r}')
    if value <= 0:
        raise PopupSpecError(f'{where} must be positive, got {value!r}')
    return value


def _reject_unknown(mapping: dict[str, object], allowed: set[str], where: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise PopupSpecError(f'{where} has unknown keys: {unknown!r}')
