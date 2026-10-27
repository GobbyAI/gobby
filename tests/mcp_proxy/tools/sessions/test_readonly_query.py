"""Tests for query_readonly: one read-only, row-capped, credential-blind DB query tool."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions import create_session_messages_registry
from gobby.storage.hub.protocol import HubDatabase

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("query_readonly_gdaemon")]

TOOL = "query_readonly"
PROBE_SEQUENCE = "query_readonly_probe_seq"
HANDOFF_TABLES = ("session_handoffs", "session_handoff_deliveries")
CREDENTIAL_NAME_PARTS = (
    "secret",
    "encrypted",
    "wrapped_dek",
    "kek_",
    "key_hash",
    "token_hash",
    "password",
)
# The name pattern must find every one of these; a miss means the pattern lost coverage.
KNOWN_CREDENTIAL_COLUMNS = {
    ("secrets", "encrypted_value"),
    ("secret_key_material", "wrapped_dek"),
    ("secret_key_material", "kek_salt"),
    ("api_keys", "key_hash"),
    ("auth_sessions", "token_hash"),
    ("users", "password_hash"),
    ("comms_channels", "webhook_secret"),
    ("deployment_runtime", "grant_signing_secret"),
}
CREDENTIAL_TABLES = ("secrets", "secret_key_material", "api_keys", "auth_sessions")


@pytest.fixture
def registry(temp_db: HubDatabase) -> InternalToolRegistry:
    return create_session_messages_registry(session_manager=MagicMock(), db=temp_db)


async def _run(registry: InternalToolRegistry, **arguments: Any) -> dict[str, Any]:
    assert registry.get_tool(TOOL) is not None, f"gobby-sessions does not register {TOOL}"
    result: dict[str, Any] = await registry.call(TOOL, arguments)
    return result


def _credential_columns(db: HubDatabase) -> set[tuple[str, str]]:
    clauses = " OR ".join("column_name LIKE %s" for _ in CREDENTIAL_NAME_PARTS)
    rows = db.fetchall(
        "SELECT table_name AS table_name, column_name AS column_name "
        "FROM information_schema.columns "
        "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
        f"AND ({clauses})",
        tuple(f"%{part}%" for part in CREDENTIAL_NAME_PARTS),
    )
    return {(row["table_name"], row["column_name"]) for row in rows}


async def test_query_readonly_is_registered_on_the_sessions_registry(
    registry: InternalToolRegistry,
) -> None:
    assert registry.get_tool(TOOL) is not None


@pytest.mark.parametrize("table", HANDOFF_TABLES)
async def test_select_reads_the_handoff_tables(registry: InternalToolRegistry, table: str) -> None:
    result = await _run(registry, sql=f"SELECT count(*) AS n FROM {table}")

    assert result["success"] is True
    assert len(result["rows"]) == 1


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM session_handoffs",
        "UPDATE session_handoffs SET id = id",
        "DROP TABLE session_handoff_deliveries",
        "CREATE TABLE query_readonly_probe (id int)",
        "SELECT 1; SELECT 2",
        "SELECT 1; DELETE FROM session_handoffs",
    ],
)
async def test_writes_ddl_and_multiple_statements_are_refused(
    registry: InternalToolRegistry, sql: str
) -> None:
    result = await _run(registry, sql=sql)

    assert result["success"] is False


async def test_statement_over_the_timeout_is_refused(registry: InternalToolRegistry) -> None:
    result = await _run(registry, sql="SELECT pg_sleep(30)")

    assert result["success"] is False


async def test_read_only_transaction_refuses_a_sequence_advance(
    registry: InternalToolRegistry, temp_db: HubDatabase
) -> None:
    with temp_db.transaction() as conn:
        conn.execute(f"CREATE SEQUENCE IF NOT EXISTS {PROBE_SEQUENCE}")

    result = await _run(registry, sql=f"SELECT nextval('{PROBE_SEQUENCE}')")

    assert result["success"] is False


async def test_row_cap_defaults_to_200_and_clamps_to_2000(registry: InternalToolRegistry) -> None:
    series = "SELECT g FROM generate_series(1, 5000) AS g"

    default = await _run(registry, sql=series)
    assert len(default["rows"]) == 200

    maximum = await _run(registry, sql=series, limit=5000)
    assert len(maximum["rows"]) == 2000


def test_credential_pattern_covers_the_known_credential_columns(temp_db: HubDatabase) -> None:
    assert KNOWN_CREDENTIAL_COLUMNS <= _credential_columns(temp_db)


async def test_every_credential_column_is_refused(
    registry: InternalToolRegistry, temp_db: HubDatabase
) -> None:
    columns = _credential_columns(temp_db)
    assert KNOWN_CREDENTIAL_COLUMNS <= columns

    for table, column in sorted(columns):
        result = await _run(registry, sql=f'SELECT "{column}" FROM "{table}" LIMIT 1')
        assert result["success"] is False, f"{table}.{column} was readable"


@pytest.mark.parametrize("table", CREDENTIAL_TABLES)
async def test_credential_tables_are_refused(registry: InternalToolRegistry, table: str) -> None:
    result = await _run(registry, sql=f"SELECT count(*) FROM {table}")

    assert result["success"] is False
