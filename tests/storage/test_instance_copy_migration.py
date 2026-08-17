"""Copy migration for workflow_instances into agent_step_instances."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from gobby.storage.sessions._constants import LIVE_SESSION_STATUSES

pytestmark = pytest.mark.integration

_REPO = Path(__file__).resolve().parents[2]
_MIGRATION = _REPO / "crates/gcore/assets/schema/migrations/377_copy_agent_step_instances.sql"

_STEPS_A = [{"name": "claim", "prompt": "claim it"}, {"name": "implement", "prompt": "write it"}]
_STEPS_B = [{"name": "review", "prompt": "check it"}]
_STEPS_REFRESHED = [{"name": "review", "prompt": "new body"}]


def _schema_sql() -> str:
    return """
CREATE TABLE sessions (
    id uuid PRIMARY KEY,
    project_id uuid,
    status text NOT NULL
);

CREATE TABLE session_variables (
    session_id uuid PRIMARY KEY,
    variables jsonb DEFAULT '{}'::jsonb,
    updated_at timestamptz DEFAULT now() NOT NULL
);

CREATE TABLE workflow_instances (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL,
    workflow_name text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    priority integer DEFAULT 100 NOT NULL,
    current_step text,
    step_entered_at timestamptz,
    step_action_count integer DEFAULT 0,
    total_action_count integer DEFAULT 0,
    variables jsonb DEFAULT '{}'::jsonb,
    context_injected boolean DEFAULT false,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL
);

CREATE TABLE workflow_definitions (
    id uuid PRIMARY KEY,
    project_id uuid,
    name text NOT NULL,
    definition_json jsonb NOT NULL,
    workflow_type text DEFAULT 'workflow' NOT NULL,
    deleted_at timestamptz
);

CREATE TABLE agent_definitions (
    id uuid PRIMARY KEY,
    project_id uuid,
    name text NOT NULL,
    description text,
    enabled boolean DEFAULT true NOT NULL,
    enabled_pinned boolean DEFAULT false NOT NULL,
    definition_json jsonb NOT NULL,
    source text DEFAULT 'installed'::text NOT NULL,
    tags jsonb,
    deleted_at timestamptz,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL
);
CREATE UNIQUE INDEX uq_agent_defs_live_name
    ON agent_definitions (name, project_id) NULLS NOT DISTINCT
    WHERE deleted_at IS NULL;

CREATE TABLE agent_step_workflows (
    id uuid PRIMARY KEY,
    agent_definition_id uuid NOT NULL UNIQUE
        REFERENCES agent_definitions(id) ON DELETE CASCADE,
    steps_json jsonb NOT NULL,
    variables_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    exit_condition text,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL
);

CREATE TABLE agent_step_instances (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL UNIQUE,
    agent_step_workflow_id uuid,
    agent_name text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    current_step text,
    step_entered_at timestamptz,
    step_action_count integer DEFAULT 0 NOT NULL,
    total_action_count integer DEFAULT 0 NOT NULL,
    variables jsonb DEFAULT '{}'::jsonb NOT NULL,
    context_injected boolean DEFAULT false NOT NULL,
    snapshot_json jsonb NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL
);
"""


@pytest.fixture
def copy_schema(postgres_database_url: str) -> Iterator[tuple[str, str]]:
    schema = f"gobby_test_instcopy_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(postgres_database_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        conn.execute(_schema_sql())
    try:
        yield postgres_database_url, schema
    finally:
        with psycopg.connect(postgres_database_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def _connect(url: str, schema: str, *, autocommit: bool = True) -> psycopg.Connection[Any]:
    conn = psycopg.connect(url, autocommit=autocommit, row_factory=dict_row)
    conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
    return conn


def _apply_copy(conn: psycopg.Connection[Any]) -> None:
    conn.execute(_MIGRATION.read_text(encoding="utf-8"))


def _insert_session(
    conn: psycopg.Connection[Any],
    *,
    session_id: str,
    status: str = "active",
    project_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO sessions (id, project_id, status) VALUES (%s, %s, %s)",
        (session_id, project_id, status),
    )


def _set_agent_type(
    conn: psycopg.Connection[Any],
    session_id: str,
    agent_type: str | None,
    extra: dict[str, Any] | None = None,
) -> None:
    variables: dict[str, Any] = dict(extra or {})
    if agent_type is not None:
        variables["_agent_type"] = agent_type
    conn.execute(
        """
        INSERT INTO session_variables (session_id, variables)
        VALUES (%s, %s)
        ON CONFLICT (session_id) DO UPDATE SET variables = EXCLUDED.variables
        """,
        (session_id, Jsonb(variables)),
    )


def _insert_instance(
    conn: psycopg.Connection[Any],
    *,
    row_id: str,
    session_id: str,
    workflow_name: str,
    current_step: str | None = "implement",
    enabled: bool = True,
    variables: dict[str, Any] | None = None,
    context_injected: bool | None = False,
    step_action_count: int | None = 2,
    total_action_count: int | None = 5,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    step_entered_at: datetime | None = None,
) -> None:
    now = datetime.now(UTC)
    conn.execute(
        """
        INSERT INTO workflow_instances (
            id, session_id, workflow_name, enabled, current_step, step_entered_at,
            step_action_count, total_action_count, variables, context_injected,
            created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            row_id,
            session_id,
            workflow_name,
            enabled,
            current_step,
            step_entered_at or now,
            step_action_count,
            total_action_count,
            Jsonb(variables) if variables is not None else None,
            context_injected,
            created_at or now,
            updated_at or now,
        ),
    )


def _insert_generated(
    conn: psycopg.Connection[Any],
    *,
    name: str,
    steps: list[dict[str, Any]],
    variables: dict[str, Any] | None = None,
    exit_condition: str | None = "done",
) -> None:
    conn.execute(
        """
        INSERT INTO workflow_definitions (id, name, definition_json, workflow_type)
        VALUES (%s, %s, %s, 'workflow')
        """,
        (
            str(uuid.uuid4()),
            name,
            Jsonb(
                {
                    "name": name,
                    "type": "step",
                    "steps": steps,
                    "variables": variables or {},
                    "exit_condition": exit_condition,
                }
            ),
        ),
    )


def _insert_agent(
    conn: psycopg.Connection[Any],
    *,
    name: str,
    steps: list[dict[str, Any]],
    project_id: str | None = None,
    child_id: str | None = None,
    variables: dict[str, Any] | None = None,
    exit_condition: str | None = "done",
) -> str:
    agent_id = str(uuid.uuid4())
    lineage = child_id or str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO agent_definitions (
            id, project_id, name, description, definition_json, source
        ) VALUES (%s, %s, %s, %s, %s, 'installed')
        """,
        (agent_id, project_id, name, name, Jsonb({"name": name, "provider": "claude"})),
    )
    conn.execute(
        """
        INSERT INTO agent_step_workflows (
            id, agent_definition_id, steps_json, variables_json, exit_condition
        ) VALUES (%s, %s, %s, %s, %s)
        """,
        (lineage, agent_id, Jsonb(steps), Jsonb(variables or {}), exit_condition),
    )
    return lineage


def _typed(conn: psycopg.Connection[Any], session_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM agent_step_instances WHERE session_id = %s",
        (session_id,),
    ).fetchone()
    return None if row is None else dict(row)


def test_migration_sql_exists_and_uses_live_status_constants() -> None:
    assert _MIGRATION.is_file(), f"missing copy migration {_MIGRATION}"
    sql_text = _MIGRATION.read_text(encoding="utf-8")
    for status in LIVE_SESSION_STATUSES:
        assert status in sql_text
    assert "LOCK TABLE workflow_instances IN ACCESS EXCLUSIVE MODE" in sql_text
    assert "ACCESS EXCLUSIVE" in sql_text


def test_handoff_ready_session_continuity(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    session_id = str(uuid.uuid4())
    instance_id = str(uuid.uuid4())
    entered = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
    created = entered - timedelta(hours=1)
    updated = entered + timedelta(minutes=5)
    with _connect(url, schema) as conn:
        _insert_session(conn, session_id=session_id, status="handoff_ready")
        _set_agent_type(conn, session_id, "coder")
        lineage = _insert_agent(conn, name="coder", steps=_STEPS_A)
        _insert_generated(conn, name="coder-steps", steps=_STEPS_A, variables={"goal": "ship"})
        _insert_instance(
            conn,
            row_id=instance_id,
            session_id=session_id,
            workflow_name="coder-steps",
            current_step="implement",
            variables={"goal": "ship", "task_claimed": True},
            created_at=created,
            updated_at=updated,
            step_entered_at=entered,
        )
        _apply_copy(conn)
        copied = _typed(conn, session_id)
        assert copied is not None
        assert str(copied["id"]) == instance_id
        assert copied["agent_name"] == "coder"
        assert copied["current_step"] == "implement"
        assert copied["variables"] == {"goal": "ship", "task_claimed": True}
        assert copied["agent_step_workflow_id"] == uuid.UUID(lineage)
        assert copied["snapshot_json"]["steps"][1]["name"] == "implement"


def test_runtime_field_equivalence(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    session_id = str(uuid.uuid4())
    instance_id = str(uuid.uuid4())
    created = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    updated = created + timedelta(days=1)
    entered = created + timedelta(hours=2)
    with _connect(url, schema) as conn:
        _insert_session(conn, session_id=session_id)
        _set_agent_type(conn, session_id, "coder")
        lineage = _insert_agent(conn, name="coder", steps=_STEPS_A)
        _insert_generated(conn, name="coder-steps", steps=_STEPS_A)
        _insert_instance(
            conn,
            row_id=instance_id,
            session_id=session_id,
            workflow_name="coder-steps",
            current_step="claim",
            enabled=True,
            variables=None,
            context_injected=None,
            step_action_count=None,
            total_action_count=None,
            created_at=created,
            updated_at=updated,
            step_entered_at=entered,
        )
        _apply_copy(conn)
        copied = _typed(conn, session_id)
        assert copied is not None
        assert str(copied["id"]) == instance_id
        assert copied["enabled"] is True
        assert copied["current_step"] == "claim"
        assert copied["step_action_count"] == 0
        assert copied["total_action_count"] == 0
        assert copied["variables"] == {}
        assert copied["context_injected"] is False
        assert copied["created_at"] == created
        assert copied["updated_at"] == updated
        assert copied["step_entered_at"] == entered
        assert copied["agent_step_workflow_id"] == uuid.UUID(lineage)


def test_definitionless_snapshot_migrates_with_null_lineage(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    session_id = str(uuid.uuid4())
    with _connect(url, schema) as conn:
        _insert_session(conn, session_id=session_id)
        _set_agent_type(conn, session_id, "orphan")
        _insert_generated(conn, name="orphan-steps", steps=_STEPS_A, variables={"keep": True})
        _insert_instance(
            conn,
            row_id=str(uuid.uuid4()),
            session_id=session_id,
            workflow_name="orphan-steps",
            current_step="claim",
            variables={"keep": True},
        )
        _apply_copy(conn)
        copied = _typed(conn, session_id)
        assert copied is not None
        assert copied["agent_step_workflow_id"] is None
        assert copied["snapshot_json"]["steps"][0]["name"] == "claim"
        assert copied["snapshot_json"]["variables"] == {"keep": True}


def test_neither_lineage_nor_snapshot_fails(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    session_id = str(uuid.uuid4())
    with _connect(url, schema) as conn:
        _insert_session(conn, session_id=session_id)
        _set_agent_type(conn, session_id, "ghost")
        _insert_instance(
            conn,
            row_id=str(uuid.uuid4()),
            session_id=session_id,
            workflow_name="ghost-steps",
            current_step="claim",
        )
        with pytest.raises(psycopg.errors.RaiseException, match="ghost"):
            _apply_copy(conn)


def test_generated_row_missing_current_step_fails(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    session_id = str(uuid.uuid4())
    with _connect(url, schema) as conn:
        _insert_session(conn, session_id=session_id)
        _set_agent_type(conn, session_id, "coder")
        _insert_agent(conn, name="coder", steps=_STEPS_REFRESHED)
        _insert_generated(conn, name="coder-steps", steps=_STEPS_REFRESHED)
        _insert_instance(
            conn,
            row_id=str(uuid.uuid4()),
            session_id=session_id,
            workflow_name="coder-steps",
            current_step="implement",
        )
        with pytest.raises(psycopg.errors.RaiseException, match="current_step"):
            _apply_copy(conn)


def test_rebuild_branch_missing_current_step_fails(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    session_id = str(uuid.uuid4())
    with _connect(url, schema) as conn:
        _insert_session(conn, session_id=session_id)
        _set_agent_type(conn, session_id, "coder")
        _insert_agent(conn, name="coder", steps=_STEPS_REFRESHED)
        _insert_instance(
            conn,
            row_id=str(uuid.uuid4()),
            session_id=session_id,
            workflow_name="coder-steps",
            current_step="implement",
        )
        with pytest.raises(psycopg.errors.RaiseException, match="current_step"):
            _apply_copy(conn)


def test_disabled_instance_continuity(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    session_id = str(uuid.uuid4())
    with _connect(url, schema) as conn:
        _insert_session(conn, session_id=session_id)
        _set_agent_type(conn, session_id, "coder")
        _insert_agent(conn, name="coder", steps=_STEPS_A)
        _insert_generated(conn, name="coder-steps", steps=_STEPS_A)
        _insert_instance(
            conn,
            row_id=str(uuid.uuid4()),
            session_id=session_id,
            workflow_name="coder-steps",
            current_step="implement",
            enabled=False,
            variables={"paused": True},
        )
        _apply_copy(conn)
        copied = _typed(conn, session_id)
        assert copied is not None
        assert copied["enabled"] is False
        assert copied["current_step"] == "implement"
        assert copied["variables"] == {"paused": True}


def test_candidate_resolution_determinism(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    live = str(uuid.uuid4())
    dead = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    stamp = datetime(2026, 3, 3, 3, 3, tzinfo=UTC)
    older_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    newer_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    with _connect(url, schema) as conn:
        _insert_session(conn, session_id=live, project_id=project_id)
        _insert_session(conn, session_id=dead, status="expired")
        _set_agent_type(conn, live, "coder")
        global_lineage = _insert_agent(conn, name="coder", steps=_STEPS_A)
        project_lineage = _insert_agent(conn, name="coder", steps=_STEPS_A, project_id=project_id)
        _insert_generated(conn, name="coder-steps", steps=_STEPS_A)
        _insert_instance(
            conn,
            row_id=newer_id,
            session_id=live,
            workflow_name="coder-steps",
            current_step="claim",
            created_at=stamp,
            updated_at=stamp,
        )
        _insert_instance(
            conn,
            row_id=older_id,
            session_id=live,
            workflow_name="coder-steps",
            current_step="implement",
            created_at=stamp,
            updated_at=stamp,
        )
        _insert_instance(
            conn,
            row_id=str(uuid.uuid4()),
            session_id=dead,
            workflow_name="session-lifecycle",
            current_step="boot",
        )
        _apply_copy(conn)
        copied = _typed(conn, live)
        assert copied is not None
        assert str(copied["id"]) == older_id
        assert copied["agent_step_workflow_id"] == uuid.UUID(project_lineage)
        assert copied["agent_step_workflow_id"] != uuid.UUID(global_lineage)
        assert _typed(conn, dead) is None

        empty = str(uuid.uuid4())
        _insert_session(conn, session_id=empty)
        _apply_copy(conn)
        assert _typed(conn, empty) is None


def test_non_qualifying_live_instance_fails(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    other = str(uuid.uuid4())
    with _connect(url, schema) as conn:
        _insert_session(conn, session_id=other)
        _insert_instance(
            conn,
            row_id=str(uuid.uuid4()),
            session_id=other,
            workflow_name="session-lifecycle",
            current_step="boot",
        )
        with pytest.raises(psycopg.errors.RaiseException, match="session-lifecycle"):
            _apply_copy(conn)


def test_active_identity_resolution(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    aba = str(uuid.uuid4())
    switched = str(uuid.uuid4())
    unresolved = str(uuid.uuid4())
    now = datetime.now(UTC)
    with _connect(url, schema) as conn:
        for session_id in (aba, switched, unresolved):
            _insert_session(conn, session_id=session_id)
        _set_agent_type(conn, aba, "alpha")
        _set_agent_type(
            conn,
            switched,
            "beta",
            extra={"_step_workflow_name": "alpha-steps"},
        )
        _insert_agent(conn, name="alpha", steps=_STEPS_A)
        _insert_agent(conn, name="beta", steps=_STEPS_B)
        _insert_generated(conn, name="alpha-steps", steps=_STEPS_A)
        _insert_generated(conn, name="beta-steps", steps=_STEPS_B)
        _insert_instance(
            conn,
            row_id=str(uuid.uuid4()),
            session_id=aba,
            workflow_name="alpha-steps",
            current_step="implement",
            updated_at=now - timedelta(minutes=10),
        )
        _insert_instance(
            conn,
            row_id=str(uuid.uuid4()),
            session_id=aba,
            workflow_name="beta-steps",
            current_step="review",
            updated_at=now,
        )
        _insert_instance(
            conn,
            row_id=str(uuid.uuid4()),
            session_id=switched,
            workflow_name="alpha-steps",
            current_step="implement",
        )
        _insert_instance(
            conn,
            row_id=str(uuid.uuid4()),
            session_id=unresolved,
            workflow_name="alpha-steps",
            current_step="claim",
        )
        with pytest.raises(psycopg.errors.RaiseException, match=unresolved):
            _apply_copy(conn)
        conn.execute("DELETE FROM workflow_instances WHERE session_id = %s", (unresolved,))
        _apply_copy(conn)
        aba_row = _typed(conn, aba)
        assert aba_row is not None
        assert aba_row["agent_name"] == "alpha"
        assert aba_row["current_step"] == "implement"
        assert _typed(conn, switched) is None


def test_legacy_write_fence(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    session_id = str(uuid.uuid4())
    instance_id = str(uuid.uuid4())
    holder = _connect(url, schema, autocommit=False)
    waiter = _connect(url, schema, autocommit=True)
    try:
        _insert_session(holder, session_id=session_id)
        _set_agent_type(holder, session_id, "coder")
        _insert_agent(holder, name="coder", steps=_STEPS_A)
        _insert_generated(holder, name="coder-steps", steps=_STEPS_A)
        _insert_instance(
            holder,
            row_id=instance_id,
            session_id=session_id,
            workflow_name="coder-steps",
            current_step="claim",
        )
        holder.commit()
        _apply_copy(holder)
        waiter.execute("SET lock_timeout = '200ms'")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            waiter.execute(
                "UPDATE workflow_instances SET current_step = %s WHERE id = %s",
                ("hacked", instance_id),
            )
        holder.commit()
        with pytest.raises(psycopg.errors.RaiseException):
            waiter.execute(
                "UPDATE workflow_instances SET current_step = %s WHERE id = %s",
                ("hacked", instance_id),
            )
        copied = holder.execute(
            "SELECT current_step FROM agent_step_instances WHERE session_id = %s",
            (session_id,),
        ).fetchone()
        assert copied is not None and copied["current_step"] == "claim"
    finally:
        holder.close()
        waiter.close()


def test_absent_legacy_table_is_receipted_noop(copy_schema: tuple[str, str]) -> None:
    url, schema = copy_schema
    with _connect(url, schema) as conn:
        conn.execute("DROP TABLE workflow_instances")
        _apply_copy(conn)
        remaining = conn.execute("SELECT count(*) AS n FROM agent_step_instances").fetchone()
        assert remaining is not None and int(remaining["n"]) == 0
