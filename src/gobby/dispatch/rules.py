"""Ordered pure decision rules for lifecycle dispatch."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from gobby.dispatch.actions import (
    Action,
    AdvanceLifecycleAction,
    CreateIsolationAction,
    EscalateAction,
    SpawnAgentAction,
    StartExpansionAction,
)
from gobby.dispatch.prompts import PROMPT_BUILDERS

Rule = Callable[[object, object], Action | None]

_SKIP_PREFIX = "stage-:"


def evaluate(task: object, context: object, rules: Sequence[Rule] | None = None) -> Action | None:
    """Return the first action emitted by the ordered rule list."""
    for rule in rules or BASE_RULES:
        action = rule(task, context)
        if action is not None:
            return action
    return None


def plan_review_rule(task: object, context: object) -> Action | None:
    if _state(task) != ("plan_review", "open"):
        return None

    artifacts = _artifacts(context)
    if _stage_skipped(task, "plan_review"):
        return _advance(task, "test_arch", "open", "plan_review_skipped")
    if not _field(artifacts, "plan_file_path"):
        return None
    if _plan_is_awaiting_revision(artifacts):
        return None
    if _maxed_out(task, artifacts, context, "plan_review_attempts", "max_review_rounds"):
        return _fallback(task, "plan_review", "test_arch", "open")
    return _spawn(task, context, "plan-reviewer")


def test_arch_rule(task: object, context: object) -> Action | None:
    if _state(task) != ("test_arch", "open"):
        return None
    if _stage_skipped(task, "test_arch"):
        return _advance(task, "expanding", "open", "test_arch_skipped")
    if _maxed_out(task, _artifacts(context), context, "test_arch_attempts", "max_review_rounds"):
        return _fallback(task, "test_arch", "expanding", "open")
    return _spawn(task, context, "test-architect")


def expansion_rule(task: object, context: object) -> Action | None:
    if _state(task) != ("expanding", "open"):
        return None
    if _stage_skipped(task, "expanding"):
        return _advance(task, "in_development", "open", "expansion_skipped")
    artifacts = _artifacts(context)
    if _maxed_out(task, artifacts, context, "expansion_attempts", "max_expansion_attempts"):
        return _fallback(task, "expansion", "in_development", "open")
    return StartExpansionAction(task_id=_task_id(task), task_ref=_task_ref(task))


def isolation_rule(task: object, context: object) -> Action | None:
    if _state(task) != ("in_development", "open") or not _is_leaf(task):
        return None

    isolation = _isolation(task)
    if isolation not in {"worktree", "clone"}:
        return None
    artifacts = _artifacts(context)
    if _has_isolation_pair(artifacts, isolation):
        return None
    return CreateIsolationAction(
        task_id=_task_id(task),
        task_ref=_task_ref(task),
        isolation=isolation,
        base_branch=_field(artifacts, "target_branch"),
    )


def dev_rule(task: object, context: object) -> Action | None:
    if _state(task) != ("in_development", "open") or not _is_leaf(task):
        return None
    if is_blocked_by_deps(task) or not _field(task, "assigned_agent"):
        return None
    isolation = _isolation(task)
    if isolation in {"worktree", "clone"} and not _has_isolation_pair(
        _artifacts(context), isolation
    ):
        return None
    return _spawn(task, context, str(_field(task, "assigned_agent")))


def qa_rule(task: object, context: object) -> Action | None:
    if _state(task) != ("in_development", "needs_review") or not _is_leaf(task):
        return None
    if _stage_skipped(task, "qa"):
        return _advance(task, "holistic_review", "review_approved", "qa_skipped")
    artifacts = _artifacts(context)
    if _maxed_out(task, artifacts, context, "qa_attempts", "max_qa_rounds"):
        return _fallback(task, "qa", "holistic_review", "review_approved")
    return _spawn(task, context, "qa-reviewer")


def leaf_park_rule(task: object, context: object) -> Action | None:
    if _state(task) != ("in_development", "review_approved") or not _is_leaf(task):
        return None
    return _advance(task, "holistic_review", "review_approved", "leaf_parked")


def all_leaves_holistic_rule(task: object, context: object) -> Action | None:
    if _state(task) != ("in_development", "open") or _field(task, "task_type") != "epic":
        return None
    children = list(_children(context))
    if not children or not all(_is_child_terminal_or_parked(child) for child in children):
        return None
    return _advance(task, "holistic_review", "open", "all_leaves_holistic")


def holistic_rule(task: object, context: object) -> Action | None:
    if _state(task) != ("holistic_review", "open"):
        return None
    if _stage_skipped(task, "holistic_review"):
        return _advance(task, "pr", "open", "holistic_review_skipped")
    if _field(task, "task_type") != "epic":
        return None
    children = list(_children(context))
    if not children or not all(_is_child_terminal_or_parked(child) for child in children):
        return None
    artifacts = _artifacts(context)
    if _maxed_out(task, artifacts, context, "holistic_attempts", "max_holistic_rounds"):
        return _fallback(task, "holistic_review", "pr", "open")
    return _spawn(task, context, "reviewer")


def pr_rule(task: object, context: object) -> Action | None:
    if _state(task) != ("pr", "open"):
        return None
    if _stage_skipped(task, "pr") or _is_unattended(task):
        return _advance(task, "merging", "open", "pr_unattended")
    return EscalateAction(task_id=_task_id(task), reason="pr_creation_required")


BASE_RULES: list[Rule] = [
    plan_review_rule,
    test_arch_rule,
    expansion_rule,
    isolation_rule,
    dev_rule,
    qa_rule,
    leaf_park_rule,
    all_leaves_holistic_rule,
    holistic_rule,
    pr_rule,
]


def _field(obj: object, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _state(task: object) -> tuple[str, str]:
    return str(_field(task, "lifecycle")), str(_field(task, "status"))


def _task_id(task: object) -> str:
    return str(_field(task, "id"))


def _task_ref(task: object) -> str:
    return str(_field(task, "ref") or _field(task, "task_ref") or _field(task, "id"))


def _has_label(task: object, label: str) -> bool:
    return label in set(_field(task, "labels", ()) or ())


def _stage_skipped(task: object, stage: str) -> bool:
    return _has_label(task, f"{_SKIP_PREFIX}{stage}")


def _artifacts(context: object) -> object:
    return _field(context, "artifacts")


def _children(context: object) -> Sequence[object]:
    return _field(context, "children", ()) or ()


def _is_leaf(task: object) -> bool:
    return bool(_field(task, "task_type") == "task")


def _is_unattended(task: object) -> bool:
    return bool(_field(task, "unattended", False))


def _isolation(task: object) -> str:
    return str(_field(task, "isolation", "none") or "none")


def is_blocked_by_deps(task: object) -> bool:
    blocked_by = _field(task, "active_blocked_by", None)
    if blocked_by is None:
        blocked_by = _field(task, "blocked_by", ())
    return bool(blocked_by)


def _has_isolation_pair(artifacts: object, isolation: str) -> bool:
    path = _field(artifacts, f"{isolation}_path")
    ident = _field(artifacts, f"{isolation}_id")
    if ident is None:
        ident = _field(artifacts, "base_commit_sha")
    return bool(path and ident)


def _plan_is_awaiting_revision(artifacts: object) -> bool:
    last_hash = _field(artifacts, "last_reviewed_plan_hash")
    current_hash = _field(artifacts, "plan_file_hash")
    return bool(last_hash and last_hash == current_hash)


def _maxed_out(
    task: object,
    artifacts: object,
    context: object,
    counter_name: str,
    cap_name: str,
) -> bool:
    attempts = _attempts(task, artifacts, counter_name)
    cap = _cap(context, artifacts, cap_name)
    return cap is not None and attempts >= cap


def _attempts(task: object, artifacts: object, counter_name: str) -> int:
    artifact_attempts = _field(artifacts, counter_name)
    if artifact_attempts is not None:
        return int(artifact_attempts)
    if counter_name in {"qa_attempts", "holistic_attempts"}:
        return int(_field(task, "validation_fail_count", 0) or 0)
    return int(_field(task, "dispatch_failure_count", 0) or 0)


def _cap(context: object, artifacts: object, name: str) -> int | None:
    artifact_cap = _field(artifacts, name)
    if artifact_cap is not None:
        return int(artifact_cap)
    build_config = _field(context, "build_config")
    if build_config is not None and _field(build_config, name) is not None:
        return int(_field(build_config, name))
    value = _field(context, name)
    return int(value) if value is not None else None


def _spawn(task: object, context: object, agent_slug: str) -> SpawnAgentAction:
    prompt_context = _prompt_context(context)
    builder = PROMPT_BUILDERS.get(agent_slug) or PROMPT_BUILDERS["default"]
    return SpawnAgentAction(
        task_id=_task_id(task),
        task_ref=_task_ref(task),
        agent_slug=agent_slug,
        prompt=builder(task, prompt_context),
        additional_skills=tuple(_field(task, "additional_skills", ()) or ()),
    )


def _prompt_context(context: object) -> dict[str, object]:
    if isinstance(context, dict):
        return dict(context)
    return {name: value for name, value in vars(context).items() if not name.startswith("_")}


def _advance(task: object, lifecycle: str, status: str, reason: str) -> AdvanceLifecycleAction:
    from_lifecycle, from_status = _state(task)
    return AdvanceLifecycleAction(
        task_id=_task_id(task),
        from_lifecycle=from_lifecycle,
        from_status=from_status,
        to_lifecycle=lifecycle,
        to_status=status,
        reason=reason,
    )


def _fallback(task: object, rule_name: str, lifecycle: str, status: str) -> Action:
    if _is_unattended(task):
        return _advance(task, lifecycle, status, f"{rule_name}_max_attempts_unattended")
    return EscalateAction(task_id=_task_id(task), reason=f"{rule_name}_rejected:max_attempts")


def _is_child_terminal_or_parked(child: object) -> bool:
    lifecycle, status = _state(child)
    if status in {"closed", "escalated"}:
        return True
    return (lifecycle, status) in {
        ("holistic_review", "review_approved"),
        ("merged", "closed"),
    }


__all__ = [
    "BASE_RULES",
    "Rule",
    "all_leaves_holistic_rule",
    "dev_rule",
    "evaluate",
    "expansion_rule",
    "holistic_rule",
    "is_blocked_by_deps",
    "isolation_rule",
    "leaf_park_rule",
    "plan_review_rule",
    "pr_rule",
    "qa_rule",
    "test_arch_rule",
]
