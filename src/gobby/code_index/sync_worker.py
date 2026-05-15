"""Background sync worker for code index external stores.

Polls SQLite for files with vectors_synced=0 or graph_synced=0 and
syncs them to Qdrant (embeddings) and Neo4j (graph edges) in-process.
Replaces the old subprocess-based retry mechanism in maintenance.py.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.code_index.graph import CodeGraph
    from gobby.code_index.models import IndexedFile
    from gobby.code_index.storage import CodeIndexStorage
    from gobby.config.code_index import CodeIndexConfig
    from gobby.config.persistence import EmbeddingsConfig

logger = logging.getLogger(__name__)


async def _run_db(
    run_db: Callable[..., Awaitable[Any]] | None,
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    if run_db is None:
        return await asyncio.to_thread(func, *args, **kwargs)
    return await run_db(func, *args, **kwargs)


async def sync_worker_loop(
    storage: CodeIndexStorage,
    vector_store: Any | None,
    graph: CodeGraph | None,
    config: CodeIndexConfig,
    embeddings_config: EmbeddingsConfig,
    shutdown_flag: asyncio.Event,
    run_db: Callable[..., Awaitable[Any]] | None = None,
) -> None:
    """Continuous worker that syncs pending files to Qdrant and Neo4j.

    Polls every config.sync_worker_interval_seconds (default 5s).
    Processes up to config.sync_worker_batch_size files per poll (default 50).
    Each file's vector and graph sync are independent — one can succeed
    while the other fails and retries on the next poll.
    """
    interval = config.sync_worker_interval_seconds
    batch_size = config.sync_worker_batch_size
    embed_model = None

    logger.info(f"Code index sync worker started (interval={interval}s, batch={batch_size})")

    # Set up embedding adapter for vector sync
    if config.embedding_enabled and vector_store is not None:
        try:
            from gobby.search.embeddings import generate_embeddings

            class _EmbedAdapter:
                """Adapter wrapping generate_embeddings() to match embed_model.embed() interface."""

                async def embed(self, texts: list[str]) -> list[list[float]]:
                    return await generate_embeddings(
                        texts,
                        model=embeddings_config.model,
                        api_base=embeddings_config.api_base,
                        api_key=embeddings_config.api_key,
                        expected_dim=embeddings_config.dim,
                    )

            embed_model = _EmbedAdapter()
        except Exception as e:
            logger.warning(f"Sync worker: embedding unavailable: {e}")

    while not shutdown_flag.is_set():
        try:
            await _sync_pass(
                storage=storage,
                vector_store=vector_store,
                graph=graph,
                config=config,
                embed_model=embed_model,
                batch_size=batch_size,
                embedding_dim=embeddings_config.dim,
                run_db=run_db,
            )
        except Exception as e:
            logger.error(f"Sync worker pass error: {e}", exc_info=True)

        try:
            await asyncio.wait_for(shutdown_flag.wait(), timeout=interval)
            break  # Shutdown signaled
        except TimeoutError:
            pass  # Normal timeout, loop again

    logger.info("Code index sync worker stopped")


async def _sync_pass(
    storage: CodeIndexStorage,
    vector_store: Any | None,
    graph: CodeGraph | None,
    config: CodeIndexConfig,
    embed_model: Any | None,
    batch_size: int,
    embedding_dim: int,
    run_db: Callable[..., Awaitable[Any]] | None = None,
) -> None:
    """Single sync pass across all indexed projects."""
    projects = await _run_db(run_db, storage.list_indexed_projects)

    for project in projects:
        if not project.root_path:
            continue

        files = await _run_db(
            run_db,
            storage.get_pending_sync_files,
            project.id,
            limit=batch_size,
            vectors=config.embedding_enabled,
            graph=config.graph_enabled,
        )
        if not files:
            continue

        root = Path(project.root_path)
        synced_count = 0

        for file in files:
            try:
                did_sync = await _sync_file(
                    storage=storage,
                    vector_store=vector_store,
                    graph=graph,
                    config=config,
                    embed_model=embed_model,
                    project_id=project.id,
                    root=root,
                    file=file,
                    embedding_dim=embedding_dim,
                    run_db=run_db,
                )
                if did_sync:
                    synced_count += 1
            except Exception as e:
                logger.warning(f"Sync worker: failed to sync {file.file_path}: {e}")

        if synced_count > 0:
            logger.info(
                f"Sync worker: processed {synced_count}/{len(files)} files for project {project.id}"
            )


async def _sync_file(
    storage: CodeIndexStorage,
    vector_store: Any | None,
    graph: CodeGraph | None,
    config: CodeIndexConfig,
    embed_model: Any | None,
    project_id: str,
    root: Path,
    file: IndexedFile,
    embedding_dim: int,
    run_db: Callable[..., Awaitable[Any]] | None = None,
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
        if vector_store is not None and embed_model is not None:
            try:
                await _sync_vectors(
                    storage=storage,
                    vector_store=vector_store,
                    embed_model=embed_model,
                    config=config,
                    project_id=project_id,
                    file=current,
                    embedding_dim=embedding_dim,
                    run_db=run_db,
                )
                await _run_db(run_db, storage.mark_vectors_synced, current.id)
                did_work = True
            except Exception as e:
                logger.warning(f"Sync worker: vector sync failed for {current.file_path}: {e}")

    # Graph sync
    if not current.graph_synced and config.graph_enabled:
        if graph is not None and graph.available:
            try:
                await _run_db(run_db, storage.mark_graph_sync_attempted, current.id)
                await _sync_graph(
                    storage=storage,
                    graph=graph,
                    project_id=project_id,
                    file=current,
                    run_db=run_db,
                )
                await _run_db(run_db, storage.mark_graph_synced, current.id)
                did_work = True
            except Exception as e:
                logger.warning(f"Sync worker: graph sync failed for {current.file_path}: {e}")

    return did_work


async def _sync_vectors(
    storage: CodeIndexStorage,
    vector_store: Any,
    embed_model: Any,
    config: CodeIndexConfig,
    project_id: str,
    file: IndexedFile,
    embedding_dim: int,
    run_db: Callable[..., Awaitable[Any]] | None = None,
) -> None:
    """Generate embeddings and upsert to Qdrant for a file's symbols."""
    symbols = await _run_db(run_db, storage.get_symbols_for_file, project_id, file.file_path)
    if not symbols:
        return

    collection = f"{config.qdrant_collection_prefix}{project_id}"
    await vector_store.ensure_collection(collection, embedding_dim)

    # Delete old vectors for this file's symbols
    try:
        await vector_store.delete(
            filters={"file_path": file.file_path, "project_id": project_id},
            collection_name=collection,
        )
    except Exception:
        pass  # Collection may not exist yet

    # Build embedding texts (same format as CodeIndexer._embed_symbols)
    texts = []
    ids = []
    for sym in symbols:
        parts = [sym.qualified_name]
        if sym.signature:
            parts.append(sym.signature)
        if sym.docstring:
            parts.append(sym.docstring[:200])
        texts.append(" ".join(parts))
        ids.append(sym.id)

    # Generate embeddings
    embeddings = await embed_model.embed(texts)

    # Build upsert items
    items = []
    for i, emb in enumerate(embeddings):
        if emb is not None:
            items.append(
                (
                    ids[i],
                    emb,
                    {
                        "name": symbols[i].name,
                        "kind": symbols[i].kind,
                        "file_path": symbols[i].file_path,
                        "project_id": project_id,
                    },
                )
            )

    if items:
        await vector_store.batch_upsert(items=items, collection_name=collection)


async def _sync_graph(
    storage: CodeIndexStorage,
    graph: CodeGraph,
    project_id: str,
    file: IndexedFile,
    *,
    run_db: Callable[..., Awaitable[Any]] | None = None,
) -> None:
    """Write Neo4j edges for a file from SQLite import/call/symbol data."""
    # Read relations from SQLite
    imports = await _run_db(run_db, storage.get_imports_for_file, project_id, file.file_path)
    calls = await _run_db(run_db, storage.get_calls_for_file, project_id, file.file_path)
    symbols = await _run_db(run_db, storage.get_symbols_for_file, project_id, file.file_path)

    # Build contains list (for DEFINES edges)
    contains = [
        {"id": sym.id, "name": sym.name, "kind": sym.kind, "line_start": sym.line_start}
        for sym in symbols
    ]

    # Write to Neo4j
    await graph.sync_file(
        project_id=project_id,
        file_path=file.file_path,
        imports=imports,
        calls=calls,
        contains=contains,
    )
