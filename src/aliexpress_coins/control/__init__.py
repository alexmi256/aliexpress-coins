"""Control: the only layer allowed to synthesise a tap."""

from __future__ import annotations

from aliexpress_coins.control.controller import (
    LOGGER_NAME,
    MAX_JITTER_FRACTION,
    SAFE_INSET_FRACTION,
    Controller,
    Frame,
    JitterSource,
    Pixel,
    Point,
    TapRecord,
    clamp_point,
    jitter_radius,
    safe_inset,
)
from aliexpress_coins.control.device import (
    AdbInputDevice,
    InputDevice,
    InputResult,
    InputRunner,
    SubprocessInputRunner,
)
from aliexpress_coins.control.errors import ControlError, InputError, TapTargetNotFound

__all__ = [
    'LOGGER_NAME',
    'MAX_JITTER_FRACTION',
    'SAFE_INSET_FRACTION',
    'AdbInputDevice',
    'ControlError',
    'Controller',
    'Frame',
    'InputDevice',
    'InputError',
    'InputResult',
    'InputRunner',
    'JitterSource',
    'Pixel',
    'Point',
    'SubprocessInputRunner',
    'TapRecord',
    'TapTargetNotFound',
    'clamp_point',
    'jitter_radius',
    'safe_inset',
]
