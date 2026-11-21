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

The second half of the module is the dismissal loop (issue #18). It captures,
classifies, identifies the topmost anchored family, executes one declared
attempt, and captures again to verify. It is the only sweep a navigation or task
calls: :func:`dismiss_after` names the integration point and :func:`dismiss` is
the one loop behind it. A frame that is ``POPUP`` without an anchored family, or
an anchored blocking family with no executable attempt, raises the corpus's
named failure instead of falling through to a guessed coordinate.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml
from PIL import Image, UnidentifiedImageError

from aliexpress_coins.control.controller import LOGGER_NAME, Controller, Pixel, TapRecord
from aliexpress_coins.control.errors import PopupSpecError, PopupUnclosable
from aliexpress_coins.perception.matching import (
    DEFAULT_MIN_OCR_SCORE,
    DEFAULT_MIN_SCORE,
    find_all,
    find_text,
)
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.state import CaptureSource, Classification, ScreenState, classify
from aliexpress_coins.perception.templates import TemplateMatch, TemplateStore, load_templates

TEMPLATE_PREFIX = 'assets/templates/'
"""Templates are reviewed crops and live in exactly one directory."""

FIXTURE_PREFIX = 'tests/data/popups/'
"""Full-frame fixtures are reviewed captures and live in exactly one directory."""

_FAMILY_PATTERN = re.compile(r'[a-z][a-z0-9_]*')

DEFAULT_MAX_DISMISS_ROUNDS = 6
"""The most capture/tap/verify rounds one sweep may take.

A sweep normally needs one round per popup plus one final frame to verify the
page. Six covers the longest observed chain (launch modal -> bundle badge) and
still bounds a pair of families that re-open each other. A run loop may raise it
for a known-long onboarding chain, but it is never unbounded.
"""

DEFAULT_DISMISS_BUDGET_SECONDS = 30.0
"""Wall-clock budget for one sweep, independent of the round count.

The round bound alone is not enough on a slow device: a capture plus OCR can
take over a second. The budget stops a sweep that is making no useful progress
even when it has only spent a few rounds.
"""

REPEAT_TARGET_QUANTUM = 0.02
"""The repeated-target guard buckets a point to this fraction of the frame.

An animated or carousel promo can move its close control by a few pixels
between captures. Keying the guard on exact pixels would treat each rendering
as a new target and tap the same control repeatedly; a 2% bucket absorbs that
re-render drift while remaining far smaller than the distance between separate
controls.
"""

MAX_DISMISS_LABEL_WIDTH_FRACTION = 0.25
MAX_DISMISS_LABEL_HEIGHT_FRACTION = 0.10
"""A dismiss *label* must sit in a small box (docs/01-recon-findings.md §4).

The measured labels (`No thanks`, `Don't allow`) are under 10% of the frame
width. A 400 px-wide `Cancel order` button on the 1200 px reference frame is a
third of the width and must never be mistaken for a close control. Both bounds
are fractions of the current frame, so they scale with the device.
"""


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


class DismissOutcome(StrEnum):
    """What happened when a declared close attempt was considered."""

    EXECUTED = 'executed'
    NOT_FOUND = 'not_found'
    TOO_LARGE = 'too_large'
    NOT_APPLICABLE = 'not_applicable'
    REPEATED = 'repeated'
    UNEXECUTABLE = 'unexecutable'


class DismissStop(StrEnum):
    """Why one sweep ended."""

    CLEAR = 'clear'
    MAX_ROUNDS = 'max_rounds'
    BUDGET = 'budget'
    UNCLOSABLE = 'unclosable'
    DRY_RUN = 'dry_run'


@dataclass(frozen=True, slots=True)
class DismissFrame:
    """One captured frame used by the sweep, with what it was read as."""

    index: int
    snapshot: Snapshot
    classification: Classification
    family: str | None = None

    @property
    def state(self) -> ScreenState:
        """The classifier state for this frame."""
        return self.classification.state

    def as_dict(self) -> dict[str, object]:
        """The report-safe fields; the image itself belongs in the artifact store."""
        return {
            'frame': self.index,
            'state': self.state.value,
            'score': self.classification.score,
            'family': self.family,
            'evidence': [item.signal for item in self.classification.evidence],
        }


@dataclass(frozen=True, slots=True)
class DismissAttempt:
    """One close attempt, including the ones that were skipped and why."""

    round: int
    family: str
    kind: CloseKind
    outcome: DismissOutcome
    label: str | None = None
    score: float | None = None
    target: Pixel | None = None
    box: tuple[float, float, float, float] | None = None
    tap: TapRecord | None = None
    detail: str | None = None

    @property
    def executed(self) -> bool:
        """Whether this attempt reached the controller (a dry run still counts)."""
        return self.outcome is DismissOutcome.EXECUTED

    def as_dict(self) -> dict[str, object]:
        """Render the attempt for the run report."""
        record: dict[str, object] = {
            'round': self.round,
            'family': self.family,
            'kind': self.kind.value,
            'outcome': self.outcome.value,
            'label': self.label,
            'score': self.score,
            'target': list(self.target) if self.target is not None else None,
            'box': list(self.box) if self.box is not None else None,
            'detail': self.detail,
        }
        if self.tap is not None:
            record['tap'] = self.tap.as_dict()
        return record


@dataclass(frozen=True, slots=True)
class DismissReport:
    """What one popup sweep closed, tried, saw and left behind.

    ``closed`` is ordered by when a family was verified gone, so a chain reads
    top-to-bottom. ``remaining`` is true when the last frame still held a known
    family or classified as a popup. ``stop`` distinguishes a clean sweep from
    a bounded chain or a family that could not be closed.
    """

    closed: tuple[str, ...]
    attempts: tuple[DismissAttempt, ...]
    frames: tuple[DismissFrame, ...]
    remaining: bool
    stop: DismissStop
    final: Classification | None = None
    dry_run: bool = False
    unclosed: tuple[str, ...] = ()

    @property
    def dismissed(self) -> tuple[str, ...]:
        """Alias for :attr:`closed`, for report vocabulary."""
        return self.closed

    @property
    def bounded(self) -> bool:
        """Whether the sweep ended on the round or wall-clock bound."""
        return self.stop in (DismissStop.MAX_ROUNDS, DismissStop.BUDGET)

    @property
    def final_state(self) -> ScreenState | None:
        """The state of the last frame, if one was captured."""
        return self.final.state if self.final is not None else None

    @property
    def taps(self) -> tuple[DismissAttempt, ...]:
        """The attempts that reached the controller (including dry-run taps)."""
        return tuple(attempt for attempt in self.attempts if attempt.executed)

    def as_dict(self) -> dict[str, object]:
        """Render the whole sweep for ``run.json``."""
        return {
            'closed': list(self.closed),
            'remaining': self.remaining,
            'stop': self.stop.value,
            'dry_run': self.dry_run,
            'unclosed': list(self.unclosed),
            'final_state': self.final_state.value if self.final_state is not None else None,
            'frames': [frame.as_dict() for frame in self.frames],
            'attempts': [attempt.as_dict() for attempt in self.attempts],
        }


@dataclass(frozen=True, slots=True)
class _Target:
    """An executable close target resolved from one declared attempt."""

    kind: CloseKind
    point: tuple[float, float]
    label: str | None = None
    score: float | None = None
    box: TextBox | None = None
    detail: str | None = None

    @property
    def key(self) -> Pixel:
        """The pixel the controller would aim at, before clamping/jitter."""
        return round(self.point[0]), round(self.point[1])


def _guard_key(point: tuple[float, float], snapshot: Snapshot) -> tuple[int, int]:
    """Bucket a point so a small re-render shift is still the same target."""
    return (
        round(point[0] / snapshot.width / REPEAT_TARGET_QUANTUM),
        round(point[1] / snapshot.height / REPEAT_TARGET_QUANTUM),
    )


def dismiss_after(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    *,
    templates: TemplateStore | None = None,
    max_rounds: int = DEFAULT_MAX_DISMISS_ROUNDS,
    budget: float = DEFAULT_DISMISS_BUDGET_SECONDS,
    settle: float = 0.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    logger: logging.Logger | None = None,
) -> DismissReport:
    """Sweep popups after one navigation or task.

    This is the integration point the run loop calls — after every navigation
    and after every task — never a task itself. It is a thin name over
    :func:`dismiss` so the run loop, the manual run and any future runner all
    invoke the same loop rather than growing a second one.

    ``capture`` is the seam used for every frame, including the first: pass the
    run's snapshotter or a callable, not one already-captured frame. The sweep
    deliberately takes the post-navigation frame itself so the caller cannot
    accidentally hand it a stale snapshot.
    """
    return dismiss(
        controller,
        capture,
        corpus,
        templates=templates,
        max_rounds=max_rounds,
        budget=budget,
        settle=settle,
        clock=clock,
        sleep=sleep,
        logger=logger,
    )


def dismiss(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    *,
    templates: TemplateStore | None = None,
    max_rounds: int = DEFAULT_MAX_DISMISS_ROUNDS,
    budget: float = DEFAULT_DISMISS_BUDGET_SECONDS,
    settle: float = 0.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    logger: logging.Logger | None = None,
) -> DismissReport:
    """Capture, identify one anchored family, close it, and verify the result.

    The loop never taps a guessed coordinate. A family is only considered when
    its own anchor matched the frame; a ``POPUP`` frame with no matching family
    raises :class:`~aliexpress_coins.control.errors.PopupUnclosable` before any
    input is sent. Close attempts are taken from the corpus in policy order.

    A fraction is a last resort and is never used on a frame that carries a
    recognised page marker. That matters for families whose anchor is also a
    real page marker (``Coins boost``): the boost page itself must not be tapped
    at a remembered modal coordinate. A ``POPUP`` or unmodelled overlay may use
    the configured fallback once its family anchor has matched.

    ``settle`` is injected like the clock; a live caller can wait for the UI to
    react, while a fixture-driven test passes zero. The loop is bounded by both
    ``max_rounds`` and ``budget`` so a pair of families that re-open each other
    stops with a report instead of spinning.
    """
    if max_rounds < 1:
        raise ValueError(f'max_rounds must be positive, got {max_rounds!r}')
    if budget <= 0:
        raise ValueError(f'budget must be positive, got {budget!r}')
    if settle < 0:
        raise ValueError(f'settle must not be negative, got {settle!r}')

    log = logger if logger is not None else logging.getLogger(LOGGER_NAME)
    has_template = any(attempt.kind is CloseKind.TEMPLATE for family in corpus.families for attempt in family.close)
    store = templates
    if store is None and has_template:
        store = load_templates(corpus)

    started = clock()
    frames: list[DismissFrame] = []
    attempts: list[DismissAttempt] = []
    closed: list[str] = []
    active_family: str | None = None
    active_targets: set[tuple[int, int]] = set()
    executed_families: set[str] = set()
    pending: str | None = None
    current_family: str | None = None
    final: Classification | None = None
    stop = DismissStop.CLEAR
    round_index = 0

    while True:
        if round_index >= max_rounds:
            stop = DismissStop.MAX_ROUNDS
            break
        if round_index > 0 and clock() - started >= budget:
            stop = DismissStop.BUDGET
            break

        snapshot = _capture(capture)
        classification = classify(snapshot)
        family = _matching_family(snapshot, corpus)
        round_index += 1
        current_family = family.family if family is not None else None
        final = classification
        frames.append(
            DismissFrame(
                index=round_index,
                snapshot=snapshot,
                classification=classification,
                family=current_family,
            ),
        )

        if pending is not None and current_family != pending:
            closed.append(pending)
            pending = None

        if family is None:
            if classification.state is ScreenState.POPUP:
                message = (
                    f'popup_unclosable: frame {round_index} is POPUP ({classification.describe()}) '
                    'but no corpus family anchor matched; no tap was issued'
                )
                _warn(log, 'popup_unclosable', family=None, round=round_index, detail=message)
                raise PopupUnclosable(message)
            stop = DismissStop.CLEAR
            break

        if family.family != active_family:
            active_family = family.family
            active_targets.clear()

        target, considered = _select_target(
            snapshot,
            classification,
            family,
            store,
            round_index=round_index,
            active_targets=active_targets,
        )
        attempts.extend(considered)

        if target is None:
            detail = _no_target_detail(family, classification, considered)
            never_tapped = family.family not in executed_families
            if family.blocking and classification.state is ScreenState.POPUP and never_tapped:
                message = f'{corpus.error}: {detail}'
                _warn(log, 'popup_unclosable', family=family.family, round=round_index, detail=detail)
                raise PopupUnclosable(message)
            stop = DismissStop.UNCLOSABLE
            _warn(log, 'popup_not_closed', family=family.family, round=round_index, detail=detail)
            break

        tap = _execute_target(controller, family, target)
        executed_families.add(family.family)
        attempts.append(
            DismissAttempt(
                round=round_index,
                family=family.family,
                kind=target.kind,
                outcome=DismissOutcome.EXECUTED,
                label=target.label,
                score=target.score,
                target=target.key,
                box=target.box.bbox if target.box is not None else None,
                tap=tap,
                detail=target.detail,
            ),
        )
        active_targets.add(_guard_key(target.point, snapshot))

        if controller.dry_run:
            stop = DismissStop.DRY_RUN
            break
        pending = family.family
        if settle:
            sleep(settle)

    remaining_popup = final is not None and final.state is ScreenState.POPUP
    remaining = current_family is not None or remaining_popup
    unclosed: tuple[str, ...] = (current_family,) if current_family is not None else ()

    report = DismissReport(
        closed=tuple(closed),
        attempts=tuple(attempts),
        frames=tuple(frames),
        remaining=remaining,
        stop=stop,
        final=final,
        dry_run=controller.dry_run,
        unclosed=unclosed,
    )
    if report.bounded or (report.remaining and report.stop is DismissStop.UNCLOSABLE):
        _warn(
            log,
            'popup_sweep_stopped',
            family=unclosed[0] if unclosed else None,
            round=len(frames),
            detail=report.stop.value,
        )
    return report


def _matching_family(snapshot: Snapshot, corpus: PopupCorpus) -> PopupFamily | None:
    """Return the first corpus family whose anchor matched, topmost first."""
    for family in corpus.families:
        if _anchor_matched(snapshot, family.anchor):
            return family
    return None


def _anchor_matched(snapshot: Snapshot, anchor: str) -> bool:
    """Match an anchor even when campaign artwork glues OCR words together.

    The launch modal's headline is read as ``SEASOWelcome-giftSDAY`` on the
    reviewed fixture: the words are there, but the art on either side and the
    hyphen break a normal label comparison. The fallback compares only letters
    and digits, so the anchor must still appear as a contiguous run; it does not
    match an unrelated headline or turn into a bare coordinate tap.
    """
    found = find_text(
        snapshot,
        anchor,
        min_score=DEFAULT_MIN_SCORE,
        min_ocr_score=DEFAULT_MIN_OCR_SCORE,
    )
    if found is not None:
        return True

    needle = _compact(anchor)
    if len(needle) < 4:
        return False
    return any(needle in _compact(box.text) for box in snapshot.boxes if box.score >= DEFAULT_MIN_OCR_SCORE)


def _compact(text: str) -> str:
    return ''.join(character for character in text.casefold() if character.isalnum())


def _select_target(
    snapshot: Snapshot,
    classification: Classification,
    family: PopupFamily,
    store: TemplateStore | None,
    *,
    round_index: int,
    active_targets: set[tuple[int, int]],
) -> tuple[_Target | None, tuple[DismissAttempt, ...]]:
    """Resolve the first executable attempt in the declared policy order."""
    considered: list[DismissAttempt] = []
    for attempt in family.close:
        if attempt.kind is CloseKind.FAIL:
            considered.append(
                DismissAttempt(
                    round=round_index,
                    family=family.family,
                    kind=attempt.kind,
                    outcome=DismissOutcome.UNEXECUTABLE,
                    detail=attempt.reason,
                ),
            )
            continue

        if attempt.kind is CloseKind.LABEL:
            target, record = _label_target(snapshot, family, attempt, round_index)
            if target is None:
                if record is not None:
                    considered.append(record)
                continue
        elif attempt.kind is CloseKind.TEMPLATE:
            target, record = _template_target(snapshot, family, store, round_index)
            if target is None:
                if record is not None:
                    considered.append(record)
                continue
        elif attempt.kind is CloseKind.FRACTION:
            if classification.state not in (ScreenState.POPUP, ScreenState.UNKNOWN):
                considered.append(
                    DismissAttempt(
                        round=round_index,
                        family=family.family,
                        kind=attempt.kind,
                        outcome=DismissOutcome.NOT_APPLICABLE,
                        detail='fraction fallback is not usable on a recognised page or a dead frame',
                    ),
                )
                continue
            fraction = attempt.fraction
            if fraction is None:  # pragma: no cover - the loader rejects this
                continue
            target = _Target(
                kind=attempt.kind,
                point=(fraction[0] * snapshot.width, fraction[1] * snapshot.height),
                detail='configured fraction after the family anchor matched',
            )
        else:  # pragma: no cover - CloseKind is closed and validated at load time
            continue

        if _guard_key(target.point, snapshot) in active_targets:
            considered.append(
                DismissAttempt(
                    round=round_index,
                    family=family.family,
                    kind=target.kind,
                    outcome=DismissOutcome.REPEATED,
                    label=target.label,
                    score=target.score,
                    target=target.key,
                    box=target.box.bbox if target.box is not None else None,
                    detail='the same target was already tapped while this family stayed on screen',
                ),
            )
            continue
        return target, tuple(considered)
    return None, tuple(considered)


def _label_target(
    snapshot: Snapshot,
    family: PopupFamily,
    attempt: CloseAttempt,
    round_index: int,
) -> tuple[_Target | None, DismissAttempt | None]:
    label = attempt.label
    if label is None:  # pragma: no cover - the loader guarantees a label
        return None, DismissAttempt(
            round=round_index,
            family=family.family,
            kind=attempt.kind,
            outcome=DismissOutcome.NOT_FOUND,
            detail='label attempt has no label',
        )

    matches = find_all(
        snapshot,
        label,
        min_score=DEFAULT_MIN_SCORE,
        min_ocr_score=DEFAULT_MIN_OCR_SCORE,
    )
    small = [match for match in matches if _is_small_label(match.box, snapshot)]
    if not small:
        outcome = DismissOutcome.TOO_LARGE if matches else DismissOutcome.NOT_FOUND
        detail = (
            f'label {label!r} only matched boxes larger than '
            f'{MAX_DISMISS_LABEL_WIDTH_FRACTION:.0%} width / {MAX_DISMISS_LABEL_HEIGHT_FRACTION:.0%} height'
            if matches
            else f'label {label!r} is not in the frame in a small box'
        )
        return None, DismissAttempt(
            round=round_index,
            family=family.family,
            kind=attempt.kind,
            outcome=outcome,
            label=label,
            detail=detail,
        )

    best = max(small, key=lambda match: (match.score, match.ocr_score))
    return (
        _Target(
            kind=attempt.kind,
            point=best.center,
            label=label,
            score=best.score,
            box=best.box,
            detail=f'label {best.text!r} in a small box',
        ),
        None,
    )


def _template_target(
    snapshot: Snapshot,
    family: PopupFamily,
    store: TemplateStore | None,
    round_index: int,
) -> tuple[_Target | None, DismissAttempt | None]:
    if store is None:  # pragma: no cover - dismiss loads the store when a row needs one
        return None, DismissAttempt(
            round=round_index,
            family=family.family,
            kind=CloseKind.TEMPLATE,
            outcome=DismissOutcome.NOT_FOUND,
            detail='no template store is available',
        )

    match = store.match(snapshot, family.family)
    if match is None:
        return None, DismissAttempt(
            round=round_index,
            family=family.family,
            kind=CloseKind.TEMPLATE,
            outcome=DismissOutcome.NOT_FOUND,
            detail=f'no glyph matched the {family.family!r} crop',
        )
    return _from_template_match(family, match)


def _from_template_match(family: PopupFamily, match: TemplateMatch) -> tuple[_Target, None]:
    box = TextBox(
        text=family.family,
        box=(
            (float(match.box[0]), float(match.box[1])),
            (float(match.box[2]), float(match.box[1])),
            (float(match.box[2]), float(match.box[3])),
            (float(match.box[0]), float(match.box[3])),
        ),
        score=match.score,
    )
    target = _Target(
        kind=CloseKind.TEMPLATE,
        point=match.center,
        score=match.score,
        box=box,
        detail=f'glyph {family.family!r} at scale {match.scale:.3f}',
    )
    return target, None


def _execute_target(controller: Controller, family: PopupFamily, target: _Target) -> TapRecord:
    why = f'close popup {family.family} via {target.kind.value}'
    if target.box is not None:
        return controller.tap_box(target.box, why=why, label=target.label or family.family)
    return controller.tap(target.point, why=why, label=target.label or family.family)


def _is_small_label(box: TextBox, snapshot: Snapshot) -> bool:
    return (
        box.width <= snapshot.width * MAX_DISMISS_LABEL_WIDTH_FRACTION
        and box.height <= snapshot.height * MAX_DISMISS_LABEL_HEIGHT_FRACTION
    )


def _no_target_detail(
    family: PopupFamily,
    classification: Classification,
    considered: tuple[DismissAttempt, ...],
) -> str:
    reasons = (
        ', '.join(f'{attempt.kind.value}:{attempt.outcome.value}' for attempt in considered) or 'no close attempts'
    )
    return (
        f'popup {family.family!r} matched its anchor but no close attempt was executable '
        f'({reasons}; frame={classification.state.value})'
    )


def _capture(source: CaptureSource) -> Snapshot:
    if callable(source):
        return source()
    return source.capture()


def _warn(logger: logging.Logger, event: str, **fields: object) -> None:
    logger.warning(
        '%s',
        json.dumps({'event': event, **fields}, separators=(',', ':'), sort_keys=True),
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
