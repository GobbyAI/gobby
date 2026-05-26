"""Smoke test for the pinned pg_search score API."""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.integration


def test_pdb_score_compiles_and_orders() -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is required for the pg_search score API smoke test")

    psycopg = pytest.importorskip("psycopg")
    schema = f"gobby_pg_search_smoke_{uuid.uuid4().hex}"

    with psycopg.connect(dsn, autocommit=True) as conn:
        try:
            conn.execute(f'CREATE SCHEMA "{schema}"')
            conn.execute(
                f"""
                CREATE TABLE "{schema}".tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL
                )
                """
            )
            conn.execute(
                f"""
                CREATE INDEX tasks_search_bm25 ON "{schema}".tasks
                USING bm25 (id, title)
                WITH (key_field='id')
                """
            )
            conn.execute(
                f"""
                INSERT INTO "{schema}".tasks (id, title)
                VALUES
                    ('alpha-alpha', 'alpha alpha'),
                    ('alpha-beta', 'alpha beta'),
                    ('gamma', 'gamma')
                """
            )

            rows = conn.execute(
                f"""
                SELECT id, pdb.score(id) AS score
                FROM "{schema}".tasks
                WHERE title @@@ 'alpha'
                ORDER BY score DESC
                """
            ).fetchall()

            assert [row[0] for row in rows] == ["alpha-alpha", "alpha-beta"]
            assert rows[0][1] > rows[1][1]
        finally:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
