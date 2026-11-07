"""Engine dry run of every bundled runbook that starts with a seat guard.

Each bundled runbook is synced into an isolated hub, and its installed row runs through
PipelineExecutor. The guard step calls the real check_runbook_seats tool against that hub.
Every other MCP step is recorded and answered with a stub, so no seat launches and no
daemon is contacted.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from gobby.agents.sync import sync_bundled_agents
from gobby.mcp_proxy.tools.agents_registry import create_agents_registry
from gobby.storage.definitions.pipelines import PipelineDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.storage.workspaces import WorkspaceManager
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.pipeline_executor import PipelineExecutor
from gobby.workflows.pipeline_models import PipelineDefinition
from gobby.workflows.pipeline_state import ExecutionStatus
from gobby.workflows.sync_pipelines import get_bundled_pipelines_path, sync_bundled_pipelines
from gobby.workflows.templates import TemplateEngine
from tests.agents.conftest import AGENT_TEST_MACHINE_ID

pytestmark = pytest.mark.integration

GUARD_TOOL = "check_runbook_seats"
GUARD_SERVER = "gobby-agents"


def _calls_guard(node: object) -> bool:
    if isinstance(node, dict):
        if node.get("tool") == GUARD_TOOL:
            return True
        return any(_calls_guard(value) for value in node.values())
    if isinstance(node, list):
        return any(_calls_guard(item) for item in node)
    return False


def _guarded_runbook_names() -> list[str]:
    names: list[str] = []
    for path in sorted(get_bundled_pipelines_path().glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and _calls_guard(data):
            names.append(str(data["name"]))
    return names


GUARDED_RUNBOOKS = _guarded_runbook_names()


@dataclass(frozen=True)
class _Hub:
    db: HubDatabase
    project_id: str
    workspace_id: str
    report_to: str


@pytest.fixture
def hub(temp_db: HubDatabase, sample_project: dict[str, Any]) -> _Hub:
    """An isolated hub with the bundled agents and runbooks synced into it."""
    agents = sync_bundled_agents(temp_db)
    pipelines = sync_bundled_pipelines(temp_db)
    assert agents["success"], agents["errors"]
    assert pipelines["success"], pipelines["errors"]
    project_id = str(sample_project["id"])
    workspace = WorkspaceManager(temp_db).create(AGENT_TEST_MACHINE_ID, "dry-run")[0]
    report = SessionManager(temp_db).register(
        external_id=f"report-{uuid.uuid4()}",
        machine_id=AGENT_TEST_MACHINE_ID,
        source="claude",
        project_id=project_id,
    )
    return _Hub(
        db=temp_db,
        project_id=project_id,
        workspace_id=workspace.id,
        report_to=report.id,
    )


class _DryRunProxy:
    """Route the guard to the real tool; record and stub every other MCP step."""

    def __init__(
        self, registry: Any, caller_session_id: str, task: Task, project_path: str
    ) -> None:
        self.registry = registry
        self.caller_session_id = caller_session_id
        self.task = task
        self.project_path = project_path
        self.guard_results: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def call_tool(
        self, server: str, tool: str, arguments: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        if (server, tool) == (GUARD_SERVER, GUARD_TOOL):
            with session_context_for_test(self.caller_session_id):
                result: dict[str, Any] = self.registry.call_sync(tool, arguments)
            self.guard_results.append(result)
            return result
        self.calls.append((server, tool, arguments))
        if tool == "get_task":
            return {
                "id": self.task.id,
                "seq_num": self.task.seq_num,
                "task_type": self.task.task_type,
                "title": self.task.title,
            }
        # Every other step answers with the output keys the runbooks read from
        # admit_team and spawn_agent, so each template renders against a real shape.
        stub = f"stub-{len(self.calls)}"
        return {
            "success": True,
            "child_session_id": stub,
            "session_id": stub,
            "pane_ref": f"pane-{stub}",
            "tab_ref": f"tab-{stub}",
            "run_id": f"run-{stub}",
            "project_path": self.project_path,
            "main_project_path": self.project_path,
            "task_id": self.task.id,
            "worktree": f"worktree-{stub}",
            "team": {"team_id": f"team-{stub}", "mode": "task"},
        }


def _run_inputs(raw_inputs: dict[str, Any], supplied: dict[str, str]) -> dict[str, str]:
    """Pass the dry-run values; every required runbook input must have one."""
    for name, spec in raw_inputs.items():
        if isinstance(spec, dict) and spec.get("required") and name not in supplied:
            raise AssertionError(f"required runbook input '{name}' has no dry-run value")
    return dict(supplied)


def _guard_arguments(node: object) -> dict[str, Any]:
    """The arguments of the first seat-guard step, at any depth."""
    if isinstance(node, dict):
        arguments = node.get("arguments")
        if node.get("tool") == GUARD_TOOL and isinstance(arguments, dict):
            return dict(arguments)
        for value in node.values():
            found = _guard_arguments(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _guard_arguments(item)
            if found:
                return found
    return {}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", GUARDED_RUNBOOKS)
async def test_guarded_runbook_dry_run_gives_one_task_tab(
    name: str, hub: _Hub, tmp_path: Path
) -> None:
    """The installed runbook runs in the engine; its guard titles the tab '<runbook> - #<seq>'."""
    db = hub.db
    row = PipelineDefinitionManager(db).get_by_name(name, project_id=None)
    assert row is not None
    definition = PipelineDefinition.model_validate(row.definition_json)
    task = LocalTaskManager(db).create_task(
        project_id=hub.project_id,
        title=f"{name} dry run",
        validation_criteria="The dry run titles one task tab.",
    )
    task_ref = f"#{task.seq_num}"
    supplied = {
        "workspace": hub.workspace_id,
        "task": task_ref,
        "project_path": str(tmp_path),
        "report_to": hub.report_to,
    }
    inputs = _run_inputs(row.definition_json.get("inputs") or {}, supplied)

    executions = LocalPipelineExecutionManager(db, hub.project_id)
    execution = executions.create_execution(
        name, inputs_json=json.dumps(inputs), project_id=hub.project_id
    )
    child = SessionManager(db).register(
        external_id=f"pipeline-{execution.id}",
        machine_id=AGENT_TEST_MACHINE_ID,
        source="pipeline",
        project_id=hub.project_id,
    )
    executions.update_execution_session(execution.id, child.id)

    proxy = _DryRunProxy(
        create_agents_registry(MagicMock(), session_manager=SessionManager(db), db=db),
        child.id,
        task,
        str(tmp_path),
    )
    executor = PipelineExecutor(
        db=db,
        execution_manager=executions,
        llm_service=MagicMock(),
        template_engine=TemplateEngine(),
        tool_proxy_getter=lambda: proxy,
        session_manager=SessionManager(db),
    )
    with patch("gobby.utils.project_context.get_project_context", return_value=None):
        result = await executor.execute(
            definition,
            inputs,
            hub.project_id,
            execution_id=execution.id,
            session_id=child.id,
        )

    assert result.status == ExecutionStatus.COMPLETED, (result.status, result.outputs_json)
    assert len(proxy.guard_results) == 1
    guard = proxy.guard_results[0]
    assert guard["success"] is True, guard
    assert guard["launch"], "the guard admitted no seat to launch"

    tab = guard["tab_title"]
    guard_arguments = _guard_arguments(row.definition_json)
    if "task" in guard_arguments:
        bare, separator, ref = tab.rpartition(" - ")
        assert separator and ref == task_ref, tab
        assert bare and "#" not in bare, tab
    else:
        # A static tab keeps its fixed name: the guard adds no task reference.
        assert tab == guard_arguments["tab_title"], tab
    serialized = json.dumps([arguments for _, _, arguments in proxy.calls], default=str)
    assert tab in serialized, "no downstream step received the guard's tab title"
    assert f"{tab} - {task_ref}" not in serialized, "a downstream step received a doubled title"
