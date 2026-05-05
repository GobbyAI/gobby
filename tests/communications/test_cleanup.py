"""Tests for communications message cleanup and retention."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.runner_maintenance import cleanup_comms_messages_loop


@pytest.mark.asyncio
async def test_cleanup_deletes_old_messages():
    """cleanup_comms_messages_loop deletes messages older than retention_days."""
    mock_store = MagicMock()
    mock_store.delete_messages_before.return_value = 5

    shutdown_calls = iter([False, True])

    def is_shutdown() -> bool:
        return next(shutdown_calls, True)

    with (
        patch("asyncio.sleep", new_callable=AsyncMock) as sleep_mock,
        patch(
            "gobby.storage.communications.LocalCommunicationsStore",
            return_value=mock_store,
        ),
    ):
        await cleanup_comms_messages_loop(MagicMock(), is_shutdown, retention_days=30)

    sleep_mock.assert_called_once_with(24 * 60 * 60)
    mock_store.delete_messages_before.assert_called_once()
    cutoff_arg = mock_store.delete_messages_before.call_args[0][0]
    assert isinstance(cutoff_arg, datetime)
    # Cutoff should be approximately 30 days ago
    expected = datetime.now(UTC) - timedelta(days=30)
    assert abs((cutoff_arg - expected).total_seconds()) < 5


@pytest.mark.asyncio
async def test_cleanup_respects_retention_days():
    """Different retention_days values produce different cutoff dates."""
    mock_store = MagicMock()
    mock_store.delete_messages_before.return_value = 0

    shutdown_calls = iter([False, True])

    def is_shutdown() -> bool:
        return next(shutdown_calls, True)

    with (
        patch("asyncio.sleep", new_callable=AsyncMock),
        patch(
            "gobby.storage.communications.LocalCommunicationsStore",
            return_value=mock_store,
        ),
    ):
        await cleanup_comms_messages_loop(MagicMock(), is_shutdown, retention_days=7)

    cutoff_arg = mock_store.delete_messages_before.call_args[0][0]
    expected = datetime.now(UTC) - timedelta(days=7)
    assert abs((cutoff_arg - expected).total_seconds()) < 5


@pytest.mark.asyncio
async def test_cleanup_runs_on_interval():
    """Cleanup loop sleeps for 24 hours between iterations."""
    mock_store = MagicMock()
    mock_store.delete_messages_before.return_value = 0

    call_count = 0
    max_calls = 3

    def is_shutdown() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count > max_calls

    with (
        patch("asyncio.sleep", new_callable=AsyncMock) as sleep_mock,
        patch(
            "gobby.storage.communications.LocalCommunicationsStore",
            return_value=mock_store,
        ),
    ):
        await cleanup_comms_messages_loop(MagicMock(), is_shutdown, retention_days=30)

    # Should have called sleep for each iteration
    for call in sleep_mock.call_args_list:
        assert call[0][0] == 24 * 60 * 60


@pytest.mark.asyncio
async def test_cleanup_handles_cancelled_error():
    """Cleanup loop exits cleanly on CancelledError."""
    mock_store = MagicMock()

    with (
        patch("asyncio.sleep", side_effect=asyncio.CancelledError),
        patch(
            "gobby.storage.communications.LocalCommunicationsStore",
            return_value=mock_store,
        ),
    ):
        result = await cleanup_comms_messages_loop(MagicMock(), lambda: False, retention_days=30)

    assert result is None
    assert mock_store.delete_messages_before.call_count == 0


@pytest.mark.asyncio
async def test_cleanup_handles_db_error_gracefully():
    """Cleanup loop continues on database errors."""
    mock_store = MagicMock()
    mock_store.delete_messages_before.side_effect = [RuntimeError("DB locked"), 3]

    call_count = 0

    def is_shutdown() -> bool:
        nonlocal call_count
        call_count += 1
        return call_count > 2

    with (
        patch("asyncio.sleep", new_callable=AsyncMock),
        patch(
            "gobby.storage.communications.LocalCommunicationsStore",
            return_value=mock_store,
        ),
    ):
        # Should not raise despite first call failing
        await cleanup_comms_messages_loop(MagicMock(), is_shutdown, retention_days=30)

    assert mock_store.delete_messages_before.call_count == 2


@pytest.mark.asyncio
async def test_cleanup_zero_deleted_no_error():
    """Cleanup loop handles zero deleted messages without error."""
    mock_store = MagicMock()
    mock_store.delete_messages_before.return_value = 0

    shutdown_calls = iter([False, True])

    def is_shutdown() -> bool:
        return next(shutdown_calls, True)

    with (
        patch("asyncio.sleep", new_callable=AsyncMock),
        patch(
            "gobby.storage.communications.LocalCommunicationsStore",
            return_value=mock_store,
        ),
    ):
        await cleanup_comms_messages_loop(MagicMock(), is_shutdown, retention_days=30)

    mock_store.delete_messages_before.assert_called_once()
    assert mock_store.delete_messages_before.call_count == 1
    assert mock_store.delete_messages_before.call_args is not None
