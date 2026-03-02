"""Tests for mcp_proxy/services/recommendation.py — targeting uncovered lines."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

if TYPE_CHECKING:
    from gobby.mcp_proxy.services.recommendation import RecommendationService

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_llm_service() -> MagicMock:
    svc = MagicMock()
    provider = MagicMock()
    provider.generate_text = AsyncMock(
        return_value='```json\n{"recommendations": [{"server": "s", "tool": "t", "reason": "r"}]}\n```'
    )
    svc.get_default_provider.return_value = provider
    return svc


@pytest.fixture
def mock_mcp_manager() -> MagicMock:
    mgr = MagicMock()
    mgr.get_available_servers.return_value = ["gobby-tasks", "gobby-memory"]
    return mgr


@pytest.fixture
def mock_semantic_search() -> MagicMock:
    ss = MagicMock()
    result = MagicMock()
    result.server_name = "gobby-tasks"
    result.tool_name = "create_task"
    result.description = "Create a task"
    result.similarity = 0.95
    ss.search_tools = AsyncMock(return_value=[result])
    return ss


@pytest.fixture
def service(
    mock_llm_service: MagicMock,
    mock_mcp_manager: MagicMock,
) -> RecommendationService:
    from gobby.mcp_proxy.services.recommendation import RecommendationService

    svc = RecommendationService(
        llm_service=mock_llm_service,
        mcp_manager=mock_mcp_manager,
        project_id="proj-1",
        db=MagicMock(),
    )
    svc._loader = MagicMock()
    svc._loader.render.return_value = "recommend tools for this task"
    return svc


@pytest.fixture
def service_with_semantic(
    mock_llm_service: MagicMock,
    mock_mcp_manager: MagicMock,
    mock_semantic_search: MagicMock,
) -> RecommendationService:
    from gobby.mcp_proxy.services.recommendation import RecommendationService

    svc = RecommendationService(
        llm_service=mock_llm_service,
        mcp_manager=mock_mcp_manager,
        semantic_search=mock_semantic_search,
        project_id="proj-1",
        db=MagicMock(),
    )
    svc._loader = MagicMock()
    svc._loader.render.return_value = "recommend tools for this task"
    return svc


# --- recommend_tools: LLM mode ---


@pytest.mark.asyncio
async def test_recommend_llm(service: RecommendationService) -> None:
    result = await service.recommend_tools("create a task")
    assert result["success"] is True
    assert result["search_mode"] == "llm"
    assert len(result["recommendations"]) == 1


@pytest.mark.asyncio
async def test_recommend_llm_error(service: RecommendationService) -> None:
    service._llm_service.get_default_provider.side_effect = RuntimeError("no provider")
    result = await service.recommend_tools("test")
    assert result["success"] is False


@pytest.mark.asyncio
async def test_recommend_llm_bad_json(service: RecommendationService) -> None:
    provider = MagicMock()
    provider.generate_text = AsyncMock(return_value="not json at all")
    service._llm_service.get_default_provider.return_value = provider

    result = await service.recommend_tools("test")
    assert result["success"] is True
    assert result["recommendations"] == []


@pytest.mark.asyncio
async def test_recommend_llm_json_no_backticks(service: RecommendationService) -> None:
    provider = MagicMock()
    provider.generate_text = AsyncMock(return_value='{"recommendations": [{"t": 1}]}')
    service._llm_service.get_default_provider.return_value = provider

    result = await service.recommend_tools("test")
    assert result["success"] is True


# --- recommend_tools: semantic mode ---


@pytest.mark.asyncio
async def test_recommend_semantic(service_with_semantic: RecommendationService) -> None:
    result = await service_with_semantic.recommend_tools("create a task", search_mode="semantic")
    assert result["success"] is True
    assert result["search_mode"] == "semantic"
    assert result["total_results"] == 1


@pytest.mark.asyncio
async def test_recommend_semantic_no_search(service: RecommendationService) -> None:
    result = await service.recommend_tools("test", search_mode="semantic")
    assert result["success"] is False
    assert "not configured" in result["error"]


@pytest.mark.asyncio
async def test_recommend_semantic_no_project() -> None:
    from gobby.mcp_proxy.services.recommendation import RecommendationService

    svc = RecommendationService(
        llm_service=MagicMock(),
        mcp_manager=MagicMock(),
        semantic_search=MagicMock(),
        project_id=None,
        db=MagicMock(),
    )
    result = await svc.recommend_tools("test", search_mode="semantic")
    assert result["success"] is False
    assert "Project ID" in result["error"]


@pytest.mark.asyncio
async def test_recommend_semantic_error(service_with_semantic: RecommendationService) -> None:
    service_with_semantic._semantic_search.search_tools = AsyncMock(
        side_effect=RuntimeError("index error")
    )
    result = await service_with_semantic.recommend_tools("test", search_mode="semantic")
    assert result["success"] is False


# --- recommend_tools: hybrid mode ---


@pytest.mark.asyncio
async def test_recommend_hybrid(service_with_semantic: RecommendationService) -> None:
    result = await service_with_semantic.recommend_tools("create task", search_mode="hybrid")
    assert result["success"] is True
    assert result["search_mode"] in ("hybrid", "hybrid_fallback")


@pytest.mark.asyncio
async def test_recommend_hybrid_semantic_fails(service: RecommendationService) -> None:
    # service has no semantic search, so semantic fails → falls back to LLM
    result = await service.recommend_tools("test", search_mode="hybrid")
    assert result["success"] is True
    assert result["search_mode"] == "llm"


@pytest.mark.asyncio
async def test_recommend_hybrid_llm_rerank_fails(
    service_with_semantic: RecommendationService,
) -> None:
    provider = MagicMock()
    provider.generate_text = AsyncMock(side_effect=RuntimeError("LLM down"))
    service_with_semantic._llm_service.get_default_provider.return_value = provider

    result = await service_with_semantic.recommend_tools("test", search_mode="hybrid")
    assert result["success"] is True
    assert result["search_mode"] == "hybrid_fallback"


# --- _get_config ---


def test_get_config_default() -> None:
    from gobby.mcp_proxy.services.recommendation import RecommendationService

    svc = RecommendationService(llm_service=MagicMock(), mcp_manager=MagicMock(), db=MagicMock())
    config = svc._get_config()
    assert config is not None


def test_get_config_provided() -> None:
    from gobby.mcp_proxy.services.recommendation import RecommendationService

    custom_config = MagicMock()
    svc = RecommendationService(
        llm_service=MagicMock(),
        mcp_manager=MagicMock(),
        config=custom_config,
        db=MagicMock(),
    )
    assert svc._get_config() is custom_config
