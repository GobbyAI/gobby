"""Contract tests for the tech-writer agent definition."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


def _agent() -> dict:
    path = (
        Path(__file__).resolve().parents[2]
        / "src/gobby/install/shared/workflows/agents/tech-writer.yaml"
    )
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_loads_required_skills_before_implementation() -> None:
    agent = _agent()
    steps = {step["name"]: step for step in agent["steps"]}
    load_step = steps["load_skills"]

    assert agent["step_variables"]["required_skills"] == [
        "tech-writer",
        "task-transitions",
        "verification-before-completion",
    ]
    assert steps["claim"]["transitions"] == [{"to": "load_skills", "when": "vars.task_claimed"}]
    assert load_step["allowed_mcp_tools"] == ["gobby-skills:get_skill"]
    assert 'get_skill(name="tech-writer")' in load_step["status_message"]
    assert 'get_skill(name="task-transitions")' in load_step["status_message"]
    assert 'get_skill(name="verification-before-completion")' in load_step["status_message"]
    assert load_step["transitions"] == [
        {
            "to": "implement",
            "when": "all(skill in vars.get('loaded_skills', []) for skill in vars.required_skills)",
        }
    ]

    success = load_step["on_mcp_success"][0]
    assert success["server"] == "gobby-skills"
    assert success["tool"] == "get_skill"
    assert success["variable"] == "required_skills_loaded"


def test_excludes_late_task_skill_injection() -> None:
    agent = _agent()
    excludes = set(agent["workflows"]["rule_selectors"]["exclude"])

    assert "tag:task-skill-injection" in excludes


def test_handoff_transitions_to_end_agent_run_termination() -> None:
    agent = _agent()
    steps = {step["name"]: step for step in agent["steps"]}
    implement = steps["implement"]
    terminate = steps["terminate"]
    success_tools = {
        f"{item['server']}:{item['tool']}" for item in implement.get("on_mcp_success", [])
    }

    assert {
        "gobby-tasks:close_task",
        "gobby-tasks-ops:submit_for_review",
    } <= success_tools
    assert implement["transitions"] == [{"to": "terminate", "when": "vars.implementation_complete"}]
    assert "gobby-agents:end_agent_run" in implement["blocked_mcp_tools"]
    assert terminate["allowed_mcp_tools"] == ["gobby-agents:end_agent_run"]
