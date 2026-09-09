"""MCP adapter for coordinator-only terminal worktree checkpoints."""

from __future__ import annotations

import logging
from typing import Any

from gobby.agents.worktree_checkpoint import (
    checkpoint_agent_worktree as checkpoint_terminal_agent_worktree,
)
from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry

logger = logging.getLogger(__name__)


def register_agent_checkpoint_tools(
    registry: InternalToolRegistry,
    ctx: AgentsRegistryContext,
) -> None:
    @registry.tool(
        name="checkpoint_agent_worktree",
        description=(
            "Checkpoint a terminal child agent's task-owned isolated worktree for reuse. "
            "Only the run's parent coordinator may call this operation; it never releases "
            "a task claim or terminates an agent run."
        ),
    )
    async def checkpoint_agent_worktree(run_id: str) -> dict[str, Any]:
        caller_ref = ctx.get_current_session_id()
        if not caller_ref:
            return {
                "success": False,
                "error": "No active coordinator session context",
                "error_code": "session_required",
            }
        try:
            caller_session_id = ctx.resolve_session_id(caller_ref)
        except ValueError as exc:
            return {"success": False, "error": str(exc), "error_code": "session_invalid"}
        try:
            return await checkpoint_terminal_agent_worktree(
                agent_run_manager=ctx.agent_run_manager,
                task_manager=ctx.task_manager,
                worktree_storage=ctx.worktree_storage,
                db=ctx.db,
                run_id=run_id,
                caller_session_id=caller_session_id,
            )
        except Exception as exc:
            logger.exception("Unexpected worktree checkpoint failure for run %s", run_id)
            return {
                "success": False,
                "error": f"Checkpoint failed: {exc}",
                "error_code": "checkpoint_failed",
            }
