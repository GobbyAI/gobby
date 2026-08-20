"""Tests for cancelling stale helper agent runs."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.agents import create_agents_registry
from tests.agents.terminal_fixtures import make_live_terminal, make_pending_terminal

pytestmark = pytest.mark.unit


def _make_runner_with_run_storage() -> MagicMock:
    runner = MagicMock()
    runner.run_storage = MagicMock()
    runner.run_storage.db = object()
    runner.run_storage.list_by_parent.return_value = []
    return runner


def _make_agent_run(
    run_id: str,
    *,
    status: str = "running",
    agent_name: str = "status-helper",
    parent_session_id: str = "parent-session",
) -> MagicMock:
    run = MagicMock()
    run.id = run_id
    run.status = status
    run.agent_name = agent_name
    run.parent_session_id = parent_session_id
    run.child_session_id = f"child-{run_id}"
    run.terminal_id = f"tmux-{run_id}"
    return run


@pytest.mark.asyncio
async def test_cancel_stale_helpers_requires_parent_session_id() -> None:
    runner = _make_runner_with_run_storage()
    registry = create_agents_registry(runner)
    cancel_stale_helpers = registry._tools["cancel_stale_helpers"].func

    result = await cancel_stale_helpers(
        parent_session_id="",
        agent_name="status-helper",
    )

    assert result == {"success": False, "error": "parent_session_id is required"}


@pytest.mark.asyncio
async def test_cancel_stale_helpers_requires_agent_name() -> None:
    runner = _make_runner_with_run_storage()
    registry = create_agents_registry(runner)
    cancel_stale_helpers = registry._tools["cancel_stale_helpers"].func

    result = await cancel_stale_helpers(parent_session_id="parent-session", agent_name="")

    assert result == {"success": False, "error": "agent_name is required"}


@pytest.mark.asyncio
async def test_cancel_stale_helpers_returns_empty_when_no_helpers_running() -> None:
    runner = _make_runner_with_run_storage()
    registry = create_agents_registry(runner)
    cancel_stale_helpers = registry._tools["cancel_stale_helpers"].func

    result = await cancel_stale_helpers(
        parent_session_id="parent-session",
        agent_name="status-helper",
    )

    assert result == {"success": True, "cancelled": [], "errors": [], "count": 0}
    runner.run_storage.list_by_parent.assert_called_once_with("parent-session")


@pytest.mark.asyncio
async def test_cancel_stale_helpers_returns_failure_for_bad_session_ref() -> None:
    runner = _make_runner_with_run_storage()
    session_manager = MagicMock()
    session_manager.resolve_session_reference.side_effect = ValueError("bad session")
    registry = create_agents_registry(runner, session_manager=session_manager)
    cancel_stale_helpers = registry._tools["cancel_stale_helpers"].func

    result = await cancel_stale_helpers(
        parent_session_id="missing",
        agent_name="status-helper",
    )

    assert result == {"success": False, "error": "bad session"}
    runner.run_storage.list_by_parent.assert_not_called()


@pytest.mark.asyncio
async def test_best_effort_continues_on_per_run_failure() -> None:
    first = _make_agent_run("run-first")
    second = _make_agent_run("run-second")
    runner = _make_runner_with_run_storage()
    runner.run_storage.list_by_parent.return_value = [first, second]
    runner.get_run.side_effect = {"run-first": first, "run-second": second}.__getitem__

    registry = create_agents_registry(runner)
    cancel_stale_helpers = registry._tools["cancel_stale_helpers"].func

    async def kill_side_effect(
        run: MagicMock, *_args: object, **_kwargs: object
    ) -> dict[str, bool]:
        if run.id == "run-first":
            raise RuntimeError("process refused TERM")
        return {"success": True}

    with (
        patch(
            "gobby.mcp_proxy.tools.agents._kill_agent_process",
            new_callable=AsyncMock,
            side_effect=kill_side_effect,
        ) as kill_agent_process,
        patch(
            "gobby.mcp_proxy.tools.agent_cancellation.terminalize_cancelled_agent_run",
            new_callable=AsyncMock,
            return_value=True,
        ) as terminalize,
        patch(
            "gobby.mcp_proxy.tools.agents._cleanup_terminal_artifacts",
            new_callable=AsyncMock,
        ) as cleanup,
        patch(
            "gobby.agents.terminal_delivery.deliver_existing_terminal_run_in_scope",
            new_callable=AsyncMock,
        ) as deliver,
    ):
        result = await cancel_stale_helpers(
            parent_session_id="parent-session",
            agent_name="status-helper",
        )

    assert result == {
        "success": True,
        "cancelled": ["run-second"],
        "errors": [{"run_id": "run-first", "error": "process refused TERM"}],
        "count": 1,
    }
    assert kill_agent_process.await_count == 2
    terminalize.assert_awaited_once()
    cleanup.assert_awaited_once()
    assert deliver.await_count == 2


@pytest.mark.asyncio
async def test_cleanup_step_order_parity_with_stop_agent() -> None:
    call_order: list[str] = []
    stale_run = _make_agent_run("run-123")
    runner = _make_runner_with_run_storage()
    runner.run_storage.list_by_parent.return_value = [stale_run]
    runner.get_run.return_value = stale_run

    registry = create_agents_registry(runner)
    cancel_stale_helpers = registry._tools["cancel_stale_helpers"].func

    async def kill_side_effect(*_args: object, **_kwargs: object) -> dict[str, bool]:
        call_order.append("kill")
        return {"success": True}

    async def terminalize_side_effect(**_kwargs: object) -> bool:
        call_order.append("terminalize")
        return True

    async def cleanup_side_effect(**_kwargs: object) -> None:
        call_order.append("cleanup")

    async def deliver_side_effect(**_kwargs: object) -> None:
        call_order.append("deliver")

    with (
        patch(
            "gobby.mcp_proxy.tools.agents._kill_agent_process",
            new_callable=AsyncMock,
            side_effect=kill_side_effect,
        ) as kill_agent_process,
        patch(
            "gobby.mcp_proxy.tools.agent_cancellation.terminalize_cancelled_agent_run",
            new_callable=AsyncMock,
            side_effect=terminalize_side_effect,
        ) as terminalize,
        patch(
            "gobby.mcp_proxy.tools.agents._cleanup_terminal_artifacts",
            new_callable=AsyncMock,
            side_effect=cleanup_side_effect,
        ) as cleanup,
        patch(
            "gobby.agents.terminal_delivery.deliver_existing_terminal_run_in_scope",
            new_callable=AsyncMock,
            side_effect=deliver_side_effect,
        ) as deliver,
    ):
        result = await cancel_stale_helpers(
            parent_session_id="parent-session",
            agent_name="status-helper",
        )

    assert result == {"success": True, "cancelled": ["run-123"], "errors": [], "count": 1}
    assert call_order == ["kill", "terminalize", "cleanup", "deliver"]
    kill_agent_process.assert_awaited_once()
    terminalize.assert_awaited_once()
    cleanup.assert_awaited_once()
    deliver.assert_awaited_once()


@pytest.mark.asyncio
async def test_db_less_registry_uses_runner_run_storage() -> None:
    stale_run = _make_agent_run("run-123")
    runner = _make_runner_with_run_storage()
    runner.run_storage.list_by_parent.return_value = [stale_run]
    runner.get_run.return_value = stale_run

    registry = create_agents_registry(runner, db=None)
    cancel_stale_helpers = registry._tools["cancel_stale_helpers"].func

    with (
        patch(
            "gobby.mcp_proxy.tools.agents._kill_agent_process",
            new_callable=AsyncMock,
            return_value={"success": True},
        ),
        patch(
            "gobby.mcp_proxy.tools.agent_cancellation.terminalize_cancelled_agent_run",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "gobby.mcp_proxy.tools.agents._cleanup_terminal_artifacts",
            new_callable=AsyncMock,
        ),
        patch(
            "gobby.agents.terminal_delivery.deliver_existing_terminal_run_in_scope",
            new_callable=AsyncMock,
        ) as deliver,
    ):
        result = await cancel_stale_helpers(
            parent_session_id="parent-session",
            agent_name="status-helper",
        )

    assert result == {"success": True, "cancelled": ["run-123"], "errors": [], "count": 1}
    runner.run_storage.list_by_parent.assert_called_once_with("parent-session")
    assert deliver.await_args is not None
    assert deliver.await_args.kwargs["db"] is runner.run_storage.db
