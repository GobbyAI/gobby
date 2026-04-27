"""Red tests for persisted local-model flags on sessions and agent runs."""

from __future__ import annotations

import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.projects import LocalProjectManager
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
    )

    assert session["is_local"] is True
    assert SessionManager(db).get(session["id"]).is_local is True
