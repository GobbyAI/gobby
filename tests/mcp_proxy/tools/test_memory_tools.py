"""
Tests for gobby.mcp_proxy.tools.memory module.

Tests the memory MCP tools including:
- create_memory
- search_memories
- delete_memory
- list_memories
- get_memory
- get_related_memories
- update_memory
- memory_stats
- search_knowledge_graph
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.memory import create_memory_registry
from gobby.mcp_proxy.tools.memory_scope import get_current_project_id
from gobby.storage.memories import MemoryType
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.utils.session_context import SessionContext, session_context_for_test

pytestmark = pytest.mark.unit

_VALID_RATIONALE = (
    "Durable convention: future sessions should reuse this so they do not rediscover it."
)


class MockMemory:
    """Mock memory object for tests."""

    # Provenance the tools read with getattr; tests set them per case.
    rationale: str | None = None
    source_task_id: str | None = None
    created_by_agent: str | None = None
    graph_confidence: float | None = None
    collapsed_duplicates: list[str] | None = None

    def __init__(
        self,
        id: str = "mem-123",
        content: str = "Test memory content",
        memory_type: str = "fact",
        created_at: str = "2024-01-01T00:00:00",
        updated_at: str | None = None,
        project_id: str = PERSONAL_PROJECT_ID,
        is_global: bool = False,
        source_type: str = "agent",
        source_session_id: str | None = None,
        access_count: int = 0,
        tags: list[str] | None = None,
        similarity: float | None = None,
        search_via: str | None = None,
        ranking_score: float | None = None,
        raw_semantic_score: float | None = None,
        temporal_decay_factor: float | None = None,
        ranking_mode: str | None = None,
    ):
        self.id = id
        self.content = content
        self.memory_type = memory_type
        self.created_at = created_at
        self.updated_at = updated_at or created_at
        self.project_id = project_id
        self.is_global = is_global
        self.source_type = source_type
        self.source_session_id = source_session_id
        self.access_count = access_count
        self.tags = tags or []
        if similarity is not None:
            self.similarity = similarity
        if search_via is not None:
            self.search_via = search_via
        if ranking_score is not None:
            self.ranking_score = ranking_score
        if raw_semantic_score is not None:
            self.raw_semantic_score = raw_semantic_score
        if temporal_decay_factor is not None:
            self.temporal_decay_factor = temporal_decay_factor
        if ranking_mode is not None:
            self.ranking_mode = ranking_mode


@pytest.fixture
def mock_memory_manager() -> MagicMock:
    """Create a mock memory manager."""
    manager = MagicMock()
    manager.create_memory = AsyncMock(return_value=MockMemory())
    manager.search_memories = AsyncMock(return_value=[MockMemory()])
    manager.delete_memory = AsyncMock(return_value=True)
    manager.delete_memory_scoped = AsyncMock(return_value=True)
    manager.list_memories = MagicMock(return_value=[MockMemory()])
    manager.get_memory = MagicMock(return_value=MockMemory())
    # Identity resolution passes refs through unchanged unless a test overrides it.
    manager.resolve_memory_id = MagicMock(side_effect=lambda ref, project_id=None: ref)
    manager.get_related = AsyncMock(return_value=[MockMemory()])
    manager.update_memory = AsyncMock(return_value=MockMemory())
    manager.update_memory_scoped = AsyncMock(return_value=MockMemory())
    manager.promote_memory = AsyncMock(return_value=MockMemory(is_global=True))
    manager.get_stats = AsyncMock(return_value={"total": 10, "by_type": {"fact": 5}})
    manager.db = MagicMock()
    manager.content_exists = MagicMock(return_value=False)
    manager.config = MagicMock()
    manager.injection_outcome_recorder = None
    return manager


@pytest.fixture
def memory_registry(mock_memory_manager: MagicMock) -> InternalToolRegistry:
    """Create a memory registry with mocked dependencies."""
    return create_memory_registry(lambda: mock_memory_manager)


class TestGetCurrentProjectId:
    """Tests for get_current_project_id helper."""

    def test_returns_project_id(self) -> None:
        """Returns project ID when available."""
        with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "project-123", "name": "test"}
            result = get_current_project_id()
            assert result == "project-123"

    def test_returns_none_when_no_context(self) -> None:
        """Returns None when no project context."""
        with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
            mock_ctx.return_value = None
            result = get_current_project_id()
            assert result is None

    def test_returns_none_when_no_id(self) -> None:
        """Returns None when context has no ID."""
        with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"name": "test"}
            result = get_current_project_id()
            assert result is None


class TestCreateMemory:
    """Tests for create_memory tool."""

    @pytest.mark.asyncio
    async def test_create_memory_success(self, memory_registry, mock_memory_manager):
        """Test successful memory creation with similar_existing in response."""
        mock_memory_manager.search_memories.return_value = [
            MockMemory(id="existing-1", content="Similar memory", similarity=0.85),
        ]

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001"},
        ):
            result = await memory_registry.call(
                "create_memory",
                {
                    "content": "Test content",
                    "memory_type": "fact",
                    "rationale": _VALID_RATIONALE,
                },
            )

        assert result["success"] is True
        assert "memory" in result
        assert result["memory"]["id"] == "mem-123"
        assert "similar_existing" in result
        assert len(result["similar_existing"]) == 1
        assert result["similar_existing"][0]["id"] == "existing-1"
        assert result["similar_existing"][0]["similarity"] == 0.85
        mock_memory_manager.create_memory.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_memory_with_tags(self, memory_registry, mock_memory_manager):
        """Test memory creation with tags."""
        mock_memory_manager.search_memories.return_value = []

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001"},
        ):
            result = await memory_registry.call(
                "create_memory",
                {"content": "Test", "tags": ["tag1", "tag2"], "rationale": _VALID_RATIONALE},
            )

        assert result["success"] is True
        assert result["similar_existing"] == []
        call_kwargs = mock_memory_manager.create_memory.call_args.kwargs
        assert call_kwargs["tags"] == ["tag1", "tag2"]

    @pytest.mark.asyncio
    async def test_create_memory_skips_ephemeral_implementation_note(
        self,
        memory_registry,
        mock_memory_manager,
    ) -> None:
        """Run-specific build notes should not enter persistent memory."""
        result = await memory_registry.call(
            "create_memory",
            {
                "content": "Gobby build #epic E2E docs test #14353 completed.",
                "memory_type": "implementation_note",
                "tags": ["gobby", "build-e2e", "#14353"],
                "rationale": _VALID_RATIONALE,
            },
        )

        assert result == {
            "success": True,
            "skipped": True,
            "reason": "ephemeral_implementation_note",
        }
        mock_memory_manager.create_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_memory_persists_proposal_without_task_redirect(
        self,
        mock_memory_manager,
    ) -> None:
        registry = create_memory_registry(lambda: mock_memory_manager)
        proposal = (
            "SessionStart should persist a durable completion marker. If any invariant is "
            "missing, call an idempotent ensure_session_activation(session_id) helper that "
            "creates only missing pieces and preserves existing progress; do not replay raw "
            "SessionStart side effects wholesale."
        )

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001"},
        ):
            result = await registry.call(
                "create_memory",
                {"content": proposal, "rationale": _VALID_RATIONALE},
            )

        assert result["success"] is True
        assert "redirected_to_task_note" not in result
        call_kwargs = mock_memory_manager.create_memory.call_args.kwargs
        assert call_kwargs["content"] == proposal

    @pytest.mark.asyncio
    async def test_create_memory_rejects_noncanonical_type(
        self,
        memory_registry,
        mock_memory_manager,
    ) -> None:
        result = await memory_registry.call(
            "create_memory",
            {
                "content": "Bad type",
                "memory_type": "debugging_pattern",
                "rationale": _VALID_RATIONALE,
            },
        )

        assert result["success"] is False
        assert "Invalid memory_type 'debugging_pattern'" in result["error"]
        mock_memory_manager.create_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_memory_with_session_id(self, memory_registry, mock_memory_manager):
        """Test memory creation resolves and passes source_session_id."""
        mock_memory_manager.search_memories.return_value = []
        resolved_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        with (
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value={"id": "11111111-1111-4111-8111-111111110001"},
            ),
            patch(
                "gobby.storage.session_resolution.resolve_session_reference",
                return_value=resolved_uuid,
            ) as mock_resolve,
            patch(
                "gobby.mcp_proxy.tools.memory_write.derive_memory_create_provenance",
                return_value=(None, None),
            ),
        ):
            result = await memory_registry.call(
                "create_memory",
                {"content": "Test", "session_id": "#42", "rationale": _VALID_RATIONALE},
            )

        assert result["success"] is True, result
        mock_resolve.assert_called_once_with(
            mock_memory_manager.db, "#42", "11111111-1111-4111-8111-111111110001"
        )
        call_kwargs = mock_memory_manager.create_memory.call_args.kwargs
        assert call_kwargs["source_session_id"] == resolved_uuid

    @pytest.mark.asyncio
    async def test_create_memory_session_resolution_failure(
        self, memory_registry, mock_memory_manager
    ):
        """Test memory creation falls back to None when session resolution fails."""
        mock_memory_manager.search_memories.return_value = []

        with (
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value={"id": "11111111-1111-4111-8111-111111110001"},
            ),
            patch(
                "gobby.storage.session_resolution.resolve_session_reference",
                side_effect=ValueError("Session not found"),
            ),
        ):
            result = await memory_registry.call(
                "create_memory",
                {"content": "Test", "session_id": "#999", "rationale": _VALID_RATIONALE},
            )

        assert result["success"] is True
        call_kwargs = mock_memory_manager.create_memory.call_args.kwargs
        assert call_kwargs["source_session_id"] is None

    @pytest.mark.asyncio
    async def test_create_memory_falls_back_to_session_context(
        self, memory_registry: InternalToolRegistry, mock_memory_manager: MagicMock
    ) -> None:
        """Without an explicit session_id the seeded session context is the source."""
        mock_memory_manager.search_memories.return_value = []
        context_uuid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

        with (
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value={"id": "11111111-1111-4111-8111-111111110001"},
            ),
            patch(
                "gobby.storage.session_resolution.resolve_session_reference",
            ) as mock_resolve,
            patch(
                "gobby.mcp_proxy.tools.memory_write.derive_memory_create_provenance",
                return_value=(None, None),
            ) as mock_derive,
            session_context_for_test(context_uuid),
        ):
            result = await memory_registry.call(
                "create_memory", {"content": "Test", "rationale": _VALID_RATIONALE}
            )

        assert result["success"] is True, result
        mock_resolve.assert_not_called()
        call_kwargs = mock_memory_manager.create_memory.call_args.kwargs
        assert call_kwargs["source_session_id"] == context_uuid
        assert mock_derive.call_args.kwargs["resolved_session_id"] == context_uuid

    @pytest.mark.asyncio
    async def test_create_memory_explicit_session_id_beats_context(
        self, memory_registry: InternalToolRegistry, mock_memory_manager: MagicMock
    ) -> None:
        """An explicit session_id argument wins over the seeded session context."""
        mock_memory_manager.search_memories.return_value = []
        resolved_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        with (
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value={"id": "11111111-1111-4111-8111-111111110001"},
            ),
            patch(
                "gobby.storage.session_resolution.resolve_session_reference",
                return_value=resolved_uuid,
            ),
            patch(
                "gobby.mcp_proxy.tools.memory_write.derive_memory_create_provenance",
                return_value=(None, None),
            ),
            session_context_for_test("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        ):
            result = await memory_registry.call(
                "create_memory",
                {"content": "Test", "session_id": "#42", "rationale": _VALID_RATIONALE},
            )

        assert result["success"] is True, result
        call_kwargs = mock_memory_manager.create_memory.call_args.kwargs
        assert call_kwargs["source_session_id"] == resolved_uuid

    @pytest.mark.asyncio
    async def test_create_memory_without_session_context_stores_null(
        self, memory_registry: InternalToolRegistry, mock_memory_manager: MagicMock
    ) -> None:
        """No explicit session_id and no seeded context yields NULL without error."""
        mock_memory_manager.search_memories.return_value = []

        with (
            patch(
                "gobby.utils.project_context.get_project_context",
                return_value={"id": "11111111-1111-4111-8111-111111110001"},
            ),
            patch(
                "gobby.mcp_proxy.tools.memory_write.derive_memory_create_provenance",
                return_value=(None, None),
            ),
        ):
            result = await memory_registry.call(
                "create_memory", {"content": "Test", "rationale": _VALID_RATIONALE}
            )

        assert result["success"] is True, result
        call_kwargs = mock_memory_manager.create_memory.call_args.kwargs
        assert call_kwargs["source_session_id"] is None

    @pytest.mark.asyncio
    async def test_create_memory_receives_outer_call_tool_session_context(
        self, memory_registry: InternalToolRegistry, mock_memory_manager: MagicMock
    ) -> None:
        """The proxy's outer call_tool(session_id=...) seeds the session create_memory stores.

        create_memory declares ``session_id`` optional, so the proxy never injects the
        caller's ref into its arguments; the seeded SessionContext is the only carrier.
        Only the DB-backed ref resolver is stubbed; seeding, dispatch, and reset run for real.
        """
        from gobby.mcp_proxy.server import GobbyDaemonTools

        mock_memory_manager.search_memories.return_value = []
        seeded_uuid = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
        project_id = "11111111-1111-4111-8111-111111110001"
        mcp_manager = MagicMock()
        mcp_manager.project_id = project_id
        mcp_manager.connections = {}
        mcp_manager.health = {}
        mcp_manager.server_configs = []
        handler = GobbyDaemonTools(
            mcp_manager=mcp_manager,
            daemon_port=8787,
            websocket_port=8788,
            start_time=1000.0,
            internal_manager=MagicMock(),
            db=MagicMock(),
        )

        async def proxied_call(
            server_name: str, tool_name: str, arguments: dict[str, Any], *_args: Any, **_kw: Any
        ) -> Any:
            assert (server_name, tool_name) == ("gobby-memory", "create_memory")
            assert "session_id" not in arguments
            return await memory_registry.call(tool_name, arguments)

        with (
            patch(
                "gobby.utils.session_context._resolve_context_values",
                return_value=(
                    seeded_uuid,
                    project_id,
                    SessionContext(session_id=seeded_uuid),
                    {"id": project_id},
                ),
            ),
            patch(
                "gobby.mcp_proxy.tools.memory_write.derive_memory_create_provenance",
                return_value=(None, None),
            ) as mock_derive,
            patch.object(handler.tool_proxy, "call_tool", AsyncMock(side_effect=proxied_call)),
        ):
            result = await handler.call_tool(
                server_name="gobby-memory",
                tool_name="create_memory",
                arguments={"content": "Test", "rationale": _VALID_RATIONALE},
                session_id="#42",
            )

        assert not getattr(result, "is_error", False), result
        call_kwargs = mock_memory_manager.create_memory.call_args.kwargs
        assert call_kwargs["source_session_id"] == seeded_uuid
        assert mock_derive.call_args.kwargs["resolved_session_id"] == seeded_uuid

    @pytest.mark.asyncio
    async def test_create_memory_auto_supersedes_near_duplicate(
        self, memory_registry, mock_memory_manager
    ):
        """A >=0.9 raw-cosine match is superseded atomically and reported.

        The duplicate is old: its decayed ``similarity`` sits well under 0.9, and only
        the bare cosine says it is the same memory (#21010).
        """
        duplicate_id = "22222222-2222-4222-8222-222222220001"
        weaker_id = "22222222-2222-4222-8222-222222220002"
        third_id = "22222222-2222-4222-8222-222222220003"
        fourth_id = "22222222-2222-4222-8222-222222220004"
        fifth_duplicate_id = "22222222-2222-4222-8222-222222220005"
        mock_memory_manager.search_memories.return_value = [
            MockMemory(
                id=duplicate_id,
                content="Near duplicate",
                similarity=0.475,
                raw_semantic_score=0.95,
                temporal_decay_factor=0.5,
            ),
            MockMemory(
                id=weaker_id,
                content="Related",
                similarity=0.7,
                raw_semantic_score=0.7,
                temporal_decay_factor=1.0,
            ),
            MockMemory(id=third_id, content="Third", raw_semantic_score=0.8),
            MockMemory(id=fourth_id, content="Fourth", raw_semantic_score=0.75),
            MockMemory(
                id=fifth_duplicate_id,
                content="Fifth duplicate",
                raw_semantic_score=0.92,
            ),
        ]

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001"},
        ):
            result = await memory_registry.call(
                "create_memory",
                {"content": "Test content", "rationale": _VALID_RATIONALE},
            )

        assert result["success"] is True
        assert result["auto_superseded"] == [
            {"id": duplicate_id, "similarity": 0.95},
            {"id": fifth_duplicate_id, "similarity": 0.92},
        ]
        assert [entry["id"] for entry in result["similar_existing"]] == [
            duplicate_id,
            weaker_id,
            third_id,
            fourth_id,
            fifth_duplicate_id,
        ]
        # similar_existing reports the undecayed score, the axis search ranks on.
        assert result["similar_existing"][0]["similarity"] == pytest.approx(0.95)
        assert result["similar_existing"][0]["raw_semantic_score"] == 0.95
        call_kwargs = mock_memory_manager.create_memory.call_args.kwargs
        assert call_kwargs["supersedes"] == [duplicate_id, fifth_duplicate_id]
        probe_kwargs = mock_memory_manager.search_memories.call_args.kwargs
        assert probe_kwargs["limit"] == 5
        assert probe_kwargs["embed_text"] == f"Test content\n\nWhy: {_VALID_RATIONALE}"

    @pytest.mark.asyncio
    async def test_create_memory_below_threshold_is_not_superseded(
        self, memory_registry, mock_memory_manager
    ):
        """Matches below the 0.9 raw-cosine threshold are only reported.

        A boosted-and-fresh ``similarity`` above 0.9 does not count: the bare cosine
        is the duplicate test (#21010).
        """
        related_id = "22222222-2222-4222-8222-222222220003"
        mock_memory_manager.search_memories.return_value = [
            MockMemory(
                id=related_id,
                content="Related",
                similarity=0.95,
                raw_semantic_score=0.89,
                temporal_decay_factor=1.0,
            ),
        ]

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001"},
        ):
            result = await memory_registry.call(
                "create_memory",
                {"content": "Test content", "rationale": _VALID_RATIONALE},
            )

        assert result["success"] is True
        assert "auto_superseded" not in result
        call_kwargs = mock_memory_manager.create_memory.call_args.kwargs
        assert call_kwargs["supersedes"] == []

    @pytest.mark.asyncio
    async def test_create_memory_similar_search_failure_nonfatal(
        self, memory_registry, mock_memory_manager
    ):
        """Test that similarity search failure doesn't break memory creation."""
        mock_memory_manager.search_memories.side_effect = Exception("Search unavailable")

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001"},
        ):
            result = await memory_registry.call(
                "create_memory",
                {"content": "Test", "rationale": _VALID_RATIONALE},
            )

        assert result["success"] is True
        assert result["similar_existing"] == []

    @pytest.mark.asyncio
    async def test_create_memory_error(self, memory_registry, mock_memory_manager):
        """Test memory creation error handling."""
        mock_memory_manager.create_memory.side_effect = Exception("Database error")

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            result = await memory_registry.call(
                "create_memory",
                {"content": "Test", "rationale": _VALID_RATIONALE},
            )

        assert result["success"] is False
        assert "Database error" in result["error"]


class TestSearchMemories:
    """Tests for search_memories tool."""

    @pytest.mark.asyncio
    async def test_search_memories_success(self, memory_registry, mock_memory_manager):
        """Test successful memory search."""
        mock_memory_manager.search_memories.return_value = [
            MockMemory(
                id="21000000-0000-4000-8000-000000000005",
                content="Memory 1",
                similarity=0.95,
                search_via="semantic",
                ranking_score=0.91,
                raw_semantic_score=0.95,
                temporal_decay_factor=1.0,
                ranking_mode="semantic_only",
            ),
            MockMemory(id="m2", content="Memory 2", similarity=0.85),
        ]

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001"},
        ):
            result = await memory_registry.call(
                "search_memories", {"query": "test query", "limit": 5}
            )

        assert result["success"] is True
        assert len(result["memories"]) == 2
        assert result["memories"][0]["similarity"] == 0.95
        assert result["memories"][0]["ranking_score"] == 0.91
        assert result["memories"][0]["search_via"] == "semantic"
        assert result["memories"][0]["ranking_mode"] == "semantic_only"
        call_kwargs = mock_memory_manager.search_memories.call_args.kwargs
        assert call_kwargs["limit"] == 5
        assert call_kwargs["min_score"] is None

    @pytest.mark.asyncio
    async def test_search_memories_with_filters(self, memory_registry, mock_memory_manager):
        """Test search with tag filters."""
        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001"},
        ):
            result = await memory_registry.call(
                "search_memories",
                {
                    "query": "test",
                    "memory_type": "pattern",
                    "tags_all": ["important"],
                    "tags_any": ["work", "personal"],
                    "tags_none": ["archived"],
                },
            )

        assert result["success"] is True
        call_kwargs = mock_memory_manager.search_memories.call_args.kwargs
        assert call_kwargs["memory_type"] == "pattern"
        assert call_kwargs["tags_all"] == ["important"]
        assert call_kwargs["tags_any"] == ["work", "personal"]
        assert call_kwargs["tags_none"] == ["archived"]

    @pytest.mark.asyncio
    async def test_search_memories_rejects_noncanonical_type(
        self,
        memory_registry,
        mock_memory_manager,
    ) -> None:
        result = await memory_registry.call(
            "search_memories",
            {"query": "test", "memory_type": "debugging_pattern"},
        )

        assert result["success"] is False
        assert "Invalid memory_type 'debugging_pattern'" in result["error"]
        mock_memory_manager.search_memories.assert_not_called()

    @pytest.mark.asyncio
    async def test_search_memories_does_not_apply_default_threshold(
        self, memory_registry, mock_memory_manager
    ):
        """Manual search applies no implicit similarity floor."""
        mock_memory_manager.search_memories.return_value = [
            MockMemory(
                id="21000000-0000-4000-8000-000000000005", content="Memory 1", similarity=0.65
            ),
        ]

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001"},
        ):
            result = await memory_registry.call(
                "search_memories", {"query": "test query", "limit": 5}
            )

        assert result["success"] is True
        assert [mem["id"] for mem in result["memories"]] == ["21000000-0000-4000-8000-000000000005"]
        call_kwargs = mock_memory_manager.search_memories.call_args.kwargs
        assert call_kwargs["limit"] == 5
        assert call_kwargs["min_score"] is None

    @pytest.mark.asyncio
    async def test_search_memories_with_explicit_min_score_filters_results(
        self, memory_registry, mock_memory_manager
    ):
        """Explicit min_score gates on the undecayed axis and travels to the service.

        Memory 1 is old: decayed 0.65 is under the 0.6 floor by a hair only because
        of age, and its undecayed 0.8 clears it. Memory 2 is fresh and genuinely weak:
        decayed 0.55 undecays to 0.579 and stays out (#21010).
        """
        mock_memory_manager.search_memories.return_value = [
            MockMemory(
                id="21000000-0000-4000-8000-000000000005",
                content="Memory 1",
                similarity=0.65,
                ranking_score=0.08,
                raw_semantic_score=0.8,
                temporal_decay_factor=0.8125,
                ranking_mode="rrf",
            ),
            MockMemory(
                id="m2",
                content="Memory 2",
                similarity=0.55,
                ranking_score=0.12,
                raw_semantic_score=0.55,
                temporal_decay_factor=0.95,
                ranking_mode="rrf",
            ),
        ]

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001"},
        ):
            result = await memory_registry.call(
                "search_memories", {"query": "test query", "limit": 2, "min_score": 0.6}
            )

        assert result["success"] is True
        assert [mem["id"] for mem in result["memories"]] == ["21000000-0000-4000-8000-000000000005"]
        assert result["memories"][0]["undecayed_similarity"] == pytest.approx(0.8)
        call_kwargs = mock_memory_manager.search_memories.call_args.kwargs
        assert call_kwargs["limit"] == 2
        assert call_kwargs["min_score"] == 0.6
        assert result["diagnostics"] == {
            "candidates_considered": 2,
            "returned": 1,
            "threshold": 0.6,
            "threshold_axis": "undecayed_similarity",
            "score_range": [pytest.approx(0.5789, abs=1e-4), pytest.approx(0.8)],
        }

    @pytest.mark.asyncio
    async def test_search_memories_error(self, memory_registry, mock_memory_manager):
        """Test search error handling."""
        mock_memory_manager.search_memories.side_effect = Exception("Search error")

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            result = await memory_registry.call("search_memories", {"query": "test"})

        assert result["success"] is False
        assert "Search error" in result["error"]


class TestDeleteMemory:
    """Tests for delete_memory tool."""

    @pytest.mark.asyncio
    async def test_delete_memory_success(self, memory_registry, mock_memory_manager):
        """Test successful memory deletion."""
        with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "project-a", "name": "Project A"}
            result = await memory_registry.call("delete_memory", {"memory_id": "mem-123"})

        assert result == {"success": True}  # Success response
        mock_memory_manager.delete_memory_scoped.assert_awaited_once_with("mem-123", "project-a")

    @pytest.mark.asyncio
    async def test_delete_memory_not_found(self, memory_registry, mock_memory_manager):
        """Test deletion when memory not found."""
        mock_memory_manager.delete_memory_scoped.return_value = False

        result = await memory_registry.call("delete_memory", {"memory_id": "nonexistent"})

        assert "error" in result
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_delete_memory_error(self, memory_registry, mock_memory_manager):
        """Test deletion error handling."""
        mock_memory_manager.delete_memory_scoped.side_effect = Exception("Delete error")

        result = await memory_registry.call("delete_memory", {"memory_id": "mem-123"})

        assert "error" in result
        assert "Delete error" in result["error"]


class TestRestoreMemory:
    """Tests for restore_memory tool."""

    @pytest.mark.asyncio
    async def test_restore_memory_success(self, memory_registry, mock_memory_manager) -> None:
        """Restoring an existing memory returns success and calls storage."""
        mock_memory_manager.restore_memory = MagicMock(return_value=True)
        mock_memory_manager.restore_memory_indices = AsyncMock(return_value=None)

        result = await memory_registry.call("restore_memory", {"memory_id": "mem-123"})

        assert result == {"success": True}
        mock_memory_manager.restore_memory.assert_called_once_with("mem-123")
        mock_memory_manager.restore_memory_indices.assert_awaited_once_with(
            "mem-123", "Test memory content", PERSONAL_PROJECT_ID, False, "fact"
        )

    @pytest.mark.asyncio
    async def test_restore_memory_not_found(self, memory_registry, mock_memory_manager) -> None:
        """A missing memory raises ValueError and is reported as an error."""
        mock_memory_manager.restore_memory = MagicMock(
            side_effect=ValueError("Memory nope not found")
        )

        result = await memory_registry.call("restore_memory", {"memory_id": "nope"})

        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_restore_memory_error(self, memory_registry, mock_memory_manager) -> None:
        """Unexpected storage failures are surfaced as errors, not raised."""
        mock_memory_manager.restore_memory = MagicMock(side_effect=Exception("Restore error"))

        result = await memory_registry.call("restore_memory", {"memory_id": "mem-123"})

        assert result["success"] is False
        assert "Restore error" in result["error"]


class TestPromoteMemoryToGlobal:
    """Tests for promote_memory_to_global tool."""

    @pytest.mark.asyncio
    async def test_promote_memory_to_global_success(
        self,
        memory_registry,
        mock_memory_manager,
    ) -> None:
        promoted = MockMemory(id="mem-123", is_global=True)
        mock_memory_manager.promote_memory.return_value = promoted

        result = await memory_registry.call(
            "promote_memory_to_global",
            {"memory_id": "mem-123"},
        )

        assert result == {
            "success": True,
            "memory": {
                "id": "mem-123",
                "project_id": PERSONAL_PROJECT_ID,
                "is_global": True,
                "updated_at": promoted.updated_at,
            },
        }
        mock_memory_manager.promote_memory.assert_awaited_once_with("mem-123")

    @pytest.mark.asyncio
    async def test_promote_memory_to_global_has_no_target_argument(
        self,
        memory_registry,
        mock_memory_manager,
    ) -> None:
        with pytest.raises(ValueError, match="Unknown argument.*target_project_id"):
            await memory_registry.call(
                "promote_memory_to_global",
                {
                    "memory_id": "mem-123",
                    "target_project_id": "11111111-1111-4111-8111-111111110002",
                },
            )

        mock_memory_manager.promote_memory.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_promote_global_memory_requires_owner_project(
        self,
        memory_registry: InternalToolRegistry,
        mock_memory_manager: MagicMock,
    ) -> None:
        mock_memory_manager.get_memory.return_value = MockMemory(
            project_id="owner-project",
            is_global=True,
        )

        with patch("gobby.utils.project_context.get_project_context") as mock_context:
            mock_context.return_value = {"id": "current-project"}
            result = await memory_registry.call(
                "promote_memory_to_global",
                {"memory_id": "mem-123"},
            )

        assert result == {"success": False, "error": "Memory mem-123 not found"}
        mock_memory_manager.promote_memory.assert_not_awaited()


class TestListMemories:
    """Tests for list_memories tool."""

    @pytest.mark.asyncio
    async def test_list_memories_success(self, memory_registry, mock_memory_manager):
        """Test successful memory listing."""
        mock_memory_manager.list_memories.return_value = [
            MockMemory(id="21000000-0000-4000-8000-000000000005"),
            MockMemory(id="m2"),
            MockMemory(id="m3"),
        ]

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001"},
        ):
            result = await memory_registry.call("list_memories", {})

        assert result["success"] is True
        assert result["count"] == 3
        assert len(result["memories"]) == 3

    @pytest.mark.asyncio
    async def test_list_memories_with_filters(self, memory_registry, mock_memory_manager):
        """Test listing with filters."""
        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001"},
        ):
            result = await memory_registry.call(
                "list_memories",
                {
                    "memory_type": "fact",
                    "limit": 20,
                    "tags_all": ["work"],
                },
            )

        assert result["success"] is True
        call_kwargs = mock_memory_manager.list_memories.call_args.kwargs
        assert call_kwargs["memory_type"] == "fact"
        assert call_kwargs["limit"] == 20
        assert call_kwargs["tags_all"] == ["work"]

    @pytest.mark.asyncio
    async def test_list_memories_error(self, memory_registry, mock_memory_manager):
        """Test list error handling."""
        mock_memory_manager.list_memories.side_effect = Exception("List error")

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            result = await memory_registry.call("list_memories", {})

        assert result["success"] is False
        assert "List error" in result["error"]


class TestGetMemory:
    """Tests for get_memory tool."""

    @pytest.mark.asyncio
    async def test_get_memory_success(self, memory_registry, mock_memory_manager):
        """Test successful memory retrieval."""
        mock_memory_manager.get_memory.return_value = MockMemory(
            id="mem-123",
            content="Test content",
            memory_type="fact",
            project_id="11111111-1111-4111-8111-111111110001",
            access_count=5,
            tags=["tag1"],
        )

        result = await memory_registry.call("get_memory", {"memory_id": "mem-123"})

        assert result["success"] is True
        assert result["memory"]["id"] == "mem-123"
        assert result["memory"]["content"] == "Test content"
        assert result["memory"]["access_count"] == 5
        assert result["memory"]["tags"] == ["tag1"]

    @pytest.mark.asyncio
    async def test_get_memory_not_found(self, memory_registry, mock_memory_manager):
        """Test retrieval when memory not found."""
        mock_memory_manager.get_memory.return_value = None

        result = await memory_registry.call("get_memory", {"memory_id": "nonexistent"})

        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_get_memory_value_error(self, memory_registry, mock_memory_manager):
        """Test retrieval with ValueError."""
        mock_memory_manager.get_memory.side_effect = ValueError("Invalid ID format")

        result = await memory_registry.call("get_memory", {"memory_id": "invalid"})

        assert result["success"] is False
        assert "Invalid ID format" in result["error"]

    @pytest.mark.asyncio
    async def test_get_memory_error(self, memory_registry, mock_memory_manager):
        """Test retrieval error handling."""
        mock_memory_manager.get_memory.side_effect = Exception("Get error")

        result = await memory_registry.call("get_memory", {"memory_id": "mem-123"})

        assert result["success"] is False
        assert "Get error" in result["error"]


_FULL_ID = "c12fce9e-11c0-5112-8f03-fbf794031f6f"
_SIBLING_ID = "c12fce9e-0000-5000-8000-000000000000"
_PROJECT_ID = "11111111-1111-4111-8111-111111110001"

# (tool, extra arguments, downstream manager method, positional index or kwarg name
# of the memory id that method receives)
_PREFIX_REF_CASES: list[tuple[str, dict[str, Any], str, int | str]] = [
    ("update_memory", {"tags": ["baseline-419"]}, "update_memory_scoped", "memory_id"),
    ("delete_memory", {}, "delete_memory_scoped", 0),
    ("restore_memory", {}, "restore_memory", 0),
    ("promote_memory_to_global", {}, "promote_memory", 0),
    ("demote_memory_from_global", {}, "demote_memory", 0),
    ("move_memory", {"new_project_id": _PROJECT_ID}, "move_memory", 0),
    ("get_related_memories", {}, "get_related", "memory_id"),
]


class TestMemoryRefResolution:
    """Every memory_id argument goes through MemoryManager.resolve_memory_id first."""

    @pytest.mark.asyncio
    async def test_get_memory_resolves_unique_prefix(
        self, memory_registry: InternalToolRegistry, mock_memory_manager: MagicMock
    ) -> None:
        mock_memory_manager.resolve_memory_id.side_effect = None
        mock_memory_manager.resolve_memory_id.return_value = _FULL_ID
        mock_memory_manager.get_memory.return_value = MockMemory(id=_FULL_ID)

        with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": _PROJECT_ID, "name": "Project"}
            result = await memory_registry.call("get_memory", {"memory_id": "c12fce9e"})

        assert result["success"] is True
        assert result["memory"]["id"] == _FULL_ID
        mock_memory_manager.resolve_memory_id.assert_called_once_with(
            "c12fce9e", project_id=_PROJECT_ID
        )
        mock_memory_manager.get_memory.assert_called_once_with(_FULL_ID, project_id=_PROJECT_ID)

    @pytest.mark.asyncio
    async def test_get_memory_rejects_ambiguous_prefix(
        self, memory_registry: InternalToolRegistry, mock_memory_manager: MagicMock
    ) -> None:
        from gobby.memory.facade import AmbiguousMemoryReferenceError

        mock_memory_manager.resolve_memory_id.side_effect = AmbiguousMemoryReferenceError(
            "c12f", [_FULL_ID, _SIBLING_ID]
        )

        result = await memory_registry.call("get_memory", {"memory_id": "c12f"})

        assert result["success"] is False
        assert _FULL_ID in result["error"]
        assert _SIBLING_ID in result["error"]
        mock_memory_manager.get_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_memory_malformed_ref_is_not_found(
        self, memory_registry: InternalToolRegistry, mock_memory_manager: MagicMock
    ) -> None:
        mock_memory_manager.resolve_memory_id.side_effect = None
        mock_memory_manager.resolve_memory_id.return_value = None

        result = await memory_registry.call("get_memory", {"memory_id": "not-a-uuid"})

        assert result == {"success": False, "error": "Memory not-a-uuid not found"}
        assert "invalid input syntax" not in result["error"]
        # The raw ref never reaches a repository lookup.
        mock_memory_manager.get_memory.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool", "extra_args", "downstream", "id_position"),
        _PREFIX_REF_CASES,
        ids=[case[0] for case in _PREFIX_REF_CASES],
    )
    async def test_write_tools_resolve_prefix_refs(
        self,
        memory_registry: InternalToolRegistry,
        mock_memory_manager: MagicMock,
        tool: str,
        extra_args: dict[str, Any],
        downstream: str,
        id_position: int | str,
    ) -> None:
        mock_memory_manager.resolve_memory_id.side_effect = None
        mock_memory_manager.resolve_memory_id.return_value = _FULL_ID
        mock_memory_manager.get_memory.return_value = MockMemory(id=_FULL_ID)
        mock_memory_manager.restore_memory = MagicMock(return_value=True)
        mock_memory_manager.restore_memory_indices = AsyncMock(return_value=None)
        mock_memory_manager.demote_memory = AsyncMock(return_value=MagicMock())
        mock_memory_manager.move_memory = AsyncMock(return_value=MagicMock())

        result = await memory_registry.call(tool, {"memory_id": "c12fce9e", **extra_args})

        assert result["success"] is True, result
        mock_memory_manager.resolve_memory_id.assert_called_once()
        assert mock_memory_manager.resolve_memory_id.call_args.args[0] == "c12fce9e"
        call = getattr(mock_memory_manager, downstream).call_args
        received = (
            call.kwargs[id_position] if isinstance(id_position, str) else call.args[id_position]
        )
        assert received == _FULL_ID


class TestGetRelatedMemories:
    """Tests for get_related_memories tool."""

    @pytest.mark.asyncio
    async def test_get_related_memories_success(self, memory_registry, mock_memory_manager):
        """Test successful related memories retrieval."""
        mock_memory_manager.get_related.return_value = [
            MockMemory(id="related-1", similarity=0.84),
            MockMemory(id="related-2", similarity=0.63),
        ]

        with patch("gobby.mcp_proxy.tools.memory.get_current_project_id", return_value=None):
            result = await memory_registry.call(
                "get_related_memories",
                {"memory_id": "mem-123", "limit": 5, "min_similarity": 0.3},
            )

        assert result["success"] is True
        assert result["memory_id"] == "mem-123"
        assert result["count"] == 2
        assert [entry["similarity"] for entry in result["related"]] == [0.84, 0.63]
        mock_memory_manager.get_related.assert_called_once_with(
            memory_id="mem-123",
            limit=5,
            min_similarity=0.3,
            project_id=None,
        )

    @pytest.mark.asyncio
    async def test_get_related_memories_value_error(self, memory_registry, mock_memory_manager):
        """Test related memories with ValueError."""
        mock_memory_manager.get_related.side_effect = ValueError("Memory not found")

        result = await memory_registry.call("get_related_memories", {"memory_id": "nonexistent"})

        assert result["success"] is False
        assert "Memory not found" in result["error"]

    @pytest.mark.asyncio
    async def test_get_related_memories_error(self, memory_registry, mock_memory_manager):
        """Test related memories error handling."""
        mock_memory_manager.get_related.side_effect = Exception("Related error")

        result = await memory_registry.call("get_related_memories", {"memory_id": "mem-123"})

        assert result["success"] is False
        assert "Related error" in result["error"]


class TestUpdateMemory:
    """Tests for update_memory tool."""

    @pytest.mark.asyncio
    async def test_update_memory_success(self, memory_registry, mock_memory_manager):
        """Test successful memory update."""
        updated_memory = MockMemory(id="mem-123", updated_at="2024-01-02T00:00:00")
        mock_memory_manager.update_memory_scoped.return_value = updated_memory

        with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "project-a", "name": "Project A"}
            result = await memory_registry.call(
                "update_memory",
                {
                    "memory_id": "mem-123",
                    "content": "Updated content",
                    "tags": ["new-tag"],
                    "rationale": _VALID_RATIONALE,
                    "memory_type": "pattern",
                },
            )

        assert result["success"] is True
        assert result["memory"]["id"] == "mem-123"
        mock_memory_manager.update_memory_scoped.assert_awaited_once_with(
            memory_id="mem-123",
            project_id="project-a",
            content="Updated content",
            tags=["new-tag"],
            memory_type=MemoryType.PATTERN,
            rationale=_VALID_RATIONALE,
        )

    @pytest.mark.asyncio
    async def test_update_memory_partial(self, memory_registry, mock_memory_manager):
        """Test partial memory update."""
        mock_memory_manager.update_memory_scoped.return_value = MockMemory()

        with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "project-a", "name": "Project A"}
            result = await memory_registry.call(
                "update_memory", {"memory_id": "mem-123", "tags": ["updated"]}
            )

        assert result["success"] is True
        mock_memory_manager.update_memory_scoped.assert_awaited_once_with(
            memory_id="mem-123",
            project_id="project-a",
            content=None,
            tags=["updated"],
            memory_type=None,
            rationale=None,
        )

    @pytest.mark.asyncio
    async def test_update_memory_value_error(self, memory_registry, mock_memory_manager):
        """Test update with ValueError."""
        mock_memory_manager.update_memory_scoped.side_effect = ValueError("Memory not found")

        result = await memory_registry.call(
            "update_memory",
            {"memory_id": "nonexistent", "content": "New", "rationale": _VALID_RATIONALE},
        )

        assert result["success"] is False
        assert "Memory not found" in result["error"]

    @pytest.mark.asyncio
    async def test_update_memory_error(self, memory_registry, mock_memory_manager):
        """Test update error handling."""
        mock_memory_manager.update_memory_scoped.side_effect = Exception("Update error")

        result = await memory_registry.call(
            "update_memory",
            {"memory_id": "mem-123", "content": "New", "rationale": _VALID_RATIONALE},
        )

        assert result["success"] is False
        assert "Update error" in result["error"]


class TestMemoryStats:
    """Tests for memory_stats tool."""

    @pytest.mark.asyncio
    async def test_memory_stats_success(self, memory_registry, mock_memory_manager):
        """Test successful stats retrieval."""
        mock_memory_manager.get_stats.return_value = {
            "total": 100,
            "by_type": {"fact": 60, "preference": 40},
        }

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001"},
        ):
            result = await memory_registry.call("memory_stats", {})

        assert "stats" in result
        assert result["stats"]["total"] == 100
        mock_memory_manager.get_stats.assert_awaited_once_with(
            project_id="11111111-1111-4111-8111-111111110001"
        )

    @pytest.mark.asyncio
    async def test_memory_stats_error(self, memory_registry, mock_memory_manager):
        """Test stats error handling."""
        mock_memory_manager.get_stats.side_effect = Exception("Stats error")

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            result = await memory_registry.call("memory_stats", {})

        assert "error" in result
        assert "Stats error" in result["error"]


class TestRegistryCreation:
    """Tests for create_memory_registry function."""

    def test_creates_registry(self, mock_memory_manager) -> None:
        """Test registry is created with correct name."""
        registry = create_memory_registry(lambda: mock_memory_manager)

        assert registry.name == "gobby-memory"
        assert "memory management" in registry.description.lower()

    def test_all_tools_registered(self, mock_memory_manager) -> None:
        """Test all expected tools are registered."""
        registry = create_memory_registry(lambda: mock_memory_manager)

        expected_tools = [
            "create_memory",
            "search_memories",
            "delete_memory",
            "restore_memory",
            "promote_memory_to_global",
            "list_memories",
            "get_memory",
            "get_related_memories",
            "update_memory",
            "memory_stats",
            "search_knowledge_graph",
        ]

        # Get available tools from registry
        tools = registry.list_tools()
        # Handle both object and dict formats
        tool_names = [t["name"] if isinstance(t, dict) else t.name for t in tools]

        for tool_name in expected_tools:
            assert tool_name in tool_names, f"Tool {tool_name} not found"

        assert "remember_with_image" not in tool_names
        assert "remember_screenshot" not in tool_names

    def test_backup_tool_descriptions_name_machine_local_path(
        self,
        mock_memory_manager: MagicMock,
    ) -> None:
        registry = create_memory_registry(lambda: mock_memory_manager)

        expected_path = "~/.gobby/backups/<project-uuid>/memories.jsonl"
        for tool_name in ("backup_memories", "restore_memories"):
            tool = registry.get_tool_metadata(tool_name)
            assert tool is not None
            assert expected_path in tool.description

    def test_registry_with_llm_service(self, mock_memory_manager) -> None:
        """Test registry creation with LLM service (optional parameter)."""
        mock_llm = MagicMock()
        registry = create_memory_registry(
            lambda: mock_memory_manager, llm_service_resolver=lambda: mock_llm
        )

        # Should still work even though llm_service isn't used in current implementation
        assert registry is not None
        assert len(registry.list_tools()) > 0


class TestSearchMemoriesToolRegistration:
    """Tests for search_memories tool registration."""

    @pytest.mark.asyncio
    async def test_search_memories_tool_exists(
        self,
        memory_registry: InternalToolRegistry,
    ) -> None:
        """Test that search_memories tool is registered."""
        tools = memory_registry.list_tools()
        tool_names = [t["name"] if isinstance(t, dict) else t.name for t in tools]
        assert "search_memories" in tool_names


class TestSearchMemoriesResultShape:
    """Every hit carries provenance for the agent's judgment (#21010)."""

    @pytest.mark.asyncio
    async def test_hits_carry_rationale_provenance_and_collapsed_duplicates(
        self, memory_registry: InternalToolRegistry, mock_memory_manager: MagicMock
    ) -> None:
        hit = MockMemory(
            id="21000000-0000-4000-8000-000000000007",
            content="Memory 1",
            updated_at="2024-02-01T00:00:00",
            similarity=0.72,
            raw_semantic_score=0.8,
            temporal_decay_factor=0.9,
        )
        hit.rationale = "Why a future session needs this."
        hit.source_task_id = "31000000-0000-4000-8000-000000000001"
        hit.created_by_agent = "analyst"
        hit.graph_confidence = None
        hit.collapsed_duplicates = ["21000000-0000-4000-8000-000000000008"]
        mock_memory_manager.search_memories.return_value = [hit]

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001"},
        ):
            result = await memory_registry.call("search_memories", {"query": "q"})

        assert result["success"] is True
        memory = result["memories"][0]
        assert memory["rationale"] == "Why a future session needs this."
        assert memory["updated_at"] == "2024-02-01T00:00:00"
        assert memory["source_task_id"] == "31000000-0000-4000-8000-000000000001"
        assert memory["created_by_agent"] == "analyst"
        assert memory["graph_confidence"] is None
        assert memory["collapsed_duplicates"] == ["21000000-0000-4000-8000-000000000008"]
        assert memory["undecayed_similarity"] == pytest.approx(0.8)
        assert mock_memory_manager.search_memories.call_args.kwargs["min_score"] is None

    @pytest.mark.asyncio
    async def test_diagnostics_are_emitted_without_a_threshold(
        self, memory_registry: InternalToolRegistry, mock_memory_manager: MagicMock
    ) -> None:
        mock_memory_manager.search_memories.return_value = [
            MockMemory(id="a", similarity=0.7, temporal_decay_factor=1.0),
            MockMemory(id="b", similarity=0.5, temporal_decay_factor=0.5),
            MockMemory(id="c"),
        ]

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001"},
        ):
            result = await memory_registry.call("search_memories", {"query": "q"})

        assert [mem["id"] for mem in result["memories"]] == ["a", "b", "c"]
        assert result["diagnostics"] == {
            "candidates_considered": 3,
            "returned": 3,
            "threshold": 0.0,
            "threshold_axis": "undecayed_similarity",
            "score_range": [pytest.approx(0.7), pytest.approx(1.0)],
        }

    @pytest.mark.asyncio
    async def test_diagnostics_score_range_is_none_without_scored_hits(
        self, memory_registry: InternalToolRegistry, mock_memory_manager: MagicMock
    ) -> None:
        mock_memory_manager.search_memories.return_value = []

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001"},
        ):
            result = await memory_registry.call("search_memories", {"query": "q"})

        assert result["memories"] == []
        assert result["diagnostics"]["candidates_considered"] == 0
        assert result["diagnostics"]["score_range"] is None


class TestListAndGetMemoryShape:
    @pytest.mark.asyncio
    async def test_list_memories_returns_rationale_updated_at_and_source_task(
        self, memory_registry: InternalToolRegistry, mock_memory_manager: MagicMock
    ) -> None:
        row = MockMemory(id="mem-9", updated_at="2024-03-01T00:00:00")
        row.rationale = "Durable claim."
        row.source_task_id = "31000000-0000-4000-8000-000000000002"
        mock_memory_manager.list_memories.return_value = [row]

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001"},
        ):
            result = await memory_registry.call("list_memories", {})

        listed = result["memories"][0]
        assert listed["rationale"] == "Durable claim."
        assert listed["updated_at"] == "2024-03-01T00:00:00"
        assert listed["source_task_id"] == "31000000-0000-4000-8000-000000000002"

    @pytest.mark.asyncio
    async def test_get_memory_returns_rationale_and_provenance(
        self, memory_registry: InternalToolRegistry, mock_memory_manager: MagicMock
    ) -> None:
        row = MockMemory(id="mem-9")
        row.rationale = "Durable claim."
        row.source_task_id = "31000000-0000-4000-8000-000000000002"
        row.created_by_agent = "curator"
        mock_memory_manager.get_memory.return_value = row

        with patch(
            "gobby.utils.project_context.get_project_context",
            return_value={"id": "11111111-1111-4111-8111-111111110001"},
        ):
            result = await memory_registry.call("get_memory", {"memory_id": "mem-9"})

        memory = result["memory"]
        assert memory["rationale"] == "Durable claim."
        assert memory["source_task_id"] == "31000000-0000-4000-8000-000000000002"
        assert memory["created_by_agent"] == "curator"


class TestUpdateMemoryRationale:
    """A content change re-argues the durable-value claim (#21010)."""

    @pytest.mark.asyncio
    async def test_content_change_without_rationale_is_rejected(
        self, memory_registry: InternalToolRegistry, mock_memory_manager: MagicMock
    ) -> None:
        result = await memory_registry.call(
            "update_memory", {"memory_id": "mem-123", "content": "New body"}
        )

        assert result["success"] is False
        assert result["error"].startswith("rationale_required")
        mock_memory_manager.update_memory_scoped.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_overlong_rationale_is_rejected(
        self, memory_registry: InternalToolRegistry, mock_memory_manager: MagicMock
    ) -> None:
        result = await memory_registry.call(
            "update_memory", {"memory_id": "mem-123", "rationale": "x" * 501}
        )

        assert result["success"] is False
        assert result["error"].startswith("rationale_required")
        mock_memory_manager.update_memory_scoped.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rationale_only_update_passes_through(
        self, memory_registry: InternalToolRegistry, mock_memory_manager: MagicMock
    ) -> None:
        updated = MockMemory(id="mem-123")
        updated.rationale = _VALID_RATIONALE
        mock_memory_manager.update_memory_scoped.return_value = updated

        with patch("gobby.utils.project_context.get_project_context") as mock_ctx:
            mock_ctx.return_value = {"id": "project-a", "name": "Project A"}
            result = await memory_registry.call(
                "update_memory", {"memory_id": "mem-123", "rationale": _VALID_RATIONALE}
            )

        assert result["success"] is True
        assert result["memory"]["rationale"] == _VALID_RATIONALE
        mock_memory_manager.update_memory_scoped.assert_awaited_once_with(
            memory_id="mem-123",
            project_id="project-a",
            content=None,
            tags=None,
            memory_type=None,
            rationale=_VALID_RATIONALE,
        )

    @pytest.mark.asyncio
    async def test_noncanonical_memory_type_is_rejected(
        self, memory_registry: InternalToolRegistry, mock_memory_manager: MagicMock
    ) -> None:
        result = await memory_registry.call(
            "update_memory", {"memory_id": "mem-123", "memory_type": "implementation_note"}
        )

        assert result["success"] is False
        assert "Invalid memory_type" in result["error"]
        mock_memory_manager.update_memory_scoped.assert_not_awaited()


class TestSearchMemoriesDeliveryOutcomes:
    """The tool result is the delivery point of the search cohort (#21011, contract §5.1)."""

    _PROJECT = {"id": "11111111-1111-4111-8111-111111110001"}

    @pytest.mark.asyncio
    async def test_returned_hits_record_injected_outcomes_at_list_position(
        self, memory_registry: InternalToolRegistry, mock_memory_manager: MagicMock
    ) -> None:
        mock_memory_manager.search_memories.return_value = [
            MockMemory(id="m1", similarity=0.9, temporal_decay_factor=1.0),
            MockMemory(id="m2", similarity=0.4, temporal_decay_factor=1.0),
            MockMemory(id="m3", similarity=0.8, temporal_decay_factor=1.0),
        ]
        recorded: list[list[dict[str, Any]]] = []
        mock_memory_manager.injection_outcome_recorder = recorded.append

        with (
            patch("gobby.utils.project_context.get_project_context", return_value=self._PROJECT),
            patch("gobby.utils.session_context.get_current_session_id", return_value="sess-1"),
        ):
            result = await memory_registry.call(
                "search_memories", {"query": "dispatch", "min_score": 0.5}
            )

        assert [hit["id"] for hit in result["memories"]] == ["m1", "m3"]
        assert recorded == [
            [
                {
                    "session_id": "sess-1",
                    "recall_request_id": result["recall_request_id"],
                    "memory_id": memory_id,
                    "project_id": result["project_id"],
                    "outcome": "injected",
                    "injection_position": position,
                    "caller": "mcp_proxy.memory.search_memories",
                }
                for position, memory_id in enumerate(["m1", "m3"])
            ]
        ]

    @pytest.mark.asyncio
    async def test_no_outcome_rows_without_a_session_or_a_recorder(
        self, memory_registry: InternalToolRegistry, mock_memory_manager: MagicMock
    ) -> None:
        recorded: list[list[dict[str, Any]]] = []
        mock_memory_manager.injection_outcome_recorder = recorded.append

        with (
            patch("gobby.utils.project_context.get_project_context", return_value=self._PROJECT),
            patch("gobby.utils.session_context.get_current_session_id", return_value=None),
        ):
            sessionless = await memory_registry.call("search_memories", {"query": "dispatch"})

        assert sessionless["success"] is True
        assert sessionless["memories"]
        assert recorded == []

        mock_memory_manager.injection_outcome_recorder = None
        with (
            patch("gobby.utils.project_context.get_project_context", return_value=self._PROJECT),
            patch("gobby.utils.session_context.get_current_session_id", return_value="sess-1"),
        ):
            hub_off = await memory_registry.call("search_memories", {"query": "dispatch"})

        assert hub_off["success"] is True
        assert hub_off["memories"]
