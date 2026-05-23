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


def test_build_smoke_agent_runtime_mappings() -> None:
    expected = {
        "backend-developer": ("codex", "gpt-5.5", "xhigh"),
        "fullstack-developer": ("codex", "gpt-5.5", "xhigh"),
        "frontend-developer": ("codex", "gpt-5.5", "high"),
        "tech-writer": ("codex", "gpt-5.5", "high"),
        "qa-reviewer": ("claude", "opus", "xhigh"),
        "doc-reviewer": ("claude", "opus", "xhigh"),
        "holistic-reviewer": ("codex", "gpt-5.5", "xhigh"),
        "merge-orchestrator": ("claude", "opus", "xhigh"),
        "merge-worker": ("gemini", "gemini-3.1-pro-preview", "high"),
    }

    for agent_name, (provider, model, reasoning_effort) in expected.items():
        agent = _agent(agent_name)

        assert agent["enabled"] is True
        assert agent["provider"] == provider
        assert agent["model"] == model
        assert agent["reasoning_effort"] == reasoning_effort


def test_holistic_review_skill_defines_methodology_and_verdict_block() -> None:
    skill_text = (SKILLS_DIR / "holistic-review/SKILL.md").read_text()

    for heading in (
        "### spec_compliance",
        "### code_quality",
        "### testing",
        "### yagni",
    ):
        assert heading in skill_text
    assert "## Holistic Findings" in skill_text
    assert "verdict: approve | request_changes | needs_discussion" in skill_text
    assert "spec_compliance: OK | Drift | Gap" in skill_text
    assert "code_quality: OK | Drift | Gap" in skill_text
    assert "testing: OK | Drift | Gap" in skill_text
    assert "yagni: OK | Drift | Gap" in skill_text
    assert "operational_risk" not in skill_text


def test_holistic_review_skill_allows_docs_epic_plan_substitute() -> None:
    skill_text = (SKILLS_DIR / "holistic-review/SKILL.md").read_text()

    assert "Discovery Brief" in skill_text
    assert "descendant task set" in skill_text
    assert "do not escalate solely" in skill_text
    assert 'complete_stage(stage_name="holistic_qa")' in skill_text
    assert 'fail_stage(stage_name="holistic_qa")' in skill_text
    assert 'approve_review(stage_name="holistic_qa")' not in skill_text
    assert 'reject_review(stage_name="holistic_qa")' not in skill_text


def test_holistic_reviewer_loads_skill_reads_files_and_terminates_cleanly() -> None:
    agent = _agent("holistic-reviewer")
    load_skill = _step(agent, "load_skill")
    review = _step(agent, "review")
    terminate = _step(agent, "terminate")

    assert "gobby-skills:get_skill" in _allowed_mcp_tools(load_skill)
    assert {
        "code-index",
        "holistic-review",
        "tech-writer",
        "task-transitions",
        "verification-before-completion",
    }.issubset(set(agent["step_variables"]["required_skills"]))
    for skill_name in agent["step_variables"]["required_skills"]:
        assert f'get_skill(name="{skill_name}")' in load_skill["status_message"]
    assert "Discovery Brief" in agent["instructions"]
    assert "descendant task set" in agent["instructions"]
    assert review["allowed_tools"] == "all"
    assert {
        "gobby-tasks:close_task",
        "gobby-tasks:de_escalate_task",
        "gobby-tasks-ops:submit_for_review",
        "gobby-tasks-ops:approve_review",
        "gobby-tasks-ops:reject_review",
        "gobby-tasks:reopen_task",
    }.issubset(_blocked_mcp_tools(review))
    assert "gobby-agents:end_agent_run" in _allowed_mcp_tools(terminate)


def test_test_architecture_skill_outputs_structured_prose_not_expansion_tasks() -> None:
    skill_text = (SKILLS_DIR / "test-architecture/SKILL.md").read_text(encoding="utf-8")

    assert "## Test Architecture" in skill_text
    for heading in (
        "### Integration",
        "### E2E",
        "### Regression",
        "### Contract",
        "### Infrastructure",
    ):
        assert heading in skill_text
    assert "Do NOT write `### N.N` task sections" in skill_text
    assert "[category: test] leaves" in skill_text


def test_architect_requires_architecture_and_test_architecture_sections() -> None:
    agent = _agent("architect")
    load_skill = _step(agent, "load_skill")

    assert {"architecture", "test-architecture"}.issubset(set(agent["skills"]["methodology"]))
    assert "## Architecture Brief" in agent["instructions"]
    assert "## Test Architecture" in agent["instructions"]
    assert "tool_input.name == 'test-architecture'" in str(load_skill.get("on_mcp_success"))


def test_qa_reviewer_records_review_verdict_without_closing_task() -> None:
    agent = _agent("qa-reviewer")
    review = _step(agent, "review")
    transitions = review["transitions"]

    instructions = agent["instructions"]
    assert "approve_review" in instructions
    assert "reject_review" in instructions
    assert "escalate_task" in instructions
    assert 'reason="qa_approved"' not in instructions
    assert "Do NOT call close_task" in instructions

    blocked_tools = _blocked_mcp_tools(review)
    assert "gobby-tasks:close_task" in blocked_tools

    success_hooks = review["on_mcp_success"]
    assert any(hook["tool"] == "approve_review" for hook in success_hooks)
    assert any(hook["tool"] == "reject_review" for hook in success_hooks)
    assert not any(hook["tool"] == "close_task" for hook in success_hooks)
    assert transitions == [{"to": "terminate", "when": "vars.review_complete"}]


@pytest.mark.parametrize(
    ("agent_name", "tool_words"),
    [
        ("frontend-developer", {"npm", "pnpm", "yarn", "playwright", "vite", "eslint"}),
        ("backend-developer", {"pytest", "mypy", "ruff", "sqlite3", "uv", "poetry"}),
        ("fullstack-developer", {"pytest", "ruff", "npm", "pnpm", "playwright", "uv"}),
    ],
)
def test_developer_agents_support_toolchain_allowlists_and_additional_skills(
    agent_name: str,
    tool_words: set[str],
) -> None:
    agent = _agent(agent_name)
    load_required = _step(agent, "load_required_skills")
    load_skills = _step(agent, "load_additional_skills")
    implement = _step(agent, "implement")
    terminate = _step(agent, "terminate")

    tool_allowlist = set(agent["skills"]["tool_allowlist"])
    assert tool_words.issubset(tool_allowlist)
    assert agent["step_variables"]["required_skills"] == [
        "development-discipline",
        "task-transitions",
        "verification-before-completion",
    ]
    assert "gobby-skills:get_skill" in _allowed_mcp_tools(load_required)
    for skill_name in agent["step_variables"]["required_skills"]:
        assert f'get_skill(name="{skill_name}")' in load_required["status_message"]
    assert "development-discipline" in agent["instructions"]
    assert "task-transitions" in agent["instructions"]
    assert "verification-before-completion" in agent["instructions"]
    assert "test-driven-development" in agent["instructions"]
    assert "gobby-skills:get_skill" in _allowed_mcp_tools(load_skills)
    assert "additional_skills" in load_skills["status_message"]
    assert "additional_skills_loaded" in agent["step_variables"]
    assert "loaded_skills" in str(load_skills["transitions"])
    assert "gobby-agents:end_agent_run" in _blocked_mcp_tools(implement)
    assert "_skipped_stages" not in implement["status_message"]
    assert "manifest" in implement["status_message"]
    assert "close_task" in implement["status_message"]
    assert "submit_for_review" in implement["status_message"]
    assert "gobby-agents:end_agent_run" in _allowed_mcp_tools(terminate)
    assert "gobby-agents:kill_agent" not in _allowed_mcp_tools(terminate)


def test_tdd_discipline_skills_are_bundled() -> None:
    discipline = (SKILLS_DIR / "development-discipline/SKILL.md").read_text()
    tdd = (SKILLS_DIR / "test-driven-development/SKILL.md").read_text()

    assert "test judgment" in discipline.lower()
    assert "test-driven-development" in discipline
    assert "red" in tdd.lower()
    assert "minimal green" in tdd.lower()
    assert "refactor/final-green" in tdd.lower()
    assert "test-quality audit" in tdd.lower()


def test_qa_and_holistic_reviewers_check_tdd_required_evidence() -> None:
    qa = _agent("qa-reviewer")
    holistic = _agent("holistic-reviewer")
    qa_skill = (SKILLS_DIR / "qa/SKILL.md").read_text()
    holistic_skill = (SKILLS_DIR / "holistic-review/SKILL.md").read_text()

    for text in (
        qa["instructions"],
        _step(qa, "review")["status_message"],
        holistic["instructions"],
        _step(holistic, "review")["status_message"],
        qa_skill,
        holistic_skill,
    ):
        assert "tdd:required" in text
        assert "test-driven-development" in text
        assert "red" in text.lower()
        assert "green" in text.lower()
        assert "test-quality" in text.lower()


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


def test_triage_agent_uses_current_agent_schema_and_methodology_skill() -> None:
    from gobby.workflows.definitions import AgentDefinitionBody

    agent = _agent("triage-agent")
    body = AgentDefinitionBody.model_validate(agent)

    assert "prompt" not in agent
    assert agent["skills"] == {"methodology": ["triage-judgment"]}
    assert body.instructions is not None
    assert "Return structured JSON only" in body.instructions


def test_backend_developer_documents_default_fallback_audit_marker() -> None:
    agent = _agent("backend-developer")
    instructions = agent["instructions"]

    assert "default-agent fallback" in instructions
    assert "## Agent Selection" in instructions
    assert "Defaulted to `backend-developer`" in instructions


def test_tech_writer_loads_methodology_skill_after_claim() -> None:
    agent = _agent("tech-writer")
    claim = _step(agent, "claim")
    load_skill = _step(agent, "load_skills")
    implement = _step(agent, "implement")

    assert claim["transitions"] == [{"to": "load_skills", "when": "vars.task_claimed"}]
    assert "gobby-skills:get_skill" in _allowed_mcp_tools(load_skill)
    assert {"tech-writer", "task-transitions", "verification-before-completion"}.issubset(
        set(agent["step_variables"]["required_skills"])
    )
    assert 'get_skill(name="tech-writer")' in load_skill["status_message"]
    assert "submit_for_review" in implement["status_message"]


def test_planner_relies_on_review_handoff_to_clear_rejected_verdict_label() -> None:
    instructions = " ".join(_agent("planner")["instructions"].split())

    assert "submit_for_review" in instructions
    assert "Do not call remove_label for `planning-current-verdict:rejected`" in instructions
    assert "clears it atomically with the resubmission" in instructions
