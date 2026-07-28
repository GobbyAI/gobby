"""Repair-proof contracts for consecutive plan-review rounds."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from gobby.plans.review_evidence_io import build_inter_round_diff, reviewed_section_hashes
from gobby.plans.review_evidence_models import (
    PlanReviewEvidence,
    ReviewEvidenceError,
    SectionHash,
    canonical_json_object,
)
from gobby.plans.review_findings import validate_plan_review_findings

REPAIR_SUBMISSION_ARTIFACT_KEY = "plan_review_repair_submission"

DEVIATION_PROOF_FIELDS = (
    "violated_invariant",
    "original_counterexample",
    "how_alternative_closes_it",
    "validation_evidence",
    "accepted_risk",
)

_RESOLUTION_FIELDS = frozenset({"prior_finding_id", "decision"})
_DEVIATION_PROOF_FIELD_SET = frozenset(DEVIATION_PROOF_FIELDS)
_ATTESTATION_FIELDS = frozenset(
    {
        "prior_finding_id",
        "check_key",
        "changed_section_ids",
        "accepted_resolution",
        "deviation_from_minimal_repair",
        "changed_symbols",
        "consumer_sites_swept",
        "adjacent_variants_swept",
        "validation_evidence",
        "deferred_sites",
    }
)
_ATTESTATION_LIST_FIELDS = (
    "changed_section_ids",
    "changed_symbols",
    "consumer_sites_swept",
    "adjacent_variants_swept",
    "validation_evidence",
    "deferred_sites",
)
_SUBMISSION_FIELDS = frozenset(
    {
        "round_number",
        "prior_finding_resolutions",
        "repair_attestations",
        "consumed_evidence_id",
    }
)


@dataclass(frozen=True)
class RepairSubmission:
    """One typed repair payload bound to the round it prepares."""

    round_number: int
    prior_finding_resolutions: tuple[dict[str, object], ...]
    repair_attestations: tuple[dict[str, object], ...]
    consumed_evidence_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "round_number": self.round_number,
            "prior_finding_resolutions": [
                dict(record) for record in self.prior_finding_resolutions
            ],
            "repair_attestations": [dict(attestation) for attestation in self.repair_attestations],
        }


@dataclass(frozen=True)
class RepairPreparation:
    """Validated durable context for a new evidence row."""

    repair_attestations: tuple[dict[str, object], ...]
    prior_round_context: dict[str, object]


def build_repair_submission(
    *,
    round_number: int,
    prior_findings: Sequence[Mapping[str, object]],
    recorded_votes: Sequence[Mapping[str, object]],
    edit_diff: Mapping[str, Mapping[str, object]],
) -> RepairSubmission:
    """Build taskless repair records from votes and the applied edit evidence."""
    findings = _finding_identity_map(prior_findings)
    votes: dict[str, dict[str, object]] = {}
    for index, raw_vote in enumerate(recorded_votes):
        vote = canonical_json_object(raw_vote)
        unknown = sorted(set(vote) - {"prior_finding_id", "decision", "accepted_resolution"})
        if unknown:
            raise _invalid(f"recorded_votes[{index}] has unknown fields: {', '.join(unknown)}")
        finding_id = _required_string(vote, "prior_finding_id", f"recorded_votes[{index}]")
        if finding_id in votes:
            raise _invalid(f"duplicate recorded vote: {finding_id}")
        if finding_id not in findings:
            raise _invalid(f"recorded vote references unknown prior finding: {finding_id}")
        decision = vote.get("decision")
        if decision not in {"repair", "carry"}:
            raise _invalid(f"recorded vote decision must be repair or carry: {finding_id}")
        if decision == "repair":
            _required_string(vote, "accepted_resolution", f"recorded_votes[{index}]")
        elif "accepted_resolution" in vote:
            raise _invalid(f"carry vote cannot include accepted_resolution: {finding_id}")
        votes[finding_id] = vote

    missing_votes = sorted(set(findings) - set(votes))
    if missing_votes:
        raise ReviewEvidenceError(
            "missing_finding_resolution",
            f"missing resolution record for prior finding: {', '.join(missing_votes)}",
        )
    extra_edits = sorted(set(edit_diff) - set(findings))
    if extra_edits:
        raise _invalid(f"edit diff references unknown prior finding: {', '.join(extra_edits)}")

    resolutions: list[dict[str, object]] = []
    attestations: list[dict[str, object]] = []
    for finding_id, finding in findings.items():
        vote = votes[finding_id]
        decision = cast(str, vote["decision"])
        resolutions.append(
            _canonical_resolution(
                {"prior_finding_id": finding_id, "decision": decision},
                owner=f"resolution[{finding_id}]",
            )
        )
        if decision == "carry":
            continue
        if finding_id not in edit_diff:
            raise ReviewEvidenceError(
                "missing_repair_attestation",
                f"missing edit evidence for repair-decided finding: {finding_id}",
            )
        raw_attestation = canonical_json_object(edit_diff[finding_id])
        derived = {
            **raw_attestation,
            "prior_finding_id": finding_id,
            "check_key": finding["check_key"],
            "accepted_resolution": vote["accepted_resolution"],
        }
        for field in ("prior_finding_id", "check_key", "accepted_resolution"):
            if field in raw_attestation and raw_attestation[field] != derived[field]:
                raise _invalid(f"edit evidence {field} conflicts for prior finding: {finding_id}")
        attestations.append(
            _canonical_attestation(derived, owner=f"repair_attestations[{finding_id}]")
        )

    return RepairSubmission(
        round_number=_positive_round(round_number),
        prior_finding_resolutions=tuple(resolutions),
        repair_attestations=tuple(attestations),
    )


def canonicalize_repair_submission(raw: Mapping[str, object]) -> RepairSubmission:
    """Validate a transport payload without trusting caller-owned record shapes."""
    payload = canonical_json_object(raw)
    unknown = sorted(set(payload) - _SUBMISSION_FIELDS)
    if unknown:
        raise _invalid(f"repair submission has unknown fields: {', '.join(unknown)}")
    resolutions = _object_array(
        payload.get("prior_finding_resolutions"),
        owner="repair submission prior_finding_resolutions",
    )
    attestations = _object_array(
        payload.get("repair_attestations"),
        owner="repair submission repair_attestations",
    )
    consumed = payload.get("consumed_evidence_id")
    if consumed is not None and (not isinstance(consumed, str) or not consumed.strip()):
        raise _invalid("repair submission consumed_evidence_id must be a non-empty string")
    return RepairSubmission(
        round_number=_positive_round(payload.get("round_number")),
        prior_finding_resolutions=tuple(
            _canonical_resolution(record, owner=f"prior_finding_resolutions[{index}]")
            for index, record in enumerate(resolutions)
        ),
        repair_attestations=tuple(
            _canonical_attestation(record, owner=f"repair_attestations[{index}]")
            for index, record in enumerate(attestations)
        ),
        consumed_evidence_id=consumed,
    )


def encode_repair_submission(raw: Mapping[str, object] | RepairSubmission) -> str:
    """Serialize a canonical submission for stage-state persistence."""
    submission = raw if isinstance(raw, RepairSubmission) else canonicalize_repair_submission(raw)
    payload = submission.to_dict()
    if submission.consumed_evidence_id is not None:
        payload["consumed_evidence_id"] = submission.consumed_evidence_id
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def decode_repair_submission(
    raw: str,
    *,
    expected_round_number: int,
) -> RepairSubmission:
    """Decode and verify the round binding on a staged submission."""
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise _invalid(f"repair submission is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise _invalid("repair submission must be a JSON object")
    submission = canonicalize_repair_submission(cast(dict[str, object], decoded))
    if submission.round_number != expected_round_number:
        raise ReviewEvidenceError(
            "repair_submission_round_mismatch",
            "repair submission round does not match the round being prepared",
            details={
                "expected_round_number": expected_round_number,
                "submission_round_number": submission.round_number,
            },
        )
    return submission


def consumed_repair_submission(raw: str, *, evidence_id: str) -> str:
    """Return the idempotent consumption receipt for a persisted submission."""
    if not evidence_id.strip():
        raise _invalid("consumed evidence_id must be a non-empty string")
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise _invalid(f"repair submission is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise _invalid("repair submission must be a JSON object")
    submission = canonicalize_repair_submission(cast(dict[str, object], decoded))
    if (
        submission.consumed_evidence_id is not None
        and submission.consumed_evidence_id != evidence_id
    ):
        raise ReviewEvidenceError(
            "repair_submission_consumed",
            "repair submission was consumed by a different evidence row",
        )
    return encode_repair_submission(
        RepairSubmission(
            round_number=submission.round_number,
            prior_finding_resolutions=submission.prior_finding_resolutions,
            repair_attestations=submission.repair_attestations,
            consumed_evidence_id=evidence_id,
        )
    )


def validate_repair_preparation(
    *,
    prior_evidence: PlanReviewEvidence,
    current_sections: Sequence[SectionHash],
    current_snapshot: bytes,
    prior_finding_resolutions: Sequence[Mapping[str, object]] | None,
    repair_attestations: Sequence[Mapping[str, object]] | None,
) -> RepairPreparation:
    """Validate proof coverage against the server-owned prior finding universe."""
    round_result = prior_evidence.round_result
    if round_result is None:
        raise _invalid("prior evidence must have a finalized round result")
    raw_findings = round_result.get("findings")
    if not isinstance(raw_findings, list) or any(
        not isinstance(finding, Mapping) for finding in raw_findings
    ):
        raise _invalid("prior round_result.findings must be an array of objects")
    findings = validate_plan_review_findings(
        cast(list[Mapping[str, object]], raw_findings),
        evidence=prior_evidence,
    )
    finding_map = _finding_identity_map(findings)
    resolutions = [
        _canonical_resolution(record, owner=f"prior_finding_resolutions[{index}]")
        for index, record in enumerate(prior_finding_resolutions or ())
    ]
    resolution_map = _unique_records(
        resolutions,
        known_ids=set(finding_map),
        owner="resolution record",
    )
    missing_resolutions = sorted(set(finding_map) - set(resolution_map))
    if missing_resolutions:
        raise ReviewEvidenceError(
            "missing_finding_resolution",
            f"missing resolution record for prior finding: {', '.join(missing_resolutions)}",
        )

    repair_ids: set[str] = set()
    for finding_id, resolution in resolution_map.items():
        decision = resolution["decision"]
        if decision == "carry":
            if finding_map[finding_id]["severity"] == "blocking":
                raise ReviewEvidenceError(
                    "blocking_finding_carry",
                    f"blocking finding cannot use carry resolution: {finding_id}",
                )
            continue
        repair_ids.add(finding_id)

    attestations = [
        _canonical_attestation(record, owner=f"repair_attestations[{index}]")
        for index, record in enumerate(repair_attestations or ())
    ]
    attestation_map = _unique_records(
        attestations,
        known_ids=repair_ids,
        owner="repair attestation",
    )
    missing_attestations = sorted(repair_ids - set(attestation_map))
    if missing_attestations:
        raise ReviewEvidenceError(
            "missing_repair_attestation",
            f"missing repair attestation for prior finding: {', '.join(missing_attestations)}",
        )

    changed_sections = _changed_section_ids(prior_evidence, current_sections)
    for finding_id, attestation in attestation_map.items():
        if attestation["check_key"] != finding_map[finding_id]["check_key"]:
            raise ReviewEvidenceError(
                "repair_check_key_mismatch",
                f"repair attestation check_key mismatch for prior finding: {finding_id}",
            )
        claimed = cast(list[str], attestation["changed_section_ids"])
        if not claimed or not set(claimed) <= changed_sections:
            raise ReviewEvidenceError(
                "repair_hash_diff_mismatch",
                f"repair attestation changed_section_ids are outside the hash diff: {finding_id}",
                details={"changed_section_ids": sorted(changed_sections)},
            )

    return RepairPreparation(
        repair_attestations=tuple(attestations),
        prior_round_context=build_prior_round_context(
            prior_evidence=prior_evidence,
            findings=findings,
            resolutions=resolutions,
            attestations=attestations,
            current_snapshot=current_snapshot,
        ),
    )


def build_prior_round_context(
    *,
    prior_evidence: PlanReviewEvidence,
    findings: Sequence[Mapping[str, object]],
    resolutions: Sequence[Mapping[str, object]],
    attestations: Sequence[Mapping[str, object]],
    current_snapshot: bytes,
) -> dict[str, object]:
    """Assemble durable causal routing context from consecutive round inputs."""
    round_diff = build_inter_round_diff(prior_evidence.snapshot, current_snapshot)
    return {
        "prior_evidence_id": prior_evidence.evidence_id,
        "prior_findings": [
            {
                "finding_id": finding["finding_id"],
                "check_key": finding["check_key"],
            }
            for finding in findings
        ],
        "prior_finding_resolutions": [dict(resolution) for resolution in resolutions],
        "repair_attestations": [dict(attestation) for attestation in attestations],
        "changed_acceptance_item_ids": list(round_diff.acceptance_item_ids),
        "changed_section_targets": list(round_diff.section_targets),
    }


def repair_preparation_for_round(
    *,
    evidence_rows: Sequence[PlanReviewEvidence],
    round_number: int,
    current_sections: Sequence[SectionHash],
    current_snapshot: bytes,
    prior_finding_resolutions: Sequence[Mapping[str, object]] | None,
    repair_attestations: Sequence[Mapping[str, object]] | None,
) -> RepairPreparation | None:
    """Resolve the canonical prior row, then validate caller repair proof."""
    prior_rows = [
        row
        for row in evidence_rows
        if row.finalized_at is not None
        and row.expired_at is None
        and row.round_number < round_number
    ]
    if not prior_rows:
        if prior_finding_resolutions or repair_attestations:
            raise ReviewEvidenceError(
                "unexpected_repair_context",
                "repair context requires a finalized prior review round",
            )
        return None
    return validate_repair_preparation(
        prior_evidence=prior_rows[-1],
        current_sections=current_sections,
        current_snapshot=current_snapshot,
        prior_finding_resolutions=prior_finding_resolutions,
        repair_attestations=repair_attestations,
    )


def _finding_identity_map(
    findings: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for index, raw in enumerate(findings):
        finding = canonical_json_object(raw)
        owner = f"prior_findings[{index}]"
        finding_id = _required_string(finding, "finding_id", owner)
        _required_string(finding, "check_key", owner)
        _required_string(finding, "severity", owner)
        _required_string(finding, "minimal_repair", owner)
        if finding_id in result:
            raise _invalid(f"duplicate prior finding: {finding_id}")
        result[finding_id] = finding
    return result


def _canonical_resolution(
    raw: Mapping[str, object],
    *,
    owner: str,
) -> dict[str, object]:
    resolution = canonical_json_object(raw)
    unknown = sorted(set(resolution) - _RESOLUTION_FIELDS)
    if unknown:
        raise _invalid(f"{owner} has unknown fields: {', '.join(unknown)}")
    _required_string(resolution, "prior_finding_id", owner)
    if resolution.get("decision") not in {"repair", "carry"}:
        raise _invalid(f"{owner}.decision must be repair or carry")
    return resolution


def _canonical_attestation(
    raw: Mapping[str, object],
    *,
    owner: str,
) -> dict[str, object]:
    attestation = canonical_json_object(raw)
    unknown = sorted(set(attestation) - _ATTESTATION_FIELDS)
    if unknown:
        raise _invalid(f"{owner} has unknown fields: {', '.join(unknown)}")
    missing = sorted(_ATTESTATION_FIELDS - set(attestation))
    if missing:
        raise _invalid(f"{owner} is missing fields: {', '.join(missing)}")
    _required_string(attestation, "prior_finding_id", owner)
    _required_string(attestation, "check_key", owner)
    _required_string(attestation, "accepted_resolution", owner)
    attestation["deviation_from_minimal_repair"] = _canonical_deviation_proof(
        attestation["deviation_from_minimal_repair"],
        owner=f"{owner}.deviation_from_minimal_repair",
    )
    for field in _ATTESTATION_LIST_FIELDS:
        attestation[field] = _string_array(attestation[field], owner=f"{owner}.{field}")
    if not attestation["changed_section_ids"]:
        raise _invalid(f"{owner}.changed_section_ids must be non-empty")
    return attestation


def _canonical_deviation_proof(raw: object, *, owner: str) -> dict[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise _invalid(f"{owner} must be null or an object")
    proof = canonical_json_object(raw)
    unknown = sorted(set(proof) - _DEVIATION_PROOF_FIELD_SET)
    if unknown:
        raise _invalid(f"{owner} has unknown fields: {', '.join(unknown)}")
    missing = sorted(_DEVIATION_PROOF_FIELD_SET - set(proof))
    if missing:
        raise _invalid(f"{owner} is missing fields: {', '.join(missing)}")
    for field in DEVIATION_PROOF_FIELDS:
        _required_string(proof, field, owner)
    return cast(dict[str, str], proof)


def _unique_records(
    records: Sequence[dict[str, object]],
    *,
    known_ids: set[str],
    owner: str,
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for record in records:
        finding_id = cast(str, record["prior_finding_id"])
        if finding_id in result:
            raise _invalid(f"duplicate {owner}: {finding_id}")
        if finding_id not in known_ids:
            raise _invalid(f"{owner} references unknown prior finding: {finding_id}")
        result[finding_id] = record
    return result


def _changed_section_ids(
    prior_evidence: PlanReviewEvidence,
    current_sections: Sequence[SectionHash],
) -> set[str]:
    prior = reviewed_section_hashes(prior_evidence.section_manifest)
    current = reviewed_section_hashes(tuple(current_sections))
    return {
        section_id
        for section_id in set(prior) | set(current)
        if prior.get(section_id) != current.get(section_id)
    }


def _object_array(raw: object, *, owner: str) -> list[dict[str, object]]:
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise _invalid(f"{owner} must be an array of objects")
    return cast(list[dict[str, object]], raw)


def _string_array(raw: object, *, owner: str) -> list[str]:
    if (
        not isinstance(raw, list)
        or any(not isinstance(item, str) or not item.strip() for item in raw)
        or len(raw) != len(set(raw))
    ):
        raise _invalid(f"{owner} must be an array of unique non-empty strings")
    return cast(list[str], raw)


def _required_string(payload: Mapping[str, object], field: str, owner: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{owner}.{field} must be a non-empty string")
    return value


def _positive_round(raw: object) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
        raise _invalid("repair submission round_number must be a positive integer")
    return raw


def _invalid(message: str) -> ReviewEvidenceError:
    return ReviewEvidenceError("invalid_repair_attestation", message)
