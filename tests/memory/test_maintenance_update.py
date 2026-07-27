"""Tests for updated maintenance.py — no decay, Qdrant stats."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.memory.services.maintenance import get_stats
from gobby.storage.memories_scope import MemoryScope

pytestmark = pytest.mark.unit


def _make_storage(memories=None):
    """Create a mock storage with configurable memories."""
    storage = MagicMock()
    storage.list_memories.return_value = memories or []
    return storage


def _make_memory(memory_type="fact"):
    """Create a mock memory."""
    m = MagicMock()
    m.memory_type = memory_type
    # Memory normalizes stored timestamps to aware datetimes; mirror that here.
    m.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    return m


class TestGetStatsNoDecay:
    """Tests that decay_memories is removed."""

    def test_decay_memories_not_importable(self) -> None:
        """decay_memories function should not exist in maintenance module."""
        from gobby.memory.services import maintenance

        assert not hasattr(maintenance, "decay_memories")


class TestGetStatsVectorCount:
    """Tests for vector_count in get_stats."""

    async def test_vector_count_included_when_vector_store_provided(self) -> None:
        """get_stats includes vector_count when vector_store is given."""
        storage = _make_storage([_make_memory()])
        db = MagicMock()
        vector_store = MagicMock()
        vector_store.count = AsyncMock(return_value=42)

        stats = await get_stats(storage, db, project_id=None, vector_store=vector_store)

        assert stats["vector_count"] == 42

    async def test_no_vector_count_without_vector_store(self) -> None:
        """get_stats omits vector_count when no vector_store."""
        storage = _make_storage([_make_memory()])
        db = MagicMock()

        stats = await get_stats(storage, db, project_id=None)

        assert "vector_count" not in stats

    async def test_vector_count_graceful_on_error(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """get_stats handles vector_store errors gracefully."""
        storage = _make_storage([_make_memory()])
        db = MagicMock()
        vector_store = MagicMock()
        vector_store.count = AsyncMock(side_effect=Exception("Qdrant down"))

        stats = await get_stats(storage, db, project_id=None, vector_store=vector_store)

        assert stats["vector_count"] == -1
        record = next(
            record
            for record in caplog.records
            if record.message == "Failed to retrieve memory vector count"
        )
        assert record.exc_info is not None
        assert record.__dict__["project_id"] is None


class TestGetStatsBasicBehavior:
    """Tests that basic stats behavior is preserved."""

    async def test_empty_memories(self) -> None:
        """get_stats returns zeros for empty memory store."""
        storage = _make_storage([])
        db = MagicMock()

        stats = await get_stats(storage, db, project_id=None)

        assert stats["total_count"] == 0
        assert stats["by_type"] == {}

    async def test_counts_by_type(self) -> None:
        """get_stats counts memories by type."""
        storage = _make_storage(
            [
                _make_memory("fact"),
                _make_memory("fact"),
                _make_memory("preference"),
            ]
        )
        db = MagicMock()

        stats = await get_stats(storage, db, project_id=None)

        assert stats["total_count"] == 3
        assert stats["by_type"]["fact"] == 2
        assert stats["by_type"]["preference"] == 1

    async def test_project_id_passed_through(self) -> None:
        """get_stats passes project_id to storage."""
        storage = _make_storage([])
        db = MagicMock()

        await get_stats(storage, db, project_id="proj-1")

        storage.list_memories.assert_called_with(
            scope=MemoryScope.project_visible("proj-1"), limit=10000
        )
        assert storage.list_memories.call_count >= 1
        assert storage.list_memories.call_args is not None
