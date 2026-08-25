"""Polling background loop manager for communications channels."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.communications.adapters.base import BaseChannelAdapter
    from gobby.communications.manager import CommunicationsManager

logger = logging.getLogger(__name__)


class PollingManager:
    """Manages background polling loops for non-webhook channels."""

    def __init__(self, manager: CommunicationsManager, default_interval: int = 30) -> None:
        """Initialize polling manager.

        Args:
            manager: Parent communications manager (used to route inbound messages).
            default_interval: Default poll interval in seconds.
        """
        self._manager = manager
        self._default_interval = default_interval
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._intervals: dict[str, int] = {}

    def start_polling(
        self, channel_name: str, adapter: BaseChannelAdapter, interval: int | None = None
    ) -> None:
        """Start background polling loop for a channel.

        Args:
            channel_name: Name of the channel to poll.
            adapter: The channel adapter instance.
            interval: Optional custom interval in seconds.
        """
        if self.is_polling(channel_name):
            logger.warning("Already polling channel %r", channel_name)
            return

        poll_interval = interval if interval is not None else self._default_interval
        self._intervals[channel_name] = poll_interval

        task = asyncio.create_task(
            self._poll_loop(channel_name, adapter, poll_interval),
            name=f"poll_{channel_name}",
        )
        self._tasks[channel_name] = task
        logger.info("Started polling for %r (interval=%ss)", channel_name, poll_interval)

    def stop_polling(self, channel_name: str) -> None:
        """Stop background polling loop for a channel.

        Args:
            channel_name: Name of the channel to stop polling.
        """
        task = self._tasks.pop(channel_name, None)
        self._intervals.pop(channel_name, None)
        if task is not None and not task.done():
            task.cancel()
            logger.info("Stopped polling for %r", channel_name)

    def stop_all(self) -> None:
        """Stop all background polling loops."""
        for channel_name in list(self._tasks.keys()):
            self.stop_polling(channel_name)

    def is_polling(self, channel_name: str) -> bool:
        """Check if actively polling a channel.

        Args:
            channel_name: Name of the channel to check.

        Returns:
            True if actively polling.
        """
        task = self._tasks.get(channel_name)
        return task is not None and not task.done()

    async def _poll_loop(
        self, channel_name: str, adapter: BaseChannelAdapter, interval: int
    ) -> None:
        """The actual async polling loop coroutine.

        Args:
            channel_name: Name of the channel.
            adapter: Channel adapter.
            interval: Poll interval in seconds.
        """
        consecutive_failures = 0
        max_backoff = 300  # 5 minutes max backoff
        base_backoff = 5  # start with 5 seconds backoff

        while True:
            try:
                messages = await adapter.poll()

                if messages:
                    stored = await self._manager.handle_inbound_messages(channel_name, messages)
                    if stored:
                        await adapter.acknowledge_messages(stored)

                consecutive_failures = 0
                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_failures += 1
                # A bare ``TimeoutError()`` renders as ''; name the class so the line
                # still says what failed (#20981).
                error = str(e) or type(e).__name__
                sleep_duration = min(base_backoff * (2 ** (consecutive_failures - 1)), max_backoff)
                if consecutive_failures == 1:
                    # One traceback per failure streak; repeats stay one line so a
                    # transient outage cannot flood the log (#20867).
                    logger.exception(
                        "Error polling channel %r: %s (backing off %ss)",
                        channel_name,
                        error,
                        sleep_duration,
                    )
                else:
                    logger.warning(
                        "Error polling channel %r: %s (failure %s in a row, backing off %ss)",
                        channel_name,
                        error,
                        consecutive_failures,
                        sleep_duration,
                    )
                await asyncio.sleep(sleep_duration)
