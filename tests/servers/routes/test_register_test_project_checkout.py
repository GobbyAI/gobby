"""Test-project registration refuses a checkout root that another project owns."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.servers.routes.admin import create_admin_router
from gobby.storage.hub.protocol import HubDatabase
from tests.fixtures.isolated_checkout import install_isolated_checkout_project, write_project_marker

pytestmark = pytest.mark.integration


@pytest.fixture
def client(temp_db: HubDatabase) -> TestClient:
    server = MagicMock()
    server.test_mode = True
    server.session_manager = MagicMock()
    server.session_manager.db = temp_db
    app = FastAPI()
    app.include_router(create_admin_router(server))
    return TestClient(app)


def test_refused_root_is_409_and_leaves_no_project_row(
    temp_db: HubDatabase,
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = install_isolated_checkout_project(
        temp_db, tmp_path / "checkout", name="owner", monkeypatch=monkeypatch
    )
    # The same root now carries a marker for a second project id while the
    # first project still owns the root on this machine.
    newcomer_id = str(uuid.uuid4())
    write_project_marker(Path(owner.root_path), project_id=newcomer_id, name="newcomer")

    response = client.post(
        "/api/admin/test/register-project",
        json={"project_id": newcomer_id, "name": "newcomer", "repo_path": owner.root_path},
    )

    assert response.status_code == 409
    assert "already owned" in response.json()["detail"]
    row = temp_db.execute("SELECT id FROM projects WHERE id = %s", (newcomer_id,)).fetchone()
    assert row is None


def test_missing_session_manager_is_503_not_500() -> None:
    server = MagicMock()
    server.test_mode = True
    server.session_manager = None
    app = FastAPI()
    app.include_router(create_admin_router(server))
    client = TestClient(app)

    response = client.post(
        "/api/admin/test/register-project",
        json={"project_id": str(uuid.uuid4()), "name": "no-manager"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Session manager not available"
