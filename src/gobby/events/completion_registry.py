"""Completion event registry for push-based async operation notifications.

In-memory event bus that:
1. Lets pipeline executor block on completion events (wait step type)
2. Triggers wake callbacks to notify subscribing sessions when operations complete
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine, Mapping
from typing import Any

logger = logging.getLogger(__name__)

# Type for the wake callback: (session_id, message, result) -> structured outcome
WakeCallback = Callable[[str, str, dict[str, Any]], Coroutine[Any, Any, object]]


def wake_result_is_delivered(result: object) -> bool:
    """Return whether a wake outcome durably resolves a subscriber row."""
    if not isinstance(result, Mapping):
        return False
    return result.get("ism_persisted") is True or result.get("error_code") == "session_not_found"


class CompletionResultEvictedError(RuntimeError):
    """Raised when cleanup removes a notified result before a waiter resumes."""


class CompletionEventRegistry:
    """In-memory registry for completion events with subscriber notifications.

    Instances are confined to one asyncio event-loop thread. Methods do not
    provide cross-thread synchronization; register, notify, wait, subscribe,
    and cleanup must all run on that owning loop thread.

    Used by:
    - PipelineExecutor: `wait` step type blocks via registry.wait()
    - Daemon: registry.notify() fires wake callbacks for subscribed sessions
    """

    def __init__(
        self,
        wake_callback: WakeCallback | None = None,
    ) -> None:
        self._events: dict[str, asyncio.Event] = {}
        self._results: dict[str, dict[str, Any]] = {}
        self._subscribers: dict[str, list[str]] = {}
        self._continuation_prompts: dict[str, str] = {}
        self._wake_callback = wake_callback

    def register(
        self,
        completion_id: str,
        subscribers: list[str],
        continuation_prompt: str | None = None,
    ) -> bool:
        """Register a completion event with subscriber session IDs.

        Args:
            completion_id: Unique ID (execution_id or run_id)
            subscribers: Session IDs to notify on completion
            continuation_prompt: Optional prompt describing what to do with results

        Returns:
            ``True`` for a new entry and ``False`` when merging an existing entry.
        """
        if completion_id in self._events:
            registered_subscribers = self._subscribers.setdefault(completion_id, [])
            seen = set(registered_subscribers)
            added_subscriber_count = 0
            for session_id in subscribers:
                if session_id not in seen:
                    registered_subscribers.append(session_id)
                    seen.add(session_id)
                    added_subscriber_count += 1
            if added_subscriber_count:
                logger.debug(
                    "Merged %d new subscriber(s) into completion registration %s (%d total)",
                    added_subscriber_count,
                    completion_id,
                    len(registered_subscribers),
                )
            if continuation_prompt and completion_id not in self._continuation_prompts:
                self._continuation_prompts[completion_id] = continuation_prompt
            elif (
                continuation_prompt
                and continuation_prompt != self._continuation_prompts[completion_id]
            ):
                logger.warning(
                    "Ignoring conflicting continuation prompt for completion registration %s",
                    completion_id,
                )
            return False

        self._events[completion_id] = asyncio.Event()
        self._subscribers[completion_id] = list(subscribers)
        if continuation_prompt:
            self._continuation_prompts[completion_id] = continuation_prompt
        return True

    def is_registered(self, completion_id: str) -> bool:
        """Check if a completion event is registered."""
        return completion_id in self._events

    def is_awaiting(self, session_id: str) -> bool:
        """Return whether ``session_id`` subscribes to a completion not yet notified.

        Such a session is parked by design (``wait_for_agent`` ends the turn and
        the daemon wakes it with the result), so watchdogs must not read its quiet
        pane as idle or stagnant.
        """
        return any(
            session_id in subscribers and completion_id not in self._results
            for completion_id, subscribers in self._subscribers.items()
        )

    async def notify(
        self,
        completion_id: str,
        result: dict[str, Any],
        message: str = "",
    ) -> dict[str, bool] | None:
        """Signal completion and wake all subscribers.

        Args:
            completion_id: The completion event ID
            result: Result data to store and pass to wake callbacks
            message: Human-readable message for wake notifications
        """
        event = self._events.get(completion_id)
        if event is None:
            logger.debug("notify() called for unregistered ID %s - ignoring", completion_id)
            return None
        if completion_id in self._results:
            logger.debug("notify() called for completed ID %s - ignoring duplicate", completion_id)
            return None

        # Include continuation_prompt in result so wake dispatcher can use it
        # Enrich a copy to avoid mutating the caller's dict
        cp = self._continuation_prompts.get(completion_id)
        if cp and "continuation_prompt" not in result:
            result = {**result, "continuation_prompt": cp}

        self._results[completion_id] = result
        event.set()

        delivery: dict[str, bool] = {}
        for session_id in list(self._subscribers.get(completion_id, [])):
            delivery[session_id] = False
            if self._wake_callback is None:
                continue
            try:
                wake_result = await self._wake_callback(session_id, message, result)
                delivery[session_id] = wake_result_is_delivered(wake_result)
            except Exception:
                logger.warning(
                    "Wake callback failed for session %s (completion %s)",
                    session_id,
                    completion_id,
                    exc_info=True,
                )
        return delivery

    async def wait(self, completion_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Block until a completion event fires.

        Args:
            completion_id: The completion event ID to wait on
            timeout: Max seconds to wait (None = wait forever)

        Returns:
            The result dict stored by notify()

        Raises:
            KeyError: If completion_id is not registered
            CompletionResultEvictedError: If cleanup removes the result before this waiter resumes
            asyncio.TimeoutError: If timeout expires before notification
        """
        event = self._events.get(completion_id)
        if event is None:
            raise KeyError(f"Completion event {completion_id!r} not registered")

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            self.cleanup(completion_id)
            raise
        try:
            return self._results[completion_id]
        except KeyError:
            raise CompletionResultEvictedError(
                f"Completion result {completion_id!r} was removed before the waiter resumed"
            ) from None

    def get_result(self, completion_id: str) -> dict[str, Any] | None:
        """Get the stored result for a completion event, or None."""
        return self._results.get(completion_id)

    def subscribe(self, completion_id: str, session_id: str) -> None:
        """Add a subscriber to an existing completion event.

        Args:
            completion_id: The completion event ID
            session_id: Session ID to add as subscriber

        Raises:
            KeyError: If completion_id is not registered
        """
        subs = self._subscribers.get(completion_id)
        if subs is None:
            raise KeyError(f"Completion event {completion_id!r} not registered")
        if session_id not in subs:
            subs.append(session_id)

    def get_subscribers(self, completion_id: str) -> list[str]:
        """Get subscriber session IDs for a completion event."""
        return list(self._subscribers.get(completion_id, []))

    def get_continuation_prompt(self, completion_id: str) -> str | None:
        """Get the continuation prompt for a completion event."""
        return self._continuation_prompts.get(completion_id)

    def cleanup(self, completion_id: str) -> None:
        """Remove all state for a completion event."""
        self._events.pop(completion_id, None)
        self._results.pop(completion_id, None)
        self._subscribers.pop(completion_id, None)
        self._continuation_prompts.pop(completion_id, None)
