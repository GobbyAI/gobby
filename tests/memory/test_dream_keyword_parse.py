"""One unparseable memory must not stop the dream keyword channel."""

from __future__ import annotations

import logging
import types
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from gobby.memory.dream.models import DreamCandidate
from gobby.memory.dream.related import RetrievalScope, _keyword_hits_bulk


def _candidate(memory_id: str) -> DreamCandidate:
    now = datetime.now(UTC)
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
