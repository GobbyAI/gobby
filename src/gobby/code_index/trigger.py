"""Debounced trigger for incremental code index updates.

Accumulates file edit notifications and coalesces them into
batched index_changed_files() calls after a configurable delay.
Thread-safe: accepts notifications from sync threads, schedules
work on the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

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
        # Pending files grouped by canonical root path.
        self._pending_by_root: dict[str, set[str]] = {}
        self._flush_timers_by_root: dict[str, asyncio.TimerHandle] = {}

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
        root_key = self._root_key(root_path)
        normalized_path = self._normalize_file_path(file_path, root_key)

        if root_key in self._flush_timers_by_root:
            self._flush_timers_by_root[root_key].cancel()

        if root_key not in self._pending_by_root:
            self._pending_by_root[root_key] = set()
        self._pending_by_root[root_key].add(normalized_path)

        # Set new flush timer
        def _schedule_flush(root: str = root_key, pid: str = project_id) -> None:
            self._loop.create_task(self._flush(root, pid))

        self._flush_timers_by_root[root_key] = self._loop.call_later(
            self._debounce_seconds,
            _schedule_flush,
        )

    @staticmethod
    def _root_key(root_path: str) -> str:
        """Return the canonical debounce key for a filesystem root."""
        return str(Path(root_path).resolve(strict=False))

    @staticmethod
    def _normalize_file_path(file_path: str, root_key: str) -> str:
        """Return a gcode --files path that resolves under cwd=root_key."""
        root = Path(root_key)
        target = Path(file_path)
        if not target.is_absolute():
            target = root / target

        resolved = target.resolve(strict=False)
        if resolved.is_relative_to(root):
            return os.path.normpath(os.fspath(resolved.relative_to(root)))
        return os.path.normpath(os.fspath(resolved))

    async def _flush(self, root_key: str, project_id: str) -> None:
        """Flush pending files for a root via gcode subprocess."""
        files = self._pending_by_root.pop(root_key, set())
        self._flush_timers_by_root.pop(root_key, None)

        if not files:
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
                cwd=root_key,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode == 0:
                logger.debug(
                    f"gcode indexed {len(files)} files for project {project_id} at {root_key}"
                )
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
