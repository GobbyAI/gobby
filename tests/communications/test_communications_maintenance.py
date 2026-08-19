import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.runner_maintenance import cleanup_comms_messages_loop
from gobby.storage.communications import LocalCommunicationsStore
from gobby.storage.inter_session_messages import InterSessionMessageManager


@pytest.fixture(autouse=True)
def mock_attachment_manager() -> Iterator[MagicMock]:
    manager = MagicMock()
    with patch("gobby.communications.attachments.AttachmentManager", return_value=manager):
        yield manager


@pytest.mark.asyncio
async def test_cleanup_comms_messages_loop(
    mock_attachment_manager: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    attachment_paths = ["/attachments/first.txt", "/attachments/second.txt"]
    run_db_calls: list[tuple[Callable[..., object], tuple[object, ...], dict[str, object]]] = []

    async def run_db(func: Callable[..., object], *args: object, **kwargs: object) -> object:
        run_db_calls.append((func, args, kwargs))
        owner = getattr(func, "__self__", None)
        if isinstance(owner, LocalCommunicationsStore):
            return 5, attachment_paths
        if isinstance(owner, InterSessionMessageManager):
            return 3
        raise AssertionError(f"Unexpected cleanup boundary: {func!r}")

    shutdown_checks = 0

    def is_shutdown_requested() -> bool:
        nonlocal shutdown_checks
        shutdown_checks += 1
        return shutdown_checks > 1

    mock_attachment_manager.delete_paths.return_value = 2
    mock_attachment_manager.cleanup_old.return_value = 4
    sleep = AsyncMock()
    expected_cutoff_start = datetime.now(UTC) - timedelta(days=30)

    with caplog.at_level(logging.INFO, logger="gobby.runner_maintenance"):
        await cleanup_comms_messages_loop(
            object(),
            is_shutdown_requested,
            retention_days=30,
            run_db=run_db,
            interval_seconds=123,
            startup_delay_seconds=0,
            sleep=sleep,
        )

    expected_cutoff_end = datetime.now(UTC) - timedelta(days=30)
    assert len(run_db_calls) == 2
    comms_call, mailbox_call = run_db_calls
    assert isinstance(getattr(comms_call[0], "__self__", None), LocalCommunicationsStore)
    assert isinstance(getattr(mailbox_call[0], "__self__", None), InterSessionMessageManager)
    comms_cutoff = comms_call[1][0]
    mailbox_cutoff = mailbox_call[1][0]
    assert isinstance(comms_cutoff, datetime)
    assert mailbox_cutoff is comms_cutoff
    assert expected_cutoff_start <= comms_cutoff <= expected_cutoff_end
    assert comms_call[2] == mailbox_call[2] == {"limit": 500}

    mock_attachment_manager.delete_paths.assert_called_once_with(attachment_paths)
    mock_attachment_manager.cleanup_old.assert_called_once_with(days=30, limit=500)
    assert caplog.messages == [
        "Comms message cleanup: removed 5 old messages",
        "Comms attachment cleanup: removed 2 files for retained messages",
        "Mailbox message cleanup: removed 3 old delivered messages",
        "Comms attachment cleanup: removed 4 old local files",
    ]
    sleep.assert_awaited_once_with(123)
    assert shutdown_checks == 2
