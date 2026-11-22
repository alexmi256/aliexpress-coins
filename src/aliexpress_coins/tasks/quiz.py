"""The quiz executor: open the quiz, answer at random, absorb the result (#25).

The quiz is a page transition, not a dwell task (docs/05-app-knowledge.md §5):
open it from the roster row, read the question, pick one of the answer options at
random, read the result page, tap ``Go back`` and absorb whatever comes after —
the coin page, or the ``YOU CAN NOW REDEEM!`` interstitial when the quiz paid
coin credit — and return.

The measured contract has four rules:

* **the deny list never leaves the dispatch point** — this module only ever taps
  the row's own ``GO`` and the quiz page's own controls. It never scans for a
  purchase control, and it reads the result page's learned answer as evidence,
  not as a directive to guess better;
* **options are enumerated from OCR** — two-option (True/False) and four-option
  questions were both observed, so the executor never assumes a count;
* **the screen is the authority** — the result page must print ``Correct answer:``
  and ``Your answer:``, and a visible ``You got +N`` is the app's own
  acknowledgement: it completes the quiz even when the roster row cannot be
  re-read. The row is the second, stronger signal when it is available — advanced,
  gone, or claimed — and a missing row on the conditional inline coin page is
  never treated as proof by itself;
* **the run always returns** — a failure returns through the shortcut route,
  never a blind BACK press, which can leave the app.

A wrong answer still pays (docs/05 §5: "a wrong answer still pays something"),
so nothing downstream branches on a guess being right. The learned answer is
recorded in the result for the report; the question bank is issue #39 and is not
written here.
"""

from __future__ import annotations

import json
import logging
import random
import re
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from aliexpress_coins.control.controller import LOGGER_NAME, Controller, TapRecord
from aliexpress_coins.control.errors import PopupUnclosable
from aliexpress_coins.control.popups import DismissReport, PopupCorpus, dismiss_after
from aliexpress_coins.perception.errors import StateTimeout
from aliexpress_coins.perception.layout import Row
from aliexpress_coins.perception.matching import (
    DEFAULT_MIN_OCR_SCORE,
    DEFAULT_MIN_SCORE,
    normalize,
    similarity,
)
from aliexpress_coins.perception.snapshot import Snapshot, TextBox
from aliexpress_coins.perception.state import CaptureSource, ScreenState, classify
from aliexpress_coins.perception.templates import TemplateStore
from aliexpress_coins.runner.errors import RunInterrupted
from aliexpress_coins.runner.safety import RunAborted
from aliexpress_coins.runner.state import DailyStateStore
from aliexpress_coins.tasks.browse import DONE_LABELS, ReturnResult, find_task_row, return_to_coins_page
from aliexpress_coins.tasks.catalogue import AllowedTaskSpec, TaskCategory
from aliexpress_coins.tasks.discovery import BoostList, DiscoveryRoute, RosterEntry, open_boost_list
from aliexpress_coins.tasks.dispatch import authorization_of
from aliexpress_coins.tasks.errors import TaskError
from aliexpress_coins.tasks.surface import (
    COIN_PAGE_MARKERS,
    REDEEM_MARKER,
    RouteFirer,
    SurfaceRoute,
    contains_text,
    has_task_rows,
    is_boost_page,
    is_empty_boost,
)

__all__ = [
    'DEFAULT_EXIT_TIMEOUT',
    'DEFAULT_INTERVAL',
    'DEFAULT_LOAD_TIMEOUT',
    'DEFAULT_RESULT_TIMEOUT',
    'DEFAULT_RETURN_ATTEMPTS',
    'DEFAULT_RETURN_INTERVAL',
    'DEFAULT_RETURN_TIMEOUT',
    'MAX_ROUNDS',
    'MIN_OPTIONS',
    'OPTION_GAP_RATIO',
    'QUESTION_PREFIX',
    'REDEEM_INTERSTITIAL',
    'AnswerOption',
    'QuizOutcome',
    'QuizResult',
    'find_answer_options',
    'find_go_back',
    'is_question_state',
    'is_result_state',
    'question_of',
    'read_payout',
    'run_quiz',
    'run_quiz_task',
]

REDEEM_INTERSTITIAL = 'redeem_interstitial'
"""The corpus family name of the unclosable reward interstitial."""

DEFAULT_LOAD_TIMEOUT = 15.0
"""How long the roster surface may take to stop loading before the executor gives up."""

DEFAULT_RESULT_TIMEOUT = 10.0
"""How long the question page may take to appear after the row's ``GO`` is pressed."""

DEFAULT_EXIT_TIMEOUT = 10.0
"""How long to wait for the result page's ``Go back`` to leave the quiz."""

DEFAULT_INTERVAL = 0.5
"""How often capture is polled while a page is loading or settling."""

DEFAULT_RETURN_TIMEOUT = 20.0
"""How long one shortcut re-entry may take to show the coin-page markers."""

DEFAULT_RETURN_INTERVAL = 0.5
"""How often the return route is polled."""

DEFAULT_RETURN_ATTEMPTS = 2
"""The bounded number of shortcut re-entries spent trying to reach the coin page."""

MAX_ROUNDS = 3
"""The most question/result rounds one visit may run.

One question per visit was the observed contract (docs/05 §5). The bound exists
so a page that re-shows questions cannot loop forever; reaching it stops the
loop and the return/verification decides, exactly as a completed visit would.
"""

MIN_OPTIONS = 2
"""The fewest options the question page must show before the executor taps one.

The spec's measured layouts are two-option (True/False) and four-option
questions. A single detected cluster is a false consolidation, so the task
fails honestly rather than tapping a guess.
"""

OPTION_GAP_RATIO = 0.5
"""How far apart two OCR lines may be and still be one option.

Lines within one option merge when their vertical gap is under this fraction of
the frame's median box height; a gap at or over it starts the next option.
Measured on the reviewed four-option frame: the wrapped question's lines sit
9 px apart, the options sit ~60 px apart, so 0.5 * median height separates them.
"""

QUESTION_PREFIX = re.compile(r'^\s*Q\s*[:.)]', re.IGNORECASE)
"""The prefix the question line carries (``Q : Which statements …``)."""

_PAYOUT = re.compile(r'You got\s*\+\s*(\d+)', re.IGNORECASE)
"""The result page's payout line (``You got +6``)."""

_CORRECT_ANSWER = re.compile(r'correct\s*answer', re.IGNORECASE)
"""The marker for the learned-answer line (``Correct answer: X``)."""

_BARE_NUMBER = re.compile(r'^\s*\d{1,3}\s*$')
"""A standalone digit: a page counter, never an answer option."""


class QuizOutcome(StrEnum):
    """What one quiz visit actually did."""

    COMPLETED = 'completed'
    FAILED = 'failed'
    SKIPPED = 'skipped'


@dataclass(frozen=True, slots=True)
class AnswerOption:
    """One answer option, as text plus the box that carries it.

    ``text`` is every OCR line the option wraps, joined in reading order;
    ``box`` is the first line's box, which is the tap target. Tapping any line
    of the option's block selects it; the composite bounding box is not a safe
    target (it can cover the gap between lines), so the first line is tapped.
    """

    text: str
    box: TextBox

    def as_dict(self) -> dict[str, object]:
        """Render the option for the run report."""
        return {'text': self.text, 'box': list(self.box.bbox)}


@dataclass(frozen=True, slots=True)
class QuizResult:
    """One quiz visit, the evidence behind it and whether state was recorded.

    ``verification`` names the proof: ``interstitial`` (the redeem overlay
    appeared, which means the quiz paid coin credit), ``row_gone``, ``action_gone``,
    ``action_completed`` on the success path; ``daily_limit``, ``not_settled``,
    ``task_surface_unavailable``, ``row_vanished``, ``row_complete``,
    ``question_not_arrived``, ``options_not_found``, ``result_not_arrived``,
    ``go_back_not_found``, ``exit_not_reached``, ``no_change``, ``no_observation``
    or ``return_failed`` on the others. ``detail`` is the report-safe sentence.
    """

    key: str
    title: str
    outcome: QuizOutcome
    verification: str
    detail: str
    limit: int
    done_today_before: int
    done_today_after: int
    rounds: int = 0
    question: str | None = None
    options: tuple[AnswerOption, ...] = ()
    chosen: str | None = None
    correct: str | None = None
    payout: str | None = None
    taps: tuple[TapRecord, ...] = ()
    stop: str | None = None
    recorded: bool = False
    return_state: ReturnResult | None = None
    dismissals: tuple[DismissReport, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, 'options', tuple(self.options))
        object.__setattr__(self, 'taps', tuple(self.taps))
        object.__setattr__(self, 'dismissals', tuple(self.dismissals))

    @property
    def completed(self) -> bool:
        """Whether the quiz was verified and the completion recorded."""
        return self.outcome is QuizOutcome.COMPLETED

    @property
    def failed(self) -> bool:
        """Whether the screen did not prove the quiz."""
        return self.outcome is QuizOutcome.FAILED

    @property
    def skipped(self) -> bool:
        """Whether the executor did no work because the spec or the app says stop."""
        return self.outcome is QuizOutcome.SKIPPED

    @property
    def interstitial(self) -> str | None:
        """The unclosable reward interstitial that ended the flow, if any."""
        return self.stop

    @property
    def returned(self) -> bool:
        """Whether the run finished on the coins page."""
        return self.return_state is not None and self.return_state.returned

    @property
    def reward_coins(self) -> int | None:
        """The payout as an integer when OCR read a number, else ``None``."""
        if self.payout is None:
            return None
        match = re.search(r'\d+', self.payout)
        return int(match.group(0)) if match is not None else None

    def as_dict(self) -> dict[str, object]:
        """Render the result for ``run.json`` without losing the evidence."""
        record: dict[str, object] = {
            'key': self.key,
            'title': self.title,
            'outcome': self.outcome.value,
            'verification': self.verification,
            'detail': self.detail,
            'limit': self.limit,
            'done_today_before': self.done_today_before,
            'done_today_after': self.done_today_after,
            'rounds': self.rounds,
            'question': self.question,
            'options': [option.text for option in self.options],
            'chosen': self.chosen,
            'correct': self.correct,
            'payout': self.payout,
            'reward_coins': self.reward_coins,
            'recorded': self.recorded,
            'stop': self.stop,
            'taps': [tap.as_dict() for tap in self.taps],
            'dismissals': [report.as_dict() for report in self.dismissals],
        }
        if self.return_state is not None:
            record['return'] = self.return_state.as_dict()
        return record


@dataclass(frozen=True, slots=True)
class _Verdict:
    """The internal answer from one verification frame."""

    status: str
    decisive: bool
    detail: str


def find_answer_options(snapshot: Snapshot) -> tuple[AnswerOption, ...]:
    """Enumerate the answer options on a quiz question page.

    The question is the box carrying the ``Q :`` prefix; it is clustered with
    its continuation lines (a question wraps over two), and every OCR line
    below the question is clustered into an option by the gap rule. Clusters
    whose whole text is a bare number — a page counter — are never options.
    """
    question = _question_box(snapshot)
    if question is None:
        return ()
    heights = [box.height for box in snapshot.boxes if box.height > 0]
    if not heights:
        return ()
    median_height = statistics.median(heights)
    question_cluster = _line_cluster(snapshot, question, median_height)
    question_top = min(box.bbox[1] for box in question_cluster)

    clusters: list[list[TextBox]] = []
    current: list[TextBox] = []
    current_bottom = 0.0
    for box in sorted(snapshot.boxes, key=lambda item: (item.bbox[1], item.bbox[0])):
        if box in question_cluster or box.bbox[1] < question_top:
            continue
        if current and box.bbox[1] - current_bottom >= median_height * OPTION_GAP_RATIO:
            clusters.append(current)
            current = []
        current.append(box)
        current_bottom = max(box.bbox[3], current_bottom)
    if current:
        clusters.append(current)

    options: list[AnswerOption] = []
    for cluster in clusters:
        text = ' '.join(box.text for box in cluster)
        normalized = normalize(text)
        if len(normalized) < 2:
            continue
        if _BARE_NUMBER.fullmatch(text):
            continue
        options.append(AnswerOption(text=text, box=cluster[0]))
    return tuple(options)


def find_go_back(snapshot: Snapshot) -> TextBox | None:
    """Return the result page's ``Go back`` control, or ``None``.

    OCR often appends a chevron (``Go back >``) or the app names the destination
    (``Go back to coins``); the normalised prefix still identifies the exit
    control. This is the one result-page control the flow must find after the
    quiz has already paid.
    """
    for box in snapshot.boxes:
        if box.score < DEFAULT_MIN_OCR_SCORE:
            continue
        if normalize(box.text).startswith('go back'):
            return box
    return None


def read_payout(snapshot: Snapshot) -> str | None:
    """Return the result page's payout line (``+6``), or ``None``."""
    for box in snapshot.boxes:
        if box.score < DEFAULT_MIN_OCR_SCORE:
            continue
        match = _PAYOUT.search(box.text)
        if match is not None:
            return f'+{match.group(1)}'
    return None


def run_quiz_task(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    state: DailyStateStore,
    entry: RosterEntry,
    *,
    navigate: RouteFirer,
    snapshot: Snapshot | None = None,
    templates: TemplateStore | None = None,
    route: SurfaceRoute = SurfaceRoute.SHORTCUT,
    load_timeout: float = DEFAULT_LOAD_TIMEOUT,
    arrival_timeout: float = DEFAULT_RESULT_TIMEOUT,
    exit_timeout: float = DEFAULT_EXIT_TIMEOUT,
    interval: float = DEFAULT_INTERVAL,
    return_timeout: float = DEFAULT_RETURN_TIMEOUT,
    return_interval: float = DEFAULT_RETURN_INTERVAL,
    return_attempts: int = DEFAULT_RETURN_ATTEMPTS,
    settle: float = 0.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    logger: logging.Logger | None = None,
) -> QuizResult:
    """Run one quiz task from an allowed roster row.

    The executor re-resolves the row's ``GO`` from the current frame, presses it,
    waits for the question, enumerates the options from OCR, picks one with the
    injected ``rng``, reads the result page, taps ``Go back`` and absorbs what
    follows. State is checked before the first capture; an absent row is a skip,
    never a tap. A wrong answer is not a failure: a visible payout or a changed
    row records the completion, and only a question that never arrived, a
    question with no answer control, or a transport fault is reported failed.
    """
    log = logger if logger is not None else logging.getLogger(LOGGER_NAME)
    spec = _allowed_spec(entry)
    limit = spec.max_per_run
    done_before = state.count(spec.key)
    if done_before >= limit:
        result = _skipped(
            spec,
            entry.title,
            done_before,
            limit,
            verification='daily_limit',
            detail=f'{spec.key!r} already completed {done_before} time(s) today; the limit is {limit}',
        )
        _log_result(log, result)
        return result

    if load_timeout <= 0:
        raise ValueError(f'load_timeout must be positive, got {load_timeout!r}')
    if arrival_timeout <= 0:
        raise ValueError(f'arrival_timeout must be positive, got {arrival_timeout!r}')
    if exit_timeout <= 0:
        raise ValueError(f'exit_timeout must be positive, got {exit_timeout!r}')
    if interval <= 0:
        raise ValueError(f'interval must be positive, got {interval!r}')
    if return_timeout <= 0:
        raise ValueError(f'return_timeout must be positive, got {return_timeout!r}')
    if return_interval <= 0:
        raise ValueError(f'return_interval must be positive, got {return_interval!r}')
    if return_attempts < 1:
        raise ValueError(f'return_attempts must be positive, got {return_attempts!r}')
    if settle < 0:
        raise ValueError(f'settle must not be negative, got {settle!r}')

    rng = rng if rng is not None else random.Random()

    frame = snapshot if snapshot is not None else _capture(capture)
    frame, settled, entry_sweep = _settle_surface(
        controller,
        capture,
        corpus,
        frame,
        templates=templates,
        timeout=load_timeout,
        interval=interval,
        settle=settle,
        clock=clock,
        sleep=sleep,
        logger=log,
    )

    row, action, _badge = find_task_row(frame, entry.title)

    if not settled:
        return _finish_failed(
            controller=controller,
            capture=capture,
            corpus=corpus,
            navigate=navigate,
            spec=spec,
            entry=entry,
            done_before=done_before,
            limit=limit,
            verification='not_settled',
            detail='the task surface never stopped loading; no action was tapped',
            route=route,
            templates=templates,
            return_timeout=return_timeout,
            return_interval=return_interval,
            return_attempts=return_attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=log,
        )

    if not _is_task_surface(frame):
        return _finish_failed(
            controller=controller,
            capture=capture,
            corpus=corpus,
            navigate=navigate,
            spec=spec,
            entry=entry,
            done_before=done_before,
            limit=limit,
            verification='task_surface_unavailable',
            detail=f'{entry.title!r} is not on a task surface in this frame',
            route=route,
            templates=templates,
            return_timeout=return_timeout,
            return_interval=return_interval,
            return_attempts=return_attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=log,
        )

    if row is None or action is None:
        status = 'row_vanished' if row is None else 'row_complete'
        detail = (
            f'{entry.title!r} is absent from the frame; the roster entry is stale'
            if row is None
            else f'{entry.title!r} has no action left; the app already considers it finished'
        )
        result = _skipped(
            spec,
            entry.title,
            done_before,
            limit,
            verification=status,
            detail=detail,
        )
        _log_result(log, result)
        return result

    taps: list[TapRecord] = []
    rounds = 0
    question: str | None = None
    options: tuple[AnswerOption, ...] = ()
    chosen: str | None = None
    correct: str | None = None
    payout: str | None = None
    stop: str | None = None
    dismissals: list[DismissReport] = []
    if entry_sweep is not None:
        dismissals.append(entry_sweep)
    current = frame

    try:
        taps.append(
            controller.tap_box(
                action,
                why=f'quiz {spec.key}: open the quiz',
                label=action.text,
            ),
        )
        while True:
            current, arrived, dismiss, stop = _wait_for_surface(
                controller,
                capture,
                corpus,
                _is_question_page,
                timeout=arrival_timeout,
                interval=interval,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=log,
                templates=templates,
            )
            if dismiss is not None:
                dismissals.append(dismiss)
            if stop is not None:
                break
            if not arrived:
                return _finish_failed(
                    controller=controller,
                    capture=capture,
                    corpus=corpus,
                    navigate=navigate,
                    spec=spec,
                    entry=entry,
                    done_before=done_before,
                    limit=limit,
                    verification='question_not_arrived',
                    detail='the question page never appeared after the row was pressed',
                    rounds=rounds,
                    question=question,
                    options=options,
                    chosen=chosen,
                    correct=correct,
                    payout=payout,
                    taps=taps,
                    stop=stop,
                    dismissals=dismissals,
                    route=route,
                    templates=templates,
                    return_timeout=return_timeout,
                    return_interval=return_interval,
                    return_attempts=return_attempts,
                    settle=settle,
                    clock=clock,
                    sleep=sleep,
                    logger=log,
                )
            options = find_answer_options(current)
            if len(options) < MIN_OPTIONS:
                return _finish_failed(
                    controller=controller,
                    capture=capture,
                    corpus=corpus,
                    navigate=navigate,
                    spec=spec,
                    entry=entry,
                    done_before=done_before,
                    limit=limit,
                    verification='options_not_found',
                    detail='the question page showed fewer than 2 answer options; no answer was picked',
                    rounds=rounds,
                    question=question,
                    options=options,
                    chosen=chosen,
                    correct=correct,
                    payout=payout,
                    taps=taps,
                    stop=stop,
                    dismissals=dismissals,
                    route=route,
                    templates=templates,
                    return_timeout=return_timeout,
                    return_interval=return_interval,
                    return_attempts=return_attempts,
                    settle=settle,
                    clock=clock,
                    sleep=sleep,
                    logger=log,
                )
            question = _question_text(current)
            rounds += 1
            chosen_option = rng.choice(options)
            chosen = chosen_option.text
            taps.append(
                controller.tap_box(
                    chosen_option.box,
                    why=f'quiz {spec.key}: choose the random answer',
                    label=chosen_option.box.text,
                ),
            )
            current, arrived, dismiss, stop = _wait_for_surface(
                controller,
                capture,
                corpus,
                _is_result_page,
                timeout=arrival_timeout,
                interval=interval,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=log,
                templates=templates,
            )
            if dismiss is not None:
                dismissals.append(dismiss)
            if stop is not None:
                break
            if not arrived:
                return _finish_failed(
                    controller=controller,
                    capture=capture,
                    corpus=corpus,
                    navigate=navigate,
                    spec=spec,
                    entry=entry,
                    done_before=done_before,
                    limit=limit,
                    verification='result_not_arrived',
                    detail='the question page did not reach the result page',
                    rounds=rounds,
                    question=question,
                    options=options,
                    chosen=chosen,
                    correct=correct,
                    payout=payout,
                    taps=taps,
                    stop=stop,
                    dismissals=dismissals,
                    route=route,
                    templates=templates,
                    return_timeout=return_timeout,
                    return_interval=return_interval,
                    return_attempts=return_attempts,
                    settle=settle,
                    clock=clock,
                    sleep=sleep,
                    logger=log,
                )
            payout = read_payout(current)
            correct = _correct_answer(current)
            go_back = find_go_back(current)
            if go_back is None:
                return _finish_failed(
                    controller=controller,
                    capture=capture,
                    corpus=corpus,
                    navigate=navigate,
                    spec=spec,
                    entry=entry,
                    done_before=done_before,
                    limit=limit,
                    verification='go_back_not_found',
                    detail='the result page showed no Go back control',
                    rounds=rounds,
                    question=question,
                    options=options,
                    chosen=chosen,
                    correct=correct,
                    payout=payout,
                    taps=taps,
                    stop=stop,
                    dismissals=dismissals,
                    route=route,
                    templates=templates,
                    return_timeout=return_timeout,
                    return_interval=return_interval,
                    return_attempts=return_attempts,
                    settle=settle,
                    clock=clock,
                    sleep=sleep,
                    logger=log,
                )
            taps.append(
                controller.tap_box(
                    go_back,
                    why=f'quiz {spec.key}: leave the result page',
                    label=go_back.text,
                ),
            )
            current, arrived, dismiss, stop = _wait_for_surface(
                controller,
                capture,
                corpus,
                _is_round_done,
                timeout=exit_timeout,
                interval=interval,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=log,
                templates=templates,
            )
            if dismiss is not None:
                dismissals.append(dismiss)
            if stop is not None:
                # The sweep re-captures at exception time; the screen can turn
                # into the redeem interstitial between the target check and the
                # sweep's frame. Keep that stop so an answered quiz can still
                # take the interstitial completion path.
                break
            if not arrived:
                return _finish_failed(
                    controller=controller,
                    capture=capture,
                    corpus=corpus,
                    navigate=navigate,
                    spec=spec,
                    entry=entry,
                    done_before=done_before,
                    limit=limit,
                    verification='exit_not_reached',
                    detail='the quiz stayed on the result page past the exit timeout',
                    rounds=rounds,
                    question=question,
                    options=options,
                    chosen=chosen,
                    correct=correct,
                    payout=payout,
                    taps=taps,
                    stop=stop,
                    dismissals=dismissals,
                    route=route,
                    templates=templates,
                    return_timeout=return_timeout,
                    return_interval=return_interval,
                    return_attempts=return_attempts,
                    settle=settle,
                    clock=clock,
                    sleep=sleep,
                    logger=log,
                )
            if _is_redeem(current):
                stop = REDEEM_INTERSTITIAL
                break
            if _on_coin_page(current):
                break
            if _is_question_page(current) and rounds < MAX_ROUNDS:
                continue
            # The exit was reached but not by any of the three known exits
            # (a loading page, a wrong surface): stop the round loop and let
            # the return/verification decide.
            break

        return_state = return_to_coins_page(
            controller,
            capture,
            corpus,
            navigate,
            snapshot=current if stop is not None else None,
            templates=templates,
            route=route,
            timeout=return_timeout,
            interval=return_interval,
            attempts=return_attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=log,
        )
        if not return_state.returned:
            return _result(
                spec,
                entry.title,
                QuizOutcome.FAILED,
                verification='return_failed',
                detail='the shortcut route did not prove the coins page; the run is not reported complete',
                limit=limit,
                done_before=done_before,
                done_after=state.count(spec.key),
                rounds=rounds,
                question=question,
                options=options,
                chosen=chosen,
                correct=correct,
                payout=payout,
                taps=taps,
                stop=stop,
                dismissals=dismissals,
                return_state=return_state,
                logger=log,
            )

        if stop == REDEEM_INTERSTITIAL:
            if rounds == 0:
                # A late or leftover interstitial is not proof this visit paid.
                return _result(
                    spec,
                    entry.title,
                    QuizOutcome.FAILED,
                    verification='redeem_interstitial',
                    detail='the reward interstitial appeared before any answer; no completion was recorded',
                    limit=limit,
                    done_before=done_before,
                    done_after=done_before,
                    rounds=rounds,
                    question=question,
                    options=options,
                    chosen=chosen,
                    correct=correct,
                    payout=payout,
                    taps=taps,
                    stop=stop,
                    dismissals=dismissals,
                    return_state=return_state,
                    logger=log,
                )
            # The quiz paid coin credit: the run is complete even though the
            # overlay cannot be closed. The coins page is what the route proves.
            state.record(spec.key)
            return _result(
                spec,
                entry.title,
                QuizOutcome.COMPLETED,
                verification='interstitial',
                detail='quiz finished; the reward interstitial followed and the coins page is proven',
                limit=limit,
                done_before=done_before,
                done_after=state.count(spec.key),
                rounds=rounds,
                question=question,
                options=options,
                chosen=chosen,
                correct=correct,
                payout=payout,
                taps=taps,
                stop=stop,
                dismissals=dismissals,
                return_state=return_state,
                recorded=True,
                logger=log,
            )

        verdict, verify_dismiss, verify_return = _verify_task_row(
            controller=controller,
            capture=capture,
            corpus=corpus,
            navigate=navigate,
            title=entry.title,
            frame=return_state.snapshot if return_state.snapshot is not None else current,
            templates=templates,
            route=route,
            load_timeout=load_timeout,
            interval=interval,
            settle=settle,
            timeout=return_timeout,
            return_interval=return_interval,
            return_attempts=return_attempts,
            clock=clock,
            sleep=sleep,
            logger=log,
        )
        if verify_dismiss is not None:
            dismissals.append(verify_dismiss)
        if verify_return is not None:
            return_state = return_state.merged(verify_return)
            if not return_state.returned:
                return _result(
                    spec,
                    entry.title,
                    QuizOutcome.FAILED,
                    verification='return_failed',
                    detail='the verification surface was read but the run could not return to the coins page',
                    limit=limit,
                    done_before=done_before,
                    done_after=state.count(spec.key),
                    rounds=rounds,
                    question=question,
                    options=options,
                    chosen=chosen,
                    correct=correct,
                    payout=payout,
                    taps=taps,
                    stop=stop,
                    dismissals=dismissals,
                    return_state=return_state,
                    logger=log,
                )

        # A decisive row verdict is the strongest signal. The result page's own
        # payout may stand in only when the row could not be read at all: a row
        # that was read and still offers GO contradicts the payout, and the
        # conventions say a false completed is worse than unverified.
        payout_fallback = payout is not None and verdict.status == 'no_observation'
        if verdict.decisive or payout_fallback:
            verification = verdict.status if verdict.decisive else 'result_reward'
            detail = (
                verdict.detail
                if verdict.decisive
                else f'the result paid {payout}; the roster row could not be read for a second signal'
            )
            state.record(spec.key)
            return _result(
                spec,
                entry.title,
                QuizOutcome.COMPLETED,
                verification=verification,
                detail=detail,
                limit=limit,
                done_before=done_before,
                done_after=state.count(spec.key),
                rounds=rounds,
                question=question,
                options=options,
                chosen=chosen,
                correct=correct,
                payout=payout,
                taps=taps,
                stop=stop,
                dismissals=dismissals,
                return_state=return_state,
                recorded=True,
                logger=log,
            )

        return _result(
            spec,
            entry.title,
            QuizOutcome.FAILED,
            verification=verdict.status,
            detail=verdict.detail,
            limit=limit,
            done_before=done_before,
            done_after=state.count(spec.key),
            rounds=rounds,
            question=question,
            options=options,
            chosen=chosen,
            correct=correct,
            payout=payout,
            taps=taps,
            stop=stop,
            dismissals=dismissals,
            return_state=return_state,
            logger=log,
        )
    except RunAborted, RunInterrupted:
        raise
    except Exception:
        # A popup the corpus cannot close, or a navigation that raised, must not
        # leave the app on a quiz page. The original exception still wins.
        _return_quietly(
            controller,
            capture,
            corpus,
            navigate,
            templates=templates,
            route=route,
            timeout=return_timeout,
            interval=return_interval,
            attempts=return_attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=log,
        )
        raise


# The issue's public name; ``run_quiz_task`` says what it does at the dispatch point.
run_quiz = run_quiz_task
MAX_QUESTION_ROUNDS = MAX_ROUNDS


def _allowed_spec(entry: RosterEntry) -> AllowedTaskSpec:
    """Return the entry's allowed spec, refusing anything the dispatch point missed."""
    spec = entry.spec
    if not entry.allowed or spec is None or entry.dispatch_target is None:
        reason = entry.reason or 'no allowed action is exposed'
        raise TaskError(f'{entry.title!r} is not dispatchable: {reason}')
    if spec.category is not TaskCategory.QUIZ:
        raise TaskError(f'{entry.title!r} is {spec.category.value!r}, not a quiz task')
    authorization_of(entry)
    return spec


def _skipped(
    spec: AllowedTaskSpec,
    title: str,
    done: int,
    limit: int,
    *,
    verification: str,
    detail: str,
) -> QuizResult:
    return QuizResult(
        key=spec.key,
        title=title,
        outcome=QuizOutcome.SKIPPED,
        verification=verification,
        detail=detail,
        limit=limit,
        done_today_before=done,
        done_today_after=done,
    )


def _result(
    spec: AllowedTaskSpec,
    title: str,
    outcome: QuizOutcome,
    *,
    verification: str,
    detail: str,
    limit: int,
    done_before: int,
    done_after: int,
    rounds: int,
    question: str | None,
    options: Sequence[AnswerOption],
    chosen: str | None,
    correct: str | None,
    payout: str | None,
    taps: Sequence[TapRecord],
    stop: str | None,
    dismissals: Sequence[DismissReport],
    return_state: ReturnResult | None = None,
    recorded: bool = False,
    logger: logging.Logger,
) -> QuizResult:
    result = QuizResult(
        key=spec.key,
        title=title,
        outcome=outcome,
        verification=verification,
        detail=detail,
        limit=limit,
        done_today_before=done_before,
        done_today_after=done_after,
        rounds=rounds,
        question=question,
        options=tuple(options),
        chosen=chosen,
        correct=correct,
        payout=payout,
        taps=tuple(taps),
        stop=stop,
        recorded=recorded,
        return_state=return_state,
        dismissals=tuple(dismissals),
    )
    _log_result(logger, result)
    return result


def _finish_failed(
    *,
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    navigate: RouteFirer,
    spec: AllowedTaskSpec,
    entry: RosterEntry,
    done_before: int,
    limit: int,
    verification: str,
    detail: str,
    route: SurfaceRoute,
    templates: TemplateStore | None,
    return_timeout: float,
    return_interval: float,
    return_attempts: int,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
    rounds: int = 0,
    question: str | None = None,
    options: Sequence[AnswerOption] = (),
    chosen: str | None = None,
    correct: str | None = None,
    payout: str | None = None,
    taps: Sequence[TapRecord] = (),
    stop: str | None = None,
    dismissals: Sequence[DismissReport] = (),
) -> QuizResult:
    """End a failure on the coins page and build its result."""
    return_state = return_to_coins_page(
        controller,
        capture,
        corpus,
        navigate,
        snapshot=None,
        templates=templates,
        route=route,
        timeout=return_timeout,
        interval=return_interval,
        attempts=return_attempts,
        settle=settle,
        clock=clock,
        sleep=sleep,
        logger=logger,
    )
    if not return_state.returned:
        verification = 'return_failed'
        detail = f'{detail}; the shortcut route did not prove the coins page'
    result = QuizResult(
        key=spec.key,
        title=entry.title,
        outcome=QuizOutcome.FAILED,
        verification=verification,
        detail=detail,
        limit=limit,
        done_today_before=done_before,
        done_today_after=done_before,
        rounds=rounds,
        question=question,
        options=tuple(options),
        chosen=chosen,
        correct=correct,
        payout=payout,
        taps=tuple(taps),
        stop=stop,
        return_state=return_state,
        dismissals=tuple(dismissals),
    )
    _log_result(logger, result)
    return result


def _settle_surface(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    frame: Snapshot,
    *,
    templates: TemplateStore | None,
    timeout: float,
    interval: float,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
) -> tuple[Snapshot, bool, DismissReport | None]:
    """Resolve loading and popups before a row is read or pressed."""
    deadline = clock() + timeout
    dismiss: DismissReport | None = None
    while True:
        state = classify(frame).state
        if state is ScreenState.LOADING:
            now = clock()
            if now >= deadline:
                return frame, False, dismiss
            sleep(min(interval, max(0.0, deadline - now)))
            frame = _capture(capture)
            continue
        if state is ScreenState.POPUP:
            now = clock()
            if now >= deadline:
                return frame, False, dismiss
            report, stop = _sweep(
                controller,
                capture,
                corpus,
                templates=templates,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=logger,
            )
            dismiss = report
            if stop is not None:
                return frame, False, dismiss
            frame = report.frames[-1].snapshot if report is not None and report.frames else _capture(capture)
            continue
        return frame, True, dismiss


def _wait_for_surface(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    target: Callable[[Snapshot], bool],
    *,
    timeout: float,
    interval: float,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
    templates: TemplateStore | None,
) -> tuple[Snapshot, bool, DismissReport | None, str | None]:
    """Poll until the frame reaches ``target`` or the budget runs out.

    A popup seen during the wait is swept; the redeem interstitial is surfaced
    as a stop rather than a failure, because the quiz paid credit when it appeared.
    """
    started = clock()
    deadline = started + timeout
    current = _capture(capture)
    dismiss: DismissReport | None = None
    while True:
        classification = classify(current)
        if target(current):
            return current, True, dismiss, None
        if classification.state is ScreenState.POPUP:
            report, stop = _sweep(
                controller,
                capture,
                corpus,
                templates=templates,
                settle=settle,
                clock=clock,
                sleep=sleep,
                logger=logger,
            )
            dismiss = report
            if stop is not None:
                return current, False, dismiss, stop
            current = report.frames[-1].snapshot if report is not None and report.frames else _capture(capture)
            continue
        if classification.state is ScreenState.LOADING:
            now = clock()
            if now >= deadline:
                return current, False, dismiss, None
            sleep(min(interval, max(0.0, deadline - now)))
            current = _capture(capture)
            continue
        now = clock()
        if now >= deadline:
            return current, False, dismiss, None
        sleep(min(interval, max(0.0, deadline - now)))
        current = _capture(capture)


def _verify_task_row(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    navigate: RouteFirer,
    title: str,
    frame: Snapshot,
    *,
    templates: TemplateStore | None,
    route: SurfaceRoute,
    load_timeout: float,
    interval: float,
    settle: float,
    timeout: float,
    return_interval: float,
    return_attempts: int,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
) -> tuple[_Verdict, DismissReport | None, ReturnResult | None]:
    """Read the row on the returned surface, then re-open the list if needed.

    The inline coin page is a partial subset — rows come and go — so only the
    populated boost list may prove a missing row. A row that cannot be read
    means no observation, never a re-guess.
    """
    del load_timeout  # the inline read is bounded by the caller's settle
    first = _row_verdict(frame, title, trusted=False)
    if first.decisive:
        return first, None, None

    try:
        opened = open_boost_list(
            controller,
            capture,
            corpus,
            templates=templates,
            timeout=timeout,
            interval=interval,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=logger,
        )
    except StateTimeout, PopupUnclosable:
        final = return_to_coins_page(
            controller,
            capture,
            corpus,
            navigate,
            snapshot=None,
            templates=templates,
            route=route,
            timeout=timeout,
            interval=return_interval,
            attempts=return_attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=logger,
        )
        return (
            _Verdict(
                status='no_observation',
                decisive=False,
                detail='the boost list could not be read for verification',
            ),
            None,
            final,
        )

    trusted = _trusted_surface(opened)
    verdict = _row_verdict(opened.snapshot, title, trusted=trusted)

    if _on_coin_page(opened.snapshot):
        # The inline route was already on the coins page; no second entry.
        return verdict, opened.dismiss, None

    final = return_to_coins_page(
        controller,
        capture,
        corpus,
        navigate,
        snapshot=opened.snapshot,
        templates=templates,
        route=route,
        timeout=timeout,
        interval=return_interval,
        attempts=return_attempts,
        settle=settle,
        clock=clock,
        sleep=sleep,
        logger=logger,
    )
    return verdict, opened.dismiss, final


def _row_verdict(
    snapshot: Snapshot,
    title: str,
    *,
    trusted: bool,
) -> _Verdict:
    """Turn one frame into a completion verdict, strongest signal first."""
    row, action, _badge = find_task_row(snapshot, title)

    if row is None:
        if trusted and is_boost_page(snapshot) and has_task_rows(snapshot):
            return _Verdict(
                status='row_gone',
                decisive=True,
                detail=f'{title!r} is gone from the populated boost list',
            )
        return _Verdict(
            status='no_observation',
            decisive=False,
            detail=f'{title!r} was not found and the surface cannot prove it is gone',
        )
    if _row_has_done_label(row):
        return _Verdict(
            status='action_completed',
            decisive=True,
            detail=f'{title!r} now reads as completed',
        )
    if action is None and trusted:
        return _Verdict(
            status='action_gone',
            decisive=True,
            detail=f'{title!r} no longer offers an action',
        )
    return _Verdict(
        status='no_change',
        decisive=False,
        detail=f'{title!r} is still present with the same action',
    )


def _trusted_surface(opened: BoostList) -> bool:
    """Whether the opened surface is a task list whose missing row means something."""
    if opened.empty or is_empty_boost(opened.snapshot):
        return False
    if opened.route is DiscoveryRoute.BOOST:
        return is_boost_page(opened.snapshot) and has_task_rows(opened.snapshot)
    return _is_coin_page(opened.snapshot) and has_task_rows(opened.snapshot)


def _is_task_surface(snapshot: Snapshot) -> bool:
    """Whether a frame is the task list the roster was read from."""
    return is_boost_page(snapshot) or _is_coin_page(snapshot)


def _is_coin_page(snapshot: Snapshot) -> bool:
    return all(contains_text(snapshot, marker) for marker in COIN_PAGE_MARKERS)


def _on_coin_page(snapshot: Snapshot) -> bool:
    """The coin page itself, not the boost sheet drawn over it."""
    return _is_coin_page(snapshot) and not is_boost_page(snapshot)


def _is_question_page(snapshot: Snapshot) -> bool:
    return (
        _question_box(snapshot) is not None
        and contains_text(snapshot, 'Coins quiz')
        and contains_text(snapshot, 'Answer quiz questions for')
        and contains_text(snapshot, 'more coins')
    )


def _is_result_page(snapshot: Snapshot) -> bool:
    return contains_text(snapshot, 'Correct answer:') and contains_text(snapshot, 'Your answer:')


def _is_redeem(snapshot: Snapshot) -> bool:
    """Whether the unclosable reward interstitial is the top surface."""
    return contains_text(snapshot, REDEEM_MARKER)


def _is_round_done(snapshot: Snapshot) -> bool:
    return _is_redeem(snapshot) or _on_coin_page(snapshot) or _is_question_page(snapshot)


def _question_box(snapshot: Snapshot) -> TextBox | None:
    """The box that carries the question prefix (``Q :``), or ``None``."""
    for box in snapshot.boxes:
        if box.score < DEFAULT_MIN_OCR_SCORE:
            continue
        if QUESTION_PREFIX.match(box.text):
            return box
    return None


def _line_cluster(snapshot: Snapshot, question: TextBox, median_height: float) -> tuple[TextBox, ...]:
    """The question's own lines, as a cluster of boxes.

    Lines stacked within the gap rule of one another are one question; the
    vertical gap that separates options is deliberately kept above this rule
    so the two never merge.
    """
    cluster = [question]
    current_bottom = question.bbox[3]
    for box in sorted(snapshot.boxes, key=lambda item: (item.bbox[1], item.bbox[0])):
        if box is question:
            continue
        top = box.bbox[1]
        if top <= question.bbox[1]:
            continue
        if top - current_bottom >= median_height * OPTION_GAP_RATIO:
            break
        cluster.append(box)
        current_bottom = max(box.bbox[3], current_bottom)
    return tuple(cluster)


def _question_text(snapshot: Snapshot) -> str | None:
    """The full question text, continuation lines included."""
    question = _question_box(snapshot)
    if question is None:
        return None
    heights = [box.height for box in snapshot.boxes if box.height > 0]
    median_height = statistics.median(heights) if heights else 0.0
    cluster = _line_cluster(snapshot, question, median_height)
    return ' '.join(box.text for box in cluster)


def _correct_answer(snapshot: Snapshot) -> str | None:
    """Read the learned answer off the result page (``Correct answer: X``)."""
    for box in snapshot.boxes:
        if box.score < DEFAULT_MIN_OCR_SCORE:
            continue
        if _CORRECT_ANSWER.search(box.text):
            value = re.sub(r'^.*correct\s*answer\s*[:.]?\s*', '', box.text, count=1, flags=re.IGNORECASE)
            value = value.strip()
            if value:
                return value
    return None


def _sweep(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    *,
    templates: TemplateStore | None,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
) -> tuple[DismissReport | None, str | None]:
    """Run the one dismissal loop, treating the redeem interstitial as a stop."""
    try:
        report = dismiss_after(
            controller,
            capture,
            _without_boost_family(corpus),
            templates=templates,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=logger,
        )
    except PopupUnclosable:
        frame = _capture(capture)
        if contains_text(frame, REDEEM_MARKER):
            return None, REDEEM_INTERSTITIAL
        raise
    return report, None


def _without_boost_family(corpus: PopupCorpus) -> PopupCorpus:
    """Remove the family whose anchor is the task list itself.

    The quiz executor may sweep while still on the task surface. That page
    carries the ``Coins boost`` anchor, so a full-corpus sweep would try to
    close the very list the run is working from.
    """
    families = tuple(family for family in corpus.families if family.family != 'coins_boost_sheet')
    if len(families) == len(corpus.families):
        return corpus
    return replace(corpus, families=families)


def _row_has_done_label(row: Row) -> bool:
    """Whether any box in the row reads as a completion label."""
    return any(similarity(label, box.text) >= DEFAULT_MIN_SCORE for label in DONE_LABELS for box in row.boxes)


def _capture(source: CaptureSource) -> Snapshot:
    if callable(source):
        return source()
    return source.capture()


def _return_quietly(
    controller: Controller,
    capture: CaptureSource,
    corpus: PopupCorpus,
    navigate: RouteFirer,
    *,
    templates: TemplateStore | None,
    route: SurfaceRoute,
    timeout: float,
    interval: float,
    attempts: int,
    settle: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    logger: logging.Logger,
) -> ReturnResult | None:
    """Best-effort return used while another exception is in flight."""
    try:
        return return_to_coins_page(
            controller,
            capture,
            corpus,
            navigate,
            templates=templates,
            route=route,
            timeout=timeout,
            interval=interval,
            attempts=attempts,
            settle=settle,
            clock=clock,
            sleep=sleep,
            logger=logger,
        )
    except RunAborted, RunInterrupted:
        raise
    except Exception as exc:
        _warn(logger, 'return_failed', detail=f'{type(exc).__name__}: {exc}')
        return None


def _warn(logger: logging.Logger, event: str, **fields: object) -> None:
    logger.warning(
        '%s',
        json.dumps({'event': event, **fields}, separators=(',', ':'), sort_keys=True),
    )


def _log_result(logger: logging.Logger, result: QuizResult) -> None:
    logger.info(
        '%s',
        json.dumps({'event': 'quiz_task', **result.as_dict()}, separators=(',', ':'), sort_keys=True),
    )


# Public names for the spec's state matchers, so callers and tests can use the
# vocabulary of `specs/flows.yaml` instead of the internal helpers.
is_question_state = _is_question_page
is_result_state = _is_result_page
question_of = _question_text
