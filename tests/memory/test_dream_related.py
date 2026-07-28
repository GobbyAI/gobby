"""Tests for bounded related-memory evidence retrieval."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.memory.dream import related as related_module
from gobby.memory.dream.models import DreamCandidate
from gobby.memory.dream.options import DreamRunOptions
from gobby.memory.dream.related import (
    VECTOR_EVIDENCE_MIN_SCORE,
    RelatedEvidenceSession,
    RetrievalScope,
    _attach_candidate_evidence,
    _distinctive_terms,
    _hydrate_hits,
    _keyword_hits,
    _vector_hits,
    gather_related_evidence,
)
from gobby.memory.services.keyword import MemoryKeywordSearchService
from gobby.memory.vectorstore import VectorStore
from gobby.storage.memories import LocalMemoryManager
from gobby.storage.memories_crud import render_get_memories_statement
from gobby.storage.memories_models import Memory, MemoryType

_NOW = datetime(2026, 7, 22, tzinfo=UTC)
_PROJECT_A = "11111111-1111-4111-8111-111111111111"
_PROJECT_B = "22222222-2222-4222-8222-222222222222"


def _candidate(
    memory_id: str = "candidate",
    *,
    created_at: datetime = _NOW,
    content: str = "sandwich TEST IMPL REF expansion #15008 execute_expansion",
) -> DreamCandidate:
    return DreamCandidate(
        id=memory_id,
        content=content,
        memory_type="fact",
        project_id="project-a",
        is_global=False,
        source_type="agent",
        source_session_id=None,
        tags=[],
        age_days=30.0,
        access_count=0,
        created_at=created_at,
        updated_at=created_at,
        last_accessed_at=None,
    )


def _memory(
    memory_id: str,
    *,
    created_at: datetime,
    project_id: str = "project-a",
    is_global: bool = False,
    content: str | None = None,
) -> Memory:
    return Memory(
        id=memory_id,
        memory_type=MemoryType.FACT,
        content=content or f"content for {memory_id}",
        created_at=created_at,
        updated_at=created_at,
        project_id=project_id,
        is_global=is_global,
    )


@pytest.mark.unit
def test_distinctive_terms_priorities() -> None:
    content = (
        "The sandwich TEST IMPL REF expansion for #15008 uses execute_expansion "
        "and ABC123 while the sandwich repeats"
    )

    terms = _distinctive_terms(content, max_terms=7).split()

    assert terms == [
        "15008",
        "TEST",
        "IMPL",
        "REF",
        "execute_expansion",
        "ABC123",
        "sandwich",
    ]


@pytest.mark.parametrize(
    ("options", "include_global", "expected"),
    [
        (DreamRunOptions(global_only=True), False, RetrievalScope.global_only()),
        (
            DreamRunOptions(project_id="project-a"),
            True,
            RetrievalScope.project_and_global("project-a"),
        ),
        (
            DreamRunOptions(project_id="project-a"),
            False,
            RetrievalScope.project_only("project-a"),
        ),
        (DreamRunOptions(), True, None),
    ],
)
@pytest.mark.unit
def test_dream_run_options_retrieval_scope(
    options: DreamRunOptions,
    include_global: bool,
    expected: RetrievalScope | None,
) -> None:
    assert options.retrieval_scope(include_global=include_global) == expected


@pytest.mark.unit
async def test_regression_sandwich_pair_attaches_via_keyword() -> None:
    candidate = _candidate()
    older = _memory("older", created_at=_NOW - timedelta(days=2))
    newer = _memory(
        "decision",
        created_at=_NOW + timedelta(days=2),
        content="The #15008 sandwich decision uses execute_expansion",
    )
    session = RelatedEvidenceSession()
    vector_store = MagicMock()
    vector_store.supports_stored_vector_search = True
    vector_store.search_by_stored_vectors = AsyncMock(
        return_value={"candidate": [("decision", 0.169)]}
    )
    with (
        patch(
            "gobby.memory.dream.related._keyword_hits",
            AsyncMock(return_value=[("candidate", 1.0), ("older", 0.95), ("decision", 0.9)]),
        ),
        patch(
            "gobby.memory.dream.related._hydrate_hits",
            AsyncMock(return_value=[older, newer]),
        ),
    ):
        result = await gather_related_evidence(
            [candidate],
            db=MagicMock(),
            vector_store=vector_store,
            dream_config=SimpleNamespace(related_evidence_top_k=1),
            session=session,
            scope=RetrievalScope.project_only("project-a"),
        )
    await session.aclose()

    assert [evidence.id for evidence in result[0].related] == ["decision"]
    assert result[0].related[0].matched_via == "keyword"
    assert result[0].related[0].content == newer.content


@pytest.mark.unit
async def test_total_retrieval_failure_returns_original() -> None:
    candidate = _candidate()
    session = RelatedEvidenceSession()
    keyword = AsyncMock(side_effect=RuntimeError("postgres unavailable"))
    vector = AsyncMock(side_effect=RuntimeError("qdrant unavailable"))
    with (
        patch("gobby.memory.dream.related._keyword_hits", keyword),
        patch("gobby.memory.dream.related._vector_hits", vector),
    ):
        result = await gather_related_evidence(
            [candidate],
            db=MagicMock(),
            vector_store=MagicMock(),
            dream_config=SimpleNamespace(),
            session=session,
            scope=RetrievalScope.project_only("project-a"),
        )
    await session.aclose()

    assert result == [candidate]
    keyword.assert_awaited_once()
    vector.assert_awaited_once()
    assert not session._tasks


@pytest.mark.unit
async def test_vector_unavailable_degrades() -> None:
    candidate = _candidate()
    newer = _memory("keyword-hit", created_at=_NOW + timedelta(days=1))
    session = RelatedEvidenceSession()
    with (
        patch(
            "gobby.memory.dream.related._keyword_hits",
            AsyncMock(return_value=[("keyword-hit", 0.8)]),
        ),
        patch(
            "gobby.memory.dream.related._vector_hits",
            AsyncMock(side_effect=RuntimeError("qdrant unavailable")),
        ),
        patch(
            "gobby.memory.dream.related._hydrate_hits",
            AsyncMock(return_value=[newer]),
        ),
    ):
        result = await gather_related_evidence(
            [candidate],
            db=MagicMock(),
            vector_store=MagicMock(),
            dream_config=SimpleNamespace(),
            session=session,
            scope=RetrievalScope.project_only("project-a"),
        )
    await session.aclose()

    assert result[0].related[0].id == "keyword-hit"
    assert result[0].related[0].matched_via == "keyword"


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        (RetrievalScope.project_only("project-a"), ["project-hit"]),
        (
            RetrievalScope.project_and_global("project-a"),
            ["project-hit", "global-hit"],
        ),
        (RetrievalScope.global_only(), ["global-hit"]),
    ],
)
@pytest.mark.unit
async def test_scope_isolation_matrix(scope: RetrievalScope, expected: list[str]) -> None:
    candidate = _candidate()
    hydrated = [
        _memory("project-hit", created_at=_NOW + timedelta(days=1)),
        _memory(
            "global-hit",
            created_at=_NOW + timedelta(days=2),
            project_id="global-owner",
            is_global=True,
        ),
        _memory(
            "wrong-project",
            created_at=_NOW + timedelta(days=3),
            project_id="project-b",
        ),
    ]
    session = RelatedEvidenceSession()
    with (
        patch(
            "gobby.memory.dream.related._keyword_hits",
            AsyncMock(
                return_value=[
                    ("project-hit", 1.0),
                    ("global-hit", 0.9),
                    ("wrong-project", 0.8),
                ]
            ),
        ),
        patch(
            "gobby.memory.dream.related._vector_hits",
            AsyncMock(
                return_value={
                    "candidate": [
                        ("project-hit", 0.8),
                        ("global-hit", 0.7),
                        ("wrong-project", 0.6),
                    ]
                }
            ),
        ),
        patch(
            "gobby.memory.dream.related._hydrate_hits",
            AsyncMock(return_value=hydrated),
        ),
    ):
        result = await gather_related_evidence(
            [candidate],
            db=MagicMock(),
            vector_store=MagicMock(),
            dream_config=SimpleNamespace(related_evidence_top_k=5),
            session=session,
            scope=scope,
        )
    await session.aclose()

    assert [evidence.id for evidence in result[0].related] == expected
    vector_filter = scope.vector_filter().model_dump(exclude_none=True)
    rendered = repr(vector_filter)
    if scope.kind in {"global_only", "project_and_global"}:
        assert "is_global" in rendered
        assert "value': True" in rendered
    if scope.kind == "project_only":
        assert "value': False" in rendered


@pytest.mark.unit
async def test_vector_floor_boundaries() -> None:
    store = MagicMock()
    store.supports_stored_vector_search = True
    store.search_by_stored_vectors = AsyncMock(
        return_value={
            "candidate": [
                ("below", VECTOR_EVIDENCE_MIN_SCORE - 0.05),
                ("above", VECTOR_EVIDENCE_MIN_SCORE + 0.05),
                ("regression", 0.169),
            ]
        }
    )

    result = await _vector_hits(
        [_candidate()],
        vector_store=store,
        scope=RetrievalScope.project_only("project-a"),
        fetch_limit=10,
    )

    assert result == {"candidate": [("above", VECTOR_EVIDENCE_MIN_SCORE + 0.05)]}


@pytest.mark.parametrize(
    ("direction", "anchor", "expected"),
    [
        ("newer", None, ["newer"]),
        ("older", None, ["older"]),
        ("newer", _NOW + timedelta(days=2), []),
        ("older", _NOW - timedelta(days=2), []),
    ],
)
@pytest.mark.unit
def test_temporal_direction(
    direction: str,
    anchor: datetime | None,
    expected: list[str],
) -> None:
    candidate = _candidate()
    memories = [
        _memory("newer", created_at=_NOW + timedelta(days=1)),
        _memory("older", created_at=_NOW - timedelta(days=1)),
    ]
    result = _attach_candidate_evidence(
        candidate,
        [memory.id for memory in memories],
        {memory.id: memory for memory in memories},
        keyword_ids={memory.id for memory in memories},
        vector_ids=set(),
        top_k=5,
        temporal_direction=direction,  # type: ignore[arg-type]
        anchor_at=anchor,
        scope=RetrievalScope.project_only("project-a"),
    )

    assert [evidence.id for evidence in result.related] == expected


@pytest.mark.parametrize(
    ("scope", "predicate"),
    [
        (RetrievalScope.global_only(), "memories.is_global IS TRUE"),
        (
            RetrievalScope.project_only(_PROJECT_A),
            "memories.project_id = %s AND memories.is_global IS FALSE",
        ),
        (
            RetrievalScope.project_and_global(_PROJECT_A),
            "(memories.project_id = %s OR memories.is_global IS TRUE)",
        ),
    ],
)
@pytest.mark.integration
def test_keyword_scope_sql_contract(
    scope: RetrievalScope,
    predicate: str,
    postgres_db: Any,
) -> None:
    postgres_db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s), (%s, %s)",
        (_PROJECT_A, "Related Project A", _PROJECT_B, "Related Project B"),
    )
    storage = LocalMemoryManager(postgres_db)
    project_memory = storage.create_memory(
        "rare sandwich terms project",
        project_id=_PROJECT_A,
    )
    global_memory = storage.create_memory(
        "rare sandwich terms global",
        project_id=_PROJECT_B,
        is_global=True,
    )
    storage.create_memory(
        "rare sandwich terms foreign",
        project_id=_PROJECT_B,
    )
    service = MemoryKeywordSearchService(postgres_db)

    statement = service.render_search(
        "rare sandwich terms",
        10,
        project_id=scope.project_id,
        scope=scope.kind,
    )

    assert statement is not None
    sql, params = statement
    assert predicate in sql
    assert "project_id = ''" not in sql
    assert params[-1] == 10

    result_ids = {
        memory_id
        for memory_id, _score in service.search(
            "rare sandwich terms",
            10,
            project_id=scope.project_id,
            scope=scope.kind,
        )
    }
    expected_ids = (
        {global_memory.id}
        if scope.kind == "global_only"
        else (
            {project_memory.id}
            if scope.kind == "project_only"
            else {project_memory.id, global_memory.id}
        )
    )
    assert result_ids == expected_ids


@pytest.mark.parametrize(
    "scope",
    [
        RetrievalScope.global_only(),
        RetrievalScope.project_only("project-a"),
        RetrievalScope.project_and_global("project-a"),
    ],
)
@pytest.mark.unit
def test_rendered_statement_parity(scope: RetrievalScope) -> None:
    db = MagicMock()
    db.fetchall.return_value = []
    service = MemoryKeywordSearchService(db)
    rendered_keyword = service.render_search(
        "rare terms",
        8,
        project_id=scope.project_id,
        scope=scope.kind,
    )
    service.search(
        "rare terms",
        8,
        project_id=scope.project_id,
        scope=scope.kind,
    )
    assert rendered_keyword == db.fetchall.call_args.args

    db.fetchall.reset_mock()
    storage = LocalMemoryManager(db)
    rendered_hydration = render_get_memories_statement(
        ["memory-a"],
        scope.memory_scope(),
    )
    storage.get_memories(
        ["memory-a"],
        scope.memory_scope(),
    )
    assert rendered_hydration == db.fetchall.call_args.args


@pytest.mark.integration
async def test_async_keyword_and_hydration_use_dedicated_statements(postgres_db: Any) -> None:
    postgres_db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s), (%s, %s)",
        (_PROJECT_A, "Async Project A", _PROJECT_B, "Async Project B"),
    )
    storage = LocalMemoryManager(postgres_db)
    project_memory = storage.create_memory(
        "asyncevidencelexeme project",
        project_id=_PROJECT_A,
    )
    foreign_memory = storage.create_memory(
        "asyncevidencelexeme foreign",
        project_id=_PROJECT_B,
    )
    scope = RetrievalScope.project_only(_PROJECT_A)

    keyword_hits = await _keyword_hits(
        _candidate(content="asyncevidencelexeme"),
        db=postgres_db,
        scope=scope,
        fetch_limit=5,
    )
    hydrated = await _hydrate_hits(
        [project_memory.id, foreign_memory.id],
        db=postgres_db,
        scope=scope,
    )

    assert [memory_id for memory_id, _score in keyword_hits] == [project_memory.id]
    assert [memory.id for memory in hydrated] == [project_memory.id]
    assert hydrated[0].project_id == _PROJECT_A


@pytest.mark.unit
async def test_blocking_channel_deadline(caplog: pytest.LogCaptureFixture) -> None:
    blocker = asyncio.Event()

    async def block_keyword(*_args: object, **_kwargs: object) -> list[tuple[str, float]]:
        await blocker.wait()
        return []

    candidate = _candidate()
    session = RelatedEvidenceSession()
    with (
        patch.object(related_module, "RELATED_EVIDENCE_CALL_TIMEOUT_SECONDS", 0.02),
        patch.object(related_module, "RELATED_EVIDENCE_PAGE_DEADLINE_SECONDS", 0.2),
        patch("gobby.memory.dream.related._keyword_hits", side_effect=block_keyword),
        patch("gobby.memory.dream.related._vector_hits", AsyncMock(return_value={})),
    ):
        started = asyncio.get_running_loop().time()
        result = await gather_related_evidence(
            [candidate],
            db=MagicMock(),
            vector_store=None,
            dream_config=SimpleNamespace(),
            session=session,
            scope=RetrievalScope.project_only("project-a"),
        )
        elapsed = asyncio.get_running_loop().time() - started
        sentinel = await asyncio.wait_for(asyncio.to_thread(lambda: "usable"), timeout=0.2)
    await session.aclose()

    assert result == [candidate]
    assert elapsed < 0.2
    assert sentinel == "usable"
    assert "channels=keyword" in caplog.text


@pytest.mark.unit
async def test_saturated_keyword_calls_do_not_starve_vector(
    caplog: pytest.LogCaptureFixture,
) -> None:
    blocker = asyncio.Event()
    candidates = [_candidate(f"candidate-{index}") for index in range(8)]
    started_count = 0
    active_count = 0

    async def block_keyword(*_args: object, **_kwargs: object) -> list[tuple[str, float]]:
        nonlocal active_count, started_count
        started_count += 1
        active_count += 1
        try:
            await blocker.wait()
        finally:
            active_count -= 1
        return []

    vector = AsyncMock(return_value={})
    session = RelatedEvidenceSession()
    with (
        patch.object(related_module, "RELATED_EVIDENCE_CALL_TIMEOUT_SECONDS", 0.03),
        patch.object(related_module, "RELATED_EVIDENCE_PAGE_DEADLINE_SECONDS", 0.2),
        patch("gobby.memory.dream.related._keyword_hits", side_effect=block_keyword),
        patch("gobby.memory.dream.related._vector_hits", vector),
    ):
        started = asyncio.get_running_loop().time()
        result = await gather_related_evidence(
            candidates,
            db=MagicMock(),
            vector_store=MagicMock(),
            dream_config=SimpleNamespace(),
            session=session,
            scope=RetrievalScope.project_only("project-a"),
        )
        elapsed = asyncio.get_running_loop().time() - started
        await session.aclose()
        calls_after_close = started_count
        await asyncio.wait_for(asyncio.to_thread(lambda: None), timeout=0.2)

    assert result == candidates
    assert elapsed < 0.2
    assert "channels=keyword" in caplog.text
    assert "channels=page" not in caplog.text
    assert started_count > 0
    assert started_count <= 4
    assert calls_after_close == started_count
    assert active_count == 0
    assert session._timeout_counts["keyword"] == 1
    assert session._timeout_counts["vector"] == 0
    assert vector.await_count == 1
    assert not session._tasks


@pytest.mark.unit
async def test_persistent_blocker_bounded_connections() -> None:
    blocker = asyncio.Event()
    candidates = [_candidate(f"candidate-{index}") for index in range(8)]
    newer = _memory("vector-hit", created_at=_NOW + timedelta(days=1))

    async def block_keyword(*_args: object, **_kwargs: object) -> list[tuple[str, float]]:
        await blocker.wait()
        return []

    async def healthy_vector(
        page_candidates: list[DreamCandidate],
        **_kwargs: object,
    ) -> dict[str, list[tuple[str, float]]]:
        return {page_candidate.id: [("vector-hit", 0.8)] for page_candidate in page_candidates}

    keyword = AsyncMock(side_effect=block_keyword)
    vector = AsyncMock(side_effect=healthy_vector)
    session = RelatedEvidenceSession()
    with (
        patch.object(related_module, "RELATED_EVIDENCE_CALL_TIMEOUT_SECONDS", 0.01),
        patch.object(related_module, "RELATED_EVIDENCE_PAGE_DEADLINE_SECONDS", 0.2),
        patch("gobby.memory.dream.related._keyword_hits", keyword),
        patch("gobby.memory.dream.related._vector_hits", vector),
        patch(
            "gobby.memory.dream.related._hydrate_hits",
            AsyncMock(return_value=[newer]),
        ),
    ):
        first = await gather_related_evidence(
            candidates,
            db=MagicMock(),
            vector_store=MagicMock(),
            dream_config=SimpleNamespace(),
            session=session,
            scope=RetrievalScope.project_only("project-a"),
        )
        second = await gather_related_evidence(
            candidates,
            db=MagicMock(),
            vector_store=MagicMock(),
            dream_config=SimpleNamespace(),
            session=session,
            scope=RetrievalScope.project_only("project-a"),
        )
        keyword_calls_before_breaker = keyword.await_count
        recovered = await gather_related_evidence(
            candidates,
            db=MagicMock(),
            vector_store=MagicMock(),
            dream_config=SimpleNamespace(),
            session=session,
            scope=RetrievalScope.project_only("project-a"),
        )
    await session.aclose()

    assert first == candidates
    assert second == candidates
    assert recovered[0].related[0].id == "vector-hit"
    assert keyword_calls_before_breaker > 0
    assert keyword.await_count == keyword_calls_before_breaker
    assert vector.await_count == 3
    assert session.channel_tripped("keyword")
    assert not session.channel_tripped("vector")
    assert not session.channel_tripped("hydration")
    assert session._timeout_counts == {"keyword": 2, "vector": 0, "hydration": 0}
    assert not session._tasks
    fresh_session = RelatedEvidenceSession()
    assert not fresh_session.channel_tripped("keyword")
    assert fresh_session._timeout_counts == {"keyword": 0, "vector": 0, "hydration": 0}
    await fresh_session.aclose()


@pytest.mark.unit
async def test_session_caller_owned() -> None:
    candidate = _candidate()
    session = RelatedEvidenceSession()
    with (
        patch("gobby.memory.dream.related._keyword_hits", AsyncMock(return_value=[])),
        patch("gobby.memory.dream.related._vector_hits", AsyncMock(return_value={})),
    ):
        await gather_related_evidence(
            [candidate],
            db=MagicMock(),
            vector_store=None,
            dream_config=SimpleNamespace(),
            session=session,
            scope=RetrievalScope.project_only("project-a"),
        )
        await gather_related_evidence(
            [candidate],
            db=MagicMock(),
            vector_store=None,
            dream_config=SimpleNamespace(),
            session=session,
            scope=RetrievalScope.project_only("project-a"),
        )

    assert not session._closed
    assert session._page_index == 2
    invalid_gather = cast(
        Callable[..., Awaitable[list[DreamCandidate]]],
        gather_related_evidence,
    )
    with pytest.raises(TypeError, match="session"):
        await invalid_gather(
            [candidate],
            db=MagicMock(),
            vector_store=None,
            dream_config=SimpleNamespace(),
            scope=RetrievalScope.project_only("project-a"),
        )
    await session.aclose()


@pytest.mark.unit
async def test_qdrant_timeout_integer_contract() -> None:
    store = VectorStore(url="http://qdrant:6333", collection_name="timeout-contract")
    client = MagicMock()
    client.retrieve = AsyncMock(return_value=[SimpleNamespace(id="candidate", vector=[1.0, 0.0])])
    client.query_batch_points = AsyncMock(
        return_value=[SimpleNamespace(points=[SimpleNamespace(id="neighbor", score=0.8)])]
    )
    client.close = AsyncMock()
    store._client = client

    result = await store.search_by_stored_vectors(["candidate"], limit=2, timeout=0.25)
    await store.close()

    assert result == {"candidate": [("neighbor", 0.8)]}
    assert client.retrieve.await_args.kwargs["timeout"] == 1
    assert client.query_batch_points.await_args.kwargs["timeout"] == 1


@pytest.mark.unit
async def test_trickling_response_aborted_at_absolute_deadline() -> None:
    store = VectorStore(url="http://qdrant:6333", collection_name="absolute-deadline")
    blocker = asyncio.Event()

    async def blocked_retrieve(**_kwargs: object) -> list[object]:
        await blocker.wait()
        return []

    client = MagicMock()
    client.retrieve = AsyncMock(side_effect=blocked_retrieve)
    client.close = AsyncMock()
    store._client = client

    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):
        await store.search_by_stored_vectors(["candidate"], limit=2, timeout=0.03)
    elapsed = asyncio.get_running_loop().time() - started
    await store.close()

    assert elapsed < 0.2
    client.close.assert_awaited_once()


@pytest.mark.unit
async def test_uninitialized_client_blocking_init() -> None:
    store = VectorStore(url="http://qdrant:6333", collection_name="blocking-init")
    blocker = asyncio.Event()

    async def blocked_exists(*_args: object, **_kwargs: object) -> bool:
        await blocker.wait()
        return False

    client = MagicMock()
    client.collection_exists = AsyncMock(side_effect=blocked_exists)
    client.close = AsyncMock()
    candidate = _candidate()
    session = RelatedEvidenceSession()
    with (
        patch("gobby.memory.vectorstore.AsyncQdrantClient", return_value=client) as client_factory,
        patch.object(related_module, "RELATED_EVIDENCE_CALL_TIMEOUT_SECONDS", 0.02),
        patch.object(related_module, "RELATED_EVIDENCE_PAGE_DEADLINE_SECONDS", 0.2),
        patch("gobby.memory.dream.related._keyword_hits", AsyncMock(return_value=[])),
    ):
        result = await gather_related_evidence(
            [candidate],
            db=MagicMock(),
            vector_store=store,
            dream_config=SimpleNamespace(),
            session=session,
            scope=RetrievalScope.project_only("project-a"),
        )
        sentinel = await asyncio.wait_for(asyncio.to_thread(lambda: "usable"), timeout=0.2)
    await session.aclose()
    await store.close()

    assert result == [candidate]
    assert sentinel == "usable"
    assert client.collection_exists.await_count == 1
    assert "timeout" not in client.collection_exists.await_args.kwargs
    assert client_factory.call_args.kwargs["timeout"] == 5
    assert not session._tasks
    client.close.assert_awaited_once()


@pytest.mark.unit
async def test_local_mode_keyword_only_degradation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    baseline_threads = threading.active_count()
    candidate = _candidate()
    newer = _memory("keyword-hit", created_at=_NOW + timedelta(days=1))
    local_store = MagicMock()
    local_store.supports_stored_vector_search = False
    session = RelatedEvidenceSession()
    with (
        patch.object(related_module, "_LOCAL_VECTOR_WARNING_EMITTED", False),
        patch(
            "gobby.memory.dream.related._keyword_hits",
            AsyncMock(return_value=[("keyword-hit", 0.8)]),
        ),
        patch(
            "gobby.memory.dream.related._hydrate_hits",
            AsyncMock(return_value=[newer]),
        ),
    ):
        first = await gather_related_evidence(
            [candidate],
            db=MagicMock(),
            vector_store=local_store,
            dream_config=SimpleNamespace(),
            session=session,
            scope=RetrievalScope.project_only("project-a"),
        )
        second = await gather_related_evidence(
            [candidate],
            db=MagicMock(),
            vector_store=local_store,
            dream_config=SimpleNamespace(),
            session=session,
            scope=RetrievalScope.project_only("project-a"),
        )
    await session.aclose()

    assert first[0].related[0].id == "keyword-hit"
    assert second[0].related[0].id == "keyword-hit"
    assert caplog.text.count("disabled for local Qdrant mode") == 1
    assert threading.active_count() == baseline_threads


@pytest.mark.parametrize(
    "scope",
    [RetrievalScope.global_only(), RetrievalScope.project_and_global(_PROJECT_A)],
)
@pytest.mark.integration
def test_keyword_global_not_starved(scope: RetrievalScope, postgres_db: Any) -> None:
    postgres_db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s), (%s, %s)",
        (_PROJECT_A, "Starvation Project A", _PROJECT_B, "Starvation Project B"),
    )
    storage = LocalMemoryManager(postgres_db)
    global_memory = storage.create_memory(
        "starvationlexeme global",
        project_id=_PROJECT_B,
        is_global=True,
    )
    for index in range(6):
        storage.create_memory(
            f"starvationlexeme starvationlexeme foreign {index}",
            project_id=_PROJECT_B,
        )

    service = MemoryKeywordSearchService(postgres_db)
    statement = service.render_search(
        "starvationlexeme",
        3,
        project_id=scope.project_id,
        scope=scope.kind,
    )

    assert statement is not None
    sql, _params = statement
    assert sql.index("memories.is_global IS TRUE") < sql.index("ORDER BY") < sql.index("LIMIT")
    result_ids = {
        memory_id
        for memory_id, _score in service.search(
            "starvationlexeme",
            3,
            project_id=scope.project_id,
            scope=scope.kind,
        )
    }
    assert result_ids == {global_memory.id}


@pytest.mark.unit
async def test_statement_timeout_server_hygiene() -> None:
    db = SimpleNamespace(conninfo="postgresql://evidence")
    bounded = AsyncMock(return_value=[])
    with patch("gobby.memory.dream.related.run_bounded_db", bounded):
        result = await _keyword_hits(
            _candidate(),
            db=db,
            scope=RetrievalScope.global_only(),
            fetch_limit=4,
        )

    assert result == []
    assert bounded.await_args is not None
    assert bounded.await_args.kwargs == {
        "conninfo": "postgresql://evidence",
        "deadline_seconds": related_module.RELATED_EVIDENCE_CALL_TIMEOUT_SECONDS,
    }


@pytest.mark.unit
async def test_blocked_first_set_local_bounded_release() -> None:
    blocker = asyncio.Event()

    async def block() -> None:
        await blocker.wait()

    session = RelatedEvidenceSession()
    with patch.object(related_module, "RELATED_EVIDENCE_CALL_TIMEOUT_SECONDS", 0.01):
        outcome = await session.run_call("keyword", block, page_index=1)
    await asyncio.wait_for(session.aclose(), timeout=0.2)

    assert outcome.timed_out
    assert not session._tasks


@pytest.mark.unit
async def test_cancel_failure_no_accumulation() -> None:
    baseline_threads = threading.active_count()
    for page in range(3):
        blocker = asyncio.Event()

        async def block(event: asyncio.Event = blocker) -> None:
            await event.wait()

        session = RelatedEvidenceSession()
        with patch.object(related_module, "RELATED_EVIDENCE_CALL_TIMEOUT_SECONDS", 0.005):
            outcome = await session.run_call("hydration", block, page_index=page)
        await asyncio.wait_for(session.aclose(), timeout=0.2)
        assert outcome.timed_out
        assert not session._tasks

    assert threading.active_count() == baseline_threads


@pytest.mark.unit
async def test_hub_pool_independence() -> None:
    class PoolGuard:
        conninfo = "postgresql://dedicated"

        @property
        def pool(self) -> object:
            raise AssertionError("evidence retrieval touched the sync pool")

    bounded = AsyncMock(return_value=[])
    with patch("gobby.memory.dream.related.run_bounded_db", bounded):
        await _keyword_hits(
            _candidate(),
            db=PoolGuard(),
            scope=RetrievalScope.project_only("project-a"),
            fetch_limit=4,
        )

    assert bounded.await_args is not None
    assert bounded.await_args.kwargs["conninfo"] == "postgresql://dedicated"
