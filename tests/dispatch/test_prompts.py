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
        "backend-developer",
        "developer",
        "expansion-qa",
        "holistic-reviewer",
        "merge-orchestrator",
        "plan-adversary",
        "planner",
        "qa-reviewer",
        "test-architect",
    } <= set(PROMPT_BUILDERS)


def test_qa_reviewer_prompt_builder_registered() -> None:
    from gobby.dispatch.prompts import PROMPT_BUILDERS

    builder = PROMPT_BUILDERS["qa-reviewer"]
    prompt = builder(SimpleNamespace(ref="#42", title="Review me"), {"reason": "qa"})

    assert "qa-reviewer.yaml agent" in prompt
    assert "#42" in prompt


def test_holistic_reviewer_prompt_builder_registered() -> None:
    from gobby.dispatch.prompts import PROMPT_BUILDERS

    assert "holistic-reviewer" in PROMPT_BUILDERS


def test_test_architect_prompt_builder_registered() -> None:
    from gobby.dispatch.prompts import PROMPT_BUILDERS

    builder = PROMPT_BUILDERS["test-architect"]
    prompt = builder(SimpleNamespace(ref="#77", title="Design tests"), {"reason": "test arch"})

    assert "Draft the test architecture" in prompt
    assert "test-architect.yaml agent" in prompt
    assert "#77" in prompt


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
