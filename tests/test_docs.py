"""The runbook and ADR index are part of the gate (issue #37).

A failure class without a troubleshooting row is a document that lies about what the
operator can do, and an ADR that is not in the index is a decision nobody can find.
Both are checked here rather than left to review.
"""

from __future__ import annotations

import re
from pathlib import Path

from aliexpress_coins.runner.failures import FAILURE_TABLE, FailureCode

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK_PATH = ROOT / 'docs' / '06-runbook.md'
ADR_DIR = ROOT / 'docs' / 'adr'
ADR_INDEX_PATH = ADR_DIR / 'README.md'

_FAILURE_CODES = frozenset(code.value for code in FailureCode)
_ROW = re.compile(r'^\|\s*`(?P<code>[a-z_]+)`\s*\|(?P<cells>.*)\|\s*$')
_LINK = re.compile(r'\]\((?P<target>[a-zA-Z0-9_-]+\.md)\)')


def _troubleshooting_rows() -> dict[str, tuple[str, ...]]:
    """Return the runbook's code -> row cells for every taxonomy row it contains."""
    rows: dict[str, tuple[str, ...]] = {}
    for line in RUNBOOK_PATH.read_text(encoding='utf-8').splitlines():
        match = _ROW.match(line)
        if match is None:
            continue
        code = match.group('code')
        if code not in _FAILURE_CODES:
            continue
        rows[code] = tuple(cell.strip() for cell in match.group('cells').split('|'))
    return rows


def _expected_retry(code: FailureCode) -> str:
    spec = FAILURE_TABLE[code]
    if spec.retries == 0:
        return 'none'
    return f'{spec.retry.value} {chr(215)}{spec.retries}'


def test_every_failure_code_has_a_troubleshooting_row() -> None:
    rows = _troubleshooting_rows()
    missing = sorted(code.value for code in FailureCode if code.value not in rows)

    assert not missing, f'the runbook is missing a troubleshooting row for: {", ".join(missing)}'
    assert set(rows) == _FAILURE_CODES, 'the troubleshooting table names a code that is not in the taxonomy'


def test_every_troubleshooting_row_says_whether_the_run_retries() -> None:
    rows = _troubleshooting_rows()
    for code in FailureCode:
        cells = rows[code.value]
        assert len(cells) == 3, f'{code.value} must have meaning, operator check and retry cells, got {cells!r}'
        meaning, check, retry = cells
        assert meaning, f'{code.value} has no meaning'
        assert check, f'{code.value} has no operator check'
        assert retry == _expected_retry(code), (
            f'{code.value} says retry {retry!r}, taxonomy says {_expected_retry(code)!r}'
        )


def test_every_adr_is_listed_in_the_index() -> None:
    records = sorted(path.name for path in ADR_DIR.glob('*.md') if path.name != 'README.md')
    index = ADR_INDEX_PATH.read_text(encoding='utf-8')

    assert records, 'the ADR directory has no records'
    missing = [name for name in records if f'({name})' not in index]
    assert not missing, f'the ADR index is missing: {", ".join(missing)}'


def test_every_adr_link_in_the_index_exists() -> None:
    index = ADR_INDEX_PATH.read_text(encoding='utf-8')
    links = _LINK.findall(index)

    assert links, 'the ADR index has no records'
    missing = [link for link in links if not (ADR_DIR / link).is_file()]
    assert not missing, f'the ADR index links to missing files: {", ".join(missing)}'
