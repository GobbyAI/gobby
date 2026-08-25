"""Tests for the communications polling manager."""

import asyncio
import logging
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.communications.polling import PollingManager
from tests._timing import drain_asyncio_tasks, wait_for_async_condition

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_manager():
    manager = MagicMock()
    manager.handle_inbound_messages = AsyncMock()
    return manager


@pytest.fixture
def mock_adapter():
    adapter = MagicMock()
    adapter.poll = AsyncMock(return_value=[])
    adapter.acknowledge_messages = AsyncMock()
    return adapter


@pytest.fixture
def polling_manager(mock_manager):
    return PollingManager(manager=mock_manager, default_interval=1)


@pytest.mark.asyncio
async def test_start_polling_creates_task(polling_manager, mock_adapter):
    """start_polling should create and store an asyncio task."""
    polling_manager.start_polling("test-channel", mock_adapter)

    assert polling_manager.is_polling("test-channel")
    assert "test-channel" in polling_manager._tasks
    assert not polling_manager._tasks["test-channel"].done()

    # Cleanup
    polling_manager.stop_all()


@pytest.mark.asyncio
async def test_stop_polling_cancels_task(polling_manager, mock_adapter):
    """stop_polling should cancel the background task."""
    polling_manager.start_polling("test-channel", mock_adapter)
    assert polling_manager.is_polling("test-channel")

    task = polling_manager._tasks["test-channel"]
    polling_manager.stop_polling("test-channel")

    await drain_asyncio_tasks()

    assert not polling_manager.is_polling("test-channel")
    assert "test-channel" not in polling_manager._tasks
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_stop_all_cancels_all_tasks(polling_manager, mock_adapter):
    """stop_all should cancel all running polling tasks."""
    polling_manager.start_polling("channel-1", mock_adapter)
    polling_manager.start_polling("channel-2", mock_adapter)

    assert len(polling_manager._tasks) == 2

    polling_manager.stop_all()

    assert len(polling_manager._tasks) == 0
    assert not polling_manager.is_polling("channel-1")
    assert not polling_manager.is_polling("channel-2")


@pytest.mark.asyncio
async def test_poll_loop_calls_adapter(polling_manager, mock_adapter, mock_manager):
    """poll loop should call adapter.poll() and handle messages."""
    msg1 = MagicMock()
    call_count = 0
    mock_manager.handle_inbound_messages.return_value = [msg1]

    async def poll_side_effect():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [msg1]
        return []

    mock_adapter.poll.side_effect = poll_side_effect

    polling_manager.start_polling("test-channel", mock_adapter, interval=0)

    await wait_for_async_condition(
        lambda: mock_adapter.acknowledge_messages.called,
        description="inbound message acknowledgement",
    )

    polling_manager.stop_all()

    # Verify poll was called
    assert mock_adapter.poll.call_count >= 1

    # Verify messages were passed to manager
    mock_manager.handle_inbound_messages.assert_called_once_with("test-channel", [msg1])
    mock_adapter.acknowledge_messages.assert_awaited_once_with([msg1])


@pytest.mark.asyncio
async def test_poll_loop_logs_one_traceback_per_failure_streak(
    polling_manager: PollingManager,
    mock_adapter: MagicMock,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure streak gets one traceback; repeats are one line; success resets (#20867)."""
    monkeypatch.setattr("gobby.communications.polling.asyncio.sleep", AsyncMock())

    outcomes: Iterator[BaseException | list[object]] = iter(
        [
            Exception("first streak, failure 1"),
            Exception("first streak, failure 2"),
            [],
            Exception("second streak, failure 1"),
            asyncio.CancelledError(),
        ]
    )

    async def poll_side_effect() -> list[object]:
        outcome = next(outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    mock_adapter.poll.side_effect = poll_side_effect

    with caplog.at_level(logging.WARNING, logger="gobby.communications.polling"):
        await polling_manager._poll_loop("test-channel", mock_adapter, interval=0)

    error_records = [r for r in caplog.records if "Error polling channel" in r.message]
    assert len(error_records) == 3
    assert error_records[0].exc_info, "the streak's first failure carries the traceback"
    assert not error_records[1].exc_info, "repeat failures in a streak stay one line"
    assert "failure 2 in a row" in error_records[1].getMessage()
    assert error_records[2].exc_info, (
        "a successful poll resets the streak, so the next failure gets a traceback again"
    )


@pytest.mark.asyncio
async def test_poll_loop_names_the_exception_class_when_its_message_is_empty(
    polling_manager: PollingManager,
    mock_adapter: MagicMock,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare ``TimeoutError()`` renders as ``''``; the line names the class (#20981)."""
    monkeypatch.setattr("gobby.communications.polling.asyncio.sleep", AsyncMock())

    outcomes: Iterator[BaseException] = iter(
        [TimeoutError(), TimeoutError(), asyncio.CancelledError()]
    )

    async def poll_side_effect() -> list[object]:
        raise next(outcomes)

    mock_adapter.poll.side_effect = poll_side_effect

    with caplog.at_level(logging.WARNING, logger="gobby.communications.polling"):
        await polling_manager._poll_loop("test-channel", mock_adapter, interval=0)

    messages = [r.getMessage() for r in caplog.records if "Error polling channel" in r.message]
    assert messages == [
        "Error polling channel 'test-channel': TimeoutError (backing off 5s)",
        "Error polling channel 'test-channel': TimeoutError (failure 2 in a row, backing off 10s)",
    ]


@pytest.mark.asyncio
async def test_poll_loop_error_handling(polling_manager, mock_adapter, mock_manager):
    """poll loop should catch errors and back off without crashing."""
    call_count = 0

    async def poll_side_effect():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("Network error")
        return []

    mock_adapter.poll.side_effect = poll_side_effect

    # Start polling — backoff after error is 5s, so task will be in backoff sleep
    polling_manager.start_polling("test-channel", mock_adapter, interval=0)

    await wait_for_async_condition(
        lambda: mock_adapter.poll.call_count >= 1,
        description="first poll attempt",
    )

    # Task should still be running despite the error
    assert polling_manager.is_polling("test-channel")

    polling_manager.stop_all()

    # poll should have been called at least once
    assert mock_adapter.poll.call_count >= 1
