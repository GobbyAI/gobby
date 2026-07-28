"""Repair-proof contracts for consecutive plan-review rounds."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import cast

from gobby.plans.consumer_sweep import CandidateSite, CandidateSiteInventory
from gobby.plans.review_evidence_io import build_inter_round_diff, reviewed_section_hashes
from gobby.plans.review_evidence_models import (
    PlanReviewEvidence,
    ReviewEvidenceError,
    SectionHash,
    canonical_json_object,
)
from gobby.plans.review_findings import validate_plan_review_findings
from gobby.plans.review_ledger import inject_dismissed_ledger_context

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
_ATTESTATION_UNIVERSE_FIELDS = frozenset(
    {
        "repair_universe_digest",
        "sweep_query_evidence",
        "repair_bundle_interactions",
    }
)
_ATTESTATION_FIELDS = _ATTESTATION_REQUIRED_FIELDS | _ATTESTATION_UNIVERSE_FIELDS
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


@dataclass(frozen=True)
class RepairInteractionEdge:
    """One server-derived obligation to check two repairs together."""

    edge_id: str
    repair_ids: tuple[str, str]
    shared_sections: tuple[str, ...]
    shared_check_keys: tuple[str, ...]
    shared_contracts: tuple[str, ...]
    shared_targets: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "edge_id": self.edge_id,
            "repair_ids": list(self.repair_ids),
            "shared_sections": list(self.shared_sections),
            "shared_check_keys": list(self.shared_check_keys),
            "shared_contracts": list(self.shared_contracts),
            "shared_targets": list(self.shared_targets),
        }


@dataclass(frozen=True)
class RepairSweepRequirement:
    """Required site, variant, and interaction coverage for one repair."""

    prior_finding_id: str
    check_key: str
    changed_section_ids: tuple[str, ...]
    changed_contracts: tuple[str, ...]
    changed_targets: tuple[str, ...]
    required_consumer_site_ids: tuple[str, ...]
    adjacent_variant_ids: tuple[str, ...]
    interaction_edge_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "prior_finding_id": self.prior_finding_id,
            "check_key": self.check_key,
            "changed_section_ids": list(self.changed_section_ids),
            "changed_contracts": list(self.changed_contracts),
            "changed_targets": list(self.changed_targets),
            "required_consumer_site_ids": list(self.required_consumer_site_ids),
            "adjacent_variant_ids": list(self.adjacent_variant_ids),
            "interaction_edge_ids": list(self.interaction_edge_ids),
        }


@dataclass(frozen=True)
class RepairUniverse:
    """Canonical server-owned repair sweep graph exposed before attestation."""

    digest: str
    candidate_sites: tuple[CandidateSite, ...]
    requirements: tuple[RepairSweepRequirement, ...]
    interaction_edges: tuple[RepairInteractionEdge, ...]

    def graph_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "candidate_sites": [site.to_dict() for site in self.candidate_sites],
            "requirements": [requirement.to_dict() for requirement in self.requirements],
            "interaction_edges": [edge.to_dict() for edge in self.interaction_edges],
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.graph_dict(), "digest": self.digest}


def derive_repair_universe(
    *,
    prior_findings: Sequence[Mapping[str, object]],
    inventory: CandidateSiteInventory,
    repair_finding_ids: Sequence[str] | None = None,
) -> RepairUniverse:
    """Derive one deterministic repair graph from server-owned findings and sites."""
    findings = _finding_identity_map(prior_findings)
    repair_ids = (
        tuple(sorted(findings)) if repair_finding_ids is None else tuple(sorted(repair_finding_ids))
    )
    if len(repair_ids) != len(set(repair_ids)):
        raise _invalid("repair_finding_ids must be unique")
    unknown_repair_ids = sorted(set(repair_ids) - set(findings))
    if unknown_repair_ids:
        raise _invalid(
            f"repair_finding_ids reference unknown prior findings: {', '.join(unknown_repair_ids)}"
        )
    sites = tuple(
        sorted(
            inventory.sites,
            key=lambda site: (site.site_id, site.path, site.source_kind, site.source_ref),
        )
    )
    contracts = tuple(sorted(set(inventory.changed_contracts)))
    targets = tuple(sorted(set(inventory.changed_targets)))
    identities: dict[str, tuple[str, str]] = {}
    for finding_id in repair_ids:
        finding = findings[finding_id]
        identities[finding_id] = (
            _required_string(finding, "section_id", f"prior finding {finding_id}"),
            cast(str, finding["check_key"]),
        )

    interaction_edges: list[RepairInteractionEdge] = []
    for first_id, second_id in combinations(sorted(identities), 2):
        first_section, first_check_key = identities[first_id]
        second_section, second_check_key = identities[second_id]
        shared_sections = (first_section,) if first_section == second_section else ()
        shared_check_keys = (first_check_key,) if first_check_key == second_check_key else ()
        if not (shared_sections or shared_check_keys or contracts or targets):
            continue
        edge_payload: dict[str, object] = {
            "repair_ids": [first_id, second_id],
            "shared_sections": list(shared_sections),
            "shared_check_keys": list(shared_check_keys),
            "shared_contracts": list(contracts),
            "shared_targets": list(targets),
        }
        interaction_edges.append(
            RepairInteractionEdge(
                edge_id=_canonical_digest(edge_payload),
                repair_ids=(first_id, second_id),
                shared_sections=shared_sections,
                shared_check_keys=shared_check_keys,
                shared_contracts=contracts,
                shared_targets=targets,
            )
        )

    requirements_list: list[RepairSweepRequirement] = []
    for finding_id, (section_id, check_key) in sorted(identities.items()):
        section_site_ids = tuple(site.site_id for site in sites if section_id in site.section_ids)
        requirements_list.append(
            RepairSweepRequirement(
                prior_finding_id=finding_id,
                check_key=check_key,
                changed_section_ids=(section_id,),
                changed_contracts=contracts,
                changed_targets=targets,
                required_consumer_site_ids=section_site_ids,
                adjacent_variant_ids=tuple(
                    _canonical_digest({"check_key": check_key, "site_id": site_id})
                    for site_id in section_site_ids
                ),
                interaction_edge_ids=tuple(
                    edge.edge_id for edge in interaction_edges if finding_id in edge.repair_ids
                ),
            ),
        )
    requirements = tuple(requirements_list)
    provisional = RepairUniverse(
        digest="",
        candidate_sites=sites,
        requirements=requirements,
        interaction_edges=tuple(interaction_edges),
    )
    return RepairUniverse(
        digest=_canonical_digest(provisional.graph_dict()),
        candidate_sites=sites,
        requirements=requirements,
        interaction_edges=tuple(interaction_edges),
    )


def validate_repair_universe_attestations(
    *,
    universe: RepairUniverse,
    attestations: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    """Refuse caller proof that does not discharge the server-derived graph."""
    requirement_map = {
        requirement.prior_finding_id: requirement for requirement in universe.requirements
    }
    canonical = tuple(
        _canonical_attestation(record, owner=f"repair_attestations[{index}]")
        for index, record in enumerate(attestations)
    )
    attestation_map = _unique_records(
        canonical,
        known_ids=set(requirement_map),
        owner="repair attestation",
    )
    for finding_id, attestation in attestation_map.items():
        requirement = requirement_map[finding_id]
        missing_fields = sorted(_ATTESTATION_UNIVERSE_FIELDS - set(attestation))
        if missing_fields:
            raise ReviewEvidenceError(
                "missing_repair_universe_proof",
                f"repair attestation is missing universe proof for {finding_id}: "
                f"{', '.join(missing_fields)}",
            )
        if attestation.get("repair_universe_digest") != universe.digest:
            raise ReviewEvidenceError(
                "repair_universe_drift",
                f"repair attestation digest does not match current universe: {finding_id}",
                details={"expected_digest": universe.digest},
            )
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
                "repair_sweep_universe_mismatch",
                f"deferred sites are outside the universe for {finding_id}: {extra_deferred}",
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
                f"repair interaction records do not match universe for {finding_id}: "
                f"missing={missing_edges}, extra={extra_edges}",
            )
    return canonical


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
    repair_universe: RepairUniverse | None = None,
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
    if repair_universe is not None:
        validate_repair_universe_attestations(
            universe=repair_universe,
            attestations=attestations,
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
            repair_universe=repair_universe,
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
    repair_universe: RepairUniverse | None = None,
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
    if repair_universe is not None:
        context["repair_universe"] = repair_universe.to_dict()
        context["repair_universe_digest"] = repair_universe.digest
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
    repair_universe: RepairUniverse | None = None,
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
        repair_universe=repair_universe,
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
    digest = attestation.get("repair_universe_digest")
    if digest is not None and (not isinstance(digest, str) or not _is_sha256(digest)):
        raise _invalid(f"{owner}.repair_universe_digest must be lowercase SHA-256")
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
            "repair_sweep_universe_mismatch",
            f"{label} do not match universe for {finding_id}: missing={missing}, extra={extra}",
        )


def _canonical_digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


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
