"""One unparseable memory must not stop the dream keyword channel."""

from __future__ import annotations

import logging
import types
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.memory.dream.models import DreamCandidate
from gobby.memory.dream.related import (
    RelatedEvidenceSession,
    RetrievalScope,
    _keyword_hits_bulk,
    gather_related_evidence,
)
from gobby.storage.memories_models import Memory, MemoryType


def _candidate(memory_id: str, *, created_at: datetime | None = None) -> DreamCandidate:
    now = created_at or datetime.now(UTC)
    return DreamCandidate(
        id=memory_id,
        content=f"plain content for {memory_id}",
        memory_type="fact",
        project_id="proj-1",
        is_global=False,
        source_type=None,
        source_session_id=None,
        tags=[],
        age_days=1.0,
        access_count=0,
        created_at=now,
        updated_at=now,
        last_accessed_at=None,
    )


def _closed(fn: types.FunctionType, name: str) -> object:
    closure = fn.__closure__
    assert closure is not None
    return closure[fn.__code__.co_freevars.index(name)].cell_contents


@pytest.mark.asyncio
async def test_keyword_channel_skips_one_unparseable_memory(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def render(queries: list[tuple[str, str]], **_kwargs: object) -> tuple[str, tuple[str, ...]]:
        return "SELECT 1", tuple(memory_id for memory_id, _query in queries)

    async def run_bounded(execute: object, **_kwargs: object) -> list[dict[str, object]]:
        assert isinstance(execute, types.FunctionType)
        params = _closed(execute, "params")
        assert isinstance(params, tuple)
        if params == ("bad-memory",):
            raise RuntimeError("could not parse query string 'content:(bad)'")
        if "bad-memory" in params:
            raise RuntimeError("could not parse query string 'content:(batch)'")
        return [
            {
                "candidate_key": "good-memory",
                "id": "hit-1",
                "hit_rank": 1,
                "score": 0.5,
            }
        ]

    db = MagicMock()
    db.conninfo = "dbname=test"
    with (
        caplog.at_level(logging.WARNING),
        patch("gobby.memory.dream.related.render_bulk_keyword_statement", render),
        patch("gobby.memory.dream.related.run_bounded_db", run_bounded),
    ):
        hits = await _keyword_hits_bulk(
            [_candidate("bad-memory"), _candidate("good-memory")],
            db=db,
            scope=RetrievalScope.project_only("proj-1"),
            fetch_limit=5,
        )

    assert hits["good-memory"] == [("hit-1", 0.5)]
    assert "bad-memory" not in hits
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "bad-memory" in record.getMessage()
    ]
    assert warnings
    assert all(record.exc_info is None for record in warnings)


@pytest.mark.asyncio
async def test_related_evidence_round_completes_other_memory_after_parse_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dream related-evidence round keeps the other memory when one query cannot parse."""
    anchor = datetime(2026, 9, 23, tzinfo=UTC)
    hit = Memory(
        id="hit-1",
        memory_type=MemoryType.FACT,
        content="evidence for the other memory",
        created_at=anchor + timedelta(days=1),
        updated_at=anchor + timedelta(days=1),
        project_id="proj-1",
        is_global=False,
    )

    def render(queries: list[tuple[str, str]], **_kwargs: object) -> tuple[str, tuple[str, ...]]:
        return "SELECT 1", tuple(memory_id for memory_id, _query in queries)

    async def run_bounded(execute: object, **_kwargs: object) -> list[dict[str, object]]:
        assert isinstance(execute, types.FunctionType)
        params = _closed(execute, "params")
        assert isinstance(params, tuple)
        if "bad-memory" in params:
            raise RuntimeError("could not parse query string 'content:(bad)'")
        return [
            {
                "candidate_key": "good-memory",
                "id": "hit-1",
                "hit_rank": 1,
                "score": 0.5,
            }
        ]

    vector_store = MagicMock()
    vector_store.search_by_stored_vectors = AsyncMock(return_value={})
    session = RelatedEvidenceSession()
    db = MagicMock()
    db.conninfo = "dbname=test"
    with (
        caplog.at_level(logging.WARNING),
        patch("gobby.memory.dream.related.render_bulk_keyword_statement", render),
        patch("gobby.memory.dream.related.run_bounded_db", run_bounded),
        patch(
            "gobby.memory.dream.related._hydrate_hits",
            AsyncMock(return_value=[hit]),
        ),
    ):
        result = await gather_related_evidence(
            [
                _candidate("bad-memory", created_at=anchor),
                _candidate("good-memory", created_at=anchor),
            ],
            db=db,
            vector_store=vector_store,
            dream_config=SimpleNamespace(evidence_retry_attempts=1, related_evidence_top_k=5),
            session=session,
            scope=RetrievalScope.project_only("proj-1"),
        )
    await session.aclose()

    by_id = {candidate.id: candidate for candidate in result}
    assert [item.id for item in by_id["good-memory"].related] == ["hit-1"]
    assert by_id["bad-memory"].related == ()
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "bad-memory" in record.getMessage()
    ]
    assert warnings
    assert all(record.exc_info is None for record in warnings)
