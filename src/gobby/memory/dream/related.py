"""Related-memory retrieval for dream review candidates.

Evidence is gathered per bounded work unit (at most 25 candidates): one bulk
keyword query, one stored-vector batch query, and at most one hydration query.
All three channels are required in every deployment mode; a channel that fails
every bounded attempt raises a typed dependency failure instead of silently
degrading candidate evidence.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from typing import Any, Literal, TypeVar

from psycopg.rows import dict_row
from qdrant_client.models import Filter

from gobby.memory.dream.models import DreamCandidate, RelatedMemoryEvidence
from gobby.memory.services.keyword import MemoryKeywordSearchService
from gobby.memory.vectorstore_filters import memory_scope_filter
from gobby.search.keyword import sanitize_pg_search_query
from gobby.storage.hub.async_ops import run_bounded_db
from gobby.storage.memories_crud import map_get_memories_rows, render_get_memories_statement
from gobby.storage.memories_scope import MemoryScope

logger = logging.getLogger(__name__)

RELATED_EVIDENCE_UNIT_SIZE = 25
# Defaults for the MemoryDreamConfig evidence knobs; config values win when set.
RELATED_EVIDENCE_CHANNEL_TIMEOUT_SECONDS = 30.0
RELATED_EVIDENCE_RETRY_ATTEMPTS = 3
RELATED_EVIDENCE_RETRY_BACKOFF_SECONDS = (1.0, 4.0)
RELATED_EVIDENCE_PHASE_TIMEOUT_SECONDS = 210.0
VECTOR_EVIDENCE_MIN_SCORE = 0.35
_RRF_K = 60
_DEFAULT_FETCH_LIMIT = 20
_DEFAULT_TOP_K = 5
_T = TypeVar("_T")

_TASK_REF_PATTERN = re.compile(r"#(\d{3,6})")
_STOPWORDS = frozenset(
    {
        "about",
        "after",
        "again",
        "also",
        "among",
        "and",
        "are",
        "because",
        "before",
        "being",
        "between",
        "both",
        "can",
        "could",
        "does",
        "each",
        "for",
        "from",
        "had",
        "has",
        "have",
        "into",
        "its",
        "may",
        "more",
        "most",
        "must",
        "other",
        "our",
        "out",
        "over",
        "same",
        "should",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "then",
        "there",
        "these",
        "they",
        "this",
        "through",
        "under",
        "uses",
        "was",
        "were",
        "when",
        "where",
        "which",
        "while",
        "with",
        "would",
        "your",
    }
)

RetrievalScopeKind = Literal["global_only", "project_only", "project_and_global"]
_Channel = Literal["keyword", "vector", "hydration"]


class RelatedEvidenceError(RuntimeError):
    """Required related-evidence retrieval could not be completed."""


class RelatedEvidenceChannelError(RelatedEvidenceError):
    """One required evidence channel failed every bounded attempt."""

    def __init__(self, channel: str, *, attempts: int, detail: str) -> None:
        super().__init__(
            f"related-evidence {channel} channel failed after {attempts} attempt(s): {detail}"
        )
        self.channel = channel
        self.attempts = attempts
        self.detail = detail


class RelatedEvidencePhaseTimeoutError(RelatedEvidenceError):
    """A work unit exceeded the overall evidence-phase budget."""

    def __init__(self, unit_index: int, *, timeout_seconds: float) -> None:
        super().__init__(
            f"related-evidence unit {unit_index} exceeded the "
            f"{timeout_seconds:.0f}s evidence-phase budget"
        )
        self.unit_index = unit_index
        self.timeout_seconds = timeout_seconds


@dataclass(frozen=True)
class RetrievalScope:
    """One explicit project/global evidence-retrieval scope."""

    kind: RetrievalScopeKind
    project_id: str | None = None

    def __post_init__(self) -> None:
        requires_project = self.kind in {"project_only", "project_and_global"}
        if requires_project and not self.project_id:
            raise ValueError(f"{self.kind} retrieval scope requires project_id")
        if not requires_project and self.project_id is not None:
            raise ValueError(f"{self.kind} retrieval scope rejects project_id")

    @classmethod
    def global_only(cls) -> RetrievalScope:
        return cls("global_only")

    @classmethod
    def project_only(cls, project_id: str) -> RetrievalScope:
        return cls("project_only", project_id)

    @classmethod
    def project_and_global(cls, project_id: str) -> RetrievalScope:
        return cls("project_and_global", project_id)

    def vector_filter(self) -> Filter:
        result = memory_scope_filter(self.memory_scope())
        if result is None:
            raise AssertionError("related-memory scope must always render a vector filter")
        return result

    def memory_scope(self) -> MemoryScope:
        if self.kind == "global_only":
            return MemoryScope.global_only()
        project_id = self.project_id
        if project_id is None:
            raise ValueError(f"{self.kind} retrieval scope requires project_id")
        if self.kind == "project_only":
            return MemoryScope.project_only(project_id)
        return MemoryScope.project_visible(project_id)

    def accepts_memory(self, project_id: str, is_global: bool) -> bool:
        if self.kind == "global_only":
            return is_global
        if self.kind == "project_only":
            return project_id == self.project_id and not is_global
        return project_id == self.project_id or is_global


class RelatedEvidenceSession:
    """Caller-owned child-task lifecycle for related-evidence retrieval."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._unit_index = 0
        self._closed = False

    async def __aenter__(self) -> RelatedEvidenceSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        await self.aclose()

    def next_unit_index(self) -> int:
        self._unit_index += 1
        return self._unit_index

    def create_task(self, awaitable: Awaitable[_T], *, name: str) -> asyncio.Task[_T]:
        if self._closed:
            raise RuntimeError("RelatedEvidenceSession is closed")

        async def await_value() -> _T:
            return await awaitable

        task = asyncio.create_task(await_value(), name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()


@dataclass
class _AttemptMetrics:
    """Timing captured inside one channel attempt."""

    admitted_at: float | None = None

    def mark_admitted(self) -> None:
        if self.admitted_at is None:
            self.admitted_at = asyncio.get_running_loop().time()

    def pool_wait(self, started: float) -> float | None:
        if self.admitted_at is None:
            return None
        return max(0.0, self.admitted_at - started)


@dataclass(frozen=True)
class _ChannelContext:
    session: RelatedEvidenceSession
    scope: RetrievalScope
    unit_index: int
    run_id: str | None
    channel_timeout_seconds: float
    retry_attempts: int


async def gather_related_evidence(
    candidates: list[DreamCandidate],
    *,
    db: Any,
    vector_store: Any | None,
    dream_config: Any,
    session: RelatedEvidenceSession,
    scope: RetrievalScope,
    temporal_direction: Literal["newer", "older"] = "newer",
    anchor_at: datetime | None = None,
    run_id: str | None = None,
) -> list[DreamCandidate]:
    """Populate related evidence in bounded work units; all channels required."""
    if not candidates:
        return []
    if temporal_direction not in {"newer", "older"}:
        logger.warning("Invalid related-memory temporal direction: %s", temporal_direction)
        return candidates
    if vector_store is None:
        raise RelatedEvidenceChannelError("vector", attempts=0, detail="no vector store configured")

    fetch_limit = _positive_config(
        dream_config, "related_evidence_fetch_limit", _DEFAULT_FETCH_LIMIT
    )
    top_k = _positive_config(dream_config, "related_evidence_top_k", _DEFAULT_TOP_K)
    channel_timeout_seconds = _positive_float_config(
        dream_config,
        "evidence_channel_timeout_seconds",
        RELATED_EVIDENCE_CHANNEL_TIMEOUT_SECONDS,
    )
    retry_attempts = _positive_config(
        dream_config, "evidence_retry_attempts", RELATED_EVIDENCE_RETRY_ATTEMPTS
    )
    phase_timeout_seconds = _positive_float_config(
        dream_config,
        "evidence_phase_timeout_seconds",
        RELATED_EVIDENCE_PHASE_TIMEOUT_SECONDS,
    )

    enriched: list[DreamCandidate] = []
    for start in range(0, len(candidates), RELATED_EVIDENCE_UNIT_SIZE):
        unit = candidates[start : start + RELATED_EVIDENCE_UNIT_SIZE]
        enriched.extend(
            await _gather_unit(
                unit,
                db=db,
                vector_store=vector_store,
                session=session,
                scope=scope,
                temporal_direction=temporal_direction,
                anchor_at=anchor_at,
                fetch_limit=fetch_limit,
                top_k=top_k,
                unit_index=session.next_unit_index(),
                run_id=run_id,
                channel_timeout_seconds=channel_timeout_seconds,
                retry_attempts=retry_attempts,
                phase_timeout_seconds=phase_timeout_seconds,
            )
        )
    return enriched


async def _gather_unit(
    unit: list[DreamCandidate],
    *,
    db: Any,
    vector_store: Any,
    session: RelatedEvidenceSession,
    scope: RetrievalScope,
    temporal_direction: Literal["newer", "older"],
    anchor_at: datetime | None,
    fetch_limit: int,
    top_k: int,
    unit_index: int,
    run_id: str | None,
    channel_timeout_seconds: float = RELATED_EVIDENCE_CHANNEL_TIMEOUT_SECONDS,
    retry_attempts: int = RELATED_EVIDENCE_RETRY_ATTEMPTS,
    phase_timeout_seconds: float = RELATED_EVIDENCE_PHASE_TIMEOUT_SECONDS,
) -> list[DreamCandidate]:
    context = _ChannelContext(
        session=session,
        scope=scope,
        unit_index=unit_index,
        run_id=run_id,
        channel_timeout_seconds=channel_timeout_seconds,
        retry_attempts=retry_attempts,
    )
    unit_tasks: list[asyncio.Task[Any]] = []
    try:
        async with asyncio.timeout(phase_timeout_seconds):
            keyword_task = session.create_task(
                _run_channel(
                    "keyword",
                    partial(
                        _keyword_hits_bulk,
                        unit,
                        db=db,
                        scope=scope,
                        fetch_limit=fetch_limit,
                        deadline_seconds=channel_timeout_seconds,
                    ),
                    context=context,
                ),
                name=f"dream-related-keyword-unit-{unit_index}",
            )
            vector_task = session.create_task(
                _run_channel(
                    "vector",
                    partial(
                        _vector_hits,
                        unit,
                        vector_store=vector_store,
                        scope=scope,
                        fetch_limit=fetch_limit,
                        deadline_seconds=channel_timeout_seconds,
                    ),
                    context=context,
                ),
                name=f"dream-related-vector-unit-{unit_index}",
            )
            unit_tasks = [keyword_task, vector_task]
            keyword_hits, vector_hits = await asyncio.gather(keyword_task, vector_task)

            ranked = {
                candidate.id: _rrf_rank(
                    candidate.id,
                    keyword_hits.get(candidate.id, []),
                    vector_hits.get(candidate.id, []),
                )
                for candidate in unit
            }
            hydration_ids = list(
                dict.fromkeys(item for values in ranked.values() for item in values)
            )
            hydrated_rows: list[Any] = []
            if hydration_ids:
                hydration_task = session.create_task(
                    _run_channel(
                        "hydration",
                        partial(
                            _hydrate_hits,
                            hydration_ids,
                            db=db,
                            scope=scope,
                            deadline_seconds=channel_timeout_seconds,
                        ),
                        context=context,
                    ),
                    name=f"dream-related-hydration-unit-{unit_index}",
                )
                unit_tasks.append(hydration_task)
                hydrated_rows = await hydration_task
    except TimeoutError as exc:
        await _drain_tasks(unit_tasks)
        raise RelatedEvidencePhaseTimeoutError(
            unit_index, timeout_seconds=phase_timeout_seconds
        ) from exc
    except BaseException:
        await _drain_tasks(unit_tasks)
        raise

    hydrated = {memory.id: memory for memory in hydrated_rows}
    return [
        _attach_candidate_evidence(
            candidate,
            ranked.get(candidate.id, []),
            hydrated,
            keyword_ids={item_id for item_id, _score in keyword_hits.get(candidate.id, [])},
            vector_ids={item_id for item_id, _score in vector_hits.get(candidate.id, [])},
            top_k=top_k,
            temporal_direction=temporal_direction,
            anchor_at=anchor_at,
            scope=scope,
        )
        for candidate in unit
    ]


async def _run_channel[T](
    channel: _Channel,
    operation: Callable[[_AttemptMetrics], Awaitable[T]],
    *,
    context: _ChannelContext,
) -> T:
    loop = asyncio.get_running_loop()
    detail = "unknown failure"
    last_error: Exception | None = None
    for attempt in range(1, context.retry_attempts + 1):
        metrics = _AttemptMetrics()
        started = loop.time()
        task = context.session.create_task(
            operation(metrics),
            name=f"dream-related-{channel}-unit-{context.unit_index}-attempt-{attempt}",
        )
        outcome = "error"
        try:
            async with asyncio.timeout(context.channel_timeout_seconds):
                return await task
        except TimeoutError as exc:
            last_error = exc
            task.cancel()
            with suppress(asyncio.CancelledError, TimeoutError):
                await task
            # Budget expiry cancels the attempt task; a task that instead
            # completed with its own TimeoutError is a failed attempt.
            if task.cancelled():
                outcome = "timeout"
                detail = f"attempt exceeded {context.channel_timeout_seconds:.0f}s"
            else:
                detail = f"{type(exc).__name__}: {exc}"
        except asyncio.CancelledError:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise
        except Exception as exc:
            last_error = exc
            detail = f"{type(exc).__name__}: {exc}"
        _log_attempt_failure(
            channel,
            context=context,
            attempt=attempt,
            pool_wait=metrics.pool_wait(started),
            duration=loop.time() - started,
            outcome=outcome,
        )
        if attempt < context.retry_attempts:
            backoffs = RELATED_EVIDENCE_RETRY_BACKOFF_SECONDS
            await asyncio.sleep(backoffs[min(attempt - 1, len(backoffs) - 1)])
    error = RelatedEvidenceChannelError(channel, attempts=context.retry_attempts, detail=detail)
    if last_error is None:
        raise error
    raise error from last_error


async def _drain_tasks(tasks: list[asyncio.Task[Any]]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _log_attempt_failure(
    channel: _Channel,
    *,
    context: _ChannelContext,
    attempt: int,
    pool_wait: float | None,
    duration: float,
    outcome: str,
) -> None:
    logger.info(
        "Related-evidence attempt failed: run=%s scope=%s unit=%s channel=%s "
        "attempt=%s/%s pool_wait=%s duration=%.3fs outcome=%s",
        context.run_id or "-",
        context.scope.kind,
        context.unit_index,
        channel,
        attempt,
        context.retry_attempts,
        f"{pool_wait:.3f}s" if pool_wait is not None else "n/a",
        duration,
        outcome,
    )


def render_bulk_keyword_statement(
    queries: list[tuple[str, str]],
    *,
    db: Any,
    scope: RetrievalScope,
    fetch_limit: int,
) -> tuple[str, tuple[Any, ...]] | None:
    """Combine per-candidate rendered keyword statements into one round trip.

    Each branch keeps its own BM25 ranking via a per-branch ``ROW_NUMBER``
    ordinal, so consumers can restore the exact per-candidate hit order
    regardless of how the executor interleaves ``UNION ALL`` output.
    """
    if len(queries) > RELATED_EVIDENCE_UNIT_SIZE:
        raise ValueError(
            f"bulk keyword search accepts at most {RELATED_EVIDENCE_UNIT_SIZE} queries"
        )
    service = MemoryKeywordSearchService(db)
    parts: list[str] = []
    params: list[Any] = []
    for candidate_id, query in queries:
        if not query:
            continue
        statement = service.render_search(
            query,
            fetch_limit,
            project_id=scope.project_id,
            scope=scope.kind,
        )
        if statement is None:
            continue
        sql, sql_params = statement
        parts.append(
            "SELECT %s AS candidate_key, ranked.id AS id, ranked.score AS score, "
            f"ROW_NUMBER() OVER () AS hit_rank FROM ({sql}) AS ranked"
        )
        params.append(candidate_id)
        params.extend(sql_params)
    if not parts:
        return None
    return "\nUNION ALL\n".join(parts), tuple(params)


async def _keyword_hits_bulk(
    unit: list[DreamCandidate],
    metrics: _AttemptMetrics | None = None,
    *,
    db: Any,
    scope: RetrievalScope,
    fetch_limit: int,
    deadline_seconds: float = RELATED_EVIDENCE_CHANNEL_TIMEOUT_SECONDS,
) -> dict[str, list[tuple[str, float]]]:
    stats = metrics or _AttemptMetrics()
    statement = render_bulk_keyword_statement(
        [(candidate.id, _distinctive_terms(candidate.content)) for candidate in unit],
        db=db,
        scope=scope,
        fetch_limit=fetch_limit,
    )
    if statement is None:
        stats.mark_admitted()
        return {}
    sql, params = statement

    async def execute(connection: Any, _remaining: float) -> list[Any]:
        stats.mark_admitted()
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(sql, params)
            return list(await cursor.fetchall())

    rows = await run_bounded_db(
        execute,
        conninfo=db.conninfo,
        deadline_seconds=deadline_seconds,
    )
    grouped: dict[str, list[tuple[int, str, float]]] = {}
    for row in rows:
        grouped.setdefault(str(row["candidate_key"]), []).append(
            (int(row["hit_rank"]), str(row["id"]), float(row["score"]))
        )
    return {
        candidate_id: [(memory_id, score) for _rank, memory_id, score in sorted(values)]
        for candidate_id, values in grouped.items()
    }


async def _vector_hits(
    unit: list[DreamCandidate],
    metrics: _AttemptMetrics | None = None,
    *,
    vector_store: Any,
    scope: RetrievalScope,
    fetch_limit: int,
    deadline_seconds: float = RELATED_EVIDENCE_CHANNEL_TIMEOUT_SECONDS,
) -> dict[str, list[tuple[str, float]]]:
    (metrics or _AttemptMetrics()).mark_admitted()
    results = await vector_store.search_by_stored_vectors(
        [candidate.id for candidate in unit],
        limit=fetch_limit,
        query_filter=scope.vector_filter(),
        timeout=deadline_seconds,
    )
    return {
        memory_id: [
            (hit_id, score) for hit_id, score in hits if float(score) >= VECTOR_EVIDENCE_MIN_SCORE
        ]
        for memory_id, hits in results.items()
    }


async def _hydrate_hits(
    memory_ids: list[str],
    metrics: _AttemptMetrics | None = None,
    *,
    db: Any,
    scope: RetrievalScope,
    deadline_seconds: float = RELATED_EVIDENCE_CHANNEL_TIMEOUT_SECONDS,
) -> list[Any]:
    stats = metrics or _AttemptMetrics()
    statement = render_get_memories_statement(
        memory_ids,
        scope.memory_scope(),
        visibility="active",
    )
    if statement is None:
        stats.mark_admitted()
        return []
    sql, params = statement

    async def execute(connection: Any, _remaining: float) -> list[Any]:
        stats.mark_admitted()
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(sql, params)
            return list(await cursor.fetchall())

    rows = await run_bounded_db(
        execute,
        conninfo=db.conninfo,
        deadline_seconds=deadline_seconds,
    )
    return map_get_memories_rows(rows, memory_ids)


def _rrf_rank(
    candidate_id: str,
    keyword_hits: list[tuple[str, float]],
    vector_hits: list[tuple[str, float]],
) -> list[str]:
    scores: dict[str, float] = {}
    for hits in (keyword_hits, vector_hits):
        for rank, (memory_id, _score) in enumerate(hits, start=1):
            if memory_id == candidate_id:
                continue
            scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (_RRF_K + rank)
    return sorted(scores, key=lambda memory_id: (-scores[memory_id], memory_id))


def _attach_candidate_evidence(
    candidate: DreamCandidate,
    ranked_ids: list[str],
    hydrated: dict[str, Any],
    *,
    keyword_ids: set[str],
    vector_ids: set[str],
    top_k: int,
    temporal_direction: Literal["newer", "older"],
    anchor_at: datetime | None,
    scope: RetrievalScope,
) -> DreamCandidate:
    anchor = anchor_at or candidate.created_at
    related: list[RelatedMemoryEvidence] = []
    for memory_id in ranked_ids:
        memory = hydrated.get(memory_id)
        if memory is None or memory.id == candidate.id:
            continue
        project_id = getattr(memory, "project_id", "")
        is_global = bool(getattr(memory, "is_global", False))
        if not scope.accepts_memory(project_id, is_global):
            continue
        created_at = memory.created_at
        if temporal_direction == "newer" and created_at <= anchor:
            continue
        if temporal_direction == "older" and created_at >= anchor:
            continue
        via_keyword = memory_id in keyword_ids
        via_vector = memory_id in vector_ids
        matched_via = (
            "keyword+vector"
            if via_keyword and via_vector
            else ("keyword" if via_keyword else "vector")
        )
        related.append(
            RelatedMemoryEvidence(
                id=memory.id,
                memory_type=memory.memory_type,
                created_at=created_at,
                newer_by_days=(created_at - anchor).total_seconds() / 86_400,
                content=memory.content,
                matched_via=matched_via,
            )
        )
        if len(related) >= top_k:
            break
    return replace(candidate, related=tuple(related))


def _positive_config(config: Any, name: str, default: int) -> int:
    value = getattr(config, name, default)
    return value if isinstance(value, int) and value > 0 else default


def _positive_float_config(config: Any, name: str, default: float) -> float:
    value = getattr(config, name, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return float(value) if value > 0 else default


def _distinctive_terms(content: str, max_terms: int = 24) -> str:
    """Return deterministic, high-signal search terms from memory content."""
    if max_terms <= 0:
        return ""

    tokens = sanitize_pg_search_query(content).split()
    task_refs = _deduplicate(_TASK_REF_PATTERN.findall(content))
    task_ref_set = set(task_refs)
    eligible = [
        token
        for token in tokens
        if len(token) >= 3 and token.lower() not in _STOPWORDS and token not in task_ref_set
    ]
    identifiers = [token for token in eligible if _is_identifier_like(token)]
    remaining = [token for token in eligible if not _is_identifier_like(token)]
    return " ".join(_deduplicate([*task_refs, *identifiers, *remaining])[:max_terms])


def _is_identifier_like(token: str) -> bool:
    return (
        "_" in token
        or (any(char.isalpha() for char in token) and any(char.isdigit() for char in token))
        or (token.isupper() and any(char.isalpha() for char in token))
    )


def _deduplicate(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
