"""Turn-end stop-gate suppression for provider-recorded user interrupts."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.found_work_gate import FoundWorkStopFacts
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
EXTERNAL_SESSION_ID = "22222222-2222-4222-8222-222222222222"
PROJECT_ID = "33333333-3333-4333-8333-333333333333"
MACHINE_ID = "21000000-0000-4000-8000-000000000001"
STOP_GATE_NAMES = (
    "review-gobby-session-feedback-on-stop",
    "require-task-close",
    "require-epic-tree-close",
)
_STOP_GATE_WHEN = (
    "variables.get('mode_level', 2) >= 1 "
    "and variables.get('task_claimed') "
    "and not has_active_agent_wait()"
)


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    return temp_db


def _create_session(db: HubDatabase) -> None:
    db.execute(
        """
        INSERT INTO projects (id, name, created_at)
        VALUES (%s, %s, CURRENT_TIMESTAMP)
        """,
        (PROJECT_ID, "interrupt-stop-gates"),
    )
    db.execute(
        """
        INSERT INTO sessions
            (id, external_id, machine_id, source, project_id, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (SESSION_ID, EXTERNAL_SESSION_ID, MACHINE_ID, "claude", PROJECT_ID),
    )


def _insert_turn_end_rules(db: HubDatabase) -> None:
    manager = RuleDefinitionManager(db)
    manager.create(
        name="observe-interrupt-fact",
        definition_json=RuleDefinitionBody(
            event=RuleTriggerEvent.TURN_END,
            when="variables.get('turn_interrupt_initiated')",
            effects=[
                RuleEffect(
                    type="set_variable",
                    variable="interrupt_condition_seen",
                    value=True,
                )
            ],
        ).model_dump(mode="json"),
        priority=1,
    )
    for priority, rule_name in enumerate(STOP_GATE_NAMES, start=10):
        effects = [RuleEffect(type="block", reason=f"{rule_name} blocked turn end")]
        if rule_name == "require-task-close":
            effects.insert(
                0,
                RuleEffect(
                    type="set_variable",
                    variable="non_block_effect_applied",
                    value=True,
                ),
            )
        manager.create(
            name=rule_name,
            definition_json=RuleDefinitionBody(
                event=RuleTriggerEvent.TURN_END,
                when=_STOP_GATE_WHEN,
                effects=effects,
            ).model_dump(mode="json"),
            priority=priority,
        )


def _claude_record(role: str, content: str) -> dict[str, object]:
    return {"type": role, "message": {"role": role, "content": content}}


def _write_interrupt_turn(transcript: Path) -> None:
    records = [
        _claude_record("user", "[Request interrupted by user]"),
        _claude_record("user", "hey had a question"),
        _claude_record("assistant", "go ahead"),
    ]
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )


def _append_ordinary_turn(transcript: Path) -> None:
    records = [
        _claude_record("user", "continue"),
        _claude_record("assistant", "ordinary response"),
    ]
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write("".join(f"{json.dumps(record)}\n" for record in records))


def _prepare_handler(db: HubDatabase, project_path: Path) -> WorkflowHookHandler:
    _create_session(db)
    _insert_turn_end_rules(db)
    SessionVariableManager(db).merge_variables(
        SESSION_ID,
        {
            "_agent_type": "default",
            "_variable_defaults_loaded": True,
            "baseline_dirty_files": [],
            "claimed_tasks": {"44444444-4444-4444-8444-444444444444": "#42"},
            "mode_level": 1,
            "session_edited_files": [],
            "stop_attempts": 0,
            "task_claimed": True,
        },
    )
    return WorkflowHookHandler(rule_engine=RuleEngine(db))


def _stop_event(transcript: Path, project_path: Path) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.STOP,
        session_id=EXTERNAL_SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        cwd=str(project_path),
        project_id=PROJECT_ID,
        data={"transcript_path": str(transcript)},
        metadata={
            "_platform_session_id": SESSION_ID,
            "project_path": str(project_path),
        },
    )


async def _evaluate(handler: WorkflowHookHandler, event: HookEvent) -> HookResponse:
    with (
        patch.object(
            handler._found_work_analyzer,
            "analyze",
            AsyncMock(return_value=FoundWorkStopFacts()),
        ),
        patch.object(handler._found_work_analyzer, "unclaimed_found_work", return_value=()),
    ):
        return await handler._evaluate_rules(event)


@pytest.mark.asyncio
async def test_interrupt_initiated_turn_suppresses_turn_end_block_effects(
    db: HubDatabase,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    enable_log_propagation: None,
) -> None:
    transcript = tmp_path / "claude.jsonl"
    _write_interrupt_turn(transcript)
    handler = _prepare_handler(db, tmp_path)

    with caplog.at_level(logging.INFO, logger="gobby.workflows.engine.evaluation"):
        response = await _evaluate(handler, _stop_event(transcript, tmp_path))

    assert response.decision == "allow"
    variables = SessionVariableManager(db).get_variables(SESSION_ID)
    assert variables["non_block_effect_applied"] is True
    assert variables["interrupt_condition_seen"] is True
    assert variables["turn_interrupt_initiated"] is True
    audit = db.fetchone(
        """
        SELECT rule_id, result, reason
        FROM workflow_audit_log
        WHERE session_id = %s AND rule_id = %s
        """,
        (SESSION_ID, "interrupt-initiated-turn"),
    )
    assert audit is not None
    assert audit["rule_id"] == "interrupt-initiated-turn"
    assert audit["result"] == "allow"
    assert all(name in audit["reason"] for name in STOP_GATE_NAMES)
    assert any(
        record.levelno == logging.INFO
        and "Suppressed 3 turn_end stop gate(s)" in record.getMessage()
        and all(name in record.getMessage() for name in STOP_GATE_NAMES)
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_ordinary_turn_end_still_blocks(db: HubDatabase, tmp_path: Path) -> None:
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text(
        "".join(
            f"{json.dumps(record)}\n"
            for record in (
                _claude_record("user", "ordinary prompt"),
                _claude_record("assistant", "ordinary response"),
            )
        ),
        encoding="utf-8",
    )
    handler = _prepare_handler(db, tmp_path)

    response = await _evaluate(handler, _stop_event(transcript, tmp_path))

    assert response.decision == "block"
    assert "[aggregated:3-gates]" in (response.reason or "")
    assert all(name in (response.reason or "") for name in STOP_GATE_NAMES)
    variables = SessionVariableManager(db).get_variables(SESSION_ID)
    assert variables["turn_interrupt_initiated"] is False


@pytest.mark.asyncio
async def test_fact_is_recomputed_every_turn_end(db: HubDatabase, tmp_path: Path) -> None:
    transcript = tmp_path / "claude.jsonl"
    _write_interrupt_turn(transcript)
    handler = _prepare_handler(db, tmp_path)
    event = _stop_event(transcript, tmp_path)

    first_response = await _evaluate(handler, event)

    assert first_response.decision == "allow"
    first_variables = SessionVariableManager(db).get_variables(SESSION_ID)
    assert first_variables["interrupt_condition_seen"] is True
    # Simulate an agent-owned set_variable write before the next turn-end.
    SessionVariableManager(db).merge_variables(
        SESSION_ID,
        {
            "interrupt_condition_seen": False,
            "turn_interrupt_initiated": True,
        },
    )
    _append_ordinary_turn(transcript)

    second_response = await _evaluate(handler, event)

    assert second_response.decision == "block"
    second_variables = SessionVariableManager(db).get_variables(SESSION_ID)
    assert second_variables["interrupt_condition_seen"] is False
    assert second_variables["turn_interrupt_initiated"] is False
