"""Errors raised by the task layer."""

from __future__ import annotations


class TaskError(Exception):
    """Base class for a task-layer failure."""


class TaskCatalogueError(TaskError):
    """The task catalogue is missing, malformed or refuses one of its own rows.

    Raised while the catalogue is loaded, before a run can touch the device. A
    bad catalogue is a configuration error, not a mid-run surprise: the nightly
    run must never be the first place an unknown category or a contradictory
    deny rule is discovered.
    """
