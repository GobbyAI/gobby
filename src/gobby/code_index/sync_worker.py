"""Background sync worker for code index external projections.

Polls hub-indexed files with unsynced vector or graph flags and syncs them
to gcode-owned vector and graph projections.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.code_index.cleanup import purge_missing_project
from gobby.code_index.gcode_gateway import (
    GcodeCommandError,
    GcodeDaemonConfigUnavailableError,
    GcodeEmbeddingTransportError,
    GcodeFalkorTransportError,
    GcodeIndexedFileNotFoundError,
    GcodeProjectNotFoundError,
    GcodeTimeoutError,
    GcodeUnavailableError,
)
from gobby.code_index.sync_breaker import SyncCircuitBreaker
from gobby.storage.hub.postgres_pool import is_pool_unavailable
from gobby.utils.logging import ThrottledLogger

if TYPE_CHECKING:
    from gobby.code_index.context import CodeIndexContext
    from gobby.code_index.gcode_gateway import GcodeGateway
    from gobby.code_index.models import IndexedFile
    from gobby.code_index.storage import CodeIndexStorage
    from gobby.config.code_index import CodeIndexConfig

logger = logging.getLogger(__name__)

_pool_outage_log = ThrottledLogger()

_EMBEDDING_CONFIG_UNAVAILABLE = "embedding config is required for vector lifecycle commands"
_VECTOR_SYNC_RETRY_BACKOFF_SECONDS = (1.0, 2.0)
_GRAPH_SYNC_RETRY_BACKOFF_SECONDS = _VECTOR_SYNC_RETRY_BACKOFF_SECONDS

_GRAPH_SYNC_LANGUAGES = frozenset(
    {
        "c",
        "cpp",
        "csharp",
        "dart",
        "elixir",
        "go",
        "java",
        "javascript",
        "javascriptreact",
        "jsx",
        "php",
        "python",
        "ruby",
        "rust",
        "tsx",
        "typescript",
        "typescriptreact",
    }
)


@dataclass
class _MissingProject:
    id: str
    root_path: str | None


def _file_needs_graph_sync(file: IndexedFile) -> bool:
    return file.symbol_count > 0 and file.language.lower() in _GRAPH_SYNC_LANGUAGES


def _is_transient_vector_error(error: Exception) -> bool:
    if isinstance(error, (GcodeEmbeddingTransportError, GcodeTimeoutError, GcodeUnavailableError)):
        return True
    return isinstance(error, GcodeCommandError) and (
        _EMBEDDING_CONFIG_UNAVAILABLE in error.stderr.casefold()
    )


def _is_transient_graph_error(error: Exception) -> bool:
    return isinstance(
        error,
        (GcodeFalkorTransportError, GcodeTimeoutError, GcodeUnavailableError),
    )


async def _sync_vector_file_with_retry(
    gcode_gateway: GcodeGateway,
    project_root: Path,
    file: IndexedFile,
    *,
    timeout: float | None = None,
) -> bool:
    attempts = len(_VECTOR_SYNC_RETRY_BACKOFF_SECONDS) + 1
    for attempt in range(1, attempts + 1):
        try:
            return await _sync_vector_file(
                gcode_gateway=gcode_gateway,
                project_root=project_root,
                file=file,
                timeout=timeout,
            )
        except (GcodeCommandError, GcodeTimeoutError, GcodeUnavailableError) as error:
            if not _is_transient_vector_error(error):
                raise
            if attempt == 1:
                logger.warning(
                    "Sync worker: transient vector sync failure for %s; retrying up to %s times: %s",
                    file.file_path,
                    attempts - 1,
                    error,
                )
            if attempt == attempts:
                raise
            await asyncio.sleep(_VECTOR_SYNC_RETRY_BACKOFF_SECONDS[attempt - 1])

    raise AssertionError("unreachable vector retry state")


async def _sync_graph_file_with_retry(
    gcode_gateway: GcodeGateway,
    project_root: Path,
    file: IndexedFile,
    *,
    timeout: float | None = None,
) -> bool:
    attempts = len(_GRAPH_SYNC_RETRY_BACKOFF_SECONDS) + 1
    for attempt in range(1, attempts + 1):
        try:
            return await _sync_graph(
                gcode_gateway=gcode_gateway,
                project_root=project_root,
                file=file,
                timeout=timeout,
            )
        except (GcodeCommandError, GcodeTimeoutError, GcodeUnavailableError) as error:
            if not _is_transient_graph_error(error):
                raise
            if attempt == 1:
                logger.warning(
                    "Sync worker: transient graph sync failure for %s; retrying up to %s times: %s",
                    file.file_path,
                    attempts - 1,
                    error,
                )
            if attempt == attempts:
                raise
            await asyncio.sleep(_GRAPH_SYNC_RETRY_BACKOFF_SECONDS[attempt - 1])

    raise AssertionError("unreachable graph retry state")


def _arm_breakers(
    *breakers: SyncCircuitBreaker | None,
) -> tuple[SyncCircuitBreaker, ...] | None:
    active = tuple(breaker for breaker in breakers if breaker is not None)
    if any(not breaker.pending_allowed() for breaker in active):
        return None
    armed = tuple(breaker for breaker in active if breaker.should_attempt())
    if len(armed) != len(active):
        raise RuntimeError("breaker readiness changed while arming a sync attempt")
    return armed


def _record_breaker_outcomes(
    armed: tuple[SyncCircuitBreaker, ...],
    *,
    failed: tuple[SyncCircuitBreaker | None, ...] = (),
) -> None:
    failed_ids = {id(breaker) for breaker in failed if breaker is not None}
    for breaker in armed:
        if id(breaker) in failed_ids:
            breaker.record_failure()
        else:
            breaker.record_success()


async def _run_db(
    run_db: Callable[..., Awaitable[Any]] | None,
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    if run_db is None:
        return await asyncio.to_thread(func, *args, **kwargs)
    return await run_db(func, *args, **kwargs)


async def _handle_indexed_file_not_found(
    *,
    storage: CodeIndexStorage,
    project_id: str,
    root: Path,
    error: GcodeIndexedFileNotFoundError,
    sync_kind: str,
    synced_field: str,
    run_db: Callable[..., Awaitable[Any]] | None,
) -> bool:
    refreshed = await _run_db(run_db, storage.get_file, project_id, error.file_path)
    if refreshed is None:
        logger.info(
            "Sync worker: indexed file %s disappeared from project %s during "
            "%s sync; leaving %s=false",
            error.file_path,
            project_id,
            sync_kind,
            synced_field,
        )
        return False
    if not await asyncio.to_thread(root.is_dir):
        await purge_missing_project(
            project=_MissingProject(id=project_id, root_path=str(root)),
            storage=storage,
            run_db=run_db,
        )
        return False
    if not await asyncio.to_thread((root / refreshed.file_path).exists):
        return False
    logger.warning(
        "Sync worker: indexed file %s missing in gcode project %s during "
        "%s sync; leaving %s=false for retry: %s",
        refreshed.file_path,
        error.project_id,
        sync_kind,
        synced_field,
        error,
    )
    return True


async def _handle_project_not_found(
    *,
    storage: CodeIndexStorage,
    project_id: str,
    root: Path,
    error: GcodeProjectNotFoundError,
    sync_kind: str,
    file_path: str,
    run_db: Callable[..., Awaitable[Any]] | None,
) -> None:
    if not await asyncio.to_thread(root.is_dir):
        await purge_missing_project(
            project=_MissingProject(id=project_id, root_path=str(root)),
            storage=storage,
            run_db=run_db,
        )
        return
    logger.warning(
        "Sync worker: gcode project missing for %s at %s during %s sync of %s: %s",
        project_id,
        root,
        sync_kind,
        file_path,
        error,
    )


async def sync_worker_loop(
    storage: CodeIndexStorage,
    context: CodeIndexContext,
    config: CodeIndexConfig,
    shutdown_flag: asyncio.Event,
    run_db: Callable[..., Awaitable[Any]] | None = None,
) -> None:
    """Continuous worker that syncs pending files to gcode projections.

    Polls every config.sync_worker_interval_seconds (default 5s).
    Processes up to config.sync_worker_batch_size files per poll (default 50).
    Each file's vector and graph sync are independent — one can succeed
    while the other fails and retries on the next poll.
    """
    interval = config.sync_worker_interval_seconds
    batch_size = config.sync_worker_batch_size
    vector_breaker = SyncCircuitBreaker(
        name="Vector sync",
        probe_target="embedding endpoint",
        operation="vector sync",
        failure_threshold=config.sync_worker_breaker_failure_threshold,
        base_backoff_seconds=config.sync_worker_breaker_backoff_seconds,
        max_backoff_seconds=config.sync_worker_breaker_max_backoff_seconds,
    )
    logger.info(
        "Code index sync worker started (interval=%ss, batch=%s)",
        interval,
        batch_size,
    )

    while not shutdown_flag.is_set():
        gcode_gateway = context.gcode_gateway

        try:
            await _sync_pass(
                storage=storage,
                gcode_gateway=gcode_gateway,
                config=config,
                batch_size=batch_size,
                run_db=run_db,
                vector_breaker=vector_breaker,
                gateway_breaker=context.daemon_config_breaker,
            )
        except Exception as e:
            if is_pool_unavailable(e):
                _pool_outage_log(
                    logger,
                    logging.WARNING,
                    "Sync worker: hub temporarily unavailable; skipping pass",
                )
            else:
                logger.exception("Sync worker pass error: %s", e)

        try:
            await asyncio.wait_for(shutdown_flag.wait(), timeout=interval)
            break  # Shutdown signaled
        except TimeoutError:
            pass  # Normal timeout, loop again

    logger.info("Code index sync worker stopped")


async def _sync_pass(
    storage: CodeIndexStorage,
    gcode_gateway: GcodeGateway | None,
    config: CodeIndexConfig,
    batch_size: int,
    run_db: Callable[..., Awaitable[Any]] | None = None,
    vector_breaker: SyncCircuitBreaker | None = None,
    gateway_breaker: SyncCircuitBreaker | None = None,
) -> None:
    """Single sync pass across all indexed projects."""
    projects = await _run_db(run_db, storage.list_indexed_projects)
    vectors_wanted = config.embedding_enabled and gcode_gateway is not None
    if vector_breaker is not None and not vector_breaker.pending_allowed():
        vectors_wanted = False
    if gateway_breaker is not None and not gateway_breaker.pending_allowed():
        vectors_wanted = False

    for project in projects:
        if not project.root_path:
            continue

        root = Path(project.root_path).expanduser()
        if not await asyncio.to_thread(root.is_dir):
            continue

        files = await _run_db(
            run_db,
            storage.get_pending_sync_files,
            project.id,
            limit=batch_size,
            vectors=vectors_wanted,
            graph=config.graph_enabled,
        )
        if not files:
            continue

        synced_count = 0

        for file in files:
            try:
                did_sync = await _sync_file(
                    storage=storage,
                    gcode_gateway=gcode_gateway,
                    config=config,
                    project_id=project.id,
                    root=root,
                    file=file,
                    run_db=run_db,
                    vector_breaker=vector_breaker,
                    gateway_breaker=gateway_breaker,
                )
                if did_sync:
                    synced_count += 1
            except Exception as e:
                logger.exception(
                    "Sync worker: failed to sync %s: %s",
                    file.file_path,
                    e,
                )

        if synced_count > 0:
            logger.debug(
                "Sync worker: processed %s/%s files for project %s",
                synced_count,
                len(files),
                project.id,
            )


async def _sync_file(
    storage: CodeIndexStorage,
    gcode_gateway: GcodeGateway | None,
    config: CodeIndexConfig,
    project_id: str,
    root: Path,
    file: IndexedFile,
    run_db: Callable[..., Awaitable[Any]] | None = None,
    vector_breaker: SyncCircuitBreaker | None = None,
    gateway_breaker: SyncCircuitBreaker | None = None,
) -> bool:
    """Sync a single file's vectors and/or graph edges. Returns True if any work done."""
    # Validate: file record still exists (not invalidated between poll and process)
    current = await _run_db(run_db, storage.get_file, project_id, file.file_path)
    if current is None:
        return False

    # Validate: file still exists on disk
    full_path = root / current.file_path
    if not full_path.exists():
        return False

    did_work = False

    # Vector sync
    if not current.vectors_synced and config.embedding_enabled:
        armed = (
            _arm_breakers(gateway_breaker, vector_breaker) if gcode_gateway is not None else None
        )
        if gcode_gateway is not None and armed is not None:
            try:
                await _run_db(run_db, storage.mark_vector_sync_attempted, current.id)
                await _sync_vector_file_with_retry(
                    gcode_gateway=gcode_gateway,
                    project_root=root,
                    file=current,
                    timeout=config.sync_worker_projection_timeout_seconds,
                )
            except GcodeDaemonConfigUnavailableError:
                _record_breaker_outcomes(armed, failed=(gateway_breaker,))
                return did_work
            except GcodeIndexedFileNotFoundError as e:
                # Per-file data error: never affects the breaker.
                _record_breaker_outcomes(armed)
                if not await _handle_indexed_file_not_found(
                    storage=storage,
                    project_id=project_id,
                    root=root,
                    error=e,
                    sync_kind="vector",
                    synced_field="vectors_synced",
                    run_db=run_db,
                ):
                    return False
            except GcodeProjectNotFoundError as e:
                _record_breaker_outcomes(armed)
                await _handle_project_not_found(
                    storage=storage,
                    project_id=project_id,
                    root=root,
                    error=e,
                    sync_kind="vector",
                    file_path=current.file_path,
                    run_db=run_db,
                )
                return False
            except GcodeEmbeddingTransportError as e:
                _record_breaker_outcomes(armed, failed=(vector_breaker,))
                logger.error(
                    "Sync worker: vector sync retries exhausted for %s: %s",
                    current.file_path,
                    e,
                )
            except (GcodeTimeoutError, GcodeUnavailableError) as e:
                _record_breaker_outcomes(armed, failed=(vector_breaker,))
                logger.error(
                    "Sync worker: vector sync retries exhausted for %s: %s",
                    current.file_path,
                    e,
                )
            except GcodeCommandError as e:
                if _is_transient_vector_error(e):
                    _record_breaker_outcomes(armed, failed=(gateway_breaker,))
                    logger.error(
                        "Sync worker: vector sync retries exhausted for %s: %s",
                        current.file_path,
                        e,
                    )
                else:
                    _record_breaker_outcomes(armed)
                    logger.exception(
                        "Sync worker: vector sync failed for %s: %s",
                        current.file_path,
                        e,
                    )
            except Exception as e:
                _record_breaker_outcomes(armed)
                logger.exception(
                    "Sync worker: vector sync failed for %s: %s",
                    current.file_path,
                    e,
                )
            else:
                _record_breaker_outcomes(armed)
                await _run_db(run_db, storage.mark_vectors_synced, current.id, current.content_hash)
                did_work = True

    # Graph sync
    if not current.graph_synced and config.graph_enabled:
        if gcode_gateway is not None:
            try:
                if not _file_needs_graph_sync(current):
                    await _run_db(
                        run_db, storage.mark_graph_synced, current.id, current.content_hash
                    )
                    did_work = True
                else:
                    armed = _arm_breakers(gateway_breaker)
                    if armed is not None:
                        try:
                            await _run_db(run_db, storage.mark_graph_sync_attempted, current.id)
                            graph_synced = await _sync_graph_file_with_retry(
                                gcode_gateway=gcode_gateway,
                                project_root=root,
                                file=current,
                                timeout=config.sync_worker_projection_timeout_seconds,
                            )
                        except GcodeDaemonConfigUnavailableError:
                            _record_breaker_outcomes(armed, failed=(gateway_breaker,))
                            return did_work
                        except GcodeIndexedFileNotFoundError as e:
                            _record_breaker_outcomes(armed)
                            if not await _handle_indexed_file_not_found(
                                storage=storage,
                                project_id=project_id,
                                root=root,
                                error=e,
                                sync_kind="graph",
                                synced_field="graph_synced",
                                run_db=run_db,
                            ):
                                return False
                        except GcodeProjectNotFoundError as e:
                            _record_breaker_outcomes(armed)
                            await _handle_project_not_found(
                                storage=storage,
                                project_id=project_id,
                                root=root,
                                error=e,
                                sync_kind="graph",
                                file_path=current.file_path,
                                run_db=run_db,
                            )
                            return False
                        except (
                            GcodeFalkorTransportError,
                            GcodeTimeoutError,
                            GcodeUnavailableError,
                        ) as e:
                            _record_breaker_outcomes(armed)
                            logger.error(
                                "Sync worker: graph sync retries exhausted for %s: %s",
                                current.file_path,
                                e,
                            )
                        except GcodeCommandError as e:
                            _record_breaker_outcomes(armed)
                            logger.exception(
                                "Sync worker: graph sync failed for %s: %s",
                                current.file_path,
                                e,
                            )
                        except Exception as e:
                            _record_breaker_outcomes(armed)
                            logger.exception(
                                "Sync worker: graph sync failed for %s: %s",
                                current.file_path,
                                e,
                            )
                        else:
                            _record_breaker_outcomes(armed)
                            if graph_synced:
                                await _run_db(
                                    run_db,
                                    storage.mark_graph_synced,
                                    current.id,
                                    current.content_hash,
                                )
                                did_work = True
            except Exception as e:
                logger.exception(
                    "Sync worker: graph sync failed for %s: %s",
                    current.file_path,
                    e,
                )

    return did_work


async def _sync_vector_file(
    gcode_gateway: GcodeGateway,
    project_root: Path,
    file: IndexedFile,
    *,
    timeout: float | None = None,
) -> bool:
    """Delegate one file's vector projection sync to gcode."""
    result = await gcode_gateway.vector_sync_file(project_root, file.file_path, timeout=timeout)
    if not result.get("success", True):
        raise RuntimeError(result.get("error", "gcode vector sync-file failed"))
    return True


async def _sync_graph(
    gcode_gateway: GcodeGateway,
    project_root: Path,
    file: IndexedFile,
    *,
    timeout: float | None = None,
) -> bool:
    """Ask gcode to sync one indexed file into the code graph projection."""
    result = await gcode_gateway.graph_sync_file(project_root, file.file_path, timeout=timeout)
    # Treat stale gcode skip responses as terminal for the daemon queue. gcode
    # owns index eligibility; the daemon should not retry a file gcode skipped.
    if result.get("status") == "skipped" and result.get("reason") == "indexed_file_not_found":
        return True
    return True
