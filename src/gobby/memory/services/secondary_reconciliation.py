"""Fenced secondary-index reconciliation for a single memory.

SQL uses a short statement timeout on the lock connection. Embedding,
VectorStore, and Falkor I/O run while ``pg_advisory_lock(_memory_lock_key)``
is held, but they are not wrapped in ``run_bounded_db``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Protocol

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from gobby.memory.embedding_text import memory_embedding_text
from gobby.memory.services.crossref import CrossrefService, _crossref_scope_filter
from gobby.memory.vectorstore import is_recoverable_vector_store_error
from gobby.storage.hub.async_ops import run_bounded_db
from gobby.storage.memories import LocalMemoryManager, Memory
from gobby.storage.memories_crud import _memory_lock_key

if TYPE_CHECKING:
    from gobby.config.persistence import MemoryConfig
    from gobby.memory.services.knowledge_graph import KnowledgeGraphService
    from gobby.memory.vectorstore import VectorStore

logger = logging.getLogger(__name__)

SECONDARY_RECONCILIATION_IO_SECONDS = 60.0


class ReconciliationHost(Protocol):
    """Collaborators required to rebuild one memory's secondary stores."""

    _config: MemoryConfig
    _vector_store: VectorStore | None
    _embed_fn: Callable[..., Any] | None
    _crossref_service: CrossrefService
    _kg_service_provider: Callable[[], KnowledgeGraphService | None]

    @property
    def storage(self) -> LocalMemoryManager: ...

    async def embed_and_upsert(
        self,
        memory_id: str,
        content: str,
        payload: dict[str, Any] | None = None,
    ) -> bool: ...

    def _log_vector_store_failure(self, message: str, error: BaseException) -> None: ...


def _mutator_budget_seconds() -> float:
    from gobby.memory.services.lifecycle import MUTATOR_RECONCILIATION_BUDGET_SECONDS

    return MUTATOR_RECONCILIATION_BUDGET_SECONDS


def _supersession_budget_seconds() -> float:
    from gobby.memory.services.lifecycle import SUPERSESSION_CLEANUP_BUDGET_SECONDS

    return SUPERSESSION_CLEANUP_BUDGET_SECONDS


def _timeout_milliseconds(remaining: float) -> int:
    return max(1, math.floor(remaining * 1000.0))


@asynccontextmanager
async def memory_row_session(
    conninfo: str,
    memory_id: str,
    *,
    timeout_seconds: float | None = None,
) -> AsyncIterator[psycopg.AsyncConnection[Any]]:
    """Hold ``pg_advisory_lock(_memory_lock_key)`` with no idle transaction."""
    lock_key = _memory_lock_key(memory_id)
    deadline = _mutator_budget_seconds() if timeout_seconds is None else timeout_seconds
    timeout_ms = _timeout_milliseconds(deadline)
    connection = await psycopg.AsyncConnection.connect(
        conninfo,
        connect_timeout=max(1, math.ceil(deadline)),
        prepare_threshold=None,
    )
    locked = False
    try:
        await connection.execute(
            sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(timeout_ms))
        )
        await connection.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
        locked = True
        await connection.commit()
        yield connection
    finally:
        try:
            if not connection.closed:
                await connection.rollback()
                if locked:
                    await connection.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
                    await connection.commit()
        finally:
            await connection.close()


async def _run_sql_slice[T](
    connection: psycopg.AsyncConnection[Any],
    deadline_seconds: float,
    work: Callable[[psycopg.AsyncConnection[Any]], Awaitable[T]],
) -> T:
    timeout_ms = _timeout_milliseconds(deadline_seconds)
    await connection.execute(
        sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(timeout_ms))
    )
    await connection.execute(sql.SQL("SET LOCAL lock_timeout = {}").format(sql.Literal(timeout_ms)))
    try:
        result = await work(connection)
        await connection.commit()
        return result
    except Exception:
        await connection.rollback()
        raise


def _snapshot_matches(row: dict[str, Any], memory: Memory) -> bool:
    return (
        row.get("deleted_at") is None
        and row["content"] == memory.content
        and str(row["project_id"]) == memory.project_id
        and bool(row["is_global"]) == memory.is_global
        and row["memory_type"] == memory.memory_type.value
    )


async def _load_active_snapshot(
    connection: psycopg.AsyncConnection[Any],
    memory: Memory,
) -> Memory | None:
    async with connection.cursor(row_factory=dict_row) as cursor:
        await cursor.execute(
            """
            SELECT *
            FROM memories
            WHERE id = %s
            FOR SHARE
            """,
            (memory.id,),
        )
        row = await cursor.fetchone()
    if row is None or not _snapshot_matches(row, memory):
        return None
    return Memory.from_row(row)


async def _index_payload(
    host: ReconciliationHost,
    current: Memory,
    *,
    payload_only: bool,
) -> bool:
    payload = {
        "project_id": current.project_id,
        "is_global": current.is_global,
        "memory_type": current.memory_type.value,
    }
    if not payload_only:
        return await host.embed_and_upsert(
            current.id,
            memory_embedding_text(current.content, current.rationale),
            payload=payload,
        )
    if host._vector_store is None:
        return False
    try:
        await host._vector_store.set_payload(current.id, payload)
    except Exception as exc:
        host._log_vector_store_failure(
            f"VectorStore payload update failed for {current.id}",
            exc,
        )
        return False
    return True


async def _cleanup_graph(
    host: ReconciliationHost,
    current: Memory,
    *,
    payload_only: bool,
    graph_cleanup_project_id: str | None,
    graph_cleanup_is_global: bool | None,
) -> None:
    if payload_only:
        return
    kg_service = host._kg_service_provider()
    if kg_service is None:
        return
    if graph_cleanup_project_id is None and graph_cleanup_is_global is None:
        return
    await kg_service.remove_memory_from_graph(
        current.id,
        project_id=graph_cleanup_project_id,
        is_global=graph_cleanup_is_global,
    )


async def _search_crossref_candidates(
    host: ReconciliationHost,
    current: Memory,
) -> list[tuple[str, float]] | None:
    if not getattr(host._config, "auto_crossref", False):
        return None
    if host._vector_store is None or host._embed_fn is None:
        return None
    embedding = await host._embed_fn(memory_embedding_text(current.content, current.rationale))
    max_links = getattr(host._config, "crossref_max_links", None) or 5
    results = await host._vector_store.search(
        embedding,
        limit=max_links + 1,
        filters=_crossref_scope_filter(current.project_id, current.is_global),
    )
    return list(results)


async def _apply_reconcile_sql(
    host: ReconciliationHost,
    connection: psycopg.AsyncConnection[Any],
    memory: Memory,
    *,
    payload_only: bool,
    indexed: bool,
    search_results: list[tuple[str, float]] | None,
) -> bool:
    current = await _load_active_snapshot(connection, memory)
    if current is None:
        return False

    if not payload_only:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                UPDATE memories
                SET graph_processed = FALSE,
                    graph_attempts = 0,
                    graph_status = 'pending'
                WHERE id = %s
                """,
                (current.id,),
            )

    crossrefs_converged = True
    if payload_only:
        pass
    elif getattr(host._config, "auto_crossref", False):
        if search_results is None:
            crossrefs_converged = False
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    DELETE FROM memory_crossrefs
                    WHERE source_id = %s OR target_id = %s
                    """,
                    (current.id, current.id),
                )
        else:
            threshold = getattr(host._config, "crossref_threshold", None) or 0.7
            max_links = getattr(host._config, "crossref_max_links", None) or 5
            await host._crossref_service._replace_fenced(
                current,
                search_results,
                threshold,
                max_links,
                connection,
            )
    else:
        async with connection.cursor() as cursor:
            await cursor.execute(
                """
                DELETE FROM memory_crossrefs
                WHERE source_id = %s OR target_id = %s
                """,
                (current.id, current.id),
            )

    if not indexed or not crossrefs_converged:
        return False
    async with connection.cursor() as cursor:
        await cursor.execute(
            """
            UPDATE memories
            SET vector_needs_reindex = FALSE
            WHERE id = %s
              AND content = %s
              AND project_id = %s
              AND is_global = %s
              AND memory_type = %s
              AND deleted_at IS NULL
            """,
            (
                current.id,
                current.content,
                current.project_id,
                current.is_global,
                current.memory_type.value,
            ),
        )
        return int(cursor.rowcount) == 1


async def reconcile_active_snapshot(
    host: ReconciliationHost,
    memory: Memory,
    *,
    graph_cleanup_project_id: str | None = None,
    graph_cleanup_is_global: bool | None = None,
    payload_only: bool = False,
    notify_changed: bool = True,
) -> bool:
    """Rebuild secondaries only while the scheduled active row remains locked."""
    try:
        async with memory_row_session(
            host.storage.db.conninfo,
            memory.id,
            timeout_seconds=_mutator_budget_seconds(),
        ) as connection:

            async def verify(conn: psycopg.AsyncConnection[Any]) -> Memory | None:
                return await _load_active_snapshot(conn, memory)

            current = await _run_sql_slice(connection, _mutator_budget_seconds(), verify)
            if current is None:
                return False

            search_results: list[tuple[str, float]] | None = None
            try:
                async with asyncio.timeout(SECONDARY_RECONCILIATION_IO_SECONDS):
                    indexed = await _index_payload(host, current, payload_only=payload_only)
                    await _cleanup_graph(
                        host,
                        current,
                        payload_only=payload_only,
                        graph_cleanup_project_id=graph_cleanup_project_id,
                        graph_cleanup_is_global=graph_cleanup_is_global,
                    )
                    if not payload_only:
                        search_results = await _search_crossref_candidates(host, current)
            except TimeoutError:
                logger.warning(
                    "Fenced secondary reconciliation failed for %s: %s",
                    memory.id,
                    "secondary I/O deadline expired",
                )
                return False

            async def apply(conn: psycopg.AsyncConnection[Any]) -> bool:
                return await _apply_reconcile_sql(
                    host,
                    conn,
                    current,
                    payload_only=payload_only,
                    indexed=indexed,
                    search_results=search_results,
                )

            converged = await _run_sql_slice(connection, _mutator_budget_seconds(), apply)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Fenced secondary reconciliation failed for %s: %s", memory.id, exc)
        return False
    if converged:
        memory.vector_needs_reindex = False
        if notify_changed:
            host.storage.notify_changed()
    return converged


async def _delete_artifacts(
    host: ReconciliationHost,
    memory_id: str,
    project_id: str | None,
    is_global: bool | None,
) -> None:
    if host._vector_store:
        try:
            await host._vector_store.delete(memory_id)
        except Exception as exc:
            if is_recoverable_vector_store_error(exc):
                host._log_vector_store_failure(
                    f"VectorStore purge unavailable for {memory_id}", exc
                )
            else:
                logger.warning("VectorStore purge failed for %s: %s", memory_id, exc)
    kg_service = host._kg_service_provider()
    if kg_service:
        try:
            await kg_service.remove_memory_from_graph(
                memory_id,
                project_id=project_id,
                is_global=is_global,
            )
        except Exception as exc:
            logger.warning("Graph purge failed for %s: %s", memory_id, exc)


async def _stamp_recreated_row(host: ReconciliationHost, memory_id: str) -> None:
    async def stamp(connection: Any, _remaining: float) -> bool:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(
                """
                SELECT deleted_at
                FROM memories
                WHERE id = %s
                FOR UPDATE
                """,
                (memory_id,),
            )
            row = await cursor.fetchone()
            if row is None or row["deleted_at"] is not None:
                return False
            await cursor.execute(
                """
                UPDATE memories
                SET vector_needs_reindex = TRUE,
                    graph_processed = FALSE,
                    graph_attempts = 0,
                    graph_status = 'pending'
                WHERE id = %s
                """,
                (memory_id,),
            )
            return True

    try:
        recreated = await run_bounded_db(
            stamp,
            conninfo=host.storage.db.conninfo,
            deadline_seconds=_supersession_budget_seconds(),
            lock_timeout=True,
        )
    except Exception as exc:
        logger.warning("Recreated-row repair stamp failed for %s: %s", memory_id, exc)
        return
    if recreated:
        host.storage.notify_changed()


async def purge_secondary_indices(
    host: ReconciliationHost,
    memory_id: str,
    project_id: str | None = None,
    is_global: bool | None = None,
    *,
    require_hidden: bool = False,
) -> None:
    """Drop a removed memory's VectorStore vector and FalkorDB graph artifacts."""
    if require_hidden:

        async def load_hidden(
            connection: psycopg.AsyncConnection[Any],
        ) -> tuple[str, bool] | None:
            async with connection.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    """
                    SELECT project_id, is_global, deleted_at
                    FROM memories
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (memory_id,),
                )
                row = await cursor.fetchone()
            if row is None or row["deleted_at"] is None:
                return None
            return str(row["project_id"]), bool(row["is_global"])

        try:
            budget = _supersession_budget_seconds()
            async with memory_row_session(
                host.storage.db.conninfo,
                memory_id,
                timeout_seconds=budget,
            ) as connection:
                started = time.monotonic()
                sql_budget = budget / 2.0
                hidden = await _run_sql_slice(connection, sql_budget, load_hidden)
                if hidden is None:
                    return
                remaining = budget - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError
                await asyncio.wait_for(
                    _delete_artifacts(host, memory_id, hidden[0], hidden[1]),
                    timeout=remaining,
                )
        except TimeoutError:
            logger.warning(
                "Visibility-locked secondary purge failed for %s: %s",
                memory_id,
                "secondary I/O deadline expired",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Visibility-locked secondary purge failed for %s: %s",
                memory_id,
                exc,
            )
        return

    try:
        await _delete_artifacts(host, memory_id, project_id, is_global)
        await _stamp_recreated_row(host, memory_id)
    except Exception as exc:
        logger.warning("Secondary-store purge failed for %s: %s", memory_id, exc)


async def rebuild_crossrefs_for_memory(
    host: ReconciliationHost,
    memory: Memory,
    threshold: float | None = None,
    max_links: int | None = None,
) -> int:
    """Rebuild one memory's links on the shared active-snapshot fence."""
    async with memory_row_session(
        host.storage.db.conninfo,
        memory.id,
        timeout_seconds=_mutator_budget_seconds(),
    ) as connection:
        try:
            async with asyncio.timeout(SECONDARY_RECONCILIATION_IO_SECONDS):
                created = await host._crossref_service.rebuild_for_memory(
                    memory,
                    threshold,
                    max_links,
                    connection=connection,
                )
            await connection.commit()
            return int(created)
        except TimeoutError as exc:
            await connection.rollback()
            raise TimeoutError("secondary I/O deadline expired") from exc
