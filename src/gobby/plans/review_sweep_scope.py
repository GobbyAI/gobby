"""Canonical sweep-scope graphs and deterministic inter-scope deltas."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import TypeVar, cast

from gobby.plans.consumer_sweep import CandidateSite, CandidateSiteInventory
from gobby.plans.review_evidence_models import ReviewEvidenceError, canonical_json_object

SWEEP_SCOPE_VERSION = 1

_SCOPE_FIELDS = frozenset(
    {
        "version",
        "candidate_sites",
        "requirements",
        "interaction_edges",
    }
)
_SITE_FIELDS = frozenset(
    {
        "site_id",
        "path",
        "source_kind",
        "source_ref",
        "status",
        "language",
        "section_ids",
    }
)
_REQUIREMENT_FIELDS = frozenset(
    {
        "prior_finding_id",
        "check_key",
        "changed_section_ids",
        "changed_contracts",
        "changed_targets",
        "required_consumer_site_ids",
        "adjacent_variant_ids",
        "interaction_edge_ids",
    }
)
_EDGE_FIELDS = frozenset(
    {
        "edge_id",
        "repair_ids",
        "shared_sections",
        "shared_check_keys",
        "shared_contracts",
        "shared_targets",
    }
)
_DELTA_KINDS = ("requirements", "candidate_sites", "interaction_edges")
_T = TypeVar("_T")


@dataclass(frozen=True)
class SweepInteractionEdge:
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
            "repair_ids": sorted(self.repair_ids),
            "shared_sections": sorted(self.shared_sections),
            "shared_check_keys": sorted(self.shared_check_keys),
            "shared_contracts": sorted(self.shared_contracts),
            "shared_targets": sorted(self.shared_targets),
        }


@dataclass(frozen=True)
class SweepRequirement:
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
            "changed_section_ids": sorted(self.changed_section_ids),
            "changed_contracts": sorted(self.changed_contracts),
            "changed_targets": sorted(self.changed_targets),
            "required_consumer_site_ids": sorted(self.required_consumer_site_ids),
            "adjacent_variant_ids": sorted(self.adjacent_variant_ids),
            "interaction_edge_ids": sorted(self.interaction_edge_ids),
        }


@dataclass(frozen=True)
class SweepScope:
    """Canonical repair sweep graph submitted with round attestations."""

    candidate_sites: tuple[CandidateSite, ...]
    requirements: tuple[SweepRequirement, ...]
    interaction_edges: tuple[SweepInteractionEdge, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "version": SWEEP_SCOPE_VERSION,
            "candidate_sites": [
                site.to_dict()
                for site in sorted(self.candidate_sites, key=lambda item: item.site_id)
            ],
            "requirements": [
                requirement.to_dict()
                for requirement in sorted(
                    self.requirements,
                    key=lambda item: item.prior_finding_id,
                )
            ],
            "interaction_edges": [
                edge.to_dict()
                for edge in sorted(self.interaction_edges, key=lambda item: item.edge_id)
            ],
        }

    @property
    def digest(self) -> str:
        return _canonical_digest(self.to_dict())


def derive_sweep_scope(
    *,
    prior_findings: Sequence[Mapping[str, object]],
    inventory: CandidateSiteInventory,
    repair_finding_ids: Sequence[str] | None = None,
) -> SweepScope:
    """Derive one deterministic sweep graph from server-owned findings and sites."""
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
    identities = {
        finding_id: (
            _required_string(findings[finding_id], "section_id", f"prior finding {finding_id}"),
            _required_string(findings[finding_id], "check_key", f"prior finding {finding_id}"),
        )
        for finding_id in repair_ids
    }
    interaction_edges = _derive_interaction_edges(identities, inventory)
    requirements = tuple(
        _derive_requirement(
            finding_id=finding_id,
            section_id=section_id,
            check_key=check_key,
            sites=sites,
            inventory=inventory,
            interaction_edges=interaction_edges,
        )
        for finding_id, (section_id, check_key) in sorted(identities.items())
    )
    return SweepScope(
        candidate_sites=sites,
        requirements=requirements,
        interaction_edges=interaction_edges,
    )


def canonicalize_sweep_scope(
    raw: Mapping[str, object],
    *,
    digest: str,
) -> SweepScope:
    """Parse one submitted graph and verify its canonical digest."""
    payload = canonical_json_object(raw)
    _require_exact_fields(payload, _SCOPE_FIELDS, owner="sweep_scope")
    version = payload["version"]
    if isinstance(version, bool) or version != SWEEP_SCOPE_VERSION:
        raise _invalid(f"sweep_scope.version must be {SWEEP_SCOPE_VERSION}")
    scope = SweepScope(
        candidate_sites=tuple(
            sorted(
                (
                    _canonical_site(record, owner=f"sweep_scope.candidate_sites[{index}]")
                    for index, record in enumerate(
                        _object_array(
                            payload["candidate_sites"], owner="sweep_scope.candidate_sites"
                        )
                    )
                ),
                key=lambda site: site.site_id,
            )
        ),
        requirements=tuple(
            sorted(
                (
                    _canonical_requirement(
                        record,
                        owner=f"sweep_scope.requirements[{index}]",
                    )
                    for index, record in enumerate(
                        _object_array(payload["requirements"], owner="sweep_scope.requirements")
                    )
                ),
                key=lambda requirement: requirement.prior_finding_id,
            )
        ),
        interaction_edges=tuple(
            sorted(
                (
                    _canonical_edge(record, owner=f"sweep_scope.interaction_edges[{index}]")
                    for index, record in enumerate(
                        _object_array(
                            payload["interaction_edges"],
                            owner="sweep_scope.interaction_edges",
                        )
                    )
                ),
                key=lambda edge: edge.edge_id,
            )
        ),
    )
    _validate_unique_ids(scope)
    _validate_scope_references(scope)
    if scope.digest != digest:
        raise ReviewEvidenceError(
            "sweep_scope_digest_mismatch",
            "sweep_scope_digest does not match the submitted sweep_scope",
            details={"expected_digest": scope.digest},
        )
    return scope


def compute_scope_deltas(
    *,
    submitted: SweepScope,
    current: SweepScope,
) -> tuple[dict[str, object], dict[str, object]]:
    """Partition graph drift into required-scope changes and inventory churn."""
    submitted_requirements = {
        requirement.prior_finding_id: requirement.to_dict()
        for requirement in submitted.requirements
    }
    current_requirements = {
        requirement.prior_finding_id: requirement.to_dict() for requirement in current.requirements
    }
    required_site_ids = _referenced_ids(
        (*submitted.requirements, *current.requirements),
        field="required_consumer_site_ids",
    )
    required_edge_ids = _referenced_ids(
        (*submitted.requirements, *current.requirements),
        field="interaction_edge_ids",
    )
    submitted_sites = {site.site_id: site.to_dict() for site in submitted.candidate_sites}
    current_sites = {site.site_id: site.to_dict() for site in current.candidate_sites}
    submitted_edges = {edge.edge_id: edge.to_dict() for edge in submitted.interaction_edges}
    current_edges = {edge.edge_id: edge.to_dict() for edge in current.interaction_edges}
    required_scope_delta: dict[str, object] = {
        "requirements": _record_delta(submitted_requirements, current_requirements),
        "candidate_sites": _record_delta(
            submitted_sites,
            current_sites,
            include_ids=required_site_ids,
        ),
        "interaction_edges": _record_delta(
            submitted_edges,
            current_edges,
            include_ids=required_edge_ids,
        ),
    }
    inventory_churn = empty_scope_delta()
    inventory_churn["candidate_sites"] = _record_delta(
        submitted_sites,
        current_sites,
        exclude_ids=required_site_ids,
    )
    return required_scope_delta, inventory_churn


def empty_scope_delta() -> dict[str, object]:
    """Return the canonical no-drift payload used by storage and migrations."""
    return {
        kind: {
            "added": [],
            "removed": [],
            "changed": [],
        }
        for kind in _DELTA_KINDS
    }


def _derive_interaction_edges(
    identities: Mapping[str, tuple[str, str]],
    inventory: CandidateSiteInventory,
) -> tuple[SweepInteractionEdge, ...]:
    edges: list[SweepInteractionEdge] = []
    for first_id, second_id in combinations(sorted(identities), 2):
        first_section, first_check_key = identities[first_id]
        second_section, second_check_key = identities[second_id]
        shared_sections = (first_section,) if first_section == second_section else ()
        shared_check_keys = (first_check_key,) if first_check_key == second_check_key else ()
        shared_contracts = tuple(
            sorted(
                set(inventory.contracts_by_section.get(first_section, ()))
                & set(inventory.contracts_by_section.get(second_section, ()))
            )
        )
        shared_targets = tuple(
            sorted(
                set(inventory.targets_by_section.get(first_section, ()))
                & set(inventory.targets_by_section.get(second_section, ()))
            )
        )
        if not (shared_sections or shared_check_keys or shared_contracts or shared_targets):
            continue
        edge_payload: dict[str, object] = {
            "repair_ids": [first_id, second_id],
            "shared_sections": list(shared_sections),
            "shared_check_keys": list(shared_check_keys),
            "shared_contracts": list(shared_contracts),
            "shared_targets": list(shared_targets),
        }
        edges.append(
            SweepInteractionEdge(
                edge_id=_canonical_digest(edge_payload),
                repair_ids=(first_id, second_id),
                shared_sections=shared_sections,
                shared_check_keys=shared_check_keys,
                shared_contracts=shared_contracts,
                shared_targets=shared_targets,
            )
        )
    return tuple(edges)


def _derive_requirement(
    *,
    finding_id: str,
    section_id: str,
    check_key: str,
    sites: Sequence[CandidateSite],
    inventory: CandidateSiteInventory,
    interaction_edges: Sequence[SweepInteractionEdge],
) -> SweepRequirement:
    section_site_ids = tuple(site.site_id for site in sites if section_id in site.section_ids)
    return SweepRequirement(
        prior_finding_id=finding_id,
        check_key=check_key,
        changed_section_ids=(section_id,),
        changed_contracts=inventory.contracts_by_section.get(section_id, ()),
        changed_targets=inventory.targets_by_section.get(section_id, ()),
        required_consumer_site_ids=section_site_ids,
        adjacent_variant_ids=tuple(
            _canonical_digest(
                {
                    "prior_finding_id": finding_id,
                    "check_key": check_key,
                    "site_id": site_id,
                }
            )
            for site_id in section_site_ids
        ),
        interaction_edge_ids=tuple(
            edge.edge_id for edge in interaction_edges if finding_id in edge.repair_ids
        ),
    )


def _canonical_site(raw: Mapping[str, object], *, owner: str) -> CandidateSite:
    _require_exact_fields(raw, _SITE_FIELDS, owner=owner)
    return CandidateSite(
        site_id=_required_string(raw, "site_id", owner),
        path=_required_string(raw, "path", owner),
        source_kind=_required_string(raw, "source_kind", owner),
        source_ref=_required_string(raw, "source_ref", owner),
        status=_required_string(raw, "status", owner),
        language=_required_string(raw, "language", owner),
        section_ids=_string_tuple(raw["section_ids"], owner=f"{owner}.section_ids"),
    )


def _canonical_requirement(raw: Mapping[str, object], *, owner: str) -> SweepRequirement:
    _require_exact_fields(raw, _REQUIREMENT_FIELDS, owner=owner)
    return SweepRequirement(
        prior_finding_id=_required_string(raw, "prior_finding_id", owner),
        check_key=_required_string(raw, "check_key", owner),
        changed_section_ids=_string_tuple(
            raw["changed_section_ids"],
            owner=f"{owner}.changed_section_ids",
        ),
        changed_contracts=_string_tuple(
            raw["changed_contracts"],
            owner=f"{owner}.changed_contracts",
        ),
        changed_targets=_string_tuple(
            raw["changed_targets"],
            owner=f"{owner}.changed_targets",
        ),
        required_consumer_site_ids=_string_tuple(
            raw["required_consumer_site_ids"],
            owner=f"{owner}.required_consumer_site_ids",
        ),
        adjacent_variant_ids=_string_tuple(
            raw["adjacent_variant_ids"],
            owner=f"{owner}.adjacent_variant_ids",
        ),
        interaction_edge_ids=_string_tuple(
            raw["interaction_edge_ids"],
            owner=f"{owner}.interaction_edge_ids",
        ),
    )


def _canonical_edge(raw: Mapping[str, object], *, owner: str) -> SweepInteractionEdge:
    _require_exact_fields(raw, _EDGE_FIELDS, owner=owner)
    repair_ids = _string_tuple(raw["repair_ids"], owner=f"{owner}.repair_ids")
    if len(repair_ids) != 2:
        raise _invalid(f"{owner}.repair_ids must contain exactly two IDs")
    return SweepInteractionEdge(
        edge_id=_required_string(raw, "edge_id", owner),
        repair_ids=repair_ids,
        shared_sections=_string_tuple(raw["shared_sections"], owner=f"{owner}.shared_sections"),
        shared_check_keys=_string_tuple(
            raw["shared_check_keys"],
            owner=f"{owner}.shared_check_keys",
        ),
        shared_contracts=_string_tuple(
            raw["shared_contracts"],
            owner=f"{owner}.shared_contracts",
        ),
        shared_targets=_string_tuple(raw["shared_targets"], owner=f"{owner}.shared_targets"),
    )


def _validate_unique_ids(scope: SweepScope) -> None:
    _unique(scope.candidate_sites, key=lambda item: item.site_id, owner="candidate site")
    _unique(
        scope.requirements,
        key=lambda item: item.prior_finding_id,
        owner="sweep requirement",
    )
    _unique(scope.interaction_edges, key=lambda item: item.edge_id, owner="interaction edge")


def _validate_scope_references(scope: SweepScope) -> None:
    site_ids = {site.site_id for site in scope.candidate_sites}
    edge_map = {edge.edge_id: edge for edge in scope.interaction_edges}
    requirement_map = {
        requirement.prior_finding_id: requirement for requirement in scope.requirements
    }
    referenced_edges: set[str] = set()
    for requirement in scope.requirements:
        missing_sites = sorted(set(requirement.required_consumer_site_ids) - site_ids)
        missing_edges = sorted(set(requirement.interaction_edge_ids) - set(edge_map))
        if missing_sites or missing_edges:
            raise _invalid(
                f"sweep requirement {requirement.prior_finding_id} has missing graph references: "
                f"sites={missing_sites}, edges={missing_edges}"
            )
        referenced_edges.update(requirement.interaction_edge_ids)
    if referenced_edges != set(edge_map):
        raise _invalid("sweep_scope has interaction edges outside its requirements")
    for edge in scope.interaction_edges:
        if any(repair_id not in requirement_map for repair_id in edge.repair_ids):
            raise _invalid(f"interaction edge {edge.edge_id} references an unknown repair")
        for repair_id in edge.repair_ids:
            if edge.edge_id not in requirement_map[repair_id].interaction_edge_ids:
                raise _invalid(f"interaction edge {edge.edge_id} is not referenced by {repair_id}")


def _record_delta(
    submitted: Mapping[str, dict[str, object]],
    current: Mapping[str, dict[str, object]],
    *,
    include_ids: set[str] | None = None,
    exclude_ids: set[str] | None = None,
) -> dict[str, object]:
    ids = set(submitted) | set(current)
    if include_ids is not None:
        ids &= include_ids
    if exclude_ids is not None:
        ids -= exclude_ids
    added = [current[record_id] for record_id in sorted(ids - set(submitted))]
    removed = [submitted[record_id] for record_id in sorted(ids - set(current))]
    changed = [
        {
            "id": record_id,
            "submitted": submitted[record_id],
            "current": current[record_id],
        }
        for record_id in sorted(ids & set(submitted) & set(current))
        if submitted[record_id] != current[record_id]
    ]
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
    }


def _referenced_ids(
    requirements: Sequence[SweepRequirement],
    *,
    field: str,
) -> set[str]:
    return {
        value
        for requirement in requirements
        for value in cast(tuple[str, ...], getattr(requirement, field))
    }


def _finding_identity_map(
    findings: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for index, raw in enumerate(findings):
        finding = canonical_json_object(raw)
        finding_id = _required_string(finding, "finding_id", f"prior_findings[{index}]")
        if finding_id in result:
            raise _invalid(f"duplicate prior finding ID: {finding_id}")
        result[finding_id] = finding
    return result


def _require_exact_fields(
    raw: Mapping[str, object],
    fields: frozenset[str],
    *,
    owner: str,
) -> None:
    missing = sorted(fields - set(raw))
    unknown = sorted(set(raw) - fields)
    if missing or unknown:
        raise _invalid(f"{owner} fields disagree with schema: missing={missing}, unknown={unknown}")


def _object_array(raw: object, *, owner: str) -> list[dict[str, object]]:
    if not isinstance(raw, list) or any(not isinstance(item, Mapping) for item in raw):
        raise _invalid(f"{owner} must be an array of objects")
    return [canonical_json_object(cast(Mapping[str, object], item)) for item in raw]


def _string_tuple(raw: object, *, owner: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item for item in raw):
        raise _invalid(f"{owner} must be an array of non-empty strings")
    values = tuple(sorted(cast(list[str], raw)))
    if len(values) != len(set(values)):
        raise _invalid(f"{owner} must contain unique values")
    return values


def _required_string(payload: Mapping[str, object], field: str, owner: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise _invalid(f"{owner}.{field} must be a non-empty string")
    return value


def _unique(
    records: Sequence[_T],
    *,
    key: Callable[[_T], str],
    owner: str,
) -> None:
    values = [key(record) for record in records]
    if len(values) != len(set(values)):
        raise _invalid(f"{owner} IDs must be unique")


def _canonical_digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _invalid(message: str) -> ReviewEvidenceError:
    return ReviewEvidenceError("invalid_sweep_scope", message)
