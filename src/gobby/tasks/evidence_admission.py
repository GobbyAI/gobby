"""Deterministic admission of task-close evidence."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from gobby.storage.verification_receipts import VerificationReceipt
from gobby.tasks.verification_outcome_projection import (
    VerificationOutcomeProjection,
    project_verification_outcomes,
)

_UNTRUSTED_PROVENANCE = frozenset(
    {
        "",
        "before_tool",
        "manual_attestation",
        "raw_result.default_success",
        "raw_result.missing",
    }
)
_ACTOR_ATTESTATION_TYPES = frozenset({"manual_diff_review", "actor_attestation"})


@dataclass(frozen=True)
class EvidenceRejection:
    """Why one audit receipt was excluded from satisfying evidence."""

    evidence_id: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"evidence_id": self.evidence_id, "reason": self.reason}


@dataclass(frozen=True)
class EvidenceAdmission:
    """Admissible evidence plus complete deterministic audit diagnostics."""

    receipts: tuple[VerificationReceipt, ...]
    projection: VerificationOutcomeProjection
    rejections: tuple[EvidenceRejection, ...]

    @property
    def evidence_ids(self) -> frozenset[str]:
        return frozenset(receipt.id for receipt in self.receipts)

    def audit_summary(self) -> dict[str, Any]:
        return {
            "admissible_total": len(self.receipts),
            "rejected_total": len(self.rejections),
            "rejected_by_reason": dict(
                sorted(Counter(rejection.reason for rejection in self.rejections).items())
            ),
        }


def _criteria_accept_actor_attestation(validation_criteria: str) -> bool:
    lowered = validation_criteria.casefold()
    return any(
        phrase in lowered
        for phrase in (
            "actor attestation",
            "agent attestation",
            "manual diff review",
            "agent confirmation",
        )
    )


def admit_task_evidence(
    receipts: Sequence[VerificationReceipt],
    *,
    task_id: str,
    validation_epoch: int,
    validation_criteria: str,
) -> EvidenceAdmission:
    """Admit only task-owned, trusted, terminal, current-state evidence."""
    projection = project_verification_outcomes(receipts)
    superseded = projection.superseded_by
    accepts_attestation = _criteria_accept_actor_attestation(validation_criteria)
    admitted: list[VerificationReceipt] = []
    rejected: list[EvidenceRejection] = []

    for receipt in projection.raw_receipts:
        reason: str | None = None
        if receipt.task_id != task_id:
            reason = "wrong_task"
        elif receipt.id in superseded:
            reason = "superseded"
        elif receipt.normalized_outcome == "pending":
            reason = "pending"
        elif receipt.normalized_outcome == "failure":
            reason = "failed"
        elif receipt.normalized_outcome == "unknown":
            reason = "unknown"
        elif receipt.normalized_outcome == "conflicting":
            reason = "conflicting"
        elif receipt.normalized_outcome != "success":
            reason = "unsupported_outcome"
        elif receipt.completed_at is None:
            reason = "non_terminal"
        elif receipt.validation_epoch is None:
            reason = "missing_validation_epoch"
        elif receipt.validation_epoch != validation_epoch:
            reason = "stale_validation_epoch"
        elif (receipt.outcome_provenance or "") in _UNTRUSTED_PROVENANCE:
            reason = "untrusted_outcome"
        elif receipt.evidence_type in _ACTOR_ATTESTATION_TYPES and not accepts_attestation:
            reason = "actor_attestation_not_accepted"

        if reason is None:
            admitted.append(receipt)
        else:
            rejected.append(EvidenceRejection(receipt.id, reason))

    return EvidenceAdmission(
        receipts=tuple(admitted),
        projection=projection,
        rejections=tuple(rejected),
    )
