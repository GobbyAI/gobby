"""Pre-adversary plan-enhancement dispatch rule.

The planning stage runs a constructive enhancement sub-loop *before* the
adversary gate. While an enhancement budget remains and the plan has not
converged, the ``needs_review`` planning stage is routed to the advisory
``plan-enhancer`` agent instead of the adversary. ``rules.py`` registers
``planning_enhancement_rule`` immediately before ``planning_review_rule`` so the
enhancer preempts the adversary only while enabled and incomplete; once the
budget is spent or the plan converges this rule returns ``None`` and dispatch
falls through to the unchanged adversary rule.

Kept in its own module so ``dispatch/rules.py`` only imports and registers the
rule. The module defines its own small field accessors — mirroring
``discovery_artifacts._field`` — so the dependency direction stays one-way
(``rules`` -> ``_planning_enhancement``) and there is no circular import.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from gobby.dispatch.actions import SpawnAgentAction
from gobby.dispatch.prompts import PROMPT_BUILDERS

PLAN_ENHANCER_AGENT = "plan-enhancer"
_PLANNING_STAGE = "planning"
_NEEDS_REVIEW = "needs_review"


def planning_enhancement_rule(task: object, context: object) -> SpawnAgentAction | None:
    """Spawn ``plan-enhancer`` while the planning stage has enhancement budget.

    Fires only when the current manifest stage is ``planning`` in
    ``needs_review``, the build opted into enhancement
    (``plan_enhancement_rounds > 0``), the plan has not converged, completed
    rounds are below the target, a plan artifact exists, and the enhancer agent
    is dispatchable. Otherwise returns ``None`` so dispatch falls through to
    ``planning_review_rule`` (the adversary).
    """
    stage = _matching_current_stage(task, context, _PLANNING_STAGE, _NEEDS_REVIEW)
    if stage is None:
        return None

    artifacts = _artifacts(task, context)
    target = _int_field(artifacts, "plan_enhancement_rounds")
    if target <= 0:
        return None
    if _bool_field(artifacts, "plan_enhancement_converged"):
        return None
    completed = _int_field(artifacts, "plan_enhancement_rounds_completed")
    if completed >= target:
        return None
    if not _plan_file_path(artifacts):
        return None
    if not _enhancer_dispatchable(context):
        # Enhancement is an advisory pre-pass; when the enhancer agent is
        # unavailable, degrade gracefully to the adversary rather than block
        # the plan in needs_review.
        return None

    return _spawn_plan_enhancer(
        task,
        stage,
        context,
        round_number=completed + 1,
        max_rounds=target,
    )


def _spawn_plan_enhancer(
    task: object,
    stage: object,
    context: object,
    *,
    round_number: int,
    max_rounds: int,
) -> SpawnAgentAction:
    prompt_context = _prompt_context(context)
    prompt_context["stage_name"] = _stage_name(stage)
    prompt_context["stage_state"] = _stage_state(stage)
    prompt_context["round_number"] = round_number
    prompt_context["max_enhancement_rounds"] = max_rounds
    builder = PROMPT_BUILDERS.get(PLAN_ENHANCER_AGENT) or PROMPT_BUILDERS["default"]
    initial_variables: dict[str, object] = {
        "stage_name": _stage_name(stage),
        "stage_state": _stage_state(stage),
        "round_number": round_number,
        "max_enhancement_rounds": max_rounds,
    }
    return SpawnAgentAction(
        task_id=_task_id(task),
        task_ref=_task_ref(task),
        agent_slug=PLAN_ENHANCER_AGENT,
        prompt=builder(task, prompt_context),
        initial_variables=initial_variables,
        additional_skills=tuple(_field(task, "additional_skills", ()) or ()),
    )


# --- small, self-contained accessors (mirror discovery_artifacts._field) ---


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
    pending = [stage for stage in _stages(task) if _stage_state(stage) != "done"]
    if pending:
        return min(pending, key=_stage_position)
    fallback: object | None = _field(context, "current_stage")
    return fallback


def _enhancer_dispatchable(context: object) -> bool:
    if PLAN_ENHANCER_AGENT not in PROMPT_BUILDERS:
        return False
    agent = _agent_definition(context, PLAN_ENHANCER_AGENT)
    return agent is not None and bool(_field(agent, "enabled", True))


def _agent_definition(context: object, agent_slug: str) -> object | None:
    agent = _mapping_field(_field(context, "agents", {}), agent_slug)
    if agent is not None:
        return agent
    return _mapping_field(_field(context, "agent_definitions", {}), agent_slug)


def _prompt_context(context: object) -> dict[str, object]:
    return {
        "artifacts": _field(context, "artifacts"),
        "build_config": _field(context, "build_config"),
        "failure_context": _field(context, "failure_context"),
        "reason": _field(context, "reason"),
    }


def _artifacts(task: object, context: object) -> object:
    return _field(context, "artifacts", _field(task, "artifacts", {}))


def _plan_file_path(artifacts: object) -> str:
    value = _field(artifacts, "plan_file_path")
    return str(value) if value not in (None, "") else ""


def _stages(task: object) -> Sequence[object]:
    return tuple(_field(task, "stages", ()) or ())


def _stage_name(stage: object | None) -> str:
    if stage is None:
        return ""
    return str(_field(stage, "stage_name", _field(stage, "name", "")))


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


def _int_field(obj: object, name: str) -> int:
    return int(_field(obj, name, 0) or 0)


def _bool_field(obj: object, name: str) -> bool:
    return bool(_field(obj, name, False))


def _mapping_field(obj: object, key: str) -> object | None:
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def _field(obj: object, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


__all__ = ["PLAN_ENHANCER_AGENT", "planning_enhancement_rule"]
