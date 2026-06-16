from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from gobby.hooks.envelope_dedupe import (
    ENVELOPE_ID_HEADER,
    claim_envelope_processing,
    is_envelope_processed,
    mark_envelope_processed,
)
from gobby.hooks.inbox import (
    _compute_sleep_seconds,
    _load_envelope,
    _quarantine_file,
    drain_hook_inbox_once,
)

pytestmark = pytest.mark.unit


def _valid_envelope() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "enqueued_at": "2026-04-16T12:00:00Z",
        "critical": False,
        "hook_type": "session-start",
        "input_data": {},
        "source": "claude",
        "headers": {},
    }


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
    assert is_envelope_processed(
        "n-0000000000001-abcd",
        processed_dir=inbox_dir / "processed",
    )
    mock_client.post.assert_awaited_once_with(
        "/api/hooks/execute",
        json=envelope,
        headers={
            **envelope["headers"],
            ENVELOPE_ID_HEADER: "n-0000000000001-abcd",
        },
    )


@pytest.mark.asyncio
async def test_drain_hook_inbox_skips_already_processed_envelope(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    envelope = {
        "schema_version": 1,
        "enqueued_at": "2026-04-16T12:00:00Z",
        "critical": False,
        "hook_type": "session-start",
        "input_data": {"session_id": "sess-123"},
        "source": "claude",
        "headers": {},
    }
    envelope_path = inbox_dir / "n-0000000000001-abcd.json"
    envelope_path.write_text(json.dumps(envelope))
    mark_envelope_processed(
        "n-0000000000001-abcd",
        processed_dir=inbox_dir / "processed",
    )

    mock_client = AsyncMock()
    mock_client.post = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("gobby.hooks.inbox.httpx.AsyncClient", return_value=mock_client):
        replayed = await drain_hook_inbox_once(FastAPI(), inbox_dir=inbox_dir)

    assert replayed == 0
    assert not envelope_path.exists()
    mock_client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_drain_hook_inbox_skips_fresh_envelope(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    timestamp_ms = int(datetime.now(UTC).timestamp() * 1000)
    envelope_path = inbox_dir / f"n-{timestamp_ms}-abcd.json"
    envelope_path.write_text(json.dumps(_valid_envelope()))

    mock_client = AsyncMock()
    mock_client.post = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("gobby.hooks.inbox.httpx.AsyncClient", return_value=mock_client):
        replayed = await drain_hook_inbox_once(FastAPI(), inbox_dir=inbox_dir)

    assert replayed == 0
    assert envelope_path.exists()
    mock_client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_drain_hook_inbox_skips_active_processing_marker(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    envelope_id = "n-0000000000001-abcd"
    envelope_path = inbox_dir / f"{envelope_id}.json"
    envelope_path.write_text(json.dumps(_valid_envelope()))
    claim_envelope_processing(envelope_id, processed_dir=inbox_dir / "processed")

    mock_client = AsyncMock()
    mock_client.post = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("gobby.hooks.inbox.httpx.AsyncClient", return_value=mock_client):
        replayed = await drain_hook_inbox_once(FastAPI(), inbox_dir=inbox_dir)

    assert replayed == 0
    assert envelope_path.exists()
    mock_client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_drain_hook_inbox_clears_stale_processing_marker_and_replays(
    tmp_path: Path,
) -> None:
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    envelope_id = "n-0000000000001-abcd"
    envelope_path = inbox_dir / f"{envelope_id}.json"
    envelope_path.write_text(json.dumps(_valid_envelope()))
    processed_dir = inbox_dir / "processed"
    claim_envelope_processing(envelope_id, processed_dir=processed_dir)
    marker_path = next(processed_dir.glob("*.json"))
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["claimed_at"] = (datetime.now(UTC) - timedelta(seconds=121)).isoformat()
    marker_path.write_text(json.dumps(marker), encoding="utf-8")

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
    assert is_envelope_processed(envelope_id, processed_dir=processed_dir)
    mock_client.post.assert_awaited_once()


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


@pytest.mark.asyncio
async def test_drain_hook_inbox_keeps_conflict_replay_files(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    envelope_path = inbox_dir / "n-0000000000001-abcd.json"
    envelope_path.write_text(json.dumps(_valid_envelope()))

    mock_response = MagicMock()
    mock_response.status_code = 409
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("gobby.hooks.inbox.httpx.AsyncClient", return_value=mock_client):
        replayed = await drain_hook_inbox_once(FastAPI(), inbox_dir=inbox_dir)

    assert replayed == 0
    assert envelope_path.exists()
    mock_client.post.assert_awaited_once()


def test_load_envelope_skips_quarantine_failure_without_raising(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    envelope_path = inbox_dir / "n-0000000000001-abcd.json"
    envelope_path.write_text("{invalid", encoding="utf-8")

    with caplog.at_level("WARNING"):
        with patch("gobby.hooks.inbox.Path.write_text", side_effect=OSError("disk full")):
            envelope = _load_envelope(envelope_path)

    assert envelope is None
    assert not envelope_path.exists()
    assert (inbox_dir / "quarantine" / envelope_path.name).exists()
    assert "Skipping hook inbox file" in caplog.text


def test_load_envelope_quarantines_non_utf8_files(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    envelope_path = inbox_dir / "n-0000000000001-abcd.json"
    envelope_path.write_bytes(b"\xff\xfe\x00bad-json")

    envelope = _load_envelope(envelope_path)

    assert envelope is None
    assert not envelope_path.exists()
    quarantined = inbox_dir / "quarantine" / envelope_path.name
    assert quarantined.read_bytes() == b"\xff\xfe\x00bad-json"
    meta = json.loads((inbox_dir / "quarantine" / f"{envelope_path.name}.meta.json").read_text())
    assert meta["reason"] == "invalid_json"


def test_quarantine_missing_file_is_handled_as_race(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    inbox_dir = tmp_path / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    envelope_path = inbox_dir / "n-0000000000001-abcd.json"

    with caplog.at_level("DEBUG", logger="gobby.hooks.inbox"):
        quarantined = _quarantine_file(envelope_path, reason="invalid_json", detail="missing")

    assert quarantined is True
    assert "disappeared before quarantine" in caplog.text
    assert "Failed to quarantine hook inbox file" not in caplog.text


def test_compute_sleep_seconds_clamps_negative_jitter() -> None:
    with patch("gobby.hooks.inbox._JITTER_RANDOM.uniform", return_value=-10.0):
        assert _compute_sleep_seconds(interval_seconds=5, jitter_seconds=10.0) == 0.0
