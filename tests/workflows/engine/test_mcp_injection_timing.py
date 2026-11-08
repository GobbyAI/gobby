"""Timeout diagnostics retain the active stage of a shared MCP injection."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.hooks.events import HookEvent
from gobby.memory import stage_timing
from gobby.storage.definitions.rules import RuleDefinitionRow
from gobby.workflows.engine import mcp_injections

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("mode", ["inline", "background", "uncached"])
async def test_timeout_names_pending_dispatch_stage(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, mode: str
) -> None:
    host = mcp_injections.CachedMcpInjectionMixin()
    host._initialize_cached_mcp_injections()

    async def dispatch(*_args: Any) -> dict[str, Any]:
        with stage_timing.timed("synthetic_socket_wait"):
            await asyncio.Future[None]()
        return {}

    monkeypatch.setattr(host, "_mcp_dispatcher", dispatch, raising=False)
    monkeypatch.setattr(mcp_injections, "_BACKGROUND_TIMEOUT_SECONDS", 0.05)
    effect = SimpleNamespace(server="synthetic", tool="recall", timeout_seconds=0.01)
    row = cast(RuleDefinitionRow, SimpleNamespace(name="synthetic-recall"))
    event = cast(HookEvent, SimpleNamespace(metadata={"_platform_session_id": "test-session"}))
    with caplog.at_level(logging.INFO, logger="gobby.workflows.engine.mcp_injections"):
        if mode == "uncached":
            with pytest.raises(TimeoutError):
                await host._timed_mcp_dispatch(effect, row, {}, event, 0.01)
        elif mode == "background":
            await host._run_cached_mcp_injection(effect, row, {}, event, "test-session", "key")
        else:
            await host._apply_cached_mcp_injection(effect, row, {}, event, {}, [], {})
            tasks = [entry.task for entry in host._mcp_injection_inflight.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    assert any("synthetic_socket_wait=" in record.getMessage() for record in caplog.records)
    if mode == "inline":
        assert any(
            "active=" in record.getMessage() and "synthetic_socket_wait" in record.getMessage()
            for record in caplog.records
        )
