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
from aliexpress_coins.control.errors import (
    ControlError,
    InputError,
    PopupSpecError,
    PopupUnclosable,
    TapTargetNotFound,
)
from aliexpress_coins.control.popups import (
    TEMPLATE_PREFIX,
    CloseAttempt,
    CloseKind,
    ObservedGeometry,
    PopupCorpus,
    PopupFamily,
    PopupStatus,
    ReferenceGeometry,
    load_popup_corpus,
)

__all__ = [
    'LOGGER_NAME',
    'MAX_JITTER_FRACTION',
    'SAFE_INSET_FRACTION',
    'TEMPLATE_PREFIX',
    'AdbInputDevice',
    'CloseAttempt',
    'CloseKind',
    'ControlError',
    'Controller',
    'Frame',
    'InputDevice',
    'InputError',
    'InputResult',
    'InputRunner',
    'JitterSource',
    'ObservedGeometry',
    'Pixel',
    'Point',
    'PopupCorpus',
    'PopupFamily',
    'PopupSpecError',
    'PopupStatus',
    'PopupUnclosable',
    'ReferenceGeometry',
    'SubprocessInputRunner',
    'TapRecord',
    'TapTargetNotFound',
    'clamp_point',
    'jitter_radius',
    'load_popup_corpus',
    'safe_inset',
]
