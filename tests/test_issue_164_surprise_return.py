"""Issue #164: a misrouted BACK must not lose the surprise round.

The measured product page normally returns to the grid with one BACK. The
recorded failure `20261127T140626Z` left the round at 1/2 because BACK did not
land on a surface the executor counted. These tests replay that shape: BACK
lands on the coin page, the explicit shortcut/boost route re-enters the grid,
and progress is proved only by the grid's own counter.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from aliexpress_coins.control.controller import SwipeRecord, TapRecord
from aliexpress_coins.control.errors import InputError, PopupUnclosable
from aliexpress_coins.control.popups import DismissReport, DismissStop, PopupCorpus, load_popup_corpus
from aliexpress_coins.perception.errors import SnapshotError, StateTimeout
from aliexpress_coins.perception.ocr import RapidOcrEngine
from aliexpress_coins.perception.snapshot import Snapshot
from aliexpress_coins.runner import engine
from aliexpress_coins.runner.errors import RunInterrupted
from aliexpress_coins.runner.failures import FailureCode
from aliexpress_coins.tasks import surprise as surprise_module
from aliexpress_coins.tasks.browse import ReturnResult
from aliexpress_coins.tasks.catalogue import TaskCatalogue, load_task_catalogue
from aliexpress_coins.tasks.discovery import BoostList, DiscoveryRoute
from aliexpress_coins.tasks.surface import SurfaceRoute
from aliexpress_coins.tasks.surprise import (
    SurpriseOutcome,
    SurpriseResult,
    _is_return_surface,
    _is_scrolled_grid,
    _reenter_grid,
    is_surprise_grid,
)
from tests.test_tasks_surprise import (
    CATALOGUE_PATH,
    CORPUS_PATH,
    DATA,
    PRODUCTS,
    _boost_frame,
    _box,
    _coins_frame,
    _controller,
    _detail_frame,
    _entry,
    _grid_frame,
    _load,
    _logger,
    _run,
    _snapshot,
    _state,
    _SteppingClock,
    _World,
    _wrong_frame,
)


@pytest.fixture(scope='module')
def catalogue() -> TaskCatalogue:
    return load_task_catalogue(CATALOGUE_PATH)


@pytest.fixture(scope='module')
def corpus() -> PopupCorpus:
    return load_popup_corpus(CORPUS_PATH)


@pytest.fixture(scope='module')
def ocr() -> RapidOcrEngine:
    return RapidOcrEngine()


def _non_app_frame() -> Snapshot:
    """The launcher/clock surface the recorded misroute ended on."""
    return _snapshot(
        _box('9:12', 20, 5, 100, 35),
        _box('日', 500, 700, 560, 760),
        _box('7', 580, 700, 640, 760),
    )


class _SwipeWorld(_World):
    """A world whose swipe reveals the next frame on the same page."""

    def __init__(
        self,
        series: dict[str, Sequence[Snapshot]],
        transitions: dict[tuple[str, str | None], str],
        *,
        on_swipe: Callable[[str], str | None],
        start: str | None = None,
        on_tap: Callable[[str, str | None, int], str | None] | None = None,
        on_key: Callable[[str, str, int], str | None] | None = None,
    ) -> None:
        super().__init__(series, transitions, start=start, on_tap=on_tap, on_key=on_key)
        self._on_swipe = on_swipe

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int) -> None:
        super().swipe(x1, y1, x2, y2, duration_ms)
        target = self._on_swipe(self.current)
        if target is not None:
            self.go(target)


def _misrouted_world(detail: Snapshot | None = None, *, back_to: str = 'coins') -> _World:
    """BACK lands off-grid; the boost route must recover the grid."""
    pending: dict[str, str] = {}
    next_grid = {'grid0': 'grid1', 'grid1': 'grid2', 'grid2': 'grid3'}

    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        if screen in next_grid and label in PRODUCTS[:3]:
            pending['return'] = back_to
            pending['grid'] = next_grid[screen]
            return 'detail'
        if screen == 'coins' and label == 'Earn more >':
            return 'boost_route'
        if screen == 'boost_route' and label == 'GO':
            return pending.pop('grid')
        if screen == 'grid3' and label == 'Mission Completed':
            return 'complete'
        return None

    def on_key(screen: str, key: str, _count: int) -> str | None:
        if screen == 'detail' and key == 'KEYCODE_BACK':
            return pending.pop('return')
        return None

    return _World(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'grid0': (_grid_frame(0),),
            'grid1': (_grid_frame(1),),
            'grid2': (_grid_frame(2),),
            'grid3': (_grid_frame(3, mission=True),),
            'complete': (_grid_frame(3),),
            'detail': (detail if detail is not None else _detail_frame(),),
            'coins': (_coins_frame(),),
            'launcher': (_non_app_frame(),),
            'boost_route': (_boost_frame(progress='1/2'),),
        },
        transitions={('boost', 'GO'): 'grid0'},
        on_tap=on_tap,
        on_key=on_key,
    )


def test_a_misrouted_back_lands_on_the_coins_page_and_the_route_finishes_the_round(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    world = _misrouted_world()
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state, max_rounds=1)

    assert result.outcome is SurpriseOutcome.COMPLETED
    assert result.verification == 'counter_complete'
    assert result.round_records[0].counter_change == '0/3 -> 3/3'
    assert result.round_records[0].items == tuple(PRODUCTS[:3])
    assert result.round_records[0].taps == 3
    assert result.recorded is True
    assert state.count('surprise_items') == 1
    assert world.keys == ['KEYCODE_BACK'] * 3
    assert [tap.label for tap in result.taps if tap.label in PRODUCTS] == list(PRODUCTS[:3])
    # Every product return went through the boost row, not through a guessed
    # coordinate left behind by the misrouted BACK.
    assert sum(tap.label == 'Earn more' for tap in result.taps) == 3
    assert sum('re-enter the grid after a misrouted BACK' in tap.reason for tap in result.taps) == 3
    assert not any('cart' in (tap.label or '').lower() or 'buy' in (tap.label or '').lower() for tap in result.taps)


@pytest.mark.slow
def test_the_recorded_product_page_back_is_reentered_from_the_coin_page(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    ocr: RapidOcrEngine,
    tmp_path: Path,
) -> None:
    recorded = _load(DATA / 'flows' / 'surprise_items_item_detail.png', ocr)
    world = _misrouted_world(recorded)
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state, max_rounds=1)

    assert result.outcome is SurpriseOutcome.COMPLETED
    assert result.round_records[0].counter_change == '0/3 -> 3/3'
    assert result.round_records[0].items == tuple(PRODUCTS[:3])
    assert result.round_records[0].taps == 3
    assert result.recorded is True
    assert world.keys == ['KEYCODE_BACK'] * 3


@pytest.mark.slow
def test_the_recorded_headerless_grid_is_recognised_and_restored(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    ocr: RapidOcrEngine,
    tmp_path: Path,
) -> None:
    scrolled = _load(DATA / 'flows' / 'surprise_items_issue_164_scrolled_back.png', ocr)
    assert is_surprise_grid(scrolled) is False
    assert _is_scrolled_grid(scrolled) is True
    assert _is_scrolled_grid(scrolled, {'product a', 'product b'}) is False
    assert _is_return_surface(scrolled, {'product a', 'product b'}) is True

    pending: dict[str, str] = {}

    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        if screen == 'grid0' and label == 'Product A':
            pending['return'] = 'scrolled'
            return 'detail'
        if screen == 'restored' and label == 'Product B':
            pending['return'] = 'grid2'
            return 'detail'
        if screen == 'grid2' and label == 'Product C':
            pending['return'] = 'grid3'
            return 'detail'
        if screen == 'grid3' and label == 'Mission Completed':
            return 'complete'
        return None

    def on_key(screen: str, key: str, _count: int) -> str | None:
        if screen == 'detail' and key == 'KEYCODE_BACK':
            return pending.pop('return')
        return None

    world = _SwipeWorld(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'grid0': (_grid_frame(0),),
            'scrolled': (scrolled,),
            'restored': (_grid_frame(1),),
            'grid2': (_grid_frame(2),),
            'grid3': (_grid_frame(3, mission=True),),
            'complete': (_grid_frame(3),),
            'detail': (_detail_frame(),),
            'coins': (_coins_frame(),),
        },
        transitions={('boost', 'GO'): 'grid0'},
        on_swipe=lambda screen: 'restored' if screen == 'scrolled' else None,
        on_tap=on_tap,
        on_key=on_key,
    )
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state, max_rounds=1)

    assert result.outcome is SurpriseOutcome.COMPLETED
    assert result.round_records[0].counter_change == '0/3 -> 3/3'
    assert result.round_records[0].items == tuple(PRODUCTS[:3])
    assert result.recorded is True
    assert [swipe.label for swipe in result.swipes] == ['grid restore']
    assert not any(tap.label == 'Earn more' for tap in result.taps)
    assert world.keys == ['KEYCODE_BACK'] * 3


def test_a_known_one_column_scrolled_return_uses_the_existing_restore(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    scrolled = _snapshot(_box('Product A', 90, 300, 490, 355))
    pending: dict[str, str] = {}

    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        if screen == 'grid0' and label == 'Product A':
            pending['return'] = 'scrolled'
            return 'detail'
        if screen == 'restored' and label == 'Product B':
            pending['return'] = 'grid2'
            return 'detail'
        if screen == 'grid2' and label == 'Product C':
            pending['return'] = 'grid3'
            return 'detail'
        if screen == 'grid3' and label == 'Mission Completed':
            return 'complete'
        return None

    def on_key(screen: str, key: str, _count: int) -> str | None:
        if screen == 'detail' and key == 'KEYCODE_BACK':
            return pending.pop('return')
        return None

    world = _SwipeWorld(
        {
            'boost': (_boost_frame(progress='1/2'),),
            'grid0': (_grid_frame(0),),
            'scrolled': (scrolled,),
            'restored': (_grid_frame(1),),
            'grid2': (_grid_frame(2),),
            'grid3': (_grid_frame(3, mission=True),),
            'complete': (_grid_frame(3),),
            'detail': (_detail_frame(),),
            'coins': (_coins_frame(),),
        },
        {('boost', 'GO'): 'grid0'},
        start='boost',
        on_swipe=lambda screen: 'restored' if screen == 'scrolled' else None,
        on_tap=on_tap,
        on_key=on_key,
    )

    result = _run(world, catalogue, corpus, _state(tmp_path), max_rounds=1)

    assert result.outcome is SurpriseOutcome.COMPLETED
    assert result.round_records[0].items == tuple(PRODUCTS[:3])
    assert [swipe.label for swipe in result.swipes] == ['grid restore']
    assert not any(tap.label == 'Earn more' for tap in result.taps)


def test_a_lower_counter_after_reentry_resets_the_round_baseline(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    pending: dict[str, str] = {}

    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        if screen == 'grid0' and label == 'Product A':
            pending['return'] = 'coins'
            return 'detail'
        if screen == 'coins' and label == 'Earn more >':
            return 'boost_route'
        if screen == 'boost_route' and label == 'GO':
            return 'reset0'
        if screen == 'reset0' and label == 'Product B':
            pending['return'] = 'grid1'
            return 'detail'
        if screen == 'grid1' and label == 'Product C':
            pending['return'] = 'grid2'
            return 'detail'
        if screen == 'grid2' and label == 'Product D':
            pending['return'] = 'grid3'
            return 'detail'
        if screen == 'grid3' and label == 'Mission Completed':
            return 'complete'
        return None

    def on_key(screen: str, key: str, _count: int) -> str | None:
        if screen == 'detail' and key == 'KEYCODE_BACK':
            return pending.pop('return')
        return None

    world = _World(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'grid0': (_grid_frame(1),),
            'reset0': (_grid_frame(0),),
            'grid1': (_grid_frame(1),),
            'grid2': (_grid_frame(2),),
            'grid3': (_grid_frame(3, mission=True),),
            'complete': (_grid_frame(3),),
            'detail': (_detail_frame(),),
            'coins': (_coins_frame(),),
            'boost_route': (_boost_frame(progress='1/2'),),
        },
        transitions={('boost', 'GO'): 'grid0'},
        on_tap=on_tap,
        on_key=on_key,
    )

    result = _run(world, catalogue, corpus, _state(tmp_path), max_rounds=1)

    record = result.round_records[0]
    assert result.outcome is SurpriseOutcome.COMPLETED
    assert record.counter_change == '1/3 -> 3/3'
    # The reset round may offer the earlier product again; its lost credit is
    # not carried into the record, and only the post-reset credits are named.
    assert record.taps == 5
    assert record.items == ('Product B', 'Product C', 'Product D')
    assert result.recorded is True
    assert world.keys == ['KEYCODE_BACK'] * 4


def test_a_non_app_landing_is_reentered_through_the_shortcut(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    # A fallback for a genuinely non-app landing. The recorded #164 frames did
    # not do this: BACK returned to the headerless scrolled grid.
    world = _misrouted_world(back_to='launcher')
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state, max_rounds=1)

    assert result.outcome is SurpriseOutcome.COMPLETED
    assert result.round_records[0].counter_change == '0/3 -> 3/3'
    assert result.round_records[0].items == tuple(PRODUCTS[:3])
    assert result.recorded is True
    assert SurfaceRoute.SHORTCUT in world.navigations


def test_a_tap_that_never_returns_to_the_grid_is_not_progress(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    no_route = _snapshot(*(box for box in _coins_frame().boxes if 'Earn more' not in box.text))
    world = _World(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'grid0': (_grid_frame(0),),
            'detail': (_detail_frame(),),
            'coins': (no_route,),
        },
        transitions={
            ('boost', 'GO'): 'grid0',
            ('grid0', 'Product A'): 'detail',
        },
        on_key=lambda screen, key, _count: 'coins' if screen == 'detail' and key == 'KEYCODE_BACK' else None,
    )
    state = _state(tmp_path)

    result = _run(world, catalogue, corpus, state, max_rounds=1)

    record = result.round_records[0]
    assert result.outcome is SurpriseOutcome.FAILED
    assert result.verification == 'back_not_reached'
    assert 'shortcut/boost route could not re-enter it' in result.detail
    assert record.taps == 1
    assert record.items == ()
    assert record.complete is False
    assert record.proven is False
    assert result.rounds == 0
    assert result.recorded is False
    assert state.count('surprise_items') == 0
    assert [tap.label for tap in result.taps if tap.label in PRODUCTS] == ['Product A']


def test_the_surprise_back_failure_reaches_the_issue_168_reset_retry() -> None:
    result = SurpriseResult(
        key='surprise_items',
        title='Browse surprise items',
        outcome=SurpriseOutcome.FAILED,
        verification='back_not_reached',
        detail='BACK did not return to the grid and the shortcut/boost route could not re-enter it',
        limit=2,
        done_today_before=1,
        done_today_after=1,
    )

    dispatched = engine._from_executor(result)

    assert dispatched.failure is not None
    assert dispatched.failure.code is FailureCode.TASK_UNVERIFIED
    assert dispatched.failure.retryable is True
    assert engine._reset_retry_trigger(dispatched) == 'back_not_reached'


def _tap_record(label: str) -> TapRecord:
    return TapRecord(
        index=0,
        label=label,
        reason='test',
        requested=(0, 0),
        clamped=(0, 0),
        jitter=(0, 0),
        point=(0, 0),
        suppressed=False,
    )


def _call_reenter(
    world: _World,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    *,
    navigate: object,
    taps: list[TapRecord] | None = None,
    swipes: list[SwipeRecord] | None = None,
    dismissals: list[DismissReport] | None = None,
) -> tuple[object, str | None]:
    entry = _entry(catalogue)
    spec = entry.spec
    assert spec is not None
    clock = _SteppingClock()
    tap_records = [] if taps is None else taps
    swipe_records = [] if swipes is None else swipes
    dismissal_records = [] if dismissals is None else dismissals
    return _reenter_grid(
        _controller(world, world.frame),
        world.capture,
        corpus,
        navigate,  # type: ignore[arg-type]
        world.current_frame,
        spec=spec,
        templates=None,
        route=SurfaceRoute.SHORTCUT,
        arrival_timeout=0.5,
        return_timeout=0.5,
        return_interval=0.5,
        return_attempts=1,
        interval=0.5,
        settle=0.0,
        clock=clock,
        sleep=clock.sleep,
        logger=_logger(),
        taps=tap_records,
        dismissals=dismissal_records,
        swipes=swipe_records,
    )


def test_grid_reentry_without_a_navigation_route_is_named(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = _World({'grid': (_grid_frame(0),)}, {}, start='grid')
    caplog.set_level(logging.WARNING)

    current, stop = _call_reenter(world, catalogue, corpus, navigate=None)

    assert current is None
    assert stop is None
    assert 'no navigation route was supplied' in caplog.text


@pytest.mark.parametrize(
    ('error', 'route_level'),
    [
        (StateTimeout('route timed out'), True),
        (SnapshotError('capture failed'), False),
        (PopupUnclosable('unclosable popup'), False),
        (RunInterrupted('stop requested'), False),
    ],
)
def test_a_route_exception_is_named_or_re_raised(
    error: Exception,
    route_level: bool,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World({'grid': (_grid_frame(0),)}, {}, start='grid')

    def explode(*_args: object, **_kwargs: object) -> ReturnResult:
        raise error

    monkeypatch.setattr(surprise_module, 'return_to_coins_page', explode)

    if route_level:
        assert _call_reenter(world, catalogue, corpus, navigate=world.navigate) == (None, None)
    else:
        with pytest.raises(type(error)):
            _call_reenter(world, catalogue, corpus, navigate=world.navigate)


def test_a_route_that_does_not_prove_the_coin_page_is_not_accepted(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World({'grid': (_grid_frame(0),)}, {}, start='grid')
    monkeypatch.setattr(
        surprise_module,
        'return_to_coins_page',
        lambda *_args, **_kwargs: ReturnResult(False, True, 1, SurfaceRoute.SHORTCUT, 0.0),
    )

    assert _call_reenter(world, catalogue, corpus, navigate=world.navigate) == (None, None)


@pytest.mark.parametrize(
    ('error', 'route_level'),
    [
        (StateTimeout('boost route timed out'), True),
        (SnapshotError('capture failed'), False),
        (PopupUnclosable('unclosable popup'), False),
        (RunInterrupted('stop requested'), False),
    ],
)
def test_a_boost_route_exception_is_named_or_re_raised(
    error: Exception,
    route_level: bool,
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World({'grid': (_grid_frame(0),)}, {}, start='grid')
    monkeypatch.setattr(
        surprise_module,
        'return_to_coins_page',
        lambda *_args, **_kwargs: ReturnResult(
            True,
            True,
            1,
            SurfaceRoute.SHORTCUT,
            0.0,
            snapshot=_coins_frame(),
        ),
    )

    def explode(*_args: object, **_kwargs: object) -> BoostList:
        raise error

    monkeypatch.setattr(surprise_module, 'open_boost_list', explode)

    if route_level:
        assert _call_reenter(world, catalogue, corpus, navigate=world.navigate) == (None, None)
    else:
        with pytest.raises(type(error)):
            _call_reenter(world, catalogue, corpus, navigate=world.navigate)


def test_a_back_that_lands_on_an_inline_coin_row_reenters_without_the_boost_sheet(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inline = _snapshot(
        *_coins_frame().boxes,
        _box('Browse surprise items', 60, 1200, 700, 1250),
        _box('1/2', 1000, 1200, 1080, 1250),
        _box('GO', 1090, 1200, 1160, 1250),
    )
    grid = _grid_frame(1)
    world = _World(
        {'inline': (inline,), 'grid': (grid,)},
        {('inline', 'GO'): 'grid'},
        start='inline',
    )
    monkeypatch.setattr(
        surprise_module,
        'return_to_coins_page',
        lambda *_args, **_kwargs: ReturnResult(
            True,
            False,
            0,
            SurfaceRoute.SHORTCUT,
            0.0,
            snapshot=inline,
        ),
    )
    taps: list[TapRecord] = []

    current, stop = _call_reenter(world, catalogue, corpus, navigate=world.navigate, taps=taps)

    assert current == grid
    assert stop is None
    assert [tap.label for tap in taps] == ['GO']


def test_a_reentry_attempt_that_never_paints_a_grid_is_not_accepted(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(
        {'boost_route': (_boost_frame(progress='1/2'),), 'wrong': (_wrong_frame(),)},
        {('boost_route', 'GO'): 'wrong'},
        start='boost_route',
    )
    monkeypatch.setattr(
        surprise_module,
        'return_to_coins_page',
        lambda *_args, **_kwargs: ReturnResult(
            True,
            True,
            1,
            SurfaceRoute.SHORTCUT,
            0.0,
            snapshot=_coins_frame(),
        ),
    )

    assert _call_reenter(world, catalogue, corpus, navigate=world.navigate) == (None, None)


def test_the_boost_row_is_found_below_the_first_viewport(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    top = _snapshot(
        _box('Coins boost', 80, 90, 400, 150),
        _box('Explore sponsored items', 60, 420, 700, 470),
        _box('GO', 1000, 480, 1080, 530),
    )
    below = _boost_frame(progress='1/2')
    grid = _grid_frame(1)
    world = _SwipeWorld(
        {'top': (top,), 'below': (below,), 'grid': (grid,)},
        {('below', 'GO'): 'grid'},
        start='top',
        on_swipe=lambda screen: 'below' if screen == 'top' else None,
    )
    monkeypatch.setattr(
        surprise_module,
        'return_to_coins_page',
        lambda *_args, **_kwargs: ReturnResult(
            True,
            True,
            1,
            SurfaceRoute.SHORTCUT,
            0.0,
            snapshot=_coins_frame(),
        ),
    )
    monkeypatch.setattr(
        surprise_module,
        'open_boost_list',
        lambda *_args, **_kwargs: BoostList(snapshot=top, route=DiscoveryRoute.BOOST, detail='opened'),
    )
    taps: list[TapRecord] = []
    swipes: list[SwipeRecord] = []

    current, stop = _call_reenter(
        world,
        catalogue,
        corpus,
        navigate=world.navigate,
        taps=taps,
        swipes=swipes,
    )

    assert current == grid
    assert stop is None
    assert [tap.label for tap in taps] == ['GO']
    assert [swipe.label for swipe in swipes] == ['scroll search']


def test_a_known_scrolled_grid_without_a_detail_return_is_still_restored(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    scrolled = _snapshot(
        _box('Product A', 90, 300, 490, 355),
        _box('Product B', 690, 300, 1090, 355),
        _box('C$30.00', 60, 470, 300, 510),
        _box('C$31.00', 690, 470, 1090, 510),
    )
    world = _SwipeWorld(
        {
            'boost': (_boost_frame(progress='1/2'),),
            'grid0': (_grid_frame(0),),
            'scrolled': (scrolled,),
            'restored': (_grid_frame(0),),
            'coins': (_coins_frame(),),
        },
        {('boost', 'GO'): 'grid0', ('grid0', 'Product A'): 'scrolled'},
        start='boost',
        on_swipe=lambda screen: 'restored' if screen == 'scrolled' else None,
    )

    result = _run(
        world,
        catalogue,
        corpus,
        _state(tmp_path),
        max_rounds=1,
        arrival_timeout=0.1,
        counter_timeout=0.1,
    )

    assert any(swipe.label == 'grid restore' for swipe in result.swipes)


def test_a_failed_row_search_gesture_is_named_not_retried_forever(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    top = _snapshot(_box('Coins boost', 80, 90, 400, 150), _box('GO', 1000, 480, 1080, 530))
    world = _World({'top': (top,), 'grid': (_grid_frame(0),)}, {}, start='top')
    monkeypatch.setattr(
        surprise_module,
        'return_to_coins_page',
        lambda *_args, **_kwargs: ReturnResult(
            True,
            True,
            1,
            SurfaceRoute.SHORTCUT,
            0.0,
            snapshot=_coins_frame(),
        ),
    )
    monkeypatch.setattr(
        surprise_module,
        'open_boost_list',
        lambda *_args, **_kwargs: BoostList(snapshot=top, route=DiscoveryRoute.BOOST, detail='opened'),
    )

    def explode(*_args: object, **_kwargs: object) -> object:
        raise InputError('swipe unavailable')

    monkeypatch.setattr(surprise_module, 'scroll_until_found', explode)

    assert _call_reenter(world, catalogue, corpus, navigate=world.navigate) == (None, None)


def test_a_boost_retap_is_kept_in_the_evidence(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(
        {'boost_route': (_boost_frame(progress='1/2'),), 'grid': (_grid_frame(1),)},
        {('boost_route', 'GO'): 'grid'},
        start='boost_route',
    )
    monkeypatch.setattr(
        surprise_module,
        'return_to_coins_page',
        lambda *_args, **_kwargs: ReturnResult(
            True,
            True,
            1,
            SurfaceRoute.SHORTCUT,
            0.0,
            snapshot=_coins_frame(),
        ),
    )
    boost = BoostList(
        snapshot=_boost_frame(progress='1/2'),
        route=DiscoveryRoute.BOOST,
        detail='opened',
        tap=_tap_record('Earn more'),
        retap=_tap_record('Earn more'),
    )
    monkeypatch.setattr(surprise_module, 'open_boost_list', lambda *_args, **_kwargs: boost)
    taps: list[TapRecord] = []

    current, stop = _call_reenter(world, catalogue, corpus, navigate=world.navigate, taps=taps)

    assert current is not None
    assert stop is None
    assert [tap.label for tap in taps] == ['Earn more', 'Earn more', 'GO']


def test_a_popup_swept_while_re_entering_the_grid_is_recorded(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(
        {'boost_route': (_boost_frame(progress='1/2'),), 'grid': (_grid_frame(1),)},
        {('boost_route', 'GO'): 'grid'},
        start='boost_route',
    )
    monkeypatch.setattr(
        surprise_module,
        'return_to_coins_page',
        lambda *_args, **_kwargs: ReturnResult(
            True,
            True,
            1,
            SurfaceRoute.SHORTCUT,
            0.0,
            snapshot=_coins_frame(),
        ),
    )
    boost = BoostList(snapshot=_boost_frame(progress='1/2'), route=DiscoveryRoute.BOOST, detail='opened')
    report = DismissReport(closed=('test_popup',), attempts=(), frames=(), remaining=False, stop=DismissStop.CLEAR)
    grid = _grid_frame(1)
    monkeypatch.setattr(surprise_module, 'open_boost_list', lambda *_args, **_kwargs: boost)
    monkeypatch.setattr(
        surprise_module,
        '_wait_for_surface',
        lambda *_args, **_kwargs: (grid, True, report, None),
    )
    dismissals: list[DismissReport] = []

    current, stop = _call_reenter(world, catalogue, corpus, navigate=world.navigate, dismissals=dismissals)

    assert current is not None
    assert stop is None
    assert dismissals == [report]


def test_a_redeem_interstitial_on_the_returned_page_is_a_stop(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redeem = _snapshot(
        _box('YOU CAN NOW REDEEM!', 80, 700, 1120, 780),
        _box('Coins', 101, 194, 254, 242),
        _box('Daily coins', 66, 919, 320, 978),
    )
    world = _World({'grid': (_grid_frame(0),)}, {}, start='grid')
    monkeypatch.setattr(
        surprise_module,
        'return_to_coins_page',
        lambda *_args, **_kwargs: ReturnResult(
            True,
            True,
            1,
            SurfaceRoute.SHORTCUT,
            0.0,
            snapshot=redeem,
        ),
    )

    current, stop = _call_reenter(world, catalogue, corpus, navigate=world.navigate)

    assert current is redeem
    assert stop == 'redeem_interstitial'


def test_the_reward_interstitial_during_reentry_stops_the_round(
    catalogue: TaskCatalogue,
    corpus: PopupCorpus,
    tmp_path: Path,
) -> None:
    redeem = _snapshot(
        _box('YOU CAN NOW REDEEM!', 80, 700, 1120, 780),
        _box('Coins', 101, 194, 254, 242),
        _box('Daily coins', 66, 919, 320, 978),
    )

    def on_tap(screen: str, label: str | None, _count: int) -> str | None:
        if screen == 'grid0' and label == 'Product A':
            return 'detail'
        if screen == 'coins' and label == 'Earn more >':
            return 'boost_route'
        return None

    world = _World(
        series={
            'boost': (_boost_frame(progress='1/2'),),
            'grid0': (_grid_frame(0),),
            'detail': (_detail_frame(),),
            'coins': (_coins_frame(),),
            'boost_route': (_boost_frame(progress='1/2'),),
            'redeem': (redeem,),
        },
        transitions={
            ('boost', 'GO'): 'grid0',
            ('boost_route', 'GO'): 'redeem',
        },
        on_tap=on_tap,
        on_key=lambda screen, key, _count: 'coins' if screen == 'detail' and key == 'KEYCODE_BACK' else None,
    )

    result = _run(world, catalogue, corpus, _state(tmp_path), max_rounds=1)

    assert result.outcome is SurpriseOutcome.FAILED
    assert result.stop == 'redeem_interstitial'
    assert result.round_records[0].taps == 1
    assert result.round_records[0].proven is False
    assert result.recorded is False
