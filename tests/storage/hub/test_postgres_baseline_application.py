"""Integration coverage for gdaemon-owned fresh schema application."""

from __future__ import annotations

import contextlib
import subprocess
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from gobby.storage.schema_contract import apply_schema, expected_schema_identity
from gobby.utils.native_bin import resolve_native_bin
from tests.fixtures.postgres import isolated_test_schema

pytestmark = pytest.mark.integration


@contextlib.contextmanager
def _isolated_test_database(base_url: str, label: str) -> Iterator[str]:
    database_name = f"gobby_test_{label}_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(base_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))

    database_url = make_conninfo(base_url, dbname=database_name)
    try:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute("CREATE EXTENSION IF NOT EXISTS pg_search")
        yield database_url
    finally:
        with psycopg.connect(base_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name))
            )


def _run_standalone_setup(binary_name: str, arguments: list[str]) -> None:
    binary = resolve_native_bin(binary_name)
    assert binary is not None, f"{binary_name} must be installed"
    result = subprocess.run(
        [binary, *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_gdaemon_applies_fresh_baseline_to_named_test_schema(
    postgres_database_url: str,
) -> None:
    with isolated_test_schema(postgres_database_url, "gdaemon") as schema_name:
        apply_schema(postgres_database_url, schema=schema_name)

        with psycopg.connect(
            postgres_database_url,
            autocommit=True,
            row_factory=dict_row,
        ) as connection:
            connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
            receipt = connection.execute(
                """
                SELECT version, filename, checksum
                FROM schema_migrations
                ORDER BY version
                """
            ).fetchall()
            projects = connection.execute("SELECT to_regclass('projects') AS relation").fetchone()

    identity = expected_schema_identity()
    assert receipt == [
        {
            "version": identity["latest_version"],
            "filename": f"baseline@{identity['baseline_version']}",
            "checksum": identity["latest_checksum"],
        }
    ]
    assert projects == {"relation": "projects"}


def test_gdaemon_adopts_gcode_standalone_tables(
    postgres_database_url: str,
) -> None:
    project_id = "11111111-1111-1111-1111-111111111111"
    with _isolated_test_database(postgres_database_url, "gcodeadopt") as database_url:
        _run_standalone_setup(
            "gcode",
            [
                "setup",
                "--standalone",
                "--database-url",
                database_url,
                "--no-services",
                "--quiet",
                "--format",
                "json",
            ],
        )
        with psycopg.connect(
            database_url,
            autocommit=True,
            row_factory=dict_row,
        ) as connection:
            connection.execute(
                "INSERT INTO code_indexed_projects (id, root_path) VALUES (%s, %s)",
                (project_id, "/tmp/adopted-gcode"),
            )

        apply_schema(database_url)

        with psycopg.connect(
            database_url,
            autocommit=True,
            row_factory=dict_row,
        ) as connection:
            adopted = connection.execute(
                "SELECT root_path FROM code_indexed_projects WHERE id = %s",
                (project_id,),
            ).fetchone()
            receipt = connection.execute(
                "SELECT COUNT(*) AS count FROM schema_migrations"
            ).fetchone()

    assert adopted == {"root_path": "/tmp/adopted-gcode"}
    assert receipt == {"count": 1}


def test_gdaemon_adopts_gwiki_standalone_tables(
    postgres_database_url: str,
) -> None:
    document_id = "adopted-gwiki-document"
    with _isolated_test_database(postgres_database_url, "gwikiadopt") as database_url:
        _run_standalone_setup(
            "gwiki",
            [
                "setup",
                "--standalone",
                "--database-url",
                database_url,
                "--no-services",
                "--quiet",
                "--format",
                "json",
            ],
        )
        with psycopg.connect(
            database_url,
            autocommit=True,
            row_factory=dict_row,
        ) as connection:
            connection.execute(
                """
                INSERT INTO gwiki_documents (
                    id, scope_kind, scope_id, path, title,
                    source_kind, content_hash, body
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    document_id,
                    "project",
                    "test-project",
                    "knowledge/adopted.md",
                    "Adopted",
                    "test",
                    "abc123",
                    "preserved body",
                ),
            )

        apply_schema(database_url)

        with psycopg.connect(
            database_url,
            autocommit=True,
            row_factory=dict_row,
        ) as connection:
            adopted = connection.execute(
                "SELECT body FROM gwiki_documents WHERE id = %s",
                (document_id,),
            ).fetchone()
            receipt = connection.execute(
                "SELECT COUNT(*) AS count FROM schema_migrations"
            ).fetchone()

    assert adopted == {"body": "preserved body"}
    assert receipt == {"count": 1}
