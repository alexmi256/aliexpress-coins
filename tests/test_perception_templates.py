"""Scale-aware glyph matching, its guards and its data-driven store (issue #15)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml
from numpy.typing import NDArray
from PIL import Image

from aliexpress_coins.control.popups import PopupCorpus, load_popup_corpus
from aliexpress_coins.perception import templates as templates_module
from aliexpress_coins.perception.errors import TemplateError
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.snapshot import Snapshot
from aliexpress_coins.perception.templates import (
    DEFAULT_TEMPLATE_THRESHOLD,
    SCALE_SEARCH_STEPS,
    Template,
    TemplateStore,
    load_templates,
    match_template,
)

ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = ROOT / 'specs' / 'popups.yaml'
POPUP_FIXTURES = ROOT / 'tests' / 'data' / 'popups'
TEMPLATE_DIR = ROOT / 'assets' / 'templates'
LAUNCH_FIXTURE = POPUP_FIXTURES / 'launch_welcome_gifts.png'
LAUNCH_TEMPLATE = TEMPLATE_DIR / 'launch_welcome_gifts.png'
CAPTURED_AT = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


@pytest.fixture(scope='module')
def corpus() -> PopupCorpus:
    return load_popup_corpus(CORPUS_PATH)


@pytest.fixture(scope='module')
def store(corpus: PopupCorpus) -> TemplateStore:
    return load_templates(corpus)


@pytest.fixture(scope='module')
def launch_frame() -> Image.Image:
    with Image.open(LAUNCH_FIXTURE) as opened:
        return opened.convert('L').copy()


def _gray(path: Path) -> NDArray[np.uint8]:
    with Image.open(path) as opened:
        return np.asarray(opened.convert('L'), dtype=np.uint8)


def _colour_frame(path: Path) -> NDArray[np.uint8]:
    with Image.open(path) as opened:
        return np.asarray(opened.convert('RGB'), dtype=np.uint8)


@dataclass(frozen=True)
class _Attempt:
    kind: str
    template: Path | None = None
    source_width: int | None = None


@dataclass(frozen=True)
class _Family:
    family: str
    close: tuple[_Attempt, ...]


@dataclass(frozen=True)
class _Corpus:
    families: tuple[_Family, ...]


def _write_corpus(root: Path, template: Path, fixture: Path, family: str = 'new_family') -> Path:
    specs = root / 'specs'
    specs.mkdir(parents=True, exist_ok=True)
    document = {
        'version': 1,
        'reference': {
            'device': 'test',
            'width': 1200,
            'height': 2000,
            'density': 240,
            'sdk': 35,
            'source': 'a test frame',
        },
        'policy': {
            'escalation': ['label', 'template', 'fraction'],
            'on_exhausted': 'fail',
            'error': 'popup_unclosable',
        },
        'families': [
            {
                'family': family,
                'when': 'a new popup',
                'anchor': 'New popup',
                'status': 'reproduced',
                'observed_at': {'width': 1200, 'height': 2000, 'density': 240, 'sdk': 35},
                'fixture': f'tests/data/popups/{fixture.name}',
                'close': {
                    'template': f'assets/templates/{template.name}',
                    'source_width': 1200,
                },
            },
        ],
    }
    path = specs / 'popups.yaml'
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding='utf-8')
    return path


def _synthetic_glyph(size: int = 16) -> NDArray[np.uint8]:
    glyph = np.full((size, size), 32, dtype=np.uint8)
    glyph[2:-2, 2:-2] = 220
    glyph[size // 2 - 1 : size // 2 + 1, :] = 10
    glyph[:, size // 2 - 1 : size // 2 + 1] = 10
    return glyph


def _place(
    glyph: NDArray[np.uint8], *, width: int = 1200, height: int = 2000, left: int = 100, top: int = 200
) -> NDArray[np.uint8]:
    frame = np.full((height, width), 128, dtype=np.uint8)
    glyph_height, glyph_width = glyph.shape
    frame[top : top + glyph_height, left : left + glyph_width] = glyph
    return frame


def test_the_store_is_built_from_corpus_rows_and_records_source_width(
    corpus: PopupCorpus,
    store: TemplateStore,
) -> None:
    assert len(store) == 5
    assert {template.family for template in store} == {
        family.family for family in corpus.families for attempt in family.close if attempt.kind == 'template'
    }
    for template in store:
        assert template.source_width == corpus.reference.width
        assert template.path is not None
        assert template.path.parent == TEMPLATE_DIR
        assert template.image.ndim == 2
        assert template.width > 0
        assert template.height > 0


def test_the_launch_modal_close_is_found_on_the_real_fixture(
    store: TemplateStore,
    launch_frame: Image.Image,
) -> None:
    match = match_template(launch_frame, store.by_family('launch_welcome_gifts'))

    assert match is not None
    assert match.score >= DEFAULT_TEMPLATE_THRESHOLD
    assert match.score > 0.99
    assert match.location == (532, 1600)
    assert match.scale == pytest.approx(1.0)
    assert match.size == (134, 133)
    assert match.box == (532, 1600, 666, 1733)
    assert match.center == (599.0, 1666.5)
    assert 1.0 in match.scales_tried
    assert len(match.scales_tried) == SCALE_SEARCH_STEPS


def test_a_crop_cut_at_1200_still_matches_a_1080_wide_frame(
    store: TemplateStore,
    launch_frame: Image.Image,
) -> None:
    frame = launch_frame.convert('RGB').resize((1080, 1800), Image.Resampling.LANCZOS)

    match = match_template(frame, store.by_family('launch_welcome_gifts'))

    assert match is not None
    assert match.score >= DEFAULT_TEMPLATE_THRESHOLD
    assert match.scale == pytest.approx(0.9, abs=0.01)
    assert match.location[0] == pytest.approx(479, abs=3)
    assert match.location[1] == pytest.approx(1440, abs=3)


def test_a_wrong_glyph_scores_below_the_threshold(
    store: TemplateStore,
    launch_frame: Image.Image,
) -> None:
    coupon = store.by_family('promo_coupon_unlocked')

    assert match_template(launch_frame, coupon) is None
    scored = match_template(launch_frame, coupon, min_score=0.0)
    assert scored is not None
    assert scored.score < DEFAULT_TEMPLATE_THRESHOLD
    assert scored.score == pytest.approx(0.791, abs=0.01)


@pytest.mark.parametrize('value', [255, 0])
def test_a_uniform_template_never_matches(
    value: int,
    launch_frame: Image.Image,
) -> None:
    flat = np.full((32, 32), value, dtype=np.uint8)

    assert match_template(launch_frame, flat) is None
    assert match_template(launch_frame, Image.fromarray(flat)) is None


@pytest.mark.parametrize('value', [255, 0])
def test_a_uniform_frame_never_matches(value: int, store: TemplateStore) -> None:
    frame = np.full((2000, 1200), value, dtype=np.uint8)

    assert match_template(frame, store.by_family('launch_welcome_gifts')) is None


def test_blanking_the_control_region_removes_the_match(
    store: TemplateStore,
    launch_frame: Image.Image,
) -> None:
    frame = np.asarray(launch_frame).copy()
    frame[1580:1750, 510:690] = 255

    assert match_template(frame, store.by_family('launch_welcome_gifts')) is None


def test_ncc_matches_the_same_glyph_over_a_light_sheet_and_a_dark_scrim() -> None:
    glyph = _gray(LAUNCH_TEMPLATE)
    template = Template(image=glyph, source_width=400)
    light = np.clip(glyph.astype(np.float32) * 0.5 + 127.5, 0, 255).astype(np.uint8)
    dark = np.clip(glyph.astype(np.float32) * 0.5, 0, 255).astype(np.uint8)

    light_match = match_template(_place(light, width=400, height=400, left=120, top=100), template)
    dark_match = match_template(_place(dark, width=400, height=400, left=120, top=100), template)

    assert light_match is not None
    assert dark_match is not None
    assert light_match.score > 0.99
    assert dark_match.score > 0.99
    assert light_match.location == (120, 100)
    assert dark_match.location == (120, 100)


def test_an_inverted_glyph_is_not_silently_accepted() -> None:
    # NCC is invariant to brightness and contrast, not to a polarity reversal.
    # The corpus stores crops in the app's rendering, so an inverted crop is a
    # different template, not the same glyph over a different scrim.
    glyph = _gray(LAUNCH_TEMPLATE)
    template = Template(image=glyph, source_width=400)
    inverted = (255 - glyph).astype(np.uint8)

    assert match_template(_place(inverted, width=400, height=400, left=120, top=100), template) is None
    scored = match_template(
        _place(inverted, width=400, height=400, left=120, top=100),
        template,
        min_score=0.0,
    )
    assert scored is not None
    assert scored.score < DEFAULT_TEMPLATE_THRESHOLD


def test_adding_a_template_is_a_file_drop_and_a_rule_row(tmp_path: Path) -> None:
    glyph = _synthetic_glyph()
    template_path = tmp_path / 'assets' / 'templates' / 'new_family.png'
    template_path.parent.mkdir(parents=True)
    Image.fromarray(glyph).save(template_path)
    fixture_path = tmp_path / 'tests' / 'data' / 'popups' / 'new_family.png'
    fixture_path.parent.mkdir(parents=True)
    Image.fromarray(_place(glyph)).save(fixture_path)
    corpus_path = _write_corpus(tmp_path, template_path, fixture_path)

    corpus = load_popup_corpus(corpus_path)
    store = load_templates(corpus)
    match = store.match(_place(glyph), 'new_family')

    assert len(store) == 1
    assert store.by_family('new_family').source_width == 1200
    assert match is not None
    assert match.score >= DEFAULT_TEMPLATE_THRESHOLD
    assert match.location == (100, 200)


def test_a_family_without_a_crop_is_still_a_lookup_error(store: TemplateStore) -> None:
    with pytest.raises(KeyError, match='unknown template family'):
        store.by_family('missing')


def test_match_all_returns_every_acceptable_family_best_first(
    store: TemplateStore,
    launch_frame: Image.Image,
) -> None:
    matches = store.match_all(launch_frame)

    assert matches
    assert [match.template.family for match in matches][:2] == [
        'launch_welcome_gifts',
        'promo_bundle_deals_badge',
    ]
    assert [match.score for match in matches] == sorted((match.score for match in matches), reverse=True)
    assert all(match.score >= DEFAULT_TEMPLATE_THRESHOLD for match in matches)


@pytest.mark.parametrize('template', [LAUNCH_TEMPLATE, str(LAUNCH_TEMPLATE)])
def test_a_path_template_uses_the_frame_width_when_source_width_is_omitted(
    template: Path | str,
    launch_frame: Image.Image,
) -> None:
    match = match_template(launch_frame, template)

    assert match is not None
    assert match.score > 0.99
    assert match.location == (532, 1600)
    assert match.scale == pytest.approx(1.0)


def test_refinement_reconsiders_every_scale_not_just_the_coarse_neighbours(
    corpus: PopupCorpus,
    store: TemplateStore,
) -> None:
    family = corpus.by_family('push_permission_prompt')
    assert family.fixture is not None
    with Image.open(family.fixture) as opened:
        frame = opened.convert('L')

    match = match_template(frame, store.by_family('push_permission_prompt'))

    assert match is not None
    assert match.score > 0.99
    assert match.scale == pytest.approx(1.0)
    assert match.location == (1147, 220)


def test_explicit_scales_are_the_only_scales_tried(
    store: TemplateStore,
    launch_frame: Image.Image,
) -> None:
    match = match_template(
        launch_frame,
        store.by_family('launch_welcome_gifts'),
        scales=(1.0,),
    )

    assert match is not None
    assert match.scale == 1.0
    assert match.scales_tried == (1.0,)


def test_scale_steps_of_one_uses_only_the_nominal_scale(
    store: TemplateStore,
    launch_frame: Image.Image,
) -> None:
    match = match_template(
        launch_frame,
        store.by_family('launch_welcome_gifts'),
        scale_steps=1,
    )

    assert match is not None
    assert match.scale == pytest.approx(1.0)
    assert match.scales_tried == pytest.approx((1.0,))


def test_duplicate_rounded_template_sizes_are_searched_once() -> None:
    glyph = _synthetic_glyph()
    frame = _place(glyph, width=100, height=100, left=10, top=20)

    match = match_template(frame, glyph, scales=(1.0, 1.0001))

    assert match is not None
    assert match.scales_tried == (1.0,)


def test_a_target_smaller_than_the_template_returns_none(store: TemplateStore) -> None:
    tiny = np.arange(9, dtype=np.uint8).reshape(3, 3)
    assert match_template(tiny, store.by_family('launch_welcome_gifts')) is None


def test_a_small_frame_and_glyph_still_use_the_full_resolution_fallback() -> None:
    glyph = np.array(
        [
            [0, 0, 255],
            [0, 255, 0],
            [255, 0, 0],
        ],
        dtype=np.uint8,
    )
    frame = _place(glyph, width=40, height=40, left=12, top=14)

    match = match_template(frame, glyph)

    assert match is not None
    assert match.location[0] == pytest.approx(12, abs=1)
    assert match.location[1] == pytest.approx(14, abs=1)


def test_an_invalid_scale_is_skipped_without_stopping_the_search(
    store: TemplateStore,
    launch_frame: Image.Image,
) -> None:
    match = match_template(
        launch_frame,
        store.by_family('launch_welcome_gifts'),
        scales=(0.001, 1.0),
    )

    assert match is not None
    assert match.scale == 1.0


@pytest.mark.parametrize(
    'frame',
    [
        np.zeros((20, 20), dtype=np.uint8),
    ],
)
def test_an_empty_frame_is_not_searched(frame: NDArray[np.uint8]) -> None:
    assert match_template(frame[:0, :0], np.ones((2, 2), dtype=np.uint8)) is None


def test_a_flat_window_is_masked_in_the_variance_map() -> None:
    frame = np.full((20, 30), 200, dtype=np.uint8)
    frame[10:20, 20:30] = np.arange(100, dtype=np.uint8).reshape(10, 10)
    values = frame.astype(np.float32)
    integral = cv2.integral(values)
    integral_squared = cv2.integral(values * values)

    variance = templates_module._window_variance(integral, integral_squared, 5, 5)

    assert variance[0, 0] == pytest.approx(0.0)
    assert float(variance.max()) > 0.0


def test_a_template_that_becomes_flat_after_resizing_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    glyph = _synthetic_glyph()
    frame = _place(glyph, width=100, height=100, left=10, top=20)

    def flat_resize(_source: object, size: tuple[int, int], **_kwargs: object) -> NDArray[np.uint8]:
        return np.zeros((size[1], size[0]), dtype=np.uint8)

    monkeypatch.setattr(cv2, 'resize', flat_resize)

    assert templates_module._search(frame, glyph, (1.0,)) is None


def test_a_non_finite_score_map_never_becomes_a_match(
    monkeypatch: pytest.MonkeyPatch,
    store: TemplateStore,
    launch_frame: Image.Image,
) -> None:
    def nan_scores(frame: NDArray[np.uint8], template: NDArray[np.uint8], _method: int) -> NDArray[np.float32]:
        frame_height, frame_width = frame.shape[:2]
        template_height, template_width = template.shape[:2]
        shape = (frame_height - template_height + 1, frame_width - template_width + 1)
        return np.full(shape, np.nan, dtype=np.float32)

    monkeypatch.setattr(cv2, 'matchTemplate', nan_scores)

    assert match_template(launch_frame, store.by_family('launch_welcome_gifts')) is None


def test_a_failed_refinement_falls_back_to_the_full_resolution_search(
    monkeypatch: pytest.MonkeyPatch,
    store: TemplateStore,
    launch_frame: Image.Image,
) -> None:
    original = templates_module._search
    calls = 0

    def fail_the_refinement(
        gray: NDArray[np.uint8],
        template_image: NDArray[np.uint8],
        candidates: tuple[float, ...],
        *,
        factor: float = 1.0,
    ) -> templates_module._Hit | None:
        nonlocal calls
        calls += 1
        if calls == 2:
            return None
        return original(gray, template_image, candidates, factor=factor)

    monkeypatch.setattr(templates_module, '_search', fail_the_refinement)

    match = match_template(launch_frame, store.by_family('launch_welcome_gifts'))

    assert match is not None
    assert match.location == (532, 1600)
    assert calls >= 3


def test_a_coarse_hit_outside_the_frame_falls_back_to_full_search(
    monkeypatch: pytest.MonkeyPatch,
    store: TemplateStore,
    launch_frame: Image.Image,
) -> None:
    original = templates_module._coarse_search

    def misplaced(*_args: object, **_kwargs: object) -> templates_module._Hit:
        return templates_module._Hit(
            score=0.99,
            location=(100_000, 100_000),
            scale=1.0,
            size=(10, 10),
            tried=(1.0,),
        )

    monkeypatch.setattr(templates_module, '_coarse_search', misplaced)
    try:
        match = match_template(launch_frame, store.by_family('launch_welcome_gifts'))
    finally:
        monkeypatch.setattr(templates_module, '_coarse_search', original)

    assert match is not None
    assert match.location == (532, 1600)


def test_the_matcher_accepts_pil_colour_arrays_rgba_and_a_snapshot(launch_frame: Image.Image) -> None:
    rgb = _colour_frame(LAUNCH_FIXTURE)
    rgba = np.dstack((rgb, np.full(rgb.shape[:2], 255, dtype=np.uint8)))
    snapshot = Snapshot(
        image=launch_frame.convert('RGB'),
        boxes=(),
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(1200, 2000, density=240, sdk=35),
    )
    template = Template.from_path(LAUNCH_TEMPLATE, source_width=1200)

    for frame in (launch_frame, launch_frame.convert('RGB'), np.asarray(launch_frame), rgb, rgba, snapshot):
        match = match_template(frame, template)
        assert match is not None
        assert match.location == (532, 1600)


def test_template_from_path_defaults_the_source_width_to_the_crop_width() -> None:
    template = Template.from_path(LAUNCH_TEMPLATE)

    assert template.source_width == template.width == 134
    assert template.family == 'launch_welcome_gifts'
    assert template.path == LAUNCH_TEMPLATE


def test_a_source_width_can_override_a_loaded_template() -> None:
    glyph = _synthetic_glyph()
    template = Template(image=glyph, source_width=1200)
    frame = _place(glyph, width=100, height=100, left=10, top=20)

    match = match_template(frame, template, source_width=100)

    assert match is not None
    assert match.location == (10, 20)


def test_an_empty_crop_file_is_a_template_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    empty = Image.fromarray(np.empty((0, 0), dtype=np.uint8))

    class _Opened:
        def __enter__(self) -> Image.Image:
            return empty

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(Image, 'open', lambda _path: _Opened())

    with pytest.raises(TemplateError, match='template is empty'):
        Template.from_path(tmp_path / 'empty.png')


def test_an_unreadable_crop_is_a_template_error(tmp_path: Path) -> None:
    broken = tmp_path / 'broken.png'
    broken.write_text('not a png', encoding='utf-8')

    with pytest.raises(TemplateError, match='could not read template'):
        Template.from_path(broken)


@pytest.mark.parametrize(
    ('image', 'source_width', 'message'),
    [
        (np.empty((0, 0), dtype=np.uint8), 1200, 'must not be empty'),
        (np.ones((2, 2), dtype=np.uint8), 0, 'source_width must be positive'),
        (np.ones((2, 2, 2), dtype=np.uint8), 1200, 'must be 2-D'),
    ],
)
def test_invalid_templates_are_rejected(
    image: NDArray[np.uint8],
    source_width: int,
    message: str,
) -> None:
    with pytest.raises(TemplateError, match=message):
        Template(image=image, source_width=source_width)


def test_a_store_cannot_be_empty() -> None:
    with pytest.raises(TemplateError, match='cannot be empty'):
        TemplateStore(())


@pytest.mark.parametrize(
    ('attempts', 'message'),
    [
        ((_Attempt('template'),), 'without a path or source_width'),
        ((_Attempt('template', Path('a.png')),), 'without a path or source_width'),
        ((_Attempt('label'),), 'references no templates'),
    ],
)
def test_a_broken_or_empty_template_corpus_is_rejected(
    attempts: tuple[_Attempt, ...],
    message: str,
) -> None:
    with pytest.raises(TemplateError, match=message):
        load_templates(_Corpus((_Family('demo', attempts),)))


@pytest.mark.parametrize('value', [-0.1, 1.1])
def test_the_match_threshold_is_a_fraction(value: float, launch_frame: Image.Image) -> None:
    with pytest.raises(ValueError, match='min_score'):
        match_template(launch_frame, LAUNCH_TEMPLATE, min_score=value)


@pytest.mark.parametrize(
    ('options', 'message'),
    [
        ({'scales': ()}, 'scales must not be empty'),
        ({'scales': (1.0, 0.0)}, 'scales must be positive'),
        ({'scale_radius': -0.1}, 'scale_radius must not be negative'),
        ({'scale_steps': 0}, 'scale_steps must be positive'),
    ],
)
def test_scale_search_parameters_are_validated(
    options: dict[str, object],
    message: str,
    launch_frame: Image.Image,
) -> None:
    with pytest.raises(ValueError, match=message):
        match_template(launch_frame, LAUNCH_TEMPLATE, **options)  # type: ignore[arg-type]
