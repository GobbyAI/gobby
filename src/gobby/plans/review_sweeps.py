"""Canonical structured sweep records for plan-review coverage."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import cast

from gobby.plans.review_evidence_models import (
    ReviewEvidenceError,
    canonical_json_object,
)
from gobby.plans.review_findings import CHECK_KEY_RE

_BUNDLE_KEYS = frozenset(
    {
        "cross_lane_interactions",
        "adjacent_variant_sweeps",
        "causal_repair_sweeps",
        "candidate_dispositions",
    }
)
_DISPOSITION_FIELDS = frozenset(
    {
        "candidate_id",
        "check_key",
        "source_section_ids",
        "source_hash",
        "rationale",
        "disposition",
        "finding_id",
    }
)


@dataclass(frozen=True)
class SweepValidation:
    """Canonical records and server-derived completion state."""

    record_bundle: dict[str, object]
    disposition_counts: dict[str, int]
    cross_lane_interaction_complete: bool
    adjacent_variant_complete: bool
    dispositions: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class _Candidate:
    candidate_id: str
    lane_id: str
    section_ids: tuple[str, ...]
    citation_hashes: frozenset[str]


@dataclass(frozen=True)
class _RepairRequirement:
    prior_finding_id: str
    changed_section_ids: tuple[str, ...]
    changed_contracts: tuple[str, ...]
    site_ids: tuple[str, ...]


def validate_sweep_records(
    *,
    lanes: Sequence[Mapping[str, object]],
    raw: Mapping[str, object],
    prior_round_context: Mapping[str, object] | None,
) -> SweepValidation:
    """Validate caller records against the server-owned sweep universe."""
    payload = _bundle_payload(raw)
    candidates = _candidate_map(lanes)
    dispositions = _validate_dispositions(
        payload["candidate_dispositions"],
        candidates=candidates,
    )
    disposition_map = {cast(str, record["candidate_id"]): record for record in dispositions}
    required_cross: set[tuple[str, str]] = set()
    for first, second in combinations(candidates.values(), 2):
        if first.lane_id == second.lane_id:
            continue
        first_id, second_id = sorted((first.candidate_id, second.candidate_id))
        required_cross.add((first_id, second_id))
    cross_records, recorded_cross, referenced = _validate_cross_lane_records(
        payload["cross_lane_interactions"],
        candidates=candidates,
        required=required_cross,
    )
    required_adjacent = {
        (candidate_id, cast(str, disposition["check_key"]))
        for candidate_id, disposition in disposition_map.items()
    }
    adjacent_records, recorded_adjacent, adjacent_references = _validate_adjacent_records(
        payload["adjacent_variant_sweeps"],
        candidates=candidates,
        required=required_adjacent,
    )
    referenced.update(adjacent_references)
    unreferenced = sorted(set(candidates) - referenced)
    if unreferenced:
        raise ReviewEvidenceError(
            "unreferenced_candidate",
            "candidate is absent from every sweep record: " + ", ".join(unreferenced),
        )
    requirements = _repair_requirements(prior_round_context)
    causal_records = _validate_causal_records(
        payload["causal_repair_sweeps"],
        requirements=requirements,
    )
    bundle: dict[str, object] = {
        "cross_lane_interactions": cross_records,
        "adjacent_variant_sweeps": adjacent_records,
        "causal_repair_sweeps": causal_records,
        "candidate_dispositions": dispositions,
    }
    counts = _disposition_counts(dispositions)
    return SweepValidation(
        record_bundle=bundle,
        disposition_counts=counts,
        cross_lane_interaction_complete=required_cross == recorded_cross,
        adjacent_variant_complete=required_adjacent == recorded_adjacent,
        dispositions=tuple(dispositions),
    )


def validate_record_bundle(raw: object) -> tuple[dict[str, object], dict[str, int]]:
    """Canonicalize a bundle carried inside a durable round result."""
    if not isinstance(raw, Mapping):
        raise _invalid("coverage attestation record_bundle must be an object")
    payload = _bundle_payload(raw)
    dispositions = _validate_dispositions(
        payload["candidate_dispositions"],
        candidates=None,
    )
    candidate_ids = {cast(str, record["candidate_id"]) for record in dispositions}
    cross = _canonical_cross_records(
        payload["cross_lane_interactions"],
        candidate_ids=candidate_ids,
    )
    adjacent = _canonical_adjacent_records(
        payload["adjacent_variant_sweeps"],
        candidate_ids=candidate_ids,
    )
    causal = _canonical_causal_records(payload["causal_repair_sweeps"])
    referenced = {
        candidate_id
        for record in cross
        for candidate_id in cast(list[str], record["candidate_ids"])
    }
    referenced.update(
        candidate_id
        for record in adjacent
        for candidate_id in (
            [cast(str, record["seed_candidate_id"])]
            + cast(list[str], record["resulting_candidate_ids"])
        )
    )
    unreferenced = sorted(candidate_ids - referenced)
    if unreferenced:
        raise ReviewEvidenceError(
            "unreferenced_candidate",
            "candidate is absent from every sweep record: " + ", ".join(unreferenced),
        )
    bundle: dict[str, object] = {
        "cross_lane_interactions": cross,
        "adjacent_variant_sweeps": adjacent,
        "causal_repair_sweeps": causal,
        "candidate_dispositions": dispositions,
    }
    return bundle, _disposition_counts(dispositions)


def _bundle_payload(raw: Mapping[str, object]) -> dict[str, object]:
    payload = canonical_json_object(raw)
    if set(payload) != _BUNDLE_KEYS:
        raise _invalid(
            "structured sweep records must contain exactly " + ", ".join(sorted(_BUNDLE_KEYS))
        )
    return payload


def _candidate_map(
    lanes: Sequence[Mapping[str, object]],
) -> dict[str, _Candidate]:
    result: dict[str, _Candidate] = {}
    for lane in lanes:
        lane_id = cast(str, lane["lane_id"])
        for candidate in cast(list[dict[str, object]], lane["candidate_issues"]):
            candidate_id = cast(str, candidate["candidate_id"])
            citations = cast(list[dict[str, object]], candidate["source_citations"])
            result[candidate_id] = _Candidate(
                candidate_id=candidate_id,
                lane_id=lane_id,
                section_ids=tuple(cast(list[str], candidate["section_ids"])),
                citation_hashes=frozenset(
                    str(citation.get("sha256") or citation["content_sha256"])
                    for citation in citations
                ),
            )
    return result


def _validate_dispositions(
    raw: object,
    *,
    candidates: Mapping[str, _Candidate] | None,
) -> list[dict[str, object]]:
    records = _object_array(raw, owner="candidate_dispositions")
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    finding_ids: set[str] = set()
    for index, record in enumerate(records):
        owner = f"candidate_dispositions[{index}]"
        unknown = sorted(set(record) - _DISPOSITION_FIELDS)
        if unknown:
            raise _invalid(f"{owner} has unknown fields: {', '.join(unknown)}")
        candidate_id = _required_string(record, "candidate_id", owner)
        if candidate_id in seen:
            raise _invalid(f"unknown or duplicate candidate disposition: {candidate_id}")
        if candidates is not None and candidate_id not in candidates:
            raise _invalid(f"unknown or duplicate candidate disposition: {candidate_id}")
        seen.add(candidate_id)
        check_key = _required_string(record, "check_key", owner)
        if CHECK_KEY_RE.fullmatch(check_key) is None:
            raise _invalid(f"{owner}.check_key is invalid")
        source_ids = _string_list(
            record.get("source_section_ids"),
            owner=f"{owner}.source_section_ids",
            allow_empty=False,
        )
        source_hash = _required_sha256(record, "source_hash", owner)
        if candidates is not None:
            candidate = candidates[candidate_id]
            if source_ids != sorted(candidate.section_ids):
                raise _invalid("candidate disposition source_section_ids differ from the candidate")
            if source_hash not in candidate.citation_hashes:
                raise _invalid(
                    "candidate disposition source_hash is absent from candidate citations"
                )
        _required_string(record, "rationale", owner)
        disposition = record.get("disposition")
        if disposition == "emitted_finding":
            finding_id = _required_string(record, "finding_id", owner)
            if finding_id in finding_ids:
                raise ReviewEvidenceError(
                    "duplicate_finding",
                    f"finding_id is duplicated: {finding_id}",
                )
            finding_ids.add(finding_id)
        elif disposition == "dismissed":
            if "finding_id" in record:
                raise _invalid(f"{owner}.finding_id is invalid for a dismissed candidate")
        else:
            raise _invalid(f"{owner}.disposition must be emitted_finding or dismissed")
        record["source_section_ids"] = source_ids
        result.append(record)
    if candidates is not None and seen != set(candidates):
        missing = sorted(set(candidates) - seen)
        raise ReviewEvidenceError(
            "undisposed_candidates",
            "every candidate requires a disposition: " + ", ".join(missing),
        )
    return sorted(result, key=lambda record: cast(str, record["candidate_id"]))


def _validate_cross_lane_records(
    raw: object,
    *,
    candidates: Mapping[str, _Candidate],
    required: set[tuple[str, str]],
) -> tuple[list[dict[str, object]], set[tuple[str, str]], set[str]]:
    records = _canonical_cross_records(raw, candidate_ids=set(candidates))
    recorded: set[tuple[str, str]] = set()
    referenced: set[str] = set()
    for record in records:
        candidate_ids = cast(list[str], record["candidate_ids"])
        key = cast(tuple[str, str], tuple(candidate_ids))
        if key not in required:
            raise _outside("cross-lane interaction", candidate_ids)
        if key in recorded:
            raise _invalid(f"duplicate cross-lane interaction: {candidate_ids}")
        recorded.add(key)
        referenced.update(candidate_ids)
        affected = cast(list[str], record["affected_section_ids"])
        expected_sections = sorted(
            set(candidates[key[0]].section_ids) | set(candidates[key[1]].section_ids)
        )
        if affected != expected_sections:
            raise _invalid(f"cross-lane interaction affected sections differ for {candidate_ids}")
    return records, recorded, referenced


def _canonical_cross_records(
    raw: object,
    *,
    candidate_ids: set[str],
) -> list[dict[str, object]]:
    records = _object_array(raw, owner="cross_lane_interactions")
    result: list[dict[str, object]] = []
    required_fields = {
        "candidate_ids",
        "affected_section_ids",
        "interaction_checked",
        "disposition",
    }
    for index, record in enumerate(records):
        owner = f"cross_lane_interactions[{index}]"
        _exact_fields(record, required_fields, owner=owner)
        participants = _string_list(
            record["candidate_ids"],
            owner=f"{owner}.candidate_ids",
            allow_empty=False,
        )
        if len(participants) != 2:
            raise _invalid(f"{owner}.candidate_ids must contain exactly two candidates")
        unknown = sorted(set(participants) - candidate_ids)
        if unknown:
            raise _invalid(f"{owner} references unknown candidates: {', '.join(unknown)}")
        record["candidate_ids"] = sorted(participants)
        record["affected_section_ids"] = _string_list(
            record["affected_section_ids"],
            owner=f"{owner}.affected_section_ids",
            allow_empty=False,
        )
        _required_string(record, "interaction_checked", owner)
        _required_string(record, "disposition", owner)
        result.append(record)
    return sorted(result, key=lambda record: cast(list[str], record["candidate_ids"]))


def _validate_adjacent_records(
    raw: object,
    *,
    candidates: Mapping[str, _Candidate],
    required: set[tuple[str, str]],
) -> tuple[list[dict[str, object]], set[tuple[str, str]], set[str]]:
    records = _canonical_adjacent_records(raw, candidate_ids=set(candidates))
    recorded: set[tuple[str, str]] = set()
    referenced: set[str] = set()
    for record in records:
        key = (
            cast(str, record["seed_candidate_id"]),
            cast(str, record["check_key"]),
        )
        if key not in required:
            raise _outside("adjacent-variant sweep", key)
        if key in recorded:
            raise _invalid(f"duplicate adjacent-variant sweep: {key[0]}")
        recorded.add(key)
        referenced.add(key[0])
        referenced.update(cast(list[str], record["resulting_candidate_ids"]))
    return records, recorded, referenced


def _canonical_adjacent_records(
    raw: object,
    *,
    candidate_ids: set[str],
) -> list[dict[str, object]]:
    records = _object_array(raw, owner="adjacent_variant_sweeps")
    result: list[dict[str, object]] = []
    required_fields = {
        "check_key",
        "seed_candidate_id",
        "query_evidence",
        "sites_checked",
        "resulting_candidate_ids",
    }
    for index, record in enumerate(records):
        owner = f"adjacent_variant_sweeps[{index}]"
        _exact_fields(record, required_fields, owner=owner)
        check_key = _required_string(record, "check_key", owner)
        if CHECK_KEY_RE.fullmatch(check_key) is None:
            raise _invalid(f"{owner}.check_key is invalid")
        seed = _required_string(record, "seed_candidate_id", owner)
        if seed not in candidate_ids:
            raise _invalid(f"{owner} references unknown candidate: {seed}")
        results = _string_list(
            record["resulting_candidate_ids"],
            owner=f"{owner}.resulting_candidate_ids",
            allow_empty=True,
        )
        unknown = sorted(set(results) - candidate_ids)
        if unknown:
            raise _invalid(f"{owner} references unknown candidates: {', '.join(unknown)}")
        query_evidence = _string_list(
            record["query_evidence"],
            owner=f"{owner}.query_evidence",
            allow_empty=True,
        )
        if not results and not query_evidence:
            raise ReviewEvidenceError(
                "missing_zero_result_query_evidence",
                f"zero-result sweep requires query evidence: {seed}",
            )
        record["query_evidence"] = query_evidence
        record["sites_checked"] = _string_list(
            record["sites_checked"],
            owner=f"{owner}.sites_checked",
            allow_empty=True,
        )
        record["resulting_candidate_ids"] = results
        result.append(record)
    return sorted(
        result,
        key=lambda record: (
            cast(str, record["seed_candidate_id"]),
            cast(str, record["check_key"]),
        ),
    )


def _validate_causal_records(
    raw: object,
    *,
    requirements: Mapping[str, _RepairRequirement],
) -> list[dict[str, object]]:
    records = _canonical_causal_records(raw)
    seen: set[str] = set()
    for record in records:
        finding_id = cast(str, record["prior_finding_id"])
        requirement = requirements.get(finding_id)
        if requirement is None:
            raise _outside("causal repair sweep", finding_id)
        if finding_id in seen:
            raise _invalid(f"duplicate causal repair sweep: {finding_id}")
        seen.add(finding_id)
        comparisons = (
            ("changed_section_ids", requirement.changed_section_ids),
            ("changed_contracts", requirement.changed_contracts),
            ("sites_checked", requirement.site_ids),
        )
        for field, expected in comparisons:
            if cast(list[str], record[field]) != list(expected):
                raise _invalid(f"causal repair sweep {field} differ for {finding_id}")
        if not requirement.site_ids and not cast(list[str], record["query_evidence"]):
            raise ReviewEvidenceError(
                "missing_zero_result_query_evidence",
                f"zero-result sweep requires query evidence: {finding_id}",
            )
    missing = sorted(set(requirements) - seen)
    if missing:
        raise ReviewEvidenceError(
            "unswept_repair_surface",
            "changed repair surfaces lack causal sweeps: " + ", ".join(missing),
        )
    return records


def _canonical_causal_records(raw: object) -> list[dict[str, object]]:
    records = _object_array(raw, owner="causal_repair_sweeps")
    result: list[dict[str, object]] = []
    required_fields = {
        "prior_finding_id",
        "changed_section_ids",
        "changed_contracts",
        "sites_checked",
        "query_evidence",
        "disposition",
    }
    for index, record in enumerate(records):
        owner = f"causal_repair_sweeps[{index}]"
        _exact_fields(record, required_fields, owner=owner)
        _required_string(record, "prior_finding_id", owner)
        for field in (
            "changed_section_ids",
            "changed_contracts",
            "sites_checked",
            "query_evidence",
        ):
            record[field] = _string_list(
                record[field],
                owner=f"{owner}.{field}",
                allow_empty=True,
            )
        _required_string(record, "disposition", owner)
        result.append(record)
    return sorted(result, key=lambda record: cast(str, record["prior_finding_id"]))


def _repair_requirements(
    context: Mapping[str, object] | None,
) -> dict[str, _RepairRequirement]:
    if context is None:
        return {}
    payload = canonical_json_object(context)
    resolutions = _object_array(
        payload.get("prior_finding_resolutions", []),
        owner="prior_round_context.prior_finding_resolutions",
    )
    repair_ids = {
        _required_string(record, "prior_finding_id", "prior finding resolution")
        for record in resolutions
        if record.get("decision") == "repair"
    }
    attestations = {
        _required_string(record, "prior_finding_id", "repair attestation"): record
        for record in _object_array(
            payload.get("repair_attestations", []),
            owner="prior_round_context.repair_attestations",
        )
    }
    inventory = payload.get("consumer_site_inventory", {})
    if not isinstance(inventory, Mapping):
        raise _invalid("prior_round_context.consumer_site_inventory must be an object")
    global_contracts = tuple(
        _string_list(
            inventory.get("changed_contracts", []),
            owner="consumer_site_inventory.changed_contracts",
            allow_empty=True,
        )
    )
    sites = _object_array(
        inventory.get("sites", []),
        owner="consumer_site_inventory.sites",
    )
    global_sites = tuple(
        sorted(_required_string(site, "site_id", "consumer site") for site in sites)
    )
    universe = payload.get("repair_universe")
    universe_requirements: dict[str, dict[str, object]] = {}
    if universe is not None:
        if not isinstance(universe, Mapping):
            raise _invalid("prior_round_context.repair_universe must be an object")
        for record in _object_array(
            universe.get("requirements", []),
            owner="repair_universe.requirements",
        ):
            finding_id = _required_string(record, "prior_finding_id", "repair requirement")
            universe_requirements[finding_id] = record
        if repair_ids and set(universe_requirements) != repair_ids:
            raise _invalid("repair_universe requirements disagree with repair resolutions")
        repair_ids.update(universe_requirements)
    result: dict[str, _RepairRequirement] = {}
    for finding_id in sorted(repair_ids):
        requirement = universe_requirements.get(finding_id)
        attestation = attestations.get(finding_id)
        if requirement is None and attestation is None:
            raise _invalid(f"repair context is missing changed surfaces for {finding_id}")
        source = requirement or cast(dict[str, object], attestation)
        result[finding_id] = _RepairRequirement(
            prior_finding_id=finding_id,
            changed_section_ids=tuple(
                _string_list(
                    source.get("changed_section_ids", []),
                    owner=f"repair requirement {finding_id}.changed_section_ids",
                    allow_empty=False,
                )
            ),
            changed_contracts=tuple(
                _string_list(
                    source.get("changed_contracts", list(global_contracts)),
                    owner=f"repair requirement {finding_id}.changed_contracts",
                    allow_empty=True,
                )
            ),
            site_ids=tuple(
                _string_list(
                    source.get("required_consumer_site_ids", list(global_sites)),
                    owner=f"repair requirement {finding_id}.required_consumer_site_ids",
                    allow_empty=True,
                )
            ),
        )
    return result


def _disposition_counts(records: Sequence[Mapping[str, object]]) -> dict[str, int]:
    return {
        "total": len(records),
        "emitted_findings": sum(record["disposition"] == "emitted_finding" for record in records),
        "dismissed": sum(record["disposition"] == "dismissed" for record in records),
    }


def _object_array(raw: object, *, owner: str) -> list[dict[str, object]]:
    if not isinstance(raw, list) or any(not isinstance(item, Mapping) for item in raw):
        raise _invalid(f"{owner} must be an array of objects")
    return [canonical_json_object(cast(Mapping[str, object], item)) for item in raw]


def _string_list(raw: object, *, owner: str, allow_empty: bool) -> list[str]:
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item for item in raw):
        raise _invalid(f"{owner} must be an array of non-empty strings")
    values = cast(list[str], raw)
    if len(values) != len(set(values)):
        raise _invalid(f"{owner} contains duplicates")
    if not allow_empty and not values:
        raise _invalid(f"{owner} must be non-empty")
    return sorted(values)


def _exact_fields(
    record: Mapping[str, object],
    expected: set[str],
    *,
    owner: str,
) -> None:
    if set(record) != expected:
        raise _invalid(f"{owner} must contain exactly {', '.join(sorted(expected))}")


def _required_string(record: Mapping[str, object], field: str, owner: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{owner}.{field} must be a non-empty string")
    return value


def _required_sha256(record: Mapping[str, object], field: str, owner: str) -> str:
    value = _required_string(record, field, owner)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise _invalid(f"{owner}.{field} must be lowercase SHA-256")
    return value


def _outside(owner: str, key: object) -> ReviewEvidenceError:
    return ReviewEvidenceError(
        "sweep_record_outside_universe",
        f"{owner} is outside the required universe: {key}",
    )


def _invalid(message: str) -> ReviewEvidenceError:
    return ReviewEvidenceError("invalid_sweep_records", message)


__all__ = [
    "SweepValidation",
    "validate_record_bundle",
    "validate_sweep_records",
]
