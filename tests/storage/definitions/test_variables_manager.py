"""CRUD, scope, conflict, purge, and defaults-map tests for variables."""

from __future__ import annotations

from uuid import uuid4

import pytest

from gobby.storage.definitions import (
    DefinitionNameConflictError,
    DefinitionNotFoundError,
    SessionVariableDefaultManager,
)
from gobby.storage.hub.postgres import PostgresHubDatabase

_PROJECT = str(uuid4())


def _mgr(db: PostgresHubDatabase) -> SessionVariableDefaultManager:
    return SessionVariableDefaultManager(db)


def test_create_get_scope_fallback_and_defaults_map(
    definition_db: PostgresHubDatabase,
) -> None:
    manager = _mgr(definition_db)
    manager.create(name="model", default_value="global-model", enabled=True)
    manager.create(
        name="model",
        default_value="project-model",
        project_id=_PROJECT,
        enabled=True,
    )
    manager.create(name="quiet", default_value=True, enabled=False)

    fetched = manager.get_by_name("model", project_id=_PROJECT)
    assert fetched is not None
    assert fetched.default_value == "project-model"

    scoped = manager.get_defaults_map(project_id=_PROJECT, enabled_only=True)
    assert scoped == {"model": "project-model"}
    globals_only = manager.get_defaults_map(enabled_only=True)
    assert globals_only == {"model": "global-model"}
    including_disabled = manager.get_defaults_map(enabled_only=False)
    assert including_disabled["quiet"] is True


def test_same_domain_conflict_restore_and_purge(definition_db: PostgresHubDatabase) -> None:
    manager = _mgr(definition_db)
    first = manager.create(name="temp", default_value=1)
    with pytest.raises(DefinitionNameConflictError):
        manager.create(name="temp", default_value=2)
    assert manager.delete(first.id) is True
    replacement = manager.create(name="temp", default_value=3)
    with pytest.raises(DefinitionNameConflictError):
        manager.restore(first.id)
    manager.hard_delete(replacement.id)
    restored = manager.restore(first.id)
    assert restored.default_value == 1
    manager.delete(restored.id)
    definition_db.execute(
        "UPDATE session_variable_defaults SET deleted_at = NOW() - INTERVAL '40 days' "
        "WHERE id = %s",
        (restored.id,),
    )
    assert manager.purge_deleted(older_than_days=30) == 1
    with pytest.raises(DefinitionNotFoundError):
        manager.get(restored.id, include_deleted=True)


def test_variables_have_no_duplicate(definition_db: PostgresHubDatabase) -> None:
    manager = _mgr(definition_db)
    manager.create(name="once", default_value="x")
    assert not hasattr(manager, "duplicate")
