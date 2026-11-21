"""The immutable frame value and its query helpers.

A `Snapshot` is deliberately self-contained: the image, the OCR boxes that were
derived from it, when it was taken and the geometry it came from. It holds no
adb handle, so it can be stored, replayed and asserted against without a device.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from PIL import Image

from aliexpress_coins.perception.errors import SnapshotError
from aliexpress_coins.perception.geometry import DeviceGeometry

Point = tuple[float, float]
BoxPoints = tuple[Point, Point, Point, Point]
Rect = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class TextBox:
    """One OCR result: recognised text, its four-corner quad and confidence."""

    text: str
    box: BoxPoints
    score: float

    def __post_init__(self) -> None:
        points = tuple((float(x), float(y)) for x, y in self.box)
        if len(points) != 4:
            raise ValueError('a text box needs exactly four points')
        object.__setattr__(self, 'box', points)
        object.__setattr__(self, 'score', float(self.score))

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """The axis-aligned bounds as `(left, top, right, bottom)`."""
        xs = [x for x, _ in self.box]
        ys = [y for _, y in self.box]
        return min(xs), min(ys), max(xs), max(ys)

    @property
    def center(self) -> Point:
        """The centre of the box, in the coordinates of its frame."""
        left, top, right, bottom = self.bbox
        return (left + right) / 2.0, (top + bottom) / 2.0

    @property
    def width(self) -> float:
        """The width of the axis-aligned bounds."""
        left, _, right, _ = self.bbox
        return right - left

    @property
    def height(self) -> float:
        """The height of the axis-aligned bounds."""
        _, top, _, bottom = self.bbox
        return bottom - top

    def shifted(self, dx: float, dy: float) -> TextBox:
        """Return the same box translated by `(dx, dy)`."""
        p0, p1, p2, p3 = self.box
        return TextBox(
            text=self.text,
            box=(
                (p0[0] + dx, p0[1] + dy),
                (p1[0] + dx, p1[1] + dy),
                (p2[0] + dx, p2[1] + dy),
                (p3[0] + dx, p3[1] + dy),
            ),
            score=self.score,
        )


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One captured frame, its OCR boxes and the geometry it came from."""

    image: Image.Image
    boxes: tuple[TextBox, ...]
    captured_at: datetime
    geometry: DeviceGeometry

    def __post_init__(self) -> None:
        object.__setattr__(self, 'boxes', tuple(self.boxes))

    @property
    def width(self) -> int:
        """The frame width in pixels."""
        return self.image.width

    @property
    def height(self) -> int:
        """The frame height in pixels."""
        return self.image.height

    @property
    def profile_id(self) -> str:
        """The replay key of the device geometry."""
        return self.geometry.profile_id

    def find(self, label: str) -> TextBox | None:
        """Return the first box whose text equals `label`, ignoring case and edges.

        Fuzzy matching belongs to #13; this is the exact lookup a caller uses when
        it already knows the rendered label.
        """
        needle = label.strip().casefold()
        for box in self.boxes:
            if box.text.strip().casefold() == needle:
                return box
        return None

    def has(self, label: str) -> bool:
        """Whether `find(label)` would return a box."""
        return self.find(label) is not None

    def describe(self) -> str:
        """Render the frame and its labels for a log line or a test failure."""
        header = (
            f'Snapshot {self.width}x{self.height} profile={self.profile_id} '
            f'at={self.captured_at.isoformat()} boxes={len(self.boxes)}'
        )
        lines = [header]
        for box in self.boxes:
            x, y = box.center
            lines.append(f'  {box.text!r} @ ({x:.1f}, {y:.1f}) score={box.score:.3f}')
        return '\n'.join(lines)

    def crop(self, rect: Rect) -> Snapshot:
        """Crop the frame and shift the boxes into crop-local coordinates."""
        return crop_snapshot(self, rect)


def crop_snapshot(snapshot: Snapshot, rect: Rect) -> Snapshot:
    """Return a `Snapshot` of `rect` in frame coordinates.

    Boxes whose centre lies inside the rectangle are kept, in their original
    order, and translated by `(-left, -top)`. The geometry (and therefore the
    device profile) is unchanged: a crop is still a frame from the same device.
    """
    left, top, right, bottom = rect
    if left < 0 or top < 0 or right > snapshot.width or bottom > snapshot.height:
        raise SnapshotError(
            f'crop {rect!r} is outside the {snapshot.width}x{snapshot.height} frame',
        )
    if left >= right or top >= bottom:
        raise SnapshotError(f'crop {rect!r} has no area')

    boxes = tuple(
        box.shifted(-left, -top)
        for box in snapshot.boxes
        if left <= box.center[0] < right and top <= box.center[1] < bottom
    )
    return Snapshot(
        image=snapshot.image.crop(rect),
        boxes=boxes,
        captured_at=snapshot.captured_at,
        geometry=snapshot.geometry,
    )
