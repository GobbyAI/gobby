"""Generic review attestations written through the authenticated evidence writer."""

from __future__ import annotations

from collections.abc import Sequence

from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.tasks.attestations import (
    AttestationSubmission,
    EvidenceReference,
    TaskAttestation,
    record_attestation,
)
from gobby.workflows.state_manager import SessionVariableManager

REVIEW_WRITER = "fixture-review-writer"


def grant_step_writes(
    db: HubDatabase,
    session_id: str,
    project_id: str,
    name: str,
    steps: Sequence[str],
    *,
    capabilities: Sequence[str] = (),
    supplemental_review: bool = False,
    team_manager: bool = False,
    integration_paths: Sequence[str] = (),
) -> None:
    """Run a registered session as an installed definition that writes the supplied arbitrary evidence kinds."""
    definitions = AgentDefinitionManager(db)
    if definitions.get_by_name(name, project_id) is None:
        definitions.create(
            name,
            {
                "name": name,
                "version": "1.0",
                "prompts": {"agent": "Record the assigned task evidence."},
                "workflows": {
                    "rule_selectors": {"include": []},
                    "variables": {"team_manager": team_manager},
                },
                "task_attestation_writes": list(steps),
                "capabilities": list(capabilities),
                "supplemental_review": supplemental_review,
                "direct_integration_commit_paths": list(integration_paths),
            },
            project_id=project_id,
        )
    SessionVariableManager(db).set_variable(session_id, "_agent_type", name)


def grant_review_writes(db: HubDatabase, session_id: str, project_id: str) -> None:
    """Give a registered session an installed definition that writes the review step."""
    grant_step_writes(db, session_id, project_id, REVIEW_WRITER, ("review",))


def approve_review(
    db: HubDatabase, *, task_id: str, reviewer_session_id: str, commit_sha: str
) -> TaskAttestation:
    """Record a LAND for the task's latest linked commit in its current review round."""
    return _review(
        db,
        task_id,
        reviewer_session_id,
        commit_sha,
        "approved",
        "LAND: reviewed against the integration head.",
    )


def reject_review(
    db: HubDatabase, *, task_id: str, reviewer_session_id: str, commit_sha: str
) -> TaskAttestation:
    """Record a BOUNCE for the task's latest linked commit in its current review round."""
    return _review(
        db,
        task_id,
        reviewer_session_id,
        commit_sha,
        "rejected",
        "BOUNCE: a blocking finding remains.",
    )


def _review(
    db: HubDatabase,
    task_id: str,
    reviewer_session_id: str,
    commit_sha: str,
    status: str,
    feedback: str,
) -> TaskAttestation:
    record, _ = record_attestation(
        db,
        task_id=task_id,
        session_id=reviewer_session_id,
        submission=AttestationSubmission(
            kind="review",
            result=status,
            summary=feedback,
            reference=EvidenceReference(scheme="git", value=commit_sha),
        ),
    )
    return record
