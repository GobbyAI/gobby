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
from gobby.code_index.eligibility import resolve_indexed_project
from gobby.code_index.gcode_gateway import (
    GcodeDaemonConfigUnavailableError,
    GcodeProjectNotFoundError,
    _classify_gcode_command_error,
)
from gobby.code_index.maintenance_launch import open_launch_async

if TYPE_CHECKING:
    from gobby.code_index.context import CodeIndexContext
    from gobby.code_index.summarizer import SymbolSummarizer

logger = logging.getLogger(__name__)

_SUMMARY_DB_WRITE_CONCURRENCY = 4


async def code_index_maintenance_loop(
    context: CodeIndexContext,
    shutdown_flag: asyncio.Event | None = None,
    interval: int = 3600,
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
    logger.info("Code index maintenance loop started (interval=%ss)", interval)
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
            logger.exception("Code index maintenance error: %s", e)

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
    """Single maintenance pass: re-index via gcode and generate summaries."""
    if missing_root_observations is None:
        missing_root_observations = {}

    await _retry_pending_projection_cleanups(context)
    projects = await context.run_db(context.storage.list_indexed_projects)
    gcode_gateway = context.gcode_gateway
    daemon_config_breaker = context.daemon_config_breaker
    index_timeout = context.config.maintenance_index_timeout_seconds

    if gcode_gateway is None:
        logger.warning("gcode unavailable — skipping maintenance index. Run `gobby install`.")

    factory = getattr(context, "launch_factory", None)
    if gcode_gateway is not None and factory is None:
        logger.error("Maintenance reindex skipped: launch factory is not configured")

    active_project_ids = {str(project.id) for project in projects}
    for stale_project_id in set(missing_root_observations) - active_project_ids:
        missing_root_observations.pop(stale_project_id, None)

    for project in projects:
        project_id = str(project.id)
        exists, deleted = await _registry_state(context, project_id)
        decision = await asyncio.to_thread(
            resolve_indexed_project,
            project_id,
            project.root_path,
            project_exists=exists,
            project_deleted=deleted,
        )
        if decision.kind != "active":
            missing_root_observations.pop(project_id, None)
            await _reconcile_stale_selector(context, project_id, decision.kind)
            continue

        missing_root_observations.pop(project_id, None)
        root = decision.root
        if root is None:
            logger.error(
                "Active indexed project %s has no root; treating as stale",
                project_id,
            )
            await _reconcile_stale_selector(context, project_id, "missing_root")
            continue

        if (
            gcode_gateway is not None
            and factory is not None
            and daemon_config_breaker.should_attempt()
        ):
            purge_project = False
            try:
                async with open_launch_async(
                    factory, project_id, timeout_seconds=index_timeout
                ) as launch:
                    result = await gcode_gateway.maintenance_index(
                        root, timeout=index_timeout, env=launch.env
                    )
                daemon_config_breaker.record_success()
                if result.returncode == 3:
                    logger.debug(
                        "Maintenance reindex skipped for %s (index lock busy)",
                        project.id,
                    )
                elif not result.success:
                    detail = result.stderr.strip() or result.stdout.strip() or "<no output>"
                    if result.timed_out:
                        logger.error(
                            "Maintenance reindex timed out for %s: %s",
                            project.id,
                            detail,
                        )
                    else:
                        error = _classify_gcode_command_error(
                            result.command,
                            result.returncode or 1,
                            detail,
                        )
                        if isinstance(error, GcodeProjectNotFoundError):
                            purge_project = True
                        else:
                            logger.error(
                                "Maintenance reindex failed for %s (exit code %s): %s",
                                project.id,
                                result.returncode,
                                detail,
                            )
            except GcodeDaemonConfigUnavailableError:
                daemon_config_breaker.record_failure()
            except Exception:
                daemon_config_breaker.record_inconclusive()
                logger.exception("Maintenance reindex failed for %s", project.id)

            if purge_project:
                await purge_missing_project(
                    project=project,
                    storage=context.storage,
                    run_db=context.run_db,
                )
                continue

        if summarizer:
            await _summarize_unsummarized(
                context,
                project,
                summarizer,
                symbol_summary_batch_size,
            )


async def _registry_state(context: CodeIndexContext, project_id: str) -> tuple[bool, bool]:
    raw = await context.run_db(context.storage.get_registry_project, project_id)
    if not isinstance(raw, tuple) or len(raw) != 2:
        return True, False
    return bool(raw[0]), bool(raw[1])


async def _reconcile_stale_selector(context: CodeIndexContext, project_id: str, reason: str) -> str:
    """Clear projections then the machine-local selector. Leave directories alone."""
    gateway = context.gcode_gateway
    factory = getattr(context, "launch_factory", None)
    exists, _deleted = await _registry_state(context, project_id)
    if exists and factory is not None and gateway is not None:
        try:
            async with open_launch_async(factory, project_id, timeout_seconds=60) as launch:
                await gateway.graph_clear(project_id, env=launch.env)
                await gateway.vector_clear(project_id=project_id, env=launch.env)
        except Exception:
            logger.exception(
                "Code index reconcile failed for %s (%s); retaining selector",
                project_id,
                reason,
            )
            await context.run_db(
                context.storage.record_projection_cleanup_failure,
                project_id,
                "graph",
                f"reconcile:{reason}",
            )
            return "failed"
    await context.run_db(context.storage.delete_project_index, project_id)
    logger.info("Reconciled stale code-index selector %s (%s)", project_id, reason)
    return "reconciled"


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
            "Generated %s summaries for %s (%s skipped/failed)",
            len(results),
            project.id,
            len(symbols) - len(results),
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
