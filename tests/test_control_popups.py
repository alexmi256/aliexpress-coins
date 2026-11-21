"""Popup corpus loading and validation (issue #11).

`specs/popups.yaml` is data, so the tests do two jobs: they assert the committed
corpus is complete and internally consistent, and they feed deliberately
malformed documents through the loader to prove a bad row cannot reach a run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from PIL import Image

from aliexpress_coins.control.errors import PopupSpecError
from aliexpress_coins.control.popups import (
    CloseKind,
    PopupStatus,
    load_popup_corpus,
)

ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = ROOT / 'specs' / 'popups.yaml'
TEMPLATES = ROOT / 'assets' / 'templates'
FIXTURES = ROOT / 'tests' / 'data' / 'popups'


def _reference() -> dict[str, object]:
    return {
        'device': 'T3_US',
        'width': 1200,
        'height': 2000,
        'density': 240,
        'sdk': 35,
        'source': 'a test frame',
    }


def _policy() -> dict[str, object]:
    return {
        'escalation': ['template', 'label', 'fraction'],
        'on_exhausted': 'fail',
        'error': 'popup_unclosable',
    }


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        'family': 'demo',
        'when': 'a test popup',
        'anchor': 'Demo popup',
        'status': 'reproduced',
        'observed_at': {'width': 1200, 'height': 2000, 'density': 240, 'sdk': 35},
        'close': {'label': 'Close'},
    }
    row.update(overrides)
    return row


def _document(*, families: object | None = None, **overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        'version': 1,
        'reference': _reference(),
        'policy': _policy(),
        'families': [_row()] if families is None else families,
    }
    document.update(overrides)
    return document


def _write_object(tmp_path: Path, document: object, *, root: Path | None = None) -> Path:
    base = tmp_path if root is None else root
    specs = base / 'specs'
    specs.mkdir(parents=True, exist_ok=True)
    path = specs / 'popups.yaml'
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding='utf-8')
    return path


def _write_raw(tmp_path: Path, text: str, *, root: Path | None = None) -> Path:
    base = tmp_path if root is None else root
    specs = base / 'specs'
    specs.mkdir(parents=True, exist_ok=True)
    path = specs / 'popups.yaml'
    path.write_text(text, encoding='utf-8')
    return path


def _png(path: Path, size: tuple[int, int] = (8, 8)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', size, (0, 0, 0)).save(path)
    return path


def _template(tmp_path: Path, name: str = 'demo.png') -> Path:
    return _png(tmp_path / 'assets' / 'templates' / name)


def _fixture(tmp_path: Path, name: str = 'demo.png') -> Path:
    return _png(tmp_path / 'tests' / 'data' / 'popups' / name, size=(1200, 2000))


def test_the_committed_corpus_loads_and_names_every_discovered_family() -> None:
    corpus = load_popup_corpus(CORPUS_PATH)

    assert corpus.version == 1
    assert corpus.error == 'popup_unclosable'
    assert corpus.escalation == (CloseKind.TEMPLATE, CloseKind.LABEL, CloseKind.FRACTION)
    assert [family.family for family in corpus.families] == [
        'launch_welcome_gifts',
        'promo_coupon_unlocked',
        'promo_bundle_deals_badge',
        'coins_boost_sheet',
        'push_permission_prompt',
        'system_notification_permission',
        'redeem_interstitial',
    ]
    assert corpus.reference.device == 'T3_US'
    assert corpus.reference.width == 1200
    assert corpus.reference.height == 2000
    assert corpus.reference.density == 240
    assert corpus.reference.sdk == 35
    assert corpus.note is not None
    assert 'popup_unclosable' in corpus.note


def test_every_family_has_an_executable_attempt_in_policy_order() -> None:
    corpus = load_popup_corpus(CORPUS_PATH)

    for family in corpus.families:
        assert family.close, family.family
        assert family.when
        assert family.anchor
        kinds = [attempt.kind for attempt in family.close]
        assert kinds == sorted(kinds, key=list(_order()).index), family.family
        for attempt in family.close:
            if attempt.kind is CloseKind.TEMPLATE:
                assert attempt.template is not None
                assert attempt.source_width == corpus.reference.width
                assert attempt.label is None
                assert attempt.fraction is None
            elif attempt.kind is CloseKind.LABEL:
                assert attempt.label
                assert attempt.template is None
                assert attempt.fraction is None
            elif attempt.kind is CloseKind.FRACTION:
                assert attempt.fraction is not None
                assert 0.0 <= attempt.fraction[0] <= 1.0
                assert 0.0 <= attempt.fraction[1] <= 1.0
                assert attempt.template is None
                assert attempt.label is None
            else:
                assert attempt.reason
                assert family.is_unclosable


def _order() -> tuple[CloseKind, ...]:
    return (CloseKind.TEMPLATE, CloseKind.LABEL, CloseKind.FRACTION, CloseKind.FAIL)


def test_templates_are_all_referenced_and_fit_the_reference_frame() -> None:
    corpus = load_popup_corpus(CORPUS_PATH)
    referenced = {
        attempt.template
        for family in corpus.families
        for attempt in family.close
        if attempt.kind is CloseKind.TEMPLATE and attempt.template is not None
    }

    assert referenced == {path.resolve() for path in TEMPLATES.glob('*.png')}
    for path in referenced:
        with Image.open(path) as image:
            assert image.width <= corpus.reference.width
            assert image.height <= corpus.reference.height


def test_popup_fixtures_are_full_frames_at_the_reference_geometry() -> None:
    corpus = load_popup_corpus(CORPUS_PATH)

    assert corpus.by_family('redeem_interstitial').fixture is None
    for family in corpus.families:
        if family.fixture is None:
            continue
        with Image.open(family.fixture) as image:
            assert image.size == (corpus.reference.width, corpus.reference.height), family.family

    for path in FIXTURES.glob('*.png'):
        with Image.open(path) as image:
            assert image.size == (1200, 2000), path.name


def test_the_redeem_interstitial_is_a_loud_failure_not_a_guess() -> None:
    family = load_popup_corpus(CORPUS_PATH).by_family('redeem_interstitial')

    assert family.is_unclosable
    assert [attempt.kind for attempt in family.close] == [CloseKind.FAIL]
    assert family.close[0].reason
    assert 'BACK' in family.close[0].reason


def test_glyph_only_families_use_a_template_and_text_families_use_a_label() -> None:
    corpus = load_popup_corpus(CORPUS_PATH)

    launch = corpus.by_family('launch_welcome_gifts')
    assert [attempt.kind for attempt in launch.close] == [CloseKind.TEMPLATE, CloseKind.FRACTION]
    assert launch.close[1].fraction == (0.499, 0.833)

    system = corpus.by_family('system_notification_permission')
    assert [attempt.kind for attempt in system.close] == [CloseKind.LABEL]
    assert system.close[0].label == "Don't allow"

    push = corpus.by_family('push_permission_prompt')
    assert [attempt.kind for attempt in push.close] == [CloseKind.TEMPLATE, CloseKind.LABEL]
    assert push.close[1].label == 'No thanks'


def test_status_records_which_families_were_reproduced() -> None:
    corpus = load_popup_corpus(CORPUS_PATH)

    assert corpus.by_family('coins_boost_sheet').status is PopupStatus.REPRODUCED
    assert corpus.by_family('launch_welcome_gifts').status is PopupStatus.OBSERVED_NOT_REPRODUCED
    assert corpus.by_family('redeem_interstitial').status is PopupStatus.OBSERVED_NOT_REPRODUCED


def test_family_lookup_and_chain_expansion() -> None:
    corpus = load_popup_corpus(CORPUS_PATH)

    launch = corpus.by_family('launch_welcome_gifts')
    assert launch.chains_to == ('promo_bundle_deals_badge',)
    assert [family.family for family in corpus.chain_of('launch_welcome_gifts')] == ['promo_bundle_deals_badge']
    assert [family.family for family in corpus.chain_of('promo_coupon_unlocked')] == ['promo_bundle_deals_badge']
    assert corpus.chain_of('coins_boost_sheet') == ()
    with pytest.raises(KeyError, match='unknown popup family'):
        corpus.by_family('missing')
    with pytest.raises(KeyError, match='unknown popup family'):
        corpus.chain_of('missing')


def test_default_root_is_derived_from_a_specs_directory(tmp_path: Path) -> None:
    _template(tmp_path)
    document = _document(
        families=[
            _row(
                fixture='tests/data/popups/demo.png',
                close={'template': 'assets/templates/demo.png', 'source_width': 1200},
            ),
        ],
    )
    _fixture(tmp_path)
    path = _write_object(tmp_path, document)

    corpus = load_popup_corpus(path)

    attempt = corpus.by_family('demo').close[0]
    assert attempt.template == (tmp_path / 'assets' / 'templates' / 'demo.png').resolve()
    assert corpus.by_family('demo').fixture == (tmp_path / 'tests' / 'data' / 'popups' / 'demo.png').resolve()


def test_explicit_root_separates_the_corpus_from_the_repository(tmp_path: Path) -> None:
    repository = tmp_path / 'repo'
    _template(repository)
    path = _write_object(
        tmp_path,
        _document(families=[_row(close={'template': 'assets/templates/demo.png', 'source_width': 1200})]),
    )

    corpus = load_popup_corpus(path, root=repository)

    assert corpus.by_family('demo').close[0].template == (repository / 'assets' / 'templates' / 'demo.png').resolve()


def test_default_root_without_a_specs_directory_is_the_corpus_directory(tmp_path: Path) -> None:
    _template(tmp_path)
    path = tmp_path / 'popups.yaml'
    path.write_text(
        yaml.safe_dump(
            _document(families=[_row(close={'template': 'assets/templates/demo.png', 'source_width': 1200})]),
        ),
        encoding='utf-8',
    )

    corpus = load_popup_corpus(path)

    assert corpus.by_family('demo').close[0].template == (tmp_path / 'assets' / 'templates' / 'demo.png').resolve()


def test_a_missing_corpus_is_a_spec_error(tmp_path: Path) -> None:
    with pytest.raises(PopupSpecError, match='not found'):
        load_popup_corpus(tmp_path / 'missing.yaml')


def test_an_unreadable_corpus_is_a_spec_error(tmp_path: Path) -> None:
    with pytest.raises(PopupSpecError, match='could not read'):
        load_popup_corpus(tmp_path)


def test_malformed_yaml_is_a_spec_error(tmp_path: Path) -> None:
    path = _write_raw(tmp_path, '[unclosed\n')

    with pytest.raises(PopupSpecError, match='invalid YAML'):
        load_popup_corpus(path)


def test_a_non_utf8_corpus_is_a_spec_error(tmp_path: Path) -> None:
    path = tmp_path / 'popups.yaml'
    path.write_bytes(b'\xff\xfe')

    with pytest.raises(PopupSpecError, match='not valid UTF-8'):
        load_popup_corpus(path)


def test_a_non_mapping_document_is_rejected(tmp_path: Path) -> None:
    path = _write_object(tmp_path, ['not', 'a', 'mapping'])

    with pytest.raises(PopupSpecError, match='corpus must be a mapping'):
        load_popup_corpus(path)


def test_non_string_keys_are_rejected(tmp_path: Path) -> None:
    path = _write_raw(tmp_path, '1: value\n')

    with pytest.raises(PopupSpecError, match='non-string key'):
        load_popup_corpus(path)


@pytest.mark.parametrize(
    ('overrides', 'message'),
    [
        ({'version': True}, 'version must be an integer'),
        ({'version': 0}, 'version must be positive'),
        ({'reference': {'device': 'x', 'width': 1200}}, 'height must be an integer'),
        (
            {'reference': {'device': 'x', 'width': 0, 'height': 2000, 'density': 240, 'sdk': 35, 'source': 's'}},
            'width must be positive',
        ),
        (
            {'policy': {'escalation': ['template', 'teleport'], 'on_exhausted': 'fail', 'error': 'e'}},
            'unknown close kind',
        ),
        (
            {'policy': {'escalation': ['label', 'template', 'fraction'], 'on_exhausted': 'fail', 'error': 'e'}},
            'policy.escalation must be',
        ),
        (
            {'policy': {'escalation': ['template', 'label', 'fraction'], 'on_exhausted': 'retry', 'error': 'e'}},
            "on_exhausted must be 'fail'",
        ),
        (
            {'policy': {'escalation': ['template', 'label', 'fraction'], 'on_exhausted': 'fail', 'error': '  '}},
            'must not be empty',
        ),
        ({'families': 'not a list'}, 'families must be a list'),
        ({'families': []}, 'at least one row'),
        ({'extra': True}, 'unknown keys'),
    ],
)
def test_document_level_errors_are_rejected(tmp_path: Path, overrides: dict[str, object], message: str) -> None:
    path = _write_object(tmp_path, _document(**overrides))

    with pytest.raises(PopupSpecError, match=message):
        load_popup_corpus(path)


@pytest.mark.parametrize(
    ('close', 'message'),
    [
        ({}, 'must contain template, label, fraction or fail'),
        ({'strategy': 'teleport'}, 'unknown keys'),
        ({'source_width': 1200}, 'source_width requires a template'),
        ({'template': 'assets/templates/demo.png'}, 'source_width must be an integer'),
        ({'template': 'assets/templates/demo.png', 'source_width': True}, 'source_width must be an integer'),
        ({'template': 'assets/templates/demo.png', 'source_width': 1200, 'label': '  '}, 'must not be empty'),
        ({'template': 'assets/templates/demo.png', 'source_width': 1200, 'fraction': [0.5]}, 'exactly two'),
        ({'template': 'assets/templates/demo.png', 'source_width': 1200, 'fraction': [0.5, 1.5]}, 'between 0 and 1'),
        ({'template': 'assets/templates/demo.png', 'source_width': 1200, 'fraction': ['x', 0.5]}, 'must be a number'),
        ({'template': 'assets/templates/demo.png', 'source_width': 1200, 'fraction': [True, 0.5]}, 'must be a number'),
        (
            {'template': 'assets/templates/demo.png', 'source_width': 1200, 'fraction': [float('nan'), 0.5]},
            'between 0 and 1',
        ),
        ({'fail': '  '}, 'must not be empty'),
        ({'fail': 'a reason', 'label': 'Close'}, 'must be the only key'),
        ('not a mapping', 'must be a mapping'),
    ],
)
def test_close_errors_are_rejected(tmp_path: Path, close: object, message: str) -> None:
    _template(tmp_path)
    path = _write_object(tmp_path, _document(families=[_row(close=close)]))

    with pytest.raises(PopupSpecError, match=message):
        load_popup_corpus(path)


@pytest.mark.parametrize(
    ('close', 'message'),
    [
        ({'template': 'elsewhere/demo.png', 'source_width': 1200}, 'must live under'),
        ({'template': 'assets/templates/demo.jpg', 'source_width': 1200}, 'must be a .png'),
        ({'template': 'assets/templates/missing.png', 'source_width': 1200}, 'does not exist'),
    ],
)
def test_template_paths_are_rejected(tmp_path: Path, close: dict[str, object], message: str) -> None:
    path = _write_object(tmp_path, _document(families=[_row(close=close)]))

    with pytest.raises(PopupSpecError, match=message):
        load_popup_corpus(path)


def test_an_unreadable_template_is_rejected(tmp_path: Path) -> None:
    broken = tmp_path / 'assets' / 'templates' / 'demo.png'
    broken.parent.mkdir(parents=True)
    broken.write_text('not a png', encoding='utf-8')
    path = _write_object(
        tmp_path,
        _document(families=[_row(close={'template': 'assets/templates/demo.png', 'source_width': 1200})]),
    )

    with pytest.raises(PopupSpecError, match='not a readable PNG'):
        load_popup_corpus(path)


@pytest.mark.parametrize(
    ('overrides', 'message'),
    [
        ({'family': 'Demo'}, 'snake_case'),
        ({'status': 'maybe'}, 'status must be one of'),
        ({'when': '  '}, 'must not be empty'),
        ({'anchor': ''}, 'must not be empty'),
        ({'observed_at': {'width': 1200, 'height': 2000, 'density': 240}}, 'sdk must be an integer'),
        ({'observed_at': {'width': 1200, 'height': 2000, 'density': 0, 'sdk': 35}}, 'density must be positive'),
        ({'chains_to': ['demo']}, 'cannot chain to itself'),
        ({'chains_to': ['demo', 'demo']}, 'must not repeat'),
        ({'chains_to': ['ghost']}, 'chains to unknown family'),
        ({'notes': 7}, 'notes must be a string'),
        ({'extra': 'x'}, 'unknown keys'),
        ({'fixture': 'elsewhere/demo.png'}, 'must be a .png under'),
        ({'fixture': 'tests/data/popups/missing.png'}, 'does not exist'),
    ],
)
def test_family_errors_are_rejected(tmp_path: Path, overrides: dict[str, object], message: str) -> None:
    path = _write_object(tmp_path, _document(families=[_row(**overrides)]))

    with pytest.raises(PopupSpecError, match=message):
        load_popup_corpus(path)


def test_an_unreadable_fixture_is_rejected(tmp_path: Path) -> None:
    broken = tmp_path / 'tests' / 'data' / 'popups' / 'demo.png'
    broken.parent.mkdir(parents=True)
    broken.write_text('not a png', encoding='utf-8')
    path = _write_object(tmp_path, _document(families=[_row(fixture='tests/data/popups/demo.png')]))

    with pytest.raises(PopupSpecError, match='not a readable PNG'):
        load_popup_corpus(path)


def test_duplicate_families_are_rejected(tmp_path: Path) -> None:
    path = _write_object(tmp_path, _document(families=[_row(), _row()]))

    with pytest.raises(PopupSpecError, match='duplicate popup families'):
        load_popup_corpus(path)


def test_a_family_row_must_be_a_mapping(tmp_path: Path) -> None:
    path = _write_object(tmp_path, _document(families=['not a row']))

    with pytest.raises(PopupSpecError, match='must be a mapping'):
        load_popup_corpus(path)
