"""The single dispatch gate and its refusal audit line (issue #20).

Every task row reaches an executor through :class:`DispatchGate`. The gate does
exactly four things, in this order:

1. **resolves** the row against the catalogue again (the discovery decision is a
   candidate, not a licence);
2. **records** the allow/deny decision as a structured event;
3. **refuses** anything that is not allowed, with a stable code and an audit
   line carrying the label, rule, reason and surface;
4. **mints** a sealed :class:`~aliexpress_coins.tasks.catalogue.DispatchAuthorization`
   for an allowed row and attaches it to the entry.

The token is the reachability guard. Every executor entry point requires an
authorized row (or an authorization token), so calling an executor directly with
a freshly enumerated row fails before a capture or a tap. A raw ``RosterEntry``
is a candidate; only the gate can turn it into a dispatch.

Refusal is the default: a label that matches no allowed row is refused as
``unrecognised``. A matched deny rule uses that rule's key as the refusal code,
which makes the eleven-plus refused families distinguishable in the audit line
without inventing another taxonomy.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace

from aliexpress_coins.tasks.catalogue import (
    DEFAULT_DENY_REASON,
    DecisionKind,
    DispatchAuthorization,
    TaskCatalogue,
    TaskDecision,
)
from aliexpress_coins.tasks.discovery import RosterEntry
from aliexpress_coins.tasks.errors import TaskError

__all__ = [
    'DispatchGate',
    'PolicyRefusal',
    'PolicyRefused',
    'authorization_of',
]


@dataclass(frozen=True, slots=True)
class PolicyRefusal:
    """One refused row, in the shape the audit line and report use."""

    code: str
    label: str
    subtitle: str | None
    rule: str | None
    reason: str
    surface: str | None = None
    matched_pattern: str | None = None
    matched_field: str | None = None

    @classmethod
    def from_decision(cls, decision: TaskDecision, *, surface: str | None) -> PolicyRefusal:
        """Project a catalogue decision onto the refusal record."""
        return cls(
            code=decision.rule or decision.reason or DEFAULT_DENY_REASON,
            label=decision.title,
            subtitle=decision.subtitle,
            rule=decision.rule,
            reason=decision.reason or DEFAULT_DENY_REASON,
            surface=surface,
            matched_pattern=decision.matched_pattern,
            matched_field=decision.matched_field,
        )

    @property
    def identity(self) -> tuple[str, str]:
        """The dedupe key: one audit line per label and refusal code per run."""
        return self.label, self.code

    def as_dict(self) -> dict[str, object]:
        """The complete structured event; no executor sees this row."""
        return {
            'event': 'policy_refusal',
            'code': self.code,
            'label': self.label,
            'subtitle': self.subtitle,
            'rule': self.rule,
            'reason': self.reason,
            'surface': self.surface,
            'matched_pattern': self.matched_pattern,
            'matched_field': self.matched_field,
        }


class PolicyRefused(TaskError):
    """The gate refused a row; the caller must not dispatch it."""

    def __init__(self, refusal: PolicyRefusal) -> None:
        super().__init__(f'{refusal.label!r} refused by {refusal.code}: {refusal.reason}')
        self.refusal = refusal


class DispatchGate:
    """Resolve, record and authorise one row before it becomes an action."""

    def __init__(
        self,
        catalogue: TaskCatalogue,
        *,
        logger: logging.Logger | None = None,
        surface: str | None = None,
    ) -> None:
        self.catalogue = catalogue
        self.surface = surface
        self._logger = logger if logger is not None else logging.getLogger(__name__)
        self._sequence = 0
        self._refused: set[tuple[str, str]] = set()

    def authorize(self, entry: RosterEntry) -> RosterEntry:
        """Return an authorized copy of ``entry``, or raise :class:`PolicyRefused`.

        The catalogue is consulted again rather than trusting the discovery
        decision. That keeps one answer authoritative even if a roster is built
        or mutated between enumeration and dispatch.
        """
        decision = self.catalogue.decide(entry.title, subtitle=entry.subtitle)
        self._sequence += 1
        if decision.decision is not DecisionKind.ALLOW or decision.spec is None:
            refusal = PolicyRefusal.from_decision(decision, surface=self.surface)
            self._record_refusal(refusal)
            raise PolicyRefused(refusal)

        spec = decision.spec
        authorization = DispatchAuthorization._mint(
            key=spec.key,
            title=decision.title,
            category=spec.category,
            catalogue_version=self.catalogue.version,
            rule=spec.key,
            surface=self.surface,
            sequence=self._sequence,
        )
        authorized = replace(entry, decision=decision, authorization=authorization)
        self._logger.info(
            '%s',
            json.dumps(
                {
                    'event': 'policy_decision',
                    'decision': 'allow',
                    'label': decision.title,
                    'subtitle': decision.subtitle,
                    'key': spec.key,
                    'category': spec.category.value,
                    'rule': spec.key,
                    'reason': None,
                    'surface': self.surface,
                },
                separators=(',', ':'),
                sort_keys=True,
            ),
        )
        return authorized

    def refusal_for(self, entry: RosterEntry) -> PolicyRefusal | None:
        """Audit a refused candidate without raising; ``None`` when allowed.

        The live loop records a policy skip for every denied row it observes.
        This helper gives that path the same audit line as a dispatch attempt,
        while keeping the executor path exception-based.
        """
        try:
            self.authorize(entry)
        except PolicyRefused as exc:
            return exc.refusal
        return None

    def _record_refusal(self, refusal: PolicyRefusal) -> None:
        if refusal.identity in self._refused:
            return
        self._refused.add(refusal.identity)
        self._logger.warning('%s', json.dumps(refusal.as_dict(), separators=(',', ':'), sort_keys=True))


def authorization_of(entry: RosterEntry) -> DispatchAuthorization:
    """Return the gate's token for ``entry`` or fail before any executor work.

    Executors call this instead of trusting ``entry.allowed``. The check is
    deliberately about the *token*, not only the catalogue decision: a row that
    was enumerated but never dispatched has no token.
    """
    token = entry.authorization
    if token is None or not entry.allowed or not entry.authorized:
        raise TaskError(
            f'{entry.title!r} is not dispatchable: no dispatch authorization (the policy gate was bypassed)'
        )
    if token.category is not entry.category:  # pragma: no cover - covers() already proves this
        raise TaskError(f'{entry.title!r} is not dispatchable: its authorization names another category')
    return token
