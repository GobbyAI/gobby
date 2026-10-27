"""``query_readonly``: one read-only, row-capped SELECT over the daemon database.

Postgres enforces the guarantees that matter: the transaction is READ ONLY, the
statement and lock waits are bounded, and nothing can write, advance a sequence,
or run DDL. The statement checks here are front gates with clear refusals.

Credential boundary. Each statement runs under ``SET LOCAL ROLE
gobby_query_readonly`` (migration 482), so privilege checks run as that role. The
role holds column-level SELECT on public base tables only. It has no grants on
views, the agent-auth schema, credential tables, credential-named columns, or the
explicitly excluded columns (connection URLs and args included), and it has no
membership. The denied-name check is a second layer. ``SQL_FROM_TEXT_FUNCTIONS`` lists
every built-in that runs SQL held in text. ``set_config`` is one: PUBLIC can execute it,
and ``set_config('role', 'none')`` in the same statement would reset the role to the
daemon runtime. A statement that names one of them is refused, and so is one that names
any function outside pg_catalog, since a public plpgsql function can EXECUTE text. A
statement cannot call a function named only by concatenation, the text-to-SQL entry
points (``*_to_xml``, dblink) are refused, and ``SET ROLE`` cannot start a SELECT or
WITH.

Residual. A future Postgres built-in that runs SQL from text must join
SQL_FROM_TEXT_FUNCTIONS. Views must never be granted to the role. Tables added after
migration 482 stay unreadable until granted. The code index, projects, and project
checkouts are row-level secured with no policy for this role, so it reads zero rows
there.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import psycopg

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.hub.protocol import HubDatabase, Transaction

DEFAULT_ROW_LIMIT = 200
MAX_ROW_LIMIT = 2000
STATEMENT_TIMEOUT_MS = 5_000
# Migration 482: column-level SELECT on non-credential public tables, no views.
QUERY_ROLE = "gobby_query_readonly"

# LIKE patterns over information_schema column names. A column matching any of them
# is a credential column wherever it lives.
CREDENTIAL_COLUMN_PATTERNS = (
    "%secret%",
    "%encrypted%",
    "%wrapped_dek%",
    "%kek_%",
    "%key_hash%",
    "%token_hash%",
    "%password%",
)
# Tables that hold credentials by design; refused even if the catalog scan finds nothing.
CREDENTIAL_TABLES = frozenset({"secrets", "secret_key_material", "api_keys", "auth_sessions"})
# Built-ins that parse and run SQL held in text, so a statement can assemble the text at run
# time and the lexical checks never see it. set_config resets the role the same way, and
# dblink runs SQL on another connection. Each name is refused before Postgres resolves the
# call; the privilege tests run every one to prove it. Other functions outside pg_catalog are
# refused by the catalog check, since a public plpgsql function can EXECUTE text.
SQL_FROM_TEXT_FUNCTIONS = frozenset(
    {
        "cursor_to_xml",
        "cursor_to_xmlschema",
        "database_to_xml",
        "database_to_xml_and_xmlschema",
        "database_to_xmlschema",
        "dblink",
        "query_to_xml",
        "query_to_xml_and_xmlschema",
        "query_to_xmlschema",
        "schema_to_xml",
        "schema_to_xml_and_xmlschema",
        "schema_to_xmlschema",
        "set_config",
        "table_to_xml",
        "table_to_xml_and_xmlschema",
        "table_to_xmlschema",
        "ts_rewrite",
        "ts_stat",
    }
)
# Catalogs that expose password hashes or host files, plus every SQL-from-text built-in.
DENIED_NAMES = SQL_FROM_TEXT_FUNCTIONS | frozenset(
    {
        "pg_authid",
        "pg_shadow",
        "pg_user_mapping",
        "pg_user_mappings",
        "pg_stat_file",
    }
)
# Functions that read files, signal backends, or execute SQL held in text.
DENIED_PREFIXES = (
    "dblink",
    "lo_",
    "pg_ls_",
    "pg_read_",
    "pg_terminate_",
    "pg_cancel_",
    "pg_reload_",
    "pg_rotate_",
)
DENIED_FRAGMENTS = ("to_xml",)

_CATALOG_SQL = (
    "SELECT table_name, column_name FROM information_schema.columns "
    "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
    "AND column_name ILIKE ANY(%s::text[])"
)
# Functions outside pg_catalog. A public plpgsql function can EXECUTE text, so the name gate
# refuses any statement that names one.
_FUNCTION_SQL = (
    "SELECT DISTINCT p.proname AS name FROM pg_proc p "
    "JOIN pg_namespace n ON n.oid = p.pronamespace "
    "WHERE n.nspname <> 'pg_catalog'"
)
_NAME = re.compile(r"[a-z_][a-z0-9_$]*")
_UNICODE_ESCAPE = re.compile(r"\bu&", re.IGNORECASE)
_STATEMENT_START = re.compile(r"(select|with)\b", re.IGNORECASE)


@dataclass(frozen=True)
class _Catalog:
    """Credential names in this database, lowercased, read inside the query's transaction."""

    columns: frozenset[str]
    tables: frozenset[str]
    functions: frozenset[str]


def _single_statement(sql: str) -> str:
    statement = sql.strip()
    if statement.endswith(";"):
        statement = statement[:-1].rstrip()
    return statement


def _static_refusal(statement: str) -> str | None:
    # Non-ASCII text can fold to ASCII names in Postgres, so it is refused outright.
    if not statement.isascii():
        return "Only ASCII SQL is accepted."
    if _UNICODE_ESCAPE.search(statement):
        return "U& escape syntax is refused."
    if ";" in statement:
        return "Send one statement per call."
    if _STATEMENT_START.match(statement) is None:
        return "Only a single SELECT or WITH statement is accepted."
    return None


def _names(statement: str, params: Sequence[Any]) -> set[str]:
    texts = [statement, *(param for param in params if isinstance(param, str))]
    return {name for text in texts for name in _NAME.findall(text.lower())}


def _name_refusal(names: set[str], catalog: _Catalog) -> str | None:
    if names & (catalog.columns | catalog.tables | DENIED_NAMES):
        return (
            "That names a credential table or column, or a catalog that holds one. "
            "Those are never readable through query_readonly."
        )
    if names & catalog.functions:
        return (
            "That calls a function outside pg_catalog, which can run SQL held in text. "
            "It is refused."
        )
    for name in names:
        if name.startswith(DENIED_PREFIXES) or any(f in name for f in DENIED_FRAGMENTS):
            return f"{name} reads files, signals backends, or runs SQL from text; it is refused."
    return None


def _load_catalog(txn: Transaction) -> _Catalog:
    rows = txn.execute(_CATALOG_SQL, (list(CREDENTIAL_COLUMN_PATTERNS),)).fetchall()
    tables = {str(row["table_name"]).lower() for row in rows}
    columns = {str(row["column_name"]).lower() for row in rows}
    functions = {str(row["name"]).lower() for row in txn.execute(_FUNCTION_SQL).fetchall()}
    return _Catalog(
        columns=frozenset(columns),
        tables=frozenset(tables | CREDENTIAL_TABLES),
        functions=frozenset(functions),
    )


def _refused(message: str) -> dict[str, Any]:
    return {"success": False, "error": message, "error_code": "query_refused"}


def _run(db: HubDatabase, sql: str, params: list[Any], limit: int) -> dict[str, Any]:
    statement = _single_statement(sql)
    refusal = _static_refusal(statement)
    if refusal is not None:
        return _refused(refusal)
    with db.bounded_transaction(
        statement_timeout_ms=STATEMENT_TIMEOUT_MS, repeatable_read_read_only=True
    ) as txn:
        refusal = _name_refusal(_names(statement, params), _load_catalog(txn))
        if refusal is not None:
            return _refused(refusal)
        txn.execute(f"SET LOCAL ROLE {QUERY_ROLE}")
        wrapped = f"SELECT * FROM (\n{statement}\n) AS readonly_result LIMIT {limit}"
        rows = txn.execute(wrapped, params).fetchall()
    return {
        "success": True,
        "rows": json.loads(json.dumps(list(rows), default=str)),
        "limit": limit,
    }


def register_readonly_query_tools(registry: InternalToolRegistry, db: HubDatabase) -> None:
    """Register ``query_readonly`` on the sessions registry."""

    @registry.tool(
        name="query_readonly",
        description=(
            "Run one read-only SELECT or WITH on the daemon database. Postgres enforces "
            "READ ONLY and a 5-second statement timeout. Rows are capped at `limit` "
            f"(default {DEFAULT_ROW_LIMIT}, max {MAX_ROW_LIMIT}). Credential tables and "
            "columns (secrets, API keys, auth sessions, password hashes, webhook and "
            "signing secrets) are refused. Bind values through `params` with %s."
        ),
    )
    async def query_readonly(
        sql: str, params: list[Any] | None = None, limit: int = DEFAULT_ROW_LIMIT
    ) -> dict[str, Any]:
        """Run one bounded read-only query; refusals and failures return success False."""
        if isinstance(limit, bool) or not isinstance(limit, int):
            return {
                "success": False,
                "error": "limit must be an integer",
                "error_code": "invalid_query",
            }
        bounded = max(1, min(limit, MAX_ROW_LIMIT))
        try:
            return await asyncio.to_thread(_run, db, sql, list(params or []), bounded)
        except psycopg.Error as exc:
            return {"success": False, "error": f"Query failed: {exc}", "error_code": "query_failed"}
