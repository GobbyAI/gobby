from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from tests.fixtures.postgres import isolated_test_schema

pytestmark = pytest.mark.integration

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "gobby"
    / "storage"
    / "migrations"
    / "374_provider_capability_matrix.sql"
)


def test_route_rows_cascade_on_capability_delete(postgres_database_url: str) -> None:
    migration_sql = MIGRATION_PATH.read_text(encoding="utf-8")

    with isolated_test_schema(postgres_database_url, "provcap") as schema_name:
        with psycopg.connect(postgres_database_url, autocommit=True) as connection:
            connection.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name)))
            connection.execute(migration_sql)
            connection.execute(
                """
                INSERT INTO provider_model_capabilities (
                    provider,
                    canonical_model,
                    display_name,
                    generation,
                    provenance
                ) VALUES ('openai', 'gpt-test', 'GPT Test', 1, '{}'::jsonb)
                """
            )
            connection.execute(
                """
                INSERT INTO provider_model_routes (
                    provider,
                    canonical_model,
                    speed_mode,
                    selector,
                    generation,
                    provenance
                ) VALUES ('openai', 'gpt-test', 'standard', 'gpt-test', 1, '{}'::jsonb)
                """
            )

            connection.execute(
                """
                DELETE FROM provider_model_capabilities
                WHERE provider = 'openai' AND canonical_model = 'gpt-test'
                """
            )
            route_count = connection.execute(
                "SELECT COUNT(*) FROM provider_model_routes"
            ).fetchone()

    assert route_count is not None
    assert route_count[0] == 0
