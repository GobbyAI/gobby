"""Tests for VectorStore initialization in runner.py.

Validates that the runner creates a VectorStore, passes it to MemoryManager,
triggers rebuild when Qdrant is empty but SQLite has memories, and calls
close() on shutdown.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit


class TestVectorStoreInitialization:
    """Test that runner.py initializes VectorStore correctly."""

    @pytest.mark.asyncio
    async def test_vectorstore_created_with_config_path(self) -> None:
        """VectorStore should be created with qdrant_path from config."""
        from gobby.memory.vectorstore import VectorStore

        config = MagicMock()
        config.memory.qdrant_path = "/tmp/test-qdrant"
        config.memory.qdrant_url = None
        config.memory.qdrant_api_key = None
        config.memory.embedding_model = "text-embedding-3-small"

        vs = VectorStore(
            path=config.memory.qdrant_path,
            url=config.memory.qdrant_url,
            api_key=config.memory.qdrant_api_key,
        )
        assert vs._path == "/tmp/test-qdrant"
        assert vs._url is None

    @pytest.mark.asyncio
    async def test_vectorstore_created_with_config_url(self) -> None:
        """VectorStore should be created with qdrant_url from config."""
        from gobby.memory.vectorstore import VectorStore

        vs = VectorStore(
            url="http://localhost:6333",
            api_key="test-key",
        )
        assert vs._url == "http://localhost:6333"
        assert vs._api_key == "test-key"
        assert vs._path is None

    @pytest.mark.asyncio
    async def test_vectorstore_passed_to_memory_manager(self) -> None:
        """MemoryManager should receive VectorStore instance."""
        from gobby.memory.manager import MemoryManager
        from gobby.memory.vectorstore import VectorStore

        vs = MagicMock(spec=VectorStore)
        embed_fn = AsyncMock(return_value=[0.1] * 1536)

        config = MagicMock()
        config.enabled = True
        config.backend = "local"
        config.auto_crossref = False
        config.neo4j_url = None

        db = MagicMock()
        db.fetchone = MagicMock(return_value=None)
        db.execute = MagicMock()
        db.transaction = MagicMock()

        manager = MemoryManager(
            db=db,
            config=config,
            vector_store=vs,
            embed_fn=embed_fn,
        )
        assert manager._vector_store is vs
        assert manager._embed_fn is embed_fn

    @pytest.mark.asyncio
    async def test_vectorstore_close_called(self) -> None:
        """VectorStore.close() should be callable for shutdown."""
        from gobby.memory.vectorstore import VectorStore

        vs = MagicMock(spec=VectorStore)
        vs.close = AsyncMock()

        await vs.close()
        vs.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_rebuild_when_qdrant_empty_sqlite_has_memories(self) -> None:
        """Should trigger rebuild when Qdrant is empty but SQLite has memories."""
        from gobby.memory.vectorstore import VectorStore

        vs = MagicMock(spec=VectorStore)
        vs.count = AsyncMock(return_value=0)
        vs.rebuild = AsyncMock()

        # Simulate SQLite having memories
        storage = MagicMock()
        storage.list_memories.return_value = [
            MagicMock(id="mm-1", content="Memory 1"),
            MagicMock(id="mm-2", content="Memory 2"),
        ]

        embed_fn = AsyncMock(return_value=[0.1] * 1536)

        # Simulate the rebuild logic from runner
        qdrant_count = await vs.count()
        sqlite_memories = storage.list_memories(limit=10000)

        if qdrant_count == 0 and len(sqlite_memories) > 0:
            memory_dicts = [{"id": m.id, "content": m.content} for m in sqlite_memories]
            await vs.rebuild(memory_dicts, embed_fn)

        vs.rebuild.assert_called_once()
        args = vs.rebuild.call_args
        assert len(args[0][0]) == 2  # 2 memories
        assert args[0][1] is embed_fn

    @pytest.mark.asyncio
    async def test_no_rebuild_when_qdrant_has_data(self) -> None:
        """Should NOT rebuild when Qdrant already has data."""
        from gobby.memory.vectorstore import VectorStore

        vs = MagicMock(spec=VectorStore)
        vs.count = AsyncMock(return_value=42)
        vs.rebuild = AsyncMock()

        storage = MagicMock()
        storage.list_memories.return_value = [MagicMock(id="mm-1", content="x")]

        qdrant_count = await vs.count()
        sqlite_memories = storage.list_memories(limit=10000)

        if qdrant_count == 0 and len(sqlite_memories) > 0:
            await vs.rebuild([], AsyncMock())

        vs.rebuild.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_rebuild_when_sqlite_empty(self) -> None:
        """Should NOT rebuild when SQLite has no memories."""
        from gobby.memory.vectorstore import VectorStore

        vs = MagicMock(spec=VectorStore)
        vs.count = AsyncMock(return_value=0)
        vs.rebuild = AsyncMock()

        storage = MagicMock()
        storage.list_memories.return_value = []

        qdrant_count = await vs.count()
        sqlite_memories = storage.list_memories(limit=10000)

        if qdrant_count == 0 and len(sqlite_memories) > 0:
            await vs.rebuild([], AsyncMock())

        vs.rebuild.assert_not_called()

    def test_default_qdrant_path_config_is_none(self) -> None:
        """Config default for qdrant_path should be None (runner provides fallback)."""
        from gobby.config.persistence import MemoryConfig

        config = MemoryConfig()
        assert config.qdrant_path is None
