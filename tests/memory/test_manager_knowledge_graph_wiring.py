"""Tests for KnowledgeGraphService wiring in MemoryManager."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypeVar, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.persistence import MemoryConfig
from gobby.llm.base import LLMProviderCancellation
from gobby.memory.services.knowledge_graph import (
    KnowledgeGraphRebuildService,
    KnowledgeGraphResult,
    KnowledgeGraphStatus,
)
from gobby.memory.write_result import MemoryWriteResult
from gobby.storage.memories import LocalMemoryManager, Memory
from tests._timing import wait_for_async_condition

pytestmark = pytest.mark.unit

BACKGROUND_TASK_CLEANUP_TIMEOUT = 1.0
BACKGROUND_TASK_CLEANUP_INTERVAL = 0.01
_RunDbResult = TypeVar("_RunDbResult")


def _mock_llm_service() -> MagicMock:
    llm_service = MagicMock()
    llm_service.call_json_feature = AsyncMock(return_value={"entities": [], "relations": []})
    return llm_service


def _make_manager(
    falkordb_host: str | None = None,
    llm_service: MagicMock | None = None,
    vector_store: AsyncMock | None = None,
    embed_fn: AsyncMock | None = None,
    config: MemoryConfig | None = None,
) -> MagicMock:
    """Create a MemoryManager with controlled dependencies.

    We import lazily so we can patch before construction.
    """
    from gobby.memory.manager import MemoryManager

    db = MagicMock()
    db.fetchall = MagicMock(return_value=[])
    db.fetchone = MagicMock(return_value=None)
    db.execute = MagicMock()

    config = config or MemoryConfig()

    kwargs = {
        "db": db,
        "config": config,
        "llm_service": llm_service,
        "vector_store": vector_store,
        "embed_fn": embed_fn,
        "falkordb_host": falkordb_host,
        "falkordb_password": "secret" if falkordb_host else None,
    }
    if falkordb_host:
        with patch("gobby.memory.manager.FalkorClient") as falkor_cls:
            falkor_cls.return_value = AsyncMock()
            return MemoryManager(**kwargs)
    return MemoryManager(**kwargs)


class TestKnowledgeGraphServiceInitialization:
    """Test that KnowledgeGraphService is initialized correctly."""

    def test_kg_service_created_when_FalkorDB_and_llm_configured(self) -> None:
        """KnowledgeGraphService is created when FalkorDB URL + LLM are configured."""
        embed_fn = AsyncMock(return_value=[0.1, 0.2])
        vs = AsyncMock()

        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=_mock_llm_service(),
            vector_store=vs,
            embed_fn=embed_fn,
        )

        assert manager._kg_service is not None

    def test_kg_service_created_without_vector_dependencies(self) -> None:
        """KnowledgeGraphService can run without vector search dependencies."""
        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=_mock_llm_service(),
        )

        assert manager._kg_service is not None

    def test_kg_service_uses_feature_routing(self) -> None:
        """KnowledgeGraphService wiring should honor memory.kg feature routing."""
        llm_service = _mock_llm_service()

        with patch("gobby.memory.manager.KnowledgeGraphService") as mock_kg_service:
            manager = _make_manager(
                falkordb_host="127.0.0.1",
                llm_service=llm_service,
                vector_store=AsyncMock(),
                embed_fn=AsyncMock(return_value=[0.1]),
            )

        call_kwargs = mock_kg_service.call_args.kwargs
        assert call_kwargs["llm_service"] is llm_service
        assert call_kwargs["feature_config"] is manager.config.kg

    def test_kg_service_uses_cluster_density_config(self) -> None:
        """KnowledgeGraphService wiring should honor clustering density config."""
        config = MemoryConfig(cluster_min_cluster_size=7, cluster_min_samples=None)

        with patch("gobby.memory.manager.KnowledgeGraphService") as mock_kg_service:
            _make_manager(
                falkordb_host="127.0.0.1",
                llm_service=_mock_llm_service(),
                vector_store=AsyncMock(),
                embed_fn=AsyncMock(return_value=[0.1]),
                config=config,
            )

        call_kwargs = mock_kg_service.call_args.kwargs
        assert call_kwargs["cluster_min_cluster_size"] == 7
        assert call_kwargs["cluster_min_samples"] is None

    def test_kg_service_none_when_no_FalkorDB(self) -> None:
        """KnowledgeGraphService is None when FalkorDB is not configured."""
        manager = _make_manager(
            falkordb_host=None,
            llm_service=_mock_llm_service(),
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(),
        )

        assert manager._kg_service is None

    def test_kg_service_none_when_no_llm(self) -> None:
        """KnowledgeGraphService is None when LLM service not available."""
        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=None,
        )

        assert manager._kg_service is None


class TestGraphDelegation:
    """Test that graph read methods delegate to KnowledgeGraphService."""

    async def test_get_entity_graph_delegates_to_kg_service(self) -> None:
        """get_entity_graph delegates to KnowledgeGraphService."""
        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=_mock_llm_service(),
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        expected = {"entities": [{"name": "Josh"}], "relationships": []}
        manager._kg_service.get_entity_graph = AsyncMock(return_value=expected)

        result = await manager.get_entity_graph(limit=100)

        assert result == expected
        manager._kg_service.get_entity_graph.assert_called_once_with(
            limit=100, relationship_limit=2000, project_id=None
        )

    async def test_get_entity_neighbors_delegates_to_kg_service(self) -> None:
        """get_entity_neighbors delegates to KnowledgeGraphService."""
        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=_mock_llm_service(),
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        expected = {"entities": [], "relationships": []}
        manager._kg_service.get_entity_neighbors = AsyncMock(return_value=expected)

        result = await manager.get_entity_neighbors("Josh")

        assert result == expected
        manager._kg_service.get_entity_neighbors.assert_called_once_with(
            "Josh",
            project_id=None,
        )

    async def test_get_knowledge_graph_counts_delegates_to_kg_service(self) -> None:
        """get_knowledge_graph_counts returns actual FalkorDB counts."""
        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=_mock_llm_service(),
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        expected = {"graph": "gobby_kg", "memory_nodes": 3, "entity_nodes": 7}
        manager._kg_service.get_graph_counts = AsyncMock(return_value=expected)

        result = await manager.get_knowledge_graph_counts(project_id="proj-1")

        assert result == expected
        manager._kg_service.get_graph_counts.assert_called_once_with(project_id="proj-1")

    async def test_get_entity_graph_returns_none_when_no_kg_service(self) -> None:
        """get_entity_graph returns None when KnowledgeGraphService is not available."""
        manager = _make_manager(falkordb_host=None)

        result = await manager.get_entity_graph()

        assert result is None

    async def test_get_entity_neighbors_returns_none_when_no_kg_service(self) -> None:
        """get_entity_neighbors returns None when KnowledgeGraphService is not available."""
        manager = _make_manager(falkordb_host=None)

        result = await manager.get_entity_neighbors("Josh")

        assert result is None

    async def test_clear_knowledge_graph_requeues_affected_memories(self) -> None:
        """clear_knowledge_graph should reset graph_processed for affected memories."""
        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=_mock_llm_service(),
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        manager._kg_service.clear_graph = AsyncMock(
            return_value={"memories_deleted": 2, "entities_deleted": 4}
        )
        manager.storage.mark_pending_graphs = MagicMock(return_value=3)

        result = await manager.clear_knowledge_graph(project_id="proj-1")

        manager._kg_service.clear_graph.assert_awaited_once_with(project_id="proj-1")
        assert manager._kg_service.clear_graph.await_count == 1
        assert manager._kg_service.clear_graph.await_args is not None
        manager.storage.mark_pending_graphs.assert_called_once_with("proj-1")
        assert manager.storage.mark_pending_graphs.call_count == 1
        assert manager.storage.mark_pending_graphs.call_args is not None
        assert result == {
            "success": True,
            "memories_marked_pending": 3,
            "memories_deleted": 2,
            "entities_deleted": 4,
        }

    async def test_rebuild_knowledge_graph_marks_successful_memories_processed(self) -> None:
        """Explicit rebuild should reconcile graph_processed for successful rows."""
        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=_mock_llm_service(),
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        mem1 = MagicMock(id="mem-1", content="First memory", project_id="proj-1")
        mem2 = MagicMock(id="mem-2", content="Second memory", project_id="proj-1")
        mem3 = MagicMock(id="mem-3", content="Third memory", project_id="proj-1")

        manager._fetch_all_project_memories = AsyncMock(return_value=[mem1, mem2, mem3])
        manager._kg_service.add_to_graph = AsyncMock(
            side_effect=[
                KnowledgeGraphResult(KnowledgeGraphStatus.SUCCESS),
                KnowledgeGraphResult(KnowledgeGraphStatus.NOOP_NO_ENTITIES),
                KnowledgeGraphResult(KnowledgeGraphStatus.DETERMINISTIC_FAILURE),
            ]
        )
        manager.mark_graph_processed = MagicMock()
        manager.storage.mark_pending_graph = MagicMock()

        result = await manager.rebuild_knowledge_graph(project_id="proj-1")

        assert manager._kg_service.add_to_graph.await_count == 3
        manager.storage.mark_pending_graph.assert_any_call("mem-1")
        manager.storage.mark_pending_graph.assert_any_call("mem-2")
        manager.storage.mark_pending_graph.assert_any_call("mem-3")
        assert manager.storage.mark_pending_graph.call_count == 3
        manager.mark_graph_processed.assert_any_call("mem-1")
        manager.mark_graph_processed.assert_any_call("mem-2")
        assert manager.mark_graph_processed.call_count == 2
        assert result["memories_processed"] == 3
        assert result["memories_marked_pending"] == 3
        assert result["memories_marked_processed"] == 2
        assert result["memories_extracted"] == 1
        assert result["noop_no_entities"] == 1
        assert result["errors"] == 1

    async def test_rebuild_whitespace_entity_marks_memory_processed_without_retry(self) -> None:
        llm_service = _mock_llm_service()
        llm_service.call_json_feature = AsyncMock(
            return_value={"entities": [{"entity": "  \t ", "entity_type": "concept"}]}
        )
        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=llm_service,
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )
        memory = MagicMock(id="mem-empty", content="Malformed extraction", project_id="proj-1")
        manager._fetch_all_project_memories = AsyncMock(return_value=[memory])
        manager._kg_service._extractor._prompt_loader.render = MagicMock(
            return_value="extract entities"
        )
        manager.mark_graph_processed = MagicMock()
        manager.storage.mark_pending_graph = MagicMock()

        result = await manager.rebuild_knowledge_graph(project_id="proj-1")

        manager.mark_graph_processed.assert_called_once_with("mem-empty")
        assert result["noop_no_entities"] == 1
        assert result["memories_marked_processed"] == 1
        assert result["errors"] == 0

    async def test_rebuild_knowledge_graph_reports_progress_and_failed_memory_ids(self) -> None:
        """Rebuild progress snapshots and final result should identify failing rows."""
        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=_mock_llm_service(),
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        mem1 = MagicMock(id="mem-1", content="First memory", project_id="proj-1")
        mem2 = MagicMock(id="mem-2", content="Second memory", project_id="proj-1")
        manager._fetch_all_project_memories = AsyncMock(return_value=[mem1, mem2])
        manager._kg_service.add_to_graph = AsyncMock(
            side_effect=[
                KnowledgeGraphResult(KnowledgeGraphStatus.SUCCESS),
                KnowledgeGraphResult(
                    KnowledgeGraphStatus.DETERMINISTIC_FAILURE,
                    errors=["bad-json"],
                ),
            ]
        )
        manager.mark_graph_processed = MagicMock()
        manager.storage.mark_pending_graph = MagicMock()

        progress_updates: list[dict[str, object]] = []

        async def _on_progress(progress: dict[str, object]) -> None:
            progress_updates.append(progress)

        result = await manager.rebuild_knowledge_graph(
            project_id="proj-1",
            progress_callback=_on_progress,
        )

        assert progress_updates[0]["memories_total"] == 2
        assert progress_updates[0]["memories_completed"] == 0
        assert progress_updates[0]["memories_marked_pending"] == 2
        assert progress_updates[-1]["memories_completed"] == 2
        assert progress_updates[-1]["errors"] == 1
        assert result["failed_memories"] == [
            {
                "memory_id": "mem-2",
                "project_id": "proj-1",
                "status": "deterministic_failure",
                "errors": ["bad-json"],
            }
        ]

    async def test_rebuild_knowledge_graph_treats_llm_cancellation_as_retryable(self) -> None:
        """Provider shutdown cancellation leaves memory pending instead of permanent-failed."""
        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=_mock_llm_service(),
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        mem = MagicMock(id="mem-1", content="First memory", project_id="proj-1")
        manager._fetch_all_project_memories = AsyncMock(return_value=[mem])
        manager._kg_service.add_to_graph = AsyncMock(
            side_effect=LLMProviderCancellation("Claude SDK process terminated [exit_code=143]")
        )
        manager.mark_graph_processed = MagicMock()
        manager.storage.mark_pending_graph = MagicMock()

        result = await manager.rebuild_knowledge_graph(project_id="proj-1")

        assert result["success"] is True
        assert result["errors"] == 1
        assert result["failed_memories"] == [
            {
                "memory_id": "mem-1",
                "project_id": "proj-1",
                "status": "retryable_failure",
                "errors": ["Claude SDK process terminated [exit_code=143]"],
            }
        ]
        manager.mark_graph_processed.assert_not_called()


class TestKnowledgeGraphRebuildService:
    """Service-level tests for KG rebuild orchestration."""

    async def test_rebuild_marks_pending_and_processed_memories(self) -> None:
        """The rebuild service owns pending and processed graph bookkeeping."""

        class Storage:
            def __init__(self) -> None:
                self.pending: list[str] = []

            def mark_pending_graph(self, memory_id: str) -> None:
                self.pending.append(memory_id)

        async def run_db(
            func: Callable[..., _RunDbResult],
            *args: Any,
            **kwargs: Any,
        ) -> _RunDbResult:
            return func(*args, **kwargs)

        storage = Storage()
        memories = [
            Memory(
                id="mem-1",
                memory_type="fact",
                content="Python memory",
                created_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
                project_id="proj-1",
            )
        ]
        processed: list[str] = []
        kg_service = MagicMock()
        kg_service.add_to_graph = AsyncMock(
            return_value=KnowledgeGraphResult(KnowledgeGraphStatus.NOOP_NO_ENTITIES)
        )
        service = KnowledgeGraphRebuildService(
            storage_provider=lambda: storage,
            kg_service_provider=lambda: kg_service,
            falkor_client_provider=lambda: None,
            run_db=run_db,
            list_memories=lambda *args: memories,
            fetch_all_project_memories=AsyncMock(return_value=memories),
            mark_graph_processed=processed.append,
            record_graph_failure=lambda *_args, **_kwargs: "pending",
        )

        result = await service.rebuild_knowledge_graph(project_id=None)

        assert result["success"] is True
        assert result["memories_marked_pending"] == 1
        assert result["memories_marked_processed"] == 1
        assert result["noop_no_entities"] == 1
        assert storage.pending == ["mem-1"]
        assert processed == ["mem-1"]
        kg_service.add_to_graph.assert_awaited_once_with(
            "Python memory",
            memory_id="mem-1",
            project_id="proj-1",
            is_global=False,
        )

    @pytest.mark.asyncio
    async def test_rebuild_applies_failure_policy_and_survives_state_error(self) -> None:
        """Rebuild jobs apply the queue policy without dying on a state-write outage."""

        class Storage:
            def mark_pending_graph(self, memory_id: str) -> None:
                assert memory_id == "mem-poison"

        async def run_db(
            func: Callable[..., _RunDbResult],
            *args: Any,
            **kwargs: Any,
        ) -> _RunDbResult:
            return func(*args, **kwargs)

        memory = Memory(
            id="mem-poison",
            memory_type="fact",
            content="Invalid extraction input",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            project_id="proj-1",
        )
        failures: list[tuple[str, bool, int]] = []

        def record_failure(
            memory_id: str,
            *,
            deterministic: bool,
            max_attempts: int,
        ) -> str:
            failures.append((memory_id, deterministic, max_attempts))
            raise RuntimeError("database unavailable")

        kg_service = MagicMock()
        kg_service.add_to_graph = AsyncMock(
            return_value=KnowledgeGraphResult(KnowledgeGraphStatus.DETERMINISTIC_FAILURE)
        )
        service = KnowledgeGraphRebuildService(
            storage_provider=Storage,
            kg_service_provider=lambda: kg_service,
            falkor_client_provider=lambda: None,
            run_db=run_db,
            list_memories=lambda *args: [memory],
            fetch_all_project_memories=AsyncMock(return_value=[memory]),
            mark_graph_processed=lambda _memory_id: None,
            record_graph_failure=record_failure,
            max_deterministic_attempts=4,
        )

        result = await service.rebuild_knowledge_graph(project_id=None)

        assert result["errors"] == 1
        assert failures == [("mem-poison", True, 4)]
        assert result["failed_memories"][0]["errors"] == [
            "Failed to persist graph retry state: database unavailable"
        ]

    @pytest.mark.asyncio
    async def test_rebuild_concurrency_one_prevents_overlapping_add_to_graph(self) -> None:
        """Configured rebuild concurrency gates concurrent add_to_graph calls."""

        class Storage:
            def mark_pending_graph(self, memory_id: str) -> None:
                pass

        async def run_db(
            func: Callable[..., _RunDbResult],
            *args: Any,
            **kwargs: Any,
        ) -> _RunDbResult:
            return func(*args, **kwargs)

        memories = [
            Memory(
                id=f"mem-{index}",
                memory_type="fact",
                content=f"Memory {index}",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                updated_at=datetime(2026, 1, 1, tzinfo=UTC),
                project_id="proj-1",
            )
            for index in range(3)
        ]
        active = 0
        max_active = 0
        lock = asyncio.Lock()
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        release_first = asyncio.Event()
        processed: list[str] = []

        async def add_to_graph(
            _content: str,
            *,
            memory_id: str,
            project_id: str,
            is_global: bool,
        ) -> KnowledgeGraphResult:
            nonlocal active, max_active
            assert memory_id.startswith("mem-")
            assert project_id == "proj-1"
            assert is_global is False
            async with lock:
                active += 1
                max_active = max(max_active, active)
            if memory_id == "mem-0":
                first_started.set()
                await release_first.wait()
            else:
                second_started.set()
            async with lock:
                active -= 1
            return KnowledgeGraphResult(KnowledgeGraphStatus.SUCCESS)

        kg_service = MagicMock()
        kg_service.add_to_graph = AsyncMock(side_effect=add_to_graph)
        service = KnowledgeGraphRebuildService(
            storage_provider=cast(Callable[[], LocalMemoryManager], Storage),
            kg_service_provider=lambda: kg_service,
            falkor_client_provider=lambda: None,
            run_db=run_db,
            list_memories=lambda *args: memories,
            fetch_all_project_memories=AsyncMock(return_value=memories),
            mark_graph_processed=processed.append,
            record_graph_failure=lambda *_args, **_kwargs: "pending",
            max_rebuild_concurrency=1,
        )

        rebuild_task = asyncio.create_task(service.rebuild_knowledge_graph(project_id=None))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(second_started.wait(), timeout=0.02)
        finally:
            release_first.set()
        result = await rebuild_task

        assert result["success"] is True
        assert max_active == 1
        assert processed == ["mem-0", "mem-1", "mem-2"]


class TestGraphBackgroundTask:
    """Test that create_memory chains a graph background task."""

    async def test_create_memory_fires_graph_task_after_dedup(self) -> None:
        """create_memory fires a graph background task when KnowledgeGraphService is available."""
        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=_mock_llm_service(),
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        # Mock the backend to avoid real DB operations
        manager._backend = AsyncMock()
        manager._backend.content_exists = AsyncMock(return_value=False)

        from gobby.memory.protocol import MemoryRecord

        mock_record = MagicMock(spec=MemoryRecord)
        mock_record.id = "test-id"
        mock_record.memory_type = "fact"
        mock_record.content = "Josh uses Python"
        mock_record.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        mock_record.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
        mock_record.project_id = None
        mock_record.source_type = "user"
        mock_record.source_session_id = None

        mock_record.deleted_at = None
        mock_record.dream_action = None
        mock_record.last_dreamed_at = None
        mock_record.access_count = 0
        mock_record.last_accessed_at = None
        mock_record.tags = []
        manager._backend.create = AsyncMock(return_value=MemoryWriteResult(mock_record, "created"))

        # Mock KG service
        manager._kg_service.add_to_graph = AsyncMock()

        manager._lifecycle_service._reconcile_active_snapshot = AsyncMock(return_value=True)

        created = await manager.create_memory(content="Josh uses Python")

        # Graph queuing now happens inside the active-row reconciliation fence.
        assert created.id == "test-id"
        assert created.content == "Josh uses Python"
        assert created.memory_type.value == "fact"
        manager._lifecycle_service._reconcile_active_snapshot.assert_awaited_once()

    async def test_create_memory_no_graph_task_when_no_kg_service(self) -> None:
        """create_memory doesn't fire graph task when KnowledgeGraphService is unavailable."""
        manager = _make_manager(falkordb_host=None)

        manager._backend = AsyncMock()
        manager._backend.content_exists = AsyncMock(return_value=False)

        from gobby.memory.protocol import MemoryRecord

        mock_record = MagicMock(spec=MemoryRecord)
        mock_record.id = "test-id"
        mock_record.memory_type = "fact"
        mock_record.content = "test"
        mock_record.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        mock_record.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
        mock_record.project_id = None
        mock_record.source_type = "user"
        mock_record.source_session_id = None

        mock_record.deleted_at = None
        mock_record.dream_action = None
        mock_record.last_dreamed_at = None
        mock_record.access_count = 0
        mock_record.last_accessed_at = None
        mock_record.tags = []
        manager._backend.create = AsyncMock(return_value=MemoryWriteResult(mock_record, "created"))

        await manager.create_memory(content="test")

        # No graph background tasks should exist
        graph_tasks = [t for t in manager._background_tasks if "graph" in (t.get_name() or "")]
        assert len(graph_tasks) == 0
        assert manager._kg_service is None

    async def test_graph_task_failure_logged_not_raised(self) -> None:
        """Graph background task failure is logged but doesn't propagate."""
        manager = _make_manager(
            falkordb_host="127.0.0.1",
            llm_service=_mock_llm_service(),
            vector_store=AsyncMock(),
            embed_fn=AsyncMock(return_value=[0.1]),
        )

        manager._backend = AsyncMock()
        manager._backend.content_exists = AsyncMock(return_value=False)

        from gobby.memory.protocol import MemoryRecord

        mock_record = MagicMock(spec=MemoryRecord)
        mock_record.id = "test-id"
        mock_record.memory_type = "fact"
        mock_record.content = "test"
        mock_record.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        mock_record.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
        mock_record.project_id = None
        mock_record.source_type = "user"
        mock_record.source_session_id = None

        mock_record.deleted_at = None
        mock_record.dream_action = None
        mock_record.last_dreamed_at = None
        mock_record.access_count = 0
        mock_record.last_accessed_at = None
        mock_record.tags = []
        manager._backend.create = AsyncMock(return_value=MemoryWriteResult(mock_record, "created"))

        # Make graph service fail
        manager._kg_service.add_to_graph = AsyncMock(side_effect=Exception("FalkorDB down"))

        memory = await manager.create_memory(content="test")
        await wait_for_async_condition(
            lambda: len(manager._background_tasks) == 0,
            description="graph background task cleanup",
            timeout=BACKGROUND_TASK_CLEANUP_TIMEOUT,
            interval=BACKGROUND_TASK_CLEANUP_INTERVAL,
        )

        assert memory.id == "test-id"
        assert manager._kg_service is not None
        assert len(manager._background_tasks) == 0


class TestNoGraphServiceReference:
    """Test that old GraphService is no longer referenced."""

    def test_manager_has_no_graph_service_attribute(self) -> None:
        """MemoryManager should not have _graph_service attribute (replaced by _kg_service)."""
        manager = _make_manager()
        assert not hasattr(manager, "_graph_service")
