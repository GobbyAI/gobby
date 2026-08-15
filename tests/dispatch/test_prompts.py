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
        "epic-reviewer",
        "merge-orchestrator",
        "plan-adversary",
        "plan-adversary-taskless",
        "plan-enhancer",
        "plan-enhancer-taskless",
        "planner",
        "product-manager",
        "qa-reviewer",
        "researcher",
    } <= set(PROMPT_BUILDERS)


def test_plan_enhancer_prompt_builder_renders_round_and_plan_path() -> None:
    from types import SimpleNamespace

    from gobby.dispatch.prompts import PROMPT_BUILDERS

    builder = PROMPT_BUILDERS["plan-enhancer"]
    task = SimpleNamespace(ref="#42", title="Ship the widget")
    artifacts = SimpleNamespace(plan_file_path=".gobby/plans/widget.md")
    prompt = builder(
        task,
        {
            "artifacts": artifacts,
            "round_number": 2,
            "max_enhancement_rounds": 3,
        },
    )

    assert "Enhance the plan" in prompt
    assert "round 2" in prompt
    assert "of at most 3" in prompt
    assert "round_number=2" in prompt
    assert ".gobby/plans/widget.md" in prompt
    # The enhancer is advisory: it must never be told to gate or edit the plan.
    assert "never approve, reject, edit the plan, or write the manifest" in prompt


def test_plan_enhancer_taskless_shares_builder_and_omits_round_when_absent() -> None:
    from types import SimpleNamespace

    from gobby.dispatch.prompts import PROMPT_BUILDERS

    assert PROMPT_BUILDERS["plan-enhancer-taskless"] is PROMPT_BUILDERS["plan-enhancer"]

    builder = PROMPT_BUILDERS["plan-enhancer-taskless"]
    task = SimpleNamespace(ref="#7", title="Thin draft")
    prompt = builder(task, {"reason": "stage"})

    assert "Enhance the plan" in prompt
    assert "enhancement round" not in prompt


def test_qa_reviewer_prompt_builder_registered() -> None:
    from gobby.dispatch.prompts import PROMPT_BUILDERS

    builder = PROMPT_BUILDERS["qa-reviewer"]
    prompt = builder(SimpleNamespace(ref="#42", title="Review me"), {"reason": "qa"})

    assert "qa-reviewer.yaml agent" in prompt
    assert "#42" in prompt
    assert "Spawn-time auto-claim normally already owns the task" in prompt
    assert "Do not call claim_task unless" in prompt
    assert "Do not call get_step_status" in prompt
    assert "Do not run full pytest, Cargo, Vitest, or Jest suites" in prompt
    assert "focused validation" in prompt
    assert "`cargo test -p <package>`" in prompt
    assert "`cargo test <name> -p <package>`" in prompt
    assert "worker-safety hook blocks a command" in prompt
    assert "never retry that blocked command" in prompt
    assert "Run validation commands in the foreground" in prompt
    assert "Do not use shell backgrounding" in prompt
    assert "Monitor, TaskOutput, or tmux polling" in prompt
    assert "do not launch duplicate validation commands" in prompt


def test_prompt_builder_uses_seq_ref_when_loaded_task_has_no_ref() -> None:
    from gobby.dispatch.prompts import PROMPT_BUILDERS

    task = SimpleNamespace(id="2bc4656b-f91a-4434-8272-8167e6cb924b", seq_num=14370, title="E2E")

    prompt = PROMPT_BUILDERS["analyst"](task, {"reason": "stage"})

    assert "#14370" in prompt
    assert "2bc4656b-f91a-4434-8272-8167e6cb924b" not in prompt


def test_planner_prompt_includes_plan_file_path_from_artifacts() -> None:
    from gobby.dispatch.prompts import PROMPT_BUILDERS

    task = SimpleNamespace(ref="#99", title="Plan build")
    artifacts = SimpleNamespace(plan_file_path=".gobby/plans/task-99-plan.md")

    prompt = PROMPT_BUILDERS["planner"](task, {"artifacts": artifacts})

    assert "plan_file_path" in prompt
    assert ".gobby/plans/task-99-plan.md" in prompt


def test_doc_reviewer_prompt_builder_registered() -> None:
    from gobby.dispatch.prompts import PROMPT_BUILDERS

    builder = PROMPT_BUILDERS["doc-reviewer"]
    prompt = builder(SimpleNamespace(ref="#43", title="Review docs"), {"reason": "docs"})

    assert "doc-reviewer.yaml agent" in prompt
    assert "#43" in prompt


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


def test_failure_context_is_capped_with_truncation_marker() -> None:
    from gobby.dispatch.prompts import PROMPT_BUILDERS

    task = SimpleNamespace(ref="#42", title="Follow-up")
    prompt = PROMPT_BUILDERS["developer"](task, {"failure_context": "x" * 2500})
    rendered_context = prompt.split(
        "Previous failure context for this follow-up work:\n", maxsplit=1
    )[1]

    assert len(rendered_context) == 2000
    assert rendered_context.endswith("\n[truncated]")


def test_merge_orchestrator_prompt_builder_registered() -> None:
    from gobby.dispatch.prompts import PROMPT_BUILDERS

    assert "merge-orchestrator" in PROMPT_BUILDERS


def test_merge_worker_prompt_builder_registered() -> None:
    from gobby.dispatch.prompts import PROMPT_BUILDERS

    assert "merge-worker" in PROMPT_BUILDERS


def test_builder_type_alias_exported() -> None:
    from gobby.dispatch.prompts import PromptBuilder

    assert PromptBuilder


def test_plan_review_evidence_uses_stage_native_snapshot_handle() -> None:
    from gobby.dispatch.prompts import attach_plan_review_evidence

    rendered = attach_plan_review_evidence(
        "review this plan",
        evidence_id="evidence-1",
        round_number=2,
    )

    assert rendered.startswith("review this plan\n")
    assert "`get_plan_review_snapshot`" in rendered
    assert "complete decoded snapshot" in rendered
    assert '{"evidence_id":"evidence-1","round_number":2}' in rendered
    assert "<plan-review-snapshot-" not in rendered
