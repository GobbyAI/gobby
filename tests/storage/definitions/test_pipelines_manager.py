"""CRUD, duplicate, scope-move, and canvas/version tests for pipelines."""

from __future__ import annotations

import json
import time
from threading import Event, Thread
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from gobby.storage.definitions import (
    DefinitionNameConflictError,
    PipelineDefinitionManager,
)
from gobby.storage.hub.postgres import PostgresHubDatabase

_PROJECT = str(uuid4())


def _mgr(db: PostgresHubDatabase) -> PipelineDefinitionManager:
    return PipelineDefinitionManager(db)


def test_crud_duplicate_scope_and_canvas_version(definition_db: PostgresHubDatabase) -> None:
    manager = _mgr(definition_db)
    created = manager.create(
        name="lint",
        definition_json={"steps": [{"run": "ruff"}]},
        version="1.0",
        canvas_json={"x": 1},
        tags=["ci"],
    )
    assert created.version == "1.0"
    assert created.canvas_json == {"x": 1}

    updated = manager.update(
        created.id,
        version="2.0",
        canvas_json={"x": 2},
        description="lint pipeline",
    )
    assert updated.version == "2.0"
    assert updated.canvas_json == {"x": 2}
    assert updated.description == "lint pipeline"

    moved = manager.move_to_project(updated.id, _PROJECT)
    assert moved.project_id == _PROJECT
    copy = manager.duplicate(moved.id, "lint-copy")
    assert copy.name == "lint-copy"
    assert copy.definition_json["name"] == "lint-copy"
    assert copy.project_id == _PROJECT
    assert copy.canvas_json == {"x": 2}
    with pytest.raises(DefinitionNameConflictError):
        manager.duplicate(moved.id, "lint")
    globalized = manager.move_to_global(moved.id)
    assert globalized.project_id is None


def test_pipeline_live_conflict_and_restore(definition_db: PostgresHubDatabase) -> None:
    manager = _mgr(definition_db)
    first = manager.create(name="build", definition_json={"steps": []})
    with pytest.raises(DefinitionNameConflictError):
        manager.create(name="build", definition_json={"steps": [{"run": "x"}]})
    manager.delete(first.id)
    replacement = manager.create(name="build", definition_json={"steps": [{"run": "y"}]})
    with pytest.raises(DefinitionNameConflictError):
        manager.restore(first.id)
    manager.hard_delete(replacement.id)
    restored = manager.restore(first.id)
    assert restored.definition_json == {"name": "build", "steps": []}


def test_create_and_rename_rewrite_payload_name(definition_db: PostgresHubDatabase) -> None:
    manager = _mgr(definition_db)
    created = manager.create(
        name="lint",
        definition_json={"name": "stale-name", "steps": [{"run": "ruff"}]},
    )
    assert created.definition_json["name"] == "lint"
    renamed = manager.update(created.id, name="lint-v2")
    assert renamed.name == "lint-v2"
    assert renamed.definition_json["name"] == "lint-v2"


_NEW_STEPS = [{"run": "ruff-new"}]
_OLD_STEPS = [{"run": "ruff"}]


def _set_search_path(conn: psycopg.Connection[object], schema: str) -> None:
    conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))


def _wait_for_row_lock_waiter(
    postgres_database_url: str,
    finished: Event,
    writer_application_name: str,
    *,
    timeout: float = 5.0,
) -> None:
    deadline = time.monotonic() + timeout
    with psycopg.connect(postgres_database_url) as monitor:
        while time.monotonic() < deadline:
            if finished.is_set():
                raise AssertionError("writer finished before acquiring the row lock")
            row = monitor.execute(
                """
                SELECT count(*)
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
                  AND application_name = %s
                  AND wait_event_type = 'Lock'
                """,
                (writer_application_name,),
            ).fetchone()
            if row is not None and int(row[0]) >= 1:
                return
            time.sleep(0.02)
    raise AssertionError("timed out waiting for a pipeline row lock waiter")


@pytest.mark.parametrize("holder_change", ("content", "rename"))
def test_concurrent_rename_and_content_update_both_orderings(
    definition_db: PostgresHubDatabase,
    postgres_database_url: str,
    holder_change: str,
) -> None:
    created = _mgr(definition_db).create(
        name="lint",
        definition_json={"steps": _OLD_STEPS},
    )
    schema_row = definition_db.fetchone("SELECT current_schema() AS schema")
    assert schema_row is not None
    schema = str(schema_row["schema"])
    writer_db = PostgresHubDatabase(definition_db.conninfo)
    writer_db.open()
    assert _mgr(writer_db).get(created.id).name == "lint"
    writer_fields: dict[str, Any] = (
        {"name": "lint-v2"}
        if holder_change == "content"
        else {"definition_json": {"steps": _NEW_STEPS}}
    )
    finished = Event()
    errors: list[Exception] = []

    def run_writer() -> None:
        try:
            _mgr(writer_db).update(created.id, **writer_fields)
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    writer = Thread(target=run_writer, daemon=True)
    try:
        with psycopg.connect(postgres_database_url) as holder:
            _set_search_path(holder, schema)
            holder.execute("BEGIN")
            try:
                locked = holder.execute(
                    "SELECT id FROM pipeline_definitions WHERE id = %s FOR UPDATE",
                    (created.id,),
                ).fetchone()
                assert locked is not None
                writer.start()
                _wait_for_row_lock_waiter(
                    postgres_database_url,
                    finished,
                    writer_db.application_name,
                )
                if holder_change == "content":
                    holder.execute(
                        """
                        UPDATE pipeline_definitions
                        SET definition_json = %s::jsonb, updated_at = now()
                        WHERE id = %s
                        """,
                        (json.dumps({"name": "lint", "steps": _NEW_STEPS}), created.id),
                    )
                else:
                    holder.execute(
                        """
                        UPDATE pipeline_definitions
                        SET name = %s, definition_json = %s::jsonb, updated_at = now()
                        WHERE id = %s
                        """,
                        (
                            "lint-v2",
                            json.dumps({"name": "lint-v2", "steps": _OLD_STEPS}),
                            created.id,
                        ),
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

    row = _mgr(definition_db).get(created.id)
    assert row.name == "lint-v2"
    assert row.definition_json["name"] == "lint-v2"
    assert row.definition_json["steps"] == _NEW_STEPS


def test_sync_does_not_override_concurrent_user_pin(
    definition_db: PostgresHubDatabase,
    postgres_database_url: str,
) -> None:
    created = _mgr(definition_db).create(
        name="lint",
        definition_json={"steps": _OLD_STEPS},
        enabled=True,
    )
    assert created.enabled_pinned is False
    schema_row = definition_db.fetchone("SELECT current_schema() AS schema")
    assert schema_row is not None
    schema = str(schema_row["schema"])
    writer_db = PostgresHubDatabase(definition_db.conninfo)
    writer_db.open()
    assert _mgr(writer_db).get(created.id).enabled is True
    finished = Event()
    errors: list[Exception] = []

    def run_sync() -> None:
        try:
            _mgr(writer_db).update_from_sync(
                created.id,
                enabled=False,
                description="from-template",
            )
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
                    "SELECT id FROM pipeline_definitions WHERE id = %s FOR UPDATE",
                    (created.id,),
                ).fetchone()
                assert locked is not None
                writer.start()
                _wait_for_row_lock_waiter(
                    postgres_database_url,
                    finished,
                    writer_db.application_name,
                )
                holder.execute(
                    """
                    UPDATE pipeline_definitions
                    SET enabled = TRUE, enabled_pinned = TRUE, updated_at = now()
                    WHERE id = %s
                    """,
                    (created.id,),
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

    row = _mgr(definition_db).get(created.id)
    assert row.enabled is True
    assert row.enabled_pinned is True
    assert row.description == "from-template"
