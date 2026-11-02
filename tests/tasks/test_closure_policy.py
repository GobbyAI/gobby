"""YAML chooses task requirements, including independent review, without role literals."""

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from gobby.storage.definitions.rules import RuleDefinitionRow
from gobby.tasks.attestations import (
    TaskAttestation,
)
from gobby.tasks.closure_policy import ClosureSnapshot, evaluate_closure_policy
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.definitions import RuleDefinitionBody

pytestmark = pytest.mark.unit
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _rule(
    name: str, when: str, *, tags: list[str] | None = None, event: str = "task_close"
) -> RuleDefinitionRow:
    return RuleDefinitionRow(
        id=name,
        name=name,
        enabled=True,
        enabled_pinned=False,
        priority=10,
        definition_json={
            "event": event,
            "when": when,
            "effects": [{"type": "block", "reason": name}],
        },
        tags=tags or ["closure"],
        source="installed",
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_landing_and_closure_select_their_own_declared_requirements() -> None:
    rules = [
        _rule("close-only", "True"),
        _rule(
            "land-only",
            "latest_attestation('permission').get('result') != 'granted'",
            event="task_land",
        ),
    ]
    before = ClosureSnapshot(task={}, event="task_land")
    blocked = evaluate_closure_policy(before, agent=_agent(), rules=rules)
    assert blocked.blockers == ("land-only",)
    after = before.model_copy(
        update={"attestations": (_record("permission", "author", 1, result="granted"),)}
    )
    assert evaluate_closure_policy(after, agent=_agent(), rules=rules).allowed
    closing = evaluate_closure_policy(
        after.model_copy(update={"event": "task_close"}), agent=_agent(), rules=rules
    )
    assert closing.blockers == ("close-only",)
    assert closing.policy_revision != blocked.policy_revision


def test_landing_policy_has_the_same_pure_decision_effect_contract() -> None:
    with pytest.raises(ValidationError, match="block"):
        RuleDefinitionBody.model_validate(
            {
                "event": "task_land",
                "effects": [{"type": "set_variable", "variable": "approved", "value": True}],
            }
        )


def _agent(name: str = "arbitrary-author") -> AgentDefinitionBody:
    return AgentDefinitionBody.model_validate(
        {
            "name": name,
            "prompts": {"agent": "Finish the task."},
            "workflows": {"rule_selectors": {"include": ["tag:closure"]}},
        }
    )


def _record(kind: str, author: str, sequence: int, **updates: Any) -> TaskAttestation:
    return TaskAttestation.model_validate(
        {
            "id": f"record-{sequence}",
            "task_id": "task-1",
            "kind": kind,
            "result": "approved",
            "summary": "Checked.",
            "session_id": author,
            "agent_definition": "a-renamed-definition",
            "agent_definition_hash": "digest",
            "agent_capabilities": [],
            "sequence": sequence,
            "created_at": _NOW,
            "reference": {"scheme": "manuscript", "value": "chapter-7"},
            **updates,
        }
    )


def test_baseline_implementation_is_required_by_a_rule_only() -> None:
    snapshot = ClosureSnapshot(task={"id": "task-1", "category": "manual"})
    required = _rule(
        "implementation-proof", "latest_attestation('implementation').get('result') != 'approved'"
    )
    assert evaluate_closure_policy(snapshot, agent=_agent(), rules=[]).allowed
    refused = evaluate_closure_policy(snapshot, agent=_agent(), rules=[required])
    assert not refused.allowed
    assert refused.blockers == ("implementation-proof",)
    completed = snapshot.model_copy(
        update={"attestations": (_record("implementation", "self", 1),)}
    )
    assert evaluate_closure_policy(completed, agent=_agent(), rules=[required]).allowed


def _selected_policy(*tags: str) -> AgentDefinitionBody:
    return AgentDefinitionBody.model_validate(
        {
            "name": "arbitrary-seat",
            "prompts": {"agent": "Finish the task."},
            "workflows": {"rule_selectors": {"include": [f"tag:{tag}" for tag in tags]}},
        }
    )


@pytest.mark.parametrize("reviewer,allowed", [("maker", False), ("peer", True)])
def test_independence_is_selected_policy_not_a_recording_constraint(
    reviewer: str, allowed: bool
) -> None:
    snapshot = ClosureSnapshot(
        task={"id": "task-1"},
        attestations=(
            _record("implementation", "maker", 1),
            _record("independent-review", reviewer, 2),
        ),
    )
    independence = _rule(
        "any-review-policy-name",
        "latest_attestation('independent-review').get('session_id') == "
        "latest_attestation('implementation').get('session_id')",
    )
    assert evaluate_closure_policy(snapshot, agent=_agent(), rules=[]).allowed
    assert (
        evaluate_closure_policy(
            snapshot, agent=_agent("renamed-seat"), rules=[independence]
        ).allowed
        is allowed
    )


def test_custom_non_git_kind_and_correction_affect_the_same_snapshot() -> None:
    original = _record("cake-tasted", "baker", 1)
    correction = _record("cake-tasted", "baker", 2, result="rejected", supersedes_id=original.id)
    rule = _rule(
        "taste-policy",
        "not any(a.kind == 'cake-tasted' and a.result == 'approved' for a in attestations)",
    )
    snapshot = ClosureSnapshot(task={"id": "task-1"}, attestations=(original,))
    first = evaluate_closure_policy(snapshot, agent=_agent(), rules=[rule])
    assert first.allowed
    second = evaluate_closure_policy(
        snapshot.model_copy(update={"attestations": (original, correction)}),
        agent=_agent(),
        rules=[rule],
    )
    assert not second.allowed
    assert first.snapshot_revision != second.snapshot_revision
    assert first.policy_revision == second.policy_revision


def test_policy_selection_revision_and_expression_errors_fail_closed() -> None:
    snapshot = ClosureSnapshot(task={"id": "task-1"})
    selected = _rule("chosen", "False")
    ignored = _rule("unselected", "True", tags=["other"])
    first = evaluate_closure_policy(snapshot, agent=_agent(), rules=[selected, ignored])
    assert first.allowed
    assert first.policy_revision
    changed = _rule("chosen", "undefined_name > 0")
    refused = evaluate_closure_policy(snapshot, agent=_agent(), rules=[changed, ignored])
    assert not refused.allowed
    assert refused.policy_revision != first.policy_revision
    assert any("chosen" in reason for reason in refused.blockers)


@pytest.mark.parametrize(
    "effect",
    [
        {"type": "set_variable", "variable": "bypass", "value": True},
        {"type": "block", "reason": "Blocked", "tools": ["Edit"]},
    ],
)
def test_closure_rule_schema_rejects_mutation_and_tool_scoped_effects(
    effect: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError, match="task_close"):
        RuleDefinitionBody.model_validate({"event": "task_close", "effects": [effect]})


@pytest.mark.parametrize("scope", [{"agent_scope": ["other-seat"]}, {"audience": "autonomous"}])
def test_closure_schema_requires_applicability_in_snapshot_expressions(
    scope: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError, match="task_close"):
        RuleDefinitionBody.model_validate(
            {"event": "task_close", **scope, "effects": [{"type": "block", "reason": "Scoped"}]}
        )
