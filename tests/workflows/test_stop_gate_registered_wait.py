"""A claimed task's stop gate accepts a live registered wait_for_agent as progress (#25176)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.storage.agent_resume import register_daemon_resume_waiter
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipeline_subscribers import CompletionSubscriberManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.found_work_gate import FoundWorkStopFacts
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_path
from tests.fixtures.isolated_checkout import patch_local_machine_id

pytestmark = pytest.mark.integration

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
_RULE = "require-task-close"
_STOP_GATE = get_bundled_rules_path() / "stop-gates" / "require-task-close.yaml"


def _insert_claimed_task_gate(db: HubDatabase) -> None:
    """Install the shipped claimed-task gate's own condition, so the test pins that rule."""
    data: Any = yaml.safe_load(_STOP_GATE.read_text(encoding="utf-8"))
    RuleDefinitionManager(db).create(
        name=_RULE,
        definition_json=RuleDefinitionBody(
            event=RuleTriggerEvent.TURN_END,
            when=str(data["rules"][_RULE]["when"]),
            effects=[RuleEffect(type="block", reason=f"{_RULE} blocked turn end")],
        ).model_dump(mode="json"),
        priority=50,
    )


def _claimed_seat(
    db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> tuple[str, str]:
    """A Claude Code seat holding one claimed task; returns (session id, external id)."""
    external_id = "stop-gate-registered-wait"
    with pytest.MonkeyPatch.context() as identity_patch:
        patch_local_machine_id(identity_patch, LOCAL_MACHINE_ID)
        session = session_manager.register(
            external_id=external_id,
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=str(sample_project["id"]),
        )
    task = LocalTaskManager(db).create_task(
        project_id=str(sample_project["id"]),
        title="Hold the claimed task through a registered wait",
        claimed_by_session_id=session.id,
        validation_criteria="A live wait_for_agent keeps the stop gate quiet.",
    )
    _insert_claimed_task_gate(db)
    SessionVariableManager(db).merge_variables(
        session.id,
        {
            "_agent_type": "default",
            "_variable_defaults_loaded": True,
            "_memory_initial_stop_checked": True,
            "claimed_tasks": {task.id: f"#{task.seq_num}"},
            "mode_level": 1,
            "session_edited_files": [],
            "stop_attempts": 0,
            "task_claimed": True,
        },
    )
    (tmp_path / "claude.jsonl").write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "go"}}) + "\n",
        encoding="utf-8",
    )
    return session.id, external_id


def _running_agent_run(db: HubDatabase, session_id: str) -> str:
    """A reviewer run the claimed session waits on, started as a live run."""
    with pytest.MonkeyPatch.context() as identity_patch:
        patch_local_machine_id(identity_patch, LOCAL_MACHINE_ID)
        run_manager = LocalAgentRunManager(db)
        run = run_manager.create(
            parent_session_id=session_id,
            provider="claude",
            prompt="review the claimed task",
        )
        started = run_manager.start(run.id)
    assert started is not None
    return run.id


async def _stop(
    db: HubDatabase,
    session_id: str,
    external_id: str,
    project_id: str,
    project_path: Path,
) -> HookResponse:
    handler = WorkflowHookHandler(rule_engine=RuleEngine(db))
    event = HookEvent(
        event_type=HookEventType.STOP,
        session_id=external_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        cwd=str(project_path),
        project_id=project_id,
        data={"transcript_path": str(project_path / "claude.jsonl")},
        metadata={"_platform_session_id": session_id, "project_path": str(project_path)},
    )
    with (
        patch.object(
            handler._found_work_analyzer,
            "analyze",
            AsyncMock(return_value=FoundWorkStopFacts()),
        ),
        patch.object(handler._found_work_analyzer, "unclaimed_found_work", return_value=()),
    ):
        return await handler._evaluate_rules(event)


async def test_live_registered_wait_lets_a_claimed_task_turn_end(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    """A session parked on a live wait_for_agent run is not held by the claimed-task gate."""
    session_id, external_id = _claimed_seat(temp_db, session_manager, sample_project, tmp_path)
    run_id = _running_agent_run(temp_db, session_id)
    CompletionSubscriberManager(temp_db).add_completion_subscriber(run_id, session_id)

    response = await _stop(temp_db, session_id, external_id, str(sample_project["id"]), tmp_path)

    assert response.decision == "allow", response.reason


async def test_daemon_recovery_wait_lets_a_claimed_task_turn_end(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    """A wait parked on a run awaiting daemon-restart recovery is still live: the gate lets go."""
    session_id, external_id = _claimed_seat(temp_db, session_manager, sample_project, tmp_path)
    run_id = _running_agent_run(temp_db, session_id)
    LocalAgentRunManager(temp_db).cancel(run_id, terminal_reason="daemon_stop")
    register_daemon_resume_waiter(temp_db, run_id=run_id, subscriber_session_id=session_id)

    response = await _stop(temp_db, session_id, external_id, str(sample_project["id"]), tmp_path)

    assert response.decision == "allow", response.reason


async def test_claimed_task_gate_holds_again_once_the_wait_resolves(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    """When the awaited run completes the wait is no longer live, so the gate holds."""
    session_id, external_id = _claimed_seat(temp_db, session_manager, sample_project, tmp_path)
    run_id = _running_agent_run(temp_db, session_id)
    CompletionSubscriberManager(temp_db).add_completion_subscriber(run_id, session_id)
    LocalAgentRunManager(temp_db).complete(run_id, result="done")

    response = await _stop(temp_db, session_id, external_id, str(sample_project["id"]), tmp_path)

    assert response.decision == "block"
    assert _RULE in (response.reason or "")


async def test_claimed_task_without_a_registered_wait_is_held(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    tmp_path: Path,
) -> None:
    """A claimed session with no registered wait still gets the claimed-task block."""
    session_id, external_id = _claimed_seat(temp_db, session_manager, sample_project, tmp_path)

    response = await _stop(temp_db, session_id, external_id, str(sample_project["id"]), tmp_path)

    assert response.decision == "block"
    assert _RULE in (response.reason or "")
