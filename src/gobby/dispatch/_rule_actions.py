"""Action builders used by dispatch rules."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import cast

from gobby.dispatch._rule_state import (
    _agent_dispatchable,
    _field,
    _matching_current_stage,
    _prompt_context,
    _registry_entry,
    _stage_name,
    _stage_review_exhausted,
    _stage_revision_review_budget_open,
    _stage_state,
    _stage_work_exhausted,
    _task_id,
    _task_ref,
)
from gobby.dispatch.actions import (
    Action,
    AdvanceStageAction,
    EscalateAction,
    SpawnAgentAction,
    StartPipelineAction,
)
from gobby.dispatch.prompts import PROMPT_BUILDERS

logger = logging.getLogger("gobby.dispatch.rules")

_STAGE_AGENT_SLUGS: dict[tuple[str, str], str] = {
    ("ideation", "in_progress"): "analyst",
    ("research", "in_progress"): "researcher",
    ("architecture", "in_progress"): "architect",
    ("prd", "in_progress"): "product-manager",
    ("planning", "in_progress"): "planner",
    ("planning", "needs_review"): "plan-adversary",
    ("expansion", "needs_review"): "expansion-qa",
    ("holistic_qa", "in_progress"): "holistic-reviewer",
    ("merge", "in_progress"): "merge-orchestrator",
}


def _spawn_on_stage(
    task: object,
    context: object,
    stage_name: str,
    state: str,
    agent_slug: str,
) -> Action | None:
    stage = _matching_current_stage(task, context, stage_name, state)
    if stage is None:
        return None
    if state == "needs_review" and _stage_review_exhausted(stage, context):
        return EscalateAction(task_id=_task_id(task), reason=f"{stage_name}_max_review_rounds")
    if (
        state == "in_progress"
        and _stage_work_exhausted(stage, context)
        and not _stage_revision_review_budget_open(stage, context)
    ):
        return EscalateAction(task_id=_task_id(task), reason=f"{stage_name}_max_work_attempts")
    if not _agent_dispatchable(context, agent_slug):
        return EscalateAction(task_id=_task_id(task), reason=f"{stage_name}_no_agent")
    return _spawn_stage_agent(task, stage, context, agent_slug)


def _spawn_configured_stage_agent(
    task: object,
    context: object,
    stage_name: str,
    state: str,
) -> Action | None:
    agent_slug = _STAGE_AGENT_SLUGS.get((stage_name, state))
    if agent_slug is None:
        return None
    return _spawn_on_stage(task, context, stage_name, state, agent_slug)


def _start_configured_stage_pipeline(
    task: object,
    stage: object,
    context: object,
) -> Action | None:
    stage_name = _stage_name(stage)
    registry_entry = _registry_entry(context, stage_name, stage)
    dispatch_type = _field(registry_entry, "dispatch_type")
    if dispatch_type not in {None, "agent", "pipeline"}:
        return EscalateAction(task_id=_task_id(task), reason=f"{stage_name}_invalid_dispatch_type")
    if dispatch_type != "pipeline":
        return None
    pipeline_name = _field(registry_entry, "dispatch_target")
    if not pipeline_name:
        return EscalateAction(task_id=_task_id(task), reason=f"{stage_name}_missing_pipeline")
    return StartPipelineAction(
        task_id=_task_id(task),
        task_ref=_task_ref(task),
        stage_name=stage_name,
        pipeline_name=str(pipeline_name),
        dispatch_inputs=_dispatch_inputs(registry_entry),
    )


def _dispatch_inputs(registry_entry: object | None) -> dict[str, object]:
    raw = _field(registry_entry, "dispatch_inputs_json")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return cast(dict[str, object], raw)
    if not isinstance(raw, str):
        _log_invalid_dispatch_inputs(registry_entry, raw, TypeError("expected str or dict"))
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log_invalid_dispatch_inputs(registry_entry, raw, exc)
        return {}
    if not isinstance(parsed, dict):
        _log_invalid_dispatch_inputs(registry_entry, raw, TypeError("expected JSON object"))
        return {}
    return cast(dict[str, object], parsed)


def _log_invalid_dispatch_inputs(
    registry_entry: object | None,
    raw: object,
    exc: Exception,
) -> None:
    logger.debug(
        "Invalid stage registry dispatch_inputs_json; ignoring",
        extra={
            "registry_entry": _registry_entry_identity(registry_entry),
            "raw_dispatch_inputs_json": raw,
            "error": str(exc),
        },
        exc_info=(type(exc), exc, exc.__traceback__),
    )


def _registry_entry_identity(registry_entry: object | None) -> object | None:
    if registry_entry is None:
        return None
    identity: dict[str, object] = {}
    for field_name in ("id", "name", "stage_name"):
        value = _field(registry_entry, field_name)
        if value:
            identity[field_name] = value
    return identity or repr(registry_entry)


def _spawn_required_stage_agent(
    task: object,
    context: object,
    stage_name: str,
    state: str,
    *,
    agent_slug: str,
    has_agent: Callable[[object], bool],
    missing_agent_reason: str,
) -> Action | None:
    stage = _matching_current_stage(task, context, stage_name, state)
    if stage is None:
        return None
    if not has_agent(context):
        return EscalateAction(task_id=_task_id(task), reason=missing_agent_reason)
    return _spawn_stage_agent(task, stage, context, agent_slug)


def _complete_stage_on_state(
    task: object,
    context: object,
    stage_name: str,
    state: str,
) -> AdvanceStageAction | None:
    if _matching_current_stage(task, context, stage_name, state) is None:
        return None
    return AdvanceStageAction(
        task_id=_task_id(task),
        stage_name=stage_name,
        method="complete_stage",
    )


def _complete_review_approved_stage(
    task: object,
    context: object,
    stage_name: str,
) -> AdvanceStageAction | None:
    return _complete_stage_on_state(task, context, stage_name, "review_approved")


def _spawn_stage_agent(
    task: object,
    stage: object,
    context: object,
    agent_slug: str,
    *,
    resume_review: bool = False,
) -> SpawnAgentAction:
    prompt_context = _prompt_context(context)
    prompt_context["stage_name"] = _stage_name(stage)
    prompt_context["stage_state"] = _stage_state(stage)
    if resume_review:
        prompt_context["reason"] = "holistic_qa_resume_review"
        prompt_context["resume_review"] = True
    builder = PROMPT_BUILDERS.get(agent_slug) or PROMPT_BUILDERS["default"]
    initial_variables: dict[str, object] = {
        "stage_name": _stage_name(stage),
        "stage_state": _stage_state(stage),
    }
    if resume_review:
        initial_variables["resume_review"] = True
    return SpawnAgentAction(
        task_id=_task_id(task),
        task_ref=_task_ref(task),
        agent_slug=agent_slug,
        prompt=builder(task, prompt_context),
        initial_variables=initial_variables,
        additional_skills=tuple(_field(task, "additional_skills", ()) or ()),
    )


__all__ = [
    "_complete_review_approved_stage",
    "_complete_stage_on_state",
    "_dispatch_inputs",
    "_spawn_configured_stage_agent",
    "_spawn_required_stage_agent",
    "_spawn_stage_agent",
    "_start_configured_stage_pipeline",
]
