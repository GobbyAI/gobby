"""Prompt builders for lifecycle dispatcher agent spawns."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping

PromptBuilder = Callable[[object, Mapping[str, object]], str]

_FAILURE_CONTEXT_MAX_CHARS = 2000
_FAILURE_CONTEXT_TRUNCATION_MARKER = "\n[truncated]"


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


def _artifact_value(context: Mapping[str, object], name: str) -> str | None:
    artifacts = context.get("artifacts")
    value = _field(artifacts, name) if artifacts is not None else None
    return str(value) if value not in (None, "") else None


def _bounded_failure_context(context: Mapping[str, object]) -> str | None:
    value = _context_value(context, "failure_context")
    if value is None or len(value) <= _FAILURE_CONTEXT_MAX_CHARS:
        return value
    prefix_length = _FAILURE_CONTEXT_MAX_CHARS - len(_FAILURE_CONTEXT_TRUNCATION_MARKER)
    return f"{value[:prefix_length]}{_FAILURE_CONTEXT_TRUNCATION_MARKER}"


def _prompt(task: object, context: Mapping[str, object], *, role: str, contract: str) -> str:
    reason = _context_value(context, "reason")
    reason_line = f"\nDispatch reason: {reason}." if reason else ""
    failure_context = _bounded_failure_context(context)
    failure_block = (
        f"\n\nPrevious failure context for this follow-up work:\n{failure_context}"
        if failure_context
        else ""
    )
    return (
        f"{role} for {_task_ref(task)}: {_task_title(task)}.\n"
        f"Follow the {contract} contract for this task.{reason_line}{failure_block}"
    )


def _planner(task: object, context: Mapping[str, object]) -> str:
    base = _prompt(
        task,
        context,
        role="Revise the plan",
        contract="planner.yaml agent",
    )
    plan_file_path = _artifact_value(context, "plan_file_path")
    plan_file_line = (
        f"\nUse plan_file_path as the exact plan artifact path to edit: {plan_file_path}."
        if plan_file_path
        else ""
    )
    return (
        f"{base}{plan_file_line}\nTreat discovery marker blocks in the task description as "
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
        "Use assigned_task_id as the task to claim, read, and update.\n"
        "Treat existing discovery marker blocks in the task description as "
        "authoritative upstream context.\n"
        f"Update only the {marker_name} marker block and include one "
        f"`## {section_title}` section.\n"
        "After verifying the persisted marker block, call gobby-agents:end_agent_run; "
        f"the dispatcher will complete the {stage_name} stage after validating the artifact."
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


def attach_plan_review_evidence(
    prompt: str,
    *,
    evidence_id: str,
    round_number: int,
) -> str:
    """Append the immutable stage-native evidence handle to an adversary prompt."""
    metadata = json.dumps(
        {
            "evidence_id": evidence_id,
            "round_number": round_number,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "\n".join(
        [
            prompt,
            "",
            "## Immutable Plan Review Evidence",
            "",
            "Use this evidence handle as the complete review target. Call "
            "`get_plan_review_snapshot` once with its evidence_id and review the complete "
            "decoded snapshot returned by the daemon. Pass evidence_id and "
            "round_number with the structured verdict.",
            "",
            "```json",
            metadata,
            "```",
        ]
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
    base = _prompt(
        task,
        context,
        role="Review the implementation",
        contract="qa-reviewer.yaml agent",
    )
    return (
        f"{base}\n"
        "Spawn-time auto-claim normally already owns the task. Do not call "
        "claim_task unless the active step prompt explicitly says the current "
        "step is CLAIM. Load the required QA skills before review, then use "
        "get_task, get_task_diff, and exactly one review verdict tool. Do not "
        "treat the first diff page as complete: follow byte_end and both "
        "cursor_end values with the returned snapshot_hash and view_hash. Do not "
        "call get_step_status. Do not run full pytest, Cargo, Vitest, or "
        "Jest suites; run only focused validation relevant to the task diff. "
        "For Rust, use `cargo test -p <package>` or "
        "`cargo test <name> -p <package>`. If a "
        "worker-safety hook blocks a command, read the block reason, switch to "
        "a focused file/filter command, and never retry that blocked command. "
        "Run validation commands in the foreground. Do not use shell "
        "backgrounding, detached commands, shell sleep loops, Monitor, "
        "TaskOutput, or tmux polling to observe validation. If validation "
        "cannot complete or be observed promptly, interrupt or abandon it once, "
        "record the limitation in verdict evidence, and do not launch duplicate "
        "validation commands."
    )


def _trajectory_monitor(task: object, context: Mapping[str, object]) -> str:
    base = _prompt(
        task,
        context,
        role="Audit the full implementation trajectory",
        contract="trajectory-monitor.yaml agent",
    )
    return (
        f"{base}\n"
        "Resolve the authoritative workspace and target branch from task "
        "artifacts before inspecting git history. Audit every linked commit, "
        "the cumulative merge-base..HEAD diff, unlinked branch commits, and "
        "the post-approval delta against the task and plan scope. Emit exactly "
        "one PR-stage verdict, then terminate."
    )


def _doc_reviewer(task: object, context: Mapping[str, object]) -> str:
    return _prompt(
        task,
        context,
        role="Review the documentation implementation",
        contract="doc-reviewer.yaml agent",
    )


def _epic_reviewer(task: object, context: Mapping[str, object]) -> str:
    return _prompt(
        task,
        context,
        role="Perform epic review",
        contract="epic-reviewer.yaml agent",
    )


def _merge_runner(task: object, context: Mapping[str, object]) -> str:
    return _prompt(
        task,
        context,
        role="Run the merge flow",
        contract="merge agent",
    )


def _plan_enhancer(task: object, context: Mapping[str, object]) -> str:
    base = _prompt(
        task,
        context,
        role="Enhance the plan",
        contract="plan-enhancer.yaml agent",
    )
    round_number = _context_value(context, "round_number")
    if round_number:
        max_rounds = _context_value(context, "max_enhancement_rounds")
        of_clause = f" of at most {max_rounds}" if max_rounds else ""
        round_line = (
            f"\nThis is enhancement round {round_number}{of_clause}; "
            f"record it with round_number={round_number}."
        )
    else:
        round_line = ""
    plan_file_path = _artifact_value(context, "plan_file_path")
    plan_file_line = (
        f"\nUse plan_file_path as the exact plan artifact path to read: {plan_file_path}."
        if plan_file_path
        else ""
    )
    return (
        f"{base}{round_line}{plan_file_line}\nProduce ranked Better/Bigger suggestions only; "
        "never approve, reject, edit the plan, or write the manifest."
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
    "fullstack-developer": _developer,
    "epic-reviewer": _epic_reviewer,
    "merge-orchestrator": _merge_runner,
    "merge-worker": _merge_runner,
    "plan-adversary": _plan_adversary,
    "plan-adversary-taskless": _plan_adversary,
    "plan-enhancer": _plan_enhancer,
    "plan-enhancer-taskless": _plan_enhancer,
    "plan-reviewer": _plan_adversary,
    "planner": _planner,
    "product-manager": _product_manager,
    "qa-dev": _qa_dev,
    "qa-reviewer": _qa_reviewer,
    "trajectory-monitor": _trajectory_monitor,
    "reviewer": _epic_reviewer,
    "researcher": _researcher,
    "tech-writer": _developer,
}


__all__ = ["PROMPT_BUILDERS", "PromptBuilder"]
