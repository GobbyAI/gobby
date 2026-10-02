"""Pipeline seat spawns reconcile by step invocation id (deploy-runbook 7.2)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.spawn_agent import _factory, create_spawn_agent_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import TerminalManager, mint_terminal_id
from gobby.storage.workspaces import WorkspaceManager
from gobby.tasks.agentic_close_review import TASK_CLOSE_REVIEWER_AGENT
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import (
    SessionContext,
    reset_session_context,
    set_session_context,
)
from gobby.workflows.definitions import PipelineDefinition, PipelineStep
from gobby.workflows.pipeline_state import ExecutionStatus
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit

PLACEMENT = {"kind": "tab", "title": "L3"}
DEFINITION = PipelineDefinition(
    name="runbook",
    steps=[PipelineStep(id="seat_a", exec="true"), PipelineStep(id="seat_b", exec="true")],
)


def _invocation_id(execution_id: str, step_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"gobby-pipeline:{execution_id}:{step_id}"))


@dataclass
class _Harness:
    db: HubDatabase
    sessions: SessionManager
    runs: LocalAgentRunManager
    workspaces: WorkspaceManager
    terminals: TerminalManager
    machine_id: str
    project_id: str
    execution_id: str
    child: Session
    reserver: MagicMock
    registry: Any
    launches: list[dict[str, Any]] = field(default_factory=list)

    def step_id(self, step: str) -> str:
        return _invocation_id(self.execution_id, step)

    def pipeline_session(self, execution_id: str, project_id: str | None = None) -> Session:
        return self.sessions.register(
            external_id=f"pipeline-{execution_id}",
            machine_id=self.machine_id,
            source="pipeline",
            project_id=project_id or self.project_id,
        )

    def execution(self, status: ExecutionStatus, project_id: str | None = None) -> str:
        manager = LocalPipelineExecutionManager(self.db, project_id or self.project_id)
        execution = manager.create_execution(
            "runbook",
            "{}",
            definition_json=DEFINITION.model_dump_json(),
            project_id=project_id or self.project_id,
        )
        manager.update_execution_status(execution_id=execution.id, status=status)
        return execution.id

    def run(self, run_id: str, parent: Session, *, started: bool) -> str:
        """A run with ``run_id``; a started one holds a terminal, as a launched seat does."""
        self.runs.create(
            parent_session_id=parent.id, provider="claude", prompt="Run the seat", run_id=run_id
        )
        if started:
            terminal_id = mint_terminal_id()
            self.terminals.create_pending(
                terminal_id,
                self.project_id,
                "tmux",
                "gobby",
                terminal_id,
                machine_id=self.machine_id,
            )
            self.runs.start(run_id)
            self.runs.update_runtime(run_id, terminal_id=terminal_id)
        return run_id

    def seat_pane(self, terminal_id: str, title: str = "L3") -> tuple[str, str]:
        """Bind ``terminal_id`` into a new tab; return its tab and pane ids."""
        workspace, _ = self.workspaces.create(self.machine_id, "agents")
        change = self.workspaces.create_tab(
            workspace.id, pane_id=str(uuid.uuid4()), project_id=self.project_id, title=title
        )
        pane = self.workspaces.set_pane_terminal(
            change.panes[0].id, terminal_id, owns_terminal=True
        )
        assert pane is not None
        return change.tabs[0].id, pane.id

    def refs(self, terminal_id: str) -> dict[str, str]:
        """The fresh-placement refs of the pane holding ``terminal_id``."""
        pane = self.workspaces.get_pane_for_terminal(terminal_id)
        assert pane is not None
        tab = next(
            tab
            for workspace in self.workspaces.list_for_node(self.machine_id)
            for tab in self.workspaces.list_tabs(workspace.id)
            if tab.id == pane.tab_id
        )
        workspace = self.workspaces.get(tab.workspace_id)
        assert workspace is not None
        node = self.workspaces.resolve_node(self.machine_id).ref
        prefix = f"{node}:{workspace.ref}"
        return {
            "workspace": prefix,
            "tab_ref": f"{prefix}:{tab.ref}",
            "pane_ref": f"{prefix}:{tab.ref}:{pane.ref}",
        }

    async def spawn(self, caller: Session, **overrides: Any) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "prompt": "Run the seat",
            "parent_session_id": self.child.id,
            "placement": PLACEMENT,
        }
        arguments.update(overrides)
        token = set_session_context(SessionContext(session_id=caller.id))
        try:
            result: dict[str, Any] = await self.registry._tools["spawn_agent"].func(**arguments)
        finally:
            reset_session_context(token)
        return result


@pytest.fixture
def seat(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _Harness:
    machine_id = require_machine_id()
    LocalMachineManager(temp_db).upsert_seen(machine_id, TEST_USER_ID, hostname="local")
    project_id = str(sample_project["id"])
    sessions = SessionManager(temp_db)
    reserver = MagicMock()
    h = _Harness(
        db=temp_db,
        sessions=sessions,
        runs=LocalAgentRunManager(temp_db),
        workspaces=WorkspaceManager(temp_db),
        terminals=TerminalManager(temp_db),
        machine_id=machine_id,
        project_id=project_id,
        execution_id="",
        child=MagicMock(),
        reserver=reserver,
        registry=create_spawn_agent_registry(
            MagicMock(),
            session_manager=sessions,
            db=temp_db,
            agent_pane_reserver_resolver=lambda: reserver,
        ),
    )
    h.execution_id = h.execution(ExecutionStatus.RUNNING)
    h.child = h.pipeline_session(h.execution_id)

    async def launch(**kwargs: Any) -> dict[str, Any]:
        """The ordinary path: a new run created with the reserved id."""
        h.launches.append(kwargs)
        run_id = kwargs["reserved_run_id"] or str(uuid.uuid4())
        h.runs.create(
            parent_session_id=kwargs["parent_session_id"],
            provider="claude",
            prompt=kwargs["prompt"],
            run_id=run_id,
        )
        return {"success": True, "run_id": run_id}

    context = {"id": project_id, "project_path": str(tmp_path)}
    monkeypatch.setattr(_factory, "spawn_agent_impl", launch)
    monkeypatch.setattr(_factory, "get_project_context", lambda *a, **k: context)
    monkeypatch.setattr(_factory, "_load_agent_body", lambda *a, **k: None)
    return h


def _assert_nothing_launched(h: _Harness) -> None:
    assert h.launches == []
    assert h.reserver.mock_calls == []


def _seat_live(h: _Harness, terminal_id: str) -> None:
    h.seat_pane(terminal_id)


def _seat_ended(h: _Harness, terminal_id: str) -> None:
    _, pane_id = h.seat_pane(terminal_id)
    h.workspaces.remove_pane(pane_id)


def _seat_moved(h: _Harness, terminal_id: str) -> None:
    tab_id, _ = h.seat_pane(terminal_id)
    other, _ = h.workspaces.create(h.machine_id, "elsewhere")
    h.workspaces.move_tab(tab_id, workspace_id=other.id, position=0)


def _seat_renamed(h: _Harness, terminal_id: str) -> None:
    tab_id, _ = h.seat_pane(terminal_id)
    h.workspaces.rename_tab(tab_id, "L3-renamed")


@pytest.mark.parametrize(
    "seat_change",
    [
        pytest.param(_seat_live, id="live"),
        pytest.param(_seat_ended, id="ended"),
        pytest.param(_seat_moved, id="moved"),
        pytest.param(_seat_renamed, id="renamed"),
    ],
)
async def test_started_run_is_adopted(
    seat: _Harness, seat_change: Callable[[_Harness, str], None]
) -> None:
    run_id = seat.run(seat.step_id("seat_a"), seat.child, started=True)
    run = seat.runs.get(run_id)
    assert run is not None and run.terminal_id is not None
    seat_change(seat, run.terminal_id)
    bound = seat.workspaces.get_pane_for_terminal(run.terminal_id)

    reply = await seat.spawn(seat.child, reserved_run_id=run_id)

    refs: dict[str, str | None] = (
        {**seat.refs(run.terminal_id)}
        if bound is not None
        else {"workspace": None, "tab_ref": None, "pane_ref": None}
    )
    assert reply == {
        "success": True,
        "adopted": True,
        "run_id": run_id,
        "status": "running",
        **refs,
    }
    _assert_nothing_launched(seat)


async def test_distinct_steps_do_not_share_runs(seat: _Harness) -> None:
    first = await seat.spawn(seat.child, reserved_run_id=seat.step_id("seat_a"))
    seat.runs.start(seat.step_id("seat_a"))
    second = await seat.spawn(seat.child, reserved_run_id=seat.step_id("seat_b"))

    assert first == {"success": True, "run_id": seat.step_id("seat_a")}
    assert second == {"success": True, "run_id": seat.step_id("seat_b")}
    assert [launch["reserved_run_id"] for launch in seat.launches] == [
        seat.step_id("seat_a"),
        seat.step_id("seat_b"),
    ]
    assert seat.runs.get(seat.step_id("seat_a")) is not None
    assert seat.runs.get(seat.step_id("seat_b")) is not None


def _prepared(h: _Harness, run_id: str) -> str:
    h.run(run_id, h.child, started=False)
    return "pending"


def _failed_before_start(h: _Harness, run_id: str) -> str:
    h.run(run_id, h.child, started=False)
    h.runs.fail(run_id, "provider exited before start")
    return "error"


@pytest.mark.parametrize(
    "settle",
    [
        pytest.param(_prepared, id="prepared"),
        pytest.param(_failed_before_start, id="failed-before-start"),
    ],
)
async def test_unstarted_run_refuses(
    seat: _Harness, settle: Callable[[_Harness, str], str]
) -> None:
    run_id = seat.step_id("seat_a")
    status = settle(seat, run_id)

    reply = await seat.spawn(seat.child, reserved_run_id=run_id)

    assert reply["success"] is False
    assert reply["error_code"] == "seat_launch_unsettled"
    assert run_id in reply["error"] and status in reply["error"]
    _assert_nothing_launched(seat)


def _other_project(h: _Harness) -> str:
    return LocalProjectManager(h.db).create(f"other-{uuid.uuid4()}", machine_id=h.machine_id).id


def _non_pipeline_caller(h: _Harness) -> tuple[Session, dict[str, Any]]:
    caller = h.sessions.register(
        external_id=f"claude-{uuid.uuid4()}",
        machine_id=h.machine_id,
        source="claude",
        project_id=h.project_id,
    )
    return caller, {"reserved_run_id": h.step_id("seat_a")}


def _foreign_parent(h: _Harness) -> tuple[Session, dict[str, Any]]:
    other = h.pipeline_session(h.execution(ExecutionStatus.RUNNING))
    return h.child, {"reserved_run_id": h.step_id("seat_a"), "parent_session_id": other.id}


def _missing_execution(h: _Harness) -> tuple[Session, dict[str, Any]]:
    caller = h.pipeline_session(str(uuid.uuid4()))
    return caller, {
        "reserved_run_id": _invocation_id(caller.external_id[len("pipeline-") :], "seat_a"),
        "parent_session_id": caller.id,
    }


def _finished_execution(h: _Harness) -> tuple[Session, dict[str, Any]]:
    execution_id = h.execution(ExecutionStatus.COMPLETED)
    caller = h.pipeline_session(execution_id)
    return caller, {
        "reserved_run_id": _invocation_id(execution_id, "seat_a"),
        "parent_session_id": caller.id,
    }


def _execution_in_other_project(h: _Harness) -> tuple[Session, dict[str, Any]]:
    execution_id = h.execution(ExecutionStatus.RUNNING, project_id=_other_project(h))
    caller = h.pipeline_session(execution_id)
    return caller, {
        "reserved_run_id": _invocation_id(execution_id, "seat_a"),
        "parent_session_id": caller.id,
    }


def _not_a_step(h: _Harness) -> tuple[Session, dict[str, Any]]:
    return h.child, {"reserved_run_id": str(uuid.uuid4())}


def _deleted_caller(h: _Harness) -> tuple[Session, dict[str, Any]]:
    h.db.execute("UPDATE sessions SET status = 'deleted' WHERE id = %s", (h.child.id,))
    return h.child, {"reserved_run_id": h.step_id("seat_a")}


def _queued_reviewer(h: _Harness) -> tuple[Session, dict[str, Any]]:
    """The task-close reviewer branch still validates its queued run."""
    caller, _ = _non_pipeline_caller(h)
    return caller, {
        "agent": TASK_CLOSE_REVIEWER_AGENT,
        "parent_session_id": caller.id,
        "placement": None,
        "reserved_run_id": str(uuid.uuid4()),
    }


def _non_pipeline_parent(h: _Harness) -> Session:
    return h.sessions.register(
        external_id=f"claude-{uuid.uuid4()}",
        machine_id=h.machine_id,
        source="claude",
        project_id=h.project_id,
    )


def _other_execution_parent(h: _Harness) -> Session:
    return h.pipeline_session(h.execution(ExecutionStatus.RUNNING))


def _other_project_parent(h: _Harness) -> Session:
    project_id = _other_project(h)
    return h.pipeline_session(h.execution(ExecutionStatus.RUNNING, project_id), project_id)


def _conflict(parent_case: Callable[[_Harness], Session]) -> Callable[[_Harness], Any]:
    def case(h: _Harness) -> tuple[Session, dict[str, Any]]:
        return h.child, {
            "reserved_run_id": h.run(h.step_id("seat_a"), parent_case(h), started=True)
        }

    return case


def _replacement_child(h: _Harness) -> tuple[Session, dict[str, Any]]:
    """A later child row with the launching child's external id; its key admits one live row."""
    run_id = h.run(h.step_id("seat_a"), h.child, started=True)
    h.db.execute("UPDATE sessions SET session_type = 'retired' WHERE id = %s", (h.child.id,))
    replacement = h.pipeline_session(h.execution_id)
    assert replacement.id != h.child.id
    return replacement, {"reserved_run_id": run_id, "parent_session_id": replacement.id}


_UNAUTHORIZED = {"success": False, "error_code": "invocation_unauthorized"}
_CONFLICT = {"success": False, "error_code": "invocation_conflict"}


@pytest.mark.parametrize(
    ("caller_case", "expected", "looks_up_runs"),
    [
        pytest.param(
            _non_pipeline_caller,
            {"success": False, "error": "reserved_run_id is reviewer-internal"},
            False,
            id="non-pipeline-caller",
        ),
        pytest.param(_foreign_parent, _UNAUTHORIZED, False, id="parent-not-caller"),
        pytest.param(_missing_execution, _UNAUTHORIZED, False, id="execution-missing"),
        pytest.param(_finished_execution, _UNAUTHORIZED, False, id="execution-not-running"),
        pytest.param(
            _execution_in_other_project, _UNAUTHORIZED, False, id="execution-other-project"
        ),
        pytest.param(_not_a_step, _UNAUTHORIZED, False, id="not-a-step-invocation"),
        pytest.param(_deleted_caller, _UNAUTHORIZED, False, id="caller-deleted"),
        pytest.param(
            _conflict(_non_pipeline_parent), _CONFLICT, True, id="run-parent-not-pipeline"
        ),
        pytest.param(_conflict(_other_execution_parent), _CONFLICT, True, id="run-other-execution"),
        pytest.param(_conflict(_other_project_parent), _CONFLICT, True, id="run-other-project"),
        pytest.param(
            _replacement_child, {"success": True, "adopted": True}, True, id="replacement"
        ),
        pytest.param(
            _queued_reviewer,
            {"success": False, "error": "reserved queued reviewer run mismatch"},
            True,
            id="queued-reviewer",
        ),
    ],
)
async def test_invocation_authority(
    seat: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    caller_case: Callable[[_Harness], tuple[Session, dict[str, Any]]],
    expected: dict[str, Any],
    looks_up_runs: bool,
) -> None:
    caller, arguments = caller_case(seat)
    if arguments.get("agent") == TASK_CLOSE_REVIEWER_AGENT:
        monkeypatch.setattr(_factory, "_load_agent_body", lambda *a, **k: MagicMock())
    lookups: list[str] = []
    real_get = LocalAgentRunManager.get

    def counting_get(self: LocalAgentRunManager, run_id: str) -> Any:
        lookups.append(run_id)
        return real_get(self, run_id)

    monkeypatch.setattr(LocalAgentRunManager, "get", counting_get)

    reply = await seat.spawn(caller, **arguments)

    assert {key: reply.get(key) for key in expected} == expected
    assert bool(lookups) is looks_up_runs
    _assert_nothing_launched(seat)


async def test_pipeline_caller_launches_queued_reviewer(
    seat: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A close review promoted inside a pipeline step keeps the queued reviewer launch."""
    run_id = str(uuid.uuid4())
    seat.runs.create(
        parent_session_id=seat.child.id,
        provider="claude",
        prompt="Review the close",
        agent_name=TASK_CLOSE_REVIEWER_AGENT,
        run_id=run_id,
    )
    seat.db.execute("UPDATE agent_runs SET status = 'queued' WHERE id = %s", (run_id,))
    reviewer_body = MagicMock()
    reviewer_body.workflows.pipeline = None
    monkeypatch.setattr(_factory, "_load_agent_body", lambda *a, **k: reviewer_body)

    async def launch(**kwargs: Any) -> dict[str, Any]:
        seat.launches.append(kwargs)
        return {"success": True, "run_id": kwargs["reserved_run_id"]}

    monkeypatch.setattr(_factory, "spawn_agent_impl", launch)

    reply = await seat.spawn(
        seat.child,
        agent=TASK_CLOSE_REVIEWER_AGENT,
        prompt="Review the close",
        placement=None,
        reserved_run_id=run_id,
    )

    assert reply == {"success": True, "run_id": run_id}
    assert [launch["reserved_run_id"] for launch in seat.launches] == [run_id]


async def test_adopted_reply_names_bound_pane(seat: _Harness) -> None:
    bound_id = seat.run(seat.step_id("seat_a"), seat.child, started=True)
    unbound_id = seat.run(seat.step_id("seat_b"), seat.child, started=True)
    bound_run, unbound_run = seat.runs.get(bound_id), seat.runs.get(unbound_id)
    assert bound_run is not None and bound_run.terminal_id is not None
    assert unbound_run is not None and unbound_run.terminal_id is not None
    seat.seat_pane(bound_run.terminal_id)

    bound = await seat.spawn(seat.child, reserved_run_id=bound_id)
    unbound = await seat.spawn(seat.child, reserved_run_id=unbound_id)

    assert {key: bound[key] for key in ("workspace", "tab_ref", "pane_ref")} == seat.refs(
        bound_run.terminal_id
    )
    assert seat.workspaces.placement_refs_for_terminal(bound_run.terminal_id) == seat.refs(
        bound_run.terminal_id
    )
    assert {key: unbound[key] for key in ("workspace", "tab_ref", "pane_ref")} == {
        "workspace": None,
        "tab_ref": None,
        "pane_ref": None,
    }
    assert seat.workspaces.placement_refs_for_terminal(unbound_run.terminal_id) is None
