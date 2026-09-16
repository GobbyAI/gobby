"""Headless spawned runs refuse durable waits instead of dying on them (#22367)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.session_coordinator import (
    _INCOMPLETE_STEP_WORKFLOW_ERROR,
    SessionCoordinator,
)
from gobby.mcp_proxy.tools.agents_query_tools import register_agent_query_tools
from gobby.mcp_proxy.tools.coordination import register_coordination_tools
from gobby.mcp_proxy.tools.headless_waits import HEADLESS_WAIT_ERROR_CODE
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.storage.agents import LocalAgentRunManager

pytestmark = pytest.mark.unit

#: ``grok --single`` and ``droid exec`` exit when the turn yields.
HEADLESS_PROVIDERS = ("grok", "droid")
#: These keep reading their terminal across a yielded turn, so a wake reaches them.
TERMINAL_PROVIDERS = ("claude", "codex", "qwen")
WAITER = "waiter-session"
OWNER = "owner-session"


def _registry(provider: str | None) -> tuple[InternalToolRegistry, MagicMock]:
    """Register both wait tools against a session whose run uses ``provider``.

    ``None`` stands for a session that is not a spawned run at all.
    """
    ctx = MagicMock()
    ctx.get_current_session_id.return_value = WAITER
    ctx.resolve_session_id.side_effect = lambda reference: reference
    ctx.agent_run_manager.get_by_session.return_value = (
        None if provider is None else SimpleNamespace(provider=provider)
    )
    registry = InternalToolRegistry(name="gobby-agents", description="test")
    register_coordination_tools(registry, ctx)
    register_agent_query_tools(registry, ctx)
    return registry, ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", HEADLESS_PROVIDERS)
async def test_headless_run_refuses_a_coordination_wait_without_registering(provider: str) -> None:
    registry, _ctx = _registry(provider)

    with patch("gobby.mcp_proxy.tools.coordination.CoordinationWaitManager") as manager:
        result = await registry.call(
            "wait_for_coordination",
            {"owner_session": OWNER, "coordination_key": "restart-1"},
        )

    assert result["success"] is False
    assert result["error_code"] == HEADLESS_WAIT_ERROR_CODE
    assert provider in result["error"]
    assert "send_message" in result["retry_guidance"]
    # No durable wait may outlive the process that would have to be woken.
    manager.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", HEADLESS_PROVIDERS)
async def test_headless_run_refuses_an_agent_wait_before_resolving_the_target(
    provider: str,
) -> None:
    registry, ctx = _registry(provider)

    result = await registry.call("wait_for_agent", {"run_id": "deadbeef"})

    assert result["success"] is False
    assert result["error_code"] == HEADLESS_WAIT_ERROR_CODE
    # The refusal is target-independent: no run is looked up to reach it.
    ctx.agent_run_manager.find_by_id_prefix.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", TERMINAL_PROVIDERS)
async def test_terminal_run_still_registers_a_coordination_wait(provider: str) -> None:
    registry, _ctx = _registry(provider)

    with (
        patch("gobby.mcp_proxy.tools.coordination.CoordinationWaitManager") as manager,
        patch("gobby.mcp_proxy.tools.coordination.coordination_wait_payload") as payload,
    ):
        payload.return_value = {"success": True, "outcome": "waiting"}
        result = await registry.call(
            "wait_for_coordination",
            {"owner_session": OWNER, "coordination_key": "restart-1"},
        )

    assert result == {"success": True, "outcome": "waiting"}
    manager.return_value.register.assert_called_once()


@pytest.mark.asyncio
async def test_session_without_an_agent_run_still_registers_a_coordination_wait() -> None:
    registry, _ctx = _registry(None)

    with (
        patch("gobby.mcp_proxy.tools.coordination.CoordinationWaitManager") as manager,
        patch("gobby.mcp_proxy.tools.coordination.coordination_wait_payload") as payload,
    ):
        payload.return_value = {"success": True, "outcome": "waiting"}
        result = await registry.call(
            "wait_for_coordination",
            {"owner_session": OWNER, "coordination_key": "restart-1"},
        )

    assert result == {"success": True, "outcome": "waiting"}
    manager.return_value.register.assert_called_once()


def _coordinator() -> SessionCoordinator:
    # The guard reaches only ``agent_run_manager.db``, which it hands to a patched
    # CoordinationWaitManager, so a real manager would add a hub for nothing.
    manager = cast(LocalAgentRunManager, SimpleNamespace(db=MagicMock()))
    return SessionCoordinator(agent_run_manager=manager)


def _incomplete_workflow() -> SimpleNamespace:
    return SimpleNamespace(
        workflow_name="grok-worker",
        current_step="implement",
        exit_condition="task_closed",
        eval_error=None,
    )


def test_pending_coordination_wait_does_not_fail_the_run_on_session_end() -> None:
    coordinator = _coordinator()

    with (
        patch("gobby.hooks.session_coordinator.CoordinationWaitManager") as manager,
        patch("gobby.workflows.step_context.first_incomplete_step_workflow") as lookup,
    ):
        manager.return_value.has_active_wait.return_value = True
        lookup.return_value = _incomplete_workflow()
        failure = coordinator._incomplete_step_workflow_error("session-1")

    assert failure is None
    # The step is incomplete *because* the session is waiting; that is not a failure.
    lookup.assert_not_called()


def test_incomplete_step_workflow_without_a_wait_still_fails_the_run() -> None:
    coordinator = _coordinator()

    with (
        patch("gobby.hooks.session_coordinator.CoordinationWaitManager") as manager,
        patch("gobby.workflows.step_context.first_incomplete_step_workflow") as lookup,
    ):
        manager.return_value.has_active_wait.return_value = False
        lookup.return_value = _incomplete_workflow()
        failure = coordinator._incomplete_step_workflow_error("session-1")

    assert failure is not None
    assert _INCOMPLETE_STEP_WORKFLOW_ERROR in failure
    assert "workflow=grok-worker" in failure


def test_unreadable_coordination_state_does_not_mask_an_incomplete_workflow() -> None:
    coordinator = _coordinator()

    with (
        patch("gobby.hooks.session_coordinator.CoordinationWaitManager") as manager,
        patch("gobby.workflows.step_context.first_incomplete_step_workflow") as lookup,
    ):
        manager.return_value.has_active_wait.side_effect = RuntimeError("db down")
        lookup.return_value = _incomplete_workflow()
        failure = coordinator._incomplete_step_workflow_error("session-1")

    assert failure is not None
    assert _INCOMPLETE_STEP_WORKFLOW_ERROR in failure
