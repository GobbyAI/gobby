"""The private gdaemon-to-Python bridge leg into the application handlers (#24620, B1)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import gobby.servers.websocket.native_bridge as native_bridge
from gobby.hub.caller_scope import require_machine_owned
from gobby.hub.node_client import NodeCommandError
from gobby.servers._app_ui import _mount_ws_endpoint
from gobby.servers.auth_service import AuthService
from gobby.servers.grant_auth import AuthDecision
from gobby.servers.http import HTTPServer
from gobby.servers.websocket.models import WebSocketConfig
from gobby.servers.websocket.server import WebSocketServer
from gobby.storage.auth import AuthStore
from gobby.storage.executor import DatabaseExecutor
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.users import LocalUserManager
from gobby.utils.local_token import derive_managed_signing_key, issue_agent_api_token
from gobby.utils.machine_id import get_machine_id
from tests.fixtures.postgres import TEST_USER_ID, TEST_USER_PASSWORD_HASH

pytestmark = pytest.mark.unit

SECRET = "native-bridge-test-front-door"
GENERATION = "0123456789abcdef0123456789abcdef"
LOOPBACK = ("127.0.0.1", 50000)
OTHER_USER_ID = "20000000-0000-4000-8000-000000000002"
OPERATOR_IDENTITY = {
    "X-Gobby-User-Id": TEST_USER_ID,
    "X-Gobby-Machine-Id": "machine-1",
    "X-Gobby-Key-Id": "operator-key",
}
BREAK_GLASS_VALUE = "native-bridge-recovery"


class _Publisher:
    """The bridge's view of the native publisher: secret, generation and leg registry."""

    def __init__(self, generation: str | None = GENERATION) -> None:
        self.front_door_secret = SECRET
        self.generation = generation
        self.legs: dict[str, set[Any]] = {}
        self.unregistered: list[Any] = []

    def register_leg(self, generation: str, leg: Any) -> bool:
        if generation != self.generation:
            return False
        self.legs.setdefault(generation, set()).add(leg)
        return True

    def unregister_leg(self, generation: str, leg: Any) -> None:
        self.legs.get(generation, set()).discard(leg)
        self.unregistered.append(leg)


def _ws_server(
    auth_service: AuthService | None,
    publisher: _Publisher | None,
    db_executor: DatabaseExecutor | None = None,
) -> WebSocketServer:
    callback = (
        auth_service.verify_ws_identity
        if auth_service is not None
        else AsyncMock(return_value="test-user")
    )
    server = WebSocketServer(
        WebSocketConfig(max_message_size=1024), MagicMock(), callback, db_executor=db_executor
    )
    if publisher is not None:
        # B3 declares WebSocketServer.native_publisher; the bridge reads it by name.
        vars(server)["native_publisher"] = publisher
    return server


def _client(
    websocket_server: WebSocketServer,
    auth_service: Any,
    *,
    database: Any = None,
    peer: tuple[str, int] = LOOPBACK,
) -> TestClient:
    app = FastAPI()
    server = SimpleNamespace(
        auth_service=auth_service,
        services=SimpleNamespace(websocket_server=websocket_server, database=database),
        websocket_server=websocket_server,
    )
    _mount_ws_endpoint(app, cast(HTTPServer, server))
    return TestClient(app, client=peer)


def _headers(
    entry: str = "browser",
    *,
    secret: str | None = SECRET,
    generation: str | None = GENERATION,
    leg: str = "7",
    path: str = "/ws",
    extra: dict[str, str] | None = None,
    repeated: tuple[tuple[str, str], ...] = (),
) -> httpx.Headers:
    items = [
        ("X-Gobby-WS-Entry", entry),
        ("X-Gobby-WS-Path", path),
        ("X-Gobby-WS-Leg", leg),
        ("X-Forwarded-For", "127.0.0.1"),
        ("X-Forwarded-Proto", "http"),
    ]
    if secret is not None:
        items.append(("X-Gobby-Front-Door", secret))
    if generation is not None:
        items.append(("X-Gobby-WS-Generation", generation))
    items.extend((extra or {}).items())
    items.extend(repeated)
    return httpx.Headers(items)


def _ready(*, admitted: bool, **fields: Any) -> dict[str, Any]:
    return {
        "type": "bridge_ready",
        "version": 1,
        "generation": GENERATION,
        "admitted": admitted,
        **fields,
    }


def _refused_before_ready(client: TestClient, headers: httpx.Headers) -> int:
    with client.websocket_connect("/_gobby/ws/bridge", headers=headers) as websocket:
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_text()
    return closed.value.code


@pytest.mark.parametrize(
    ("case", "peer", "publisher", "headers"),
    [
        ("non-loopback peer", ("203.0.113.9", 50000), _Publisher(), _headers()),
        ("python-only mode", LOOPBACK, None, _headers()),
        ("missing secret", LOOPBACK, _Publisher(), _headers(secret=None)),
        ("wrong secret", LOOPBACK, _Publisher(), _headers(secret="other-boot")),
        ("absent generation", LOOPBACK, _Publisher(), _headers(generation=None)),
        ("stale generation", LOOPBACK, _Publisher(), _headers(generation="f" * 32)),
        ("failed generation", LOOPBACK, _Publisher(generation=None), _headers()),
        (
            "duplicate generation header",
            LOOPBACK,
            _Publisher(),
            _headers(repeated=(("x-gobby-ws-generation", GENERATION),)),
        ),
        (
            "duplicate forwarded header",
            LOOPBACK,
            _Publisher(),
            _headers(repeated=(("X-FORWARDED-FOR", "198.51.100.4"),)),
        ),
        (
            "duplicate identity header",
            LOOPBACK,
            _Publisher(),
            _headers(
                extra=OPERATOR_IDENTITY,
                repeated=(("x-gobby-user-id", "someone-else"),),
            ),
        ),
    ],
)
def test_bridge_requires_pair_proof_and_current_generation(
    case: str,
    peer: tuple[str, int],
    publisher: _Publisher | None,
    headers: httpx.Headers,
) -> None:
    websocket_server = _ws_server(None, publisher)
    auth_service = MagicMock()
    trusted = MagicMock(side_effect=native_bridge._trusted_headers)

    with (
        patch.object(websocket_server, "handle_connection", AsyncMock()) as handler,
        patch.object(native_bridge, "_trusted_headers", trusted),
    ):
        code = _refused_before_ready(_client(websocket_server, auth_service, peer=peer), headers)

    assert code == 1008, case
    handler.assert_not_awaited()
    auth_service.is_request_authenticated.assert_not_called()
    if case == "non-loopback peer":
        trusted.assert_not_called()
    assert publisher is None or publisher.legs == {}


@pytest.fixture
def bridge_auth(temp_db: HubDatabase, tmp_path: Path) -> AuthService:
    bootstrap = tmp_path / "bootstrap.yaml"
    bootstrap.write_text(json.dumps({"api_key": "operator-token"}))
    break_glass = tmp_path / "break_glass"
    break_glass.touch(mode=0o600)
    break_glass.write_text(BREAK_GLASS_VALUE)
    service = AuthService(lambda: temp_db, bootstrap_file=bootstrap, break_glass_file=break_glass)
    service.bind_runtime(
        grant_service=None,
        local_machine_id=None,
        clock=None,
        front_door_secret=SECRET,
    )
    return service


@pytest.fixture
def db_executor() -> Iterator[DatabaseExecutor]:
    executor = DatabaseExecutor(max_workers=1, thread_name_prefix="native-bridge-db")
    yield executor
    executor.shutdown()


def test_trusted_headers_refuse_repeated_or_folded_synthesized_headers() -> None:
    raw = [(b"x-gobby-ws-leg", b"7"), (b"cookie", b"a=1"), (b"Cookie", b"b=2")]
    assert native_bridge._trusted_headers(raw) == {"x-gobby-ws-leg": "7", "cookie": "a=1"}
    assert native_bridge._trusted_headers([*raw, (b"X-Gobby-WS-Leg", b"8")]) is None
    assert native_bridge._trusted_headers([(b"x-gobby-key-id", b"k1, k2")]) is None
    assert native_bridge._trusted_headers([(b"x-gobby-ws-path", b"/ws?a=1,2")]) == {
        "x-gobby-ws-path": "/ws?a=1,2"
    }


def test_bridge_admission_matrix_matches_entry_owners(
    temp_db: HubDatabase, bridge_auth: AuthService, db_executor: DatabaseExecutor
) -> None:
    cookie = f"gobby_session={AuthStore(temp_db).create_session(TEST_USER_ID)[0]}"
    managed = issue_agent_api_token(
        derive_managed_signing_key("operator-token"),
        agent_run_id="run-1",
        session_id="session-1",
        project_id="project-1",
    )
    operator = {"Authorization": "Bearer operator-key", **OPERATOR_IDENTITY}
    missing = {"status": 401, "body": "Unauthorized: Missing Authorization header\n"}
    refused_browser = {"close_code": 4401, "reason": "Authentication required"}
    cases: list[tuple[str, str, dict[str, str], dict[str, Any] | None]] = [
        ("direct", "no authorization", {}, missing),
        (
            "direct",
            "non-bearer",
            {"Authorization": "Basic b3BlcmF0b3I="},
            {"status": 401, "body": "Unauthorized: Expected Bearer token\n"},
        ),
        (
            "direct",
            "managed bearer",
            {"Authorization": f"Bearer {managed}"},
            {"status": 403, "body": "Forbidden: Invalid token\n"},
        ),
        ("direct", "cookie only", {"Cookie": cookie}, missing),
        ("direct", "operator identity", operator, None),
        ("browser", "operator identity", operator, None),
        (
            "browser",
            "managed bearer with valid cookie",
            {"Authorization": f"Bearer {managed}", "Cookie": cookie},
            refused_browser,
        ),
        ("browser", "valid cookie", {"Cookie": cookie}, None),
        ("browser", "invalid cookie", {"Cookie": "gobby_session=wrong"}, refused_browser),
        (
            "browser",
            "break-glass with valid cookie",
            {"X-Gobby-Break-Glass": BREAK_GLASS_VALUE, "Cookie": cookie},
            None,
        ),
        (
            "browser",
            "break-glass alone",
            {"X-Gobby-Break-Glass": BREAK_GLASS_VALUE},
            refused_browser,
        ),
    ]

    for entry, case, credentials, refusal in cases:
        publisher = _Publisher()
        websocket_server = _ws_server(bridge_auth, publisher, db_executor)
        client = _client(websocket_server, bridge_auth, database=temp_db)
        headers = _headers(entry, path="/ws" if entry == "browser" else "/", extra=credentials)
        with client.websocket_connect("/_gobby/ws/bridge", headers=headers) as websocket:
            ready = websocket.receive_json()
            if refusal is not None:
                assert ready == _ready(admitted=False, **refusal), (entry, case)
                with pytest.raises(WebSocketDisconnect):
                    websocket.receive_text()
                assert publisher.legs == {}, (entry, case)
                continue
            assert ready == _ready(admitted=True), (entry, case)
            welcome = websocket.receive_json()
            assert welcome["type"] == "connection_established", (entry, case)
            if entry == "direct":
                assert welcome["user_id"] == TEST_USER_ID, case
            else:
                assert welcome["user_id"].startswith("local-web-"), case
                assert len(welcome["user_id"]) == len("local-web-") + 8, case

    # Identity headers without the pair secret never reach admission.
    for entry in ("direct", "browser"):
        websocket_server = _ws_server(bridge_auth, _Publisher(), db_executor)
        headers = _headers(entry, secret=None, extra=operator)
        assert _refused_before_ready(_client(websocket_server, bridge_auth), headers) == 1008


def _record_owner_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    websocket_server: WebSocketServer,
    machine_ids: tuple[str, ...],
) -> dict[str, str]:
    """Record each require_machine_owned answer inside the connection's bound scope."""
    outcomes: dict[str, str] = {}
    original = websocket_server._handle_connection

    async def probe(websocket: Any) -> None:
        for machine_id in machine_ids:
            try:
                require_machine_owned(machine_id)
            except NodeCommandError as error:
                outcomes[machine_id] = error.code
            else:
                outcomes[machine_id] = "ok"
        await original(websocket)

    monkeypatch.setattr(websocket_server, "_handle_connection", probe)
    return outcomes


def test_browser_cookie_owner_is_bound_as_caller(
    temp_db: HubDatabase,
    bridge_auth: AuthService,
    db_executor: DatabaseExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = str(UUID(get_machine_id()))
    remote = str(uuid4())
    manager = LocalMachineManager(temp_db)
    manager.upsert_seen(hub, TEST_USER_ID, label="hub")
    manager.upsert_seen(remote, TEST_USER_ID, label="remote")
    cookie = f"gobby_session={AuthStore(temp_db).create_session(TEST_USER_ID)[0]}"
    websocket_server = _ws_server(bridge_auth, _Publisher(), db_executor)
    outcomes = _record_owner_outcomes(monkeypatch, websocket_server, (hub, remote))
    client = _client(websocket_server, bridge_auth, database=temp_db)
    headers = _headers("browser", path="/ws", extra={"Cookie": cookie})
    with client.websocket_connect("/_gobby/ws/bridge", headers=headers) as websocket:
        assert websocket.receive_json() == _ready(admitted=True)
        welcome = websocket.receive_json()
        assert welcome["user_id"].startswith("local-web-")
    assert outcomes == {hub: "ok", remote: "ok"}


def test_browser_decision_without_owner_answers_machine_not_owned(
    temp_db: HubDatabase,
    bridge_auth: AuthService,
    db_executor: DatabaseExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = str(UUID(get_machine_id()))
    monkeypatch.setattr(bridge_auth, "authenticate", lambda _request: AuthDecision(allowed=True))
    websocket_server = _ws_server(bridge_auth, _Publisher(), db_executor)
    outcomes = _record_owner_outcomes(monkeypatch, websocket_server, (hub,))
    client = _client(websocket_server, bridge_auth, database=temp_db)
    headers = _headers("browser", path="/ws")
    with client.websocket_connect("/_gobby/ws/bridge", headers=headers) as websocket:
        assert websocket.receive_json() == _ready(admitted=True)
        websocket.receive_json()
    assert outcomes == {hub: "machine_not_owned"}


def test_browser_break_glass_header_keeps_cookie_owner_bound(
    temp_db: HubDatabase,
    bridge_auth: AuthService,
    db_executor: DatabaseExecutor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Break-glass admits only HTTP, so the cookie decides this leg and binds TEST_USER_ID.
    # The canonical seed owns the hub row as TEST_USER_ID, so probe a remote machine that
    # OTHER_USER_ID owns: a bound caller answers machine_not_owned, while an unbound
    # caller would answer "ok".
    LocalUserManager(temp_db).create(
        name="Other Test User",
        email="other-test-user@gobby.local",
        password_hash=TEST_USER_PASSWORD_HASH,
        user_id=OTHER_USER_ID,
    )
    remote = str(uuid4())
    LocalMachineManager(temp_db).upsert_seen(remote, OTHER_USER_ID, label="remote")
    cookie = f"gobby_session={AuthStore(temp_db).create_session(TEST_USER_ID)[0]}"
    websocket_server = _ws_server(bridge_auth, _Publisher(), db_executor)
    outcomes = _record_owner_outcomes(monkeypatch, websocket_server, (remote,))
    client = _client(websocket_server, bridge_auth, database=temp_db)
    headers = _headers(
        "browser",
        path="/ws",
        extra={"X-Gobby-Break-Glass": BREAK_GLASS_VALUE, "Cookie": cookie},
    )
    with client.websocket_connect("/_gobby/ws/bridge", headers=headers) as websocket:
        assert websocket.receive_json() == _ready(admitted=True)
        websocket.receive_json()
    assert outcomes == {remote: "machine_not_owned"}


def test_bridge_preserves_application_connection_lifecycle(
    db_executor: DatabaseExecutor,
) -> None:
    publisher = _Publisher()
    websocket_server = _ws_server(None, publisher, db_executor)
    websocket_server._chat_sessions["conv-1"] = SimpleNamespace(
        _pending_plan_content="Ship it", provider="claude", db_session_id=None
    )
    received: list[str | bytes] = []

    async def echo(websocket: Any, message: str | bytes) -> None:
        received.append(message)
        await websocket.send(message)

    auth_service = MagicMock()
    auth_service.authenticate.return_value = AuthDecision(allowed=True)
    text = json.dumps({"type": "bridge_ready", "version": 1, "generation": GENERATION})
    binary = b"\x01\x02terminal-bytes\xc3\xa9"
    with (
        patch.object(websocket_server, "_handle_message", side_effect=echo),
        patch.object(websocket_server, "_cleanup_terminal_client", AsyncMock()) as cleanup,
    ):
        client = _client(websocket_server, auth_service, database=MagicMock())
        with client.websocket_connect("/_gobby/ws/bridge", headers=_headers()) as websocket:
            assert websocket.receive_json() == _ready(admitted=True)
            welcome = websocket.receive_json()
            assert welcome["type"] == "connection_established"
            assert welcome["conversation_ids"] == ["conv-1"]
            pending = websocket.receive_json()
            assert pending["type"] == "plan_pending_approval"
            assert pending["plan_content"] == "Ship it"
            websocket.send_text(text)
            assert websocket.receive_text() == text
            websocket.send_bytes(binary)
            assert websocket.receive_bytes() == binary
            legs = list(publisher.legs[GENERATION])
            assert len(legs) == 1
            assert legs[0].leg == 7
            assert legs[0].subscriptions == set()
        cleanup.assert_awaited_once()

    assert received == [text, binary]
    assert websocket_server.clients == {}
    assert publisher.unregistered == legs
    assert publisher.legs[GENERATION] == set()
