"""Shared teardown for recall benchmark-owned FalkorDB graphs."""

from __future__ import annotations

from typing import Protocol

import redis


class _GraphCommandClient(Protocol):
    async def _execute_command(self, *parts: object) -> object: ...


async def drop_recall_benchmark_graph(client: _GraphCommandClient, graph_name: str) -> None:
    """Delete the complete benchmark graph namespace, including its graph key."""
    try:
        await client._execute_command("GRAPH.DELETE", graph_name)
    except redis.exceptions.ResponseError as exc:
        message = str(exc).lower()
        if "graph does not exist" not in message and "empty key" not in message:
            raise
