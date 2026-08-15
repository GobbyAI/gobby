"""Tests for batched required related-memory evidence retrieval."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.memory.dream import related as related_module
from gobby.memory.dream.models import DreamCandidate
from gobby.memory.dream.options import DreamRunOptions
from gobby.memory.dream.related import (
    VECTOR_EVIDENCE_MIN_SCORE,
    RelatedEvidenceChannelError,
    RelatedEvidencePhaseTimeoutError,
    RelatedEvidenceSession,
    RetrievalScope,
    _attach_candidate_evidence,
    _distinctive_terms,
    _hydrate_hits,
    _keyword_hits_bulk,
    _vector_hits,
    gather_related_evidence,
    render_bulk_keyword_statement,
)
from gobby.memory.services.keyword import MemoryKeywordSearchService
from gobby.memory.vectorstore import VectorStore
from gobby.storage.embedding_generation_state import EmbeddingGenerationLeaseLost
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


def _fast_retries() -> Any:
    return patch.object(related_module, "RELATED_EVIDENCE_RETRY_BACKOFF_SECONDS", (0.0, 0.0))


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
    vector_store.search_by_stored_vectors = AsyncMock(
        return_value={"candidate": [("decision", 0.169)]}
    )
    with (
        patch(
            "gobby.memory.dream.related._keyword_hits_bulk",
            AsyncMock(
                return_value={"candidate": [("candidate", 1.0), ("older", 0.95), ("decision", 0.9)]}
            ),
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
async def test_missing_vector_store_raises_typed_failure() -> None:
    session = RelatedEvidenceSession()

    with pytest.raises(RelatedEvidenceChannelError) as excinfo:
        await gather_related_evidence(
            [_candidate()],
            db=MagicMock(),
            vector_store=None,
            dream_config=SimpleNamespace(),
            session=session,
            scope=RetrievalScope.project_only("project-a"),
        )
    await session.aclose()

    assert excinfo.value.channel == "vector"
    assert excinfo.value.attempts == 0


@pytest.mark.unit
async def test_all_channels_failing_raises_typed_failure() -> None:
    candidate = _candidate()
    session = RelatedEvidenceSession()
    keyword = AsyncMock(side_effect=RuntimeError("postgres unavailable"))
    vector = AsyncMock(side_effect=RuntimeError("qdrant unavailable"))
    with (
        _fast_retries(),
        patch("gobby.memory.dream.related._keyword_hits_bulk", keyword),
        patch("gobby.memory.dream.related._vector_hits", vector),
    ):
        with pytest.raises(RelatedEvidenceChannelError) as excinfo:
            await gather_related_evidence(
                [candidate],
                db=MagicMock(),
                vector_store=MagicMock(),
                dream_config=SimpleNamespace(),
                session=session,
                scope=RetrievalScope.project_only("project-a"),
            )
    await session.aclose()

    assert excinfo.value.channel in {"keyword", "vector"}
    assert excinfo.value.attempts == related_module.RELATED_EVIDENCE_RETRY_ATTEMPTS
    assert keyword.await_count == related_module.RELATED_EVIDENCE_RETRY_ATTEMPTS
    assert not session._tasks


@pytest.mark.unit
async def test_fenced_vector_exhaustion_chains_original_lease_error() -> None:
    lease_error = EmbeddingGenerationLeaseLost("Embedding generation serving is fenced")
    session = RelatedEvidenceSession()
    with (
        _fast_retries(),
        patch("gobby.memory.dream.related._keyword_hits_bulk", AsyncMock(return_value={})),
        patch(
            "gobby.memory.dream.related._vector_hits",
            AsyncMock(side_effect=lease_error),
        ),
    ):
        with pytest.raises(RelatedEvidenceChannelError) as excinfo:
            await gather_related_evidence(
                [_candidate()],
                db=MagicMock(),
                vector_store=MagicMock(),
                dream_config=SimpleNamespace(),
                session=session,
                scope=RetrievalScope.project_only("project-a"),
            )
    await session.aclose()

    assert excinfo.value.channel == "vector"
    assert excinfo.value.__cause__ is lease_error


@pytest.mark.unit
async def test_evidence_retry_attempts_config_bounds_channel_attempts() -> None:
    candidate = _candidate()
    session = RelatedEvidenceSession()
    keyword = AsyncMock(side_effect=RuntimeError("postgres unavailable"))
    vector = AsyncMock(return_value={})
    with (
        patch("gobby.memory.dream.related._keyword_hits_bulk", keyword),
        patch("gobby.memory.dream.related._vector_hits", vector),
    ):
        with pytest.raises(RelatedEvidenceChannelError) as excinfo:
            await gather_related_evidence(
                [candidate],
                db=MagicMock(),
                vector_store=MagicMock(),
                dream_config=SimpleNamespace(evidence_retry_attempts=1),
                session=session,
                scope=RetrievalScope.project_only("project-a"),
            )
    await session.aclose()

    # The config knob replaces the module default: one attempt, no retries.
    assert excinfo.value.attempts == 1
    assert keyword.await_count == 1


@pytest.mark.unit
async def test_failed_channel_retries_while_success_preserved(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="gobby.memory.dream.related")
    candidate = _candidate()
    newer = _memory("vector-hit", created_at=_NOW + timedelta(days=1))
    keyword = AsyncMock(return_value={})
    vector = AsyncMock(
        side_effect=[
            RuntimeError("qdrant hiccup"),
            RuntimeError("qdrant hiccup"),
            {"candidate": [("vector-hit", 0.8)]},
        ]
    )
    session = RelatedEvidenceSession()
    with (
        _fast_retries(),
        patch("gobby.memory.dream.related._keyword_hits_bulk", keyword),
        patch("gobby.memory.dream.related._vector_hits", vector),
        patch("gobby.memory.dream.related._hydrate_hits", AsyncMock(return_value=[newer])),
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

    assert result[0].related[0].id == "vector-hit"
    assert result[0].related[0].matched_via == "vector"
    assert keyword.await_count == 1
    assert vector.await_count == 3
    assert "channel=vector attempt=1/3" in caplog.text
    assert "outcome=error" in caplog.text


@pytest.mark.unit
async def test_fenced_vector_attempt_succeeds_after_lease_recovery() -> None:
    lease_error = EmbeddingGenerationLeaseLost("Embedding generation serving is fenced")
    vector = AsyncMock(side_effect=[lease_error, {}])
    session = RelatedEvidenceSession()
    with (
        _fast_retries(),
        patch("gobby.memory.dream.related._keyword_hits_bulk", AsyncMock(return_value={})),
        patch("gobby.memory.dream.related._vector_hits", vector),
    ):
        result = await gather_related_evidence(
            [_candidate()],
            db=MagicMock(),
            vector_store=MagicMock(),
            dream_config=SimpleNamespace(),
            session=session,
            scope=RetrievalScope.project_only("project-a"),
        )
    await session.aclose()

    assert result[0].related == ()
    assert vector.await_count == 2


@pytest.mark.unit
async def test_exhausted_channel_drains_tasks(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="gobby.memory.dream.related")
    blocker = asyncio.Event()

    async def block_keyword(*_args: object, **_kwargs: object) -> dict[str, Any]:
        await blocker.wait()
        return {}

    vector = AsyncMock(return_value={})
    session = RelatedEvidenceSession()
    with (
        _fast_retries(),
        patch.object(related_module, "RELATED_EVIDENCE_CHANNEL_TIMEOUT_SECONDS", 0.02),
        patch.object(related_module, "RELATED_EVIDENCE_RETRY_ATTEMPTS", 2),
        patch("gobby.memory.dream.related._keyword_hits_bulk", side_effect=block_keyword),
        patch("gobby.memory.dream.related._vector_hits", vector),
    ):
        started = asyncio.get_running_loop().time()
        with pytest.raises(RelatedEvidenceChannelError) as excinfo:
            await gather_related_evidence(
                [_candidate()],
                db=MagicMock(),
                vector_store=MagicMock(),
                dream_config=SimpleNamespace(),
                session=session,
                scope=RetrievalScope.project_only("project-a"),
            )
        elapsed = asyncio.get_running_loop().time() - started

    assert excinfo.value.channel == "keyword"
    assert excinfo.value.attempts == 2
    assert elapsed < 1.0
    assert "outcome=timeout" in caplog.text
    assert not session._tasks
    await session.aclose()


@pytest.mark.unit
async def test_phase_timeout_raises_and_drains() -> None:
    blocker = asyncio.Event()

    async def block_keyword(*_args: object, **_kwargs: object) -> dict[str, Any]:
        await blocker.wait()
        return {}

    session = RelatedEvidenceSession()
    with (
        patch.object(related_module, "RELATED_EVIDENCE_PHASE_TIMEOUT_SECONDS", 0.03),
        patch("gobby.memory.dream.related._keyword_hits_bulk", side_effect=block_keyword),
        patch("gobby.memory.dream.related._vector_hits", AsyncMock(return_value={})),
    ):
        started = asyncio.get_running_loop().time()
        with pytest.raises(RelatedEvidencePhaseTimeoutError) as excinfo:
            await gather_related_evidence(
                [_candidate()],
                db=MagicMock(),
                vector_store=MagicMock(),
                dream_config=SimpleNamespace(),
                session=session,
                scope=RetrievalScope.project_only("project-a"),
            )
        elapsed = asyncio.get_running_loop().time() - started

    assert excinfo.value.unit_index == 1
    assert elapsed < 1.0
    assert not session._tasks
    await session.aclose()


@pytest.mark.parametrize(
    ("candidate_count", "expected_units"),
    [(1, 1), (25, 1), (26, 2), (60, 3)],
)
@pytest.mark.unit
async def test_work_unit_operation_counts(candidate_count: int, expected_units: int) -> None:
    candidates = [_candidate(f"candidate-{index}") for index in range(candidate_count)]
    newer = _memory("hit", created_at=_NOW + timedelta(days=1))

    async def keyword(
        unit: list[DreamCandidate], *_args: object, **_kwargs: object
    ) -> dict[str, list[tuple[str, float]]]:
        return {unit[0].id: [("hit", 1.0)]}

    keyword_mock = AsyncMock(side_effect=keyword)
    vector_mock = AsyncMock(return_value={})
    hydrate_mock = AsyncMock(return_value=[newer])
    session = RelatedEvidenceSession()
    with (
        patch("gobby.memory.dream.related._keyword_hits_bulk", keyword_mock),
        patch("gobby.memory.dream.related._vector_hits", vector_mock),
        patch("gobby.memory.dream.related._hydrate_hits", hydrate_mock),
    ):
        result = await gather_related_evidence(
            candidates,
            db=MagicMock(),
            vector_store=MagicMock(),
            dream_config=SimpleNamespace(),
            session=session,
            scope=RetrievalScope.project_only("project-a"),
        )
    await session.aclose()

    assert len(result) == candidate_count
    assert keyword_mock.await_count == expected_units
    assert vector_mock.await_count == expected_units
    assert hydrate_mock.await_count == expected_units
    assert result[0].related[0].id == "hit"


@pytest.mark.unit
async def test_empty_channels_attach_empty_evidence() -> None:
    candidates = [_candidate(f"candidate-{index}") for index in range(3)]
    hydrate_mock = AsyncMock(return_value=[])
    session = RelatedEvidenceSession()
    with (
        patch("gobby.memory.dream.related._keyword_hits_bulk", AsyncMock(return_value={})),
        patch("gobby.memory.dream.related._vector_hits", AsyncMock(return_value={})),
        patch("gobby.memory.dream.related._hydrate_hits", hydrate_mock),
    ):
        result = await gather_related_evidence(
            candidates,
            db=MagicMock(),
            vector_store=MagicMock(),
            dream_config=SimpleNamespace(),
            session=session,
            scope=RetrievalScope.project_only("project-a"),
        )
    await session.aclose()

    assert [candidate.related for candidate in result] == [(), (), ()]
    assert hydrate_mock.await_count == 0
    assert not session._tasks


@pytest.mark.unit
async def test_saturation_bounded_db_usage(caplog: pytest.LogCaptureFixture) -> None:
    """Sixty candidates stay far below the former four-connection ceiling."""
    caplog.set_level(logging.INFO, logger="gobby.memory.dream.related")
    candidates = [_candidate(f"candidate-{index}") for index in range(60)]
    newer = _memory("hit", created_at=_NOW + timedelta(days=1))
    active_db = 0
    peak_db = 0
    active_total = 0
    peak_total = 0

    async def tracked[T](kind: str, value: T) -> T:
        nonlocal active_db, peak_db, active_total, peak_total
        if kind == "db":
            active_db += 1
            peak_db = max(peak_db, active_db)
        active_total += 1
        peak_total = max(peak_total, active_total)
        try:
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await checkpoint.wait()
            return value
        finally:
            if kind == "db":
                active_db -= 1
            active_total -= 1

    async def keyword(
        unit: list[DreamCandidate], *_args: object, **_kwargs: object
    ) -> dict[str, list[tuple[str, float]]]:
        return await tracked("db", {unit[0].id: [("hit", 1.0)]})

    async def vector(*_args: object, **_kwargs: object) -> dict[str, list[tuple[str, float]]]:
        return await tracked("vector", {})

    async def hydrate(*_args: object, **_kwargs: object) -> list[Memory]:
        return await tracked("db", [newer])

    session = RelatedEvidenceSession()
    with (
        patch("gobby.memory.dream.related._keyword_hits_bulk", side_effect=keyword),
        patch("gobby.memory.dream.related._vector_hits", side_effect=vector),
        patch("gobby.memory.dream.related._hydrate_hits", side_effect=hydrate),
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

    assert len(result) == 60
    assert peak_db == 1
    assert peak_total <= 2
    assert elapsed < related_module.RELATED_EVIDENCE_PHASE_TIMEOUT_SECONDS
    assert "channels=page" not in caplog.text
    assert "timed out" not in caplog.text
    assert not session._tasks


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
            "gobby.memory.dream.related._keyword_hits_bulk",
            AsyncMock(
                return_value={
                    "candidate": [
                        ("project-hit", 1.0),
                        ("global-hit", 0.9),
                        ("wrong-project", 0.8),
                    ]
                }
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


@pytest.mark.unit
def test_bulk_keyword_statement_shape() -> None:
    db = MagicMock()
    service = MemoryKeywordSearchService(db)
    scope = RetrievalScope.project_only("project-a")
    single = service.render_search(
        "alpha terms",
        7,
        project_id=scope.project_id,
        scope=scope.kind,
    )
    assert single is not None
    single_sql, single_params = single

    statement = render_bulk_keyword_statement(
        [("cand-1", "alpha terms"), ("cand-2", ""), ("cand-3", "beta terms")],
        db=db,
        scope=scope,
        fetch_limit=7,
    )

    assert statement is not None
    sql, params = statement
    assert sql.count("UNION ALL") == 1
    assert sql.count("ROW_NUMBER() OVER ()") == 2
    assert sql.count(single_sql) >= 1
    assert "ORDER BY score DESC, id ASC" in single_sql
    assert "created_at" not in single_sql
    assert params[0] == "cand-1"
    assert params[1 : 1 + len(single_params)] == single_params
    assert "cand-3" in params
    assert "cand-2" not in params

    assert render_bulk_keyword_statement([("cand", "")], db=db, scope=scope, fetch_limit=7) is None
    with pytest.raises(ValueError, match="at most"):
        render_bulk_keyword_statement(
            [(f"cand-{index}", "terms") for index in range(26)],
            db=db,
            scope=scope,
            fetch_limit=7,
        )


@pytest.mark.integration
async def test_bulk_keyword_and_hydration_against_postgres(postgres_db: Any) -> None:
    postgres_db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s), (%s, %s)",
        (_PROJECT_A, "Async Project A", _PROJECT_B, "Async Project B"),
    )
    storage = LocalMemoryManager(postgres_db)
    first_memory = storage.create_memory(
        "asyncevidencelexeme project",
        project_id=_PROJECT_A,
    )
    second_memory = storage.create_memory(
        "otherevidencelexeme project",
        project_id=_PROJECT_A,
    )
    foreign_memory = storage.create_memory(
        "asyncevidencelexeme foreign",
        project_id=_PROJECT_B,
    )
    scope = RetrievalScope.project_only(_PROJECT_A)

    keyword_hits = await _keyword_hits_bulk(
        [
            _candidate("cand-first", content="asyncevidencelexeme"),
            _candidate("cand-second", content="otherevidencelexeme"),
            _candidate("cand-empty", content=""),
        ],
        db=postgres_db,
        scope=scope,
        fetch_limit=5,
    )
    hydrated = await _hydrate_hits(
        [first_memory.id, foreign_memory.id],
        db=postgres_db,
        scope=scope,
    )

    assert [memory_id for memory_id, _score in keyword_hits["cand-first"]] == [first_memory.id]
    assert [memory_id for memory_id, _score in keyword_hits["cand-second"]] == [second_memory.id]
    assert "cand-empty" not in keyword_hits
    assert [memory.id for memory in hydrated] == [first_memory.id]
    assert hydrated[0].project_id == _PROJECT_A


@pytest.mark.integration
async def test_bulk_keyword_25_candidates_finish_under_channel_budget(
    postgres_db: Any,
) -> None:
    postgres_db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s)",
        (_PROJECT_A, "Budget Project A"),
    )
    storage = LocalMemoryManager(postgres_db)
    tokens = [f"budgetlexeme{index:02d}" for index in range(25)]
    for token in tokens:
        storage.create_memory(token, project_id=_PROJECT_A)
    loop = asyncio.get_running_loop()
    started = loop.time()
    hits = await _keyword_hits_bulk(
        [_candidate(f"cand-{index}", content=token) for index, token in enumerate(tokens)],
        db=postgres_db,
        scope=RetrievalScope.project_only(_PROJECT_A),
        fetch_limit=5,
    )
    elapsed = loop.time() - started

    assert elapsed < 10.0
    assert len(hits) == 25


@pytest.mark.unit
async def test_session_caller_owned() -> None:
    candidate = _candidate()
    session = RelatedEvidenceSession()
    with (
        patch("gobby.memory.dream.related._keyword_hits_bulk", AsyncMock(return_value={})),
        patch("gobby.memory.dream.related._vector_hits", AsyncMock(return_value={})),
    ):
        await gather_related_evidence(
            [candidate],
            db=MagicMock(),
            vector_store=MagicMock(),
            dream_config=SimpleNamespace(),
            session=session,
            scope=RetrievalScope.project_only("project-a"),
        )
        await gather_related_evidence(
            [candidate],
            db=MagicMock(),
            vector_store=MagicMock(),
            dream_config=SimpleNamespace(),
            session=session,
            scope=RetrievalScope.project_only("project-a"),
        )

    assert not session._closed
    assert session._unit_index == 2
    invalid_gather = cast(
        Callable[..., Awaitable[list[DreamCandidate]]],
        gather_related_evidence,
    )
    with pytest.raises(TypeError, match="session"):
        await invalid_gather(
            [candidate],
            db=MagicMock(),
            vector_store=MagicMock(),
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

    with pytest.raises(TimeoutError):
        await store.search_by_stored_vectors(["candidate"], limit=2, timeout=0.03)
    await store.close()

    client.close.assert_awaited_once()


@pytest.mark.unit
async def test_uninitialized_client_blocking_init_fails_vector_channel() -> None:
    store = VectorStore(url="http://qdrant:6333", collection_name="blocking-init")
    blocker = asyncio.Event()

    async def blocked_exists(*_args: object, **_kwargs: object) -> bool:
        await blocker.wait()
        return False

    client = MagicMock()
    client.collection_exists = AsyncMock(side_effect=blocked_exists)
    client.close = AsyncMock()
    session = RelatedEvidenceSession()
    with (
        patch("gobby.memory.vectorstore.AsyncQdrantClient", return_value=client) as client_factory,
        patch.object(related_module, "RELATED_EVIDENCE_CHANNEL_TIMEOUT_SECONDS", 0.02),
        patch.object(related_module, "RELATED_EVIDENCE_RETRY_ATTEMPTS", 1),
        patch("gobby.memory.dream.related._keyword_hits_bulk", AsyncMock(return_value={})),
    ):
        with pytest.raises(RelatedEvidenceChannelError) as excinfo:
            await gather_related_evidence(
                [_candidate()],
                db=MagicMock(),
                vector_store=store,
                dream_config=SimpleNamespace(),
                session=session,
                scope=RetrievalScope.project_only("project-a"),
            )
    await session.aclose()
    await store.close()

    assert excinfo.value.channel == "vector"
    assert client.collection_exists.await_count == 1
    assert "timeout" not in client.collection_exists.await_args.kwargs
    assert client_factory.call_args.kwargs["timeout"] == 5
    assert not session._tasks
    client.close.assert_awaited_once()


@pytest.mark.integration
async def test_local_mode_serves_stored_vector_search(tmp_path: Path) -> None:
    """Local embedded Qdrant serves stored-vector batch search without a gate."""
    store = VectorStore(
        path=str(tmp_path / "qdrant"),
        collection_name="local-evidence",
        embedding_dim=4,
    )
    await store.initialize()
    await store.upsert(
        _stable_uuid("candidate"),
        [1.0, 0.0, 0.0, 0.0],
        {"project_id": "project-a", "is_global": False},
    )
    await store.upsert(
        _stable_uuid("neighbor"),
        [0.9, 0.1, 0.0, 0.0],
        {"project_id": "project-a", "is_global": False},
    )

    result = await store.search_by_stored_vectors([_stable_uuid("candidate")], limit=2)
    await store.close()

    assert not hasattr(store, "supports_stored_vector_search")
    hits = result[_stable_uuid("candidate")]
    assert any(hit_id == _stable_uuid("neighbor") for hit_id, _score in hits)


def _stable_uuid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


@pytest.mark.unit
async def test_statement_timeout_server_hygiene() -> None:
    db = SimpleNamespace(conninfo="postgresql://evidence")
    bounded = AsyncMock(return_value=[])
    with patch("gobby.memory.dream.related.run_bounded_db", bounded):
        result = await _keyword_hits_bulk(
            [_candidate()],
            db=db,
            scope=RetrievalScope.global_only(),
            fetch_limit=4,
        )

    assert result == {}
    assert bounded.await_args is not None
    assert bounded.await_args.kwargs == {
        "conninfo": "postgresql://evidence",
        "deadline_seconds": related_module.RELATED_EVIDENCE_CHANNEL_TIMEOUT_SECONDS,
    }


@pytest.mark.unit
async def test_hub_pool_independence() -> None:
    class PoolGuard:
        conninfo = "postgresql://dedicated"

        @property
        def pool(self) -> object:
            raise AssertionError("evidence retrieval touched the sync pool")

    bounded = AsyncMock(return_value=[])
    with patch("gobby.memory.dream.related.run_bounded_db", bounded):
        await _keyword_hits_bulk(
            [_candidate()],
            db=PoolGuard(),
            scope=RetrievalScope.project_only("project-a"),
            fetch_limit=4,
        )

    assert bounded.await_args is not None
    assert bounded.await_args.kwargs["conninfo"] == "postgresql://dedicated"
