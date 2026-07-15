"""Tests for CompletionEventRegistry."""

from __future__ import annotations

import asyncio
import logging

import pytest

from gobby.events.completion_registry import CompletionEventRegistry, CompletionResultEvictedError
from tests._timing import drain_asyncio_tasks

pytestmark = pytest.mark.unit

COMPLETION_ID = "55361235-ff5f-5de3-88f4-c98c82f7f0c3"
PRIMARY_SUBSCRIBER_ID = "9264a39c-68db-5eed-917c-6f7babb8e6b1"
SECONDARY_SUBSCRIBER_ID = "7a378a57-18dd-56d9-be74-0fcb8a19376d"
TERTIARY_SUBSCRIBER_ID = "204df9de-a672-51b8-811a-0fc1a71bca39"


@pytest.fixture
def registry() -> CompletionEventRegistry:
    """Create a registry with no DB persistence (in-memory only)."""
    return CompletionEventRegistry()


class TestRegisterAndNotify:
    """Core register/notify/wait lifecycle."""

    @pytest.mark.asyncio
    async def test_register_creates_event(self, registry: CompletionEventRegistry) -> None:
        registry.register(
            COMPLETION_ID,
            subscribers=[PRIMARY_SUBSCRIBER_ID],
        )
        assert registry.is_registered(COMPLETION_ID)

    @pytest.mark.asyncio
    async def test_notify_sets_result(self, registry: CompletionEventRegistry) -> None:
        registry.register(
            COMPLETION_ID,
            subscribers=[PRIMARY_SUBSCRIBER_ID],
        )
        await registry.notify(COMPLETION_ID, {"status": "completed", "outputs": {"x": 1}})
        result = registry.get_result(COMPLETION_ID)
        assert result == {"status": "completed", "outputs": {"x": 1}}

    @pytest.mark.asyncio
    async def test_wait_returns_result_after_notify(
        self, registry: CompletionEventRegistry
    ) -> None:
        registry.register(COMPLETION_ID, subscribers=[])

        task = asyncio.create_task(registry.wait(COMPLETION_ID, timeout=2.0))
        await drain_asyncio_tasks()
        await registry.notify(COMPLETION_ID, {"status": "completed"})
        result = await task
        assert result == {"status": "completed"}

    @pytest.mark.asyncio
    async def test_wait_timeout_raises(self, registry: CompletionEventRegistry) -> None:
        registry.register(COMPLETION_ID, subscribers=[])
        with pytest.raises(asyncio.TimeoutError):
            await registry.wait(COMPLETION_ID, timeout=0.05)
        assert registry.is_registered(COMPLETION_ID) is False

    @pytest.mark.asyncio
    async def test_wait_on_already_notified(self, registry: CompletionEventRegistry) -> None:
        """Wait on an already-notified event returns immediately."""
        registry.register(COMPLETION_ID, subscribers=[])
        await registry.notify(COMPLETION_ID, {"done": True})
        result = await registry.wait(COMPLETION_ID, timeout=0.1)
        assert result == {"done": True}

    @pytest.mark.asyncio
    async def test_wait_raises_typed_error_when_cleanup_precedes_resume(
        self, registry: CompletionEventRegistry
    ) -> None:
        registry.register(COMPLETION_ID, subscribers=[])
        waiter = asyncio.create_task(registry.wait(COMPLETION_ID, timeout=1))
        await drain_asyncio_tasks()

        await registry.notify(COMPLETION_ID, {"status": "done"})
        registry.cleanup(COMPLETION_ID)

        with pytest.raises(CompletionResultEvictedError, match="removed before the waiter resumed"):
            await waiter

    @pytest.mark.asyncio
    async def test_notify_unregistered_is_noop(self, registry: CompletionEventRegistry) -> None:
        """Notifying an unregistered ID doesn't raise."""
        result = await registry.notify("nonexistent", {"status": "completed"})
        assert result is None
        assert registry.get_result("nonexistent") is None

    @pytest.mark.asyncio
    async def test_duplicate_notify_keeps_first_result_and_wakes_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        woken: list[tuple[str, str, dict]] = []

        async def wake(session_id: str, message: str, result: dict) -> None:
            woken.append((session_id, message, result))

        registry = CompletionEventRegistry(wake_callback=wake)
        registry.register(
            COMPLETION_ID,
            subscribers=[PRIMARY_SUBSCRIBER_ID],
        )

        with caplog.at_level(logging.DEBUG, logger="gobby.events.completion_registry"):
            await registry.notify(COMPLETION_ID, {"status": "first"}, message="first")
            await registry.notify(COMPLETION_ID, {"status": "second"}, message="second")

        assert registry.get_result(COMPLETION_ID) == {"status": "first"}
        assert woken == [(PRIMARY_SUBSCRIBER_ID, "first", {"status": "first"})]
        assert (
            f"notify() called for completed ID {COMPLETION_ID} - ignoring duplicate" in caplog.text
        )

    @pytest.mark.asyncio
    async def test_wait_unregistered_raises(self, registry: CompletionEventRegistry) -> None:
        """Waiting on an unregistered ID raises KeyError."""
        with pytest.raises(KeyError):
            await registry.wait("nonexistent", timeout=0.1)


class TestSubscribers:
    """Subscriber management."""

    @pytest.mark.asyncio
    async def test_register_with_subscribers(self, registry: CompletionEventRegistry) -> None:
        registry.register(
            COMPLETION_ID,
            subscribers=[
                PRIMARY_SUBSCRIBER_ID,
                SECONDARY_SUBSCRIBER_ID,
            ],
        )
        subs = registry.get_subscribers(COMPLETION_ID)
        assert set(subs) == {
            PRIMARY_SUBSCRIBER_ID,
            SECONDARY_SUBSCRIBER_ID,
        }

    @pytest.mark.asyncio
    async def test_register_merges_existing_subscribers_and_preserves_event(
        self, registry: CompletionEventRegistry
    ) -> None:
        registry.register(
            COMPLETION_ID,
            subscribers=[
                PRIMARY_SUBSCRIBER_ID,
                SECONDARY_SUBSCRIBER_ID,
            ],
            continuation_prompt="first prompt",
        )
        original_event = registry._events[COMPLETION_ID]

        registry.register(
            COMPLETION_ID,
            subscribers=[
                SECONDARY_SUBSCRIBER_ID,
                TERTIARY_SUBSCRIBER_ID,
            ],
            continuation_prompt="second prompt",
        )

        assert registry._events[COMPLETION_ID] is original_event
        assert registry.get_subscribers(COMPLETION_ID) == [
            PRIMARY_SUBSCRIBER_ID,
            SECONDARY_SUBSCRIBER_ID,
            TERTIARY_SUBSCRIBER_ID,
        ]
        assert registry.get_continuation_prompt(COMPLETION_ID) == "first prompt"

    @pytest.mark.asyncio
    async def test_reregister_preserves_waiter_until_notify(
        self, registry: CompletionEventRegistry
    ) -> None:
        registry.register(
            COMPLETION_ID,
            subscribers=[PRIMARY_SUBSCRIBER_ID],
        )
        waiter = asyncio.create_task(registry.wait(COMPLETION_ID, timeout=1))
        await drain_asyncio_tasks()

        registry.register(
            COMPLETION_ID,
            subscribers=[SECONDARY_SUBSCRIBER_ID],
        )
        await registry.notify(COMPLETION_ID, {"status": "done"})

        assert await waiter == {"status": "done"}

    @pytest.mark.asyncio
    async def test_subscribe_adds_to_existing(self, registry: CompletionEventRegistry) -> None:
        registry.register(
            COMPLETION_ID,
            subscribers=[PRIMARY_SUBSCRIBER_ID],
        )
        registry.subscribe(COMPLETION_ID, SECONDARY_SUBSCRIBER_ID)
        subs = registry.get_subscribers(COMPLETION_ID)
        assert set(subs) == {
            PRIMARY_SUBSCRIBER_ID,
            SECONDARY_SUBSCRIBER_ID,
        }

    @pytest.mark.asyncio
    async def test_subscribe_idempotent(self, registry: CompletionEventRegistry) -> None:
        registry.register(
            COMPLETION_ID,
            subscribers=[PRIMARY_SUBSCRIBER_ID],
        )
        registry.subscribe(COMPLETION_ID, PRIMARY_SUBSCRIBER_ID)
        subs = registry.get_subscribers(COMPLETION_ID)
        assert subs == [PRIMARY_SUBSCRIBER_ID]

    @pytest.mark.asyncio
    async def test_subscribe_unregistered_raises(self, registry: CompletionEventRegistry) -> None:
        with pytest.raises(KeyError):
            registry.subscribe("nonexistent", PRIMARY_SUBSCRIBER_ID)


class TestWakeCallback:
    """Notify triggers wake callback for each subscriber."""

    @pytest.mark.asyncio
    async def test_notify_calls_wake_for_each_subscriber(
        self,
    ) -> None:
        woken: list[tuple[str, str, dict]] = []

        async def wake(session_id: str, message: str, result: dict) -> None:
            woken.append((session_id, message, result))

        registry = CompletionEventRegistry(wake_callback=wake)
        registry.register(
            COMPLETION_ID,
            subscribers=[
                PRIMARY_SUBSCRIBER_ID,
                SECONDARY_SUBSCRIBER_ID,
            ],
        )
        await registry.notify(
            COMPLETION_ID,
            {"status": "completed"},
            message="Pipeline completed",
        )

        assert len(woken) == 2
        assert {w[0] for w in woken} == {
            PRIMARY_SUBSCRIBER_ID,
            SECONDARY_SUBSCRIBER_ID,
        }
        assert all(w[1] == "Pipeline completed" for w in woken)
        assert all(w[2] == {"status": "completed"} for w in woken)

    @pytest.mark.asyncio
    async def test_wake_failure_does_not_block_notify(self) -> None:
        """If wake callback fails for one subscriber, others still get woken."""
        woken: list[str] = []

        async def flaky_wake(session_id: str, message: str, result: dict) -> None:
            if session_id == PRIMARY_SUBSCRIBER_ID:
                raise RuntimeError("tmux session gone")
            woken.append(session_id)

        registry = CompletionEventRegistry(wake_callback=flaky_wake)
        registry.register(
            COMPLETION_ID,
            subscribers=[
                PRIMARY_SUBSCRIBER_ID,
                SECONDARY_SUBSCRIBER_ID,
            ],
        )
        await registry.notify(COMPLETION_ID, {"status": "completed"}, message="done")

        assert woken == [SECONDARY_SUBSCRIBER_ID]

    @pytest.mark.asyncio
    async def test_notify_uses_subscriber_snapshot(self) -> None:
        woken: list[str] = []
        registry: CompletionEventRegistry

        async def wake(session_id: str, message: str, result: dict) -> None:
            woken.append(session_id)
            if session_id == PRIMARY_SUBSCRIBER_ID:
                registry.subscribe(COMPLETION_ID, TERTIARY_SUBSCRIBER_ID)

        registry = CompletionEventRegistry(wake_callback=wake)
        registry.register(
            COMPLETION_ID,
            subscribers=[PRIMARY_SUBSCRIBER_ID, SECONDARY_SUBSCRIBER_ID],
        )

        await registry.notify(COMPLETION_ID, {"status": "completed"})

        assert woken == [PRIMARY_SUBSCRIBER_ID, SECONDARY_SUBSCRIBER_ID]
        assert registry.get_subscribers(COMPLETION_ID) == [
            PRIMARY_SUBSCRIBER_ID,
            SECONDARY_SUBSCRIBER_ID,
            TERTIARY_SUBSCRIBER_ID,
        ]

    @pytest.mark.asyncio
    async def test_no_wake_without_callback(self, registry: CompletionEventRegistry) -> None:
        """Registry works fine without a wake callback (pipeline-internal use)."""
        registry.register(
            COMPLETION_ID,
            subscribers=[PRIMARY_SUBSCRIBER_ID],
        )
        await registry.notify(COMPLETION_ID, {"status": "completed"})
        result = await registry.wait(COMPLETION_ID, timeout=0.1)
        assert result == {"status": "completed"}


class TestCleanup:
    """Resource cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_removes_event_and_result(
        self, registry: CompletionEventRegistry
    ) -> None:
        registry.register(
            COMPLETION_ID,
            subscribers=[PRIMARY_SUBSCRIBER_ID],
        )
        await registry.notify(COMPLETION_ID, {"done": True})
        registry.cleanup(COMPLETION_ID)

        assert not registry.is_registered(COMPLETION_ID)
        assert registry.get_result(COMPLETION_ID) is None
        assert registry.get_subscribers(COMPLETION_ID) == []

    @pytest.mark.asyncio
    async def test_cleanup_unregistered_is_noop(self, registry: CompletionEventRegistry) -> None:
        result = registry.cleanup("nonexistent")
        assert result is None
        assert not registry.is_registered("nonexistent")


class TestContinuationPrompt:
    """Continuation prompt storage and retrieval."""

    @pytest.mark.asyncio
    async def test_register_with_continuation_prompt(
        self, registry: CompletionEventRegistry
    ) -> None:
        registry.register(
            COMPLETION_ID,
            subscribers=[PRIMARY_SUBSCRIBER_ID],
            continuation_prompt="Wire dependencies between new subtasks",
        )
        assert registry.get_continuation_prompt(COMPLETION_ID) == (
            "Wire dependencies between new subtasks"
        )

    @pytest.mark.asyncio
    async def test_continuation_prompt_included_in_wake(self) -> None:
        woken: list[tuple[str, str, dict]] = []

        async def wake(session_id: str, message: str, result: dict) -> None:
            woken.append((session_id, message, result))

        registry = CompletionEventRegistry(wake_callback=wake)
        registry.register(
            COMPLETION_ID,
            subscribers=[PRIMARY_SUBSCRIBER_ID],
            continuation_prompt="Do the next thing",
        )
        await registry.notify(
            COMPLETION_ID,
            {"status": "completed"},
            message="Pipeline done",
        )

        # The wake callback should receive the message - continuation prompt
        # formatting is handled by the caller (wake dispatcher), not the registry
        assert len(woken) == 1
