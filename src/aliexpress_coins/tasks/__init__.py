"""Task specs and the default-deny policy (issue #21).

The catalogue is data: :func:`~aliexpress_coins.tasks.catalogue.load_task_catalogue`
turns ``specs/tasks.yaml`` into typed specs and refuses every label it does not
recognise. The dispatch point (#22/#38/#20) calls
:meth:`~aliexpress_coins.tasks.catalogue.TaskCatalogue.decide` before an
executor sees a row.
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
from aliexpress_coins.tasks.errors import TaskCatalogueError, TaskError

__all__ = [
    'AllowedTaskSpec',
    'DecisionKind',
    'DeniedTaskSpec',
    'TaskCatalogue',
    'TaskCatalogueError',
    'TaskCategory',
    'TaskDecision',
    'TaskError',
    'load_task_catalogue',
]
