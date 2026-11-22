"""Run artifact store: a frame and an OCR sidecar that replay together.

The layout is the one other modules already write into:

    runs/<run-id>/shots/<name>.png
    runs/<run-id>/shots/<name>.json

`AECOINS_RUNS_DIR` selects the root. The sidecar is the source of truth for the
box list; reloading it must not lose or reorder a box, and the payload is plain
JSON so a human can inspect it without this package.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, cast

from PIL import Image

from aliexpress_coins.perception.errors import ArtifactError
from aliexpress_coins.perception.geometry import DeviceGeometry, Orientation
from aliexpress_coins.perception.snapshot import BoxPoints, Snapshot, TextBox

SIDECAR_SCHEMA = 1
_DEFAULT_RUNS_DIR = 'runs'
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ArtifactPaths:
    """The two files written for one named snapshot."""

    image: Path
    sidecar: Path


def _validate_component(value: str, kind: str) -> str:
    if not value or value in {'.', '..'}:
        raise ArtifactError(f'{kind} must be a non-empty file name')
    if Path(value).name != value or '/' in value or '\\' in value:
        raise ArtifactError(f'{kind} must be a single path component: {value!r}')
    return value


def stage_file(path: Path, data: bytes) -> Path:
    """Write the complete payload to a sibling temp file, not the target yet.

    The temp file lives in ``path.parent`` so the caller's ``os.replace`` stays
    within one filesystem and is atomic. The directory is created when missing.
    A failed stage removes its own temp file and re-raises ``OSError``; callers
    translate that into their layer's typed error.
    """
    temporary: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return Path(temporary)
    except OSError:
        if temporary is not None:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write one payload so a reader sees the old file or the new one.

    The payload is staged and fsynced first, then renamed over the target. A
    kill between those steps leaves the staged ``.name.*`` sibling behind, not
    a truncated target. Failures clean up the staged file and re-raise
    ``OSError`` for the caller to wrap.
    """
    staged: Path | None = None
    try:
        staged = stage_file(path, data)
        os.replace(staged, path)
    except OSError:
        if staged is not None:
            with contextlib.suppress(OSError):
                staged.unlink()
        raise


def _stage(path: Path, data: bytes) -> Path:
    """Stage a payload, translating an I/O failure into :class:`ArtifactError`."""
    try:
        return stage_file(path, data)
    except OSError as exc:
        raise ArtifactError(f'could not write {path}: {exc}') from exc


def _discard(*paths: Path) -> None:
    for path in paths:
        with contextlib.suppress(OSError):
            path.unlink()


def _write_pair(first: Path, first_data: bytes, second: Path, second_data: bytes) -> None:
    """Stage both files fully, then replace the targets together."""
    staged: list[Path] = []
    try:
        staged.append(_stage(first, first_data))
        staged.append(_stage(second, second_data))
        os.replace(staged[0], first)
        os.replace(staged[1], second)
    except ArtifactError:
        _discard(*staged)
        raise
    except OSError as exc:
        _discard(*staged)
        raise ArtifactError(f'could not write {first} and {second}: {exc}') from exc


def _geometry_payload(geometry: DeviceGeometry) -> dict[str, Any]:
    return {
        'width': geometry.width,
        'height': geometry.height,
        'density': geometry.density,
        'sdk': geometry.sdk,
        'orientation': geometry.orientation,
    }


def _sidecar_payload(snapshot: Snapshot, name: str) -> dict[str, Any]:
    return {
        'schema': SIDECAR_SCHEMA,
        'name': name,
        'captured_at': snapshot.captured_at.isoformat(),
        'profile_id': snapshot.profile_id,
        'geometry': _geometry_payload(snapshot.geometry),
        'image': {'width': snapshot.width, 'height': snapshot.height},
        'boxes': [
            {
                'text': box.text,
                'box': [[x, y] for x, y in box.box],
                'score': box.score,
            }
            for box in snapshot.boxes
        ],
    }


def _mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ArtifactError(f'{what} must be a JSON object')
    return cast(Mapping[str, Any], value)


def _sequence(value: Any, what: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ArtifactError(f'{what} must be a JSON array')
    return value


def _int_field(data: Mapping[str, Any], key: str, what: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArtifactError(f'{what} field {key!r} must be an integer')
    return value


def _str_field(data: Mapping[str, Any], key: str, what: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ArtifactError(f'{what} field {key!r} must be a string')
    return value


def _geometry_from_payload(value: Any) -> DeviceGeometry:
    data = _mapping(value, 'geometry')
    orientation = _str_field(data, 'orientation', 'geometry')
    if orientation not in {'portrait', 'landscape'}:
        raise ArtifactError(f'geometry orientation is not valid: {orientation!r}')
    try:
        return DeviceGeometry(
            width=_int_field(data, 'width', 'geometry'),
            height=_int_field(data, 'height', 'geometry'),
            density=_int_field(data, 'density', 'geometry'),
            sdk=_int_field(data, 'sdk', 'geometry'),
            orientation=cast(Orientation, orientation),
        )
    except ValueError as exc:
        raise ArtifactError(f'invalid geometry in sidecar: {exc}') from exc


def _box_from_payload(value: Any) -> TextBox:
    data = _mapping(value, 'box entry')
    points: list[tuple[float, float]] = []
    for raw_point in _sequence(data.get('box'), 'box points'):
        pair = _sequence(raw_point, 'one box point')
        if len(pair) != 2:
            raise ArtifactError('each box point must be [x, y]')
        try:
            points.append((float(pair[0]), float(pair[1])))
        except (TypeError, ValueError) as exc:
            raise ArtifactError(f'box point is not numeric: {pair!r}') from exc
    if len(points) != 4:
        raise ArtifactError(f'a box must have four points, got {len(points)}')

    score = data.get('score')
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ArtifactError("box entry field 'score' must be a number")
    return TextBox(
        text=_str_field(data, 'text', 'box entry'),
        box=cast(BoxPoints, tuple(points)),
        score=float(score),
    )


class ArtifactStore:
    """Write and reload snapshots inside one run directory."""

    def __init__(self, runs_dir: str | Path, run_id: str) -> None:
        self.runs_dir = Path(runs_dir)
        self.run_id = _validate_component(run_id, 'run id')

    @property
    def run_dir(self) -> Path:
        return self.runs_dir / self.run_id

    @property
    def shots_dir(self) -> Path:
        return self.run_dir / 'shots'

    @classmethod
    def for_run(
        cls,
        runs_dir: str | Path | None = None,
        *,
        run_id: str | None = None,
        clock: Clock = _utc_now,
    ) -> ArtifactStore:
        """Create a store for a fresh (or explicitly named) run."""
        root = Path(runs_dir) if runs_dir is not None else Path(_DEFAULT_RUNS_DIR)
        identifier = run_id if run_id is not None else clock().strftime('%Y%m%dT%H%M%SZ')
        return cls(root, identifier)

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        run_id: str | None = None,
        clock: Clock = _utc_now,
    ) -> ArtifactStore:
        """Use the container's `AECOINS_RUNS_DIR`, falling back to `runs/`."""
        env = os.environ if environment is None else environment
        root = env.get('AECOINS_RUNS_DIR', '').strip() or _DEFAULT_RUNS_DIR
        return cls.for_run(root, run_id=run_id, clock=clock)

    def paths(self, name: str) -> ArtifactPaths:
        """Resolve, but do not create, the two files for a snapshot name."""
        stem = _validate_component(name, 'snapshot name')
        return ArtifactPaths(
            image=self.shots_dir / f'{stem}.png',
            sidecar=self.shots_dir / f'{stem}.json',
        )

    def write(self, snapshot: Snapshot, name: str) -> ArtifactPaths:
        """Write the PNG and its JSON sidecar after staging both payloads."""
        paths = self.paths(name)
        payload = _sidecar_payload(snapshot, name)
        sidecar_bytes = (json.dumps(payload, ensure_ascii=False, indent=2) + '\n').encode('utf-8')

        buffer = BytesIO()
        try:
            snapshot.image.save(buffer, format='PNG')
        except OSError as exc:
            raise ArtifactError(f'could not encode snapshot {name!r}: {exc}') from exc

        _write_pair(paths.image, buffer.getvalue(), paths.sidecar, sidecar_bytes)
        return paths

    def load(self, name: str) -> Snapshot:
        """Reload a name, reconstructing the same ordered box list."""
        paths = self.paths(name)
        try:
            raw = paths.sidecar.read_text(encoding='utf-8')
        except OSError as exc:
            raise ArtifactError(f'could not read {paths.sidecar}: {exc}') from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ArtifactError(f'{paths.sidecar} is not valid JSON: {exc}') from exc

        data = _mapping(payload, 'sidecar')
        if data.get('schema', SIDECAR_SCHEMA) != SIDECAR_SCHEMA:
            raise ArtifactError(f'unsupported sidecar schema: {data.get("schema")!r}')

        geometry = _geometry_from_payload(data.get('geometry'))
        recorded_profile = data.get('profile_id')
        if recorded_profile is not None and recorded_profile != geometry.profile_id:
            raise ArtifactError(
                f'sidecar profile {recorded_profile!r} does not match its geometry {geometry.profile_id!r}',
            )

        captured_raw = _str_field(data, 'captured_at', 'sidecar')
        try:
            captured_at = datetime.fromisoformat(captured_raw)
        except ValueError as exc:
            raise ArtifactError(f'invalid captured_at in sidecar: {captured_raw!r}') from exc
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=UTC)

        boxes = tuple(_box_from_payload(box) for box in _sequence(data.get('boxes'), 'boxes'))

        try:
            with Image.open(paths.image) as opened:
                opened.load()
                image = opened.convert('RGB')
        except OSError as exc:
            raise ArtifactError(f'could not read {paths.image}: {exc}') from exc

        recorded_image = data.get('image')
        if recorded_image is not None:
            image_data = _mapping(recorded_image, 'image')
            expected = (
                _int_field(image_data, 'width', 'image'),
                _int_field(image_data, 'height', 'image'),
            )
            if expected != (image.width, image.height):
                raise ArtifactError(
                    f'sidecar image size {expected} does not match the PNG {(image.width, image.height)}',
                )

        return Snapshot(image=image, boxes=boxes, captured_at=captured_at, geometry=geometry)
