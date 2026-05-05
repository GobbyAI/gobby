"""Ordered pure decision rules for stage-native dispatch."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from typing import Any, cast

from gobby.dispatch.actions import (
    Action,
    AdvanceStageAction,
    CreateIsolationAction,
    EscalateAction,
    SpawnAgentAction,
    StartPipelineAction,
    StartStageAction,
)
from gobby.dispatch.prompts import PROMPT_BUILDERS

logger = logging.getLogger(__name__)

Rule = Callable[[object, object], Action | None]

_STAGE_AGENT_SLUGS: dict[tuple[str, str], str] = {
    ("ideation", "in_progress"): "analyst",
    ("research", "in_progress"): "researcher",
    ("architecture", "in_progress"): "architect",
    ("prd", "in_progress"): "product-manager",
    ("planning", "in_progress"): "planner",
    ("planning", "needs_review"): "plan-adversary",
    ("test_arch", "in_progress"): "test-architect",
    ("expansion", "needs_review"): "expansion-qa",
    ("holistic_qa", "in_progress"): "holistic-reviewer",
    ("merge", "in_progress"): "merge-orchestrator",
}

NON_MERGE_TERMINAL_MANIFEST_EXHAUSTION = {
    "research_spike": ("ideation.ready", "research.ready", "prd.done", "manifest_exhausted"),
    "prd_doc": ("ideation.ready", "prd.done", "manifest_exhausted"),
    "architecture_doc": ("research.ready", "architecture.done", "manifest_exhausted"),
}
DISABLED_DISCOVERY_AGENT_ESCALATION_REASONS = {
    "ideation": "ideation_no_agent",
    "research": "research_no_agent",
    "architecture": "architecture_no_agent",
    "prd": "prd_no_agent",
}

_AUTO_ADVANCE_NON_AGENT_STAGES = {"expansion", "pr"}
_AUTO_ADVANCE_DEDICATED_STAGES = {"development", "holistic_qa"}
_DISABLED_AGENT_EXCLUDED_STAGES = {
    "expansion",
    "pr",
    "development",
    "holistic_qa",
}


def evaluate(task: object, context: object, rules: Sequence[Rule] | None = None) -> Action | None:
    """Return the first action emitted by the ordered rule list."""
    for rule in rules or RULES:
        action = rule(task, context)
        if action is not None:
            return action
    return None


def auto_advance_ready_rule(task: object, context: object) -> Action | None:
    stage = _current_stage(task, context)
    if stage is None or _stage_state(stage) != "ready":
        return None
    stage_name = _stage_name(stage)
    if stage_name in _AUTO_ADVANCE_DEDICATED_STAGES:
        return None
    if not _previous_stage_done(task, stage):
        return None
    registry_entry = _registry_entry(context, stage_name, stage)
    if bool(_field(registry_entry, "requires_human", _field(stage, "requires_human", False))):
        return None
    if stage_name not in _AUTO_ADVANCE_NON_AGENT_STAGES:
        if not _default_agent(stage, context):
            return None
        if not stage_agent_available(context, stage_name):
            return None
    return StartStageAction(task_id=_task_id(task), stage_name=stage_name)


def disabled_agent_escalation_rule(task: object, context: object) -> Action | None:
    stage = _current_stage(task, context)
    if stage is None or _stage_state(stage) != "ready":
        return None
    stage_name = _stage_name(stage)
    if stage_name in _DISABLED_AGENT_EXCLUDED_STAGES:
        return None
    if not _default_agent(stage, context):
        return None
    if stage_agent_available(context, stage_name):
        return None
    reason = DISABLED_DISCOVERY_AGENT_ESCALATION_REASONS.get(stage_name, f"{stage_name}_no_agent")
    return EscalateAction(task_id=_task_id(task), reason=reason)


def development_isolation_rule(task: object, context: object) -> Action | None:
    stage = _matching_current_stage(task, context, "development", "ready")
    if stage is None or not _is_leaf(task):
        return None

    isolation = _isolation(task)
    if isolation == "none":
        return StartStageAction(task_id=_task_id(task), stage_name=_stage_name(stage))
    if isolation not in {"worktree", "clone"}:
        return EscalateAction(
            task_id=_task_id(task), reason=f"development_isolation_invalid:{isolation}"
        )

    artifacts = _artifacts(task, context)
    if _has_isolation_pair(artifacts, isolation):
        return StartStageAction(task_id=_task_id(task), stage_name=_stage_name(stage))
    return CreateIsolationAction(
        task_id=_task_id(task),
        task_ref=_task_ref(task),
        isolation=isolation,
        base_branch=_field(artifacts, "target_branch"),
    )


def all_leaves_holistic_rule(task: object, context: object) -> Action | None:
    stage = _matching_current_stage(task, context, "holistic_qa", "ready")
    if stage is None or not _is_epic(task):
        return None
    children = list(_children(task, context))
    if not children:
        return None
    if not all(is_child_parked(child) or _is_closed(child) for child in children):
        return None
    return StartStageAction(task_id=_task_id(task), stage_name="holistic_qa")


def ideation_rule(task: object, context: object) -> Action | None:
    return _spawn_configured_stage_agent(task, context, "ideation", "in_progress")


def research_rule(task: object, context: object) -> Action | None:
    return _spawn_configured_stage_agent(task, context, "research", "in_progress")


def architecture_rule(task: object, context: object) -> Action | None:
    return _spawn_configured_stage_agent(task, context, "architecture", "in_progress")


def prd_rule(task: object, context: object) -> Action | None:
    return _spawn_configured_stage_agent(task, context, "prd", "in_progress")


def planning_work_rule(task: object, context: object) -> Action | None:
    return _spawn_configured_stage_agent(task, context, "planning", "in_progress")


def planning_review_rule(task: object, context: object) -> Action | None:
    return _spawn_configured_stage_agent(task, context, "planning", "needs_review")


def planning_advance_rule(task: object, context: object) -> Action | None:
    return _complete_review_approved_stage(task, context, "planning")


def test_arch_rule(task: object, context: object) -> Action | None:
    return _spawn_configured_stage_agent(task, context, "test_arch", "in_progress")


def expansion_work_rule(task: object, context: object) -> Action | None:
    stage = _matching_current_stage(task, context, "expansion", "in_progress")
    if stage is None or _stage_work_exhausted(stage, context):
        return None
    return _start_configured_stage_pipeline(task, stage, context)


def expansion_review_rule(task: object, context: object) -> Action | None:
    stage = _matching_current_stage(task, context, "expansion", "needs_review")
    if stage is None:
        return None
    if _stage_review_exhausted(stage, context):
        return EscalateAction(task_id=_task_id(task), reason="expansion_max_review_rounds")
    if not _has_agent(context, "expansion-qa"):
        return EscalateAction(task_id=_task_id(task), reason="expansion_no_reviewer")
    return _spawn_stage_agent(task, stage, context, "expansion-qa")


def expansion_advance_rule(task: object, context: object) -> Action | None:
    return _complete_review_approved_stage(task, context, "expansion")


def development_rule(task: object, context: object) -> Action | None:
    stage = _matching_current_stage(task, context, "development", "in_progress")
    if stage is None or not _is_leaf(task) or is_blocked_by_deps(task):
        return None
    if _stage_work_exhausted(stage, context):
        return EscalateAction(task_id=_task_id(task), reason="development_max_work_attempts")
    agent_slug = _development_agent(task, stage, context)
    return _spawn_stage_agent(task, stage, context, str(agent_slug))


def development_review_rule(task: object, context: object) -> Action | None:
    stage = _matching_current_stage(task, context, "development", "needs_review")
    if stage is None or not _is_leaf(task):
        return None
    if _stage_review_exhausted(stage, context):
        return EscalateAction(task_id=_task_id(task), reason="development_max_review_rounds")
    if not _has_agent(context, "qa-reviewer"):
        return EscalateAction(task_id=_task_id(task), reason="development_no_reviewer")
    return _spawn_stage_agent(task, stage, context, "qa-reviewer")


def development_advance_rule(task: object, context: object) -> Action | None:
    if not _is_leaf(task):
        return None
    return _complete_stage_on_state(task, context, "development", "review_approved")


def holistic_qa_rule(task: object, context: object) -> Action | None:
    stage = _matching_current_stage(task, context, "holistic_qa", "in_progress")
    if stage is None:
        return None
    if _stage_work_exhausted(stage, context):
        return EscalateAction(task_id=_task_id(task), reason="holistic_qa_max_work_attempts")
    if not _has_agent(context, "holistic-reviewer"):
        return EscalateAction(task_id=_task_id(task), reason="holistic_qa_no_reviewer")
    return _spawn_stage_agent(task, stage, context, "holistic-reviewer")


def holistic_qa_review_rule(task: object, context: object) -> Action | None:
    stage = _matching_current_stage(task, context, "holistic_qa", "needs_review")
    if stage is None or _stage_review_exhausted(stage, context):
        return None
    if not _has_agent(context, "holistic-reviewer"):
        return EscalateAction(task_id=_task_id(task), reason="holistic_qa_no_reviewer")
    return _spawn_stage_agent(task, stage, context, "holistic-reviewer", resume_review=True)


def holistic_qa_advance_rule(task: object, context: object) -> Action | None:
    return _complete_review_approved_stage(task, context, "holistic_qa")


def pr_work_rule(task: object, context: object) -> Action | None:
    return _spawn_required_stage_agent(
        task,
        context,
        "pr",
        "in_progress",
        agent_slug="merge-orchestrator",
        has_agent=_has_merge_agent,
        missing_agent_reason="pr_no_agent",
    )


def pr_review_rule(task: object, context: object) -> Action | None:
    if _matching_current_stage(task, context, "pr", "needs_review") is None:
        return None
    return None


def pr_advance_rule(task: object, context: object) -> Action | None:
    return _complete_review_approved_stage(task, context, "pr")


def merge_rule(task: object, context: object) -> Action | None:
    return _spawn_required_stage_agent(
        task,
        context,
        "merge",
        "in_progress",
        agent_slug="merge-orchestrator",
        has_agent=_has_merge_agent,
        missing_agent_reason="merge_no_agent",
    )


def task_has_stage(task: object, stage_name: str) -> bool:
    """True when the task manifest contains stage_name."""
    return any(_stage_name(stage) == stage_name for stage in _stages(task))


def current_stage(task: object) -> object | None:
    """Return the leftmost manifest row whose state is not done."""
    pending = [stage for stage in _stages(task) if _stage_state(stage) != "done"]
    if not pending:
        return None
    return min(pending, key=_stage_position)


def is_child_parked(child: object) -> bool:
    """True when a leaf child is no longer blocking parent holistic QA."""
    return (
        _is_leaf(child)
        and not bool(_field(child, "is_escalated", False))
        and (_is_closed(child) or current_stage(child) is None)
    )


def stage_agent_available(context: object, stage_name: str) -> bool:
    agent_slug = _default_agent(_field(context, "current_stage"), context, stage_name)
    if not agent_slug:
        return False
    return _has_agent(context, str(agent_slug))


def _has_merge_agent(context: object) -> bool:
    return _has_agent(context, "merge-orchestrator")


def _has_agent(context: object, agent_slug: str) -> bool:
    agent = _agent_definition(context, agent_slug)
    if agent is None:
        return False
    return bool(_field(agent, "enabled", True))


def is_blocked_by_deps(task: object) -> bool:
    blocked_by = _field(task, "active_blocked_by", None)
    if blocked_by is None:
        blocked_by = _field(task, "blocked_by", ())
    return bool(blocked_by)


BASE_RULES: list[Rule] = [
    auto_advance_ready_rule,
    disabled_agent_escalation_rule,
    development_isolation_rule,
    all_leaves_holistic_rule,
    ideation_rule,
    research_rule,
    architecture_rule,
    prd_rule,
    planning_work_rule,
    planning_review_rule,
    planning_advance_rule,
    test_arch_rule,
    expansion_work_rule,
    expansion_review_rule,
    expansion_advance_rule,
    development_rule,
    development_review_rule,
    development_advance_rule,
    holistic_qa_rule,
    holistic_qa_review_rule,
    holistic_qa_advance_rule,
    pr_work_rule,
    pr_review_rule,
    pr_advance_rule,
]

RULES: list[Rule] = [*BASE_RULES, merge_rule]


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
    if state == "in_progress" and _stage_work_exhausted(stage, context):
        return EscalateAction(task_id=_task_id(task), reason=f"{stage_name}_max_work_attempts")
    return _spawn_stage_agent(task, stage, context, agent_slug)


def _spawn_configured_stage_agent(
    task: object,
    context: object,
    stage_name: str,
    state: str,
) -> Action | None:
    return _spawn_on_stage(
        task, context, stage_name, state, _STAGE_AGENT_SLUGS[(stage_name, state)]
    )


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


def _matching_current_stage(
    task: object,
    context: object,
    stage_name: str,
    state: str,
) -> object | None:
    stage = _current_stage(task, context)
    if stage is None:
        return None
    if _stage_name(stage) == stage_name and _stage_state(stage) == state:
        return stage
    return None


def _current_stage(task: object, context: object) -> object | None:
    return current_stage(task) or _field(context, "current_stage")


def _previous_stage_done(task: object, stage: object) -> bool:
    position = _stage_position(stage)
    if position == 0:
        return True
    for candidate in _stages(task):
        if _stage_position(candidate) == position - 1:
            return _stage_state(candidate) == "done"
    return False


def _stage_work_exhausted(stage: object, context: object) -> bool:
    cap = _stage_cap(stage, context, "max_work_attempts", "default_max_work_attempts")
    return cap is not None and int(_field(stage, "work_attempt_count", 0) or 0) >= cap


def _stage_review_exhausted(stage: object, context: object) -> bool:
    cap = _stage_cap(stage, context, "max_review_rounds", "default_max_review_rounds")
    return cap is not None and int(_field(stage, "review_round_count", 0) or 0) >= cap


def _stage_cap(
    stage: object,
    context: object,
    stage_cap_name: str,
    registry_cap_name: str,
) -> int | None:
    value = _field(stage, stage_cap_name)
    if value is None:
        registry_entry = _registry_entry(context, _stage_name(stage), stage)
        value = _field(registry_entry, registry_cap_name)
    return int(value) if value is not None else None


def _default_agent(
    stage: object | None,
    context: object,
    stage_name: str | None = None,
) -> str | None:
    resolved_stage_name = stage_name or (_stage_name(stage) if stage is not None else None)
    registry_entry = _registry_entry(context, resolved_stage_name, stage)
    value = _field(registry_entry, "default_agent", _field(stage, "default_agent"))
    return str(value) if value else None


def _development_agent(task: object, stage: object, context: object) -> str:
    assigned_agent = _field(task, "assigned_agent")
    if assigned_agent:
        return str(assigned_agent)
    if _field(task, "category") == "docs" and _has_agent(context, "tech-writer"):
        return "tech-writer"
    return _default_agent(stage, context) or "backend-developer"


def _registry_entry(
    context: object, stage_name: str | None, stage: object | None = None
) -> object | None:
    if not stage_name:
        return stage
    registry = _field(context, "stage_registry", {})
    if isinstance(registry, dict):
        entry = registry.get(stage_name)
        return cast(object, entry) if entry is not None else stage
    if isinstance(registry, Sequence) and not isinstance(registry, str):
        for entry in registry:
            if str(_field(entry, "name", "")) == stage_name:
                return cast(object, entry)
    mapped = _mapping_field(registry, stage_name)
    return mapped if mapped is not None else stage


def _artifacts(task: object, context: object) -> object:
    return _field(context, "artifacts", _field(task, "artifacts", {}))


def _children(task: object, context: object) -> Sequence[object]:
    return tuple(_field(context, "children", _field(task, "children", ())) or ())


def _is_leaf(task: object) -> bool:
    return str(_field(task, "task_type", "")) != "epic" and not bool(_field(task, "children", ()))


def _is_epic(task: object) -> bool:
    return str(_field(task, "task_type", "")) == "epic"


def _is_closed(task: object) -> bool:
    state = _field(task, "state")
    if isinstance(state, dict) and state.get("is_closed"):
        return True
    return bool(_field(task, "is_closed", False) or _field(task, "closed_at"))


def _isolation(task: object) -> str:
    return str(_field(task, "isolation", "worktree"))


def _has_isolation_pair(artifacts: object, isolation: str) -> bool:
    if isolation == "clone":
        return bool(_field(artifacts, "clone_path")) and bool(_field(artifacts, "clone_id"))
    return bool(_field(artifacts, "worktree_path")) and bool(_field(artifacts, "worktree_id"))


def _stages(task: object) -> Sequence[object]:
    return tuple(_field(task, "stages", ()) or ())


def _stage_name(stage: object | None) -> str:
    if stage is None:
        return ""
    value = _field(stage, "stage_name", _field(stage, "name", ""))
    return str(value)


def _stage_state(stage: object) -> str:
    return str(_field(stage, "state", ""))


def _stage_position(stage: object) -> int:
    return int(_field(stage, "position", 0) or 0)


def _task_id(task: object) -> str:
    return str(_field(task, "id"))


def _task_ref(task: object) -> str:
    return str(_field(task, "ref", _task_id(task)))


def _prompt_context(context: object) -> dict[str, object]:
    return {
        "artifacts": _field(context, "artifacts"),
        "build_config": _field(context, "build_config"),
        "reason": _field(context, "reason"),
    }


def _field(obj: object, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _mapping_field(obj: object, key: str) -> object | None:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _agent_definition(context: object, agent_slug: str) -> object | None:
    agent = _mapping_field(_field(context, "agents", {}), agent_slug)
    if agent is not None:
        return agent
    return _mapping_field(_field(context, "agent_definitions", {}), agent_slug)


__all__ = [
    "BASE_RULES",
    "DISABLED_DISCOVERY_AGENT_ESCALATION_REASONS",
    "NON_MERGE_TERMINAL_MANIFEST_EXHAUSTION",
    "RULES",
    "Rule",
    "all_leaves_holistic_rule",
    "architecture_rule",
    "auto_advance_ready_rule",
    "current_stage",
    "development_advance_rule",
    "development_isolation_rule",
    "development_review_rule",
    "development_rule",
    "disabled_agent_escalation_rule",
    "evaluate",
    "expansion_advance_rule",
    "expansion_review_rule",
    "expansion_work_rule",
    "holistic_qa_advance_rule",
    "holistic_qa_review_rule",
    "holistic_qa_rule",
    "ideation_rule",
    "is_blocked_by_deps",
    "is_child_parked",
    "merge_rule",
    "planning_advance_rule",
    "planning_review_rule",
    "planning_work_rule",
    "pr_advance_rule",
    "pr_review_rule",
    "pr_work_rule",
    "prd_rule",
    "research_rule",
    "stage_agent_available",
    "task_has_stage",
    "test_arch_rule",
]
