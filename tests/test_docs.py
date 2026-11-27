"""The runbook, ADR index, documentation graph and bank example are part of the gate (issues #37, #89, #90).

A failure class without a troubleshooting row is a document that lies about what the
operator can do, an ADR that is not in the index is a decision nobody can find, a broken
link in the README or docs misleads a new user, an unlinked file in ``docs/`` rots
silently, and the normalisation example in ``data/README.md`` is a derivation nothing
executes. All of them are checked here rather than left to review.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from collections import deque
from pathlib import Path

import pytest

from aliexpress_coins.runner.config import DEFAULT_RUN_BUDGET_SECONDS, MAX_TASK_DISPATCHES
from aliexpress_coins.runner.failures import FAILURE_TABLE, FailureCode
from aliexpress_coins.tasks.quiz_bank import normalize_question, question_key
from aliexpress_coins.tasks.surface import Surface

ROOT = Path(__file__).resolve().parents[1]
README_PATH = ROOT / 'README.md'
DOCS_DIR = ROOT / 'docs'
RUNBOOK_PATH = DOCS_DIR / '06-runbook.md'
ADR_DIR = DOCS_DIR / 'adr'
ADR_INDEX_PATH = ADR_DIR / 'README.md'
PYPROJECT_PATH = ROOT / 'pyproject.toml'
DATA_README_PATH = ROOT / 'data' / 'README.md'

_FAILURE_CODES = frozenset(code.value for code in FailureCode)
_ROW = re.compile(r'^\|\s*`(?P<code>[a-z_]+)`\s*\|(?P<cells>.*)\|\s*$')
_ADR_LINK = re.compile(r'\]\((?P<target>[a-zA-Z0-9_-]+\.md)\)')
_MD_LINK = re.compile(r'!?\[[^\]]*\]\((?P<target>[^)]+)\)')
_HTML_SRC = re.compile(r'<(?:img|a)\b[^>]*\b(?:src|href)=["\'](?P<target>[^"\']+)["\']', re.IGNORECASE)
_FENCED_BLOCK = re.compile(r'```.*?```', re.DOTALL)
_COVERAGE_LINE = re.compile(r'(?:coverage|floor|ratchet)', re.IGNORECASE)
_PERCENT = re.compile(r'\b(?P<pct>\d+(?:\.\d+)?)%')
_JSON_BLOCK = re.compile(r'```json\s*\n(?P<body>.*?)```', re.DOTALL)
_SHA256_HEX = re.compile(r'[0-9a-f]{64}')
_UNFOLD_CONFUSABLES = str.maketrans({'1': 'l', '0': 'o'})
_DERIVATION = (
    'the documented derivation is, in order: NFKC, case-fold, fold confusables towards the digit '
    '(l -> 1, o -> 0), strip punctuation, collapse whitespace'
)
_TROUBLESHOOTING_START = '## 10. Troubleshooting by failure code'
_TROUBLESHOOTING_END = '### Run-level statuses that are not failures'
_SURFACE_MARKER = '**`surface`** is the screen the planner understood:'
_CLAIM_SECTION_START = '## What a claim run does'
_CLAIM_SECTION_END = '\n## '
_REQUIRED_REFUSAL_CODES = ('spending_order', 'spending_cart', 'game_play_and_earn')


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


def _strip_code_blocks(text: str) -> str:
    return _FENCED_BLOCK.sub('', text)


def _claim_section() -> str:
    """The README's operator section about the claim loop, without its trailing heading."""
    text = README_PATH.read_text(encoding='utf-8')
    _, found, after = text.partition(_CLAIM_SECTION_START)
    assert found, f'{README_PATH} has no {_CLAIM_SECTION_START!r} section'
    return after.partition(_CLAIM_SECTION_END)[0]


def _local_targets(source: Path) -> list[Path]:
    """Resolve every local link or image target in a markdown file."""
    text = _strip_code_blocks(source.read_text(encoding='utf-8'))
    raw_targets = [*_MD_LINK.findall(text), *_HTML_SRC.findall(text)]
    resolved: list[Path] = []
    for raw in raw_targets:
        target = raw.strip().strip('<>').split(None, 1)[0]
        target = target.split('#', 1)[0].split('?', 1)[0]
        if not target or '://' in target or target.startswith('mailto:'):
            continue
        resolved.append((source.parent / target).resolve())
    return resolved


def _display_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _missing_links(source: Path, *, root: Path = ROOT) -> list[str]:
    """Return sorted relative paths of local links in ``source`` that do not exist."""
    missing = {_display_path(target, root) for target in _local_targets(source) if not target.exists()}
    return sorted(missing)


def _orphaned_docs(readme: Path, docs_dir: Path, *, root: Path = ROOT) -> list[str]:
    """Return sorted relative paths of ``docs/*.md`` files unreachable from ``readme``."""
    resolved_root = root.resolve()
    start = readme.resolve()
    visited: set[Path] = {start}
    queue: deque[Path] = deque([start])

    while queue:
        current = queue.popleft()
        if not current.is_file():
            continue
        for target in _local_targets(current):
            if not target.exists():
                continue
            if target.is_dir():
                index = target / 'README.md'
                if index.is_file():
                    target = index.resolve()
                else:
                    continue
            if target.suffix != '.md' or target in visited:
                continue
            try:
                target.relative_to(resolved_root)
            except ValueError:
                continue
            visited.add(target)
            queue.append(target)

    all_docs = {path.resolve() for path in docs_dir.rglob('*.md')}
    orphans = all_docs - visited
    return sorted(_display_path(path, resolved_root) for path in orphans)


def _pyproject_coverage_floor(pyproject: Path = PYPROJECT_PATH) -> float:
    data = tomllib.loads(pyproject.read_text(encoding='utf-8'))
    return float(data['tool']['coverage']['report']['fail_under'])


def _quoted_coverage_floors(readme_text: str) -> list[float]:
    quoted: list[float] = []
    for paragraph in re.split(r'\n\s*\n', _strip_code_blocks(readme_text)):
        if not _COVERAGE_LINE.search(paragraph):
            continue
        for match in _PERCENT.finditer(paragraph):
            quoted.append(float(match.group('pct')))
    return quoted


def _coverage_floor_mismatches(readme_text: str, expected: float) -> list[float]:
    return [value for value in _quoted_coverage_floors(readme_text) if value != expected]


def _bank_example() -> dict[str, object]:
    """The example entry from the ```json block in ``data/README.md``."""
    text = DATA_README_PATH.read_text(encoding='utf-8')
    match = _JSON_BLOCK.search(text)
    assert match is not None, f'{DATA_README_PATH} has no ```json example block'
    document: object = json.loads(match.group('body'))
    assert isinstance(document, dict), f'{DATA_README_PATH} example block is not a JSON object'
    questions = document['questions']
    assert isinstance(questions, list), f'{DATA_README_PATH} example questions is not a list'
    assert questions, f'{DATA_README_PATH} example has no questions'
    example = questions[0]
    assert isinstance(example, dict), f'{DATA_README_PATH} example entry is not a JSON object'
    return example


def _example_text(example: dict[str, object], field: str) -> str:
    """One string field of the README's example entry."""
    value = example[field]
    assert isinstance(value, str), f'data/README.md example field {field!r} is not a string: {value!r}'
    return value


def _normalisation_mismatch(question: str, documented: str) -> list[str]:
    """Every way the example's ``question`` fails to reproduce its ``normalized`` value."""
    problems: list[str] = []
    actual = normalize_question(question)
    if actual != documented:
        problems.append(f'normalize_question({question!r}) is {actual!r}, the document says {documented!r}')

    unfolded = documented.translate(_UNFOLD_CONFUSABLES)
    folded = normalize_question(unfolded)
    if folded != documented:
        problems.append(
            f'the letter form {unfolded!r} must share one key with the digit form {documented!r}, '
            f'got {folded!r}: the fold goes towards the digit, so l -> 1 and o -> 0'
        )

    upper = normalize_question(question.upper())
    if upper != documented:
        problems.append(
            f'the uppercase form {question.upper()!r} must normalise to {documented!r}, got {upper!r}: '
            f'case folding runs before the confusable fold, so O -> o -> 0 and L -> l -> 1'
        )
    return problems


def _normalisation_message(problems: list[str]) -> str:
    return (
        f'data/README.md no longer describes normalize_question(): {"; ".join(problems)}. '
        f'{_DERIVATION}. A clean "following" therefore normalises to "f0110wing", exactly as an OCR '
        f'"f0110wing" does. If the normaliser changed, update the README example and this test together; '
        f'if the README was edited, restore it.'
    )


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
    links = _ADR_LINK.findall(index)

    assert links, 'the ADR index has no records'
    missing = [link for link in links if not (ADR_DIR / link).is_file()]
    assert not missing, f'the ADR index links to missing files: {", ".join(missing)}'


def test_every_document_linked_from_readme_exists() -> None:
    missing = _missing_links(README_PATH)
    assert not missing, f'README.md links to missing paths: {", ".join(missing)}'


def test_readme_links_the_claim_section_from_the_top() -> None:
    head = README_PATH.read_text(encoding='utf-8').partition('## What it is')[0]
    assert '](#what-a-claim-run-does)' in head, 'README.md does not link the claim section from its opening lines'


def test_readme_claim_section_pins_the_shipped_bounds() -> None:
    section = _claim_section()
    checks = {
        'wall-clock default': rf'\b{DEFAULT_RUN_BUDGET_SECONDS:g}\b',
        'task derivation': r'derived from the catalogue',
        'task backstop': rf'\b{MAX_TASK_DISPATCHES}\b',
        'wall-clock setting': re.escape('AECOINS_RUN_BUDGET_SECONDS'),
        'task setting': re.escape('AECOINS_MAX_TASKS_PER_RUN'),
    }
    absent = [name for name, pattern in checks.items() if re.search(pattern, section) is None]
    assert not absent, f'README.md claim section does not name the shipped bounds: {", ".join(absent)}'


def test_shipped_budget_defaults_agree_with_the_configuration() -> None:
    expected = f'{DEFAULT_RUN_BUDGET_SECONDS:g}'
    env_example = (ROOT / '.env.example').read_text(encoding='utf-8')
    compose = (ROOT / 'compose.yaml').read_text(encoding='utf-8')
    brief = (DOCS_DIR / '00-brief.md').read_text(encoding='utf-8')
    runbook = RUNBOOK_PATH.read_text(encoding='utf-8')

    assert f'AECOINS_RUN_BUDGET_SECONDS={expected}' in env_example
    assert f'AECOINS_RUN_BUDGET_SECONDS:-{expected}' in compose
    assert f'default {expected} s' in brief
    assert f'default {expected} s' in runbook


def test_readme_claim_section_names_the_required_refusal_codes() -> None:
    section = _claim_section()
    absent = [code for code in _REQUIRED_REFUSAL_CODES if f'`{code}`' not in section]
    assert not absent, f'README.md claim section does not name the permanent refusal codes: {", ".join(absent)}'


def test_every_document_in_docs_is_reachable_from_readme() -> None:
    orphans = _orphaned_docs(README_PATH, DOCS_DIR)
    assert not orphans, f'unreachable documents in docs/: {", ".join(orphans)}'


def test_every_local_link_in_docs_exists() -> None:
    broken: dict[str, list[str]] = {}
    for doc in sorted(DOCS_DIR.rglob('*.md')):
        missing = _missing_links(doc)
        if missing:
            broken[_display_path(doc, ROOT)] = missing
    assert not broken, f'broken local links in docs/: {broken}'


def test_readme_coverage_floor_matches_pyproject() -> None:
    expected = _pyproject_coverage_floor()
    mismatches = _coverage_floor_mismatches(README_PATH.read_text(encoding='utf-8'), expected)
    assert not mismatches, f'README.md quotes coverage floor {mismatches}, pyproject.toml has {expected}'


def test_missing_readme_link_is_detected(tmp_path: Path) -> None:
    readme = tmp_path / 'README.md'
    readme.write_text('See [missing](docs/does-not-exist.md).\n', encoding='utf-8')

    assert _missing_links(readme, root=tmp_path) == ['docs/does-not-exist.md']


def test_orphaned_doc_is_detected(tmp_path: Path) -> None:
    docs = tmp_path / 'docs'
    docs.mkdir()
    (docs / 'linked.md').write_text('# Linked\n', encoding='utf-8')
    (docs / 'orphan.md').write_text('# Orphan\n', encoding='utf-8')
    readme = tmp_path / 'README.md'
    readme.write_text('See [linked](docs/linked.md).\n', encoding='utf-8')

    assert _orphaned_docs(readme, docs, root=tmp_path) == ['docs/orphan.md']


def test_mismatched_readme_coverage_floor_is_detected() -> None:
    mismatches = _coverage_floor_mismatches('Coverage floor is 98.0% in pyproject.toml.\n', 99.47)
    assert mismatches == [98.0]


def test_data_readme_example_matches_the_normaliser() -> None:
    example = _bank_example()
    question = _example_text(example, 'question')
    documented = _example_text(example, 'normalized')

    problems = _normalisation_mismatch(question, documented)
    assert not problems, _normalisation_message(problems)

    placeholder = _example_text(example, 'key')
    assert _SHA256_HEX.fullmatch(placeholder) is None, (
        f'data/README.md pins the key to the literal digest {placeholder!r}; it must describe the rule '
        f'(SHA-256 of `normalized`) so the example cannot disagree with the entry it labels'
    )
    key = question_key(question)
    digest = hashlib.sha256(documented.encode('utf-8')).hexdigest()
    assert _SHA256_HEX.fullmatch(key) is not None, f'question_key() returned {key!r}, which is not a SHA-256 digest'
    assert key == digest, (
        f'data/README.md says key is the SHA-256 of `normalized`, but question_key({question!r}) is {key!r} '
        f'while sha256({documented!r}) is {digest!r}'
    )


def test_a_drifted_data_readme_example_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = DATA_README_PATH.read_text(encoding='utf-8')
    path = tmp_path / 'README.md'
    path.write_text(text.replace('f0110wing', 'following', 1), encoding='utf-8')
    monkeypatch.setattr('tests.test_docs.DATA_README_PATH', path)

    example = _bank_example()
    problems = _normalisation_mismatch(
        _example_text(example, 'question'),
        _example_text(example, 'normalized'),
    )

    assert problems, 'editing the documented normalized value alone must not pass'
    message = _normalisation_message(problems)
    assert 'l -> 1' in message
    assert 'o -> 0' in message
    assert 'in order' in message
    assert 'NFKC' in message
