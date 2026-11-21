"""Errors raised by the control layer."""

from __future__ import annotations


class ControlError(Exception):
    """Base class for a control-layer failure."""


class InputError(ControlError):
    """The input device could not synthesise a tap."""


class TapTargetNotFound(ControlError):
    """A label was not present in the frame it was supposed to be tapped from."""
