"""Tests for gobby.mcp_proxy.tools.memory - additional coverage for edge cases.

Focuses on:
- removed image/screenshot ingestion tools
- backup_memories / restore_memories
- judge_shadow_relevance
- rebuild_crossrefs / rebuild_knowledge_graph
- reindex_embeddings
- search_knowledge_graph edge cases
"""

import asyncio
import inspect
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.memory import create_memory_registry
from gobby.memory.dream.coordinator import MemoryDreamCoordinator
from gobby.memory.dream.service import MemoryDreamService

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
    registry = create_memory_registry(lambda: mock_memory_manager)
    ephemeral = {
        "content": "Gobby build #epic E2E docs test #14353 completed.",
        "memory_type": "implementation_note",
        "tags": ["gobby", "build-e2e", "#14353"],
        "rationale": (
            "Durable convention: future sessions should reuse this so they do not rediscover it."
        ),
    }

    skipped = await registry.call("create_memory", ephemeral)
    assert skipped["skipped"] is True
    persisted = await registry.call(
        "create_memory",
        {**ephemeral, "supersedes": [superseded_id]},
    )
    assert persisted["success"] is True, persisted
    assert mock_memory_manager.create_memory.call_args.kwargs["supersedes"] == [superseded_id]


_RATIONALE_REQUIRED_PREFIX = "rationale_required:"
_VALID_RATIONALE = (
    "Durable convention: future sessions should reuse this so they do not rediscover it."
)


@pytest.mark.asyncio
def test_create_memory_description_routes_bugs_to_tasks(
    mock_memory_manager: MagicMock,
) -> None:
    registry = create_memory_registry(lambda: mock_memory_manager)
    tool = registry.get_tool_metadata("create_memory")
    assert tool is not None
    assert "gobby-tasks.create_task" in tool.description
    assert "claim=true" in tool.description
    assert tool.description.find("gobby-tasks.create_task") < tool.description.find(
        "rationale is mandatory"
    )


async def test_create_memory_requires_rationale(mock_memory_manager: MagicMock) -> None:
    registry = create_memory_registry(lambda: mock_memory_manager)
    payload = {"content": "Always use psycopg %s placeholders in hub SQL."}

    missing = await registry.call("create_memory", payload)
    assert missing["success"] is False
    assert str(missing["error"]).startswith(_RATIONALE_REQUIRED_PREFIX)

    empty = await registry.call("create_memory", {**payload, "rationale": "   "})
    assert empty["success"] is False
    assert str(empty["error"]).startswith(_RATIONALE_REQUIRED_PREFIX)

    too_long = await registry.call("create_memory", {**payload, "rationale": "x" * 501})
    assert too_long["success"] is False
    assert str(too_long["error"]).startswith(_RATIONALE_REQUIRED_PREFIX)

    ephemeral = await registry.call(
        "create_memory",
        {
            "content": "Gobby build #epic E2E docs test #14353 completed.",
            "memory_type": "implementation_note",
            "tags": ["gobby", "build-e2e", "#14353"],
            "rationale": "",
        },
    )
    assert ephemeral["success"] is False
    assert str(ephemeral["error"]).startswith(_RATIONALE_REQUIRED_PREFIX)
    mock_memory_manager.create_memory.assert_not_called()

    persisted = await registry.call("create_memory", {**payload, "rationale": _VALID_RATIONALE})
    assert persisted["success"] is True, persisted
    assert persisted["memory"]["rationale"] == _VALID_RATIONALE
    assert mock_memory_manager.create_memory.call_args.kwargs["rationale"] == _VALID_RATIONALE


@pytest.mark.asyncio
async def test_create_memory_derives_task_and_agent_provenance(
    mock_memory_manager: MagicMock,
) -> None:
    registry = create_memory_registry(lambda: mock_memory_manager)
    session_id = "11111111-1111-4111-8111-111111110042"
    task_id = "22222222-2222-4222-8222-222222220001"
    claimed = MagicMock()
    claimed.id = task_id
    agent_run = MagicMock()
    agent_run.agent_name = "backend-developer"
    interactive = MagicMock()
    interactive.source = "claude"

    with (
        patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001"},
        ),
        patch(
            "gobby.storage.session_resolution.resolve_session_reference",
            return_value=session_id,
        ),
        patch("gobby.storage.tasks.LocalTaskManager") as mock_task_manager_cls,
        patch("gobby.storage.agents.LocalAgentRunManager") as mock_agent_mgr_cls,
        patch("gobby.storage.sessions.SessionManager") as mock_session_mgr_cls,
    ):
        mock_task_manager_cls.return_value.list_tasks.return_value = [claimed]
        mock_agent_mgr_cls.return_value.get_by_session.return_value = agent_run
        mock_session_mgr_cls.return_value.get.return_value = interactive

        agent_result = await registry.call(
            "create_memory",
            {
                "content": "Always use psycopg %s placeholders in hub SQL.",
                "rationale": _VALID_RATIONALE,
                "session_id": session_id,
            },
        )
        assert agent_result["success"] is True, agent_result
        agent_kwargs = mock_memory_manager.create_memory.call_args.kwargs
        assert agent_kwargs["source_task_id"] == task_id
        assert agent_kwargs["created_by_agent"] == "backend-developer"
        assert agent_result["memory"]["source_task_id"] == task_id
        assert agent_result["memory"]["created_by_agent"] == "backend-developer"

        mock_agent_mgr_cls.return_value.get_by_session.return_value = None
        interactive_result = await registry.call(
            "create_memory",
            {
                "content": "Interactive sessions record the CLI source as created_by_agent.",
                "rationale": _VALID_RATIONALE,
                "session_id": session_id,
            },
        )
        assert interactive_result["success"] is True, interactive_result
        interactive_kwargs = mock_memory_manager.create_memory.call_args.kwargs
        assert interactive_kwargs["source_task_id"] == task_id
        assert interactive_kwargs["created_by_agent"] == "claude"
        assert interactive_result["memory"]["created_by_agent"] == "claude"


@pytest.mark.asyncio
async def test_create_memory_provenance_db_error_does_not_create(
    mock_memory_manager: MagicMock,
) -> None:
    import psycopg

    registry = create_memory_registry(lambda: mock_memory_manager)
    session_id = "11111111-1111-4111-8111-111111110042"
    with (
        patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001"},
        ),
        patch(
            "gobby.storage.session_resolution.resolve_session_reference",
            return_value=session_id,
        ),
        patch("gobby.storage.tasks.LocalTaskManager") as mock_task_manager_cls,
    ):
        mock_task_manager_cls.return_value.list_tasks.side_effect = psycopg.OperationalError(
            "connection lost"
        )
        result = await registry.call(
            "create_memory",
            {
                "content": "Always use psycopg %s placeholders in hub SQL.",
                "rationale": _VALID_RATIONALE,
                "session_id": session_id,
            },
        )
    assert result["success"] is False
    mock_memory_manager.create_memory.assert_not_called()


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
        registry = create_memory_registry(lambda: mock_memory_manager)
        tool_names = {
            tool["name"] if isinstance(tool, dict) else tool.name for tool in registry.list_tools()
        }

        assert "remember_with_image" not in tool_names
        assert "remember_screenshot" not in tool_names


# ─── backup_memories / restore_memories ─────────────────────────────────


class TestRestoreMemories:
    @pytest.mark.asyncio
    async def test_no_backup_manager(self, mock_memory_manager: MagicMock) -> None:
        registry = create_memory_registry(lambda: mock_memory_manager)
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
            lambda: mock_memory_manager, memory_backup_manager_resolver=lambda: mock_backup_manager
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
            lambda: mock_memory_manager, memory_backup_manager_resolver=lambda: mock_backup_manager
        )
        result = await registry.call("restore_memories", {})

        assert result["success"] is False
        assert "Restore crashed" in result["error"]


class TestBackupMemories:
    @pytest.mark.asyncio
    async def test_no_backup_manager(self, mock_memory_manager: MagicMock) -> None:
        registry = create_memory_registry(lambda: mock_memory_manager)
        result = await registry.call("backup_memories", {})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_success(
        self,
        mock_memory_manager: MagicMock,
        mock_backup_manager: MagicMock,
    ) -> None:
        registry = create_memory_registry(
            lambda: mock_memory_manager, memory_backup_manager_resolver=lambda: mock_backup_manager
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
            lambda: mock_memory_manager, memory_backup_manager_resolver=lambda: mock_backup_manager
        )
        result = await registry.call("backup_memories", {})

        assert result["success"] is False
        assert "Backup failed" in result["error"]


# ─── judge_shadow_relevance ──────────────────────────────────────────────


class TestJudgeShadowRelevance:
    """Tests for judge_shadow_relevance tool."""

    @pytest.mark.asyncio
    async def test_no_session_id(self, mock_memory_manager: MagicMock) -> None:
        """Returns error when session_id is empty."""
        registry = create_memory_registry(lambda: mock_memory_manager)
        result = await registry.call("judge_shadow_relevance", {"session_id": ""})
        assert result["success"] is False
        assert "required" in result["error"]

    @pytest.mark.asyncio
    async def test_success(
        self,
        mock_memory_manager: MagicMock,
    ) -> None:
        """Successful relevance judging returns the completed count."""
        with patch(
            "gobby.mcp_proxy.tools.memory.judge_shadow_candidate_relevance",
            new_callable=AsyncMock,
            return_value=2,
        ):
            registry = create_memory_registry(
                lambda: mock_memory_manager,
                llm_service_resolver=lambda: MagicMock(),
                startup_config=MagicMock(),
            )
            result = await registry.call("judge_shadow_relevance", {"session_id": "sess-123"})

        assert result["success"] is True
        assert result["completed"] == 2


# ─── rebuild_crossrefs ──────────────────────────────────────────────────


class TestRebuildCrossrefs:
    """Tests for rebuild_crossrefs tool."""

    @pytest.mark.asyncio
    async def test_success(self, mock_memory_manager: MagicMock) -> None:
        """Successful crossref rebuild."""
        mock_memory_manager.list_memories.return_value = [
            MockMemory(id="21000000-0000-4000-8000-000000000005"),
            MockMemory(id="m2"),
        ]
        mock_memory_manager.rebuild_crossrefs_for_memory.return_value = 1

        registry = create_memory_registry(lambda: mock_memory_manager)
        result = await registry.call("rebuild_crossrefs", {})

        assert result["success"] is True
        assert result["memories_processed"] == 2
        assert result["crossrefs_created"] == 2

    @pytest.mark.asyncio
    async def test_partial_failure(self, mock_memory_manager: MagicMock) -> None:
        """Handles individual crossref failures."""
        mock_memory_manager.list_memories.return_value = [
            MockMemory(id="21000000-0000-4000-8000-000000000005"),
            MockMemory(id="m2"),
        ]
        mock_memory_manager.rebuild_crossrefs_for_memory.side_effect = [
            Exception("fail"),
            1,
        ]

        registry = create_memory_registry(lambda: mock_memory_manager)
        result = await registry.call("rebuild_crossrefs", {})

        assert result["success"] is True
        assert result["crossrefs_created"] == 1

    @pytest.mark.asyncio
    async def test_list_error(self, mock_memory_manager: MagicMock) -> None:
        """Returns error when list_memories fails."""
        mock_memory_manager.list_memories.side_effect = Exception("DB error")
        registry = create_memory_registry(lambda: mock_memory_manager)
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
        registry = create_memory_registry(lambda: mock_memory_manager)
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

        registry = create_memory_registry(lambda: mock_memory_manager)
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

        registry = create_memory_registry(lambda: mock_memory_manager)
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
        registry = create_memory_registry(lambda: mock_memory_manager)
        result = await registry.call("reindex_embeddings", {})

        assert result["success"] is True
        assert result["count"] == 10

    @pytest.mark.asyncio
    async def test_error(self, mock_memory_manager: MagicMock) -> None:
        """Returns error on exception."""
        mock_memory_manager.reindex_embeddings.side_effect = Exception("Embedding error")
        registry = create_memory_registry(lambda: mock_memory_manager)
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
        registry = create_memory_registry(lambda: mock_memory_manager)
        result = await registry.call("search_knowledge_graph", {"query": "test"})

        assert result["success"] is True
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_success(self, mock_memory_manager: MagicMock) -> None:
        """Successful KG search."""
        mock_kg = MagicMock()
        mock_kg.search_graph = AsyncMock(return_value=[{"entity": "Python"}])
        mock_memory_manager.kg_service = mock_kg

        registry = create_memory_registry(lambda: mock_memory_manager)
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

        registry = create_memory_registry(lambda: mock_memory_manager)
        result = await registry.call("search_knowledge_graph", {"query": "test"})

        assert result["success"] is False
        assert "KG down" in result["error"]


# ─── memory dream ──────────────────────────────────────────────────────────


def _fake_coordinator() -> MagicMock:
    """Fake daemon-owned dream coordinator resolved by the MCP tools."""
    coordinator = MagicMock()
    coordinator.trigger = AsyncMock()
    coordinator.trigger_all_due_projects = AsyncMock()
    coordinator.service.status = AsyncMock()
    coordinator.service.revert = AsyncMock()
    return coordinator


def _dream_registry(
    mock_memory_manager: MagicMock, coordinator: MemoryDreamCoordinator | MagicMock | None
) -> InternalToolRegistry:
    return create_memory_registry(
        lambda: mock_memory_manager,
        dream_coordinator_resolver=lambda: coordinator,
    )


class _AdmissionFakeService:
    """Admission-contract fake: first start admits, later starts coalesce."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.starts = 0

    async def start_async(self, options: Any) -> dict[str, Any]:
        self.starts += 1
        if self.starts == 1:
            return {"success": True, "run_id": "run-1"}
        return {
            "success": True,
            "run_id": "run-1",
            "coalesced": True,
            "active": {"run_id": "run-1", "phase": "sweep", "checkpoint": None},
        }

    async def execute_run(self, run_id: str, options: Any) -> dict[str, Any]:
        await self.release.wait()
        return {"success": True}


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

    def test_memory_dream_exposes_no_wait_parameter(self, mock_memory_manager: MagicMock) -> None:
        registry = _dream_registry(mock_memory_manager, _fake_coordinator())
        tool = registry.get_tool_metadata("memory_dream")
        assert tool is not None
        assert inspect.iscoroutinefunction(tool.func)
        parameters = inspect.signature(tool.func).parameters
        assert "wait" not in parameters
        assert set(parameters) == {"dry_run", "skip_consolidation", "memory_type", "full_sweep"}

    @pytest.mark.asyncio
    async def test_memory_dream_scoped_triggers_run(self, mock_memory_manager: MagicMock) -> None:
        coordinator = _fake_coordinator()
        coordinator.trigger.return_value = {
            "success": True,
            "run_id": "dream-1",
            "status": "running",
            "coalesced": False,
        }
        registry = _dream_registry(mock_memory_manager, coordinator)

        result = await registry.call(
            "memory_dream",
            {"dry_run": True, "skip_consolidation": True, "memory_type": "fact"},
        )

        assert result == {
            "success": True,
            "run_id": "dream-1",
            "status": "running",
            "coalesced": False,
        }
        options = coordinator.trigger.await_args.args[0]
        assert options.dry_run is True
        assert options.skip_consolidation is True
        assert options.memory_type == "fact"
        assert options.project_id == "11111111-1111-4111-8111-111111110001"
        coordinator.trigger_all_due_projects.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_memory_dream_unscoped_triggers_all_due_projects(
        self, mock_memory_manager: MagicMock
    ) -> None:
        coordinator = _fake_coordinator()
        coordinator.trigger_all_due_projects.return_value = {
            "success": True,
            "run_id": "aggregate-1",
            "status": "running",
            "coalesced": False,
        }

        with patch("gobby.mcp_proxy.tools.memory.get_current_project_id", return_value=None):
            registry = _dream_registry(mock_memory_manager, coordinator)
            result = await registry.call(
                "memory_dream",
                {"dry_run": True, "full_sweep": True},
            )

        assert result["run_id"] == "aggregate-1"
        coordinator.trigger_all_due_projects.assert_awaited_once_with(
            dry_run=True,
            skip_consolidation=False,
            memory_type=None,
            full_sweep=True,
        )
        # No project context → never the single-run path.
        coordinator.trigger.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_memory_dream_conflict_passthrough(self, mock_memory_manager: MagicMock) -> None:
        coordinator = _fake_coordinator()
        coordinator.trigger.return_value = {
            "success": False,
            "error": "a memory dream run is already active with incompatible options",
            "error_code": "dream_run_conflict",
            "conflict": {"run_id": "other-1", "scope": "all", "phase": "sweep"},
        }
        registry = _dream_registry(mock_memory_manager, coordinator)

        result = await registry.call("memory_dream", {})

        assert result["success"] is False
        assert result["error_code"] == "dream_run_conflict"
        assert result["conflict"]["run_id"] == "other-1"

    @pytest.mark.asyncio
    async def test_memory_dream_holds_at_most_one_background_task(
        self, mock_memory_manager: MagicMock
    ) -> None:
        # The real coordinator over an admission-contract fake: the second
        # trigger coalesces onto the active run instead of launching a second
        # background task.
        service = _AdmissionFakeService()
        coordinator = MemoryDreamCoordinator(cast("MemoryDreamService", service))
        registry = _dream_registry(mock_memory_manager, coordinator)

        first = await registry.call("memory_dream", {})
        second = await registry.call("memory_dream", {})

        assert first == {
            "success": True,
            "run_id": "run-1",
            "status": "running",
            "coalesced": False,
        }
        assert second["coalesced"] is True
        assert second["run_id"] == "run-1"
        tasks = coordinator.background_tasks()
        assert len(tasks) == 1
        assert tasks[0].get_name() == "memory-dream:run-1"

        service.release.set()
        await asyncio.gather(*tasks)
        assert coordinator.background_tasks() == ()

    @pytest.mark.asyncio
    async def test_memory_dream_status_exposes_durable_checkpoint(
        self, mock_memory_manager: MagicMock
    ) -> None:
        coordinator = _fake_coordinator()
        checkpoint = {
            "phase": "sweep",
            "scope": "project:proj-1",
            "pass_number": 1,
            "batch_number": 4,
            "completed": 100,
            "remaining": 25,
            "channels": {"vector": {"attempts": 2, "latency_ms": 87}},
            "mutations": 9,
            "backlog": {"project:proj-1": 25},
            "stop_reason": None,
        }
        coordinator.service.status.return_value = {
            "success": True,
            "run": {"id": "dream-1", "status": "running", "checkpoint": checkpoint},
        }
        registry = _dream_registry(mock_memory_manager, coordinator)

        result = await registry.call("memory_dream_status", {"run_id": "dream-1"})

        assert result["success"] is True
        assert result["run"]["checkpoint"] == checkpoint
        coordinator.service.status.assert_awaited_once_with("dream-1")

    @pytest.mark.asyncio
    async def test_memory_dream_status_and_revert(self, mock_memory_manager: MagicMock) -> None:
        coordinator = _fake_coordinator()
        coordinator.service.status.return_value = {"success": True, "run": {"id": "dream-1"}}
        coordinator.service.revert.return_value = {"success": True, "run_id": "dream-1"}
        registry = _dream_registry(mock_memory_manager, coordinator)

        status = await registry.call("memory_dream_status", {"run_id": "dream-1"})
        revert = await registry.call("memory_dream_revert", {"run_id": "dream-1"})

        assert status["success"] is True
        assert revert["success"] is True
        coordinator.service.status.assert_awaited_once_with("dream-1")
        coordinator.service.revert.assert_awaited_once_with("dream-1")

    @pytest.mark.asyncio
    async def test_memory_dream_unavailable_coordinator(
        self, mock_memory_manager: MagicMock
    ) -> None:
        registry = _dream_registry(mock_memory_manager, None)

        for name, args in (
            ("memory_dream", {}),
            ("memory_dream_status", {"run_id": "dream-1"}),
            ("memory_dream_revert", {"run_id": "dream-1"}),
        ):
            result = await registry.call(name, args)
            assert result == {
                "success": False,
                "error": "memory dream coordinator is unavailable",
            }


class TestMemoryWriteToolModuleSplit:
    """Deliverable 3.1: write-path tools live in their own module.

    `memory.py` sat at 999 lines, one line under the production ceiling, so the
    write tools moved to `memory_write.py` behind a single entry point that
    `create_memory_registry` calls.
    """

    _WRITE_TOOLS = ("create_memory", "update_memory", "delete_memory", "restore_memory")

    def test_write_tools_register_through_the_new_module(
        self, mock_memory_manager: MagicMock
    ) -> None:
        from gobby.mcp_proxy.tools import memory_write

        registry = create_memory_registry(lambda: mock_memory_manager)

        for name in self._WRITE_TOOLS:
            assert name in registry, f"{name} is not registered"
        assert callable(memory_write.register_memory_write_tools)

    def test_write_tools_are_defined_in_memory_write(self) -> None:
        from gobby.mcp_proxy.tools import memory as memory_module
        from gobby.mcp_proxy.tools import memory_write

        source = inspect.getsource(memory_write.register_memory_write_tools)
        for name in self._WRITE_TOOLS:
            assert f"def {name}(" in source, f"{name} was not moved to memory_write"
        assert "register_memory_write_tools" in inspect.getsource(
            memory_module.create_memory_registry
        )

    def test_both_modules_stay_under_the_production_ceiling(self) -> None:
        from gobby.mcp_proxy.tools import memory as memory_module
        from gobby.mcp_proxy.tools import memory_write

        for module in (memory_module, memory_write):
            path = Path(str(module.__file__))
            line_count = len(path.read_text(encoding="utf-8").splitlines())
            assert line_count < 1000, f"{path.name} is {line_count} lines"
