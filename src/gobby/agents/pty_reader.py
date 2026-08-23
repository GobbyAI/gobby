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
    from typing import Protocol

    class HasMasterFd(Protocol):
        run_id: str
        master_fd: int | None


logger = logging.getLogger(__name__)

# Type for output callback: async function(run_id, data)
OutputCallback = Callable[[str, str], Awaitable[None]]

# Per-stream rewrite applied to each decoded chunk before it is broadcast.
# Stateful across chunks by contract, so a caller passes one instance per
# reader rather than a shared function.
StreamTransform = Callable[[str], str]


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

    def set_output_callback(self, callback: OutputCallback | None) -> None:
        """Set the output callback for terminal data."""
        self._output_callback = callback

    async def start_reader(
        self,
        agent: HasMasterFd,
        *,
        transform: StreamTransform | None = None,
    ) -> bool:
        """
        Start a PTY reader for an agent.

        Args:
            agent: Object with run_id and master_fd attributes
            transform: Optional per-stream rewrite applied to each decoded
                chunk before broadcast. Opt-in, so streams that do not ask for
                one are byte-for-byte unchanged.

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
                self._read_loop(agent.run_id, agent.master_fd, stop_event, transform),
                name=f"pty_reader_{agent.run_id}",
            )
            self._reader_tasks[agent.run_id] = task

            logger.debug("Started PTY reader for agent %s", agent.run_id)
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
            logger.debug("Stopped PTY reader for agent %s", run_id)
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
        transform: StreamTransform | None = None,
    ) -> None:
        """
        Read loop for a single PTY.

        Reads from the master_fd and broadcasts via callback.
        Uses select to avoid blocking the event loop.

        Args:
            run_id: Agent run ID
            master_fd: PTY master file descriptor
            stop_event: Event to signal stop
            transform: Optional per-stream rewrite for each decoded chunk
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
                    logger.debug("PTY read error for %s: %s", run_id, e)
                    break

                if not data:
                    # EOF — flush any remaining buffered bytes
                    text = decoder.decode(b"", final=True)
                    if text and transform is not None:
                        text = transform(text)
                    if text and self._output_callback:
                        try:
                            await self._output_callback(run_id, text)
                        except Exception as e:
                            logger.warning("Output callback error for %s: %s", run_id, e)
                    break

                # Decode incrementally (buffers incomplete sequences)
                text = decoder.decode(data)
                if text and transform is not None:
                    text = transform(text)

                if text and self._output_callback:
                    try:
                        await self._output_callback(run_id, text)
                    except Exception as e:
                        logger.warning("Output callback error for %s: %s", run_id, e)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("PTY reader error for %s: %s", run_id, e)
        finally:
            logger.debug("PTY reader finished for %s", run_id)


# Global singleton
_pty_reader_manager: PTYReaderManager | None = None


def get_pty_reader_manager() -> PTYReaderManager:
    """Get the global PTY reader manager singleton."""
    global _pty_reader_manager
    if _pty_reader_manager is None:
        _pty_reader_manager = PTYReaderManager()
    return _pty_reader_manager


def reset_pty_output_callback() -> None:
    """Clear the callback only when the singleton was already constructed."""
    if _pty_reader_manager is not None:
        _pty_reader_manager.set_output_callback(None)
