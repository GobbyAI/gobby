"""Runbook seat guard (#23329): the read-only admission check a runbook runs before launching seats."""

from __future__ import annotations

import builtins
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.agents.runbook_seats import (
    READ_BOUND,
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
def env(temp_db: HubDatabase, sample_project: dict[str, Any]) -> _Env:
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
        stores=runbook_seat_stores(temp_db),
        project_id=project_id,
        workspace_id=workspace.id,
        parent_session_id=parent.id,
    )


def _pipeline_child(
    env: _Env,
    status: ExecutionStatus = ExecutionStatus.RUNNING,
    *,
    workspace: str | None = None,
    machine_id: str = AGENT_TEST_MACHINE_ID,
) -> str:
    """Start an execution of the runbook in ``workspace`` on ``machine_id``; return its child session."""
    executions = LocalPipelineExecutionManager(env.db, env.project_id)
    execution = executions.create_execution(
        RUNBOOK,
        inputs_json=json.dumps({"workspace": workspace or env.workspace_id}),
        project_id=env.project_id,
    )
    child = SessionManager(env.db).register(
        external_id=f"pipeline-{execution.id}",
        machine_id=AGENT_TEST_MACHINE_ID,
        source="pipeline",
        project_id=env.project_id,
        parent_session_id=env.parent_session_id,
    )
    if machine_id != AGENT_TEST_MACHINE_ID:
        env.db.execute("UPDATE sessions SET machine_id = %s WHERE id = %s", (machine_id, child.id))
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
) -> tuple[CatalogueSeat, ...]:
    admitted = check_runbook_seats(
        env.stores,
        caller_session_id=caller,
        workspace=env.workspace_id,
        requested=requested,
        catalogue=catalogue,
    )
    return admitted.seats


@pytest.mark.parametrize("status", ACTIVE_AGENT_RUN_STATUSES)
def test_live_run_seat_admits(env: _Env, status: str) -> None:
    caller = _pipeline_child(env)
    _run(env, status, title="Lead")

    seats = _check(env, caller)

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
    seats = _check(env, caller)

    assert [seat.agent for seat in seats] == ["lead-agent", "dev-agent"]
    assert [path for path in opened if ".gobby/roles" in path] == []


@pytest.mark.parametrize(
    ("place", "refused"),
    [("same-place", True), ("other-machine", False), ("other-workspace", False)],
)
def test_runbook_refuses_only_a_launch_in_the_same_place(
    env: _Env, place: str, refused: bool
) -> None:
    if place == "other-machine":
        _pipeline_child(env, machine_id="21000000-0000-4000-8000-000000000002")
    elif place == "other-workspace":
        other = WorkspaceManager(env.db).create(AGENT_TEST_MACHINE_ID, "second-pod")[0]
        _pipeline_child(env, workspace=other.id)
    else:
        _pipeline_child(env)
    caller = _pipeline_child(env)

    if refused:
        with pytest.raises(RunbookSeatRefusal, match=f"another '{RUNBOOK}' execution is live"):
            _check(env, caller)
    else:
        seats = _check(env, caller)
        assert [seat.name for seat in seats] == ["lead", "dev"]


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
    def failing_list(*args: Any, **kwargs: Any) -> list[object]:
        raise RuntimeError("hub connection lost")

    monkeypatch.setattr(LocalPipelineExecutionManager, "list_executions", failing_list)
    return _pipeline_child(env)


def _truncated_page(env: _Env, monkeypatch: pytest.MonkeyPatch) -> str:
    caller = _pipeline_child(env)
    real_list_executions = LocalPipelineExecutionManager.list_executions

    def full_page(
        self: LocalPipelineExecutionManager, *, limit: int, **kwargs: Any
    ) -> list[object]:
        rows: list[object] = list(real_list_executions(self, limit=limit, **kwargs))
        return (rows * limit)[:limit]

    monkeypatch.setattr(LocalPipelineExecutionManager, "list_executions", full_page)
    return caller


def _not_pipeline_child(env: _Env, monkeypatch: pytest.MonkeyPatch) -> str:
    return env.parent_session_id


def _sibling_without_session(env: _Env, monkeypatch: pytest.MonkeyPatch) -> str:
    executions = LocalPipelineExecutionManager(env.db, env.project_id)
    sibling = executions.create_execution(
        RUNBOOK,
        inputs_json=json.dumps({"workspace": env.workspace_id}),
        project_id=env.project_id,
    )
    executions.update_execution_status(sibling.id, ExecutionStatus.PENDING)
    return _pipeline_child(env)


def _sibling_without_workspace(env: _Env, monkeypatch: pytest.MonkeyPatch) -> str:
    sibling = _pipeline_child(env)
    env.db.execute(
        "UPDATE pipeline_executions SET inputs_json = %s WHERE session_id = %s",
        (json.dumps({}), sibling),
    )
    return _pipeline_child(env)


@pytest.mark.parametrize(
    ("arrange", "cause"),
    [
        (_storage_error, "hub connection lost"),
        (_truncated_page, "truncated"),
        (_not_pipeline_child, "not a pipeline child session"),
        (_sibling_without_session, "has no pipeline child session"),
        (_sibling_without_workspace, "has no workspace input"),
    ],
    ids=[
        "storage-error",
        "truncated-page",
        "not-pipeline-child",
        "sibling-without-session",
        "sibling-without-workspace",
    ],
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


def test_read_bound_refuses_only_past_a_full_page(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    caller = _pipeline_child(env)
    count = READ_BOUND
    real_list_executions = LocalPipelineExecutionManager.list_executions

    def repeated_executions(
        self: LocalPipelineExecutionManager, *, limit: int, **kwargs: Any
    ) -> list[object]:
        rows: list[object] = list(real_list_executions(self, limit=limit, **kwargs))
        return (rows * count)[: min(limit, count)]

    monkeypatch.setattr(LocalPipelineExecutionManager, "list_executions", repeated_executions)

    seats = _check(env, caller)
    assert [seat.name for seat in seats] == ["lead", "dev"]

    count = READ_BOUND + 1
    with pytest.raises(RunbookSeatRefusal, match=f"truncated at {READ_BOUND} rows"):
        _check(env, caller)


def test_slot_capacity_not_enforced(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    # A zero agent cap must not refuse: runbooks do not enforce slots.
    monkeypatch.setattr(
        runbook_seat_tools, "max_active_agents_for_project", lambda path: 0, raising=False
    )
    caller = _pipeline_child(env)
    _run(env, "running")

    seats = _check(env, caller)

    assert [seat.name for seat in seats] == ["lead", "dev"]


def test_seats_sharing_a_title_admit(env: _Env) -> None:
    caller = _pipeline_child(env)
    researchers = [
        {"name": "researcher-1", "title": "Researcher", "agent": "lead-agent"},
        {"name": "researcher-2", "title": "Researcher", "agent": "lead-agent"},
    ]

    seats = _check(env, caller, requested="researcher-1,researcher-2", catalogue=researchers)

    assert [seat.name for seat in seats] == ["researcher-1", "researcher-2"]


@pytest.mark.parametrize(
    ("requested", "cause"),
    [
        ("", "no seats requested"),
        ("lead,lead", "seat 'lead' is requested twice"),
        ("lead,ghost", "seat 'ghost' is not in the catalogue"),
        ("lead, dev", "seat ' dev' is padded with whitespace"),
    ],
    ids=["empty", "duplicate", "unknown", "whitespace-padded"],
)
def test_requested_seats_validated(
    env: _Env,
    monkeypatch: pytest.MonkeyPatch,
    requested: str,
    cause: str,
) -> None:
    caller = _pipeline_child(env)
    lookups: list[object] = []

    def recording_list(*args: Any, **kwargs: Any) -> list[object]:
        lookups.append(kwargs)
        return []

    monkeypatch.setattr(LocalPipelineExecutionManager, "list_executions", recording_list)

    with pytest.raises(RunbookSeatRefusal, match=cause):
        _check(env, caller, requested=requested)
    assert lookups == []


def test_registered_tool_replies_in_mcp_step_shape(env: _Env) -> None:
    registry = create_agents_registry(
        MagicMock(), session_manager=SessionManager(env.db), db=env.db
    )
    caller = _pipeline_child(env)
    with session_context_for_test(caller):
        admitted = registry.call_sync(
            "check_runbook_seats",
            {"workspace": env.workspace_id, "requested": "lead", "catalogue": CATALOGUE},
        )
        refused = registry.call_sync(
            "check_runbook_seats",
            {"workspace": env.workspace_id, "requested": "ghost", "catalogue": CATALOGUE},
        )

    assert admitted == {
        "success": True,
        "workspace_id": env.workspace_id,
        "seats": ({"name": "lead", "title": "Lead", "agent": "lead-agent"},),
    }
    assert refused == {"success": False, "error": "seat 'ghost' is not in the catalogue"}
