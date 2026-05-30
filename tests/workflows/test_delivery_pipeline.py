"""Tests for inline inject_result delivery and memory dedup formatting."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleEvent
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.engine.effects import _is_empty_inject_payload
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    temp_db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        ("proj-delivery", "delivery-pipeline"),
    )
    return temp_db


@pytest.fixture
def engine(db: HubDatabase) -> RuleEngine:
    return RuleEngine(db)


def _vars(db: HubDatabase, session_id: str) -> dict[str, Any]:
    return SessionVariableManager(db).get_variables(session_id)


def _set_injected(db: HubDatabase, session_id: str, ids: list[str]) -> None:
    SessionVariableManager(db).set_variable(session_id, "injected_memory_ids", ids)


def _memory(mid: str) -> dict[str, Any]:
    return {"id": mid, "content": f"content-sentinel-{mid}"}


def _memory_recall_message(
    memories: list[dict[str, Any]],
    *,
    origin_turn_seq: int | None = 4,
    from_session: str = "child-helper",
) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "memory_recall", "memories": memories}
    if origin_turn_seq is not None:
        payload["origin_turn_seq"] = origin_turn_seq
    return {"from_session": from_session, "content": json.dumps(payload)}


def _plain_message(content: str, *, from_session: str = "child-plain") -> dict[str, Any]:
    return {"from_session": from_session, "content": content}


def _variables(**overrides: Any) -> dict[str, Any]:
    variables = {"parent_turn_seq": 5, "memory_recall_helper_enabled": True}
    variables.update(overrides)
    return variables


def _event(session_id: str = "ext-X", platform_session_id: str = "plat-Y") -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=session_id,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={"tool_name": "Read"},
        metadata={"_platform_session_id": platform_session_id},
    )


def _insert_rule(
    manager: LocalWorkflowDefinitionManager,
    name: str,
    effect: RuleEffect,
) -> None:
    manager.create(
        name=name,
        workflow_type="rule",
        definition_json=RuleDefinitionBody(
            event=RuleEvent.BEFORE_TOOL,
            effects=[effect],
        ).model_dump_json(),
        priority=10,
        enabled=True,
    )


def test_is_empty_inject_payload_shapes() -> None:
    assert _is_empty_inject_payload({"success": True, "messages": [], "count": 0}) is True
    assert _is_empty_inject_payload({"success": True, "memories": []}) is True
    assert _is_empty_inject_payload({"success": True, "messages": ["x"], "count": 1}) is False
    assert _is_empty_inject_payload({"success": True, "memories": [_memory("m1")]}) is False


def test_empty_delivery_no_mutation(engine: RuleEngine, db: HubDatabase) -> None:
    result = engine._format_delivery_result(
        {"success": True, "messages": [], "count": 0},
        "plat-Y",
        _variables(),
    )

    assert result is None
    assert "injected_memory_ids" not in _vars(db, "plat-Y")


def test_single_memory_recall_inject(engine: RuleEngine, db: HubDatabase) -> None:
    result = engine._format_delivery_result(
        {"messages": [_memory_recall_message([_memory("m1")])], "count": 1},
        "plat-Y",
        _variables(),
    )

    assert result is not None
    assert "content-sentinel-m1" in result
    assert _vars(db, "plat-Y")["injected_memory_ids"] == ["m1"]


def test_dedup_against_injected_ids(engine: RuleEngine, db: HubDatabase) -> None:
    _set_injected(db, "plat-Y", ["m1"])

    result = engine._format_delivery_result(
        {"messages": [_memory_recall_message([_memory("m1")])], "count": 1},
        "plat-Y",
        _variables(),
    )

    assert result is None
    assert _vars(db, "plat-Y")["injected_memory_ids"] == ["m1"]


def test_mixed_memory_recall_and_plain_messages(engine: RuleEngine, db: HubDatabase) -> None:
    result = engine._format_delivery_result(
        {
            "messages": [
                _memory_recall_message([_memory("m1")]),
                _plain_message("plain-msg-sentinel"),
            ],
            "count": 2,
        },
        "plat-Y",
        _variables(),
    )

    assert result is not None
    assert result.count("content-sentinel-m1") == 1
    assert "plain-msg-sentinel" in result
    assert _vars(db, "plat-Y")["injected_memory_ids"] == ["m1"]


def test_malformed_message_content_falls_through(engine: RuleEngine) -> None:
    result = engine._format_delivery_result(
        {"messages": [_plain_message("{not-json plain-malformed-sentinel")], "count": 1},
        "plat-Y",
        _variables(),
    )

    assert result is not None
    assert "plain-malformed-sentinel" in result


async def _append_id(db: HubDatabase, session_id: str, mid: str) -> None:
    await asyncio.to_thread(
        SessionVariableManager(db).append_to_set_variable,
        session_id,
        "injected_memory_ids",
        [mid],
    )


@pytest.mark.asyncio
async def test_concurrent_append_race_safe(db: HubDatabase) -> None:
    await asyncio.gather(_append_id(db, "plat-Y", "m1"), _append_id(db, "plat-Y", "m2"))

    assert _vars(db, "plat-Y")["injected_memory_ids"] == ["m1", "m2"]


def test_memory_recall_delivery_does_not_depend_on_child_session_state(
    engine: RuleEngine,
    db: HubDatabase,
) -> None:
    result = engine._format_delivery_result(
        {
            "messages": [
                _memory_recall_message([_memory("m-recall")], from_session="old-helper-name"),
                _plain_message("plain-sentinel", from_session="old-worker-name"),
            ],
            "count": 2,
        },
        "plat-Y",
        _variables(),
    )

    assert result is not None
    assert "content-sentinel-m-recall" in result
    assert "plain-sentinel" in result
    assert _vars(db, "plat-Y")["injected_memory_ids"] == ["m-recall"]


@pytest.mark.asyncio
async def test_session_key_uses_platform_session_id(
    db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def dispatcher(server: str, tool: str, args: dict[str, Any], event: Any) -> dict:
        return {"success": True, "result": {"memories": [_memory("m1")]}}

    manager = LocalWorkflowDefinitionManager(db)
    _insert_rule(
        manager,
        "search-memory-inline",
        RuleEffect(
            type="mcp_call",
            server="gobby-memory",
            tool="search_memories",
            arguments={"query": "q"},
            inject_result=True,
        ),
    )
    engine = RuleEngine(db, mcp_dispatcher=dispatcher)

    response = await engine.evaluate(_event(), session_id="ignored", variables={})

    assert response.context is not None
    assert "content-sentinel-m1" in response.context
    assert _vars(db, "plat-Y")["injected_memory_ids"] == ["m1"]
    assert "injected_memory_ids" not in _vars(db, "ext-X")


def test_freshness_guard_b_origin_turn_seq(engine: RuleEngine, db: HubDatabase) -> None:
    result = engine._format_delivery_result(
        {
            "messages": [
                _memory_recall_message([_memory("fresh")], origin_turn_seq=4),
                _memory_recall_message([_memory("too-old")], origin_turn_seq=3),
                _memory_recall_message([_memory("same-turn")], origin_turn_seq=5),
                _memory_recall_message([_memory("future")], origin_turn_seq=6),
                _memory_recall_message([_memory("missing")], origin_turn_seq=None),
            ],
            "count": 5,
        },
        "plat-Y",
        _variables(),
    )

    assert result is not None
    assert "content-sentinel-fresh" in result
    assert "content-sentinel-too-old" not in result
    assert "content-sentinel-same-turn" not in result
    assert "content-sentinel-future" not in result
    assert "content-sentinel-missing" not in result
    assert _vars(db, "plat-Y")["injected_memory_ids"] == ["fresh"]


@pytest.mark.parametrize("parent_turn_seq", [None, "5"])
def test_fail_closed_when_parent_turn_seq_missing(
    engine: RuleEngine,
    db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
    parent_turn_seq: Any,
) -> None:
    variables = {"memory_recall_helper_enabled": True}
    if parent_turn_seq is not None:
        variables["parent_turn_seq"] = parent_turn_seq

    result = engine._format_delivery_result(
        {
            "messages": [
                _memory_recall_message([_memory("unverified")]),
                _plain_message("plain-msg-unverified-sentinel"),
            ],
            "count": 2,
        },
        "plat-Y",
        variables,
    )

    assert result is not None
    assert "content-sentinel-unverified" not in result
    assert "plain-msg-unverified-sentinel" in result
    assert "injected_memory_ids" not in _vars(db, "plat-Y")
    assert "parent_turn_seq missing or non-int" in caplog.text


def test_kill_switch_drops_memory_recall_payloads(engine: RuleEngine, db: HubDatabase) -> None:
    result = engine._format_delivery_result(
        {
            "messages": [
                _memory_recall_message([_memory("disabled")]),
                _plain_message("plain-msg-disabled-sentinel"),
            ],
            "count": 2,
        },
        "plat-Y",
        _variables(memory_recall_helper_enabled=False),
    )

    assert result is not None
    assert "content-sentinel-disabled" not in result
    assert "plain-msg-disabled-sentinel" in result
    assert "injected_memory_ids" not in _vars(db, "plat-Y")


def test_search_memories_formatter_dedup(engine: RuleEngine, db: HubDatabase) -> None:
    first = engine._format_search_memories_result(
        {"memories": [_memory("m1")]},
        "plat-Y",
        _variables(),
    )
    assert first is not None
    assert "content-sentinel-m1" in first
    assert _vars(db, "plat-Y")["injected_memory_ids"] == ["m1"]

    second = engine._format_search_memories_result(
        {"memories": [_memory("m1"), _memory("m2")]},
        "plat-Y",
        _variables(),
    )

    assert second is not None
    assert "content-sentinel-m1" not in second
    assert "content-sentinel-m2" in second
    assert _vars(db, "plat-Y")["injected_memory_ids"] == ["m1", "m2"]
    assert engine._format_search_memories_result({"memories": []}, "plat-Y", _variables()) is None


def test_hard_cap_truncates_excess_helper_memories(
    engine: RuleEngine,
    db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="gobby.workflows.engine.effects")
    result = engine._format_delivery_result(
        {
            "messages": [
                _memory_recall_message([_memory(f"m{i}") for i in range(1, 6)]),
            ],
            "count": 1,
        },
        "plat-Y",
        _variables(),
    )

    assert result is not None
    assert "content-sentinel-m1" in result
    assert "content-sentinel-m2" in result
    assert "content-sentinel-m3" in result
    assert "content-sentinel-m4" not in result
    assert "content-sentinel-m5" not in result
    assert _vars(db, "plat-Y")["injected_memory_ids"] == ["m1", "m2", "m3"]
    assert "Capping recall memories from 5 to 3" in caplog.text


def test_cap_applies_after_merging_all_memory_recall_messages(
    engine: RuleEngine,
    db: HubDatabase,
) -> None:
    result = engine._format_delivery_result(
        {
            "messages": [
                _memory_recall_message([_memory(f"m{i}") for i in range(1, 4)]),
                _memory_recall_message([_memory(f"m{i}") for i in range(4, 7)]),
            ],
            "count": 2,
        },
        "plat-Y",
        _variables(),
    )

    assert result is not None
    assert "content-sentinel-m1" in result
    assert "content-sentinel-m2" in result
    assert "content-sentinel-m3" in result
    assert "content-sentinel-m4" not in result
    assert "content-sentinel-m5" not in result
    assert "content-sentinel-m6" not in result
    assert _vars(db, "plat-Y")["injected_memory_ids"] == ["m1", "m2", "m3"]


@pytest.mark.asyncio
async def test_apply_effect_dispatch_switch_cancel_stale_helpers_no_op(
    db: HubDatabase,
) -> None:
    async def dispatcher(server: str, tool: str, args: dict[str, Any], event: Any) -> dict:
        return {"success": True, "result": {"success": True, "cancelled": 2, "count": 2}}

    manager = LocalWorkflowDefinitionManager(db)
    _insert_rule(
        manager,
        "cancel-stale-inline",
        RuleEffect(
            type="mcp_call",
            server="gobby-agents",
            tool="cancel_stale_helpers",
            arguments={},
            inject_result=True,
        ),
    )
    engine = RuleEngine(db, mcp_dispatcher=dispatcher)

    response = await engine.evaluate(_event(), session_id="ignored", variables={})

    assert response.context is None
    assert response.metadata.get("mcp_calls", []) == []
