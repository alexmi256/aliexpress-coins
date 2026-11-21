"""Typed failures raised by the perception layer.

Callers can distinguish "the frame was bad" from "the OCR engine failed" from
"the artifact could not be written" without parsing message strings. The base
class keeps a single catch point for the runner.
"""

from __future__ import annotations


class PerceptionError(Exception):
    """Base class for every failure the perception layer reports."""


class SnapshotError(PerceptionError):
    """A frame could not be captured, decoded or cropped."""


class OcrError(PerceptionError):
    """The OCR engine could not read a frame."""


class ArtifactError(PerceptionError):
    """A snapshot or its sidecar could not be written or reloaded."""


class StateTimeout(PerceptionError):
    """A requested screen state was not observed before the deadline."""
