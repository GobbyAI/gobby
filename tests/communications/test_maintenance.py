from unittest.mock import MagicMock, patch

import pytest

from gobby.runner_maintenance import cleanup_comms_messages_loop


@pytest.mark.asyncio
async def test_cleanup_comms_messages_loop():
    # Mock dependencies
    db_mock = MagicMock()
    conn_mock = MagicMock()
    cursor_mock = MagicMock()

    # Setup mock chain
    db_mock.transaction.return_value.__enter__.return_value = conn_mock
    conn_mock.execute.return_value = cursor_mock
    cursor_mock.rowcount = 5  # Simulate 5 deleted messages

    # Control loop execution: run once then exit
    shutdown_requested = [False, True]

    def is_shutdown():
        return shutdown_requested.pop(0) if shutdown_requested else True

    # Mock asyncio.sleep to not actually sleep
    with patch("asyncio.sleep") as sleep_mock:
        # Run the loop
        await cleanup_comms_messages_loop(db_mock, is_shutdown, retention_days=30)

        # Verify sleep was called with 24 hours
        sleep_mock.assert_called_once_with(24 * 60 * 60)

        # Verify DB execute was called with correct SQL
        assert conn_mock.execute.call_count == 1
        call_args = conn_mock.execute.call_args
        assert "DELETE FROM comms_messages WHERE created_at < %s" in call_args[0][0]

        # Verify attachment cleanup was called
