"""Unit tests for ``ACPSessionLifecycleService``.

DB-free: a faithful in-memory ``SessionManager`` stand-in models the CRUD seam
(``find_by_external_id`` / ``register`` / ``update_title`` / ``update_status`` /
``delete`` / ``get``) and records the broadcast events those methods emit, so we
can assert single-broadcast behavior and the conservative-upsert rules without a
Postgres hub.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from gobby.sessions.acp_lifecycle import (
    ACPCapabilityUnsupportedError,
    ACPProviderUnavailableError,
    ACPSessionLifecycleService,
    ACPSessionNotFoundError,
    ACPTargetNotSupportedError,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeSession:
    id: str
    external_id: str
    machine_id: str
    source: str
    project_id: str
    title: str | None = None
    title_source: str | None = None
    status: str = "active"
    session_type: str = "web_chat"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "external_id": self.external_id,
            "machine_id": self.machine_id,
            "source": self.source,
            "project_id": self.project_id,
            "title": self.title,
            "title_source": self.title_source,
            "status": self.status,
            "session_type": self.session_type,
        }


class _FakeSessionManager:
    """In-memory model of the SessionManager CRUD + broadcast seam."""

    def __init__(self) -> None:
        self.rows: dict[str, _FakeSession] = {}
        self.events: list[tuple[str, str]] = []
        self.registered: list[_FakeSession] = []
        self.title_updates: list[tuple[str, str, str | None]] = []
        self.delete_error: Exception | None = None

    def seed(self, session: _FakeSession) -> _FakeSession:
        self.rows[session.id] = session
        return session

    def get(self, session_id: str) -> _FakeSession | None:
        return self.rows.get(session_id)

    def find_by_external_id(
        self,
        external_id: str,
        machine_id: str,
        project_id: str | None,
        source: str,
        session_type: str | None = None,
    ) -> _FakeSession | None:
        for row in self.rows.values():
            if (
                row.external_id == external_id
                and row.machine_id == machine_id
                and row.source == source
                and row.project_id == project_id
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
    ) -> _FakeSession:
        session_id = f"sess-{source}-{external_id}"
        row = _FakeSession(
            id=session_id,
            external_id=external_id,
            machine_id=machine_id,
            source=source,
            project_id=project_id or "",
            title=title,
            title_source="provisional" if title is None else title_source,
            status="active",
            session_type=session_type,
        )
        self.rows[session_id] = row
        self.registered.append(row)
        self.events.append(("session_created", session_id))
        return row

    def update_title(
        self, session_id: str, title: str, *, title_source: str | None = None
    ) -> _FakeSession | None:
        row = self.rows.get(session_id)
        if row is None:
            return None
        row.title = title
        if title_source is not None:
            row.title_source = title_source
        self.title_updates.append((session_id, title, title_source))
        self.events.append(("session_updated", session_id))
        return row

    def update_status(self, session_id: str, status: str) -> _FakeSession | None:
        row = self.rows.get(session_id)
        if row is None:
            return None
        row.status = status
        event = "session_expired" if status == "expired" else "session_updated"
        self.events.append((event, session_id))
        return row

    def delete(self, session_id: str) -> bool:
        if self.delete_error is not None:
            raise self.delete_error
        if session_id not in self.rows:
            return False
        del self.rows[session_id]
        self.events.append(("session_deleted", session_id))
        return True


class _FakeBackend:
    def __init__(
        self,
        *,
        available: bool = True,
        capabilities: dict[str, bool] | None = None,
        pages: list[dict[str, Any]] | None = None,
        start_error: Exception | None = None,
        list_error: Exception | None = None,
        gate: bool = False,
    ) -> None:
        self._available = available
        self.capabilities = capabilities or {}
        self._pages = pages or []
        self._start_error = start_error
        self._list_error = list_error
        self.start_calls = 0
        self.list_calls: list[tuple[str | None, str | None]] = []
        self.closed: list[str] = []
        self.deleted: list[str] = []
        # Coalescing-test gates.
        self.entered = asyncio.Event() if gate else None
        self.release = asyncio.Event() if gate else None

    async def start(self) -> None:
        self.start_calls += 1
        if self._start_error is not None:
            raise self._start_error

    def health(self) -> SimpleNamespace:
        return SimpleNamespace(available=self._available)

    def session_capabilities(self) -> dict[str, bool]:  # pragma: no cover - unused
        return dict(self.capabilities)

    async def list_sessions(
        self, *, cwd: str | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        self.list_calls.append((cwd, cursor))
        if self._list_error is not None:
            raise self._list_error
        index = 0 if cursor is None else int(cursor)
        if index >= len(self._pages):
            return {"sessions": [], "nextCursor": None}
        return self._pages[index]

    async def close_session(self, session_id: str) -> dict[str, Any]:
        self.closed.append(session_id)
        return {}

    async def delete_session(self, session_id: str) -> dict[str, Any]:
        self.deleted.append(session_id)
        return {}


class _FakeRuntimeManager:
    def __init__(self, backends: dict[str, _FakeBackend]) -> None:
        self._backends = backends
        self.cache: dict[tuple[str, str], dict[str, Any]] = {}

    def acp_backends(self) -> dict[str, _FakeBackend]:
        return dict(self._backends)

    def acp_backend(self, provider: str) -> _FakeBackend | None:
        return self._backends.get(provider)

    def acp_session_capabilities(self, provider: str) -> dict[str, bool]:
        backend = self._backends.get(provider)
        return dict(backend.capabilities) if backend else {}

    def cache_acp_session_info(self, provider: str, session_id: str, info: dict[str, Any]) -> None:
        self.cache[(provider, session_id)] = dict(info)

    def get_acp_session_info(self, provider: str, session_id: str) -> dict[str, Any] | None:
        cached = self.cache.get((provider, session_id))
        return dict(cached) if cached is not None else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_PROJECTS = {"/repo": "proj-1"}
MACHINE = "machine-1"


def _resolve(cwd: str | None) -> str | None:
    if cwd is None:
        return None
    return _PROJECTS.get(cwd)


def _service(
    session_manager: _FakeSessionManager,
    runtime_manager: _FakeRuntimeManager | None,
    *,
    page_cap: int = 20,
) -> ACPSessionLifecycleService:
    return ACPSessionLifecycleService(
        session_manager=session_manager,
        runtime_manager=runtime_manager,
        resolve_project_id=_resolve,
        machine_id=MACHINE,
        page_cap=page_cap,
    )


def _page(*infos: dict[str, Any], next_cursor: str | None = None) -> dict[str, Any]:
    return {"sessions": list(infos), "nextCursor": next_cursor}


def _info(session_id: str, *, cwd: str = "/repo", title: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"sessionId": session_id, "cwd": cwd}
    if title is not None:
        payload["title"] = title
    return payload


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_creates_new_external_rows() -> None:
    sm = _FakeSessionManager()
    backend = _FakeBackend(
        capabilities={"list": True},
        pages=[_page(_info("s1", title="One"), _info("s2", title="Two"))],
    )
    rm = _FakeRuntimeManager({"qwen": backend})

    result = await _service(sm, rm).discover()

    assert len(result["sessions"]) == 2
    assert len(sm.registered) == 2
    assert {row.session_type for row in sm.registered} == {"web_chat"}
    assert {row.title_source for row in sm.registered} == {"native"}
    assert sm.events == [("session_created", "sess-qwen-s1"), ("session_created", "sess-qwen-s2")]
    assert result["skipped"] == []
    assert result["providers"] == [
        {
            "provider": "qwen",
            "available": True,
            "supports_list": True,
            "truncated": False,
        }
    ]
    # SessionInfo cached for later list enrichment.
    assert rm.get_acp_session_info("qwen", "s1") == {
        "sessionId": "s1",
        "cwd": "/repo",
        "title": "One",
    }


@pytest.mark.asyncio
async def test_discover_uses_machine_id_factory_per_new_row() -> None:
    sm = _FakeSessionManager()
    backend = _FakeBackend(
        capabilities={"list": True},
        pages=[_page(_info("s1"), _info("s2"))],
    )
    rm = _FakeRuntimeManager({"qwen": backend})
    machine_ids = iter(["legacy-missing:one", "legacy-missing:two"])
    service = ACPSessionLifecycleService(
        session_manager=sm,
        runtime_manager=rm,
        resolve_project_id=_resolve,
        machine_id_factory=lambda: next(machine_ids),
    )

    await service.discover()

    assert [row.machine_id for row in sm.registered] == [
        "legacy-missing:one",
        "legacy-missing:two",
    ]


@pytest.mark.asyncio
async def test_discover_matched_row_is_conservative_no_register_no_move() -> None:
    sm = _FakeSessionManager()
    sm.seed(
        _FakeSession(
            id="sess-qwen-s1",
            external_id="s1",
            machine_id=MACHINE,
            source="qwen",
            project_id="proj-1",
            title="Real Title",
            title_source="manual",
            status="active",
            session_type="web_chat",
        )
    )
    backend = _FakeBackend(
        capabilities={"list": True}, pages=[_page(_info("s1", title="Agent Renamed"))]
    )
    rm = _FakeRuntimeManager({"qwen": backend})

    result = await _service(sm, rm).discover()

    assert sm.registered == []  # never register() over an existing row
    assert sm.title_updates == []  # non-provisional title is not clobbered
    row = sm.rows["sess-qwen-s1"]
    assert row.title == "Real Title"
    assert row.title_source == "manual"
    assert row.status == "active"  # status/project untouched
    assert row.project_id == "proj-1"
    assert len(result["sessions"]) == 1


@pytest.mark.asyncio
async def test_discover_refreshes_only_provisional_title() -> None:
    sm = _FakeSessionManager()
    sm.seed(
        _FakeSession(
            id="sess-qwen-s1",
            external_id="s1",
            machine_id=MACHINE,
            source="qwen",
            project_id="proj-1",
            title="Qwen Session 7",
            title_source="provisional",
            status="active",
            session_type="web_chat",
        )
    )
    backend = _FakeBackend(
        capabilities={"list": True}, pages=[_page(_info("s1", title="Fix the parser"))]
    )
    rm = _FakeRuntimeManager({"qwen": backend})

    await _service(sm, rm).discover()

    assert sm.title_updates == [("sess-qwen-s1", "Fix the parser", "native")]
    row = sm.rows["sess-qwen-s1"]
    assert row.title == "Fix the parser"
    assert row.title_source == "native"
    assert row.status == "active"
    assert sm.registered == []


@pytest.mark.asyncio
async def test_discover_per_row_resilience_skips_unresolved_cwd() -> None:
    sm = _FakeSessionManager()
    backend = _FakeBackend(
        capabilities={"list": True},
        pages=[_page(_info("good", cwd="/repo"), _info("bad", cwd="/unknown"))],
    )
    rm = _FakeRuntimeManager({"qwen": backend})

    result = await _service(sm, rm).discover()

    assert len(result["sessions"]) == 1
    assert len(sm.registered) == 1
    assert result["skipped"] == [
        {"provider": "qwen", "session_id": "bad", "reason": "unresolved_cwd"}
    ]


@pytest.mark.asyncio
async def test_discover_provider_unavailable_is_skipped_not_fatal() -> None:
    sm = _FakeSessionManager()
    backend = _FakeBackend(available=False, capabilities={"list": True})
    rm = _FakeRuntimeManager({"qwen": backend})

    result = await _service(sm, rm).discover()

    assert result["sessions"] == []
    assert result["skipped"] == [{"provider": "qwen", "reason": "provider_unavailable"}]
    assert result["providers"] == [
        {
            "provider": "qwen",
            "available": False,
            "supports_list": False,
            "truncated": False,
        }
    ]


@pytest.mark.asyncio
async def test_discover_provider_without_list_contributes_nothing() -> None:
    sm = _FakeSessionManager()
    backend = _FakeBackend(capabilities={})  # qwen today: nothing advertised
    rm = _FakeRuntimeManager({"qwen": backend})

    result = await _service(sm, rm).discover()

    assert result["sessions"] == []
    assert sm.registered == []
    assert backend.list_calls == []
    assert result["providers"] == [
        {
            "provider": "qwen",
            "available": True,
            "supports_list": False,
            "truncated": False,
        }
    ]


@pytest.mark.asyncio
async def test_discover_paginates_over_next_cursor() -> None:
    sm = _FakeSessionManager()
    backend = _FakeBackend(
        capabilities={"list": True},
        pages=[
            _page(_info("s1"), next_cursor="1"),
            _page(_info("s2"), next_cursor="2"),
            _page(_info("s3")),
        ],
    )
    rm = _FakeRuntimeManager({"qwen": backend})

    result = await _service(sm, rm).discover()

    assert len(result["sessions"]) == 3
    assert [call[1] for call in backend.list_calls] == [None, "1", "2"]


@pytest.mark.asyncio
async def test_discover_respects_page_cap() -> None:
    sm = _FakeSessionManager()
    # Every page advertises another cursor → would loop forever without the cap.
    backend = _FakeBackend(
        capabilities={"list": True},
        pages=[_page(_info(f"s{i}"), next_cursor=str(i + 1)) for i in range(10)],
    )
    rm = _FakeRuntimeManager({"qwen": backend})

    result = await _service(sm, rm, page_cap=3).discover()

    assert len(backend.list_calls) == 3
    assert result["providers"][0]["truncated"] is True
    assert result["skipped"] == [{"provider": "qwen", "reason": "page_cap_reached"}]


@pytest.mark.asyncio
async def test_discover_coalesces_concurrent_runs_per_provider() -> None:
    sm = _FakeSessionManager()
    backend = _FakeBackend(capabilities={"list": True}, pages=[_page(_info("s1"))], gate=True)
    rm = _FakeRuntimeManager({"qwen": backend})
    service = _service(sm, rm)

    t1 = asyncio.create_task(service.discover())
    t2 = asyncio.create_task(service.discover())
    assert backend.entered is not None and backend.release is not None
    await asyncio.wait_for(backend.entered.wait(), timeout=1.0)
    backend.release.set()
    await asyncio.gather(t1, t2)

    assert backend.start_calls == 1
    assert len(backend.list_calls) == 1
    assert len(sm.registered) == 1  # not double-upserted


@pytest.mark.asyncio
async def test_discover_does_not_coalesce_different_cwd_scans() -> None:
    sm = _FakeSessionManager()
    backend = _FakeBackend(capabilities={"list": True}, pages=[_page(_info("s1"))], gate=True)
    rm = _FakeRuntimeManager({"qwen": backend})
    service = _service(sm, rm)

    t1 = asyncio.create_task(service.discover(cwd="/repo"))
    t2 = asyncio.create_task(service.discover(cwd="/other"))
    assert backend.entered is not None and backend.release is not None
    await asyncio.wait_for(backend.entered.wait(), timeout=1.0)
    backend.release.set()
    await asyncio.gather(t1, t2)

    assert sorted(call[0] for call in backend.list_calls) == ["/other", "/repo"]


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


def _seed_acp_row(sm: _FakeSessionManager, *, source: str = "qwen") -> _FakeSession:
    return sm.seed(
        _FakeSession(
            id="sess-1",
            external_id="acp-session-xyz",
            machine_id=MACHINE,
            source=source,
            project_id="proj-1",
            title="Work",
            title_source="manual",
            status="active",
            session_type="web_chat",
        )
    )


@pytest.mark.asyncio
async def test_close_transitions_to_expired_with_single_broadcast() -> None:
    sm = _FakeSessionManager()
    _seed_acp_row(sm)
    backend = _FakeBackend(capabilities={"close": True})
    rm = _FakeRuntimeManager({"qwen": backend})

    result = await _service(sm, rm).close("sess-1")

    assert backend.closed == ["acp-session-xyz"]
    assert sm.rows["sess-1"].status == "expired"
    assert sm.events == [("session_expired", "sess-1")]
    assert result["session"]["status"] == "expired"
    assert result["session"]["acp"] == {
        "capabilities": {"resume": False, "close": True, "delete": False},
        "additional_directories": [],
    }


@pytest.mark.asyncio
async def test_close_unsupported_capability_raises_409_error() -> None:
    sm = _FakeSessionManager()
    _seed_acp_row(sm)
    backend = _FakeBackend(capabilities={})  # close not advertised
    rm = _FakeRuntimeManager({"qwen": backend})

    with pytest.raises(ACPCapabilityUnsupportedError):
        await _service(sm, rm).close("sess-1")
    assert backend.closed == []  # never sent on the wire
    assert sm.events == []


@pytest.mark.asyncio
async def test_close_unknown_session_raises_not_found() -> None:
    sm = _FakeSessionManager()
    rm = _FakeRuntimeManager({"qwen": _FakeBackend(capabilities={"close": True})})

    with pytest.raises(ACPSessionNotFoundError):
        await _service(sm, rm).close("nope")


@pytest.mark.asyncio
async def test_close_non_acp_target_raises_target_error() -> None:
    sm = _FakeSessionManager()
    _seed_acp_row(sm, source="claude")  # claude web_chat is not ACP-routed
    rm = _FakeRuntimeManager({"qwen": _FakeBackend(capabilities={"close": True})})

    with pytest.raises(ACPTargetNotSupportedError):
        await _service(sm, rm).close("sess-1")


@pytest.mark.asyncio
async def test_close_provider_unavailable_raises_unavailable() -> None:
    sm = _FakeSessionManager()
    _seed_acp_row(sm)
    backend = _FakeBackend(available=False, capabilities={"close": True})
    rm = _FakeRuntimeManager({"qwen": backend})

    with pytest.raises(ACPProviderUnavailableError):
        await _service(sm, rm).close("sess-1")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_hard_removes_with_single_broadcast() -> None:
    sm = _FakeSessionManager()
    _seed_acp_row(sm)
    backend = _FakeBackend(capabilities={"delete": True})
    rm = _FakeRuntimeManager({"qwen": backend})

    result = await _service(sm, rm).delete("sess-1")

    assert backend.deleted == ["acp-session-xyz"]
    assert "sess-1" not in sm.rows
    assert sm.events == [("session_deleted", "sess-1")]
    assert result["session"]["id"] == "sess-1"
    assert result["disposition"] == "removed"


@pytest.mark.asyncio
async def test_delete_fk_integrity_error_falls_back_to_expire() -> None:
    sm = _FakeSessionManager()
    _seed_acp_row(sm)
    sm.delete_error = psycopg.IntegrityError("FK violation: task references session")
    backend = _FakeBackend(capabilities={"delete": True})
    rm = _FakeRuntimeManager({"qwen": backend})

    result = await _service(sm, rm).delete("sess-1")

    assert backend.deleted == ["acp-session-xyz"]
    assert sm.rows["sess-1"].status == "expired"  # fell back to expire
    assert sm.events == [("session_expired", "sess-1")]
    assert result["session"]["status"] == "expired"
    assert result["disposition"] == "expired"


@pytest.mark.asyncio
async def test_delete_unsupported_capability_raises_409_error() -> None:
    sm = _FakeSessionManager()
    _seed_acp_row(sm)
    backend = _FakeBackend(capabilities={})
    rm = _FakeRuntimeManager({"qwen": backend})

    with pytest.raises(ACPCapabilityUnsupportedError):
        await _service(sm, rm).delete("sess-1")
    assert backend.deleted == []
