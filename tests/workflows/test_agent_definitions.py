"""Red tests for lifecycle-dispatch agent and skill definitions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS_DIR = REPO_ROOT / "src/gobby/install/shared/workflows/agents"
SKILLS_DIR = REPO_ROOT / "src/gobby/install/shared/skills"


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict)
    return data


def _agent(name: str) -> dict[str, Any]:
    return _load_yaml(AGENTS_DIR / f"{name}.yaml")


def _step(agent: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [step for step in agent["steps"] if step["name"] == name]
    assert len(matches) == 1
    return matches[0]


def _allowed_mcp_tools(step: dict[str, Any]) -> set[str]:
    value = step.get("allowed_mcp_tools") or []
    assert isinstance(value, list)
    return set(value)


def _blocked_mcp_tools(step: dict[str, Any]) -> set[str]:
    value = step.get("blocked_mcp_tools") or []
    assert isinstance(value, list)
    return set(value)


def test_holistic_review_skill_defines_methodology_and_verdict_block() -> None:
    skill_text = (SKILLS_DIR / "holistic-review/SKILL.md").read_text()

    for heading in ("### Scope", "### Reality", "### Testing", "### YAGNI"):
        assert heading in skill_text
    assert "## Holistic Findings" in skill_text
    assert "verdict: approve | request_changes | needs_discussion" in skill_text
    assert "scope: OK | Drift | Gap" in skill_text
    assert "reality: OK | Drift | Gap" in skill_text
    assert "testing: OK | Drift | Gap" in skill_text
    assert "yagni: OK | Drift | Gap" in skill_text


def test_holistic_reviewer_loads_skill_reads_files_and_terminates_cleanly() -> None:
    agent = _agent("holistic-reviewer")
    load_skill = _step(agent, "load_skill")
    review = _step(agent, "review")
    terminate = _step(agent, "terminate")

    assert "gobby-skills:get_skill" in _allowed_mcp_tools(load_skill)
    assert "tool_input.name == 'holistic-review'" in str(load_skill.get("on_mcp_success"))
    assert review["allowed_tools"] == "all"
    assert {
        "gobby-tasks:close_task",
        "gobby-tasks:de_escalate_task",
        "gobby-tasks:mark_task_needs_review",
        "gobby-tasks:reopen_task",
    }.issubset(_blocked_mcp_tools(review))
    assert "gobby-agents:end_agent_run" in _allowed_mcp_tools(terminate)


def test_test_architect_outputs_structured_prose_not_expansion_tasks() -> None:
    instructions = _agent("test-architect")["instructions"]

    assert "## Test Architecture" in instructions
    for heading in (
        "### Integration",
        "### E2E",
        "### Regression",
        "### Contract",
        "### Infrastructure",
    ):
        assert heading in instructions
    assert "Do NOT write `### N.N` task sections" in instructions
    assert "[category: test] leaves" in instructions


def test_qa_reviewer_records_review_verdict_without_closing_task() -> None:
    agent = _agent("qa-reviewer")
    review = _step(agent, "review")
    transitions = review["transitions"]

    instructions = agent["instructions"]
    assert "mark_task_review_approved" in instructions
    assert "mark_task_review_rejected" in instructions
    assert "escalate_task" in instructions
    assert 'reason="qa_approved"' not in instructions
    assert "Do NOT call close_task" in instructions

    blocked_tools = _blocked_mcp_tools(review)
    assert "gobby-tasks:close_task" in blocked_tools

    success_hooks = review["on_mcp_success"]
    assert any(hook["tool"] == "mark_task_review_approved" for hook in success_hooks)
    assert any(hook["tool"] == "mark_task_review_rejected" for hook in success_hooks)
    assert not any(hook["tool"] == "close_task" for hook in success_hooks)
    assert transitions == [{"to": "terminate", "when": "vars.review_complete"}]


@pytest.mark.parametrize(
    ("agent_name", "tool_words"),
    [
        ("developer", {"pytest", "ruff", "uv", "npm", "playwright"}),
        ("frontend-developer", {"npm", "pnpm", "yarn", "playwright", "vite", "eslint"}),
        ("backend-developer", {"pytest", "mypy", "ruff", "sqlite3", "uv", "poetry"}),
    ],
)
def test_developer_agents_support_toolchain_allowlists_and_additional_skills(
    agent_name: str,
    tool_words: set[str],
) -> None:
    agent = _agent(agent_name)
    load_skills = _step(agent, "load_additional_skills")
    implement = _step(agent, "implement")
    terminate = _step(agent, "terminate")

    tool_allowlist = set(agent["skills"]["tool_allowlist"])
    assert tool_words.issubset(tool_allowlist)
    assert "gobby-skills:get_skill" in _allowed_mcp_tools(load_skills)
    assert "additional_skills" in load_skills["status_message"]
    assert "additional_skills_loaded" in agent["step_variables"]
    assert "loaded_skills" in str(load_skills["transitions"])
    assert "gobby-agents:end_agent_run" in _blocked_mcp_tools(implement)
    assert "_skipped_stages" in implement["status_message"]
    assert "close_task" in implement["status_message"]
    assert "mark_task_needs_review" in implement["status_message"]
    assert "gobby-agents:end_agent_run" in _allowed_mcp_tools(terminate)
    assert "gobby-agents:kill_agent" not in _allowed_mcp_tools(terminate)


def test_agent_definition_model_preserves_skills_blocks() -> None:
    from gobby.workflows.definitions import AgentDefinitionBody

    body = AgentDefinitionBody.model_validate(
        {
            "name": "backend-developer",
            "skills": {
                "baseline": ["Python backend"],
                "tool_allowlist": ["pytest", "ruff"],
            },
        }
    )

    assert body.skills == {
        "baseline": ["Python backend"],
        "tool_allowlist": ["pytest", "ruff"],
    }


def test_backend_developer_documents_default_fallback_audit_marker() -> None:
    agent = _agent("backend-developer")
    instructions = agent["instructions"]

    assert "default-agent fallback" in instructions
    assert "## Agent Selection" in instructions
    assert "Defaulted to `backend-developer`" in instructions


def test_planner_relies_on_review_handoff_to_clear_rejected_verdict_label() -> None:
    instructions = " ".join(_agent("planner")["instructions"].split())

    assert "mark_task_needs_review" in instructions
    assert "Do not call remove_label for `planning-current-verdict:rejected`" in instructions
    assert "clears it atomically with the resubmission" in instructions
