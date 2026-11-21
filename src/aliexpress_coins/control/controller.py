"""The one place a tap is planned, logged and issued (issue #17).

Every later module calls :class:`Controller`; none of them knows how a tap is
synthesised. The controller is the safety boundary:

* a tap needs a non-empty reason, because a coordinator that cannot say why a
  coordinate is safe has no business tapping it;
* the requested point is clamped inside an inset derived from the frame, so the
  last row of pixels cannot land on the navigation bar;
* a bounded jitter breaks the identical-pixel signature of a bot, drawn from
  an injected RNG so a seeded replay is reproducible;
* ``dry_run`` suppresses only the device call. The record, log line and counter
  are identical to a live run, which makes a dry run a real rehearsal.

The log record is one JSON object per tap. The field names are deliberately
stable: ``event``, ``tap`` (the 1-based counter), ``label``, ``reason``,
``requested``, ``clamped``, ``jitter``, ``point`` and ``suppressed``.
"""

from __future__ import annotations

import json
import logging
import math
import random
from dataclasses import dataclass
from typing import Protocol

from aliexpress_coins.control.device import InputDevice
from aliexpress_coins.control.errors import TapTargetNotFound
from aliexpress_coins.perception.geometry import DeviceGeometry
from aliexpress_coins.perception.snapshot import TextBox

LOGGER_NAME = 'aliexpress_coins.control'
"""The logger used when the caller does not supply one."""

SAFE_INSET_FRACTION = 0.04
"""The safe border as a fraction of the shorter screen dimension.

A tap on the last row of pixels hits the navigation bar rather than the app.
The inset is expressed as a fraction of the frame, never as a stored pixel
value, so it scales when the same run is replayed at another geometry (#42).
On the 1200x2000 reference frame this is 48 px on every edge.
"""

MAX_JITTER_FRACTION = 0.005
"""The maximum jitter in either axis, as a fraction of the shorter dimension.

Half a percent is a few pixels on the reference tablet (6 px) and scales with
the device. The controller draws an integer in ``[-radius, radius]`` from its
injected RNG, then clamps again so a tap at the safe edge cannot jitter out of
the safe region.
"""

Point = tuple[float, float]
Pixel = tuple[int, int]


class JitterSource(Protocol):
    """The subset of ``random.Random`` that jitter needs."""

    def randint(self, a: int, b: int) -> int:
        """Return an integer in the inclusive range ``[a, b]``."""
        ...


class Frame(Protocol):
    """An already-captured frame, as far as ``tap_text`` needs to see it.

    ``Snapshot`` satisfies this structurally. Keeping the protocol here means
    the control layer does not import the perception pipeline, only its value
    types (``TextBox`` and ``DeviceGeometry``).
    """

    @property
    def geometry(self) -> DeviceGeometry:
        """The geometry recorded with the frame."""
        ...

    def find(self, label: str) -> TextBox | None:
        """Return the box for ``label``, or ``None``."""
        ...


@dataclass(frozen=True, slots=True)
class TapRecord:
    """One tap decision, whether it was issued or suppressed by dry run."""

    index: int
    label: str | None
    reason: str
    requested: Pixel
    clamped: Pixel
    jitter: Pixel
    point: Pixel
    suppressed: bool

    def as_dict(self) -> dict[str, object]:
        """The machine-readable fields of the log line."""
        return {
            'event': 'tap',
            'tap': self.index,
            'label': self.label,
            'reason': self.reason,
            'requested': list(self.requested),
            'clamped': list(self.clamped),
            'jitter': list(self.jitter),
            'point': list(self.point),
            'suppressed': self.suppressed,
        }

    def to_json(self) -> str:
        """Render the record as one JSON line."""
        return json.dumps(self.as_dict(), separators=(',', ':'), sort_keys=True)


def safe_inset(geometry: DeviceGeometry) -> int:
    """The border, in pixels, derived from the shorter screen dimension."""
    shorter = min(geometry.width, geometry.height)
    limit = max(0, (shorter - 1) // 2)
    desired = max(1, round(shorter * SAFE_INSET_FRACTION))
    return min(desired, limit)


def jitter_radius(geometry: DeviceGeometry) -> int:
    """The largest jitter offset, derived from the frame and capped by the inset."""
    shorter = min(geometry.width, geometry.height)
    desired = max(0, round(shorter * MAX_JITTER_FRACTION))
    return min(desired, safe_inset(geometry))


def clamp_point(point: Point, geometry: DeviceGeometry) -> Pixel:
    """Clamp a point to the safe inset of the frame, rounding to a pixel.

    Both axes use the same inset, derived from the shorter dimension. The
    result is always inside ``[inset, width - inset] x [inset, height - inset]``,
    which the geometry guarantees is a non-empty region.
    """
    inset = safe_inset(geometry)
    x, y = _pixel(point)
    return (
        min(max(x, inset), geometry.width - inset),
        min(max(y, inset), geometry.height - inset),
    )


class Controller:
    """Plan, log and (unless dry-run) issue taps on one input device."""

    def __init__(
        self,
        device: InputDevice,
        geometry: DeviceGeometry,
        *,
        dry_run: bool = False,
        rng: JitterSource | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._device = device
        self._geometry = geometry
        self._dry_run = dry_run
        self._rng = rng if rng is not None else random.Random()
        self._logger = logger if logger is not None else logging.getLogger(LOGGER_NAME)
        self._tap_count = 0

    @property
    def geometry(self) -> DeviceGeometry:
        """The screen geometry every raw and box tap is clamped to."""
        return self._geometry

    @property
    def dry_run(self) -> bool:
        """Whether the device call is suppressed."""
        return self._dry_run

    @property
    def tap_count(self) -> int:
        """Intended taps so far, including those suppressed by dry run."""
        return self._tap_count

    def tap(self, point: Point, *, why: str, label: str | None = None) -> TapRecord:
        """Tap ``point``, clamped and jittered, with a mandatory reason."""
        return self._tap(point, why=why, label=label, geometry=self._geometry)

    def tap_box(self, box: TextBox, *, why: str, label: str | None = None) -> TapRecord:
        """Tap the centre of ``box``; the label defaults to the box's text."""
        return self.tap(box.center, why=why, label=label if label is not None else box.text)

    def tap_text(self, frame: Frame, label: str, *, why: str) -> TapRecord:
        """Find ``label`` in an already-captured frame and tap its centre.

        The frame's own geometry is used, so a snapshot captured at a different
        screen size is still clamped to the screen it actually shows.
        """
        reason = _required_reason(why)
        box = frame.find(label)
        if box is None:
            raise TapTargetNotFound(
                f'{label!r} is not in the {frame.geometry.width}x{frame.geometry.height} frame',
            )
        return self._tap(box.center, why=reason, label=label, geometry=frame.geometry)

    def _tap(
        self,
        point: Point,
        *,
        why: str,
        label: str | None,
        geometry: DeviceGeometry,
    ) -> TapRecord:
        reason = _required_reason(why)
        requested = _pixel(point)
        clamped = clamp_point(requested, geometry)
        radius = jitter_radius(geometry)
        if radius:
            dx = self._rng.randint(-radius, radius)
            dy = self._rng.randint(-radius, radius)
        else:
            dx = dy = 0
        final = clamp_point((clamped[0] + dx, clamped[1] + dy), geometry)
        jitter = (final[0] - clamped[0], final[1] - clamped[1])

        self._tap_count += 1
        record = TapRecord(
            index=self._tap_count,
            label=label,
            reason=reason,
            requested=requested,
            clamped=clamped,
            jitter=jitter,
            point=final,
            suppressed=self._dry_run,
        )
        self._logger.info('%s', record.to_json())

        if not self._dry_run:
            self._device.tap(*final)
        return record


def _required_reason(why: str) -> str:
    reason = why.strip()
    if not reason:
        raise ValueError('a tap reason is required; a tap without one is not allowed')
    return reason


def _pixel(point: Point) -> Pixel:
    x = float(point[0])
    y = float(point[1])
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError(f'tap coordinates must be finite, got {point!r}')
    return round(x), round(y)
