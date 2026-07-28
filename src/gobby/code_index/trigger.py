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

from gobby.code_index.gcode_gateway import (
    GcodeDaemonConfigUnavailableError,
    GcodeGateway,
)
from gobby.code_index.sync_breaker import SyncCircuitBreaker

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
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 30.0,
        index_timeout_seconds: float = 30.0,
        *,
        gcode_gateway: GcodeGateway,
        daemon_config_breaker: SyncCircuitBreaker,
    ) -> None:
        self._loop = loop
        self._debounce_seconds = debounce_seconds
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._index_timeout_seconds = index_timeout_seconds
        self._gcode_gateway = gcode_gateway
        self._daemon_config_breaker = daemon_config_breaker
        # Pending files grouped by canonical root path.
        self._pending_by_root: dict[str, set[str]] = {}
        self._flush_timers_by_root: dict[str, asyncio.TimerHandle] = {}
        self._retry_delay_by_root: dict[str, float] = {}

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

    def _requeue_for_retry(
        self,
        root_key: str,
        project_id: str,
        files: set[str],
        *,
        retry_delay: float | None = None,
    ) -> None:
        """Return a failed batch to pending files and schedule retry with backoff."""
        self._pending_by_root.setdefault(root_key, set()).update(files)

        if root_key in self._flush_timers_by_root:
            self._flush_timers_by_root[root_key].cancel()

        if retry_delay is None:
            retry_delay = self._retry_delay_by_root.get(root_key, self._retry_base_seconds)
            self._retry_delay_by_root[root_key] = min(
                retry_delay * 2,
                self._retry_max_seconds,
            )

        def _schedule_flush(root: str = root_key, pid: str = project_id) -> None:
            self._loop.create_task(self._flush(root, pid))

        self._flush_timers_by_root[root_key] = self._loop.call_later(
            retry_delay,
            _schedule_flush,
        )

    def _clear_retry_backoff(self, root_key: str) -> None:
        """Reset retry state after a successful index run."""
        self._retry_delay_by_root.pop(root_key, None)

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
        """Flush pending files for a root through the shared gcode gateway."""
        files = self._pending_by_root.pop(root_key, set())
        self._flush_timers_by_root.pop(root_key, None)

        if not files:
            return

        if not self._daemon_config_breaker.should_attempt():
            self._requeue_for_retry(
                root_key,
                project_id,
                files,
                retry_delay=max(
                    self._retry_base_seconds,
                    self._daemon_config_breaker.retry_after_seconds(),
                ),
            )
            return

        try:
            result = await self._gcode_gateway.incremental_index(
                Path(root_key),
                sorted(files),
                timeout=self._index_timeout_seconds,
            )
            self._daemon_config_breaker.record_success()
            if result.success:
                self._clear_retry_backoff(root_key)
                logger.debug(
                    "gcode indexed %s files for project %s at %s", len(files), project_id, root_key
                )
            elif result.returncode == 3:
                logger.debug(
                    "gcode index skipped %s files for project %s (index lock busy); requeuing",
                    len(files),
                    project_id,
                )
                self._requeue_for_retry(root_key, project_id, files)
            else:
                detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
                if result.timed_out:
                    logger.warning("gcode index timed out after %gs", result.timeout_seconds)
                else:
                    logger.warning("gcode index exited %s: %s", result.returncode, detail)
                self._requeue_for_retry(root_key, project_id, files)
        except GcodeDaemonConfigUnavailableError:
            self._daemon_config_breaker.record_failure()
            self._requeue_for_retry(
                root_key,
                project_id,
                files,
                retry_delay=max(
                    self._retry_base_seconds,
                    self._daemon_config_breaker.retry_after_seconds(),
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._daemon_config_breaker.record_success()
            logger.warning("gcode index failed: %s", e)
            self._requeue_for_retry(root_key, project_id, files)
