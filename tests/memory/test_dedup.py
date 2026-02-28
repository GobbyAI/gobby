"""Tests for DedupService."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.memory.services.dedup import Action, DedupResult, DedupService

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_llm_provider():
    """Mock LLM provider with generate_json."""
    provider = MagicMock()
    provider.generate_json = AsyncMock()
    return provider


@pytest.fixture
def mock_vector_store():
    """Mock VectorStore."""
    store = MagicMock()
    store.search = AsyncMock(return_value=[])
    store.upsert = AsyncMock()
    store.delete = AsyncMock()
    return store


@pytest.fixture
def mock_storage():
    """Mock LocalMemoryManager (SQLite storage)."""
    storage = MagicMock()
    storage.get_memory = MagicMock(return_value=None)
    return storage


@pytest.fixture
def mock_embed_fn():
    """Mock embedding function."""
    fn = AsyncMock(return_value=[0.1] * 1536)
    return fn


@pytest.fixture
def mock_prompt_loader():
    """Mock PromptLoader."""
    loader = MagicMock()
    loader.render = MagicMock(return_value="rendered prompt")
    return loader


@pytest.fixture
def dedup_service(
    mock_llm_provider, mock_vector_store, mock_storage, mock_embed_fn, mock_prompt_loader
):
    """Create DedupService with all mocks."""
    return DedupService(
        llm_provider=mock_llm_provider,
        vector_store=mock_vector_store,
        storage=mock_storage,
        embed_fn=mock_embed_fn,
        prompt_loader=mock_prompt_loader,
    )


class TestAction:
    """Tests for Action dataclass."""

    def test_add_action(self) -> None:
        action = Action(event="ADD", text="New fact")
        assert action.event == "ADD"
        assert action.text == "New fact"
        assert action.memory_id is None

    def test_update_action_with_id(self) -> None:
        action = Action(event="UPDATE", text="Updated fact", memory_id="mem-123")
        assert action.event == "UPDATE"
        assert action.memory_id == "mem-123"

    def test_delete_action_with_id(self) -> None:
        action = Action(event="DELETE", text="Obsolete", memory_id="mem-456")
        assert action.event == "DELETE"
        assert action.memory_id == "mem-456"

    def test_noop_action(self) -> None:
        action = Action(event="NOOP", text="Already exists")
        assert action.event == "NOOP"


class TestDedupResult:
    """Tests for DedupResult dataclass."""

    def test_empty_result(self) -> None:
        result = DedupResult()
        assert result.added == []
        assert result.updated == []
        assert result.deleted == []

    def test_result_with_data(self) -> None:
        mock_mem = MagicMock()
        result = DedupResult(
            added=[mock_mem],
            updated=[mock_mem],
            deleted=["mem-123"],
        )
        assert len(result.added) == 1
        assert len(result.deleted) == 1


class TestExtractFacts:
    """Tests for DedupService._extract_facts()."""

    @pytest.mark.asyncio
    async def test_extract_facts_calls_llm(self, dedup_service, mock_llm_provider) -> None:
        """_extract_facts calls LLM with rendered fact_extraction prompt."""
        mock_llm_provider.generate_json.return_value = {"facts": ["Fact one", "Fact two"]}

        facts = await dedup_service._extract_facts("Some content about testing")

        assert mock_llm_provider.generate_json.called
        assert facts == ["Fact one", "Fact two"]

    @pytest.mark.asyncio
    async def test_extract_facts_renders_prompt(
        self, dedup_service, mock_llm_provider, mock_prompt_loader
    ) -> None:
        """_extract_facts renders the fact_extraction prompt with content."""
        mock_llm_provider.generate_json.return_value = {"facts": []}

        await dedup_service._extract_facts("Test content")

        mock_prompt_loader.render.assert_called_once()
        call_args = mock_prompt_loader.render.call_args
        assert call_args[0][0] == "memory/fact_extraction"
        assert call_args[0][1]["content"] == "Test content"

    @pytest.mark.asyncio
    async def test_extract_facts_empty_response(self, dedup_service, mock_llm_provider) -> None:
        """_extract_facts returns empty list when LLM returns no facts."""
        mock_llm_provider.generate_json.return_value = {"facts": []}

        facts = await dedup_service._extract_facts("Boring content")
        assert facts == []

    @pytest.mark.asyncio
    async def test_extract_facts_llm_failure_returns_empty(
        self, dedup_service, mock_llm_provider
    ) -> None:
        """_extract_facts returns empty list on LLM failure."""
        mock_llm_provider.generate_json.side_effect = Exception("LLM error")

        facts = await dedup_service._extract_facts("Some content")
        assert facts == []


class TestDecideActions:
    """Tests for DedupService._decide_actions()."""

    @pytest.mark.asyncio
    async def test_decide_actions_calls_llm(self, dedup_service, mock_llm_provider) -> None:
        """_decide_actions calls LLM with rendered dedup_decision prompt."""
        mock_llm_provider.generate_json.return_value = {
            "memory": [
                {"event": "ADD", "text": "New fact"},
            ]
        }

        actions = await dedup_service._decide_actions(
            new_facts=["New fact"],
            existing_memories=[],
        )

        assert len(actions) == 1
        assert actions[0].event == "ADD"
        assert actions[0].text == "New fact"

    @pytest.mark.asyncio
    async def test_decide_actions_with_update(self, dedup_service, mock_llm_provider) -> None:
        """_decide_actions handles UPDATE events with memory IDs."""
        mock_llm_provider.generate_json.return_value = {
            "memory": [
                {"event": "UPDATE", "text": "Updated text", "id": "mem-123"},
            ]
        }

        actions = await dedup_service._decide_actions(
            new_facts=["Updated info"],
            existing_memories=[{"id": "mem-123", "text": "Old info"}],
        )

        assert len(actions) == 1
        assert actions[0].event == "UPDATE"
        assert actions[0].memory_id == "mem-123"

    @pytest.mark.asyncio
    async def test_decide_actions_llm_failure_returns_empty(
        self, dedup_service, mock_llm_provider
    ) -> None:
        """_decide_actions returns empty list on LLM failure."""
        mock_llm_provider.generate_json.side_effect = Exception("LLM error")

        actions = await dedup_service._decide_actions(new_facts=["Fact"], existing_memories=[])
        assert actions == []


class TestProcess:
    """Tests for DedupService.process() full pipeline."""

    @pytest.mark.asyncio
    async def test_process_add_pipeline(
        self, dedup_service, mock_llm_provider, mock_storage, mock_embed_fn, mock_vector_store
    ) -> None:
        """process() extracts facts, decides ADD, stores memory."""
        # Mock fact extraction
        mock_llm_provider.generate_json.side_effect = [
            {"facts": ["The project uses Python 3.13"]},  # extract_facts
            {
                "memory": [{"event": "ADD", "text": "The project uses Python 3.13"}]
            },  # decide_actions
        ]

        # Mock storage.create_memory
        mock_mem = MagicMock()
        mock_mem.id = "mem-new"
        mock_mem.content = "The project uses Python 3.13"
        mock_storage.create_memory = MagicMock(return_value=mock_mem)

        result = await dedup_service.process(
            content="We're using Python 3.13 for this project",
            project_id="proj-1",
        )

        assert isinstance(result, DedupResult)
        assert len(result.added) == 1
        assert result.added[0].id == "mem-new"

    @pytest.mark.asyncio
    async def test_process_update_pipeline(
        self, dedup_service, mock_llm_provider, mock_storage, mock_embed_fn, mock_vector_store
    ) -> None:
        """process() handles UPDATE actions."""
        mock_llm_provider.generate_json.side_effect = [
            {"facts": ["Python version is 3.13"]},
            {"memory": [{"event": "UPDATE", "text": "Python version is 3.13", "id": "mem-old"}]},
        ]

        mock_updated = MagicMock()
        mock_updated.id = "mem-old"
        mock_updated.content = "Python version is 3.13"
        mock_storage.update_memory = MagicMock(return_value=mock_updated)

        # Return existing memory for search
        mock_vector_store.search.return_value = [("mem-old", 0.9)]
        mock_existing = MagicMock()
        mock_existing.id = "mem-old"
        mock_existing.content = "Python version is 3.12"
        mock_storage.get_memory.return_value = mock_existing

        result = await dedup_service.process(
            content="Python version is now 3.13",
            project_id="proj-1",
        )

        assert len(result.updated) == 1

    @pytest.mark.asyncio
    async def test_process_delete_pipeline(
        self, dedup_service, mock_llm_provider, mock_storage, mock_vector_store, mock_embed_fn
    ) -> None:
        """process() handles DELETE actions."""
        mock_llm_provider.generate_json.side_effect = [
            {"facts": ["Project no longer uses Redis"]},
            {"memory": [{"event": "DELETE", "text": "Obsolete", "id": "mem-redis"}]},
        ]

        mock_storage.delete_memory = MagicMock(return_value=True)

        result = await dedup_service.process(
            content="We removed Redis from the project",
            project_id="proj-1",
        )

        assert len(result.deleted) == 1
        assert result.deleted[0] == "mem-redis"

    @pytest.mark.asyncio
    async def test_process_noop_pipeline(
        self, dedup_service, mock_llm_provider, mock_embed_fn
    ) -> None:
        """process() handles NOOP — no changes made."""
        mock_llm_provider.generate_json.side_effect = [
            {"facts": ["Uses pytest"]},
            {"memory": [{"event": "NOOP", "text": "Already known"}]},
        ]

        result = await dedup_service.process(content="We use pytest", project_id="proj-1")

        assert result.added == []
        assert result.updated == []
        assert result.deleted == []

    @pytest.mark.asyncio
    async def test_process_fallback_on_extract_failure(
        self, dedup_service, mock_llm_provider, mock_storage, mock_embed_fn, mock_vector_store
    ) -> None:
        """process() falls back to simple store when fact extraction fails."""
        mock_llm_provider.generate_json.side_effect = Exception("LLM down")

        mock_mem = MagicMock()
        mock_mem.id = "mem-fallback"
        mock_mem.content = "Raw content"
        mock_storage.create_memory = MagicMock(return_value=mock_mem)

        result = await dedup_service.process(
            content="Raw content to store",
            project_id="proj-1",
            memory_type="fact",
            tags=["fallback"],
        )

        assert len(result.added) == 1
        mock_storage.create_memory.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_fallback_on_decide_failure(
        self, dedup_service, mock_llm_provider, mock_storage, mock_embed_fn, mock_vector_store
    ) -> None:
        """process() falls back to simple store when dedup decision fails."""
        mock_llm_provider.generate_json.side_effect = [
            {"facts": ["A fact"]},  # extract succeeds
            Exception("Dedup LLM error"),  # decide fails
        ]

        mock_mem = MagicMock()
        mock_mem.id = "mem-fallback2"
        mock_storage.create_memory = MagicMock(return_value=mock_mem)

        result = await dedup_service.process(
            content="Some content",
            project_id="proj-1",
        )

        # Falls back to storing the original content
        assert len(result.added) == 1

    @pytest.mark.asyncio
    async def test_process_searches_qdrant_for_similar(
        self, dedup_service, mock_llm_provider, mock_embed_fn, mock_vector_store, mock_storage
    ) -> None:
        """process() embeds each fact and searches Qdrant for similar memories."""
        mock_llm_provider.generate_json.side_effect = [
            {"facts": ["Fact A"]},
            {"memory": [{"event": "NOOP", "text": "Already exists"}]},
        ]

        mock_vector_store.search.return_value = [("mem-1", 0.95)]
        mock_existing = MagicMock()
        mock_existing.id = "mem-1"
        mock_existing.content = "Existing fact A"
        mock_storage.get_memory.return_value = mock_existing

        await dedup_service.process(content="Fact A info", project_id="proj-1")

        # Verify embed was called for the fact
        mock_embed_fn.assert_called()
        # Verify search was called
        mock_vector_store.search.assert_called()
