"""Errors raised by the runner layer."""

from __future__ import annotations


class RunnerError(Exception):
    """Base class for a runner-layer failure."""


class StateError(RunnerError):
    """The daily state could not be used as asked.

    This covers a caller mistake (an empty task key or a non-numeric balance),
    a state directory that cannot be created, and a state file whose contents
    cannot be read. A *corrupt* file is deliberately not an error: it is moved
    aside and the day starts fresh, because losing a night to a half-written
    file is the failure this module exists to prevent.
    """


class StateReadError(StateError):
    """A state file exists but could not be read from disk.

    Permission and device failures are environmental; unlike invalid JSON they
    are not evidence that the file is expendable, so they are reported.
    """


class StateWriteError(StateError):
    """A state update could not be committed atomically.

    The in-memory state is left unchanged: a caller that sees this error knows
    the previous state is still the one on disk.
    """
