"""Scale-aware template matching for glyph-only controls (issue #15).

A drawn close control has no OCR text, so its pixels are the only handle. The
matcher is deliberately small: a grayscale frame, a reviewed crop, and
``cv2.TM_CCOEFF_NORMED``. NCC subtracts the local mean and divides by the local
standard deviation, which makes it invariant to the brightness and contrast of
the scrim behind the same glyph. A polarity reversal (white glyph on dark vs
black glyph on light) is *not* invariant; the corpus stores every crop in the
polarity the app renders, so inverted searching is neither needed nor safe.

Scale is data, not an assumption. A crop records the width of the frame it was
cut from (``source_width`` in ``specs/popups.yaml``); the nominal scale for a
new frame is ``frame_width / source_width``. A bounded search around that
nominal absorbs the density/aspect-ratio drift that a width ratio alone cannot
explain. Nothing here knows the reference tablet's geometry, and no target
coordinate is stored.

NCC is undefined on a constant patch, and OpenCV reports a perfect 1.0 for a
constant template against every location. Both sides are therefore guarded:
a flat template returns ``None`` before searching, and candidate locations whose
frame window has zero variance are masked out of the score map.

Templates are discovered from the popup corpus, never from a filename list:
``load_templates(corpus)`` walks every ``CloseKind.TEMPLATE`` attempt. Adding a
crop is a file drop plus one YAML row, with no code change here.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np
from numpy.typing import NDArray
from PIL import Image, UnidentifiedImageError

from aliexpress_coins.perception.errors import TemplateError
from aliexpress_coins.perception.snapshot import Snapshot

DEFAULT_TEMPLATE_THRESHOLD = 0.88
"""The minimum ``TM_CCOEFF_NORMED`` score for a usable match.

Measured on the committed corpus: a family's own crop scores 0.999-1.000, while
the closest wrong-glyph pair (the coupon ✕ against the launch modal) peaks at
0.79 even after the scale search. 0.88 keeps the true glyph and rejects the
nearest impostor; it is the threshold recorded in docs/01-recon-findings.md §4.
"""

SCALE_SEARCH_RADIUS = 0.20
"""Search ``nominal_scale * (1 ± 0.20)``.

The nominal scale already follows the frame width. This margin covers the part
of icon size that a width ratio cannot see: density overrides, image padding and
an aspect-ratio change. It is a ratio, not a pixel count, so it ports with the
frame.
"""

SCALE_SEARCH_STEPS = 9
"""Number of scales in the bounded range (odd, so the nominal scale is included)."""

COARSE_SEARCH_FACTOR = 0.5
"""The frame and template are first searched at half linear size.

``matchTemplate`` costs roughly the frame area, so the coarse pass is about four
times cheaper. Its winning location is then verified and refined at full
resolution around a window derived from the template's own size, so the returned
location and scale are full-resolution values. The coarse score never decides a
match on its own.
"""

COARSE_SCORE_ALLOWANCE = 0.15
"""How far below the threshold a coarse score may be and still be refined.

Downscaling loses a little correlation even for a true glyph. A coarse score
more than this below ``min_score`` is not worth the full-resolution pass; one
within it is refined and judged on the full-resolution score.
"""

ZERO_VARIANCE = 1e-9
"""Variance at or below this value is a constant patch.

The unit is grayscale intensity squared. An exactly flat 8-bit crop has variance
0, so this only absorbs floating-point noise; it is not a tunable match score.
"""

FrameLike = Image.Image | NDArray[np.generic] | Snapshot
"""A frame may be a PIL image, an array, or a full :class:`Snapshot`."""


class TemplateAttempt(Protocol):
    """The part of a corpus close attempt the store needs."""

    @property
    def kind(self) -> str:
        """The attempt kind; only ``template`` is loaded here."""
        ...

    @property
    def template(self) -> Path | None:
        """The reviewed crop path."""
        ...

    @property
    def source_width(self) -> int | None:
        """The width of the frame the crop was cut from."""
        ...


class PopupFamilyLike(Protocol):
    """The part of a popup family the store needs."""

    @property
    def family(self) -> str:
        """The family id."""
        ...

    @property
    def close(self) -> Sequence[TemplateAttempt]:
        """The ordered close attempts."""
        ...


class PopupCorpusLike(Protocol):
    """The part of a popup corpus the store needs."""

    @property
    def families(self) -> Sequence[PopupFamilyLike]:
        """Every family row."""
        ...


@dataclass(frozen=True, slots=True)
class Template:
    """One reviewed crop and the frame width it was cut from."""

    image: NDArray[np.uint8]
    source_width: int
    family: str = ''
    path: Path | None = None

    def __post_init__(self) -> None:
        image = _to_gray_array(self.image)
        if image.size == 0:
            raise TemplateError('a template image must not be empty')
        if self.source_width <= 0:
            raise TemplateError(f'source_width must be positive, got {self.source_width!r}')
        object.__setattr__(self, 'image', image)
        object.__setattr__(self, 'path', Path(self.path) if self.path is not None else None)

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        *,
        family: str | None = None,
        source_width: int | None = None,
    ) -> Template:
        """Load a crop, defaulting its source width to its own pixel width."""
        crop_path = Path(path)
        try:
            with Image.open(crop_path) as opened:
                image = _to_gray_array(opened)
        except (OSError, SyntaxError, ValueError, UnidentifiedImageError) as exc:
            raise TemplateError(f'could not read template {crop_path}: {exc}') from exc
        if image.size == 0:
            raise TemplateError(f'template is empty: {crop_path}')
        width = image.shape[1]
        return cls(
            image=image,
            source_width=source_width if source_width is not None else width,
            family=family if family is not None else crop_path.stem,
            path=crop_path,
        )

    @property
    def width(self) -> int:
        """The crop width in pixels."""
        return int(self.image.shape[1])

    @property
    def height(self) -> int:
        """The crop height in pixels."""
        return int(self.image.shape[0])


@dataclass(frozen=True, slots=True)
class TemplateMatch:
    """The winning location, score and scale of one template search."""

    template: Template
    score: float
    location: tuple[int, int]
    scale: float
    scales_tried: tuple[float, ...]
    size: tuple[int, int]

    @property
    def box(self) -> tuple[int, int, int, int]:
        """The matched region as ``(left, top, right, bottom)`` in frame pixels."""
        left, top = self.location
        width, height = self.size
        return left, top, left + width, top + height

    @property
    def center(self) -> tuple[float, float]:
        """The centre of the matched region, for a box-clamped tap."""
        left, top, right, bottom = self.box
        return (left + right) / 2.0, (top + bottom) / 2.0


@dataclass(frozen=True, slots=True)
class TemplateStore:
    """Every template referenced by a popup corpus, looked up by family."""

    templates: tuple[Template, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, 'templates', tuple(self.templates))
        if not self.templates:
            raise TemplateError('a template store cannot be empty')

    def __iter__(self) -> Iterator[Template]:
        return iter(self.templates)

    def __len__(self) -> int:
        return len(self.templates)

    def by_family(self, family: str) -> Template:
        """Return one family's crop, or raise ``KeyError``."""
        for template in self.templates:
            if template.family == family:
                return template
        raise KeyError(f'unknown template family: {family!r}')

    def match(
        self,
        frame: FrameLike,
        family: str,
        *,
        min_score: float = DEFAULT_TEMPLATE_THRESHOLD,
        scale_radius: float = SCALE_SEARCH_RADIUS,
        scale_steps: int = SCALE_SEARCH_STEPS,
        scales: Sequence[float] | None = None,
    ) -> TemplateMatch | None:
        """Match one family's crop against ``frame``."""
        return match_template(
            frame,
            self.by_family(family),
            min_score=min_score,
            scale_radius=scale_radius,
            scale_steps=scale_steps,
            scales=scales,
        )

    def match_all(
        self,
        frame: FrameLike,
        *,
        min_score: float = DEFAULT_TEMPLATE_THRESHOLD,
        scale_radius: float = SCALE_SEARCH_RADIUS,
        scale_steps: int = SCALE_SEARCH_STEPS,
        scales: Sequence[float] | None = None,
    ) -> tuple[TemplateMatch, ...]:
        """Match every stored crop, best score first."""
        matches = (
            match_template(
                frame,
                template,
                min_score=min_score,
                scale_radius=scale_radius,
                scale_steps=scale_steps,
                scales=scales,
            )
            for template in self.templates
        )
        return tuple(sorted((match for match in matches if match is not None), key=lambda match: -match.score))


def load_templates(corpus: PopupCorpusLike) -> TemplateStore:
    """Load every template attempt in ``corpus``.

    Discovery is driven entirely by the corpus rows: there is no family list and
    no filename list in this module. A crop that is not referenced is a corpus
    error, and a referenced file is validated by the corpus loader before it
    reaches here.
    """
    templates: list[Template] = []
    for family in corpus.families:
        for attempt in family.close:
            if attempt.kind != 'template':
                continue
            if attempt.template is None or attempt.source_width is None:
                raise TemplateError(f'{family.family!r} has a template attempt without a path or source_width')
            templates.append(
                Template.from_path(
                    attempt.template,
                    family=family.family,
                    source_width=attempt.source_width,
                ),
            )
    if not templates:
        raise TemplateError('the corpus references no templates')
    return TemplateStore(tuple(templates))


def match_template(
    frame: FrameLike,
    template: Template | str | Path | Image.Image | NDArray[np.generic],
    *,
    source_width: int | None = None,
    min_score: float = DEFAULT_TEMPLATE_THRESHOLD,
    scale_radius: float = SCALE_SEARCH_RADIUS,
    scale_steps: int = SCALE_SEARCH_STEPS,
    scales: Sequence[float] | None = None,
) -> TemplateMatch | None:
    """Find ``template`` in ``frame`` and return the best match, or ``None``.

    ``TM_CCOEFF_NORMED`` runs over grayscale pixels. The nominal scale is
    ``frame_width / template.source_width``; unless ``scales`` is supplied, a
    bounded range of ``scale_steps`` values around it is searched. The scale
    that wins is part of the result.

    A template that is a path or an image without a source width is treated as
    cut from a frame as wide as the frame being searched (nominal scale 1.0);
    pass ``source_width`` to override that. ``min_score`` defaults to the
    documented 0.88 threshold.

    Returns ``None`` for a flat template, a flat frame, or when no scale reaches
    ``min_score``. No match is ever returned with a zero-variance window.
    """
    _check_min_score(min_score)
    gray = _frame_gray(frame)
    if gray.size == 0:
        return None
    resolved = _resolve_template(template, frame_width=int(gray.shape[1]), source_width=source_width)
    if _is_flat(resolved.image):
        return None
    if _is_flat(gray):
        return None

    candidates = _scale_candidates(resolved, int(gray.shape[1]), scale_radius, scale_steps, scales)
    coarse = _coarse_search(gray, resolved, candidates)
    if coarse is None:
        hit = _search(gray, resolved.image, candidates)
    elif coarse.score < min_score - COARSE_SCORE_ALLOWANCE:
        return None
    else:
        hit = _refine(gray, resolved, candidates, coarse)
        if hit is None:
            hit = _search(gray, resolved.image, candidates)

    if hit is None or hit.score < min_score:
        return None
    return TemplateMatch(
        template=resolved,
        score=hit.score,
        location=hit.location,
        scale=hit.scale,
        scales_tried=hit.tried,
        size=hit.size,
    )


@dataclass(frozen=True, slots=True)
class _Hit:
    """One best hit from a single-resolution search."""

    score: float
    location: tuple[int, int]
    scale: float
    size: tuple[int, int]
    tried: tuple[float, ...]


def _coarse_search(
    gray: NDArray[np.uint8],
    template: Template,
    candidates: tuple[float, ...],
) -> _Hit | None:
    factor = COARSE_SEARCH_FACTOR
    frame_height, frame_width = gray.shape
    if min(template.width, template.height) * factor < 1.0:
        return None
    if min(frame_width, frame_height) * factor < 1.0:
        return None
    coarse = np.asarray(
        cv2.resize(
            gray,
            (max(1, round(frame_width * factor)), max(1, round(frame_height * factor))),
            interpolation=cv2.INTER_AREA,
        ),
        dtype=np.uint8,
    )
    return _search(coarse, template.image, candidates, factor=factor)


def _refine(
    gray: NDArray[np.uint8],
    template: Template,
    candidates: tuple[float, ...],
    coarse: _Hit,
) -> _Hit | None:
    """Verify ``coarse`` at full resolution in a template-sized local window."""
    scale = coarse.scale
    width = max(1, round(template.width * scale))
    height = max(1, round(template.height * scale))
    frame_height, frame_width = gray.shape
    predicted_left = round(coarse.location[0] / COARSE_SEARCH_FACTOR)
    predicted_top = round(coarse.location[1] / COARSE_SEARCH_FACTOR)
    margin = max(1, round(max(width, height) * COARSE_SEARCH_FACTOR))
    left = max(0, predicted_left - margin)
    top = max(0, predicted_top - margin)
    right = min(frame_width, predicted_left + width + margin)
    bottom = min(frame_height, predicted_top + height + margin)
    if right - left < width or bottom - top < height:
        return None

    # Re-run every scale in the small local window: a coarse winner one or two
    # steps from the true scale must not decide the final scale.
    hit = _search(gray[top:bottom, left:right], template.image, candidates)
    if hit is None:
        return None
    return _Hit(
        score=hit.score,
        location=(left + hit.location[0], top + hit.location[1]),
        scale=hit.scale,
        size=hit.size,
        tried=tuple(dict.fromkeys((*coarse.tried, *hit.tried))),
    )


def _search(
    gray: NDArray[np.uint8],
    template_image: NDArray[np.uint8],
    candidates: Sequence[float],
    *,
    factor: float = 1.0,
) -> _Hit | None:
    """Search one resolution level and return its best non-flat hit."""
    frame = gray.astype(np.float32)
    integral = cv2.integral(frame)
    integral_squared = cv2.integral(frame * frame)
    frame_height, frame_width = gray.shape
    template_height, template_width = template_image.shape

    best: _Hit | None = None
    tried: list[float] = []
    seen_sizes: set[tuple[int, int]] = set()
    for scale in candidates:
        width = max(1, round(template_width * scale * factor))
        height = max(1, round(template_height * scale * factor))
        if width > frame_width or height > frame_height:
            continue
        if (width, height) in seen_sizes:
            continue
        seen_sizes.add((width, height))

        interpolation = cv2.INTER_AREA if scale * factor < 1.0 else cv2.INTER_LINEAR
        scaled = np.asarray(
            cv2.resize(template_image, (width, height), interpolation=interpolation),
            dtype=np.uint8,
        )
        if _is_flat(scaled):
            continue

        tried.append(scale)
        scores = cv2.matchTemplate(gray, scaled, cv2.TM_CCOEFF_NORMED)
        scores = np.where(_window_variance(integral, integral_squared, height, width) > ZERO_VARIANCE, scores, -np.inf)
        scores = np.where(np.isfinite(scores), scores, -np.inf)
        index = int(np.argmax(scores))
        score = float(scores.flat[index])
        if best is not None and score <= best.score:
            continue
        top, left = np.unravel_index(index, scores.shape)
        best = _Hit(
            score=score,
            location=(int(left), int(top)),
            scale=scale,
            size=(width, height),
            tried=tuple(tried),
        )

    if best is None:
        return None
    return _Hit(
        score=best.score,
        location=best.location,
        scale=best.scale,
        size=best.size,
        tried=tuple(tried),
    )


def _resolve_template(
    template: Template | str | Path | Image.Image | NDArray[np.generic],
    *,
    frame_width: int,
    source_width: int | None,
) -> Template:
    if isinstance(template, Template):
        if source_width is None or source_width == template.source_width:
            return template
        return Template(image=template.image, source_width=source_width, family=template.family, path=template.path)
    if isinstance(template, (str, Path)):
        return Template.from_path(
            template,
            source_width=source_width if source_width is not None else frame_width,
        )
    image = _to_gray_array(template)
    return Template(
        image=image,
        source_width=source_width if source_width is not None else frame_width,
    )


def _scale_candidates(
    template: Template,
    frame_width: int,
    scale_radius: float,
    scale_steps: int,
    scales: Sequence[float] | None,
) -> tuple[float, ...]:
    if scales is not None:
        if not scales:
            raise ValueError('scales must not be empty')
        candidates = tuple(float(scale) for scale in scales)
        if any(scale <= 0.0 for scale in candidates):
            raise ValueError('scales must be positive')
        return candidates
    if scale_radius < 0.0:
        raise ValueError(f'scale_radius must not be negative, got {scale_radius!r}')
    if scale_steps < 1:
        raise ValueError(f'scale_steps must be positive, got {scale_steps!r}')
    nominal = frame_width / template.source_width
    lower = max(nominal * (1.0 - scale_radius), np.finfo(np.float64).tiny)
    upper = nominal * (1.0 + scale_radius)
    if scale_steps == 1:
        return (nominal,)
    return tuple(float(scale) for scale in np.linspace(lower, upper, scale_steps))


def _window_variance(
    integral: NDArray[np.generic],
    integral_squared: NDArray[np.generic],
    height: int,
    width: int,
) -> NDArray[np.float64]:
    """Variance of every ``height x width`` frame window, in match-map order."""
    sums = np.asarray(integral, dtype=np.float64)
    squares = np.asarray(integral_squared, dtype=np.float64)
    total = _window_sum(sums, height, width)
    total_squared = _window_sum(squares, height, width)
    count = float(height * width)
    mean = total / count
    variance = total_squared / count - mean * mean
    return np.maximum(variance, 0.0)


def _window_sum(integral: NDArray[np.float64], height: int, width: int) -> NDArray[np.float64]:
    return (
        integral[height:, width:]
        - integral[:-height, width:]
        - integral[height:, :-width]
        + integral[:-height, :-width]
    )


def _frame_gray(frame: FrameLike) -> NDArray[np.uint8]:
    if isinstance(frame, Snapshot):
        return _to_gray_array(frame.image)
    if isinstance(frame, Image.Image):
        return np.asarray(frame.convert('L'), dtype=np.uint8)
    return _to_gray_array(frame)


def _to_gray_array(value: object) -> NDArray[np.uint8]:
    if isinstance(value, Image.Image):
        return np.asarray(value.convert('L'), dtype=np.uint8)
    array = np.asarray(value)
    if array.ndim == 2:
        return np.asarray(array, dtype=np.uint8)
    if array.ndim == 3 and array.shape[2] in (3, 4):
        return np.asarray(Image.fromarray(array[..., :3]).convert('L'), dtype=np.uint8)
    raise TemplateError(f'an image must be 2-D or RGB/RGBA, got shape {array.shape!r}')


def _is_flat(image: NDArray[np.uint8]) -> bool:
    return bool(image.size == 0 or float(np.var(image, dtype=np.float64)) <= ZERO_VARIANCE)


def _check_min_score(value: float) -> None:
    if not 0.0 <= value <= 1.0:
        raise ValueError(f'min_score must be between 0 and 1, got {value!r}')
