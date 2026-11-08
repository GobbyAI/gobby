"""A task's implementer can never record a review kind of its own work.

The rule declares the authorship split in YAML. Record time refuses the split for any
author whose definition selects the rule; closure and landing read the same split from
the snapshot. The default definition is exempt by not selecting the rule.
"""

from typing import Any, Literal

import pytest
from pydantic import ValidationError

from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.attestations import (
    AttestationError,
    AttestationSubmission,
    TaskAttestation,
    list_attestations,
    record_attestation,
)
from gobby.tasks.closure_policy import ClosureSnapshot, evaluate_closure_policy
from gobby.workflows.agent_models import AgentDefinitionBody
from gobby.workflows.definitions import RuleDefinitionBody
from tests.fixtures.agent_sessions import AgentSessions

pytestmark = pytest.mark.unit

_TAG = "synthetic-authorship"
_SELECTED = "synthetic-selected"
_EXEMPT = "synthetic-exempt"
_RULE = "implementer-may-not-review"
_MAKER = ["draft", "build", "polish"]
_CLOSE_WHEN = (
    "reason == 'done' and any("
    "latest_attestation(a.get('kind')).get('session_id') in "
    "[i.get('session_id') for i in all_attestations if i.get('kind') == 'build'] "
    "for a in attestations if a.get('kind') not in ['draft', 'build', 'polish'])"
)


def _rules(db: HubDatabase, project_id: str) -> None:
    rules = RuleDefinitionManager(db)
    rules.create(
        _RULE,
        {
            "event": "task_close",
            "when": _CLOSE_WHEN,
            "attestation_authorship": {"implementation_kind": "build", "maker_kinds": _MAKER},
            "effects": [{"type": "block", "reason": "Route the review to an independent seat."}],
        },
        project_id=project_id,
        tags=[_TAG],
    )
    rules.create(
        f"{_RULE}-land",
        {
            "event": "task_land",
            "when": _CLOSE_WHEN,
            "effects": [{"type": "block", "reason": "Route the review to an independent seat."}],
        },
        project_id=project_id,
        tags=[_TAG],
    )


def _author(seats: AgentSessions, definition: str) -> str:
    include = [f"tag:{_TAG}"] if definition == _SELECTED else []
    return seats.add(
        definition,
        ["draft", "build", "polish", "sign-off"],
        definition_fields={"workflows": {"rule_selectors": {"include": include}}},
    )


def _submit(
    db: HubDatabase,
    author: str,
    task_id: str,
    kind: str,
    *,
    supersedes_id: str | None = None,
) -> TaskAttestation:
    record, _created = record_attestation(
        db,
        task_id=task_id,
        session_id=author,
        submission=AttestationSubmission(
            kind=kind,
            result="approved",
            summary="Observed.",
            supersedes_id=supersedes_id,
        ),
    )
    return record


def test_implementer_is_refused_a_review_kind_of_its_own_work(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    _rules(temp_db, project_id)
    seats = AgentSessions(temp_db, project_id)
    implementer = _author(seats, _SELECTED)
    task = LocalTaskManager(temp_db).create_task(project_id, "Bake a cake")
    _submit(temp_db, implementer, task.id, "build")

    with pytest.raises(AttestationError) as refused:
        _submit(temp_db, implementer, task.id, "sign-off")

    message = str(refused.value)
    assert _RULE in message
    assert "independent seat" in message
    assert [record.kind for record in list_attestations(temp_db, task.id)] == ["build"]


def test_superseded_implementation_still_makes_its_author_an_implementer(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    _rules(temp_db, project_id)
    seats = AgentSessions(temp_db, project_id)
    implementer = _author(seats, _SELECTED)
    task = LocalTaskManager(temp_db).create_task(project_id, "Bake a cake")
    first = _submit(temp_db, implementer, task.id, "build")
    _submit(temp_db, implementer, task.id, "build", supersedes_id=first.id)

    with pytest.raises(AttestationError, match=_RULE):
        _submit(temp_db, implementer, task.id, "sign-off")


def test_another_session_of_the_same_definition_may_review(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    _rules(temp_db, project_id)
    seats = AgentSessions(temp_db, project_id)
    implementer = _author(seats, _SELECTED)
    reviewer = _author(seats, _SELECTED)
    task = LocalTaskManager(temp_db).create_task(project_id, "Bake a cake")
    _submit(temp_db, implementer, task.id, "build")

    _submit(temp_db, reviewer, task.id, "sign-off")

    assert [record.kind for record in list_attestations(temp_db, task.id)] == [
        "build",
        "sign-off",
    ]


def test_definition_that_does_not_select_the_rule_may_record_both_kinds(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    _rules(temp_db, project_id)
    seats = AgentSessions(temp_db, project_id)
    author = _author(seats, _EXEMPT)
    task = LocalTaskManager(temp_db).create_task(project_id, "Bake a cake")

    _submit(temp_db, author, task.id, "build")
    _submit(temp_db, author, task.id, "sign-off")

    assert [record.kind for record in list_attestations(temp_db, task.id)] == [
        "build",
        "sign-off",
    ]


def test_reviewer_is_refused_implementation_after_recording_review(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    _rules(temp_db, project_id)
    seats = AgentSessions(temp_db, project_id)
    author = _author(seats, _SELECTED)
    task = LocalTaskManager(temp_db).create_task(project_id, "Bake a cake")
    _submit(temp_db, author, task.id, "draft")
    _submit(temp_db, author, task.id, "sign-off")

    with pytest.raises(AttestationError, match=_RULE):
        _submit(temp_db, author, task.id, "build")


def _snapshot(
    db: HubDatabase, task_id: str, event: Literal["task_close", "task_land"]
) -> ClosureSnapshot:
    return ClosureSnapshot(
        event=event,
        task={"id": task_id},
        attestations=tuple(list_attestations(db, task_id)),
    )


def _closer(selected: bool) -> AgentDefinitionBody:
    include = [f"tag:{_TAG}"] if selected else []
    return AgentDefinitionBody.model_validate(
        {
            "name": "synthetic-closer" if selected else "synthetic-exempt-closer",
            "prompts": {"agent": "Close the task."},
            "workflows": {"rule_selectors": {"include": include}},
        }
    )


@pytest.mark.parametrize("event", ["task_close", "task_land"])
def test_review_authored_by_an_implementer_blocks_closure_and_landing(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    event: Literal["task_close", "task_land"],
) -> None:
    project_id = sample_project["id"]
    _rules(temp_db, project_id)
    seats = AgentSessions(temp_db, project_id)
    implementer = _author(seats, _EXEMPT)
    task = LocalTaskManager(temp_db).create_task(project_id, "Bake a cake")
    _submit(temp_db, implementer, task.id, "build")
    _submit(temp_db, implementer, task.id, "sign-off")
    rules = RuleDefinitionManager(temp_db).list_effective(project_id)

    blocked = evaluate_closure_policy(
        _snapshot(temp_db, task.id, event), agent=_closer(True), rules=rules
    )

    assert not blocked.allowed
    assert any("independent seat" in reason for reason in blocked.blockers)


@pytest.mark.parametrize("event", ["task_close", "task_land"])
def test_independent_review_in_place_of_implementer_review_passes(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    event: Literal["task_close", "task_land"],
) -> None:
    project_id = sample_project["id"]
    _rules(temp_db, project_id)
    seats = AgentSessions(temp_db, project_id)
    implementer = _author(seats, _EXEMPT)
    reviewer = _author(seats, _SELECTED)
    task = LocalTaskManager(temp_db).create_task(project_id, "Bake a cake")
    _submit(temp_db, implementer, task.id, "build")
    _submit(temp_db, implementer, task.id, "sign-off")
    _submit(temp_db, reviewer, task.id, "sign-off")
    rules = RuleDefinitionManager(temp_db).list_effective(project_id)

    decision = evaluate_closure_policy(
        _snapshot(temp_db, task.id, event), agent=_closer(True), rules=rules
    )

    assert decision.allowed


def test_closer_that_does_not_select_the_rule_is_not_constrained(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    _rules(temp_db, project_id)
    seats = AgentSessions(temp_db, project_id)
    implementer = _author(seats, _EXEMPT)
    task = LocalTaskManager(temp_db).create_task(project_id, "Bake a cake")
    _submit(temp_db, implementer, task.id, "build")
    _submit(temp_db, implementer, task.id, "sign-off")
    rules = RuleDefinitionManager(temp_db).list_effective(project_id)

    decision = evaluate_closure_policy(
        _snapshot(temp_db, task.id, "task_close"), agent=_closer(False), rules=rules
    )

    assert decision.allowed


def test_authorship_declaration_is_a_closed_schema_with_a_maker_implementation() -> None:
    base: dict[str, Any] = {
        "event": "task_close",
        "when": "True",
        "effects": [{"type": "block", "reason": "Review."}],
    }
    RuleDefinitionBody.model_validate(
        {
            **base,
            "attestation_authorship": {"implementation_kind": "build", "maker_kinds": _MAKER},
        }
    )
    with pytest.raises(ValidationError):
        RuleDefinitionBody.model_validate(
            {
                **base,
                "attestation_authorship": {
                    "implementation_kind": "build",
                    "maker_kinds": ["draft"],
                },
            }
        )
    with pytest.raises(ValidationError):
        RuleDefinitionBody.model_validate(
            {
                **base,
                "attestation_authorship": {
                    "implementation_kind": "build",
                    "maker_kinds": _MAKER,
                    "unexpected": True,
                },
            }
        )
