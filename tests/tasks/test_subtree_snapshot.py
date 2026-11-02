"""Recursive closure facts retain authenticated evidence and stable revisions."""

from typing import Any, cast

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.attestations import AttestationError, AttestationSubmission, record_attestation
from gobby.tasks.closure_snapshot import prepare_closure
from tests.fixtures.agent_sessions import AgentSessions

pytestmark = pytest.mark.unit


def test_assignment_scopes_evidence_to_the_bound_epic(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    from gobby.storage.agents import LocalAgentRunManager

    project_id = sample_project["id"]
    seats = AgentSessions(temp_db, project_id)
    coordinator = seats.add(
        "scoped-coordinator",
        [],
        definition_fields={
            "task_attestation_writes": ["implementation"],
            "task_attestation_scope": {"task_types": ["epic"], "assigned_only": True},
        },
    )
    tasks = LocalTaskManager(temp_db)
    own = tasks.create_task(project_id, "Assigned epic", task_type="epic")
    other = tasks.create_task(project_id, "Other epic", task_type="epic")
    submission = AttestationSubmission(
        kind="implementation", result="approved", summary="Integrated"
    )
    with pytest.raises(AttestationError, match="assignment"):
        record_attestation(temp_db, task_id=own.id, session_id=coordinator, submission=submission)
    LocalAgentRunManager(temp_db).create(
        parent_session_id=coordinator,
        child_session_id=coordinator,
        provider="codex",
        prompt="Coordinate",
        task_id=own.id,
    )
    record, _ = record_attestation(
        temp_db, task_id=own.id, session_id=coordinator, submission=submission
    )
    assert record.task_id == own.id
    with pytest.raises(AttestationError, match="assignment"):
        record_attestation(temp_db, task_id=other.id, session_id=coordinator, submission=submission)


def test_descendant_evidence_revision(temp_db: HubDatabase, sample_project: dict[str, Any]) -> None:
    project_id = sample_project["id"]
    author = AgentSessions(temp_db, project_id).add(
        "subtree-author", [], definition_fields={"task_attestation_writes": ["*"]}
    )
    tasks = LocalTaskManager(temp_db)
    root = tasks.create_task(project_id, "Root", task_type="epic")
    container = tasks.create_task(project_id, "Container", parent_task_id=root.id, task_type="epic")
    leaf = tasks.create_task(project_id, "Leaf", parent_task_id=container.id)
    before = prepare_closure(temp_db, task_id=root.id, session_id=author)
    facts = cast(dict[str, Any], before.snapshot.facts)
    assert {item["task"]["id"] for item in facts["descendants"]} == {container.id, leaf.id}
    assert facts["descendants_closed"] is False
    record, _ = record_attestation(
        temp_db,
        task_id=leaf.id,
        session_id=author,
        submission=AttestationSubmission(
            kind="arbitrary-kind", result="approved", summary="Evidence"
        ),
    )
    after = prepare_closure(temp_db, task_id=root.id, session_id=author)
    after_facts = cast(dict[str, Any], after.snapshot.facts)
    nested = next(item for item in after_facts["descendants"] if item["task"]["id"] == leaf.id)
    assert nested["attestations"][0]["session_id"] == author
    assert nested["attestations"][0]["id"] == record.id
    assert after.snapshot.facts["subtree_revision"] != facts["subtree_revision"]
    assert after.decision.snapshot_revision != before.decision.snapshot_revision
    assert prepare_closure(temp_db, task_id=root.id, session_id=author) == after
    replacement, _ = record_attestation(
        temp_db,
        task_id=leaf.id,
        session_id=author,
        submission=AttestationSubmission(
            kind="arbitrary-kind",
            result="rejected",
            summary="Corrected evidence",
            supersedes_id=record.id,
        ),
    )
    corrected = prepare_closure(temp_db, task_id=root.id, session_id=author)
    corrected_facts = cast(dict[str, Any], corrected.snapshot.facts)
    corrected_leaf = next(
        item for item in corrected_facts["descendants"] if item["task"]["id"] == leaf.id
    )
    assert [item["id"] for item in corrected_leaf["attestations"]] == [replacement.id]
    assert [item["id"] for item in corrected_leaf["all_attestations"]] == [
        record.id,
        replacement.id,
    ]
    assert corrected_facts["subtree_revision"] != after_facts["subtree_revision"]
    after = corrected
    # Root review evidence must not invalidate the descendant revision it approves.
    record_attestation(
        temp_db,
        task_id=root.id,
        session_id=author,
        submission=AttestationSubmission(
            kind="independent-review", result="approved", summary="Reviewed"
        ),
    )
    reviewed = prepare_closure(temp_db, task_id=root.id, session_id=author)
    assert reviewed.snapshot.facts["subtree_revision"] == after.snapshot.facts["subtree_revision"]
    tasks.create_task(project_id, "Repair", parent_task_id=container.id)
    assert (
        prepare_closure(temp_db, task_id=root.id, session_id=author).snapshot.facts[
            "subtree_revision"
        ]
        != reviewed.snapshot.facts["subtree_revision"]
    )
