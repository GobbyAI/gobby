"""Tests for deterministic memory recall overflow retrieval."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from gobby.hooks.memory_recall_delivery import (
    MEMORY_RECALL_DELIVERIES_VARIABLE,
    MemoryRecallDeliveryQueue,
    _memory_bodies,
)
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.memory import create_memory_registry
from gobby.mcp_proxy.tools.memory_recall import (
    MAX_DIRECT_MCP_SERIALIZED_CHARS,
    _next_chunk,
    register_memory_recall_tool,
)
from gobby.memory.manager import MemoryManager
from gobby.memory.recall import MemoryRecallRunner, _memory_to_payload
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories_models import Memory, MemoryType
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

SESSION_ID = "796c13d5-34bd-4b6a-b60c-b022df873ad2"
OTHER_SESSION_ID = "4e8c86db-b06c-41cf-8866-c2722ac87658"
PROJECT_ID = "4a0cc9e8-ab87-48c0-9c55-84831e47c510"


class FakeMemoryManager:
    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    async def search_memories(self, **_kwargs: Any) -> list[Any]:
        return []


def _create_sessions(db: HubDatabase) -> None:
    db.execute(
        "INSERT INTO projects (id, name, created_at) VALUES (%s, %s, CURRENT_TIMESTAMP) "
        "ON CONFLICT (id) DO NOTHING",
        (PROJECT_ID, "memory-recall-tool-test"),
    )
    for session_id in (SESSION_ID, OTHER_SESSION_ID):
        db.execute(
            "INSERT INTO sessions (id, external_id, machine_id, source, project_id, "
            "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP) ON CONFLICT (id) DO NOTHING",
            (
                session_id,
                f"external-{session_id}",
                "21000000-0000-4000-8000-000000000001",
                "codex",
                PROJECT_ID,
            ),
        )


def _queue(
    db: HubDatabase,
    request_id: str,
    turn_seq: int,
    memories: list[dict[str, str]],
) -> None:
    assert MemoryRecallDeliveryQueue(db).queue(
        SESSION_ID,
        recall_request_id=request_id,
        origin_turn_seq=turn_seq,
        project_id=PROJECT_ID,
        memories=memories,
    )


def _registry(db: HubDatabase) -> InternalToolRegistry:
    registry = InternalToolRegistry("test-memory-recall")
    manager = cast(MemoryManager, FakeMemoryManager(db))
    register_memory_recall_tool(
        registry,
        lambda: manager,
    )
    return registry


@pytest.mark.asyncio
async def test_inline_recall_accepts_templated_parent_turn_sequence(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_sessions(temp_db)
    captured_variables: list[dict[str, Any]] = []

    async def capture_run(
        _runner: MemoryRecallRunner,
        _event: Any,
        _session_id: str,
        variables: dict[str, Any],
    ) -> None:
        captured_variables.append(variables)

    monkeypatch.setattr(MemoryRecallRunner, "run", capture_run)
    registry = _registry(temp_db)
    schema = registry.get_schema("recall_memories_for_prompt")
    assert schema is not None
    assert schema["inputSchema"]["properties"]["parent_turn_seq"]["type"] == "string"

    with session_context_for_test(SESSION_ID):
        result = await registry.call(
            "recall_memories_for_prompt",
            {
                "prompt": "Implement the requested memory recall change.",
                "source": "codex",
                "parent_turn_seq": "7",
            },
        )

    assert result == {"success": True, "skipped": True, "memories": []}
    assert captured_variables == [{"parent_turn_seq": 7, "is_spawned_agent": False}]


@pytest.mark.asyncio
async def test_small_overflow_returns_body_and_completes_on_final_chunk(
    temp_db: HubDatabase,
) -> None:
    _create_sessions(temp_db)
    _queue(
        temp_db,
        "request-small",
        11,
        [
            {
                "id": "memory-1",
                "content": "Exact body.",
                "memory_type": "pattern",
                "similarity": "hidden",
            }
        ],
    )
    SessionVariableManager(temp_db).set_variable(SESSION_ID, "injected_memory_ids", ["existing"])

    with session_context_for_test(SESSION_ID):
        result = await _registry(temp_db).call(
            "get_recall_memories",
            {"recall_request_id": "request-small"},
        )

    assert result["success"] is True
    assert result["final_chunk"] is True
    assert result["memories"] == [
        {
            "id": "memory-1",
            "memory_type": "pattern",
            "content": "Exact body.",
            "content_offset": 0,
            "memory_complete": True,
        }
    ]
    assert "similarity" not in result["memories"][0]
    variables = SessionVariableManager(temp_db).get_variables(SESSION_ID)
    assert variables[MEMORY_RECALL_DELIVERIES_VARIABLE][0]["status"] == "complete"
    assert variables["injected_memory_ids"] == ["existing", "memory-1"]


@pytest.mark.asyncio
async def test_large_overflow_reconstructs_every_character_below_chunk_budget(
    temp_db: HubDatabase,
) -> None:
    _create_sessions(temp_db)
    bodies = ["αβγ" * 8_000, "tail-" + ("z" * 15_000)]
    _queue(
        temp_db,
        "request-large",
        12,
        [
            {"id": "memory-1", "content": bodies[0], "memory_type": "fact"},
            {"id": "memory-2", "content": bodies[1], "memory_type": "context"},
        ],
    )
    registry = _registry(temp_db)
    reconstructed = {"memory-1": "", "memory-2": ""}
    chunks: list[dict[str, Any]] = []

    with session_context_for_test(SESSION_ID):
        while True:
            chunk = await registry.call(
                "get_recall_memories",
                {"recall_request_id": "request-large"},
            )
            chunks.append(chunk)
            segment = chunk["memories"][0]
            reconstructed[segment["id"]] += segment["content"]
            variables = SessionVariableManager(temp_db).get_variables(SESSION_ID)
            if chunk["final_chunk"]:
                assert variables["injected_memory_ids"] == ["memory-1", "memory-2"]
                break
            assert variables.get("injected_memory_ids", []) == []
            assert variables[MEMORY_RECALL_DELIVERIES_VARIABLE][0]["status"] == "pending"

    assert reconstructed == {"memory-1": bodies[0], "memory-2": bodies[1]}
    assert [chunk["chunk_index"] for chunk in chunks] == list(range(len(chunks)))
    assert all(
        len(json.dumps(chunk, ensure_ascii=False, separators=(",", ":")))
        < MAX_DIRECT_MCP_SERIALIZED_CHARS
        for chunk in chunks
    )


@pytest.mark.asyncio
async def test_pending_requests_complete_oldest_first(temp_db: HubDatabase) -> None:
    _create_sessions(temp_db)
    _queue(
        temp_db,
        "request-first",
        13,
        [{"id": "first", "content": "first", "memory_type": "fact"}],
    )
    _queue(
        temp_db,
        "request-second",
        14,
        [{"id": "second", "content": "second", "memory_type": "fact"}],
    )
    registry = _registry(temp_db)

    with session_context_for_test(SESSION_ID):
        rejected = await registry.call(
            "get_recall_memories",
            {"recall_request_id": "request-second"},
        )
        first = await registry.call(
            "get_recall_memories",
            {"recall_request_id": "request-first"},
        )
        second = await registry.call(
            "get_recall_memories",
            {"recall_request_id": "request-second"},
        )

    assert rejected["success"] is False
    assert rejected["expected_recall_request_id"] == "request-first"
    assert first["final_chunk"] is True
    assert second["final_chunk"] is True


@pytest.mark.asyncio
async def test_request_is_scoped_to_ambient_session(temp_db: HubDatabase) -> None:
    _create_sessions(temp_db)
    _queue(
        temp_db,
        "request-session",
        15,
        [{"id": "memory", "content": "body", "memory_type": "fact"}],
    )
    registry = _registry(temp_db)

    with session_context_for_test(OTHER_SESSION_ID):
        wrong_session = await registry.call(
            "get_recall_memories",
            {"recall_request_id": "request-session"},
        )
    no_session = await registry.call(
        "get_recall_memories",
        {"recall_request_id": "request-session"},
    )

    assert wrong_session["success"] is False
    assert "current session" in wrong_session["error"]
    assert no_session["success"] is False
    assert "ambient Gobby session" in no_session["error"]


@pytest.mark.asyncio
async def test_queue_resolver_runtime_error_uses_retrieval_error_contract(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = InternalToolRegistry("test-memory-recall-resolver-error")

    def unavailable_manager() -> MemoryManager | None:
        raise RuntimeError("runtime is rebuilding")

    register_memory_recall_tool(registry, unavailable_manager)

    with session_context_for_test(SESSION_ID):
        result = await registry.call(
            "get_recall_memories",
            {"recall_request_id": "request-runtime-error"},
        )

    assert result == {
        "success": False,
        "recall_request_id": "request-runtime-error",
        "error": "Memory retrieval failed.",
    }
    rebuild = next(
        record
        for record in caplog.records
        if record.exc_info is not None
        and record.exc_info[0] is RuntimeError
        and "runtime is rebuilding" in str(record.exc_info[1])
    )
    assert rebuild.exc_info is not None


def test_main_memory_registry_includes_inline_and_overflow_tools(
    temp_db: HubDatabase,
) -> None:
    manager = cast(MemoryManager, FakeMemoryManager(temp_db))
    registry = create_memory_registry(lambda: manager)

    assert registry.get_tool("recall_memories_for_prompt") is not None
    assert registry.get_tool("get_recall_memories") is not None


@pytest.mark.asyncio
async def test_recall_chunks_include_rationale(temp_db: HubDatabase) -> None:
    now = datetime.now(UTC)
    populated = Memory(
        id="memory-1",
        memory_type=MemoryType.PATTERN,
        content="Exact body.",
        created_at=now,
        updated_at=now,
        rationale="keep the TS convention for future sessions",
    )
    legacy = Memory(
        id="memory-2",
        memory_type=MemoryType.FACT,
        content="Legacy row.",
        created_at=now,
        updated_at=now,
    )
    populated_payload = _memory_to_payload(populated)
    legacy_payload = _memory_to_payload(legacy)
    assert populated_payload["rationale"] == "keep the TS convention for future sessions"
    assert "rationale" in legacy_payload
    assert legacy_payload["rationale"] is None

    bodies = _memory_bodies(
        [
            populated_payload,
            legacy_payload,
            {
                "id": "memory-3",
                "content": "Empty claim.",
                "memory_type": "fact",
                "rationale": "",
            },
        ]
    )
    assert bodies[0]["rationale"] == "keep the TS convention for future sessions"
    assert "rationale" not in bodies[1]
    assert "rationale" not in bodies[2]

    chunk, _cursor = _next_chunk(
        {
            "recall_request_id": "request-rationale",
            "memories": bodies[:1],
            "cursor": {"memory_index": 0, "content_offset": 0, "chunk_index": 0},
        }
    )
    assert chunk["memories"][0]["memory_type"] == "pattern"
    assert chunk["memories"][0]["rationale"] == "keep the TS convention for future sessions"

    _create_sessions(temp_db)
    _queue(
        temp_db,
        "request-rationale",
        21,
        [
            {
                "id": "memory-1",
                "content": "Exact body.",
                "memory_type": "pattern",
                "rationale": "keep the TS convention for future sessions",
            }
        ],
    )
    with session_context_for_test(SESSION_ID):
        result = await _registry(temp_db).call(
            "get_recall_memories",
            {"recall_request_id": "request-rationale"},
        )
    assert result["success"] is True
    assert result["memories"][0]["rationale"] == "keep the TS convention for future sessions"
    stored = SessionVariableManager(temp_db).get_variables(SESSION_ID)
    queued = stored[MEMORY_RECALL_DELIVERIES_VARIABLE][0]["memories"][0]
    assert queued["rationale"] == "keep the TS convention for future sessions"
