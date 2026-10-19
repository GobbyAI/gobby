"""A drained seat keeps its developer handoff binding through idle expiry and resume."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.spawn_agent import _implementation as impl
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._automation import sweep_stale_claims
from gobby.storage.tasks._manager import LocalTaskManager
from gobby.storage.tasks._transitions import claim_task
from gobby.storage.terminals import TerminalManager, native_locator_key
from gobby.utils.project_context import reset_project_context, set_project_context
from gobby.workflows.agent_resolver import resolve_agent_with_row
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.reserved_variables import DEVELOPER_HANDOFF_TASKS_VARIABLE
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import AgentStepInstanceManager, build_step_instance
from tests.mcp_proxy.tools.spawn_agent.test_placement import (
    LOCAL_MACHINE_ID,
    _build,
    _Harness,
    _held_seat,
    _spawn,
)
from tests.mcp_proxy.tools.spawn_agent.test_resume_seat import (
    _ended_run,
    _seat_session,
    _split_snapshot,
)
from tests.terminals.fakes import FakeRuntime

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_srt_verifier")]

_DEVELOPER = (
    Path(__file__).resolve().parents[4] / "src/gobby/install/shared/workflows/agents/developer.yaml"
)
_EXITED_EPOCH = "host-epoch-before-drain"


@pytest.fixture(autouse=True)
def _local_machine_identity(
    monkeypatch: pytest.MonkeyPatch, _mock_spawn_machine_id: None
) -> Iterator[None]:
    monkeypatch.setattr(impl, "get_machine_id", lambda: LOCAL_MACHINE_ID)
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def placed(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _Harness:
    return _build(
        db=temp_db,
        project=sample_project,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        runtimes=(FakeRuntime(backend="native"),),
    )


def _seed_developer_seat(h: _Harness, session_id: str, task_id: str, ref: str, step: str) -> None:
    """Persist a bound developer instance; the handoff binding is its step variable."""
    body = AgentDefinitionBody.model_validate(yaml.safe_load(_DEVELOPER.read_text("utf-8")))
    assert body.step_workflow is not None
    variables = dict(body.step_workflow.variables)
    variables.update(
        task_claimed=True,
        assigned_task_id=task_id,
        assigned_task_ref=ref,
        skills_routed=True,
        required_skills_loaded=True,
        additional_skills_loaded=True,
    )
    variables[DEVELOPER_HANDOFF_TASKS_VARIABLE] = [task_id]
    instance = build_step_instance(
        body,
        session_id=session_id,
        step_workflow_id=None,
        variables=variables,
        current_step=step,
    )
    AgentStepInstanceManager(h.db).save(instance)


def _define_developer(db: HubDatabase) -> None:
    """Store the developer definition so resume resolves its step workflow, not the default agent."""
    raw = yaml.safe_load(_DEVELOPER.read_text("utf-8"))
    AgentDefinitionManager(db).upsert_with_steps(
        "developer", raw, raw["step_workflow"], source="custom"
    )


def _expire_through_idle_path(h: _Harness, session_id: str, project_id: str) -> None:
    """Expire a paused seat whose native pane exited, as the liveness monitor does."""
    terminals = TerminalManager(h.db)
    terminal_id = str(uuid.uuid4())
    host_terminal_id = f"host-{terminal_id[:8]}"
    h.sessions.update(session_id, terminal_context={"gobby_terminal_id": terminal_id})
    assert terminals.create_pending(
        terminal_id=terminal_id,
        project_id=project_id,
        backend="native",
        ownership="gobby",
        spawn_key=terminal_id,
        machine_id=LOCAL_MACHINE_ID,
        session_id=session_id,
    )
    assert terminals.promote_to_live(
        terminal_id,
        locator={"host_terminal_id": host_terminal_id},
        locator_key=native_locator_key(_EXITED_EPOCH, host_terminal_id),
        host_epoch=_EXITED_EPOCH,
    )
    assert terminals.mark_exited(terminal_id) is not None
    paused = h.sessions.get(session_id)
    assert paused is not None
    expired = h.sessions.expire_if_paused_terminal_exited(
        session_id,
        terminal_id=terminal_id,
        machine_id=LOCAL_MACHINE_ID,
        observed_updated_at=paused.updated_at,
        live_host_epoch=None,
    )
    assert expired is not None
    assert expired.status == "expired"


def _close_preview(session_id: str, ref: str) -> HookEvent:
    """A BEFORE_TOOL close_task preview routed through the gobby-tasks proxy."""
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=session_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "close_task",
                "arguments": {
                    "task_id": ref,
                    "preview": True,
                    "changes_summary": "Landed and released.",
                },
            },
        },
        metadata={},
    )


async def test_resumed_drained_seat_close_preview_is_not_refused(
    placed: _Harness, sample_project: dict[str, Any]
) -> None:
    h = placed
    lane = _held_seat(h, "Lane 1", state="exited")
    session = _seat_session(
        h,
        _ended_run(h, _split_snapshot(h, beside=str(uuid.uuid4()), tab_id=lane.tab_id)),
        status="paused",
    )
    task = LocalTaskManager(h.db).create_task(
        sample_project["id"],
        title="Drained seat handoff",
        validation_criteria="The resumed seat finishes its handoff.",
    )
    ref = f"#{task.seq_num}"
    claim_task(h.db, task.id, session.id)
    SessionVariableManager(h.db).merge_variables(session.id, {"is_spawned_agent": True})
    _seed_developer_seat(h, session.id, task.id, ref, "claim")
    _define_developer(h.db)
    resolved = resolve_agent_with_row(
        "developer", h.db, cli_source="claude", project_id=sample_project["id"]
    )
    assert resolved is not None
    developer_body = resolved[0]
    assert developer_body.step_workflow is not None

    _expire_through_idle_path(h, session.id, sample_project["id"])
    assert sweep_stale_claims(h.db, project_id=sample_project["id"]) == 0

    seated = AgentStepInstanceManager(h.db).get_for_session(session.id)
    assert seated is not None
    token = set_project_context(sample_project)
    try:
        reply = await _spawn(h, None, resume_session_id=session.id, agent_body=developer_body)
        await asyncio.gather(*list(impl._spawn_background_tasks.values()))
        assert reply["success"] is True, reply
        assert LocalTaskManager(h.db).get_task(task.id).claimed_by_session_id == session.id
        assert sweep_stale_claims(h.db, project_id=sample_project["id"]) == 0
        inst = AgentStepInstanceManager(h.db).get_for_session(session.id)
        assert inst is not None
        assert inst.current_step == "claim"
        # A rewrite of the seated instance stamps a new updated_at; resume must leave it alone.
        assert inst.updated_at == seated.updated_at
        assert inst.variables[DEVELOPER_HANDOFF_TASKS_VARIABLE] == [task.id]
        response = RuleEngine(h.db)._check_step_tool_enforcement(
            _close_preview(session.id, ref), session.id, {}
        )
    finally:
        reset_project_context(token)
    assert response is None, response
