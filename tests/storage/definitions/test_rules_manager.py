"""CRUD, scope, conflict, purge, listing, and revision tests for rules."""

from __future__ import annotations

from uuid import uuid4

import pytest

from gobby.storage.definitions import (
    DefinitionNameConflictError,
    DefinitionNotFoundError,
    RuleDefinitionManager,
    RuleDefinitionRow,
    bump_definitions_revision,
    get_definitions_revision,
    register_revision_listener,
)
from gobby.storage.definitions.variables import SessionVariableDefaultManager
from gobby.storage.hub.postgres import PostgresHubDatabase

_PROJECT = str(uuid4())


def _mgr(db: PostgresHubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


def _create(
    db: PostgresHubDatabase,
    *,
    name: str = "block-edit",
    project_id: str | None = None,
    event: str = "PreToolUse",
    group: str = "safety",
    priority: int = 10,
    enabled: bool = True,
) -> RuleDefinitionRow:
    return _mgr(db).create(
        name=name,
        definition_json={"event": event, "group": group, "action": "deny"},
        project_id=project_id,
        priority=priority,
        enabled=enabled,
        tags=["rule"],
        sources=["bundled"],
    )


def test_create_get_and_scope_fallback(definition_db: PostgresHubDatabase) -> None:
    manager = _mgr(definition_db)
    global_row = _create(definition_db, name="shared")
    project_row = _create(definition_db, name="shared", project_id=_PROJECT, event="Stop")

    assert manager.get(global_row.id).name == "shared"
    fetched = manager.get_by_name("shared", project_id=_PROJECT)
    assert fetched is not None
    assert fetched.id == project_row.id
    fallback = manager.get_by_name("missing", project_id=_PROJECT)
    assert fallback is None
    only_global = manager.get_by_name("shared")
    assert only_global is not None
    assert only_global.id == global_row.id


def test_same_domain_live_conflict(definition_db: PostgresHubDatabase) -> None:
    _create(definition_db, name="dup")
    with pytest.raises(DefinitionNameConflictError):
        _create(definition_db, name="dup")


def test_cross_domain_same_name_is_allowed(definition_db: PostgresHubDatabase) -> None:
    _create(definition_db, name="overlap")
    SessionVariableDefaultManager(definition_db).create(
        name="overlap",
        default_value="ok",
    )
    rule = _mgr(definition_db).get_by_name("overlap")
    variable = SessionVariableDefaultManager(definition_db).get_by_name("overlap")
    assert rule is not None
    assert variable is not None
    assert rule.id != variable.id


def test_restore_collision_and_purge(definition_db: PostgresHubDatabase) -> None:
    manager = _mgr(definition_db)
    first = _create(definition_db, name="recycle")
    assert manager.delete(first.id) is True
    replacement = _create(definition_db, name="recycle")
    with pytest.raises(DefinitionNameConflictError):
        manager.restore(first.id)
    assert manager.hard_delete(replacement.id) is True
    restored = manager.restore(first.id)
    assert restored.deleted_at is None
    manager.delete(restored.id)
    definition_db.execute(
        "UPDATE rule_definitions SET deleted_at = NOW() - INTERVAL '40 days' WHERE id = %s",
        (restored.id,),
    )
    assert manager.purge_deleted(older_than_days=30) == 1
    with pytest.raises(DefinitionNotFoundError):
        manager.get(restored.id, include_deleted=True)


def test_list_by_event_and_group_priority_order(definition_db: PostgresHubDatabase) -> None:
    manager = _mgr(definition_db)
    _create(definition_db, name="b-rule", event="PreToolUse", group="safety", priority=20)
    _create(definition_db, name="a-rule", event="PreToolUse", group="safety", priority=10)
    _create(definition_db, name="other", event="Stop", group="lifecycle", priority=1)

    by_event = manager.list_by_event("PreToolUse")
    assert [row.name for row in by_event] == ["a-rule", "b-rule"]
    by_group = manager.list_by_group("lifecycle")
    assert [row.name for row in by_group] == ["other"]


def test_move_scope_and_duplicate_precheck(definition_db: PostgresHubDatabase) -> None:
    manager = _mgr(definition_db)
    row = _create(definition_db, name="movable")
    moved = manager.move_to_project(row.id, _PROJECT)
    assert moved.project_id == _PROJECT
    assert moved.source == "project"
    globalized = manager.move_to_global(moved.id)
    assert globalized.project_id is None
    copy = manager.duplicate(globalized.id, "movable-copy")
    assert copy.name == "movable-copy"
    assert copy.source == "custom"
    with pytest.raises(DefinitionNameConflictError):
        manager.duplicate(globalized.id, "movable")


def test_missing_row_raises_typed_error(definition_db: PostgresHubDatabase) -> None:
    with pytest.raises(DefinitionNotFoundError):
        _mgr(definition_db).get(str(uuid4()))


def test_mutator_nested_rollback_leaves_revisions_untouched(
    definition_db: PostgresHubDatabase,
) -> None:
    manager = _mgr(definition_db)
    existing = _create(definition_db, name="stable")
    before_local = get_definitions_revision("rules")
    before_persistent = definition_db.fetchone(
        "SELECT revision FROM definition_revisions WHERE domain = %s",
        ("rules",),
    )
    fired: list[str] = []
    register_revision_listener("rules", lambda: fired.append("rules"))

    with pytest.raises(RuntimeError, match="outer rollback"):
        with definition_db.transaction():
            manager.create(
                name="ephemeral",
                definition_json={"event": "Stop"},
            )
            raise RuntimeError("outer rollback")

    assert get_definitions_revision("rules") == before_local
    after_persistent = definition_db.fetchone(
        "SELECT revision FROM definition_revisions WHERE domain = %s",
        ("rules",),
    )
    assert after_persistent == before_persistent
    assert fired == []
    assert manager.get_by_name("ephemeral") is None
    assert manager.get(existing.id).name == "stable"


def test_mutator_that_raises_after_write_start_does_not_bump(
    definition_db: PostgresHubDatabase,
) -> None:
    manager = _mgr(definition_db)
    _create(definition_db, name="taken")
    before_local = get_definitions_revision("rules")
    fired: list[str] = []
    register_revision_listener("rules", lambda: fired.append("rules"))

    with pytest.raises(DefinitionNameConflictError):
        manager.create(name="taken", definition_json={"event": "Stop"})

    assert get_definitions_revision("rules") == before_local
    assert fired == []
    bump_definitions_revision("agents")
    assert get_definitions_revision("rules") == before_local
