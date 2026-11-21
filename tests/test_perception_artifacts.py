"""PNG + sidecar round-trip through the run artifact store (issue #12)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from aliexpress_coins.perception.artifacts import ArtifactPaths, ArtifactStore
from aliexpress_coins.perception.errors import ArtifactError
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.snapshot import Snapshot, TextBox

CAPTURED_AT = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def _box(text: str, left: float, top: float, score: float = 0.9) -> TextBox:
    return TextBox(
        text=text,
        box=((left, top), (left + 20, top), (left + 20, top + 10), (left, top + 10)),
        score=score,
    )


def _snapshot() -> Snapshot:
    return Snapshot(
        image=Image.new('RGB', (320, 640), 'white'),
        boxes=(_box('Coins', 10, 30), _box('Daily coins', 10, 70), _box('Coins', 10, 50, 0.8)),
        captured_at=CAPTURED_AT,
        geometry=DeviceGeometry.from_frame(320, 640, density=240, sdk=35),
    )


def _rewrite(paths: ArtifactPaths, change: Callable[[dict[str, Any]], None]) -> None:
    payload: dict[str, Any] = json.loads(paths.sidecar.read_text(encoding='utf-8'))
    change(payload)
    paths.sidecar.write_text(json.dumps(payload), encoding='utf-8')


def test_write_and_load_round_trip_every_box_in_order(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, 'run-1')
    snapshot = _snapshot()

    paths = store.write(snapshot, '01-launch')
    loaded = store.load('01-launch')

    assert paths == ArtifactPaths(
        image=tmp_path / 'run-1' / 'shots' / '01-launch.png',
        sidecar=tmp_path / 'run-1' / 'shots' / '01-launch.json',
    )
    assert paths.image.is_file()
    assert paths.sidecar.is_file()
    assert loaded.boxes == snapshot.boxes
    assert [box.text for box in loaded.boxes] == ['Coins', 'Daily coins', 'Coins']
    assert loaded.captured_at == snapshot.captured_at
    assert loaded.geometry == snapshot.geometry
    assert loaded.profile_id == snapshot.profile_id
    assert loaded.image.size == snapshot.image.size
    assert loaded.image.mode == 'RGB'


def test_the_sidecar_is_plain_json_with_geometry_and_timestamp(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, 'run-1')
    snapshot = _snapshot()
    paths = store.write(snapshot, '02-coins')

    payload = json.loads(paths.sidecar.read_text(encoding='utf-8'))

    assert payload['schema'] == 1
    assert payload['profile_id'] == snapshot.profile_id
    assert payload['captured_at'] == CAPTURED_AT.isoformat()
    assert payload['geometry'] == {
        'width': 320,
        'height': 640,
        'density': 240,
        'sdk': 35,
        'orientation': 'portrait',
    }
    assert len(payload['boxes']) == len(snapshot.boxes)
    assert [box['text'] for box in payload['boxes']] == ['Coins', 'Daily coins', 'Coins']


def test_for_run_derives_the_directory_from_the_injected_clock(tmp_path: Path) -> None:
    store = ArtifactStore.for_run(tmp_path, clock=lambda: CAPTURED_AT)
    assert store.run_id == '20261121T120000Z'
    assert store.run_dir == tmp_path / '20261121T120000Z'
    assert store.shots_dir == store.run_dir / 'shots'


def test_from_environment_reuses_the_container_runs_directory(tmp_path: Path) -> None:
    store = ArtifactStore.from_environment(
        {'AECOINS_RUNS_DIR': str(tmp_path / 'artifacts')},
        run_id='run-7',
    )
    assert store.runs_dir == tmp_path / 'artifacts'
    assert store.run_id == 'run-7'


def test_from_environment_defaults_to_the_local_runs_directory() -> None:
    store = ArtifactStore.from_environment({}, run_id='run-8')
    assert store.runs_dir == Path('runs')
    assert store.shots_dir == Path('runs') / 'run-8' / 'shots'


def test_from_environment_derives_a_run_id_from_the_clock() -> None:
    store = ArtifactStore.from_environment({})
    assert len(store.run_id) == 16
    assert store.run_id.endswith('Z')
    assert store.run_id[8] == 'T'


@pytest.mark.parametrize('name', ['', '.', '..', 'a/b', '..\\escape'])
def test_snapshot_names_cannot_escape_the_shots_directory(tmp_path: Path, name: str) -> None:
    store = ArtifactStore(tmp_path, 'run-1')
    with pytest.raises(ArtifactError, match=r'single path component|non-empty'):
        store.paths(name)


@pytest.mark.parametrize('run_id', ['', '.', '..', 'a/b', '..\\escape'])
def test_run_ids_cannot_escape_the_runs_directory(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(ArtifactError, match=r'single path component|non-empty'):
        ArtifactStore(tmp_path, run_id)


def test_load_missing_sidecar_is_a_typed_error(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, 'run-1')
    with pytest.raises(ArtifactError, match='could not read'):
        store.load('missing')


def test_load_invalid_json_is_a_typed_error(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, 'run-1')
    paths = store.paths('broken')
    store.shots_dir.mkdir(parents=True)
    paths.sidecar.write_text('{not json', encoding='utf-8')
    with pytest.raises(ArtifactError, match='not valid JSON'):
        store.load('broken')


def test_load_rejects_a_profile_that_does_not_match_its_geometry(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, 'run-1')
    paths = store.write(_snapshot(), 'mismatch')
    _rewrite(paths, lambda payload: payload.__setitem__('profile_id', 'wrong'))
    with pytest.raises(ArtifactError, match='does not match'):
        store.load('mismatch')


@pytest.mark.parametrize(
    ('change', 'message'),
    [
        (lambda payload: payload.__setitem__('schema', 99), 'unsupported sidecar schema'),
        (
            lambda payload: payload['geometry'].__setitem__('orientation', 'sideways'),
            'orientation is not valid',
        ),
        (lambda payload: payload['boxes'][0].__setitem__('box', [[0, 0], [1, 1], [2, 2]]), 'four points'),
        (lambda payload: payload['boxes'][0].__setitem__('score', 'high'), 'must be a number'),
        (lambda payload: payload.__setitem__('captured_at', 'yesterday'), 'invalid captured_at'),
        (lambda payload: payload.__setitem__('image', {'width': 1, 'height': 1}), 'does not match the PNG'),
        (lambda payload: payload['geometry'].__setitem__('width', 'wide'), 'must be an integer'),
        (lambda payload: payload['geometry'].__setitem__('width', 0), 'invalid geometry'),
        (lambda payload: payload['boxes'][0].__setitem__('text', 5), 'must be a string'),
        (lambda payload: payload.__setitem__('boxes', 'nope'), 'must be a JSON array'),
        (lambda payload: payload['boxes'][0]['box'].__setitem__(0, [1, 2, 3]), r'must be \[x, y\]'),
        (lambda payload: payload['boxes'][0]['box'].__setitem__(0, ['x', 'y']), 'not numeric'),
    ],
)
def test_load_rejects_a_malformed_sidecar(
    tmp_path: Path,
    change: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    store = ArtifactStore(tmp_path, 'run-1')
    paths = store.write(_snapshot(), 'bad')
    _rewrite(paths, change)
    with pytest.raises(ArtifactError, match=message):
        store.load('bad')


def test_load_rejects_a_sidecar_that_is_not_a_json_object(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, 'run-1')
    paths = store.write(_snapshot(), 'array')
    paths.sidecar.write_text('[]', encoding='utf-8')
    with pytest.raises(ArtifactError, match='must be a JSON object'):
        store.load('array')


def test_load_reports_a_missing_png(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, 'run-1')
    paths = store.write(_snapshot(), 'gone')
    paths.image.unlink()
    with pytest.raises(ArtifactError, match='could not read'):
        store.load('gone')


def test_load_accepts_a_minimal_sidecar_and_naive_timestamp(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, 'run-1')
    paths = store.write(_snapshot(), 'minimal')

    def strip_optional_fields(payload: dict[str, Any]) -> None:
        payload.pop('schema')
        payload.pop('profile_id')
        payload.pop('image')
        payload['captured_at'] = '2026-11-21T12:00:00'

    _rewrite(paths, strip_optional_fields)

    loaded = store.load('minimal')

    assert loaded.captured_at == CAPTURED_AT
    assert loaded.captured_at.tzinfo is UTC
    assert len(loaded.boxes) == 3


def test_a_png_encoding_failure_is_a_typed_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_save(_self: Image.Image, _stream: object, **_kwargs: object) -> None:
        raise OSError('encoder unavailable')

    monkeypatch.setattr(Image.Image, 'save', broken_save)
    with pytest.raises(ArtifactError, match='could not encode'):
        ArtifactStore(tmp_path, 'run-1').write(_snapshot(), 'broken')


def test_an_atomic_write_failure_is_a_typed_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_replace(_source: object, _target: object) -> None:
        raise OSError('disk full')

    monkeypatch.setattr(os, 'replace', broken_replace)
    store = ArtifactStore(tmp_path, 'run-1')
    with pytest.raises(ArtifactError, match='disk full'):
        store.write(_snapshot(), 'broken')
    assert list(store.shots_dir.iterdir()) == []


def test_a_sidecar_replace_failure_is_a_typed_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_replace = os.replace
    calls = 0

    def flaky_replace(source: str, target: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('sidecar disk full')
        real_replace(source, target)

    monkeypatch.setattr(os, 'replace', flaky_replace)
    store = ArtifactStore(tmp_path, 'run-1')
    with pytest.raises(ArtifactError, match='sidecar disk full'):
        store.write(_snapshot(), 'broken')
    assert calls == 2
    assert not store.paths('broken').sidecar.exists()


def test_a_staging_failure_is_typed_and_removes_the_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_fsync(_fd: int) -> None:
        raise OSError('fsync failed')

    monkeypatch.setattr(os, 'fsync', broken_fsync)
    store = ArtifactStore(tmp_path, 'run-1')
    with pytest.raises(ArtifactError, match='fsync failed'):
        store.write(_snapshot(), 'broken')
    assert list(store.shots_dir.iterdir()) == []
