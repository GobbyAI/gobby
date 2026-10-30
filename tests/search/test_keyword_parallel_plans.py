"""Inspect BM25 plans without executing pg_search inside a parallel worker."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from gobby.memory.dream import related
from gobby.memory.dream.models import DreamCandidate
from gobby.search.keyword import fetch_all
from gobby.storage.hub.async_ops import AsyncDBWork, run_bounded_db
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "table,column", [("code_content_chunks", "content"), ("code_symbols", "name")]
)
def test_python_keyword_search_keeps_bm25_out_of_parallel_workers(
    temp_db: HubDatabase, table: str, column: str
) -> None:
    sql = (
        f"EXPLAIN SELECT id, pdb.score(id) FROM {table} "
        f"WHERE {column} @@@ 'marker' ORDER BY pdb.score(id) DESC, id LIMIT 10"
    )
    with temp_db.transaction() as txn:
        txn.execute("SET LOCAL parallel_setup_cost = 0")
        txn.execute("SET LOCAL parallel_tuple_cost = 0")
        txn.execute("SET LOCAL min_parallel_table_scan_size = 0")
        txn.execute("SET LOCAL min_parallel_index_scan_size = 0")
        txn.execute("SET LOCAL debug_parallel_query = on")
        txn.execute("SET LOCAL enable_seqscan = off")
        txn.execute("SET LOCAL max_parallel_workers_per_gather = 4")
        baseline = "\n".join(str(row["QUERY PLAN"]) for row in txn.execute(sql).fetchall())
        assert "Gather" in baseline, baseline
        assert "Custom Scan" in baseline, baseline

        plan = "\n".join(str(row["QUERY PLAN"]) for row in fetch_all(temp_db, sql, ()))
        assert "Gather" not in plan, plan
        assert "Custom Scan" in plan, plan


@pytest.mark.asyncio
async def test_async_keyword_search_keeps_bm25_out_of_parallel_workers(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = related.RetrievalScope.global_only()
    statement = related.render_bulk_keyword_statement(
        [("candidate", "marker")], db=temp_db, scope=scope, fetch_limit=10
    )
    assert statement is not None
    sql, params = statement
    explain = "EXPLAIN " + sql

    def render_explain(*_args: object, **_kwargs: object) -> tuple[str, tuple[Any, ...]]:
        return explain, params

    plans: list[str] = []

    async def inspect_plan(
        work: AsyncDBWork[list[dict[str, Any]]], *, conninfo: str, deadline_seconds: float
    ) -> list[dict[str, Any]]:
        async def inspect(connection: psycopg.AsyncConnection[Any], remaining: float) -> list[Any]:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute("SET LOCAL parallel_setup_cost = 0")
                await cursor.execute("SET LOCAL parallel_tuple_cost = 0")
                await cursor.execute("SET LOCAL min_parallel_table_scan_size = 0")
                await cursor.execute("SET LOCAL min_parallel_index_scan_size = 0")
                await cursor.execute("SET LOCAL debug_parallel_query = on")
                await cursor.execute("SET LOCAL enable_seqscan = off")
                await cursor.execute("SET LOCAL max_parallel_workers_per_gather = 4")
                await cursor.execute(explain, params)
                plans.append("\n".join(str(row["QUERY PLAN"]) for row in await cursor.fetchall()))
            rows = await work(connection, remaining)
            plans.append("\n".join(str(row["QUERY PLAN"]) for row in rows))
            return []

        return await run_bounded_db(inspect, conninfo=conninfo, deadline_seconds=deadline_seconds)

    monkeypatch.setattr(related, "render_bulk_keyword_statement", render_explain)
    monkeypatch.setattr(related, "run_bounded_db", inspect_plan)
    now = datetime.now(UTC)
    candidate = DreamCandidate(
        id="candidate",
        content="marker",
        memory_type="fact",
        project_id="project",
        is_global=True,
        source_type=None,
        source_session_id=None,
        tags=[],
        age_days=1.0,
        access_count=0,
        created_at=now,
        updated_at=now,
        last_accessed_at=None,
    )
    assert (
        await related._keyword_hits_bulk([candidate], db=temp_db, scope=scope, fetch_limit=10) == {}
    )
    baseline, guarded = plans
    assert "Gather" in baseline and "Custom Scan" in baseline, baseline
    assert "Gather" not in guarded, guarded
    assert "Custom Scan" in guarded, guarded
