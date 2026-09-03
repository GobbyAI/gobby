"""Regression coverage for semantic agent self-termination reasons."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.run_completion import complete_and_notify_agent_run
from gobby.agents.runner import AgentRunner
from gobby.mcp_proxy.tools.agents import create_agents_registry
from gobby.mcp_proxy.tools.agents_termination import _complete_self_terminated_run
from gobby.storage.agents import AgentRunTerminalReason
from gobby.storage.hub.protocol import HubDatabase


@pytest.mark.asyncio
async def test_blocker_exit_reports_blocked_status_and_dirty_paths(
    temp_db: HubDatabase,
) -> None:
    run = MagicMock(
        id="11111111-1111-4111-8111-111111111111",
        child_session_id="22222222-2222-4222-8222-222222222222",
        terminal_id=None,
        task_id="33333333-3333-4333-8333-333333333333",
        worktree_id=None,
        clone_id=None,
        status="running",
        terminal_reason=None,
        result=None,
        error=None,
        provider="codex",
        model=None,
        prompt="Implement the task",
        tool_calls_count=0,
        turns_used=0,
        started_at=None,
        completed_at=None,
        capture_id=None,
        resume_metadata_json={},
    )
    runner = MagicMock()
    run_storage = MagicMock()
    run_storage.db = temp_db
    run_storage.get.return_value = run
    runner.run_storage = run_storage
    runner._run_storage = run_storage
    runner._session_manager = MagicMock()
    runner._session_manager.get.return_value = None
    runner.get_run.return_value = run

    def persist_run(
        *,
        run_id: str,
        result: str | None,
        tool_calls_count: int,
        turns_used: int,
        terminal_reason: AgentRunTerminalReason | None,
    ) -> MagicMock:
        assert run_id == run.id
        run.status = "success"
        run.result = result
        run.tool_calls_count = tool_calls_count
        run.turns_used = turns_used
        run.terminal_reason = terminal_reason
        return run

    def complete_run(
        run_id: str,
        result: str | None = None,
        terminal_reason: AgentRunTerminalReason | None = None,
    ) -> bool:
        return AgentRunner.complete_run(
            runner,
            run_id,
            result=result,
            terminal_reason=terminal_reason,
        )

    run_storage.complete.side_effect = persist_run
    runner.complete_run.side_effect = complete_run
    session_vars: dict[str, Any] = {
        "blocker_handed_off": True,
        "task_edited_files": {run.task_id: ["src/dirty.py"]},
    }
    variable_manager = MagicMock()
    variable_manager.get_variables.return_value = session_vars
    completion_call: dict[str, Any] = {}

    async def capture_completion(*args: Any, **kwargs: Any) -> bool:
        completion_call.update(kwargs)
        return await complete_and_notify_agent_run(*args, **kwargs)

    with (
        patch(
            "gobby.mcp_proxy.tools.agents._kill_agent_process",
            new_callable=AsyncMock,
            return_value={"success": True},
        ),
        patch(
            "gobby.mcp_proxy.tools.agents._cleanup_terminal_artifacts",
            new_callable=AsyncMock,
        ),
        patch(
            "gobby.mcp_proxy.tools.agents.complete_and_notify_agent_run",
            new=capture_completion,
        ),
        patch(
            "gobby.workflows.state_manager.SessionVariableManager",
            return_value=variable_manager,
        ),
        patch(
            "gobby.agents.run_completion.SessionVariableManager",
            return_value=variable_manager,
        ),
        patch(
            "gobby.agents.run_completion._agent_run_checkout_root",
            return_value="/repo",
        ),
        patch(
            "gobby.agents.run_completion.task_dirty_paths",
            return_value={"src/dirty.py"},
        ) as dirty_paths,
    ):
        termination = await _complete_self_terminated_run(
            runner=runner,
            run=run,
            kill_db=temp_db,
            completion_registry=None,
            session_manager=None,
        )
        get_result = create_agents_registry(runner)._tools["get_agent_result"].func
        result = await get_result(run_id=run.id)

    assert termination["success"] is True
    assert termination["terminal_reason"] == "task_blocker"
    runner.complete_run.assert_called_once_with(
        run.id,
        result=None,
        terminal_reason="task_blocker",
    )
    run_storage.complete.assert_called_once_with(
        run_id=run.id,
        result=None,
        tool_calls_count=0,
        turns_used=0,
        terminal_reason="task_blocker",
    )
    assert completion_call["terminal_reason"] == "task_blocker"
    assert completion_call["notify_result"] == {
        "status": "blocked",
        "run_id": run.id,
        "dirty_paths": ["src/dirty.py"],
        "terminal_reason": "task_blocker",
    }
    assert 'dirty_paths=["src/dirty.py"]' in completion_call["message"]
    assert result["status"] == "blocked"
    assert result["terminal_reason"] == "task_blocker"
    assert result["dirty_paths"] == ["src/dirty.py"]
    dirty_paths.assert_called_with({"src/dirty.py"}, "/repo")
