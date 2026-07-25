"""Tests for gobby.mcp_proxy.tools.memory - additional coverage for edge cases.

Focuses on:
- removed image/screenshot ingestion tools
- backup_memories / restore_memories
- build_turn_and_digest
- rebuild_crossrefs / rebuild_knowledge_graph
- reindex_embeddings
- search_knowledge_graph edge cases
"""

import asyncio
import inspect
import logging
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools import memory_dream as memory_dream_tools
from gobby.mcp_proxy.tools.memory import create_memory_registry

pytestmark = pytest.mark.unit


class MockMemory:
    """Mock memory object for tests."""

    def __init__(
        self,
        id: str = "mem-123",
        content: str = "Test memory",
        memory_type: str = "fact",
        created_at: str = "2024-01-01T00:00:00",
        updated_at: str | None = None,
        project_id: str | None = None,
        source_type: str = "agent",
        access_count: int = 0,
        tags: list[str] | None = None,
    ) -> None:
        self.id = id
        self.content = content
        self.memory_type = memory_type
        self.created_at = created_at
        self.updated_at = updated_at or created_at
        self.project_id = project_id
        self.is_global = False
        self.source_type = source_type
        self.access_count = access_count
        self.tags = tags or []


@pytest.mark.asyncio
async def test_create_memory_supersedes_guard_bypass(mock_memory_manager: MagicMock) -> None:
    superseded_id = str(uuid.uuid4())
    registry = create_memory_registry(mock_memory_manager)
    ephemeral = {
        "content": "Gobby build #epic E2E docs test #14353 completed.",
        "memory_type": "implementation_note",
        "tags": ["gobby", "build-e2e", "#14353"],
    }

    skipped = await registry.call("create_memory", ephemeral)
    assert skipped["skipped"] is True
    persisted = await registry.call(
        "create_memory",
        {**ephemeral, "supersedes": [superseded_id]},
    )
    assert persisted["success"] is True, persisted
    assert mock_memory_manager.create_memory.call_args.kwargs["supersedes"] == [superseded_id]

    mock_memory_manager.create_memory.reset_mock()
    proposal = (
        "SessionStart should persist a durable completion marker. If any invariant is "
        "missing, call an idempotent ensure_session_activation(session_id) helper."
    )
    task_manager = MagicMock()
    proposal_registry = create_memory_registry(
        mock_memory_manager,
        task_manager=task_manager,
    )
    await proposal_registry.call(
        "create_memory",
        {"content": proposal, "supersedes": [superseded_id]},
    )
    mock_memory_manager.create_memory.assert_awaited_once()
    task_manager.create_task_with_decomposition.assert_not_called()


@pytest.fixture
def mock_memory_manager() -> MagicMock:
    """Create a mock memory manager."""
    manager = MagicMock()
    manager.create_memory = AsyncMock(return_value=MockMemory())
    manager.search_memories = AsyncMock(return_value=[])
    manager.delete_memory = AsyncMock(return_value=True)
    manager.list_memories = MagicMock(return_value=[])
    manager.get_memory = MagicMock(return_value=MockMemory())
    manager.get_related = AsyncMock(return_value=[])
    manager.update_memory = AsyncMock(return_value=MockMemory())
    manager.get_stats = AsyncMock(return_value={"total": 0})
    manager.rebuild_crossrefs_for_memory = AsyncMock(return_value=2)
    manager.reindex_embeddings = AsyncMock(return_value={"success": True, "count": 5})
    manager.kg_service = None
    manager.db = MagicMock()
    return manager


@pytest.fixture
def mock_llm_service() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_backup_manager() -> MagicMock:
    manager = MagicMock()
    manager.restore = AsyncMock(return_value=5)
    manager.backup = AsyncMock(return_value=10)
    return manager


@pytest.fixture
def mock_session_manager() -> MagicMock:
    return MagicMock()


# ─── removed media ingestion tools ───────────────────────────────────────


class TestRemovedMediaIngestionTools:
    """Tests that obsolete memory media ingestion tools are not registered."""

    def test_image_and_screenshot_tools_not_registered(
        self,
        mock_memory_manager: MagicMock,
    ) -> None:
        registry = create_memory_registry(mock_memory_manager)
        tool_names = {
            tool["name"] if isinstance(tool, dict) else tool.name for tool in registry.list_tools()
        }

        assert "remember_with_image" not in tool_names
        assert "remember_screenshot" not in tool_names


# ─── backup_memories / restore_memories ─────────────────────────────────


class TestRestoreMemories:
    @pytest.mark.asyncio
    async def test_no_backup_manager(self, mock_memory_manager: MagicMock) -> None:
        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("restore_memories", {})
        assert result["success"] is False
        assert "not available" in result["error"]

    @pytest.mark.asyncio
    async def test_success(
        self,
        mock_memory_manager: MagicMock,
        mock_backup_manager: MagicMock,
    ) -> None:
        registry = create_memory_registry(
            mock_memory_manager, memory_backup_manager=mock_backup_manager
        )
        result = await registry.call("restore_memories", {})

        assert result["success"] is True
        assert result["restored"] == 5
        mock_backup_manager.restore.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exception(
        self,
        mock_memory_manager: MagicMock,
        mock_backup_manager: MagicMock,
    ) -> None:
        mock_backup_manager.restore.side_effect = RuntimeError("Restore crashed")
        registry = create_memory_registry(
            mock_memory_manager, memory_backup_manager=mock_backup_manager
        )
        result = await registry.call("restore_memories", {})

        assert result["success"] is False
        assert "Restore crashed" in result["error"]


class TestBackupMemories:
    @pytest.mark.asyncio
    async def test_no_backup_manager(self, mock_memory_manager: MagicMock) -> None:
        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("backup_memories", {})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_success(
        self,
        mock_memory_manager: MagicMock,
        mock_backup_manager: MagicMock,
    ) -> None:
        registry = create_memory_registry(
            mock_memory_manager, memory_backup_manager=mock_backup_manager
        )
        result = await registry.call("backup_memories", {})

        assert result["success"] is True
        assert result["backed_up"] == 10
        mock_backup_manager.backup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exception(
        self,
        mock_memory_manager: MagicMock,
        mock_backup_manager: MagicMock,
    ) -> None:
        mock_backup_manager.backup.side_effect = RuntimeError("Backup failed")
        registry = create_memory_registry(
            mock_memory_manager, memory_backup_manager=mock_backup_manager
        )
        result = await registry.call("backup_memories", {})

        assert result["success"] is False
        assert "Backup failed" in result["error"]


# ─── build_turn_and_digest ──────────────────────────────────────────────


class TestBuildTurnAndDigest:
    """Tests for build_turn_and_digest tool."""

    @pytest.mark.asyncio
    async def test_no_session_id(self, mock_memory_manager: MagicMock) -> None:
        """Returns error when session_id is empty."""
        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("build_turn_and_digest", {"session_id": ""})
        assert result["success"] is False
        assert "required" in result["error"]

    @pytest.mark.asyncio
    async def test_success(
        self,
        mock_memory_manager: MagicMock,
        mock_session_manager: MagicMock,
    ) -> None:
        """Successful turn and digest build."""
        with patch(
            "gobby.mcp_proxy.tools.memory._build_turn_and_digest",
            new_callable=AsyncMock,
            return_value={"turn_number": 1, "title": "Test"},
        ):
            registry = create_memory_registry(
                mock_memory_manager, session_manager=mock_session_manager
            )
            result = await registry.call("build_turn_and_digest", {"session_id": "sess-123"})

        assert result["success"] is True
        assert result["turn_number"] == 1
        assert result["title"] == "Test"

    @pytest.mark.asyncio
    async def test_digest_contract_error_returns_failure(
        self,
        mock_memory_manager: MagicMock,
        mock_session_manager: MagicMock,
    ) -> None:
        """Digest contract errors from the pipeline surface as tool failures."""
        with patch(
            "gobby.mcp_proxy.tools.memory._build_turn_and_digest",
            new_callable=AsyncMock,
            return_value={"error": "memory.turn_record returned invalid JSON contract"},
        ):
            registry = create_memory_registry(
                mock_memory_manager, session_manager=mock_session_manager
            )
            result = await registry.call("build_turn_and_digest", {"session_id": "sess-123"})

        assert result["success"] is False
        assert "invalid JSON contract" in result["error"]

    @pytest.mark.asyncio
    async def test_returns_none_skipped(
        self,
        mock_memory_manager: MagicMock,
        mock_session_manager: MagicMock,
    ) -> None:
        """Returns skipped when result is None."""
        with patch(
            "gobby.mcp_proxy.tools.memory._build_turn_and_digest",
            new_callable=AsyncMock,
            return_value=None,
        ):
            registry = create_memory_registry(
                mock_memory_manager, session_manager=mock_session_manager
            )
            result = await registry.call("build_turn_and_digest", {"session_id": "sess-123"})

        assert result["success"] is True
        assert result["skipped"] is True

    @pytest.mark.asyncio
    async def test_exception(
        self,
        mock_memory_manager: MagicMock,
        mock_session_manager: MagicMock,
    ) -> None:
        """Returns error on exception."""
        with patch(
            "gobby.mcp_proxy.tools.memory._build_turn_and_digest",
            new_callable=AsyncMock,
            side_effect=RuntimeError("LLM failed"),
        ):
            registry = create_memory_registry(
                mock_memory_manager, session_manager=mock_session_manager
            )
            result = await registry.call("build_turn_and_digest", {"session_id": "sess-123"})

        assert result["success"] is False
        assert "LLM failed" in result["error"]


# ─── rebuild_crossrefs ──────────────────────────────────────────────────


class TestRebuildCrossrefs:
    """Tests for rebuild_crossrefs tool."""

    @pytest.mark.asyncio
    async def test_success(self, mock_memory_manager: MagicMock) -> None:
        """Successful crossref rebuild."""
        mock_memory_manager.list_memories.return_value = [
            MockMemory(id="m1"),
            MockMemory(id="m2"),
        ]
        mock_memory_manager.rebuild_crossrefs_for_memory.return_value = 1

        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("rebuild_crossrefs", {})

        assert result["success"] is True
        assert result["memories_processed"] == 2
        assert result["crossrefs_created"] == 2

    @pytest.mark.asyncio
    async def test_partial_failure(self, mock_memory_manager: MagicMock) -> None:
        """Handles individual crossref failures."""
        mock_memory_manager.list_memories.return_value = [
            MockMemory(id="m1"),
            MockMemory(id="m2"),
        ]
        mock_memory_manager.rebuild_crossrefs_for_memory.side_effect = [
            Exception("fail"),
            1,
        ]

        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("rebuild_crossrefs", {})

        assert result["success"] is True
        assert result["crossrefs_created"] == 1

    @pytest.mark.asyncio
    async def test_list_error(self, mock_memory_manager: MagicMock) -> None:
        """Returns error when list_memories fails."""
        mock_memory_manager.list_memories.side_effect = Exception("DB error")
        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("rebuild_crossrefs", {})

        assert result["success"] is False
        assert "DB error" in result["error"]


# ─── rebuild_knowledge_graph ────────────────────────────────────────────


class TestRebuildKnowledgeGraph:
    """Tests for rebuild_knowledge_graph tool."""

    @pytest.mark.asyncio
    async def test_no_kg_service(self, mock_memory_manager: MagicMock) -> None:
        """Returns error when KG service not initialized."""
        mock_memory_manager.kg_service = None
        mock_memory_manager.rebuild_knowledge_graph = AsyncMock(
            return_value={
                "success": False,
                "error": "KnowledgeGraphService not initialized",
            }
        )
        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("rebuild_knowledge_graph", {})

        assert result["success"] is False
        assert "not initialized" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_success(self, mock_memory_manager: MagicMock) -> None:
        """Successful knowledge graph rebuild."""
        mock_memory_manager.rebuild_knowledge_graph = AsyncMock(
            return_value={
                "success": True,
                "memories_processed": 2,
                "memories_extracted": 2,
                "errors": 0,
            }
        )

        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("rebuild_knowledge_graph", {})

        assert result["success"] is True
        assert result["memories_extracted"] == 2
        assert result["errors"] == 0

    @pytest.mark.asyncio
    async def test_partial_failure(self, mock_memory_manager: MagicMock) -> None:
        """Counts errors on individual extraction failures."""
        mock_memory_manager.rebuild_knowledge_graph = AsyncMock(
            return_value={
                "success": True,
                "memories_processed": 2,
                "memories_extracted": 1,
                "errors": 1,
            }
        )

        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("rebuild_knowledge_graph", {})

        assert result["success"] is True
        assert result["memories_extracted"] == 1
        assert result["errors"] == 1


# ─── reindex_embeddings ─────────────────────────────────────────────────


class TestReindexEmbeddings:
    """Tests for reindex_embeddings tool."""

    @pytest.mark.asyncio
    async def test_success(self, mock_memory_manager: MagicMock) -> None:
        """Successful reindex."""
        mock_memory_manager.reindex_embeddings.return_value = {
            "success": True,
            "count": 10,
        }
        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("reindex_embeddings", {})

        assert result["success"] is True
        assert result["count"] == 10

    @pytest.mark.asyncio
    async def test_error(self, mock_memory_manager: MagicMock) -> None:
        """Returns error on exception."""
        mock_memory_manager.reindex_embeddings.side_effect = Exception("Embedding error")
        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("reindex_embeddings", {})

        assert result["success"] is False
        assert "Embedding error" in result["error"]


# ─── search_knowledge_graph ─────────────────────────────────────────────


class TestSearchKnowledgeGraph:
    """Tests for search_knowledge_graph tool."""

    @pytest.mark.asyncio
    async def test_no_kg_service(self, mock_memory_manager: MagicMock) -> None:
        """Returns empty results when KG service not available."""
        mock_memory_manager.kg_service = None
        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("search_knowledge_graph", {"query": "test"})

        assert result["success"] is True
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_success(self, mock_memory_manager: MagicMock) -> None:
        """Successful KG search."""
        mock_kg = MagicMock()
        mock_kg.search_graph = AsyncMock(return_value=[{"entity": "Python"}])
        mock_memory_manager.kg_service = mock_kg

        registry = create_memory_registry(mock_memory_manager)
        with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "project-a", "name": "Project A"}
            result = await registry.call("search_knowledge_graph", {"query": "Python", "limit": 5})

        assert result["success"] is True
        assert len(result["results"]) == 1
        mock_kg.search_graph.assert_awaited_once_with(
            "Python",
            limit=5,
            project_id="project-a",
            include_global=True,
        )

    @pytest.mark.asyncio
    async def test_error(self, mock_memory_manager: MagicMock) -> None:
        """Returns error on exception."""
        mock_kg = MagicMock()
        mock_kg.search_graph = AsyncMock(side_effect=Exception("KG down"))
        mock_memory_manager.kg_service = mock_kg

        registry = create_memory_registry(mock_memory_manager)
        result = await registry.call("search_knowledge_graph", {"query": "test"})

        assert result["success"] is False
        assert "KG down" in result["error"]


# ─── memory dream ────────────────────────────────────────────────────────


class TestMemoryDreamTools:
    """Tests for memory dream MCP wrappers."""

    @pytest.fixture(autouse=True)
    def _scoped_project(self) -> Iterator[None]:
        # Registration captures get_current_project_id as the tool's get_project_id;
        # a concrete project routes the tool to the scoped single-run path. Tests
        # that need the unscoped (all-due-projects) path re-patch this to None.
        with patch(
            "gobby.mcp_proxy.tools.memory.get_current_project_id",
            return_value="11111111-1111-4111-8111-111111110001",
        ):
            yield

    @pytest.mark.asyncio
    async def test_memory_dream_runs_service(
        self,
        mock_memory_manager: MagicMock,
        mock_llm_service: MagicMock,
    ) -> None:
        config = SimpleNamespace(memory=SimpleNamespace(dream=SimpleNamespace()))
        service = MagicMock()
        service.run = AsyncMock(return_value={"success": True, "run_id": "dream-1"})
        registry = create_memory_registry(
            mock_memory_manager,
            llm_service=mock_llm_service,
            config=config,
        )
        status_tool = registry.get_tool_metadata("memory_dream_status")
        assert status_tool is not None
        assert inspect.iscoroutinefunction(status_tool.func)

        with patch(
            "gobby.mcp_proxy.tools.memory_dream.MemoryDreamService",
            return_value=service,
        ):
            result = await registry.call(
                "memory_dream",
                {
                    "dry_run": True,
                    "wait": True,
                    "skip_consolidation": True,
                    "memory_type": "fact",
                },
            )

        assert result["success"] is True
        options = service.run.await_args.args[0]
        assert options.dry_run is True
        assert options.skip_consolidation is True
        assert options.memory_type == "fact"
        assert options.project_id == "11111111-1111-4111-8111-111111110001"

    @pytest.mark.asyncio
    async def test_memory_dream_unscoped_runs_all_due_projects(
        self,
        mock_memory_manager: MagicMock,
        mock_llm_service: MagicMock,
    ) -> None:
        config = SimpleNamespace(memory=SimpleNamespace(dream=SimpleNamespace()))
        service = MagicMock()
        aggregate = {
            "success": True,
            "targets": 1,
            "completed": 1,
            "failed": 0,
            "mutations": 2,
            "runs": [],
        }
        service.run_all_due_projects = AsyncMock(return_value=aggregate)
        service.run = AsyncMock()
        service.start_async = AsyncMock()

        with patch("gobby.mcp_proxy.tools.memory.get_current_project_id", return_value=None):
            registry = create_memory_registry(
                mock_memory_manager,
                llm_service=mock_llm_service,
                config=config,
            )
            with patch(
                "gobby.mcp_proxy.tools.memory_dream.MemoryDreamService",
                return_value=service,
            ):
                result = await registry.call(
                    "memory_dream",
                    {"dry_run": True, "wait": True, "full_sweep": True},
                )

        assert result == aggregate
        service.run_all_due_projects.assert_awaited_once()
        kwargs = service.run_all_due_projects.await_args.kwargs
        assert kwargs["dry_run"] is True
        assert kwargs["full_sweep"] is True
        # No project context → never the single-run path.
        service.run.assert_not_awaited()
        service.start_async.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_memory_dream_unscoped_background_returns_aggregate_run_id(
        self,
        mock_memory_manager: MagicMock,
        mock_llm_service: MagicMock,
    ) -> None:
        config = SimpleNamespace(memory=SimpleNamespace(dream=SimpleNamespace()))
        service = MagicMock()
        service.start_all_due_projects_async = AsyncMock(
            return_value={"success": True, "run_id": "aggregate-1"}
        )
        service.execute_all_due_projects_run = AsyncMock(return_value={"success": True})
        service.run_all_due_projects = AsyncMock()
        service.start_async = AsyncMock()

        with patch("gobby.mcp_proxy.tools.memory.get_current_project_id", return_value=None):
            registry = create_memory_registry(
                mock_memory_manager,
                llm_service=mock_llm_service,
                config=config,
            )
            with patch(
                "gobby.mcp_proxy.tools.memory_dream.MemoryDreamService",
                return_value=service,
            ):
                result = await registry.call(
                    "memory_dream",
                    {"dry_run": True, "wait": False, "full_sweep": True},
                )
                background_tasks = memory_dream_tools.get_background_tasks()
                assert len(background_tasks) == 1
                assert background_tasks[0].get_name() == "memory-dream:aggregate-1"
                background_results = await asyncio.gather(*background_tasks)

        assert result == {"success": True, "run_id": "aggregate-1", "status": "started"}
        assert background_results == [{"success": True}]
        assert memory_dream_tools.get_background_tasks() == ()
        service.start_all_due_projects_async.assert_awaited_once_with(
            dry_run=True,
            skip_consolidation=False,
            memory_type=None,
            full_sweep=True,
        )
        service.execute_all_due_projects_run.assert_awaited_once_with(
            "aggregate-1",
            dry_run=True,
            skip_consolidation=False,
            memory_type=None,
            full_sweep=True,
        )
        service.run_all_due_projects.assert_not_awaited()
        service.start_async.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_memory_dream_background_logs_failed_result(
        self,
        mock_memory_manager: MagicMock,
        mock_llm_service: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        config = SimpleNamespace(memory=SimpleNamespace(dream=SimpleNamespace()))
        service = MagicMock()
        service.start_async = AsyncMock(return_value={"success": True, "run_id": "dream-1"})
        service.execute_run = AsyncMock(return_value={"success": False, "error": "boom"})
        registry = create_memory_registry(
            mock_memory_manager,
            llm_service=mock_llm_service,
            config=config,
        )
        caplog.set_level(logging.WARNING, logger="gobby.mcp_proxy.tools.memory_dream")

        with patch(
            "gobby.mcp_proxy.tools.memory_dream.MemoryDreamService",
            return_value=service,
        ):
            result = await registry.call("memory_dream", {"wait": False})
            background_tasks = memory_dream_tools.get_background_tasks()
            await asyncio.gather(*background_tasks)

        assert result == {"success": True, "run_id": "dream-1", "status": "started"}
        assert len(background_tasks) == 1
        messages = [record.getMessage() for record in caplog.records]
        assert any("Background memory dream failed" in message for message in messages)
        assert any("dream-1" in message for message in messages)
        assert any("memory-dream:dream-1" in message for message in messages)

    @pytest.mark.asyncio
    async def test_memory_dream_background_create_task_failure_marks_run_failed(
        self,
        mock_memory_manager: MagicMock,
        mock_llm_service: MagicMock,
    ) -> None:
        config = SimpleNamespace(memory=SimpleNamespace(dream=SimpleNamespace()))
        service = MagicMock()
        service.start_async = AsyncMock(return_value={"success": True, "run_id": "dream-1"})
        service.execute_run = AsyncMock(return_value={"success": True})
        service.record_run_failure = MagicMock(return_value={"status": "failed"})
        registry = create_memory_registry(
            mock_memory_manager,
            llm_service=mock_llm_service,
            config=config,
        )

        with (
            patch(
                "gobby.mcp_proxy.tools.memory_dream.MemoryDreamService",
                return_value=service,
            ),
            patch(
                "gobby.mcp_proxy.tools.memory_dream.asyncio.create_task",
                side_effect=RuntimeError("scheduler unavailable"),
            ),
        ):
            result = await registry.call("memory_dream", {"wait": False})

        assert result["success"] is False
        assert result["run_id"] == "dream-1"
        assert result["status"] == "failed"
        assert "scheduler unavailable" in result["error"]
        service.record_run_failure.assert_called_once()
        assert memory_dream_tools.get_background_tasks() == ()

    async def test_memory_dream_status_and_revert(
        self,
        mock_memory_manager: MagicMock,
        mock_llm_service: MagicMock,
    ) -> None:
        config = SimpleNamespace(memory=SimpleNamespace(dream=SimpleNamespace()))
        service = MagicMock()
        service.status = AsyncMock(return_value={"success": True, "run": {"id": "dream-1"}})
        service.revert = AsyncMock(return_value={"success": True, "run_id": "dream-1"})
        registry = create_memory_registry(
            mock_memory_manager,
            llm_service=mock_llm_service,
            config=config,
        )

        with patch(
            "gobby.mcp_proxy.tools.memory_dream.MemoryDreamService",
            return_value=service,
        ):
            status = await registry.call("memory_dream_status", {"run_id": "dream-1"})
            revert = await registry.call("memory_dream_revert", {"run_id": "dream-1"})

        assert status["success"] is True
        assert revert["success"] is True
        service.status.assert_awaited_once_with("dream-1")
        service.revert.assert_awaited_once_with("dream-1")

    @pytest.mark.asyncio
    async def test_memory_dream_reuses_registered_service(
        self,
        mock_memory_manager: MagicMock,
        mock_llm_service: MagicMock,
    ) -> None:
        config = SimpleNamespace(memory=SimpleNamespace(dream=SimpleNamespace()))
        service = MagicMock()
        service.run = AsyncMock(return_value={"success": True, "run_id": "dream-1"})
        service.status = AsyncMock(return_value={"success": True, "run": {"id": "dream-1"}})
        service.revert = AsyncMock(return_value={"success": True, "run_id": "dream-1"})
        registry = create_memory_registry(
            mock_memory_manager,
            llm_service=mock_llm_service,
            config=config,
        )

        with patch(
            "gobby.mcp_proxy.tools.memory_dream.MemoryDreamService",
            return_value=service,
        ) as service_factory:
            run = await registry.call("memory_dream", {"wait": True})
            status = await registry.call("memory_dream_status", {"run_id": "dream-1"})
            revert = await registry.call("memory_dream_revert", {"run_id": "dream-1"})

        assert run == {"success": True, "run_id": "dream-1"}
        assert status == {"success": True, "run": {"id": "dream-1"}}
        assert revert == {"success": True, "run_id": "dream-1"}
        service_factory.assert_called_once()

    @pytest.mark.asyncio
    async def test_memory_dream_start_failure_releases_background_slot(
        self,
        mock_memory_manager: MagicMock,
        mock_llm_service: MagicMock,
    ) -> None:
        config = SimpleNamespace(memory=SimpleNamespace(dream=SimpleNamespace()))
        service = MagicMock()
        service.start_async = AsyncMock(
            side_effect=[
                *[
                    RuntimeError("database unavailable")
                    for _ in range(memory_dream_tools.MAX_BACKGROUND_DREAM_TASKS)
                ],
                {"success": True, "run_id": "dream-1"},
            ]
        )
        service.execute_run = AsyncMock(return_value={"success": True})
        registry = create_memory_registry(
            mock_memory_manager,
            llm_service=mock_llm_service,
            config=config,
        )

        with patch(
            "gobby.mcp_proxy.tools.memory_dream.MemoryDreamService",
            return_value=service,
        ):
            for _ in range(memory_dream_tools.MAX_BACKGROUND_DREAM_TASKS):
                with pytest.raises(RuntimeError, match="database unavailable"):
                    await registry.call("memory_dream", {"wait": False})

            result = await registry.call("memory_dream", {"wait": False})
            background_tasks = memory_dream_tools.get_background_tasks()
            await asyncio.gather(*background_tasks)

        assert result == {"success": True, "run_id": "dream-1", "status": "started"}
        assert service.start_async.await_count == memory_dream_tools.MAX_BACKGROUND_DREAM_TASKS + 1

    @pytest.mark.asyncio
    async def test_memory_dream_background_start_respects_task_cap(
        self,
        mock_memory_manager: MagicMock,
        mock_llm_service: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        config = SimpleNamespace(memory=SimpleNamespace(dream=SimpleNamespace()))
        service = MagicMock()
        service.start_async = AsyncMock(return_value={"success": True, "run_id": "dream-1"})
        registry = create_memory_registry(
            mock_memory_manager,
            llm_service=mock_llm_service,
            config=config,
        )
        monkeypatch.setattr(memory_dream_tools, "MAX_BACKGROUND_DREAM_TASKS", 0)

        with patch(
            "gobby.mcp_proxy.tools.memory_dream.MemoryDreamService",
            return_value=service,
        ):
            result = await registry.call("memory_dream", {"wait": False})

        assert result["success"] is False
        assert "limit reached" in result["error"].lower()
        service.start_async.assert_not_called()
