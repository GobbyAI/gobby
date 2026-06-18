"""Background maintenance loop for code indexing.

Periodically walks indexed projects, triggers re-indexing via gcode,
and recovers files with incomplete graph/vector sync.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.code_index.cleanup import purge_missing_project
from gobby.code_index.gcode_gateway import (
    GcodeProjectNotFoundError,
    _classify_gcode_command_error,
)
from gobby.utils.native_bin import resolve_native_bin

if TYPE_CHECKING:
    from gobby.code_index.context import CodeIndexContext
    from gobby.code_index.summarizer import SymbolSummarizer

logger = logging.getLogger(__name__)

_SUMMARY_DB_WRITE_CONCURRENCY = 4


async def code_index_maintenance_loop(
    context: CodeIndexContext,
    shutdown_flag: asyncio.Event | None = None,
    interval: int = 300,
    summarizer: SymbolSummarizer | None = None,
    symbol_summary_batch_size: int = 20,
) -> None:
    """Background loop that checks for stale indexed files.

    Args:
        context: CodeIndexContext (provides storage access).
        shutdown_flag: Event that signals shutdown.
        interval: Seconds between maintenance runs.
        summarizer: Optional SymbolSummarizer for generating summaries.
        symbol_summary_batch_size: Max symbols to summarize per pass.
    """
    logger.info(f"Code index maintenance loop started (interval={interval}s)")
    missing_root_observations: dict[str, int] = {}

    while True:
        # Check shutdown
        if shutdown_flag is not None and shutdown_flag.is_set():
            break

        try:
            await _run_maintenance(
                context,
                summarizer,
                symbol_summary_batch_size,
                missing_root_observations=missing_root_observations,
            )
        except Exception as e:
            logger.error(f"Code index maintenance error: {e}", exc_info=True)

        # Wait for interval or shutdown
        if shutdown_flag is not None:
            try:
                await asyncio.wait_for(shutdown_flag.wait(), timeout=interval)
                break  # Shutdown signaled
            except TimeoutError:
                pass  # Normal timeout, loop again
        else:
            await asyncio.sleep(interval)

    logger.info("Code index maintenance loop stopped")


async def _run_maintenance(
    context: CodeIndexContext,
    summarizer: SymbolSummarizer | None = None,
    symbol_summary_batch_size: int = 20,
    missing_root_observations: dict[str, int] | None = None,
) -> None:
    """Single maintenance pass: re-index via gcode, recover unsynced files, generate summaries."""
    if missing_root_observations is None:
        missing_root_observations = {}

    await _retry_pending_projection_cleanups(context)
    projects = await context.run_db(context.storage.list_indexed_projects)
    gcode_bin = await asyncio.to_thread(resolve_native_bin, "gcode")

    if gcode_bin is None:
        logger.warning("gcode not installed — skipping maintenance index. Run `gobby install`.")

    active_project_ids = {str(project.id) for project in projects}
    for stale_project_id in set(missing_root_observations) - active_project_ids:
        missing_root_observations.pop(stale_project_id, None)

    for project in projects:
        project_id = str(project.id)
        if not project.root_path:
            missing_root_observations.pop(project_id, None)
            continue

        root = Path(project.root_path).expanduser()
        if not await asyncio.to_thread(root.is_dir):
            observations = missing_root_observations.get(project_id, 0) + 1
            missing_root_observations[project_id] = observations
            threshold = context.config.missing_root_purge_observations
            if observations >= threshold:
                await purge_missing_project(
                    project=project,
                    storage=context.storage,
                    config=context.config,
                    clear_graph=context.clear_graph,
                    run_db=context.run_db,
                )
                missing_root_observations.pop(project_id, None)
            continue

        missing_root_observations.pop(project_id, None)

        if gcode_bin is not None:
            proc: asyncio.subprocess.Process | None = None
            purge_project = False
            try:
                command = [
                    gcode_bin,
                    "index",
                    "--project",
                    str(root),
                    "--quiet",
                    "--sync-projections",
                ]
                proc = await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                if proc.returncode != 0:
                    detail = stderr.decode(errors="replace").strip() if stderr else "<no stderr>"
                    error = _classify_gcode_command_error(
                        command,
                        proc.returncode or 1,
                        detail,
                    )
                    if isinstance(error, GcodeProjectNotFoundError):
                        purge_project = True
                    else:
                        logger.warning(
                            f"Maintenance reindex failed for {project.id} "
                            f"(exit code {proc.returncode}): {detail}"
                        )
            except asyncio.CancelledError:
                if proc is not None:
                    try:
                        proc.kill()
                        await proc.wait()
                    except ProcessLookupError:
                        pass
                raise
            except TimeoutError:
                if proc is not None:
                    try:
                        proc.kill()
                        await proc.wait()
                    except ProcessLookupError:
                        pass
                logger.warning(f"Maintenance reindex timed out for {project.id}")
            except Exception as e:
                logger.warning(
                    "Maintenance reindex failed for %s: %s", project.id, e, exc_info=True
                )

            if purge_project:
                await purge_missing_project(
                    project=project,
                    storage=context.storage,
                    config=context.config,
                    clear_graph=context.clear_graph,
                    run_db=context.run_db,
                )
                continue

        await _reconcile_orphan_files(context, project.id, root)

        # Generate summaries for unsummarized symbols
        if summarizer:
            await _summarize_unsummarized(
                context,
                project,
                summarizer,
                symbol_summary_batch_size,
            )


async def _reconcile_orphan_files(
    context: CodeIndexContext,
    project_id: str,
    root: Path,
) -> None:
    indexed_files = await context.run_db(context.storage.list_files, project_id)
    exists_results = await asyncio.gather(
        *(asyncio.to_thread((root / file.file_path).exists) for file in indexed_files)
    )
    current_paths = {
        file.file_path for file, exists in zip(indexed_files, exists_results, strict=True) if exists
    }
    orphan_paths = await context.run_db(context.storage.get_orphan_files, project_id, current_paths)
    if not orphan_paths:
        return

    cleaned_paths: list[str] = []
    for file_path in orphan_paths:
        if context.config.embedding_enabled and context.gcode_gateway is not None:
            try:
                result = await context.gcode_gateway.vector_sync_file(root, file_path)
                if not result.get("success", True):
                    raise RuntimeError(result.get("error", "gcode vector sync-file failed"))
            except Exception as e:
                logger.warning(
                    "Vector cleanup failed for orphaned code index file %s:%s: %s",
                    project_id,
                    file_path,
                    e,
                    exc_info=True,
                )
                continue

        await context.run_db(context.storage.delete_imports_for_file, project_id, file_path)
        await context.run_db(context.storage.delete_calls_for_file, project_id, file_path)
        await context.run_db(context.storage.delete_content_chunks_for_file, project_id, file_path)
        await context.run_db(context.storage.delete_symbols_for_file, project_id, file_path)
        await context.run_db(context.storage.delete_file, project_id, file_path)
        cleaned_paths.append(file_path)

    if cleaned_paths:
        await context.run_db(
            context.storage.mark_prune_dirty,
            project_id,
            str(root),
            "orphan_files",
        )

    if not cleaned_paths or not context.config.graph_enabled:
        return

    try:
        result = await context.clear_graph(project_id)
        if not result.get("success", False):
            error = str(result.get("error", "unknown error"))
            await context.run_db(
                context.storage.record_projection_cleanup_failure,
                project_id,
                "graph",
                error,
            )
            logger.warning(
                "Graph cleanup reported failure for orphan files in %s: %s", project_id, error
            )
            return
    except Exception as e:
        await context.run_db(
            context.storage.record_projection_cleanup_failure,
            project_id,
            "graph",
            str(e),
        )
        logger.warning(
            "Graph cleanup failed for orphan files in %s: %s",
            project_id,
            e,
            exc_info=True,
        )
        return

    await context.run_db(context.storage.reset_graph_sync_for_project, project_id)


async def _retry_pending_projection_cleanups(
    context: CodeIndexContext,
    *,
    limit: int = 100,
) -> None:
    pending = await context.run_db(context.storage.list_projection_cleanup_pending, limit)
    for marker in pending:
        if marker.store == "graph":
            await _retry_pending_graph_cleanup(context, marker.project_id)
        elif marker.store == "vector":
            await _retry_pending_vector_cleanup(context, marker.project_id)
        else:
            logger.warning(
                "Unknown code index projection cleanup store %s for project %s",
                marker.store,
                marker.project_id,
            )


async def _retry_pending_graph_cleanup(context: CodeIndexContext, project_id: str) -> None:
    if not context.config.graph_enabled:
        await context.run_db(
            context.storage.record_projection_cleanup_failure,
            project_id,
            "graph",
            "graph cleanup unavailable",
        )
        return

    try:
        result = await context.clear_graph(project_id)
        if not result.get("success", False):
            error = str(result.get("error", "unknown error"))
            await context.run_db(
                context.storage.record_projection_cleanup_failure,
                project_id,
                "graph",
                error,
            )
            logger.warning("Pending graph cleanup reported failure for %s: %s", project_id, error)
            return
    except Exception as e:
        await context.run_db(
            context.storage.record_projection_cleanup_failure,
            project_id,
            "graph",
            str(e),
        )
        logger.warning("Pending graph cleanup failed for %s: %s", project_id, e, exc_info=True)
        return

    await context.run_db(context.storage.clear_projection_cleanup_pending, project_id, "graph")


async def _retry_pending_vector_cleanup(context: CodeIndexContext, project_id: str) -> None:
    if not context.config.embedding_enabled or context.gcode_gateway is None:
        await context.run_db(
            context.storage.record_projection_cleanup_failure,
            project_id,
            "vector",
            "vector cleanup unavailable",
        )
        return

    project = await context.run_db(context.storage.get_project_stats, project_id)
    if project is None or not project.root_path:
        await context.run_db(context.storage.clear_projection_cleanup_pending, project_id, "vector")
        return

    root = Path(project.root_path).expanduser()
    try:
        result = await context.gcode_gateway.vector_clear(root)
        if not result.get("success", True):
            raise RuntimeError(result.get("error", "gcode vector clear failed"))
    except Exception as e:
        await context.run_db(
            context.storage.record_projection_cleanup_failure,
            project_id,
            "vector",
            str(e),
        )
        logger.warning("Pending vector cleanup failed for %s: %s", project_id, e, exc_info=True)
        return

    await context.run_db(context.storage.clear_projection_cleanup_pending, project_id, "vector")


async def _summarize_unsummarized(
    context: CodeIndexContext,
    project: Any,
    summarizer: SymbolSummarizer,
    batch_size: int,
) -> None:
    """Generate summaries for symbols that don't have one yet."""
    symbols = await context.run_db(
        context.storage.get_unsummarized_symbols,
        project.id,
        limit=batch_size,
    )
    if not symbols:
        return

    root = Path(project.root_path)
    source_by_symbol_id = await asyncio.to_thread(_read_symbol_sources, root, symbols)

    def read_source(symbol: Any) -> str | None:
        return source_by_symbol_id.get(symbol.id)

    results = await summarizer.summarize_batch(symbols, read_source)
    attempted_symbol_ids = {
        symbol.id for symbol in symbols if source_by_symbol_id.get(symbol.id) is not None
    }
    content_hash_by_symbol_id = {symbol.id: symbol.content_hash for symbol in symbols}
    failed_symbols = [
        (symbol_id, content_hash_by_symbol_id[symbol_id])
        for symbol_id in sorted(attempted_symbol_ids - set(results))
    ]
    if failed_symbols:
        await context.run_db(context.storage.mark_symbol_summaries_attempted, failed_symbols)
        logger.warning(
            "Summary generation failed for %s/%s symbol(s) in project %s",
            len(failed_symbols),
            len(attempted_symbol_ids),
            project.id,
        )

    await _update_symbol_summaries(context, results, content_hash_by_symbol_id)

    if results:
        logger.debug(
            f"Generated {len(results)} summaries for {project.id} "
            f"({len(symbols) - len(results)} skipped/failed)"
        )


def _read_symbol_sources(root: Path, symbols: list[Any]) -> dict[str, str | None]:
    return {symbol.id: _read_symbol_source(root, symbol) for symbol in symbols}


async def _update_symbol_summaries(
    context: CodeIndexContext,
    results: dict[str, str],
    content_hash_by_symbol_id: dict[str, str],
    *,
    concurrency: int = _SUMMARY_DB_WRITE_CONCURRENCY,
) -> None:
    """Persist generated summaries with bounded DB write concurrency."""
    semaphore = asyncio.Semaphore(concurrency)

    async def update_one(symbol_id: str, summary: str) -> None:
        async with semaphore:
            content_hash = content_hash_by_symbol_id.get(symbol_id)
            if content_hash is None:
                return
            await context.run_db(
                context.storage.update_symbol_summary,
                symbol_id,
                content_hash,
                summary,
            )

    items = list(results.items())
    write_results = await asyncio.gather(
        *(update_one(symbol_id, summary) for symbol_id, summary in items),
        return_exceptions=True,
    )
    for (symbol_id, _summary), result in zip(items, write_results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            logger.warning("Failed to persist summary for symbol %s: %s", symbol_id, result)


def _read_symbol_source(root: Path, symbol: Any) -> str | None:
    """Read symbol source from disk."""
    full_path = root / symbol.file_path
    if not full_path.exists():
        return None
    try:
        lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
        # line_start/line_end are 1-indexed
        start = max(0, symbol.line_start - 1)
        end = symbol.line_end
        return "\n".join(lines[start:end])
    except OSError:
        return None
