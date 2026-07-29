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
from gobby.plans.review_ledger import inject_dismissed_ledger_context
from gobby.plans.review_sweep_scope import SweepScope, canonicalize_sweep_scope
from gobby.utils.hashing import is_sha256

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
_ATTESTATION_REQUIRED_FIELDS = frozenset(
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
_ATTESTATION_SCOPE_FIELDS = frozenset(
    {
        "sweep_scope_digest",
        "sweep_query_evidence",
        "repair_bundle_interactions",
    }
)
_ATTESTATION_FIELDS = _ATTESTATION_REQUIRED_FIELDS | _ATTESTATION_SCOPE_FIELDS
_ATTESTATION_LIST_FIELDS = (
    "changed_section_ids",
    "changed_symbols",
    "consumer_sites_swept",
    "adjacent_variants_swept",
    "validation_evidence",
)
_SUBMISSION_FIELDS = frozenset(
    {
        "round_number",
        "prior_finding_resolutions",
        "repair_attestations",
        "sweep_scope",
        "sweep_scope_digest",
        "consumed_evidence_id",
    }
)


@dataclass(frozen=True)
class RepairSubmission:
    """One typed repair payload bound to the round it prepares."""

    round_number: int
    prior_finding_resolutions: tuple[dict[str, object], ...]
    repair_attestations: tuple[dict[str, object], ...]
    sweep_scope: dict[str, object] | None
    sweep_scope_digest: str | None
    consumed_evidence_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "round_number": self.round_number,
            "prior_finding_resolutions": [
                dict(record) for record in self.prior_finding_resolutions
            ],
            "repair_attestations": [dict(attestation) for attestation in self.repair_attestations],
        }
        if self.sweep_scope is not None:
            payload["sweep_scope"] = dict(self.sweep_scope)
            payload["sweep_scope_digest"] = self.sweep_scope_digest
        return payload


@dataclass(frozen=True)
class RepairPreparation:
    """Validated durable context for a new evidence row."""

    repair_attestations: tuple[dict[str, object], ...]
    prior_round_context: dict[str, object]


def validate_sweep_scope_attestations(
    *,
    scope: SweepScope,
    attestations: Sequence[Mapping[str, object]],
    repair_finding_ids: set[str],
) -> tuple[dict[str, object], ...]:
    """Validate caller proof against the submitted sweep graph."""
    requirement_map = {
        requirement.prior_finding_id: requirement for requirement in scope.requirements
    }
    unknown_requirements = sorted(set(requirement_map) - repair_finding_ids)
    if unknown_requirements:
        raise ReviewEvidenceError(
            "repair_sweep_scope_mismatch",
            f"sweep scope has requirements outside repair resolutions: {unknown_requirements}",
        )
    canonical = tuple(
        _canonical_attestation(record, owner=f"repair_attestations[{index}]")
        for index, record in enumerate(attestations)
    )
    attestation_map = _unique_records(
        canonical,
        known_ids=repair_finding_ids,
        owner="repair attestation",
    )
    for finding_id, attestation in attestation_map.items():
        missing_fields = sorted(_ATTESTATION_SCOPE_FIELDS - set(attestation))
        if missing_fields:
            raise ReviewEvidenceError(
                "missing_sweep_scope_proof",
                f"repair attestation is missing sweep-scope proof for {finding_id}: "
                f"{', '.join(missing_fields)}",
            )
        if attestation.get("sweep_scope_digest") != scope.digest:
            raise ReviewEvidenceError(
                "sweep_scope_digest_mismatch",
                f"repair attestation digest does not match submitted scope: {finding_id}",
                details={"expected_digest": scope.digest},
            )
        requirement = requirement_map.get(finding_id)
        if requirement is None:
            continue
        deferred = {
            cast(str, record["site_id"])
            for record in cast(list[dict[str, object]], attestation["deferred_sites"])
        }
        swept_consumers = set(cast(list[str], attestation["consumer_sites_swept"]))
        required_consumers = set(requirement.required_consumer_site_ids)
        _validate_sweep_set(
            finding_id=finding_id,
            label="consumer sites",
            required=required_consumers,
            swept=swept_consumers,
            deferred=deferred,
        )
        swept_variants = set(cast(list[str], attestation["adjacent_variants_swept"]))
        required_variants = set(requirement.adjacent_variant_ids)
        _validate_sweep_set(
            finding_id=finding_id,
            label="adjacent variants",
            required=required_variants,
            swept=swept_variants,
            deferred=deferred,
        )
        extra_deferred = sorted(deferred - required_consumers - required_variants)
        if extra_deferred:
            raise ReviewEvidenceError(
                "repair_sweep_scope_mismatch",
                f"deferred sites are outside the scope for {finding_id}: {extra_deferred}",
            )
        if not required_consumers and not cast(list[str], attestation["sweep_query_evidence"]):
            raise ReviewEvidenceError(
                "missing_zero_result_query_evidence",
                f"zero-result sweep requires query evidence: {finding_id}",
            )
        interaction_ids = {
            cast(str, record["edge_id"])
            for record in cast(
                list[dict[str, object]],
                attestation["repair_bundle_interactions"],
            )
        }
        required_edges = set(requirement.interaction_edge_ids)
        missing_edges = sorted(required_edges - interaction_ids)
        extra_edges = sorted(interaction_ids - required_edges)
        if missing_edges or extra_edges:
            raise ReviewEvidenceError(
                "repair_bundle_interaction_mismatch",
                f"repair interaction records do not match scope for {finding_id}: "
                f"missing={missing_edges}, extra={extra_edges}",
            )
    return canonical


def build_repair_submission(
    *,
    round_number: int,
    prior_findings: Sequence[Mapping[str, object]],
    recorded_votes: Sequence[Mapping[str, object]],
    edit_diff: Mapping[str, Mapping[str, object]],
    sweep_scope: Mapping[str, object] | None = None,
    sweep_scope_digest: str | None = None,
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

    repair_ids = {
        cast(str, resolution["prior_finding_id"])
        for resolution in resolutions
        if resolution["decision"] == "repair"
    }
    canonical_scope, canonical_digest = _submission_scope(
        sweep_scope=sweep_scope,
        sweep_scope_digest=sweep_scope_digest,
        repair_finding_ids=repair_ids,
    )
    return RepairSubmission(
        round_number=_positive_round(round_number),
        prior_finding_resolutions=tuple(resolutions),
        repair_attestations=tuple(attestations),
        sweep_scope=canonical_scope,
        sweep_scope_digest=canonical_digest,
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
    canonical_resolutions = tuple(
        _canonical_resolution(record, owner=f"prior_finding_resolutions[{index}]")
        for index, record in enumerate(resolutions)
    )
    repair_ids = {
        cast(str, resolution["prior_finding_id"])
        for resolution in canonical_resolutions
        if resolution["decision"] == "repair"
    }
    canonical_attestations = tuple(
        _canonical_attestation(record, owner=f"repair_attestations[{index}]")
        for index, record in enumerate(attestations)
    )
    canonical_scope, canonical_digest = _submission_scope(
        sweep_scope=cast(Mapping[str, object] | None, payload.get("sweep_scope")),
        sweep_scope_digest=cast(str | None, payload.get("sweep_scope_digest")),
        repair_finding_ids=repair_ids,
    )
    return RepairSubmission(
        round_number=_positive_round(payload.get("round_number")),
        prior_finding_resolutions=canonical_resolutions,
        repair_attestations=canonical_attestations,
        sweep_scope=canonical_scope,
        sweep_scope_digest=canonical_digest,
        consumed_evidence_id=consumed,
    )


def _submission_scope(
    *,
    sweep_scope: object,
    sweep_scope_digest: object,
    repair_finding_ids: set[str],
) -> tuple[dict[str, object] | None, str | None]:
    if sweep_scope is None and sweep_scope_digest is None:
        if repair_finding_ids:
            raise _invalid("repair submission requires sweep_scope and sweep_scope_digest")
        return None, None
    if not repair_finding_ids:
        raise _invalid("repair submission sweep_scope requires a repair resolution")
    if not isinstance(sweep_scope, Mapping) or not isinstance(sweep_scope_digest, str):
        raise _invalid("repair submission sweep_scope and sweep_scope_digest must be paired")
    canonical = canonicalize_sweep_scope(sweep_scope, digest=sweep_scope_digest)
    return canonical.to_dict(), canonical.digest


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
            sweep_scope=submission.sweep_scope,
            sweep_scope_digest=submission.sweep_scope_digest,
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
    submitted_sweep_scope: SweepScope | None = None,
    current_sweep_scope: SweepScope | None = None,
    required_scope_delta: Mapping[str, object] | None = None,
    inventory_churn: Mapping[str, object] | None = None,
) -> RepairPreparation:
    """Validate repair proof and preserve submitted/current sweep ownership."""
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
    if repair_ids and submitted_sweep_scope is None:
        raise ReviewEvidenceError(
            "missing_sweep_scope",
            "repair attestations require sweep_scope and sweep_scope_digest",
        )
    if not repair_ids and submitted_sweep_scope is not None:
        raise ReviewEvidenceError(
            "unexpected_sweep_scope",
            "sweep_scope requires at least one repair resolution",
        )
    if submitted_sweep_scope is not None:
        validate_sweep_scope_attestations(
            scope=submitted_sweep_scope,
            attestations=attestations,
            repair_finding_ids=repair_ids,
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
            current_sections=current_sections,
            current_snapshot=current_snapshot,
            submitted_sweep_scope=submitted_sweep_scope,
            current_sweep_scope=current_sweep_scope,
            required_scope_delta=required_scope_delta,
            inventory_churn=inventory_churn,
        ),
    )


def build_prior_round_context(
    *,
    prior_evidence: PlanReviewEvidence,
    findings: Sequence[Mapping[str, object]],
    resolutions: Sequence[Mapping[str, object]],
    attestations: Sequence[Mapping[str, object]],
    current_sections: Sequence[SectionHash],
    current_snapshot: bytes,
    submitted_sweep_scope: SweepScope | None = None,
    current_sweep_scope: SweepScope | None = None,
    required_scope_delta: Mapping[str, object] | None = None,
    inventory_churn: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Assemble durable causal routing context from consecutive round inputs."""
    round_diff = build_inter_round_diff(prior_evidence.snapshot, current_snapshot)
    context: dict[str, object] = {
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
    if submitted_sweep_scope is not None:
        context["submitted_sweep_scope"] = submitted_sweep_scope.to_dict()
        context["submitted_sweep_scope_digest"] = submitted_sweep_scope.digest
    if current_sweep_scope is not None:
        context["current_sweep_scope"] = current_sweep_scope.to_dict()
    if required_scope_delta is not None:
        context["required_scope_delta"] = dict(required_scope_delta)
    if inventory_churn is not None:
        context["inventory_churn"] = dict(inventory_churn)
    return inject_dismissed_ledger_context(
        prior_round_context=context,
        prior_ledger=prior_evidence.quality_ledger or (),
        current_section_hashes={
            section.section_id: section.section_hash for section in current_sections
        },
    )


def repair_preparation_for_round(
    *,
    evidence_rows: Sequence[PlanReviewEvidence],
    round_number: int,
    current_sections: Sequence[SectionHash],
    current_snapshot: bytes,
    prior_finding_resolutions: Sequence[Mapping[str, object]] | None,
    repair_attestations: Sequence[Mapping[str, object]] | None,
    submitted_sweep_scope: SweepScope | None = None,
    current_sweep_scope: SweepScope | None = None,
    required_scope_delta: Mapping[str, object] | None = None,
    inventory_churn: Mapping[str, object] | None = None,
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
        if prior_finding_resolutions or repair_attestations or submitted_sweep_scope is not None:
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
        submitted_sweep_scope=submitted_sweep_scope,
        current_sweep_scope=current_sweep_scope,
        required_scope_delta=required_scope_delta,
        inventory_churn=inventory_churn,
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
    missing = sorted(_ATTESTATION_REQUIRED_FIELDS - set(attestation))
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
    attestation["deferred_sites"] = _canonical_deferred_sites(
        attestation["deferred_sites"],
        owner=f"{owner}.deferred_sites",
    )
    digest = attestation.get("sweep_scope_digest")
    if digest is not None and not is_sha256(digest):
        raise _invalid(f"{owner}.sweep_scope_digest must be lowercase SHA-256")
    if "sweep_query_evidence" in attestation:
        attestation["sweep_query_evidence"] = _string_array(
            attestation["sweep_query_evidence"],
            owner=f"{owner}.sweep_query_evidence",
        )
    if "repair_bundle_interactions" in attestation:
        attestation["repair_bundle_interactions"] = _canonical_repair_interactions(
            attestation["repair_bundle_interactions"],
            owner=f"{owner}.repair_bundle_interactions",
        )
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


def _canonical_deferred_sites(raw: object, *, owner: str) -> list[dict[str, object]]:
    records = _object_array(raw, owner=owner)
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, record in enumerate(records):
        if set(record) != {"site_id", "reason"}:
            raise _invalid(f"{owner}[{index}] must contain exactly site_id and reason")
        site_id = _required_string(record, "site_id", f"{owner}[{index}]")
        _required_string(record, "reason", f"{owner}[{index}]")
        if site_id in seen:
            raise _invalid(f"{owner} contains duplicate site_id: {site_id}")
        seen.add(site_id)
        result.append(record)
    return result


def _canonical_repair_interactions(
    raw: object,
    *,
    owner: str,
) -> list[dict[str, object]]:
    records = _object_array(raw, owner=owner)
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    required = {"edge_id", "disposition", "validation_evidence"}
    for index, record in enumerate(records):
        record_owner = f"{owner}[{index}]"
        if set(record) != required:
            raise _invalid(
                f"{record_owner} must contain exactly edge_id, disposition, and validation_evidence"
            )
        edge_id = _required_string(record, "edge_id", record_owner)
        _required_string(record, "disposition", record_owner)
        record["validation_evidence"] = _string_array(
            record["validation_evidence"],
            owner=f"{record_owner}.validation_evidence",
        )
        if not record["validation_evidence"]:
            raise _invalid(f"{record_owner}.validation_evidence must be non-empty")
        if edge_id in seen:
            raise _invalid(f"{owner} contains duplicate edge_id: {edge_id}")
        seen.add(edge_id)
        result.append(record)
    return result


def _validate_sweep_set(
    *,
    finding_id: str,
    label: str,
    required: set[str],
    swept: set[str],
    deferred: set[str],
) -> None:
    covered = swept | deferred
    missing = sorted(required - covered)
    extra = sorted(swept - required)
    if missing or extra:
        raise ReviewEvidenceError(
            "repair_sweep_scope_mismatch",
            f"{label} do not match scope for {finding_id}: missing={missing}, extra={extra}",
        )


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
