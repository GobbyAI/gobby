"""The operator dirty-path approval route admits only a verified operator identity."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.identity import hash_password
from gobby.servers.http import HTTPServer
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.users import LocalUserManager
from gobby.utils.local_token import derive_managed_signing_key, issue_agent_api_token
from gobby.workflows.dirty_edit_grants import (
    OPERATOR_DIRTY_PATH_APPROVALS_VARIABLE,
    approved_dirty_paths,
)
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory
from tests.fixtures.postgres import TEST_USER_EMAIL, TEST_USER_ID
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit

BOOTSTRAP_KEY = "dirty-path-bootstrap-key"
FRONT_DOOR_SECRET = "dirty-path-front-door-secret"
PASSWORD = "correctpassword"
LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000003"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def db(hub_db: HubDatabase) -> HubDatabase:
    LocalUserManager(hub_db).update_password(TEST_USER_ID, hash_password(PASSWORD))
    return hub_db


@pytest.fixture
def seat(
    db: HubDatabase, isolated_checkout_factory: IsolatedCheckoutFactory, tmp_path: Path
) -> Session:
    project = isolated_checkout_factory(db, "dirty-path-routes", root=tmp_path).project
    (tmp_path / "a.txt").write_text("orphaned\n", encoding="utf-8")
    return SessionManager(db).register(
        external_id="route-seat",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=project.id,
    )


def _server(db: HubDatabase) -> HTTPServer:
    server = create_http_server(
        config=DaemonConfig(),
        database=db,
        task_manager=LocalTaskManager(db),
        authenticated_requests=False,
    )
    server.auth_service.bind_runtime(
        grant_service=None,
        local_machine_id=LOCAL_MACHINE_ID,
        clock=None,
        front_door_secret=FRONT_DOOR_SECRET,
    )
    return server


def _route(session_id: str) -> str:
    return f"/api/sessions/{session_id}/dirty-path-approvals"


def _body(checkout: Path, paths: list[str] | None = None) -> dict[str, Any]:
    return {"checkout_root": str(checkout), "paths": paths or ["a.txt"]}


def _variables(db: HubDatabase, session_id: str) -> dict[str, Any]:
    return SessionVariableManager(db).get_variables(session_id)


def test_dirty_path_approval_route_refuses_managed_claims(
    db: HubDatabase, seat: Session, tmp_path: Path
) -> None:
    token = issue_agent_api_token(
        derive_managed_signing_key(BOOTSTRAP_KEY),
        agent_run_id=str(uuid.uuid4()),
        session_id=seat.id,
        project_id="project-123",
    )
    response = TestClient(_server(db).app).post(
        _route(seat.id),
        json=_body(tmp_path),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert OPERATOR_DIRTY_PATH_APPROVALS_VARIABLE not in _variables(db, seat.id)


@pytest.mark.parametrize("case", ["absent", "cookie", "forged"])
def test_dirty_path_approval_route_refuses_without_a_verified_operator(
    db: HubDatabase, seat: Session, tmp_path: Path, case: str
) -> None:
    client = TestClient(_server(db).app)
    headers: dict[str, str] = {}
    if case == "cookie":
        login = client.post(
            "/api/auth/login", json={"email": TEST_USER_EMAIL, "password": PASSWORD}
        )
        assert login.status_code == 200, login.text
    elif case == "forged":
        headers = {
            "X-Gobby-User-Id": TEST_USER_ID,
            "X-Gobby-Machine-Id": LOCAL_MACHINE_ID,
            "X-Gobby-Key-Id": str(uuid.uuid4()),
            "X-Gobby-Front-Door": "not-the-front-door-secret",
        }
    response = client.post(_route(seat.id), json=_body(tmp_path), headers=headers)

    assert response.status_code == 403
    assert OPERATOR_DIRTY_PATH_APPROVALS_VARIABLE not in _variables(db, seat.id)


def test_dirty_path_approval_route_records_a_verified_operator_approval(
    db: HubDatabase, seat: Session, tmp_path: Path
) -> None:
    key_id = str(uuid.uuid4())
    operator = TestClient(
        _server(db).app,
        headers={
            "X-Gobby-Front-Door": FRONT_DOOR_SECRET,
            "X-Gobby-User-Id": TEST_USER_ID,
            "X-Gobby-Machine-Id": LOCAL_MACHINE_ID,
            "X-Gobby-Key-Id": key_id,
        },
    )
    approved = operator.post(_route(seat.id), json=_body(tmp_path, ["a.txt"]))

    assert approved.status_code == 200, approved.text
    assert approved.json()["session_id"] == seat.id
    assert set(approved.json()["approvals"]) == {"a.txt"}
    variables = _variables(db, seat.id)
    stored = variables[OPERATOR_DIRTY_PATH_APPROVALS_VARIABLE][str(tmp_path.resolve())]
    assert stored["approved_by"] == f"{TEST_USER_ID}/{key_id}"
    assert approved_dirty_paths(variables, str(tmp_path)) == {"a.txt"}

    relative = operator.post(
        _route(seat.id), json={"checkout_root": "relative", "paths": ["a.txt"]}
    )
    escape = operator.post(_route(seat.id), json=_body(tmp_path, ["../escape.txt"]))
    missing = operator.post(_route(str(uuid.uuid4())), json=_body(tmp_path))

    assert relative.status_code == 400
    assert escape.status_code == 400
    assert missing.status_code == 404
    assert approved_dirty_paths(_variables(db, seat.id), str(tmp_path)) == {"a.txt"}
