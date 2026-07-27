"""Storage coverage for oversized MCP tool results."""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest

from gobby.config.features import ToolResultOffloadConfig
from gobby.search.keyword import (
    MAX_PG_SEARCH_QUERY_CHARS,
    pick_search_backend,
    sanitize_pg_search_query,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tool_results import ToolResultStore


def _config(*, chunk_chars: int = 200, retention_days: int = 3) -> ToolResultOffloadConfig:
    return ToolResultOffloadConfig(chunk_chars=chunk_chars, retention_days=retention_days)


def _save(
    store: ToolResultStore,
    *,
    project_id: str,
    content: str,
    total_chars: int | None = None,
) -> str:
    return store.save(
        project_id=project_id,
        session_id=str(uuid.uuid4()),
        server_name="example-server",
        tool_name="large-tool",
        content=content,
        content_kind="text",
        total_chars=total_chars if total_chars is not None else len(content),
    )


def test_save_chunks_on_line_boundaries_and_hard_splits_long_lines(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    store = ToolResultStore(temp_db, _config(chunk_chars=200))
    content = f"{'a' * 99}\n{'b' * 99}\n{'c' * 250}\ntail"

    result_id = _save(
        store,
        project_id=sample_project["id"],
        content=content,
        total_chars=len(content) + 25,
    )

    result = temp_db.fetchone(
        "SELECT total_chars, stored_chars FROM tool_results WHERE id = %s",
        (result_id,),
    )
    chunks = temp_db.fetchall(
        """SELECT ordinal, start_offset, end_offset, content
           FROM tool_result_chunks
           WHERE result_id = %s
           ORDER BY ordinal""",
        (result_id,),
    )

    assert result is not None
    assert result["total_chars"] == len(content) + 25
    assert result["stored_chars"] == len(content)
    assert [
        (row["ordinal"], row["start_offset"], row["end_offset"], row["content"]) for row in chunks
    ] == [
        (0, 0, 200, content[0:200]),
        (1, 200, 400, content[200:400]),
        (2, 400, len(content), content[400:]),
    ]


def test_save_persists_total_chars_above_signed_32_bit_range(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    store = ToolResultStore(temp_db, _config())
    total_chars = 2**31 + 17

    result_id = _save(
        store,
        project_id=sample_project["id"],
        content="stored excerpt",
        total_chars=total_chars,
    )

    result = temp_db.fetchone(
        "SELECT total_chars FROM tool_results WHERE id = %s",
        (result_id,),
    )
    assert result is not None
    assert result["total_chars"] == total_chars


def test_save_runs_cleanup_with_configured_retention(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    store = ToolResultStore(temp_db, _config(retention_days=3))
    expired_id = _save(store, project_id=sample_project["id"], content="expired")
    temp_db.execute(
        "UPDATE tool_results SET created_at = NOW() - INTERVAL '4 days' WHERE id = %s",
        (expired_id,),
    )

    current_id = _save(store, project_id=sample_project["id"], content="current")

    assert temp_db.fetchone("SELECT id FROM tool_results WHERE id = %s", (expired_id,)) is None
    assert temp_db.fetchone("SELECT id FROM tool_results WHERE id = %s", (current_id,)) is not None


def test_get_slice_pages_content_and_reports_offsets(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    store = ToolResultStore(temp_db, _config())
    content = "".join(str(index % 10) for index in range(450))
    result_id = _save(
        store,
        project_id=sample_project["id"],
        content=content,
        total_chars=500,
    )

    page = store.get_slice(result_id, sample_project["id"], offset=190, limit=30)
    final_page = store.get_slice(result_id, sample_project["id"], offset=440, limit=30)

    assert page == {
        "content": content[190:220],
        "offset": 190,
        "next_offset": 220,
        "total_chars": 500,
        "stored_chars": 450,
    }
    assert final_page == {
        "content": content[440:],
        "offset": 440,
        "next_offset": None,
        "total_chars": 500,
        "stored_chars": 450,
    }


def test_reads_enforce_project_and_ttl(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    project_manager: LocalProjectManager,
) -> None:
    store = ToolResultStore(temp_db, _config(retention_days=3))
    other_project = project_manager.create(name="other-tool-result-project")
    result_id = _save(store, project_id=sample_project["id"], content="owned content")

    assert store.get_meta(result_id, other_project.id) is None
    assert store.get_slice(result_id, other_project.id, offset=0, limit=20) is None

    temp_db.execute(
        "UPDATE tool_results SET created_at = NOW() - INTERVAL '4 days' WHERE id = %s",
        (result_id,),
    )

    assert store.get_meta(result_id, sample_project["id"]) is None
    assert store.get_slice(result_id, sample_project["id"], offset=0, limit=20) is None


class _NoSqlDatabase:
    def fetchone(self, sql: str, params: tuple[Any, ...]) -> None:
        raise AssertionError(f"SQL must not execute: {sql!r} {params!r}")


@pytest.mark.parametrize("result_id", ["not-a-uuid", "x" * 100_000])
def test_malformed_result_ids_are_unknown_without_sql(result_id: str) -> None:
    db = cast(HubDatabase, _NoSqlDatabase())
    store = ToolResultStore(db, _config())
    project_id = str(uuid.uuid4())

    assert store.get_meta(result_id, project_id) is None
    assert store.get_slice(result_id, project_id, offset=0, limit=20) is None


@pytest.mark.parametrize(("offset", "limit"), [(-1, 1), (0, 0), (0, -1)])
def test_get_slice_rejects_invalid_bounds_before_sql(offset: int, limit: int) -> None:
    db = cast(HubDatabase, _NoSqlDatabase())
    store = ToolResultStore(db, _config())

    with pytest.raises(ValueError):
        store.get_slice(str(uuid.uuid4()), str(uuid.uuid4()), offset=offset, limit=limit)


def test_tool_result_chunks_are_registered_for_ranked_bm25_search(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    store = ToolResultStore(temp_db, _config())
    result_id = _save(
        store,
        project_id=sample_project["id"],
        content=f"{'needle ' * 20}\n{'padding ' * 30}\nneedle",
    )
    _save(
        store,
        project_id=sample_project["id"],
        content="needle from a different result",
    )
    expected = temp_db.fetchall(
        """SELECT id
           FROM tool_result_chunks
           WHERE result_id = %s
             AND content LIKE '%%needle%%'
           ORDER BY ordinal""",
        (result_id,),
    )

    assert store.get_meta(result_id, sample_project["id"]) is not None
    hits = pick_search_backend(temp_db, "tool_result_chunks").search(
        sanitize_pg_search_query("needle"),
        5,
        filters={"result_id": result_id},
    )

    assert [hit.id for hit in hits] == [str(row["id"]) for row in expected]
    assert hits[0].score >= hits[-1].score


def test_pg_search_query_bound_is_fixed_beside_sanitizer() -> None:
    assert MAX_PG_SEARCH_QUERY_CHARS == 1_000
