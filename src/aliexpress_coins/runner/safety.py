"""Structural abort conditions for screens the run must not act on (issue #20).

The guard sits on the capture seam, so every executor frame is seen by the same
checks rather than each executor re-implementing them. Three conditions abort the
whole run:

* ``unexpected_screen`` — the *same* unrecognised or dead screen is captured
  three times in a row, during navigation or inside a task. A known screen
  resets the count, and the identity is the classifier state/surface plus a
  coarse layout/marker fingerprint, so OCR jitter does not reset it. The
  condition is "the same surface", not "three arbitrary failures". Inside a
  task the engine first spends its bounded recovery (three shortcut entries)
  and fails only that task when the coins page is proven again; if recovery
  fails, the run aborts with the original screen (#83).
* ``dangerous_control`` — a short buy / checkout / cart / redeem action label
  (a button caption, not the same vocabulary inside a sentence) is visible while
  a task is running. The one exception is the measured surprise-item product
  detail page, whose unavoidable ``Add to cart`` / ``Buy now`` buttons are
  documented as never tapped (docs/07 §product detail); the guard still aborts on
  any other spending or redeem control there.
* ``balance_decreased`` — a readable coin-page balance is lower than the baseline
  recorded before the first task. Coins only go up in an authorised run, so a
  decrease proves something acted outside the policy.

An abort raises :class:`RunAborted`, which carries a named
:class:`~aliexpress_coins.runner.failures.Failure` and the screen that triggered
it. The engine reports both and still runs its return-to-coins-page path.
"""

from __future__ import annotations

import re
import statistics
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum

from aliexpress_coins.perception.layout import find_action_in_row, group_rows
from aliexpress_coins.perception.matching import DEFAULT_MIN_OCR_SCORE, normalize
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.state import CaptureSource, Classification, ScreenState, classify
from aliexpress_coins.runner.failures import Failure, FailureCode, failure
from aliexpress_coins.tasks.catalogue import TaskCatalogue
from aliexpress_coins.tasks.signin import read_coin_balance
from aliexpress_coins.tasks.surface import (
    COIN_PAGE_MARKERS,
    REDEEM_MARKER,
    Surface,
    classify_surface,
    contains_text,
    is_boost_page,
    is_logged_out,
)

__all__ = [
    'DANGEROUS_WORDS',
    'UNEXPECTED_SCREEN_LIMIT',
    'AbortCode',
    'RunAborted',
    'SafetyGuard',
    'ScreenReport',
    'find_dangerous_controls',
]

UNEXPECTED_SCREEN_LIMIT = 3
"""How many identical unexpected frames in a row abort the run."""

DANGEROUS_WORDS = frozenset(
    {
        'add to bag',
        'add to cart',
        'buy',
        'buy it now',
        'buy now',
        'check out',
        'checkout',
        'order now',
        'place an order',
        'place order',
        'purchase',
        'purchase now',
        'redeem',
        'redeem now',
    }
)
"""Exact short labels that are spending/redeem *controls*, not vocabulary.

A label is dangerous only when the whole OCR line is one of these action
phrases, optionally followed by a price or count the button carries. The same
words inside a sentence are copy: the quiz page's ``Can be applied directly at
Checkout`` answer is promotional text, not a button. Bare ``cart``, ``order``
and ``orders`` are destinations or row titles, not actions, so they are
deliberately absent.
"""

_CONTROL_MAX_HEIGHT_RATIO = 5.0
"""A control caption's box is no taller than this multiple of the median text box.

The bound only rejects display-scale text. A prominent CTA can be several times
the body size on a small-print page (a 100 px caption beside 20 px text is 5x),
while a headline an order of magnitude taller is not a tappable caption. The
scale comes from the frame's own text, never a stored pixel size.
"""

_CONTROL_SUFFIX = re.compile(r'(?:[·•|:~-]\s*)?(?:\(\s*\d+\s*\)?|(?:c\$|us\s*\$|\$|cs)\s*\d[\d.,]*)')
"""A bounded price/count/separator remainder after a button caption."""

_PRODUCT_DETAIL = re.compile(r'\bitem\s+\d+\s*/\s*\d+\b')
_PRODUCT_DETAIL_SAFE = frozenset({'add to cart', 'buy', 'buy now'})
_COIN_REDEEM_LABEL = 'redeem'

_LAYOUT_X_BUCKETS = 10
_LAYOUT_Y_BUCKETS = 15
"""Coarse screen-layout buckets for the unexpected-screen fingerprint."""

_COUNTDOWN = re.compile(r'^\s*\d{1,3}\s*s\s*$', re.IGNORECASE)
"""The measured ``Ns`` timer-chip form; a status-bar clock is not a countdown."""

_COUNTDOWN_RIGHT_FRACTION = 0.72
_COUNTDOWN_TOP_FRACTION = 0.12
_COUNTDOWN_BOTTOM_FRACTION = 0.88
"""The measured right-edge/content band of the browse timer chip (docs/01 §7b)."""


class AbortCode(StrEnum):
    """The three structural abort conditions."""

    UNEXPECTED_SCREEN = 'unexpected_screen'
    DANGEROUS_CONTROL = 'dangerous_control'
    BALANCE_DECREASED = 'balance_decreased'


@dataclass(frozen=True, slots=True)
class ScreenReport:
    """The screen an abort saw, small enough for ``run.json`` and a log line."""

    state: str
    surface: str
    labels: tuple[str, ...]
    fingerprint: str

    @property
    def summary(self) -> str:
        """One readable sentence naming the surface and its first labels."""
        shown = ', '.join(self.labels[:4]) or 'no readable labels'
        return f'{self.state}/{self.surface}: {shown}'

    def as_dict(self) -> dict[str, object]:
        """Render the screen for the report."""
        return {
            'state': self.state,
            'surface': self.surface,
            'labels': list(self.labels),
            'fingerprint': self.fingerprint,
        }


class RunAborted(Exception):
    """A safety condition stopped the run; the engine reports it and returns home."""

    def __init__(self, *, code: AbortCode, reason: str, screen: ScreenReport) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.screen = screen
        self.failure: Failure = failure(
            FailureCode(code.value),
            detail=f'{reason}; screen={screen.summary}',
        )

    def as_dict(self) -> dict[str, object]:
        """The abort record carried by ``run.json``."""
        return {
            'code': self.code.value,
            'reason': self.reason,
            'screen': self.screen.as_dict(),
            'failure': self.failure.as_dict(),
        }


class SafetyGuard:
    """Inspect every captured frame against the three abort conditions."""

    def __init__(
        self,
        *,
        catalogue: TaskCatalogue | None = None,
        limit: int = UNEXPECTED_SCREEN_LIMIT,
    ) -> None:
        if limit < 1:
            raise ValueError(f'limit must be positive, got {limit!r}')
        self.limit = limit
        self.catalogue = catalogue
        self._task_depth = 0
        self._task_label: str | None = None
        self._streak_fingerprint: str | None = None
        self._streak = 0
        self._recovering = 0
        self._baseline_balance: float | None = None
        self._disarmed = False
        self.failure: Failure | None = None
        self.last_screen: ScreenReport | None = None
        self.frames = 0

    @property
    def task_running(self) -> bool:
        """Whether an executor is currently allowed to act."""
        return self._task_depth > 0

    @property
    def streak(self) -> int:
        """How many identical unexpected frames have been seen in a row."""
        return self._streak

    def expect_balance(self, balance: float | None) -> None:
        """Set the coin balance the run started from; ``None`` disables the check."""
        self._baseline_balance = balance

    def disarm(self) -> None:
        """Stop inspecting after an abort so cleanup cannot abort a second time.

        The return-to-coins-page path runs after the report has already named the
        abort. Re-reading the same bad screen there would replace a clean abort
        with a cleanup failure, so the guard is deliberately disarmed first.
        """
        self._baseline_balance = None
        self._task_depth = 0
        self._task_label = None
        self._disarmed = True
        self._recovering = 0
        self._reset_streak()

    @contextmanager
    def task(self, label: str | None = None) -> Iterator[None]:
        """Mark the dispatch window in which dangerous controls are fatal.

        The unexpected-screen streak starts fresh for each task: a product page
        inside a browse task is not the same surface as the task list that was
        read before it.
        """
        self._task_depth += 1
        self._task_label = label
        self._reset_streak()
        try:
            yield
        finally:
            self._task_depth -= 1
            if self._task_depth == 0:
                self._task_label = None
            self._reset_streak()

    @contextmanager
    def recovering(self) -> Iterator[None]:
        """Suspend the unexpected-screen streak while the engine drives back home.

        A task that hit the limit has already failed; recovery gets the full
        bounded navigation budget before the run can be called unrecoverable.
        The balance and dangerous-control checks keep their teeth, and the
        caller turns a failed recovery into the run-level abort (#83).
        """
        self._recovering += 1
        self._reset_streak()
        try:
            yield
        finally:
            self._recovering -= 1
            if self._recovering == 0:
                self._reset_streak()

    def wrap(self, source: CaptureSource | Callable[[], Snapshot]) -> Callable[[], Snapshot]:
        """Wrap a capture seam so every frame is inspected exactly once."""

        def capture() -> Snapshot:
            snapshot = source() if callable(source) else source.capture()
            return self.inspect(snapshot)

        return capture

    def inspect(self, snapshot: Snapshot) -> Snapshot:
        """Run the safety checks on one frame; return it unchanged when safe."""
        self.frames += 1
        if self._disarmed:
            return snapshot
        classification = classify(snapshot)
        surface = classify_surface(snapshot)
        report = _screen_report(snapshot, classification.state, surface)
        self.last_screen = report

        if self.task_running:
            controls = find_dangerous_controls(snapshot, self.catalogue)
            if controls:
                label = controls[0].text.strip()
                reason = f'a {label!r} control appeared while {self._task_label or "a task"} was running'
                self._abort(AbortCode.DANGEROUS_CONTROL, reason, report)
            if not self._recovering:
                self._track_unexpected(
                    report,
                    unexpected=_is_unexpected_inside_task(snapshot, classification, surface),
                )
            self._check_balance(snapshot, report)
            return snapshot

        if not self._recovering:
            self._track_unexpected(report, unexpected=_is_unexpected(snapshot, classification.state, surface))
        self._check_balance(snapshot, report)
        return snapshot

    def _track_unexpected(self, screen: ScreenReport, *, unexpected: bool) -> None:
        """Count consecutive sightings of the same unexpected screen."""
        if not unexpected:
            self._reset_streak()
            return
        if self._streak_fingerprint == screen.fingerprint:
            self._streak += 1
        else:
            self._streak_fingerprint = screen.fingerprint
            self._streak = 1
        if self._streak >= self.limit:
            reason = f'the same unexpected screen was seen {self._streak} times in a row: {screen.summary}'
            self._abort(AbortCode.UNEXPECTED_SCREEN, reason, screen)

    def _check_balance(self, snapshot: Snapshot, screen: ScreenReport) -> None:
        baseline = self._baseline_balance
        if baseline is None or not _is_coin_page(snapshot):
            return
        current = read_coin_balance(snapshot)
        if current is None or current >= baseline:
            return
        reason = f'the coin balance decreased from {baseline:g} to {current:g}'
        self._abort(AbortCode.BALANCE_DECREASED, reason, screen)

    def _abort(self, code: AbortCode, reason: str, screen: ScreenReport) -> None:
        self.failure = failure(FailureCode(code.value), detail=f'{reason}; screen={screen.summary}')
        raise RunAborted(code=code, reason=reason, screen=screen)

    def _reset_streak(self) -> None:
        self._streak_fingerprint = None
        self._streak = 0


def find_dangerous_controls(
    snapshot: Snapshot,
    catalogue: TaskCatalogue | None = None,
) -> tuple[TextBox, ...]:
    """Return the OCR boxes that name a short buy/checkout/cart/redeem caption.

    A control is one OCR line whose box is caption-sized relative to the
    frame's median text height and whose whole normalised text is an action
    caption, optionally followed by a bounded price/count/separator. A wrapped
    two-line caption is recovered from its row. The known surprise-item product
    detail page is exempt for its two purchase buttons because the task visits
    that page deliberately and never taps them; any other dangerous control on
    that page still aborts. With a catalogue, the labels of *denied* task rows
    are exempt (policy refuses those rows); a purchase label beside an allowed
    row is still a live control.
    """
    product_detail = _is_product_detail(snapshot)
    refused_row_labels = _refused_row_labels(snapshot, catalogue)
    median_height = _median_text_height(snapshot)
    found: list[TextBox] = []
    found_ids: set[int] = set()
    for box in snapshot.boxes:
        if box.score < DEFAULT_MIN_OCR_SCORE:
            continue
        if id(box) in refused_row_labels:
            continue
        text = normalize(box.text)
        if not text:
            continue
        caption = _control_caption(text)
        if caption is None or not _is_control_box(box, median_height):
            continue
        if _is_exempt_caption(snapshot, caption, product_detail=product_detail):
            continue
        found.append(box)
        found_ids.add(id(box))

    # A button whose caption wraps over two OCR lines is still one control;
    # its row carries the complete caption. Rows already matched above are
    # skipped so a sidecar price is not reported as a second control.
    for row in group_rows(snapshot.boxes, snapshot.geometry):
        if any(id(box) in found_ids for box in row.boxes):
            continue
        candidate = next(
            (
                box
                for box in row.boxes
                if box.score >= DEFAULT_MIN_OCR_SCORE and id(box) not in refused_row_labels and normalize(box.text)
            ),
            None,
        )
        if candidate is None or not _is_control_box(candidate, median_height):
            continue
        caption = _control_caption(normalize(row.text))
        if caption is None or _is_exempt_caption(snapshot, caption, product_detail=product_detail):
            continue
        found.append(candidate)

    return tuple(found)


def _control_caption(text: str) -> str | None:
    """The action caption matched by the whole normalised line, if any.

    The caption must match a :data:`DANGEROUS_WORDS` entry from the start; the
    only permitted remainder is a price, a count, or their separator. A sentence
    that merely contains ``checkout`` or ``redeem`` stays copy and cannot abort.
    """
    for caption in DANGEROUS_WORDS:
        if text == caption:
            return caption
        if text.startswith(f'{caption} '):
            remainder = text[len(caption) + 1 :].strip()
            if remainder and _CONTROL_SUFFIX.fullmatch(remainder):
                return caption
    return None


def _is_exempt_caption(snapshot: Snapshot, caption: str, *, product_detail: bool) -> bool:
    """Apply the coin-page redeem and known product-detail exemptions."""
    if caption == _COIN_REDEEM_LABEL and _is_coin_page(snapshot):
        return True
    return product_detail and caption in _PRODUCT_DETAIL_SAFE


def _median_text_height(snapshot: Snapshot) -> float | None:
    """The median height of readable text boxes, the frame's caption scale."""
    heights = [
        box.height
        for box in snapshot.boxes
        if box.height > 0 and box.text.strip() and box.score >= DEFAULT_MIN_OCR_SCORE
    ]
    return float(statistics.median(heights)) if heights else None


def _is_control_box(box: TextBox, median_height: float | None) -> bool:
    """Whether ``box`` is one caption-sized line, not a display headline."""
    if median_height is None or median_height <= 0 or box.height <= 0:
        return False
    return box.height <= median_height * _CONTROL_MAX_HEIGHT_RATIO


def _screen_report(snapshot: Snapshot, state: ScreenState, surface: Surface) -> ScreenReport:
    boxes = tuple(box for box in snapshot.boxes if box.text.strip() and box.score >= DEFAULT_MIN_OCR_SCORE)
    labels = tuple(box.text.strip() for box in boxes)
    ordered = sorted(boxes, key=lambda box: (box.bbox[1], box.bbox[0]))
    # A stuck screen repeats its layout even when OCR misreads a character.
    # Quantised centres plus the initials of the first few labels identify it
    # without depending on every glyph, so jitter does not reset the streak.
    layout = tuple(
        (
            round(box.center[0] / max(1, snapshot.width) * _LAYOUT_X_BUCKETS),
            round(box.center[1] / max(1, snapshot.height) * _LAYOUT_Y_BUCKETS),
        )
        for box in ordered[:8]
    )
    markers = tuple((normalize(box.text)[:3] or '?') for box in ordered[:3])
    fingerprint = f'{state.value}/{surface.value}/{layout}/{markers}'
    return ScreenReport(
        state=state.value,
        surface=surface.value,
        labels=labels,
        fingerprint=fingerprint,
    )


def _is_unexpected(snapshot: Snapshot, state: ScreenState, surface: Surface) -> bool:
    if is_logged_out(snapshot):
        return False
    if state in (ScreenState.UNKNOWN, ScreenState.ERROR):
        return True
    return state is ScreenState.PAGE and surface is Surface.UNKNOWN


def _is_unexpected_inside_task(
    snapshot: Snapshot,
    classification: Classification,
    surface: Surface,
) -> bool:
    """A task page only counts as unexpected when its content is truly unknown.

    Known task pages classify as ``PAGE``; a measured surprise product detail and
    a signed-out screen are known too. Transient states are not "stuck": a blank
    WebView is loading evidence, the empty boost page is a documented reset, and
    a running countdown is an allowed browse destination. Those are left to the
    executor's own settle timeout. Anything else that stays unknown or dead for
    three captures is a stuck screen.
    """
    if _is_product_detail(snapshot) or is_logged_out(snapshot):
        return False
    if surface is Surface.EMPTY_BOOST:
        return False
    if any(evidence.signal == 'blank_frame' for evidence in classification.evidence):
        return False
    if _has_countdown(snapshot):
        return False
    return classification.state in (ScreenState.UNKNOWN, ScreenState.ERROR)


def _has_countdown(snapshot: Snapshot) -> bool:
    left = snapshot.width * _COUNTDOWN_RIGHT_FRACTION
    top = snapshot.height * _COUNTDOWN_TOP_FRACTION
    bottom = snapshot.height * _COUNTDOWN_BOTTOM_FRACTION
    for box in snapshot.boxes:
        if box.score < DEFAULT_MIN_OCR_SCORE:
            continue
        centre_x, centre_y = box.center
        if centre_x < left or not top <= centre_y <= bottom:
            continue
        if _COUNTDOWN.match(unicodedata.normalize('NFKC', box.text)):
            return True
    return False


def _refused_row_labels(snapshot: Snapshot, catalogue: TaskCatalogue | None) -> set[int]:
    """Labels of catalogue-denied task rows with an action, not live controls.

    The boost list always renders the refused families, including ``Place an
    order on this page`` and ``Add 5 items to your cart``, each with its own
    ``GO``. Those are policy decisions, not controls the running task can press:
    the dispatcher never hands them to an executor. Only a label the catalogue
    actually denies is exempt; a dangerous label beside an allowed row's ``GO``
    remains a live control.
    """
    if catalogue is None:
        return set()
    protected: set[int] = set()
    for row in group_rows(snapshot.boxes, snapshot.geometry):
        action = find_action_in_row(row)
        if action is None:
            continue
        title = next(
            (box for box in row.boxes if box is not action and any(character.isalpha() for character in box.text)),
            None,
        )
        if title is None:
            continue
        decision = catalogue.decide(title.text)
        if decision.denied and decision.rule is not None:
            protected.update(id(box) for box in row.boxes if box is not action)
    return protected


def _is_product_detail(snapshot: Snapshot) -> bool:
    return any(
        box.score >= DEFAULT_MIN_OCR_SCORE and _PRODUCT_DETAIL.search(normalize(box.text)) for box in snapshot.boxes
    )


def _is_coin_page(snapshot: Snapshot) -> bool:
    return (
        all(contains_text(snapshot, marker) for marker in COIN_PAGE_MARKERS)
        and not is_boost_page(snapshot)
        and not contains_text(snapshot, REDEEM_MARKER)
    )
