"""Tests for plan approval WebSocket handlers."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.servers.websocket.handlers.plan_approval import handle_recovered_plan_approval

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_recovered_plan_approval_finds_droid_external_id_fallback() -> None:
    session_manager = MagicMock()
    session_manager.db = None
    session_manager.get.return_value = None
    droid_session = MagicMock()
    droid_session.id = "db-droid-session"

    def find_active_by_external_id(external_id: str, source: str):
        if external_id == "external-droid-session" and source == "droid":
            return droid_session
        return None

    session_manager.find_active_by_external_id.side_effect = find_active_by_external_id
    mixin = MagicMock()
    mixin.session_manager = session_manager
    websocket = AsyncMock()
    websocket.send = AsyncMock()

    await handle_recovered_plan_approval(
        mixin,
        websocket,
        "external-droid-session",
        {"decision": "approve"},
    )

    session_manager.find_active_by_external_id.assert_any_call("external-droid-session", "droid")
    assert session_manager.find_active_by_external_id.call_count >= 1
    assert session_manager.find_active_by_external_id.call_args is not None
    session_manager.update_chat_mode.assert_called_once_with("db-droid-session", "normal")
    assert session_manager.update_chat_mode.call_count == 1
    assert session_manager.update_chat_mode.call_args is not None
    sent_messages = [json.loads(call.args[0]) for call in websocket.send.await_args_list]
    assert sent_messages == [
        {
            "type": "mode_changed",
            "conversation_id": "external-droid-session",
            "mode": "normal",
            "reason": "plan_approved",
        },
        {
            "type": "plan_approved_recovered",
            "conversation_id": "external-droid-session",
        },
    ]
