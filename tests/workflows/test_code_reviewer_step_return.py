"""A persistent code-reviewer seat returns to its candidate-wait step once its leaf closes.

The definition is the shipped ``code-reviewer.yaml``, parsed rather than synthesized, so
these tests fail on the shipped text. Each test drives the real step engine through the
claim, the close, and the candidate transitions.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._transitions import claim_task
from gobby.utils.project_context import reset_project_context, set_project_context
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.observers import reconcile_claimed_tasks
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_context import get_active_step_workflow_context
from gobby.workflows.step_instances import AgentStepInstanceManager, build_step_instance

pytestmark = pytest.mark.unit

MACHINE_ID = "21000000-0000-4000-8000-000000000002"
CLAIM_STATUS_TEXT = "You hold the task's claim"


@dataclass
class _Reviewer:
    db: HubDatabase
    session_id: str
    project_id: str
    task_id: str
    ref: str


@pytest.fixture
def reviewer(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[_Reviewer]:
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", MACHINE_ID)
    LocalProjectCheckoutManager(temp_db).register(MACHINE_ID, sample_project["id"], str(tmp_path))
    session_id = (
        SessionManager(temp_db)
        .register(
            external_id="code-reviewer-seat",
            machine_id=MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
        )
        .id
    )
    SessionVariableManager(temp_db).merge_variables(session_id, {"is_spawned_agent": True})
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"],
        title="Leaf under review",
        validation_criteria="The reviewer returns to its candidate wait.",
    )
    token = set_project_context(sample_project)
    try:
        yield _Reviewer(
            db=temp_db,
            session_id=session_id,
            project_id=sample_project["id"],
            task_id=task.id,
            ref=f"#{task.seq_num}",
        )
    finally:
        reset_project_context(token)


def _reviewer_body() -> AgentDefinitionBody:
    path = get_bundled_agents_path() / "code-reviewer.yaml"
    return AgentDefinitionBody.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _persist(reviewer: _Reviewer, step: str, variables: dict[str, Any]) -> None:
    body = _reviewer_body()
    assert body.step_workflow is not None
    instance = build_step_instance(
        body,
        session_id=reviewer.session_id,
        step_workflow_id=None,
        variables={**body.step_workflow.variables, **variables},
        current_step=step,
    )
    AgentStepInstanceManager(reviewer.db).save(instance)


def _engine(reviewer: _Reviewer) -> RuleEngine:
    """Build the engine as the daemon does, so task_state_in can read the candidate leaf."""
    return RuleEngine(reviewer.db, task_manager=LocalTaskManager(reviewer.db))


def _current_step(reviewer: _Reviewer) -> str:
    variables = SessionVariableManager(reviewer.db).get_variables(reviewer.session_id)
    context = get_active_step_workflow_context(reviewer.db, reviewer.session_id, variables)
    assert context is not None
    return context.current_step


def _instance_variables(reviewer: _Reviewer) -> dict[str, Any]:
    instance = AgentStepInstanceManager(reviewer.db).get_for_session(reviewer.session_id)
    assert instance is not None
    return instance.variables


def _tool_event(
    session_id: str,
    server: str,
    tool: str,
    arguments: dict[str, Any],
    output: dict[str, Any],
) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id=session_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {"server_name": server, "tool_name": tool, "arguments": arguments},
            "tool_output": {"success": True, "result": output},
        },
        metadata={},
    )


def _close_and_prune(reviewer: _Reviewer) -> None:
    """Close the leaf and prune its claim the way the turn-end observers do."""
    LocalTaskManager(reviewer.db).close_task(reviewer.task_id, reason="done", force=True)
    variables = SessionVariableManager(reviewer.db).get_variables(reviewer.session_id)
    reconcile_claimed_tasks(variables, reviewer.session_id, LocalTaskManager(reviewer.db))
    SessionVariableManager(reviewer.db).merge_variables(
        reviewer.session_id, {"claimed_tasks": variables["claimed_tasks"]}
    )


@pytest.mark.asyncio
async def test_fix_returns_to_candidate_wait_once_claimed_leaf_closes(reviewer: _Reviewer) -> None:
    """A handed-over claim opens fix; closing that leaf returns the seat to await."""
    _persist(reviewer, "await", {})
    claim_task(reviewer.db, reviewer.task_id, reviewer.session_id)
    engine = _engine(reviewer)

    await engine._process_step_after_tool(
        _tool_event(
            reviewer.session_id,
            "gobby-tasks",
            "claim_task",
            {"task_id": reviewer.ref},
            {"task_id": reviewer.task_id, "ref": reviewer.ref, "already_claimed": True},
        ),
        reviewer.session_id,
        {},
    )
    assert _current_step(reviewer) == "fix"

    _close_and_prune(reviewer)

    assert _current_step(reviewer) == "await"
    variables = _instance_variables(reviewer)
    assert variables["fixing"] is False
    assert variables["assigned_task_id"] is None


def test_stop_gate_status_does_not_claim_a_closed_unclaimed_task(reviewer: _Reviewer) -> None:
    """The step text that require-step-completion renders no longer says the seat holds the claim."""
    _persist(reviewer, "await", {})
    claim_task(reviewer.db, reviewer.task_id, reviewer.session_id)
    _persist(
        reviewer,
        "fix",
        {"fixing": True, "assigned_task_id": reviewer.task_id, "assigned_task_ref": reviewer.ref},
    )
    _close_and_prune(reviewer)

    context = get_active_step_workflow_context(
        reviewer.db,
        reviewer.session_id,
        SessionVariableManager(reviewer.db).get_variables(reviewer.session_id),
    )

    assert context is not None
    assert context.current_step == "await"
    assert CLAIM_STATUS_TEXT not in (context.status_message or "")


@pytest.mark.asyncio
async def test_closed_candidate_returns_review_to_candidate_wait(reviewer: _Reviewer) -> None:
    """A review whose candidate leaf closes returns to await without a verdict."""
    _persist(
        reviewer,
        "review",
        {
            "candidate_received": True,
            "candidate_task": reviewer.task_id,
            "candidate_sha": "a" * 40,
        },
    )
    LocalTaskManager(reviewer.db).close_task(reviewer.task_id, reason="done", force=True)

    await _engine(reviewer)._process_step_after_tool(
        _tool_event(
            reviewer.session_id,
            "gobby-tasks",
            "get_task",
            {"task_id": reviewer.task_id},
            {"id": reviewer.task_id, "state": {"is_closed": True}},
        ),
        reviewer.session_id,
        {},
    )

    assert _current_step(reviewer) == "await"


@pytest.mark.asyncio
async def test_next_candidate_after_return_reaches_review_with_reviewing_link(
    reviewer: _Reviewer,
) -> None:
    """After the return, a routed candidate moves the seat to review, where the link is admitted."""
    next_leaf = LocalTaskManager(reviewer.db).create_task(
        reviewer.project_id,
        title="Next leaf",
        validation_criteria="The next leaf is reviewed.",
    )
    _persist(
        reviewer,
        "await",
        {"candidate_received": True, "candidate_task": next_leaf.id, "candidate_sha": "b" * 40},
    )

    await _engine(reviewer)._process_step_after_tool(
        _tool_event(reviewer.session_id, "gobby-agents", "get_inter_session_message", {}, {}),
        reviewer.session_id,
        {},
    )

    assert _current_step(reviewer) == "review"
    body = _reviewer_body()
    assert body.step_workflow is not None
    steps = {step.name: step for step in body.step_workflow.steps}
    assert "gobby-tasks:link_task_to_session" in steps["review"].allowed_mcp_tools
    assert "gobby-tasks:link_task_to_session" not in steps["fix"].allowed_mcp_tools
