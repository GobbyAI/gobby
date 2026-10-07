"""HookManager shares one hub read scope per hook event (#23721)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.hook_manager import HookManager
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import get_machine_id

pytestmark = pytest.mark.unit


def _event() -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="read-scope-external",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={},
        machine_id="21000000-0000-4000-8000-000000000002",
    )


@pytest.fixture
def session_id(session_manager: SessionManager, sample_project: dict[str, Any]) -> str:
    return session_manager.register(
        external_id="read-scope-session",
        machine_id=get_machine_id(),
        source="claude",
        project_id=sample_project["id"],
    ).id


@pytest.fixture
def row_reads(session_manager: SessionManager, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    fetchone: Callable[..., Any] = session_manager.db.fetchone

    def counting_fetchone(sql: str, params: Any = ()) -> Any:
        if sql.startswith("SELECT * FROM sessions LEFT JOIN"):
            reads.append(sql)
        return fetchone(sql, params)

    monkeypatch.setattr(session_manager.db, "fetchone", counting_fetchone)
    return reads


def _two_reads(session_manager: SessionManager, session_id: str) -> HookResponse:
    session_manager.get(session_id)
    session_manager.get(session_id)
    return HookResponse(decision="allow")


def test_handle_reads_session_row_once_per_event(
    manager_with_mocks: HookManager,
    session_manager: SessionManager,
    session_id: str,
    row_reads: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        manager_with_mocks,
        "_handle_internal",
        lambda event: _two_reads(session_manager, session_id),
    )

    manager_with_mocks.handle(_event())
    manager_with_mocks.handle(_event())

    assert len(row_reads) == 2


def test_handle_async_reads_session_row_once_per_event(
    manager_with_mocks: HookManager,
    session_manager: SessionManager,
    session_id: str,
    row_reads: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def handle_internal_async(event: HookEvent) -> HookResponse:
        return await asyncio.to_thread(_two_reads, session_manager, session_id)

    monkeypatch.setattr(manager_with_mocks, "_handle_internal_async", handle_internal_async)

    asyncio.run(manager_with_mocks.handle_async(_event()))

    assert len(row_reads) == 1
