"""Explicit metadata-free checkouts retain their spawning project's identity."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry


@pytest.mark.asyncio
@pytest.mark.parametrize("has_metadata", [False, True])
async def test_explicit_checkout_project_identity_at_tool_boundary(
    tmp_path: Path, has_metadata: bool
) -> None:
    parent_context = {"id": "parent-project", "project_path": "/parent"}
    checkout_context = {"id": "checkout-project", "project_path": str(tmp_path)}
    implementation = AsyncMock(return_value={"success": True, "run_id": "review"})
    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._factory._context_from_project_path",
            return_value=checkout_context if has_metadata else None,
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._factory._parent_session_project_context",
            return_value=parent_context,
        ),
        patch(
            "gobby.utils.session_context.resolve_session_ref",
            return_value="parent-session",
        ),
        patch("gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body", return_value=None),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._factory.spawning_session_provider",
            return_value="codex",
        ),
        patch("gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl", implementation),
    ):
        registry = create_spawn_agent_registry(MagicMock())
        result = await registry.call(
            "spawn_agent",
            {
                "prompt": "Review this checkout",
                "project_path": str(tmp_path),
                "parent_session_id": "parent-session",
                "isolation": "none",
            },
        )

    assert result == {"success": True, "run_id": "review"}
    implementation.assert_awaited_once()
    arguments = implementation.await_args
    assert arguments is not None
    assert arguments.kwargs["project_path"] == str(tmp_path)
    assert arguments.kwargs["target_project_id"] == (
        "checkout-project" if has_metadata else "parent-project"
    )
