"""Credential sentinels must stay unreadable through query_readonly by any path.

Each test seeds a synthetic credential sentinel in a temporary schema on the
isolated test hub, then asks ``query_readonly`` for it through a different
catalog or function path. The assertion is only that the sentinel never
appears in a returned row. A refusal or a permission error both pass.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions import create_session_messages_registry
from gobby.mcp_proxy.tools.sessions._readonly_query import SQL_FROM_TEXT_FUNCTIONS
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

TOOL = "query_readonly"
SENTINEL = "credential-sentinel-25195-value"
ACTIVITY_SENTINEL = "credential-sentinel-25195-activity"
ROW_COUNT = 200
SENTINEL_ROWS = 150


@pytest.fixture
def registry(temp_db: HubDatabase) -> InternalToolRegistry:
    return create_session_messages_registry(session_manager=MagicMock(), db=temp_db)


@pytest.fixture
def fixture_schema(temp_db: HubDatabase) -> Iterator[str]:
    """Synthetic credential table with repeated sentinel values, analyzed for stats."""
    schema = f"sentinel_{uuid.uuid4().hex[:12]}"
    with temp_db.transaction() as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"CREATE TABLE {schema}.secrets (id int, encrypted_value text)")
        conn.execute(
            f"INSERT INTO {schema}.secrets "
            "SELECT g, CASE WHEN g <= %s THEN %s ELSE 'filler-' || g END "
            f"FROM generate_series(1, {ROW_COUNT}) g",
            (SENTINEL_ROWS, SENTINEL),
        )
        conn.execute(f"ANALYZE {schema}.secrets")
        conn.execute(
            f"CREATE VIEW {schema}.plain AS SELECT encrypted_value AS payload FROM {schema}.secrets"
        )
    try:
        yield schema
    finally:
        with temp_db.transaction() as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


async def _run(registry: InternalToolRegistry, **arguments: Any) -> dict[str, Any]:
    result: dict[str, Any] = await registry.call(TOOL, arguments)
    return result


def _leaked(result: dict[str, Any], needle: str) -> bool:
    return needle in json.dumps(result.get("rows", []), default=str)


async def test_pg_stats_most_common_values_hide_the_sentinel(
    registry: InternalToolRegistry, fixture_schema: str
) -> None:
    result = await _run(
        registry,
        sql=(
            "SELECT most_common_vals::text AS v FROM pg_stats "
            f"WHERE tablename = concat('sec','rets') AND schemaname = '{fixture_schema}'"
        ),
    )

    assert not _leaked(result, SENTINEL)


async def test_pg_statistic_does_not_expose_the_sentinel(
    registry: InternalToolRegistry, fixture_schema: str
) -> None:
    result = await _run(registry, sql="SELECT stavalues1::text AS v FROM pg_statistic")

    assert not _leaked(result, SENTINEL)


async def test_pg_stat_activity_hides_other_session_text(
    registry: InternalToolRegistry, postgres_database_url: str
) -> None:
    other = psycopg.connect(postgres_database_url, autocommit=True)
    try:
        other.execute(f"SELECT '{ACTIVITY_SENTINEL}'::text")

        result = await _run(registry, sql="SELECT query AS v FROM pg_stat_activity")
    finally:
        other.close()

    assert not _leaked(result, ACTIVITY_SENTINEL)


async def test_neutral_view_over_credentials_is_unreadable(
    registry: InternalToolRegistry, fixture_schema: str
) -> None:
    result = await _run(registry, sql=f"SELECT payload::text AS v FROM {fixture_schema}.plain")

    assert not _leaked(result, SENTINEL)


async def test_set_config_cannot_reset_role_to_read_credentials(
    registry: InternalToolRegistry, fixture_schema: str
) -> None:
    result = await _run(
        registry,
        sql=(
            "SELECT (SELECT set_config('role', 'none', true)) AS a, "
            f"(SELECT string_agg(payload, ',') FROM {fixture_schema}.plain) AS v"
        ),
    )

    assert not _leaked(result, SENTINEL)


async def test_query_to_xml_cannot_read_credentials(
    registry: InternalToolRegistry, fixture_schema: str
) -> None:
    result = await _run(
        registry,
        sql=(
            "SELECT query_to_xml("
            f"'SELECT string_agg(payload, '','') AS v FROM {fixture_schema}.plain', "
            "false, false, '')::text AS v"
        ),
    )

    assert not _leaked(result, SENTINEL)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT set_config('role', 'none', true) AS a",
        "SELECT pg_catalog.set_config('role', 'none', true) AS a",
        "SELECT \"set_config\"('role', 'none', true) AS a",
    ],
)
async def test_set_config_is_refused_by_name(registry: InternalToolRegistry, sql: str) -> None:
    result = await _run(registry, sql=sql)

    assert result["success"] is False


async def test_statement_runs_as_the_query_role(registry: InternalToolRegistry) -> None:
    result = await _run(registry, sql="SELECT current_user::text AS v")

    assert result["success"] is True
    assert result["rows"] == [{"v": "gobby_query_readonly"}]


async def test_mcp_server_env_is_unreadable(
    registry: InternalToolRegistry, temp_db: HubDatabase
) -> None:
    """mcp_servers has no credential-named column, so only the column grant hides env.

    The name read is a positive control: it must succeed, so a schema built without the
    query-role grants fails here instead of passing the env check vacuously.
    """
    with temp_db.transaction() as conn:
        project = conn.execute("SELECT id FROM projects LIMIT 1").fetchone()
    assert project is not None
    server_id = str(uuid.uuid4())
    server_name = f"sentinel-{server_id[:8]}"
    with temp_db.transaction() as conn:
        conn.execute(
            "INSERT INTO mcp_servers (id, name, project_id, transport, env) "
            "VALUES (%s, %s, %s, 'stdio', %s)",
            (server_id, server_name, project["id"], json.dumps({"TOKEN": SENTINEL})),
        )
    try:
        control = await _run(registry, sql="SELECT name::text AS v FROM mcp_servers")
        result = await _run(registry, sql="SELECT env::text AS v FROM mcp_servers")
    finally:
        with temp_db.transaction() as conn:
            conn.execute("DELETE FROM mcp_servers WHERE id = %s", (server_id,))

    assert control["success"] is True
    assert server_name in json.dumps(control["rows"], default=str)
    assert result["success"] is False
    assert "permission denied" in result["error"]
    assert not _leaked(result, SENTINEL)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT url::text AS v FROM mcp_servers",
        "SELECT args::text AS v FROM mcp_servers",
        "SELECT remote_url::text AS v FROM clones",
        "SELECT platform_url::text AS v FROM comms_attachments",
    ],
)
async def test_connection_url_and_args_columns_are_unreadable(
    registry: InternalToolRegistry, sql: str
) -> None:
    """URLs and args can carry tokens, so these columns carry no grant for the query role."""
    result = await _run(registry, sql=sql)

    assert result["success"] is False
    assert "permission denied" in result["error"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id::text AS v FROM mcp_servers",
        "SELECT id::text AS v FROM clones",
        "SELECT id::text AS v FROM comms_attachments",
    ],
)
async def test_same_tables_stay_readable_outside_the_denied_columns(
    registry: InternalToolRegistry, sql: str
) -> None:
    """Positive control: the denial is column-level, so the id column still reads."""
    result = await _run(registry, sql=sql)

    assert result["success"] is True


async def test_mcp_server_url_sentinel_stays_unreadable_direct_and_whole_row(
    registry: InternalToolRegistry, temp_db: HubDatabase
) -> None:
    """A token in mcp_servers.url is credential material even though the name looks benign."""
    with temp_db.transaction() as conn:
        project = conn.execute("SELECT id FROM projects LIMIT 1").fetchone()
    assert project is not None
    server_id = str(uuid.uuid4())
    server_name = f"sentinel-{server_id[:8]}"
    with temp_db.transaction() as conn:
        conn.execute(
            "INSERT INTO mcp_servers (id, name, project_id, transport, url) "
            "VALUES (%s, %s, %s, 'stdio', %s)",
            (server_id, server_name, project["id"], f"https://example.test/mcp?token={SENTINEL}"),
        )
    try:
        control = await _run(registry, sql="SELECT name::text AS v FROM mcp_servers")
        direct = await _run(registry, sql="SELECT url::text AS v FROM mcp_servers")
        whole_row = await _run(registry, sql="SELECT to_jsonb(m)::text AS v FROM mcp_servers m")
    finally:
        with temp_db.transaction() as conn:
            conn.execute("DELETE FROM mcp_servers WHERE id = %s", (server_id,))

    assert control["success"] is True
    assert direct["success"] is False
    assert whole_row["success"] is False
    assert not _leaked(direct, SENTINEL)
    assert not _leaked(whole_row, SENTINEL)


async def test_ts_stat_is_refused_by_name(registry: InternalToolRegistry) -> None:
    """ts_stat executes SQL from text, so it is refused by name like set_config.

    The inner text reads a column the role can read, so privileges cannot refuse it.
    Only the denied-name gate can, which makes this test fail if the name is removed.
    """
    result = await _run(
        registry,
        sql=("SELECT word::text AS v FROM ts_stat('SELECT to_tsvector(name) FROM mcp_servers')"),
    )

    assert result["success"] is False


# Real call shapes where a valid call proves the refusal; the rest are refused by name alone.
SQL_FROM_TEXT_SHAPES = {
    "dblink": "SELECT dblink('dbname=unused', 'SELECT 1')::text AS v",
    "query_to_xml": "SELECT query_to_xml('SELECT 1', false, false, '')::text AS v",
    "query_to_xml_and_xmlschema": (
        "SELECT query_to_xml_and_xmlschema('SELECT 1', false, false, '')::text AS v"
    ),
    "query_to_xmlschema": "SELECT query_to_xmlschema('SELECT 1', false, false, '')::text AS v",
    "set_config": "SELECT set_config('role', 'none', true)::text AS v",
    "ts_rewrite": (
        "SELECT ts_rewrite('a'::tsquery, 'SELECT ''a''::tsquery, ''b''::tsquery')::text AS v"
    ),
    "ts_stat": "SELECT word::text AS v FROM ts_stat('SELECT ''x''::tsvector')",
}


@pytest.mark.parametrize("name", sorted(SQL_FROM_TEXT_FUNCTIONS))
async def test_sql_from_text_builtin_is_refused_by_name(
    registry: InternalToolRegistry, name: str
) -> None:
    """Each built-in that runs SQL held in text is refused before Postgres resolves the call."""
    sql = SQL_FROM_TEXT_SHAPES.get(name, f"SELECT {name}()::text AS v")
    result = await _run(registry, sql=sql)

    assert result["success"] is False
    assert result["error_code"] == "query_refused"


async def test_function_outside_pg_catalog_is_refused(
    registry: InternalToolRegistry, temp_db: HubDatabase
) -> None:
    """A public plpgsql function can EXECUTE text, so any function outside pg_catalog is refused.

    The query role holds USAGE on the schema, so only the name gate can refuse the call.
    """
    schema = f"fn_{uuid.uuid4().hex[:12]}"
    with temp_db.transaction() as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(
            f"CREATE FUNCTION {schema}.run_text(sql text) RETURNS text LANGUAGE plpgsql AS "
            "$$ BEGIN EXECUTE sql; RETURN 'ran'; END $$"
        )
        conn.execute(f"GRANT USAGE ON SCHEMA {schema} TO gobby_query_readonly")
        conn.execute(f"GRANT EXECUTE ON FUNCTION {schema}.run_text(text) TO gobby_query_readonly")
    try:
        result = await _run(registry, sql=f"SELECT {schema}.run_text('SELECT 1')::text AS v")
    finally:
        with temp_db.transaction() as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")

    assert result["success"] is False
    assert result["error_code"] == "query_refused"


async def test_validation_argv_is_withheld_from_the_query_role(
    registry: InternalToolRegistry,
) -> None:
    """validation_run_records.argv can carry command tokens, so the query role gets no SELECT.

    The id read is a positive control: the denial is column-level, not table-level.
    """
    privilege = await _run(
        registry,
        sql=(
            "SELECT has_column_privilege('gobby_query_readonly', "
            "'validation_run_records', 'argv', 'SELECT')::text AS v"
        ),
    )
    control = await _run(registry, sql="SELECT id::text AS v FROM validation_run_records")
    denied = await _run(registry, sql="SELECT argv::text AS v FROM validation_run_records")

    assert privilege["success"] is True
    assert privilege["rows"] == [{"v": "false"}]
    assert control["success"] is True
    assert denied["success"] is False
    assert "permission denied" in denied["error"]
