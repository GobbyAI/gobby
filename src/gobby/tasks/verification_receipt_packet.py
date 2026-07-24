"""Deterministic, risk-ranked verification receipt prompt packets.

Detail ranking: explicit receipts, then unresolved failures/conflicts (newer
than the latest success), then successes, then resolved failures, then
unknowns — recency within each group. Historical red-phase failures rank
below the final green run without hiding a current failure.

The refusal floor guarantees one representative catalog row per high-risk
outcome (failure/conflicting/unknown) plus the completeness disclosure; the
remaining high-risk receipts stay visible through aggregated tail tallies.
``evidence_budget_exceeded`` fires only when that minimal disclosure itself
cannot fit the budget.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from gobby.storage.verification_receipts import VerificationReceipt
from gobby.tasks.verification_outcome_projection import (
    VerificationOutcomeProjection,
    project_verification_outcomes,
)

VERIFICATION_RECEIPT_PACKET_BUDGET_CHARS = 32_000
_DETAIL_LIMIT = 12
_HIGH_RISK_OUTCOMES = frozenset({"failure", "conflicting", "unknown"})


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _middle_elide(value: str | None, max_chars: int) -> str | None:
    if value is None or len(value) <= max_chars:
        return value
    if max_chars <= 1:
        return "…"[:max_chars]
    head = (max_chars - 1) // 2
    tail = max_chars - head - 1
    return value[:head] + "…" + value[-tail:]


def _timestamp(receipt: VerificationReceipt) -> datetime:
    return receipt.completed_at or receipt.started_at


def _priority(
    receipt: VerificationReceipt,
    explicit_ids: frozenset[str],
    latest_success_at: datetime | None,
) -> tuple[int, int, float, str]:
    outcome = receipt.normalized_outcome
    unresolved_failure = outcome in {"failure", "conflicting"} and (
        latest_success_at is None or _timestamp(receipt) > latest_success_at
    )
    if receipt.id in explicit_ids:
        group = 0
    elif unresolved_failure:
        group = 1
    elif outcome == "success":
        group = 2
    elif outcome in {"failure", "conflicting"}:
        group = 3
    elif outcome == "unknown":
        group = 4
    else:
        group = 5
    return (
        0 if receipt.id in explicit_ids else 1,
        group,
        -_timestamp(receipt).timestamp(),
        receipt.id,
    )


@dataclass(frozen=True)
class EvidenceCompleteness:
    total: int
    effective_total: int
    detailed: int
    catalogued: int
    aggregated: int
    unassigned: int
    per_outcome: dict[str, int]
    effective_per_outcome: dict[str, int]
    superseded_total: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "effective_total": self.effective_total,
            "detailed": self.detailed,
            "catalogued": self.catalogued,
            "aggregated": self.aggregated,
            "unassigned": self.unassigned,
            "per_outcome": self.per_outcome,
            "effective_per_outcome": self.effective_per_outcome,
            "superseded_total": self.superseded_total,
        }


@dataclass(frozen=True)
class VerificationReceiptPacket:
    text: str | None
    disclosure: EvidenceCompleteness
    projection: VerificationOutcomeProjection
    error: str | None = None
    detailed_receipt_ids: tuple[str, ...] = ()
    catalogued_receipt_ids: tuple[str, ...] = ()


def _detail(receipt: VerificationReceipt) -> dict[str, Any]:
    return {
        "receipt_id": receipt.id,
        "provider": receipt.provider,
        "execution_id": receipt.execution_id,
        "outcome": receipt.normalized_outcome,
        "outcome_provenance": receipt.outcome_provenance,
        "exit_code": receipt.exit_code,
        "command": receipt.command,
        "cwd": _middle_elide(receipt.cwd, 256),
        "started_at": receipt.started_at.isoformat(),
        "completed_at": receipt.completed_at.isoformat() if receipt.completed_at else None,
        "output_first": _middle_elide(receipt.output_first_4k, 768),
        "output_last": _middle_elide(receipt.output_last_4k, 768),
        "output_sha256": receipt.output_sha256,
        "output_bytes": receipt.output_bytes,
        "evidence_type": receipt.evidence_type,
        "details_json": _middle_elide(_json(receipt.details), 512),
    }


def _catalog(receipt: VerificationReceipt, *, command_chars: int = 160) -> dict[str, Any]:
    return {
        "receipt_id": receipt.id,
        "outcome": receipt.normalized_outcome,
        "exit_code": receipt.exit_code,
        "command": _middle_elide(receipt.command, command_chars),
        "completed_at": receipt.completed_at.isoformat() if receipt.completed_at else None,
    }


def _aggregate(receipts: Sequence[VerificationReceipt]) -> dict[str, Any] | None:
    if not receipts:
        return None
    return {
        "count": len(receipts),
        "receipt_id_range": {"first": receipts[0].id, "last": receipts[-1].id},
        "outcomes": project_verification_outcomes(receipts).per_outcome,
    }


def _render(
    *,
    projection: VerificationOutcomeProjection,
    unassigned: int,
    details: Sequence[dict[str, Any]],
    catalog: Sequence[dict[str, Any]],
    aggregated_receipts: Sequence[VerificationReceipt],
) -> tuple[str, EvidenceCompleteness]:
    disclosure = EvidenceCompleteness(
        total=projection.raw_total,
        effective_total=projection.total,
        detailed=len(details),
        catalogued=len(catalog),
        aggregated=len(aggregated_receipts),
        unassigned=unassigned,
        per_outcome=projection.raw_per_outcome,
        effective_per_outcome=projection.per_outcome,
        superseded_total=projection.superseded_total,
    )
    payload: dict[str, Any] = {
        "canonical_outcome_projection": projection.to_dict(),
        "evidence_completeness": disclosure.to_dict(),
        "detailed_receipts": list(details),
        "receipt_catalog": list(catalog),
    }
    aggregate = _aggregate(aggregated_receipts)
    if aggregate is not None:
        payload["aggregated_tail"] = [aggregate]
    return "Verification receipt packet:\n" + _json(payload), disclosure


def build_verification_receipt_packet(
    receipts: Sequence[VerificationReceipt],
    *,
    explicit_receipt_ids: Sequence[str] = (),
    unassigned_count: int = 0,
    budget_chars: int = VERIFICATION_RECEIPT_PACKET_BUDGET_CHARS,
) -> VerificationReceiptPacket:
    """Build a bounded packet or refuse only when its minimal disclosure cannot fit."""
    if budget_chars <= 0:
        raise ValueError("budget_chars must be positive")
    projection = project_verification_outcomes(receipts)
    explicit_ids = frozenset(
        projection.resolve_receipt_id(receipt_id) for receipt_id in explicit_receipt_ids
    )
    success_times = [
        _timestamp(receipt)
        for receipt in projection.receipts
        if receipt.normalized_outcome == "success"
    ]
    latest_success_at = max(success_times) if success_times else None
    ordered = sorted(
        projection.receipts,
        key=lambda receipt: _priority(receipt, explicit_ids, latest_success_at),
    )
    mandatory: list[VerificationReceipt] = []
    represented_high_risk: set[str] = set()
    for receipt in ordered:
        outcome = receipt.normalized_outcome
        if outcome in _HIGH_RISK_OUTCOMES and outcome not in represented_high_risk:
            mandatory.append(receipt)
            represented_high_risk.add(outcome)
    mandatory_ids = {receipt.id for receipt in mandatory}
    catalog_receipts = list(mandatory)
    catalog = [_catalog(receipt, command_chars=48) for receipt in catalog_receipts]
    tail = [receipt for receipt in ordered if receipt.id not in mandatory_ids]
    floor_text, floor_disclosure = _render(
        projection=projection,
        unassigned=unassigned_count,
        details=[],
        catalog=catalog,
        aggregated_receipts=tail,
    )
    if len(floor_text) > budget_chars:
        return VerificationReceiptPacket(
            text=None,
            disclosure=floor_disclosure,
            projection=projection,
            error="evidence_budget_exceeded",
            catalogued_receipt_ids=tuple(receipt.id for receipt in catalog_receipts),
        )

    details: list[dict[str, Any]] = []
    for receipt in ordered:
        if len(details) >= _DETAIL_LIMIT:
            break
        candidate_details = [*details, _detail(receipt)]
        candidate_text, _ = _render(
            projection=projection,
            unassigned=unassigned_count,
            details=candidate_details,
            catalog=catalog,
            aggregated_receipts=tail,
        )
        if len(candidate_text) <= budget_chars:
            details = candidate_details

    for receipt in [item for item in tail if item.normalized_outcome not in _HIGH_RISK_OUTCOMES]:
        candidate_receipts = [*catalog_receipts, receipt]
        candidate_ids = {item.id for item in candidate_receipts}
        candidate_tail = [item for item in ordered if item.id not in candidate_ids]
        candidate_catalog = [
            _catalog(item, command_chars=48 if item.id in mandatory_ids else 160)
            for item in candidate_receipts
        ]
        candidate_text, _ = _render(
            projection=projection,
            unassigned=unassigned_count,
            details=details,
            catalog=candidate_catalog,
            aggregated_receipts=candidate_tail,
        )
        if len(candidate_text) > budget_chars:
            break
        catalog_receipts = candidate_receipts
        catalog = candidate_catalog
        tail = candidate_tail

    text, disclosure = _render(
        projection=projection,
        unassigned=unassigned_count,
        details=details,
        catalog=catalog,
        aggregated_receipts=tail,
    )
    return VerificationReceiptPacket(
        text=text,
        disclosure=disclosure,
        projection=projection,
        detailed_receipt_ids=tuple(str(item["receipt_id"]) for item in details),
        catalogued_receipt_ids=tuple(receipt.id for receipt in catalog_receipts),
    )
