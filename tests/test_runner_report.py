"""The minimal run report: atomic files, a stable JSON shape and a readable table."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aliexpress_coins.runner.errors import ReportError
from aliexpress_coins.runner.failures import FailureCode, failure
from aliexpress_coins.runner.report import RunEventLog, RunReport, RunStatus, prune_runs

STARTED = datetime(2026, 11, 22, 3, 30, tzinfo=UTC)
FINISHED = datetime(2026, 11, 22, 3, 42, tzinfo=UTC)


def _report(tmp_path: Path) -> RunReport:
    return RunReport(tmp_path, '20261122T033000Z', budget_seconds=2700.0, started_at=STARTED)


def test_for_run_uses_the_utc_clock_for_the_directory_name(tmp_path: Path) -> None:
    report = RunReport.for_run(tmp_path, budget_seconds=60.0, clock=lambda: STARTED)
    assert report.run_id == '20261122T033000Z'
    assert report.run_dir == tmp_path / '20261122T033000Z'
    assert report.summary_path.name == 'summary.md'
    assert report.json_path.name == 'run.json'
    assert report.roster_log_path.name == 'roster.jsonl'


def test_a_full_report_round_trips_through_json_and_markdown(tmp_path: Path) -> None:
    report = _report(tmp_path)
    report.hygiene = {'serial': '192.168.8.65:5555', 'state': 'device', 'was_asleep': True, 'kept_awake': True}
    report.geometry = {'width': 1200, 'height': 2000, 'density': 240, 'sdk': 35}
    report.surface = 'boost_page'
    report.roster = {
        'status': 'available',
        'count': 4,
        'allowed_count': 3,
        'denied_count': 1,
    }
    report.observations = 3
    report.balance_before = 1240.0
    report.balance_after = 1246.0
    report.returned_to_coins_page = True
    report.route_error = 'route fallback was used'
    report.record_popups(['launch_modal', 'launch_modal', 'bundle_badge'])
    report.record_shot('balance-before', report.run_dir / 'shots' / 'balance-before.png')
    report.notification = {'attempted': True, 'ok': False, 'sink': 'webhook', 'error': 'HTTPError: 500'}
    report.record_task(
        title='Take the coins quiz',
        key='coins_quiz',
        category='quiz',
        outcome='completed',
        verification='result_reward',
        detail='payout | read\nonce',
        duration_seconds=8.25,
        completed=True,
        result={'payout': '+6'},
    )
    report.skip(
        'Place an order on this page',
        key=None,
        category=None,
        reason='policy',
        detail='spending: placing an order is refused',
    )
    report.skip(
        'Place an order on this page',
        key=None,
        category=None,
        reason='policy',
        detail='duplicate is collapsed',
    )
    report.skip(
        'Explore sponsored items',
        key='sponsored_items',
        category='browse_wait',
        reason='run budget',
        detail='wall-clock budget exhausted',
    )
    report.finish(
        RunStatus.BUDGET, detail='wall-clock budget of 60s exhausted', budget_used_seconds=60.0, finished_at=FINISHED
    )
    report.write()

    payload = json.loads(report.json_path.read_text(encoding='utf-8'))
    summary = report.summary_path.read_text(encoding='utf-8')

    assert payload['schema'] == 4
    assert payload['run_id'] == '20261122T033000Z'
    assert payload['status'] == 'budget'
    assert payload['budget'] == {'seconds': 2700.0, 'used_seconds': 60.0}
    assert payload['tasks'][0]['state'] == 'completed'
    assert payload['tasks'][0]['result'] == {'payout': '+6'}
    assert len(payload['skipped']) == 2
    assert payload['roster']['count'] == 4
    assert payload['balance_delta'] == 6.0
    assert payload['popups_closed'] == {'launch_modal': 2, 'bundle_badge': 1}
    assert payload['shots'] == {'balance-before': 'shots/balance-before.png'}
    assert payload['notification']['ok'] is False
    assert 'Take the coins quiz' in summary
    assert 'payout \\| read once' in summary
    assert 'run budget' in summary
    assert '**Route fallback:** route fallback was used' in summary
    assert '**Device:** 192.168.8.65:5555 (device; was asleep; kept awake)' in summary
    assert '**Geometry:** 1200x2000 @ 240 dpi' in summary
    assert '**Balance:** 1240 -> 1246 (+6)' in summary
    assert '**Popups closed:** bundle_badge: 1, launch_modal: 2' in summary
    assert '**Notification:** failed via webhook: HTTPError: 500' in summary
    assert report.ok is True


def test_a_dry_run_plan_and_an_abort_are_visible_in_the_summary(tmp_path: Path) -> None:
    report = _report(tmp_path)
    report.plan = {
        'dry_run': True,
        'surface': 'rows_present',
        'steps': [{'order': 1, 'label': 'Take the coins quiz', 'key': 'coins_quiz'}],
        'refusals': [{'label': 'Play & Earn', 'code': 'game_play_and_earn'}],
        'taps_issued': 0,
        'detail': 'dry run: 1 task(s) planned, 1 refusal(s); no tap was issued',
    }
    report.plan_note = 'read from the visible surface'
    report.abort = {
        'code': 'dangerous_control',
        'reason': "a 'Buy now' control appeared",
        'screen': {'state': 'unknown', 'surface': 'unknown', 'labels': ['Buy now', 'Checkout']},
    }
    report.finish(RunStatus.ABORTED, detail='aborted', budget_used_seconds=1.0, finished_at=FINISHED)
    report.write()

    summary = report.summary_path.read_text(encoding='utf-8')
    payload = json.loads(report.json_path.read_text(encoding='utf-8'))
    assert payload['plan']['taps_issued'] == 0
    assert payload['plan_note'] == 'read from the visible surface'
    assert payload['abort']['code'] == 'dangerous_control'
    assert '**Plan (dry run):** dry run: 1 task(s) planned' in summary
    assert '**Plan note:** read from the visible surface' in summary
    assert "**Abort:** dangerous_control — a 'Buy now' control appeared" in summary
    assert report.ok is False


def test_a_task_aborted_in_flight_keeps_its_row_and_the_screen(tmp_path: Path) -> None:
    report = _report(tmp_path)
    index = report.begin_task(title='Take the coins quiz', key='coins_quiz', category='quiz')
    assert report.tasks[index].state == 'in_flight'

    screen = {
        'state': 'page',
        'surface': 'unknown',
        'labels': ['Coins quiz', 'Can be applied directly at Checkout'],
        'fingerprint': 'page/unknown/quiz',
    }
    report.abort = {
        'code': 'dangerous_control',
        'reason': "a 'Can be applied directly at Checkout' control appeared",
        'screen': screen,
    }
    report.abort_in_flight(
        code='dangerous_control',
        reason=report.abort['reason'],
        failure=failure(FailureCode.DANGEROUS_CONTROL, detail=report.abort['reason']),
        screen=screen,
        duration_seconds=1.5,
    )
    report.finish(RunStatus.ABORTED, detail='aborted', budget_used_seconds=1.5, finished_at=FINISHED)
    report.write()

    payload = json.loads(report.json_path.read_text(encoding='utf-8'))
    summary = report.summary_path.read_text(encoding='utf-8')
    task = payload['tasks'][0]
    assert task['title'] == 'Take the coins quiz'
    assert task['state'] == 'aborted'
    assert task['outcome'] == 'aborted'
    assert task['code'] == 'dangerous_control'
    assert task['result']['abort']['screen']['labels'][1] == 'Can be applied directly at Checkout'
    assert 'No task was dispatched.' not in summary
    assert '| Take the coins quiz | coins_quiz | dangerous_control | aborted |' in summary
    assert 'Can be applied directly at Checkout' in summary


def test_finishing_a_task_keeps_its_precise_outcome_and_abort_leaves_terminal_rows_alone(
    tmp_path: Path,
) -> None:
    report = _report(tmp_path)
    assert report.abort_in_flight(code='dangerous_control', reason='nothing was running') is False

    index = report.begin_task(title='Turn on notifications', key='notifications', category='notification')
    terminal = report.finish_task(
        index,
        outcome='already_done',
        verification='already_done',
        detail='the notification row was already claimed today',
        duration_seconds=2.0,
        completed=False,
        result={'key': 'notifications'},
    )

    assert terminal.state == 'already_done'
    assert terminal.outcome == 'already_done'
    assert report.abort_in_flight(code='dangerous_control', reason='too late') is False
    assert report.tasks == [terminal]


def test_a_routing_abort_still_writes_an_empty_tasks_table(tmp_path: Path) -> None:
    report = _report(tmp_path)
    report.abort = {
        'code': 'unexpected_screen',
        'reason': 'the same unexpected screen was seen 3 times in a row',
        'screen': {'state': 'unknown', 'surface': 'unknown', 'labels': ['For you']},
    }
    report.finish(RunStatus.ABORTED, detail='aborted while routing', budget_used_seconds=1.0, finished_at=FINISHED)
    report.write()

    payload = json.loads(report.json_path.read_text(encoding='utf-8'))
    summary = report.summary_path.read_text(encoding='utf-8')
    assert payload['tasks'] == []
    assert 'No task was dispatched.' in summary


def test_a_completed_run_with_no_rows_still_writes_a_report(tmp_path: Path) -> None:
    report = _report(tmp_path)
    report.finish(RunStatus.COMPLETED, budget_used_seconds=1.0, finished_at=FINISHED)
    report.write()

    summary = report.summary_path.read_text(encoding='utf-8')
    payload = json.loads(report.json_path.read_text(encoding='utf-8'))
    assert payload['status'] == 'completed'
    assert 'No task was dispatched.' in summary
    assert 'Nothing was skipped.' in summary
    assert '**Device:** not prepared' in summary
    assert '**Geometry:** not captured' in summary
    assert '**Roster:** not read' in summary
    assert '**Back on coins page:** not attempted' in summary
    assert report.ok is True


def test_a_failed_run_reports_the_error_and_is_not_ok(tmp_path: Path) -> None:
    report = _report(tmp_path)
    report.error = 'DeviceError: tablet absent'
    report.returned_to_coins_page = False
    report.return_error = 'route did not prove the page'
    report.finish(RunStatus.FAILED, detail='DeviceError: tablet absent', budget_used_seconds=0.2)
    report.write()

    summary = report.summary_path.read_text(encoding='utf-8')
    payload = json.loads(report.json_path.read_text(encoding='utf-8'))
    assert payload['error'] == 'DeviceError: tablet absent'
    assert '**Run error:** DeviceError: tablet absent' in summary
    assert '**Back on coins page:** no (route did not prove the page)' in summary
    assert report.ok is False


def test_running_and_stopped_statuses_are_not_ok(tmp_path: Path) -> None:
    report = _report(tmp_path)
    assert report.status is RunStatus.RUNNING
    assert report.ok is False
    report.finish(RunStatus.STOPPED, budget_used_seconds=1.0)
    assert report.ok is False
    assert report.finished_at is not None


@pytest.mark.parametrize('run_id', ['', '.', '..', 'a/b', '..\\escape'])
def test_run_ids_cannot_escape_the_runs_directory(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(ReportError, match=r'single path component|non-empty'):
        RunReport(tmp_path, run_id, budget_seconds=1.0, started_at=STARTED)


@pytest.mark.parametrize('budget', [0.0, -1.0])
def test_the_budget_must_be_positive(tmp_path: Path, budget: float) -> None:
    with pytest.raises(ValueError, match='must be positive'):
        RunReport(tmp_path, 'run', budget_seconds=budget, started_at=STARTED)


def test_a_write_failure_is_a_typed_report_error(tmp_path: Path) -> None:
    not_a_directory = tmp_path / 'file'
    not_a_directory.write_text('x', encoding='utf-8')
    report = RunReport(not_a_directory, 'run', budget_seconds=1.0, started_at=STARTED)
    with pytest.raises(ReportError, match='could not write'):
        report.write()


def test_the_event_log_keeps_structured_events_as_jsonl(tmp_path: Path) -> None:
    log = logging.getLogger('test.run.event-log')
    log.setLevel(logging.INFO)
    log.propagate = False
    handler = RunEventLog(tmp_path / 'run.jsonl')
    handler.record({'event': 'run_started', 'run_id': 'run-1'})
    log.addHandler(handler)
    try:
        log.info('%s', json.dumps({'event': 'run_hygiene', 'serial': 'tablet'}))
        log.warning('a plain sentence')
        log.warning('%s', json.dumps(['not', 'an', 'object']))
    finally:
        log.removeHandler(handler)
        handler.close()

    lines = [json.loads(line) for line in (tmp_path / 'run.jsonl').read_text(encoding='utf-8').splitlines()]
    assert lines[0] == {'event': 'run_started', 'run_id': 'run-1'}
    assert lines[1]['event'] == 'run_hygiene'
    assert lines[1]['serial'] == 'tablet'
    assert lines[1]['level'] == 'info'
    assert lines[2] == {
        'event': 'log',
        'level': 'warning',
        'logger': 'test.run.event-log',
        'message': 'a plain sentence',
        'time': lines[2]['time'],
    }
    assert lines[3]['event'] == 'log'
    assert lines[3]['value'] == ['not', 'an', 'object']


def test_an_event_log_write_failure_is_handled_and_does_not_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / 'a-file'
    parent.write_text('x', encoding='utf-8')
    handler = RunEventLog(parent / 'run.jsonl')
    handled: list[logging.LogRecord] = []
    monkeypatch.setattr(handler, 'handleError', handled.append)
    record = logging.LogRecord('test', logging.INFO, __file__, 1, 'message', (), None)

    handler.emit(record)

    assert len(handled) == 1
    handler.close()


def test_report_helpers_keep_outside_paths_artifact_errors_and_off_notifications(
    tmp_path: Path,
) -> None:
    report = _report(tmp_path)
    outside = tmp_path.parent / 'outside.png'
    report.record_shot('outside', outside)
    report.record_artifact_error('balance-after: OSError: disk full')
    report.notification = {'attempted': False, 'ok': False, 'reason': 'not configured'}
    report.finish(RunStatus.COMPLETED, budget_used_seconds=1.0, finished_at=FINISHED)
    report.write()

    summary = report.summary_path.read_text(encoding='utf-8')
    payload = json.loads(report.json_path.read_text(encoding='utf-8'))
    assert report.shots_dir == report.run_dir / 'shots'
    assert report.balance_delta is None
    assert payload['shots'] == {'outside': str(outside)}
    assert '**Artifact errors:** balance-after: OSError: disk full' in summary
    assert '**Notification:** not attempted (not configured)' in summary


def test_retention_keeps_the_newest_n_and_never_prunes_the_current_run(tmp_path: Path) -> None:
    for name in ('20260101T000000Z', '20260102T000000Z', '20260103T000000Z', '20260104T000000Z'):
        (tmp_path / name).mkdir()
        (tmp_path / name / 'run.json').write_text('{}', encoding='utf-8')

    pruned = prune_runs(tmp_path, keep=3, current_run_id='20260102T000000Z')

    assert [path.name for path in pruned] == ['20260101T000000Z']
    kept = sorted(path.name for path in tmp_path.iterdir() if path.is_dir())
    assert kept == ['20260102T000000Z', '20260103T000000Z', '20260104T000000Z']


def test_retention_keeps_a_current_run_that_sorts_oldest(tmp_path: Path) -> None:
    for name in ('20260101T000000Z', '20260102T000000Z', '20260103T000000Z'):
        (tmp_path / name).mkdir()

    pruned = prune_runs(tmp_path, keep=2, current_run_id='20260101T000000Z')

    assert [path.name for path in pruned] == ['20260102T000000Z']
    assert sorted(path.name for path in tmp_path.iterdir()) == ['20260101T000000Z', '20260103T000000Z']


def test_retention_does_not_touch_files_or_links(tmp_path: Path) -> None:
    (tmp_path / '20260101T000000Z').mkdir()
    (tmp_path / '20260102T000000Z').mkdir()
    (tmp_path / 'not-a-run.txt').write_text('keep me', encoding='utf-8')

    assert prune_runs(tmp_path, keep=1, current_run_id='20260102T000000Z') == (tmp_path / '20260101T000000Z',)
    assert (tmp_path / 'not-a-run.txt').is_file()


def test_retention_leaves_operator_directories_alone_and_logs_them(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    (tmp_path / '20260101T000000Z').mkdir()
    (tmp_path / '20260102T000000Z').mkdir()
    (tmp_path / 'manual').mkdir()
    (tmp_path / 'stray.txt').write_text('keep me', encoding='utf-8')

    with caplog.at_level(logging.INFO, logger='aliexpress_coins.runner.report'):
        pruned = prune_runs(tmp_path, keep=1, current_run_id='20260102T000000Z')

    assert pruned == (tmp_path / '20260101T000000Z',)
    assert (tmp_path / 'manual').is_dir()
    assert (tmp_path / 'stray.txt').is_file()
    assert 'runs_not_pruned' in caplog.text
    assert 'manual' in caplog.text
    assert 'stray.txt' in caplog.text


def test_retention_requires_a_positive_keep(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match='keep must be positive'):
        prune_runs(tmp_path, keep=0, current_run_id='run')


def test_retention_treats_a_missing_root_as_nothing_to_prune(tmp_path: Path) -> None:
    assert prune_runs(tmp_path / 'missing', keep=3, current_run_id='run') == ()


def test_retention_reports_an_unreadable_root(tmp_path: Path) -> None:
    file = tmp_path / 'runs'
    file.write_text('x', encoding='utf-8')
    with pytest.raises(ReportError, match='could not list run directories'):
        prune_runs(file, keep=3, current_run_id='run')


def test_retention_reports_a_directory_that_cannot_be_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / '20260101T000000Z').mkdir()
    (tmp_path / '20260102T000000Z').mkdir()

    def broken_rmtree(path: Path) -> None:
        raise OSError(f'cannot remove {path}')

    monkeypatch.setattr('aliexpress_coins.runner.report.shutil.rmtree', broken_rmtree)
    with pytest.raises(ReportError, match='could not prune every old run'):
        prune_runs(tmp_path, keep=1, current_run_id='20260102T000000Z')


def test_a_named_failure_and_a_task_code_reach_both_report_files(tmp_path: Path) -> None:
    report = _report(tmp_path)
    report.record_failure(
        failure(FailureCode.APP_LOGGED_OUT, detail='a sign-in prompt replaced the account surface'),
    )
    report.record_task(
        title='Take the coins quiz',
        key='coins_quiz',
        category='quiz',
        outcome='failed',
        verification='no_change',
        detail='the row did not move',
        duration_seconds=2.0,
        completed=False,
        result={},
        failure=failure(FailureCode.TASK_UNVERIFIED, detail='the row did not move'),
    )
    report.finish(RunStatus.FAILED, detail='stopped', budget_used_seconds=2.0, finished_at=FINISHED)
    report.write()

    payload = json.loads(report.json_path.read_text(encoding='utf-8'))
    summary = report.summary_path.read_text(encoding='utf-8')

    assert payload['failure']['code'] == 'app_logged_out'
    assert payload['failure']['stage'] == 'account'
    assert payload['failure']['abort_run'] is True
    assert payload['tasks'][0]['code'] == 'task_unverified'
    assert payload['tasks'][0]['failure']['retryable'] is True
    assert '**Failure:** app_logged_out — The AliExpress account is signed out' in summary
    assert '| Take the coins quiz | coins_quiz | task_unverified |' in summary
    assert report.failure_code == 'app_logged_out'


def test_a_completed_task_has_no_code_and_a_run_without_a_failure_has_none(tmp_path: Path) -> None:
    report = _report(tmp_path)
    record = report.record_task(
        title='Take the coins quiz',
        key='coins_quiz',
        category='quiz',
        outcome='completed',
        verification='result_reward',
        detail='paid',
        duration_seconds=1.0,
        completed=True,
        result={},
    )

    assert record.code is None
    assert report.failure_code is None
    report.write()
    payload = json.loads(report.json_path.read_text(encoding='utf-8'))
    assert payload['failure'] is None
    assert payload['tasks'][0]['code'] is None
