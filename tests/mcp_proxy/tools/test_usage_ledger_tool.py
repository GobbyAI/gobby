"""get_usage_ledger tool and GET /usage/ledger route: scope, cursor errors and parity (plan 3.1, #23601)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from gobby.mcp_proxy.tools.metrics import create_metrics_registry
from gobby.servers.routes.admin._usage import register_usage_routes
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.token_events import TokenEvent, TokenEventStore
from gobby.utils.machine_id import get_machine_id
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.integration

BASE = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
PROJECT_CONTEXT = "gobby.utils.project_context.get_project_context"


def _session(session_manager: SessionManager, project_id: str, name: str) -> str:
    machine_id = get_machine_id()
    assert machine_id is not None
    LocalMachineManager(session_manager.db).upsert_seen(machine_id, TEST_USER_ID)
    return session_manager.register(
        external_id=f"ledger-tool-{name}",
        machine_id=machine_id,
        source="claude",
        project_id=project_id,
    ).id


def _ledger_tool(session_manager: SessionManager) -> Any:
    registry = create_metrics_registry(MagicMock(), session_storage=session_manager)
    return registry._tools["get_usage_ledger"]


def test_ledger_tool_and_route_scope_and_errors(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    project_id = sample_project["id"]
    session_id = _session(session_manager, project_id, "scope")
    store = TokenEventStore(temp_db)
    for minute, message_id in ((1, "msg-1"), (2, "msg-2")):
        store.record(
            TokenEvent(
                session_id=session_id,
                project_id=project_id,
                message_id=message_id,
                source="claude",
                origin="transcript",
                model=None,
                input_tokens=10,
                output_tokens=5,
                cache_creation_tokens=0,
                cache_read_tokens=0,
                event_at=datetime(2026, 10, 20, 10, minute, tzinfo=UTC),
                api_calls=1,
            )
        )
    task = LocalTaskManager(temp_db).create_task(
        project_id, "Ledger scope", validation_criteria="Scope follows."
    )
    temp_db.execute(
        """
        INSERT INTO task_claim_intervals (task_id, session_id, claimed_at, released_at)
        VALUES (%s, %s, %s, %s)
        """,
        # Closed, so claimed_ms does not move with the clock between the two reads below.
        (task.id, session_id, BASE, datetime(2026, 10, 20, 11, 0, tzinfo=UTC)),
    )
    other_project = LocalProjectManager(temp_db).create("ledger-other")
    foreign_session = _session(session_manager, other_project.id, "foreign")

    ledger = _ledger_tool(session_manager).func
    with patch(PROJECT_CONTEXT, return_value={"id": project_id}):
        by_ref = ledger(task_id=f"#{task.seq_num}")
        by_uuid = ledger(task_id=task.id)
        malformed = ledger(task_id=f"#{task.seq_num}", cursor="%%%not-base64")
        refused = ledger(session_id=foreign_session)

    assert by_ref["success"] is True
    assert by_ref["totals"]["rows"] == 2
    assert by_ref == {**by_uuid, "success": True}
    assert malformed["success"] is False
    assert refused["success"] is False

    server = MagicMock()
    server.services.database = temp_db
    router = APIRouter()
    register_usage_routes(router, server)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get("/usage/ledger", params={"task_id": task.id})
    assert response.status_code == 200
    assert response.json() == {k: v for k, v in by_uuid.items() if k != "success"}

    session_response = client.get("/usage/ledger", params={"session_id": session_id})
    assert session_response.status_code == 200
    assert session_response.json()["totals"]["rows"] == 2

    cursor_response = client.get(
        "/usage/ledger", params={"task_id": task.id, "cursor": "%%%not-base64"}
    )
    assert cursor_response.status_code == 400
    assert client.get("/usage/ledger").status_code == 400
    both = {"task_id": task.id, "session_id": session_id}
    assert client.get("/usage/ledger", params=both).status_code == 400
