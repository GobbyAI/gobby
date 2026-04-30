"""Phase 2 tests for the canonical in-process expansion entry point."""

from __future__ import annotations

import asyncio
from inspect import signature
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def test_start_expansion_idempotent() -> None:
    from gobby.mcp_proxy.tools.tasks._expansion import start_expansion_run_impl

    result = start_expansion_run_impl(
        task_manager=MagicMock(),
        llm_service=MagicMock(),
        config=MagicMock(),
        completion_registry=MagicMock(),
        triggering_session_id="session-1",
        task_id="#1",
    )

    assert result.reused is True


def test_completion_emits_terminal_event() -> None:
    from gobby.mcp_proxy.tools.tasks._expansion import start_expansion_run_impl

    registry = MagicMock()
    start_expansion_run_impl(
        task_manager=MagicMock(),
        llm_service=MagicMock(),
        config=MagicMock(),
        completion_registry=registry,
        triggering_session_id="session-1",
        task_id="#1",
        run_id="run-complete",
    )

    registry.emit.assert_any_call("expansion_run_completed", task_id="#1", run_id="run-complete")


def test_failure_emits_terminal_event() -> None:
    from gobby.mcp_proxy.tools.tasks._expansion import start_expansion_run_impl

    registry = MagicMock()
    with patch(
        "gobby.tasks.expansion_service.ExpansionService.compile_and_apply_run",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        start_expansion_run_impl(
            task_manager=MagicMock(),
            llm_service=MagicMock(),
            config=MagicMock(),
            completion_registry=registry,
            triggering_session_id="session-1",
            task_id="#1",
            run_id="run-failed",
        )

    registry.emit.assert_any_call(
        "expansion_run_failed",
        task_id="#1",
        run_id="run-failed",
        reason="boom",
    )


def test_cancellation_emits_terminal_event() -> None:
    from gobby.mcp_proxy.tools.tasks._expansion import start_expansion_run_impl

    registry = MagicMock()
    with patch(
        "gobby.tasks.expansion_service.ExpansionService.compile_and_apply_run",
        AsyncMock(side_effect=asyncio.CancelledError()),
    ):
        start_expansion_run_impl(
            task_manager=MagicMock(),
            llm_service=MagicMock(),
            config=MagicMock(),
            completion_registry=registry,
            triggering_session_id="session-1",
            task_id="#1",
            run_id="run-cancelled",
        )

    registry.emit.assert_any_call("expansion_run_cancelled", task_id="#1", run_id="run-cancelled")


def test_start_expansion_accepts_caller_allocated_run_id() -> None:
    from gobby.mcp_proxy.tools.tasks._expansion import start_expansion_run_impl

    assert "run_id" in signature(start_expansion_run_impl).parameters


def test_synchronous_terminal_emits_event() -> None:
    from gobby.mcp_proxy.tools.tasks._expansion import start_expansion_run_impl

    registry = MagicMock()
    result = start_expansion_run_impl(
        task_manager=MagicMock(),
        llm_service=MagicMock(),
        config=MagicMock(),
        completion_registry=registry,
        triggering_session_id="session-1",
        task_id="#1",
        run_id="run-sync",
        auto_apply=True,
    )

    assert result.status in {"completed", "failed", "cancelled"}
    assert registry.emit.called
