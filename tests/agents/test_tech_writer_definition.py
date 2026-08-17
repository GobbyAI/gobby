"""Contract tests for the tech-writer agent definition."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gobby.workflows.safe_evaluator import SafeExpressionEvaluator

pytestmark = pytest.mark.unit


def _agent() -> dict:
    path = (
        Path(__file__).resolve().parents[2]
        / "src/gobby/install/shared/workflows/agents/tech-writer.yaml"
    )
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_loads_required_skills_before_implementation() -> None:
    agent = _agent()
    steps = {step["name"]: step for step in agent["step_workflow"]["steps"]}
    load_step = steps["load_skills"]

    assert agent["step_workflow"]["variables"]["required_skills"] == [
        "tech-writer",
        "tasks",
    ]
    assert steps["claim"]["transitions"] == [{"to": "load_skills", "when": "vars.task_claimed"}]
    assert load_step["allowed_mcp_tools"] == ["gobby-skills:get_skill"]
    assert 'get_skill(name="tech-writer")' in load_step["status_message"]
    assert 'get_skill(name="tasks")' in load_step["status_message"]
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

    assert "tag:task-skill-gates" in excludes


def test_handoff_transitions_to_end_agent_run_termination() -> None:
    agent = _agent()
    steps = {step["name"]: step for step in agent["step_workflow"]["steps"]}
    implement = steps["implement"]
    terminate = steps["terminate"]
    success_tools = {
        f"{item['server']}:{item['tool']}" for item in implement.get("on_mcp_success", [])
    }

    assert {
        "gobby-tasks:close_task",
        "gobby-tasks-ops:submit_for_review",
    } <= success_tools
    close_hook = next(
        item
        for item in implement["on_mcp_success"]
        if item["server"] == "gobby-tasks" and item["tool"] == "close_task"
    )
    assert "not tool_input.get('preview', False)" in close_hook["when"]
    assert "tool_input.get('task_id') == vars.get('assigned_task_id')" in close_hook["when"]
    assert implement["transitions"] == [{"to": "terminate", "when": "vars.implementation_complete"}]
    assert "gobby-agents:end_agent_run" in implement["blocked_mcp_tools"]
    assert terminate["allowed_mcp_tools"] == ["gobby-agents:end_agent_run"]


def test_close_task_when_tolerates_null_result() -> None:
    implement = next(
        step for step in _agent()["step_workflow"]["steps"] if step["name"] == "implement"
    )
    close_hook = next(
        item
        for item in implement["on_mcp_success"]
        if item["server"] == "gobby-tasks" and item["tool"] == "close_task"
    )
    evaluator = SafeExpressionEvaluator(
        {
            "tool_input": {"task_id": "#1", "preview": False},
            "vars": {"assigned_task_id": "#1"},
            "tool_output": {"closed": False, "result": None},
        },
        {},
    )
    assert evaluator.evaluate(close_hook["when"]) is False

    closed_evaluator = SafeExpressionEvaluator(
        {
            "tool_input": {"task_id": "#1", "preview": False},
            "vars": {"assigned_task_id": "#1"},
            "tool_output": {"result": {"closed": True}},
        },
        {},
    )
    assert closed_evaluator.evaluate(close_hook["when"]) is True
