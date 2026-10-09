"""The release_composer valve over HTTP."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.servers.routes.sessions import create_sessions_router
from gobby.storage.terminals import Terminal
from gobby.terminals.composer_ledger import ComposerLedger, LedgerRead
from tests.terminals.fakes import MemoryTerminalStore, make_memory_terminal

pytestmark = pytest.mark.unit

_SEAT = "seat-session"
_ROUTE = f"/api/sessions/{_SEAT}/release-composer"


def _client() -> tuple[TestClient, Terminal]:
    seat = make_memory_terminal()
    seat.session_id = _SEAT
    server = MagicMock()
    server.services = SimpleNamespace(terminal_manager=MemoryTerminalStore(seat))

    async def run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    server.run_db = AsyncMock(side_effect=run_db)
    app = FastAPI()
    app.include_router(create_sessions_router(server))
    return TestClient(app), seat


def test_operator_releases_a_pre_ledger_seat(composer_ledger: ComposerLedger) -> None:
    client, seat = _client()

    response = client.post(_ROUTE)

    assert response.status_code == 200
    assert response.json() == {"status": "released", "session_id": _SEAT, "terminal_id": seat.id}
    assert composer_ledger.read(seat.id) == LedgerRead("empty")


def test_the_target_session_cannot_release_itself(composer_ledger: ComposerLedger) -> None:
    client, seat = _client()

    response = client.post(_ROUTE, headers={"X-Gobby-Session-Id": _SEAT})

    assert response.status_code == 403
    assert response.json() == {"detail": "A session cannot release its own composer"}
    assert composer_ledger.read(seat.id) == LedgerRead("blocked", "untracked")
