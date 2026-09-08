"""Storage queries that guard exclusive worktree reuse."""

from __future__ import annotations

from typing import Any

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id


def test_get_active_run_for_worktree_ignores_terminal_runs(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    worktree_id = "31000000-0000-4000-8000-000000000001"
    other_worktree_id = "31000000-0000-4000-8000-000000000002"
    parent = session_manager.register(
        external_id="worktree-query-parent",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_project["id"],
    )
    manager = LocalAgentRunManager(temp_db)
    run = manager.create(
        parent_session_id=parent.id,
        provider="codex",
        prompt="checkpoint",
        worktree_id=worktree_id,
    )

    assert manager.get_active_run_for_worktree(worktree_id) == run
    assert manager.get_active_run_for_worktree(other_worktree_id) is None

    manager.complete(run.id, result="done")

    assert manager.get_active_run_for_worktree(worktree_id) is None
