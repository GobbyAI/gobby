from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from gobby.hooks.inbox import drain_hook_inbox_once

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_drain_hook_inbox_replays_full_envelope_with_promoted_headers(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    envelope = {
        "schema_version": 1,
        "enqueued_at": "2026-04-16T12:00:00Z",
        "critical": False,
        "hook_type": "session-start",
        "input_data": {"session_id": "sess-123"},
        "source": "claude",
        "headers": {
            "X-Gobby-Project-Id": "proj-123",
            "X-Gobby-Session-Id": "sess-123",
        },
    }
    envelope_path = inbox_dir / "n-0000000000001-abcd.json"
    envelope_path.write_text(json.dumps(envelope))

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("gobby.hooks.inbox.httpx.AsyncClient", return_value=mock_client):
        replayed = await drain_hook_inbox_once(FastAPI(), inbox_dir=inbox_dir)

    assert replayed == 1
    assert not envelope_path.exists()
    mock_client.post.assert_awaited_once_with(
        "/api/hooks/execute",
        json=envelope,
        headers=envelope["headers"],
    )


@pytest.mark.asyncio
async def test_drain_hook_inbox_keeps_failed_replay_files(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    envelope = {
        "schema_version": 1,
        "enqueued_at": "2026-04-16T12:00:00Z",
        "critical": False,
        "hook_type": "session-start",
        "input_data": {},
        "source": "claude",
        "headers": {},
    }
    envelope_path = inbox_dir / "n-0000000000001-abcd.json"
    envelope_path.write_text(json.dumps(envelope))

    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("gobby.hooks.inbox.httpx.AsyncClient", return_value=mock_client):
        replayed = await drain_hook_inbox_once(FastAPI(), inbox_dir=inbox_dir)

    assert replayed == 0
    assert envelope_path.exists()
