"""Parameterized enabled_pinned reconciliation for typed definition managers."""

from __future__ import annotations

import time
from collections.abc import Callable
from threading import Event, Thread
from typing import Any, Protocol

import psycopg
import pytest
from psycopg import sql

from gobby.storage.definitions import (
    AgentDefinitionManager,
    PipelineDefinitionManager,
    RuleDefinitionManager,
    SessionVariableDefaultManager,
)
from gobby.storage.hub.postgres import PostgresHubDatabase


class DefinitionManager(Protocol):
    def get(self, definition_id: str, include_deleted: bool = False) -> Any: ...

    def update(self, definition_id: str, **fields: Any) -> Any: ...

    def toggle_enabled(self, definition_id: str) -> Any: ...

    def update_from_sync(self, definition_id: str, **fields: Any) -> Any: ...


def _rule_factory(db: PostgresHubDatabase) -> tuple[DefinitionManager, str]:
    manager = RuleDefinitionManager(db)
    row = manager.create(name="rule", definition_json={"event": "Stop"}, enabled=True)
    return manager, row.id


def _variable_factory(db: PostgresHubDatabase) -> tuple[DefinitionManager, str]:
    manager = SessionVariableDefaultManager(db)
    row = manager.create(name="var", default_value="a", enabled=True)
    return manager, row.id


def _pipeline_factory(db: PostgresHubDatabase) -> tuple[DefinitionManager, str]:
    manager = PipelineDefinitionManager(db)
    row = manager.create(name="pipe", definition_json={"steps": []}, enabled=True)
    return manager, row.id


def _agent_factory(db: PostgresHubDatabase) -> tuple[DefinitionManager, str]:
    manager = AgentDefinitionManager(db)
    row = manager.create(name="agent", definition_json={"name": "agent"}, enabled=True)
    return manager, row.id


_FACTORIES = [_rule_factory, _variable_factory, _pipeline_factory, _agent_factory]
_FACTORY_IDS = ["rules", "variables", "pipelines", "agents"]


def _set_search_path(conn: psycopg.Connection[object], schema: str) -> None:
    conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))


def _wait_for_row_lock_waiter(
    postgres_database_url: str,
    finished: Event,
    table: str,
    *,
    timeout: float = 5.0,
) -> None:
    deadline = time.monotonic() + timeout
    with psycopg.connect(postgres_database_url) as monitor:
        while time.monotonic() < deadline:
            if finished.is_set():
                raise AssertionError("sync finished before acquiring the row lock")
            row = monitor.execute(
                """
                SELECT count(*)
                FROM pg_stat_activity
                WHERE wait_event_type = 'Lock'
                  AND query ILIKE %s
                """,
                (f"%{table}%",),
            ).fetchone()
            if row is not None and int(row[0]) >= 1:
                return
            time.sleep(0.02)
    raise AssertionError(f"timed out waiting for a {table} row lock waiter")


def _update_from_sync(manager: DefinitionManager, definition_id: str) -> None:
    manager.update_from_sync(
        definition_id,
        enabled=False,
        description="from-template",
    )


def _agent_upsert_from_sync(manager: DefinitionManager, definition_id: str) -> None:
    assert isinstance(manager, AgentDefinitionManager)
    assert manager.get(definition_id).name == "agent"
    manager.upsert_from_sync(
        name="agent",
        body_json={"name": "agent"},
        step_workflow=None,
        enabled=False,
        description="from-template",
    )


_CONCURRENT_CASES = [
    (_rule_factory, RuleDefinitionManager, "rule_definitions", _update_from_sync),
    (
        _variable_factory,
        SessionVariableDefaultManager,
        "session_variable_defaults",
        _update_from_sync,
    ),
    (_agent_factory, AgentDefinitionManager, "agent_definitions", _update_from_sync),
    (_agent_factory, AgentDefinitionManager, "agent_definitions", _agent_upsert_from_sync),
]
_CONCURRENT_CASE_IDS = ["rules", "variables", "agents-update", "agents-upsert"]


@pytest.mark.parametrize(
    "factory",
    _FACTORIES,
    ids=_FACTORY_IDS,
)
def test_user_update_and_toggle_stamp_enabled_pinned(
    definition_db: PostgresHubDatabase,
    factory: Callable[[PostgresHubDatabase], tuple[DefinitionManager, str]],
) -> None:
    manager, definition_id = factory(definition_db)
    updated = manager.update(definition_id, enabled=False)
    assert updated.enabled is False
    assert updated.enabled_pinned is True

    toggled = manager.toggle_enabled(definition_id)
    assert toggled.enabled is True
    assert toggled.enabled_pinned is True


@pytest.mark.parametrize(
    "factory",
    _FACTORIES,
    ids=_FACTORY_IDS,
)
def test_sync_adopts_template_enabled_while_unpinned(
    definition_db: PostgresHubDatabase,
    factory: Callable[[PostgresHubDatabase], tuple[DefinitionManager, str]],
) -> None:
    manager, definition_id = factory(definition_db)
    synced = manager.update_from_sync(definition_id, enabled=False)
    assert synced.enabled is False
    assert synced.enabled_pinned is False


@pytest.mark.parametrize(
    "factory",
    _FACTORIES,
    ids=_FACTORY_IDS,
)
def test_sync_preserves_pinned_enabled_even_when_equal_to_template(
    definition_db: PostgresHubDatabase,
    factory: Callable[[PostgresHubDatabase], tuple[DefinitionManager, str]],
) -> None:
    manager, definition_id = factory(definition_db)
    manager.update(definition_id, enabled=True)
    pinned = manager.get(definition_id)
    assert pinned.enabled_pinned is True
    synced = manager.update_from_sync(definition_id, enabled=False)
    assert synced.enabled is True
    assert synced.enabled_pinned is True


@pytest.mark.parametrize(
    ("factory", "manager_factory", "table", "sync"),
    _CONCURRENT_CASES,
    ids=_CONCURRENT_CASE_IDS,
)
def test_sync_does_not_override_concurrent_user_pin(
    definition_db: PostgresHubDatabase,
    postgres_database_url: str,
    factory: Callable[[PostgresHubDatabase], tuple[DefinitionManager, str]],
    manager_factory: Callable[[PostgresHubDatabase], DefinitionManager],
    table: str,
    sync: Callable[[DefinitionManager, str], None],
) -> None:
    manager, definition_id = factory(definition_db)
    assert manager.get(definition_id).enabled_pinned is False
    schema_row = definition_db.fetchone("SELECT current_schema() AS schema")
    assert schema_row is not None
    schema = str(schema_row["schema"])
    writer_db = PostgresHubDatabase(definition_db.conninfo)
    writer_db.open()
    writer_manager = manager_factory(writer_db)
    assert writer_manager.get(definition_id).enabled is True
    finished = Event()
    errors: list[Exception] = []

    def run_sync() -> None:
        try:
            sync(writer_manager, definition_id)
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    writer = Thread(target=run_sync, daemon=True)
    try:
        with psycopg.connect(postgres_database_url) as holder:
            _set_search_path(holder, schema)
            holder.execute("BEGIN")
            try:
                locked = holder.execute(
                    sql.SQL("SELECT id FROM {} WHERE id = %s FOR UPDATE").format(
                        sql.Identifier(table)
                    ),
                    (definition_id,),
                ).fetchone()
                assert locked is not None
                writer.start()
                _wait_for_row_lock_waiter(postgres_database_url, finished, table)
                holder.execute(
                    sql.SQL(
                        """
                        UPDATE {}
                        SET enabled = TRUE, enabled_pinned = TRUE, updated_at = now()
                        WHERE id = %s
                        """
                    ).format(sql.Identifier(table)),
                    (definition_id,),
                )
                holder.execute("COMMIT")
            except Exception:
                holder.execute("ROLLBACK")
                raise
        assert finished.wait(timeout=5)
        writer.join(timeout=1)
        if errors:
            raise errors[0]
    finally:
        writer_db.close()

    row = manager.get(definition_id)
    assert row.enabled is True
    assert row.enabled_pinned is True
    assert row.description == "from-template"
