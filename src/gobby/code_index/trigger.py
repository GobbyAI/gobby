"""End-of-tick batching for incremental code index updates.

Accepts notifications from sync threads, coalesces same-turn paths by
project root, and schedules work on the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from gobby.code_index.gcode_gateway import (
    GcodeDaemonConfigUnavailableError,
    GcodeGateway,
)
from gobby.code_index.maintenance_launch import open_launch_async
from gobby.code_index.sync_breaker import SyncCircuitBreaker

if TYPE_CHECKING:
    from gobby.code_index.maintenance_launch import MaintenanceLaunchFactory

logger = logging.getLogger(__name__)


class _LaunchFactorySource(Protocol):
    launch_factory: MaintenanceLaunchFactory | None


class CodeIndexTrigger:
    """End-of-tick trigger for post-edit incremental code indexing.

    Accepts file change notifications from any thread and coalesces
    them into serialized gcode index calls per canonical project root.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 30.0,
        index_timeout_seconds: float = 30.0,
        *,
        gcode_gateway: GcodeGateway,
        daemon_config_breaker: SyncCircuitBreaker,
        launch_factory: MaintenanceLaunchFactory | None = None,
        launch_source: _LaunchFactorySource | None = None,
    ) -> None:
        self._loop = loop
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._index_timeout_seconds = index_timeout_seconds
        self._gcode_gateway = gcode_gateway
        self._daemon_config_breaker = daemon_config_breaker
        self._launch_factory = launch_factory
        self._launch_source = launch_source
        # Pending files grouped by canonical root path.
        self._pending_by_root: dict[str, set[str]] = {}
        self._project_id_by_root: dict[str, str] = {}
        # Overlay claim per root: derived code-index id for worktree/clone
        # roots, None for ordinary project roots.
        self._overlay_by_root: dict[str, str | None] = {}
        self._scheduled_by_root: dict[str, asyncio.Handle] = {}
        self._active_tasks_by_root: dict[str, asyncio.Task[None]] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._retry_delay_by_root: dict[str, float] = {}

    def notify_file_changed(
        self,
        file_path: str,
        project_id: str,
        root_path: str,
        code_overlay_project_id: str | None = None,
    ) -> None:
        """Thread-safe notification that a file was edited.

        Can be called from any thread. Schedules end-of-tick indexing on the
        event loop. ``code_overlay_project_id`` carries the derived overlay id
        when ``root_path`` is a worktree/clone isolation workspace, so the
        launch grant admits gcode's writes under that id.
        """
        self._loop.call_soon_threadsafe(
            self._schedule_file, file_path, project_id, root_path, code_overlay_project_id
        )

    def _schedule_file(
        self,
        file_path: str,
        project_id: str,
        root_path: str,
        code_overlay_project_id: str | None = None,
    ) -> None:
        """Add a file to its root's next batch (runs on the event loop)."""
        root_key = self._root_key(root_path)
        normalized_path = self._normalize_file_path(file_path, root_key)
        self._pending_by_root.setdefault(root_key, set()).add(normalized_path)
        self._project_id_by_root[root_key] = project_id
        self._overlay_by_root[root_key] = code_overlay_project_id

        if root_key in self._scheduled_by_root or root_key in self._active_tasks_by_root:
            return
        self._schedule_batch(root_key)

    def _schedule_batch(self, root_key: str, delay: float | None = None) -> None:
        """Queue one immediate batch or delayed retry for a root."""
        if root_key in self._scheduled_by_root:
            return
        if delay is None:
            handle = self._loop.call_soon(self._start_batch, root_key)
        else:
            handle = self._loop.call_later(delay, self._start_batch, root_key)
        self._scheduled_by_root[root_key] = handle

    def _start_batch(self, root_key: str) -> None:
        """Start a root's pending batch when no other run is active."""
        self._scheduled_by_root.pop(root_key, None)
        if root_key in self._active_tasks_by_root:
            return
        if not self._pending_by_root.get(root_key):
            self._project_id_by_root.pop(root_key, None)
            self._overlay_by_root.pop(root_key, None)
            return

        project_id = self._project_id_by_root[root_key]
        task = self._loop.create_task(self._flush(root_key, project_id))
        self._active_tasks_by_root[root_key] = task
        self._background_tasks.add(task)

        def _consume_result(done_task: asyncio.Task[None]) -> None:
            self._batch_done(root_key, done_task)

        task.add_done_callback(_consume_result)

    def _batch_done(self, root_key: str, task: asyncio.Task[None]) -> None:
        """Consume task completion and queue edits received during the run."""
        self._background_tasks.discard(task)
        if self._active_tasks_by_root.get(root_key) is task:
            self._active_tasks_by_root.pop(root_key, None)

        try:
            task.result()
        except asyncio.CancelledError:
            logger.debug("gcode index batch cancelled for %s", root_key)
        except Exception:
            logger.exception("gcode index batch task failed for %s", root_key)

        if self._pending_by_root.get(root_key):
            if root_key not in self._scheduled_by_root:
                self._schedule_batch(root_key)
        elif root_key not in self._scheduled_by_root:
            self._project_id_by_root.pop(root_key, None)
            self._overlay_by_root.pop(root_key, None)

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
        self._project_id_by_root[root_key] = project_id

        scheduled = self._scheduled_by_root.pop(root_key, None)
        if scheduled is not None:
            scheduled.cancel()

        if retry_delay is None:
            retry_delay = self._retry_delay_by_root.get(root_key, self._retry_base_seconds)
            self._retry_delay_by_root[root_key] = min(
                retry_delay * 2,
                self._retry_max_seconds,
            )

        self._schedule_batch(root_key, delay=retry_delay)

    def _clear_retry_backoff(self, root_key: str) -> None:
        """Reset retry state after a successful index run."""
        self._retry_delay_by_root.pop(root_key, None)

    @staticmethod
    def _root_key(root_path: str) -> str:
        """Return the canonical batching key for a filesystem root."""
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
        scheduled = self._scheduled_by_root.pop(root_key, None)
        if scheduled is not None:
            scheduled.cancel()

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
            if self._launch_source is not None:
                factory = self._launch_source.launch_factory
            else:
                factory = self._launch_factory
            timeout = self._index_timeout_seconds
            if factory is None:
                result = await self._gcode_gateway.incremental_index(
                    Path(root_key),
                    sorted(files),
                    timeout=timeout,
                )
            else:
                async with open_launch_async(
                    factory,
                    project_id,
                    timeout_seconds=timeout,
                    code_overlay_project_id=self._overlay_by_root.get(root_key),
                ) as launch:
                    result = await self._gcode_gateway.incremental_index(
                        Path(root_key),
                        sorted(files),
                        timeout=timeout,
                        env=launch.env,
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
                    logger.warning(
                        "gcode index timed out after %gs for project %s at %s",
                        result.timeout_seconds,
                        project_id,
                        root_key,
                    )
                else:
                    logger.warning(
                        "gcode index exited %s for project %s at %s: %s",
                        result.returncode,
                        project_id,
                        root_key,
                        detail,
                    )
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
            logger.warning("gcode index failed for project %s at %s: %s", project_id, root_key, e)
            self._requeue_for_retry(root_key, project_id, files)
