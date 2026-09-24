"""Maintenance and schema-sweep loops re-read live config each pass."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.code_index.maintenance import code_index_maintenance_loop
from gobby.config.code_index import CodeIndexConfig
from gobby.runner_maintenance import storage_hygiene

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_maintenance_loop_applies_captured_batch_sizes_on_the_next_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[int, int]] = []
    shutdown = asyncio.Event()
    holder = [
        CodeIndexConfig(
            maintenance_interval_seconds=1,
            symbol_summary={"batch_size": 4},
            community_label={"batch_size": 2},
        )
    ]

    async def fake_run(
        _context: Any,
        _summarizer: Any,
        symbol_summary_batch_size: int,
        **kwargs: Any,
    ) -> None:
        seen.append((symbol_summary_batch_size, kwargs["community_label_batch_size"]))
        if len(seen) == 1:
            holder[0] = CodeIndexConfig(
                maintenance_interval_seconds=1,
                symbol_summary={"batch_size": 9},
                community_label={"batch_size": 8},
            )
            return
        shutdown.set()

    monkeypatch.setattr("gobby.code_index.maintenance._run_maintenance", fake_run)

    await asyncio.wait_for(
        code_index_maintenance_loop(
            context=MagicMock(),
            shutdown_flag=shutdown,
            interval=1,
            symbol_summary_batch_size=4,
            community_label_batch_size=2,
            capture_config=lambda: holder[0],
        ),
        timeout=2,
    )

    assert seen == [(4, 2), (9, 8)]


@pytest.mark.asyncio
async def test_schema_sweep_applies_captured_database_url_on_the_next_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls: list[str] = []
    holder = ["postgresql://first"]

    def sweep(database_url: str, age_hours: int = 24) -> None:
        del age_hours
        urls.append(database_url)
        if len(urls) == 1:
            holder[0] = "postgresql://second"

    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(storage_hygiene, "sweep_orphaned_test_schemas", sweep)

    await storage_hygiene.sweep_test_schemas_loop(
        "postgresql://first",
        lambda: False,
        interval_seconds=1,
        sleep=sleep,
        capture_database_url=lambda: holder[0],
    )

    assert urls == ["postgresql://first", "postgresql://second"]
