"""Errors raised by the control layer."""

from __future__ import annotations


class ControlError(Exception):
    """Base class for a control-layer failure."""


class InputError(ControlError):
    """The input device could not synthesise a tap."""


class TapTargetNotFound(ControlError):
    """A label was not present in the frame it was supposed to be tapped from."""


class PopupSpecError(ControlError):
    """The popup corpus is missing, malformed or references a missing artefact."""


class PopupUnclosable(ControlError):
    """A popup has no executable close strategy (issue #11).

    Raised instead of tapping a guessed coordinate. The run must stop with this
    named failure so the corpus can be corrected.
    """
