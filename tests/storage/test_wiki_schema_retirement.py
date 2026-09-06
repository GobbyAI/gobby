"""Bounded wiki retirement SQL against isolated PostgreSQL schemas."""

from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from tests.fixtures.postgres import isolated_test_schema

pytestmark = pytest.mark.integration

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "crates/gcore/assets/schema/migrations/426_retire_legacy_wiki.sql"
).read_text(encoding="utf-8")
_WIKI_TABLES = (
    "gwiki_documents",
    "gwiki_chunks",
    "gwiki_links",
    "gwiki_sources",
    "gwiki_ingestions",
)
_PRESERVED_TABLES = (
    "sessions",
    "session_handoffs",
    "memories",
    "projects",
    "code_indexed_files",
    "grant_reservations",
)


def test_retirement_deletes_only_wiki_tables_and_repeats(postgres_database_url: str) -> None:
    with (
        isolated_test_schema(postgres_database_url, "wikiretire") as owned,
        isolated_test_schema(postgres_database_url, "wikikeep") as outside,
        psycopg.connect(postgres_database_url, autocommit=True) as connection,
    ):
        for schema in (owned, outside):
            for table in (*_WIKI_TABLES, *_PRESERVED_TABLES):
                relation = sql.Identifier(schema, table)
                connection.execute(
                    sql.SQL("CREATE TABLE {} (id integer PRIMARY KEY)").format(relation)
                )
                connection.execute(sql.SQL("INSERT INTO {} VALUES (7)").format(relation))
        connection.execute(
            sql.SQL("SET search_path TO {}, {}").format(
                sql.Identifier(owned), sql.Identifier(outside)
            )
        )

        for _ in range(2):
            connection.execute(_MIGRATION)
            remaining = connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = %s", (owned,)
            ).fetchall()
            assert {row[0] for row in remaining} == set(_PRESERVED_TABLES)
            for schema, tables in (
                (owned, _PRESERVED_TABLES),
                (outside, (*_WIKI_TABLES, *_PRESERVED_TABLES)),
            ):
                for table in tables:
                    rows = connection.execute(
                        sql.SQL("SELECT id FROM {}").format(sql.Identifier(schema, table))
                    ).fetchall()
                    assert rows == [(7,)], (schema, table)


def test_retirement_refuses_uninventoried_dependencies_atomically(
    postgres_database_url: str,
) -> None:
    with (
        isolated_test_schema(postgres_database_url, "wikidependency") as schema,
        psycopg.connect(postgres_database_url, autocommit=True) as connection,
    ):
        connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        for table in _WIKI_TABLES:
            connection.execute(
                sql.SQL("CREATE TABLE {} (id integer)").format(sql.Identifier(table))
            )
            connection.execute(sql.SQL("INSERT INTO {} VALUES (7)").format(sql.Identifier(table)))
        connection.execute("CREATE VIEW unrelated_view AS SELECT id FROM gwiki_documents")

        with pytest.raises(psycopg.errors.DependentObjectsStillExist):
            connection.execute(_MIGRATION)

        for table in (*_WIKI_TABLES, "unrelated_view"):
            rows = connection.execute(
                sql.SQL("SELECT id FROM {}").format(sql.Identifier(table))
            ).fetchall()
            assert rows == [(7,)], table
