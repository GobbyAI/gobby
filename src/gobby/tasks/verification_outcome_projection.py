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
    raw_receipts: tuple[VerificationReceipt, ...]
    per_outcome: dict[str, int]
    raw_per_outcome: dict[str, int]
    superseded_by: dict[str, str]
    ready: bool
    latest_receipt_id: str | None
    latest_timestamp: datetime | None

    @property
    def total(self) -> int:
        return len(self.receipts)

    @property
    def raw_total(self) -> int:
        return len(self.raw_receipts)

    @property
    def superseded_total(self) -> int:
        return len(self.superseded_by)

    def resolve_receipt_id(self, receipt_id: str) -> str:
        """Resolve an audit receipt ID to its effective authoritative receipt."""
        return self.superseded_by.get(receipt_id, receipt_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "per_outcome": dict(self.per_outcome),
            "raw_total": self.raw_total,
            "raw_per_outcome": dict(self.raw_per_outcome),
            "superseded_total": self.superseded_total,
            "ready": self.ready,
            "latest_receipt_id": self.latest_receipt_id,
            "latest_timestamp": (
                self.latest_timestamp.isoformat() if self.latest_timestamp is not None else None
            ),
        }


def _receipt_timestamp(receipt: VerificationReceipt) -> datetime:
    return receipt.completed_at or receipt.started_at


def _is_authoritative(receipt: VerificationReceipt) -> bool:
    return (
        receipt.completed_at is not None
        and receipt.normalized_outcome in {"success", "failure"}
        and receipt.exit_code is not None
    )


def _is_supersession_candidate(receipt: VerificationReceipt) -> bool:
    return (
        receipt.normalized_outcome in {"provisional", "unknown"}
        and receipt.outcome_provenance == "before_tool"
    )


def _is_nested_execution(receipt: VerificationReceipt, authority: VerificationReceipt) -> bool:
    prefix = f"{receipt.execution_id}:"
    suffix = authority.execution_id.removeprefix(prefix)
    return authority.execution_id.startswith(prefix) and suffix.isdigit()


def _has_matching_command_output(
    receipt: VerificationReceipt,
    authority: VerificationReceipt,
) -> bool:
    return (
        receipt.command is not None
        and receipt.command == authority.command
        and receipt.output_sha256 is not None
        and receipt.output_sha256 == authority.output_sha256
    )


def _matching_authorities(
    receipt: VerificationReceipt,
    ordered: Sequence[VerificationReceipt],
) -> list[VerificationReceipt]:
    return [
        authority
        for authority in ordered
        if authority.session_id == receipt.session_id
        and authority.started_at > receipt.started_at
        and _is_authoritative(authority)
        and (
            _is_nested_execution(receipt, authority)
            or _has_matching_command_output(receipt, authority)
        )
    ]


def _superseded_receipts(
    ordered: Sequence[VerificationReceipt],
) -> dict[str, str]:
    superseded_by: dict[str, str] = {}
    for receipt in ordered:
        if not _is_supersession_candidate(receipt):
            continue
        authorities = _matching_authorities(receipt, ordered)
        outcomes = {authority.normalized_outcome for authority in authorities}
        if len(outcomes) != 1:
            continue
        authority = min(
            authorities,
            key=lambda item: (item.started_at, item.id),
        )
        superseded_by[receipt.id] = authority.id
    return superseded_by


def project_verification_outcomes(
    receipts: Sequence[VerificationReceipt],
) -> VerificationOutcomeProjection:
    """Project durable receipts after reconciling duplicate capture layers."""
    raw_receipts = tuple(
        sorted(
            receipts,
            key=lambda receipt: (_receipt_timestamp(receipt), receipt.id),
        )
    )
    superseded_by = _superseded_receipts(raw_receipts)
    ordered = tuple(receipt for receipt in raw_receipts if receipt.id not in superseded_by)
    per_outcome: dict[str, int] = dict(
        sorted(Counter(receipt.normalized_outcome for receipt in ordered).items())
    )
    raw_per_outcome: dict[str, int] = dict(
        sorted(Counter(receipt.normalized_outcome for receipt in raw_receipts).items())
    )
    latest = ordered[-1] if ordered else None
    return VerificationOutcomeProjection(
        receipts=ordered,
        raw_receipts=raw_receipts,
        per_outcome=per_outcome,
        raw_per_outcome=raw_per_outcome,
        superseded_by=superseded_by,
        ready=per_outcome.get("success", 0) > 0,
        latest_receipt_id=latest.id if latest is not None else None,
        latest_timestamp=_receipt_timestamp(latest) if latest is not None else None,
    )


__all__ = ["VerificationOutcomeProjection", "project_verification_outcomes"]
