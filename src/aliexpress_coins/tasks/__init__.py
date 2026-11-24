"""Task specs, the default-deny policy, discovery and the task surface (#21, #22, #38).

The catalogue is data: :func:`~aliexpress_coins.tasks.catalogue.load_task_catalogue`
turns ``specs/tasks.yaml`` into typed specs and refuses every label it does not
recognise. The dispatch point (#22/#38/#20) calls
:meth:`~aliexpress_coins.tasks.catalogue.TaskCatalogue.decide` before an
executor sees a row.

Discovery lives alongside it: :func:`~aliexpress_coins.tasks.discovery.open_boost_list`
opens the full list (or degrades to the coin page's inline rows), and
:func:`~aliexpress_coins.tasks.discovery.enumerate_roster` turns one frame into
an ordered roster of allowed and refused entries. :func:`classify_surface`
names the variant on arrival, and
:func:`~aliexpress_coins.tasks.discovery.discover_roster` applies the one-refresh
route policy. :func:`~aliexpress_coins.tasks.discovery.observe_roster` re-reads
the surface after a completion and diffs it against the previous observation;
:class:`~aliexpress_coins.tasks.discovery.RosterObserver` keeps that timeline in
``<run>/roster.jsonl`` and reports unrecognised titles as catalogue candidates.
"""

from aliexpress_coins.tasks.catalogue import (
    AllowedTaskSpec,
    ContaminatedMatch,
    DecisionKind,
    DeniedTaskSpec,
    TaskCatalogue,
    TaskCategory,
    TaskDecision,
    TaskPolicyContext,
    load_task_catalogue,
)
from aliexpress_coins.tasks.discovery import (
    BoostList,
    BoostOpenResult,
    DiscoveryResult,
    DiscoveryRoute,
    Roster,
    RosterChange,
    RosterDiff,
    RosterEntry,
    RosterLog,
    RosterObservation,
    RosterObserver,
    RosterRow,
    RosterStatus,
    diff_rosters,
    discover_roster,
    enumerate_roster,
    is_boost_page,
    is_empty_boost,
    observe_roster,
    open_boost_list,
    read_roster_log,
)
from aliexpress_coins.tasks.errors import RosterLogError, TaskCatalogueError, TaskError
from aliexpress_coins.tasks.surface import (
    SHORTCUT_DEEP_LINK,
    SURFACE_REFRESH_LIMIT,
    RouteFirer,
    Surface,
    SurfaceRoute,
    classify_surface,
)

__all__ = [
    'SHORTCUT_DEEP_LINK',
    'SURFACE_REFRESH_LIMIT',
    'AllowedTaskSpec',
    'BoostList',
    'BoostOpenResult',
    'ContaminatedMatch',
    'DecisionKind',
    'DeniedTaskSpec',
    'DiscoveryResult',
    'DiscoveryRoute',
    'Roster',
    'RosterChange',
    'RosterDiff',
    'RosterEntry',
    'RosterLog',
    'RosterLogError',
    'RosterObservation',
    'RosterObserver',
    'RosterRow',
    'RosterStatus',
    'RouteFirer',
    'Surface',
    'SurfaceRoute',
    'TaskCatalogue',
    'TaskCatalogueError',
    'TaskCategory',
    'TaskDecision',
    'TaskError',
    'TaskPolicyContext',
    'classify_surface',
    'diff_rosters',
    'discover_roster',
    'enumerate_roster',
    'is_boost_page',
    'is_empty_boost',
    'load_task_catalogue',
    'observe_roster',
    'open_boost_list',
    'read_roster_log',
]
