"""
PTY Reader for streaming terminal output from embedded agents.

Reads from PTY master file descriptors and broadcasts output via callbacks.
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import os
import select
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.agents.registry import RunningAgent

logger = logging.getLogger(__name__)

# Type for output callback: async function(run_id, data)
OutputCallback = Callable[[str, str], Awaitable[None]]


class PTYReaderManager:
    """
    Manages PTY reading tasks for embedded agents.

    Starts a reader task for each agent with a master_fd and
    broadcasts output via the provided callback.
    """

    def __init__(self, output_callback: OutputCallback | None = None):
        """
        Initialize the PTY reader manager.

        Args:
            output_callback: Async callback for terminal output (run_id, data)
        """
        self._output_callback = output_callback
        self._reader_tasks: dict[str, asyncio.Task[None]] = {}
        self._stop_events: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    def set_output_callback(self, callback: OutputCallback) -> None:
        """Set the output callback for terminal data."""
        self._output_callback = callback

    async def start_reader(self, agent: RunningAgent) -> bool:
        """
        Start a PTY reader for an agent.

        Args:
            agent: RunningAgent with master_fd set

        Returns:
            True if reader was started, False if already running or no fd
        """
        if agent.master_fd is None:
            return False

        async with self._lock:
            if agent.run_id in self._reader_tasks:
                return False  # Already reading

            stop_event = asyncio.Event()
            self._stop_events[agent.run_id] = stop_event

            task = asyncio.create_task(
                self._read_loop(agent.run_id, agent.master_fd, stop_event),
                name=f"pty_reader_{agent.run_id}",
            )
            self._reader_tasks[agent.run_id] = task

            logger.debug(f"Started PTY reader for agent {agent.run_id}")
            return True

    async def stop_reader(self, run_id: str) -> bool:
        """
        Stop a PTY reader for an agent.

        Args:
            run_id: Agent run ID

        Returns:
            True if reader was stopped, False if not running
        """
        async with self._lock:
            stop_event = self._stop_events.pop(run_id, None)
            task = self._reader_tasks.pop(run_id, None)

        if stop_event:
            stop_event.set()

        if task:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except (asyncio.CancelledError, TimeoutError):
                pass
            logger.debug(f"Stopped PTY reader for agent {run_id}")
            return True

        return False

    async def stop_all(self) -> None:
        """Stop all PTY readers."""
        async with self._lock:
            run_ids = list(self._reader_tasks.keys())

        for run_id in run_ids:
            await self.stop_reader(run_id)

    async def _read_loop(
        self,
        run_id: str,
        master_fd: int,
        stop_event: asyncio.Event,
    ) -> None:
        """
        Read loop for a single PTY.

        Reads from the master_fd and broadcasts via callback.
        Uses select to avoid blocking the event loop.

        Args:
            run_id: Agent run ID
            master_fd: PTY master file descriptor
            stop_event: Event to signal stop
        """
        loop = asyncio.get_running_loop()
        # Incremental decoder buffers incomplete multi-byte UTF-8 sequences
        # across read boundaries, preventing corruption when a character
        # straddles two 4 KB chunks.
        decoder = codecs.getincrementaldecoder("utf-8")("replace")

        try:
            while not stop_event.is_set():
                # Use select with timeout to check for data
                try:
                    ready, _, _ = await loop.run_in_executor(
                        None,
                        lambda: select.select([master_fd], [], [], 0.1),
                    )
                except (ValueError, OSError):
                    # FD closed or invalid
                    break

                if not ready:
                    continue

                # Read available data
                try:
                    data = await loop.run_in_executor(
                        None,
                        lambda: os.read(master_fd, 4096),
                    )
                except OSError as e:
                    # FD closed or error
                    logger.debug(f"PTY read error for {run_id}: {e}")
                    break

                if not data:
                    # EOF — flush any remaining buffered bytes
                    text = decoder.decode(b"", final=True)
                    if text and self._output_callback:
                        try:
                            await self._output_callback(run_id, text)
                        except Exception as e:
                            logger.warning(f"Output callback error for {run_id}: {e}")
                    break

                # Decode incrementally (buffers incomplete sequences)
                text = decoder.decode(data)

                if text and self._output_callback:
                    try:
                        await self._output_callback(run_id, text)
                    except Exception as e:
                        logger.warning(f"Output callback error for {run_id}: {e}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"PTY reader error for {run_id}: {e}")
        finally:
            logger.debug(f"PTY reader finished for {run_id}")


# Global singleton
_pty_reader_manager: PTYReaderManager | None = None


def get_pty_reader_manager() -> PTYReaderManager:
    """Get the global PTY reader manager singleton."""
    global _pty_reader_manager
    if _pty_reader_manager is None:
        _pty_reader_manager = PTYReaderManager()
    return _pty_reader_manager
