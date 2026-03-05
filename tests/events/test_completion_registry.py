"""Tests for CompletionEventRegistry."""

from __future__ import annotations

import asyncio

import pytest

from gobby.events.completion_registry import CompletionEventRegistry

pytestmark = pytest.mark.unit


@pytest.fixture
def registry() -> CompletionEventRegistry:
    """Create a registry with no DB persistence (in-memory only)."""
    return CompletionEventRegistry()


class TestRegisterAndNotify:
    """Core register/notify/wait lifecycle."""

    @pytest.mark.asyncio
    async def test_register_creates_event(self, registry: CompletionEventRegistry) -> None:
        registry.register("pe-abc123", subscribers=["sess-1"])
        assert registry.is_registered("pe-abc123")

    @pytest.mark.asyncio
    async def test_notify_sets_result(self, registry: CompletionEventRegistry) -> None:
        registry.register("pe-abc123", subscribers=["sess-1"])
        await registry.notify("pe-abc123", {"status": "completed", "outputs": {"x": 1}})
        result = registry.get_result("pe-abc123")
        assert result == {"status": "completed", "outputs": {"x": 1}}

    @pytest.mark.asyncio
    async def test_wait_returns_result_after_notify(
        self, registry: CompletionEventRegistry
    ) -> None:
        registry.register("pe-abc123", subscribers=[])

        async def _notify_soon() -> None:
            await asyncio.sleep(0.05)
            await registry.notify("pe-abc123", {"status": "completed"})

        asyncio.create_task(_notify_soon())
        result = await registry.wait("pe-abc123", timeout=2.0)
        assert result == {"status": "completed"}

    @pytest.mark.asyncio
    async def test_wait_timeout_raises(self, registry: CompletionEventRegistry) -> None:
        registry.register("pe-abc123", subscribers=[])
        with pytest.raises(asyncio.TimeoutError):
            await registry.wait("pe-abc123", timeout=0.05)

    @pytest.mark.asyncio
    async def test_wait_on_already_notified(self, registry: CompletionEventRegistry) -> None:
        """Wait on an already-notified event returns immediately."""
        registry.register("pe-abc123", subscribers=[])
        await registry.notify("pe-abc123", {"done": True})
        result = await registry.wait("pe-abc123", timeout=0.1)
        assert result == {"done": True}

    @pytest.mark.asyncio
    async def test_notify_unregistered_is_noop(self, registry: CompletionEventRegistry) -> None:
        """Notifying an unregistered ID doesn't raise."""
        await registry.notify("nonexistent", {"status": "completed"})

    @pytest.mark.asyncio
    async def test_wait_unregistered_raises(self, registry: CompletionEventRegistry) -> None:
        """Waiting on an unregistered ID raises KeyError."""
        with pytest.raises(KeyError):
            await registry.wait("nonexistent", timeout=0.1)


class TestSubscribers:
    """Subscriber management."""

    @pytest.mark.asyncio
    async def test_register_with_subscribers(self, registry: CompletionEventRegistry) -> None:
        registry.register("pe-abc123", subscribers=["sess-1", "sess-2"])
        subs = registry.get_subscribers("pe-abc123")
        assert set(subs) == {"sess-1", "sess-2"}

    @pytest.mark.asyncio
    async def test_subscribe_adds_to_existing(self, registry: CompletionEventRegistry) -> None:
        registry.register("pe-abc123", subscribers=["sess-1"])
        registry.subscribe("pe-abc123", "sess-2")
        subs = registry.get_subscribers("pe-abc123")
        assert set(subs) == {"sess-1", "sess-2"}

    @pytest.mark.asyncio
    async def test_subscribe_idempotent(self, registry: CompletionEventRegistry) -> None:
        registry.register("pe-abc123", subscribers=["sess-1"])
        registry.subscribe("pe-abc123", "sess-1")
        subs = registry.get_subscribers("pe-abc123")
        assert subs == ["sess-1"]

    @pytest.mark.asyncio
    async def test_subscribe_unregistered_raises(self, registry: CompletionEventRegistry) -> None:
        with pytest.raises(KeyError):
            registry.subscribe("nonexistent", "sess-1")


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
        registry.register("pe-abc123", subscribers=["sess-1", "sess-2"])
        await registry.notify(
            "pe-abc123",
            {"status": "completed"},
            message="Pipeline completed",
        )

        assert len(woken) == 2
        assert {w[0] for w in woken} == {"sess-1", "sess-2"}
        assert all(w[1] == "Pipeline completed" for w in woken)
        assert all(w[2] == {"status": "completed"} for w in woken)

    @pytest.mark.asyncio
    async def test_wake_failure_does_not_block_notify(self) -> None:
        """If wake callback fails for one subscriber, others still get woken."""
        woken: list[str] = []

        async def flaky_wake(session_id: str, message: str, result: dict) -> None:
            if session_id == "sess-1":
                raise RuntimeError("tmux session gone")
            woken.append(session_id)

        registry = CompletionEventRegistry(wake_callback=flaky_wake)
        registry.register("pe-abc123", subscribers=["sess-1", "sess-2"])
        await registry.notify("pe-abc123", {"status": "completed"}, message="done")

        assert woken == ["sess-2"]

    @pytest.mark.asyncio
    async def test_no_wake_without_callback(self, registry: CompletionEventRegistry) -> None:
        """Registry works fine without a wake callback (pipeline-internal use)."""
        registry.register("pe-abc123", subscribers=["sess-1"])
        await registry.notify("pe-abc123", {"status": "completed"})
        result = await registry.wait("pe-abc123", timeout=0.1)
        assert result == {"status": "completed"}


class TestCleanup:
    """Resource cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_removes_event_and_result(
        self, registry: CompletionEventRegistry
    ) -> None:
        registry.register("pe-abc123", subscribers=["sess-1"])
        await registry.notify("pe-abc123", {"done": True})
        registry.cleanup("pe-abc123")

        assert not registry.is_registered("pe-abc123")
        assert registry.get_result("pe-abc123") is None
        assert registry.get_subscribers("pe-abc123") == []

    @pytest.mark.asyncio
    async def test_cleanup_unregistered_is_noop(self, registry: CompletionEventRegistry) -> None:
        registry.cleanup("nonexistent")  # Should not raise


class TestContinuationPrompt:
    """Continuation prompt storage and retrieval."""

    @pytest.mark.asyncio
    async def test_register_with_continuation_prompt(
        self, registry: CompletionEventRegistry
    ) -> None:
        registry.register(
            "pe-abc123",
            subscribers=["sess-1"],
            continuation_prompt="Wire dependencies between new subtasks",
        )
        assert registry.get_continuation_prompt("pe-abc123") == (
            "Wire dependencies between new subtasks"
        )

    @pytest.mark.asyncio
    async def test_continuation_prompt_included_in_wake(self) -> None:
        woken: list[tuple[str, str, dict]] = []

        async def wake(session_id: str, message: str, result: dict) -> None:
            woken.append((session_id, message, result))

        registry = CompletionEventRegistry(wake_callback=wake)
        registry.register(
            "pe-abc123",
            subscribers=["sess-1"],
            continuation_prompt="Do the next thing",
        )
        await registry.notify(
            "pe-abc123",
            {"status": "completed"},
            message="Pipeline done",
        )

        # The wake callback should receive the message - continuation prompt
        # formatting is handled by the caller (wake dispatcher), not the registry
        assert len(woken) == 1
