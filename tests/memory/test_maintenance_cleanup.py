"""Tests for memory maintenance finder helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.memory.services.maintenance import (
    find_code_derivable_memories,
    find_duplicate_memories,
    find_orphaned_memories,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_memory(
    memory_id: str = "mem-1",
    content: str = "Some valuable insight",
    memory_type: str = "fact",
    access_count: int = 0,
    created_at: str = "2025-01-01T00:00:00+00:00",
    updated_at: str = "2025-01-01T00:00:00+00:00",
    last_accessed_at: str | None = None,
    source_session_id: str | None = None,
    project_id: str | None = None,
    tags: list[str] | None = None,
) -> MagicMock:
    m = MagicMock()
    m.id = memory_id
    m.content = content
    m.memory_type = memory_type
    m.access_count = access_count
    m.created_at = created_at
    m.updated_at = updated_at
    m.last_accessed_at = last_accessed_at
    m.source_session_id = source_session_id
    m.project_id = project_id
    m.tags = tags or []
    return m


def _make_db_row(
    memory_id: str = "mem-1",
    content: str = "Some valuable insight",
    memory_type: str = "fact",
    access_count: int = 0,
    created_at: str = "2025-01-01T00:00:00+00:00",
    updated_at: str = "2025-01-01T00:00:00+00:00",
    source_type: str = "user",
    source_session_id: str | None = None,
    project_id: str | None = None,
    tags: str | None = None,
    last_accessed_at: str | None = None,
    media: str | None = None,
) -> dict:
    return {
        "id": memory_id,
        "content": content,
        "memory_type": memory_type,
        "access_count": access_count,
        "created_at": created_at,
        "updated_at": updated_at,
        "source_type": source_type,
        "source_session_id": source_session_id,
        "project_id": project_id,
        "tags": tags,
        "last_accessed_at": last_accessed_at,
        "media": media,
    }


class _FakeRow(dict):
    """Dict that supports both dict[] and row['key'] access like dict[str, object]."""

    def keys(self) -> list[str]:
        return list(super().keys())


def _row(**kwargs) -> _FakeRow:
    return _FakeRow(**_make_db_row(**kwargs))


# ---------------------------------------------------------------------------
# find_duplicate_memories
# ---------------------------------------------------------------------------


class TestFindDuplicateMemories:
    @pytest.mark.asyncio
    async def test_detects_near_exact_duplicates(self) -> None:
        mem_a = _make_memory(memory_id="a", content="hello world", access_count=5)
        mem_b = _make_memory(memory_id="b", content="hello world!", access_count=1)

        storage = MagicMock()
        storage.list_memories.return_value = [mem_a, mem_b]
        storage.get_memory.side_effect = lambda mid: mem_a if mid == "a" else mem_b

        vector_store = MagicMock()
        # When embedding mem_a, find mem_b as near-exact match
        vector_store.search = AsyncMock(
            side_effect=[
                [("b", 0.97)],  # search for mem_a finds mem_b
                [],  # search for mem_b (already seen)
            ]
        )
        embed_fn = AsyncMock(return_value=[0.1] * 768)

        result = await find_duplicate_memories(
            storage,
            vector_store,
            embed_fn,
            similarity_threshold=0.95,
        )

        assert len(result) == 1
        assert result[0]["keep_id"] == "a"  # higher access_count
        assert result[0]["delete_id"] == "b"
        assert result[0]["score"] == 0.97

    @pytest.mark.asyncio
    async def test_keeps_higher_access_count(self) -> None:
        mem_a = _make_memory(memory_id="a", content="fact 1", access_count=1)
        mem_b = _make_memory(memory_id="b", content="fact 1 dup", access_count=10)

        storage = MagicMock()
        storage.list_memories.return_value = [mem_a, mem_b]
        storage.get_memory.return_value = mem_b

        vector_store = MagicMock()
        vector_store.search = AsyncMock(
            side_effect=[
                [("b", 0.96)],
                [],
            ]
        )
        embed_fn = AsyncMock(return_value=[0.1] * 768)

        result = await find_duplicate_memories(
            storage,
            vector_store,
            embed_fn,
            similarity_threshold=0.95,
        )

        assert len(result) == 1
        assert result[0]["keep_id"] == "b"
        assert result[0]["delete_id"] == "a"

    @pytest.mark.asyncio
    async def test_below_threshold_not_flagged(self) -> None:
        mem = _make_memory(memory_id="a")
        storage = MagicMock()
        storage.list_memories.return_value = [mem]

        vector_store = MagicMock()
        vector_store.search = AsyncMock(return_value=[("other", 0.80)])
        embed_fn = AsyncMock(return_value=[0.1] * 768)

        result = await find_duplicate_memories(
            storage,
            vector_store,
            embed_fn,
            similarity_threshold=0.95,
        )

        assert len(result) == 0
        assert vector_store.search.await_count == 1

    @pytest.mark.asyncio
    async def test_empty_store(self) -> None:
        storage = MagicMock()
        storage.list_memories.return_value = []

        vector_store = MagicMock()
        embed_fn = AsyncMock()

        result = await find_duplicate_memories(storage, vector_store, embed_fn)

        assert result == []


# ---------------------------------------------------------------------------
# find_code_derivable_memories
# ---------------------------------------------------------------------------


class TestFindCodeDerivableMemories:
    @pytest.mark.parametrize(
        "content",
        [
            "File src/main.py contains the entry point",
            "The file `utils.py` defines helper functions",
            "function processData is defined in handlers.ts",
            "The class UserManager is located in src/users.py",
            "The directory src/routes/ contains API handlers",
            "import os from stdlib",
            "src/config.yaml",
            "`models.py`",
        ],
    )
    def test_detects_code_derivable_patterns(self, content: str) -> None:
        mem = _make_memory(memory_id="cd-1", content=content)
        storage = MagicMock()
        storage.list_memories.return_value = [mem]

        result = find_code_derivable_memories(storage)

        assert len(result) == 1, f"Expected '{content}' to be flagged as code-derivable"

    @pytest.mark.parametrize(
        "content",
        [
            "We chose FastAPI over Flask because of async support and automatic OpenAPI docs",
            "The authentication flow uses JWT tokens with a 15-minute expiry",
            "Users reported that the dashboard takes 8 seconds to load on slow connections",
            "Never use eval() in the template renderer — security risk",
            "File uploads should be validated server-side, not just client-side",
        ],
    )
    def test_preserves_valuable_memories(self, content: str) -> None:
        mem = _make_memory(memory_id="val-1", content=content)
        storage = MagicMock()
        storage.list_memories.return_value = [mem]

        result = find_code_derivable_memories(storage)

        assert len(result) == 0, f"Expected '{content}' to NOT be flagged"

    def test_skips_long_content(self) -> None:
        """Memories over 200 chars are not flagged even if they match patterns."""
        long_content = "File src/main.py contains " + "x" * 200
        mem = _make_memory(memory_id="long-1", content=long_content)
        storage = MagicMock()
        storage.list_memories.return_value = [mem]

        result = find_code_derivable_memories(storage)

        assert len(result) == 0


# ---------------------------------------------------------------------------
# find_orphaned_memories
# ---------------------------------------------------------------------------


class TestFindOrphanedMemories:
    def test_finds_orphaned_by_session(self) -> None:
        old_date = (datetime.now(UTC) - timedelta(days=120)).isoformat()
        db = MagicMock()
        db.fetchall.return_value = [
            _row(memory_id="orphan-1", source_session_id="dead-session", created_at=old_date),
        ]

        result = find_orphaned_memories(db, min_age_days=90)

        assert len(result) == 1
        assert result[0].id == "orphan-1"
        # Verify LEFT JOIN pattern
        sql = db.fetchall.call_args[0][0]
        assert "LEFT JOIN sessions" in sql
        assert "s.id IS NULL" in sql

    def test_respects_min_age_days(self) -> None:
        db = MagicMock()
        db.fetchall.return_value = []

        find_orphaned_memories(db, min_age_days=60)

        call_args = db.fetchall.call_args
        cutoff_param = call_args[0][1][0]
        cutoff_dt = datetime.fromisoformat(cutoff_param)
        expected = datetime.now(UTC) - timedelta(days=60)
        assert abs((cutoff_dt - expected).total_seconds()) < 5

    def test_filters_by_project_id(self) -> None:
        db = MagicMock()
        db.fetchall.return_value = []

        find_orphaned_memories(db, project_id="proj-1")

        sql = db.fetchall.call_args[0][0]
        assert "project_id = %s" in sql
