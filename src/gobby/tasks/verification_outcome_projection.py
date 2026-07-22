"""Canonical completion-readiness projection over durable verification receipts."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from gobby.storage.verification_receipts import VerificationReceipt


@dataclass(frozen=True)
class VerificationOutcomeProjection:
    """Provider-neutral outcome summary used by task-close evidence consumers."""

    receipts: tuple[VerificationReceipt, ...]
    per_outcome: dict[str, int]
    ready: bool
    latest_receipt_id: str | None
    latest_timestamp: datetime | None

    @property
    def total(self) -> int:
        return len(self.receipts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "per_outcome": dict(self.per_outcome),
            "ready": self.ready,
            "latest_receipt_id": self.latest_receipt_id,
            "latest_timestamp": (
                self.latest_timestamp.isoformat() if self.latest_timestamp is not None else None
            ),
        }


def _receipt_timestamp(receipt: VerificationReceipt) -> datetime:
    return receipt.completed_at or receipt.started_at


def project_verification_outcomes(
    receipts: Sequence[VerificationReceipt],
) -> VerificationOutcomeProjection:
    """Project durable receipts without interpreting commands, output, or providers."""
    ordered = tuple(
        sorted(
            receipts,
            key=lambda receipt: (_receipt_timestamp(receipt), receipt.id),
        )
    )
    per_outcome: dict[str, int] = dict(
        sorted(Counter(receipt.normalized_outcome for receipt in ordered).items())
    )
    latest = ordered[-1] if ordered else None
    return VerificationOutcomeProjection(
        receipts=ordered,
        per_outcome=per_outcome,
        ready=per_outcome.get("success", 0) > 0,
        latest_receipt_id=latest.id if latest is not None else None,
        latest_timestamp=_receipt_timestamp(latest) if latest is not None else None,
    )


__all__ = ["VerificationOutcomeProjection", "project_verification_outcomes"]
