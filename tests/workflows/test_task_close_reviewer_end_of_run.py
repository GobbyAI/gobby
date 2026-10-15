"""A task-close reviewer ends its run after the verdict within its review step allowance."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from gobby.agents.sync import sync_bundled_agents
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.hooks.session_activation import (
    _resolve_active_rule_names,
    clear_active_rule_names_cache,
)
from gobby.skills.formatting import skill_fetch_directive
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.agent_resolver import resolve_agent
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.step_instances import AgentStepInstanceManager, build_step_instance
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.integration

REVIEWER = "task-close-reviewer"
HANDOFF_SKILL = "gobby:references/sessions/handoffs.md"
END_OF_RUN_GATES = {"require-handoff-feedback-before-authoring", "require-handoff-discipline"}
END_RUN_ARGUMENTS = {
    "current_state": "Verdict valid for #7; the close lifecycle finalized it.",
    "next_steps": ["Continue with the next ready task."],
}


@pytest.fixture
def db(hub_db: HubDatabase) -> Iterator[HubDatabase]:
    """Sync the bundled agents and rules that session activation and the engine read."""
    clear_active_rule_names_cache()
    assert sync_bundled_agents(hub_db)["success"] is True
    sync_bundled_rules(hub_db, get_bundled_rules_path())
    yield hub_db
    clear_active_rule_names_cache()


@pytest.fixture
def session_id(db: HubDatabase) -> str:
    session, project = str(uuid4()), str(uuid4())
    db.execute(
        "INSERT INTO projects (id, name, created_at) VALUES (%s, %s, CURRENT_TIMESTAMP)",
        (project, "gobby"),
    )
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, "
        "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (session, session, "21000000-0000-4000-8000-000000000001", "claude", project),
    )
    return session


def _event(
    session_id: str,
    data: dict[str, Any],
    event_type: HookEventType = HookEventType.BEFORE_TOOL,
) -> HookEvent:
    normalize_tool_fields(data)
    return HookEvent(
        event_type=event_type,
        session_id=session_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
    )


def _schema_lookup(session_id: str, server: str, tool: str) -> HookEvent:
    return _event(
        session_id,
        {
            "tool_name": "mcp__gobby__get_tool_schema",
            "tool_input": {"server_name": server, "tool_name": tool},
        },
    )


def _call(
    session_id: str,
    server: str,
    tool: str,
    arguments: dict[str, Any],
    output: dict[str, Any] | None = None,
) -> HookEvent:
    data: dict[str, Any] = {
        "tool_name": "mcp__gobby__call_tool",
        "tool_input": {"server_name": server, "tool_name": tool, "arguments": arguments},
    }
    if output is None:
        return _event(session_id, data)
    data["tool_output"] = output
    return _event(session_id, data, HookEventType.AFTER_TOOL)


def _start_review_step(db: HubDatabase, session_id: str) -> AgentStepInstanceManager:
    """Place the installed reviewer definition at its review step, as after startup loads."""
    definition = resolve_agent(REVIEWER, db)
    row = AgentDefinitionManager(db).get_by_name(REVIEWER)
    assert definition is not None and definition.step_workflow is not None
    assert row is not None and row.step_workflow_id is not None
    instances = AgentStepInstanceManager(db)
    instance = build_step_instance(
        definition,
        session_id=session_id,
        step_workflow_id=row.step_workflow_id,
        current_step="review",
        variables={**definition.step_workflow.variables, "required_skills_loaded": True},
    )
    instances.save(instance)
    return instances


def _review_step_mcp_allowance(db: HubDatabase) -> set[str]:
    definition = resolve_agent(REVIEWER, db)
    assert definition is not None and definition.step_workflow is not None
    review = next(step for step in definition.step_workflow.steps if step.name == "review")
    allowed = review.allowed_mcp_tools
    assert isinstance(allowed, list)
    return set(allowed)


def _assert_allowed(response: HookResponse, call: str) -> None:
    assert response.decision != "block", f"{call}: {response.reason}"


@pytest.mark.asyncio
async def test_reviewer_ends_run_after_verdict_within_step_allowance(
    db: HubDatabase, session_id: str
) -> None:
    active = _resolve_active_rule_names(db, REVIEWER, None)
    assert active is not None
    assert END_OF_RUN_GATES.isdisjoint(active)
    # Every MCP call this end of run makes is named in the review step allowance.
    allowance = _review_step_mcp_allowance(db)
    assert {"gobby-tasks:submit_close_review", "gobby-agents:end_agent_run"} <= allowance

    instances = _start_review_step(db, session_id)
    engine = RuleEngine(db)
    # Survey active in the gobby project, no feedback submitted, handoff guidance never loaded.
    variables: dict[str, Any] = {
        "_agent_type": REVIEWER,
        "is_spawned_agent": True,
        "project": {"name": "gobby"},
        "loaded_skill_references": [
            "gobby:references/code-index/overview.md",
            "gobby:references/tasks/overview.md",
        ],
        "loaded_skills": ["proportionality", "code-review"],
    }

    async def run(event: HookEvent) -> HookResponse:
        return await engine.evaluate(event, session_id=session_id, variables=variables)

    verdict_args = {"review_id": "c1f4c3ba", "verdict": {"status": "valid"}}
    _assert_allowed(
        await run(_schema_lookup(session_id, "gobby-tasks", "submit_close_review")),
        "submit_close_review schema",
    )
    _assert_allowed(
        await run(_call(session_id, "gobby-tasks", "submit_close_review", verdict_args)),
        "submit_close_review",
    )
    await run(
        _call(
            session_id,
            "gobby-tasks",
            "submit_close_review",
            verdict_args,
            {"success": True, "result": {"status": "closed"}},
        )
    )

    # The reviewer's own enforcement is live: a lifecycle mutation stays denied.
    denied = await run(_call(session_id, "gobby-tasks", "close_task", {"task_id": "#7"}))
    assert denied.decision == "block"
    assert "close_task" in (denied.reason or "")

    _assert_allowed(
        await run(_schema_lookup(session_id, "gobby-agents", "end_agent_run")),
        "end_agent_run schema",
    )
    _assert_allowed(
        await run(_call(session_id, "gobby-agents", "end_agent_run", END_RUN_ARGUMENTS)),
        "end_agent_run",
    )
    await run(
        _call(
            session_id,
            "gobby-agents",
            "end_agent_run",
            END_RUN_ARGUMENTS,
            {"success": True, "result": {"status": "completed"}},
        )
    )
    finished = instances.get_for_session(session_id)
    assert finished is not None
    assert finished.variables.get("review_complete") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", [None, "developer", "code-reviewer"])
async def test_end_of_run_gates_unchanged_for_other_sessions(
    db: HubDatabase, session_id: str, agent_type: str | None
) -> None:
    if agent_type is not None:
        active = _resolve_active_rule_names(db, agent_type, None)
        assert active is not None
        assert END_OF_RUN_GATES <= active
    engine = RuleEngine(db)
    variables: dict[str, Any] = {
        "is_spawned_agent": agent_type is not None,
        "project": {"name": "gobby"},
        "loaded_skill_references": [],
    }
    if agent_type is not None:
        variables["_agent_type"] = agent_type
    end_run = _call(session_id, "gobby-agents", "end_agent_run", END_RUN_ARGUMENTS)

    async def run(event: HookEvent) -> HookResponse:
        return await engine.evaluate(event, session_id=session_id, variables=variables)

    survey = await run(end_run)
    assert survey.decision == "block"
    assert "gobby-sessions:feedback" in (survey.reason or "")

    variables["_gobby_feedback_epoch_submitted"] = True
    guidance = await run(end_run)
    assert guidance.decision == "block"
    assert skill_fetch_directive(HANDOFF_SKILL).splitlines()[0] in (guidance.reason or "")

    variables["loaded_skill_references"] = [HANDOFF_SKILL]
    _assert_allowed(await run(end_run), "end_agent_run")
