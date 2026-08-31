"""Full-route regression for on_receipt staged effects (#21424).

Covers the whole seam, not just the engine: the staged payload is produced on
the isolated ``WorkflowEvaluationRuntime`` thread, consumed by
``take_worker_staging`` on the adapter-executor thread exactly as
``adapter_execution.run_adapter`` does, written into ``hook_receipt_effects`` by
``attach_delivery_receipt``, and applied to ``session_variables`` when the
receipt is acknowledged.

Before the fix the payload never crossed the thread hop, so the receipt was
prepared without it and the acknowledge_variable was lost on both the staged and
direct-write paths at once, leaving every ``delivery: on_receipt`` gate unable to
clear itself.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.receipt_effects import (
    STAGED_EFFECTS_FIELD,
    apply_acknowledged_receipt,
    take_worker_staging,
)
from gobby.hooks.receipt_redelivery import attach_delivery_receipt
from gobby.storage.hook_receipts import acknowledge_receipt
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

_RECEIPT_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "crates/gcore/assets/schema/migrations/416_hook_receipt_effects.sql"
)

ACK_VARIABLE = "_gobby_feedback_epoch_reviewed"

# Pre-seeded by the postgres fixture; sessions.machine_id carries an FK.
TEST_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture
def receipts_db(temp_db: HubDatabase) -> HubDatabase:
    """temp_db with the hook-receipt table applied."""
    sql = "\n".join(
        line
        for line in _RECEIPT_MIGRATION.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--")
    )
    with temp_db.transaction() as conn:
        for statement in (part.strip() for part in sql.split(";")):
            if statement:
                conn.execute(statement)
    return temp_db


def _create_session(db: HubDatabase, session_id: str) -> None:
    """session_variables carries an FK to sessions, which needs a project."""
    project_id = str(uuid4())
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)",
            (project_id, "staged-effects"),
        )
        conn.execute(
            "INSERT INTO sessions (id, external_id, machine_id, source, project_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (session_id, session_id, TEST_MACHINE_ID, "claude", project_id),
        )


def _block_with_staged_ack(session_id: str) -> Any:
    """Stand in for a delivered gate carrying an on_receipt acknowledge_variable."""

    async def fake_evaluate_rules(
        event: HookEvent,
        *,
        blocking_deadline: BlockingEffectDeadline | None = None,
    ) -> HookResponse:
        return HookResponse(
            decision="block",
            reason="survey required",
            metadata={
                STAGED_EFFECTS_FIELD: {
                    "session_id": session_id,
                    "session_variables": {ACK_VARIABLE: True},
                }
            },
        )

    return fake_evaluate_rules


def _staged_variables(db: HubDatabase, receipt_id: str) -> dict[str, Any]:
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT staged_payload FROM hook_receipt_effects WHERE receipt_id = %s",
            (receipt_id,),
        ).fetchone()
    assert row is not None, "no receipt row was prepared"
    payload = row["staged_payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert isinstance(payload, dict)
    variables = payload.get("session_variables")
    return variables if isinstance(variables, dict) else {}


def test_on_receipt_ack_reaches_the_db_and_persists_on_acknowledgment(
    receipts_db: HubDatabase,
) -> None:
    session_id = str(uuid4())
    _create_session(receipts_db, session_id)
    envelope_id = f"n-{uuid4()}"
    handler = WorkflowHookHandler(evaluation_runtime=WorkflowEvaluationRuntime())
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=session_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"tool_name": "mcp__gobby__call_tool"},
        metadata={"_platform_session_id": session_id},
    )

    def adapter_thread() -> dict[str, Any]:
        """Mirror adapter_execution.run_adapter: clear, evaluate, take, attach."""
        take_worker_staging()
        with patch.object(handler, "_evaluate_rules", new=_block_with_staged_ack(session_id)):
            response = handler.evaluate(event)
        assert response.decision == "block"
        staged = take_worker_staging()
        return attach_delivery_receipt(
            {"continue": True},
            db=receipts_db,
            envelope_id=envelope_id,
            session_id=session_id,
            staged_payload=staged or None,
        )

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(adapter_thread).result(timeout=30)
    finally:
        handler.shutdown()

    with receipts_db.transaction() as conn:
        row = conn.execute(
            "SELECT receipt_id, delivery_generation FROM hook_receipt_effects "
            "WHERE current_envelope_id = %s",
            (envelope_id,),
        ).fetchone()
    assert row is not None, "attach_delivery_receipt prepared no receipt"
    receipt_id = row["receipt_id"]
    generation = row["delivery_generation"]

    # The staged payload reached the database, not just the in-memory response.
    assert _staged_variables(receipts_db, receipt_id) == {ACK_VARIABLE: True}

    receipt = acknowledge_receipt(
        receipts_db, receipt_id=receipt_id, delivery_generation=generation
    )
    assert receipt is not None

    variables = SessionVariableManager(receipts_db)
    apply_acknowledged_receipt(receipt, variable_manager=variables)

    stored = variables.get_variables(session_id)
    assert stored.get(ACK_VARIABLE) is True

    # The gate's own guard (`not variables.get(...)`) is now false, so a second
    # delivery would not fire: the gate can finally clear itself.
    assert not (not stored.get(ACK_VARIABLE))


def test_receipt_carries_no_variables_when_nothing_is_staged(
    receipts_db: HubDatabase,
) -> None:
    """A pass-through evaluation must not invent staged variables."""
    session_id = str(uuid4())
    envelope_id = f"n-{uuid4()}"
    handler = WorkflowHookHandler(evaluation_runtime=WorkflowEvaluationRuntime())
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=session_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={},
        metadata={"_platform_session_id": session_id},
    )

    async def allow(
        event: HookEvent,
        *,
        blocking_deadline: BlockingEffectDeadline | None = None,
    ) -> HookResponse:
        return HookResponse(decision="allow")

    def adapter_thread() -> None:
        take_worker_staging()
        with patch.object(handler, "_evaluate_rules", new=allow):
            handler.evaluate(event)
        staged = take_worker_staging()
        attach_delivery_receipt(
            {"continue": True},
            db=receipts_db,
            envelope_id=envelope_id,
            session_id=session_id,
            staged_payload=staged or None,
        )

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(adapter_thread).result(timeout=30)
    finally:
        handler.shutdown()

    with receipts_db.transaction() as conn:
        row = conn.execute(
            "SELECT receipt_id FROM hook_receipt_effects WHERE current_envelope_id = %s",
            (envelope_id,),
        ).fetchone()
    assert row is not None
    receipt_id = row["receipt_id"]
    assert _staged_variables(receipts_db, receipt_id) == {}
