"""An explicit register_session reactivates an expired session it owns (#25647)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.mcp_proxy.tools.sessions import create_session_messages_registry
from gobby.servers.auth_service import AuthService
from gobby.servers.routes.sessions import create_sessions_router
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import session_context_for_test
from tests.fixtures.postgres import TEST_USER_ID
from tests.storage.test_terminals import LOCAL_MACHINE_ID

pytestmark = pytest.mark.integration

EXTERNAL_ID = "01a1a44e-0000-7000-8000-000000025647"


@pytest.fixture(autouse=True)
def _local_machine() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _expired_past_horizon(
    manager: SessionManager, project_id: str, machine_id: str, source: str = "codex"
) -> Session:
    session = manager.register(
        external_id=EXTERNAL_ID,
        machine_id=machine_id,
        source=source,
        project_id=project_id,
        routed_machine_id=machine_id,
    )
    manager.update_status(session.id, "expired")
    aged = datetime.now(UTC) - timedelta(hours=25)
    manager.db.execute(
        "UPDATE sessions SET updated_at = %s, last_activity = %s WHERE id = %s",
        (aged, aged, session.id),
    )
    return session


def _client(manager: SessionManager, tmp_path: Path) -> TestClient:
    key_file = tmp_path / "break_glass"
    key_file.touch(mode=0o600)
    key_file.write_text("test-recovery-value")
    server = MagicMock()
    server.session_manager = manager
    server.auth_service = AuthService(lambda: manager.db, break_glass_file=key_file)
    server.run_db = AsyncMock(side_effect=lambda func, *args, **kwargs: func(*args, **kwargs))
    server.resolve_project_id = lambda project_id, cwd: project_id
    app = FastAPI()
    app.include_router(create_sessions_router(server))
    return TestClient(
        app,
        client=("127.0.0.1", 50000),
        headers={"X-Gobby-Break-Glass": "test-recovery-value"},
    )


def _assert_freshly_active(manager: SessionManager, session_id: str) -> None:
    row = manager.get(session_id)
    assert row is not None
    assert row.status == "active"
    assert row.last_activity is not None
    assert row.last_activity > datetime.now(UTC) - timedelta(minutes=1)


@pytest.mark.parametrize("source", ["codex", "grok"])
async def test_mcp_registration_reactivates_an_expired_session(
    session_manager: SessionManager, sample_project: dict[str, Any], source: str
) -> None:
    session = _expired_past_horizon(
        session_manager, sample_project["id"], LOCAL_MACHINE_ID, source
    )
    registry = create_session_messages_registry(session_manager=session_manager)

    with session_context_for_test(session.id):
        result = await registry.call(
            "register_session",
            {"external_id": EXTERNAL_ID, "source": source, "project_id": sample_project["id"]},
        )

    assert result["session_id"] == session.id, result
    assert result["status"] == "active"
    _assert_freshly_active(session_manager, session.id)


@pytest.mark.parametrize("source", ["codex", "grok"])
def test_rest_registration_reactivates_an_expired_session(
    session_manager: SessionManager, sample_project: dict[str, Any], tmp_path: Path, source: str
) -> None:
    session = _expired_past_horizon(
        session_manager, sample_project["id"], LOCAL_MACHINE_ID, source
    )

    response = _client(session_manager, tmp_path).post(
        "/api/sessions/register",
        json={"external_id": EXTERNAL_ID, "source": source, "project_id": sample_project["id"]},
        headers={"X-Gobby-Session-Id": session.id},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == session.id
    assert body["session_status"] == "active"
    _assert_freshly_active(session_manager, session.id)


def _assert_still_expired(manager: SessionManager, session_id: str) -> None:
    row = manager.get(session_id)
    assert row is not None
    assert row.status == "expired"


@pytest.mark.parametrize("caller", [None, "claude"], ids=["no-caller-session", "other-source"])
async def test_mcp_registration_by_another_caller_leaves_the_session_expired(
    session_manager: SessionManager, sample_project: dict[str, Any], caller: str | None
) -> None:
    session = _expired_past_horizon(session_manager, sample_project["id"], LOCAL_MACHINE_ID)
    registry = create_session_messages_registry(session_manager=session_manager)
    request = {"external_id": EXTERNAL_ID, "source": "codex", "project_id": sample_project["id"]}

    if caller is None:
        result = await registry.call("register_session", request)
    else:
        other = session_manager.register(
            external_id=str(uuid.uuid4()),
            machine_id=LOCAL_MACHINE_ID,
            source=caller,
            project_id=sample_project["id"],
        )
        with session_context_for_test(other.id):
            result = await registry.call("register_session", request)

    assert result["session_id"] == session.id, result
    assert result["status"] == "expired"
    _assert_still_expired(session_manager, session.id)


@pytest.mark.parametrize("caller", [None, "other"], ids=["no-caller-header", "other-session"])
def test_rest_registration_by_another_caller_leaves_the_session_expired(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
    caller: str | None,
) -> None:
    session = _expired_past_horizon(session_manager, sample_project["id"], LOCAL_MACHINE_ID)
    headers: dict[str, str] = {}
    if caller is not None:
        other = session_manager.register(
            external_id=str(uuid.uuid4()),
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=sample_project["id"],
        )
        headers["X-Gobby-Session-Id"] = other.id

    response = _client(session_manager, tmp_path).post(
        "/api/sessions/register",
        json={"external_id": EXTERNAL_ID, "source": "codex", "project_id": sample_project["id"]},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["session_status"] == "expired"
    _assert_still_expired(session_manager, session.id)


async def test_registration_refuses_another_machines_session(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    other_machine = str(uuid.uuid4())
    LocalMachineManager(temp_db).upsert_seen(other_machine, TEST_USER_ID)
    session = _expired_past_horizon(session_manager, sample_project["id"], other_machine)
    registry = create_session_messages_registry(session_manager=session_manager)
    request = {"external_id": EXTERNAL_ID, "source": "codex", "project_id": sample_project["id"]}

    result = await registry.call("register_session", request)
    response = _client(session_manager, tmp_path).post("/api/sessions/register", json=request)

    assert result["error_code"] == "machine_ownership_mismatch", result
    assert result["owner_machine_id"] == other_machine
    assert response.status_code == 409, response.text
    row = session_manager.get(session.id)
    assert row is not None
    assert row.status == "expired"
    assert row.machine_id == other_machine
