"""The one place a tap is planned, logged and issued (issue #17).

Every later module calls :class:`Controller`; none of them knows how a tap is
synthesised. The controller is the safety boundary:

* a tap needs a non-empty reason, because a coordinator that cannot say why a
  coordinate is safe has no business tapping it;
* a raw point is clamped inside an inset derived from the frame, so the last
  row of pixels cannot land on the navigation bar;
* a box tap is clamped inside the box, not the inset: the box is the measured
  hit area, so an edge control is not moved off itself;
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
from aliexpress_coins.control.errors import InputError, TapTargetNotFound
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

_SWIPE_DURATION_MS = 300
"""The measured drag duration shared by ordinary and observation-only swipes."""

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


@dataclass(frozen=True, slots=True)
class KeyRecord:
    """One key press, whether it was issued or suppressed by dry run."""

    index: int
    key: str
    reason: str
    suppressed: bool

    def as_dict(self) -> dict[str, object]:
        """The machine-readable fields of the log line."""
        return {
            'event': 'key',
            'key_press': self.index,
            'name': self.key,
            'reason': self.reason,
            'suppressed': self.suppressed,
        }

    def to_json(self) -> str:
        """Render the record as one JSON line."""
        return json.dumps(self.as_dict(), separators=(',', ':'), sort_keys=True)


@dataclass(frozen=True, slots=True)
class SwipeRecord:
    """One swipe decision, including its clamped endpoints."""

    index: int
    label: str | None
    reason: str
    requested: tuple[Pixel, Pixel]
    clamped: tuple[Pixel, Pixel]
    duration_ms: int
    suppressed: bool

    def as_dict(self) -> dict[str, object]:
        """The machine-readable fields of the log line."""
        return {
            'event': 'swipe',
            'swipe': self.index,
            'label': self.label,
            'reason': self.reason,
            'requested': [list(point) for point in self.requested],
            'clamped': [list(point) for point in self.clamped],
            'duration_ms': self.duration_ms,
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


def _clamp_to_box(point: Pixel, box: TextBox, geometry: DeviceGeometry) -> Pixel:
    """Clamp a pixel to the inside of ``box``, itself clamped to the screen.

    A matched box is the hit area: moving the point into the generic inset can
    put it outside a control drawn near the edge. A zero-area, degenerate or
    fully off-screen box has no usable hit area, so the point falls back to the
    safe inset rather than the screen edge.
    """
    if box.width <= 0 or box.height <= 0:
        return clamp_point(point, geometry)
    left, top, right, bottom = box.bbox
    min_x, max_x = max(0, math.ceil(left)), min(geometry.width - 1, math.floor(right))
    min_y, max_y = max(0, math.ceil(top)), min(geometry.height - 1, math.floor(bottom))
    if min_x > max_x or min_y > max_y:
        return clamp_point(point, geometry)
    x = min(max(point[0], 0), geometry.width - 1)
    y = min(max(point[1], 0), geometry.height - 1)
    return (min(max(x, min_x), max_x), min(max(y, min_y), max_y))


class Controller:
    """Plan, log and (unless dry-run) issue taps on one input device.

    Prefer :meth:`from_frame` when a captured frame is available: its geometry
    is already reconciled with the portrait screencap, unlike a raw ``wm size``
    probe on the reference device.
    """

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
        self._key_count = 0
        self._swipe_count = 0

    @classmethod
    def from_frame(
        cls,
        device: InputDevice,
        frame: Frame,
        *,
        dry_run: bool = False,
        rng: JitterSource | None = None,
        logger: logging.Logger | None = None,
    ) -> Controller:
        """Build a controller clamped to an already-captured frame's geometry.

        Use this instead of passing an unscaled ``probe_geometry()`` result:
        ``wm size`` can report the physical panel in landscape (2000x1200)
        while the portrait screencap is 1200x2000.
        """
        return cls(
            device,
            frame.geometry,
            dry_run=dry_run,
            rng=rng,
            logger=logger,
        )

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

    @property
    def key_count(self) -> int:
        """Intended key presses so far, including dry-run suppressions."""
        return self._key_count

    @property
    def swipe_count(self) -> int:
        """Intended swipes so far, including dry-run suppressions."""
        return self._swipe_count

    def tap(self, point: Point, *, why: str, label: str | None = None) -> TapRecord:
        """Tap ``point``, clamped and jittered, with a mandatory reason."""
        return self._tap(point, why=why, label=label, geometry=self._geometry)

    def tap_box(self, box: TextBox, *, why: str, label: str | None = None) -> TapRecord:
        """Tap the centre of ``box``; the label defaults to the box's text.

        ``box`` must be in full-frame coordinates. ``Snapshot.crop`` shifts
        boxes into crop-local coordinates while keeping the original geometry,
        so a box taken from a crop would tap too far up and left.

        A box tap is clamped inside the box rather than the generic safe inset.
        The box is the hit area the perception step established, so a control
        at the screen edge is not moved off itself by a 48 px border.
        """
        return self._tap(
            box.center,
            why=why,
            label=label if label is not None else box.text,
            geometry=self._geometry,
            box=box,
        )

    def press_key(self, key: str, *, why: str) -> KeyRecord:
        """Press one Android key (``KEYCODE_BACK``) through the input device.

        A key is a navigation action, not a tap: it has a mandatory reason and
        is logged like one, but it does not move the tap counter. A device that
        cannot press keys raises :class:`InputError` instead of silently doing
        nothing, so a caller can fall back to a visible control.
        """
        reason = _required_reason(why)
        name = key.strip()
        if not name:
            raise ValueError('a key name is required')
        self._key_count += 1
        record = KeyRecord(
            index=self._key_count,
            key=name,
            reason=reason,
            suppressed=self._dry_run,
        )
        self._logger.info('%s', record.to_json())
        if not self._dry_run:
            keyevent = getattr(self._device, 'keyevent', None)
            if keyevent is None:
                raise InputError(f'the input device cannot press {name!r}')
            keyevent(name)
        return record

    def swipe(
        self,
        start: Point,
        end: Point,
        *,
        why: str,
        label: str | None = None,
        duration_ms: int = _SWIPE_DURATION_MS,
    ) -> SwipeRecord:
        """Swipe between two points, clamped to the safe inset of the frame.

        Scrolling is a search, not a fixed gesture: the caller derives both
        points from the current frame, and this method only guarantees the
        gesture starts and ends on the screen. A dry run suppresses the gesture.
        """
        return self._swipe(
            start,
            end,
            why=why,
            label=label,
            duration_ms=duration_ms,
            suppress_in_dry_run=True,
        )

    def swipe_for_observation(
        self,
        start: Point,
        end: Point,
        *,
        why: str,
        label: str | None = None,
        duration_ms: int = _SWIPE_DURATION_MS,
    ) -> SwipeRecord:
        """Scroll a read-only list even during a dry run.

        Roster enumeration needs to move the boost list to prove that no task is
        hidden below the fold. This explicit observation-only path never taps or
        presses a key, so ``taps_issued`` remains zero; ordinary :meth:`swipe`
        keeps its dry-run suppression for task executors.
        """
        return self._swipe(
            start,
            end,
            why=why,
            label=label,
            duration_ms=duration_ms,
            suppress_in_dry_run=False,
        )

    def _swipe(
        self,
        start: Point,
        end: Point,
        *,
        why: str,
        label: str | None,
        duration_ms: int,
        suppress_in_dry_run: bool,
    ) -> SwipeRecord:
        reason = _required_reason(why)
        if duration_ms <= 0:
            raise ValueError(f'the swipe duration must be positive, got {duration_ms!r}')
        requested = (_pixel(start), _pixel(end))
        clamped = (clamp_point(start, self._geometry), clamp_point(end, self._geometry))
        self._swipe_count += 1
        record = SwipeRecord(
            index=self._swipe_count,
            label=label,
            reason=reason,
            requested=requested,
            clamped=clamped,
            duration_ms=duration_ms,
            suppressed=self._dry_run and suppress_in_dry_run,
        )
        self._logger.info('%s', record.to_json())
        if not (self._dry_run and suppress_in_dry_run):
            swipe = getattr(self._device, 'swipe', None)
            if swipe is None:
                raise InputError('the input device cannot swipe')
            swipe(clamped[0][0], clamped[0][1], clamped[1][0], clamped[1][1], duration_ms)
        return record

    def tap_text(self, frame: Frame, label: str, *, why: str) -> TapRecord:
        """Find ``label`` in an already-captured frame and tap its centre.

        The frame's own geometry is used, so a snapshot captured at a different
        screen size is still clamped to the screen it actually shows. ``frame``
        must be a full frame: a cropped ``Snapshot`` keeps the original geometry
        but its boxes are crop-local, so it is not a valid source here.
        """
        reason = _required_reason(why)
        box = frame.find(label)
        if box is None:
            self._logger.info(
                '%s',
                json.dumps(
                    {
                        'event': 'tap_miss',
                        'label': label,
                        'reason': reason,
                        'frame': [frame.geometry.width, frame.geometry.height],
                    },
                    separators=(',', ':'),
                    sort_keys=True,
                ),
            )
            raise TapTargetNotFound(
                f'{label!r} is not in the {frame.geometry.width}x{frame.geometry.height} frame',
            )
        return self._tap(box.center, why=reason, label=label, geometry=frame.geometry, box=box)

    def _tap(
        self,
        point: Point,
        *,
        why: str,
        label: str | None,
        geometry: DeviceGeometry,
        box: TextBox | None = None,
    ) -> TapRecord:
        reason = _required_reason(why)
        requested = _pixel(point)
        clamped = _clamp_to_box(requested, box, geometry) if box is not None else clamp_point(requested, geometry)
        radius = jitter_radius(geometry)
        if radius:
            dx = self._rng.randint(-radius, radius)
            dy = self._rng.randint(-radius, radius)
        else:
            dx = dy = 0
        jittered = (clamped[0] + dx, clamped[1] + dy)
        final = _clamp_to_box(jittered, box, geometry) if box is not None else clamp_point(jittered, geometry)
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
