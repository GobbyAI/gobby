"""Task, stage, and context helpers for dispatch rules."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, cast

from gobby.dispatch.prompts import PROMPT_BUILDERS
from gobby.tasks.categories import AGENT_BY_IMPLEMENTATION_DOMAIN, IMPLEMENTATION_DOMAINS

logger = logging.getLogger("gobby.dispatch.rules")


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
    return _agent_dispatchable(context, str(agent_slug))


def _has_merge_agent(context: object) -> bool:
    return _agent_dispatchable(context, "merge-orchestrator")


def _agent_dispatchable(context: object, agent_slug: str) -> bool:
    return _has_agent(context, agent_slug) and agent_slug in PROMPT_BUILDERS


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
    # Intentionally strict > (NOT >=), and this is the same cap-attempt bound the
    # storage layer enforces with >= — the operators differ only because the two
    # checks run at different lifecycle points (gobby-#17668):
    #   * start_stage increments work_attempt_count when a stage enters
    #     in_progress, BEFORE this rule dispatches that attempt's agent. So at
    #     count == cap the cap-th attempt is still in flight and must run;
    #     the dispatcher only stops once count > cap.
    #   * StageStateTransitions.transition escalates with
    #     work_attempt_count >= effective_cap AFTER an attempt's fail_stage,
    #     where count reflects completed attempts.
    # Both therefore allow exactly `cap` attempts. Switching this to >= would
    # escalate before the final attempt ran (cap-1 effective attempts) and break
    # the tested "fires/allows at cap" dispatch contract.
    return cap is not None and int(_field(stage, "work_attempt_count", 0) or 0) > cap


def _stage_revision_review_budget_open(stage: object, context: object) -> bool:
    if _field(stage, "review_policy") != "required":
        return False
    review_rounds = int(_field(stage, "review_round_count", 0) or 0)
    work_attempts = int(_field(stage, "work_attempt_count", 0) or 0)
    if review_rounds <= 0 or work_attempts <= review_rounds:
        return False
    return not _stage_review_exhausted(stage, context)


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
        assigned_slug = str(assigned_agent)
        if _agent_dispatchable(context, assigned_slug):
            return assigned_slug
        logger.warning(
            "Ignoring unavailable assigned development agent; falling back",
            extra={
                "task_id": _task_id(task),
                "task_ref": _task_ref(task),
                "assigned_agent": assigned_slug,
            },
        )
    if _field(task, "category") == "code":
        implementation_domain = _field(task, "implementation_domain")
        if implementation_domain is not None and implementation_domain in IMPLEMENTATION_DOMAINS:
            return AGENT_BY_IMPLEMENTATION_DOMAIN[str(implementation_domain)]
    if _field(task, "category") == "docs" and _agent_dispatchable(context, "tech-writer"):
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


def _holistic_descendant_gate(context: object) -> object | None:
    return cast(object | None, _field(context, "holistic_descendant_gate", None))


def _holistic_descendant_gate_body(gate: object) -> str:
    blockers = tuple(_field(gate, "blockers", ()) or ())
    lines = ["Holistic QA is waiting for nonterminal descendants:"]
    for blocker in blockers:
        ref = _field(blocker, "task_ref", _field(blocker, "task_id", "unknown"))
        path = _field(blocker, "task_path", "no-path") or "no-path"
        title = _field(blocker, "title", "")
        stage_name = _field(blocker, "stage_name", "none") or "none"
        stage_state = _field(blocker, "stage_state", "none") or "none"
        escalated = str(bool(_field(blocker, "is_escalated", False))).lower()
        lines.append(
            f"- {ref} ({path}): {title} [stage={stage_name}:{stage_state}, escalated={escalated}]"
        )
    return "\n".join(lines)


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


def _task_has_label(task: object, label: str) -> bool:
    return label in set(_field(task, "labels", ()) or ())


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
    value = _field(task, "ref") or _field(task, "task_ref")
    if value:
        return str(value)
    seq_num = _field(task, "seq_num")
    if seq_num not in (None, ""):
        return f"#{seq_num}"
    return _task_id(task)


def _prompt_context(context: object) -> dict[str, object]:
    return {
        "artifacts": _field(context, "artifacts"),
        "build_config": _field(context, "build_config"),
        "failure_context": _field(context, "failure_context"),
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
    "_agent_dispatchable",
    "_artifacts",
    "_children",
    "_current_stage",
    "_default_agent",
    "_development_agent",
    "_field",
    "_has_agent",
    "_has_isolation_pair",
    "_has_merge_agent",
    "_holistic_descendant_gate",
    "_holistic_descendant_gate_body",
    "_is_closed",
    "_is_epic",
    "_is_leaf",
    "_isolation",
    "_matching_current_stage",
    "_previous_stage_done",
    "_prompt_context",
    "_registry_entry",
    "_stage_name",
    "_stage_position",
    "_stage_revision_review_budget_open",
    "_stage_review_exhausted",
    "_stage_state",
    "_stage_work_exhausted",
    "_stages",
    "_task_has_label",
    "_task_id",
    "_task_ref",
    "current_stage",
    "is_blocked_by_deps",
    "is_child_parked",
    "stage_agent_available",
    "task_has_stage",
]
