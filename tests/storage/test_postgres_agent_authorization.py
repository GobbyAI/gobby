"""Integration tests for scoped PostgreSQL managed-agent principals."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from gobby.storage.schema_contract import apply_schema
from tests.fixtures.postgres import (
    TEST_USER_EMAIL,
    TEST_USER_ID,
    TEST_USER_NAME,
    TEST_USER_PASSWORD_HASH,
)

pytestmark = pytest.mark.integration

AUTH_SCHEMA = "gobby_agent_auth"
CAPABILITY_ROLE = "gobby_gcode_capability"
ISSUER_ROLE = "gobby_agent_issuer"
RUNTIME_ROLE = "gobby_daemon_runtime"


@dataclass(frozen=True)
class AuthorizationFixture:
    database_url: str
    project_id: UUID
    other_project_id: UUID
    session_id: UUID
    agent_run_id: UUID
    machine_id: UUID
    execution_id: UUID
    role_name: str
    password: str

    @property
    def agent_url(self) -> str:
        parsed = conninfo_to_dict(self.database_url)
        parsed.update(
            user=self.role_name,
            password=self.password,
            application_name=f"gobby-agent-{self.execution_id}",
        )
        return make_conninfo("", **parsed)


def _require_isolated_hub(database_url: str) -> None:
    parsed = conninfo_to_dict(database_url)
    assert parsed.get("host") == "127.0.0.1"
    assert parsed.get("port") == "60892"
    assert parsed.get("dbname") == "gobby_test"


def _as_runtime(
    conn: psycopg.Connection[Any],
    statement: str,
    params: tuple[Any, ...],
) -> Any:
    conn.execute(f"SET ROLE {RUNTIME_ROLE}")
    try:
        row = conn.execute(statement, params).fetchone()
        assert row is not None
        return row[0] if len(row) == 1 else row
    finally:
        conn.execute("RESET ROLE")


def _issue(
    conn: psycopg.Connection[Any],
    fixture: AuthorizationFixture,
    execution_id: UUID,
    password: str,
) -> tuple[str, int]:
    row = _as_runtime(
        conn,
        f"""
        SELECT * FROM {AUTH_SCHEMA}.issue_principal(
            %s, 'agent_run', %s, %s, %s, NOW() + INTERVAL '30 minutes', %s
        )
        """,
        (
            execution_id,
            fixture.session_id,
            fixture.agent_run_id,
            fixture.machine_id,
            password,
        ),
    )
    return str(row[0]), int(row[1])


def _seed(conn: psycopg.Connection[Any], fixture: AuthorizationFixture) -> None:
    conn.execute(
        "INSERT INTO public.projects (id, name, repo_path) VALUES (%s, %s, %s), (%s, %s, %s)",
        (
            fixture.project_id,
            f"agent-auth-{fixture.project_id}",
            f"/tmp/{fixture.project_id}",
            fixture.other_project_id,
            f"agent-auth-{fixture.other_project_id}",
            f"/tmp/{fixture.other_project_id}",
        ),
    )
    conn.execute(
        """
        INSERT INTO public.users (id, email, name, password_hash)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (TEST_USER_ID, TEST_USER_EMAIL, TEST_USER_NAME, TEST_USER_PASSWORD_HASH),
    )
    conn.execute(
        "INSERT INTO public.machines (id, hostname, owner_user_id) "
        "VALUES (%s, 'agent-auth-test', %s)",
        (fixture.machine_id, TEST_USER_ID),
    )
    conn.execute(
        """
        INSERT INTO public.sessions (id, external_id, machine_id, source, project_id)
        VALUES (%s, %s, %s, 'codex', %s)
        """,
        (
            fixture.session_id,
            f"agent-auth-{fixture.session_id}",
            fixture.machine_id,
            fixture.project_id,
        ),
    )
    conn.execute(
        """
        INSERT INTO public.agent_runs (id, parent_session_id, machine_id, provider, prompt)
        VALUES (%s, %s, %s, 'codex', 'authorization fixture')
        """,
        (fixture.agent_run_id, fixture.session_id, fixture.machine_id),
    )
    conn.execute(
        "UPDATE public.sessions SET agent_run_id = %s WHERE id = %s",
        (fixture.agent_run_id, fixture.session_id),
    )
    for project_id in (fixture.project_id, fixture.other_project_id):
        conn.execute(
            "INSERT INTO public.code_indexed_projects (id) VALUES (%s)",
            (project_id,),
        )
        conn.execute(
            """
            INSERT INTO public.code_indexed_project_states
                (machine_id, project_id, root_path, total_files, total_symbols,
                 last_indexed_at, index_duration_ms)
            VALUES (%s, %s, %s, 1, 1, NOW(), 1)
            """,
            (fixture.machine_id, project_id, f"/tmp/{project_id}"),
        )
        conn.execute(
            """
            INSERT INTO public.code_indexed_files
                (id, project_id, file_path, language, content_hash, symbol_count, byte_size)
            VALUES (%s, %s, 'src/lib.rs', 'rust', %s, 1, 10)
            """,
            (uuid4(), project_id, f"hash-{project_id}"),
        )
        conn.execute(
            """
            INSERT INTO public.code_symbols (
                id, project_id, file_path, name, qualified_name, kind, language,
                byte_start, byte_end, line_start, line_end, content_hash,
                file_content_hash
            ) VALUES (%s, %s, 'src/lib.rs', %s, %s, 'function', 'rust', 0, 10, 1, 1, %s, %s)
            """,
            (
                uuid4(),
                project_id,
                f"symbol_{str(project_id).replace('-', '_')}",
                f"crate::symbol_{str(project_id).replace('-', '_')}",
                f"hash-{project_id}",
                f"hash-{project_id}",
            ),
        )
        conn.execute(
            """
            INSERT INTO public.code_content_chunks
                (id, project_id, file_path, chunk_index, line_start, line_end, content,
                 language, content_hash)
            VALUES (%s, %s, 'src/lib.rs', 0, 1, 1, %s, 'rust', %s)
            """,
            (uuid4(), project_id, f"content-{project_id}", f"hash-{project_id}"),
        )


def _cleanup(conn: psycopg.Connection[Any], fixture: AuthorizationFixture) -> None:
    execution_ids = conn.execute(
        f"SELECT DISTINCT managed_execution_id FROM {AUTH_SCHEMA}.principal_bindings "
        "WHERE session_id = %s AND revoked_at IS NULL",
        (fixture.session_id,),
    ).fetchall()
    for (execution_id,) in execution_ids:
        _as_runtime(
            conn,
            f"SELECT {AUTH_SCHEMA}.revoke_principal(%s, NULL)",
            (execution_id,),
        )
    for table in (
        "code_content_chunks",
        "code_calls",
        "code_imports",
        "code_symbols",
        "code_indexed_files",
        "code_index_projection_cleanup_pending",
        "code_index_prune_dirty_projects",
    ):
        conn.execute(
            f"DELETE FROM public.{table} WHERE project_id IN (%s, %s)",
            (fixture.project_id, fixture.other_project_id),
        )
    conn.execute(
        "DELETE FROM public.code_indexed_projects WHERE id IN (%s, %s)",
        (fixture.project_id, fixture.other_project_id),
    )
    conn.execute(
        "UPDATE public.sessions SET agent_run_id = NULL WHERE id = %s",
        (fixture.session_id,),
    )
    conn.execute("DELETE FROM public.agent_runs WHERE id = %s", (fixture.agent_run_id,))
    conn.execute("DELETE FROM public.sessions WHERE id = %s", (fixture.session_id,))
    conn.execute("DELETE FROM public.machines WHERE id = %s", (fixture.machine_id,))
    conn.execute(
        "DELETE FROM public.projects WHERE id IN (%s, %s)",
        (fixture.project_id, fixture.other_project_id),
    )


@pytest.fixture(scope="module")
def authorization_fixture(postgres_database_url: str) -> Iterator[AuthorizationFixture]:
    _require_isolated_hub(postgres_database_url)
    database_name = f"gobby_test_agent_auth_{uuid4().hex[:12]}"
    parameters = conninfo_to_dict(postgres_database_url)
    parameters["dbname"] = database_name
    isolated_url = make_conninfo("", **parameters)
    with psycopg.connect(postgres_database_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    try:
        with psycopg.connect(isolated_url, autocommit=True) as connection:
            connection.execute("CREATE EXTENSION pg_search")
        apply_schema(isolated_url)
        fixture = AuthorizationFixture(
            database_url=isolated_url,
            project_id=uuid4(),
            other_project_id=uuid4(),
            session_id=uuid4(),
            agent_run_id=uuid4(),
            machine_id=uuid4(),
            execution_id=uuid4(),
            role_name="",
            password=f"agent-test-{uuid4()}",
        )
        with psycopg.connect(isolated_url, autocommit=True) as conn:
            try:
                _seed(conn, fixture)
                role_name, generation = _issue(
                    conn, fixture, fixture.execution_id, fixture.password
                )
                assert generation == 1
                fixture = replace(fixture, role_name=role_name)
                yield fixture
            finally:
                _cleanup(conn, fixture)
    finally:
        with psycopg.connect(postgres_database_url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name)))


def _ids(conn: psycopg.Connection[Any], statement: str) -> set[UUID]:
    return {row[0] for row in conn.execute(statement).fetchall()}


def _denied(conn: psycopg.Connection[Any], statement: str) -> None:
    with pytest.raises(psycopg.Error):
        conn.execute(statement)


def test_roles_memberships_functions_and_rls_are_hardened(
    authorization_fixture: AuthorizationFixture,
) -> None:
    fixture = authorization_fixture
    with psycopg.connect(fixture.database_url, autocommit=True, row_factory=dict_row) as conn:
        roles = {
            row["rolname"]: row
            for row in conn.execute(
                """
                SELECT rolname, rolcanlogin, rolcreaterole, rolbypassrls
                FROM pg_roles WHERE rolname IN (%s, %s, %s)
                """,
                (ISSUER_ROLE, CAPABILITY_ROLE, RUNTIME_ROLE),
            ).fetchall()
        }
        assert set(roles) == {ISSUER_ROLE, CAPABILITY_ROLE, RUNTIME_ROLE}
        assert all(not row["rolcanlogin"] and not row["rolbypassrls"] for row in roles.values())
        assert roles[ISSUER_ROLE]["rolcreaterole"] is True
        assert roles[RUNTIME_ROLE]["rolcreaterole"] is False
        membership = conn.execute(
            """
            SELECT admin_option, inherit_option, set_option FROM pg_auth_members
            WHERE roleid = %s::regrole AND member = %s::regrole
            """,
            (CAPABILITY_ROLE, fixture.role_name),
        ).fetchone()
        assert membership == {
            "admin_option": False,
            "inherit_option": True,
            "set_option": False,
        }
        issuer_membership = conn.execute(
            """
            SELECT admin_option, inherit_option, set_option FROM pg_auth_members
            WHERE roleid = %s::regrole AND member = %s::regrole
            """,
            (CAPABILITY_ROLE, ISSUER_ROLE),
        ).fetchone()
        assert issuer_membership == {
            "admin_option": True,
            "inherit_option": False,
            "set_option": False,
        }
        project_scope_parallelism = conn.execute(
            """
            SELECT p.proparallel
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = %s AND p.proname = 'current_project_id'
            """,
            (AUTH_SCHEMA,),
        ).fetchone()
        assert project_scope_parallelism == {"proparallel": "s"}
        functions = conn.execute(
            """
            SELECT p.proname, p.prosecdef, p.proconfig,
                   has_function_privilege('public', p.oid, 'EXECUTE') AS public_execute,
                   has_function_privilege(%s, p.oid, 'EXECUTE') AS runtime_execute
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = %s AND p.proname = ANY(%s)
            """,
            (
                RUNTIME_ROLE,
                AUTH_SCHEMA,
                ["issue_principal", "rotate_principal", "revoke_principal", "reconcile_principal"],
            ),
        ).fetchall()
        assert len(functions) == 4
        for row in functions:
            settings = set(row["proconfig"] or [])
            assert row["prosecdef"] is True
            assert f"search_path={AUTH_SCHEMA}, pg_temp" in settings
            assert "createrole_self_grant=" in settings
            assert row["public_execute"] is False
            assert row["runtime_execute"] is True
        rls = conn.execute(
            """
            SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class
            WHERE relnamespace = 'public'::regnamespace
              AND relname = ANY(%s)
            """,
            (
                [
                    "projects",
                    "code_indexed_projects",
                    "code_indexed_files",
                    "code_symbols",
                    "code_imports",
                    "code_calls",
                    "code_content_chunks",
                    "code_index_projection_cleanup_pending",
                    "code_index_prune_dirty_projects",
                ],
            ),
        ).fetchall()
        assert len(rls) == 9
        assert all(row["relrowsecurity"] and row["relforcerowsecurity"] for row in rls)


def test_representative_search_symbol_index_freshness_graph_vector_and_status_sql(
    authorization_fixture: AuthorizationFixture,
) -> None:
    fixture = authorization_fixture
    with psycopg.connect(fixture.agent_url, autocommit=True) as agent:
        assert agent.execute("SELECT current_user, session_user").fetchone() == (
            fixture.role_name,
            fixture.role_name,
        )
        assert _ids(agent, "SELECT id FROM code_indexed_projects") == {fixture.project_id}
        assert _ids(agent, "SELECT project_id FROM code_symbols") == {fixture.project_id}
        assert _ids(agent, "SELECT project_id FROM code_content_chunks") == {fixture.project_id}
        assert (
            agent.execute(
                "SELECT name FROM code_symbols WHERE project_id = %s",
                (fixture.other_project_id,),
            ).fetchone()
            is None
        )
        assert (
            agent.execute(
                "UPDATE code_indexed_files SET graph_synced = TRUE WHERE project_id = %s",
                (fixture.project_id,),
            ).rowcount
            == 1
        )
        assert (
            agent.execute(
                "UPDATE code_indexed_files SET vectors_synced = TRUE WHERE project_id = %s",
                (fixture.project_id,),
            ).rowcount
            == 1
        )
        assert (
            agent.execute(
                "UPDATE code_indexed_files SET content_hash = content_hash WHERE project_id = %s",
                (fixture.other_project_id,),
            ).rowcount
            == 0
        )


def test_search_path_transactions_prepared_role_and_session_spoofs_fail_closed(
    authorization_fixture: AuthorizationFixture,
) -> None:
    fixture = authorization_fixture
    with psycopg.connect(fixture.agent_url, autocommit=True) as agent:
        agent.execute(f"SET search_path = pg_temp, public, {AUTH_SCHEMA}")
        agent.execute(
            "SELECT set_config('gobby.project_id', %s::text, false)",
            (fixture.other_project_id,),
        )
        assert _ids(agent, "SELECT id FROM code_indexed_projects") == {fixture.project_id}
        agent.execute(
            "PREPARE cross_project(UUID) AS SELECT id FROM code_indexed_projects WHERE id = $1"
        )
        execute_cross_project = sql.SQL("EXECUTE cross_project({})").format(
            sql.Literal(fixture.other_project_id)
        )
        assert agent.execute(execute_cross_project).fetchall() == []
        with agent.transaction():
            agent.execute("SET LOCAL search_path = public, pg_temp")
            agent.execute(
                "SELECT set_config('gobby.project_id', %s::text, true)",
                (fixture.other_project_id,),
            )
            assert (
                agent.execute(
                    "SELECT id FROM code_indexed_projects WHERE id = %s",
                    (fixture.other_project_id,),
                ).fetchall()
                == []
            )
        for statement in (
            f"SET ROLE {CAPABILITY_ROLE}",
            f"SET ROLE {RUNTIME_ROLE}",
            f"SET ROLE {ISSUER_ROLE}",
            f"ALTER ROLE {fixture.role_name} CREATEROLE",
            "SET SESSION AUTHORIZATION gobby_test",
        ):
            _denied(agent, statement)


def test_ownership_disabled_rls_public_defaults_and_operator_relations_are_denied(
    authorization_fixture: AuthorizationFixture,
) -> None:
    fixture = authorization_fixture
    probe = f"agent_default_probe_{uuid4().hex}"
    with psycopg.connect(fixture.database_url, autocommit=True) as admin:
        admin.execute(
            f"CREATE FUNCTION public.{probe}() RETURNS integer LANGUAGE sql AS 'SELECT 1'"
        )
        try:
            with psycopg.connect(fixture.agent_url, autocommit=True) as agent:
                for statement in (
                    "ALTER TABLE code_symbols OWNER TO gobby_gcode_capability",
                    "ALTER TABLE code_symbols DISABLE ROW LEVEL SECURITY",
                    "CREATE TEMP TABLE agent_escape(id integer)",
                    "CREATE TABLE public.agent_escape(id integer)",
                    "SELECT * FROM sessions",
                    "SELECT * FROM tasks",
                    "SELECT * FROM config_store",
                    "SELECT * FROM schema_migrations",
                    f"SELECT * FROM {AUTH_SCHEMA}.principal_bindings",
                    f"SELECT public.{probe}()",
                ):
                    with pytest.raises(psycopg.Error):
                        agent.execute(statement)
        finally:
            admin.execute(f"DROP FUNCTION public.{probe}()")


def test_lifecycle_uses_derived_quoted_roles_and_reconciles_expiry(
    authorization_fixture: AuthorizationFixture,
) -> None:
    fixture = authorization_fixture
    with psycopg.connect(fixture.database_url, autocommit=True) as admin:
        rotated = _as_runtime(
            admin,
            f"SELECT * FROM {AUTH_SCHEMA}.rotate_principal(%s, NOW() + INTERVAL '45 minutes', %s)",
            (fixture.execution_id, f"rotated-{uuid4()}"),
        )
        rotated_role = str(rotated[0])
        assert int(rotated[1]) == 2
        assert re.fullmatch(r"gobby_agent_[0-9a-f]{32}_2", rotated_role)
        assert (
            _as_runtime(
                admin,
                f"SELECT {AUTH_SCHEMA}.revoke_principal(%s, %s)",
                (fixture.execution_id, 2),
            )
            == 1
        )
        rotated_role_row = admin.execute("SELECT to_regrole(%s)", (rotated_role,)).fetchone()
        assert rotated_role_row is not None
        assert rotated_role_row[0] is None

        expiring_execution = uuid4()
        expiring_role, _ = _issue(admin, fixture, expiring_execution, f"expiring-{uuid4()}")
        admin.execute(
            f"UPDATE {AUTH_SCHEMA}.principal_bindings "
            "SET issued_at = NOW() - INTERVAL '2 seconds', "
            "expires_at = NOW() - INTERVAL '1 second' "
            "WHERE managed_execution_id = %s",
            (expiring_execution,),
        )
        assert (
            _as_runtime(
                admin,
                f"SELECT {AUTH_SCHEMA}.reconcile_principal(%s)",
                (expiring_execution,),
            )
            == 1
        )
        expiring_role_row = admin.execute("SELECT to_regrole(%s)", (expiring_role,)).fetchone()
        assert expiring_role_row is not None
        assert expiring_role_row[0] is None


def test_operator_inventory_is_secret_free_and_drain_removes_login_authority(
    authorization_fixture: AuthorizationFixture,
) -> None:
    fixture = authorization_fixture
    with psycopg.connect(fixture.database_url, autocommit=True) as admin:
        row = _as_runtime(
            admin,
            f"""
            SELECT managed_execution_id, owner_kind, project_id, login_capable,
                   active_sessions
            FROM {AUTH_SCHEMA}.list_active_principals()
            WHERE managed_execution_id = %s
            """,
            (fixture.execution_id,),
        )
        assert row == (
            fixture.execution_id,
            "agent_run",
            fixture.project_id,
            True,
            0,
        )

        drained = _as_runtime(
            admin,
            f"SELECT {AUTH_SCHEMA}.drain_ephemeral_principals()",
            (),
        )
        assert drained >= 1
        role = admin.execute("SELECT to_regrole(%s)", (fixture.role_name,)).fetchone()
        assert role is not None
        assert role[0] is None
        active = admin.execute(
            f"SELECT count(*) FROM {AUTH_SCHEMA}.principal_bindings "
            "WHERE managed_execution_id = %s AND revoked_at IS NULL",
            (fixture.execution_id,),
        ).fetchone()
        assert active == (0,)


def test_crafted_missing_expired_and_duplicate_issue_inputs_are_rejected(
    authorization_fixture: AuthorizationFixture,
) -> None:
    fixture = authorization_fixture
    with psycopg.connect(fixture.database_url, autocommit=True) as admin:
        admin.execute(f"SET ROLE {RUNTIME_ROLE}")
        try:
            statements = (
                (
                    f"SELECT * FROM {AUTH_SCHEMA}.issue_principal(%s, 'agent_run', %s, %s, %s, "
                    "NOW() + INTERVAL '5 minutes', 'password')",
                    (uuid4(), uuid4(), fixture.agent_run_id, fixture.machine_id),
                ),
                (
                    f"SELECT * FROM {AUTH_SCHEMA}.issue_principal(%s, 'crafted-owner', %s, %s, %s, "
                    "NOW() + INTERVAL '5 minutes', 'password')",
                    (uuid4(), fixture.session_id, fixture.agent_run_id, fixture.machine_id),
                ),
                (
                    f"SELECT * FROM {AUTH_SCHEMA}.issue_principal(%s, 'agent_run', %s, %s, %s, "
                    "NOW() - INTERVAL '1 second', 'password')",
                    (uuid4(), fixture.session_id, fixture.agent_run_id, fixture.machine_id),
                ),
                (
                    f"SELECT * FROM {AUTH_SCHEMA}.issue_principal(%s, 'agent_run', %s, %s, %s, "
                    "NOW() + INTERVAL '5 minutes', 'password')",
                    (
                        fixture.execution_id,
                        fixture.session_id,
                        fixture.agent_run_id,
                        fixture.machine_id,
                    ),
                ),
                (
                    f"SELECT * FROM {AUTH_SCHEMA}.issue_principal(%s::uuid, 'agent_run', %s, %s, %s, "
                    "NOW() + INTERVAL '5 minutes', 'password')",
                    (
                        "not-a-uuid'; CREATE ROLE escaped; --",
                        fixture.session_id,
                        fixture.agent_run_id,
                        fixture.machine_id,
                    ),
                ),
            )
            for statement, params in statements:
                with pytest.raises(psycopg.Error):
                    admin.execute(statement, params)
        finally:
            admin.execute("RESET ROLE")


def test_daemon_registry_and_one_hour_principal_lifetime_are_enforced(
    authorization_fixture: AuthorizationFixture,
) -> None:
    fixture = authorization_fixture
    execution_id = uuid4()
    with psycopg.connect(fixture.database_url, autocommit=True) as admin:
        registered = _as_runtime(
            admin,
            f"SELECT {AUTH_SCHEMA}.heartbeat_daemon(%s, INTERVAL '2 minutes')",
            (fixture.machine_id,),
        )
        assert registered == fixture.machine_id

        registry = admin.execute(
            f"SELECT lease_expires_at > heartbeat_at FROM {AUTH_SCHEMA}.daemon_registry "
            "WHERE machine_id = %s",
            (fixture.machine_id,),
        ).fetchone()
        assert registry == (True,)

        with pytest.raises(psycopg.errors.InvalidParameterValue):
            _as_runtime(
                admin,
                f"""SELECT * FROM {AUTH_SCHEMA}.issue_principal(
                    %s, 'agent_run', %s, %s, %s,
                    NOW() + INTERVAL '1 hour 1 second', %s
                )""",
                (
                    execution_id,
                    fixture.session_id,
                    fixture.agent_run_id,
                    fixture.machine_id,
                    f"too-long-{uuid4()}",
                ),
            )

        binding = admin.execute(
            f"SELECT 1 FROM {AUTH_SCHEMA}.principal_bindings WHERE managed_execution_id = %s",
            (execution_id,),
        ).fetchone()
        assert binding is None
