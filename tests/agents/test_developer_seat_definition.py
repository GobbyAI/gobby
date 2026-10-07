"""Contract tests for the developer seat definition.

agent-definition-profiles 3.2 folds backend-, frontend- and fullstack-developer
into one `developer` seat that routes the skills each task needs and loops from
one claimed task to the next.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path
from gobby.skills.sync import get_bundled_skills_path
from gobby.workflows.definitions import AgentDefinitionBody
from tests.agents._yaml_helpers import flat

pytestmark = pytest.mark.unit

# The skills the prompt's routing table names, each a bundled skill.
ROUTED_SKILLS = ("impeccable", "typescript", "rust", "tech-writer")
STEP_ORDER = [
    "load_skills",
    "claim",
    "route_skills",
    "load_additional_skills",
    "implement",
    "submit",
]
# Per-task state the close handlers reset so the next claim starts clean.
PER_TASK_FLAGS = (
    "task_claimed",
    "implementation_complete",
    "additional_skills_loaded",
    "skills_routed",
    "candidate_sent",
    "bounced",
    "assigned_task_id",
    "assigned_task_ref",
)


def _raw(name: str) -> dict[str, Any]:
    data = yaml.safe_load((get_bundled_agents_path() / f"{name}.yaml").read_text())
    assert isinstance(data, dict), name
    return data


def _body() -> AgentDefinitionBody:
    return AgentDefinitionBody.model_validate(_raw("developer"))


def _prompt() -> str:
    prompts = _body().prompts
    assert prompts is not None
    return flat(prompts.agent)


def _section(text: str, heading: str) -> str:
    """``heading`` and its body, up to the next level-2 heading."""
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start:] if end == -1 else text[start:end]


def _transitions(step: dict[str, Any]) -> dict[str, str]:
    return {transition["to"]: transition["when"] for transition in step.get("transitions", [])}


def _steps() -> dict[str, dict[str, Any]]:
    return {step["name"]: step for step in _raw("developer")["step_workflow"]["steps"]}


def test_developer_carries_the_seat_shape() -> None:
    raw = _raw("developer")
    body = _body()
    prompt = _prompt()
    persona = _raw("default")["prompts"]["persona"]

    assert raw["tags"] == ["gobby", "seat"]
    assert not {"enabled", "priority", "model", "reasoning_effort"} & set(raw)
    assert body.version == "1.0"
    assert body.surfaces == ["spawn", "persona"]
    assert (body.provider, body.model, body.checkout_mode, body.timeout) == (
        "inherit",
        None,
        "inherit",
        0,
    )
    assert body.prompts is not None
    assert body.prompts.persona == body.prompts.agent
    assert body.workflows.rule_selectors is not None
    assert {"tag:default", "tag:roles", "tag:worker-safety"} <= set(
        body.workflows.rule_selectors.include
    )
    assert body.blocked_mcp_tools == ["gobby-agents:kill_agent"]
    assert set(body.send_message_targets) == {"parent", "session"}
    for heading in ("## Platform Context", "## Skills"):
        assert flat(_section(persona, heading)) in prompt
    assert "gobby-agents:send_message" in prompt
    assert "lesson" not in prompt.lower()


def test_developer_prompt_carries_the_lane_flow_and_skill_routing() -> None:
    prompt = _prompt()

    assert ("`LANE= EVENT=CANDIDATE TASK=#NNNNN TASK_TITLE= RUN= WT= COMMIT= NOTE=`") in prompt
    assert "Lane Manager and the assigned Code Reviewer" in prompt
    assert "only when your Lane Manager releases it" in prompt
    assert "HOLD" in prompt
    assert "GO" in prompt
    assert "gobby-agents:wait_for_coordination" in prompt
    assert 'set_variable(scope="step")' in prompt
    assert "Orchestrator" in prompt
    assert re.search(r"\bPD\b", prompt) is None
    for skill in ROUTED_SKILLS:
        assert f"`{skill}`" in prompt, skill
        assert (get_bundled_skills_path() / skill / "SKILL.md").is_file(), skill


def test_developer_reaches_implement_only_through_both_skill_gates() -> None:
    workflow = _raw("developer")["step_workflow"]
    steps = _steps()

    assert [step["name"] for step in workflow["steps"]] == STEP_ORDER
    assert "exit_condition" not in workflow
    assert workflow["variables"]["required_skills"] == [
        "gobby:references/development/obligations.md",
        "restraint",
        "gobby:references/tasks/overview.md",
    ]
    assert _transitions(steps["load_skills"]) == {
        "claim": "all(skill_loaded(skill) for skill in vars.required_skills)"
    }
    assert set(_transitions(steps["claim"])) == {"route_skills"}
    assert set(_transitions(steps["route_skills"])) == {"load_additional_skills"}
    assert "vars.skills_routed" in _transitions(steps["route_skills"])["load_additional_skills"]
    assert set(_transitions(steps["load_additional_skills"])) == {"implement"}
    assert set(_transitions(steps["submit"])) == {"implement", "claim"}
    assert "gobby-tasks:close_task" in steps["implement"]["blocked_mcp_tools"]


def test_developer_close_handlers_reset_every_per_task_flag() -> None:
    workflow = _raw("developer")["step_workflow"]
    submit = _steps()["submit"]
    close_handlers = [
        handler
        for handler in submit["on_mcp_success"]
        if (handler["server"], handler["tool"]) == ("gobby-tasks", "close_task")
    ]

    assert [handler["variable"] for handler in close_handlers] == list(PER_TASK_FLAGS)
    for flag in PER_TASK_FLAGS:
        assert flag in workflow["variables"], flag
    assert _transitions(submit)["claim"] == "not vars.task_claimed"
