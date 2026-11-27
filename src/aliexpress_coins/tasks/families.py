"""Cross-day evidence about a catalogue family that no read has shown (#161).

A rotating sheet makes one night's absence ambiguous: the family may be behind
another rotation, or the app may simply not be offering it. This value records
the evidence over days (one nightly observation per local day) so the run report
can tell those apart without ever forgetting the family:

* ``unread`` still means the read was incomplete and the night stays open;
* ``not_offered`` means the family has been absent for the configured number of
  consecutive observed days and is now dormant.

Dormancy is deliberately visible: ``unseen_since`` says when the current absence
began, and ``missed_days`` plus ``samples`` say how much evidence supports it.
A sighting clears every one of those fields. There is no separate retry timer:
the sheet is read every night, so a returning row is caught on the next run and
clears dormancy immediately.

The type is kept in ``tasks`` so both discovery's :class:`~.discovery.Roster`
and the runner's day state can carry it without a layering cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

__all__ = ['FamilyEvidence']


@dataclass(frozen=True, slots=True)
class FamilyEvidence:
    """What the collector has observed about one catalogue family across nights."""

    key: str
    last_seen: date | None = None
    unseen_since: date | None = None
    missed_days: int = 0
    samples: int = 0
    dormant_since: date | None = None
    last_checked: date | None = None

    @property
    def dormant(self) -> bool:
        """Whether the family has crossed the configured absence threshold."""
        return self.dormant_since is not None

    def as_dict(self) -> dict[str, Any]:
        """Render the evidence for ``run.json`` and the daily state."""
        return {
            'key': self.key,
            'last_seen': None if self.last_seen is None else self.last_seen.isoformat(),
            'unseen_since': None if self.unseen_since is None else self.unseen_since.isoformat(),
            'missed_days': self.missed_days,
            'samples': self.samples,
            'dormant': self.dormant,
            'dormant_since': None if self.dormant_since is None else self.dormant_since.isoformat(),
            'last_checked': None if self.last_checked is None else self.last_checked.isoformat(),
        }
