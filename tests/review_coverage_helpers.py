"""Small builders for valid plan-review coverage payloads in focused tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from gobby.plans.consumer_sweep import CandidateSite, CandidateSiteInventory
from gobby.plans.digests import canonical_json_sha256
from gobby.plans.review_coverage import REVIEW_LANES
from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_models import PlanReviewEvidence
from gobby.plans.review_sweep_scope import SweepRequirement, SweepScope
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._artifacts import TaskArtifactManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from tests.storage.tasks._stage_test_helpers import set_stage_state


@dataclass(frozen=True)
class StageReviewSetup:
    db: HubDatabase
    manager: LocalTaskManager
    evidence: PlanReviewEvidenceService
    runs: LocalAgentRunManager
    sessions: SessionManager
    project_id: str
    task_id: str
    plan_path: Path
    plan_relative_path: str
    parent_session_id: str


@pytest.fixture
def stage_review_setup(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> StageReviewSetup:
    monkeypatch.setattr(
        "gobby.plans.review_evidence_preparation.derive_settled_sweep_inputs",
        settled_repair_inputs,
    )
    project = LocalProjectManager(temp_db).create(
        name="stage-review-findings",
        repo_path=str(tmp_path),
    )
    sessions = SessionManager(temp_db)
    parent = sessions.register(
        external_id="stage-review-launcher",
        machine_id="test-machine",
        source="codex",
        project_id=project.id,
    )
    plan_path = tmp_path / ".gobby" / "plans" / "review.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        "\n".join(
            [
                "# Review",
                "**Plan ID:** review",
                "",
                "## P1 Foundation",
                "`kind: framing`",
                "",
                "### 1.1 Implement",
                "`kind: deliverable`",
                "",
                "Target: `src/example.py`",
                "",
                "**Acceptance:**",
                "- 1.1.1 — Implemented. test: `tests/test_example.py`",
                "",
                "## Task Mapping",
                "`kind: framing`",
                "",
                "Pending.",
                "",
                "## V1 Plan Changelog",
                "`kind: verification`",
                "",
                "No rounds.",
                "",
                "## M1 Task Manifest",
                "`kind: manifest`",
                "",
                "```yaml",
                "- title: Implement",
                "  source_section: '1.1'",
                "  covers: [1.1.1]",
                "  category: code",
                "  implementation_domain: backend",
                "  priority: 2",
                "  task_type: feature",
                "  tdd: false",
                "  labels: [covers:review:1.1:1.1.1]",
                "  description: Implement.",
                "  validation_criteria: Tested.",
                "```",
                "",
            ]
        ),
        encoding="utf-8",
    )
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project.id,
        "Plan review anchor",
        task_type="review_anchor",
        category="planning",
        isolation="none",
        validation_criteria="Test task completion is observable.",
    )
    manager.initialize_task_manifest(task.id, stage_names=["planning"])
    set_stage_state(temp_db, task.id, "planning", "needs_review")
    TaskArtifactManager(temp_db).set_artifacts_atomic(
        task.id,
        plan_file_path=str(plan_path),
    )
    return StageReviewSetup(
        db=temp_db,
        manager=manager,
        evidence=PlanReviewEvidenceService(temp_db),
        runs=LocalAgentRunManager(temp_db),
        sessions=sessions,
        project_id=project.id,
        task_id=task.id,
        plan_path=plan_path,
        plan_relative_path=".gobby/plans/review.md",
        parent_session_id=parent.id,
    )


def review_findings() -> list[dict[str, object]]:
    return [
        {
            "finding_id": "F1",
            "section_id": "1.1",
            "check_key": "failure-atomicity",
            "severity": "blocking",
            "category": "unhandled-edge",
            "location": "§ 1.1",
            "description": "The failure path can leave partial state.",
            "minimal_repair": "Specify rollback before retry.",
            "repair_scope": "existing_sections",
            "root_cause": "Only the successful write was modeled.",
            "prevention": "Walk every write failure boundary.",
            "participating_section_ids": ["1.1"],
            "failure_trace": {
                "preconditions": "The first durable write succeeds.",
                "action": "The second durable write fails.",
                "wrong_outcome": "The first write remains visible.",
                "violated_obligation": "The operation must commit atomically.",
                "citation": [{"path": "plan.md", "sha256": "0" * 64}],
            },
        },
        {
            "finding_id": "F2",
            "section_id": "1.1",
            "check_key": "cross-round-causality",
            "severity": "blocking",
            "category": "bad-sequencing",
            "location": "§ 1.1",
            "description": "The prior fix created a conflicting order.",
            "minimal_repair": "Restore the prerequisite before the new write.",
            "repair_scope": "existing_sections",
            "principle": "Causal fixes preserve established prerequisites.",
            "prevention": "Recheck all sections changed by a causal fix.",
            "introduced_in_round": 1,
            "causal_finding_id": "F1",
            "causal_section_ids": ["1.1"],
            "failure_trace": {
                "preconditions": "The prior repair is present.",
                "action": "The new write executes before its prerequisite.",
                "wrong_outcome": "The prerequisite is observed too late.",
                "violated_obligation": "Causal repairs must preserve prerequisite order.",
                "citation": [{"path": "plan.md", "sha256": "0" * 64}],
            },
        },
    ]


def settled_repair_inputs(
    *,
    prior_evidence: PlanReviewEvidence,
    repair_finding_ids: list[str] | tuple[str, ...],
    **_kwargs: object,
) -> tuple[CandidateSiteInventory, SweepScope]:
    assert prior_evidence.round_result is not None
    findings = cast(list[dict[str, object]], prior_evidence.round_result["findings"])
    finding_map = {cast(str, finding["finding_id"]): finding for finding in findings}
    consumer_site = "src/example.py:consumer"
    adjacent_site = "src/example.py:retry"
    sites = tuple(
        CandidateSite(
            site_id=site_id,
            path="src/example.py",
            source_kind="symbol_call",
            source_ref="gobby.example.rollback",
            status="resolved",
            language="python",
            section_ids=("1.1",),
        )
        for site_id in (consumer_site, adjacent_site)
    )
    inventory = CandidateSiteInventory(
        changed_acceptance_item_ids=("1.1.1",),
        changed_targets=("src/example.py",),
        changed_symbols=("gobby.example.rollback",),
        changed_contracts=(),
        targets_by_section={"1.1": ("src/example.py",)},
        contracts_by_section={},
        resolved_languages=("python",),
        unsupported_targets=(),
        sites=sites,
    )
    universe = SweepScope(
        candidate_sites=sites,
        requirements=tuple(
            SweepRequirement(
                prior_finding_id=finding_id,
                check_key=cast(str, finding_map[finding_id]["check_key"]),
                changed_section_ids=(cast(str, finding_map[finding_id]["section_id"]),),
                changed_contracts=(),
                changed_targets=("src/example.py",),
                required_consumer_site_ids=(consumer_site,),
                adjacent_variant_ids=(adjacent_site,),
                interaction_edge_ids=(),
            )
            for finding_id in repair_finding_ids
        ),
        interaction_edges=(),
    )
    return inventory, universe


def prepare_bound_review(
    setup: StageReviewSetup,
    *,
    round_number: int = 1,
    task_id: str | None = None,
    plan_path: Path | None = None,
) -> tuple[str, str]:
    target_task_id = task_id or setup.task_id
    prepared = setup.evidence.prepare_plan_review_round(
        project_id=setup.project_id,
        plan_path=plan_path or setup.plan_path,
        round_number=round_number,
        task_id=target_task_id,
        stage="planning",
    )
    run = setup.runs.create(
        parent_session_id=setup.parent_session_id,
        provider="codex",
        prompt="review",
        task_id=target_task_id,
    )
    setup.evidence.bind_evidence_run(prepared.evidence_id, run.id)
    hold_dispatch_mutex(setup, task_id=target_task_id, run_id=run.id)
    return prepared.evidence_id, run.id


def hold_dispatch_mutex(
    setup: StageReviewSetup,
    *,
    task_id: str,
    run_id: str,
) -> None:
    mutexes = TaskDispatchMutexManager(setup.db)
    mutexes.ensure_table()
    assert mutexes.acquire_mutex(
        task_id,
        holder=f"test:{run_id}",
        kind="spawn_agent",
        ttl_seconds=600,
        run_id=run_id,
    )


def manifest_digest(entries: Sequence[Mapping[str, object]]) -> str:
    """Return the canonical digest used by the review manifest service."""
    return canonical_json_sha256(list(entries))


def coverage_attestation(
    *,
    evidence_id: str = "test-evidence",
    manifest_entries: Sequence[Mapping[str, object]] | None = None,
    shadow_valid: bool = True,
) -> dict[str, object]:
    """Build a canonical signed attestation for tests outside coverage validation."""
    shadow: dict[str, object]
    if shadow_valid:
        entries = list(manifest_entries or [])
        shadow = {
            "status": "valid",
            "manifest_digest": manifest_digest(entries),
            "entry_count": len(entries),
        }
    else:
        shadow = {
            "status": "invalid",
            "diagnostics": [{"code": "test_shadow_failure", "message": "invalid"}],
        }
    payload: dict[str, object] = {
        "version": 1,
        "evidence_id": evidence_id,
        "lanes": [
            {"lane_id": lane_id, "status": "completed", "candidate_count": 0}
            for lane_id in REVIEW_LANES
        ],
        "source_digest": "0" * 64,
        "disposition_counts": {
            "total": 0,
            "emitted_findings": 0,
            "dismissed": 0,
        },
        "cross_lane_interaction_complete": True,
        "adjacent_variant_complete": True,
        "record_bundle": {
            "cross_lane_interactions": [],
            "adjacent_variant_sweeps": [],
            "causal_repair_sweeps": [],
            "candidate_dispositions": [],
        },
        "shadow_manifest_status": shadow,
    }
    payload["attestation_digest"] = canonical_json_sha256(payload)
    return payload
