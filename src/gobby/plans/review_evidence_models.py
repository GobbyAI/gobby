"""Typed records and validation for durable plan-review evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

ReviewVerdict = Literal["approved", "needs_review"]
LessonMintStatus = Literal["pending", "minted", "failed", "none"]
ManifestState = Literal["pending", "applied", "revoked"]


class ReviewEvidenceError(ValueError):
    """Deterministic boundary error returned by plan-review evidence tools."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": False,
            "error": self.code,
            "message": str(self),
            "retryable": self.retryable,
            **self.details,
        }


@dataclass(frozen=True)
class SectionHash:
    """Hash identity for one complete snapshot section."""

    section_id: str
    section_hash: str

    def to_dict(self) -> dict[str, str]:
        return {
            "section_id": self.section_id,
            "section_hash": self.section_hash,
        }


@dataclass(frozen=True)
class PreparedReviewEvidence:
    """Public preparation result."""

    evidence_id: str
    plan_hash: str
    sections: tuple[SectionHash, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "evidence_id": self.evidence_id,
            "plan_hash": self.plan_hash,
            "sections": [section.to_dict() for section in self.sections],
        }


@dataclass(frozen=True)
class PlanReviewEvidence:
    """One immutable evidence row plus its durable lifecycle checkpoints."""

    evidence_id: str
    project_id: str
    plan_path: str
    plan_hash: str
    section_manifest: tuple[SectionHash, ...]
    snapshot: bytes
    round_number: int
    session_id: str | None
    task_id: str | None
    stage: str | None
    dispatch_run_id: str | None
    lease_expires_at: datetime | None
    finalized_at: datetime | None
    expired_at: datetime | None
    round_result: dict[str, object] | None
    approval_result: dict[str, object] | None
    approved_at: datetime | None
    lesson_mint_status: LessonMintStatus | None
    lesson_mint_detail: dict[str, object] | None
    manifest_digest: str | None
    manifest_payload: dict[str, object] | None
    manifest_state: ManifestState | None
    manifest_result: dict[str, object] | None
    manifest_applied_at: datetime | None
    quality_ledger: list[dict[str, object]] | None
    repair_attestations: list[dict[str, object]] | None
    prior_round_context: dict[str, object] | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> PlanReviewEvidence:
        manifest_raw = _json_value(row["section_manifest"])
        if not isinstance(manifest_raw, list):
            raise ReviewEvidenceError(
                "invalid_evidence_row",
                "section_manifest must be a JSON array",
            )
        sections: list[SectionHash] = []
        for item in manifest_raw:
            if not isinstance(item, dict):
                raise ReviewEvidenceError(
                    "invalid_evidence_row",
                    "section_manifest entries must be JSON objects",
                )
            section_id = item.get("section_id")
            section_hash = item.get("section_hash")
            if not isinstance(section_id, str) or not isinstance(section_hash, str):
                raise ReviewEvidenceError(
                    "invalid_evidence_row",
                    "section_manifest entries require string section_id and section_hash",
                )
            sections.append(SectionHash(section_id=section_id, section_hash=section_hash))
        return cls(
            evidence_id=str(row["evidence_id"]),
            project_id=str(row["project_id"]),
            plan_path=str(row["plan_path"]),
            plan_hash=str(row["plan_hash"]),
            section_manifest=tuple(sections),
            snapshot=bytes(row["snapshot"]),
            round_number=int(row["round_number"]),
            session_id=_optional_string(row["session_id"]),
            task_id=_optional_string(row["task_id"]),
            stage=_optional_string(row["stage"]),
            dispatch_run_id=_optional_string(row["dispatch_run_id"]),
            lease_expires_at=cast(datetime | None, row["lease_expires_at"]),
            finalized_at=cast(datetime | None, row["finalized_at"]),
            expired_at=cast(datetime | None, row["expired_at"]),
            round_result=_optional_json_object(row["round_result"]),
            approval_result=_optional_json_object(row["approval_result"]),
            approved_at=cast(datetime | None, row["approved_at"]),
            lesson_mint_status=cast(LessonMintStatus | None, row["lesson_mint_status"]),
            lesson_mint_detail=_optional_json_object(row["lesson_mint_detail"]),
            manifest_digest=_optional_string(row["manifest_digest"]),
            manifest_payload=_optional_json_object(row["manifest_payload"]),
            manifest_state=cast(ManifestState | None, row["manifest_state"]),
            manifest_result=_optional_json_object(row["manifest_result"]),
            manifest_applied_at=cast(datetime | None, row["manifest_applied_at"]),
            quality_ledger=_optional_json_object_list(row["quality_ledger"]),
            repair_attestations=_optional_json_object_list(row["repair_attestations"]),
            prior_round_context=_optional_json_object(row["prior_round_context"]),
            created_at=cast(datetime, row["created_at"]),
        )

    @property
    def is_interactive(self) -> bool:
        return self.session_id is not None

    @property
    def is_live(self) -> bool:
        return self.finalized_at is None and self.expired_at is None

    def prepared_result(self) -> PreparedReviewEvidence:
        return PreparedReviewEvidence(
            evidence_id=self.evidence_id,
            plan_hash=self.plan_hash,
            sections=self.section_manifest,
        )


def validate_round_result(raw: Mapping[str, object]) -> dict[str, object]:
    """Validate and canonicalize the stable round-result envelope."""
    from gobby.plans.review_coverage import validate_coverage_attestation
    from gobby.plans.review_ledger import validate_candidate_dispositions

    payload = canonical_json_object(raw)
    verdict = payload.get("verdict")
    if verdict in {"needs_requirements", "inconclusive"}:
        return _validate_non_attested_result(payload, verdict=str(verdict))
    if verdict not in {"approved", "needs_review"}:
        raise ReviewEvidenceError(
            "invalid_round_result",
            "round_result.verdict is not a supported terminal verdict",
        )
    findings = payload.get("findings")
    if not isinstance(findings, list):
        raise ReviewEvidenceError(
            "invalid_round_result",
            "round_result.findings must be an array",
        )
    if any(not isinstance(finding, dict) for finding in findings):
        raise ReviewEvidenceError(
            "invalid_round_result",
            "round_result.findings entries must be objects",
        )
    payload["coverage_attestation"] = validate_coverage_attestation(
        payload.get("coverage_attestation"),
        verdict=str(verdict),
    )
    dispositions = validate_candidate_dispositions(payload)
    if "candidate_dispositions" in payload or dispositions:
        payload["candidate_dispositions"] = dispositions
    if verdict == "approved":
        entries = payload.get("manifest_entries")
        if not isinstance(entries, list) or not entries:
            raise ReviewEvidenceError(
                "invalid_round_result",
                "approved round_result requires non-empty manifest_entries",
            )
        if any(not isinstance(entry, dict) for entry in entries):
            raise ReviewEvidenceError(
                "invalid_round_result",
                "round_result.manifest_entries entries must be objects",
            )
        routing_decisions = payload.get("routing_decisions")
        if not isinstance(routing_decisions, dict):
            raise ReviewEvidenceError(
                "invalid_round_result",
                "approved round_result requires routing_decisions",
            )
    return payload


def _validate_non_attested_result(
    payload: dict[str, object],
    *,
    verdict: str,
) -> dict[str, object]:
    if set(payload) != {"verdict", "evidence_id", "reason"}:
        raise ReviewEvidenceError(
            "invalid_round_result",
            f"{verdict} round_result must contain exactly verdict, evidence_id, and reason",
        )
    evidence_id = payload.get("evidence_id")
    if not isinstance(evidence_id, str) or not evidence_id:
        raise ReviewEvidenceError(
            "invalid_round_result",
            f"{verdict} round_result requires a non-empty evidence_id",
        )
    reason = payload.get("reason")
    if not isinstance(reason, dict):
        raise ReviewEvidenceError(
            "invalid_round_result",
            f"{verdict} round_result.reason must be an object",
        )
    reason_code = reason.get("reason_code")
    if verdict == "needs_requirements":
        _validate_string_list_reason(
            reason,
            reason_code="missing_requirements",
            field="questions",
        )
        return payload
    if reason_code == "source_drift":
        _validate_string_list_reason(reason, reason_code="source_drift", field="paths")
    elif reason_code == "index_mismatch":
        _validate_index_mismatch_reason(reason)
    elif reason_code == "timeout":
        _validate_timeout_reason(reason)
    else:
        raise ReviewEvidenceError(
            "invalid_round_result",
            "inconclusive reason_code must be source_drift, index_mismatch, or timeout",
        )
    return payload


def _validate_string_list_reason(
    reason: dict[str, object],
    *,
    reason_code: str,
    field: str,
) -> None:
    if set(reason) != {"reason_code", field} or reason.get("reason_code") != reason_code:
        raise ReviewEvidenceError(
            "invalid_round_result",
            f"{reason_code} reason must contain exactly reason_code and {field}",
        )
    values = reason.get(field)
    if (
        not isinstance(values, list)
        or not values
        or any(not isinstance(value, str) or not value for value in values)
    ):
        raise ReviewEvidenceError(
            "invalid_round_result",
            f"{reason_code} reason.{field} must be a non-empty string array",
        )


def _validate_index_mismatch_reason(reason: dict[str, object]) -> None:
    expected_keys = {"reason_code", "expected_token", "actual_token"}
    if set(reason) != expected_keys:
        raise ReviewEvidenceError(
            "invalid_round_result",
            "index_mismatch reason must contain reason_code, expected_token, and actual_token",
        )
    for field in ("expected_token", "actual_token"):
        value = reason.get(field)
        if not isinstance(value, str) or not value:
            raise ReviewEvidenceError(
                "invalid_round_result",
                f"index_mismatch reason.{field} must be a non-empty string",
            )


def _validate_timeout_reason(reason: dict[str, object]) -> None:
    if set(reason) != {"reason_code", "timeout_seconds"}:
        raise ReviewEvidenceError(
            "invalid_round_result",
            "timeout reason must contain exactly reason_code and timeout_seconds",
        )
    timeout_seconds = reason.get("timeout_seconds")
    if (
        not isinstance(timeout_seconds, int | float)
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= 0
    ):
        raise ReviewEvidenceError(
            "invalid_round_result",
            "timeout reason.timeout_seconds must be a positive number",
        )


def canonical_json_object(raw: Mapping[str, object]) -> dict[str, object]:
    """Round-trip through canonical JSON to reject non-JSON values and detach callers."""
    try:
        encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ReviewEvidenceError(
            "invalid_json_payload",
            f"payload must be JSON-serializable: {exc}",
        ) from exc
    if not isinstance(decoded, dict):
        raise ReviewEvidenceError("invalid_json_payload", "payload must be a JSON object")
    return cast(dict[str, object], decoded)


def canonical_json_bytes(raw: Mapping[str, object]) -> bytes:
    payload = canonical_json_object(raw)
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _json_value(raw: object) -> object:
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _optional_json_object(raw: object) -> dict[str, object] | None:
    if raw is None:
        return None
    value = _json_value(raw)
    if not isinstance(value, dict):
        raise ReviewEvidenceError("invalid_evidence_row", "JSON column must contain an object")
    return cast(dict[str, object], value)


def _optional_json_object_list(raw: object) -> list[dict[str, object]] | None:
    if raw is None:
        return None
    value = _json_value(raw)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ReviewEvidenceError(
            "invalid_evidence_row",
            "JSON column must contain an array of objects",
        )
    return cast(list[dict[str, object]], value)


def _optional_string(raw: object) -> str | None:
    return str(raw) if raw is not None else None
