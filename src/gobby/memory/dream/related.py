"""Related-memory retrieval for dream review candidates."""

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
from gobby.memory.vectorstore_client import VectorStoreUnavailableError
from gobby.memory.vectorstore_filters import memory_scope_filter
from gobby.search.keyword import sanitize_pg_search_query
from gobby.storage.hub.async_ops import run_bounded_db
from gobby.storage.memories_crud import map_get_memories_rows, render_get_memories_statement
from gobby.storage.memories_scope import MemoryScope

logger = logging.getLogger(__name__)

RELATED_EVIDENCE_PAGE_DEADLINE_SECONDS = 15.0
RELATED_EVIDENCE_CALL_TIMEOUT_SECONDS = 5.0
RELATED_EVIDENCE_CHANNEL_TRIP_LIMIT = 2
RELATED_EVIDENCE_DRAIN_TIMEOUT_SECONDS = 7.0
VECTOR_EVIDENCE_MIN_SCORE = 0.35
_RRF_K = 60
_DEFAULT_FETCH_LIMIT = 20
_DEFAULT_TOP_K = 5
_LOCAL_VECTOR_WARNING_EMITTED = False
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
_DB_BACKED_CHANNELS: frozenset[_Channel] = frozenset({"keyword", "hydration"})


@dataclass(frozen=True)
class _CallOutcome:
    value: Any = None
    timed_out: bool = False
    failed: bool = False


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
    """Sweep-owned concurrency, circuit-breaker, and task-lifecycle state."""

    def __init__(self) -> None:
        self._db_semaphore = asyncio.Semaphore(4)
        self._timeout_counts: dict[_Channel, int] = {
            "keyword": 0,
            "vector": 0,
            "hydration": 0,
        }
        self._tasks: set[asyncio.Task[Any]] = set()
        self._page_index = 0
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

    def next_page_index(self) -> int:
        self._page_index += 1
        return self._page_index

    def channel_tripped(self, channel: _Channel) -> bool:
        return self._timeout_counts[channel] >= RELATED_EVIDENCE_CHANNEL_TRIP_LIMIT

    def record_channel_page(self, channel: _Channel, *, timed_out: bool) -> None:
        if self.channel_tripped(channel) and not timed_out:
            return
        self._timeout_counts[channel] = self._timeout_counts[channel] + 1 if timed_out else 0

    def create_task(self, awaitable: Awaitable[_T], *, name: str) -> asyncio.Task[_T]:
        if self._closed:
            raise RuntimeError("RelatedEvidenceSession is closed")

        async def await_value() -> _T:
            return await awaitable

        task = asyncio.create_task(await_value(), name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def run_call(
        self,
        channel: _Channel,
        operation: Callable[[], Awaitable[_T]],
        *,
        page_index: int,
    ) -> _CallOutcome:
        if self.channel_tripped(channel):
            return _CallOutcome()

        task: asyncio.Task[_T] | None = None

        async def execute() -> _CallOutcome:
            nonlocal task
            async with asyncio.timeout(RELATED_EVIDENCE_CALL_TIMEOUT_SECONDS):
                task = self.create_task(
                    operation(),
                    name=f"dream-related-{channel}-page-{page_index}",
                )
                return _CallOutcome(value=await task)

        try:
            if channel in _DB_BACKED_CHANNELS:
                async with self._db_semaphore:
                    return await execute()
            return await execute()
        except TimeoutError:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            return _CallOutcome(timed_out=True)
        except VectorStoreUnavailableError:
            return _CallOutcome(failed=True)
        except Exception:
            logger.debug(
                "Related-memory %s channel failed on page %s",
                channel,
                page_index,
                exc_info=True,
            )
            return _CallOutcome(failed=True)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                async with asyncio.timeout(RELATED_EVIDENCE_DRAIN_TIMEOUT_SECONDS):
                    await asyncio.gather(*tasks, return_exceptions=True)
            except TimeoutError:
                logger.warning(
                    "Related-memory session drain exceeded %.1fs",
                    RELATED_EVIDENCE_DRAIN_TIMEOUT_SECONDS,
                )
        self._tasks.clear()


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
) -> list[DreamCandidate]:
    """Populate related evidence within bounded best-effort channel deadlines."""
    if not candidates:
        return []
    if temporal_direction not in {"newer", "older"}:
        logger.warning("Invalid related-memory temporal direction: %s", temporal_direction)
        return candidates

    page_index = session.next_page_index()
    started = asyncio.get_running_loop().time()
    try:
        async with asyncio.timeout(RELATED_EVIDENCE_PAGE_DEADLINE_SECONDS):
            return await _gather_page(
                candidates,
                db=db,
                vector_store=vector_store,
                dream_config=dream_config,
                session=session,
                scope=scope,
                temporal_direction=temporal_direction,
                anchor_at=anchor_at,
                page_index=page_index,
                started=started,
            )
    except TimeoutError:
        elapsed = asyncio.get_running_loop().time() - started
        logger.warning(
            "Related-memory page %s exceeded %.2fs (channels=page)",
            page_index,
            elapsed,
        )
        return candidates
    except Exception:
        logger.warning("Related-memory retrieval failed on page %s", page_index, exc_info=True)
        return candidates


async def _gather_page(
    candidates: list[DreamCandidate],
    *,
    db: Any,
    vector_store: Any | None,
    dream_config: Any,
    session: RelatedEvidenceSession,
    scope: RetrievalScope,
    temporal_direction: Literal["newer", "older"],
    anchor_at: datetime | None,
    page_index: int,
    started: float,
) -> list[DreamCandidate]:
    fetch_limit = _positive_config(
        dream_config, "related_evidence_fetch_limit", _DEFAULT_FETCH_LIMIT
    )
    top_k = _positive_config(dream_config, "related_evidence_top_k", _DEFAULT_TOP_K)

    keyword_tasks = [
        session.create_task(
            session.run_call(
                "keyword",
                partial(
                    _keyword_hits,
                    candidate,
                    db=db,
                    scope=scope,
                    fetch_limit=fetch_limit,
                ),
                page_index=page_index,
            ),
            name=f"dream-related-keyword-dispatch-{page_index}-{candidate.id}",
        )
        for candidate in candidates
    ]
    vector_task = session.create_task(
        session.run_call(
            "vector",
            lambda: _vector_hits(
                candidates,
                vector_store=vector_store,
                scope=scope,
                fetch_limit=fetch_limit,
            ),
            page_index=page_index,
        ),
        name=f"dream-related-vector-dispatch-{page_index}",
    )
    keyword_outcomes = await asyncio.gather(*keyword_tasks)
    vector_outcome = await vector_task

    keyword_timed_out = any(outcome.timed_out for outcome in keyword_outcomes)
    session.record_channel_page("keyword", timed_out=keyword_timed_out)
    session.record_channel_page("vector", timed_out=vector_outcome.timed_out)
    timed_out_channels = [
        channel
        for channel, timed_out in (
            ("keyword", keyword_timed_out),
            ("vector", vector_outcome.timed_out),
        )
        if timed_out
    ]
    if timed_out_channels:
        _log_page_timeout(page_index, started, timed_out_channels)
        return candidates

    keyword_hits = {
        candidate.id: outcome.value or []
        for candidate, outcome in zip(candidates, keyword_outcomes, strict=True)
    }
    vector_hits = vector_outcome.value or {}
    ranked = {
        candidate.id: _rrf_rank(
            candidate.id,
            keyword_hits.get(candidate.id, []),
            vector_hits.get(candidate.id, []),
        )
        for candidate in candidates
    }
    hydration_ids = list(dict.fromkeys(item for values in ranked.values() for item in values))
    if not hydration_ids:
        return candidates

    hydration_outcome = await session.run_call(
        "hydration",
        lambda: _hydrate_hits(hydration_ids, db=db, scope=scope),
        page_index=page_index,
    )
    session.record_channel_page("hydration", timed_out=hydration_outcome.timed_out)
    if hydration_outcome.timed_out:
        _log_page_timeout(page_index, started, ["hydration"])
        return candidates
    if hydration_outcome.failed:
        return candidates

    hydrated = {memory.id: memory for memory in hydration_outcome.value or []}
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
        for candidate in candidates
    ]


async def _keyword_hits(
    candidate: DreamCandidate,
    *,
    db: Any,
    scope: RetrievalScope,
    fetch_limit: int,
) -> list[tuple[str, float]]:
    query = _distinctive_terms(candidate.content)
    if not query:
        return []
    service = MemoryKeywordSearchService(db)
    statement = service.render_search(
        query,
        fetch_limit,
        project_id=scope.project_id,
        scope=scope.kind,
    )
    if statement is None:
        return []
    sql, params = statement

    async def execute(connection: Any, _remaining: float) -> list[Any]:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(sql, params)
            return list(await cursor.fetchall())

    rows = await run_bounded_db(
        execute,
        conninfo=db.conninfo,
        deadline_seconds=RELATED_EVIDENCE_CALL_TIMEOUT_SECONDS,
    )
    return [(hit.id, hit.score) for hit in service.map_rows(rows)]


async def _vector_hits(
    candidates: list[DreamCandidate],
    *,
    vector_store: Any | None,
    scope: RetrievalScope,
    fetch_limit: int,
) -> dict[str, list[tuple[str, float]]]:
    global _LOCAL_VECTOR_WARNING_EMITTED

    if vector_store is None:
        return {}
    if not bool(getattr(vector_store, "supports_stored_vector_search", True)):
        if not _LOCAL_VECTOR_WARNING_EMITTED:
            logger.warning("Stored-vector related evidence is disabled for local Qdrant mode")
            _LOCAL_VECTOR_WARNING_EMITTED = True
        return {}
    results = await vector_store.search_by_stored_vectors(
        [candidate.id for candidate in candidates],
        limit=fetch_limit,
        query_filter=scope.vector_filter(),
        timeout=RELATED_EVIDENCE_CALL_TIMEOUT_SECONDS,
    )
    return {
        memory_id: [
            (hit_id, score) for hit_id, score in hits if float(score) >= VECTOR_EVIDENCE_MIN_SCORE
        ]
        for memory_id, hits in results.items()
    }


async def _hydrate_hits(
    memory_ids: list[str],
    *,
    db: Any,
    scope: RetrievalScope,
) -> list[Any]:
    statement = render_get_memories_statement(
        memory_ids,
        scope.memory_scope(),
        visibility="active",
    )
    if statement is None:
        return []
    sql, params = statement

    async def execute(connection: Any, _remaining: float) -> list[Any]:
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(sql, params)
            return list(await cursor.fetchall())

    rows = await run_bounded_db(
        execute,
        conninfo=db.conninfo,
        deadline_seconds=RELATED_EVIDENCE_CALL_TIMEOUT_SECONDS,
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


def _log_page_timeout(page_index: int, started: float, channels: list[str]) -> None:
    elapsed = asyncio.get_running_loop().time() - started
    logger.warning(
        "Related-memory page %s timed out after %.2fs (channels=%s)",
        page_index,
        elapsed,
        ",".join(channels),
    )


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
