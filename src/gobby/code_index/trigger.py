"""End-of-tick batching for incremental code index updates.

Accepts notifications from sync threads, coalesces same-turn paths by
project root, and schedules work on the asyncio event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Protocol

from gobby.code_index.eligibility import overlay_project_id_for_root
from gobby.code_index.gcode_gateway import (
    GcodeCommandResult,
    GcodeDaemonConfigUnavailableError,
    GcodeGateway,
    _typed_gcode_error,
)
from gobby.code_index.maintenance_launch import open_launch_async
from gobby.code_index.sync_breaker import SyncCircuitBreaker

if TYPE_CHECKING:
    from gobby.code_index.maintenance_launch import MaintenanceLaunchFactory

logger = logging.getLogger(__name__)
_CHECKOUT_MARKERS = (".gobby/isolation.json", ".gobby/project.json")


def _file_version(root: str, path: str) -> tuple[int, int, int, int, int] | None:
    try:
        stat = (Path(root) / path).stat()
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


@dataclass
class _SuspendedBatch:
    overlay_id: str | None
    file_versions: dict[str, tuple[int, int, int, int, int] | None]
    marker_versions: dict[str, tuple[int, int, int, int, int] | None]


class _LaunchFactorySource(Protocol):
    launch_factory: MaintenanceLaunchFactory | None


class CodeIndexTrigger:
    """End-of-tick trigger for post-edit incremental code indexing.

    Accepts file change notifications from any thread and coalesces duplicate
    paths while allowing disjoint gcode index calls to overlap.
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
        self._suspended_by_root: dict[str, _SuspendedBatch] = {}
        self._scheduled_by_root: dict[str, asyncio.Handle] = {}
        self._active_tasks_by_root: dict[str, set[asyncio.Task[None]]] = {}
        self._active_files_by_root: dict[str, set[str]] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._retry_delay_by_root: dict[str, float] = {}

    def notify_file_changed(
        self,
        file_path: str,
        project_id: str,
        root_path: str,
    ) -> None:
        """Thread-safe notification that a file was edited.

        Can be called from any thread. Schedules end-of-tick indexing on the
        event loop. The overlay grant is resolved from the root when the batch
        launches, so a repaired isolation marker is visible on retry.
        """
        self._loop.call_soon_threadsafe(self._schedule_file, file_path, project_id, root_path)

    def _schedule_file(
        self,
        file_path: str,
        project_id: str,
        root_path: str,
    ) -> None:
        """Add a file to its root's next batch (runs on the event loop)."""
        root_key = self._root_key(root_path)
        normalized_path = self._normalize_file_path(file_path, root_key)
        if normalized_path == "." or (Path(root_key) / normalized_path).is_dir():
            logger.debug("Ignoring non-file code index notification: %s", file_path)
            return
        self._pending_by_root.setdefault(root_key, set()).add(normalized_path)
        self._project_id_by_root[root_key] = project_id

        suspended = self._suspended_by_root.get(root_key)
        if suspended is not None:
            overlay_id = overlay_project_id_for_root(Path(root_key))
            if (
                overlay_id == suspended.overlay_id
                and all(
                    _file_version(root_key, marker) == suspended.marker_versions[marker]
                    for marker in _CHECKOUT_MARKERS
                )
                and normalized_path in suspended.file_versions
                and _file_version(root_key, normalized_path)
                == suspended.file_versions[normalized_path]
            ):
                return
            self._suspended_by_root.pop(root_key, None)
            self._clear_retry_backoff(root_key)

        if root_key in self._scheduled_by_root:
            return
        self._schedule_batch(root_key)

    def _schedule_batch(self, root_key: str, delay: float | None = None) -> None:
        """Queue one immediate batch or delayed retry for a root."""
        if root_key in self._scheduled_by_root or root_key in self._suspended_by_root:
            return
        if delay is None:
            handle = self._loop.call_soon(self._start_batch, root_key)
        else:
            handle = self._loop.call_later(delay, self._start_batch, root_key)
        self._scheduled_by_root[root_key] = handle

    def _start_batch(self, root_key: str) -> None:
        """Start a root's pending files that do not overlap active work."""
        self._scheduled_by_root.pop(root_key, None)
        if root_key in self._suspended_by_root:
            return
        pending = self._pending_by_root.get(root_key)
        if not pending:
            if not self._active_tasks_by_root.get(root_key):
                self._project_id_by_root.pop(root_key, None)
            return

        active_files = self._active_files_by_root.setdefault(root_key, set())
        files = pending - active_files
        if not files:
            return
        pending.difference_update(files)
        if not pending:
            self._pending_by_root.pop(root_key, None)
        active_files.update(files)

        project_id = self._project_id_by_root[root_key]
        task = self._loop.create_task(self._flush(root_key, project_id, files=files))
        self._active_tasks_by_root.setdefault(root_key, set()).add(task)
        self._background_tasks.add(task)

        def _consume_result(done_task: asyncio.Task[None]) -> None:
            self._batch_done(root_key, done_task, files)

        task.add_done_callback(_consume_result)

    def _batch_done(
        self,
        root_key: str,
        task: asyncio.Task[None],
        files: set[str],
    ) -> None:
        """Consume task completion and queue edits received during the run."""
        self._background_tasks.discard(task)
        active = self._active_tasks_by_root.get(root_key)
        if active is not None:
            active.discard(task)
            if not active:
                self._active_tasks_by_root.pop(root_key, None)
        active_files = self._active_files_by_root.get(root_key)
        if active_files is not None:
            active_files.difference_update(files)
            if not active_files:
                self._active_files_by_root.pop(root_key, None)

        try:
            task.result()
        except asyncio.CancelledError:
            self._pending_by_root.setdefault(root_key, set()).update(files)
            logger.debug("gcode index batch cancelled for %s", root_key)
            return
        except Exception:
            self._pending_by_root.setdefault(root_key, set()).update(files)
            logger.exception("gcode index batch task failed for %s", root_key)
            return

        if self._pending_by_root.get(root_key):
            if root_key not in self._scheduled_by_root and root_key not in self._suspended_by_root:
                self._schedule_batch(root_key)
        elif root_key not in self._scheduled_by_root and root_key not in self._active_tasks_by_root:
            self._project_id_by_root.pop(root_key, None)

    def _suspend_checkout_failure(self, root_key: str, project_id: str, files: set[str]) -> None:
        """Keep files without retrying a checkout error until the root changes."""
        pending = self._pending_by_root.setdefault(root_key, set())
        notified_during_batch = bool(pending)
        pending.update(files)
        self._project_id_by_root[root_key] = project_id
        if notified_during_batch:
            # The follow-up event has not been indexed yet; allow one more batch.
            self._suspended_by_root.pop(root_key, None)
            return
        self._clear_retry_backoff(root_key)
        scheduled = self._scheduled_by_root.pop(root_key, None)
        if scheduled is not None:
            scheduled.cancel()
        self._suspended_by_root[root_key] = _SuspendedBatch(
            overlay_id=overlay_project_id_for_root(Path(root_key)),
            file_versions={path: _file_version(root_key, path) for path in pending},
            marker_versions={
                marker: _file_version(root_key, marker) for marker in _CHECKOUT_MARKERS
            },
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

    async def _flush(
        self,
        root_key: str,
        project_id: str,
        *,
        files: set[str] | None = None,
    ) -> None:
        """Flush pending files for a root through the shared gcode gateway."""
        if files is None:
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

        started = perf_counter()
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
                    code_overlay_project_id=overlay_project_id_for_root(Path(root_key)),
                ) as launch:
                    result = await self._gcode_gateway.incremental_index(
                        Path(root_key),
                        sorted(files),
                        timeout=timeout,
                        env=launch.env,
                    )
            self._daemon_config_breaker.record_success()
            logger.debug(
                "gcode index timing project=%s root=%s files=%s active_batches=%s "
                "elapsed_seconds=%.3f subprocess_seconds=%.3f",
                project_id,
                root_key,
                len(files),
                len(self._active_tasks_by_root.get(root_key, set())),
                perf_counter() - started,
                result.duration_seconds,
            )
            if result.success:
                self._clear_retry_backoff(root_key)
                self._suspended_by_root.pop(root_key, None)
                busy_files = _busy_files_from_result(result, files)
                if busy_files:
                    self._requeue_for_retry(root_key, project_id, busy_files)
                logger.debug(
                    "gcode indexed %s files for project %s at %s; %s busy",
                    len(files) - len(busy_files),
                    project_id,
                    root_key,
                    len(busy_files),
                )
            elif result.returncode == 3:
                busy_files = _busy_files_from_result(result, files) or files
                logger.debug(
                    "gcode index skipped %s files for project %s (index lock busy); requeuing",
                    len(busy_files),
                    project_id,
                )
                self._requeue_for_retry(root_key, project_id, busy_files)
            else:
                detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
                if result.timed_out:
                    phases = "\n".join(
                        line
                        for line in result.stderr.splitlines()
                        if line.startswith("gcode_index_phase ")
                    )
                    logger.warning(
                        "gcode index timed out after %gs for project %s at %s; "
                        "files=%s active_batches=%s elapsed_seconds=%.3f "
                        "subprocess_seconds=%.3f phases=%s",
                        result.timeout_seconds,
                        project_id,
                        root_key,
                        len(files),
                        len(self._active_tasks_by_root.get(root_key, set())),
                        perf_counter() - started,
                        result.duration_seconds,
                        phases or "(no phase diagnostics)",
                    )
                else:
                    logger.warning(
                        "gcode index exited %s for project %s at %s: %s",
                        result.returncode,
                        project_id,
                        root_key,
                        detail,
                    )
                payload = _typed_gcode_error(result.stderr) if result.returncode == 2 else None
                if payload is not None and payload.get("error") in {
                    "checkout_required",
                    "checkout_mismatch",
                }:
                    self._suspend_checkout_failure(root_key, project_id, files)
                else:
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


def _busy_files_from_result(result: GcodeCommandResult, requested: set[str]) -> set[str]:
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return set()
    if not isinstance(payload, dict):
        return set()
    busy = payload.get("busy_files")
    if not isinstance(busy, list):
        return set()
    return {path for path in busy if isinstance(path, str) and path in requested}
