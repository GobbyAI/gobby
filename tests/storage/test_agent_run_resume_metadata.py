from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager

pytestmark = pytest.mark.unit


def test_agent_run_persists_resume_metadata(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
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
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddddd10",
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
        "dddddddd-dddd-4ddd-8ddd-dddddddddd10",
        {"provider": "codex", "sandbox_args": ["--sandbox"]},
    )

    assert updated is not None
    assert updated.resume_metadata_json == {
        "provider": "codex",
        "sandbox_args": ["--sandbox"],
    }
    assert updated.to_dict()["resume_metadata_json"] == updated.resume_metadata_json
    assert updated.to_brief()["resume_metadata_json"] == updated.resume_metadata_json


def test_agent_run_accepts_none_resume_metadata(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
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
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddddd11",
        resume_metadata_json=None,
    )

    assert created.resume_metadata_json is None
    assert created.to_dict()["resume_metadata_json"] is None
    assert created.to_brief()["resume_metadata_json"] is None


def test_update_resume_metadata_returns_none_for_missing_run(temp_db: HubDatabase) -> None:
    manager = LocalAgentRunManager(temp_db)

    assert (
        manager.update_resume_metadata(
            "00000000-0000-0000-0000-0000000000ff", {"provider": "codex"}
        )
        is None
    )


def test_daemon_stop_resume_candidates_exclude_consumed_and_expired_runs(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    session = SessionManager(temp_db).register(
        external_id="parent-resume-candidates",
        machine_id="machine-1",
        source="test",
        project_id=sample_project["id"],
    )
    manager = LocalAgentRunManager(temp_db)
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Resume candidate filtering",
        validation_criteria="Only recent unconsumed daemon-stop runs are candidates.",
    )

    recent = manager.create(
        parent_session_id=session.id,
        provider="codex",
        prompt="recent",
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddddd12",
        task_id=task.id,
        resume_metadata_json={"provider": "codex"},
    )
    consumed = manager.create(
        parent_session_id=session.id,
        provider="codex",
        prompt="consumed",
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddddd13",
        task_id=task.id,
        resume_metadata_json={
            "provider": "codex",
            "daemon_stop_resume_consumed_at": "2026-06-01T00:00:00+00:00",
        },
    )
    expired = manager.create(
        parent_session_id=session.id,
        provider="codex",
        prompt="expired",
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddddd14",
        task_id=task.id,
        resume_metadata_json={"provider": "codex"},
    )

    for run in (recent, consumed, expired):
        manager.start(run.id)
        manager.cancel(run.id, terminal_reason="daemon_stop")

    old_timestamp = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    temp_db.execute(
        "UPDATE agent_runs SET completed_at = %s, updated_at = %s WHERE id = %s",
        (old_timestamp, old_timestamp, expired.id),
    )

    candidates = manager.list_daemon_stop_resume_candidates(task.id, max_age_hours=24)

    assert [candidate.id for candidate in candidates] == [recent.id]
