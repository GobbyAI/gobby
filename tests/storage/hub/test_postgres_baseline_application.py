"""Integration coverage for gdaemon-owned fresh schema application."""

from __future__ import annotations

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from gobby.storage.schema_contract import apply_schema, expected_schema_identity
from tests.fixtures.postgres import isolated_test_schema

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
    assert receipt == [
        {
            "version": identity["latest_version"],
            "filename": f"baseline@{identity['baseline_version']}",
            "checksum": identity["latest_checksum"],
        }
    ]
    assert projects == {"relation": "projects"}
