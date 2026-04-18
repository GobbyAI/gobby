"""Debounced trigger for incremental code index updates.

Accumulates file edit notifications and coalesces them into
batched index_changed_files() calls after a configurable delay.
Thread-safe: accepts notifications from sync threads, schedules
work on the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import logging

from gobby.utils.native_bin import resolve_native_bin

logger = logging.getLogger(__name__)


class CodeIndexTrigger:
    """Debounced trigger for post-edit incremental code indexing.

    Accepts file change notifications from any thread and coalesces
    them into batched gcode index calls after a debounce window.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        debounce_seconds: float = 2.0,
    ) -> None:
        self._loop = loop
        self._debounce_seconds = debounce_seconds
        # Pending files grouped by project_id
        self._pending: dict[str, set[str]] = {}
        # Root path per project (same for all files in a project)
        self._root_paths: dict[str, str] = {}
        # Per-project flush timer handle
        self._flush_timers: dict[str, asyncio.TimerHandle] = {}

    def notify_file_changed(
        self,
        file_path: str,
        project_id: str,
        root_path: str,
    ) -> None:
        """Thread-safe notification that a file was edited.

        Can be called from any thread. Schedules debounced indexing
        on the event loop.
        """
        self._loop.call_soon_threadsafe(self._schedule_file, file_path, project_id, root_path)

    def _schedule_file(self, file_path: str, project_id: str, root_path: str) -> None:
        """Schedule or reschedule indexing for a file (runs on event loop)."""
        # Cancel existing flush timer for this project
        if project_id in self._flush_timers:
            self._flush_timers[project_id].cancel()

        # Add file to pending set
        if project_id not in self._pending:
            self._pending[project_id] = set()
        self._pending[project_id].add(file_path)
        self._root_paths[project_id] = root_path

        # Set new flush timer
        def _schedule_flush(pid: str = project_id) -> None:
            self._loop.create_task(self._flush(pid))

        self._flush_timers[project_id] = self._loop.call_later(
            self._debounce_seconds,
            _schedule_flush,
        )

    async def _flush(self, project_id: str) -> None:
        """Flush pending files for a project via gcode subprocess."""
        files = self._pending.pop(project_id, set())
        self._flush_timers.pop(project_id, None)
        root_path = self._root_paths.pop(project_id, None)

        if not files or not root_path:
            return

        gcode_bin = resolve_native_bin("gcode")
        if gcode_bin is None:
            logger.warning("gcode not installed — skipping incremental index. Run `gobby install`.")
            return

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                gcode_bin,
                "index",
                "--files",
                *files,
                "--quiet",
                cwd=root_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode == 0:
                logger.debug(f"gcode indexed {len(files)} files for project {project_id}")
            else:
                detail = stderr.decode().strip() if stderr else "(no stderr)"
                logger.warning(f"gcode index exited {proc.returncode}: {detail}")
        except asyncio.CancelledError:
            try:
                if proc is not None:
                    proc.kill()
                    await proc.wait()
            except ProcessLookupError:
                pass
            raise
        except TimeoutError:
            logger.warning("gcode index timed out after 30s")
            try:
                if proc is not None:
                    proc.kill()
                    await proc.wait()
            except ProcessLookupError:
                pass
        except Exception as e:
            logger.warning(f"gcode index failed: {e}")
