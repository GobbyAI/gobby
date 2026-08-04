"""Contract tests for legacy machine ownership conversion."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from psycopg import sql

from tests.fixtures.postgres import isolated_test_schema

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "src/gobby/storage/migrations/375_machine_scope.sql"
BASELINE = REPO_ROOT / "src/gobby/storage/postgres_baseline_schema.sql"
MACHINE_A = "10000000-0000-4000-8000-000000000001"
MACHINE_B = "10000000-0000-4000-8000-000000000002"
SESSION_A = "20000000-0000-4000-8000-000000000001"
SESSION_B = "20000000-0000-4000-8000-000000000002"
SESSION_WITHOUT_MACHINE = "20000000-0000-4000-8000-000000000003"
PROJECT_ID = "30000000-0000-4000-8000-000000000001"
WORKTREE_ID = "40000000-0000-4000-8000-000000000001"
CLONE_ID = "50000000-0000-4000-8000-000000000001"
AGENT_ID = "60000000-0000-4000-8000-000000000001"
PIPELINE_ID = "70000000-0000-4000-8000-000000000001"
CRON_ID = "80000000-0000-4000-8000-000000000001"


def _create_pre_m0_schema(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    conn.execute(
        """
        CREATE TABLE machines (id UUID PRIMARY KEY);
        CREATE TABLE sessions (
            id UUID PRIMARY KEY,
            machine_id UUID REFERENCES machines(id)
        );
        CREATE TABLE worktrees (
            id UUID PRIMARY KEY,
            project_id UUID NOT NULL,
            branch_name TEXT NOT NULL,
            worktree_path TEXT NOT NULL,
            agent_session_id UUID REFERENCES sessions(id) ON DELETE SET NULL
        );
        CREATE UNIQUE INDEX idx_worktrees_path ON worktrees(worktree_path);
        CREATE UNIQUE INDEX idx_worktrees_branch ON worktrees(project_id, branch_name);
        CREATE TABLE clones (
            id UUID PRIMARY KEY,
            clone_path TEXT NOT NULL,
            agent_session_id UUID REFERENCES sessions(id) ON DELETE SET NULL
        );
        CREATE UNIQUE INDEX idx_clones_path ON clones(clone_path);
        CREATE TABLE agent_runs (
            id UUID PRIMARY KEY,
            parent_session_id UUID NOT NULL REFERENCES sessions(id),
            child_session_id UUID REFERENCES sessions(id),
            status TEXT NOT NULL
        );
        CREATE TABLE pipeline_executions (
            id UUID PRIMARY KEY,
            session_id UUID NOT NULL REFERENCES sessions(id)
        );
        CREATE TABLE cron_runs (
            id UUID PRIMARY KEY,
            status TEXT NOT NULL,
            scheduler_owner TEXT,
            agent_run_id UUID,
            pipeline_execution_id UUID
        );
        """
    )
    conn.execute(
        "INSERT INTO machines(id) VALUES (%s), (%s)",
        (MACHINE_A, MACHINE_B),
    )
    conn.execute(
        """
        INSERT INTO sessions(id, machine_id) VALUES
            (%s, %s),
            (%s, %s),
            (%s, NULL)
        """,
        (SESSION_A, MACHINE_A, SESSION_B, MACHINE_B, SESSION_WITHOUT_MACHINE),
    )
    conn.execute(
        "INSERT INTO pipeline_executions(id, session_id) VALUES (%s, %s)",
        (PIPELINE_ID, SESSION_A),
    )


def _connect_to_schema(
    postgres_database_url: str,
    schema_name: str,
) -> psycopg.Connection[tuple[object, ...]]:
    conn: psycopg.Connection[tuple[object, ...]] = psycopg.connect(
        postgres_database_url,
        autocommit=True,
    )
    conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
    return conn


@pytest.mark.unit
def test_migration_and_baseline_contract() -> None:
    """Migration and baseline retain the required machine-scope structure."""
    migration = MIGRATION.read_text(encoding="utf-8")
    baseline = BASELINE.read_text(encoding="utf-8")

    for table in ("worktrees", "clones", "agent_runs", "cron_runs"):
        assert f"ALTER TABLE {table}\n    ADD COLUMN IF NOT EXISTS machine_id UUID" in migration
        assert f"ALTER TABLE {table}\n    ALTER COLUMN machine_id SET NOT NULL" in migration

    assert "s.id = w.agent_session_id" in migration
    assert "s.id = c.agent_session_id" in migration
    assert "SELECT child.machine_id" in migration
    assert "SELECT parent.machine_id" in migration
    assert "WHERE status IN ('pending', 'running')" in migration
    assert "FROM agent_runs ar" in migration
    assert "FROM pipeline_executions pe" in migration
    assert "unresolved_worktrees" in migration
    assert "unresolved_clones" in migration
    assert "unresolved_agent_runs" in migration
    assert "unresolved_cron_runs" in migration
    assert (
        "DELETE confirmed stale rows or repair their authoritative session/run linkage" in migration
    )

    assert "machine_id UUID NOT NULL REFERENCES machines(id)" in baseline
    assert (
        "CREATE UNIQUE INDEX idx_worktrees_path ON worktrees(machine_id, worktree_path);"
        in baseline
    )
    assert (
        "CREATE UNIQUE INDEX idx_worktrees_branch "
        "ON worktrees(project_id, branch_name, machine_id);"
    ) in baseline
    assert "CREATE UNIQUE INDEX idx_clones_path ON clones(machine_id, clone_path);" in baseline
    assert (
        "CREATE INDEX idx_agent_runs_machine_status ON agent_runs(machine_id, status);" in baseline
    )


@pytest.mark.integration
def test_legacy_shapes_convert_or_abort_with_remediation(
    postgres_database_url: str,
) -> None:
    """Every legal legacy owner shape converts or fails with actionable row diagnostics."""
    migration = MIGRATION.read_text(encoding="utf-8")

    with isolated_test_schema(postgres_database_url, "m0scopeok") as schema_name:
        with _connect_to_schema(postgres_database_url, schema_name) as conn:
            _create_pre_m0_schema(conn)
            conn.execute(
                "INSERT INTO worktrees VALUES (%s, %s, 'shared', '/shared', %s)",
                (WORKTREE_ID, PROJECT_ID, SESSION_A),
            )
            conn.execute(
                "INSERT INTO clones VALUES (%s, '/clone', %s)",
                (CLONE_ID, SESSION_A),
            )
            conn.execute(
                "INSERT INTO agent_runs VALUES (%s, %s, %s, 'completed')",
                (AGENT_ID, SESSION_A, SESSION_B),
            )
            conn.execute(
                """
                INSERT INTO cron_runs VALUES
                    (%s, 'completed', NULL, %s, NULL),
                    ('80000000-0000-4000-8000-000000000002', 'failed', NULL, NULL, %s)
                """,
                (CRON_ID, AGENT_ID, PIPELINE_ID),
            )

            conn.execute(migration)

            assert conn.execute("SELECT machine_id FROM worktrees").fetchone() == (UUID(MACHINE_A),)
            assert conn.execute("SELECT machine_id FROM clones").fetchone() == (UUID(MACHINE_A),)
            assert conn.execute("SELECT machine_id FROM agent_runs").fetchone() == (
                UUID(MACHINE_B),
            )
            assert conn.execute("SELECT machine_id FROM cron_runs ORDER BY id").fetchall() == [
                (UUID(MACHINE_B),),
                (UUID(MACHINE_A),),
            ]
            assert conn.execute(
                """
                SELECT table_name, is_nullable
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND column_name = 'machine_id'
                  AND table_name IN ('worktrees', 'clones', 'agent_runs', 'cron_runs')
                ORDER BY table_name
                """
            ).fetchall() == [
                ("agent_runs", "NO"),
                ("clones", "NO"),
                ("cron_runs", "NO"),
                ("worktrees", "NO"),
            ]

    with isolated_test_schema(postgres_database_url, "m0scopefix") as schema_name:
        with _connect_to_schema(postgres_database_url, schema_name) as conn:
            _create_pre_m0_schema(conn)
            conn.execute(
                "INSERT INTO worktrees VALUES (%s, %s, 'detached', '/detached', NULL)",
                (WORKTREE_ID, PROJECT_ID),
            )
            conn.execute(
                "INSERT INTO clones VALUES (%s, '/detached-clone', NULL)",
                (CLONE_ID,),
            )
            conn.execute(
                "INSERT INTO agent_runs VALUES (%s, %s, NULL, 'completed')",
                (AGENT_ID, SESSION_WITHOUT_MACHINE),
            )
            conn.execute(
                "INSERT INTO cron_runs VALUES (%s, 'completed', NULL, NULL, NULL)",
                (CRON_ID,),
            )

            with pytest.raises(psycopg.errors.RaiseException) as exc_info:
                conn.execute(migration)

            detail = exc_info.value.diag.message_detail or ""
            assert "unresolved_worktrees" in detail and WORKTREE_ID in detail
            assert "unresolved_clones" in detail and CLONE_ID in detail
            assert "unresolved_agent_runs" in detail and AGENT_ID in detail
            assert "unresolved_cron_runs" in detail and CRON_ID in detail
            assert conn.execute(
                """
                SELECT count(*)
                FROM information_schema.columns
                WHERE table_schema = current_schema() AND column_name = 'machine_id'
                  AND table_name IN ('worktrees', 'clones', 'agent_runs', 'cron_runs')
                """
            ).fetchone() == (0,)

            conn.execute(
                "UPDATE worktrees SET agent_session_id = %s",
                (SESSION_A,),
            )
            conn.execute(
                "UPDATE clones SET agent_session_id = %s",
                (SESSION_A,),
            )
            conn.execute(
                "UPDATE agent_runs SET parent_session_id = %s",
                (SESSION_A,),
            )
            conn.execute(
                "UPDATE cron_runs SET agent_run_id = %s",
                (AGENT_ID,),
            )

            conn.execute(migration)
            assert (
                conn.execute(
                    """
                SELECT
                    (SELECT machine_id FROM worktrees),
                    (SELECT machine_id FROM clones),
                    (SELECT machine_id FROM agent_runs),
                    (SELECT machine_id FROM cron_runs)
                """
                ).fetchone()
                == (UUID(MACHINE_A),) * 4
            )

    with isolated_test_schema(postgres_database_url, "m0scopeactive") as schema_name:
        with _connect_to_schema(postgres_database_url, schema_name) as conn:
            _create_pre_m0_schema(conn)
            conn.execute(
                "INSERT INTO cron_runs VALUES (%s, 'running', 'daemon-a', NULL, NULL)",
                (CRON_ID,),
            )

            with pytest.raises(psycopg.errors.RaiseException) as exc_info:
                conn.execute(migration)

            assert exc_info.value.diag.message_primary == (
                "machine scope migration blocked: cron state is not drained"
            )
            detail = exc_info.value.diag.message_detail or ""
            assert CRON_ID in detail and '"status": "running"' in detail
