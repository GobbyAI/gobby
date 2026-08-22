"""Tests for bounded oversized MCP tool-result offloading."""

from __future__ import annotations

import json
from typing import Any, NamedTuple, cast
from unittest.mock import MagicMock, patch

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

import gobby.mcp_proxy.services.result_offload as result_offload_module
from gobby.config.features import ToolResultOffloadConfig
from gobby.mcp_proxy.services.result_offload import (
    _WRAPPER_MUTATION_RESERVE,
    ToolResultOffloader,
)
from gobby.mcp_proxy.services.tool_execution import _execute_tool_dispatch
from gobby.search.keyword import MAX_PG_SEARCH_QUERY_CHARS, SearchHit, SearchQuerySyntaxError
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tool_results import ToolResultStore
from tests.mcp_proxy.result_offload_test_support import TEST_MAX_ENVELOPE_CHARS

pytestmark = pytest.mark.unit
RESULT_ID = "11111111-1111-1111-1111-111111111111"
PROJECT_ID = "22222222-2222-2222-2222-222222222222"


class OffloadHarness(NamedTuple):
    offloader: ToolResultOffloader
    store: MagicMock
    db: MagicMock
    search: MagicMock
    config: ToolResultOffloadConfig


class ExplodingString:
    def __str__(self) -> str:
        raise RuntimeError("cannot render")


class DispatchOffloader:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def maybe_offload(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self.result


class DispatchService:
    def __init__(self, offloader: DispatchOffloader) -> None:
        self._result_offloader = offloader
        self.after_calls: list[dict[str, object]] = []

    async def _apply_after_tool_workflow(self, **kwargs: object) -> None:
        self.after_calls.append(kwargs)


def _config(**overrides: Any) -> ToolResultOffloadConfig:
    values: dict[str, Any] = {
        "threshold_chars": 3_000,
        "max_envelope_chars": TEST_MAX_ENVELOPE_CHARS,
        "preview_chars": 400,
        "chunk_chars": 500,
        "max_stored_chars": 10_000,
        "intent_match_limit": 5,
        "retention_days": 9,
        "exempt_tools": ["gobby-results/*", "safe/small_*"],
    }
    values.update(overrides)
    return ToolResultOffloadConfig(**values)


def _harness(
    *,
    config: ToolResultOffloadConfig | None = None,
    project_id: str | None = PROJECT_ID,
) -> OffloadHarness:
    resolved_config = config or _config()
    store = MagicMock(spec=ToolResultStore)
    store.save.return_value = RESULT_ID
    db = MagicMock(spec=HubDatabase)
    search = MagicMock()
    with patch(
        "gobby.mcp_proxy.services.result_offload.BM25SearchBackend",
        return_value=search,
    ):
        offloader = ToolResultOffloader(
            cast(ToolResultStore, store),
            cast(HubDatabase, db),
            resolved_config,
            lambda: project_id,
        )
    return OffloadHarness(offloader, store, db, search, resolved_config)


def _serialized_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str))


@pytest.mark.asyncio
async def test_dispatch_offloads_after_after_tool_workflow_sees_full_result() -> None:
    full_result = {"payload": "x" * 4_000}
    envelope = {"offloaded": True, "result_id": RESULT_ID}
    offloader = DispatchOffloader(envelope)
    service = DispatchService(offloader)

    async def execute_tool(**_kwargs: object) -> object:
        return full_result

    with patch(
        "gobby.mcp_proxy.services.tool_execution._execute_tool",
        new=execute_tool,
    ):
        result = await _execute_tool_dispatch(
            service=service,
            server_name="example",
            tool_name="large_tool",
            arguments={"value": 1},
            effective_session_id="session",
            project_id=PROJECT_ID,
            emit_after_workflow=True,
            timeout=None,
            wrapper_originated=True,
            intent="find payload",
        )

    assert result == envelope
    assert service.after_calls == [
        {
            "server_name": "example",
            "tool_name": "large_tool",
            "arguments": {"value": 1},
            "session_id": "session",
            "tool_output": full_result,
        }
    ]
    assert offloader.calls == [
        {
            "server_name": "example",
            "tool_name": "large_tool",
            "result": full_result,
            "session_id": "session",
            "intent": "find payload",
            "project_id": PROJECT_ID,
        }
    ]


@pytest.mark.asyncio
async def test_dispatch_keeps_full_result_for_internal_consumers() -> None:
    full_result = {"payload": "x" * 4_000}
    offloader = DispatchOffloader({"offloaded": True})
    service = DispatchService(offloader)

    async def execute_tool(**_kwargs: object) -> object:
        return full_result

    with patch(
        "gobby.mcp_proxy.services.tool_execution._execute_tool",
        new=execute_tool,
    ):
        result = await _execute_tool_dispatch(
            service=service,
            server_name="example",
            tool_name="large_tool",
            arguments={},
            effective_session_id=None,
            project_id=None,
            emit_after_workflow=False,
            timeout=None,
            wrapper_originated=False,
            intent=None,
        )

    assert result is full_result
    assert offloader.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "server_name", "tool_name"),
    [
        (_config(enabled=False), "server", "tool"),
        (_config(), "gobby-results", "search_tool_result"),
        (_config(), "safe", "small_lookup"),
    ],
)
async def test_disabled_and_exempt_results_pass_through_untouched(
    config: ToolResultOffloadConfig,
    server_name: str,
    tool_name: str,
) -> None:
    harness = _harness(config=config)
    result = {"payload": "x" * 4_000}

    actual = await harness.offloader.maybe_offload(
        server_name=server_name,
        tool_name=tool_name,
        result=result,
        session_id="session",
        intent=None,
    )

    assert actual is result
    harness.store.save.assert_not_called()


@pytest.mark.parametrize(
    ("server_name", "tool_name"),
    [
        ("gobby-skills", "get_skill"),
        ("gobby-skills", "get_skill_file"),
        ("gobby-agents", "get_inter_session_message"),
        ("gobby-results", "get_tool_result"),
        ("gobby-results", "search_tool_result"),
    ],
)
@pytest.mark.asyncio
async def test_mandatory_exemptions_cannot_be_removed_by_config(
    server_name: str,
    tool_name: str,
) -> None:
    harness = _harness(config=_config(exempt_tools=[]))
    result = {"payload": "x" * 4_000}

    actual = await harness.offloader.maybe_offload(
        server_name=server_name,
        tool_name=tool_name,
        result=result,
        session_id="session",
        intent=None,
    )

    assert actual is result
    harness.store.save.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {"error": "boom", "hint": "r" * 4_000, "schema": {"type": "object"}},
        {"result": {"content": [{"type": "text", "text": "boom"}], "isError": True}},
        CallToolResult(
            content=[TextContent(type="text", text="boom")],
            is_error=True,
        ),
    ],
)
async def test_error_results_use_shared_classifier_and_bypass_offloading(result: object) -> None:
    harness = _harness()

    actual = await harness.offloader.maybe_offload(
        server_name="server",
        tool_name="tool",
        result=result,
        session_id="session",
        intent="find boom",
    )

    assert actual is result
    harness.store.save.assert_not_called()
    harness.search.search.assert_not_called()


@pytest.mark.asyncio
async def test_call_tool_result_with_non_text_content_passes_through() -> None:
    harness = _harness()
    result = CallToolResult(
        content=[
            ImageContent(
                type="image",
                data="a" * 4_000,
                mimeType="image/png",
            )
        ]
    )

    actual = await harness.offloader.maybe_offload(
        server_name="server",
        tool_name="tool",
        result=result,
        session_id="session",
        intent=None,
    )

    assert actual is result
    harness.store.save.assert_not_called()


@pytest.mark.asyncio
async def test_under_threshold_result_returns_original_object() -> None:
    harness = _harness()
    result = {"payload": "small"}

    actual = await harness.offloader.maybe_offload(
        server_name="server",
        tool_name="tool",
        result=result,
        session_id="session",
        intent=None,
    )

    assert actual is result
    harness.store.save.assert_not_called()


@pytest.mark.asyncio
async def test_default_threshold_keeps_15000_chars_and_offloads_15001() -> None:
    harness = _harness(config=ToolResultOffloadConfig())
    inline = CallToolResult(content=[TextContent(type="text", text="x" * 15_000)])
    oversized = CallToolResult(content=[TextContent(type="text", text="x" * 15_001)])

    inline_actual = await harness.offloader.maybe_offload(
        server_name="server",
        tool_name="tool",
        result=inline,
        session_id="session",
        intent=None,
    )
    oversized_actual = await harness.offloader.maybe_offload(
        server_name="server",
        tool_name="tool",
        result=oversized,
        session_id="session",
        intent=None,
    )

    assert inline_actual is inline
    assert isinstance(oversized_actual, dict)
    assert oversized_actual["offloaded"] is True
    assert harness.store.save.call_count == 1


@pytest.mark.asyncio
async def test_oversized_result_is_stored_and_returns_configured_envelope() -> None:
    harness = _harness()
    result = {"docs": ["x" * 4_000], "meta": {"source": "test"}}

    actual = await harness.offloader.maybe_offload(
        server_name="server",
        tool_name="tool",
        result=result,
        session_id="session",
        intent=None,
    )

    serialized = json.dumps(result, indent=2, default=str)
    harness.store.save.assert_called_once_with(
        project_id=PROJECT_ID,
        session_id="session",
        server_name="server",
        tool_name="tool",
        content=serialized,
        content_kind="json",
        total_chars=len(serialized),
    )
    assert actual["offloaded"] is True
    assert actual["result_id"] == RESULT_ID
    assert actual["retrieval_available"] is True
    assert actual["content_kind"] == "json"
    assert actual["structure"] == {
        "type": "object",
        "keys": {"docs": "list[1]", "meta": "object"},
    }
    assert actual["preview"] == serialized[: harness.config.preview_chars]
    assert actual["stored_chars"] == actual["total_chars"] == len(serialized)
    assert "3,000 chars" not in actual["guidance"]
    assert "3000 chars" in actual["guidance"]
    assert "retrievable for 9 days" in actual["guidance"]
    assert "matches" not in actual


async def test_explicit_project_id_keeps_oversized_result_retrievable() -> None:
    harness = _harness(project_id=None)

    actual = await harness.offloader.maybe_offload(
        server_name="server",
        tool_name="tool",
        result={"payload": "x" * 4_000},
        session_id="session",
        intent=None,
        project_id=PROJECT_ID,
    )

    assert actual["retrieval_available"] is True
    assert actual["result_id"] == RESULT_ID
    assert harness.store.save.call_args.kwargs["project_id"] == PROJECT_ID


@pytest.mark.asyncio
async def test_intent_search_returns_ranked_chunk_matches() -> None:
    harness = _harness()
    harness.search.search.return_value = [
        SearchHit(id="chunk-1", score=8.1),
        SearchHit(id="chunk-2", score=4.2),
    ]
    harness.db.fetchone.side_effect = [
        {
            "ordinal": 17,
            "start_offset": 34120,
            "end_offset": 36080,
            "content": "matched first section",
        },
        {
            "ordinal": 3,
            "start_offset": 6000,
            "end_offset": 7000,
            "content": "matched second section",
        },
    ]

    actual = await harness.offloader.maybe_offload(
        server_name="server",
        tool_name="tool",
        result="x" * 4_000,
        session_id="session",
        intent="find configuration",
    )

    harness.search.search.assert_called_once_with(
        "find configuration",
        5,
        filters={"result_id": RESULT_ID},
    )
    assert actual["matches"] == [
        {
            "ordinal": 17,
            "start_offset": 34120,
            "end_offset": 36080,
            "score": 8.1,
            "content": "matched first section",
        },
        {
            "ordinal": 3,
            "start_offset": 6000,
            "end_offset": 7000,
            "score": 4.2,
            "content": "matched second section",
        },
    ]


@pytest.mark.asyncio
async def test_persistence_failure_returns_bounded_non_retrievable_envelope() -> None:
    harness = _harness()
    harness.store.save.side_effect = RuntimeError("database unavailable")

    actual = await harness.offloader.maybe_offload(
        server_name="server",
        tool_name="tool",
        result={"payload": "x" * 4_000},
        session_id="session",
        intent="find payload",
    )

    assert actual["offloaded"] is True
    assert actual["retrieval_available"] is False
    assert actual["stored_chars"] == 0
    assert "result_id" not in actual
    assert "matches" not in actual
    assert "cannot be retrieved" in actual["guidance"]
    assert _serialized_size(actual) <= (
        harness.config.max_envelope_chars - _WRAPPER_MUTATION_RESERVE
    )
    harness.search.search.assert_not_called()


@pytest.mark.asyncio
async def test_genuine_serialization_failure_passes_through_verbatim() -> None:
    harness = _harness()
    result: list[object] = []
    result.append(result)

    actual = await harness.offloader.maybe_offload(
        server_name="server",
        tool_name="tool",
        result=result,
        session_id="session",
        intent=None,
    )

    assert actual is result
    harness.store.save.assert_not_called()


@pytest.mark.asyncio
async def test_default_string_serialization_failure_passes_through_verbatim() -> None:
    harness = _harness()
    result = ExplodingString()

    actual = await harness.offloader.maybe_offload(
        server_name="server",
        tool_name="tool",
        result=result,
        session_id="session",
        intent=None,
    )

    assert actual is result
    harness.store.save.assert_not_called()


@pytest.mark.asyncio
async def test_over_max_stored_chars_is_typed_too_large_and_does_not_persist() -> None:
    harness = _harness()
    result = "x" * 12_000

    actual = await harness.offloader.maybe_offload(
        server_name="server",
        tool_name="tool",
        result=result,
        session_id="session",
        intent=None,
    )

    harness.store.save.assert_not_called()
    assert actual["stored"] is False
    assert actual["reason"] == "too_large"
    assert actual["total_chars"] == 12_000
    assert actual["stored_chars"] == 0
    assert actual["retrieval_available"] is False
    assert "result_id" not in actual
    assert "tail is not retrievable" in actual["guidance"]


async def test_successful_offload_persists_every_serialized_character() -> None:
    harness = _harness()
    result = "y" * 8_000

    actual = await harness.offloader.maybe_offload(
        server_name="server",
        tool_name="tool",
        result=result,
        session_id="session",
        intent=None,
    )

    save_kwargs = harness.store.save.call_args.kwargs
    assert save_kwargs["content"] == result
    assert save_kwargs["total_chars"] == len(result)
    assert actual["stored_chars"] == actual["total_chars"] == len(result)
    assert actual["result_id"] == RESULT_ID
    assert actual["retrieval_available"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("persistence_fails", [False, True])
async def test_pathological_identity_fields_are_digest_bounded(
    persistence_fails: bool,
) -> None:
    harness = _harness()
    if persistence_fails:
        harness.store.save.side_effect = RuntimeError("database unavailable")
    server_name = "server-" + ("s" * 20_000)
    tool_name = "tool-" + ("t" * 20_000)

    actual = await harness.offloader.maybe_offload(
        server_name=server_name,
        tool_name=tool_name,
        result="x" * 4_000,
        session_id="session",
        intent=None,
    )

    assert len(actual["server_name"]) == 130
    assert len(actual["tool_name"]) == 130
    assert "…#" in actual["server_name"]
    assert "…#" in actual["tool_name"]
    assert _serialized_size(actual) <= (
        harness.config.max_envelope_chars - _WRAPPER_MUTATION_RESERVE
    )


@pytest.mark.asyncio
async def test_intent_is_bounded_before_sanitization_and_search() -> None:
    harness = _harness()
    seen: list[str] = []

    def capture(value: str) -> str:
        seen.append(value)
        return "bounded intent"

    with patch(
        "gobby.mcp_proxy.services.result_offload.sanitize_pg_search_query",
        side_effect=capture,
    ):
        actual = await harness.offloader.maybe_offload(
            server_name="server",
            tool_name="tool",
            result="x" * 4_000,
            session_id="session",
            intent="q" * 2_000_000,
        )

    assert actual["retrieval_available"] is True
    assert len(seen) == 1
    assert len(seen[0]) == MAX_PG_SEARCH_QUERY_CHARS
    harness.search.search.assert_called_once_with(
        "bounded intent",
        5,
        filters={"result_id": RESULT_ID},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["", "   ", "!!! ???"])
async def test_empty_or_sanitized_empty_intent_skips_search(intent: str) -> None:
    harness = _harness()

    actual = await harness.offloader.maybe_offload(
        server_name="server",
        tool_name="tool",
        result="x" * 4_000,
        session_id="session",
        intent=intent,
    )

    harness.search.search.assert_not_called()
    assert "matches" not in actual


@pytest.mark.asyncio
async def test_top_level_list_uses_json_serialization_and_reports_length() -> None:
    harness = _harness()
    result = ["x" * 4_000, {"nested": True}]

    actual = await harness.offloader.maybe_offload(
        server_name="server",
        tool_name="tool",
        result=result,
        session_id="session",
        intent=None,
    )

    assert actual["content_kind"] == "json"
    assert actual["structure"] == {"type": "list", "length": 2}
    assert harness.store.save.call_args.kwargs["content"] == json.dumps(
        result,
        indent=2,
        default=str,
    )


@pytest.mark.asyncio
async def test_oversized_bare_scalar_uses_json_serialization() -> None:
    harness = _harness()
    result = int("9" * 3_500)

    actual = await harness.offloader.maybe_offload(
        server_name="server",
        tool_name="tool",
        result=result,
        session_id="session",
        intent=None,
    )

    assert actual["content_kind"] == "json"
    assert harness.store.save.call_args.kwargs["content"] == json.dumps(
        result,
        indent=2,
        default=str,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "search_error",
    [SearchQuerySyntaxError("bad query"), RuntimeError("backend unavailable")],
)
async def test_post_store_search_failure_keeps_persisted_envelope(
    search_error: Exception,
) -> None:
    harness = _harness()
    harness.search.search.side_effect = search_error

    actual = await harness.offloader.maybe_offload(
        server_name="server",
        tool_name="tool",
        result="x" * 4_000,
        session_id="session",
        intent="find section",
    )

    assert actual["retrieval_available"] is True
    assert actual["result_id"] == RESULT_ID
    assert "matches" not in actual


@pytest.mark.asyncio
async def test_envelope_budget_trims_matches_and_preserves_invariant() -> None:
    harness = _harness()
    harness.search.search.return_value = [
        SearchHit(id=f"chunk-{index}", score=10.0 - index) for index in range(5)
    ]
    harness.db.fetchone.side_effect = [
        {
            "ordinal": index,
            "start_offset": index * 2_000,
            "end_offset": (index + 1) * 2_000,
            "content": "m" * 2_000,
        }
        for index in range(5)
    ]

    actual = await harness.offloader.maybe_offload(
        server_name="server",
        tool_name="tool",
        result={"k" * 5_000: ["x" * 4_000]},
        session_id="session",
        intent="find matches",
    )

    assert _serialized_size(actual) <= (
        harness.config.max_envelope_chars - _WRAPPER_MUTATION_RESERVE
    )
    assert len(actual["matches"]) < 5
    assert actual["structure"]["type"] == "object"


@pytest.mark.asyncio
async def test_envelope_budget_reserves_the_intent_matches_field() -> None:
    harness = _harness(config=_config(preview_chars=1_800))

    actual = await harness.offloader.maybe_offload(
        server_name="server-" + ("s" * 20_000),
        tool_name="tool-" + ("t" * 20_000),
        result="x" * 4_000,
        session_id="session",
        intent="find matches",
    )

    assert actual["matches"] == []
    assert _serialized_size(actual) <= (
        harness.config.max_envelope_chars - _WRAPPER_MUTATION_RESERVE
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("persistence_fails", [False, True])
async def test_over_budget_envelope_degrades_without_losing_success(
    monkeypatch: pytest.MonkeyPatch,
    persistence_fails: bool,
) -> None:
    harness = _harness()
    if persistence_fails:
        harness.store.save.side_effect = RuntimeError("storage unavailable")
    monkeypatch.setattr(
        result_offload_module,
        "_fit_text_field",
        lambda *_args, **_kwargs: "x" * 5_000,
    )

    actual = await harness.offloader.maybe_offload(
        server_name="server",
        tool_name="tool",
        result={"payload": "z" * 4_000},
        session_id="session",
        intent=None,
    )

    assert actual["offloaded"] is True
    assert actual["retrieval_available"] is (not persistence_fails)
    assert "preview" not in actual
    assert _serialized_size(actual) <= (
        harness.config.max_envelope_chars - _WRAPPER_MUTATION_RESERVE
    )


@pytest.mark.asyncio
async def test_structured_call_tool_result_serializes_mapping_payload() -> None:
    harness = _harness()
    payload = {"docs": ["x" * 4_000]}
    result = CallToolResult(
        content=[TextContent(type="text", text="short")],
        structured_content=payload,
    )

    actual = await harness.offloader.maybe_offload(
        server_name="server",
        tool_name="tool",
        result=result,
        session_id="session",
        intent=None,
    )

    serialized = json.dumps(payload, indent=2, default=str)
    assert actual["content_kind"] == "json"
    assert actual["preview"] == serialized[: harness.config.preview_chars]
    assert harness.store.save.call_args.kwargs["content"] == serialized


@pytest.mark.asyncio
async def test_text_call_tool_result_joins_and_stores_text_parts() -> None:
    harness = _harness()
    result = CallToolResult(
        content=[
            TextContent(type="text", text="a" * 2_000),
            TextContent(type="text", text="b" * 2_000),
        ]
    )

    actual = await harness.offloader.maybe_offload(
        server_name="server",
        tool_name="tool",
        result=result,
        session_id="session",
        intent=None,
    )

    serialized = f"{'a' * 2_000}\n{'b' * 2_000}"
    assert actual["content_kind"] == "text"
    assert harness.store.save.call_args.kwargs["content"] == serialized


@pytest.mark.asyncio
async def test_under_threshold_call_tool_result_keeps_original_object() -> None:
    harness = _harness()
    result = CallToolResult(content=[TextContent(type="text", text="small")])

    actual = await harness.offloader.maybe_offload(
        server_name="server",
        tool_name="tool",
        result=result,
        session_id="session",
        intent=None,
    )

    assert actual is result
    harness.store.save.assert_not_called()
