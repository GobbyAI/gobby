"""Authoritative final-state evidence assembled during task-close evaluation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from gobby.storage.tasks import Task
from gobby.storage.verification_receipts import (
    VerificationReceipt,
    VerificationReceiptWrite,
    verification_receipt_id,
)
from gobby.utils.datetime import utc_now

_EXCERPT_BYTES = 4096
_CATEGORY_ARTIFACT_TYPES = {
    "docs": "document_artifact",
    "planning": "plan_artifact",
    "research": "research_artifact",
}


@dataclass(frozen=True)
class TaskStateEvidence:
    """One stable linked-state receipt and its durable write representation."""

    receipt: VerificationReceipt
    write: VerificationReceiptWrite


def _excerpt(value: str, *, first: bool) -> str:
    encoded = value.encode("utf-8")
    selected = encoded[:_EXCERPT_BYTES] if first else encoded[-_EXCERPT_BYTES:]
    return selected.decode("utf-8", errors="replace")


def build_linked_diff_evidence(
    task: Task,
    *,
    session_id: str,
    validation_context: str,
    observed_at: datetime | None = None,
) -> TaskStateEvidence | None:
    """Build stable evidence for the final linked-commit diff represented by context."""
    if not task.commits or not validation_context.strip():
        return None
    timestamp = observed_at or utc_now()
    encoded = validation_context.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    evidence_type = _CATEGORY_ARTIFACT_TYPES.get(task.category or "", "linked_diff")
    execution_id = f"task-state:{task.id}:epoch:{task.validation_epoch}:{evidence_type}"
    details = {
        "commit_shas": list(task.commits),
        "content_sha256": digest,
        "task_category": task.category,
        "task_type": task.task_type,
    }
    write = VerificationReceiptWrite(
        project_id=task.project_id,
        session_id=session_id,
        task_id=task.id,
        provider="gobby",
        execution_id=execution_id,
        source_event_id=execution_id,
        evidence_type=evidence_type,
        normalized_outcome="success",
        started_at=timestamp,
        command=None,
        outcome_provenance="git.linked_commit_diff",
        completed_at=timestamp,
        output=validation_context,
        validation_epoch=task.validation_epoch,
        details=details,
        attribution_source="explicit_task",
        attribution_actor=session_id,
        attributed_at=timestamp,
    )
    receipt = VerificationReceipt(
        id=verification_receipt_id(
            task.project_id,
            session_id,
            "gobby",
            execution_id,
        ),
        project_id=task.project_id,
        session_id=session_id,
        task_id=task.id,
        provider="gobby",
        execution_id=execution_id,
        source_event_id=execution_id,
        evidence_type=evidence_type,
        command=None,
        cwd=None,
        normalized_outcome="success",
        outcome_provenance="git.linked_commit_diff",
        exit_code=None,
        started_at=timestamp,
        completed_at=timestamp,
        output_first_4k=_excerpt(validation_context, first=True),
        output_last_4k=_excerpt(validation_context, first=False),
        output_sha256=digest,
        output_bytes=len(encoded),
        validation_epoch=task.validation_epoch,
        details=details,
        attribution_source="explicit_task",
        attribution_actor=session_id,
        attributed_at=timestamp,
        created_at=timestamp,
        updated_at=timestamp,
    )
    return TaskStateEvidence(receipt=receipt, write=write)
