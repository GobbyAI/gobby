from __future__ import annotations

import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


def test_agent_run_persists_resume_metadata(temp_db, sample_project) -> None:
    session = SessionManager(temp_db).register(
        external_id="parent-resume-metadata",
        machine_id="machine-1",
        source="test",
        project_id=sample_project["id"],
    )
    manager = LocalAgentRunManager(temp_db)

    created = manager.create(
        parent_session_id=session.id,
        provider="codex",
        prompt="work",
        run_id="run-resume-meta",
        resume_metadata_json={
            "provider": "codex",
            "cwd": "/tmp/worktree",
            "initial_variables": {"stage_name": "development"},
        },
    )

    assert created.resume_metadata_json == {
        "provider": "codex",
        "cwd": "/tmp/worktree",
        "initial_variables": {"stage_name": "development"},
    }

    updated = manager.update_resume_metadata(
        "run-resume-meta",
        {"provider": "codex", "sandbox_args": ["--sandbox"]},
    )

    assert updated is not None
    assert updated.resume_metadata_json == {
        "provider": "codex",
        "sandbox_args": ["--sandbox"],
    }
    assert updated.to_dict()["resume_metadata_json"] == updated.resume_metadata_json
    assert updated.to_brief()["resume_metadata_json"] == updated.resume_metadata_json


def test_agent_run_accepts_none_resume_metadata(temp_db, sample_project) -> None:
    session = SessionManager(temp_db).register(
        external_id="parent-resume-metadata-none",
        machine_id="machine-1",
        source="test",
        project_id=sample_project["id"],
    )
    manager = LocalAgentRunManager(temp_db)

    created = manager.create(
        parent_session_id=session.id,
        provider="codex",
        prompt="work",
        run_id="run-resume-meta-none",
        resume_metadata_json=None,
    )

    assert created.resume_metadata_json is None
    assert created.to_dict()["resume_metadata_json"] is None
    assert created.to_brief()["resume_metadata_json"] is None


def test_update_resume_metadata_returns_none_for_missing_run(temp_db) -> None:
    manager = LocalAgentRunManager(temp_db)

    assert manager.update_resume_metadata("missing-run", {"provider": "codex"}) is None
