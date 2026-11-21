"""Task specs, the default-deny policy and task discovery (issues #21 and #22).

The catalogue is data: :func:`~aliexpress_coins.tasks.catalogue.load_task_catalogue`
turns ``specs/tasks.yaml`` into typed specs and refuses every label it does not
recognise. The dispatch point (#22/#38/#20) calls
:meth:`~aliexpress_coins.tasks.catalogue.TaskCatalogue.decide` before an
executor sees a row.

Discovery lives alongside it: :func:`~aliexpress_coins.tasks.discovery.open_boost_list`
opens the full list (or degrades to the coin page's inline rows), and
:func:`~aliexpress_coins.tasks.discovery.enumerate_roster` turns one frame into
an ordered roster of allowed and refused entries.
"""

from aliexpress_coins.tasks.catalogue import (
    AllowedTaskSpec,
    DecisionKind,
    DeniedTaskSpec,
    TaskCatalogue,
    TaskCategory,
    TaskDecision,
    load_task_catalogue,
)
from aliexpress_coins.tasks.discovery import (
    BoostList,
    BoostOpenResult,
    DiscoveryResult,
    DiscoveryRoute,
    Roster,
    RosterEntry,
    RosterStatus,
    discover_roster,
    enumerate_roster,
    is_boost_page,
    is_empty_boost,
    open_boost_list,
)
from aliexpress_coins.tasks.errors import TaskCatalogueError, TaskError

__all__ = [
    'AllowedTaskSpec',
    'BoostList',
    'BoostOpenResult',
    'DecisionKind',
    'DeniedTaskSpec',
    'DiscoveryResult',
    'DiscoveryRoute',
    'Roster',
    'RosterEntry',
    'RosterStatus',
    'TaskCatalogue',
    'TaskCatalogueError',
    'TaskCategory',
    'TaskDecision',
    'TaskError',
    'discover_roster',
    'enumerate_roster',
    'is_boost_page',
    'is_empty_boost',
    'load_task_catalogue',
    'open_boost_list',
]
