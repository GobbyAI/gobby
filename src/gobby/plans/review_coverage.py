"""Trusted derivation and coverage checks for parallel plan review."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from gobby.plans.digests import canonical_json_sha256
from gobby.plans.parser import Kind, PlanDocument
from gobby.plans.review_citations import validate_source_citation
from gobby.plans.review_evidence_io import build_section_manifest
from gobby.plans.review_evidence_models import ReviewEvidenceError, canonical_json_object
from gobby.plans.review_ledger import (
    dismissed_ledger_entries_from_context,
    validate_quality_ledger,
)
from gobby.plans.review_sweeps import validate_record_bundle, validate_sweep_records
from gobby.plans.semantic_lint import collect_target_inventory
from gobby.utils.hashing import is_sha256

REVIEW_LANES = (
    "requirements_traceability",
    "repository_blast_radius",
    "runtime_invariants",
)
_COMPLEX_DELIVERABLES = 8
_COMPLEX_ACCEPTANCE_ITEMS = 24
_COMPLEX_TARGET_FILES = 12
_COMPLEX_CHANGED_SECTIONS = 4


def review_complexity(
    document: PlanDocument,
    *,
    changed_section_count: int,
) -> dict[str, object]:
    """Return deterministic fanout routing for one immutable plan snapshot."""
    deliverables = [section for section in document.sections if section.kind is Kind.deliverable]
    acceptance_count = sum(len(section.acceptance_items) for section in deliverables)
    targets = {
        target for section in deliverables for target in collect_target_inventory(document, section)
    }
    counts = {
        "deliverables": len(deliverables),
        "acceptance_items": acceptance_count,
        "target_files": len(targets),
        "changed_sections": changed_section_count,
    }
    complex_review = (
        counts["deliverables"] >= _COMPLEX_DELIVERABLES
        or counts["acceptance_items"] >= _COMPLEX_ACCEPTANCE_ITEMS
        or counts["target_files"] >= _COMPLEX_TARGET_FILES
        or counts["changed_sections"] >= _COMPLEX_CHANGED_SECTIONS
    )
    return {
        "mode": "parallel" if complex_review else "sequential",
        "counts": counts,
        "thresholds": {
            "deliverables": _COMPLEX_DELIVERABLES,
            "acceptance_items": _COMPLEX_ACCEPTANCE_ITEMS,
            "target_files": _COMPLEX_TARGET_FILES,
            "changed_sections": _COMPLEX_CHANGED_SECTIONS,
        },
        "lanes": list(REVIEW_LANES),
        "max_workers": 3 if complex_review else 0,
    }


def validate_approval_condition(
    *,
    findings: Sequence[Mapping[str, object]],
    quality_ledger: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Require zero blocking findings while preserving non-gating ledger entries."""
    blocking_ids = [
        str(finding.get("finding_id", f"finding-{index}"))
        for index, finding in enumerate(findings)
        if finding.get("severity") == "blocking"
    ]
    if blocking_ids:
        raise ReviewEvidenceError(
            "blocking_findings_remaining",
            "approval requires zero blocking findings",
            details={"finding_ids": blocking_ids},
        )
    return validate_quality_ledger(quality_ledger)


def validate_review_coverage(
    *,
    evidence_id: str,
    project_root: Path,
    document: PlanDocument,
    plan_hash: str,
    lane_results: Sequence[object],
    candidate_dispositions: Mapping[str, object],
    shadow_manifest_status: Mapping[str, object],
    expected_shadow_manifest_status: Mapping[str, object],
    prior_round_context: Mapping[str, object] | None,
) -> dict[str, object]:
    """Validate exhaustive lane output and return a canonical attestation."""
    lanes = _validate_lanes(
        document,
        lane_results,
    )
    sweep_validation = validate_sweep_records(
        lanes=lanes,
        raw=candidate_dispositions,
        prior_round_context=prior_round_context,
    )
    canonical_shadow = canonical_json_object(shadow_manifest_status)
    expected_shadow = canonical_json_object(expected_shadow_manifest_status)
    if canonical_shadow != expected_shadow:
        raise ReviewEvidenceError(
            "shadow_manifest_mismatch",
            "shadow_manifest_status differs from canonical derivation",
        )
    citations: list[dict[str, object]] = []
    for lane in lanes:
        citations.extend(cast(list[dict[str, object]], lane["source_citations"]))
        candidates = cast(list[dict[str, object]], lane["candidate_issues"])
        for candidate in candidates:
            citations.extend(cast(list[dict[str, object]], candidate["source_citations"]))
    source_hashes = _rehash_sources(project_root, citations)
    current_section_hashes = {
        section.section_id: section.section_hash
        for section in build_section_manifest(document.source_path.read_bytes())
    }
    _reject_unchanged_dismissal_reopens(
        dispositions=sweep_validation.dispositions,
        prior_round_context=prior_round_context,
        current_section_hashes=current_section_hashes,
    )
    source_digest = _source_digest(plan_hash, source_hashes)
    shadow_summary: dict[str, object] = {"status": expected_shadow["status"]}
    if expected_shadow["status"] == "valid":
        shadow_summary["manifest_digest"] = expected_shadow["manifest_digest"]
        shadow_summary["entry_count"] = expected_shadow["entry_count"]
    else:
        shadow_summary["diagnostics"] = expected_shadow["diagnostics"]
    attestation: dict[str, object] = {
        "version": 1,
        "evidence_id": evidence_id,
        "lanes": [
            {
                "lane_id": lane["lane_id"],
                "status": "completed",
                "candidate_count": len(cast(list[dict[str, object]], lane["candidate_issues"])),
            }
            for lane in lanes
        ],
        "source_digest": source_digest,
        "disposition_counts": sweep_validation.disposition_counts,
        "cross_lane_interaction_complete": sweep_validation.cross_lane_interaction_complete,
        "adjacent_variant_complete": sweep_validation.adjacent_variant_complete,
        "record_bundle": sweep_validation.record_bundle,
        "shadow_manifest_status": shadow_summary,
    }
    attestation["attestation_digest"] = canonical_json_sha256(attestation)
    return attestation


def validate_coverage_attestation(
    raw: object,
    *,
    verdict: str,
) -> dict[str, object]:
    """Validate the canonical summary embedded in a round result."""
    if not isinstance(raw, Mapping):
        raise ReviewEvidenceError(
            "invalid_coverage_attestation",
            "round_result.coverage_attestation must be an object",
        )
    attestation: dict[str, object] = canonical_json_object(raw)
    expected_keys = {
        "version",
        "evidence_id",
        "lanes",
        "source_digest",
        "disposition_counts",
        "cross_lane_interaction_complete",
        "adjacent_variant_complete",
        "record_bundle",
        "shadow_manifest_status",
        "attestation_digest",
    }
    if "record_bundle" not in attestation:
        raise ReviewEvidenceError(
            "invalid_coverage_attestation",
            "coverage attestation requires record_bundle",
        )
    if set(attestation) != expected_keys or attestation.get("version") != 1:
        raise ReviewEvidenceError(
            "invalid_coverage_attestation",
            "coverage attestation does not match the canonical version-1 schema",
        )
    _required_string(
        attestation,
        "evidence_id",
        "coverage attestation",
        error_code="invalid_coverage_attestation",
    )
    lanes = attestation.get("lanes")
    if not isinstance(lanes, list) or len(lanes) != len(REVIEW_LANES):
        raise ReviewEvidenceError(
            "invalid_coverage_attestation",
            "coverage attestation must contain exactly three lanes",
        )
    lane_ids: list[str] = []
    for lane in lanes:
        if not isinstance(lane, dict):
            raise ReviewEvidenceError(
                "invalid_coverage_attestation",
                "coverage attestation lane entries must be objects",
            )
        if set(lane) != {"lane_id", "status", "candidate_count"}:
            raise ReviewEvidenceError(
                "invalid_coverage_attestation",
                "coverage attestation lane entry has non-canonical fields",
            )
        lane_id = lane.get("lane_id")
        if lane_id not in REVIEW_LANES or lane.get("status") != "completed":
            raise ReviewEvidenceError(
                "invalid_coverage_attestation",
                "coverage attestation lanes must be the three completed canonical lanes",
            )
        lane_ids.append(str(lane_id))
        candidate_count = lane.get("candidate_count")
        if (
            not isinstance(candidate_count, int)
            or isinstance(candidate_count, bool)
            or candidate_count < 0
        ):
            raise ReviewEvidenceError(
                "invalid_coverage_attestation",
                "coverage attestation candidate_count must be a non-negative integer",
            )
    if tuple(lane_ids) != REVIEW_LANES:
        raise ReviewEvidenceError(
            "invalid_coverage_attestation",
            "coverage attestation lanes are missing, duplicated, or out of order",
        )
    if not is_sha256(attestation.get("source_digest")):
        raise ReviewEvidenceError(
            "invalid_coverage_attestation",
            "coverage attestation source_digest must be SHA-256",
        )
    if attestation.get("cross_lane_interaction_complete") is not True:
        raise ReviewEvidenceError(
            "invalid_coverage_attestation",
            "cross-lane interaction pass is incomplete",
        )
    if attestation.get("adjacent_variant_complete") is not True:
        raise ReviewEvidenceError(
            "invalid_coverage_attestation",
            "adjacent-variant sweep is incomplete",
        )
    shadow = attestation.get("shadow_manifest_status")
    if not isinstance(shadow, dict) or shadow.get("status") not in {"valid", "invalid"}:
        raise ReviewEvidenceError(
            "invalid_coverage_attestation",
            "coverage attestation has invalid shadow-manifest status",
        )
    if verdict == "approved" and shadow.get("status") != "valid":
        raise ReviewEvidenceError(
            "invalid_coverage_attestation",
            "approval requires a valid shadow manifest",
        )
    if shadow.get("status") == "valid":
        if set(shadow) != {"status", "manifest_digest", "entry_count"}:
            raise ReviewEvidenceError(
                "invalid_coverage_attestation",
                "valid shadow manifest status has non-canonical fields",
            )
        if not is_sha256(shadow.get("manifest_digest")):
            raise ReviewEvidenceError(
                "invalid_coverage_attestation",
                "valid shadow manifest status requires a SHA-256 manifest_digest",
            )
        entry_count = shadow.get("entry_count")
        if not isinstance(entry_count, int) or isinstance(entry_count, bool) or entry_count < 1:
            raise ReviewEvidenceError(
                "invalid_coverage_attestation",
                "valid shadow manifest status requires a positive entry_count",
            )
    else:
        if set(shadow) != {"status", "diagnostics"}:
            raise ReviewEvidenceError(
                "invalid_coverage_attestation",
                "invalid shadow manifest status has non-canonical fields",
            )
        diagnostics = shadow.get("diagnostics")
        if not isinstance(diagnostics, list) or not diagnostics:
            raise ReviewEvidenceError(
                "invalid_coverage_attestation",
                "invalid shadow manifest status requires diagnostics",
            )
    counts = attestation.get("disposition_counts")
    if not isinstance(counts, dict):
        raise ReviewEvidenceError(
            "invalid_coverage_attestation",
            "coverage attestation disposition_counts must be an object",
        )
    if set(counts) != {"total", "emitted_findings", "dismissed"}:
        raise ReviewEvidenceError(
            "invalid_coverage_attestation",
            "coverage attestation disposition_counts has non-canonical fields",
        )
    values = [counts[key] for key in ("total", "emitted_findings", "dismissed")]
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values):
        raise ReviewEvidenceError(
            "invalid_coverage_attestation",
            "coverage attestation disposition counts must be non-negative integers",
        )
    if counts["total"] != counts["emitted_findings"] + counts["dismissed"]:
        raise ReviewEvidenceError(
            "invalid_coverage_attestation",
            "coverage attestation disposition counts are inconsistent",
        )
    if counts["total"] != sum(int(lane["candidate_count"]) for lane in lanes):
        raise ReviewEvidenceError(
            "invalid_coverage_attestation",
            "coverage attestation disposition_counts differ from lane candidate counts",
        )
    record_bundle, bundle_counts = validate_record_bundle(attestation.get("record_bundle"))
    if counts != bundle_counts:
        raise ReviewEvidenceError(
            "invalid_coverage_attestation",
            "coverage attestation disposition_counts disagree with record_bundle",
        )
    attestation["record_bundle"] = record_bundle
    digest = attestation.pop("attestation_digest", None)
    expected_digest = canonical_json_sha256(attestation)
    if digest != expected_digest:
        raise ReviewEvidenceError(
            "invalid_coverage_attestation",
            "coverage attestation digest mismatch",
        )
    attestation["attestation_digest"] = digest
    return attestation


def _validate_lanes(
    document: PlanDocument,
    lane_results: Sequence[object],
) -> list[dict[str, object]]:
    if len(lane_results) != len(REVIEW_LANES):
        raise ReviewEvidenceError(
            "invalid_lane_results",
            "lane_results must contain exactly three lanes",
        )
    expected_sections = {
        section.section_id for section in document.sections if section.kind is Kind.deliverable
    }
    by_id: dict[str, dict[str, object]] = {}
    candidate_ids: set[str] = set()
    for raw_lane in lane_results:
        if not isinstance(raw_lane, Mapping):
            raise ReviewEvidenceError("invalid_lane_results", "lane result must be an object")
        lane = canonical_json_object(raw_lane)
        lane_id = _required_string(
            lane,
            "lane_id",
            "lane result",
            error_code="invalid_lane_results",
        )
        if lane_id not in REVIEW_LANES or lane_id in by_id:
            raise ReviewEvidenceError(
                "invalid_lane_results",
                f"unknown or duplicate review lane: {lane_id}",
            )
        if lane.get("status") != "completed":
            raise ReviewEvidenceError(
                "invalid_lane_results",
                f"review lane {lane_id} is incomplete",
            )
        checked = _string_list(lane.get("section_ids_checked"), "section_ids_checked")
        if set(checked) != expected_sections or len(checked) != len(expected_sections):
            raise ReviewEvidenceError(
                "invalid_section_ids",
                f"review lane {lane_id} did not cover every deliverable section",
            )
        citations = _citation_list(
            lane.get("source_citations"),
        )
        candidates_raw = lane.get("candidate_issues")
        if not isinstance(candidates_raw, list):
            raise ReviewEvidenceError(
                "invalid_lane_results",
                f"review lane {lane_id} candidate_issues must be an array",
            )
        candidates: list[dict[str, object]] = []
        for raw_candidate in candidates_raw:
            candidate = _validate_candidate(
                raw_candidate,
                expected_sections=expected_sections,
            )
            candidate_id = str(candidate["candidate_id"])
            if candidate_id in candidate_ids:
                raise ReviewEvidenceError(
                    "duplicate_candidate",
                    f"candidate_id is duplicated: {candidate_id}",
                )
            candidate_ids.add(candidate_id)
            candidates.append(candidate)
        lane["source_citations"] = citations
        lane["candidate_issues"] = candidates
        by_id[lane_id] = lane
    return [by_id[lane_id] for lane_id in REVIEW_LANES]


def _validate_candidate(
    raw: object,
    *,
    expected_sections: set[str],
) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise ReviewEvidenceError("invalid_candidate", "candidate issue must be an object")
    candidate: dict[str, object] = canonical_json_object(raw)
    allowed_fields = {
        "candidate_id",
        "violated_invariant",
        "suggested_fix",
        "section_ids",
        "confidence",
        "source_citations",
        "adjacent_sites_checked",
    }
    unknown = sorted(set(candidate) - allowed_fields)
    if unknown:
        raise ReviewEvidenceError(
            "invalid_candidate",
            f"candidate issue has unknown fields: {', '.join(unknown)}",
        )
    _required_string(
        candidate,
        "candidate_id",
        "candidate issue",
        error_code="invalid_candidate",
    )
    _required_string(
        candidate,
        "violated_invariant",
        "candidate issue",
        error_code="invalid_candidate",
    )
    _required_string(
        candidate,
        "suggested_fix",
        "candidate issue",
        error_code="invalid_candidate",
    )
    section_ids = _string_list(candidate.get("section_ids"), "section_ids")
    if (
        not section_ids
        or len(section_ids) != len(set(section_ids))
        or not set(section_ids) <= expected_sections
    ):
        raise ReviewEvidenceError(
            "invalid_section_ids",
            "candidate issue references an unknown or empty section set",
        )
    confidence = candidate.get("confidence")
    if not isinstance(confidence, int | float) or isinstance(confidence, bool):
        raise ReviewEvidenceError("invalid_candidate", "candidate confidence must be numeric")
    if not 0 <= float(confidence) <= 1:
        raise ReviewEvidenceError(
            "invalid_candidate",
            "candidate confidence must be between 0 and 1",
        )
    candidate["source_citations"] = _citation_list(
        candidate.get("source_citations"),
    )
    candidate["adjacent_sites_checked"] = _string_list(
        candidate.get("adjacent_sites_checked"),
        "adjacent_sites_checked",
    )
    candidate["section_ids"] = section_ids
    return candidate


def _reject_unchanged_dismissal_reopens(
    *,
    dispositions: Sequence[Mapping[str, object]],
    prior_round_context: Mapping[str, object] | None,
    current_section_hashes: Mapping[str, str],
) -> None:
    prior_dismissals = dismissed_ledger_entries_from_context(prior_round_context)
    for disposition in dispositions:
        if disposition.get("disposition") != "emitted_finding":
            continue
        source_sections = cast(list[str], disposition["source_section_ids"])
        for dismissal in prior_dismissals:
            if (
                dismissal["check_key"] != disposition["check_key"]
                or dismissal["source_section_ids"] != source_sections
                or dismissal["source_hash"] != disposition["source_hash"]
            ):
                continue
            stored_hashes = cast(dict[str, str], dismissal["section_hashes_at_entry"])
            section_hash_changed = any(
                current_section_hashes.get(section_id) != section_hash
                for section_id, section_hash in stored_hashes.items()
            )
            if dismissal["reopenable"] != section_hash_changed:
                raise ReviewEvidenceError(
                    "invalid_dismissal_context",
                    "dismissal reopenable marker differs from the bound section hashes",
                )
            if not section_hash_changed:
                raise ReviewEvidenceError(
                    "unchanged_dismissal_reopened",
                    "candidate reopens a prior dismissal without a source or section hash change",
                    details={"ledger_entry_id": dismissal["ledger_entry_id"]},
                )


def _citation_list(
    raw: object,
) -> list[dict[str, object]]:
    if not isinstance(raw, list) or not raw:
        raise ReviewEvidenceError(
            "invalid_source_citation",
            "source_citations must be a non-empty array",
        )
    citations: list[dict[str, object]] = []
    for item in raw:
        citations.append(
            validate_source_citation(
                item,
            )
        )
    return citations


def _rehash_sources(
    project_root: Path,
    citations: Sequence[Mapping[str, object]],
) -> dict[str, str]:
    try:
        root = project_root.resolve(strict=True)
    except FileNotFoundError:
        raise ReviewEvidenceError(
            "source_drift",
            f"project root is missing: {project_root}",
            retryable=True,
            details={"paths": [str(project_root)]},
        ) from None
    except OSError as error:
        raise ReviewEvidenceError(
            "source_io_error",
            f"project root could not be resolved: {project_root}",
            details={"paths": [str(project_root)], "io_error": str(error)},
        ) from error
    claimed_hashes: dict[str, str] = {}
    resolved_paths: dict[str, Path] = {}
    for citation in citations:
        relative = str(citation["path"])
        path = Path(relative)
        if path.is_absolute():
            raise ReviewEvidenceError(
                "invalid_source_path",
                f"source citation must be repository-relative: {relative}",
            )
        try:
            resolved = (root / path).resolve(strict=True)
            resolved.relative_to(root)
        except FileNotFoundError:
            raise ReviewEvidenceError(
                "source_drift",
                f"cited source is missing: {relative}",
                retryable=True,
                details={"paths": [relative]},
            ) from None
        except OSError as error:
            raise ReviewEvidenceError(
                "source_io_error",
                f"cited source could not be resolved: {relative}",
                details={"paths": [relative], "io_error": str(error)},
            ) from error
        except ValueError:
            raise ReviewEvidenceError(
                "invalid_source_path",
                f"source citation escapes the repository: {relative}",
            ) from None
        if not resolved.is_file():
            raise ReviewEvidenceError(
                "invalid_source_path",
                f"source citation is not a regular file: {relative}",
            )
        claimed = str(citation["sha256"])
        prior = claimed_hashes.get(relative)
        if prior is not None and prior != claimed:
            raise ReviewEvidenceError(
                "source_drift",
                f"conflicting hashes were cited for {relative}",
                retryable=True,
                details={"paths": [relative]},
            )
        claimed_hashes[relative] = claimed
        resolved_paths[relative] = resolved
    changed = [
        relative
        for relative, resolved in resolved_paths.items()
        if hashlib.sha256(resolved.read_bytes()).hexdigest() != claimed_hashes[relative]
    ]
    if changed:
        raise ReviewEvidenceError(
            "source_drift",
            "cited source changed during review: " + ", ".join(sorted(changed)),
            retryable=True,
            details={"paths": sorted(changed)},
        )
    return dict(sorted(claimed_hashes.items()))


def _source_digest(plan_hash: str, source_hashes: Mapping[str, str]) -> str:
    payload = {"plan_hash": plan_hash, "sources": source_hashes}
    return canonical_json_sha256(payload)


def _required_string(
    payload: Mapping[str, object],
    key: str,
    owner: str,
    *,
    error_code: str,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ReviewEvidenceError(
            error_code,
            f"{owner}.{key} must be a non-empty string",
        )
    return value


def _string_list(raw: object, owner: str) -> list[str]:
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item for item in raw):
        raise ReviewEvidenceError(
            f"invalid_{owner}",
            f"{owner} must be an array of non-empty strings",
        )
    if len(set(raw)) != len(raw):
        raise ReviewEvidenceError(f"invalid_{owner}", f"{owner} contains duplicates")
    return list(raw)


__all__ = [
    "REVIEW_LANES",
    "review_complexity",
    "validate_coverage_attestation",
    "validate_review_coverage",
]
