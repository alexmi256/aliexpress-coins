"""The minimal run report: atomic files, a stable JSON shape and a readable table."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aliexpress_coins.runner.errors import ReportError
from aliexpress_coins.runner.report import RunReport, RunStatus

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

    assert payload['schema'] == 1
    assert payload['run_id'] == '20261122T033000Z'
    assert payload['status'] == 'budget'
    assert payload['budget'] == {'seconds': 2700.0, 'used_seconds': 60.0}
    assert payload['tasks'][0]['result'] == {'payout': '+6'}
    assert len(payload['skipped']) == 2
    assert payload['roster']['count'] == 4
    assert 'Take the coins quiz' in summary
    assert 'payout \\| read once' in summary
    assert 'run budget' in summary
    assert '**Route fallback:** route fallback was used' in summary
    assert '1200x2000 @ 240 dpi' in summary
    assert '1240 -> 1246' in summary
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
