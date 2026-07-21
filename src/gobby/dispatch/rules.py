"""Ordered pure decision rules for stage-native dispatch."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from gobby.dispatch._planning_enhancement import planning_enhancement_rule
from gobby.dispatch._rule_actions import (
    _complete_review_approved_stage,
    _complete_stage_on_state,
    _spawn_configured_stage_agent,
    _spawn_required_stage_agent,
    _spawn_stage_agent,
    _start_configured_stage_pipeline,
)
from gobby.dispatch._rule_actions import (
    _dispatch_inputs as _rule_dispatch_inputs,
)
from gobby.dispatch._rule_merge import _has_workspace_merge_source, _workspace_merge_action
from gobby.dispatch._rule_state import (
    _agent_dispatchable,
    _children,
    _current_stage,
    _default_agent,
    _development_agent,
    _epic_descendant_gate,
    _epic_descendant_gate_body,
    _field,
    _has_merge_agent,
    _is_closed,
    _is_epic,
    _is_leaf,
    _isolation,
    _matching_current_stage,
    _previous_stage_done,
    _registry_entry,
    _stage_name,
    _stage_review_exhausted,
    _stage_state,
    _stage_work_exhausted,
    _task_id,
    current_stage,
    is_blocked_by_deps,
    is_child_parked,
    stage_agent_available,
    task_has_stage,
)
from gobby.dispatch.actions import (
    Action,
    AdvanceStageAction,
    AppendAuditMarkerAction,
    EscalateAction,
    StartStageAction,
)
from gobby.dispatch.audit import has_audit_marker
from gobby.dispatch.discovery_artifacts import discovery_artifact_ready
from gobby.dispatch.prompts import PROMPT_BUILDERS as PROMPT_BUILDERS

Rule = Callable[[object, object], Action | None]

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
_AUTO_ADVANCE_DEDICATED_STAGES = {"development", "epic_qa"}
_EPIC_DESCENDANT_GATE_HEADING = "Epic QA deferred"
_DISABLED_AGENT_EXCLUDED_STAGES = {
    "expansion",
    "pr",
    "development",
    "epic_qa",
}

_dispatch_inputs = _rule_dispatch_inputs


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
    if stage_name == "merge" and _has_workspace_merge_source(task, context):
        return StartStageAction(task_id=_task_id(task), stage_name=stage_name)
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
    if is_blocked_by_deps(task):
        return None

    isolation = _isolation(task)
    if isolation == "none":
        return StartStageAction(task_id=_task_id(task), stage_name=_stage_name(stage))
    if isolation not in {"worktree", "clone"}:
        return EscalateAction(
            task_id=_task_id(task), reason=f"development_isolation_invalid:{isolation}"
        )

    return StartStageAction(task_id=_task_id(task), stage_name=_stage_name(stage))


def epic_descendant_gate_rule(task: object, context: object) -> Action | None:
    gate = _epic_descendant_gate(context)
    if gate is None:
        return None
    stage = _current_stage(task, context)
    if _stage_name(stage) != "epic_qa" or _stage_state(stage) not in {"ready", "in_progress"}:
        return None
    description = _field(task, "description", "") or ""
    if has_audit_marker(description, _EPIC_DESCENDANT_GATE_HEADING):
        return None
    body = _epic_descendant_gate_body(gate)
    return AppendAuditMarkerAction(
        task_id=_task_id(task),
        heading=_EPIC_DESCENDANT_GATE_HEADING,
        body=body,
    )


def all_leaves_epic_rule(task: object, context: object) -> Action | None:
    stage = _matching_current_stage(task, context, "epic_qa", "ready")
    if stage is None or not _is_epic(task):
        return None
    if _epic_descendant_gate(context) is not None:
        return None
    children = list(_children(task, context))
    if not children:
        return None
    if not all(is_child_parked(child) or _is_closed(child) for child in children):
        return None
    return StartStageAction(task_id=_task_id(task), stage_name="epic_qa")


def epic_development_start_rule(task: object, context: object) -> Action | None:
    stage = _matching_current_stage(task, context, "development", "ready")
    if stage is None or not _is_epic(task):
        return None
    if not _children(task, context):
        return None
    return StartStageAction(task_id=_task_id(task), stage_name="development")


def epic_development_complete_rule(task: object, context: object) -> Action | None:
    stage = _matching_current_stage(task, context, "development", "in_progress")
    if stage is None or not _is_epic(task):
        return None
    children = list(_children(task, context))
    if not children:
        return None
    if not all(is_child_parked(child) or _is_closed(child) for child in children):
        return None
    return AdvanceStageAction(
        task_id=_task_id(task),
        stage_name="development",
        method="complete_stage",
        validation_override_reason="children_parked",
    )


def discovery_artifact_complete_rule(task: object, context: object) -> Action | None:
    stage = _current_stage(task, context)
    if stage is None or _stage_state(stage) != "in_progress":
        return None
    stage_name = _stage_name(stage)
    if not discovery_artifact_ready(task, stage_name):
        return None
    return AdvanceStageAction(
        task_id=_task_id(task),
        stage_name=stage_name,
        method="complete_stage",
        by_session_id="dispatcher",
    )


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
    if not _agent_dispatchable(context, "expansion-qa"):
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
    if not _agent_dispatchable(context, agent_slug):
        return EscalateAction(task_id=_task_id(task), reason="development_no_agent")
    return _spawn_stage_agent(task, stage, context, str(agent_slug))


def development_review_rule(task: object, context: object) -> Action | None:
    stage = _matching_current_stage(task, context, "development", "needs_review")
    if stage is None or not _is_leaf(task):
        return None
    if _stage_review_exhausted(stage, context):
        return EscalateAction(task_id=_task_id(task), reason="development_max_review_rounds")
    reviewer_agent = _field(stage, "reviewer_agent")
    if not reviewer_agent or not _agent_dispatchable(context, str(reviewer_agent)):
        return EscalateAction(task_id=_task_id(task), reason="development_no_reviewer")
    return _spawn_stage_agent(task, stage, context, str(reviewer_agent))


def development_advance_rule(task: object, context: object) -> Action | None:
    if not _is_leaf(task):
        return None
    return _complete_stage_on_state(task, context, "development", "review_approved")


def epic_qa_rule(task: object, context: object) -> Action | None:
    stage = _matching_current_stage(task, context, "epic_qa", "in_progress")
    if stage is None:
        return None
    if _epic_descendant_gate(context) is not None:
        return None
    if _stage_work_exhausted(stage, context):
        return EscalateAction(task_id=_task_id(task), reason="epic_qa_max_work_attempts")
    if not _agent_dispatchable(context, "epic-reviewer"):
        return EscalateAction(task_id=_task_id(task), reason="epic_qa_no_reviewer")
    return _spawn_stage_agent(task, stage, context, "epic-reviewer")


def epic_qa_review_rule(task: object, context: object) -> Action | None:
    stage = _matching_current_stage(task, context, "epic_qa", "needs_review")
    if stage is None:
        return None
    if _stage_review_exhausted(stage, context):
        return EscalateAction(task_id=_task_id(task), reason="epic_qa_max_review_rounds")
    if not _agent_dispatchable(context, "epic-reviewer"):
        return EscalateAction(task_id=_task_id(task), reason="epic_qa_no_reviewer")
    return _spawn_stage_agent(task, stage, context, "epic-reviewer", resume_review=True)


def epic_qa_advance_rule(task: object, context: object) -> Action | None:
    return _complete_review_approved_stage(task, context, "epic_qa")


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
    stage = _matching_current_stage(task, context, "pr", "needs_review")
    if stage is None:
        return None
    if _stage_review_exhausted(stage, context):
        return EscalateAction(task_id=_task_id(task), reason="pr_max_review_rounds")
    reviewer_agent = _field(stage, "reviewer_agent")
    if not reviewer_agent or not _agent_dispatchable(context, str(reviewer_agent)):
        return EscalateAction(task_id=_task_id(task), reason="pr_no_reviewer")
    return _spawn_stage_agent(task, stage, context, str(reviewer_agent))


def pr_advance_rule(task: object, context: object) -> Action | None:
    return _complete_review_approved_stage(task, context, "pr")


def merge_rule(task: object, context: object) -> Action | None:
    workspace_action = _workspace_merge_action(task, context)
    if workspace_action is not None:
        return workspace_action
    return _spawn_required_stage_agent(
        task,
        context,
        "merge",
        "in_progress",
        agent_slug="merge-orchestrator",
        has_agent=_has_merge_agent,
        missing_agent_reason="merge_no_agent",
    )


BASE_RULES: list[Rule] = [
    auto_advance_ready_rule,
    disabled_agent_escalation_rule,
    development_isolation_rule,
    epic_descendant_gate_rule,
    all_leaves_epic_rule,
    epic_development_start_rule,
    epic_development_complete_rule,
    discovery_artifact_complete_rule,
    ideation_rule,
    research_rule,
    architecture_rule,
    prd_rule,
    planning_work_rule,
    planning_enhancement_rule,
    planning_review_rule,
    planning_advance_rule,
    expansion_work_rule,
    expansion_review_rule,
    expansion_advance_rule,
    development_rule,
    development_review_rule,
    development_advance_rule,
    epic_qa_rule,
    epic_qa_review_rule,
    epic_qa_advance_rule,
    pr_work_rule,
    pr_review_rule,
    pr_advance_rule,
]

RULES: list[Rule] = [*BASE_RULES, merge_rule]


__all__ = [
    "BASE_RULES",
    "DISABLED_DISCOVERY_AGENT_ESCALATION_REASONS",
    "NON_MERGE_TERMINAL_MANIFEST_EXHAUSTION",
    "RULES",
    "Rule",
    "all_leaves_epic_rule",
    "architecture_rule",
    "auto_advance_ready_rule",
    "current_stage",
    "development_advance_rule",
    "development_isolation_rule",
    "development_review_rule",
    "development_rule",
    "disabled_agent_escalation_rule",
    "epic_development_complete_rule",
    "epic_development_start_rule",
    "evaluate",
    "expansion_advance_rule",
    "expansion_review_rule",
    "expansion_work_rule",
    "epic_descendant_gate_rule",
    "epic_qa_advance_rule",
    "epic_qa_review_rule",
    "epic_qa_rule",
    "ideation_rule",
    "is_blocked_by_deps",
    "is_child_parked",
    "merge_rule",
    "planning_advance_rule",
    "planning_enhancement_rule",
    "planning_review_rule",
    "planning_work_rule",
    "pr_advance_rule",
    "pr_review_rule",
    "pr_work_rule",
    "prd_rule",
    "research_rule",
    "stage_agent_available",
    "task_has_stage",
]
