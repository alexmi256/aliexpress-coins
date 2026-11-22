"""The minimal run report: atomic files, a stable JSON shape and a readable table."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aliexpress_coins.runner.errors import ReportError
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
    report.hygiene = {'serial': '192.168.8.65:5555', 'state': 'device'}
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

    assert payload['schema'] == 2
    assert payload['run_id'] == '20261122T033000Z'
    assert payload['status'] == 'budget'
    assert payload['budget'] == {'seconds': 2700.0, 'used_seconds': 60.0}
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
    assert '1200x2000 @ 240 dpi' in summary
    assert '**Balance:** 1240 -> 1246 (+6)' in summary
    assert '**Popups closed:** bundle_badge: 1, launch_modal: 2' in summary
    assert '**Notification:** failed via webhook: HTTPError: 500' in summary
    assert report.ok is True


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
