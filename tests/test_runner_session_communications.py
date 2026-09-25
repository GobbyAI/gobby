"""Isolated integration tests for Telegram target fallback wiring."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.runner_broadcasting import setup_session_status_communications
from gobby.sessions.status_events import SessionStatusTransition
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _transition(session_id: str, project_id: str) -> SessionStatusTransition:
    return SessionStatusTransition(
        session_id=session_id,
        project_id=project_id,
        agent_run_id=str(uuid.uuid4()),
        status="paused",
        transitioned_at=datetime(2026, 7, 30, 12, 0, tzinfo=UTC),
        seq_num=42,
        title="Existing agent",
        source="codex",
    )


async def test_session_bridge_registers_without_websocket(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    session_manager = SessionManager(temp_db)
    delivered = asyncio.Event()
    communications_manager = MagicMock()

    async def handle_transition(*_args: object, **_kwargs: object) -> list[object]:
        delivered.set()
        return []

    communications_manager.handle_session_status_transition = AsyncMock(
        side_effect=handle_transition
    )
    daemon_loop = asyncio.get_running_loop()
    listener = setup_session_status_communications(
        session_manager,
        communications_manager,
        lambda: daemon_loop,
    )
    transition = _transition(str(uuid.uuid4()), sample_project["id"])

    await asyncio.to_thread(listener, transition)
    await asyncio.wait_for(delivered.wait(), timeout=1.0)

    assert listener in session_manager._status_transition_listeners
    communications_manager.handle_session_status_transition.assert_awaited_once_with(transition)
