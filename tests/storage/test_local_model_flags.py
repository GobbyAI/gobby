"""Red tests for persisted local-model flags on sessions and agent runs."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from gobby.agents.local_model import count_active_local_agents
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.agents._selectors import _AgentRunSelectorMixin
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from tests.agents.terminal_fixtures import make_live_terminal, make_pending_terminal

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def test_baseline_schema_includes_local_flag_columns(temp_db: HubDatabase) -> None:
    db = temp_db

    agent_columns = {
        row["column_name"]
        for row in db.fetchall(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            ("agent_runs",),
        )
    }
    session_columns = {
        row["column_name"]
        for row in db.fetchall(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            ("sessions",),
        )
    }

    assert "is_local" in agent_columns
    assert "is_local" in session_columns


def test_agent_run_manager_persists_is_local_flag(temp_db: HubDatabase) -> None:
    db = temp_db
    project = LocalProjectManager(db).create(name="agent-local-project")
    session = SessionManager(db).register(
        external_id="parent-ext",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="claude",
        project_id=project.id,
    )

    run = LocalAgentRunManager(db).create(
        parent_session_id=session.id,
        provider="lmstudio",
        model="qwen2.5-coder",
        prompt="local run",
        is_local=True,
    )

    assert run.is_local is True
    assert run.to_dict()["is_local"] is True


def test_count_active_local_agents_uses_run_manager_is_local_flag(
    temp_db: HubDatabase,
) -> None:
    db = temp_db
    project = LocalProjectManager(db).create(name="agent-local-count-project")
    session = SessionManager(db).register(
        external_id="parent-count-ext",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="claude",
        project_id=project.id,
    )
    run_manager = LocalAgentRunManager(db)

    active_local = run_manager.create(
        parent_session_id=session.id,
        provider="endpoint:lm-studio",
        model="qwen2.5-coder",
        prompt="local run",
        is_local=True,
    )
    run_manager.create(
        parent_session_id=session.id,
        provider="claude",
        model="sonnet",
        prompt="remote run",
        is_local=False,
    )
    completed_local = run_manager.create(
        parent_session_id=session.id,
        provider="endpoint:lm-studio",
        model="qwen2.5-coder",
        prompt="finished local run",
        is_local=True,
    )
    run_manager.start(active_local.id)
    run_manager.start(completed_local.id)
    run_manager.complete(completed_local.id)

    assert count_active_local_agents(run_manager) == 1


def test_session_manager_persists_is_local_flag(temp_db: HubDatabase) -> None:
    db = temp_db
    project = LocalProjectManager(db).create(name="session-local-project")

    session = SessionManager(db).create_web_chat_session(
        machine_id="21000000-0000-4000-8000-000000000001",
        project_id=project.id,
        source="codex",
        title="Local session",
        model="qwen2.5-coder",
        is_local=True,
        sandbox_enabled=False,
        sandbox_policy_hash="",
    )

    assert session.is_local is True
    assert session.to_dict()["is_local"] is True
    reloaded = SessionManager(db).get(session.id)
    assert reloaded is not None
    assert reloaded.is_local is True


def test_session_row_preserves_null_flag_without_legacy_reclassification() -> None:
    row = {
        "id": "session-local-legacy",
        "external_id": "external-local-legacy",
        "machine_id": "21000000-0000-4000-8000-000000000001",
        "source": "lmstudio",
        "project_id": "project-1",
        "title": None,
        "title_source": None,
        "status": "active",
        "transcript_path": None,
        "summary_path": None,
        "summary_markdown": None,
        "git_branch": None,
        "parent_session_id": None,
        "created_at": "2026-04-08T12:00:00+00:00",
        "updated_at": "2026-04-08T12:00:00+00:00",
        "agent_depth": 0,
        "spawned_by_agent_id": None,
        "workflow_name": None,
        "agent_run_id": None,
        "context_injected": 0,
        "original_prompt": None,
        "usage_input_tokens": 0,
        "usage_output_tokens": 0,
        "usage_cache_creation_tokens": 0,
        "usage_cache_read_tokens": 0,
        "context_window": None,
        "model": "qwen2.5-coder",
        "is_local": None,
        "terminal_context": None,
        "seq_num": 1,
        "had_edits": 0,
        "digest_markdown": None,
        "last_turn_markdown": None,
        "chat_mode": "plan",
        "last_digest_input_hash": None,
        "message_count": 0,
        "turn_count": 0,
        "tool_call_count": 0,
        "last_assistant_content": None,
        "approved_tools_json": None,
        "session_type": "terminal",
        "sandbox_enabled": 0,
        "sandbox_policy_hash": None,
    }

    assert Session.from_row(row).is_local is False

    row["is_local"] = 1
    assert Session.from_row(row).is_local is True

    row["is_local"] = 0
    assert Session.from_row(row).is_local is False


def test_agent_run_row_preserves_null_flag_without_legacy_reclassification() -> None:
    row = {
        "id": "run-local-legacy",
        "parent_session_id": "parent-session",
        "machine_id": LOCAL_MACHINE_ID,
        "child_session_id": None,
        "claimed_session_id": None,
        "workflow_name": None,
        "agent_name": None,
        "provider": "lmstudio",
        "model": "qwen2.5-coder",
        "is_local": None,
        "requested_reasoning_effort": None,
        "effective_reasoning_effort": None,
        "reasoning_required": 0,
        "reasoning_status": "not_requested",
        "reasoning_message": None,
        "status": "pending",
        "prompt": "local run",
        "result": None,
        "error": None,
        "tool_calls_count": 0,
        "turns_used": 0,
        "started_at": None,
        "completed_at": None,
        "created_at": "2026-04-08T12:00:00+00:00",
        "updated_at": "2026-04-08T12:00:00+00:00",
        "sdk_session_id": None,
        "continuation_prompt": None,
        "task_id": None,
        "pid": None,
        "terminal_id": None,
        "worktree_id": None,
        "clone_id": None,
        "timeout_seconds": None,
        "terminal_reason": None,
    }

    assert AgentRun.from_row(row).is_local is False

    row["is_local"] = 1
    assert AgentRun.from_row(row).is_local is True

    row["is_local"] = 0
    assert AgentRun.from_row(row).is_local is False


def test_agent_selector_defaults_null_is_local_to_false() -> None:
    sql = _AgentRunSelectorMixin._select_runs_with_live_stats_sql()

    assert "COALESCE(ar.is_local, FALSE) AS is_local" in sql
    assert "lmstudio" not in sql
    assert "gpt-oss" not in sql
