"""Red tests for persisted local-model flags on sessions and agent runs."""

from __future__ import annotations

import pytest

from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


def test_baseline_schema_includes_local_flag_columns(tmp_path) -> None:
    db = LocalDatabase(tmp_path / "local-flags.db")
    run_migrations(db)

    agent_columns = {row["name"] for row in db.fetchall("PRAGMA table_info(agent_runs)")}
    session_columns = {row["name"] for row in db.fetchall("PRAGMA table_info(sessions)")}

    assert "is_local" in agent_columns
    assert "is_local" in session_columns


def test_agent_run_manager_persists_is_local_flag(tmp_path) -> None:
    db = LocalDatabase(tmp_path / "agent-local.db")
    run_migrations(db)
    project = LocalProjectManager(db).create(name="agent-local-project")
    session = SessionManager(db).register(
        external_id="parent-ext",
        machine_id="machine-1",
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


def test_session_manager_persists_is_local_flag(tmp_path) -> None:
    db = LocalDatabase(tmp_path / "session-local.db")
    run_migrations(db)
    project = LocalProjectManager(db).create(name="session-local-project")

    session = SessionManager(db).create_web_chat_session(
        machine_id="machine-1",
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


def test_session_row_uses_legacy_local_fallback_when_flag_is_null() -> None:
    row = {
        "id": "session-local-legacy",
        "external_id": "external-local-legacy",
        "machine_id": "machine-1",
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

    assert Session.from_row(row).is_local is True

    row["is_local"] = 0
    assert Session.from_row(row).is_local is False


def test_agent_run_row_uses_legacy_local_fallback_when_flag_is_null() -> None:
    row = {
        "id": "run-local-legacy",
        "parent_session_id": "parent-session",
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
        "tmux_session_name": None,
        "worktree_id": None,
        "clone_id": None,
        "timeout_seconds": None,
        "terminal_reason": None,
    }

    assert AgentRun.from_row(row).is_local is True

    row["is_local"] = 0
    assert AgentRun.from_row(row).is_local is False
