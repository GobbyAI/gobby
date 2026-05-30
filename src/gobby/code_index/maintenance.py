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
    summary_batch_size: int = 20,
) -> None:
    """Background loop that checks for stale indexed files.

    Args:
        context: CodeIndexContext (provides storage access).
        shutdown_flag: Event that signals shutdown.
        interval: Seconds between maintenance runs.
        summarizer: Optional SymbolSummarizer for generating summaries.
        summary_batch_size: Max symbols to summarize per pass.
    """
    logger.info(f"Code index maintenance loop started (interval={interval}s)")

    while True:
        # Check shutdown
        if shutdown_flag is not None and shutdown_flag.is_set():
            break

        try:
            await _run_maintenance(context, summarizer, summary_batch_size)
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
    summary_batch_size: int = 20,
) -> None:
    """Single maintenance pass: re-index via gcode, recover unsynced files, generate summaries."""
    projects = await context.run_db(context.storage.list_indexed_projects)
    gcode_bin = await asyncio.to_thread(resolve_native_bin, "gcode")

    if gcode_bin is None:
        logger.warning("gcode not installed — skipping maintenance index. Run `gobby install`.")

    for project in projects:
        if not project.root_path:
            continue

        root = Path(project.root_path).expanduser()
        if not await asyncio.to_thread(root.is_dir):
            await purge_missing_project(
                project=project,
                storage=context.storage,
                config=context.config,
                vector_store=context.vector_store,
                clear_graph=context.clear_graph,
                run_db=context.run_db,
            )
            continue

        if gcode_bin is not None:
            proc: asyncio.subprocess.Process | None = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    gcode_bin,
                    "index",
                    "--project",
                    str(root),
                    "--quiet",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                if proc.returncode != 0:
                    detail = stderr.decode().strip() if stderr else "<no stderr>"
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

        # Generate summaries for unsummarized symbols
        if summarizer:
            await _summarize_unsummarized(context, project, summarizer, summary_batch_size)


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

    await _update_symbol_summaries(context, results)

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
    *,
    concurrency: int = _SUMMARY_DB_WRITE_CONCURRENCY,
) -> None:
    """Persist generated summaries with bounded DB write concurrency."""
    semaphore = asyncio.Semaphore(concurrency)

    async def update_one(symbol_id: str, summary: str) -> None:
        async with semaphore:
            await context.run_db(context.storage.update_symbol_summary, symbol_id, summary)

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
