"""Per-recall stage timing collected through a context variable."""

from __future__ import annotations

import asyncio

import pytest

from gobby.memory import stage_timing


def test_record_outside_a_recording_is_a_no_op() -> None:
    stage_timing.record("orphan", 0.5)

    with stage_timing.recording() as timings:
        pass

    assert timings.seconds == {}
    assert timings.calls == {}


def test_recording_accumulates_seconds_and_calls_per_stage() -> None:
    with stage_timing.recording() as timings:
        stage_timing.record("falkor_wall", 0.25)
        stage_timing.record("falkor_wall", 0.75)
        stage_timing.record("embed", 0.1)

    assert timings.seconds == pytest.approx({"falkor_wall": 1.0, "embed": 0.1})
    assert timings.calls == {"falkor_wall": 2, "embed": 1}


def test_timed_block_records_elapsed_even_when_the_block_raises() -> None:
    with pytest.raises(RuntimeError):
        with stage_timing.recording() as timings:
            with stage_timing.timed("guarded"):
                raise RuntimeError("boom")

    assert timings.calls == {"guarded": 1}


def test_timed_await_records_each_concurrent_leg() -> None:
    async def leg() -> int:
        return 1

    async def run() -> list[int]:
        results = await asyncio.gather(
            stage_timing.timed_await("leg_a", leg()),
            stage_timing.timed_await("leg_b", leg()),
        )
        return list(results)

    with stage_timing.recording() as timings:
        results = asyncio.run(run())

    assert results == [1, 1]
    assert set(timings.seconds) == {"leg_a", "leg_b"}

    assert timings.calls == {"leg_a": 1, "leg_b": 1}
    assert timings.seconds["leg_a"] > 0
    assert timings.seconds["leg_b"] > 0


def test_render_lists_stages_in_name_order_as_milliseconds_and_calls() -> None:
    with stage_timing.recording() as timings:
        stage_timing.record("falkor_wall", 0.2504)
        stage_timing.record("embed", 0.012)
        stage_timing.record("falkor_wall", 0.0)

    assert timings.render() == "embed=12ms/1 falkor_wall=250ms/2"


async def test_graph_leg_records_entity_lookup_and_traversal_stages() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from gobby.memory.services._search_graph import search_graph_scored

    kg_service = MagicMock()
    kg_service.search_entities_by_vector = AsyncMock(
        return_value=[{"entity_key": "k1", "score": 0.9, "memory_ids": ["m1"]}]
    )
    kg_service.find_related_memory_ids = AsyncMock(
        return_value=SimpleNamespace(memory_ids=[], component_map={})
    )

    with stage_timing.recording() as timings:
        await search_graph_scored(
            kg_service=kg_service,
            query_embedding=[0.1, 0.2],
            related_expansion_timeout_seconds=10.0,
        )

    assert timings.calls == {"graph_entity": 1, "graph_traverse": 1}
