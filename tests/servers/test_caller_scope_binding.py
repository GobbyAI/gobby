"""HTTP and WebSocket requests bind the caller that owns their credential."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse

from gobby.hub.caller_scope import current_caller, require_machine_owned
from gobby.hub.node_client import NodeCommandError
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.runtime_grants import encode_grant_header
from gobby.servers.auth_service import AuthService
from gobby.servers.grant_auth import AuthDecision
from gobby.servers.http import HTTPServer
from gobby.servers.middleware.auth import AuthMiddleware
from gobby.servers.websocket.models import WebSocketConfig
from gobby.servers.websocket.server import WebSocketServer
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.auth import AuthStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.utils.break_glass import BREAK_GLASS_HEADER
from gobby.utils.local_token import derive_managed_signing_key, issue_agent_api_token
from gobby.utils.machine_id import get_machine_id
from tests.fixtures.postgres import TEST_USER_ID
from tests.servers.conftest import (
    OTHER_USER_ID,
    TEST_API_KEY,
    TEST_FRONT_DOOR_IDENTITY,
    TEST_FRONT_DOOR_SECRET,
    authenticate_as,
)
from tests.servers.test_auth_service import (
    _GRANT_NOW,
    _grant_service,
    _signed_presentation_grant,
)

pytestmark = pytest.mark.unit

_FRONT_DOOR_SECRET = "caller-scope-front-door"
_API_KEY = "operator-token"


class _MachinesRaise:
    """Database proxy whose machine reads fail and whose other reads pass through."""

    def __init__(self, inner: HubDatabase) -> None:
        self._inner = inner

    def fetchone(self, query: str, params: tuple[object, ...] = ()) -> Any:
        if "machines" in query:
            raise RuntimeError("machine lookup failed")
        return self._inner.fetchone(query, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _SessionsRaise:
    """Database proxy whose session reads fail and whose other reads pass through."""

    def __init__(self, inner: HubDatabase) -> None:
        self._inner = inner

    def fetchone(self, query: str, params: tuple[object, ...] = ()) -> Any:
        if "user_id" in query and "auth_sessions" in query:
            raise RuntimeError("session lookup failed")
        return self._inner.fetchone(query, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _request(
    headers: dict[str, str],
    *,
    method: str = "GET",
    path: str = "/",
    host: str = "testclient",
) -> Request:
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in headers.items()]
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": raw_headers,
            "client": (host, 50000),
            "server": ("testserver", 80),
        }
    )


def _service(
    database: HubDatabase,
    tmp_path: Path,
    *,
    break_glass_file: Path | None = None,
) -> AuthService:
    bootstrap = tmp_path / "bootstrap.yaml"
    bootstrap.parent.mkdir(parents=True, exist_ok=True)
    bootstrap.write_text(json.dumps({"api_key": _API_KEY}))
    service = AuthService(
        lambda: database,
        bootstrap_file=bootstrap,
        break_glass_file=break_glass_file or tmp_path / "absent-break-glass",
    )
    service.bind_runtime(
        grant_service=None,
        clock=None,
        front_door_secret=_FRONT_DOOR_SECRET,
        local_machine_id=get_machine_id(),
    )
    return service


def _bearer(
    temp_db: HubDatabase,
    session_manager: Any,
    sample_project: dict[str, Any],
    machine_id: str,
    *,
    api_key: str = _API_KEY,
) -> dict[str, str]:
    session = session_manager.register(
        external_id=f"caller-scope-{uuid4().hex}",
        machine_id=get_machine_id(),
        source="claude",
        project_id=sample_project["id"],
    )
    run = LocalAgentRunManager(temp_db).create(
        parent_session_id=session.id,
        provider="claude",
        prompt="caller scope",
    )
    token = issue_agent_api_token(
        derive_managed_signing_key(api_key),
        agent_run_id=run.id,
        session_id="session-123",
        project_id=sample_project["id"],
        machine_id=machine_id,
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Gobby-Agent-Run-Id": run.id,
        "X-Gobby-Session-Id": "session-123",
        "X-Gobby-Project-Id": sample_project["id"],
    }


def test_authenticate_resolves_caller_user(
    temp_db: HubDatabase,
    tmp_path: Path,
    session_manager: Any,
    sample_project: dict[str, Any],
) -> None:
    service = _service(temp_db, tmp_path)
    hub_id = get_machine_id()
    assert hub_id is not None
    hub = hub_id

    front_door = service.authenticate(
        _request(
            {
                "X-Gobby-Front-Door": _FRONT_DOOR_SECRET,
                "X-Gobby-User-Id": TEST_USER_ID,
                "X-Gobby-Machine-Id": hub,
                "X-Gobby-Key-Id": "key-1",
            }
        )
    )
    assert front_door.allowed is True
    assert front_door.user_id == TEST_USER_ID
    assert front_door.break_glass is False

    token, _expires = AuthStore(temp_db).create_session(TEST_USER_ID)
    cookie = service.authenticate(_request({"Cookie": f"gobby_session={token}"}))
    assert cookie.allowed is True
    assert cookie.user_id == TEST_USER_ID
    assert cookie.break_glass is False

    owned = service.authenticate(
        _request(
            _bearer(temp_db, session_manager, sample_project, hub),
            method="POST",
            path="/api/mcp/tools/call",
        )
    )
    assert owned.allowed is True
    assert owned.user_id == TEST_USER_ID

    missing = service.authenticate(
        _request(
            _bearer(temp_db, session_manager, sample_project, str(uuid4())),
            method="POST",
            path="/api/mcp/tools/call",
        )
    )
    assert missing.allowed is True
    assert missing.user_id is None

    raising = _service(cast(HubDatabase, _MachinesRaise(temp_db)), tmp_path / "raising")
    failed_lookup = raising.authenticate(
        _request(
            _bearer(temp_db, session_manager, sample_project, hub),
            method="POST",
            path="/api/mcp/tools/call",
        )
    )
    assert failed_lookup.allowed is True
    assert failed_lookup.user_id is None

    credential = tmp_path / "break-glass"
    credential.write_text("break-glass-secret\n")
    os.chmod(credential, 0o600)
    glass = _service(temp_db, tmp_path / "glass", break_glass_file=credential)
    decision = glass.authenticate(
        _request({BREAK_GLASS_HEADER: "break-glass-secret"}, host="127.0.0.1")
    )
    assert decision.allowed is True
    assert decision.user_id is None
    assert decision.break_glass is True

    service.bind_runtime(
        grant_service=_grant_service(),
        clock=lambda: _GRANT_NOW + 10,
        local_machine_id=hub,
        front_door_secret=_FRONT_DOOR_SECRET,
    )
    front_door_grant = encode_grant_header(_signed_presentation_grant(machine_id=hub))
    grant_front_door = service.authenticate(
        _request(
            {
                "X-Gobby-Front-Door": _FRONT_DOOR_SECRET,
                "X-Gobby-User-Id": TEST_USER_ID,
                "X-Gobby-Machine-Id": hub,
                "X-Gobby-Key-Id": "key-1",
                "X-Gobby-Runtime-Grant": front_door_grant,
            },
            method="POST",
            path="/api/admin/savings/record",
        )
    )
    assert grant_front_door.allowed is True
    assert grant_front_door.user_id == TEST_USER_ID

    bearer_headers = _bearer(temp_db, session_manager, sample_project, hub)
    agent_grant = encode_grant_header(
        _signed_presentation_grant(
            kind="agent_run",
            machine_id=hub,
            project_id=sample_project["id"],
            session_id="session-123",
            execution_id=bearer_headers["X-Gobby-Agent-Run-Id"],
        )
    )
    grant_bearer = service.authenticate(
        _request(
            bearer_headers | {"X-Gobby-Runtime-Grant": agent_grant},
            method="POST",
            path="/api/embeddings",
        )
    )
    assert grant_bearer.allowed is True
    assert grant_bearer.user_id == TEST_USER_ID

    cookie_grant = encode_grant_header(_signed_presentation_grant(machine_id=hub))
    grant_cookie = service.authenticate(
        _request(
            {
                "Cookie": f"gobby_session={token}",
                "X-Gobby-Runtime-Grant": cookie_grant,
            },
            method="POST",
            path="/api/admin/savings/record",
        )
    )
    assert grant_cookie.allowed is True
    assert grant_cookie.user_id == TEST_USER_ID

    raising_cookie = _service(
        cast(HubDatabase, _SessionsRaise(temp_db)),
        tmp_path / "raising-cookie",
    )
    raised_cookie = raising_cookie.authenticate(_request({"Cookie": f"gobby_session={token}"}))
    assert raised_cookie.allowed is True
    assert raised_cookie.user_id is None


def _probe(server: HTTPServer, path: str) -> None:
    @server.app.get(path)
    def _report() -> dict[str, object]:
        scope = current_caller()
        return {
            "bound": scope is not None,
            "user_id": None if scope is None else scope.user_id,
        }


@pytest.mark.usefixtures("isolated_http_runtime")
def test_http_request_binds_resolved_user(http_server: HTTPServer, tmp_path: Path) -> None:
    _probe(http_server, "/api/caller-scope")
    _probe(http_server, "/api/auth/caller-scope")

    with TestClient(http_server.app, client=("127.0.0.1", 50000)) as client:
        authenticate_as(http_server, TEST_USER_ID)
        owner = client.get("/api/caller-scope")
        assert owner.status_code == 200
        assert owner.json()["bound"] is True
        assert owner.json()["user_id"] == TEST_USER_ID

        authenticate_as(http_server, OTHER_USER_ID)
        other = client.get("/api/caller-scope")
        assert other.status_code == 200
        assert other.json()["bound"] is True
        assert other.json()["user_id"] == OTHER_USER_ID

        cast(Any, http_server.auth_service).authenticate = lambda _request: AuthDecision(
            allowed=True
        )
        missing = client.get("/api/caller-scope")
        assert missing.status_code == 200
        assert missing.json()["bound"] is True
        assert missing.json()["user_id"] is None

        glass_path = tmp_path / "present-break-glass"
        glass_path.write_text("break-glass-secret\n")
        os.chmod(glass_path, 0o600)
        bootstrap = tmp_path / "bootstrap.yaml"
        bootstrap.write_text(json.dumps({"api_key": _API_KEY}))
        http_server.auth_service = AuthService(
            lambda: http_server.services.database,
            bootstrap_file=bootstrap,
            break_glass_file=glass_path,
        )
        http_server.auth_service.bind_runtime(
            grant_service=None,
            clock=None,
            front_door_secret=TEST_FRONT_DOOR_SECRET,
            local_machine_id=get_machine_id(),
        )
        glass = client.get(
            "/api/caller-scope",
            headers={BREAK_GLASS_HEADER: "break-glass-secret"},
        )
        assert glass.status_code == 200
        assert glass.json()["bound"] is False
        assert glass.json()["user_id"] is None

        public = client.get("/api/auth/caller-scope")
        assert public.status_code == 200
        assert public.json()["bound"] is False
        assert public.json()["user_id"] is None

    assert current_caller() is None


def _ensure_user(database: HubDatabase, user_id: str, email: str) -> None:
    database.execute(
        """
        INSERT INTO users (id, email, name, password_hash)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (user_id, email, "Caller", "not-a-password"),
    )


def _mount_ownership_route(server: HTTPServer) -> None:
    async def ownership_probe(request: Request) -> JSONResponse:
        body = await request.json()
        machine_id = body["machine_id"]
        if not isinstance(machine_id, str):
            return JSONResponse({"code": "bad_request"}, status_code=400)
        try:
            require_machine_owned(machine_id)
        except NodeCommandError as exc:
            return JSONResponse({"code": exc.code, "stage": exc.stage}, status_code=403)
        return JSONResponse({"ok": True})

    server.app.add_api_route("/api/mcp/tools/call", ownership_probe, methods=["POST"])
    added = server.app.router.routes.pop()
    server.app.router.routes.insert(0, added)


def _post_machine(client: TestClient, machine_id: str, headers: dict[str, str]) -> Any:
    return client.post(
        "/api/mcp/tools/call",
        json={"machine_id": machine_id},
        headers=headers,
    )


def _assert_refused(response: Any) -> None:
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "machine_not_owned"
    assert body["stage"] == "none"


def _assert_proceeds(response: Any) -> None:
    assert response.status_code == 200
    assert response.json()["ok"] is True


@pytest.mark.usefixtures("isolated_http_runtime")
def test_http_ownership_matrix(
    http_server: HTTPServer,
    tmp_path: Path,
    temp_db: HubDatabase,
    session_manager: Any,
    sample_project: dict[str, Any],
) -> None:
    hub = str(UUID(get_machine_id()))
    remote = str(uuid4())
    other_machine = str(uuid4())
    _ensure_user(temp_db, OTHER_USER_ID, "other-caller@example.com")
    machines = LocalMachineManager(temp_db)
    machines.upsert_seen(remote, TEST_USER_ID, label="remote")
    machines.upsert_seen(other_machine, OTHER_USER_ID, label="other")
    _mount_ownership_route(http_server)
    require_machine_owned(hub)

    owner_cookie, _expires = AuthStore(temp_db).create_session(TEST_USER_ID)
    other_cookie, _other_expires = AuthStore(temp_db).create_session(OTHER_USER_ID)
    owner_bearer = _bearer(
        temp_db,
        session_manager,
        sample_project,
        hub,
        api_key=TEST_API_KEY,
    )
    other_bearer = _bearer(
        temp_db,
        session_manager,
        sample_project,
        other_machine,
        api_key=TEST_API_KEY,
    )
    glass_path = tmp_path / "matrix-break-glass"
    glass_path.write_text("break-glass-secret\n")
    os.chmod(glass_path, 0o600)
    http_server.auth_service = AuthService(
        lambda: temp_db,
        bootstrap_file=tmp_path / "bootstrap.yaml",
        break_glass_file=glass_path,
    )
    http_server.auth_service.bind_runtime(
        grant_service=None,
        clock=None,
        front_door_secret=TEST_FRONT_DOOR_SECRET,
        local_machine_id=str(TEST_FRONT_DOOR_IDENTITY["X-Gobby-Machine-Id"]),
    )

    with TestClient(http_server.app, client=("127.0.0.1", 50000)) as client:
        for machine_id in (hub, remote):
            _assert_refused(
                _post_machine(
                    client,
                    machine_id,
                    {"Cookie": f"gobby_session={other_cookie}"},
                )
            )
            _assert_refused(_post_machine(client, machine_id, other_bearer))
            _assert_proceeds(
                _post_machine(
                    client,
                    machine_id,
                    {"Cookie": f"gobby_session={owner_cookie}"},
                )
            )
            _assert_proceeds(_post_machine(client, machine_id, owner_bearer))
        for machine_id in (hub, remote):
            _assert_proceeds(
                _post_machine(
                    client,
                    machine_id,
                    {BREAK_GLASS_HEADER: "break-glass-secret"},
                )
            )


class _InlineExecutor:
    async def run(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        result = func(*args, **kwargs)
        if isinstance(result, Awaitable):
            return await result
        return result


class _HoldingSocket:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id
        self.remote_address = "127.0.0.1"
        self.latency = 0.0
        self.sent: list[str] = []
        self._pending: asyncio.Queue[str | None] = asyncio.Queue()

    def __aiter__(self) -> _HoldingSocket:
        return self

    async def __anext__(self) -> str:
        item = await self._pending.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    def push(self, message: str) -> None:
        self._pending.put_nowait(message)

    def finish(self) -> None:
        self._pending.put_nowait(None)


@dataclass(frozen=True, slots=True)
class _Observation:
    bound: bool
    user_id: str | None
    off_loop: bool
    outcome: str
    machine_id: str | None
    stage: str | None


class _ProbingServer(WebSocketServer):
    def __init__(self) -> None:
        async def _unused(_headers: Mapping[str, str]) -> str | None:
            return None

        super().__init__(
            config=WebSocketConfig(),
            mcp_manager=cast(MCPClientManager, MagicMock()),
            auth_callback=_unused,
            db_executor=cast(Any, _InlineExecutor()),
        )
        self.lease_registry = TerminalLeaseRegistry(daemon_epoch="test-epoch")
        self.observations: list[_Observation] = []
        self.arrived = asyncio.Event()

    async def _handle_message(
        self,
        websocket: Any,
        message: str,
        *,
        blocks_connection: bool = True,
    ) -> None:
        del blocks_connection
        parsed = json.loads(message)
        if not isinstance(parsed, dict):
            return
        data = cast(dict[str, Any], parsed)
        scope = current_caller()
        task = asyncio.current_task()
        machine_raw = data.get("machine_id")
        machine_id = machine_raw if isinstance(machine_raw, str) else None
        outcome = "ok"
        stage: str | None = None
        if machine_id is not None:
            try:
                require_machine_owned(machine_id)
            except NodeCommandError as exc:
                outcome = exc.code
                stage = exc.stage if isinstance(exc.stage, str) else None
            except NotImplementedError:
                outcome = "not_implemented"
        observation = _Observation(
            bound=scope is not None,
            user_id=None if scope is None else scope.user_id,
            off_loop=task is not None and task.get_name() == "ws-off-loop",
            outcome=outcome,
            machine_id=machine_id,
            stage=stage,
        )
        self.observations.append(observation)
        self.arrived.set()
        await websocket.send(json.dumps(asdict(observation)))


def _frame(message_type: str, machine_id: str | None = None) -> str:
    body: dict[str, str] = {"type": message_type}
    if machine_id is not None:
        body["machine_id"] = machine_id
    return json.dumps(body)


async def _drive(
    server: _ProbingServer,
    socket: _HoldingSocket,
    messages: list[str],
    decision: AuthDecision | None = None,
    database: HubDatabase | None = None,
) -> None:
    leaked: list[Any] = []

    async def _connection() -> None:
        await server.handle_connection(socket, decision, database=database)
        leaked.append(current_caller())

    connection = asyncio.create_task(_connection())
    for message in messages:
        server.arrived.clear()
        socket.push(message)
        await asyncio.wait_for(server.arrived.wait(), timeout=2)
    socket.finish()
    await connection
    assert leaked == [None]


def _assert_observed(
    observation: _Observation,
    *,
    user_id: str | None,
    bound: bool,
    off_loop: bool,
    outcome: str,
    machine_id: str | None,
    stage: str | None = None,
) -> None:
    assert observation.bound is bound
    assert observation.user_id == user_id
    assert observation.off_loop is off_loop
    assert observation.outcome == outcome
    assert observation.machine_id == machine_id
    assert observation.stage == stage


@pytest.mark.usefixtures("isolated_http_runtime")
def test_websocket_messages_bind_connection_user(
    http_server: HTTPServer,
    temp_db: HubDatabase,
) -> None:
    hub = str(UUID(get_machine_id()))
    remote = str(uuid4())
    LocalMachineManager(temp_db).upsert_seen(remote, TEST_USER_ID, label="remote")
    front_door_user = str(TEST_FRONT_DOOR_IDENTITY["X-Gobby-User-Id"])

    async def _native_and_unowned() -> None:
        native = _ProbingServer()
        await _drive(
            native,
            _HoldingSocket(front_door_user),
            [_frame("ping"), _frame("terminal_list")],
        )
        _assert_observed(
            native.observations[0],
            user_id=front_door_user,
            bound=True,
            off_loop=False,
            outcome="ok",
            machine_id=None,
        )
        _assert_observed(
            native.observations[1],
            user_id=front_door_user,
            bound=True,
            off_loop=True,
            outcome="ok",
            machine_id=None,
        )

        unowned = _ProbingServer()
        await _drive(
            unowned,
            _HoldingSocket("local-web-unowned"),
            [
                _frame("ping", hub),
                _frame("terminal_list", hub),
                _frame("ping", remote),
                _frame("terminal_list", remote),
            ],
            AuthDecision(allowed=True),
            temp_db,
        )
        expected = (
            (hub, False),
            (hub, True),
            (remote, False),
            (remote, True),
        )
        for observation, (machine_id, off_loop) in zip(unowned.observations, expected, strict=True):
            _assert_observed(
                observation,
                user_id=None,
                bound=True,
                off_loop=off_loop,
                outcome="machine_not_owned",
                machine_id=machine_id,
                stage="none",
            )

    asyncio.run(_native_and_unowned())

    cookie_server = _ProbingServer()
    http_server.websocket_server = cookie_server
    http_server.services.websocket_server = cookie_server
    token, _expires = AuthStore(temp_db).create_session(TEST_USER_ID)
    reports: list[dict[str, Any]] = []
    with TestClient(http_server.app, client=("127.0.0.1", 50000)) as client:
        with client.websocket_connect(
            "/ws",
            headers={"Cookie": f"gobby_session={token}"},
        ) as socket:
            welcome = socket.receive_json()
            assert welcome["type"] == "connection_established"
            for message_type, machine_id in (
                ("ping", hub),
                ("terminal_list", hub),
                ("ping", remote),
                ("terminal_list", remote),
            ):
                socket.send_json({"type": message_type, "machine_id": machine_id})
                report = socket.receive_json()
                assert isinstance(report, dict)
                reports.append(cast(dict[str, Any], report))
    assert current_caller() is None
    cookie_expected = (
        (hub, False),
        (hub, True),
        (remote, False),
        (remote, True),
    )
    for report, (machine_id, off_loop) in zip(reports, cookie_expected, strict=True):
        assert report["bound"] is True
        assert report["user_id"] == TEST_USER_ID
        assert report["off_loop"] is off_loop
        assert report["outcome"] == "ok"
        assert report["machine_id"] == machine_id


def test_allowed_request_without_database_is_refused() -> None:
    """An allowed request whose services lack a database does not run unbound."""
    called = False

    async def run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    auth_service = MagicMock()
    auth_service.authenticate.return_value = AuthDecision(allowed=True, user_id=TEST_USER_ID)
    server = cast(
        HTTPServer,
        SimpleNamespace(
            auth_service=auth_service,
            run_db=run_db,
            services=SimpleNamespace(),
        ),
    )
    app = FastAPI()
    app.add_middleware(AuthMiddleware, server=server)

    @app.get("/api/tasks")
    async def protected() -> dict[str, bool]:
        nonlocal called
        called = True
        return {"ok": True}

    with pytest.raises(AttributeError, match="database"):
        TestClient(app).get("/api/tasks")

    assert called is False


def test_allowed_request_without_services_is_refused() -> None:
    """An allowed request whose server has no services is refused and does not run."""
    called = False

    async def run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    auth_service = MagicMock()
    auth_service.authenticate.return_value = AuthDecision(allowed=True, user_id=TEST_USER_ID)
    server = cast(
        HTTPServer,
        SimpleNamespace(
            auth_service=auth_service,
            run_db=run_db,
        ),
    )
    app = FastAPI()
    app.add_middleware(AuthMiddleware, server=server)

    @app.get("/api/tasks")
    async def protected() -> dict[str, bool]:
        nonlocal called
        called = True
        return {"ok": True}

    response = TestClient(app).get("/api/tasks")

    assert response.status_code == 503
    assert response.json()["error"] == "Database not available"
    assert called is False
