"""Explicit rule-selection defaults and synthetic seats for tests unrelated to bundled agents."""

import json
from collections.abc import Sequence
from typing import Any

from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import AgentDefinitionBody, AgentSelector, AgentWorkflows


def make_agent_workflows(**fields: Any) -> AgentWorkflows:
    fields.setdefault("rule_selectors", AgentSelector(include=[], exclude=[]))
    return AgentWorkflows(**fields)


def make_agent_definition(**fields: Any) -> AgentDefinitionBody:
    fields.setdefault("workflows", make_agent_workflows())
    return AgentDefinitionBody(**fields)


def seat_definition_json(
    name: str,
    *,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    **fields: Any,
) -> str:
    """A synthetic seat: named rule selectors and a spawn prompt, with no bundled content."""
    body: dict[str, Any] = {
        "name": name,
        "prompts": {"agent": f"Synthetic {name} seat."},
        "workflows": {
            "rule_selectors": {"include": list(include), "exclude": list(exclude)},
        },
    }
    body.update(fields)
    return json.dumps(body)


def seed_seat(
    db: HubDatabase,
    name: str,
    *,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    **fields: Any,
) -> None:
    """Store a synthetic seat under its name in the global registry."""
    AgentDefinitionManager(db).create(
        name=name,
        source="custom",
        definition_json=seat_definition_json(name, include=include, exclude=exclude, **fields),
    )


def synthetic_developer_body() -> AgentDefinitionBody:
    """A developer-shaped step graph built from the step names the engine addresses.

    It carries only the claim binding that step_binding reads. The handoff, close and
    skill-routing handlers of a real seat are deliberately absent.
    """
    bind_claim = {
        "server": "gobby-tasks",
        "tool": "claim_task",
        "action": "set_variable",
        "variable": "assigned_task_id",
        "value": "(tool_output.get('result') or tool_output).get('task_id')",
    }
    return AgentDefinitionBody.model_validate(
        {
            "name": "developer",
            "prompts": {"agent": "Synthetic developer seat."},
            "workflows": {"rule_selectors": {"include": [], "exclude": []}},
            "step_workflow": {
                "variables": {
                    "task_claimed": False,
                    "assigned_task_id": None,
                    "assigned_task_ref": None,
                    "skills_routed": False,
                    "required_skills": ["restraint"],
                    "required_skills_loaded": False,
                    "additional_skills": [],
                    "additional_skills_loaded": False,
                    "implementation_complete": False,
                    "_developer_handoff_tasks": [],
                },
                "steps": [
                    {
                        "name": "load_skills",
                        "transitions": [
                            {"to": "claim", "when": "vars.get('required_skills_loaded')"}
                        ],
                    },
                    {"name": "claim", "on_mcp_success": [bind_claim]},
                    {"name": "route_skills"},
                    {"name": "load_additional_skills"},
                    {"name": "implement"},
                    {"name": "submit"},
                ],
            },
        }
    )
