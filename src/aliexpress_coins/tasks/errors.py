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


class RosterLogError(TaskError):
    """The roster observation log could not be appended to or read back.

    The log is a run artifact, not run state: losing a line must be reported,
    but it must not turn a completed task into a failed run. The observer logs
    the failure and keeps the previous roster.
    """
