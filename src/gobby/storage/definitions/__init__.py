"""Typed definition storage: domain tables, revisions, and managers."""

from gobby.storage.definitions.notifications import DefinitionRevisionListener
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

__all__ = [
    "DEFINITION_DOMAINS",
    "NOTIFY_CHANNEL",
    "DefinitionDomain",
    "DefinitionRevisionListener",
    "advance_persistent_revision",
    "bump_definitions_revision",
    "fetch_persistent_revisions",
    "get_definitions_revision",
    "register_revision_listener",
    "reset_definition_revision_state",
]
