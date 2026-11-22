"""Row, action and section inference over OCR boxes (issue #14)."""

from __future__ import annotations

import statistics
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import pytest
from PIL import Image

from aliexpress_coins.perception import layout as layout_module
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.layout import (
    DEFAULT_ACTION_LABELS,
    MAX_ROW_TOLERANCE_FRAME_FRACTION,
    ROW_CENTER_TOLERANCE_HEIGHT_RATIO,
    ROW_MAX_TEXT_SPAN_HEIGHT_RATIO,
    ProgressBadge,
    Row,
    find_action_in_row,
    find_progress_badge,
    group_rows,
    group_sections,
    is_section_header,
    parse_progress_badge,
    row_of,
    row_tolerance,
)
from aliexpress_coins.perception.matching import TextIndex, find_text
from aliexpress_coins.perception.ocr import RapidOcrEngine
from aliexpress_coins.perception.snapshot import Snapshot, TextBox

CAPTURED_AT = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parent / 'data' / 'boost.png'

# Measured from RapidOCR on tests/data/boost.png (1200x1520, cropped from the
# 1200x2000 device frame; no balance, nickname, order or cart data).
_EXPECTED_BOOST_ACTIONS = {
    'Daily check-in': 357.0,
    'Explore sponsored items': 688.5,
    'Browse surprise items': 1046.0,
    'Place an order on this page': 1404.0,
}
_EXPECTED_BOOST_PROGRESS = {
    'Daily check-in': None,
    'Explore sponsored items': (0, 6),
    'Browse surprise items': (0, 2),
    'Place an order on this page': (0, 3),
}


def _box(
    text: str,
    left: float,
    top: float,
    right: float,
    bottom: float,
    score: float = 0.95,
) -> TextBox:
    return TextBox(
        text=text,
        box=((left, top), (right, top), (right, bottom), (left, bottom)),
        score=score,
    )


def _centered_box(
    text: str,
    center_x: float,
    center_y: float,
    *,
    width: float = 80.0,
    height: float = 20.0,
    score: float = 0.95,
) -> TextBox:
    return _box(
        text,
        center_x - width / 2,
        center_y - height / 2,
        center_x + width / 2,
        center_y + height / 2,
        score=score,
    )


def _geometry(width: int = 400, height: int = 800) -> DeviceGeometry:
    return DeviceGeometry.from_frame(width, height, density=240, sdk=35)


def _snapshot(*boxes: TextBox, size: tuple[int, int] = (400, 800)) -> Snapshot:
    return Snapshot(
        image=Image.new('RGB', size, 'white'),
        boxes=boxes,
        captured_at=CAPTURED_AT,
        geometry=_geometry(*size),
    )


@pytest.fixture(scope='module')
def boost_snapshot() -> Snapshot:
    with Image.open(FIXTURE) as opened:
        image = opened.convert('RGB')
    return Snapshot(
        image=image,
        boxes=RapidOcrEngine().read(image),
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(image.width, image.height, density=240, sdk=35),
    )


def test_row_tolerance_is_derived_from_the_median_box_height() -> None:
    boxes = (
        _centered_box('one', 10, 10, height=20),
        _centered_box('two', 10, 40, height=30),
        _centered_box('three', 10, 80, height=40),
    )

    assert row_tolerance(boxes, _geometry(400, 2000)) == pytest.approx(67.5)
    assert ROW_CENTER_TOLERANCE_HEIGHT_RATIO == 2.25


def test_the_derived_tolerance_covers_both_measured_go_gaps_without_merging_neighbours() -> None:
    tablet_boxes = (_centered_box('title', 100, 100, height=48), _centered_box('GO', 300, 200, height=48))
    phone_boxes = (_centered_box('title', 100, 100, height=43), _centered_box('GO', 300, 169, height=43))
    coin_boxes = (_centered_box('title', 100, 100, height=45), _centered_box('GO', 300, 133, height=45))

    tablet = row_tolerance(tablet_boxes, _geometry(1200, 2000))
    phone = row_tolerance(phone_boxes, _geometry(1080, 1920))
    coin = row_tolerance(coin_boxes, _geometry(1200, 2000))

    assert tablet == pytest.approx(108.0)
    assert phone == pytest.approx(96.75)
    assert coin == pytest.approx(101.25)
    assert tablet > 100.0  # the measured tablet title -> GO gap
    assert phone > 70.0  # the measured phone title -> GO gap
    assert coin < 106.0  # the coin-page card -> first task row gap
    assert tablet < 190.0  # the smallest measured gap between adjacent task rows


def test_row_tolerance_is_capped_by_a_fraction_of_the_frame_height() -> None:
    boxes = (_centered_box('one', 10, 10, height=40),)
    geometry = _geometry(100, 100)

    assert row_tolerance(boxes, geometry) == pytest.approx(geometry.height * MAX_ROW_TOLERANCE_FRAME_FRACTION)
    assert row_tolerance((), geometry) == 0.0


def test_group_rows_returns_reading_order_with_boxes_inside_each_row() -> None:
    boxes = (
        _centered_box('second row', 100, 200),
        _centered_box('first row title', 100, 100),
        _centered_box('first row action', 300, 100),
    )

    rows = group_rows(boxes, _geometry())

    assert [row.text for row in rows] == ['first row title first row action', 'second row']
    assert [box.text for box in rows[0].boxes] == ['first row title', 'first row action']
    assert rows[0].center_y == pytest.approx(100.0)
    assert rows[0].height == pytest.approx(20.0)
    assert rows[0].left == pytest.approx(60.0)
    assert rows[0].right == pytest.approx(340.0)


def test_group_rows_of_no_boxes_is_no_rows() -> None:
    assert group_rows((), _geometry()) == ()


def test_group_rows_keeps_the_measured_title_and_action_together() -> None:
    boxes = (
        _centered_box('Take the coins quiz', 180, 1269),
        _centered_box('GO', 1019, 1301.5, height=20),
    )

    rows = group_rows(boxes, _geometry(1200, 2000))

    assert len(rows) == 1
    assert row_of(rows, boxes[0]) is rows[0]
    assert find_action_in_row(rows[0]) is boxes[1]


def test_group_rows_never_merges_a_tight_pitch_pair() -> None:
    boxes: list[TextBox] = []
    for index in range(4):
        title_y = 100.0 + index * 75.0
        boxes.append(_centered_box(f'Task {index}', 100, title_y))
        boxes.append(_centered_box('GO', 300, title_y + 31.0))

    rows = group_rows(boxes, _geometry())

    assert len(rows) == 4
    assert all(len(row.boxes) == 2 for row in rows)
    assert all(find_action_in_row(row) is not None for row in rows)


def test_group_rows_splits_a_chained_cluster_at_each_action_boundary() -> None:
    boxes: list[TextBox] = []
    for index, base in enumerate((100.0, 239.0, 378.0)):
        boxes.extend(
            (
                _centered_box(f'Task {index}', 100, base, width=150, height=48),
                _centered_box('GO', 300, base + 31, height=48),
                _centered_box('+1', 150, base + 75, height=48),
            ),
        )

    rows = group_rows(boxes, _geometry())

    assert len(rows) == 3
    for index, row in enumerate(rows):
        action = find_action_in_row(row)
        assert action is not None
        assert action.center[1] == pytest.approx(131.0 + index * 139.0)
        assert any(box.text == f'Task {index}' for box in row.boxes)


def test_group_rows_splits_stacked_text_lines_without_an_action() -> None:
    boxes = (
        _centered_box('Coins', 100, 100, width=80, height=50),
        _centered_box('Daily coins', 100, 181.5, width=120, height=50),
    )

    rows = group_rows(boxes, _geometry(400, 2000))

    assert [row.text for row in rows] == ['Coins', 'Daily coins']
    assert ROW_MAX_TEXT_SPAN_HEIGHT_RATIO == 1.6


def test_group_rows_does_not_use_an_unsure_action_as_a_split_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[TextBox, ...]] = []
    real_split = layout_module._split_at_actions

    def spy(cluster: tuple[TextBox, ...], actions: tuple[TextBox, ...]) -> tuple[tuple[TextBox, ...], ...]:
        calls.append(actions)
        return real_split(cluster, actions)

    monkeypatch.setattr(layout_module, '_split_at_actions', spy)
    boxes = (
        _centered_box('Task one', 100, 100, height=48),
        _centered_box('GO', 300, 131, height=48, score=0.1),
        _centered_box('Task two', 100, 150, height=48),
        _centered_box('GO', 300, 181, height=48, score=0.1),
    )

    group_rows(boxes, _geometry(400, 2000))

    assert calls == []


def test_group_rows_deduplicates_duplicate_action_boxes_before_splitting() -> None:
    boxes = (
        _centered_box('Task one', 100, 1000, height=48),
        _centered_box('GO', 300, 1075, height=48),
        _centered_box('GO', 305, 1075, height=48),
        _centered_box('subtitle', 100, 1100, height=48),
        _centered_box('Task two', 100, 1180, height=48),
        _centered_box('GO', 300, 1235, height=48),
    )

    rows = group_rows(boxes, _geometry(400, 2000))

    assert len(rows) == 2
    assert [box.text for box in rows[0].boxes] == ['Task one', 'GO', 'GO', 'subtitle']
    assert [box.text for box in rows[1].boxes] == ['Task two', 'GO']


def test_row_rejects_an_empty_box_list_and_sorts_in_reading_order() -> None:
    with pytest.raises(ValueError, match='at least one box'):
        Row(())

    lower = _centered_box('lower', 300, 30)
    upper = _centered_box('upper', 100, 10)
    row = Row((lower, upper))

    assert [box.text for box in row.boxes] == ['upper', 'lower']
    assert row.text == 'upper lower'


def test_row_of_accepts_a_text_match_and_reports_a_miss() -> None:
    title = _centered_box('Explore sponsored items', 100, 100)
    row = Row((title, _centered_box('GO', 300, 100)))
    match = TextIndex((title,)).find('Explore sponsored items')

    assert match is not None
    assert row_of((row,), match) is row
    assert row_of((row,), _centered_box('not here', 10, 500)) is None


def test_find_action_in_row_picks_the_rightmost_action_label() -> None:
    left = _centered_box('GO', 10, 100)
    title = _centered_box('Task', 100, 100)
    right = _centered_box('GO', 300, 100)
    row = Row((right, title, left))

    assert find_action_in_row(row) is right
    assert row.action() is right
    assert find_action_in_row(row, ()) is None
    assert find_action_in_row(Row((_centered_box('Task', 100, 100),))) is None


def test_find_action_in_row_considers_every_action_label_then_position() -> None:
    left = _centered_box('GO', 100, 100)
    right = _centered_box('Start', 300, 100)
    row = Row((left, right))

    assert find_action_in_row(row, ('GO', 'Start')) is right
    assert find_action_in_row(row, ('Start',)) is right
    assert DEFAULT_ACTION_LABELS == ('GO',)


def test_find_action_in_row_keeps_an_earlier_rightmost_candidate() -> None:
    right = _centered_box('GO', 300, 90)
    left = _centered_box('Start', 100, 100)
    row = Row((left, right))

    assert find_action_in_row(row, ('GO', 'Start')) is right


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('0/6', (0, 6)),
        ('1/2', (1, 2)),
        ('3/3', (3, 3)),
        (' 1 / 6 ', (1, 6)),
        ('\uff11/\uff12', (1, 2)),
    ],
)
def test_parse_progress_badge_accepts_spacing_and_width(text: str, expected: tuple[int, int]) -> None:
    assert parse_progress_badge(text) == expected


@pytest.mark.parametrize(
    'text',
    ['', 'C$0.03', '+1', '0/0', '5/2', '1/6 coins', '1/2/3', 'GO'],
)
def test_parse_progress_badge_rejects_non_badges(text: str) -> None:
    assert parse_progress_badge(text) is None


def test_progress_badge_reports_its_fraction_and_flags_invalid_totals() -> None:
    box = _centered_box('1/2', 100, 100)
    badge = ProgressBadge(completed=1, total=2, box=box)

    assert badge.text == '1/2'
    assert badge.fraction == pytest.approx(0.5)
    assert not badge.complete
    assert ProgressBadge(completed=2, total=2, box=box).complete

    with pytest.raises(ValueError, match='negative'):
        ProgressBadge(completed=-1, total=2, box=box)
    with pytest.raises(ValueError, match='positive'):
        ProgressBadge(completed=0, total=0, box=box)
    with pytest.raises(ValueError, match='exceed'):
        ProgressBadge(completed=3, total=2, box=box)


def test_find_progress_badge_attaches_the_rightmost_badge_to_its_row() -> None:
    first = _centered_box('0/6', 250, 100)
    second = _centered_box('1/2', 300, 100)
    row = Row((_centered_box('Task', 100, 100), first, second))

    badge = find_progress_badge(row)

    assert badge is not None
    assert badge.box is second
    assert row.progress == badge
    assert find_progress_badge(Row((_centered_box('Task', 100, 100),))) is None


def test_find_progress_badge_keeps_an_earlier_rightmost_candidate() -> None:
    right = _centered_box('0/6', 300, 90)
    left = _centered_box('1/2', 250, 100)
    row = Row((left, right))

    badge = find_progress_badge(row)

    assert badge is not None
    assert badge.box is right


def test_is_section_header_uses_labels_or_visual_prominence() -> None:
    header = Row((_centered_box('Coins boost', 100, 50, height=30),))
    short = Row((_centered_box('Ordinary row', 100, 50, height=20),))
    numeric = Row((_centered_box('30', 100, 50, height=30),))
    multiple = Row((_centered_box('Task', 100, 50, height=30), _centered_box('GO', 300, 50, height=30)))

    assert is_section_header(header, median_box_height=20)
    assert is_section_header(short, median_box_height=20) is False
    assert is_section_header(numeric, median_box_height=20) is False
    assert is_section_header(multiple, median_box_height=20) is False
    assert is_section_header(multiple, median_box_height=20, header_labels=('Task',))
    assert is_section_header(header, median_box_height=0) is False


def test_group_sections_keeps_leading_rows_and_known_headers_in_order() -> None:
    leading = Row((_centered_box('Intro', 100, 10),))
    header = Row((_centered_box('Coins boost', 100, 50, height=30),))
    first = Row((_centered_box('Task one', 100, 100), _centered_box('GO', 300, 100)))
    second_header = Row((_centered_box('Other tasks', 100, 200, height=30),))
    second = Row((_centered_box('Task two', 100, 250), _centered_box('GO', 300, 250)))

    sections = group_sections(
        (leading, header, first, second_header, second),
        header_labels=('Coins boost', 'Other tasks'),
    )

    assert [(section.title, len(section.rows)) for section in sections] == [
        (None, 1),
        ('Coins boost', 1),
        ('Other tasks', 1),
    ]
    assert sections[0].rows == (leading,)
    assert group_sections(()) == ()


def test_group_sections_keeps_a_header_with_no_rows() -> None:
    first = Row((_centered_box('First heading', 100, 10, height=30),))
    second = Row((_centered_box('Second heading', 100, 60, height=30),))

    sections = group_sections((first, second), header_labels=('First heading', 'Second heading'))

    assert [(section.title, section.rows) for section in sections] == [
        ('First heading', ()),
        ('Second heading', ()),
    ]


def test_the_measured_boost_fixture_pairs_every_title_with_its_own_action(
    boost_snapshot: Snapshot,
) -> None:
    rows = group_rows(boost_snapshot.boxes, boost_snapshot.geometry)

    actions: list[TextBox] = []
    for title, expected_y in _EXPECTED_BOOST_ACTIONS.items():
        match = find_text(boost_snapshot, title)
        assert match is not None
        row = row_of(rows, match)
        assert row is not None
        action = find_action_in_row(row)
        assert action is not None
        assert action.text == 'GO'
        assert action.center[1] == pytest.approx(expected_y, abs=1.5)
        assert action.center[0] > match.box.center[0]
        actions.append(action)

    assert len({id(action) for action in actions}) == len(_EXPECTED_BOOST_ACTIONS)
    assert len(rows) == 5


def test_the_measured_boost_fixture_attaches_each_progress_badge_to_its_row(
    boost_snapshot: Snapshot,
) -> None:
    rows = group_rows(boost_snapshot.boxes, boost_snapshot.geometry)

    for title, expected in _EXPECTED_BOOST_PROGRESS.items():
        match = find_text(boost_snapshot, title)
        assert match is not None
        row = row_of(rows, match)
        assert row is not None
        badge = find_progress_badge(row)
        if expected is None:
            assert badge is None
        else:
            assert badge is not None
            assert (badge.completed, badge.total) == expected
            assert row_of(rows, badge.box) is row


def test_the_measured_boost_fixture_has_one_section_header_and_four_rows(
    boost_snapshot: Snapshot,
) -> None:
    rows = group_rows(boost_snapshot.boxes, boost_snapshot.geometry)

    explicit = group_sections(rows, header_labels=('Coins boost',))
    visual = group_sections(rows)

    for sections in (explicit, visual):
        assert len(sections) == 1
        assert sections[0].title == 'Coins boost'
        assert len(sections[0].rows) == 4


def test_inserting_a_row_above_the_boost_list_changes_no_target_identity(
    boost_snapshot: Snapshot,
) -> None:
    boxes = boost_snapshot.boxes
    rows = group_rows(boxes, boost_snapshot.geometry)
    task_rows = tuple(row for row in rows if find_action_in_row(row) is not None)
    action_centers: list[float] = []
    for row in task_rows:
        action = find_action_in_row(row)
        assert action is not None
        action_centers.append(action.center[1])
    pitch = statistics.median(later - earlier for earlier, later in pairwise(action_centers))

    first_row = task_rows[0]
    first_action = first_row.action()
    assert first_action is not None
    synthetic = tuple(box.shifted(0, -pitch) for box in first_row.boxes)
    synthetic_title = TextBox(text='Synthetic inserted task', box=synthetic[0].box, score=synthetic[0].score)
    synthetic = (synthetic_title, *synthetic[1:])

    shifted = tuple(box.shifted(0, pitch) for box in boxes)
    shifted_by_original = {id(box): moved for box, moved in zip(boxes, shifted, strict=True)}
    new_rows = group_rows((*synthetic, *shifted), boost_snapshot.geometry)

    assert len(new_rows) == len(rows) + 1
    assert len([row for row in new_rows if find_action_in_row(row) is not None]) == len(task_rows) + 1

    for title, _ in _EXPECTED_BOOST_ACTIONS.items():
        match = find_text(boost_snapshot, title)
        assert match is not None
        original_row = row_of(rows, match)
        assert original_row is not None
        original_action = find_action_in_row(original_row)
        assert original_action is not None

        shifted_title = shifted_by_original[id(match.box)]
        new_row = row_of(new_rows, shifted_title)
        assert new_row is not None
        new_action = find_action_in_row(new_row)
        assert new_action is not None
        assert new_action.text == 'GO'
        assert new_action.center[1] == pytest.approx(original_action.center[1] + pitch)

    synthetic_row = row_of(new_rows, synthetic_title)
    assert synthetic_row is not None
    synthetic_action = find_action_in_row(synthetic_row)
    assert synthetic_action is not None
    assert synthetic_action.center[1] == pytest.approx(first_action.center[1] - pitch)
