"""Prompt builders for lifecycle dispatcher agent spawns."""

from __future__ import annotations

from collections.abc import Callable, Mapping

PromptBuilder = Callable[[object, Mapping[str, object]], str]


def _field(obj: object, name: str, default: object = "") -> object:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _task_ref(task: object) -> str:
    value = _field(task, "ref") or _field(task, "task_ref")
    if value:
        return str(value)
    seq_num = _field(task, "seq_num")
    if seq_num not in (None, ""):
        return f"#{seq_num}"
    value = _field(task, "id")
    return str(value or "the assigned task")


def _task_title(task: object) -> str:
    value = _field(task, "title")
    return str(value or _task_ref(task))


def _context_value(context: Mapping[str, object], name: str) -> str | None:
    value = context.get(name)
    return str(value) if value not in (None, "") else None


def _prompt(task: object, context: Mapping[str, object], *, role: str, contract: str) -> str:
    reason = _context_value(context, "reason")
    reason_line = f"\nDispatch reason: {reason}." if reason else ""
    return (
        f"{role} for {_task_ref(task)}: {_task_title(task)}.\n"
        f"Follow the {contract} contract for this task.{reason_line}"
    )


def _planner(task: object, context: Mapping[str, object]) -> str:
    base = _prompt(
        task,
        context,
        role="Revise the plan",
        contract="planner.yaml agent",
    )
    return (
        f"{base}\nTreat discovery marker blocks in the task description as "
        "authoritative upstream context for planning."
    )


def _discovery_stage(
    task: object,
    context: Mapping[str, object],
    *,
    role: str,
    contract: str,
    stage_name: str,
    marker_name: str,
    section_title: str,
) -> str:
    base = _prompt(task, context, role=role, contract=contract)
    return (
        f"{base}\n"
        "Use assigned_task_id as the task to claim, read, update, and complete.\n"
        "Treat existing discovery marker blocks in the task description as "
        "authoritative upstream context.\n"
        f"Update only the {marker_name} marker block and include one "
        f"`## {section_title}` section.\n"
        f"Complete the stage with stage_name='{stage_name}'."
    )


def _analyst(task: object, context: Mapping[str, object]) -> str:
    return _discovery_stage(
        task,
        context,
        role="Run ideation discovery",
        contract="analyst.yaml agent",
        stage_name="ideation",
        marker_name="ideation",
        section_title="Discovery Brief",
    )


def _researcher(task: object, context: Mapping[str, object]) -> str:
    return _discovery_stage(
        task,
        context,
        role="Run research discovery",
        contract="researcher.yaml agent",
        stage_name="research",
        marker_name="research",
        section_title="Research Findings",
    )


def _architect(task: object, context: Mapping[str, object]) -> str:
    return _discovery_stage(
        task,
        context,
        role="Run architecture discovery",
        contract="architect.yaml agent",
        stage_name="architecture",
        marker_name="architecture",
        section_title="Architecture Brief",
    )


def _product_manager(task: object, context: Mapping[str, object]) -> str:
    return _discovery_stage(
        task,
        context,
        role="Run PRD discovery",
        contract="product-manager.yaml agent",
        stage_name="prd",
        marker_name="prd",
        section_title="Product Reference Document",
    )


def _plan_adversary(task: object, context: Mapping[str, object]) -> str:
    return _prompt(
        task,
        context,
        role="Review the plan",
        contract="plan-adversary.yaml agent",
    )


def _expansion_qa(task: object, context: Mapping[str, object]) -> str:
    return _prompt(
        task,
        context,
        role="Validate the expansion output",
        contract="expansion-qa.yaml agent",
    )


def _developer(task: object, context: Mapping[str, object]) -> str:
    return _prompt(
        task,
        context,
        role="Implement the task",
        contract="developer agent",
    )


def _qa_dev(task: object, context: Mapping[str, object]) -> str:
    return _prompt(
        task,
        context,
        role="Fix QA findings",
        contract="qa-dev.yaml agent",
    )


def _qa_reviewer(task: object, context: Mapping[str, object]) -> str:
    return _prompt(
        task,
        context,
        role="Review the implementation",
        contract="qa-reviewer.yaml agent",
    )


def _doc_reviewer(task: object, context: Mapping[str, object]) -> str:
    return _prompt(
        task,
        context,
        role="Review the documentation implementation",
        contract="doc-reviewer.yaml agent",
    )


def _holistic_reviewer(task: object, context: Mapping[str, object]) -> str:
    return _prompt(
        task,
        context,
        role="Perform holistic review",
        contract="holistic-reviewer.yaml agent",
    )


def _merge_runner(task: object, context: Mapping[str, object]) -> str:
    return _prompt(
        task,
        context,
        role="Run the merge flow",
        contract="merge agent",
    )


def _default(task: object, context: Mapping[str, object]) -> str:
    return _prompt(
        task,
        context,
        role="Work the assigned task",
        contract="default.yaml agent",
    )


PROMPT_BUILDERS: dict[str, PromptBuilder] = {
    "analyst": _analyst,
    "architect": _architect,
    "backend-developer": _developer,
    "default": _default,
    "developer": _developer,
    "doc-reviewer": _doc_reviewer,
    "expansion-qa": _expansion_qa,
    "frontend-developer": _developer,
    "holistic-reviewer": _holistic_reviewer,
    "merge-orchestrator": _merge_runner,
    "merge-worker": _merge_runner,
    "plan-adversary": _plan_adversary,
    "plan-reviewer": _plan_adversary,
    "planner": _planner,
    "product-manager": _product_manager,
    "qa-dev": _qa_dev,
    "qa-reviewer": _qa_reviewer,
    "reviewer": _holistic_reviewer,
    "researcher": _researcher,
    "tech-writer": _developer,
}


__all__ = ["PROMPT_BUILDERS", "PromptBuilder"]
