"""Server-owned preparation integration for durable plan-review rounds."""

from __future__ import annotations

import subprocess  # Fixed local gcode argv. # nosec B404
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from gobby.agents.code_index import settle_indexed_value
from gobby.code_index.storage import CodeIndexStorage
from gobby.plans.consumer_sweep import (
    CandidateSiteInventory,
    derive_candidate_site_inventory,
)
from gobby.plans.review_evidence_io import (
    build_inter_round_diff,
    with_consumer_inventory_context,
)
from gobby.plans.review_evidence_models import (
    PlanReviewEvidence,
    ReviewEvidenceError,
    SectionHash,
)
from gobby.plans.review_findings import validate_plan_review_findings
from gobby.plans.review_repair import (
    RepairPreparation,
    repair_preparation_for_round,
)
from gobby.plans.review_sweep_scope import (
    SweepScope,
    canonicalize_sweep_scope,
    compute_scope_deltas,
    derive_sweep_scope,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.native_bin import resolve_native_bin_or_default


def derive_settled_sweep_inputs(
    *,
    db: HubDatabase,
    project_id: str,
    project_root: Path,
    prior_evidence: PlanReviewEvidence,
    current_snapshot: bytes,
    repair_finding_ids: Sequence[str],
) -> tuple[CandidateSiteInventory, SweepScope]:
    """Settle the index, then derive one inventory and sweep scope."""
    raw_findings = (
        prior_evidence.round_result.get("findings")
        if prior_evidence.round_result is not None
        else None
    )
    if not isinstance(raw_findings, list) or any(
        not isinstance(finding, Mapping) for finding in raw_findings
    ):
        raise ReviewEvidenceError(
            "invalid_repair_attestation",
            "prior round_result.findings must be an array of objects",
        )
    findings = validate_plan_review_findings(
        cast(list[Mapping[str, object]], raw_findings),
        evidence=prior_evidence,
    )
    storage = CodeIndexStorage(db)

    def read_last_indexed_at() -> str:
        stats = storage.get_project_stats(project_id)
        return stats.last_indexed_at.isoformat() if stats is not None else ""

    def derive() -> tuple[CandidateSiteInventory, SweepScope]:
        inventory = derive_candidate_site_inventory(
            diff=build_inter_round_diff(prior_evidence.snapshot, current_snapshot),
            project_id=project_id,
            storage=storage,
        )
        scope = derive_sweep_scope(
            prior_findings=findings,
            inventory=inventory,
            repair_finding_ids=repair_finding_ids,
        )
        return inventory, scope

    return settle_indexed_value(
        project_root,
        index_operation=lambda: _index_repository(project_root),
        read_last_indexed_at=read_last_indexed_at,
        derive=derive,
    )


def prepare_review_round_context(
    *,
    db: HubDatabase,
    project_id: str,
    project_root: Path,
    evidence_rows: Sequence[PlanReviewEvidence],
    round_number: int,
    current_sections: Sequence[SectionHash],
    current_snapshot: bytes,
    prior_finding_resolutions: Sequence[Mapping[str, object]] | None,
    repair_attestations: Sequence[Mapping[str, object]] | None,
    sweep_scope: Mapping[str, object] | None,
    sweep_scope_digest: str | None,
) -> RepairPreparation | None:
    """Validate submitted proof and attach current scope plus drift deltas."""
    submitted_scope = _submitted_scope(
        sweep_scope=sweep_scope,
        sweep_scope_digest=sweep_scope_digest,
    )
    base = repair_preparation_for_round(
        evidence_rows=evidence_rows,
        round_number=round_number,
        current_sections=current_sections,
        current_snapshot=current_snapshot,
        prior_finding_resolutions=prior_finding_resolutions,
        repair_attestations=repair_attestations,
        submitted_sweep_scope=submitted_scope,
    )
    if base is None:
        return None
    resolutions = base.prior_round_context["prior_finding_resolutions"]
    if not isinstance(resolutions, list):
        raise ReviewEvidenceError(
            "invalid_repair_context",
            "validated repair context omitted prior finding resolutions",
        )
    repair_ids = tuple(
        cast(str, resolution["prior_finding_id"])
        for resolution in cast(list[dict[str, object]], resolutions)
        if resolution["decision"] == "repair"
    )
    if not repair_ids:
        return base
    if submitted_scope is None:  # pragma: no cover - validated by repair preparation.
        raise RuntimeError("repair preparation omitted its submitted sweep scope")
    prior_evidence_id = base.prior_round_context["prior_evidence_id"]
    prior_evidence = next(
        (row for row in evidence_rows if row.evidence_id == prior_evidence_id),
        None,
    )
    if prior_evidence is None:
        raise ReviewEvidenceError(
            "invalid_repair_context",
            f"prior evidence row is missing: {prior_evidence_id}",
        )
    inventory, current_scope = derive_settled_sweep_inputs(
        db=db,
        project_id=project_id,
        project_root=project_root,
        prior_evidence=prior_evidence,
        current_snapshot=current_snapshot,
        repair_finding_ids=repair_ids,
    )
    required_scope_delta, inventory_churn = compute_scope_deltas(
        submitted=submitted_scope,
        current=current_scope,
    )
    validated = repair_preparation_for_round(
        evidence_rows=evidence_rows,
        round_number=round_number,
        current_sections=current_sections,
        current_snapshot=current_snapshot,
        prior_finding_resolutions=prior_finding_resolutions,
        repair_attestations=repair_attestations,
        submitted_sweep_scope=submitted_scope,
        current_sweep_scope=current_scope,
        required_scope_delta=required_scope_delta,
        inventory_churn=inventory_churn,
    )
    if validated is None:
        raise ReviewEvidenceError(
            "invalid_repair_context",
            "sweep scope requires a prior evidence row",
        )
    return RepairPreparation(
        repair_attestations=validated.repair_attestations,
        prior_round_context=with_consumer_inventory_context(
            validated.prior_round_context,
            inventory=inventory.to_dict(),
        ),
    )


def _submitted_scope(
    *,
    sweep_scope: Mapping[str, object] | None,
    sweep_scope_digest: str | None,
) -> SweepScope | None:
    if sweep_scope is None and sweep_scope_digest is None:
        return None
    if sweep_scope is None or sweep_scope_digest is None:
        raise ReviewEvidenceError(
            "missing_sweep_scope",
            "sweep_scope and sweep_scope_digest must be submitted together",
        )
    return canonicalize_sweep_scope(sweep_scope, digest=sweep_scope_digest)


def _index_repository(project_root: Path) -> None:
    binary = resolve_native_bin_or_default("gcode")
    try:
        completed = subprocess.run(  # Fixed argv plus trusted local path. # nosec B603
            [binary, "index", "--quiet", "--project", str(project_root)],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"gcode index failed: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"gcode index failed: {detail[:500]}")
