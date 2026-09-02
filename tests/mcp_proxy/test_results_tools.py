from __future__ import annotations

import json
import uuid
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.config.features import ToolResultOffloadConfig
from gobby.mcp_proxy.services.result_offload import _WRAPPER_MUTATION_RESERVE
from gobby.mcp_proxy.tools.results import (
    _MAX_SLICE_CHARS,
    _hydrate_matches,
    create_results_registry,
)
from gobby.search.keyword import MAX_PG_SEARCH_QUERY_CHARS, SearchHit
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tool_results import ToolResultStore
from gobby.utils.project_context import reset_project_context, set_project_context
from gobby.workflows.enforcement.blocking import DISCOVERY_TOOLS
from tests.mcp_proxy.result_offload_test_support import TEST_MAX_ENVELOPE_CHARS

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_process_project_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep result-tool scope tests independent of the invoking agent project."""
    monkeypatch.delenv("GOBBY_PROJECT_ID", raising=False)


def _config(*, retention_days: int = 13) -> ToolResultOffloadConfig:
    return ToolResultOffloadConfig(
        threshold_chars=3_000,
        max_envelope_chars=TEST_MAX_ENVELOPE_CHARS,
        preview_chars=200,
        chunk_chars=200,
        max_stored_chars=10_000,
        retention_days=retention_days,
    )


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


def _serialized_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _not_found_error(config: ToolResultOffloadConfig) -> dict[str, object]:
    return {
        "success": False,
        "error": (f"result_id not found or expired ({config.retention_days}-day retention)"),
    }


def _meta(result_id: str, project_id: str, *, total_chars: int = 500) -> dict[str, Any]:
    return {
        "result_id": result_id,
        "project_id": project_id,
        "session_id": None,
        "server_name": "server",
        "tool_name": "tool",
        "content_kind": "text",
        "total_chars": total_chars,
        "stored_chars": total_chars,
        "created_at": "now",
    }


def test_hydrate_matches_uses_one_bulk_query_and_preserves_hit_order() -> None:
    db = MagicMock()
    db.fetchall.return_value = [
        {
            "id": "chunk-2",
            "ordinal": 2,
            "start_offset": 20,
            "end_offset": 30,
            "content": "second",
        },
        {
            "id": "chunk-1",
            "ordinal": 1,
            "start_offset": 10,
            "end_offset": 20,
            "content": "first",
        },
    ]

    matches = _hydrate_matches(
        db,
        result_id="result-1",
        hits=[
            SearchHit(id="chunk-1", score=9.0),
            SearchHit(id="missing", score=8.0),
            SearchHit(id="chunk-2", score=7.0),
        ],
    )

    db.fetchall.assert_called_once()
    assert [match["content"] for match in matches] == ["first", "second"]
    assert [match["score"] for match in matches] == [9.0, 7.0]


def _mocked_registry(
    *,
    config: ToolResultOffloadConfig,
    store: MagicMock,
    backend: MagicMock,
    project_id: str = "11111111-1111-4111-8111-111111111111",
) -> Any:
    db = cast(HubDatabase, MagicMock())
    with (
        patch(
            "gobby.mcp_proxy.tools.results.ToolResultStore",
            return_value=store,
        ),
        patch(
            "gobby.mcp_proxy.tools.results.pick_search_backend",
            return_value=backend,
        ),
    ):
        return create_results_registry(
            db,
            config,
            default_project_id=project_id,
        )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_tool_result_pages_content_within_shared_budget(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    config = _config()
    store = ToolResultStore(temp_db, config)
    content = "".join(str(index % 10) for index in range(5_000))
    result_id = _save(
        store,
        project_id=sample_project["id"],
        content=content,
        total_chars=5_500,
    )
    registry = create_results_registry(
        temp_db,
        config,
        default_project_id=sample_project["id"],
    )

    result = await registry.call(
        "get_tool_result",
        {
            "result_id": result_id,
            "offset": 100,
            "limit": config.max_envelope_chars - _WRAPPER_MUTATION_RESERVE,
        },
    )

    assert result["content"] == content[100 : 100 + len(result["content"])]
    assert result["offset"] == 100
    assert result["next_offset"] == 100 + len(result["content"])
    assert result["total_chars"] == 5_500
    assert result["stored_chars"] == 5_000
    assert _serialized_size(result) <= (config.max_envelope_chars - _WRAPPER_MUTATION_RESERVE)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_search_tool_result_hydrates_ranked_chunks_within_shared_budget(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    config = _config()
    store = ToolResultStore(temp_db, config)
    content = "\n".join(f"section {index} needle {'x' * 180}" for index in range(20))
    result_id = _save(store, project_id=sample_project["id"], content=content)
    registry = create_results_registry(
        temp_db,
        config,
        default_project_id=sample_project["id"],
    )

    result = await registry.call(
        "search_tool_result",
        {"result_id": result_id, "query": "needle", "limit": 50},
    )

    assert result["result_id"] == result_id
    assert result["total_chars"] == len(content)
    assert result["matches"]
    assert all("needle" in match["content"] for match in result["matches"])
    assert all(
        {"ordinal", "start_offset", "end_offset", "score", "content"} <= match.keys()
        for match in result["matches"]
    )
    assert _serialized_size(result) <= (config.max_envelope_chars - _WRAPPER_MUTATION_RESERVE)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_unknown_expired_and_cross_project_ids_share_configured_error(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    project_manager: LocalProjectManager,
) -> None:
    config = _config(retention_days=17)
    store = ToolResultStore(temp_db, config)
    result_id = _save(
        store,
        project_id=sample_project["id"],
        content="owned content",
    )
    registry = create_results_registry(
        temp_db,
        config,
        default_project_id=sample_project["id"],
    )
    expected = _not_found_error(config)

    unknown = await registry.call(
        "get_tool_result",
        {"result_id": str(uuid.uuid4())},
    )

    other_project = project_manager.create(name="other-results-tools-project")
    token = set_project_context({"id": other_project.id})
    try:
        cross_project = await registry.call(
            "search_tool_result",
            {"result_id": result_id, "query": "owned"},
        )
    finally:
        reset_project_context(token)

    temp_db.execute(
        "UPDATE tool_results SET created_at = NOW() - INTERVAL '18 days' WHERE id = %s",
        (result_id,),
    )
    expired = await registry.call(
        "get_tool_result",
        {"result_id": result_id},
    )

    assert unknown == expected
    assert cross_project == expected
    assert expired == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("get_tool_result", {"result_id": "malformed"}),
        ("get_tool_result", {"result_id": "x" * 10_000}),
        (
            "search_tool_result",
            {"result_id": "malformed", "query": "needle"},
        ),
        (
            "search_tool_result",
            {"result_id": "x" * 10_000, "query": "needle"},
        ),
    ],
)
async def test_malformed_ids_return_not_found_before_store_or_search(
    tool_name: str,
    arguments: dict[str, object],
) -> None:
    config = _config()
    store = MagicMock(spec=ToolResultStore)
    backend = MagicMock()
    registry = _mocked_registry(config=config, store=store, backend=backend)

    result = await registry.call(tool_name, arguments)

    assert result == _not_found_error(config)
    store.get_meta.assert_not_called()
    store.get_slice.assert_not_called()
    backend.search.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "get_tool_result",
            {"result_id": str(uuid.uuid4()), "offset": -1, "limit": 10},
        ),
        (
            "get_tool_result",
            {"result_id": str(uuid.uuid4()), "offset": 0, "limit": 0},
        ),
        (
            "search_tool_result",
            {"result_id": str(uuid.uuid4()), "query": "needle", "limit": 0},
        ),
        (
            "search_tool_result",
            {"result_id": str(uuid.uuid4()), "query": "needle", "limit": 51},
        ),
        (
            "search_tool_result",
            {"result_id": str(uuid.uuid4()), "query": "", "limit": 5},
        ),
        (
            "search_tool_result",
            {"result_id": str(uuid.uuid4()), "query": "   ", "limit": 5},
        ),
        (
            "search_tool_result",
            {
                "result_id": str(uuid.uuid4()),
                "query": "x" * (MAX_PG_SEARCH_QUERY_CHARS + 1),
                "limit": 5,
            },
        ),
    ],
)
async def test_invalid_bounds_and_queries_fail_before_store_or_search(
    tool_name: str,
    arguments: dict[str, object],
) -> None:
    config = _config()
    store = MagicMock(spec=ToolResultStore)
    backend = MagicMock()
    registry = _mocked_registry(config=config, store=store, backend=backend)

    result = await registry.call(tool_name, arguments)

    assert result["success"] is False
    assert "invalid arguments" in result["error"]
    store.get_meta.assert_not_called()
    store.get_slice.assert_not_called()
    backend.search.assert_not_called()


@pytest.mark.asyncio
async def test_punctuation_only_query_runs_meta_gate_without_search() -> None:
    config = _config()
    result_id = str(uuid.uuid4())
    project_id = "11111111-1111-4111-8111-111111111111"
    store = MagicMock(spec=ToolResultStore)
    store.get_meta.return_value = _meta(result_id, project_id)
    backend = MagicMock()
    registry = _mocked_registry(
        config=config,
        store=store,
        backend=backend,
        project_id=project_id,
    )

    result = await registry.call(
        "search_tool_result",
        {"result_id": result_id, "query": "!!!???"},
    )

    assert result == {"result_id": result_id, "total_chars": 500, "matches": []}
    store.get_meta.assert_called_once_with(result_id, project_id)
    backend.search.assert_not_called()


@pytest.mark.asyncio
async def test_punctuation_only_query_preserves_unknown_id_error_parity() -> None:
    config = _config()
    result_id = str(uuid.uuid4())
    store = MagicMock(spec=ToolResultStore)
    store.get_meta.return_value = None
    backend = MagicMock()
    registry = _mocked_registry(config=config, store=store, backend=backend)

    result = await registry.call(
        "search_tool_result",
        {"result_id": result_id, "query": "!!!???"},
    )

    assert result == _not_found_error(config)
    store.get_meta.assert_called_once()
    backend.search.assert_not_called()


@pytest.mark.asyncio
async def test_search_backend_failure_returns_bounded_nondiagnostic_error() -> None:
    config = _config()
    result_id = str(uuid.uuid4())
    project_id = "11111111-1111-4111-8111-111111111111"
    store = MagicMock(spec=ToolResultStore)
    store.get_meta.return_value = _meta(result_id, project_id)
    backend = MagicMock()
    backend.search.side_effect = RuntimeError("sensitive-driver-dump-" + "x" * 10_000)
    registry = _mocked_registry(
        config=config,
        store=store,
        backend=backend,
        project_id=project_id,
    )

    result = await registry.call(
        "search_tool_result",
        {"result_id": result_id, "query": "needle"},
    )

    assert result["success"] is False
    assert result["error"] == "tool result search unavailable"
    assert "sensitive-driver-dump" not in result["error"]
    assert _serialized_size(result) <= (config.max_envelope_chars - _WRAPPER_MUTATION_RESERVE)


@pytest.mark.asyncio
async def test_get_tool_result_clamps_limit_above_live_maximum() -> None:
    """A limit above the live envelope budget is clamped, never rejected (#21532)."""
    config = _config()
    live_limit = config.max_envelope_chars - _WRAPPER_MUTATION_RESERVE
    store = MagicMock(spec=ToolResultStore)
    store.get_slice.return_value = {
        "content": "x" * 200,
        "offset": 40,
        "next_offset": 240,
        "total_chars": 5_500,
        "stored_chars": 5_000,
    }
    backend = MagicMock()
    registry = _mocked_registry(config=config, store=store, backend=backend)
    result_id = str(uuid.uuid4())

    result = await registry.call(
        "get_tool_result",
        {"result_id": result_id, "offset": 40, "limit": _MAX_SLICE_CHARS},
    )

    store.get_slice.assert_called_once_with(
        result_id,
        "11111111-1111-4111-8111-111111111111",
        offset=40,
        limit=live_limit,
    )
    assert result["content"] == "x" * 200
    assert result["next_offset"] == 240
    assert "error" not in result


@pytest.mark.asyncio
async def test_get_tool_result_store_failure_returns_bounded_error() -> None:
    config = _config()
    store = MagicMock(spec=ToolResultStore)
    store.get_slice.side_effect = RuntimeError("sensitive-driver-dump-" + "x" * 10_000)
    backend = MagicMock()
    registry = _mocked_registry(config=config, store=store, backend=backend)

    result = await registry.call(
        "get_tool_result",
        {"result_id": str(uuid.uuid4())},
    )

    assert result == {
        "success": False,
        "error": "tool result retrieval unavailable",
    }


def test_results_tool_schemas_bound_every_input() -> None:
    config = _config()
    registry = create_results_registry(
        cast(HubDatabase, MagicMock()),
        config,
        default_project_id="11111111-1111-4111-8111-111111111111",
    )

    search_schema = registry.get_schema("search_tool_result")
    get_schema = registry.get_schema("get_tool_result")

    assert search_schema is not None
    assert get_schema is not None
    search_properties = search_schema["inputSchema"]["properties"]
    get_properties = get_schema["inputSchema"]["properties"]
    assert search_properties["query"]["maxLength"] == MAX_PG_SEARCH_QUERY_CHARS
    assert search_properties["query"]["minLength"] == 1
    assert search_properties["limit"]["minimum"] == 1
    assert search_properties["limit"]["maximum"] == 50
    assert get_properties["offset"]["minimum"] == 0
    assert get_properties["limit"]["minimum"] == 1
    assert get_properties["limit"]["maximum"] == 1_000_000 - _WRAPPER_MUTATION_RESERVE
    assert get_properties["limit"]["default"] == 1_000
    assert search_properties["result_id"] == get_properties["result_id"]


def test_results_schema_limit_is_stable_across_initial_configs() -> None:
    low = _config().model_copy(update={"max_envelope_chars": 4_000})
    high = _config().model_copy(update={"max_envelope_chars": 20_000})

    low_schema = create_results_registry(cast(HubDatabase, MagicMock()), low).get_schema(
        "get_tool_result"
    )
    high_schema = create_results_registry(cast(HubDatabase, MagicMock()), high).get_schema(
        "get_tool_result"
    )

    assert low_schema is not None
    assert high_schema is not None
    assert (
        low_schema["inputSchema"]["properties"]["limit"]
        == high_schema["inputSchema"]["properties"]["limit"]
    )


def test_results_tools_are_schema_discovery_exempt() -> None:
    assert {"search_tool_result", "get_tool_result"} <= DISCOVERY_TOOLS


class _NoSqlDatabase:
    def fetchone(self, sql: str, params: tuple[Any, ...]) -> None:
        raise AssertionError(f"unexpected SQL: {sql} {params}")


@pytest.mark.parametrize(("offset", "limit"), [(-1, 1), (0, 0), (0, -1)])
def test_store_slice_rejects_invalid_bounds_before_sql(offset: int, limit: int) -> None:
    store = ToolResultStore(cast(HubDatabase, _NoSqlDatabase()), _config())

    with pytest.raises(ValueError):
        store.get_slice(
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            offset=offset,
            limit=limit,
        )
