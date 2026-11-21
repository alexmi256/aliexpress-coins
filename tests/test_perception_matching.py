"""Normalisation, similarity and the per-frame text index (issue #13)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from PIL import Image

from aliexpress_coins.perception import matching
from aliexpress_coins.perception import snapshot as snapshot_module
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.matching import (
    DEFAULT_MIN_OCR_SCORE,
    DEFAULT_MIN_SCORE,
    find_all,
    find_text,
    normalize,
    similarity,
)
from aliexpress_coins.perception.snapshot import Snapshot, TextBox

CAPTURED_AT = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def _box(text: str, left: float, top: float, right: float, bottom: float, score: float = 0.95) -> TextBox:
    return TextBox(
        text=text,
        box=((left, top), (right, top), (right, bottom), (left, bottom)),
        score=score,
    )


def _snapshot(*boxes: TextBox, size: tuple[int, int] = (400, 800)) -> Snapshot:
    return Snapshot(
        image=Image.new('RGB', size, 'white'),
        boxes=boxes,
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(size[0], size[1], density=240, sdk=35),
    )


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('  Earn   more >  ', 'earn more'),
        ('EARN MORE', 'earn more'),
        ('Earn more >', 'Earn more'),
        ('Coins credits to redeem >', 'coins credits to redeem'),
        (' "Take the coins quiz!" ', 'take the coins quiz'),
        ('Daily check-in', 'daily check-in'),
    ],
)
def test_normalize_ignores_case_whitespace_and_edge_punctuation(raw: str, expected: str) -> None:
    normalised = normalize(raw)
    assert normalised == expected.casefold()
    assert normalize(raw) == normalize(expected)


def test_normalize_keeps_internal_punctuation_and_currency() -> None:
    assert normalize('C$0.04') == 'c$0.04'
    assert normalize('worth C$0.44') == 'worth c$0.44'
    assert normalize('0/6') == '0/6'
    assert normalize('+1') == '+1'


@pytest.mark.parametrize('glyph', ['\u00d7', '\u2715', '\u2716'])
def test_normalize_maps_the_close_glyph_to_a_token(glyph: str) -> None:
    assert normalize(glyph) == 'close'
    assert normalize(glyph) != ''


def test_normalize_does_not_empty_a_symbol_only_label() -> None:
    assert normalize('S') == 'check'
    assert normalize('s') != ''
    assert normalize('X') == 'close'
    assert normalize('...') == 'u002e u002e u002e'
    assert normalize('>') == 'u003e'


def test_only_empty_or_whitespace_input_normalises_to_empty() -> None:
    assert normalize('') == ''
    assert normalize('   \t\n') == ''
    assert normalize('\u00d7') != ''


def test_similarity_accepts_the_currency_symbol_read_as_an_s() -> None:
    score = similarity('C$0.04', 'Cs0.04')
    assert score >= 0.8
    assert score == pytest.approx(5 / 6)


def test_similarity_keeps_unrelated_labels_below_the_default_threshold() -> None:
    assert similarity('Coins', 'Vouchers') < DEFAULT_MIN_SCORE
    assert similarity('Coins', 'Coins credits to use') < DEFAULT_MIN_SCORE


def test_adjacent_roster_rows_do_not_cross_match() -> None:
    adjacent = similarity('Browse surprise items', 'Browse recently viewed items')
    assert adjacent < DEFAULT_MIN_SCORE
    assert similarity('Explore sponsored items', 'Browse surprise items') < DEFAULT_MIN_SCORE


def test_digits_must_agree_because_a_different_amount_is_a_different_label() -> None:
    assert similarity('C$0.04', 'C$0.44') < DEFAULT_MIN_SCORE
    assert similarity('worth C$0.04', 'worth C$0.44') < DEFAULT_MIN_SCORE
    assert similarity('0/6', '0/2') < DEFAULT_MIN_SCORE
    assert similarity('C$0.04', 'C$0.04') == 1.0


def test_similarity_of_nothing_is_zero() -> None:
    assert similarity('', '') == 0.0
    assert similarity('', 'Coins') == 0.0
    assert similarity('Coins', ' \t ') == 0.0


def test_find_text_returns_the_best_box_in_frame_coordinates() -> None:
    earned_more = _box('Earn more >', 900, 940, 1100, 960, score=0.999)
    snapshot = _snapshot(_box('Daily coins', 100, 940, 300, 960), earned_more)

    match = find_text(snapshot, 'Earn more')

    assert match is not None
    assert match.box is earned_more
    assert match.text == 'Earn more >'
    assert match.center == (1000.0, 950.0)
    assert match.score == 1.0
    assert match.ocr_score == 0.999


def test_the_ocr_floor_and_the_text_threshold_are_independent() -> None:
    unsure = _snapshot(_box('GO', 10, 10, 30, 30, score=0.2))
    assert find_text(unsure, 'GO') is None
    assert find_text(unsure, 'GO', min_ocr_score=0.0) is not None

    loose = _snapshot(_box('Earn more', 10, 10, 30, 30, score=0.99))
    assert find_text(loose, 'Earn') is None
    assert find_text(loose, 'Earn', min_score=0.4) is not None
    assert DEFAULT_MIN_OCR_SCORE == 0.5


def test_find_all_keeps_frame_order_and_filters_each_box() -> None:
    first = _box('GO', 10, 10, 30, 30, score=0.99)
    middle = _box('Browse surprise items', 10, 50, 200, 70)
    second = _box('GO', 10, 90, 30, 110, score=0.90)
    unsure = _box('GO', 10, 130, 30, 150, score=0.1)
    snapshot = _snapshot(first, middle, second, unsure)

    matches = find_all(snapshot, 'GO')

    assert [match.box for match in matches] == [first, second]
    assert [match.score for match in matches] == [1.0, 1.0]
    assert matches[0].ocr_score == 0.99


def test_find_text_breaks_a_score_tie_by_ocr_confidence() -> None:
    low = _box('Earn more', 10, 10, 100, 30, score=0.70)
    high = _box('Earn more >', 10, 50, 100, 70, score=0.90)
    snapshot = _snapshot(low, high)

    match = find_text(snapshot, 'Earn more')

    assert match is not None
    assert match.box is high


@pytest.mark.parametrize('label', ['', '   ', '\t\n'])
def test_an_empty_query_is_rejected_rather_than_matching_everything(label: str) -> None:
    snapshot = _snapshot(_box('Coins', 10, 10, 30, 30))
    with pytest.raises(ValueError, match='a label is required'):
        find_text(snapshot, label)
    with pytest.raises(ValueError, match='a label is required'):
        find_all(snapshot, label)
    with pytest.raises(ValueError, match='a label is required'):
        snapshot.find(label)


@pytest.mark.parametrize('value', [-0.1, 1.1])
def test_thresholds_must_be_fractions(value: float) -> None:
    snapshot = _snapshot(_box('Coins', 10, 10, 30, 30))
    with pytest.raises(ValueError, match='min_score'):
        find_text(snapshot, 'Coins', min_score=value)
    with pytest.raises(ValueError, match='min_ocr_score'):
        find_all(snapshot, 'Coins', min_ocr_score=value)


def test_an_empty_ocr_box_is_not_indexed() -> None:
    empty = _box('   ', 10, 10, 30, 30)
    coin = _box('Coins', 10, 50, 30, 70)
    snapshot = _snapshot(empty, coin)

    assert find_all(snapshot, 'Coins') == (matching.TextMatch(box=coin, score=1.0),)


def test_the_text_index_is_built_once_and_describe_does_not_build_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[tuple[TextBox, ...]] = []
    real_index = matching.TextIndex

    def counting_index(boxes: Any) -> matching.TextIndex:
        built.append(tuple(boxes))
        return real_index(boxes)

    monkeypatch.setattr(snapshot_module, 'TextIndex', counting_index)
    snapshot = _snapshot(_box('Coins', 10, 10, 30, 30), _box('Daily coins', 10, 50, 30, 70))

    description = snapshot.describe()
    assert 'Coins' in description
    assert built == []

    assert snapshot.find('Coins') is not None
    assert snapshot.find('Daily coins') is not None
    assert len(built) == 1
    assert snapshot.text_index is snapshot.text_index


def test_snapshot_find_keeps_a_strict_default() -> None:
    misread = _box('Cs0.04', 10, 10, 100, 30, score=0.99)
    snapshot = _snapshot(misread)

    assert snapshot.find('C$0.04') is None
    assert not snapshot.has('C$0.04')
    assert snapshot.find('C$0.04', min_score=DEFAULT_MIN_SCORE) is misread
    assert snapshot.find(' C$0.04 ') is None
    assert snapshot.find('CS0.04') is misread


def test_snapshot_find_ignores_edge_punctuation_but_not_a_near_spelling() -> None:
    snapshot = _snapshot(_box('Earn more >', 10, 10, 100, 30), _box('Earn more', 10, 50, 100, 70))

    assert snapshot.find('Earn more') is not None
    assert snapshot.find('Earn') is None
    assert snapshot.find('Earn', min_score=0.4) is not None


# Measured 2026-11-21 from a 1200x2000 `screencap` of the coin page. The OCR
# scores are RapidOCR's own; the positions are the centres OCR returned.
_MEASURED_COINS_PAGE = (
    ('Coins', 179.5, 217.5, 1.000),
    ('30', 217.5, 354.0, 1.000),
    ('worth C$0.44', 199.5, 451.0, 0.998),
    ('Coins credits to redeem >', 321.0, 593.5, 1.000),
    ('Coins credits to use', 341.0, 780.0, 0.998),
    ('Daily coins', 192.5, 948.5, 0.998),
    ('Earn more >', 1024.5, 946.5, 0.996),
    ('Daily check-in', 306.5, 1269.0, 0.979),
    ('GO', 1019.0, 1301.5, 1.000),
    ('Explore sponsored items', 398.0, 1446.0, 0.985),
    ('0/6', 1101.5, 1441.0, 0.999),
    ('GO', 1019.5, 1475.0, 1.000),
    ('Browse surprise items', 373.5, 1616.5, 0.976),
    ('0/2', 1101.5, 1610.0, 0.998),
    ('GO', 1019.5, 1644.5, 1.000),
)


def _measured_box(text: str, x: float, y: float, score: float) -> TextBox:
    half_width = max(len(text) * 5.0, 10.0)
    return _box(text, x - half_width, y - 10, x + half_width, y + 10, score=score)


def test_the_measured_coins_page_labels_resolve() -> None:
    snapshot = _snapshot(*[_measured_box(*row) for row in _MEASURED_COINS_PAGE])

    earn_more = find_text(snapshot, 'Earn more')
    assert earn_more is not None
    assert earn_more.text == 'Earn more >'
    assert earn_more.center == (1024.5, 946.5)
    assert earn_more.score == 1.0

    buttons = find_all(snapshot, 'GO')
    assert len(buttons) == 3
    assert [button.center[1] for button in buttons] == [1301.5, 1475.0, 1644.5]
    assert all(button.score == 1.0 for button in buttons)

    assert find_text(snapshot, 'Take the coins quiz') is None  # not in this roster


def test_the_measured_quiz_row_resolves_from_the_recon_frame() -> None:
    # docs/01-recon-findings.md §3 recorded this row at (180, 1269) and its GO at
    # (982, 1273). The row is server-driven and was absent from the 2026-11-21
    # roster, so the literal is checked against that measured row.
    snapshot = _snapshot(
        _measured_box('Take the coins quiz', 180.0, 1269.0, 0.99),
        _measured_box('GO', 982.0, 1273.0, 0.99),
    )

    quiz = find_text(snapshot, 'Take the coins quiz')
    assert quiz is not None
    assert quiz.score == 1.0
    assert quiz.center == (180.0, 1269.0)
    assert find_text(snapshot, 'GO') is not None
