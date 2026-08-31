"""Full-route regression for on_receipt staged effects (#21424).

Drives the real ``review-gobby-session-feedback-before-handoff`` gate — the
bundled rule body, a real ``RuleEngine``, the real ``WorkflowHookHandler`` —
across the whole seam: the staged payload is produced on the isolated
``WorkflowEvaluationRuntime`` thread, consumed by ``take_worker_staging`` on
the adapter-executor thread exactly as ``adapter_execution.run_adapter`` does,
written into ``hook_receipt_effects`` by ``attach_delivery_receipt``, and
applied to ``session_variables`` when the receipt is acknowledged.

Before the fix the payload never crossed the thread hop, so the receipt was
prepared without it and the acknowledge_variable was lost on both the staged
and the direct-write paths at once. The gate could never clear itself, which
is what the second evaluation here proves it now does.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.hooks.events import HookEvent, HookResponse
from gobby.hooks.receipt_effects import apply_acknowledged_receipt, take_worker_staging
from gobby.hooks.receipt_redelivery import attach_delivery_receipt
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hook_receipts import acknowledge_receipt
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.blocked_tool_recovery import extract_rule_name
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import sync_rule_file

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
_RECEIPT_MIGRATION = (
    REPO_ROOT / "crates/gcore/assets/schema/migrations/416_hook_receipt_effects.sql"
)
_SESSION_FEEDBACK_RULES = (
    REPO_ROOT / "src/gobby/install/shared/workflows/rules/session-feedback/session-feedback.yaml"
)

RULE_NAME = "review-gobby-session-feedback-before-handoff"
ACK_VARIABLE = "_gobby_feedback_epoch_reviewed"

# Pre-seeded by the postgres fixture; sessions.machine_id carries an FK.
TEST_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture
def receipts_db(temp_db: HubDatabase) -> HubDatabase:
    """temp_db with the hook-receipt table applied and the real gate installed."""
    sql = "\n".join(
        line
        for line in _RECEIPT_MIGRATION.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--")
    )
    with temp_db.transaction() as conn:
        for statement in (part.strip() for part in sql.split(";")):
            if statement:
                conn.execute(statement)

    result = sync_rule_file(temp_db, _SESSION_FEEDBACK_RULES, tag="gobby")
    assert result["success"], result["errors"]

    # Guard: the whole test is vacuous if the bundled gate is renamed or moved,
    # so pin that the named rule really landed and is live.
    row = RuleDefinitionManager(temp_db).get_by_name(RULE_NAME)
    assert row is not None, f"{RULE_NAME} not synced from {_SESSION_FEEDBACK_RULES}"
    assert row.enabled is True
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


def _handoff_event(session_id: str) -> HookEvent:
    """A real before_tool event for gobby-sessions:set_handoff, adapter-translated.

    Built fresh per delivery: evaluation normalizes and annotates event.data in
    place, so a reused instance would not be a second independent delivery.
    """
    event = ClaudeCodeAdapter().translate_to_hook_event(
        {
            "hook_type": "PreToolUse",
            "input_data": {
                "hook_event_name": "PreToolUse",
                "session_id": session_id,
                "cwd": str(REPO_ROOT),
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-sessions",
                    "tool_name": "set_handoff",
                    "arguments": {"current_state": "x"},
                },
            },
        }
    )
    event.metadata["_platform_session_id"] = session_id
    return event


def _deliver(handler: WorkflowHookHandler, event: HookEvent) -> tuple[HookResponse, dict[str, Any]]:
    """Evaluate on an adapter thread, mirroring adapter_execution.run_adapter.

    The handler hops to its own "gobby-workflow-runtime" thread internally;
    staging is taken here, on the thread the real adapter takes it from.
    """

    def run() -> tuple[HookResponse, dict[str, Any]]:
        take_worker_staging()
        response = handler.evaluate(event)
        return response, take_worker_staging()

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(run).result(timeout=60)


def _handler(db: HubDatabase) -> WorkflowHookHandler:
    return WorkflowHookHandler(
        rule_engine=RuleEngine(db),
        evaluation_runtime=WorkflowEvaluationRuntime(),
    )


def _receipt(db: HubDatabase, envelope_id: str) -> tuple[str, int]:
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT receipt_id, delivery_generation FROM hook_receipt_effects "
            "WHERE current_envelope_id = %s",
            (envelope_id,),
        ).fetchone()
    assert row is not None, "attach_delivery_receipt prepared no receipt"
    return str(row["receipt_id"]), int(row["delivery_generation"])


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


def test_delivered_gate_clears_itself_once_its_receipt_is_acknowledged(
    receipts_db: HubDatabase,
) -> None:
    session_id = str(uuid4())
    _create_session(receipts_db, session_id)
    variables = SessionVariableManager(receipts_db)
    variables.merge_variables(
        session_id,
        {"_gobby_feedback_survey_active": True, "task_claimed": True},
    )
    envelope_id = f"n-{uuid4()}"
    handler = _handler(receipts_db)

    try:
        response, staged = _deliver(handler, _handoff_event(session_id))

        assert response.decision == "block"
        assert extract_rule_name(response.reason) == RULE_NAME

        # The acknowledge_variable survived the hop to the consuming thread.
        assert staged.get("session_variables") == {ACK_VARIABLE: True}

        attach_delivery_receipt(
            {"continue": True},
            db=receipts_db,
            envelope_id=envelope_id,
            session_id=session_id,
            staged_payload=staged or None,
        )
        receipt_id, generation = _receipt(receipts_db, envelope_id)
        assert _staged_variables(receipts_db, receipt_id) == {ACK_VARIABLE: True}

        # delivery: on_receipt defers the write, so the variable is still unset
        # here - whatever the second evaluation sees came from the ack below.
        assert variables.get_variables(session_id).get(ACK_VARIABLE) is None

        receipt = acknowledge_receipt(
            receipts_db, receipt_id=receipt_id, delivery_generation=generation
        )
        assert receipt is not None
        apply_acknowledged_receipt(receipt, variable_manager=variables)
        assert variables.get_variables(session_id).get(ACK_VARIABLE) is True

        # A second real evaluation of the same event: the gate is satisfied and
        # no longer fires. This is the behavior the fix restores.
        # Its staged payload is deliberately not asserted here - the runtime
        # thread retains stale staging across evaluations, which is #21427.
        second, _ = _deliver(handler, _handoff_event(session_id))
        assert second.decision == "allow", second.reason
    finally:
        handler.shutdown()


def test_receipt_carries_no_variables_when_the_gate_does_not_fire(
    receipts_db: HubDatabase,
) -> None:
    """A pass-through evaluation must not invent staged variables."""
    session_id = str(uuid4())
    _create_session(receipts_db, session_id)
    variables = SessionVariableManager(receipts_db)
    variables.merge_variables(
        session_id,
        {
            "_gobby_feedback_survey_active": True,
            "task_claimed": True,
            ACK_VARIABLE: True,
        },
    )
    envelope_id = f"n-{uuid4()}"
    handler = _handler(receipts_db)

    try:
        response, staged = _deliver(handler, _handoff_event(session_id))
        assert response.decision == "allow"
        assert staged.get("session_variables") is None
        attach_delivery_receipt(
            {"continue": True},
            db=receipts_db,
            envelope_id=envelope_id,
            session_id=session_id,
            staged_payload=staged or None,
        )
    finally:
        handler.shutdown()

    receipt_id, _ = _receipt(receipts_db, envelope_id)
    assert _staged_variables(receipts_db, receipt_id) == {}
