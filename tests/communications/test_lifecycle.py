"""Channel activation recovery, isolated from the daemon and network."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from gobby.communications.manager import CommunicationsManager
from gobby.communications.models import ChannelConfig
from gobby.config.communications import CommunicationsConfig

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class RetryClock:
    """Let tests advance each retry without sleeping in real time."""

    def __init__(self) -> None:
        self.waits: asyncio.Queue[tuple[float, asyncio.Event]] = asyncio.Queue()

    async def sleep(self, seconds: float) -> None:
        release = asyncio.Event()
        self.waits.put_nowait((seconds, release))
        await release.wait()

    async def next_wait(self) -> tuple[float, asyncio.Event]:
        return await asyncio.wait_for(self.waits.get(), timeout=1)


@dataclass
class ChannelHarness:
    manager: CommunicationsManager
    channel: ChannelConfig
    store: MagicMock
    adapters: list[MagicMock]
    factory: MagicMock
    clock: RetryClock
    polling: MagicMock
    active: asyncio.Event

    def fail_initializations(self, error: Exception, count: int | None = None) -> None:
        factory = cast(Callable[[], MagicMock], self.factory.side_effect)

        def create_adapter() -> MagicMock:
            adapter = factory()
            if count is None or len(self.adapters) <= count:
                adapter.initialize.side_effect = error
            return adapter

        self.factory.side_effect = create_adapter


@pytest.fixture
async def harness(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ChannelHarness]:
    now = datetime.now(UTC)
    channel = ChannelConfig(
        id="channel-1",
        name="operator-telegram",
        channel_type="telegram",
        enabled=True,
        config_json={"telegram_agent_targets": {"dm:123": "session-1"}},
        created_at=now,
        updated_at=now,
    )
    store = MagicMock()
    store.list_channels.return_value = [channel]
    store.get_channel_by_name.side_effect = lambda name: next(
        (item for item in store.list_channels.return_value if item.name == name), None
    )
    store.update_channel.side_effect = lambda updated: updated
    store.delete_channel.side_effect = lambda channel_id: store.list_channels.return_value.clear()
    session_store = MagicMock()
    session_store.get.return_value = MagicMock(status="active")
    manager = CommunicationsManager(CommunicationsConfig(), store, MagicMock(), session_store)
    monkeypatch.setattr(manager, "handle_session_status_transition", AsyncMock())
    monkeypatch.setattr(manager._lifecycle, "ensure_gobby_chat_channel", AsyncMock())
    adapters: list[MagicMock] = []

    def new_adapter() -> MagicMock:
        adapter = MagicMock()
        adapter.initialize = AsyncMock()
        adapter.shutdown = AsyncMock()
        adapter.supports_webhooks = False
        adapter.supports_polling = True
        adapters.append(adapter)
        return adapter

    factory = MagicMock(side_effect=new_adapter)
    monkeypatch.setattr("gobby.communications.lifecycle._get_adapter_class", lambda kind: factory)
    clock = RetryClock()
    monkeypatch.setattr("gobby.communications.lifecycle._sleep", clock.sleep, raising=False)
    active = asyncio.Event()
    polling = MagicMock(side_effect=lambda *args: active.set())
    monkeypatch.setattr(manager._polling_manager, "start_polling", polling)
    try:
        yield ChannelHarness(manager, channel, store, adapters, factory, clock, polling, active)
    finally:
        await manager._lifecycle.stop()


async def test_transient_channel_init_failure_retries_until_active(harness: ChannelHarness) -> None:
    """Startup must recover itself, retaining useful status while offline."""
    failure = httpx.ConnectError("network unavailable")
    original_factory = cast(Callable[[], MagicMock], harness.factory.side_effect)

    def create_adapter() -> MagicMock:
        adapter = original_factory()
        if len(harness.adapters) == 1:
            adapter.initialize.side_effect = failure
        return adapter

    harness.factory.side_effect = create_adapter
    await harness.manager._lifecycle.start()
    status = harness.manager.get_channel_status(harness.channel.name)
    assert status.get("retry_pending") is True
    assert status["active"] is False
    assert status["init_error"]
    harness.adapters[0].shutdown.assert_awaited_once()

    delay, release = await harness.clock.next_wait()
    assert delay == 5
    release.set()
    await asyncio.wait_for(harness.active.wait(), timeout=1)
    status = harness.manager.get_channel_status(harness.channel.name)
    assert status["active"] is True
    assert status["init_error"] is None
    assert status["retry_pending"] is False
    assert len(harness.adapters) == 2
    assert harness.manager.attached_session(harness.channel.id, "dm:123") == "session-1"


def http_failure(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.telegram.org/botSECRET/getMe")
    return httpx.HTTPStatusError(
        f"HTTP {status}: {request.url}",
        request=request,
        response=httpx.Response(status, request=request),
    )


@pytest.mark.parametrize("failure", [ValueError("bad token"), http_failure(401), http_failure(403)])
async def test_config_error_channel_init_is_not_retried(
    harness: ChannelHarness, failure: Exception
) -> None:
    harness.fail_initializations(failure)
    await harness.manager._lifecycle.start()
    status = harness.manager.get_channel_status(harness.channel.name)
    assert status["active"] is False
    assert status["retry_pending"] is False
    assert status["next_retry_at"] is None
    expected = (
        f"HTTP {failure.response.status_code}: https://api.telegram.org/bot<redacted>/getMe"
        if isinstance(failure, httpx.HTTPStatusError)
        else str(failure)
    )
    assert status["init_error"] == expected
    assert "SECRET" not in status["init_error"]
    assert harness.clock.waits.empty()
    harness.adapters[0].shutdown.assert_awaited_once()


@pytest.mark.parametrize(
    "failure",
    [httpx.ReadError("offline"), httpx.ReadTimeout("offline"), TimeoutError(), http_failure(503)],
)
async def test_transient_failure_status_is_projected_without_secrets(
    harness: ChannelHarness, failure: Exception
) -> None:
    harness.fail_initializations(failure)
    await harness.manager._lifecycle.start()
    delay, _ = await harness.clock.next_wait()
    status = harness.manager.get_channel_status(harness.channel.name)
    payload = harness.manager.channel_to_dict(harness.channel)
    assert delay == 5
    assert status["retry_pending"] is True
    assert status["retry_attempts"] == 1
    assert datetime.fromisoformat(status["next_retry_at"]).tzinfo is not None
    assert payload["retry_pending"] is True
    assert payload["init_error"] == status["init_error"]
    assert "SECRET" not in payload["init_error"]


async def test_repeated_transient_failures_cap_backoff(harness: ChannelHarness) -> None:
    harness.fail_initializations(httpx.ConnectError("offline"))
    await harness.manager._lifecycle.start()
    for attempt, expected in enumerate([5, 10, 20, 40, 80, 160, 300, 300], start=1):
        delay, release = await harness.clock.next_wait()
        assert delay == expected
        status = harness.manager.get_channel_status(harness.channel.name)
        assert status["retry_attempts"] == attempt
        assert status["active"] is False
        release.set()
    await harness.clock.next_wait()
    assert len(harness.adapters) == 9
    for adapter in harness.adapters:
        adapter.shutdown.assert_awaited_once()


async def test_shutdown_cancels_waiting_retry(harness: ChannelHarness) -> None:
    harness.fail_initializations(httpx.ConnectError("offline"))
    await harness.manager._lifecycle.start()
    _, release = await harness.clock.next_wait()
    tasks = list(harness.manager._lifecycle._retry_tasks.values())
    await harness.manager._lifecycle.stop()
    assert all(task.cancelled() for task in tasks)
    assert harness.manager._lifecycle._retry_tasks == {}
    assert harness.manager._lifecycle._retry_state == {}
    release.set()
    assert len(harness.adapters) == 1
    assert harness.manager.get_channel_status(harness.channel.name)["retry_pending"] is False


@pytest.mark.parametrize("operation", ["stop", "disable", "remove", "rename"])
async def test_channel_change_cancels_and_awaits_initializing_retry(
    harness: ChannelHarness, operation: str
) -> None:
    harness.fail_initializations(httpx.ConnectError("offline"), count=1)
    await harness.manager._lifecycle.start()
    _, release = await harness.clock.next_wait()
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    factory = cast(Callable[[], MagicMock], harness.factory.side_effect)

    async def initialize(*args: object) -> None:
        started.set()
        await asyncio.Event().wait()

    async def shutdown() -> None:
        cleanup_started.set()
        await cleanup_release.wait()

    def create_adapter() -> MagicMock:
        adapter = factory()
        if len(harness.adapters) == 2:
            adapter.initialize.side_effect = initialize
            adapter.shutdown.side_effect = shutdown
        return adapter

    harness.factory.side_effect = create_adapter
    release.set()
    await asyncio.wait_for(started.wait(), timeout=1)
    retry = harness.manager._lifecycle._retry_tasks[harness.channel.name]
    change: asyncio.Task[None] | asyncio.Task[ChannelConfig]
    if operation == "stop":
        change = asyncio.create_task(harness.manager._lifecycle.stop())
    elif operation == "remove":
        change = asyncio.create_task(harness.manager.remove_channel(harness.channel.name))
    else:
        updated = replace(
            harness.channel,
            enabled=operation != "disable",
            name="renamed" if operation == "rename" else harness.channel.name,
        )
        harness.store.list_channels.return_value = [updated]
        change = asyncio.create_task(harness.manager.update_channel(updated))
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    assert not change.done()
    cleanup_release.set()
    await asyncio.wait_for(change, timeout=1)
    assert retry.cancelled()
    harness.adapters[1].shutdown.assert_awaited_once()
    assert harness.manager._lifecycle._retry_tasks == {}
    assert harness.manager._lifecycle._retry_state == {}
    if operation == "rename":
        assert harness.manager.get_channel_status("renamed")["active"] is True
        assert harness.channel.name not in harness.manager._adapters
    else:
        assert harness.manager._adapters == {}


async def test_operator_target_restored_before_retry_polling(
    harness: ChannelHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.fail_initializations(httpx.ConnectError("offline"), count=1)
    observed_targets: list[str | None] = []

    def start_polling(*args: object) -> None:
        observed_targets.append(harness.manager.attached_session(harness.channel.id, "dm:123"))
        harness.active.set()

    monkeypatch.setattr(harness.manager._polling_manager, "start_polling", start_polling)
    await harness.manager._lifecycle.start()
    _, release = await harness.clock.next_wait()
    release.set()
    await asyncio.wait_for(harness.active.wait(), timeout=1)
    assert observed_targets == ["session-1"]


async def test_retry_stops_after_permanent_failure(
    harness: ChannelHarness, caplog: pytest.LogCaptureFixture
) -> None:
    harness.fail_initializations(httpx.ConnectError("offline"), count=1)
    await harness.manager._lifecycle.start()
    _, release = await harness.clock.next_wait()
    factory = cast(Callable[[], MagicMock], harness.factory.side_effect)

    def create_adapter() -> MagicMock:
        adapter = factory()
        adapter.initialize.side_effect = ValueError("invalid config")
        return adapter

    harness.factory.side_effect = create_adapter
    retry = harness.manager._lifecycle._retry_tasks[harness.channel.name]
    release.set()
    await asyncio.wait_for(retry, timeout=1)
    status = harness.manager.get_channel_status(harness.channel.name)
    assert status["retry_pending"] is False
    assert status["init_error"] == "invalid config"
    assert harness.clock.waits.empty()
    assert len(harness.adapters) == 2
    failures = [record for record in caplog.records if "Failed to initialize" in record.message]
    assert len(failures) == 2
    assert "offline" in failures[0].message
    assert "invalid config" in failures[1].message


@pytest.mark.parametrize(
    "failure", [http_failure(503), httpx.ConnectError("https://a/botSECRET/getMe")]
)
async def test_init_error_and_log_scrub_token_urls(
    harness: ChannelHarness, failure: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    harness.fail_initializations(failure)
    with caplog.at_level(logging.ERROR, logger="gobby.communications.lifecycle"):
        await harness.manager._lifecycle.start()
    status = harness.manager.get_channel_status(harness.channel.name)
    assert status["init_error"]
    assert "SECRET" not in status["init_error"]
    assert "SECRET" not in caplog.text
    failures = [record for record in caplog.records if "Failed to initialize" in record.message]
    assert len(failures) == 1
    assert not failures[0].exc_info


async def test_retry_logs_first_failure_and_keeps_intermediate_attempts_silent(
    harness: ChannelHarness, caplog: pytest.LogCaptureFixture
) -> None:
    harness.fail_initializations(httpx.ConnectError("offline"), count=3)
    with caplog.at_level(logging.ERROR, logger="gobby.communications.lifecycle"):
        await harness.manager._lifecycle.start()
        for expected in [5, 10, 20]:
            delay, release = await harness.clock.next_wait()
            assert delay == expected
            assert len(caplog.records) == 1
            release.set()
        await harness.active.wait()
    assert len(caplog.records) == 1
    assert "offline" in caplog.records[0].message


@pytest.mark.parametrize("operation", ["startup", "update"])
async def test_superseded_activation_does_not_cancel_caller(
    harness: ChannelHarness, operation: str
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    factory = cast(Callable[[], MagicMock], harness.factory.side_effect)

    async def initialize(*args: object) -> None:
        started.set()
        await release.wait()

    def create_adapter() -> MagicMock:
        adapter = factory()
        adapter.initialize.side_effect = initialize
        return adapter

    harness.factory.side_effect = create_adapter

    async def activate() -> None:
        if operation == "startup":
            await harness.manager._lifecycle.start()
        else:
            await harness.manager.update_channel(harness.channel)

    caller = asyncio.create_task(activate())
    await asyncio.wait_for(started.wait(), timeout=1)
    await harness.manager.remove_channel(harness.channel.name)
    release.set()
    await asyncio.wait_for(caller, timeout=1)
    assert not caller.cancelled()
    assert harness.manager._adapters == {}
    assert harness.manager._lifecycle._retry_tasks == {}
    harness.adapters[0].shutdown.assert_awaited_once()


async def test_actionable_config_detail_is_bounded(harness: ChannelHarness) -> None:
    harness.fail_initializations(ValueError("SLACK_BOT_TOKEN secret is required: " + "x" * 1000))
    await harness.manager._lifecycle.start()
    detail = harness.manager.get_channel_status(harness.channel.name)["init_error"]
    assert detail.startswith("SLACK_BOT_TOKEN secret is required:")
    assert len(detail) == 512
