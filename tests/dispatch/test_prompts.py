"""Red tests for dispatcher prompt-builder registry."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def test_all_registered_builders_callable() -> None:
    from gobby.dispatch.prompts import PROMPT_BUILDERS

    assert PROMPT_BUILDERS
    for agent_slug, builder in PROMPT_BUILDERS.items():
        prompt = builder(SimpleNamespace(ref="#1", title="Task"), {"reason": "dispatch"})

        assert agent_slug
        assert isinstance(prompt, str)
        assert "#1" in prompt or "Task" in prompt


def test_dispatch_prompt_builder_keys_present() -> None:
    from gobby.dispatch.prompts import PROMPT_BUILDERS

    assert {
        "analyst",
        "architect",
        "backend-developer",
        "developer",
        "doc-reviewer",
        "expansion-qa",
        "fullstack-developer",
        "holistic-reviewer",
        "merge-orchestrator",
        "plan-adversary",
        "plan-adversary-taskless",
        "planner",
        "product-manager",
        "qa-reviewer",
        "researcher",
    } <= set(PROMPT_BUILDERS)


def test_qa_reviewer_prompt_builder_registered() -> None:
    from gobby.dispatch.prompts import PROMPT_BUILDERS

    builder = PROMPT_BUILDERS["qa-reviewer"]
    prompt = builder(SimpleNamespace(ref="#42", title="Review me"), {"reason": "qa"})

    assert "qa-reviewer.yaml agent" in prompt
    assert "#42" in prompt


def test_prompt_builder_uses_seq_ref_when_loaded_task_has_no_ref() -> None:
    from gobby.dispatch.prompts import PROMPT_BUILDERS

    task = SimpleNamespace(id="2bc4656b-f91a-4434-8272-8167e6cb924b", seq_num=14370, title="E2E")

    prompt = PROMPT_BUILDERS["analyst"](task, {"reason": "stage"})

    assert "#14370" in prompt
    assert "2bc4656b-f91a-4434-8272-8167e6cb924b" not in prompt


def test_doc_reviewer_prompt_builder_registered() -> None:
    from gobby.dispatch.prompts import PROMPT_BUILDERS

    builder = PROMPT_BUILDERS["doc-reviewer"]
    prompt = builder(SimpleNamespace(ref="#43", title="Review docs"), {"reason": "docs"})

    assert "doc-reviewer.yaml agent" in prompt
    assert "#43" in prompt


def test_holistic_reviewer_prompt_builder_registered() -> None:
    from gobby.dispatch.prompts import PROMPT_BUILDERS

    assert "holistic-reviewer" in PROMPT_BUILDERS


@pytest.mark.parametrize(
    ("agent_slug", "stage_name", "section_title"),
    [
        ("analyst", "ideation", "Discovery Brief"),
        ("researcher", "research", "Research Findings"),
        ("architect", "architecture", "Architecture Brief"),
        ("product-manager", "prd", "Product Reference Document"),
    ],
)
def test_discovery_prompt_builders_registered(
    agent_slug: str,
    stage_name: str,
    section_title: str,
) -> None:
    from gobby.dispatch.prompts import PROMPT_BUILDERS

    builder = PROMPT_BUILDERS[agent_slug]
    prompt = builder(SimpleNamespace(ref="#91", title="Discover me"), {"reason": "stage"})

    assert "assigned_task_id" in prompt
    assert "discovery marker blocks" in prompt
    assert "end_agent_run" in prompt
    assert f"complete the {stage_name} stage" in prompt
    assert f"## {section_title}" in prompt
    assert "#91" in prompt


def test_developer_prompt_builder_registered() -> None:
    from gobby.dispatch.prompts import PROMPT_BUILDERS

    assert "developer" in PROMPT_BUILDERS


def test_merge_orchestrator_prompt_builder_registered() -> None:
    from gobby.dispatch.prompts import PROMPT_BUILDERS

    assert "merge-orchestrator" in PROMPT_BUILDERS


def test_merge_worker_prompt_builder_registered() -> None:
    from gobby.dispatch.prompts import PROMPT_BUILDERS

    assert "merge-worker" in PROMPT_BUILDERS


def test_builder_type_alias_exported() -> None:
    from gobby.dispatch.prompts import PromptBuilder

    assert PromptBuilder
