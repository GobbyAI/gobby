"""Small builders for valid plan-review coverage payloads in focused tests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from gobby.plans.consumer_sweep import CandidateSite, CandidateSiteInventory
from gobby.plans.digests import canonical_json_sha256
from gobby.plans.review_coverage import REVIEW_LANES
from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_models import (
    PlanReviewEvidence,
    SectionHash,
    validate_round_result,
)
from gobby.plans.review_sweep_scope import SweepRequirement, SweepScope
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._artifacts import TaskArtifactManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from tests.review_telemetry_helpers import enriched_telemetry
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


def ledger_finding(
    finding_id: str,
    *,
    check_key: str = "consumer-parity",
    category: str = "unhandled-edge",
    section_ids: Sequence[str] = ("1.1",),
    description: str = "Consumer misses the new field.",
    repair_scope: str = "existing_sections",
) -> dict[str, object]:
    primary, *participating = section_ids
    finding: dict[str, object] = {
        "finding_id": finding_id,
        "section_id": primary,
        "check_key": check_key,
        "severity": "major",
        "category": category,
        "location": "src/consumer.py:10",
        "description": description,
        "minimal_repair": "Read the new field.",
        "repair_scope": repair_scope,
        "prevention": "Audit every consumer.",
    }
    if participating:
        finding["participating_section_ids"] = list(participating)
    return finding


def ledger_round_result(
    *,
    findings: Sequence[Mapping[str, object]] = (),
    dispositions: Sequence[Mapping[str, object]] = (),
    counts: Mapping[str, int] | None = None,
) -> dict[str, object]:
    records = [dict(record) for record in dispositions]
    emitted = sum(record["disposition"] == "emitted_finding" for record in records)
    dismissed = sum(record["disposition"] == "dismissed" for record in records)
    canonical_counts = dict(
        counts
        or {
            "total": len(records),
            "emitted_findings": emitted,
            "dismissed": dismissed,
        }
    )
    attestation = coverage_attestation(
        evidence_id="evidence-1",
        manifest_entries=[{"source_section": "1.1"}],
    )
    lanes = attestation["lanes"]
    assert isinstance(lanes, list)
    assert isinstance(lanes[0], dict)
    lanes[0]["candidate_count"] = canonical_counts["total"]
    attestation["disposition_counts"] = canonical_counts
    record_bundle = attestation["record_bundle"]
    assert isinstance(record_bundle, dict)
    record_bundle["candidate_dispositions"] = records
    record_bundle["adjacent_variant_sweeps"] = [
        {
            "check_key": record["check_key"],
            "seed_candidate_id": record["candidate_id"],
            "query_evidence": [f"gcode search {record['candidate_id']}"],
            "sites_checked": ["src/consumer.py"],
            "resulting_candidate_ids": [],
        }
        for record in records
    ]
    unsigned = {key: value for key, value in attestation.items() if key != "attestation_digest"}
    attestation["attestation_digest"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return validate_round_result(
        {
            "verdict": "needs_review",
            "convergence_telemetry": enriched_telemetry(),
            "findings": [dict(finding) for finding in findings],
            "candidate_dispositions": records,
            "coverage_attestation": attestation,
        }
    )


def round_diff_finding(
    finding_id: str,
    *,
    severity: str = "blocking",
    participating: list[str] | None = None,
    causal: list[str] | None = None,
    causal_finding_id: str | None = None,
    introduced_in_round: int | None = None,
    principle: str | None = "Review the complete invariant.",
) -> dict[str, object]:
    finding: dict[str, object] = {
        "finding_id": finding_id,
        "section_id": "A",
        "check_key": f"check-{finding_id.lower()}",
        "severity": severity,
        "category": "unhandled-edge",
        "location": "§ A",
        "description": f"Finding {finding_id}",
        "minimal_repair": f"Fix {finding_id}",
        "repair_scope": "existing_sections",
        "prevention": f"Prevent {finding_id}",
    }
    if severity == "blocking":
        finding["failure_trace"] = {
            "preconditions": "The original plan is otherwise unchanged.",
            "action": f"The reviewer exercises {finding_id}.",
            "wrong_outcome": f"Finding {finding_id} remains reachable.",
            "violated_obligation": "The reviewed plan must close blocking failure paths.",
            "citation": [{"path": "plan.md", "sha256": "0" * 64}],
        }
    if principle is not None:
        finding["principle"] = principle
    if participating is not None:
        finding["participating_section_ids"] = participating
    if causal is not None:
        finding["causal_section_ids"] = causal
    if causal_finding_id is not None:
        finding["causal_finding_id"] = causal_finding_id
    if introduced_in_round is not None:
        finding["introduced_in_round"] = introduced_in_round
    return finding


def review_evidence_row(
    round_number: int,
    hashes: dict[str, str],
    findings: list[dict[str, object]],
    *,
    task_id: str = "task-lineage",
    stage: str = "planning",
    project_id: str = "project",
    plan_path: str = ".gobby/plans/review.md",
    finalized: bool = True,
    verdict: str = "needs_review",
    evidence_id: str | None = None,
) -> PlanReviewEvidence:
    created_at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=round_number)
    round_result: dict[str, object] = {
        "verdict": verdict,
        "findings": findings,
        "coverage_attestation": coverage_attestation(
            evidence_id=evidence_id or f"evidence-{round_number}-{task_id}",
            shadow_valid=verdict == "approved",
            manifest_entries=[{"source_section": "A"}] if verdict == "approved" else None,
        ),
        "convergence_telemetry": enriched_telemetry(),
    }
    if verdict == "approved":
        round_result["manifest_entries"] = [{"source_section": "A"}]
        round_result["routing_decisions"] = {}
    return PlanReviewEvidence(
        evidence_id=evidence_id or f"evidence-{round_number}-{task_id}",
        project_id=project_id,
        plan_path=plan_path,
        plan_hash=f"plan-{round_number}",
        section_manifest=tuple(
            SectionHash(section_id=section_id, section_hash=section_hash)
            for section_id, section_hash in hashes.items()
        ),
        snapshot=f"snapshot-{round_number}".encode(),
        round_number=round_number,
        session_id=None,
        task_id=task_id,
        stage=stage,
        dispatch_run_id=f"run-{round_number}",
        lease_expires_at=None,
        finalized_at=created_at if finalized else None,
        expired_at=None,
        round_result=round_result,
        approval_result=round_result if verdict == "approved" and finalized else None,
        approved_at=created_at if verdict == "approved" and finalized else None,
        lesson_mint_status="pending" if verdict == "approved" and finalized else None,
        lesson_mint_detail=None,
        manifest_digest=None,
        manifest_payload=None,
        manifest_state=None,
        manifest_result=None,
        manifest_applied_at=None,
        quality_ledger=None,
        repair_attestations=None,
        prior_round_context=None,
        created_at=created_at,
    )


class StubReviewLearningService:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> dict[str, object]:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("recorder unavailable")
        return {"lesson_id": "lesson-1"}


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
