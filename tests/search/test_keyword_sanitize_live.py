"""Live pg_search checks for user text that used to escape into an unparseable query."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from gobby.search.keyword import sanitize_pg_search_query

pytestmark = pytest.mark.integration

_UNPARSEABLE_INPUTS = [
    pytest.param('"name":', id="json-key"),
    pytest.param('"name": "task-executor"', id="json-key-and-value"),
    pytest.param(
        '#NNNNN "Hook path saturation: admission_wait and rule_evaluation up to 13s"`,',
        id="phrase-glued-to-backtick",
    ),
    pytest.param(
        "`COALESCE(isolation_overlay_project_id, project_id)`",
        id="backtick-parentheses",
    ),
    pytest.param(
        "`CATALOG_BACKEND=postgresql` `DATABASE_URL`. "
        "`Buylist/data/catalog.sqlite3` cutover: existing 5:00",
        id="dream-text",
    ),
]


@pytest.fixture
def content_index() -> Iterator[tuple[Any, str]]:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is required for the pg_search sanitizer parse test")
    psycopg = pytest.importorskip("psycopg")
    schema = f"gobby_pg_search_sanitize_{uuid.uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
        try:
            conn.execute(
                f"""
                CREATE TABLE "{schema}".chunks (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL
                )
                """
            )
            conn.execute(
                f"""
                CREATE INDEX chunks_search_bm25 ON "{schema}".chunks
                USING bm25 (id, content)
                WITH (key_field='id')
                """
            )
            yield conn, schema
        finally:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.mark.parametrize("raw", _UNPARSEABLE_INPUTS)
def test_sanitized_queries_parse(content_index: tuple[Any, str], raw: str) -> None:
    conn, schema = content_index
    sanitized = sanitize_pg_search_query(raw)
    rows = conn.execute(
        f'SELECT id FROM "{schema}".chunks WHERE content @@@ %s',
        (sanitized,),
    ).fetchall()
    assert sanitized
    assert rows == []


def test_literal_json_key_search_returns_stored_chunk(
    content_index: tuple[Any, str],
) -> None:
    conn, schema = content_index
    conn.execute(
        f'INSERT INTO "{schema}".chunks (id, content) VALUES (%s, %s)',
        ("row-1", '"name": "x"'),
    )
    sanitized = sanitize_pg_search_query('"name":')
    rows = conn.execute(
        f'SELECT id FROM "{schema}".chunks WHERE content @@@ %s',
        (sanitized,),
    ).fetchall()
    assert [row[0] for row in rows] == ["row-1"]
