"""Prompt builders for lifecycle dispatcher agent spawns."""

from __future__ import annotations

from collections.abc import Callable, Mapping

PromptBuilder = Callable[[object, Mapping[str, object]], str]


def _field(obj: object, name: str, default: object = "") -> object:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _task_ref(task: object) -> str:
    value = _field(task, "ref") or _field(task, "task_ref") or _field(task, "id")
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
    return _prompt(
        task,
        context,
        role="Revise the plan",
        contract="planner.yaml agent",
    )


def _plan_adversary(task: object, context: Mapping[str, object]) -> str:
    return _prompt(
        task,
        context,
        role="Review the plan",
        contract="plan-adversary.yaml agent",
    )


def _test_architect(task: object, context: Mapping[str, object]) -> str:
    return _prompt(
        task,
        context,
        role="Draft the test architecture",
        contract="test-architect.yaml agent",
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
    "backend-developer": _developer,
    "default": _default,
    "developer": _developer,
    "expansion-qa": _expansion_qa,
    "frontend-developer": _developer,
    "holistic-reviewer": _holistic_reviewer,
    "merge-orchestrator": _merge_runner,
    "merge-worker": _merge_runner,
    "plan-adversary": _plan_adversary,
    "plan-reviewer": _plan_adversary,
    "planner": _planner,
    "qa-dev": _qa_dev,
    "qa-reviewer": _qa_reviewer,
    "reviewer": _holistic_reviewer,
    "test-architect": _test_architect,
}


__all__ = ["PROMPT_BUILDERS", "PromptBuilder"]
