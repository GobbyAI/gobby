"""Block-only workflow audit coverage for RuleEngine finalization."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows import block_audit
from gobby.workflows.block_audit import audit_source_block_sync, combined_rule_condition
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.engine.evaluation import BlockGate, EvaluationContext

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

SESSION_ID = "11111111-1111-1111-1111-111111111111"


def _evaluation() -> EvaluationContext:
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="external-session",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={"tool_name": "mcp__gobby-tasks__close_task"},
    )
    return EvaluationContext(
        event=event,
        session_id=SESSION_ID,
        variables={"current_step": " review "},
        eval_context=None,
        is_before_tool=True,
        block_tool_name="gobby-tasks:close_task",
    )


def _engine(db: HubDatabase) -> RuleEngine:
    engine = RuleEngine(db)
    audit = cast(MagicMock, engine.workflow_audit)
    audit.log_rule_eval = MagicMock(return_value=1)
    return engine


def _span() -> MagicMock:
    span = MagicMock()
    span.is_recording.return_value = False
    return span


def _audit_mock(engine: RuleEngine) -> MagicMock:
    return cast(MagicMock, engine.workflow_audit.log_rule_eval)


async def test_finalize_audits_every_block_gate(temp_db: HubDatabase) -> None:
    engine = _engine(temp_db)
    audit = _audit_mock(engine)
    response = HookResponse(
        decision="block",
        reason="Rule enforced by Gobby: [rule-one]\nTwo rules blocked this tool.",
    )
    gates = [
        BlockGate(rule_name="rule-one", reason="first reason", condition="vars.first"),
        BlockGate(rule_name="rule-two", reason="second reason", condition="vars.second"),
    ]

    # The audit offload lives in `block_audit.log_enforcement_block`; the
    # evaluation module delegates to it and no longer touches `asyncio` itself.
    with patch(
        "gobby.workflows.block_audit.asyncio.to_thread",
        wraps=asyncio.to_thread,
    ) as to_thread:
        result = await engine._finalize_block_response(
            response,
            _evaluation(),
            _span(),
            block_gates=gates,
        )

    assert result is response
    assert audit.call_count == 2
    assert [entry.kwargs for entry in audit.call_args_list] == [
        {
            "session_id": SESSION_ID,
            "step": "review",
            "rule_id": "rule-one",
            "condition": json.dumps("vars.first"),
            "result": "block",
            "reason": "first reason",
            "tool_name": "gobby-tasks:close_task",
        },
        {
            "session_id": SESSION_ID,
            "step": "review",
            "rule_id": "rule-two",
            "condition": json.dumps("vars.second"),
            "result": "block",
            "reason": "second reason",
            "tool_name": "gobby-tasks:close_task",
        },
    ]
    assert any(entry.args and entry.args[0] is audit for entry in to_thread.await_args_list)


async def test_override_adds_synthetic_row_beside_gate(temp_db: HubDatabase) -> None:
    engine = _engine(temp_db)
    audit = _audit_mock(engine)
    response = HookResponse(
        decision="block",
        reason="Rule enforced by Gobby: [tool-failure-recovery]\nRecover first.",
    )

    await engine._finalize_block_response(
        response,
        _evaluation(),
        _span(),
        block_gates=[BlockGate(rule_name="installed-gate", reason="gate reason", condition=None)],
    )

    assert audit.call_count == 2
    synthetic = audit.call_args_list[1].kwargs
    assert synthetic["rule_id"] == "tool-failure-recovery"
    assert synthetic["condition"] == "-"
    assert synthetic["reason"] == response.reason


async def test_synthetic_fallback_truncates_audit_reason(temp_db: HubDatabase) -> None:
    engine = _engine(temp_db)
    audit = _audit_mock(engine)
    evaluation = _evaluation()
    evaluation.variables["current_step"] = " "
    evaluation.event.data = {}
    response = HookResponse(
        decision="block",
        reason="Rule enforced by Gobby: [fallback-block]\n" + ("x" * 5_000),
    )

    await engine._finalize_block_response(response, evaluation, _span())

    audit.assert_called_once()
    entry = audit.call_args.kwargs
    assert entry["rule_id"] == "fallback-block"
    assert entry["condition"] == "-"
    assert entry["step"] == "-"
    assert entry["tool_name"] == "-"
    assert len(entry["reason"]) == 4_096


async def test_audit_failure_does_not_change_block_response(
    temp_db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = _engine(temp_db)
    _audit_mock(engine).side_effect = RuntimeError("audit unavailable")
    response = HookResponse(
        decision="block",
        reason="Rule enforced by Gobby: [safe-block]\nStill blocked.",
    )

    with caplog.at_level(logging.WARNING):
        result = await engine._finalize_block_response(response, _evaluation(), _span())

    assert result is response
    assert result.decision == "block"
    assert "audit unavailable" in caplog.text


async def test_allow_response_does_not_write_audit_row(temp_db: HubDatabase) -> None:
    engine = _engine(temp_db)
    audit = _audit_mock(engine)

    result = await engine._finalize_block_response(
        HookResponse(decision="allow"),
        _evaluation(),
        _span(),
    )

    assert result.decision == "allow"
    audit.assert_not_called()


@pytest.mark.parametrize(
    ("rule_when", "effect_when", "expected"),
    [
        (None, None, None),
        ("vars.rule", None, "vars.rule"),
        (None, "vars.effect", "vars.effect"),
        ("vars.same", "vars.same", "vars.same"),
        ("vars.rule", "vars.effect", {"rule": "vars.rule", "effect": "vars.effect"}),
    ],
)
async def test_combined_rule_condition_records_both_levels(
    rule_when: str | None,
    effect_when: str | None,
    expected: object,
) -> None:
    assert combined_rule_condition(rule_when, effect_when) == expected


def _sync_audit_event() -> HookEvent:
    return HookEvent(
        event_type=HookEventType.STOP,
        session_id="external-session",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={},
        metadata={"_platform_session_id": SESSION_ID},
    )


async def test_audit_source_block_sync_lands_on_running_loop(temp_db: HubDatabase) -> None:
    engine = _engine(temp_db)
    handler = SimpleNamespace(rule_engine=engine)

    audit_source_block_sync(
        handler,
        _sync_audit_event(),
        rule_id="workflow-evaluation-cancelled",
        reason="cancelled",
    )

    pending = list(block_audit._background_audit_tasks)
    assert pending
    await asyncio.gather(*pending)
    audit = _audit_mock(engine)
    audit.assert_called_once()
    assert audit.call_args.kwargs["rule_id"] == "workflow-evaluation-cancelled"


async def test_audit_source_block_sync_lands_without_running_loop(
    temp_db: HubDatabase,
) -> None:
    engine = _engine(temp_db)
    handler = SimpleNamespace(rule_engine=engine)

    await asyncio.to_thread(
        lambda: audit_source_block_sync(
            handler,
            _sync_audit_event(),
            rule_id="hook-stop-safety",
            reason="blocked for safety",
        )
    )

    audit = _audit_mock(engine)
    audit.assert_called_once()
    assert audit.call_args.kwargs["rule_id"] == "hook-stop-safety"
