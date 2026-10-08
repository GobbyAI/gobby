"""Tests for API key issuance and management routes through the full app."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx2 import Response
from starlette.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from gobby.config.app import DaemonConfig
from gobby.identity import hash_password
from gobby.servers.http import HTTPServer
from gobby.storage.api_keys import ApiKeyManager
from gobby.storage.auth import hash_token
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.users import LocalUserManager
from gobby.utils import api_key_format
from gobby.utils.local_token import derive_managed_signing_key, issue_agent_api_token
from gobby.utils.machine_id import require_machine_id
from tests.fixtures.postgres import TEST_USER_EMAIL, TEST_USER_ID
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit

BOOTSTRAP_KEY = "api-keys-bootstrap-key"
FRONT_DOOR_SECRET = "api-keys-front-door-secret"
PASSWORD = "correctpassword"
OTHER_EMAIL = "other@example.com"
NEW_MACHINE = "3c0f7a52-9b1e-4d6a-8e2f-5a7b9c1d3e4f"
OTHER_MACHINE = "7d2e9f41-6a3b-4c8d-9e0f-1a2b3c4d5e6f"
REDACTED_FIELDS = {"id", "hint", "label", "machine_id", "created_at", "last_used_at", "revoked_at"}


@pytest.fixture
def db(hub_db: HubDatabase) -> HubDatabase:
    LocalUserManager(hub_db).update_password(TEST_USER_ID, hash_password(PASSWORD))
    return hub_db


def _server(db: HubDatabase) -> HTTPServer:
    server = create_http_server(
        config=DaemonConfig(),
        database=db,
        task_manager=LocalTaskManager(db),
        authenticated_requests=False,
    )
    server.auth_service.bind_runtime(
        grant_service=None,
        lease_live=None,
        local_machine_id=require_machine_id(),
        effect_fence=None,
        clock=None,
        front_door_secret=FRONT_DOOR_SECRET,
    )
    return server


def _other_user(db: HubDatabase) -> str:
    return (
        LocalUserManager(db)
        .create(name="Other", email=OTHER_EMAIL, password_hash=hash_password(PASSWORD))
        .id
    )


def _cookie_client(server: HTTPServer, email: str = TEST_USER_EMAIL) -> TestClient:
    client = TestClient(server.app)
    response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200
    return client


def _bootstrap(
    client: TestClient,
    password: str,
    machine_id: str = NEW_MACHINE,
    *,
    forwarded_for: str | None = None,
) -> Response:
    return client.post(
        "/api/auth/keys/bootstrap",
        json={
            "email": TEST_USER_EMAIL,
            "password": password,
            "machine_id": machine_id,
            "hostname": "node-1",
            "os": "linux",
            "label": "node-1 daemon",
        },
        headers={"X-Forwarded-For": forwarded_for} if forwarded_for else None,
    )


def _assert_issued(response: Response, user_id: str, machine_id: str) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    body: dict[str, Any] = response.json()
    assert set(body) == {"key", "key_id", "hint", "user_id", "machine_id"}
    assert api_key_format.parse(body["key"]) is not None
    assert body["hint"] == api_key_format.hint(body["key"])
    assert (body["user_id"], body["machine_id"]) == (user_id, machine_id)
    return body


def _stored_key(db: HubDatabase, key_id: str) -> dict[str, Any]:
    row = db.fetchone(
        "SELECT user_id::TEXT AS user_id, machine_id::TEXT AS machine_id, key_hash, revoked_at "
        "FROM api_keys WHERE id = %s",
        (key_id,),
    )
    assert row is not None
    return dict(row)


def test_bootstrap_mints_bound_key(db: HubDatabase) -> None:
    """4.2.3: password-verified bootstrap binds the machine and returns the key once."""
    server = _server(db)
    client = TestClient(server.app, client=("127.0.0.1", 50000))

    body = _assert_issued(_bootstrap(client, PASSWORD), TEST_USER_ID, NEW_MACHINE)
    stored = _stored_key(db, body["key_id"])
    assert stored["key_hash"] == hash_token(body["key"])
    assert (stored["user_id"], stored["machine_id"]) == (TEST_USER_ID, NEW_MACHINE)
    machine = LocalMachineManager(db).get(NEW_MACHINE)
    assert machine is not None
    assert (machine.owner_user_id, machine.hostname, machine.os) == (
        TEST_USER_ID,
        "node-1",
        "linux",
    )

    LocalMachineManager(db).upsert_seen(OTHER_MACHINE, _other_user(db))
    foreign = _bootstrap(client, PASSWORD, OTHER_MACHINE)
    assert foreign.status_code == 403
    assert "key" not in foreign.json()

    for _ in range(4):
        assert _bootstrap(client, "wrong").status_code == 401
    assert _bootstrap(client, PASSWORD).status_code == 200
    for _ in range(5):
        assert _bootstrap(client, "wrong").status_code == 401
    locked = _bootstrap(client, PASSWORD)
    assert locked.status_code == 429
    assert locked.headers["Retry-After"] == "60"

    # The backend trusts the front door's forwarded peer exactly as runner_lifecycle
    # configures uvicorn, so distinct peers behind gdaemon keep distinct buckets.
    front_door_app = _server(db).app
    front_door_app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="127.0.0.1,::1")
    behind_front_door = TestClient(front_door_app, client=("127.0.0.1", 50001))
    for _ in range(5):
        assert _bootstrap(behind_front_door, "wrong", forwarded_for="192.0.2.10").status_code == 401
    assert _bootstrap(behind_front_door, PASSWORD, forwarded_for="192.0.2.10").status_code == 429
    assert _bootstrap(behind_front_door, PASSWORD, forwarded_for="192.0.2.20").status_code == 200

    login_locked = TestClient(server.app, client=("127.0.0.2", 50000))
    for _ in range(5):
        response = login_locked.post(
            "/api/auth/login", json={"email": TEST_USER_EMAIL, "password": "wrong"}
        )
        assert response.status_code == 401
    assert _bootstrap(login_locked, PASSWORD).status_code == 429


@pytest.mark.parametrize("label", ["omitted", None, ""], ids=["omitted", "null", "empty"])
def test_issuance_routes_require_label(db: HubDatabase, label: str | None) -> None:
    """Both issuance routes reject a missing label before reaching storage."""
    label_field = {} if label == "omitted" else {"label": label}
    server = _server(db)
    bootstrap = TestClient(server.app).post(
        "/api/auth/keys/bootstrap",
        json={
            "email": TEST_USER_EMAIL,
            "password": PASSWORD,
            "machine_id": NEW_MACHINE,
            **label_field,
        },
    )
    mint = _cookie_client(server).post("/api/auth/keys", json=label_field)

    assert (bootstrap.status_code, mint.status_code) == (422, 422)
    assert db.fetchone("SELECT 1 FROM api_keys") is None


def test_management_routes_admit_only_resolved_principals(db: HubDatabase) -> None:
    """Only a cookie or verified front-door identity resolves a principal."""
    server = _server(db)
    local_machine = require_machine_id()
    expired_client = _cookie_client(server)
    db.execute(
        "UPDATE auth_sessions SET expires_at = '2000-01-01T00:00:00+00:00'",
    )
    agent_token = issue_agent_api_token(
        derive_managed_signing_key(BOOTSTRAP_KEY),
        agent_run_id=str(uuid.uuid4()),
        session_id="session-123",
        project_id="project-123",
    )
    rejected: dict[str, tuple[TestClient, dict[str, str]]] = {
        "absent": (TestClient(server.app), {}),
        "invalid": (TestClient(server.app), {"Authorization": "Bearer not-the-operator"}),
        "invalid-local": (TestClient(server.app), {"X-Gobby-Local-Token": "not-the-operator"}),
        "expired-cookie": (expired_client, {}),
        "managed-agent": (TestClient(server.app), {"Authorization": f"Bearer {agent_token}"}),
    }
    for name, (client, headers) in rejected.items():
        responses = [
            client.post("/api/auth/keys", json={"label": "x"}, headers=headers),
            client.get("/api/auth/keys", headers=headers),
            client.delete(f"/api/auth/keys/{uuid.uuid4()}", headers=headers),
        ]
        for response in responses:
            assert response.status_code == 401, (name, response.text)
            assert set(response.json()) == {"error", "code"}, name
    assert db.fetchone("SELECT 1 FROM api_keys") is None

    cookie = _cookie_client(server)
    cookie_key = _assert_issued(
        cookie.post("/api/auth/keys", json={"label": "cookie"}), TEST_USER_ID, local_machine
    )
    operator = TestClient(
        server.app,
        headers={
            "Authorization": f"Bearer {cookie_key['key']}",
            "X-Gobby-Front-Door": FRONT_DOOR_SECRET,
            "X-Gobby-User-Id": TEST_USER_ID,
            "X-Gobby-Machine-Id": local_machine,
            "X-Gobby-Key-Id": cookie_key["key_id"],
        },
    )
    _assert_issued(
        operator.post("/api/auth/keys", json={"label": "op"}), TEST_USER_ID, local_machine
    )
    assert len(operator.get("/api/auth/keys").json()["keys"]) == 2

    _other_user(db)
    _assert_issued(
        operator.post("/api/auth/keys", json={"label": "multi-user"}), TEST_USER_ID, local_machine
    )
    assert cookie.get("/api/auth/keys").status_code == 200

    foreign = _cookie_client(server, OTHER_EMAIL).post("/api/auth/keys", json={"label": "x"})
    assert foreign.status_code == 403
    assert foreign.json()["code"] == "machine_not_owned"


@pytest.mark.parametrize("use_front_door", [False, True], ids=["cookie", "front-door"])
def test_key_routes_use_verified_front_door_identity(db: HubDatabase, use_front_door: bool) -> None:
    """4.2.6: list and revoke see only the caller's keys and never the secret."""
    other_user = _other_user(db)
    LocalMachineManager(db).upsert_seen(NEW_MACHINE, TEST_USER_ID)
    LocalMachineManager(db).upsert_seen(OTHER_MACHINE, other_user)
    keys = ApiKeyManager(db)
    own_key, own = keys.mint(TEST_USER_ID, NEW_MACHINE, "mine")
    other_key, other = keys.mint(other_user, OTHER_MACHINE, "theirs")
    server = _server(db)
    if use_front_door:
        identity_headers = {
            "X-Gobby-User-Id": TEST_USER_ID,
            "X-Gobby-Machine-Id": NEW_MACHINE,
            "X-Gobby-Key-Id": own.id,
        }
        unverified = TestClient(server.app)
        for secret in (None, "wrong-secret"):
            forged = dict(identity_headers)
            if secret is not None:
                forged["X-Gobby-Front-Door"] = secret
            assert unverified.get("/api/auth/keys", headers=forged).status_code == 401
        client = TestClient(
            server.app,
            headers={**identity_headers, "X-Gobby-Front-Door": FRONT_DOOR_SECRET},
        )
    else:
        client = _cookie_client(server)

    listed = client.get("/api/auth/keys")
    assert listed.status_code == 200
    entries = listed.json()["keys"]
    assert [entry["id"] for entry in entries] == [own.id]
    assert set(entries[0]) == REDACTED_FIELDS
    for secret in (own_key, other_key, hash_token(own_key), hash_token(other_key)):
        assert secret not in listed.text

    absent = client.delete(f"/api/auth/keys/{uuid.uuid4()}")
    foreign = client.delete(f"/api/auth/keys/{other.id}")
    malformed = client.delete("/api/auth/keys/not-a-uuid")
    assert absent.status_code == foreign.status_code == malformed.status_code == 404
    assert absent.content == foreign.content == malformed.content
    assert _stored_key(db, other.id)["revoked_at"] is None

    revoked = client.delete(f"/api/auth/keys/{own.id}")
    assert revoked.status_code == 200
    assert _stored_key(db, own.id)["revoked_at"] is not None
    assert client.get("/api/auth/keys").json()["keys"][0]["revoked_at"] is not None
    if use_front_door:
        _assert_issued(
            client.post("/api/auth/keys", json={"label": "node"}), TEST_USER_ID, NEW_MACHINE
        )
