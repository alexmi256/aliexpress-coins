"""Open the task list and turn the visible rows into an ordered roster (issue #22).

Discovery has two routes and records which one it took:

* **boost** — ``Earn more`` on the coin page opens the ``Coins boost`` page. The
  page is the fuller list, so it is preferred whenever the label is present.
* **inline** — when ``Earn more`` is absent, the task rows already rendered on
  the coin page are used instead. Missing ``Earn more`` is a normal page
  variant, not a failure.

The roster is a set of *decisions*, not a filtered list. Every task row that has
an action is kept, with the catalogue's allow/deny answer and its reason. A
denied row still records the action it was rendered with for diagnostics, but
:attr:`RosterEntry.dispatch_target` is ``None`` so an executor cannot be handed
a target for it.

The empty ``Coins boost`` page (``There's nothing here yet``) is an arrival, not
a timeout: :class:`Roster` reports it as ``nothing available``. Reset policy for
that state belongs to #38; discovery only has to describe what it saw.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from aliexpress_coins.control.controller import Controller, TapRecord
from aliexpress_coins.control.popups import DismissReport, PopupCorpus, _matching_family, dismiss_after
from aliexpress_coins.perception.errors import StateTimeout
from aliexpress_coins.perception.layout import (
    DEFAULT_ACTION_LABELS,
    ProgressBadge,
    Row,
    find_action_in_row,
    find_progress_badge,
    group_rows,
    parse_progress_badge,
)
from aliexpress_coins.perception.matching import (
    DEFAULT_MIN_OCR_SCORE,
    DEFAULT_MIN_SCORE,
    find_text,
    normalize,
    similarity,
)
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.state import (
    CaptureSource,
    Classification,
    Observation,
    ScreenState,
    classify,
)
from aliexpress_coins.perception.templates import TemplateStore
from aliexpress_coins.tasks.catalogue import (
    DEFAULT_DENY_REASON,
    AllowedTaskSpec,
    DecisionKind,
    TaskCatalogue,
    TaskCategory,
    TaskDecision,
)

EARN_MORE_LABEL = 'Earn more'
"""The control that opens the full task list from the coin page."""

BOOST_MARKER = 'Coins boost'
"""The header shared by the populated boost page and its empty state."""

EMPTY_BOOST_MARKER = "There's nothing here yet"
"""The sentence the app renders when the boost list failed to populate."""

DEFAULT_BOOST_TIMEOUT = 30.0
"""How long to wait for the boost destination after tapping ``Earn more``."""

DEFAULT_BOOST_INTERVAL = 1.0
"""How often to capture while waiting for the boost destination."""

_REWARD = re.compile(r'^\s*[+\uff0b]\s*\d')
_REWARD_WORDS = frozenset({'coin', 'coins'})


class DiscoveryRoute(StrEnum):
    """Which surface the roster came from."""

    BOOST = 'boost'
    INLINE = 'inline'


class RosterStatus(StrEnum):
    """Whether the roster contains anything worth dispatching."""

    AVAILABLE = 'available'
    NOTHING_AVAILABLE = 'nothing available'


@dataclass(frozen=True, slots=True)
class RosterEntry:
    """One task row and the policy decision attached to it.

    ``action`` is the row's own control box. ``dispatch_target`` is deliberately
    the only target an executor should read: it is ``None`` for a denied entry,
    so a refused row cannot be tapped by accident.
    """

    title: str
    subtitle: str | None
    action: TextBox | None
    progress: ProgressBadge | None
    decision: TaskDecision
    row: Row

    @property
    def allowed(self) -> bool:
        """Whether the catalogue allows this row."""
        return self.decision.allowed

    @property
    def denied(self) -> bool:
        """Whether the catalogue refused this row."""
        return self.decision.denied

    @property
    def kind(self) -> DecisionKind:
        """The allow/deny decision for this row."""
        return self.decision.decision

    @property
    def key(self) -> str | None:
        """The allowed catalogue key, or ``None`` when refused."""
        return self.decision.spec.key if self.decision.spec is not None else None

    @property
    def category(self) -> TaskCategory | None:
        """The allowed task category, or ``None`` when refused."""
        return self.decision.spec.category if self.decision.spec is not None else None

    @property
    def spec(self) -> AllowedTaskSpec | None:
        """The allowed task spec, or ``None`` when refused."""
        return self.decision.spec

    @property
    def rule(self) -> str | None:
        """The catalogue rule that matched, including the default-deny miss."""
        return self.decision.rule

    @property
    def reason(self) -> str | None:
        """The refusal reason, or ``None`` when the row is allowed."""
        return self.decision.reason

    @property
    def deny_reason(self) -> str | None:
        """Alias for :attr:`reason`, in the catalogue's vocabulary."""
        return self.decision.deny_reason

    @property
    def action_target(self) -> TextBox | None:
        """The rendered action box, kept even for a refused row."""
        return self.action

    @property
    def dispatch_target(self) -> TextBox | None:
        """The action an executor may tap: never a refused row's control."""
        return self.action if self.allowed else None

    @property
    def dispatchable(self) -> bool:
        """Whether this row is both allowed and has an action to tap."""
        return self.allowed and self.action is not None

    @property
    def progress_text(self) -> str | None:
        """The progress badge exactly as OCR read it, if present."""
        return self.progress.text if self.progress is not None else None

    def as_dict(self) -> dict[str, object]:
        """Render the row for ``run.json`` without losing its decision."""
        record = self.decision.as_dict()
        record.update(
            {
                'row': self.row.text,
                'progress': (
                    {
                        'text': self.progress.text,
                        'completed': self.progress.completed,
                        'total': self.progress.total,
                    }
                    if self.progress is not None
                    else None
                ),
                'action': (
                    {
                        'text': self.action.text,
                        'box': list(self.action.bbox),
                        'target': list(self.action.center),
                    }
                    if self.action is not None
                    else None
                ),
                'dispatchable': self.dispatchable,
                'dispatch_target': list(self.dispatch_target.center) if self.dispatch_target is not None else None,
            },
        )
        return record


@dataclass(frozen=True, slots=True)
class Roster:
    """The ordered result of reading one task surface."""

    entries: tuple[RosterEntry, ...]
    route: DiscoveryRoute | None = None
    catalogue_version: int | None = None
    default_deny_reason: str = DEFAULT_DENY_REASON

    def __post_init__(self) -> None:
        object.__setattr__(self, 'entries', tuple(self.entries))

    @property
    def tasks(self) -> tuple[RosterEntry, ...]:
        """Alias for :attr:`entries`."""
        return self.entries

    @property
    def count(self) -> int:
        """How many task rows the surface showed."""
        return len(self.entries)

    @property
    def empty(self) -> bool:
        """Whether no task row was found."""
        return not self.entries

    @property
    def nothing_available(self) -> bool:
        """The report wording for an empty surface, including the empty boost page."""
        return not self.entries

    @property
    def status(self) -> RosterStatus:
        """``available`` or ``nothing available``, ready for a report."""
        return RosterStatus.NOTHING_AVAILABLE if self.nothing_available else RosterStatus.AVAILABLE

    @property
    def allowed_entries(self) -> tuple[RosterEntry, ...]:
        """Every row the catalogue allows, in reading order."""
        return tuple(entry for entry in self.entries if entry.allowed)

    @property
    def denied_entries(self) -> tuple[RosterEntry, ...]:
        """Every refused or unrecognised row, in reading order."""
        return tuple(entry for entry in self.entries if entry.denied)

    @property
    def allowed(self) -> tuple[RosterEntry, ...]:
        """Alias for :attr:`allowed_entries`."""
        return self.allowed_entries

    @property
    def denied(self) -> tuple[RosterEntry, ...]:
        """Alias for :attr:`denied_entries`."""
        return self.denied_entries

    @property
    def titles(self) -> tuple[str, ...]:
        """The row titles in reading order."""
        return tuple(entry.title for entry in self.entries)

    @property
    def message(self) -> str:
        """The one-line status a report can print."""
        return self.status.value

    def as_dict(self) -> dict[str, object]:
        """Render the whole catalogue snapshot for the run report."""
        return {
            'route': self.route.value if self.route is not None else None,
            'status': self.status.value,
            'count': self.count,
            'allowed_count': len(self.allowed_entries),
            'denied_count': len(self.denied_entries),
            'nothing_available': self.nothing_available,
            'catalogue': {
                'version': self.catalogue_version,
                'default_deny_reason': self.default_deny_reason,
            },
            'entries': [entry.as_dict() for entry in self.entries],
        }


@dataclass(frozen=True, slots=True)
class BoostList:
    """The surface ``open_boost_list`` arrived at, and how it got there."""

    snapshot: Snapshot
    route: DiscoveryRoute
    detail: str
    classification: Classification | None = None
    tap: TapRecord | None = None
    dismiss: DismissReport | None = None
    elapsed: float = 0.0
    history: tuple[ScreenState, ...] = ()

    @property
    def opened(self) -> bool:
        """Whether the route is the full boost page."""
        return self.route is DiscoveryRoute.BOOST

    @property
    def inline(self) -> bool:
        """Whether discovery degraded to the coin page's inline rows."""
        return self.route is DiscoveryRoute.INLINE

    @property
    def empty(self) -> bool:
        """Whether the boost page is the honest-looking empty state."""
        return is_empty_boost(self.snapshot)

    @property
    def status(self) -> RosterStatus:
        """The open result's status, using the same wording as a roster."""
        return RosterStatus.NOTHING_AVAILABLE if self.empty else RosterStatus.AVAILABLE

    def as_dict(self) -> dict[str, object]:
        """Render the route decision for the run report."""
        record: dict[str, object] = {
            'route': self.route.value,
            'detail': self.detail,
            'state': self.classification.state.value if self.classification is not None else None,
            'empty': self.empty,
            'elapsed': self.elapsed,
            'history': [state.value for state in self.history],
        }
        if self.tap is not None:
            record['tap'] = self.tap.as_dict()
        if self.dismiss is not None:
            record['dismiss'] = self.dismiss.as_dict()
        return record


# Backwards-readable aliases for callers that name the result by its purpose.
BoostOpenResult = BoostList
DiscoveryResult = BoostList


def open_boost_list(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    *,
    templates: TemplateStore | None = None,
    timeout: float = DEFAULT_BOOST_TIMEOUT,
    interval: float = DEFAULT_BOOST_INTERVAL,
    settle: float = 0.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    logger: logging.Logger | None = None,
) -> BoostList:
    """Open ``Earn more`` and wait for the ``Coins boost`` destination.

    The initial frame is the coin page. If ``Earn more`` is not there after the
    page has stopped loading and any popup has been swept, the same frame is
    returned with :attr:`DiscoveryRoute.INLINE`; no tap is issued and no
    exception is raised. Otherwise the label is tapped through
    :class:`Controller`, any popup seen on the way is closed with
    :func:`~aliexpress_coins.control.popups.dismiss_after`, and capture is
    polled until the boost marker and the same non-empty task set are visible
    on two consecutive frames (or the empty sentence is visible).

    A frame that already contains the boost destination is *not* swept as a
    popup, and the sweep after a real popup runs with the ``coins_boost_sheet``
    family removed. That family's anchor is the destination's own
    ``Coins boost`` header, so sweeping it would close the list it was meant to
    open (including immediately after a different popup is dismissed). A
    genuine popup still classifies as ``POPUP`` and is swept normally, even
    when the header and some rows are still OCR-visible behind it. A
    destination-shaped ``POPUP`` frame with no other family is treated as the
    list rather than as an unclosable modal.
    """
    if timeout <= 0:
        raise ValueError('timeout must be positive')
    if interval <= 0:
        raise ValueError('interval must be positive')
    if settle < 0:
        raise ValueError('settle must not be negative')

    started = clock()
    deadline = started + timeout
    dismiss: DismissReport | None = None

    initial = _capture(capture)
    initial_classification = classify(initial)
    earn_more = find_text(initial, EARN_MORE_LABEL)

    # A loading coin page is not proof that the label is absent, and tapping
    # through a popup would land on the scrim even when OCR can still see the
    # label underneath it. Resolve those states first; a settled page with no
    # label is a real page variant and degrades immediately.
    while initial_classification.state in (ScreenState.LOADING, ScreenState.POPUP):
        now = clock()
        if now >= deadline:
            break
        if initial_classification.state is ScreenState.POPUP:
            dismiss = dismiss_after(
                controller,
                capture,
                corpus,
                templates=templates,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=logger,
            )
        else:
            sleep(min(interval, max(0.0, deadline - now)))
        initial = _capture(capture)
        initial_classification = classify(initial)
        earn_more = find_text(initial, EARN_MORE_LABEL)

    if initial_classification.state is ScreenState.POPUP:
        raise StateTimeout(
            f'an initial popup still covers the coin page after {timeout:g}s; '
            f'no tap was issued for {EARN_MORE_LABEL!r}',
        )

    if earn_more is None:
        return BoostList(
            snapshot=initial,
            route=DiscoveryRoute.INLINE,
            detail=f'{EARN_MORE_LABEL!r} is absent; using the inline task rows on the coin page',
            classification=initial_classification,
            dismiss=dismiss,
        )

    sweep_corpus = _without_destination_family(corpus)
    tap = controller.tap_box(
        earn_more.box,
        why=f'open the {BOOST_MARKER} task list',
        label=EARN_MORE_LABEL,
    )

    observations: list[Observation] = []
    previous_signature: tuple[tuple[str, str, tuple[int, int] | None], ...] | None = None

    while True:
        snapshot = _capture(capture)
        classification = classify(snapshot)
        now = clock()
        observations.append(
            Observation(elapsed=max(0.0, now - started), classification=classification),
        )

        # A modal over an already-rendered list still hides the rows beneath it.
        # Sweep it first; the destination family is already excluded so a real
        # popup cannot be mistaken for the list itself. A destination-shaped
        # POPUP frame with no other family is the list, not an unclosable modal.
        if classification.state is ScreenState.POPUP and not (
            _boost_arrived(snapshot) and _matching_family(snapshot, sweep_corpus) is None
        ):
            if now >= deadline:
                seen = ' -> '.join(item.classification.state.value for item in observations)
                raise StateTimeout(
                    f'{BOOST_MARKER!r} was not observed within {timeout:g}s after tapping '
                    f'{EARN_MORE_LABEL!r} (last={classification.describe()}; history={seen})',
                )
            dismiss = dismiss_after(
                controller,
                capture,
                sweep_corpus,
                templates=templates,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=logger,
            )
            previous_signature = None
            continue

        if _boost_arrived(snapshot):
            if is_empty_boost(snapshot):
                return BoostList(
                    snapshot=snapshot,
                    route=DiscoveryRoute.BOOST,
                    detail=_arrival_detail(snapshot),
                    classification=classification,
                    tap=tap,
                    dismiss=dismiss,
                    elapsed=max(0.0, now - started),
                    history=tuple(item.classification.state for item in observations),
                )

            signature = _task_signature(snapshot)
            if not signature or signature == previous_signature:
                return BoostList(
                    snapshot=snapshot,
                    route=DiscoveryRoute.BOOST,
                    detail=_arrival_detail(snapshot),
                    classification=classification,
                    tap=tap,
                    dismiss=dismiss,
                    elapsed=max(0.0, now - started),
                    history=tuple(item.classification.state for item in observations),
                )
            previous_signature = signature

        if now >= deadline:
            seen = ' -> '.join(item.classification.state.value for item in observations)
            raise StateTimeout(
                f'{BOOST_MARKER!r} was not observed within {timeout:g}s after tapping '
                f'{EARN_MORE_LABEL!r} (last={classification.describe()}; history={seen})',
            )

        sleep(min(interval, max(0.0, deadline - now)))


def enumerate_roster(
    snapshot: Snapshot,
    catalogue: TaskCatalogue,
    *,
    route: DiscoveryRoute | None = None,
    action_labels: Sequence[str] = DEFAULT_ACTION_LABELS,
) -> Roster:
    """Read every action-bearing task row in ``snapshot`` in reading order.

    A row is a task row when :func:`find_action_in_row` finds its control. The
    title is the first meaningful label above that control; progress badges,
    rewards and the action itself are not part of the title or subtitle.
    Duplicate titles are kept once, so a row that appears twice in one frame is
    not dispatched twice.

    The policy is *not* used to filter: denied and unrecognised rows remain in
    the roster with their reason. Only an allowed row exposes
    :attr:`RosterEntry.dispatch_target`.
    """
    entries: list[RosterEntry] = []
    seen: set[str] = set()

    for row in group_rows(snapshot.boxes, snapshot.geometry):
        action = find_action_in_row(row, action_labels)
        if action is None:
            continue
        title, subtitle = _title_and_subtitle(row, action, action_labels)
        if title is None:
            continue
        identity = normalize(title)
        if not identity or identity in seen:
            continue
        seen.add(identity)

        decision = catalogue.decide(title, subtitle=subtitle)
        entries.append(
            RosterEntry(
                title=title,
                subtitle=subtitle,
                action=action,
                progress=find_progress_badge(row),
                decision=decision,
                row=row,
            ),
        )

    return Roster(
        entries=tuple(entries),
        route=route,
        catalogue_version=catalogue.version,
        default_deny_reason=catalogue.default_deny_reason,
    )


def discover_roster(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    catalogue: TaskCatalogue,
    *,
    templates: TemplateStore | None = None,
    timeout: float = DEFAULT_BOOST_TIMEOUT,
    interval: float = DEFAULT_BOOST_INTERVAL,
    settle: float = 0.0,
    action_labels: Sequence[str] = DEFAULT_ACTION_LABELS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    logger: logging.Logger | None = None,
) -> Roster:
    """Open the best available surface and enumerate it in one call.

    This is the integration point for the run loop and #38: it returns a roster
    whose :attr:`Roster.route` records ``boost`` or ``inline``. The caller
    decides whether an empty roster is a reset (`#38`) or the end of the run.
    """
    opened = open_boost_list(
        controller,
        capture,
        corpus,
        templates=templates,
        timeout=timeout,
        interval=interval,
        settle=settle,
        clock=clock,
        sleep=sleep,
        logger=logger,
    )
    return enumerate_roster(
        opened.snapshot,
        catalogue,
        route=opened.route,
        action_labels=action_labels,
    )


def is_boost_page(snapshot: Snapshot) -> bool:
    """Whether the ``Coins boost`` header is in the frame."""
    return _contains_text(snapshot, BOOST_MARKER)


def is_empty_boost(snapshot: Snapshot) -> bool:
    """Whether the frame is the boost page's empty state.

    The empty sentence is checked with a substring fallback because OCR often
    reads the whole call-to-action on one line.
    """
    return is_boost_page(snapshot) and _contains_text(snapshot, EMPTY_BOOST_MARKER) and not _has_task_rows(snapshot)


def _boost_arrived(snapshot: Snapshot) -> bool:
    if not is_boost_page(snapshot):
        return False
    return is_empty_boost(snapshot) or _has_task_rows(snapshot)


def _has_task_rows(snapshot: Snapshot) -> bool:
    return any(find_action_in_row(row) is not None for row in group_rows(snapshot.boxes, snapshot.geometry))


def _task_signature(snapshot: Snapshot) -> tuple[tuple[str, str, tuple[int, int] | None], ...]:
    """The title/action/progress set used to prove the list stopped changing."""
    signature: list[tuple[str, str, tuple[int, int] | None]] = []
    for row in group_rows(snapshot.boxes, snapshot.geometry):
        action = find_action_in_row(row)
        if action is None:
            continue
        title, _ = _title_and_subtitle(row, action, DEFAULT_ACTION_LABELS)
        if title is None:
            continue
        badge = find_progress_badge(row)
        progress = (badge.completed, badge.total) if badge is not None else None
        signature.append((normalize(title), normalize(action.text), progress))
    return tuple(signature)


def _arrival_detail(snapshot: Snapshot) -> str:
    if is_empty_boost(snapshot):
        return f'{BOOST_MARKER!r} is empty ({EMPTY_BOOST_MARKER!r}); report nothing available'
    return f'{EARN_MORE_LABEL!r} opened the {BOOST_MARKER!r} task list'


def _contains_text(snapshot: Snapshot, label: str) -> bool:
    if find_text(snapshot, label, min_score=DEFAULT_MIN_SCORE, min_ocr_score=DEFAULT_MIN_OCR_SCORE) is not None:
        return True
    needle = normalize(label)
    if not needle:
        return False
    return any(needle in normalize(box.text) for box in snapshot.boxes if box.score >= DEFAULT_MIN_OCR_SCORE)


def _title_and_subtitle(
    row: Row,
    action: TextBox,
    action_labels: Sequence[str],
) -> tuple[str | None, str | None]:
    candidates = [
        box
        for box in row.boxes
        if box is not action
        and not _is_action_text(box.text, action_labels)
        and parse_progress_badge(box.text) is None
        and any(character.isalpha() for character in box.text)
        and not _is_reward_text(box.text)
    ]
    if not candidates:
        return None, None

    title = candidates[0].text.strip()
    if not title:
        return None, None
    subtitle = ' '.join(box.text.strip() for box in candidates[1:] if box.text.strip())
    return title, subtitle or None


def _is_action_text(text: str, action_labels: Sequence[str]) -> bool:
    return any(similarity(label, text) >= DEFAULT_MIN_SCORE for label in action_labels)


def _is_reward_text(text: str) -> bool:
    return bool(_REWARD.match(text)) or normalize(text) in _REWARD_WORDS


def _without_destination_family(corpus: PopupCorpus) -> PopupCorpus:
    """Remove the family that would close the boost destination itself."""
    families = tuple(family for family in corpus.families if family.anchor != BOOST_MARKER)
    if len(families) == len(corpus.families):
        return corpus
    return replace(corpus, families=families)


def _capture(source: CaptureSource) -> Snapshot:
    if callable(source):
        return source()
    return source.capture()
