"""YAML chooses task requirements, including independent review, without role literals."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pytest
import yaml
from pydantic import ValidationError

from gobby.storage.definitions.rules import RuleDefinitionManager, RuleDefinitionRow
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.attestations import (
    AttestationSubmission,
    EvidenceReference,
    TaskAttestation,
    record_attestation,
)
from gobby.tasks.closure_policy import ClosureSnapshot, evaluate_closure_policy
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.sync_rules import sync_rule_file
from tests.fixtures.agent_sessions import AgentSessions

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


@pytest.mark.parametrize("event", ["task_close", "task_land"])
def test_bundled_security_policy_requires_independent_authority_for_the_exact_candidate(
    temp_db: HubDatabase, event: Literal["task_close", "task_land"]
) -> None:
    sha = "a" * 40
    snapshot = ClosureSnapshot(
        event=event,
        task={"labels": ["security-review"], "claimed_by_session_id": "maker"},
        caller={"session_id": "lander"},
        facts={"candidate_commit_sha": sha, "claimants": ["former-maker"]},
        attestations=(_record("implementation", "maker", 1),),
    )
    agent = _selected_policy("task-security")
    rules = _bundled_closure_rules(temp_db)
    assert not evaluate_closure_policy(snapshot, agent=agent, rules=rules).allowed
    approval = _record(
        "security-review",
        "security-author",
        2,
        agent_capabilities=["approve_security_review"],
        reference={"scheme": "git", "value": sha},
    )
    approved = snapshot.model_copy(update={"attestations": (*snapshot.attestations, approval)})
    assert evaluate_closure_policy(approved, agent=agent, rules=rules).allowed
    rejected = approval.model_copy(update={"id": "record-3", "sequence": 3, "result": "rejected"})
    revoked = approved.model_copy(update={"attestations": (*approved.attestations, rejected)})
    assert not evaluate_closure_policy(revoked, agent=agent, rules=rules).allowed
    for invalid in (
        approval.model_copy(update={"session_id": "maker"}),
        approval.model_copy(update={"session_id": "lander"}),
        approval.model_copy(update={"session_id": "former-maker"}),
        approval.model_copy(update={"agent_capabilities": ()}),
        approval.model_copy(update={"result": "rejected"}),
        approval.model_copy(update={"reference": EvidenceReference(scheme="git", value="b" * 40)}),
    ):
        denied = snapshot.model_copy(update={"attestations": (*snapshot.attestations, invalid)})
        assert not evaluate_closure_policy(denied, agent=agent, rules=rules).allowed


def test_bundled_landing_policy_uses_path_grants_or_independent_review(
    temp_db: HubDatabase,
) -> None:
    sha = "a" * 40
    snapshot = ClosureSnapshot(
        event="task_land",
        task={},
        caller={"session_id": "maker"},
        facts={"candidate_commit_sha": sha, "integration_path_grant": False},
        attestations=(_record("implementation", "maker", 1),),
    )
    agent = _selected_policy("task-closure-general")
    rules = _bundled_closure_rules(temp_db)
    assert not evaluate_closure_policy(snapshot, agent=agent, rules=rules).allowed
    granted = snapshot.model_copy(
        update={"facts": {**snapshot.facts, "integration_path_grant": True}}
    )
    assert evaluate_closure_policy(granted, agent=agent, rules=rules).allowed
    review = _record("review", "reviewer", 2, reference={"scheme": "git", "value": sha})
    reviewed = snapshot.model_copy(update={"attestations": (*snapshot.attestations, review)})
    assert evaluate_closure_policy(reviewed, agent=agent, rules=rules).allowed
    rejected = review.model_copy(update={"id": "record-3", "sequence": 3, "result": "rejected"})
    revoked = reviewed.model_copy(update={"attestations": (*reviewed.attestations, rejected)})
    assert not evaluate_closure_policy(revoked, agent=agent, rules=rules).allowed
    self_review = reviewed.model_copy(
        update={
            "attestations": (
                *snapshot.attestations,
                review.model_copy(update={"session_id": "maker"}),
            )
        }
    )
    assert not evaluate_closure_policy(self_review, agent=agent, rules=rules).allowed


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


def _bundled_closure_rules(db: HubDatabase) -> list[RuleDefinitionRow]:
    directory = Path(__file__).parents[2] / "src/gobby/install/shared/workflows/rules/task-closure"
    for path in sorted(directory.glob("*.yaml")):
        result = sync_rule_file(db, path)
        assert result["success"], result["errors"]
    role_rules = directory.parent / "roles/closure-authority.yaml"
    result = sync_rule_file(db, role_rules)
    assert result["success"], result["errors"]
    return RuleDefinitionManager(db).list_effective(None)


def _selected_policy(*tags: str) -> AgentDefinitionBody:
    return AgentDefinitionBody.model_validate(
        {
            "name": "arbitrary-seat",
            "prompts": {"agent": "Finish the task."},
            "workflows": {"rule_selectors": {"include": [f"tag:{tag}" for tag in tags]}},
        }
    )


def _bundled_agent(name: str) -> AgentDefinitionBody:
    path = Path(__file__).parents[2] / f"src/gobby/install/shared/workflows/agents/{name}.yaml"
    return AgentDefinitionBody.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _bundled_default() -> AgentDefinitionBody:
    return _bundled_agent("default")


def test_bundled_general_policy_accepts_non_git_implementation(temp_db: HubDatabase) -> None:
    rules = _bundled_closure_rules(temp_db)
    agent = _bundled_default()
    snapshot = ClosureSnapshot(task={"id": "task-1", "category": "manual"})
    assert not evaluate_closure_policy(snapshot, agent=agent, rules=rules).allowed
    completed = snapshot.model_copy(
        update={"attestations": (_record("implementation", "maker", 1),)}
    )
    assert evaluate_closure_policy(completed, agent=agent, rules=rules).allowed


@pytest.mark.parametrize("category", ["code", "manual"])
@pytest.mark.parametrize("reason", ["moot", "wont_do", "operator-cancelled"])
@pytest.mark.parametrize("clean", [True, False])
def test_bundled_disposition_preserves_attributed_work_safety(
    temp_db: HubDatabase, category: str, reason: str, clean: bool
) -> None:
    snapshot = ClosureSnapshot(
        task={"category": category},
        reason=reason,
        attestations=(_record("disposition", "maker", 1, evidence={"reason": reason}),),
        facts={"task_files_clean": clean},
    )
    decision = evaluate_closure_policy(
        snapshot, agent=_bundled_default(), rules=_bundled_closure_rules(temp_db)
    )
    assert decision.allowed is clean


@pytest.mark.parametrize(
    "reservation",
    [
        None,
        "claim",
        "escalated",
        "needs_human",
        "deferred",
        "needs-decision",
        "clean-window",
        "needs-planning",
        "epic-devteam",
    ],
)
def test_bundled_default_preserves_reserved_ancestors_when_last_child_closes(
    temp_db: HubDatabase, sample_project: dict[str, Any], reservation: str | None
) -> None:
    _bundled_closure_rules(temp_db)
    author = AgentSessions(temp_db, sample_project["id"]).add(
        "default", definition_fields=_bundled_default().model_dump(mode="json")
    )
    manager = LocalTaskManager(temp_db)
    parent = manager.create_task(sample_project["id"], "Reserved parent", category="manual")
    child = manager.create_task(
        sample_project["id"], "Complete child", category="manual", parent_task_id=parent.id
    )
    for task in (parent, child):
        record_attestation(
            temp_db,
            task_id=task.id,
            session_id=author,
            submission=AttestationSubmission(
                kind="implementation", result="approved", summary="Deliverable implemented."
            ),
        )
    hold = reservation if reservation in {"needs_human", "deferred"} else None
    escalated = reservation == "escalated"
    owner = author if reservation == "claim" else None
    labels = (
        [reservation]
        if reservation in {"needs-decision", "clean-window", "needs-planning", "epic-devteam"}
        else []
    )
    temp_db.execute(
        "UPDATE tasks SET hold_state = %s, follow_up_at = CASE WHEN %s = 'deferred' "
        "THEN CURRENT_TIMESTAMP + INTERVAL '1 day' ELSE NULL END, "
        "is_escalated = %s, escalated_at = CASE WHEN %s THEN CURRENT_TIMESTAMP ELSE NULL END, "
        "claimed_by_session_id = %s, labels = %s WHERE id = %s",
        (hold, hold, escalated, escalated, owner, json.dumps(labels), parent.id),
    )
    manager.close_task(child.id, closed_in_session_id=author)
    assert manager.get_task(child.id).closed_at is not None
    remaining = manager.get_task(parent.id)
    assert (remaining.closed_at is None) is (reservation is not None)
    assert remaining.claimed_by_session_id == owner
    assert remaining.is_escalated is escalated


def test_bundled_coding_policy_requires_tdd_validation_review_and_commit(
    temp_db: HubDatabase,
) -> None:
    rules = _bundled_closure_rules(temp_db)
    agent = _bundled_default()
    sha = "a" * 40
    kinds = ("test-before", "implementation", "test-after", "validation", "self-review", "closing")
    records = tuple(
        _record(kind, "maker", index, reference={"scheme": "git", "value": sha})
        for index, kind in enumerate(kinds, 1)
    )
    snapshot = ClosureSnapshot(
        task={"id": "task-1", "category": "code", "commits": [sha[:12]]},
        attestations=records,
        facts={
            "task_files_clean": True,
            "integration_ancestry": True,
            "attribution_complete": True,
            "candidate_commit_sha": sha,
            "candidate_covers_linked_commits": True,
        },
    )
    assert evaluate_closure_policy(snapshot, agent=agent, rules=rules).allowed
    for missing in kinds:
        incomplete = snapshot.model_copy(
            update={"attestations": tuple(item for item in records if item.kind != missing)}
        )
        assert not evaluate_closure_policy(incomplete, agent=agent, rules=rules).allowed, missing
    for facts in (
        {"task_files_clean": False},
        {"integration_ancestry": False},
        {"attribution_complete": False},
        {"candidate_covers_linked_commits": False},
    ):
        incomplete = snapshot.model_copy(update={"facts": {**snapshot.facts, **facts}})
        assert not evaluate_closure_policy(incomplete, agent=agent, rules=rules).allowed
    unlinked = snapshot.model_copy(update={"task": {**snapshot.task, "commits": []}})
    assert not evaluate_closure_policy(unlinked, agent=agent, rules=rules).allowed
    different_commit = snapshot.model_copy(
        update={
            "task": {**snapshot.task, "commits": [sha[:12], "b" * 12]},
            "facts": {**snapshot.facts, "candidate_commit_sha": "b" * 40},
        }
    )
    assert not evaluate_closure_policy(different_commit, agent=agent, rules=rules).allowed
    late_test = records[0].model_copy(update={"sequence": 10})
    wrong_order = snapshot.model_copy(update={"attestations": (*records[1:], late_test)})
    assert not evaluate_closure_policy(wrong_order, agent=agent, rules=rules).allowed
    for kind in ("implementation", "test-after", "validation", "self-review", "closing"):
        stale = tuple(
            item.model_copy(update={"reference": EvidenceReference(scheme="git", value="b" * 40)})
            if item.kind == kind
            else item
            for item in records
        )
        assert not evaluate_closure_policy(
            snapshot.model_copy(update={"attestations": stale}), agent=agent, rules=rules
        ).allowed, kind


def test_bundled_coding_landing_requires_tdd_validation_and_ocr(
    temp_db: HubDatabase,
) -> None:
    rules = _bundled_closure_rules(temp_db)
    agent = _selected_policy("task-closure-coding")
    kinds = ("test-before", "implementation", "test-after", "validation", "self-review")
    sha = "a" * 40
    records = tuple(
        _record(kind, "maker", index, reference={"scheme": "git", "value": sha})
        for index, kind in enumerate(kinds, 1)
    )
    snapshot = ClosureSnapshot(
        event="task_land",
        task={"category": "code"},
        attestations=records,
        facts={"candidate_commit_sha": sha},
    )
    assert evaluate_closure_policy(snapshot, agent=agent, rules=rules).allowed
    for missing in kinds:
        incomplete = snapshot.model_copy(
            update={"attestations": tuple(item for item in records if item.kind != missing)}
        )
        assert not evaluate_closure_policy(incomplete, agent=agent, rules=rules).allowed, missing
    late_test = records[0].model_copy(update={"sequence": 10})
    wrong_order = snapshot.model_copy(update={"attestations": (*records[1:], late_test)})
    assert not evaluate_closure_policy(wrong_order, agent=agent, rules=rules).allowed
    for kind in ("implementation", "test-after", "validation", "self-review"):
        stale = tuple(
            item.model_copy(update={"reference": EvidenceReference(scheme="git", value="b" * 40)})
            if item.kind == kind
            else item
            for item in records
        )
        assert not evaluate_closure_policy(
            snapshot.model_copy(update={"attestations": stale}), agent=agent, rules=rules
        ).allowed, kind


@pytest.mark.parametrize(
    ("definition", "reason", "allowed"),
    [
        ("consolidator", "moot", True),
        ("consolidator", "superseded", True),
        ("consolidator", "done", False),
        ("researcher", "done", True),
        ("researcher", "moot", False),
    ],
)
def test_role_close_reasons_are_selected_yaml_policy(
    temp_db: HubDatabase, definition: str, reason: str, allowed: bool
) -> None:
    rules = _bundled_closure_rules(temp_db)
    records = (
        _record("implementation", "author", 1),
        _record("review", "peer", 2, agent_capabilities=("approve_report_review",)),
        _record("disposition", "author", 3, evidence={"reason": reason}),
    )
    snapshot = ClosureSnapshot(
        task={"id": "report", "category": "research"}, reason=reason, attestations=records
    )
    decision = evaluate_closure_policy(snapshot, agent=_bundled_agent(definition), rules=rules)
    assert decision.allowed is allowed, decision


def test_bundled_disposition_policy_requires_matching_evidence_and_replacement(
    temp_db: HubDatabase,
) -> None:
    rules = _bundled_closure_rules(temp_db)
    agent = _bundled_default()
    snapshot = ClosureSnapshot(task={"id": "task-1", "category": "code"}, reason="superseded")
    assert not evaluate_closure_policy(snapshot, agent=agent, rules=rules).allowed
    disposition = _record("disposition", "maker", 1, evidence={"reason": "moot"})
    mismatched = snapshot.model_copy(update={"attestations": (disposition,)})
    assert not evaluate_closure_policy(mismatched, agent=agent, rules=rules).allowed
    disposition = disposition.model_copy(update={"evidence": {"reason": "superseded"}})
    completed = snapshot.model_copy(update={"attestations": (disposition,)})
    assert evaluate_closure_policy(completed, agent=agent, rules=rules).allowed
    no_replacement = snapshot.model_copy(
        update={"attestations": (disposition.model_copy(update={"reference": None}),)}
    )
    assert not evaluate_closure_policy(no_replacement, agent=agent, rules=rules).allowed


@pytest.mark.parametrize(
    "reviewer,allowed", [("maker", False), ("earlier-maker", False), ("peer", True)]
)
@pytest.mark.parametrize("definition", ["developer", "code-reviewer"])
def test_bundled_pair_policy_adds_independence_without_restricting_default(
    temp_db: HubDatabase, reviewer: str, allowed: bool, definition: str
) -> None:
    rules = _bundled_closure_rules(temp_db)
    snapshot = ClosureSnapshot(
        task={"id": "task-1", "category": "manual"},
        attestations=(
            _record("implementation", "earlier-maker", 1),
            _record("implementation", "maker", 2),
            _record("review", reviewer, 3),
        ),
    )
    assert evaluate_closure_policy(snapshot, agent=_bundled_default(), rules=rules).allowed
    assert (
        evaluate_closure_policy(
            snapshot,
            agent=_bundled_agent(definition),
            rules=rules,
        ).allowed
        is allowed
    )


@pytest.mark.parametrize("reviewer,allowed", [("maker", False), ("peer", True)])
def test_independence_is_selected_policy_not_a_recording_constraint(
    reviewer: str, allowed: bool
) -> None:
    snapshot = ClosureSnapshot(
        task={"id": "task-1"},
        attestations=(
            _record("implementation", "maker", 1),
            _record("review", reviewer, 2),
        ),
    )
    independence = _rule(
        "any-review-policy-name",
        "latest_attestation('review').get('session_id') == "
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
