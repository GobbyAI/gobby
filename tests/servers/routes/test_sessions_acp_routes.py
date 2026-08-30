"""Tests for ACP lifecycle REST routes.

Exercises ``register_acp_routes`` (close/delete) and the ``acp``
enrichment attached by ``GET /api/sessions``, asserting the locked status codes
and single-broadcast behavior through a TestClient with in-memory fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.servers.routes.sessions import create_sessions_router

pytestmark = pytest.mark.unit

MACHINE = "21000000-0000-4000-8000-000000000001"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _Session:
    id: str
    external_id: str
    source: str
    project_id: str = "proj-1"
    title: str | None = "Work"
    title_source: str | None = "manual"
    status: str = "active"
    session_type: str = "web_chat"
    machine_id: str = MACHINE
    workspace_path: str | None = "/tmp/acp-workspace"
    workspace_generation: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "external_id": self.external_id,
            "source": self.source,
            "project_id": self.project_id,
            "title": self.title,
            "status": self.status,
            "session_type": self.session_type,
            "machine_id": self.machine_id,
            "updated_at": "2026-02-10T12:00:00+00:00",
        }


class _SM:
    def __init__(self, rows: list[_Session] | None = None) -> None:
        self.rows = {row.id: row for row in (rows or [])}
        self.events: list[tuple[str, str]] = []
        self.delete_error: Exception | None = None

    # -- list path (GET /api/sessions) ---------------------------------
    def list(self, **_kwargs: Any) -> list[_Session]:
        return list(self.rows.values())

    def fetch_task_refs_by_session(self, _ids: list[str]) -> dict[str, Any]:
        return {}

    # -- lifecycle path ------------------------------------------------
    def get(self, session_id: str) -> _Session | None:
        return self.rows.get(session_id)

    def find_by_external_id(
        self,
        external_id: str,
        machine_id: str,
        project_id: str | None,
        source: str,
        session_type: str | None = None,
    ) -> _Session | None:
        for row in self.rows.values():
            if (
                row.external_id == external_id
                and row.source == source
                and row.project_id == project_id
                and row.machine_id == machine_id
                and (session_type is None or row.session_type == session_type)
            ):
                return row
        return None

    def register(
        self,
        *,
        external_id: str,
        machine_id: str,
        source: str,
        project_id: str | None,
        title: str | None = None,
        session_type: str = "terminal",
        title_source: str | None = None,
    ) -> _Session:
        row = _Session(
            id=f"sess-{external_id}",
            external_id=external_id,
            machine_id=machine_id,
            source=source,
            project_id=project_id or "",
            title=title,
            title_source=title_source,
            session_type=session_type,
        )
        self.rows[row.id] = row
        self.events.append(("session_created", row.id))
        return row

    def update_status(self, session_id: str, status: str) -> _Session | None:
        row = self.rows.get(session_id)
        if row is None:
            return None
        row.status = status
        self.events.append(
            (("session_expired" if status == "expired" else "session_updated"), session_id)
        )
        return row

    def delete(self, session_id: str) -> bool:
        if self.delete_error is not None:
            raise self.delete_error
        if session_id not in self.rows:
            return False
        del self.rows[session_id]
        self.events.append(("session_deleted", session_id))
        return True


class _Backend:
    def __init__(
        self,
        *,
        available: bool = True,
        capabilities: dict[str, bool] | None = None,
        pages: list[dict[str, Any]] | None = None,
    ) -> None:
        self._available = available
        self.capabilities = capabilities or {}
        self._pages = pages or []
        self.start_calls = 0
        self.list_calls = 0
        self.clients: list[_FakeRouteACPClient] = []

        def _factory(*_args: Any, **kwargs: Any) -> _FakeRouteACPClient:
            client = _FakeRouteACPClient(**kwargs)
            client.session_capabilities = dict(self.capabilities)
            self.clients.append(client)
            return client

        self.acp_client_cls = _factory

    @property
    def closed(self) -> list[str]:
        return [item for client in self.clients for item in client.closed]

    @property
    def deleted(self) -> list[str]:
        return [item for client in self.clients for item in client.deleted]

    async def start(self) -> None:
        self.start_calls += 1

    def health(self) -> SimpleNamespace:
        return SimpleNamespace(available=self._available)

    async def list_sessions(
        self, *, cwd: str | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        self.list_calls += 1
        index = 0 if cursor is None else int(cursor)
        if index >= len(self._pages):
            return {"sessions": [], "nextCursor": None}
        return self._pages[index]


class _FakeRouteACPClient:
    def __init__(self, cwd: str | None = None, **_kwargs: Any) -> None:
        self.cwd = cwd
        self.closed: list[str] = []
        self.deleted: list[str] = []
        self.session_capabilities: dict[str, bool] = {}

    async def start(self, **_kwargs: Any) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def close_session(self, session_id: str) -> dict[str, Any]:
        self.closed.append(session_id)
        return {}

    async def delete_session(self, session_id: str) -> dict[str, Any]:
        self.deleted.append(session_id)
        return {}


class _RM:
    def __init__(self, backends: dict[str, _Backend]) -> None:
        self._backends = backends
        self.cache: dict[tuple[str, str], dict[str, Any]] = {}

    def acp_backends(self) -> dict[str, _Backend]:
        return dict(self._backends)

    def acp_backend(self, provider: str) -> _Backend | None:
        return self._backends.get(provider)

    def acp_session_capabilities(self, provider: str) -> dict[str, bool]:
        backend = self._backends.get(provider)
        return dict(backend.capabilities) if backend else {}

    def cache_acp_session_info(self, provider: str, session_id: str, info: dict[str, Any]) -> None:
        self.cache[(provider, session_id)] = dict(info)

    def get_acp_session_info(self, provider: str, session_id: str) -> dict[str, Any] | None:
        cached = self.cache.get((provider, session_id))
        return dict(cached) if cached is not None else None


def _resolve_project_id(_project_id: str | None, cwd: str | None) -> str:
    if cwd == "/repo":
        return "proj-1"
    raise ValueError(f"no project for {cwd}")


def _server(sm: _SM, rm: _RM | None) -> SimpleNamespace:
    async def run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    return SimpleNamespace(
        session_manager=sm,
        services=SimpleNamespace(web_chat_runtime_manager=rm),
        resolve_project_id=_resolve_project_id,
        run_db=run_db,
    )


def _client(sm: _SM, rm: _RM | None) -> TestClient:
    app = FastAPI()
    app.include_router(create_sessions_router(_server(sm, rm)))
    return TestClient(app)


def _acp_row(source: str = "qwen") -> _Session:
    return _Session(id="sess-1", external_id="acp-xyz", source=source)


# ---------------------------------------------------------------------------
# removed discovery surface
# ---------------------------------------------------------------------------


def test_discovery_route_is_absent_and_listing_does_not_start_provider() -> None:
    sm = _SM()
    backend = _Backend(
        capabilities={"list": True},
        pages=[
            {"sessions": [{"sessionId": "s1", "cwd": "/repo", "title": "One"}], "nextCursor": None}
        ],
    )
    client = _client(sm, _RM({"qwen": backend}))

    assert client.post("/api/sessions/acp/discover", json={}).status_code == 404
    assert client.get("/api/sessions").status_code == 200
    assert backend.start_calls == 0
    assert backend.list_calls == 0


# ---------------------------------------------------------------------------
# close / delete response shapes + status codes
# ---------------------------------------------------------------------------


def test_close_returns_session_and_single_broadcast() -> None:
    sm = _SM([_acp_row()])
    backend = _Backend(capabilities={"close": True})
    client = _client(sm, _RM({"qwen": backend}))

    resp = client.post("/api/sessions/sess-1/acp/close")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"session"}
    assert body["session"]["status"] == "expired"
    assert body["session"]["acp"]["capabilities"]["close"] is True
    assert backend.closed == ["acp-xyz"]
    assert sm.events == [("session_expired", "sess-1")]


def test_delete_returns_session_and_single_broadcast() -> None:
    sm = _SM([_acp_row()])
    backend = _Backend(capabilities={"delete": True})
    client = _client(sm, _RM({"qwen": backend}))

    resp = client.post("/api/sessions/sess-1/acp/delete")

    assert resp.status_code == 200
    assert set(resp.json()) == {"session", "disposition"}
    assert resp.json()["disposition"] == "removed"
    assert backend.deleted == ["acp-xyz"]
    assert sm.events == [("session_deleted", "sess-1")]


def test_close_unsupported_capability_returns_409() -> None:
    sm = _SM([_acp_row()])
    client = _client(sm, _RM({"qwen": _Backend(capabilities={})}))

    resp = client.post("/api/sessions/sess-1/acp/close")

    assert resp.status_code == 409


def test_close_provider_unavailable_returns_503() -> None:
    sm = _SM([_acp_row()])
    backend = _Backend(available=False, capabilities={"close": True})
    client = _client(sm, _RM({"qwen": backend}))

    resp = client.post("/api/sessions/sess-1/acp/close")

    assert resp.status_code == 503


def test_close_unknown_session_returns_404() -> None:
    client = _client(_SM(), _RM({"qwen": _Backend(capabilities={"close": True})}))

    resp = client.post("/api/sessions/ghost/acp/close")

    assert resp.status_code == 404


def test_close_non_acp_target_returns_400() -> None:
    sm = _SM([_acp_row(source="claude")])  # claude web_chat is not ACP-routed
    client = _client(sm, _RM({"qwen": _Backend(capabilities={"close": True})}))

    resp = client.post("/api/sessions/sess-1/acp/close")

    assert resp.status_code == 400


def test_delete_unsupported_capability_returns_409() -> None:
    sm = _SM([_acp_row()])
    client = _client(sm, _RM({"qwen": _Backend(capabilities={})}))

    resp = client.post("/api/sessions/sess-1/acp/delete")

    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# acp enrichment on GET /api/sessions
# ---------------------------------------------------------------------------


def test_list_sessions_attaches_acp_block_only_for_acp_rows() -> None:
    qwen_row = _Session(id="s-qwen", external_id="acp-1", source="qwen")
    claude_row = _Session(id="s-claude", external_id="ext-2", source="claude")
    tmux_row = _Session(id="s-tmux", external_id="ext-3", source="claude", session_type="terminal")
    sm = _SM([qwen_row, claude_row, tmux_row])
    backend = _Backend(capabilities={"resume": True, "close": True})
    client = _client(sm, _RM({"qwen": backend}))

    resp = client.get("/api/sessions")

    assert resp.status_code == 200
    by_id = {s["id"]: s for s in resp.json()["sessions"]}
    assert by_id["s-qwen"]["acp"] == {
        "capabilities": {"resume": True, "close": True, "delete": False},
        "additional_directories": [],
    }
    assert "acp" not in by_id["s-claude"]  # non-ACP provider
    assert "acp" not in by_id["s-tmux"]  # not web_chat


def test_list_sessions_acp_block_present_with_empty_caps_without_runtime_manager() -> None:
    sm = _SM([_Session(id="s-qwen", external_id="acp-1", source="qwen")])
    client = _client(sm, None)

    resp = client.get("/api/sessions")

    assert resp.status_code == 200
    # No runtime manager → qwen still classifies as ACP via the fallback set, so
    # the chip stays stable; capabilities degrade to all-false (zero buttons).
    assert resp.json()["sessions"][0]["acp"] == {
        "capabilities": {"resume": False, "close": False, "delete": False},
        "additional_directories": [],
    }


def test_list_sessions_acp_block_includes_additional_directories() -> None:
    row = _Session(id="s-qwen", external_id="acp-1", source="qwen")
    sm = _SM([row])
    backend = _Backend(capabilities={"resume": True})
    rm = _RM({"qwen": backend})
    rm.cache_acp_session_info(
        "qwen", "acp-1", {"sessionId": "acp-1", "additionalDirectories": ["/extra"]}
    )
    client = _client(sm, rm)

    resp = client.get("/api/sessions")

    block = resp.json()["sessions"][0]["acp"]
    assert block["additional_directories"] == ["/extra"]
