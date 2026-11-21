"""Fuzzy text lookup over a frame's OCR boxes (issue #13).

The UI has no ids: a label is the API. OCR is good but not literal — it reads
`Earn more >` for `Earn more`, `Cs0.04` for `C$0.04`, and returns the close
control as a bare glyph. So matching is layered:

* ``normalize`` collapses the differences that carry no meaning (case, whitespace
  runs, edge punctuation) and maps glyph-only labels to stable tokens. It never
  returns ``''`` for a non-empty label, because an empty needle matches
  everything.
* ``similarity`` is a normalised Levenshtein ratio. Digit runs must agree;
  otherwise the score is capped, because `C$0.04` and `C$0.44` are different
  values even though they differ by one character.
* ``TextIndex`` groups a frame's boxes by normalised label once, so repeated
  lookups do not re-scan every box.
* Two independent floors: ``DEFAULT_MIN_OCR_SCORE`` drops boxes the engine is
  unsure about, and ``DEFAULT_MIN_SCORE`` decides whether the text is the label
  we asked for. They answer different questions and are tuned separately.

Every result keeps the original :class:`~aliexpress_coins.perception.snapshot.TextBox`:
frame coordinates and the engine's confidence are never rewritten here.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from aliexpress_coins.perception.snapshot import TextBox

DEFAULT_MIN_SCORE = 0.8
"""The default text-match threshold.

A normalised equality scores 1.0. At 0.8, `C$0.04` vs `Cs0.04` (0.83) matches,
while `Coins` vs `Vouchers` (0.25) and the adjacent task roster labels (0.54)
do not. A 0.6 match is how a tap lands on the wrong row.
"""

DEFAULT_MIN_OCR_SCORE = 0.5
"""The default OCR-confidence floor, independent of ``DEFAULT_MIN_SCORE``.

On the measured coins-page frames every real label scored >= 0.96; the only
boxes below 0.75 were fragments of artwork. #17/#18 tune this floor with the
engine, not with the task roster.
"""

STRICT_MIN_SCORE = 1.0
"""The strict default used by ``Snapshot.find``/``has``.

Normalisation still applies (case, edges), but a near-spelling is not accepted
unless the caller asks for it explicitly.
"""

_MISMATCHED_DIGITS_CEILING = 0.5
"""Cap applied when the two labels disagree about their digits.

`worth C$0.04` and `worth C$0.44` are one character apart, which the raw ratio
scores 0.92 — far above any useful threshold. Digits are the part of a label
where a single error changes meaning, so a disagreement is never a match.
"""

_GLYPH_TOKENS = {
    '\u00d7': 'close',  # U+00D7
    '\u2715': 'close',  # U+2715
    '\u2716': 'close',  # U+2716
    '\u2717': 'close',  # ✗
    '\u2718': 'close',  # ✘
    '\u2713': 'check',  # ✓
    '\u2714': 'check',  # ✔
    '\u2705': 'check',  # ✅
    '\u2611': 'check',  # ☑
}
"""Glyph-only labels get a stable token instead of normalising to ``''``."""

_EDGE_SYMBOLS = frozenset('><|')
_DIGITS = re.compile(r'\d+')


class LabelSource(Protocol):
    """Anything that can supply a per-frame text index (a ``Snapshot`` does)."""

    @property
    def text_index(self) -> TextIndex:
        """The index built from this frame's boxes."""
        ...


@dataclass(frozen=True, slots=True)
class TextMatch:
    """One matching OCR box and the text score that selected it.

    ``score`` is the text-match similarity; ``ocr_score`` is the engine's own
    confidence for the box. They are deliberately different numbers.
    """

    box: TextBox
    score: float

    @property
    def text(self) -> str:
        """The recognised text, exactly as the engine returned it."""
        return self.box.text

    @property
    def ocr_score(self) -> float:
        """The engine's confidence for this box."""
        return self.box.score

    @property
    def center(self) -> tuple[float, float]:
        """The box centre in frame coordinates."""
        return self.box.center


def normalize(text: str) -> str:
    """Reduce a label to the form matching compares.

    Case, Unicode width, whitespace runs and edge punctuation are removed:
    ``'  Earn   more >  '`` becomes ``'earn more'``. Symbol-only labels become
    tokens (``'U+00D7'`` -> ``'close'``, ``'S'`` -> ``'check'``) so that an empty
    needle can never be produced from a non-empty label. ``''`` and
    whitespace-only input are the only values that normalise to ``''``.
    """
    collapsed = ' '.join(unicodedata.normalize('NFKC', text).casefold().split())
    if not collapsed:
        return ''
    if len(collapsed) == 1:
        token = _GLYPH_TOKENS.get(collapsed)
        if token is None and collapsed == 's':
            token = 'check'
        elif token is None and collapsed == 'x':
            token = 'close'
        if token is not None:
            return token

    start, end = 0, len(collapsed)
    while start < end and _is_edge(collapsed[start]):
        start += 1
    while end > start and _is_edge(collapsed[end - 1]):
        end -= 1
    trimmed = collapsed[start:end].strip()
    if trimmed:
        return trimmed
    return _glyph_fallback(collapsed)


def similarity(a: str, b: str) -> float:
    """Return a 0..1 similarity for two labels, after normalisation.

    This is a normalised Levenshtein ratio: ``1 - distance / max(len)``. Equal
    normalised labels score 1.0, and either side empty scores 0.0. Digit runs
    that disagree are capped below the default threshold.
    """
    left, right = normalize(a), normalize(b)
    if not left or not right:
        return 0.0
    return _ratio(left, right)


class TextIndex:
    """A frame's boxes, grouped by normalised label.

    Building the index normalises each box once. A lookup scores every unique
    label once, then filters the boxes in frame order; on the measured 35-box
    frame the build takes 0.03 ms and one lookup about 0.8 ms, next to ~1.3 s
    of OCR.
    """

    __slots__ = ('_entries', '_labels')

    def __init__(self, boxes: Iterable[TextBox]) -> None:
        entries: list[tuple[int, str, TextBox]] = []
        labels: dict[str, None] = {}
        for position, box in enumerate(boxes):
            label = normalize(box.text)
            if not label:
                continue
            entries.append((position, label, box))
            labels.setdefault(label, None)
        self._entries = tuple(entries)
        self._labels = tuple(labels)

    def find(
        self,
        label: str,
        *,
        min_score: float = DEFAULT_MIN_SCORE,
        min_ocr_score: float = DEFAULT_MIN_OCR_SCORE,
    ) -> TextMatch | None:
        """Return the best match for ``label``, or ``None``.

        Ties are broken by the higher OCR confidence, then by frame order.
        """
        needle = _needle(label)
        _check_floor(min_score, 'min_score')
        _check_floor(min_ocr_score, 'min_ocr_score')
        scores = {text: _ratio(needle, text) for text in self._labels}

        best: TextMatch | None = None
        best_key: tuple[float, float, int] | None = None
        for position, text, box in self._entries:
            score = scores[text]
            if score < min_score or box.score < min_ocr_score:
                continue
            key = (score, box.score, -position)
            if best_key is None or key > best_key:
                best_key = key
                best = TextMatch(box=box, score=score)
        return best

    def find_all(
        self,
        label: str,
        *,
        min_score: float = DEFAULT_MIN_SCORE,
        min_ocr_score: float = DEFAULT_MIN_OCR_SCORE,
    ) -> tuple[TextMatch, ...]:
        """Return every match for ``label`` in frame order."""
        needle = _needle(label)
        _check_floor(min_score, 'min_score')
        _check_floor(min_ocr_score, 'min_ocr_score')
        scores = {text: _ratio(needle, text) for text in self._labels}
        return tuple(
            TextMatch(box=box, score=scores[text])
            for _, text, box in self._entries
            if scores[text] >= min_score and box.score >= min_ocr_score
        )


def find_text(
    source: LabelSource,
    label: str,
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    min_ocr_score: float = DEFAULT_MIN_OCR_SCORE,
) -> TextMatch | None:
    """Find the best box for ``label`` in ``source``'s indexed frame."""
    return source.text_index.find(label, min_score=min_score, min_ocr_score=min_ocr_score)


def find_all(
    source: LabelSource,
    label: str,
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    min_ocr_score: float = DEFAULT_MIN_OCR_SCORE,
) -> tuple[TextMatch, ...]:
    """Find every box for ``label`` in ``source``'s indexed frame, in frame order."""
    return source.text_index.find_all(label, min_score=min_score, min_ocr_score=min_ocr_score)


def _needle(label: str) -> str:
    needle = normalize(label)
    if not needle:
        raise ValueError('a label is required; an empty needle would match everything')
    return needle


def _check_floor(value: float, name: str) -> None:
    if not 0.0 <= value <= 1.0:
        raise ValueError(f'{name} must be between 0 and 1, got {value!r}')


def _is_edge(char: str) -> bool:
    return char in _EDGE_SYMBOLS or unicodedata.category(char).startswith('P')


def _glyph_fallback(text: str) -> str:
    """Stable tokens for a label that is nothing but stripped symbols."""
    chars = [char for char in text if not char.isspace()]
    return ' '.join(_GLYPH_TOKENS.get(char, f'u{ord(char):04x}') for char in chars)


def _digits(text: str) -> tuple[str, ...]:
    return tuple(_DIGITS.findall(text))


def _ratio(left: str, right: str) -> float:
    """Similarity for two already-normalised labels."""
    if left == right:
        return 1.0
    ratio = 1.0 - _levenshtein(left, right) / max(len(left), len(right))
    if _digits(left) != _digits(right):
        return min(ratio, _MISMATCHED_DIGITS_CEILING)
    return ratio


def _levenshtein(left: str, right: str) -> int:
    """Iterative edit distance, with the shorter string as the rows."""
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for row, source_char in enumerate(left, start=1):
        current = [row]
        for column, target_char in enumerate(right, start=1):
            substitution = previous[column - 1] + (source_char != target_char)
            current.append(min(previous[column] + 1, current[column - 1] + 1, substitution))
        previous = current
    return previous[-1]
