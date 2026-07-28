from __future__ import annotations

import textwrap
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from gobby.agents.code_index import IndexToken
from gobby.plans.consumer_sweep import CandidateSite, CandidateSiteInventory
from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_models import PlanReviewEvidence, ReviewEvidenceError
from gobby.plans.review_repair import RepairSweepRequirement, RepairUniverse
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from tests.review_coverage_helpers import coverage_attestation
from tests.review_telemetry_helpers import enriched_telemetry

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class _ReviewHarness:
    service: PlanReviewEvidenceService
    project_id: str
    plan_path: Path


@dataclass
class _SpawnProbe:
    evidence_ids: list[str] = field(default_factory=list)

    def spawn(self, evidence_id: str) -> None:
        self.evidence_ids.append(evidence_id)


@pytest.fixture
def review_harness(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _ReviewHarness:
    project = LocalProjectManager(temp_db).create(
        name="repair-gate-e2e",
        repo_path=str(tmp_path),
    )
    monkeypatch.setattr(
        "gobby.plans.review_evidence_preparation.derive_settled_repair_inputs",
        _settled_repair_inputs,
    )
    return _ReviewHarness(
        service=PlanReviewEvidenceService(temp_db),
        project_id=project.id,
        plan_path=_write_plan(tmp_path),
    )


def _write_plan(root: Path) -> Path:
    plan_dir = root / ".gobby" / "plans"
    plan_dir.mkdir(parents=True)
    path = plan_dir / "repair-gate-e2e.md"
    path.write_text(
        textwrap.dedent(
            """
            # Repair Gate End to End
            **Plan ID:** repair-gate-e2e

            ## P1 Phase
            `kind: framing`

            ### 1.1 Work
            `kind: deliverable`

            Target: `src/example.py`

            **Acceptance:**
            - 1.1.1 — Behavior exists. test: `tests/test_example.py`

            ## Task Mapping
            `kind: framing`

            Pending.

            ## V1 Plan Changelog
            `kind: verification`

            No rounds yet.

            ## M1 Task Manifest
            `kind: manifest`

            ```yaml
            - title: Implement example
              source_section: '1.1'
              covers:
                - 1.1.1
              category: code
              implementation_domain: backend
              priority: 2
              task_type: feature
              tdd: false
              labels:
                - covers:repair-gate-e2e:1.1:1.1.1
              description: Implement the example.
              validation_criteria: Example behavior is tested.
            ```
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return path


def _finding(finding_id: str, *, severity: str = "major") -> dict[str, object]:
    return {
        "finding_id": finding_id,
        "section_id": "1.1",
        "check_key": f"repair.{finding_id}",
        "severity": severity,
        "category": "unhandled-edge",
        "location": "1.1",
        "description": f"Repair {finding_id}.",
        "minimal_repair": f"Apply the minimal repair for {finding_id}.",
        "repair_scope": "existing_sections",
        "prevention": "Validate every consumer.",
        "principle": "Repairs require evidence.",
    }


def _resolution(finding_id: str) -> dict[str, object]:
    return {"prior_finding_id": finding_id, "decision": "repair"}


def _attestation(finding: dict[str, object]) -> dict[str, object]:
    finding_id = str(finding["finding_id"])
    return {
        "prior_finding_id": finding_id,
        "check_key": finding["check_key"],
        "changed_section_ids": ["1.1"],
        "accepted_resolution": finding["minimal_repair"],
        "deviation_from_minimal_repair": None,
        "changed_symbols": ["gobby.example.repaired_behavior"],
        "consumer_sites_swept": [f"consumer:{finding_id}"],
        "adjacent_variants_swept": [f"variant:{finding_id}"],
        "validation_evidence": ["pytest tests/test_example.py"],
        "deferred_sites": [],
        "repair_universe_digest": "a" * 64,
        "sweep_query_evidence": ["gcode usages gobby.example.repaired_behavior"],
        "repair_bundle_interactions": [],
    }


def _settled_repair_inputs(
    *,
    repair_finding_ids: Sequence[str],
    **_kwargs: object,
) -> tuple[IndexToken, CandidateSiteInventory, RepairUniverse]:
    site_ids = tuple(
        site_id
        for finding_id in repair_finding_ids
        for site_id in (f"consumer:{finding_id}", f"consumer:{finding_id}:secondary")
    )
    inventory = CandidateSiteInventory(
        changed_acceptance_item_ids=("1.1.1",),
        changed_targets=(),
        changed_symbols=("gobby.example.repaired_behavior",),
        changed_contracts=(),
        resolved_languages=("python",),
        unsupported_targets=(),
        sites=tuple(
            CandidateSite(
                site_id=site_id,
                path="src/example.py",
                source_kind="symbol_call",
                source_ref="gobby.example.repaired_behavior",
                status="resolved",
                language="python",
                section_ids=("1.1",),
            )
            for site_id in site_ids
        ),
    )
    universe = RepairUniverse(
        digest="a" * 64,
        candidate_sites=(),
        requirements=tuple(
            RepairSweepRequirement(
                prior_finding_id=finding_id,
                check_key=f"repair.{finding_id}",
                changed_section_ids=("1.1",),
                changed_contracts=(),
                changed_targets=(),
                required_consumer_site_ids=(
                    f"consumer:{finding_id}",
                    f"consumer:{finding_id}:secondary",
                ),
                adjacent_variant_ids=(f"variant:{finding_id}",),
                interaction_edge_ids=(),
            )
            for finding_id in repair_finding_ids
        ),
        interaction_edges=(),
    )
    token = IndexToken(
        repository_digest="a" * 64,
        last_indexed_at="2026-07-28T00:00:00+00:00",
        source_files=("src/example.py",),
    )
    return token, inventory, universe


def _new_task_id(harness: _ReviewHarness) -> str:
    task = LocalTaskManager(harness.service.db).create_task(
        project_id=harness.project_id,
        title="Repair gate end-to-end fixture",
        task_type="review_anchor",
        category="planning",
        validation_criteria="Round preparation remains attributable.",
    )
    return task.id


def _finalize_round_one(
    harness: _ReviewHarness,
    findings: list[dict[str, object]],
) -> PlanReviewEvidence:
    prepared = harness.service.prepare_plan_review_round(
        project_id=harness.project_id,
        plan_path=harness.plan_path,
        round_number=1,
        task_id=_new_task_id(harness),
        stage="planning",
    )
    return harness.service.finalize_plan_review_evidence(
        prepared.evidence_id,
        {
            "verdict": "needs_review",
            "findings": findings,
            "coverage_attestation": coverage_attestation(
                evidence_id=prepared.evidence_id,
                shadow_valid=False,
            ),
            "convergence_telemetry": enriched_telemetry(),
        },
    )


def _mark_plan_repaired(plan_path: Path) -> None:
    plan_path.write_text(
        plan_path.read_text(encoding="utf-8").replace(
            "Behavior exists.",
            "Behavior is repaired.",
        ),
        encoding="utf-8",
    )


def _prepare_round_two_then_spawn(
    harness: _ReviewHarness,
    *,
    resolutions: list[dict[str, object]],
    attestations: list[dict[str, object]],
    spawn_probe: _SpawnProbe,
) -> None:
    prepared = harness.service.prepare_plan_review_round(
        project_id=harness.project_id,
        plan_path=harness.plan_path,
        round_number=2,
        task_id=_new_task_id(harness),
        stage="planning",
        prior_finding_resolutions=resolutions,
        repair_attestations=attestations,
    )
    spawn_probe.spawn(prepared.evidence_id)


def test_omitted_consumer_refuses_round_two(review_harness: _ReviewHarness) -> None:
    first = _finding("consumer-one")
    omitted = _finding("consumer-two", severity="minor")
    _finalize_round_one(review_harness, [first, omitted])
    _mark_plan_repaired(review_harness.plan_path)
    spawn_probe = _SpawnProbe()

    with pytest.raises(ReviewEvidenceError, match="consumer-two") as refused:
        _prepare_round_two_then_spawn(
            review_harness,
            resolutions=[_resolution("consumer-one"), _resolution("consumer-two")],
            attestations=[_attestation(first)],
            spawn_probe=spawn_probe,
        )

    assert refused.value.code == "missing_repair_attestation"
    assert spawn_probe.evidence_ids == []


def test_omitted_resolution_refuses_round_two(review_harness: _ReviewHarness) -> None:
    first = _finding("finding-one")
    omitted = _finding("finding-two", severity="minor")
    _finalize_round_one(review_harness, [first, omitted])
    _mark_plan_repaired(review_harness.plan_path)
    spawn_probe = _SpawnProbe()

    with pytest.raises(ReviewEvidenceError, match="finding-two") as refused:
        _prepare_round_two_then_spawn(
            review_harness,
            resolutions=[_resolution("finding-one")],
            attestations=[_attestation(first)],
            spawn_probe=spawn_probe,
        )

    assert refused.value.code == "missing_finding_resolution"
    assert spawn_probe.evidence_ids == []


def test_subset_attestation_refuses_before_spawn(review_harness: _ReviewHarness) -> None:
    finding = _finding("subset")
    _finalize_round_one(review_harness, [finding])
    _mark_plan_repaired(review_harness.plan_path)
    spawn_probe = _SpawnProbe()

    with pytest.raises(
        ReviewEvidenceError,
        match="consumer:subset:secondary",
    ) as refused:
        _prepare_round_two_then_spawn(
            review_harness,
            resolutions=[_resolution("subset")],
            attestations=[_attestation(finding)],
            spawn_probe=spawn_probe,
        )

    assert refused.value.code == "repair_sweep_universe_mismatch"
    assert spawn_probe.evidence_ids == []
