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
