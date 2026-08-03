from __future__ import annotations

from typing import Any

import pytest
import redis

from tests.memory.recall_benchmark_cleanup import drop_recall_benchmark_graph


class _FakeClient:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.commands: list[tuple[Any, ...]] = []

    async def _execute_command(self, *parts: Any) -> None:
        self.commands.append(parts)
        if self.error is not None:
            raise self.error


@pytest.mark.asyncio
async def test_cleanup_drops_entire_benchmark_graph_namespace() -> None:
    client = _FakeClient()

    await drop_recall_benchmark_graph(client, "test_recall_benchmark_123")

    assert client.commands == [("GRAPH.DELETE", "test_recall_benchmark_123")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    ["Graph does not exist", "ERR Invalid graph operation on empty key"],
)
async def test_cleanup_tolerates_graph_already_missing(message: str) -> None:
    client = _FakeClient(redis.exceptions.ResponseError(message))

    await drop_recall_benchmark_graph(client, "test_recall_benchmark_123")

    assert client.commands == [("GRAPH.DELETE", "test_recall_benchmark_123")]
