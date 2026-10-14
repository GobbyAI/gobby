"""Integration coverage for gdaemon-owned fresh schema application."""

from __future__ import annotations

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from gobby.storage.schema_contract import (
    SchemaContractError,
    apply_schema,
    expected_schema_identity,
)
from tests.fixtures.postgres import isolated_test_database, isolated_test_schema

pytestmark = pytest.mark.integration


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
    assert receipt
    assert receipt[0]["version"] == identity["baseline_version"]
    assert receipt[0]["filename"] == f"baseline@{identity['baseline_version']}"
    assert receipt[-1]["version"] == identity["latest_version"]
    assert projects == {"relation": "projects"}


def test_gdaemon_adopts_gcode_standalone_tables(
    postgres_database_url: str,
) -> None:
    project_id = "11111111-1111-1111-1111-111111111111"
    with isolated_test_database(postgres_database_url) as database_url:
        apply_schema(database_url)
        with psycopg.connect(
            database_url,
            autocommit=True,
            row_factory=dict_row,
        ) as connection:
            connection.execute(
                "INSERT INTO code_indexed_projects (id) VALUES (%s)",
                (project_id,),
            )

        apply_schema(database_url)

        with psycopg.connect(
            database_url,
            autocommit=True,
            row_factory=dict_row,
        ) as connection:
            adopted = connection.execute(
                "SELECT id FROM code_indexed_projects WHERE id = %s",
                (project_id,),
            ).fetchone()
            receipt = connection.execute(
                """
                SELECT version FROM schema_migrations
                ORDER BY version
                """
            ).fetchall()

    identity = expected_schema_identity()
    assert adopted is not None
    assert str(adopted["id"]) == project_id
    assert receipt[0]["version"] == identity["baseline_version"]
    assert receipt[-1]["version"] == identity["latest_version"]


def test_gdaemon_refuses_gwiki_standalone_tables(
    postgres_database_url: str,
) -> None:
    """A gwiki-only database is an unrecognized lineage, refused before any write.

    #21787 retired the standalone gwiki baseline adoption, so gdaemon plans no
    schema over these tables and leaves their rows alone.
    """
    document_id = "standalone-gwiki-document"
    with isolated_test_database(postgres_database_url) as database_url:
        with psycopg.connect(
            database_url,
            autocommit=True,
            row_factory=dict_row,
        ) as connection:
            connection.execute(
                """
                CREATE TABLE gwiki_documents (
                    id TEXT PRIMARY KEY,
                    scope_kind TEXT NOT NULL DEFAULT 'project',
                    scope_id TEXT NOT NULL DEFAULT 'test-project',
                    path TEXT NOT NULL DEFAULT 'knowledge/adopted.md',
                    title TEXT NOT NULL DEFAULT 'Adopted',
                    source_kind TEXT NOT NULL DEFAULT 'test',
                    content_hash TEXT NOT NULL DEFAULT 'abc123',
                    provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
                    body TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                "CREATE TABLE gwiki_chunks (id TEXT PRIMARY KEY, document_id TEXT NOT NULL)"
            )
            connection.execute("CREATE TABLE gwiki_sources (id TEXT PRIMARY KEY)")
            connection.execute(
                """
                INSERT INTO gwiki_documents (id, body)
                VALUES (%s, %s)
                """,
                (document_id, "preserved body"),
            )

        with pytest.raises(SchemaContractError, match="unrecognized schema lineage"):
            apply_schema(database_url)

        with psycopg.connect(
            database_url,
            autocommit=True,
            row_factory=dict_row,
        ) as connection:
            preserved = connection.execute(
                "SELECT body FROM gwiki_documents WHERE id = %s",
                (document_id,),
            ).fetchone()
            receipts = connection.execute(
                "SELECT to_regclass('schema_migrations') AS receipts"
            ).fetchone()

    assert preserved == {"body": "preserved body"}
    assert receipts == {"receipts": None}
