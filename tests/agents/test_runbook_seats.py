"""Runbook seat guard (#23329): the read-only admission check a runbook runs before launching seats."""

from __future__ import annotations

import builtins
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.agents.runbook_seats import (
    CatalogueSeat,
    RunbookSeatRefusal,
    RunbookSeatStores,
    check_runbook_seats,
)
from gobby.mcp_proxy.tools import runbook_seat_tools
from gobby.mcp_proxy.tools.agents_registry import create_agents_registry
from gobby.mcp_proxy.tools.runbook_seat_tools import runbook_seat_stores
from gobby.storage.agents import ACTIVE_AGENT_RUN_STATUSES, AgentRun, LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.workspaces import WorkspaceManager
from gobby.utils.project_context import reset_project_context, set_project_context
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.pipeline_state import ExecutionStatus
from tests.agents.conftest import AGENT_TEST_MACHINE_ID

RUNBOOK = "deploy-runbook"
CATALOGUE: list[dict[str, Any]] = [
    {"name": "lead", "title": "Lead", "agent": "lead-agent"},
    {"name": "dev", "title": "Dev", "agent": "dev-agent"},
]


@dataclass(frozen=True)
class _Env:
    db: HubDatabase
    stores: RunbookSeatStores
    project_id: str
    workspace_id: str
    parent_session_id: str


def _install_agent(db: HubDatabase, name: str, project_id: str, *, enabled: bool = True) -> None:
    AgentDefinitionManager(db).create(
        name=name,
        definition_json={"name": name, "version": "1.0", "enabled": enabled},
        project_id=project_id,
        enabled=enabled,
    )


@pytest.fixture
def env(temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path) -> _Env:
    project_id = str(sample_project["id"])
    workspace = WorkspaceManager(temp_db).create(AGENT_TEST_MACHINE_ID, "runbook")[0]
    parent = SessionManager(temp_db).register(
        external_id="runbook-operator",
        machine_id=AGENT_TEST_MACHINE_ID,
        source="claude",
        project_id=project_id,
    )
    _install_agent(temp_db, "lead-agent", project_id)
    _install_agent(temp_db, "dev-agent", project_id)
    return _Env(
        db=temp_db,
        stores=runbook_seat_stores(temp_db, project_path=str(tmp_path)),
        project_id=project_id,
        workspace_id=workspace.id,
        parent_session_id=parent.id,
    )


def _pipeline_child(env: _Env, status: ExecutionStatus = ExecutionStatus.RUNNING) -> str:
    """Start an execution of the runbook and return its pipeline child session id."""
    executions = LocalPipelineExecutionManager(env.db, env.project_id)
    execution = executions.create_execution(RUNBOOK, project_id=env.project_id)
    child = SessionManager(env.db).register(
        external_id=f"pipeline-{execution.id}",
        machine_id=AGENT_TEST_MACHINE_ID,
        source="pipeline",
        project_id=env.project_id,
        parent_session_id=env.parent_session_id,
    )
    executions.update_execution_session(execution.id, child.id)
    executions.update_execution_status(execution.id, status)
    return child.id


def _run(env: _Env, status: str, *, title: str | None = None) -> AgentRun:
    """Create a run in ``status``; a ``title`` places it on that seat of the workspace."""
    placement = (
        {"placement": {"kind": "tab", "workspace_id": env.workspace_id, "title": title}}
        if title is not None
        else None
    )
    run = LocalAgentRunManager(env.db).create(
        parent_session_id=env.parent_session_id,
        provider="claude",
        prompt="seat",
        agent_name="lead-agent",
        resume_metadata_json=placement,
    )
    env.db.execute("UPDATE agent_runs SET status = %s WHERE id = %s", (status, run.id))
    return run


def _check(
    env: _Env,
    caller: str,
    requested: str = "lead,dev",
    catalogue: list[dict[str, Any]] = CATALOGUE,
) -> tuple[tuple[CatalogueSeat, ...], int]:
    admitted = check_runbook_seats(
        env.stores,
        caller_session_id=caller,
        workspace=env.workspace_id,
        requested=requested,
        catalogue=catalogue,
    )
    return admitted.seats, admitted.free_slots


@pytest.mark.parametrize("status", ACTIVE_AGENT_RUN_STATUSES)
def test_live_run_seat_refuses(env: _Env, status: str) -> None:
    caller = _pipeline_child(env)
    holder = _run(env, status, title="Lead")

    with pytest.raises(RunbookSeatRefusal, match=rf"seat 'lead'.*{holder.id}"):
        _check(env, caller)

    env.db.execute("UPDATE agent_runs SET status = 'completed' WHERE id = %s", (holder.id,))
    seats, _ = _check(env, caller)
    assert [seat.name for seat in seats] == ["lead", "dev"]


def test_seat_agent_definitions_resolved(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    caller = _pipeline_child(env)
    _install_agent(env.db, "off-agent", env.project_id, enabled=False)
    opened: list[str] = []
    real_open = builtins.open

    def recording_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", recording_open)
    missing = [CATALOGUE[0], {"name": "dev", "title": "Dev", "agent": "ghost-agent"}]
    disabled = [CATALOGUE[0], {"name": "dev", "title": "Dev", "agent": "off-agent"}]

    with pytest.raises(RunbookSeatRefusal, match=r"seat 'dev'.*'ghost-agent' is not installed"):
        _check(env, caller, catalogue=missing)
    with pytest.raises(RunbookSeatRefusal, match=r"seat 'dev'.*'off-agent' is disabled"):
        _check(env, caller, catalogue=disabled)
    seats, _ = _check(env, caller)

    assert [seat.agent for seat in seats] == ["lead-agent", "dev-agent"]
    assert [path for path in opened if ".gobby/roles" in path] == []


def test_concurrent_executions_admit_at_most_one(env: _Env) -> None:
    first = _pipeline_child(env)
    second = _pipeline_child(env, ExecutionStatus.PENDING)
    outcomes: list[str] = []

    for caller in (first, second):
        try:
            _check(env, caller)
        except RunbookSeatRefusal as refusal:
            outcomes.append(str(refusal))
        else:
            outcomes.append("admitted")

    assert outcomes.count("admitted") <= 1
    assert all(f"another '{RUNBOOK}' execution" in outcome for outcome in outcomes)


def _storage_error(env: _Env, monkeypatch: pytest.MonkeyPatch) -> str:
    def failing_list(*args: Any, **kwargs: Any) -> list[AgentRun]:
        raise RuntimeError("hub connection lost")

    monkeypatch.setattr(env.stores.runs, "list_by_status", failing_list)
    return _pipeline_child(env)


def _truncated_page(env: _Env, monkeypatch: pytest.MonkeyPatch) -> str:
    unplaced = _run(env, "completed")

    def full_page(*args: Any, limit: int = 100, **kwargs: Any) -> list[AgentRun]:
        return [unplaced] * limit

    monkeypatch.setattr(env.stores.runs, "list_by_status", full_page)
    return _pipeline_child(env)


def _not_pipeline_child(env: _Env, monkeypatch: pytest.MonkeyPatch) -> str:
    return env.parent_session_id


@pytest.mark.parametrize(
    ("arrange", "cause"),
    [
        (_storage_error, "hub connection lost"),
        (_truncated_page, "truncated"),
        (_not_pipeline_child, "not a pipeline child session"),
    ],
    ids=["storage-error", "truncated-page", "not-pipeline-child"],
)
def test_uncertain_lookup_fails_closed(
    env: _Env,
    monkeypatch: pytest.MonkeyPatch,
    arrange: Callable[[_Env, pytest.MonkeyPatch], str],
    cause: str,
) -> None:
    caller = arrange(env, monkeypatch)

    with pytest.raises(RunbookSeatRefusal, match=cause):
        _check(env, caller)


def test_capacity_shortfall_refuses(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runbook_seat_tools, "max_active_agents_for_project", lambda path: 2)
    caller = _pipeline_child(env)
    _run(env, "running")

    with pytest.raises(RunbookSeatRefusal, match=r"1 free agent slot.*2 seats"):
        _check(env, caller)
    seats, free_slots = _check(env, caller, requested="lead")

    assert ([seat.name for seat in seats], free_slots) == (["lead"], 1)


_LONG = "x" * 1024


@pytest.mark.parametrize(
    ("requested", "catalogue", "cause"),
    [
        ("", CATALOGUE, "no seats requested"),
        ("lead,lead", CATALOGUE, "seat 'lead' is requested twice"),
        ("lead,ghost", CATALOGUE, "seat 'ghost' is not in the catalogue"),
        ("lead, dev", CATALOGUE, "seat ' dev' is padded with whitespace"),
        (
            "lead,dev",
            [
                {"name": "lead", "title": f"{_LONG}a", "agent": "lead-agent"},
                {"name": "dev", "title": f"{_LONG}b", "agent": "dev-agent"},
            ],
            "seats 'lead' and 'dev' share one seat title",
        ),
    ],
    ids=["empty", "duplicate", "unknown", "whitespace-padded", "one-canonical-key"],
)
def test_requested_seats_validated(
    env: _Env,
    monkeypatch: pytest.MonkeyPatch,
    requested: str,
    catalogue: list[dict[str, Any]],
    cause: str,
) -> None:
    caller = _pipeline_child(env)
    lookups: list[object] = []

    def recording_list(*args: Any, **kwargs: Any) -> list[AgentRun]:
        lookups.append(kwargs)
        return []

    monkeypatch.setattr(env.stores.runs, "list_by_status", recording_list)

    with pytest.raises(RunbookSeatRefusal, match=cause):
        _check(env, caller, requested=requested, catalogue=catalogue)
    assert lookups == []


def test_registered_tool_replies_in_mcp_step_shape(
    env: _Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runbook_seat_tools, "max_active_agents_for_project", lambda path: 5)
    registry = create_agents_registry(
        MagicMock(), session_manager=SessionManager(env.db), db=env.db
    )
    caller = _pipeline_child(env)
    project_token = set_project_context({"id": env.project_id, "project_path": str(tmp_path)})
    try:
        with session_context_for_test(caller):
            admitted = registry.call_sync(
                "check_runbook_seats",
                {"workspace": env.workspace_id, "requested": "lead", "catalogue": CATALOGUE},
            )
            refused = registry.call_sync(
                "check_runbook_seats",
                {"workspace": env.workspace_id, "requested": "ghost", "catalogue": CATALOGUE},
            )
    finally:
        reset_project_context(project_token)

    assert admitted == {
        "success": True,
        "workspace_id": env.workspace_id,
        "seats": ({"name": "lead", "title": "Lead", "agent": "lead-agent"},),
        "free_slots": 5,
    }
    assert refused == {"success": False, "error": "seat 'ghost' is not in the catalogue"}
