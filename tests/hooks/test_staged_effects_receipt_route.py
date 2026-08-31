"""Full-route regression for on_receipt staged effects (#21424).

Drives the real ``review-gobby-session-feedback-before-handoff`` gate over the
production path end to end: an HTTP POST to ``/api/hooks/execute``, the real
``ClaudeCodeAdapter`` and ``HookManager``, ``run_adapter_hook`` on the bounded
adapter executor, rule evaluation on the isolated ``WorkflowEvaluationRuntime``
thread, ``prepare_receipt`` writing ``hook_receipt_effects``, and the real
inbox acknowledgment (``consume_pending_delivery_receipts``) committing the
staged variable.

The staged payload has to cross a thread hop the route does not make obvious:
rules evaluate on the runtime thread, but ``take_worker_staging`` runs on the
adapter-executor thread. The channel between them is a ``threading.local``
(hooks/receipt_effects.py), so before the fix the payload was invisible and
``prepare_receipt`` was handed nothing. ``_evaluate_rules`` also excludes
staged keys from the direct session-variable write, so the acknowledge_variable
was lost on both paths at once and no ``delivery: on_receipt`` gate could ever
clear itself. The second POST here is that gate clearing itself.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from gobby.agents.sync import sync_bundled_agents
from gobby.app_context import ServiceContainer
from gobby.config.bootstrap import BootstrapConfig
from gobby.hooks.envelope_dedupe import ENVELOPE_ID_HEADER
from gobby.hooks.hook_manager import HookManager
from gobby.hooks.inbox import consume_pending_delivery_receipts
from gobby.hooks.receipt_redelivery import DELIVERY_RECEIPT_FIELD
from gobby.hooks.runtime_compat import (
    SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
    SUPPORTED_HOOK_RESPONSE_CAPABILITY,
)
from gobby.servers.http import HTTPServer
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.servers.conftest import authenticate_test_server

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


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    """Pin the machine id so the route resolves the seeded session row."""
    with patch("gobby.utils.machine_id._cached_machine_id", TEST_MACHINE_ID):
        yield


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

    result = sync_bundled_rules(temp_db, get_bundled_rules_path())
    assert result["success"], result["errors"]
    # The session's active-rule set is resolved from its agent definition,
    # so an unsynced agent registry would silently filter every rule away.
    sync_bundled_agents(temp_db)

    # Guard: the whole test is vacuous if the bundled gate is renamed or moved,
    # so pin that the named rule really landed and is live.
    row = RuleDefinitionManager(temp_db).get_by_name(RULE_NAME)
    assert row is not None, f"{RULE_NAME} not synced from {_SESSION_FEEDBACK_RULES}"
    assert row.enabled is True
    return temp_db


@pytest.fixture
def hook_client(receipts_db: HubDatabase) -> Iterator[TestClient]:
    """The daemon's real hook route over a real HookManager on the test hub."""
    sessions = SessionManager(receipts_db)
    server = authenticate_test_server(
        HTTPServer(
            services=ServiceContainer(
                database=receipts_db,
                session_manager=sessions,
                task_manager=MagicMock(),
            ),
            port=60887,
            test_mode=True,
            bootstrap_config=BootstrapConfig(),
        )
    )
    # Preconfigured managers are reused by the lifespan instead of being rebuilt,
    # which is what keeps this HookManager pointed at the test hub.
    manager = HookManager(database=receipts_db, session_manager=sessions)
    server.app.state.hook_manager = manager
    try:
        with TestClient(server.app) as client:
            yield client
    finally:
        manager.shutdown()


def _create_session(db: HubDatabase, session_id: str) -> None:
    """session_variables carries an FK to sessions, which needs a project."""
    project_id = str(uuid4())
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)",
            (project_id, f"staged-effects-{project_id}"),
        )
        conn.execute(
            "INSERT INTO sessions (id, external_id, machine_id, source, project_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (session_id, session_id, TEST_MACHINE_ID, "claude", project_id),
        )


def _post_set_handoff(client: TestClient, session_id: str, envelope_id: str) -> dict[str, Any]:
    """One real PreToolUse delivery for gobby-sessions:set_handoff."""
    response = client.post(
        "/api/hooks/execute",
        headers={ENVELOPE_ID_HEADER: envelope_id},
        json={
            "schema_version": SUPPORTED_HOOK_ENVELOPE_SCHEMA_VERSION,
            "enqueued_at": "2026-04-16T12:00:00Z",
            "critical": False,
            "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
            "hook_type": "PreToolUse",
            "source": "claude",
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
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _blocked(body: dict[str, Any]) -> bool:
    """Whether Claude was told to deny the tool call."""
    decision = body.get("hookSpecificOutput")
    if isinstance(decision, dict) and decision.get("permissionDecision") == "deny":
        return True
    return body.get("decision") == "block"


def _staged_variables(db: HubDatabase, receipt_id: str) -> dict[str, Any]:
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT staged_payload FROM hook_receipt_effects WHERE receipt_id = %s",
            (receipt_id,),
        ).fetchone()
    assert row is not None, "prepare_receipt wrote no receipt row"
    payload = row["staged_payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert isinstance(payload, dict)
    variables = payload.get("session_variables")
    return variables if isinstance(variables, dict) else {}


def _write_ack(inbox_dir: Path, receipt: dict[str, Any]) -> None:
    """Write the ack file ghook drops for a delivered receipt."""
    inbox_dir.mkdir(parents=True, exist_ok=True)
    (inbox_dir / f"{uuid4()}.json").write_text(
        json.dumps(
            {
                "kind": "delivery-receipt",
                "receipt_id": receipt["receipt_id"],
                "delivery_generation": receipt["delivery_generation"],
            }
        ),
        encoding="utf-8",
    )


def test_delivered_gate_clears_itself_once_its_receipt_is_acknowledged(
    receipts_db: HubDatabase,
    hook_client: TestClient,
    tmp_path: Path,
) -> None:
    session_id = str(uuid4())
    _create_session(receipts_db, session_id)
    variables = SessionVariableManager(receipts_db)
    variables.merge_variables(
        session_id,
        {"_gobby_feedback_survey_active": True, "task_claimed": True},
    )

    body = _post_set_handoff(hook_client, session_id, f"n-{uuid4()}")

    assert _blocked(body), body
    receipt = body.get(DELIVERY_RECEIPT_FIELD)
    assert isinstance(receipt, dict), f"route attached no delivery receipt: {body}"

    # The acknowledge_variable crossed the adapter-executor boundary and reached
    # prepare_receipt. Before the fix this row's staged_payload was empty.
    assert _staged_variables(receipts_db, receipt["receipt_id"]) == {ACK_VARIABLE: True}

    # delivery: on_receipt defers the write, so nothing has touched
    # session_variables yet - whatever the second delivery sees came from the ack.
    assert variables.get_variables(session_id).get(ACK_VARIABLE) is None

    inbox = tmp_path / "inbox"
    _write_ack(inbox, receipt)
    assert consume_pending_delivery_receipts(hook_client.app, inbox) == 1
    assert variables.get_variables(session_id).get(ACK_VARIABLE) is True

    # A second real delivery of the same hook: the gate is satisfied and no
    # longer fires. This is the behavior the fix restores.
    second = _post_set_handoff(hook_client, session_id, f"n-{uuid4()}")
    assert not _blocked(second), second

    # And it stages the acknowledge_variable only once. The first delivery's
    # staging used to survive on the shared runtime thread and be re-staged
    # here (#21427).
    second_receipt = second.get(DELIVERY_RECEIPT_FIELD)
    assert isinstance(second_receipt, dict)
    assert _staged_variables(receipts_db, second_receipt["receipt_id"]) == {}


def test_one_sessions_staged_gate_never_reaches_another_session(
    receipts_db: HubDatabase,
    hook_client: TestClient,
) -> None:
    """Staged effects stay inside the delivery that produced them (#21427).

    Both deliveries run through one HookManager, so they share the workflow
    runtime thread and its rule-engine executor. While staging lived in a
    thread-local those shared threads accumulated it, and the next delivery to
    land there copied the previous session's variables onto its own response
    under its own session id — committing them on acknowledgment.
    """
    variables = SessionVariableManager(receipts_db)

    blocked_session = str(uuid4())
    _create_session(receipts_db, blocked_session)
    variables.merge_variables(
        blocked_session,
        {"_gobby_feedback_survey_active": True, "task_claimed": True},
    )

    # This one already acknowledged the survey, so the gate cannot fire for it
    # and it must stage nothing of its own. (The survey-active flag itself is
    # re-injected per event from daemon config, so a session variable cannot
    # switch the gate off — see engine.core.inject_survey_active.)
    quiet_session = str(uuid4())
    _create_session(receipts_db, quiet_session)
    variables.merge_variables(
        quiet_session,
        {"task_claimed": True, ACK_VARIABLE: True},
    )

    first = _post_set_handoff(hook_client, blocked_session, f"n-{uuid4()}")
    assert _blocked(first), first
    first_receipt = first.get(DELIVERY_RECEIPT_FIELD)
    assert isinstance(first_receipt, dict)
    assert _staged_variables(receipts_db, first_receipt["receipt_id"]) == {ACK_VARIABLE: True}

    second = _post_set_handoff(hook_client, quiet_session, f"n-{uuid4()}")
    assert not _blocked(second), second
    second_receipt = second.get(DELIVERY_RECEIPT_FIELD)
    assert isinstance(second_receipt, dict)

    # This is where the borrowed payload used to land: re-attributed to this
    # session id and ready to commit on acknowledgment.
    assert _staged_variables(receipts_db, second_receipt["receipt_id"]) == {}


def test_route_stages_nothing_when_the_gate_does_not_fire(
    receipts_db: HubDatabase,
    hook_client: TestClient,
) -> None:
    """A pass-through delivery must not invent staged variables."""
    session_id = str(uuid4())
    _create_session(receipts_db, session_id)
    SessionVariableManager(receipts_db).merge_variables(
        session_id,
        {
            "_gobby_feedback_survey_active": True,
            "task_claimed": True,
            ACK_VARIABLE: True,
        },
    )

    body = _post_set_handoff(hook_client, session_id, f"n-{uuid4()}")

    assert not _blocked(body), body
    receipt = body.get(DELIVERY_RECEIPT_FIELD)
    assert isinstance(receipt, dict)
    assert ACK_VARIABLE not in _staged_variables(receipts_db, receipt["receipt_id"])
