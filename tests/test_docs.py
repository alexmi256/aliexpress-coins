"""The runbook and ADR index are part of the gate (issue #37).

A failure class without a troubleshooting row is a document that lies about what the
operator can do, and an ADR that is not in the index is a decision nobody can find.
Both are checked here rather than left to review.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aliexpress_coins.runner.failures import FAILURE_TABLE, FailureCode
from aliexpress_coins.tasks.surface import Surface

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK_PATH = ROOT / 'docs' / '06-runbook.md'
ADR_DIR = ROOT / 'docs' / 'adr'
ADR_INDEX_PATH = ADR_DIR / 'README.md'

_FAILURE_CODES = frozenset(code.value for code in FailureCode)
_ROW = re.compile(r'^\|\s*`(?P<code>[a-z_]+)`\s*\|(?P<cells>.*)\|\s*$')
_LINK = re.compile(r'\]\((?P<target>[a-zA-Z0-9_-]+\.md)\)')
_TROUBLESHOOTING_START = '## 10. Troubleshooting by failure code'
_TROUBLESHOOTING_END = '### Run-level statuses that are not failures'
_SURFACE_MARKER = '**`surface`** is the screen the planner understood:'


def _troubleshooting_rows() -> dict[str, tuple[str, ...]]:
    """Return every code -> row in the troubleshooting section, including stale codes."""
    text = RUNBOOK_PATH.read_text(encoding='utf-8')
    _, found, section = text.partition(_TROUBLESHOOTING_START)
    assert found, f'{RUNBOOK_PATH} has no troubleshooting section'
    section = section.partition(_TROUBLESHOOTING_END)[0]

    rows: dict[str, tuple[str, ...]] = {}
    for line in section.splitlines():
        match = _ROW.match(line)
        if match is None:
            continue
        rows[match.group('code')] = tuple(cell.strip() for cell in match.group('cells').split('|'))
    return rows


def _troubleshooting_mismatches(rows: dict[str, tuple[str, ...]]) -> tuple[list[str], list[str]]:
    """Return missing and stale codes; both directions are failures."""
    documented = set(rows)
    return sorted(_FAILURE_CODES - documented), sorted(documented - _FAILURE_CODES)


def _documented_surface_values() -> set[str]:
    """The surface values named by the runbook's `surface` paragraph."""
    text = RUNBOOK_PATH.read_text(encoding='utf-8')
    _, found, after = text.partition(_SURFACE_MARKER)
    assert found, f'{RUNBOOK_PATH} has no surface explanation'
    paragraph = after.split('\n- **', 1)[0]
    return set(re.findall(r'`([a-z_]+)`', paragraph))


def _expected_retry(code: FailureCode) -> str:
    spec = FAILURE_TABLE[code]
    if spec.retries == 0:
        return 'none'
    return f'{spec.retry.value} {chr(215)}{spec.retries}'


def test_every_failure_code_has_a_troubleshooting_row() -> None:
    rows = _troubleshooting_rows()
    missing, stale = _troubleshooting_mismatches(rows)

    assert not missing, f'the runbook is missing a troubleshooting row for: {", ".join(missing)}'
    assert not stale, f'the troubleshooting table names a code that is not in the taxonomy: {", ".join(stale)}'


def test_a_stale_troubleshooting_row_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = RUNBOOK_PATH.read_text(encoding='utf-8')
    stale_row = '| `renamed_failure` | stale | inspect | none |\n'
    path = tmp_path / '06-runbook.md'
    path.write_text(text.replace(_TROUBLESHOOTING_END, stale_row + _TROUBLESHOOTING_END), encoding='utf-8')
    monkeypatch.setattr('tests.test_docs.RUNBOOK_PATH', path)

    missing, stale = _troubleshooting_mismatches(_troubleshooting_rows())

    assert missing == []
    assert stale == ['renamed_failure']


def test_runbook_surface_values_match_the_enum() -> None:
    assert _documented_surface_values() == {surface.value for surface in Surface}


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
