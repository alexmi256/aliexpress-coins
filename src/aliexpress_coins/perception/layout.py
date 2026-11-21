"""Row and section layout inference over a frame's OCR boxes (issue #14).

Coordinates drift: a new task at the top of the boost list moves every row down,
so a remembered ``GO`` position is a tap on the wrong task. This module recovers
the rows from the current frame instead.

A row is a vertical cluster of boxes. The cluster tolerance is **derived from
the frame**: twice the median box height (capped at a fraction of the frame
height), never a stored pixel offset. That ratio is justified by the measured
pages: on the 1200x2000 boost frame the median text-box height is ~48.5 px,
boxes inside one task row are at most ~92 px apart centre-to-centre, and
adjacent task rows are ~190 px or more apart (pitch ~340 px). On the inline
coins rows the title-to-``GO`` offset is ~31 px. Twice the median height groups
the former without merging the latter.

Rows are returned in reading order (top to bottom, boxes top-left first). A
row's action is the **rightmost** action label in its band, which is how a task
title becomes a tap target. Progress badges (``0/6``) attach to the same row;
section grouping turns the rows into what a human sees, for diagnostics.
"""

from __future__ import annotations

import re
import statistics
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.matching import (
    DEFAULT_MIN_OCR_SCORE,
    DEFAULT_MIN_SCORE,
    TextIndex,
    TextMatch,
    similarity,
)
from aliexpress_coins.perception.snapshot import TextBox

ROW_CENTER_TOLERANCE_HEIGHT_RATIO = 2.0
"""How far apart two box centres may be and still share a row.

Expressed as a multiple of the frame's median text-box height. The measured
boost row has centre gaps up to ~1.9 median heights (the reward sits below the
button) and adjacent rows are at least ~3.9 median heights apart.
"""

MAX_ROW_TOLERANCE_FRAME_FRACTION = 0.1
"""A frame-relative cap on the derived tolerance.

One bad OCR box should not be able to turn the whole frame into one row.
"""

SECTION_HEADER_HEIGHT_RATIO = 1.25
"""A lone, text-bearing row this much taller than the median is a header."""

DEFAULT_ACTION_LABELS: tuple[str, ...] = ('GO',)
"""The action label measured on every task row."""

_PROGRESS_PATTERN = re.compile(r'^\s*(?P<completed>\d+)\s*/\s*(?P<total>\d+)\s*$')


def _reading_key(box: TextBox) -> tuple[float, float, float, float]:
    """Top-to-bottom, then left-to-right, for a human reading the frame."""
    left, top, right, bottom = box.bbox
    return top, left, bottom, right


def _rightmost_key(box: TextBox) -> tuple[float, float]:
    """Order boxes by their right edge, then by centre, for action selection."""
    left, _, right, _ = box.bbox
    return right, (left + right) / 2.0


@dataclass(frozen=True, slots=True)
class Row:
    """One horizontal cluster of OCR boxes, in reading order."""

    boxes: tuple[TextBox, ...]

    def __post_init__(self) -> None:
        boxes = tuple(self.boxes)
        if not boxes:
            raise ValueError('a row needs at least one box')
        object.__setattr__(self, 'boxes', tuple(sorted(boxes, key=_reading_key)))

    @property
    def top(self) -> float:
        """The top edge of the row band."""
        return min(box.bbox[1] for box in self.boxes)

    @property
    def bottom(self) -> float:
        """The bottom edge of the row band."""
        return max(box.bbox[3] for box in self.boxes)

    @property
    def left(self) -> float:
        """The left edge of the row band."""
        return min(box.bbox[0] for box in self.boxes)

    @property
    def right(self) -> float:
        """The right edge of the row band."""
        return max(box.bbox[2] for box in self.boxes)

    @property
    def center_y(self) -> float:
        """The vertical centre of the row band."""
        return (self.top + self.bottom) / 2.0

    @property
    def height(self) -> float:
        """The height of the row band."""
        return self.bottom - self.top

    @property
    def text(self) -> str:
        """Every label in the row, joined for logs and reports."""
        return ' '.join(box.text for box in self.boxes)

    def action(
        self,
        action_labels: Sequence[str] = DEFAULT_ACTION_LABELS,
        *,
        min_score: float = DEFAULT_MIN_SCORE,
        min_ocr_score: float = DEFAULT_MIN_OCR_SCORE,
    ) -> TextBox | None:
        """The rightmost action label in this row, if there is one."""
        return find_action_in_row(
            self,
            action_labels,
            min_score=min_score,
            min_ocr_score=min_ocr_score,
        )

    @property
    def progress(self) -> ProgressBadge | None:
        """The progress badge that shares this row, if there is one."""
        return find_progress_badge(self)


@dataclass(frozen=True, slots=True)
class ProgressBadge:
    """A parsed ``completed/total`` badge and the box that carried it."""

    completed: int
    total: int
    box: TextBox

    def __post_init__(self) -> None:
        if self.completed < 0:
            raise ValueError('a progress badge cannot have a negative completed count')
        if self.total <= 0:
            raise ValueError('a progress badge total must be positive')

    @property
    def text(self) -> str:
        """The badge text exactly as OCR read it."""
        return self.box.text

    @property
    def fraction(self) -> float:
        """Completed units as a fraction of the total."""
        return self.completed / self.total

    @property
    def complete(self) -> bool:
        """Whether the badge reads as done."""
        return self.completed >= self.total


@dataclass(frozen=True, slots=True)
class Section:
    """A header and the rows beneath it, in reading order."""

    header: Row | None
    rows: tuple[Row, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, 'rows', tuple(self.rows))

    @property
    def title(self) -> str | None:
        """The header's text, or ``None`` for a leading headerless section."""
        return self.header.text if self.header is not None else None


def row_tolerance(boxes: Iterable[TextBox], geometry: DeviceGeometry) -> float:
    """Derive the vertical grouping tolerance from the frame itself.

    The median box height scales with the device, so the same rule survives the
    replay at 1080x1920. The frame-height cap only guards against a broken OCR
    result; it is a fraction, not a stored pixel value.
    """
    heights = tuple(box.height for box in boxes if box.height > 0)
    if not heights:
        return 0.0
    median_height = float(statistics.median(heights))
    derived = median_height * ROW_CENTER_TOLERANCE_HEIGHT_RATIO
    return min(derived, geometry.height * MAX_ROW_TOLERANCE_FRAME_FRACTION)


def group_rows(boxes: Iterable[TextBox], geometry: DeviceGeometry) -> tuple[Row, ...]:
    """Cluster boxes into rows by vertical band, in reading order.

    Boxes are visited by their vertical centres. A box joins the current row
    while its centre is within :func:`row_tolerance` of the previous box; the
    first larger gap starts a new row. The tolerance comes from the frame, so a
    row moves without changing the pairing inside it.
    """
    ordered = sorted(boxes, key=lambda box: (box.center[1], box.bbox[0], box.bbox[1]))
    if not ordered:
        return ()

    tolerance = row_tolerance(ordered, geometry)
    rows: list[Row] = []
    current: list[TextBox] = [ordered[0]]
    for box in ordered[1:]:
        if box.center[1] - current[-1].center[1] <= tolerance:
            current.append(box)
        else:
            rows.append(Row(tuple(current)))
            current = [box]
    rows.append(Row(tuple(current)))
    return tuple(rows)


def row_of(rows: Iterable[Row], target: TextBox | TextMatch) -> Row | None:
    """Return the row that contains ``target``, or ``None``.

    ``target`` may be a raw :class:`TextBox` or a :class:`TextMatch` from the
    matching layer, so a task title that was just found can be turned into its
    row without a second lookup.
    """
    wanted = target.box if isinstance(target, TextMatch) else target
    for row in rows:
        if any(box is wanted or box == wanted for box in row.boxes):
            return row
    return None


def find_action_in_row(
    row: Row,
    action_labels: Sequence[str] = DEFAULT_ACTION_LABELS,
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    min_ocr_score: float = DEFAULT_MIN_OCR_SCORE,
) -> TextBox | None:
    """Return the rightmost action label inside a row's band.

    A task title is on the left and its button is the rightmost matching label,
    so selecting by position rather than by remembering coordinates is what
    keeps the pairing stable when a new task shifts the list.
    """
    labels = tuple(action_labels)
    if not labels:
        return None

    index = TextIndex(row.boxes)
    best: TextBox | None = None
    for label in labels:
        for match in index.find_all(label, min_score=min_score, min_ocr_score=min_ocr_score):
            if best is None or _rightmost_key(match.box) > _rightmost_key(best):
                best = match.box
    return best


def parse_progress_badge(text: str) -> tuple[int, int] | None:
    """Parse a ``completed/total`` badge, or return ``None``.

    Spaces around the slash are tolerated because OCR sometimes inserts them;
    anything else in the string (a currency amount, a reward, a date) is not a
    badge. A zero total is rejected rather than producing a division by zero.
    """
    normalised = unicodedata.normalize('NFKC', text)
    match = _PROGRESS_PATTERN.match(normalised)
    if match is None:
        return None
    completed = int(match.group('completed'))
    total = int(match.group('total'))
    if total <= 0:
        return None
    return completed, total


def find_progress_badge(row: Row) -> ProgressBadge | None:
    """Return the rightmost progress badge in ``row``, if any."""
    best: ProgressBadge | None = None
    for box in row.boxes:
        parsed = parse_progress_badge(box.text)
        if parsed is None:
            continue
        completed, total = parsed
        badge = ProgressBadge(completed=completed, total=total, box=box)
        if best is None or _rightmost_key(box) > _rightmost_key(best.box):
            best = badge
    return best


def is_section_header(
    row: Row,
    *,
    median_box_height: float,
    header_labels: Sequence[str] = (),
    min_score: float = DEFAULT_MIN_SCORE,
    header_height_ratio: float = SECTION_HEADER_HEIGHT_RATIO,
) -> bool:
    """Whether ``row`` visually or textually starts a section.

    A known label wins. Otherwise a header is a lone, text-bearing row that is
    visibly taller than the frame's median text box; numeric-only rows such as
    a balance are not headers.
    """
    labels = tuple(header_labels)
    if any(similarity(label, box.text) >= min_score for label in labels for box in row.boxes):
        return True
    if median_box_height <= 0 or len(row.boxes) != 1:
        return False
    text = row.boxes[0].text
    if not any(character.isalpha() for character in text):
        return False
    return row.height >= median_box_height * header_height_ratio


def group_sections(
    rows: Iterable[Row],
    *,
    header_labels: Sequence[str] = (),
    min_score: float = DEFAULT_MIN_SCORE,
    header_height_ratio: float = SECTION_HEADER_HEIGHT_RATIO,
) -> tuple[Section, ...]:
    """Group rows under their headers, in reading order.

    ``header_labels`` names headers the caller already knows (``Coins boost``);
    when it is empty, a lone prominent text row is treated as a header. Rows
    before the first header form a headerless section, so no row is dropped.
    """
    ordered = tuple(rows)
    if not ordered:
        return ()

    heights = tuple(box.height for row in ordered for box in row.boxes if box.height > 0)
    median_height = float(statistics.median(heights)) if heights else 0.0
    labels = tuple(header_labels)

    sections: list[Section] = []
    header: Row | None = None
    body: list[Row] = []
    for row in ordered:
        if is_section_header(
            row,
            median_box_height=median_height,
            header_labels=labels,
            min_score=min_score,
            header_height_ratio=header_height_ratio,
        ):
            if header is not None or body:
                sections.append(Section(header=header, rows=tuple(body)))
            header = row
            body = []
        else:
            body.append(row)
    sections.append(Section(header=header, rows=tuple(body)))
    return tuple(sections)
