"""Regression coverage for semantic agent self-termination reasons."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.run_completion import complete_and_notify_agent_run
from gobby.agents.runner import AgentRunner
from gobby.agents.runtime_cleanup import AgentRuntimeCleanupResult
from gobby.mcp_proxy.tools.agents import create_agents_registry
from gobby.mcp_proxy.tools.agents_termination import (
    _cleanup_terminal_artifacts,
    _complete_self_terminated_run,
)
from gobby.storage.agents import AgentRunTerminalReason
from gobby.storage.hub.protocol import HubDatabase


def _create_sandbox_roots(gobby_home: Path, run_id: str) -> tuple[Path, Path]:
    sandbox_root = gobby_home / "run" / "sandbox" / run_id
    managed_root = gobby_home / "runtime" / "managed-executions" / run_id
    for root in (sandbox_root, managed_root):
        root.mkdir(parents=True)
        (root / "payload").write_bytes(b"sandbox data")
    violation_log = managed_root / "logs" / "violations.jsonl"
    violation_log.parent.mkdir()
    violation_log.write_text('{"operation":"read"}\n', encoding="utf-8")
    return sandbox_root, managed_root


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_id",
    [None, "44444444-4444-4444-8444-444444444444"],
    ids=["without-terminal", "managed-terminal"],
)
async def test_self_termination_reaps_sandbox_run_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_id: str | None,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    run_id = "11111111-1111-4111-8111-111111111111"
    sandbox_root, managed_root = _create_sandbox_roots(gobby_home, run_id)
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))

    run = SimpleNamespace(
        id=run_id,
        child_session_id=None,
        terminal_id=terminal_id,
        task_id=None,
    )
    terminal_run = SimpleNamespace(status="success")
    runner = MagicMock()
    runner.get_run.return_value = terminal_run
    runner.terminal_runtime_registry.resolve.return_value = MagicMock()

    async def terminate_runtime(**kwargs: Any) -> SimpleNamespace:
        await kwargs["terminalize"]("complete", None)
        assert sandbox_root.exists()
        assert managed_root.exists()
        return SimpleNamespace(success=True, error=None, error_code=None)

    terminal_manager = MagicMock()
    terminal_manager.get.return_value = SimpleNamespace(backend="tmux")
    with (
        patch(
            "gobby.mcp_proxy.tools.agents._kill_agent_process",
            new_callable=AsyncMock,
            return_value={"success": True},
        ),
        patch(
            "gobby.mcp_proxy.tools.agents.complete_and_notify_agent_run",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "gobby.mcp_proxy.tools.agents.cleanup_agent_runtime_state",
            return_value=AgentRuntimeCleanupResult(),
        ),
        patch(
            "gobby.agents.capture.terminate_managed_runtime_async",
            new=terminate_runtime,
        ),
        patch("gobby.storage.terminals.TerminalManager", return_value=terminal_manager),
        patch(
            "gobby.agents.sandbox_reaper.reap_srt_runner_process_tree",
            new_callable=AsyncMock,
            return_value=0,
        ),
    ):
        result = await _complete_self_terminated_run(
            runner=runner,
            run=run,
            kill_db=MagicMock(),
            completion_registry=None,
            session_manager=None,
        )

    assert result["success"] is True
    assert result["status"] == "success"
    assert not sandbox_root.exists()
    assert not managed_root.exists()
    retained = gobby_home / "logs" / "sandbox-violations" / f"{run_id}.jsonl"
    assert retained.read_text(encoding="utf-8") == '{"operation":"read"}\n'


@pytest.mark.asyncio
async def test_self_termination_lost_terminal_race_does_not_reap() -> None:
    run = SimpleNamespace(
        id="11111111-1111-4111-8111-111111111111",
        child_session_id=None,
        terminal_id=None,
        task_id=None,
    )
    runner = MagicMock()
    runner.get_run.return_value = SimpleNamespace(status="cancelled")
    with (
        patch(
            "gobby.mcp_proxy.tools.agents._kill_agent_process",
            new_callable=AsyncMock,
            return_value={"success": True},
        ),
        patch(
            "gobby.mcp_proxy.tools.agents.complete_and_notify_agent_run",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "gobby.mcp_proxy.tools.agents.cleanup_agent_runtime_state",
            return_value=AgentRuntimeCleanupResult(),
        ),
        patch(
            "gobby.mcp_proxy.tools.agents_termination.reap_terminal_sandbox_run",
            new_callable=AsyncMock,
        ) as reap,
    ):
        result = await _complete_self_terminated_run(
            runner=runner,
            run=run,
            kill_db=MagicMock(),
            completion_registry=None,
            session_manager=None,
        )

    assert result["status"] == "cancelled"
    assert result["noop"] is True
    reap.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_cleanup_reap_failure_warns_once_and_preserves_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    result: dict[str, Any] = {"success": True, "status": "cancelled"}
    warning = "Failed to reap SRT sandbox resources for terminal agent"
    with (
        patch(
            "gobby.mcp_proxy.tools.agents.cleanup_agent_runtime_state",
            return_value=AgentRuntimeCleanupResult(),
        ),
        patch(
            "gobby.mcp_proxy.tools.agents_termination.reap_terminal_sandbox_run",
            new_callable=AsyncMock,
            side_effect=OSError("reap failed"),
        ),
        caplog.at_level(
            logging.WARNING,
            logger="gobby.mcp_proxy.tools.agents_termination",
        ),
    ):
        await _cleanup_terminal_artifacts(
            run_id="11111111-1111-4111-8111-111111111111",
            db=MagicMock(),
            terminal_id=None,
            agent_session_id=None,
            debug=False,
            session_manager=None,
            result=result,
            terminal_transition_owned=True,
        )

    assert result == {
        "success": True,
        "status": "cancelled",
        "dispatch_mutex_released": 0,
        "agent_step_instances_deleted": 0,
    }
    warnings = [record for record in caplog.records if warning in record.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING


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
