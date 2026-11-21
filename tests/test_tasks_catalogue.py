"""Task catalogue loading, validation and default-deny policy (issue #21).

The tests do three jobs:

* assert the committed catalogue covers the measured allowed families and the
  brief's refused task types, with the exact reason a run report will print;
* prove deny beats allow and that an unknown label is refused;
* feed deliberately broken documents through the loader to show the errors land
  at startup (or in CI), not in the nightly run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aliexpress_coins.tasks import (
    TaskCatalogueError,
    TaskCategory,
    load_task_catalogue,
)
from aliexpress_coins.tasks.catalogue import _contains

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE_PATH = ROOT / 'specs' / 'tasks.yaml'

BRIEF_REFUSALS: tuple[tuple[str, str, str], ...] = (
    ('Play & Earn', 'game_play_and_earn', 'game: Play & Earn is a mini-game and is never played'),
    ('Merge Boss', 'game_merge_boss', 'game: Merge Boss is a mini-game and is never played'),
    ('GoGo Match', 'game_gogo_match', 'game: GoGo Match is a mini-game and is never played'),
    ('Prize Land', 'game_prize_land', 'game: Prize Land is a mini-game and is never played'),
    ('Shelf Shuffle', 'game_shelf_shuffle', 'game: Shelf Shuffle is a mini-game and is never played'),
    (
        'Water to win a US $0.10 prize',
        'currency_water',
        'currency: converting water or playing the water-to-win promo is refused',
    ),
    ('Convert diamonds', 'currency_diamonds', 'currency: converting diamonds is refused'),
    ('Place an order on this page', 'spending_order', 'spending: placing an order or buying anything is refused'),
    ('Add 5 items to your cart', 'spending_cart', 'spending: adding items to the cart is refused'),
    ('Leave a review', 'review', 'review: leaving a review is refused'),
    ('Share coins with friends', 'social', 'social: sharing coins or inviting friends is refused'),
)
"""The brief's eleven refused task types, with the rule and reason each one gets.

Energy conversion is the twelfth deny rule: FR-9 names it, but the eleven above
are the task types the operator's brief enumerated. It is covered by
``test_every_deny_rule_can_refuse_its_first_pattern`` below.
"""

ALLOWED_FAMILIES: tuple[tuple[str, TaskCategory, float], ...] = (
    ('Collect', TaskCategory.SIGN_IN, 0.0),
    ('Daily check-in', TaskCategory.SIGN_IN, 0.0),
    ('Take the coins quiz', TaskCategory.QUIZ, 0.0),
    ('Explore sponsored items', TaskCategory.BROWSE_WAIT, 15.0),
    ('Browse surprise items', TaskCategory.ITEM_TAP, 0.0),
    ('Browse recently viewed items', TaskCategory.BROWSE_WAIT, 15.0),
    ('Check in to win Shopping credits', TaskCategory.BROWSE_WAIT, 15.0),
    ('Add a coins shortcut', TaskCategory.SHORTCUT, 0.0),
    ('Add & start using our widget', TaskCategory.WIDGET, 0.0),
    ('Turn on notifications', TaskCategory.NOTIFICATION, 0.0),
)


def _allowed(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        'key': 'demo',
        'label_patterns': ['Demo task'],
        'category': 'browse_wait',
        'wait_seconds': 15,
        'repeatable': False,
        'max_per_run': 1,
    }
    row.update(overrides)
    return row


def _denied(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        'key': 'blocked',
        'label_patterns': ['Blocked task'],
        'deny_reason': 'blocked: this task is never touched',
    }
    row.update(overrides)
    return row


def _document(
    *,
    allowed_rows: object | None = None,
    denied_rows: object | None = None,
    **overrides: object,
) -> dict[str, object]:
    document: dict[str, object] = {
        'version': 1,
        'default_deny_reason': 'unrecognised',
        'allowed': [_allowed()] if allowed_rows is None else allowed_rows,
        'denied': [_denied()] if denied_rows is None else denied_rows,
    }
    document.update(overrides)
    return document


def _write(tmp_path: Path, document: object) -> Path:
    specs = tmp_path / 'specs'
    specs.mkdir(parents=True, exist_ok=True)
    path = specs / 'tasks.yaml'
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding='utf-8')
    return path


def test_the_committed_catalogue_loads_and_covers_both_policies() -> None:
    catalogue = load_task_catalogue(CATALOGUE_PATH)

    assert catalogue.version == 1
    assert catalogue.default_deny_reason == 'unrecognised'
    assert catalogue.allowed_keys == (
        'daily_checkin',
        'boost_checkin',
        'coins_quiz',
        'sponsored_items',
        'surprise_items',
        'recently_viewed',
        'shopping_credits_checkin',
        'learn_to_save',
        'coins_shortcut',
        'coins_widget',
        'notifications',
    )
    assert catalogue.denied_keys == (
        'game_play_and_earn',
        'game_merge_boss',
        'game_gogo_match',
        'game_shelf_shuffle',
        'currency_water',
        'currency_diamonds',
        'currency_energy',
        'game_prize_land',
        'spending_order',
        'spending_cart',
        'review',
        'social',
    )


def test_every_allowed_row_has_a_category_wait_and_run_limit() -> None:
    catalogue = load_task_catalogue(CATALOGUE_PATH)

    for spec in catalogue.allowed:
        assert spec.label_patterns
        assert isinstance(spec.category, TaskCategory)
        assert spec.wait_seconds >= 0
        assert spec.max_per_run >= 1
        if not spec.repeatable:
            assert spec.max_per_run == 1


@pytest.mark.parametrize(('label', 'rule', 'reason'), BRIEF_REFUSALS)
def test_every_refused_task_type_from_the_brief_is_refused_with_a_reason(
    label: str,
    rule: str,
    reason: str,
) -> None:
    catalogue = load_task_catalogue(CATALOGUE_PATH)

    decision = catalogue.decide(label)

    assert decision.denied
    assert not decision.allowed
    assert decision.rule == rule
    assert decision.reason == reason
    assert decision.deny_reason == reason
    assert decision.spec is None
    assert decision.title == label


def test_every_deny_rule_can_refuse_its_first_pattern() -> None:
    catalogue = load_task_catalogue(CATALOGUE_PATH)

    for rule in catalogue.denied:
        decision = catalogue.decide(rule.label_patterns[0])
        assert decision.denied
        assert decision.rule == rule.key
        assert decision.reason == rule.deny_reason
        assert decision.matched_pattern == rule.label_patterns[0]


@pytest.mark.parametrize(('label', 'category', 'wait'), ALLOWED_FAMILIES)
def test_every_allowed_family_resolves_to_a_row_with_a_category_and_wait(
    label: str,
    category: TaskCategory,
    wait: float,
) -> None:
    catalogue = load_task_catalogue(CATALOGUE_PATH)

    decision = catalogue.decide(label)

    assert decision.allowed
    assert not decision.denied
    assert decision.deny_reason is None
    assert decision.spec is not None
    assert decision.spec.category is category
    assert decision.spec.wait_seconds == wait
    assert decision.rule == decision.spec.key


def test_an_unknown_label_is_refused_as_unrecognised() -> None:
    catalogue = load_task_catalogue(CATALOGUE_PATH)

    decision = catalogue.decide('Free trial for new members')

    assert decision.denied
    assert decision.spec is None
    assert decision.rule is None
    assert decision.reason == 'unrecognised'
    assert decision.deny_reason == 'unrecognised'
    assert decision.matched_pattern is None
    assert decision.matched_field is None


def test_deny_beats_allow_and_records_the_deny_reason() -> None:
    catalogue = load_task_catalogue(CATALOGUE_PATH)

    decision = catalogue.decide('Explore sponsored items', subtitle='Water your tree in Prize Land')

    assert decision.denied
    assert decision.rule == 'currency_water'
    assert decision.reason == 'currency: converting water or playing the water-to-win promo is refused'
    assert decision.matched_field == 'subtitle'


def test_a_deny_rule_can_match_the_subtitle_the_title_does_not_name() -> None:
    catalogue = load_task_catalogue(CATALOGUE_PATH)

    decision = catalogue.decide('A card the OCR read badly', subtitle='Each order grants you rewards')

    assert decision.denied
    assert decision.rule == 'spending_order'
    assert decision.matched_pattern == 'Each order grants you rewards'
    assert decision.matched_field == 'subtitle'


def test_an_allowed_row_can_match_the_subtitle_when_the_title_is_unreadable() -> None:
    catalogue = load_task_catalogue(CATALOGUE_PATH)

    decision = catalogue.decide('unreadable title', subtitle="Don't forget to check in today")

    assert decision.allowed
    assert decision.spec is not None
    assert decision.spec.key == 'shopping_credits_checkin'
    assert decision.matched_field == 'subtitle'


def test_matching_ignores_case_and_punctuation() -> None:
    catalogue = load_task_catalogue(CATALOGUE_PATH)

    assert catalogue.decide('PLAY & EARN').denied
    assert catalogue.decide('daily check in').allowed
    assert catalogue.decide('ADD 5 ITEMS TO YOUR CART').denied


def test_matching_is_word_aware_so_a_short_rule_does_not_refuse_an_unrelated_word() -> None:
    catalogue = load_task_catalogue(CATALOGUE_PATH)

    assert catalogue.decide('Cartoon giveaway').reason == 'unrecognised'
    assert catalogue.decide('Preview request').reason == 'unrecognised'


def test_an_empty_pattern_never_matches_even_when_called_directly() -> None:
    assert not _contains('Demo task', '   ')


def test_a_decision_renders_itself_for_the_run_report() -> None:
    catalogue = load_task_catalogue(CATALOGUE_PATH)

    allowed = catalogue.decide('Take the coins quiz').as_dict()
    denied = catalogue.decide('Free trial').as_dict()

    assert allowed == {
        'title': 'Take the coins quiz',
        'subtitle': None,
        'decision': 'allow',
        'allowed': True,
        'key': 'coins_quiz',
        'category': 'quiz',
        'rule': 'coins_quiz',
        'reason': None,
        'matched_pattern': 'Take the coins quiz',
        'matched_field': 'title',
    }
    assert denied['decision'] == 'deny'
    assert denied['allowed'] is False
    assert denied['key'] is None
    assert denied['category'] is None
    assert denied['rule'] is None
    assert denied['reason'] == 'unrecognised'


def test_rows_can_be_looked_up_by_key() -> None:
    catalogue = load_task_catalogue(CATALOGUE_PATH)

    assert catalogue.allowed_by_key('coins_quiz').category is TaskCategory.QUIZ
    assert catalogue.denied_by_key('social').deny_reason.startswith('social:')
    with pytest.raises(KeyError, match='unknown allowed task'):
        catalogue.allowed_by_key('missing')
    with pytest.raises(KeyError, match='unknown denied task'):
        catalogue.denied_by_key('missing')


def test_a_catalogue_that_its_own_deny_list_refuses_fails_at_load(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        _document(
            allowed_rows=[_allowed(label_patterns=['Play & Earn'])],
            denied_rows=[_denied(label_patterns=['Play & Earn'])],
        ),
    )

    with pytest.raises(TaskCatalogueError, match='refused by its own policy'):
        load_task_catalogue(path)


def test_two_allowed_rows_that_compete_for_one_pattern_fail_at_load(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        _document(
            allowed_rows=[
                _allowed(key='first', label_patterns=['Same task']),
                _allowed(key='second', label_patterns=['Same task']),
            ],
        ),
    )

    with pytest.raises(TaskCatalogueError, match='must be unambiguous'):
        load_task_catalogue(path)


def test_a_deny_pattern_claimed_by_two_rules_fails_at_load(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        _document(
            denied_rows=[
                _denied(key='first', label_patterns=['Same denial']),
                _denied(key='second', label_patterns=['Same denial']),
            ],
        ),
    )

    with pytest.raises(TaskCatalogueError, match='claimed by both'):
        load_task_catalogue(path)


def test_duplicate_keys_fail_at_load(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        _document(
            allowed_rows=[_allowed(key='same')],
            denied_rows=[_denied(key='same')],
        ),
    )

    with pytest.raises(TaskCatalogueError, match='duplicate task keys'):
        load_task_catalogue(path)


@pytest.mark.parametrize(
    ('document', 'message'),
    [
        (_document(allowed_rows=[_allowed(category='minigame')]), 'unknown task category'),
        (_document(allowed_rows=[_allowed(category=1)]), 'Input should be an instance of TaskCategory'),
        (_document(allowed_rows=[_allowed(label_patterns=[])]), 'at least 1 item'),
        (_document(allowed_rows=[_allowed(wait_seconds=-1)]), 'greater than or equal to 0'),
        (_document(allowed_rows=[_allowed(label_patterns=['   '])]), 'must not contain an empty pattern'),
        (_document(allowed_rows=[_allowed(label_patterns=['Demo task', 'demo task!'])]), 'must not repeat'),
        (_document(allowed_rows=[_allowed(key='DemoTask')]), 'key must be snake_case'),
        (
            _document(allowed_rows=[_allowed(repeatable=False, max_per_run=2)]),
            'max_per_run must be 1 when repeatable is false',
        ),
        (_document(denied_rows=[_denied(deny_reason='   ')]), 'must not be empty'),
        (_document(allowed_rows=[_allowed(unknown_field=True)]), 'Extra inputs are not permitted'),
        (_document(denied_rows=[_denied(category='quiz')]), 'Extra inputs are not permitted'),
        (_document(version=0), 'greater than or equal to 1'),
    ],
)
def test_an_invalid_committed_spec_fails_at_load(tmp_path: Path, document: dict[str, object], message: str) -> None:
    path = _write(tmp_path, document)

    with pytest.raises(TaskCatalogueError, match=message):
        load_task_catalogue(path)


def test_a_missing_catalogue_fails_at_load(tmp_path: Path) -> None:
    with pytest.raises(TaskCatalogueError, match='task catalogue not found'):
        load_task_catalogue(tmp_path / 'missing.yaml')


def test_a_directory_instead_of_a_catalogue_fails_at_load(tmp_path: Path) -> None:
    with pytest.raises(TaskCatalogueError, match='could not read task catalogue'):
        load_task_catalogue(tmp_path)


def test_invalid_yaml_fails_at_load(tmp_path: Path) -> None:
    path = tmp_path / 'tasks.yaml'
    path.write_text('allowed: [\n', encoding='utf-8')

    with pytest.raises(TaskCatalogueError, match='invalid YAML'):
        load_task_catalogue(path)


def test_non_utf8_catalogue_fails_at_load(tmp_path: Path) -> None:
    path = tmp_path / 'tasks.yaml'
    path.write_bytes(b'\xff\xfe')

    with pytest.raises(TaskCatalogueError, match='not valid UTF-8'):
        load_task_catalogue(path)


def test_an_empty_document_fails_at_load(tmp_path: Path) -> None:
    path = tmp_path / 'tasks.yaml'
    path.write_text('', encoding='utf-8')

    with pytest.raises(TaskCatalogueError, match='<document>'):
        load_task_catalogue(path)


def test_notes_are_optional_and_trimmed(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        _document(
            allowed_rows=[_allowed(notes='  measured  ')],
            denied_rows=[_denied(notes='  why  ')],
            notes='  catalogue note  ',
        ),
    )

    catalogue = load_task_catalogue(path)

    assert catalogue.allowed_by_key('demo').notes == 'measured'
    assert catalogue.denied_by_key('blocked').notes == 'why'
    assert catalogue.notes == 'catalogue note'


def test_an_explicit_null_note_stays_none(tmp_path: Path) -> None:
    path = _write(tmp_path, _document(allowed_rows=[_allowed(notes=None)], denied_rows=[_denied(notes=None)]))

    catalogue = load_task_catalogue(path)

    assert catalogue.allowed_by_key('demo').notes is None
    assert catalogue.denied_by_key('blocked').notes is None
