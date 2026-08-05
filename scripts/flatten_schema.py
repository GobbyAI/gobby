#!/usr/bin/env python3
"""Regenerate the post-M0 PostgreSQL baseline and flatten evidence."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib
import json
import os
import re
import sys
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from gobby.config.bootstrap import load_bootstrap
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.migration_flatten import MigrationReceipt
from gobby.storage.migrations import (
    BASELINE_VERSION,
    MigrationRunner,
    _split_statements_respecting_dollar_quotes,
)

if TYPE_CHECKING or __package__:
    from scripts.schema_diff import (
        SEED_TABLE_SPECS,
        _dump_schema,
        _postgres_client_connection,
        _run_postgres_client,
        _seed_manifest,
        _split_top_level_commas,
        normalize_schema_dump,
        seed_manifest_json,
    )
else:
    _schema_diff = importlib.import_module("schema_diff")
    SEED_TABLE_SPECS = _schema_diff.SEED_TABLE_SPECS
    _dump_schema = _schema_diff._dump_schema
    _postgres_client_connection = _schema_diff._postgres_client_connection
    _run_postgres_client = _schema_diff._run_postgres_client
    _seed_manifest = _schema_diff._seed_manifest
    _split_top_level_commas = _schema_diff._split_top_level_commas
    normalize_schema_dump = _schema_diff.normalize_schema_dump
    seed_manifest_json = _schema_diff.seed_manifest_json

_TARGET_BASELINE_VERSION = 375
_SEED_TABLES = (
    "projects",
    "sessions",
    "task_stages_registry",
    "task_type_default_stages",
)
_PRESERVED_MIGRATION_VERSIONS = frozenset({354, 362, 369, 370, 371, 372, 373})
_DIVERGENCE_LEDGER = """# Pre-flatten divergence ledger

- Migration 346 is historical and unattested because pre-354 rows intentionally
  carry no filename/checksum. Migration 355 reconciles its reused-slot lineage.
- Migration 356 reconciles the recorded live-schema drift before this snapshot.
- `gobby_install_ownership` is installer-owned and accepted outside baseline
  seed authority.
- `gwiki_*` tables are gcore-owned standalone-adoption objects and are excluded
  from Gobby baseline DDL comparison.

The pinned normalized DDL and seed manifest were produced only after the live
comparison reported zero unexplained divergences.
"""


@dataclass(frozen=True)
class GeneratedFlatten:
    baseline_sql: str
    normalized_ddl: str
    seed_manifest: str
    applied_versions: tuple[int, ...]
    receipts: tuple[MigrationReceipt, ...]


@dataclass(frozen=True)
class ClusterAuthorizationSql:
    bootstrap: str
    complete: str


def _strip_dump_noise(dump: str) -> str:
    return "\n".join(
        line
        for line in dump.splitlines()
        if not line.lstrip().startswith(("--", "\\restrict", "\\unrestrict"))
    )


def _is_omitted_dump_statement(statement: str, application_schema: str) -> bool:
    compact = re.sub(r"\s+", " ", statement).strip()
    upper = compact.upper()
    schema = re.escape(application_schema)
    return (
        not compact
        or (upper.startswith("SET ") and upper != "SET CHECK_FUNCTION_BODIES = FALSE")
        or upper.startswith("SELECT PG_CATALOG.SET_CONFIG(")
        or upper.startswith("CREATE EXTENSION ")
        or upper.startswith("COMMENT ON EXTENSION ")
        or re.fullmatch(rf'CREATE SCHEMA (?:"{schema}"|{schema})', compact, re.IGNORECASE)
        is not None
        or re.match(rf'^COMMENT ON SCHEMA (?:"{schema}"|{schema})\b', compact, re.IGNORECASE)
        is not None
    )


def _unqualify_application_schema(statement: str, application_schema: str) -> str:
    schema = re.escape(application_schema)
    return re.sub(
        rf'(?<![A-Za-z0-9_$])(?:"{schema}"|{schema})\.',
        "",
        statement,
        flags=re.IGNORECASE,
    )


def _render_dump_part(dump: str, *, application_schema: str) -> list[str]:
    rendered: list[str] = []
    for raw in _split_statements_respecting_dollar_quotes(_strip_dump_noise(dump)):
        statement = raw.strip()
        if _is_omitted_dump_statement(statement, application_schema):
            continue
        if re.match(r"CREATE\s+POLICY\s+gobby_migration_owner_access\b", statement, re.I):
            statement = re.sub(
                r'(\bTO\s+)(?:"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_$]*)(?=\s+USING\b)',
                r"\g<1>CURRENT_USER",
                statement,
                count=1,
                flags=re.I,
            )
        if re.match(r"ALTER\s+DEFAULT\s+PRIVILEGES\s+FOR\s+ROLE\b", statement, re.I):
            statement = re.sub(
                r'(\bFOR\s+ROLE\s+)(?:"(?:[^"]|"")*"|[A-Za-z_][A-Za-z0-9_$]*)'
                r"(?=\s+IN\s+SCHEMA\b)",
                r"\g<1>CURRENT_USER",
                statement,
                count=1,
                flags=re.I,
            )
        rendered.append(_unqualify_application_schema(statement, application_schema) + ";")
    return rendered


def render_baseline(
    schema_dump: str,
    seed_dump: str,
    *,
    application_schema: str,
) -> str:
    """Render stable executable SQL from schema and seed ``pg_dump`` output."""

    statements = _render_dump_part(schema_dump, application_schema=application_schema)
    statements.extend(_render_dump_part(seed_dump, application_schema=application_schema))
    return (
        "-- Generated by scripts/flatten_schema.py. Do not edit by hand.\n\n"
        + "\n\n".join(statements)
        + "\n"
    )


def build_evidence_manifest(
    *,
    baseline_version: int,
    baseline_sql: str,
    normalized_ddl: str,
    seed_manifest: str,
    divergence_ledger: str,
    applied_versions: Sequence[int],
    receipts: Sequence[MigrationReceipt],
) -> str:
    """Return canonical JSON pinning every pre-flatten verification input."""

    payload = {
        "applied_versions": list(applied_versions),
        "baseline_version": baseline_version,
        "baseline_schema": {
            "path": "src/gobby/storage/postgres_baseline_schema.sql",
            "sha256": hashlib.sha256(baseline_sql.encode()).hexdigest(),
        },
        "divergence_ledger": {
            "path": "docs/evidence/pre-flatten/divergence-ledger.md",
            "sha256": hashlib.sha256(divergence_ledger.encode()).hexdigest(),
        },
        "normalized_ddl": {
            "path": "migrated-fresh.normalized.sql",
            "sha256": hashlib.sha256(normalized_ddl.encode()).hexdigest(),
        },
        "receipts": [
            {
                "checksum": receipt.checksum,
                "filename": receipt.filename,
                "version": receipt.version,
            }
            for receipt in sorted(receipts, key=lambda item: item.version)
        ],
        "seed_manifest": {
            "path": "migrated-fresh.seed.json",
            "sha256": hashlib.sha256(seed_manifest.encode()).hexdigest(),
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


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


@contextmanager
def _scratch_database(database_url: str, label: str) -> Iterator[str]:
    database_name = f"gobby_flatten_{label}_{uuid.uuid4().hex}"
    scratch_url = make_conninfo(database_url, dbname=database_name)
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    try:
        with psycopg.connect(scratch_url, autocommit=True) as connection:
            connection.execute("CREATE EXTENSION IF NOT EXISTS pg_search")
        yield scratch_url
    finally:
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(database_name)
                )
            )


def _execute_script(connection: psycopg.Connection[Any], script: str) -> None:
    for statement in _split_statements_respecting_dollar_quotes(script):
        if statement.strip():
            try:
                connection.execute(statement)
            except psycopg.Error as exc:
                label = re.sub(r"\s+", " ", statement).strip()[:160]
                raise RuntimeError(f"generated statement failed ({label}): {exc}") from exc


def _build_migrated_fresh(database_url: str, source_baseline: Path) -> None:
    baseline_sql = source_baseline.read_text(encoding="utf-8")
    with psycopg.connect(database_url) as connection:
        _execute_script(connection, baseline_sql)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (%s, NOW())",
            (BASELINE_VERSION,),
        )
    database = PostgresHubDatabase(database_url)
    try:
        database.open()
        runner = MigrationRunner(
            database,
            autocommit_connection=database._open_advisory_lock_connection,
        )
        runner.apply_pending(fresh_schema=True)
    finally:
        database.close()


def _dump_public_schema(database_url: str, *, pg_dump: str) -> str:
    conninfo, env = _postgres_client_connection(database_url)
    return _run_postgres_client(
        [
            pg_dump,
            "--schema-only",
            "--no-owner",
            "--schema=public",
            "--dbname",
            conninfo,
        ],
        action="pg_dump migrated-fresh schema",
        env=env,
    )


def _dump_seed_rows(database_url: str, *, pg_dump: str) -> str:
    conninfo, env = _postgres_client_connection(database_url)
    table_args = [item for table in _SEED_TABLES for item in ("--table", f"public.{table}")]
    return _run_postgres_client(
        [
            pg_dump,
            "--data-only",
            "--inserts",
            "--column-inserts",
            "--rows-per-insert=1",
            *table_args,
            "--dbname",
            conninfo,
        ],
        action="pg_dump migrated-fresh seeds",
        env=env,
    )


def _canonicalize_seed_dump(seed_dump: str) -> str:
    inserts: list[str] = []
    pattern = re.compile(
        r"^INSERT INTO (?:public\.)?(?P<table>[A-Za-z_][A-Za-z0-9_$]*) "
        r"\((?P<columns>.*)\) VALUES \((?P<values>.*)\)$",
        re.DOTALL,
    )
    for statement in _split_statements_respecting_dollar_quotes(_strip_dump_noise(seed_dump)):
        compact = re.sub(r"\s+", " ", statement).strip()
        if not compact.upper().startswith("INSERT INTO "):
            continue
        match = pattern.fullmatch(compact)
        if match is None:
            raise RuntimeError(f"could not canonicalize seed INSERT: {compact[:120]}")
        table = match.group("table")
        columns = [item.strip() for item in _split_top_level_commas(match.group("columns"))]
        values = [item.strip() for item in _split_top_level_commas(match.group("values"))]
        if len(columns) != len(values):
            raise RuntimeError(f"seed INSERT column/value mismatch for {table}")
        mutable = SEED_TABLE_SPECS[table].machine_mutable_columns
        values = [
            "NOW()" if column.strip('"') in mutable else value
            for column, value in zip(columns, values, strict=True)
        ]
        inserts.append(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(values)});")
    return "\n\n".join(sorted(inserts)) + "\n"


def _migration_state(database_url: str) -> tuple[tuple[int, ...], tuple[MigrationReceipt, ...]]:
    with psycopg.connect(database_url) as connection:
        rows = connection.execute(
            "SELECT version, filename, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    versions = tuple(int(row[0]) for row in rows)
    receipts = tuple(
        MigrationReceipt(int(version), str(filename), str(checksum))
        for version, filename, checksum in rows
        if int(version) >= 354
    )
    if not versions or versions[-1] != _TARGET_BASELINE_VERSION:
        raise RuntimeError(
            f"migrated-fresh head is {versions[-1] if versions else None}; "
            f"expected {_TARGET_BASELINE_VERSION}"
        )
    if any(receipt.filename == "None" or receipt.checksum == "None" for receipt in receipts):
        raise RuntimeError("migrated-fresh receipt bookkeeping is incomplete")
    return versions, receipts


def _cluster_authorization_sql(migrations_dir: Path) -> ClusterAuthorizationSql:
    selected: list[tuple[int, Path]] = []
    for path in migrations_dir.glob("*.sql"):
        prefix = path.name.split("_", 1)[0]
        if prefix.isdigit() and int(prefix) in _PRESERVED_MIGRATION_VERSIONS:
            selected.append((int(prefix), path))
    versions = {version for version, _path in selected}
    if versions != _PRESERVED_MIGRATION_VERSIONS:
        missing = sorted(_PRESERVED_MIGRATION_VERSIONS - versions)
        raise RuntimeError(f"preserved migration inputs are missing: {missing}")
    complete = (
        "\n\n".join(
            path.read_text(encoding="utf-8").rstrip() for _version, path in sorted(selected)
        )
        + "\n"
    )
    migration_369 = next(path for version, path in selected if version == 369).read_text(
        encoding="utf-8"
    )
    marker = "CREATE TABLE IF NOT EXISTS gobby_agent_auth.principal_bindings"
    bootstrap, separator, _remainder = migration_369.partition(marker)
    if not separator:
        raise RuntimeError("migration 369 cluster bootstrap marker is missing")
    return ClusterAuthorizationSql(bootstrap=bootstrap.rstrip() + "\n", complete=complete)


def _strip_cluster_owned_public_statements(schema_dump: str) -> str:
    retained: list[str] = []
    for statement in _split_statements_respecting_dollar_quotes(_strip_dump_noise(schema_dump)):
        compact = re.sub(r"\s+", " ", statement).strip()
        if compact.upper().startswith("CREATE POLICY ") and "gobby_agent_auth" in compact:
            continue
        if "gobby_maintenance_epoch_login_guard" in compact:
            continue
        if "gobby_maintenance_epoch_login_fence" in compact:
            continue
        retained.append(statement.strip() + ";")
    return "\n\n".join(retained) + "\n"


def _verify_flattened_fresh(
    database_url: str,
    generated: GeneratedFlatten,
    *,
    pg_dump: str,
) -> None:
    with psycopg.connect(database_url) as connection:
        _execute_script(connection, generated.baseline_sql)
        connection.execute(
            """
            INSERT INTO schema_migrations(version, filename, checksum, applied_at)
            VALUES (%s, %s, %s, NOW())
            """,
            (
                _TARGET_BASELINE_VERSION,
                f"baseline@{_TARGET_BASELINE_VERSION}",
                hashlib.sha256(generated.baseline_sql.encode()).hexdigest(),
            ),
        )
    flattened_dump = _dump_schema(database_url, "public", pg_dump=pg_dump)
    flattened_ddl = normalize_schema_dump(flattened_dump, schema_name="public")
    if flattened_ddl != generated.normalized_ddl:
        difference = "\n".join(
            difflib.unified_diff(
                generated.normalized_ddl.splitlines(),
                flattened_ddl.splitlines(),
                fromfile="migrated-fresh",
                tofile="flattened-fresh",
                lineterm="",
            )
        )
        raise RuntimeError(
            "flattened-fresh normalized DDL differs from migrated-fresh:\n" + difference
        )
    flattened_seeds = _seed_manifest(
        database_url,
        "public",
        require_known_fresh_tables=True,
    )
    migrated_seeds = json.loads(generated.seed_manifest)
    flattened_seed_json = seed_manifest_json(flattened_seeds)
    if json.loads(flattened_seed_json) != migrated_seeds:
        raise RuntimeError("flattened-fresh seed manifest differs from migrated-fresh")


def generate(
    database_url: str,
    *,
    source_baseline: Path,
    migrations_dir: Path,
    pg_dump: str,
) -> GeneratedFlatten:
    if BASELINE_VERSION >= _TARGET_BASELINE_VERSION:
        raise RuntimeError("generation requires the pre-flatten runner and migration inputs")
    cluster_sql = _cluster_authorization_sql(migrations_dir)
    with _scratch_database(database_url, "migrated") as migrated_url:
        _build_migrated_fresh(migrated_url, source_baseline)
        raw_schema = _dump_public_schema(migrated_url, pg_dump=pg_dump)
        seed_dump = _canonicalize_seed_dump(_dump_seed_rows(migrated_url, pg_dump=pg_dump))
        migrated_dump = _dump_schema(migrated_url, "public", pg_dump=pg_dump)
        normalized_ddl = normalize_schema_dump(migrated_dump, schema_name="public")
        seeds = _seed_manifest(migrated_url, "public", require_known_fresh_tables=True)
        applied_versions, receipts = _migration_state(migrated_url)
    public_schema = _strip_cluster_owned_public_statements(raw_schema)
    rendered_public = render_baseline(public_schema, seed_dump, application_schema="public")
    baseline_sql = (
        "-- Generated by scripts/flatten_schema.py. Do not edit by hand.\n\n"
        + cluster_sql.bootstrap
        + "\n"
        + rendered_public.removeprefix(
            "-- Generated by scripts/flatten_schema.py. Do not edit by hand.\n\n"
        ).rstrip()
        + "\n\n"
        + cluster_sql.complete
    )
    generated = GeneratedFlatten(
        baseline_sql=baseline_sql,
        normalized_ddl=normalized_ddl,
        seed_manifest=seed_manifest_json(seeds),
        applied_versions=applied_versions,
        receipts=receipts,
    )
    with _scratch_database(database_url, "flattened") as flattened_url:
        _verify_flattened_fresh(flattened_url, generated, pg_dump=pg_dump)
    return generated


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def write_generated(
    generated: GeneratedFlatten,
    *,
    baseline_path: Path,
    evidence_dir: Path,
) -> str:
    manifest = build_evidence_manifest(
        baseline_version=_TARGET_BASELINE_VERSION,
        baseline_sql=generated.baseline_sql,
        normalized_ddl=generated.normalized_ddl,
        seed_manifest=generated.seed_manifest,
        divergence_ledger=_DIVERGENCE_LEDGER,
        applied_versions=generated.applied_versions,
        receipts=generated.receipts,
    )
    _write_atomic(baseline_path, generated.baseline_sql)
    _write_atomic(evidence_dir / "migrated-fresh.normalized.sql", generated.normalized_ddl)
    _write_atomic(evidence_dir / "migrated-fresh.seed.json", generated.seed_manifest)
    _write_atomic(evidence_dir / "divergence-ledger.md", _DIVERGENCE_LEDGER)
    _write_atomic(evidence_dir / "manifest.json", manifest)
    return manifest


def _parser() -> argparse.ArgumentParser:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url")
    parser.add_argument("--pg-dump", default="pg_dump")
    parser.add_argument(
        "--baseline-path",
        type=Path,
        default=repo_root / "src/gobby/storage/postgres_baseline_schema.sql",
    )
    parser.add_argument(
        "--migrations-dir",
        type=Path,
        default=repo_root / "src/gobby/storage/migrations",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=repo_root / "docs/evidence/pre-flatten",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    generated = generate(
        _database_url(args.database_url),
        source_baseline=args.baseline_path,
        migrations_dir=args.migrations_dir,
        pg_dump=args.pg_dump,
    )
    manifest = write_generated(
        generated,
        baseline_path=args.baseline_path,
        evidence_dir=args.evidence_dir,
    )
    manifest_hash = hashlib.sha256(manifest.encode()).hexdigest()
    print(f"Generated baseline version: {_TARGET_BASELINE_VERSION}")
    print(f"Baseline: {args.baseline_path}")
    print(f"Evidence: {args.evidence_dir}")
    print(f"Evidence manifest sha256: {manifest_hash}")
    print("flattened-fresh and migrated-fresh DDL/seeds are identical")
    return 0


def main() -> int:
    try:
        return run(_parser().parse_args())
    except (OSError, RuntimeError, psycopg.Error) as exc:
        print(f"flatten_schema: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
