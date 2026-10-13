"""Runbook titles reach persisted sessions before a terminal launches."""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
import yaml

from gobby.agents.session import ChildSessionManager
from gobby.agents.spawn import prepare_terminal_resume, prepare_terminal_spawn
from gobby.mcp_proxy.tools.spawn_agent import _factory
from gobby.sessions.title_lifecycle import recompute_automatic_title, update_title_for_claim
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.definitions import PipelineDefinition
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.templates import TemplateEngine

PIPELINES = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/workflows/pipelines"


@pytest.mark.parametrize(
    ("runbook", "seat", "role"),
    [
        ("team-lane", "developer", "developer"),
        ("team-lane", "code-reviewer", "code reviewer"),
        ("team-lane", "security-reviewer", "security reviewer"),
        ("team-lane", "researcher", "researcher"),
        ("team-lane", "lane-manager", "manager"),
        ("team-coordination", "archivist", "archivist"),
        ("team-coordination", "log-monitor", "log monitor"),
        ("team-coordination", "metrics-monitor", "metrics monitor"),
        ("team-coordination", "release-manager", "release manager"),
        ("team-coordination", "inbox-manager", "inbox manager"),
        ("team-coordination", "recovery-steward", "recovery steward"),
        ("team-coordination", "consolidator", "consolidator"),
        ("team-planning", "plan-writer", "plan writer"),
        ("team-planning", "plan-adversary", "plan adversary"),
    ],
)
@pytest.mark.asyncio
@pytest.mark.parametrize("lane_pane", [None, "test-pane"])
async def test_runbook_title_survives_task_claim_and_close(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    runbook: str,
    seat: str,
    role: str,
    lane_pane: str | None,
) -> None:
    definition = PipelineDefinition.model_validate(
        yaml.safe_load((PIPELINES / f"{runbook}.yaml").read_text())
    )
    inputs = {name: spec.get("default") for name, spec in definition.inputs.items()}
    inputs.update(
        lane="3",
        seats=seat,
        workspace="test-workspace",
        project_path=str(PIPELINES),
        report_to="gobby#1",
        lane_pane=lane_pane,
    )
    step = next(step for step in definition.steps if step.id == seat)
    assert step.mcp is not None and step.mcp.arguments is not None
    renderer = StepRenderer(TemplateEngine())
    arguments = renderer.render_mcp_arguments(
        step.mcp.arguments,
        {
            "inputs": inputs,
            "_string_inputs": frozenset(
                name for name, spec in definition.inputs.items() if spec.get("type") == "string"
            ),
            "invocation_id": str(uuid4()),
            "steps": {
                **{step.id: {"output": {"pane_ref": "test-pane"}} for step in definition.steps},
                "guard": {
                    "output": {
                        "worktree": str(uuid4()),
                        "project_path": str(PIPELINES),
                        "lane_pane": "test-pane",
                        "lane_columns": 160,
                        "lane_rows": 100,
                        "report_to": "gobby#1",
                    }
                },
            },
        },
        drop_none=True,
    )
    parent = session_manager.register(
        external_id=f"parent-{runbook}-{seat}",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )
    title_listener = Mock()
    session_manager.register_title_listener(title_listener)

    async def launch(**kwargs: Any) -> dict[str, Any]:
        prepared = prepare_terminal_spawn(
            session_manager=ChildSessionManager(session_manager),
            parent_session_id=parent.id,
            project_id=parent.project_id,
            machine_id=parent.machine_id,
            source="codex",
            agent_name=kwargs["agent_lookup_name"],
            initial_variables=kwargs["initial_variables"],
        )
        return {"success": True, "prepared": prepared, "variables": kwargs["initial_variables"]}

    from gobby.workflows.agent_models import (
        AgentDefinitionBody,
        AgentPromptBlocks,
        AgentSelector,
        AgentWorkflows,
    )

    monkeypatch.setattr(
        _factory,
        "_load_agent_body",
        Mock(
            return_value=AgentDefinitionBody(
                name=seat,
                prompts=AgentPromptBlocks(agent="Runbook seat"),
                workflows=AgentWorkflows(rule_selectors=AgentSelector()),
            )
        ),
    )
    monkeypatch.setattr(_factory, "enforce_spawn_caller", AsyncMock(return_value=parent.id))
    monkeypatch.setattr(_factory, "pipeline_caller", Mock(return_value=parent))
    monkeypatch.setattr(_factory, "pipeline_invocation_reply", Mock(return_value=None))
    monkeypatch.setattr(_factory, "spawn_agent_impl", launch)
    registry = _factory.create_spawn_agent_registry(
        Mock(), session_manager=session_manager, db=session_manager.db
    )
    result = await registry.call("spawn_agent", arguments)
    assert result["success"]
    prepared = result["prepared"]
    variables = result["variables"]

    title = f"Lane 3 {role}"
    child = session_manager.get(prepared.session_id)
    assert child is not None and child.title == title and child.title_source == "definition"
    title_listener.assert_called_with(child.id, title)
    tasks = LocalTaskManager(session_manager.db)
    task = tasks.create_task(
        parent.project_id,
        "Temporary seat work",
        claimed_by_session_id=child.id,
        validation_criteria="The role title returns when the work closes.",
    )
    claimed = update_title_for_claim(session_manager, child.id, task)
    assert claimed.title != title and "Temporary seat work" in claimed.title

    LocalAgentRunManager(session_manager.db).complete(prepared.agent_run_id)
    session_manager.update_status(child.id, "expired")
    resumed = prepare_terminal_resume(
        session_manager=ChildSessionManager(session_manager),
        existing_session_id=child.id,
        original_run_id=prepared.agent_run_id,
        parent_session_id=parent.id,
        project_id=parent.project_id,
        source="codex",
        workflow_name=None,
        agent_name=seat,
        initial_variables=variables,
        git_branch=None,
        prompt="",
        model=None,
        is_local=False,
        max_agent_depth=1,
        agent_run_id=str(uuid4()),
        task_id=task.id,
        claimed_session_id=child.id,
        timeout_seconds=None,
        sandbox_enabled=False,
        requested_reasoning_effort=None,
        effective_reasoning_effort=None,
        reasoning_required=False,
        reasoning_status="not_requested",
        reasoning_message=None,
        resume_metadata_json={},
        worktree_id=None,
        clone_id=None,
        workspace_path=str(PIPELINES),
        relaunch=True,
    )
    assert resumed.session_id == child.id
    after_resume = session_manager.get(child.id)
    assert after_resume is not None and after_resume.title == claimed.title

    tasks.close_task(task.id, force=True, closed_in_session_id=child.id)
    restored = recompute_automatic_title(session_manager, child.id)

    assert restored.title == title and restored.title_source == "definition"
    title_listener.assert_called_with(child.id, title)
