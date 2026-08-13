"""Typed definition storage: domain tables, revisions, and managers."""

from gobby.storage.definitions._shared import (
    DefinitionNameConflictError,
    DefinitionNotFoundError,
    compute_definition_hash,
)
from gobby.storage.definitions.notifications import DefinitionRevisionListener
from gobby.storage.definitions.pipelines import (
    PipelineDefinitionManager,
    PipelineDefinitionRow,
)
from gobby.storage.definitions.revisions import (
    DEFINITION_DOMAINS,
    NOTIFY_CHANNEL,
    DefinitionDomain,
    advance_persistent_revision,
    bump_definitions_revision,
    fetch_persistent_revisions,
    get_definitions_revision,
    register_revision_listener,
    reset_definition_revision_state,
)
from gobby.storage.definitions.rules import RuleDefinitionManager, RuleDefinitionRow
from gobby.storage.definitions.variables import (
    SessionVariableDefaultManager,
    SessionVariableDefaultRow,
)

__all__ = [
    "DEFINITION_DOMAINS",
    "NOTIFY_CHANNEL",
    "DefinitionDomain",
    "DefinitionNameConflictError",
    "DefinitionNotFoundError",
    "DefinitionRevisionListener",
    "PipelineDefinitionManager",
    "PipelineDefinitionRow",
    "RuleDefinitionManager",
    "RuleDefinitionRow",
    "SessionVariableDefaultManager",
    "SessionVariableDefaultRow",
    "advance_persistent_revision",
    "bump_definitions_revision",
    "compute_definition_hash",
    "fetch_persistent_revisions",
    "get_definitions_revision",
    "register_revision_listener",
    "reset_definition_revision_state",
]
