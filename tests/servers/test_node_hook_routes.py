from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from gobby.servers.auth_service import AuthService
from gobby.servers.http import HTTPServer
from gobby.storage import workspace_machine_scope
from gobby.storage.hook_receipts import prepare_receipt, release_receipt, reprepare_receipt
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit
NODE = "37e78430-9ce8-447b-8a68-0688cde4a884"
LOCAL = "37e78430-9ce8-447b-8a68-0688cde4a885"


def _headers(machine: str = NODE) -> dict[str, str]:
    return {
        "X-Gobby-Front-Door": "node-test-secret",
        "X-Gobby-User-Id": TEST_USER_ID,
        "X-Gobby-Machine-Id": machine,
        "X-Gobby-Key-Id": "test-key",
    }


def _client(db: HubDatabase, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from gobby.servers.routes import node_hooks

    auth = AuthService(lambda: db)
    auth.bind_runtime(
        grant_service=None, local_machine_id=LOCAL, clock=None, front_door_secret="node-test-secret"
    )
    server = cast(
        HTTPServer, SimpleNamespace(auth_service=auth, services=SimpleNamespace(database=db))
    )
    app = FastAPI()
    app.state.database = db
    monkeypatch.setattr(node_hooks, "require_machine_id", lambda: LOCAL)
    app.include_router(node_hooks.create_node_hooks_router(server))
    return TestClient(app)


@pytest.fixture
def node_session(
    session_manager: SessionManager, sample_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> str:
    LocalMachineManager(session_manager.db).upsert_seen(NODE, TEST_USER_ID)
    monkeypatch.setattr(workspace_machine_scope, "require_machine_id", lambda: NODE)
    return session_manager.register_session(
        external_id="node-receipts", machine_id=NODE, source="grok", project_id=sample_project["id"]
    )


@pytest.mark.parametrize(
    "proof",
    [
        "execute",
        "live",
        "processed",
        "missing",
        "forged",
        "local",
        "version",
        "extra",
        "bool",
        "string",
    ],
)
def test_replay_route(temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, proof: str) -> None:
    if proof in {"execute", "live", "processed"}:
        expected = {"execute": "execute", "live": "retain", "processed": "unlink"}[proof]
        with _client(temp_db, monkeypatch) as client:
            response = client.post(
                "/api/nodes/hooks/replay",
                headers=_headers(),
                json={
                    "v": 1,
                    "envelope_id": "env",
                    "envelope": {"response_capability": "hook-response.v1"},
                    "age_seconds": 1,
                    "processed": proof == "processed",
                    "live": proof != "execute",
                },
            )
        assert response.status_code == 200
        assert response.json() == {
            "disposition": expected,
            "detail": "live" if expected == "retain" else "",
        }
    elif proof in {"missing", "forged", "local"}:
        detail = "machine_is_local" if proof == "local" else "node_identity_required"
        headers = {} if proof == "missing" else _headers(LOCAL if proof == "local" else NODE)
        if proof == "forged":
            headers["X-Gobby-Front-Door"] = "forged"
        with _client(temp_db, monkeypatch) as client:
            response = client.post("/api/nodes/hooks/replay", headers=headers, json={})
        assert response.status_code == 403
        assert response.json() == {"detail": detail}
    else:
        bodies: dict[str, dict[str, Any]] = {
            "version": {"v": 2},
            "extra": {"extra": True},
            "bool": {"v": True},
            "string": {"v": "1"},
        }
        detail = "unsupported_version" if proof == "version" else "invalid_body"
        valid: dict[str, Any] = {
            "v": 1,
            "envelope_id": "env",
            "envelope": {},
            "age_seconds": 1,
            "processed": False,
            "live": False,
        }
        with _client(temp_db, monkeypatch) as client:
            response = client.post(
                "/api/nodes/hooks/replay", headers=_headers(), json=valid | bodies[proof]
            )
        assert response.status_code == 422
        assert response.json() == {"detail": detail}


@pytest.mark.parametrize(
    "proof",
    [
        "ack",
        "release",
        "foreign-ack",
        "foreign-release",
        "missing",
        "forged",
        "local",
        "version",
        "extra",
        "bool",
        "string",
        "malformed-ack",
        "malformed-release",
    ],
)
def test_receipts_route(
    session_manager: SessionManager, node_session: str, monkeypatch: pytest.MonkeyPatch, proof: str
) -> None:
    if proof == "ack":
        db = session_manager.db
        receipt = prepare_receipt(
            db,
            session_id=node_session,
            envelope_id="original",
            staged_payload={
                "session_variables": {"delivered_once": True},
                "pending_message_ids": ["message"],
                "pending_message_session_id": node_session,
            },
        )
        assert release_receipt(db, receipt_id=receipt.receipt_id, delivery_generation=1)
        carried = reprepare_receipt(db, receipt_id=receipt.receipt_id, envelope_id="current")
        assert carried is not None
        variables = SessionVariableManager(db)
        messages = Mock()
        with _client(db, monkeypatch) as client:
            cast(FastAPI, client.app).state.hook_manager = SimpleNamespace(
                _inter_session_msg_manager=messages
            )
            body = {"v": 1, "op": "ack", "receipt_id": receipt.receipt_id, "delivery_generation": 2}
            first = client.post("/api/nodes/receipts", headers=_headers(), json=body)
            assert first.status_code == 200
            assert first.json() == {
                "acknowledged": True,
                "carrying_envelope_ids": ["original", "current"],
            }
            assert variables.get_variables(node_session)["delivered_once"] is True
            variables.merge_variables(node_session, {"delivered_once": False})
            duplicate = client.post("/api/nodes/receipts", headers=_headers(), json=body)
        assert duplicate.json() == {"acknowledged": False, "carrying_envelope_ids": []}
        assert variables.get_variables(node_session)["delivered_once"] is False
        messages.mark_delivered_batch.assert_called_once_with(["message"], node_session)
    elif proof == "release":
        db = session_manager.db
        receipt = prepare_receipt(db, session_id=node_session, envelope_id="release")
        with _client(db, monkeypatch) as client:
            body = {
                "v": 1,
                "op": "release",
                "receipt_id": receipt.receipt_id,
                "delivery_generation": 1,
            }
            first = client.post("/api/nodes/receipts", headers=_headers(), json=body)
            duplicate = client.post("/api/nodes/receipts", headers=_headers(), json=body)
        assert first.status_code == 200 and first.json() == {"released": True}
        assert duplicate.json() == {"released": False}
    elif proof.startswith("foreign-"):
        db = session_manager.db
        receipt = prepare_receipt(db, session_id=node_session, envelope_id="foreign")
        with _client(db, monkeypatch) as client:
            response = client.post(
                "/api/nodes/receipts",
                headers=_headers("foreign-node"),
                json={
                    "v": 1,
                    "op": proof.removeprefix("foreign-"),
                    "receipt_id": receipt.receipt_id,
                    "delivery_generation": 1,
                },
            )
        assert response.status_code == 200
        assert response.json() == (
            {"acknowledged": False, "carrying_envelope_ids": []}
            if proof.removeprefix("foreign-") == "ack"
            else {"released": False}
        )
        stored = db.fetchone(
            "SELECT state FROM hook_receipt_effects WHERE receipt_id = %s", (receipt.receipt_id,)
        )
        assert stored is not None and stored["state"] == "prepared"
    elif proof.startswith("malformed-"):
        with _client(session_manager.db, monkeypatch) as client:
            response = client.post(
                "/api/nodes/receipts",
                headers=_headers(),
                json={
                    "v": 1,
                    "op": proof.removeprefix("malformed-"),
                    "receipt_id": "malformed",
                    "delivery_generation": 1,
                },
            )
        assert response.status_code == 422
        assert response.json() == {"detail": "invalid_body"}
    elif proof in {"missing", "forged", "local"}:
        detail = "machine_is_local" if proof == "local" else "node_identity_required"
        headers = {} if proof == "missing" else _headers(LOCAL if proof == "local" else NODE)
        if proof == "forged":
            headers["X-Gobby-Front-Door"] = "forged"
        with _client(session_manager.db, monkeypatch) as client:
            response = client.post("/api/nodes/receipts", headers=headers, json={})
        assert response.status_code == 403
        assert response.json() == {"detail": detail}
    else:
        bodies: dict[str, dict[str, Any]] = {
            "version": {"v": 2},
            "extra": {"extra": True},
            "bool": {"v": True},
            "string": {"v": "1"},
        }
        detail = "unsupported_version" if proof == "version" else "invalid_body"
        valid: dict[str, Any] = {
            "v": 1,
            "op": "ack",
            "receipt_id": "00000000-0000-4000-8000-000000000000",
            "delivery_generation": 1,
        }
        with _client(session_manager.db, monkeypatch) as client:
            response = client.post(
                "/api/nodes/receipts", headers=_headers(), json=valid | bodies[proof]
            )
        assert response.status_code == 422
        assert response.json() == {"detail": detail}
