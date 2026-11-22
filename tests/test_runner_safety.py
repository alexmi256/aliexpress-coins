"""The three structural abort conditions (issue #20).

Frames are hand-built from the measured geometry; the safety guard is the real
one and no test reaches a device. The guard is also given a fake capture seam to
prove every frame is inspected exactly once.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from PIL import Image

from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.runner.failures import FailureCode
from aliexpress_coins.runner.safety import (
    AbortCode,
    RunAborted,
    SafetyGuard,
    find_dangerous_controls,
)
from aliexpress_coins.tasks.catalogue import TaskCatalogue, load_task_catalogue

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE: TaskCatalogue = load_task_catalogue(ROOT / 'specs' / 'tasks.yaml')
CAPTURED_AT = datetime(2026, 11, 22, 3, 30, tzinfo=UTC)


def _box(text: str, left: float, top: float, right: float, bottom: float) -> TextBox:
    return TextBox(
        text=text,
        box=((left, top), (right, top), (right, bottom), (left, bottom)),
        score=0.99,
    )


def _snapshot(*boxes: TextBox, size: tuple[int, int] = (1200, 2000)) -> Snapshot:
    return Snapshot(
        image=Image.new('RGB', size, 'white'),
        boxes=boxes,
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(*size, density=240, sdk=35),
    )


def _unknown(label: str = 'Mystery panel') -> Snapshot:
    boxes = [
        _box(f'{label} {index}', 60 + (index % 3) * 380, 300 + index * 90, 400 + (index % 3) * 380, 370 + index * 90)
        for index in range(8)
    ]
    return _snapshot(*boxes)


def _coins(balance: str) -> Snapshot:
    return _snapshot(
        _box('Coins', 100, 220, 300, 280),
        _box('Coins credits to redeem', 100, 330, 700, 380),
        _box('Daily coins', 100, 560, 500, 610),
        _box(balance, 100, 400, 260, 460),
    )


def _danger(label: str = 'Buy now') -> Snapshot:
    return _snapshot(
        _box('Wireless Earbuds', 60, 300, 600, 350),
        _box(label, 400, 1800, 700, 1860),
    )


def _guard() -> SafetyGuard:
    return SafetyGuard(catalogue=CATALOGUE, logger=logging.getLogger('test.safety'))


def test_the_same_unexpected_screen_aborts_on_the_third_frame() -> None:
    guard = _guard()
    frame = _unknown()

    guard.inspect(frame)
    guard.inspect(frame)
    assert guard.streak == 2
    with pytest.raises(RunAborted) as raised:
        guard.inspect(frame)

    assert raised.value.code is AbortCode.UNEXPECTED_SCREEN
    assert raised.value.failure.code is FailureCode.UNEXPECTED_SCREEN
    assert '3 times' in raised.value.reason
    assert raised.value.screen.labels
    assert guard.last_screen is raised.value.screen


def test_a_known_screen_resets_the_unexpected_streak() -> None:
    guard = _guard()
    unknown = _unknown()

    guard.inspect(unknown)
    guard.inspect(unknown)
    guard.inspect(_coins('40'))
    guard.inspect(unknown)
    guard.inspect(unknown)

    assert guard.streak == 2


def test_three_different_unexpected_screens_do_not_abort() -> None:
    guard = _guard()

    guard.inspect(_unknown('First'))
    guard.inspect(_unknown('Second'))
    guard.inspect(_unknown('Third'))

    assert guard.streak == 1


def test_ocr_jitter_on_the_same_unexpected_screen_keeps_the_streak() -> None:
    guard = _guard()

    guard.inspect(_unknown('Mystery panel'))
    guard.inspect(_unknown('Mystery pane1'))
    with pytest.raises(RunAborted) as raised:
        guard.inspect(_unknown('Mystery panel'))

    assert raised.value.code is AbortCode.UNEXPECTED_SCREEN


def test_different_screens_with_the_same_initials_do_not_merge() -> None:
    guard = _guard()

    guard.inspect(_unknown('Alpha'))
    guard.inspect(_unknown('April'))
    guard.inspect(_unknown('Alpha'))

    assert guard.streak == 1


def test_the_same_unexpected_screen_counts_inside_a_task() -> None:
    guard = _guard()

    def inspect_three() -> None:
        with guard.task('stuck executor'):
            guard.inspect(_unknown())
            guard.inspect(_unknown())
            guard.inspect(_unknown())

    with pytest.raises(RunAborted) as raised:
        inspect_three()

    assert raised.value.code is AbortCode.UNEXPECTED_SCREEN
    assert 'stuck executor' not in raised.value.reason


def test_a_blank_loading_webview_does_not_count_as_a_stuck_task_screen() -> None:
    guard = _guard()
    blank = _snapshot()

    with guard.task('browse'):
        guard.inspect(blank)
        guard.inspect(blank)
        guard.inspect(blank)


def test_an_empty_boost_page_does_not_count_as_a_stuck_task_screen() -> None:
    guard = _guard()
    empty = _snapshot(
        _box('Coins boost', 100, 100, 500, 150),
        _box("There's nothing here yet", 100, 300, 900, 350),
    )

    with guard.task('browse'):
        guard.inspect(empty)
        guard.inspect(empty)
        guard.inspect(empty)


def test_a_running_countdown_destination_does_not_count_as_a_stuck_task_screen() -> None:
    guard = _guard()
    destination = _snapshot(
        _box('Wireless Earbuds', 60, 300, 600, 350),
        _box('12s', 1080, 900, 1160, 940),
    )

    with guard.task('browse'):
        guard.inspect(destination)
        guard.inspect(destination)
        guard.inspect(destination)


def test_disarm_stops_all_inspection_after_an_abort() -> None:
    guard = _guard()
    guard.disarm()

    guard.inspect(_unknown())
    guard.inspect(_unknown())
    guard.inspect(_unknown())

    assert guard.streak == 0


def test_a_dangerous_control_is_only_fatal_while_a_task_is_running() -> None:
    guard = _guard()
    frame = _danger()

    guard.inspect(frame)
    with pytest.raises(RunAborted) as raised, guard.task('coins quiz'):
        guard.inspect(frame)

    assert raised.value.code is AbortCode.DANGEROUS_CONTROL
    assert raised.value.failure.code is FailureCode.DANGEROUS_CONTROL
    assert 'Buy now' in raised.value.reason
    assert raised.value.screen.surface


@pytest.mark.parametrize('label', ['Add to cart', 'Place an order', 'Checkout', 'Purchase', 'Redeem'])
def test_each_spending_control_aborts_during_a_task(label: str) -> None:
    guard = _guard()

    with pytest.raises(RunAborted) as raised, guard.task('browse'):
        guard.inspect(_danger(label))

    assert raised.value.code is AbortCode.DANGEROUS_CONTROL
    assert label in raised.value.reason


def test_the_coin_pages_own_redeem_entry_is_not_a_live_control_during_a_task() -> None:
    guard = _guard()
    page = _snapshot(
        _box('Coins', 100, 220, 300, 280),
        _box('52', 100, 400, 260, 460),
        _box('Coins credits to redeem >', 100, 330, 700, 380),
        _box('Redeem', 940, 340, 1100, 380),
        _box('Daily coins', 100, 560, 500, 610),
    )

    with guard.task('Daily coins'):
        guard.inspect(page)


def test_a_refused_task_row_on_the_list_is_not_a_live_purchase_control() -> None:
    guard = _guard()
    boost_list = _snapshot(
        _box('Coins boost', 100, 100, 500, 150),
        _box('Place an order on this page', 100, 260, 900, 300),
        _box('GO', 980, 280, 1040, 320),
        _box('Add 5 items to your cart', 100, 460, 900, 500),
        _box('GO', 980, 480, 1040, 520),
    )

    with guard.task('Take the coins quiz'):
        guard.inspect(boost_list)


def test_a_row_without_a_textual_title_does_not_exempt_its_dangerous_neighbour() -> None:
    guard = _guard()
    page = _snapshot(
        _box('52', 100, 260, 200, 300),
        _box('GO', 980, 280, 1040, 320),
        _box('Buy now', 400, 1800, 700, 1860),
    )

    with pytest.raises(RunAborted) as raised, guard.task('Daily coins'):
        guard.inspect(page)

    assert raised.value.code is AbortCode.DANGEROUS_CONTROL


def test_a_dangerous_label_beside_an_allowed_rows_go_is_still_a_live_control() -> None:
    guard = _guard()
    page = _snapshot(
        _box('Coins boost', 100, 100, 500, 150),
        _box('Daily check-in', 100, 260, 700, 300),
        _box('Buy now', 720, 280, 900, 320),
        _box('GO', 980, 280, 1040, 320),
    )

    with pytest.raises(RunAborted) as raised, guard.task('Daily coins'):
        guard.inspect(page)

    assert raised.value.code is AbortCode.DANGEROUS_CONTROL
    assert 'Buy now' in raised.value.reason


def test_a_standalone_purchase_control_on_the_task_surface_still_aborts() -> None:
    guard = _guard()
    page = _snapshot(
        _box('Coins boost', 100, 100, 500, 150),
        _box('Daily check-in', 100, 260, 900, 300),
        _box('GO', 980, 280, 1040, 320),
        _box('Buy now', 400, 1800, 700, 1860),
    )

    with pytest.raises(RunAborted) as raised, guard.task('Daily coins'):
        guard.inspect(page)

    assert raised.value.code is AbortCode.DANGEROUS_CONTROL
    assert 'Buy now' in raised.value.reason


def test_the_known_surprise_product_detail_does_not_abort_for_its_two_buttons() -> None:
    guard = _guard()
    detail = _snapshot(
        _box('Item 1/3', 500, 120, 700, 170),
        _box('Wireless Earbuds', 60, 300, 600, 350),
        _box('Add to cart', 60, 1800, 300, 1860),
        _box('Buy now', 400, 1800, 700, 1860),
    )

    with guard.task('Browse surprise items'):
        guard.inspect(detail)

    assert guard.task_running is False


def test_a_real_spending_control_on_a_product_detail_still_aborts() -> None:
    guard = _guard()
    detail = _snapshot(
        _box('Item 1/3', 500, 120, 700, 170),
        _box('Buy now', 400, 1800, 700, 1860),
        _box('Place order', 700, 1800, 1000, 1860),
    )

    with pytest.raises(RunAborted) as raised, guard.task('Browse surprise items'):
        guard.inspect(detail)

    assert raised.value.code is AbortCode.DANGEROUS_CONTROL
    assert 'Place order' in raised.value.reason


def test_a_lower_readable_balance_aborts_the_run() -> None:
    guard = _guard()
    guard.expect_balance(40.0)

    guard.inspect(_coins('42'))
    with pytest.raises(RunAborted) as raised:
        guard.inspect(_coins('39'))

    assert raised.value.code is AbortCode.BALANCE_DECREASED
    assert raised.value.failure.code is FailureCode.BALANCE_DECREASED
    assert '40' in raised.value.reason
    assert '39' in raised.value.reason
    assert 'Daily coins' in raised.value.screen.labels


def test_an_unreadable_or_rising_balance_is_safe() -> None:
    guard = _guard()
    guard.expect_balance(40.0)

    guard.inspect(_snapshot(_box('Coins credits to redeem', 100, 330, 700, 380)))
    guard.inspect(_coins('40'))
    guard.inspect(_coins('1,240'))

    assert guard.task_running is False


def test_a_balance_without_a_baseline_is_not_checked() -> None:
    guard = _guard()

    guard.inspect(_coins('1'))


def test_wrap_inspects_every_captured_frame() -> None:
    guard = _guard()
    frames = (_coins('40'), _coins('42'))
    indices = {'value': 0}

    def capture() -> Snapshot:
        frame = frames[min(indices['value'], len(frames) - 1)]
        indices['value'] += 1
        return frame

    wrapped = guard.wrap(capture)

    assert wrapped() is frames[0]
    assert wrapped() is frames[1]
    assert guard.frames == 2


def test_a_nested_task_context_keeps_the_outer_task_active() -> None:
    guard = _guard()

    with guard.task('outer'):
        with guard.task('inner'):
            assert guard.task_running is True
        assert guard.task_running is True

    assert guard.task_running is False


def test_low_confidence_ocr_is_not_a_dangerous_control() -> None:
    low = TextBox(
        text='Buy now',
        box=((400, 1800), (700, 1800), (700, 1860), (400, 1860)),
        score=0.1,
    )

    assert find_dangerous_controls(_snapshot(_box('Wireless Earbuds', 60, 300, 600, 350), low)) == ()


def test_the_guard_rejects_a_non_positive_limit() -> None:
    with pytest.raises(ValueError, match='limit must be positive'):
        SafetyGuard(limit=0)


def test_the_abort_record_is_report_safe() -> None:
    guard = _guard()

    def inspect_three() -> None:
        for _ in range(3):
            guard.inspect(_unknown())

    with pytest.raises(RunAborted) as raised:
        inspect_three()

    record = raised.value.as_dict()
    screen = cast(dict[str, Any], record['screen'])
    failure = cast(dict[str, Any], record['failure'])
    assert record['code'] == 'unexpected_screen'
    assert screen['labels']
    assert failure['code'] == 'unexpected_screen'


def test_find_dangerous_controls_ignores_the_two_redeem_destination_labels() -> None:
    frame = _snapshot(
        _box('Coins credits to redeem', 100, 330, 700, 380),
        _box('YOU CAN NOW REDEEM!', 100, 700, 1100, 780),
    )

    assert find_dangerous_controls(frame) == ()
