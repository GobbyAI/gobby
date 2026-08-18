"""Machine ownership contracts for locally created storage records."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

import gobby.storage.agents._lifecycle as agent_lifecycle_module
import gobby.storage.clones as clones_module
import gobby.storage.cron_runs as cron_runs_module
import gobby.storage.worktrees as worktrees_module
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.clones import Clone, LocalCloneManager
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronRun
from gobby.storage.worktrees import LocalWorktreeManager, Worktree

pytestmark = pytest.mark.unit

MACHINE_ID = "11111111-1111-4111-8111-111111111111"
NOW = datetime(2026, 8, 4, tzinfo=UTC)


def test_creation_paths_stamp_machine_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """All local writers persist and return the canonical machine UUID."""
    for module in (agent_lifecycle_module, cron_runs_module):
        monkeypatch.setattr(module, "get_machine_id", lambda: MACHINE_ID, raising=False)
    for module in (worktrees_module, clones_module):
        monkeypatch.setattr(module, "require_machine_id", lambda: MACHINE_ID)

    worktree_db = MagicMock()
    worktree_db.execute.return_value.fetchone.return_value = {"created_at": NOW, "updated_at": NOW}
    worktree = LocalWorktreeManager(worktree_db).create("project", "branch", "/worktree")
    clone_db = MagicMock()
    clone_db.execute.return_value.fetchone.return_value = {"created_at": NOW, "updated_at": NOW}
    clone = LocalCloneManager(clone_db).create("project", "branch", "/clone")

    agent_db = MagicMock()
    agent_manager = LocalAgentRunManager(agent_db)
    agent = AgentRun(
        id="agent-run",
        parent_session_id="parent",
        provider="codex",
        prompt="do work",
        status="pending",
        created_at=NOW,
        updated_at=NOW,
        machine_id=MACHINE_ID,
    )
    monkeypatch.setattr(agent_manager, "get", lambda _run_id: agent)
    created_agent = agent_manager.create("parent", "codex", "do work", run_id="agent-run")

    cron_row = {
        "id": "cron-run",
        "cron_job_id": "cron-job",
        "triggered_at": NOW,
        "created_at": NOW,
        "started_at": None,
        "completed_at": None,
        "status": "pending",
        "output": None,
        "error": None,
        "agent_run_id": None,
        "pipeline_execution_id": None,
        "scheduler_owner": None,
        "machine_id": MACHINE_ID,
    }
    cron_db = MagicMock()
    cron_db.fetchone.return_value = cron_row
    cron_storage = CronJobStorage(cron_db)
    monkeypatch.setattr(cron_storage, "_hydrate_run", lambda run: run)
    cron = cron_storage.create_run("cron-job")

    assert cron is not None
    records: tuple[Worktree | Clone | AgentRun | CronRun, ...] = (
        worktree,
        clone,
        created_agent,
        cron,
    )
    assert all(record.machine_id == MACHINE_ID for record in records)
    assert all(record.to_dict()["machine_id"] == MACHINE_ID for record in records)

    for db in (worktree_db, clone_db, agent_db):
        sql, params = db.execute.call_args.args
        assert "machine_id" in sql
        assert MACHINE_ID in params
    cron_sql, cron_params = cron_db.fetchone.call_args.args
    assert "machine_id" in cron_sql
    assert MACHINE_ID in cron_params


def test_models_round_trip_machine_id() -> None:
    """Every machine-owned model preserves the UUID through row and JSON forms."""
    worktree = Worktree.from_row(
        {
            "id": "worktree",
            "project_id": "project",
            "task_id": None,
            "branch_name": "branch",
            "worktree_path": "/worktree",
            "base_branch": "main",
            "agent_session_id": None,
            "status": "active",
            "created_at": NOW,
            "updated_at": NOW,
            "last_activity_at": NOW,
            "merged_at": None,
            "machine_id": MACHINE_ID,
        }
    )
    clone = Clone.from_row(
        {
            "id": "clone",
            "project_id": "project",
            "branch_name": "branch",
            "clone_path": "/clone",
            "base_branch": "main",
            "task_id": None,
            "agent_session_id": None,
            "status": "active",
            "remote_url": None,
            "last_sync_at": None,
            "cleanup_after": None,
            "created_at": NOW,
            "updated_at": NOW,
            "machine_id": MACHINE_ID,
        }
    )
    agent = AgentRun.from_row(
        {
            "id": "agent-run",
            "parent_session_id": "parent",
            "child_session_id": None,
            "workflow_name": None,
            "provider": "codex",
            "model": None,
            "status": "pending",
            "prompt": "do work",
            "result": None,
            "error": None,
            "tool_calls_count": 0,
            "turns_used": 0,
            "started_at": None,
            "completed_at": None,
            "created_at": NOW,
            "updated_at": NOW,
            "machine_id": MACHINE_ID,
        }
    )
    cron = CronRun.from_row(
        {
            "id": "cron-run",
            "cron_job_id": "cron-job",
            "triggered_at": NOW,
            "created_at": NOW,
            "machine_id": MACHINE_ID,
        }
    )

    records: tuple[Worktree | Clone | AgentRun | CronRun, ...] = (worktree, clone, agent, cron)
    assert all(record.machine_id == MACHINE_ID for record in records)
    assert all(record.to_dict()["machine_id"] == MACHINE_ID for record in records)
