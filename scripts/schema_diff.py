#!/usr/bin/env python3
"""Compare canonical PostgreSQL schema and seed manifests.

Run from the repository root. The command-line harness is defined below the
normalization helpers so the same contracts can be exercised without a live
database.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess  # nosec B404 - fixed PostgreSQL client argv, never shell=True
import sys
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from gobby.config.bootstrap import load_bootstrap
from gobby.storage.schema_contract import apply_schema

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]

_IDENTIFIER_EDGE = r"A-Za-z0-9_$"
_IGNORED_STATEMENT_PREFIXES = (
    "SET ",
    "SELECT pg_catalog.set_config(",
    "CREATE SCHEMA ",
    "COMMENT ON SCHEMA ",
)
_POSTGRES_CLIENT_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class SeedTableSpec:
    """Natural-key and mutability rules for one baseline-seeded table."""

    key_columns: tuple[str, ...]
    machine_mutable_columns: frozenset[str] = frozenset()
    live_mutable_columns: frozenset[str] = frozenset()
    live_namespace_owned: bool = True
    live_owned_key_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SeedRow:
    """Canonical row representation independent of physical column order."""

    key: tuple[JsonValue, ...]
    values: tuple[tuple[str, JsonValue], ...]


type SeedManifest = Mapping[str, tuple[SeedRow, ...]]


def _strip_dump_noise(dump: str) -> str:
    lines: list[str] = []
    for line in dump.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("--") or stripped.startswith("\\restrict"):
            continue
        if stripped.startswith("\\unrestrict"):
            continue
        lines.append(line)
    return "\n".join(lines)


def _split_sql_statements(source: str) -> Iterator[str]:
    """Split SQL statements while preserving strings, comments, and dollar bodies."""
    statement_start = 0
    index = 0
    while index < len(source):
        char = source[index]
        if char == "-" and source.startswith("--", index):
            index = _skip_line_comment(source, index)
            continue
        if char == "/" and source.startswith("/*", index):
            index = _skip_block_comment(source, index)
            continue
        if char == "'":
            prefix = source[index - 1] if index > 0 else ""
            escape_backslashes = (
                prefix in "eE"
                and bool(prefix)
                and (index < 2 or not _is_identifier_continuation(source[index - 2]))
            )
            index = _skip_single_quoted_string(
                source,
                index,
                escape_backslashes=escape_backslashes,
            )
            continue
        if char == '"':
            index = _skip_double_quoted_identifier(source, index)
            continue
        if char == "$":
            tag = _dollar_quote_tag_at(source, index)
            if tag is not None:
                close = source.find(tag, index + len(tag))
                if close < 0:
                    raise ValueError(f"unterminated dollar-quote tag {tag!r}")
                index = close + len(tag)
                continue
        if char == ";":
            yield source[statement_start:index]
            statement_start = index + 1
        index += 1
    if tail := source[statement_start:]:
        yield tail


def _skip_line_comment(source: str, start: int) -> int:
    end = source.find("\n", start + 2)
    return len(source) if end < 0 else end + 1


def _skip_block_comment(source: str, start: int) -> int:
    index = start + 2
    depth = 1
    while index < len(source) and depth:
        if source.startswith("/*", index):
            depth += 1
            index += 2
            continue
        if source.startswith("*/", index):
            depth -= 1
            index += 2
            continue
        index += 1
    return index


def _skip_single_quoted_string(
    source: str,
    start: int,
    *,
    escape_backslashes: bool,
) -> int:
    index = start + 1
    while index < len(source):
        if escape_backslashes and source[index] == "\\":
            index += 2
            continue
        if source[index] == "'":
            if index + 1 < len(source) and source[index + 1] == "'":
                index += 2
                continue
            return index + 1
        index += 1
    return index


def _skip_double_quoted_identifier(source: str, start: int) -> int:
    index = start + 1
    while index < len(source):
        if source[index] == '"':
            if index + 1 < len(source) and source[index + 1] == '"':
                index += 2
                continue
            return index + 1
        index += 1
    return index


def _dollar_quote_tag_at(source: str, start: int) -> str | None:
    if start > 0 and _is_identifier_continuation(source[start - 1]):
        return None
    if start + 1 >= len(source):
        return None
    if source[start + 1] == "$":
        return "$$"
    if not _is_identifier_start(source[start + 1]):
        return None
    tag_end = start + 2
    while tag_end < len(source) and _is_identifier_continuation(source[tag_end]):
        tag_end += 1
    if tag_end < len(source) and source[tag_end] == "$":
        return source[start : tag_end + 1]
    return None


def _is_identifier_start(char: str) -> bool:
    return char.isalpha() or char == "_"


def _is_identifier_continuation(char: str) -> bool:
    return char.isalnum() or char == "_"


def _replace_schema_token(statement: str, schema_name: str, replacement: str) -> str:
    normalized = statement.replace(f'"{schema_name}"', f'"{replacement}"')
    normalized = normalized.replace(f"'{schema_name}'", f"'{replacement}'")
    return re.sub(
        rf"(?<![{_IDENTIFIER_EDGE}]){re.escape(schema_name)}(?![{_IDENTIFIER_EDGE}])",
        replacement,
        normalized,
    )


def _replace_schema_name(statement: str, schema_name: str) -> str:
    return _replace_schema_token(statement, schema_name, "__schema__")


def _split_top_level_commas(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    index = 0
    while index < len(value):
        char = value[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            elif char == "\\" and quote == "'":
                index += 2
                continue
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(value[start:index])
            start = index + 1
        index += 1
    parts.append(value[start:])
    return parts


def _normalize_not_null_constraint_names(statement: str) -> str:
    return re.sub(
        r"\bCONSTRAINT\s+[A-Za-z_][A-Za-z0-9_$]*\s+NOT\s+NULL\b",
        "NOT NULL",
        statement,
        flags=re.IGNORECASE,
    )


def _normalize_statement(statement: str) -> str:
    compact = re.sub(r"\s+", " ", statement).strip()
    compact = _normalize_not_null_constraint_names(compact)
    create_table = re.match(
        r"^(?P<prefix>CREATE TABLE(?: ONLY)? __schema__\.[^ (]+\s*\()(?P<body>.*)\)$",
        compact,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if create_table is None:
        return compact
    entries = [
        re.sub(r"\s+", " ", item).strip()
        for item in _split_top_level_commas(create_table.group("body"))
    ]
    return f"{create_table.group('prefix')}{', '.join(sorted(entries))})"


def _mentions_accepted_table(statement: str, accepted_tables: frozenset[str]) -> bool:
    return any(
        re.search(
            rf"(?<![{_IDENTIFIER_EDGE}]){re.escape(table)}(?![{_IDENTIFIER_EDGE}])",
            statement,
        )
        is not None
        for table in accepted_tables
    )


def normalize_schema_dump(
    dump: str,
    *,
    schema_name: str,
    accepted_tables: frozenset[str] = frozenset(),
) -> str:
    """Return a stable, order-independent schema representation from ``pg_dump``."""

    statements: list[str] = []
    for raw_statement in _split_sql_statements(_strip_dump_noise(dump)):
        statement = _replace_schema_name(raw_statement, schema_name)
        normalized = _normalize_statement(statement)
        if not normalized:
            continue
        if normalized.upper().startswith(
            tuple(prefix.upper() for prefix in _IGNORED_STATEMENT_PREFIXES)
        ):
            continue
        if _mentions_accepted_table(normalized, accepted_tables):
            continue
        statements.append(normalized + ";")
    return "\n".join(sorted(statements)) + ("\n" if statements else "")


def _normalize_seed_value(value: object) -> JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date | time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_seed_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence):
        return [_normalize_seed_value(item) for item in value]
    return str(value)


def _canonical_key(key: tuple[JsonValue, ...]) -> str:
    return json.dumps(key, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def normalize_seed_rows(
    *,
    table: str,
    columns: tuple[str, ...],
    rows: Sequence[Sequence[object]],
    spec: SeedTableSpec,
) -> tuple[SeedRow, ...]:
    """Normalize seed rows using natural keys and machine-build exclusions."""

    if len(set(columns)) != len(columns):
        raise ValueError(f"{table} has duplicate column names")
    missing_keys = set(spec.key_columns) - set(columns)
    if missing_keys:
        raise ValueError(f"{table} is missing key columns: {sorted(missing_keys)}")
    excluded_keys = set(spec.key_columns) & set(spec.machine_mutable_columns)
    if excluded_keys:
        raise ValueError(f"{table} key columns cannot be machine-mutable: {sorted(excluded_keys)}")

    normalized_rows: list[SeedRow] = []
    for row in rows:
        if len(row) != len(columns):
            raise ValueError(f"{table} row has {len(row)} values for {len(columns)} columns")
        raw = dict(zip(columns, row, strict=True))
        key = tuple(_normalize_seed_value(raw[column]) for column in spec.key_columns)
        values = tuple(
            sorted(
                (column, _normalize_seed_value(value))
                for column, value in raw.items()
                if column not in spec.machine_mutable_columns
            )
        )
        normalized_rows.append(SeedRow(key=key, values=values))
    return tuple(sorted(normalized_rows, key=lambda row: _canonical_key(row.key)))


def _row_index(rows: tuple[SeedRow, ...]) -> dict[str, SeedRow]:
    return {_canonical_key(row.key): row for row in rows}


def compare_machine_seed_manifests(
    expected: SeedManifest,
    actual: SeedManifest,
) -> tuple[str, ...]:
    """Require exact normalized equality between two machine-built schemas."""

    errors: list[str] = []
    for table in sorted(set(expected) | set(actual)):
        if table not in expected:
            errors.append(f"unexpected seeded table {table}")
            continue
        if table not in actual:
            errors.append(f"missing seeded table {table}")
            continue
        expected_rows = _row_index(expected[table])
        actual_rows = _row_index(actual[table])
        for key in sorted(set(expected_rows) | set(actual_rows)):
            if key not in expected_rows:
                errors.append(f"{table} has unexpected seed key {actual_rows[key].key!r}")
            elif key not in actual_rows:
                errors.append(f"{table} is missing seed key {expected_rows[key].key!r}")
            elif expected_rows[key].values != actual_rows[key].values:
                errors.append(f"{table} seed row {expected_rows[key].key!r} differs")
    return tuple(errors)


def _immutable_live_values(row: SeedRow, spec: SeedTableSpec) -> tuple[tuple[str, JsonValue], ...]:
    return tuple(item for item in row.values if item[0] not in spec.live_mutable_columns)


def compare_live_seed_manifests(
    expected: SeedManifest,
    live: SeedManifest,
    specs: Mapping[str, SeedTableSpec],
) -> tuple[str, ...]:
    """Check live seed invariants while preserving mutable installed state."""

    errors: list[str] = []
    for table in sorted(expected):
        if table not in live:
            errors.append(f"missing live seeded table {table}")
            continue
        spec = specs[table]
        expected_rows = _row_index(expected[table])
        live_rows = _row_index(live[table])
        for key in sorted(expected_rows):
            expected_row = expected_rows[key]
            if key not in live_rows:
                errors.append(f"{table} is missing required seed key {expected_row.key!r}")
                continue
            if _immutable_live_values(expected_row, spec) != _immutable_live_values(
                live_rows[key], spec
            ):
                errors.append(f"{table} live seed row {expected_row.key!r} differs")
        for key in sorted(set(live_rows) - set(expected_rows)):
            row = live_rows[key]
            first_key = row.key[0] if row.key else None
            key_is_owned = isinstance(first_key, str) and first_key.startswith(
                spec.live_owned_key_prefixes
            )
            if spec.live_namespace_owned or key_is_owned:
                errors.append(f"{table} has unexpected seed-owned key {live_rows[key].key!r}")
    return tuple(errors)


def seed_manifest_json(manifest: SeedManifest) -> str:
    """Serialize a seed manifest deterministically for review or CI artifacts."""

    payload = {
        table: [
            {"key": list(row.key), "values": dict(row.values)}
            for row in sorted(rows, key=lambda item: _canonical_key(item.key))
        ]
        for table, rows in sorted(manifest.items())
    }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


_BOOKKEEPING_TABLES = frozenset(
    {
        "destructive_batches",
        "migration_batches",
        "migration_receipts",
        "schema_migrations",
    }
)
_LIVE_ACCEPTED_TABLES: Mapping[str, str] = {
    "gobby_install_ownership": "installer-owned component inventory",
    "gwiki_chunks": "gcore-owned wiki index",
    "gwiki_documents": "gcore-owned wiki index",
    "gwiki_ingestions": "gcore-owned wiki index",
    "gwiki_links": "gcore-owned wiki index",
    "gwiki_sources": "gcore-owned wiki index",
}
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

_SESSION_RUNTIME_COLUMNS = frozenset(
    {
        "approved_tools_json",
        "chat_mode",
        "context_injected",
        "context_usage_confidence",
        "context_usage_ratio",
        "context_usage_source",
        "context_usage_updated_at",
        "context_used_tokens",
        "context_window",
        "digest_markdown",
        "git_branch",
        "had_edits",
        "last_assistant_content",
        "last_completion_output_tokens",
        "last_digest_input_hash",
        "last_digested_pair_index",
        "last_prompt_cache_creation_tokens",
        "last_prompt_cache_read_tokens",
        "last_prompt_input_tokens",
        "last_prompt_uncached_input_tokens",
        "last_turn_markdown",
        "message_count",
        "model",
        "original_prompt",
        "parent_session_id",
        "sandbox_enabled",
        "sandbox_policy_hash",
        "seq_num",
        "status",
        "summary_digest_turn_count",
        "summary_generated_at",
        "summary_generation_mode",
        "summary_markdown",
        "summary_path",
        "summary_revision_id",
        "summary_source_context_hash",
        "terminal_context",
        "title_source",
        "tool_call_count",
        "transcript_path",
        "transcript_processed",
        "turn_count",
        "usage_cache_creation_tokens",
        "usage_cache_read_tokens",
        "usage_input_tokens",
        "usage_output_tokens",
        "workflow_name",
    }
)
_STAGE_REGISTRY_DEFINITION_COLUMNS = frozenset(
    {
        "bundled_hash",
        "category",
        "default_agent",
        "default_max_review_rounds",
        "default_max_work_attempts",
        "description",
        "dispatch_inputs_json",
        "dispatch_target",
        "dispatch_type",
        "display_label",
        "is_terminal",
        "position_hint",
        "requires_human",
        "review_policy",
        "reviewer_agent",
        "reviewer_agent_selector_json",
    }
)
SEED_TABLE_SPECS: Mapping[str, SeedTableSpec] = {
    "projects": SeedTableSpec(
        key_columns=("id",),
        machine_mutable_columns=frozenset({"created_at", "updated_at"}),
        live_mutable_columns=frozenset(
            {
                "deleted_at",
                "github_repo",
                "github_url",
                "linear_project_id",
                "linear_sync_enabled",
                "linear_synced_at",
                "linear_team_id",
                "repo_path",
            }
        ),
        live_namespace_owned=False,
        live_owned_key_prefixes=("00000000-",),
    ),
    "sessions": SeedTableSpec(
        key_columns=("id",),
        machine_mutable_columns=frozenset({"created_at", "updated_at"}),
        live_mutable_columns=_SESSION_RUNTIME_COLUMNS,
        live_namespace_owned=False,
        live_owned_key_prefixes=("00000000-",),
    ),
    "task_stages_registry": SeedTableSpec(
        key_columns=("name",),
        machine_mutable_columns=frozenset({"updated_at"}),
        live_mutable_columns=_STAGE_REGISTRY_DEFINITION_COLUMNS,
        live_namespace_owned=False,
    ),
    "task_type_default_stages": SeedTableSpec(
        key_columns=("task_type", "stage_name"),
        live_mutable_columns=frozenset({"position"}),
        live_namespace_owned=False,
    ),
}


def _validate_identifier(value: str, *, label: str) -> str:
    if _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be an unqualified PostgreSQL identifier")
    return value


@contextmanager
def _scratch_schema(database_url: str, label: str) -> Iterator[str]:
    schema_name = f"gobby_test_schema_diff_{label}_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
    try:
        yield schema_name
    finally:
        with psycopg.connect(database_url, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name))
            )


def _run_postgres_client(
    argv: list[str],
    *,
    action: str,
    stdin: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    try:
        result = subprocess.run(  # nosec B603 - fixed executables and argv, never shell=True
            argv,
            input=stdin,
            capture_output=True,
            check=False,
            env=env,
            text=True,
            timeout=_POSTGRES_CLIENT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"{action} timed out after {_POSTGRES_CLIENT_TIMEOUT_SECONDS} seconds; "
            "check database connectivity and retry"
        ) from None
    except OSError as exc:
        raise RuntimeError(f"{action} failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"{action} failed: {detail}")
    return result.stdout


def _postgres_client_connection(database_url: str) -> tuple[str, dict[str, str]]:
    parameters = conninfo_to_dict(database_url)
    env = os.environ.copy()
    for parameter, variable in (("user", "PGUSER"), ("password", "PGPASSWORD")):
        credential = parameters.pop(parameter, None)
        if credential is not None:
            env[variable] = str(credential)
    sanitized = {key: str(value) for key, value in parameters.items() if value is not None}
    return make_conninfo(**sanitized), env


def _dump_schema(
    database_url: str,
    schema_name: str,
    *,
    pg_dump: str,
) -> str:
    conninfo, env = _postgres_client_connection(database_url)
    return _run_postgres_client(
        [
            pg_dump,
            "--schema-only",
            "--no-owner",
            "--no-privileges",
            "--schema",
            schema_name,
            "--dbname",
            conninfo,
        ],
        action=f"pg_dump schema {schema_name}",
        env=env,
    )


def _schema_head(database_url: str, schema_name: str) -> int:
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    if row is None or row[0] is None:
        raise RuntimeError(f"schema {schema_name} has no migration head")
    return int(row[0])


def _table_rows(
    conn: psycopg.Connection[Any],
    table: str,
) -> tuple[tuple[str, ...], list[tuple[object, ...]]]:
    columns = tuple(
        str(row[0])
        for row in conn.execute(
            """
            SELECT column_name
              FROM information_schema.columns
             WHERE table_schema = current_schema()
               AND table_name = %s
             ORDER BY ordinal_position
            """,
            (table,),
        ).fetchall()
    )
    if not columns:
        raise RuntimeError(f"seed table {table} is missing")
    selected = sql.SQL(", ").join(sql.Identifier(column) for column in columns)
    rows = conn.execute(
        sql.SQL("SELECT {} FROM {}").format(selected, sql.Identifier(table))
    ).fetchall()
    return columns, [tuple(row) for row in rows]


def _nonempty_application_tables(conn: psycopg.Connection[Any]) -> set[str]:
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
        ).fetchall()
    }
    nonempty: set[str] = set()
    for table in sorted(tables - _BOOKKEEPING_TABLES):
        row = conn.execute(
            sql.SQL("SELECT EXISTS (SELECT 1 FROM {} LIMIT 1)").format(sql.Identifier(table))
        ).fetchone()
        if row is not None and bool(row[0]):
            nonempty.add(table)
    return nonempty


def _seed_manifest(
    database_url: str,
    schema_name: str,
    *,
    require_known_fresh_tables: bool,
) -> dict[str, tuple[SeedRow, ...]]:
    manifest: dict[str, tuple[SeedRow, ...]] = {}
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
        if require_known_fresh_tables:
            unknown = _nonempty_application_tables(conn) - set(SEED_TABLE_SPECS)
            if unknown:
                raise RuntimeError(
                    "fresh schema has unconfigured seed-bearing tables: "
                    + ", ".join(sorted(unknown))
                )
        for table, spec in SEED_TABLE_SPECS.items():
            columns, rows = _table_rows(conn, table)
            manifest[table] = normalize_seed_rows(
                table=table,
                columns=columns,
                rows=rows,
                spec=spec,
            )
    return manifest


def _schema_diff(expected: str, actual: str, *, actual_label: str) -> tuple[str, ...]:
    return tuple(
        difflib.unified_diff(
            expected.splitlines(),
            actual.splitlines(),
            fromfile="fresh-from-migrations",
            tofile=actual_label,
            lineterm="",
        )
    )


def _write_artifacts(output_dir: Path, artifacts: Mapping[str, str]) -> None:
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        for name, content in artifacts.items():
            (output_dir / name).write_text(content, encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"could not write artifacts to {output_dir}: {exc}") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Diff a fresh gdaemon-built PostgreSQL schema against the live hub.",
        epilog=(
            "Examples:\n"
            "  GOBBY_TEST_PROTECT=1 uv run --no-sync python scripts/schema_diff.py "
            "--output-dir /tmp/gobby-schema-diff\n"
            "  GOBBY_TEST_PROTECT=1 uv run --no-sync python scripts/schema_diff.py "
            "--machine-schema gobby_test_flattened_fresh"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL DSN (default: DATABASE_URL, then bootstrap.yaml)",
    )
    parser.add_argument("--live-schema", default="public")
    parser.add_argument(
        "--machine-schema",
        help="Optional independently machine-built schema requiring exact DDL and seed equality",
    )
    parser.add_argument("--output-dir", type=Path, help="Write normalized DDL and seed artifacts")
    parser.add_argument("--pg-dump", default="pg_dump")
    return parser


def _database_url(argument: str | None) -> str:
    if argument:
        return argument
    environment_url = os.environ.get("DATABASE_URL")
    if environment_url:
        return environment_url
    configured = load_bootstrap(resolve_database_url=True).database_url
    if not configured:
        raise RuntimeError("PostgreSQL database URL is not configured")
    return configured


def _build_fresh_schema(database_url: str, schema_name: str) -> None:
    apply_schema(database_url, schema=schema_name)


def run(args: argparse.Namespace) -> int:
    database_url = _database_url(args.database_url)
    live_schema = _validate_identifier(args.live_schema, label="live schema")
    machine_schema = (
        _validate_identifier(args.machine_schema, label="machine schema")
        if args.machine_schema
        else None
    )
    accepted_tables = frozenset(_LIVE_ACCEPTED_TABLES)
    artifacts: dict[str, str] = {}
    errors: list[str] = []

    live_head = _schema_head(database_url, live_schema)
    live_dump = _dump_schema(database_url, live_schema, pg_dump=args.pg_dump)
    artifacts["live.raw.sql"] = live_dump
    artifacts["live.normalized.sql"] = normalize_schema_dump(
        live_dump,
        schema_name=live_schema,
        accepted_tables=accepted_tables,
    )

    with _scratch_schema(database_url, "fresh") as fresh_schema:
        _build_fresh_schema(database_url, fresh_schema)
        fresh_dump = _dump_schema(database_url, fresh_schema, pg_dump=args.pg_dump)
        fresh_normalized = normalize_schema_dump(
            fresh_dump,
            schema_name=fresh_schema,
            accepted_tables=accepted_tables,
        )
        fresh_seeds = _seed_manifest(
            database_url,
            fresh_schema,
            require_known_fresh_tables=True,
        )
        artifacts["fresh.normalized.sql"] = fresh_normalized
        artifacts["fresh.seed.json"] = seed_manifest_json(fresh_seeds)

        schema_diff = _schema_diff(
            fresh_normalized,
            artifacts["live.normalized.sql"],
            actual_label=f"live:{live_schema}",
        )
        if schema_diff:
            errors.append("schema diff:\n" + "\n".join(schema_diff))

        live_seeds = _seed_manifest(
            database_url,
            live_schema,
            require_known_fresh_tables=False,
        )
        artifacts["live.seed.json"] = seed_manifest_json(live_seeds)
        errors.extend(compare_live_seed_manifests(fresh_seeds, live_seeds, SEED_TABLE_SPECS))

        if machine_schema is not None:
            machine_dump = _dump_schema(database_url, machine_schema, pg_dump=args.pg_dump)
            machine_normalized = normalize_schema_dump(machine_dump, schema_name=machine_schema)
            artifacts["machine.normalized.sql"] = machine_normalized
            machine_schema_diff = _schema_diff(
                fresh_normalized,
                machine_normalized,
                actual_label=f"machine:{machine_schema}",
            )
            if machine_schema_diff:
                errors.append("machine schema diff:\n" + "\n".join(machine_schema_diff))
            machine_seeds = _seed_manifest(
                database_url,
                machine_schema,
                require_known_fresh_tables=True,
            )
            artifacts["machine.seed.json"] = seed_manifest_json(machine_seeds)
            errors.extend(compare_machine_seed_manifests(fresh_seeds, machine_seeds))

    if args.output_dir is not None:
        _write_artifacts(args.output_dir, artifacts)
        print(f"Artifacts: {args.output_dir}")
    print(f"Live migration head: {live_head}")
    for table, reason in _LIVE_ACCEPTED_TABLES.items():
        if _mentions_accepted_table(live_dump, frozenset({table})):
            print(f"Accepted live-only table {table}: {reason}")
    if errors:
        print("Unexplained divergences:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("clean diff output: 0 unexplained schema or seed divergences")
    return 0


def main() -> int:
    try:
        return run(_parser().parse_args())
    except (RuntimeError, ValueError) as exc:
        print(f"schema_diff: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
